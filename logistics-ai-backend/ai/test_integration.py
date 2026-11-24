"""HTTP integration tests for the injected AI service and host workbench tools.

All state lives in temporary directories. The scripted model is an in-process
test double: tests exercise the real authenticated HTTP route and the real
WorkbenchTools task service without calling a provider or external network.
"""
from __future__ import annotations

import http.cookiejar
import importlib.util
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import ai.service as ai_service_module
from ai.service import AIService, ChatRequest, ConversationStore


ROOT = Path(__file__).resolve().parents[1]
PASSWORD = "test-only-passphrase-123"


class ScriptedModel:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.requests = []

    def complete(self, messages, tools):
        self.requests.append((messages, tools))
        if not self.outputs:
            raise AssertionError("scripted model exhausted")
        return self.outputs.pop(0)


class Client:
    def __init__(self, base_url):
        self.base_url = base_url
        self.cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookies))

    def request(self, method, path, payload=None):
        body = None if payload is None else json.dumps(payload).encode()
        req = urllib.request.Request(self.base_url + path, data=body, method=method)
        if body is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with self.opener.open(req, timeout=3) as response:
                return response.status, json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode())


class AIWorkbenchIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="julong-ai-http-")
        self.old_data_dir = os.environ.get("DATA_DIR")
        self.old_cookie_secure = os.environ.get("COOKIE_SECURE")
        os.environ["DATA_DIR"] = self.temp.name
        os.environ["COOKIE_SECURE"] = "0"
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        module_name = "julong_ai_test_app_" + str(os.getpid()) + "_" + str(id(self))
        spec = importlib.util.spec_from_file_location(module_name, ROOT / "app.py")
        self.app = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = self.app
        spec.loader.exec_module(self.app)
        self.app.init_db()
        self.app.add_user("owner_test", "Owner Test", "owner_finance", PASSWORD)
        self.app.add_user("employee_test", "Employee Test", "employee", PASSWORD)
        self.users = {}
        with self.app.db() as con:
            for username in ("owner_test", "employee_test"):
                row = con.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
                self.users[username] = dict(row)
        self.server = None
        self.server_thread = None
        self.port = self._free_port()
        self._start_server()
        self.owner = Client(self.base_url)
        self.employee = Client(self.base_url)
        self.assertEqual(self.owner.request("POST", "/api/login", {"username": "owner_test", "password": PASSWORD})[0], 200)
        self.assertEqual(self.employee.request("POST", "/api/login", {"username": "employee_test", "password": PASSWORD})[0], 200)

    def tearDown(self):
        self._stop_server()
        if self.old_data_dir is None:
            os.environ.pop("DATA_DIR", None)
        else:
            os.environ["DATA_DIR"] = self.old_data_dir
        if self.old_cookie_secure is None:
            os.environ.pop("COOKIE_SECURE", None)
        else:
            os.environ["COOKIE_SECURE"] = self.old_cookie_secure
        self.temp.cleanup()

    @staticmethod
    def _free_port():
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]

    @property
    def base_url(self):
        return "http://127.0.0.1:%d" % self.port

    def _inject_service(self, model):
        self.app._AI_SERVICE = AIService(
            ConversationStore(str(Path(self.temp.name) / "ai" / "ai.sqlite3")),
            self.app.WorkbenchContext(), self.app.WorkbenchTools(), model,
        )

    def _start_server(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", self.port), self.app.Handler)
        self.server.daemon_threads = True
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                urllib.request.urlopen(self.base_url + "/health", timeout=0.2).close()
                return
            except (OSError, urllib.error.URLError):
                time.sleep(0.02)
        self.fail("in-process isolated server failed to start")

    def _stop_server(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server_thread.join(timeout=3)
            self.server = None
            self.server_thread = None

    def _task_count(self):
        with self.app.db() as con:
            return con.execute("SELECT count(*) FROM tasks").fetchone()[0]

    def _audit_count(self, action):
        with self.app.db() as con:
            return con.execute("SELECT count(*) FROM audit WHERE action=?", (action,)).fetchone()[0]

    @staticmethod
    def _tool_call(call_id, name, arguments):
        return {"content": None, "tool_calls": [{
            "id": call_id, "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)},
        }]}

    def test_http_ai_executes_real_task_tool_and_deduplicates_replay(self):
        model = ScriptedModel([
            self._tool_call("create-1", "create_task", {"title": "Synthetic pickup follow-up", "description": "test only"}),
            {"content": "任务已创建。"},
            self._tool_call("create-1", "create_task", {"title": "Synthetic pickup follow-up", "description": "test only"}),
            {"content": "任务已存在。"},
        ])
        self._inject_service(model)
        status, first = self.owner.request("POST", "/api/ai/chat", {"message": "请创建一个取件跟进任务"})
        self.assertEqual(status, 200)
        self.assertEqual(first["tool_events"][0]["name"], "create_task")
        conversation_id = first["conversation_id"]
        self.assertEqual(self._task_count(), 1)
        self.assertEqual(self._audit_count("task.create"), 1)
        status, second = self.owner.request("POST", "/api/ai/chat", {"message": "重复创建同一任务", "conversation_id": conversation_id})
        self.assertEqual(status, 200)
        self.assertEqual(second["tool_events"][0]["state"], "succeeded")
        self.assertEqual(self._task_count(), 1)
        self.assertEqual(self._audit_count("task.create"), 1)

    def test_employee_cannot_invoke_unlisted_create_tool(self):
        model = ScriptedModel([
            self._tool_call("forbidden-create", "create_task", {"title": "Must not exist"}),
        ])
        self._inject_service(model)
        status, response = self.employee.request("POST", "/api/ai/chat", {"message": "创建任务"})
        self.assertEqual(status, 403)
        self.assertEqual(response["error"]["code"], "TOOL_NOT_AUTHORIZED")
        self.assertEqual(self._task_count(), 0)
        self.assertEqual(self._audit_count("task.create"), 0)

    def test_employee_cannot_update_another_users_task(self):
        owner_row = self.users["owner_test"]
        self.app.create_task_record(owner_row, {"title": "Unassigned synthetic task"}, "seed-task-001")
        with self.app.db() as con:
            task_id = con.execute("SELECT id FROM tasks").fetchone()[0]
        before = self._audit_count("task.update")
        model = ScriptedModel([
            self._tool_call("forbidden-update", "update_task", {"task_id": task_id, "status": "done"}),
            {"content": "没有找到可访问的任务。"},
        ])
        self._inject_service(model)
        status, response = self.employee.request("POST", "/api/ai/chat", {"message": "完成这个任务"})
        self.assertEqual(status, 200)
        self.assertEqual(response["reply"], "没有找到可访问的任务。")
        self.assertEqual(response["tool_events"][0]["state"], "succeeded")
        with self.app.db() as con:
            current = con.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()[0]
        self.assertEqual(current, "open")
        self.assertEqual(self._audit_count("task.update"), before)

    def test_financial_tool_results_are_filtered_by_authenticated_role(self):
        status, current = self.owner.request("GET", "/api/state")
        self.assertEqual(status, 200)
        state = current["state"]
        state["julong_demo_waybills_v2"] = json.dumps([{
            "number": "TEST-PRIV-1", "destination": "Synthetic City",
            "amount": "TEST-FINANCE-ONLY-9381", "recipient": "Synthetic Person",
        }])
        # Seed a synthetic historical record directly in the isolated test DB.
        with self.app.db() as con:
            con.execute("UPDATE work_state SET payload=? WHERE id=1", (json.dumps(state),))

        employee_model = ScriptedModel([
            self._tool_call("employee-waybill", "get_waybill", {"waybill_number": "TEST-PRIV-1"}),
            {"content": "已查到运单。"},
        ])
        self._inject_service(employee_model)
        status, employee_response = self.employee.request("POST", "/api/ai/chat", {"message": "查询 TEST-PRIV-1"})
        self.assertEqual(status, 200)
        self.assertNotIn("TEST-FINANCE-ONLY-9381", json.dumps(employee_response, ensure_ascii=False))

        owner_model = ScriptedModel([
            self._tool_call("owner-waybill", "get_waybill", {"waybill_number": "TEST-PRIV-1"}),
            {"content": "已查到运单。"},
        ])
        self._inject_service(owner_model)
        status, owner_response = self.owner.request("POST", "/api/ai/chat", {"message": "内部对账查询 TEST-PRIV-1"})
        self.assertEqual(status, 200)
        self.assertNotIn("TEST-FINANCE-ONLY-9381", json.dumps(owner_response, ensure_ascii=False))

    def test_pending_tool_recovers_after_service_and_http_server_restart(self):
        first_model = ScriptedModel([])
        self._inject_service(first_model)
        service = self.app._AI_SERVICE
        request = ChatRequest("julong-test", self.users["owner_test"]["id"], "继续处理")
        conversation_id = service.store.create_or_check(request)
        event = service.store.prepare_tool(
            conversation_id, "crash-before-run", "create_task",
            {"title": "Recovered synthetic task", "description": "recovered after restart"},
        )
        service.store.mark_tool_running(event["id"])
        expected_idempotency_key = event["idempotency_key"]
        seen_keys = []
        original_create = self.app.create_task_record

        def capture_idempotency(user, body, idem_key):
            seen_keys.append(idem_key)
            return original_create(user, body, idem_key)

        self.app.create_task_record = capture_idempotency
        self._stop_server()
        self.app._AI_SERVICE = None
        original_build = ai_service_module.build_service

        def build_with_test_model(data_dir, *, context_provider, tool_executor, model_client=None, model_config=None):
            return original_build(
                data_dir, context_provider=context_provider, tool_executor=tool_executor,
                model_client=ScriptedModel([{"content": "重启后已恢复任务。"}]),
            )

        ai_service_module.build_service = build_with_test_model
        try:
            self.app._ai_service()
        finally:
            ai_service_module.build_service = original_build
        self._start_server()
        status, response = self.owner.request("POST", "/api/ai/chat", {
            "message": "恢复并继续", "conversation_id": conversation_id,
        })
        self.assertEqual(status, 200)
        self.assertEqual(response["conversation_id"], conversation_id)
        self.assertEqual(self._task_count(), 1)
        self.assertEqual(self._audit_count("task.create"), 1)
        self.assertEqual(seen_keys, [expected_idempotency_key])
        events = self.app._AI_SERVICE.store.completed_events(conversation_id)
        self.assertEqual(events[0]["state"], "succeeded")
        self.assertEqual(events[0]["result_json"] is not None, True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
