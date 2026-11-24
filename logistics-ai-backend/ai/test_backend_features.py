"""Synthetic black-box checks for rules, memories, analytics, and audit."""
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
from datetime import date, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path

from ai.service import AIService, ConversationStore

ROOT = Path(__file__).resolve().parents[1]
PASSWORD = "test-only-passphrase-234"


class CapturingModel:
    def __init__(self):
        self.requests = []

    def complete(self, messages, tools):
        self.requests.append((list(messages), list(tools)))
        return {"content": "synthetic response"}


class APIClient:
    def __init__(self, base_url):
        self.base_url = base_url
        self.cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookies))

    def request(self, method, path, payload=None, headers=None):
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.base_url + path, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        try:
            with self.opener.open(req, timeout=3) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))


class BackendFeatureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="julong-ai-features-")
        self.old_data_dir = os.environ.get("DATA_DIR")
        self.old_cookie_secure = os.environ.get("COOKIE_SECURE")
        os.environ["DATA_DIR"] = self.temp.name
        os.environ["COOKIE_SECURE"] = "0"
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        module_name = "julong_ai_feature_app_" + str(os.getpid()) + "_" + str(id(self))
        spec = importlib.util.spec_from_file_location(module_name, ROOT / "app.py")
        self.app = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = self.app
        spec.loader.exec_module(self.app)
        self.app.init_db()
        self.app.add_user("owner_feat", "Feature Owner", "owner_finance", PASSWORD)
        self.app.add_user("employee_a", "Feature Employee A", "employee", PASSWORD)
        self.app.add_user("employee_b", "Feature Employee B", "employee", PASSWORD)
        with self.app.db() as con:
            self.user_rows = {
                name: dict(con.execute("SELECT * FROM users WHERE username=?", (name,)).fetchone())
                for name in ("owner_feat", "employee_a", "employee_b")
            }
        self.port = self._free_port()
        self.server = None
        self.server_thread = None
        self._start_server()
        self.clients = {name: APIClient(self.base_url) for name in self.user_rows}
        for name, client in self.clients.items():
            status, _ = client.request("POST", "/api/login", {"username": name, "password": PASSWORD})
            self.assertEqual(status, 200)
        self.model = CapturingModel()
        self.app._AI_SERVICE = AIService(
            ConversationStore(str(Path(self.temp.name) / "ai" / "ai.sqlite3")),
            self.app.WorkbenchContext(), self.app.WorkbenchTools(), self.model,
        )

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

    def _start_server(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", self.port), self.app.Handler)
        self.server.daemon_threads = True
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(self.base_url + "/health", timeout=0.2):
                    return
            except (OSError, urllib.error.URLError):
                time.sleep(0.02)
        self.fail("isolated backend did not start")

    def _stop_server(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server_thread.join(timeout=3)
            self.server = None
            self.server_thread = None

    def _create_rule(self, rule_key, title, content, effective_at, finance_only=False):
        return self.clients["owner_feat"].request("POST", "/api/rules", {
            "rule_key": rule_key, "layer": "foundation", "title": title,
            "content": content, "effective_at": effective_at, "finance_only": finance_only,
        }, {"Idempotency-Key": "rule-" + rule_key + "-" + title})

    def _chat(self, user, message, memory_scope="enterprise"):
        return self.clients[user].request("POST", "/api/ai/chat", {"message": message, "memory_scope":memory_scope})

    def test_rule_versions_use_latest_effective_and_ai_injects_only_current(self):
        today = date.today().isoformat()
        future = "2999-12-31"
        status, v1 = self._create_rule("test.dispatch", "dispatch v1", "RULE_V1_OLD_481", "2000-01-01")
        self.assertEqual(status, 201)
        status, v2 = self._create_rule("test.dispatch", "dispatch future", "RULE_FUTURE_927", future)
        self.assertEqual(status, 201)
        status, v3 = self._create_rule("test.dispatch", "dispatch current", "RULE_V3_ACTIVE_628", today)
        self.assertEqual(status, 201)
        status, finance = self._create_rule("test.finance", "private rule", "RULE_FINANCE_ONLY_819", today, True)
        self.assertEqual(status, 201)

        status, current = self.clients["employee_a"].request("GET", "/api/rules")
        self.assertEqual(status, 200)
        dispatch_current = [item for item in current["items"] if item["rule_key"] == "test.dispatch"]
        self.assertEqual([(item["version"], item["effective_at"]) for item in dispatch_current], [(3, today)])
        self.assertFalse(any(item["rule_key"] == "test.finance" for item in current["items"]))
        status, history = self.clients["owner_feat"].request("GET", "/api/rules?history=1")
        self.assertEqual(status, 200)
        self.assertEqual([item["version"] for item in history["items"] if item["rule_key"] == "test.dispatch"], [3, 2, 1])
        status, _ = self.clients["employee_a"].request("GET", "/api/rules?history=1")
        self.assertEqual(status, 403)

        status, employee_response = self._chat("employee_a", "请按当前调度规则处理")
        self.assertEqual(status, 200)
        employee_evidence = json.dumps(employee_response["evidence"], ensure_ascii=False)
        self.assertIn("RULE_V3_ACTIVE_628", employee_evidence)
        self.assertNotIn("RULE_V1_OLD_481", employee_evidence)
        self.assertNotIn("RULE_FUTURE_927", employee_evidence)
        self.assertNotIn("RULE_FINANCE_ONLY_819", employee_evidence)

        status, personal_scope = self._chat("employee_a", "查 RULE_V3_ACTIVE_628", "personal")
        self.assertEqual(status, 200)
        personal_scope_evidence = json.dumps(personal_scope["evidence"], ensure_ascii=False)
        self.assertIn("RULE_V3_ACTIVE_628", personal_scope_evidence)
        self.assertNotIn("RULE_V1_OLD_481", personal_scope_evidence)
        self.assertNotIn("RULE_FUTURE_927", personal_scope_evidence)
        self.assertNotIn("RULE_FINANCE_ONLY_819", personal_scope_evidence)

        status, owner_response = self._chat("owner_feat", "查看财务规则")
        self.assertEqual(status, 200)
        owner_evidence = json.dumps(owner_response["evidence"], ensure_ascii=False)
        self.assertIn("RULE_FINANCE_ONLY_819", owner_evidence)
        self.assertIn("RULE_V3_ACTIVE_628", owner_evidence)

    def test_enterprise_and_personal_memories_are_injected_only_within_scope(self):
        status, enterprise = self.clients["owner_feat"].request("POST", "/api/memories", {
            "scope": "enterprise", "title": "company route", "content": "ENT_MEM_SHARED_483",
        }, {"Idempotency-Key": "memory-enterprise-1"})
        self.assertEqual(status, 201)
        status, private_a = self.clients["employee_a"].request("POST", "/api/memories", {
            "scope": "personal", "title": "my preference", "content": "PERS_A_ONLY_738",
        }, {"Idempotency-Key": "memory-personal-a-1"})
        self.assertEqual(status, 201)
        status, private_b = self.clients["employee_b"].request("POST", "/api/memories", {
            "scope": "personal", "title": "my preference", "content": "PERS_B_ONLY_164",
        }, {"Idempotency-Key": "memory-personal-b-1"})
        self.assertEqual(status, 201)

        status, list_a = self.clients["employee_a"].request("GET", "/api/memories?scope=personal")
        self.assertEqual(status, 200)
        self.assertEqual([m["id"] for m in list_a["items"]], [private_a["item"]["id"]])
        status, list_b = self.clients["employee_b"].request("GET", "/api/memories?scope=personal")
        self.assertEqual(status, 200)
        self.assertEqual([m["id"] for m in list_b["items"]], [private_b["item"]["id"]])
        status, enterprise_a = self.clients["employee_a"].request("GET", "/api/memories?scope=enterprise")
        self.assertEqual(status, 200)
        self.assertIn(enterprise["item"]["id"], [m["id"] for m in enterprise_a["items"]])

        status, response_a_enterprise = self._chat("employee_a", "ENT_MEM_SHARED_483 PERS_A_ONLY_738")
        self.assertEqual(status, 200)
        evidence_a_enterprise = json.dumps(response_a_enterprise["evidence"], ensure_ascii=False)
        self.assertIn("ENT_MEM_SHARED_483", evidence_a_enterprise)
        self.assertNotIn("PERS_A_ONLY_738", evidence_a_enterprise)
        self.assertNotIn("PERS_B_ONLY_164", evidence_a_enterprise)

        status, response_a_personal = self._chat("employee_a", "PERS_A_ONLY_738", "personal")
        self.assertEqual(status, 200)
        evidence_a_personal = json.dumps(response_a_personal["evidence"], ensure_ascii=False)
        self.assertIn("PERS_A_ONLY_738", evidence_a_personal)
        self.assertNotIn("PERS_B_ONLY_164", evidence_a_personal)
        self.assertNotIn("ENT_MEM_SHARED_483", evidence_a_personal)

        status, response_b_enterprise = self._chat("employee_b", "ENT_MEM_SHARED_483 PERS_B_ONLY_164")
        self.assertEqual(status, 200)
        evidence_b_enterprise = json.dumps(response_b_enterprise["evidence"], ensure_ascii=False)
        self.assertIn("ENT_MEM_SHARED_483", evidence_b_enterprise)
        self.assertNotIn("PERS_A_ONLY_738", evidence_b_enterprise)
        self.assertNotIn("PERS_B_ONLY_164", evidence_b_enterprise)

        status, response_b_personal = self._chat("employee_b", "PERS_B_ONLY_164", "personal")
        self.assertEqual(status, 200)
        evidence_b_personal = json.dumps(response_b_personal["evidence"], ensure_ascii=False)
        self.assertIn("PERS_B_ONLY_164", evidence_b_personal)
        self.assertNotIn("PERS_A_ONLY_738", evidence_b_personal)
        self.assertNotIn("ENT_MEM_SHARED_483", evidence_b_personal)

        status, denied_rule = self.clients["employee_a"].request("POST", "/api/rules", {
            "rule_key": "test.denied", "layer": "foundation", "title": "no", "content": "no",
        })
        self.assertEqual(status, 403)
        status, denied_memory = self.clients["employee_a"].request("POST", "/api/memories", {
            "scope": "enterprise", "title": "no", "content": "no",
        })
        self.assertEqual(status, 403)

    def test_monthly_analytics_counts_and_finance_fields_are_role_correct(self):
        month = (date.today().replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
        inside1, inside2, inside3 = (month + suffix for suffix in ("-05", "-12", "-22"))
        current_month = date.today().strftime("%Y-%m")
        outside = current_month + "-03"
        owner_id = self.user_rows["owner_feat"]["id"]
        employee_id = self.user_rows["employee_a"]["id"]

        status, snapshot = self.clients["owner_feat"].request("GET", "/api/state")
        self.assertEqual(status, 200)
        state = snapshot["state"]
        docs = [
            {"number": "M-OWNER-1", "date": inside1, "destination": "North", "weight": "1.234", "amount": "100.50", "created_by": owner_id},
            {"number": "M-OWNER-2", "date": inside2, "destination": "South", "weight": "2", "amount": "200", "created_by": owner_id},
            {"number": "M-EMP-1", "date": inside3, "destination": "North", "weight": "0.25", "amount": "50", "created_by": employee_id},
            {"number": "M-OUTSIDE", "date": outside, "destination": "Other", "weight": "99", "amount": "999", "created_by": owner_id},
        ]
        receipts = [
            {"number": "M-OWNER-1", "photo": {"off": False}, "paper": {"off": False}, "delivered": {"off": True}},
            {"number": "M-OWNER-2", "delivered": {"off": False}},
            {"number": "M-EMP-1", "photo": {"off": False}},
        ]
        fees = [
            {"number": "M-OWNER-1", "date": inside1, "status": "已确认", "amount": "10"},
            {"number": "M-OWNER-2", "date": inside2, "status": "待确认", "amount": "11"},
            {"number": "M-OWNER-2", "status": "有差异", "amount": "12"},
            {"number": "M-EMP-1", "date": inside3, "status": "已确认", "amount": "7.5"},
            {"number": "M-OWNER-1", "date": outside, "status": "已确认", "amount": "999"},
            {"number": "M-OUTSIDE", "status": "已确认", "amount": "555"},
        ]
        state["julong_demo_waybills_v2"] = json.dumps(docs, ensure_ascii=False)
        state["julong_demo_receipts_v1"] = json.dumps(receipts, ensure_ascii=False)
        state["julong_demo_fees_v1"] = json.dumps(fees, ensure_ascii=False)
        # Historical finance rows bypass current write gates only in this temporary DB.
        with self.app.db() as con:
            con.execute("UPDATE work_state SET payload=? WHERE id=1", (json.dumps(state),))

        status, owner = self.clients["owner_feat"].request("GET", "/api/analytics/monthly?month=" + month)
        self.assertEqual(status, 200)
        self.assertEqual(owner["activity"]["waybills"], 3)
        self.assertEqual(owner["activity"]["weight_tonnes"], 3.484)
        self.assertEqual(owner["activity"]["by_destination"], [
            {"destination": "North", "count": 2}, {"destination": "South", "count": 1},
        ])
        self.assertEqual(owner["receipts"], {"photo_received": 2, "paper_returned": 1, "delivered_to_factory": 1, "outstanding": 2})
        self.assertEqual(owner["fees"], {"records": 4, "confirmed": 2, "pending": 1, "disputed": 1})
        self.assertEqual(owner["finance"], {
            "manufacturer_freight_total": 350.5,
            "confirmed_partner_fees_total": 17.5,
            "incomplete_waybill_amounts": 0,
            "incomplete_confirmed_fee_amounts": 0,
        })

        status, employee = self.clients["employee_a"].request("GET", "/api/analytics/monthly?month=" + month)
        self.assertEqual(status, 200)
        self.assertEqual(employee["activity"]["waybills"], 1)
        self.assertEqual(employee["activity"]["weight_tonnes"], 0.25)
        self.assertEqual(employee["receipts"], {"photo_received": 1, "paper_returned": 0, "delivered_to_factory": 0, "outstanding": 1})
        self.assertEqual(employee["fees"], {"records": 1, "confirmed": 1, "pending": 0, "disputed": 0})
        self.assertNotIn("finance", employee)
        self.assertNotIn("100.50", json.dumps(employee, ensure_ascii=False))

    def test_monthly_finance_totals_flag_incomplete_amounts_instead_of_zeroing_them(self):
        month = date.today().strftime("%Y-%m")
        day1, day2 = month + "-05", month + "-12"
        owner_id = self.user_rows["owner_feat"]["id"]
        status, snapshot = self.clients["owner_feat"].request("GET", "/api/state")
        self.assertEqual(status, 200)
        state = snapshot["state"]
        state["julong_demo_waybills_v2"] = json.dumps([
            {"number": "INCOMPLETE-1", "date": day1, "amount": "100.00", "created_by": owner_id},
            {"number": "INCOMPLETE-2", "date": day2, "amount": "", "created_by": owner_id},
        ])
        state["julong_demo_fees_v1"] = json.dumps([
            {"number": "INCOMPLETE-1", "date": day1, "status": "已确认", "amount": "7.50"},
            {"number": "INCOMPLETE-2", "date": day2, "status": "已确认", "amount": "not-a-number"},
            # Unconfirmed rows do not contribute to confirmed-fee total/completeness.
            {"number": "INCOMPLETE-2", "date": day2, "status": "待确认", "amount": ""},
        ])
        # Historical finance rows bypass current write gates only in this temporary DB.
        with self.app.db() as con:
            con.execute("UPDATE work_state SET payload=? WHERE id=1", (json.dumps(state),))

        status, result = self.clients["owner_feat"].request(
            "GET", "/api/analytics/monthly?month=" + month
        )
        self.assertEqual(status, 200)
        self.assertEqual(result["fees"], {"records": 3, "confirmed": 2, "pending": 1, "disputed": 0})
        self.assertEqual(result["finance"], {
            "manufacturer_freight_total": None,
            "confirmed_partner_fees_total": None,
            "incomplete_waybill_amounts": 1,
            "incomplete_confirmed_fee_amounts": 1,
        })

    def test_audit_records_rule_memory_and_ai_actions_without_content_leak(self):
        today = date.today().isoformat()
        status, rule = self._create_rule("audit.test", "audited rule", "AUDIT_RULE_SECRET_112", today)
        self.assertEqual(status, 201)
        status, memory = self.clients["employee_a"].request("POST", "/api/memories", {
            "scope": "personal", "title": "private memory", "content": "AUDIT_MEMORY_SECRET_992",
        }, {"Idempotency-Key": "audit-memory-test"})
        self.assertEqual(status, 201)
        status, _ = self._chat("employee_a", "AUDIT_MEMORY_SECRET_992")
        self.assertEqual(status, 200)
        status, audit = self.clients["owner_feat"].request("GET", "/api/audit?limit=100")
        self.assertEqual(status, 200)
        items = audit["items"]
        rule_event = next(item for item in items if item["action"] == "rule.version.create" and item["entity_id"] == rule["item"]["id"])
        memory_event = next(item for item in items if item["action"] == "memory.create" and item["entity_id"] == memory["item"]["id"])
        ai_event = next(item for item in items if item["action"] == "ai.chat")
        self.assertEqual(rule_event["user_id"], self.user_rows["owner_feat"]["id"])
        self.assertEqual(json.loads(rule_event["details"]), {
            "rule_key": "audit.test", "version": 1, "effective_at": today, "finance_only": False,
        })
        self.assertEqual(memory_event["user_id"], self.user_rows["employee_a"]["id"])
        self.assertEqual(json.loads(memory_event["details"]), {"scope": "personal", "finance_only": False})
        self.assertEqual(ai_event["user_id"], self.user_rows["employee_a"]["id"])
        self.assertEqual(json.loads(ai_event["details"])["status"], 200)
        audit_text = json.dumps(items, ensure_ascii=False)
        self.assertNotIn("AUDIT_RULE_SECRET_112", audit_text)
        self.assertNotIn("AUDIT_MEMORY_SECRET_992", audit_text)

    def test_ai_context_role_redaction_and_caller_bound_memory(self):
        today = date.today().isoformat()
        status, _ = self._create_rule("finance.redaction", "finance rule", "AI_FINANCE_RULE_532", today, True)
        self.assertEqual(status, 201)
        status, snapshot = self.clients["owner_feat"].request("GET", "/api/state")
        state = snapshot["state"]
        state["julong_demo_waybills_v2"] = json.dumps([{
            "number": "ROLE-TEST-001", "date": today, "destination": "Test",
            "recipient": "Synthetic", "amount": "AI_FINANCE_AMOUNT_493",
        }])
        # Historical finance rows bypass current write gates only in this temporary DB.
        with self.app.db() as con:
            con.execute("UPDATE work_state SET payload=? WHERE id=1", (json.dumps(state),))
        status, _ = self.clients["owner_feat"].request("POST", "/api/memories", {
            "scope": "personal", "title": "finance memory", "content": "AI_FINANCE_MEMORY_740", "finance_only": True,
        }, {"Idempotency-Key": "finance-memory-only"})
        self.assertEqual(status, 201)

        status, employee = self._chat("employee_a", "查 ROLE-TEST-001 与 AI_FINANCE_RULE_532")
        self.assertEqual(status, 200)
        employee_evidence = json.dumps(employee["evidence"], ensure_ascii=False)
        self.assertNotIn("AI_FINANCE_AMOUNT_493", employee_evidence)
        self.assertNotIn("AI_FINANCE_RULE_532", employee_evidence)
        self.assertNotIn("AI_FINANCE_MEMORY_740", employee_evidence)

        status, owner = self._chat("owner_feat", "查 ROLE-TEST-001 AI_FINANCE_RULE_532 AI_FINANCE_MEMORY_740", "personal")
        self.assertEqual(status, 200)
        owner_evidence = json.dumps(owner["evidence"], ensure_ascii=False)
        self.assertNotIn("AI_FINANCE_AMOUNT_493", owner_evidence)
        self.assertIn("AI_FINANCE_RULE_532", owner_evidence)
        self.assertIn("AI_FINANCE_MEMORY_740", owner_evidence)


if __name__ == "__main__":
    unittest.main(verbosity=2)
