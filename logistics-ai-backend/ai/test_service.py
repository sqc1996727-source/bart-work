from __future__ import annotations

import json
import os
import tempfile
import unittest
from typing import Any, Mapping, Sequence

from ai.service import (
    AIServiceError, ChatRequest, ConversationStore, Evidence, ModelConfig,
    OpenAICompatibleClient, build_service,
)
from ai.handler import handle_chat_request, handle_get_conversation, handle_list_conversations


class Context:
    def __init__(self):
        self.calls = []

    def retrieve(self, query: str, *, tenant_id: str, principal_id: str, memory_scope: str):
        self.calls.append((tenant_id, principal_id, memory_scope, query))
        return [Evidence("waybill:123", "到站：杭州", "waybills", {"record_id": "123"})]


class Tools:
    supports_idempotency = True

    def __init__(self):
        self.calls = []

    def list_tools(self, *, tenant_id, principal_id):
        return [{"type": "function", "function": {"name": "get_waybill", "description": "查询运单", "parameters": {"type": "object", "properties": {"waybill_id": {"type": "string"}}, "required": ["waybill_id"]}}}]

    def execute(self, name, arguments, *, tenant_id, principal_id, idempotency_key):
        self.calls.append((name, dict(arguments), tenant_id, principal_id, idempotency_key))
        return {"waybill_id": arguments["waybill_id"], "status": "已签收"}


class Model:
    def __init__(self, answers):
        self.answers = list(answers)
        self.requests = []

    def complete(self, messages: Sequence[Mapping[str, Any]], tools: Sequence[Mapping[str, Any]]):
        self.requests.append((list(messages), list(tools)))
        return self.answers.pop(0)


class AIServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.context = Context()
        self.tools = Tools()

    def tearDown(self):
        self.tmp.cleanup()

    def service(self, model):
        return build_service(self.tmp.name, context_provider=self.context, tool_executor=self.tools, model_client=model)

    def request(self, **kwargs):
        values = {"tenant_id": "company-a", "principal_id": "user-a", "message": "查运单123"}
        values.update(kwargs)
        return ChatRequest(**values)

    def test_empty_final_retries_answer_without_repeating_tools(self):
        call={"id":"one","type":"function","function":{"name":"get_waybill","arguments":"{\"waybill_id\":\"123\"}"}}
        model=Model([
            {"content":None,"tool_calls":[call],"usage":{"prompt_tokens":2,"completion_tokens":1,"total_tokens":3}},
            {"content":"","usage":{"prompt_tokens":3,"completion_tokens":2,"total_tokens":5}},
            {"content":"运单已签收。","usage":{"prompt_tokens":4,"completion_tokens":1,"total_tokens":5}},
        ])
        result=self.service(model).chat(self.request())
        self.assertEqual(result.reply,"运单已签收。")
        self.assertEqual(len(self.tools.calls),1)
        self.assertEqual(model.requests[-1][1],[])
        self.assertEqual(result.model_usage['total_tokens'],13)
        self.assertEqual(result.model_usage['calls'],3)

    def test_empty_recovery_fails_without_extra_tool_calls(self):
        model=Model([{"content":""},{"content":"","tool_calls":[{"id":"unsafe","type":"function","function":{"name":"get_waybill","arguments":"{}"}}]}])
        with self.assertRaises(AIServiceError) as caught:self.service(model).chat(self.request())
        self.assertEqual(caught.exception.code,'MODEL_EMPTY_RESPONSE')
        self.assertEqual(len(model.requests),2)
        self.assertEqual(model.requests[-1][1],[])
        self.assertEqual(self.tools.calls,[])

    def test_database_connection_closes_after_context(self):
        import sqlite3
        svc=self.service(Model([{"content":"好的"}]))
        with svc.store._connect() as con:
            con.execute('SELECT 1').fetchone()
        with self.assertRaises(sqlite3.ProgrammingError):con.execute('SELECT 1')
        with self.assertRaises(RuntimeError):
            with svc.store._connect() as failed:
                raise RuntimeError('synthetic failure')
        with self.assertRaises(sqlite3.ProgrammingError):failed.execute('SELECT 1')

    def test_unconfigured_model_is_clear_and_never_fabricates(self):
        env = {"JL_AI_BASE_URL": "", "JL_AI_MODEL": "", "JL_AI_API_KEY": ""}
        with self.assertRaises(AIServiceError) as caught:
            ModelConfig.from_env(env)
        self.assertEqual(caught.exception.code, "MODEL_NOT_CONFIGURED")
        self.assertNotIn("test-secret", caught.exception.message)
        svc = build_service(self.tmp.name, context_provider=self.context, tool_executor=self.tools)
        with self.assertRaises(AIServiceError) as caught:
            svc.chat(self.request())
        self.assertEqual(caught.exception.code, "MODEL_NOT_CONFIGURED")

    def test_http_adapter_uses_session_identity_not_body_identity(self):
        svc = self.service(Model([{"content": "来自本会话。"}]))
        status, body = handle_chat_request(svc, {
            "message": "查运单123", "tenant_id": "attacker-tenant", "principal_id": "attacker-user",
        }, {"id": "user-a", "tenant_id": "company-a"})
        self.assertEqual(status, 200)
        self.assertEqual(self.context.calls[-1][:2], ("company-a", "user-a"))
        no_identity = handle_chat_request(svc, {"message": "hi"}, None)
        self.assertEqual(no_identity[0], 401)

    def test_http_adapter_maps_missing_model_to_503(self):
        svc = build_service(self.tmp.name, context_provider=self.context, tool_executor=self.tools)
        status, body = handle_chat_request(svc, {"message": "hi"}, {"id": "user-a", "tenant_id": "company-a"})
        self.assertEqual(status, 503)
        self.assertEqual(body["error"]["code"], "MODEL_NOT_CONFIGURED")

    def test_conversation_list_and_history_are_scoped_to_session_identity(self):
        model = Model([
            {"content": "第一答复"}, {"content": "第二答复"},
            {"content": "个人答复"}, {"content": "另一用户答复"},
        ])
        svc = self.service(model)
        session_a = {"id": "user-a", "tenant_id": "company-a"}
        session_b = {"id": "user-b", "tenant_id": "company-a"}
        first = svc.chat(self.request(message="第一问"))
        svc.chat(self.request(message="第二问", conversation_id=first.conversation_id))
        personal = svc.chat(self.request(message="个人问题", memory_scope="personal"))
        svc.chat(self.request(message="其他用户问题", principal_id="user-b"))

        status, listing = handle_list_conversations(svc, session_a, {
            "scope": "enterprise", "limit": "10", "offset": "0", "user_id": "user-b",
        })
        self.assertEqual(status, 200)
        self.assertEqual(len(listing["items"]), 1)
        self.assertEqual(listing["items"][0]["conversation_id"], first.conversation_id)
        status, personal_listing = handle_list_conversations(svc, session_a, {"scope": "personal"})
        self.assertEqual(status, 200)
        self.assertEqual([x["conversation_id"] for x in personal_listing["items"]], [personal.conversation_id])

        status, first_page = handle_get_conversation(svc, first.conversation_id, session_a, {"limit": "2"})
        self.assertEqual(status, 200)
        history = first_page["conversation"]
        self.assertEqual([m["content"] for m in history["messages"]], ["第二问", "第二答复"])
        self.assertIsNotNone(history["next_before_message_id"])
        status, older_page = handle_get_conversation(svc, first.conversation_id, session_a, {
            "limit": "2", "before_message_id": str(history["next_before_message_id"]),
        })
        self.assertEqual(status, 200)
        older_messages = older_page["conversation"]["messages"]
        self.assertEqual([m["content"] for m in older_messages], ["第一问", "第一答复"])
        self.assertLess(older_messages[-1]["id"], history["messages"][0]["id"])

        status, not_owner = handle_get_conversation(svc, first.conversation_id, session_b)
        self.assertEqual(status, 404)
        self.assertEqual(not_owner["error"]["code"], "CONVERSATION_NOT_FOUND")

    def test_history_survives_service_recreation_and_unknown_ids_do_not_leak(self):
        first_service = self.service(Model([{"content": "跨浏览器可恢复"}]))
        conversation_id = first_service.chat(self.request(message="保留这段记录")).conversation_id
        restarted_service = build_service(
            self.tmp.name, context_provider=self.context, tool_executor=self.tools,
            model_client=Model([]),
        )
        status, response = handle_get_conversation(
            restarted_service, conversation_id,
            {"id": "user-a", "tenant_id": "company-a"},
        )
        self.assertEqual(status, 200)
        self.assertEqual([m["content"] for m in response["conversation"]["messages"]], ["保留这段记录", "跨浏览器可恢复"])
        status, missing = handle_get_conversation(
            restarted_service, conversation_id,
            {"id": "user-a", "tenant_id": "company-b"},
        )
        self.assertEqual(status, 404)
        self.assertEqual(missing["error"]["code"], "CONVERSATION_NOT_FOUND")

    def test_conversation_pagination_parameters_are_bounded(self):
        svc = self.service(Model([]))
        status, body = handle_list_conversations(svc, {"id": "user-a", "tenant_id": "company-a"}, {"limit": "1000"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "INVALID_PAGINATION")
        status, body = handle_get_conversation(svc, "some-id", {"id": "user-a", "tenant_id": "company-a"}, {"before_message_id": "0"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "INVALID_PAGINATION")

    def test_conversation_cannot_cross_user_tenant_or_memory_scope(self):
        model = Model([{"content": "收到。"}])
        svc = self.service(model)
        cid = svc.chat(self.request()).conversation_id
        with self.assertRaises(AIServiceError) as caught:
            svc.chat(self.request(conversation_id=cid, principal_id="user-b"))
        self.assertEqual(caught.exception.code, "CONVERSATION_SCOPE_MISMATCH")
        with self.assertRaises(AIServiceError):
            svc.chat(self.request(conversation_id=cid, memory_scope="personal"))

    def test_enterprise_and_personal_memories_persist_in_distinct_scopes(self):
        store = ConversationStore(os.path.join(self.tmp.name, "ai", "ai.sqlite3"))
        enterprise_id = store.save_memory("company-a", "user-a", "enterprise", "杭州线路周五结算", source="admin")
        personal_id = store.save_memory("company-a", "user-a", "personal", "我负责周五杭州线路", source="user")
        store2 = ConversationStore(os.path.join(self.tmp.name, "ai", "ai.sqlite3"))
        enterprise = store2.retrieve_memories("杭州线路", tenant_id="company-a", principal_id="user-b", memory_scope="enterprise")
        personal_a = store2.retrieve_memories("杭州线路", tenant_id="company-a", principal_id="user-a", memory_scope="personal")
        personal_b = store2.retrieve_memories("杭州线路", tenant_id="company-a", principal_id="user-b", memory_scope="personal")
        other_tenant = store2.retrieve_memories("杭州线路", tenant_id="company-b", principal_id="user-a", memory_scope="enterprise")
        self.assertEqual([x.evidence_id for x in enterprise], ["memory:" + enterprise_id])
        self.assertEqual([x.evidence_id for x in personal_a], ["memory:" + personal_id])
        self.assertEqual(personal_b, [])
        self.assertEqual(other_tenant, [])

    def test_retrieval_and_authorized_tool_execution_are_auditable_and_idempotent(self):
        model = Model([
            {"content": None, "tool_calls": [{"id": "call-1", "type": "function", "function": {"name": "get_waybill", "arguments": "{\"waybill_id\":\"123\"}"}}]},
            {"content": "运单123已签收。"},
        ])
        response = self.service(model).chat(self.request())
        self.assertEqual(response.reply, "运单123已签收。")
        self.assertEqual(response.evidence[0].evidence_id, "waybill:123")
        self.assertEqual(len(self.tools.calls), 1)
        self.assertEqual(self.tools.calls[0][0], "get_waybill")
        self.assertEqual(len(self.tools.calls[0][4]), 64)
        self.assertEqual(response.tool_events[0]["state"], "succeeded")
        db_path = os.path.join(self.tmp.name, "ai", "ai.sqlite3")
        with ConversationStore(db_path)._connect() as db:
            event = db.execute("SELECT state,result_json,idempotency_key FROM tool_events").fetchone()
        self.assertEqual(event["state"], "succeeded")
        self.assertIn("已签收", event["result_json"])

    def test_authorized_tool_evidence_is_added_to_final_citations_with_bounds(self):
        tools = Tools()
        execute = tools.execute
        def execute_with_evidence(name, arguments, **kwargs):
            result = execute(name, arguments, **kwargs)
            result["evidence"] = [
                {"evidence_id":"task:synthetic-1","text":"合成任务已创建","source":"tasks",
                 "metadata":{"task_id":"synthetic-1","untrusted":{"nested":"ignored"}}},
                {"evidence_id":"bad-entry","text":None,"source":"tasks"},
                {"evidence_id":"large:1","text":"x" * 5000,"source":"s" * 200},
            ]
            return result
        tools.execute = execute_with_evidence
        model = Model([
            {"content":None,"tool_calls":[{"id":"evidence-1","type":"function",
              "function":{"name":"get_waybill","arguments":"{\"waybill_id\":\"123\"}"}}]},
            {"content":"已核实合成任务。"},
        ])
        response = build_service(self.tmp.name, context_provider=self.context,
                                 tool_executor=tools, model_client=model).chat(self.request())
        evidence = {item.evidence_id:item for item in response.evidence}
        self.assertIn("waybill:123", evidence)
        self.assertEqual(evidence["task:synthetic-1"].text, "合成任务已创建")
        self.assertEqual(evidence["task:synthetic-1"].metadata, {"task_id":"synthetic-1"})
        self.assertEqual(len(evidence["large:1"].text), 4000)
        self.assertEqual(len(evidence["large:1"].source), 80)

    def test_scoped_tool_denials_become_opaque_not_found_results(self):
        for error in (LookupError("foreign waybill exists"), PermissionError("not allowed")):
            with self.subTest(error=type(error).__name__):
                tools = Tools()
                tools.execute = lambda *args, _error=error, **kwargs: (_ for _ in ()).throw(_error)
                model = Model([
                    {"content":None,"tool_calls":[{"id":"denied-1","type":"function",
                      "function":{"name":"get_waybill","arguments":"{\"waybill_id\":\"other\"}"}}]},
                    {"content":"没有找到可访问的运单。"},
                ])
                response = build_service(self.tmp.name, context_provider=self.context,
                                         tool_executor=tools, model_client=model).chat(self.request())
                self.assertEqual(response.reply, "没有找到可访问的运单。")
                self.assertEqual(response.tool_events[0]["state"], "succeeded")
                tool_message = next(m for m in model.requests[1][0] if m.get("role") == "tool")
                self.assertEqual(json.loads(tool_message["content"]), {"error":"not_found"})
                serialized = json.dumps(model.requests[1][0], ensure_ascii=False)
                self.assertNotIn("foreign waybill exists", serialized)
                self.assertNotIn("not allowed", serialized)

    def test_repeated_identical_rule_write_is_deduplicated_and_summarized(self):
        class RuleTools:
            supports_idempotency=True
            def __init__(self): self.calls=[]
            def list_tools(self, **_):
                return [{"type":"function","function":{"name":"create_business_rule_version",
                    "parameters":{"type":"object","properties":{"rule_key":{"type":"string"}}}}}]
            def execute(self,name,arguments,**_):
                self.calls.append(dict(arguments))
                return {"item":{"rule_key":arguments["rule_key"],"title":"合成规则","version":2,"effective_at":"2026-11-12"},
                        "evidence":[{"evidence_id":"rule:test:v2","text":"合成规则版本2","source":"business_rule","metadata":{"version":2}}]}
        arguments={"rule_key":"test.duplicate","layer":"foundation","title":"合成规则","content":"合成规则内容","effective_at":"2026-11-12"}
        def call(call_id):
            return {"content":None,"tool_calls":[{"id":call_id,"type":"function","function":{
                "name":"create_business_rule_version","arguments":json.dumps(arguments)}}]}
        model=Model([call("rule-call-1"),call("rule-call-2")])
        tools=RuleTools()
        response=build_service(self.tmp.name,context_provider=self.context,tool_executor=tools,model_client=model).chat(
            self.request(message="明确新增这条规则"))
        self.assertEqual(len(tools.calls),1)
        self.assertEqual([event["state"] for event in response.tool_events],["succeeded","deduplicated"])
        self.assertIn("v2",response.reply)
        self.assertIn("rule:test:v2",response.reply)
        self.assertEqual(response.model_usage["calls"],2)

    def test_distinct_rule_writes_in_same_round_are_not_conflated(self):
        class RuleTools:
            supports_idempotency=True
            def __init__(self): self.calls=[]
            def list_tools(self, **_):
                return [{"type":"function","function":{"name":"create_business_rule_version",
                    "parameters":{"type":"object","properties":{"rule_key":{"type":"string"}}}}}]
            def execute(self,name,arguments,**_):
                self.calls.append(dict(arguments)); return {"item":{"rule_key":arguments["rule_key"],"version":1}}
        def call(call_id,key):
            args={"rule_key":key,"title":"合成","content":"合成"}
            return {"id":call_id,"type":"function","function":{"name":"create_business_rule_version","arguments":json.dumps(args)}}
        tools=RuleTools(); model=Model([
            {"content":None,"tool_calls":[call("rule-a","test.a"),call("rule-b","test.b")]},
            {"content":"两条独立规则已处理。"},
        ])
        response=build_service(self.tmp.name,context_provider=self.context,tool_executor=tools,model_client=model).chat(self.request())
        self.assertEqual(len(tools.calls),2)
        self.assertEqual(len(response.tool_events),2)
        self.assertEqual(response.reply,"两条独立规则已处理。")

    def test_rule_write_replay_after_interrupted_turn_with_new_call_id_is_idempotent(self):
        class RuleTools:
            supports_idempotency=True
            def __init__(self): self.calls=[]
            def list_tools(self, **_):
                return [{"type":"function","function":{"name":"create_business_rule_version",
                    "parameters":{"type":"object","properties":{"rule_key":{"type":"string"}}}}}]
            def execute(self,name,arguments,**_):
                self.calls.append(dict(arguments))
                return {"item":{"rule_key":arguments["rule_key"],"title":"合成规则","version":2,"effective_at":"2026-11-12"}}
        arguments={"rule_key":"test.recovery","layer":"foundation","title":"合成规则","content":"合成规则内容","effective_at":"2026-11-12"}
        def call(call_id):
            return {"content":None,"tool_calls":[{"id":call_id,"type":"function","function":{
                "name":"create_business_rule_version","arguments":json.dumps(arguments)}}]}
        class ModelFailsAfterTool:
            def __init__(self): self.responses=[call("before-restart"),AIServiceError("MODEL_CONNECTION_ERROR","offline",retryable=True)]
            def complete(self,*_):
                result=self.responses.pop(0)
                if isinstance(result,Exception): raise result
                return result
        tools=RuleTools(); first=build_service(self.tmp.name,context_provider=self.context,tool_executor=tools,model_client=ModelFailsAfterTool())
        with self.assertRaises(AIServiceError) as caught:
            first.chat(self.request(message="明确新增规则",))
        cid=caught.exception.conversation_id
        self.assertEqual(len(tools.calls),1)
        restarted=build_service(self.tmp.name,context_provider=self.context,tool_executor=tools,
                                model_client=Model([call("after-restart-new-call-id"),{"content":"规则变更已确认。"}]))
        response=restarted.chat(self.request(message="明确新增规则",conversation_id=cid))
        self.assertEqual(response.reply,"规则变更已确认。")
        self.assertEqual(len(tools.calls),1)
        self.assertEqual(response.tool_events[0]["state"],"succeeded")

    def test_unexpected_tool_fault_still_fails_closed(self):
        tools = Tools()
        tools.execute = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("unexpected backend fault"))
        model = Model([{"content":None,"tool_calls":[{"id":"fault-1","type":"function",
          "function":{"name":"get_waybill","arguments":"{\"waybill_id\":\"123\"}"}}]}])
        with self.assertRaises(AIServiceError) as caught:
            build_service(self.tmp.name, context_provider=self.context,
                          tool_executor=tools, model_client=model).chat(self.request())
        self.assertEqual(caught.exception.code, "TOOL_EXECUTION_FAILED")
        self.assertNotIn("unexpected backend fault", caught.exception.message)

    def test_unadvertised_tool_is_blocked_before_execution(self):
        model = Model([{"content": None, "tool_calls": [{"id": "bad-1", "type": "function", "function": {"name": "delete_all", "arguments": "{}"}}],
                       "_model_usage": {"prompt_tokens": 11, "completion_tokens": 2, "total_tokens": 13}}])
        with self.assertRaises(AIServiceError) as caught:
            self.service(model).chat(self.request())
        self.assertEqual(caught.exception.code, "TOOL_NOT_AUTHORIZED")
        self.assertEqual(self.tools.calls, [])
        self.assertEqual(caught.exception.model_metrics["model_usage"]["total_tokens"], 13)

    def test_handler_preserves_usage_metrics_on_fail_closed_tool_error(self):
        model = Model([{"content": None, "tool_calls": [{"id": "bad-http-1", "type": "function",
                       "function": {"name": "delete_all", "arguments": "{}"}}],
                       "_model_usage": {"prompt_tokens": 9, "completion_tokens": 1, "total_tokens": 10}}])
        status, body = handle_chat_request(
            self.service(model), {"message": "删除所有记录"},
            {"id": "user-a", "tenant_id": "company-a"},
        )
        self.assertEqual(status, 403)
        self.assertEqual(body["model_usage"]["total_tokens"], 10)
        self.assertEqual(body["model_elapsed_ms"] >= 0, True)
        self.assertEqual(body["cost"]["status"], "UNKNOWN")

    def test_model_error_keeps_retryable_conversation_id(self):
        class BrokenModel:
            def complete(self, *_):
                raise AIServiceError("MODEL_CONNECTION_ERROR", "无法连接模型服务。", retryable=True)

        with self.assertRaises(AIServiceError) as caught:
            self.service(BrokenModel()).chat(self.request())
        self.assertEqual(caught.exception.code, "MODEL_CONNECTION_ERROR")
        self.assertTrue(caught.exception.retryable)
        self.assertIsNotNone(caught.exception.conversation_id)

    def test_tool_round_limit_blocks_extra_execution(self):
        model = Model([{"content": None, "tool_calls": [{"id": "over-limit", "type": "function", "function": {"name": "get_waybill", "arguments": "{\"waybill_id\":\"123\"}"}}]}])
        svc = self.service(model)
        svc.max_tool_rounds = 0
        with self.assertRaises(AIServiceError) as caught:
            svc.chat(self.request())
        self.assertEqual(caught.exception.code, "TOOL_ROUND_LIMIT")
        self.assertEqual(self.tools.calls, [])

    def test_interrupted_tool_event_recovers_with_same_idempotency_key(self):
        store = ConversationStore(os.path.join(self.tmp.name, "ai", "ai.sqlite3"))
        req = self.request()
        cid = store.create_or_check(req)
        event = store.prepare_tool(cid, "interrupted-1", "get_waybill", {"waybill_id": "123"})
        store.mark_tool_running(event["id"])
        expected_key = event["idempotency_key"]
        model = Model([{"content": "查询结果已恢复。"}])
        response = self.service(model).chat(ChatRequest(req.tenant_id, req.principal_id, "继续", cid))
        self.assertEqual(response.conversation_id, cid)
        self.assertEqual(self.tools.calls[0][4], expected_key)
        self.assertTrue(any("已签收" in str(m.get("content")) for m in model.requests[0][0]))

    def test_openai_compatible_adapter_contract_without_network(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def read(self, _n): return b'{"model":"model-x-2026","usage":{"prompt_tokens":12,"completion_tokens":3,"total_tokens":15},"choices":[{"message":{"content":"ok"}}]}'

        captured = {}

        def opener(req, timeout):
            captured["url"] = req.full_url
            captured["auth"] = req.get_header("Authorization")
            captured["timeout"] = timeout
            captured["body"] = json.loads(req.data.decode())
            return Response()

        cfg = ModelConfig("https://model.example/v1", "model-x", "test-secret", timeout_seconds=7)
        client = OpenAICompatibleClient(cfg, opener=opener)
        result = client.complete([{"role": "user", "content": "hi"}], [])
        self.assertEqual(result["content"], "ok")
        self.assertEqual(result["_model_usage"], {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15})
        self.assertEqual(result["_provider_model"], "model-x-2026")
        self.assertEqual(captured["url"], "https://model.example/v1/chat/completions")
        self.assertEqual(captured["auth"], "Bearer test-secret")
        self.assertEqual(captured["timeout"], 7)
        self.assertEqual(captured["body"]["model"], "model-x")

    def test_usage_and_elapsed_aggregate_across_tool_call_rounds(self):
        model = Model([
            {
                "content": None,
                "tool_calls": [{
                    "id": "usage-call-1", "type": "function",
                    "function": {"name": "get_waybill", "arguments": "{\"waybill_id\":\"123\"}"},
                }],
                "_model_usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
                "_provider_model": "deepseek-v4-flash",
            },
            {
                "content": "运单已签收。",
                "_model_usage": {"prompt_tokens": 20, "completion_tokens": 3, "total_tokens": 23},
                "_provider_model": "deepseek-v4-flash",
            },
        ])
        response = self.service(model).chat(self.request())
        metrics = response.as_dict()
        self.assertEqual(metrics["model_usage"], {
            "calls": 2, "reported_calls": 2, "complete": True,
            "prompt_tokens": 30, "completion_tokens": 5, "total_tokens": 35,
            "provider_model": "deepseek-v4-flash",
        })
        self.assertGreaterEqual(metrics["elapsed_ms"], metrics["model_elapsed_ms"])
        self.assertGreaterEqual(metrics["model_elapsed_ms"], 0)
        self.assertEqual(metrics["cost"], {"amount": None, "currency": None, "status": "UNKNOWN"})

    def test_usage_is_unknown_if_any_model_round_omits_provider_usage(self):
        model = Model([
            {"content": None, "tool_calls": [{
                "id": "partial-usage-call", "type": "function",
                "function": {"name": "get_waybill", "arguments": "{\"waybill_id\":\"123\"}"},
            }], "_model_usage": {"prompt_tokens": 7, "completion_tokens": 1, "total_tokens": 8}},
            {"content": "已签收。"},
        ])
        metrics = self.service(model).chat(self.request()).as_dict()
        self.assertEqual(metrics["model_usage"]["calls"], 2)
        self.assertEqual(metrics["model_usage"]["reported_calls"], 1)
        self.assertFalse(metrics["model_usage"]["complete"])
        self.assertIsNone(metrics["model_usage"]["total_tokens"])

    def test_model_config_rejects_non_tls_non_loopback_and_credentials_in_url(self):
        for url in ("http://remote.example", "https://name:secret@example.com", "http://127.0.0.1.evil"):
            with self.subTest(url=url), self.assertRaises(AIServiceError):
                ModelConfig.from_env({"JL_AI_BASE_URL": url, "JL_AI_MODEL": "x", "JL_AI_API_KEY": "k"})


if __name__ == "__main__":
    unittest.main()
