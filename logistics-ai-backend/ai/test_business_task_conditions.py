"""HTTP acceptance for structured task completion conditions using synthetic data."""
from __future__ import annotations

import http.cookiejar
import importlib.util
import json
import os
import socket
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PASSWORD = "synthetic-task-condition-pass"


class Client:
    def __init__(self, base_url):
        self.base_url = base_url
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )

    def request(self, method, path, payload=None, headers=None):
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode()
        req = urllib.request.Request(self.base_url + path, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        try:
            with self.opener.open(req, timeout=4) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())


class BusinessTaskConditionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="julong-task-conditions-")
        self.old_data_dir = os.environ.get("DATA_DIR")
        self.old_cookie_secure = os.environ.get("COOKIE_SECURE")
        os.environ["DATA_DIR"] = self.temp.name
        os.environ["COOKIE_SECURE"] = "0"
        module_name = f"julong_task_condition_app_{os.getpid()}_{id(self)}"
        spec = importlib.util.spec_from_file_location(module_name, ROOT / "app.py")
        self.app = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = self.app
        spec.loader.exec_module(self.app)
        self.app.init_db()
        for username, role in (("owner_cond", "owner_finance"),
                                ("employee_cond", "employee"),
                                ("other_cond", "employee")):
            self.app.add_user(username, username, role, PASSWORD)
        with self.app.db() as con:
            self.users = {
                name: dict(con.execute("SELECT * FROM users WHERE username=?", (name,)).fetchone())
                for name in ("owner_cond", "employee_cond", "other_cond")
            }
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.server = ThreadingHTTPServer(("127.0.0.1", self.port), self.app.Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.clients = {name: Client(self.base_url) for name in self.users}
        for name, client in self.clients.items():
            self.assertEqual(client.request("POST", "/api/login", {
                "username": name, "password": PASSWORD,
            })[0], 200)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        if self.old_data_dir is None:
            os.environ.pop("DATA_DIR", None)
        else:
            os.environ["DATA_DIR"] = self.old_data_dir
        if self.old_cookie_secure is None:
            os.environ.pop("COOKIE_SECURE", None)
        else:
            os.environ["COOKIE_SECURE"] = self.old_cookie_secure
        self.temp.cleanup()

    def _state(self, username):
        status, snapshot = self.clients[username].request("GET", "/api/state")
        self.assertEqual(status, 200)
        return snapshot

    def _save(self, username, snapshot, state):
        return self.clients[username].request("PUT", "/api/state", {
            "base_rev": snapshot["rev"], "state": state,
        })

    def _create_task(self, waybill, title, condition, idem):
        return self.clients["owner_cond"].request(
            "POST", "/api/tasks", {
                "title": title,
                "assignee_id": self.users["employee_cond"]["id"],
                "waybill_number": waybill,
                "completion_condition": condition,
            }, {"Idempotency-Key": idem},
        )

    def _task(self, task_id):
        status, result = self.clients["owner_cond"].request("GET", "/api/tasks/" + task_id)
        self.assertEqual(status, 200)
        return result["item"]

    def _event_count(self, action, task_id):
        status, result = self.clients["owner_cond"].request("GET", "/api/audit?limit=500")
        self.assertEqual(status, 200)
        return [e for e in result["items"] if e["action"] == action and e["entity_id"] == task_id]

    def test_receipt_and_fee_conditions_complete_reopen_and_remain_idempotent(self):
        employee = self.clients["employee_cond"]
        owner = self.clients["owner_cond"]
        numbers = {"receipt": "COND-RECEIPT-1", "fees": "COND-FEES-1"}

        owner_tools = self.app.WorkbenchTools().list_tools(
            tenant_id="julong-test", principal_id=self.users["owner_cond"]["id"]
        )
        employee_tools = self.app.WorkbenchTools().list_tools(
            tenant_id="julong-test", principal_id=self.users["employee_cond"]["id"]
        )
        def properties_for(tools, name):
            return next(tool["function"]["parameters"]["properties"] for tool in tools
                        if tool["function"]["name"] == name)
        self.assertIn("completion_condition", properties_for(owner_tools, "create_task"))
        self.assertIn("completion_condition", properties_for(owner_tools, "update_task"))
        self.assertNotIn("completion_condition", properties_for(employee_tools, "update_task"))
        self.assertNotIn("create_task", [tool["function"]["name"] for tool in employee_tools])

        # Employee work is tenant-visible to the owner and attributed to its creator.
        snapshot = self._state("employee_cond")
        docs = [{"number": number, "date": "2026-11-12", "created_by": self.users["employee_cond"]["id"]}
                for number in numbers.values()]
        snapshot["state"][self.app.STATE_KEYS[0]] = json.dumps(docs)
        self.assertEqual(self._save("employee_cond", snapshot, snapshot["state"])[0], 200)
        owner_state = self._state("owner_cond")["state"]
        visible_docs = json.loads(owner_state[self.app.STATE_KEYS[0]])
        self.assertEqual({d["number"] for d in visible_docs}, set(numbers.values()))
        self.assertTrue(all(d["created_by"] == self.users["employee_cond"]["id"] for d in visible_docs))

        status, receipt_task = self._create_task(numbers["receipt"], "交付厂家回单", "receipt_delivered", "receipt-task-create")
        self.assertEqual(status, 201)
        receipt_id = receipt_task["item"]["id"]
        status, duplicate = self._create_task(numbers["receipt"], "交付厂家回单", "receipt_delivered", "receipt-task-create")
        self.assertEqual(status, 201)
        self.assertEqual(duplicate["item"]["id"], receipt_id)
        self.assertEqual(self._task(receipt_id)["status"], "open")
        # Manual completion is recorded separately and must not be rewritten by business events.
        status, manual_task = self._create_task(numbers["receipt"], "人工标记完成的回单任务", "receipt_delivered", "manual-origin-task")
        self.assertEqual(status, 201)
        manual_id = manual_task["item"]["id"]
        self.assertEqual(employee.request("PATCH", "/api/tasks/" + manual_id, {"status": "done"})[0], 200)
        self.assertEqual(self._task(manual_id)["completion_origin"], "manual")

        status, fee_task = self._create_task(numbers["fees"], "确认全部关联费用", "fees_confirmed", "fees-task-create")
        self.assertEqual(status, 201)
        fee_id = fee_task["item"]["id"]
        self.assertEqual(self._task(fee_id)["status"], "open")  # no fee rows is not complete

        # A second employee cannot see or update another employee's assigned task.
        other = self.clients["other_cond"]
        self.assertEqual(other.request("GET", "/api/tasks/" + receipt_id)[0], 404)
        self.assertEqual(other.request("PATCH", "/api/tasks/" + receipt_id, {"status": "done"})[0], 404)
        self.assertEqual(other.request("POST", "/api/tasks", {"title": "forbidden"},
                                      {"Idempotency-Key": "employee-cannot-create"})[0], 403)

        # Unrelated business edits do not meet a structured completion condition.
        snapshot = self._state("employee_cond")
        docs = json.loads(snapshot["state"][self.app.STATE_KEYS[0]])
        docs[0]["remark"] = "unrelated note change"
        snapshot["state"][self.app.STATE_KEYS[0]] = json.dumps(docs, ensure_ascii=False)
        self.assertEqual(self._save("employee_cond", snapshot, snapshot["state"])[0], 200)
        self.assertEqual(self._task(receipt_id)["status"], "open")
        self.assertEqual(self._task(fee_id)["status"], "open")

        # Structured receipt delivery completes only the linked receipt task.
        snapshot = self._state("employee_cond")
        receipts = json.loads(snapshot["state"][self.app.STATE_KEYS[5]])
        receipts.append({"number": numbers["receipt"], "delivered": {"off": False}})
        snapshot["state"][self.app.STATE_KEYS[5]] = json.dumps(receipts)
        self.assertEqual(self._save("employee_cond", snapshot, snapshot["state"])[0], 200)
        receipt_done = self._task(receipt_id)
        self.assertEqual(receipt_done["status"], "done")
        self.assertEqual(receipt_done["completion_origin"], "business")
        self.assertEqual(self._task(fee_id)["status"], "open")
        self.assertEqual(self._task(manual_id)["status"], "done")
        self.assertEqual(self._task(manual_id)["completion_origin"], "manual")
        self.assertEqual(len(self._event_count("task.business_complete", receipt_id)), 1)

        # Repeating a no-op save does not duplicate completion or audit events.
        snapshot = self._state("employee_cond")
        self.assertEqual(self._save("employee_cond", snapshot, snapshot["state"])[0], 200)
        self.assertEqual(len(self._event_count("task.business_complete", receipt_id)), 1)

        # Revoking the receipt reopens only business-completed tasks.
        snapshot = self._state("employee_cond")
        receipts = json.loads(snapshot["state"][self.app.STATE_KEYS[5]])
        receipt = next(r for r in receipts if r["number"] == numbers["receipt"])
        receipt["delivered"] = {**receipt["delivered"], "off": True}
        snapshot["state"][self.app.STATE_KEYS[5]] = json.dumps(receipts)
        self.assertEqual(self._save("employee_cond", snapshot, snapshot["state"])[0], 200)
        reopened = self._task(receipt_id)
        self.assertEqual(reopened["status"], "in_progress")
        self.assertIsNone(reopened["completion_origin"])
        self.assertEqual(self._task(manual_id)["status"], "done")
        self.assertEqual(self._task(manual_id)["completion_origin"], "manual")
        self.assertEqual(len(self._event_count("task.business_reopen", receipt_id)), 1)

        # Owner seeds synthetic associated fee rows; pending or incomplete rows keep task open.
        snapshot = self._state("owner_cond")
        fees = [
            {"id": "COND-FEE-A", "number": numbers["fees"], "date": "2026-11-12", "type": "进仓费", "status": "待确认", "amount": "10.00"},
            {"id": "COND-FEE-B", "number": numbers["fees"], "date": "2026-11-12", "type": "进仓费", "status": "待确认", "amount": "20.00"},
        ]
        snapshot["state"][self.app.STATE_KEYS[4]] = json.dumps(fees, ensure_ascii=False)
        self.assertEqual(self._save("owner_cond", snapshot, snapshot["state"])[0], 200)
        self.assertEqual(self._task(fee_id)["status"], "open")

        # An authorized person reviews and confirms each fee separately.
        for index, fid in enumerate(('COND-FEE-A','COND-FEE-B')):
            status, candidate = self.clients['owner_cond'].request('POST','/api/finance/candidates',
                {'fee_id':fid,'status':'已确认','password':PASSWORD}, {'Idempotency-Key':'task-fee-'+fid})
            self.assertEqual(status,201)
            path='/api/finance/candidates/'+candidate['candidate_id']
            status, pending = self.clients['owner_cond'].request('GET','/api/finance/candidates')
            self.assertEqual(status, 200)
            self.assertIn(candidate['candidate_id'], [item['candidate_id'] for item in pending['items']])
            self.assertEqual(employee.request('GET','/api/finance/candidates')[0], 403)
            self.assertEqual(self.clients['owner_cond'].request('GET',path)[0],200)
            self.assertEqual(self.clients['owner_cond'].request('POST',path+'/confirm',{'password':PASSWORD})[0],200)
            self.assertEqual(self.clients['owner_cond'].request('GET','/api/finance/candidates')[1]['items'], [])
            if index==0: self.assertEqual(self._task(fee_id)['status'],'open')
        fees_done = self._task(fee_id)
        self.assertEqual(fees_done["status"], "done")
        self.assertEqual(fees_done["completion_origin"], "business")
        self.assertEqual(len(self._event_count("task.business_complete", fee_id)), 1)

        # Direct demotion of a confirmed fee is rejected. A newly registered pending fee reopens the task.
        snapshot = self._state("employee_cond")
        fees = json.loads(snapshot["state"][self.app.STATE_KEYS[4]])
        fees[0]["status"] = "待确认"
        snapshot["state"][self.app.STATE_KEYS[4]] = json.dumps(fees, ensure_ascii=False)
        self.assertEqual(self._save("employee_cond", snapshot, snapshot["state"])[0], 409)
        snapshot = self._state("owner_cond")
        fees = json.loads(snapshot["state"][self.app.STATE_KEYS[4]])
        fees.append({"id":"COND-FEE-C","number":numbers["fees"],"date":"2026-11-12","status":"待确认","amount":"30.00"})
        snapshot["state"][self.app.STATE_KEYS[4]] = json.dumps(fees, ensure_ascii=False)
        self.assertEqual(self._save("owner_cond", snapshot, snapshot["state"])[0], 200)
        fees_reopened = self._task(fee_id)
        self.assertEqual(fees_reopened["status"], "in_progress")
        self.assertIsNone(fees_reopened["completion_origin"])
        self.assertEqual(len(self._event_count("task.business_reopen", fee_id)), 1)

        # Audit identifies business-driven transitions without exposing amounts.
        events = self._event_count("task.business_complete", fee_id) + self._event_count("task.business_reopen", fee_id)
        serialized = json.dumps(events, ensure_ascii=False)
        self.assertIn("fees_confirmed", serialized)
        self.assertIn(numbers["fees"], serialized)
        self.assertNotIn("10.00", serialized)
        self.assertNotIn("20.00", serialized)


if __name__ == "__main__":
    unittest.main()
