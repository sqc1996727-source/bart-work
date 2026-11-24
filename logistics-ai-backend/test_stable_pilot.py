"""Focused regression tests for release fixes; no network or production state."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app
import finance_confirmation

import module_scope
from scripts import pilot_cutover
from ai.service import AIService, AIServiceError, ChatRequest, ConversationStore, EXECUTION_JOB_ID
from ai.agent_policy import ExecutionPolicy
from ai.test_service import Context, Tools, Model


class ModulePolicyTests(unittest.TestCase):
    def test_disabled_data_is_hidden_but_preserved(self):
        profile = {"industry": "logistics", "modules": ("transport",)}
        receipt_key = "julong_demo_receipts_v1"
        fee_key = "julong_demo_fees_v1"
        waybill_key = "julong_demo_waybills_v2"
        old = {
            receipt_key: json.dumps([{"number": "TEST-1", "paper": True}]),
            fee_key: json.dumps([{"id": "F-1", "amount": 50}]),
            waybill_key: json.dumps([{"number": "TEST-1", "amount": "100"}]),
        }
        for key in module_scope.suppressed_keys(profile):
            old.setdefault(key, "[]")
        visible = module_scope.project(profile, old)
        self.assertEqual(visible[receipt_key], "[]")
        self.assertEqual(visible[fee_key], "[]")
        self.assertNotIn("amount", json.loads(visible[waybill_key])[0])
        parsed = {key: json.loads(value) for key, value in visible.items()}
        merged = dict(visible)
        module_scope.preserve_disabled(profile, old, parsed, merged)
        self.assertEqual(merged[receipt_key], old[receipt_key])
        self.assertEqual(merged[fee_key], old[fee_key])
        parsed[receipt_key] = [{"number": "UNAUTHORIZED"}]
        with self.assertRaises(PermissionError):
            module_scope.preserve_disabled(profile, old, parsed, merged)

    def test_route_guards_cover_secondary_surfaces(self):
        profile = {"industry": "retail", "modules": ()}
        for route in ("/api/attachments/x", "/api/generated-documents/x",
                      "/api/analytics/monthly", "/api/inspection",
                      "/api/reconciliation/waybills", "/api/waybills/import/preview"):
            self.assertTrue(module_scope.route_denied(profile, route), route)
        self.assertFalse(module_scope.route_denied(profile, "/api/agent/jobs"))


class SnapshotTests(unittest.TestCase):
    def test_same_primary_key_with_old_content_fails(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data = root / "data"; data.mkdir()
            with sqlite3.connect(data / "julong.sqlite3") as con:
                con.execute("CREATE TABLE work_state(id INTEGER PRIMARY KEY, rev INTEGER, payload TEXT)")
                con.execute("INSERT INTO work_state VALUES(1,70,'new')")
            snapshot = root / "snapshot"
            self.assertEqual(pilot_cutover.export(data, None, snapshot), 0)
            self.assertEqual(pilot_cutover.verify(data, snapshot), 0)
            with sqlite3.connect(data / "julong.sqlite3") as con:
                con.execute("UPDATE work_state SET rev=69,payload='old' WHERE id=1")
            self.assertEqual(pilot_cutover.compare(snapshot, data), 1)
            self.assertEqual(pilot_cutover.verify(data, snapshot), 1)

    def test_added_and_deleted_rows_fail_comparison(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); a = root / "a"; b = root / "b"
            a.mkdir(); b.mkdir()
            for path in (a,b):
                with sqlite3.connect(path / "julong.sqlite3") as con:
                    con.execute("CREATE TABLE rows(id INTEGER PRIMARY KEY, payload BLOB)")
            with sqlite3.connect(a / "julong.sqlite3") as con:
                con.execute("INSERT INTO rows VALUES(1,?)", (b"one",))
            with sqlite3.connect(b / "julong.sqlite3") as con:
                con.execute("INSERT INTO rows VALUES(2,?)", (b"two",))
            self.assertTrue(pilot_cutover.compare_directories(a,b))


class CancelledRecoveryTests(unittest.TestCase):
    def test_cancelled_prior_job_is_not_replayed(self):
        class CancelAwareTools(Tools):
            def recovery_permitted(self, event, request):
                return event["job_id"] != "cancelled-job"
        with tempfile.TemporaryDirectory() as temp:
            store = ConversationStore(str(Path(temp) / "ai.sqlite3"))
            request = ChatRequest("company-a", "user-a", "first")
            cid = store.create_or_check(request)
            token = EXECUTION_JOB_ID.set("cancelled-job")
            try:
                event = store.prepare_tool(cid, "pending", "get_waybill", {"waybill_id":"123"})
            finally:
                EXECUTION_JOB_ID.reset(token)
            executor = CancelAwareTools()
            service = AIService(store,Context(),executor,Model([{"content":"继续核实"}]),execution_policy=ExecutionPolicy())
            service.chat(ChatRequest("company-a","user-a","continue",conversation_id=cid))
            self.assertEqual(executor.calls,[])
            with store._connect() as con:
                row=con.execute("SELECT state,error_code FROM tool_events WHERE id=?", (event["id"],)).fetchone()
            self.assertEqual(row["state"],"failed")
            self.assertEqual(row["error_code"],"RECOVERY_CANCELLED_OR_UNKNOWN")


class FinanceRejectionTests(unittest.TestCase):
    """Exercise the real finance validator, V3 tool adapter and durable journal."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        profile = {**app.PRODUCT, "industry": "logistics",
                   "modules": ("transport", "receipt", "finance")}
        for target, value in (("DATA_DIR", self.root),
                              ("DB_PATH", self.root / "ledger.sqlite3"),
                              ("PRODUCT", profile)):
            patcher = patch.object(app, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        state = {key: "[]" for key in app.STATE_KEYS}
        state[app.STATE_KEYS[0]] = json.dumps([{"number": "TEST-1"}])
        state[app.STATE_KEYS[4]] = json.dumps([
            {"id": "F-2001", "number": "TEST-1", "status": "已确认",
             "type": "进仓费", "amount": "50"},
            {"id": "F-2002", "number": "TEST-1", "status": "待确认",
             "type": "进仓费", "amount": "60"},
        ], ensure_ascii=False)
        with app.db() as con:
            con.execute("CREATE TABLE users(id TEXT, tenant_id TEXT, role TEXT, active INTEGER)")
            con.execute("INSERT INTO users VALUES(?,?,?,1)",
                        ("test-owner", app.TENANT_ID, "owner_finance"))
            con.execute("CREATE TABLE work_state(id INTEGER PRIMARY KEY, rev INTEGER, payload TEXT)")
            con.execute("INSERT INTO work_state VALUES(1,70,?)", (json.dumps(state),))
            con.execute("""CREATE TABLE idempotency(user_id TEXT, key TEXT, request_hash TEXT,
                response_status INTEGER, response_json TEXT, created_at TEXT,
                PRIMARY KEY(user_id,key))""")
            finance_confirmation._table(con)
        self.store = ConversationStore(str(self.root / "chat.sqlite3"))
        self.request = ChatRequest(app.TENANT_ID, "test-owner", "核实费用变更")
        self.args = {"fee_id": "F-2001", "status": "待确认"}

    @staticmethod
    def call(cid, args):
        return {"content": None, "tool_calls": [{"id": cid, "type": "function",
                "function": {"name": "update_fee_status",
                             "arguments": json.dumps(args, ensure_ascii=False)}}]}

    def service(self, model):
        return AIService(self.store, Context(), app.V3Tools(), model,
                         execution_policy=ExecutionPolicy())

    def test_rejected_fee_retry_returns_cached_failure_without_mutation(self):
        model = Model([self.call("first", self.args),
                       self.call("repeat", self.args),
                       {"content": "已确认费用不能直接退回，未生成候选。"}])
        with patch.object(finance_confirmation, "create", wraps=finance_confirmation.create) as execute:
            result = self.service(model).chat(self.request)
            self.assertEqual(execute.call_count, 1)
        self.assertIn("未生成候选", result.reply)
        self.assertEqual([e["state"] for e in result.tool_events],
                         ["rejected", "rejected"])
        self.assertEqual(result.tool_events[0]["result"]["error"],
                         "confirmed_fee_requires_reversal")
        self.assertTrue(result.tool_events[1]["result"]["replayed_rejection"])
        with self.store._connect() as con:
            events = con.execute("SELECT state,error_code FROM tool_events").fetchall()
        self.assertEqual([tuple(e) for e in events],
                         [("failed", "confirmed_fee_requires_reversal")])
        with app.db() as con:
            self.assertEqual(con.execute("SELECT count(*) FROM finance_candidates").fetchone()[0], 0)
            row = con.execute("SELECT rev,payload FROM work_state WHERE id=1").fetchone()
            self.assertEqual(row["rev"], 70)
            self.assertEqual(json.loads(json.loads(row["payload"])[app.STATE_KEYS[4]])[0]["status"], "已确认")

    def test_corrected_request_can_create_candidate_without_confirming_ledger(self):
        valid = {"fee_id": "F-2002", "status": "已确认"}
        model = Model([self.call("rejected", self.args), self.call("valid", valid),
                       self.call("repeat-valid", valid), {"content": "第二笔候选待审阅"}])
        with patch.object(finance_confirmation, "_fee_types", return_value={"进仓费"}):
            result = self.service(model).chat(self.request)
        self.assertIn("候选", result.reply)
        with app.db() as con:
            candidates = con.execute("SELECT fee_id,target_status,completed_at FROM finance_candidates").fetchall()
            self.assertEqual([tuple(c) for c in candidates], [("F-2002", "已确认", None)])
            self.assertEqual(con.execute("SELECT rev FROM work_state").fetchone()[0], 70)

    def test_uncertain_execution_failure_is_not_treated_as_business_rejection(self):
        service = self.service(Model([]))
        cid = self.store.create_or_check(self.request)
        event = self.store.prepare_tool(cid, "uncertain", "update_fee_status", self.args)
        self.store.fail_tool(event["id"], "TOOL_EXECUTION_FAILED")
        with self.store._connect() as con:
            failed = con.execute("SELECT * FROM tool_events WHERE id=?", (event["id"],)).fetchone()
        with self.assertRaises(AIServiceError) as caught:
            service._execute_event(failed, self.request)
        self.assertEqual(caught.exception.code, "TOOL_RECOVERY_BLOCKED")

    def test_unknown_validation_message_is_not_exposed(self):
        result = finance_confirmation.candidate_rejection(ValueError("private details"))
        self.assertEqual(result["error"], "invalid_request")
        self.assertNotIn("private details", json.dumps(result, ensure_ascii=False))
        self.assertFalse(result["candidate_created"])


if __name__ == "__main__":
    unittest.main()
