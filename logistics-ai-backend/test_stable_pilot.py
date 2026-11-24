"""Focused regression tests for release fixes; no network or production state."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import module_scope
from scripts import pilot_cutover
from ai.service import AIService, ChatRequest, ConversationStore, EXECUTION_JOB_ID
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


if __name__ == "__main__":
    unittest.main()
