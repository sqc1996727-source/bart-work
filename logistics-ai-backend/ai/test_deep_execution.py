"""Deep product Agent tests. All models are scripted; no external calls."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai.agent_policy import ExecutionPolicy, query_terms, relevance
from ai.service import AIService, AIServiceError, ChatRequest, ConversationStore
from ai.test_service import Context, Tools, Model

def call(cid, name="get_waybill", args=None):
    return {"content":None,"tool_calls":[{"id":cid,"type":"function","function":{"name":name,"arguments":json.dumps(args if args is not None else {"waybill_id":"123"})}}]}

class DeepExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tools=Tools()
        self.store=ConversationStore(str(Path(self.tmp.name)/"agent.sqlite3"))
        self.req=ChatRequest("company-a","user-a","核实资料")
    def service(self,model,policy=None):
        return AIService(self.store,Context(),self.tools,model,execution_policy=policy or ExecutionPolicy())
    def test_deep_multi_step_execution(self):
        model=Model([call(str(i)) for i in range(16)]+[{"content":"核实完成"}])
        result=self.service(model).chat(self.req)
        self.assertEqual(result.reply,"核实完成")
        self.assertEqual(len(self.tools.calls),16)
    def test_retryable_provider_failure(self):
        class RetryModel:
            def __init__(self):self.calls=0
            def complete(self,messages,tools):
                self.calls+=1
                if self.calls==1:raise AIServiceError("MODEL_CONNECTION_ERROR","offline",retryable=True)
                return {"content":"已恢复","usage":{"prompt_tokens":5,"completion_tokens":2,"total_tokens":7}}
        m=RetryModel()
        with patch("ai.service.time.sleep"):
            result=self.service(m).chat(self.req)
        self.assertEqual(result.reply,"已恢复")
        self.assertEqual(m.calls,2)
        self.assertIsNone(result.model_usage["total_tokens"])
    def test_unavailable_tool_repaired_without_execution(self):
        model=Model([call("a","forbidden",{}),call("b"),{"content":"已查"}])
        result=self.service(model).chat(self.req)
        self.assertEqual(len(self.tools.calls),1)
        self.assertEqual(self.tools.calls[0][0],"get_waybill")
        self.assertEqual(result.reply,"已查")
    def test_duplicate_write_does_not_end_entire_goal(self):
        class WriteTools(Tools):
            def list_tools(self,**kw):
                return super().list_tools(**kw)+[{"type":"function","function":{"name":"create_task","parameters":{"type":"object","properties":{"title":{"type":"string"}}}}}]
            def execute(self,name,arguments,**kw):
                if name=="create_task":
                    self.calls.append((name,arguments))
                    return {"item":{"id":"task-1","status":"open"}}
                return super().execute(name,arguments,**kw)
        self.tools=WriteTools()
        model=Model([call("a","create_task",{"title":"核对"}),call("b","create_task",{"title":"核对"}),call("c"),{"content":"已创建并核实"}])
        result=self.service(model).chat(self.req)
        self.assertEqual([x[0] for x in self.tools.calls],["create_task","get_waybill"])
        self.assertEqual(result.reply,"已创建并核实")
    def test_budget_blocks_additional_model_request(self):
        model=Model([call("a"),{"content":"不应调用"}])
        with self.assertRaises(AIServiceError) as cm:
            self.service(model,ExecutionPolicy(model_calls=1)).chat(self.req)
        self.assertEqual(cm.exception.code,"AGENT_BUDGET_REACHED")
        self.assertEqual(len(model.requests),1)
    def test_uncertain_tool_never_replayed(self):
        cid=self.store.create_or_check(self.req)
        event=self.store.prepare_tool(cid,"old","get_waybill",{"waybill_id":"123"})
        self.store.mark_tool_running(event["id"])
        with self.assertRaises(AIServiceError) as cm:
            self.service(Model([{"content":"不应调用"}])).chat(ChatRequest("company-a","user-a","继续",conversation_id=cid))
        self.assertEqual(cm.exception.code,"TOOL_RECOVERY_BLOCKED")
        self.assertEqual(self.tools.calls,[])
    def test_chinese_and_exact_identifiers(self):
        self.assertGreater(relevance(query_terms("天运是哪家公司"),"合作方沈阳天运"),0)
        self.assertEqual(relevance(query_terms("123"),"运单1234"),0)
        self.assertGreater(relevance(query_terms("123"),"运单123"),0)
    def test_limits_configurable(self):
        self.assertEqual(ExecutionPolicy.from_env({"BOT_AGENT_ROUNDS":"96"}).rounds,96)
        with self.assertRaises(ValueError):ExecutionPolicy.from_env({"BOT_AGENT_ROUNDS":"0"})

if __name__=="__main__":
    unittest.main()
