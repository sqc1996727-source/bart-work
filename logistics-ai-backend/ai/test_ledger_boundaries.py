"""Isolated checks for snapshot and receipt write preconditions."""

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import unittest

from ai import test_business_task_conditions as harness


class LedgerBoundariesTests(unittest.TestCase):
    def setUp(self):
        self.fixture = harness.BusinessTaskConditionTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.app = self.fixture.app
        self.owner = self.fixture.clients["owner_cond"]
        self.actor = self.fixture.users["owner_cond"]

    def snapshot(self):
        return self.owner.request("GET", "/api/state")[1]

    def write(self, rev, state):
        return self.owner.request("PUT", "/api/state", {"base_rev": rev, "state": state})

    def test_snapshot_input_validation_preserves_ledger(self):
        first = self.snapshot()
        docs = self.app.STATE_KEYS[0]
        initial = {**first["state"], docs: json.dumps([{"number": "T-101"}])}
        status, _ = self.write(first["rev"], initial)
        self.assertEqual(status, 200)
        current = self.snapshot()
        invalid_states = (
            {docs: initial[docs]},
            {**current["state"], docs: '[null]'},
            {**current["state"], docs: json.dumps([{"number": "T-101"}, {"number": "T-101"}])},
            {**current["state"], docs: json.dumps([{"number": "T-101", "destination": ["Invalid"]}])},
        )
        for invalid in invalid_states:
            self.assertEqual(self.write(current["rev"], invalid)[0], 400)
        self.assertEqual(self.snapshot(), current)

    def test_snapshot_demo_fee_is_rejected(self):
        first = self.snapshot()
        keys = self.app.STATE_KEYS
        state = {**first["state"], keys[0]: '[{"number":"T-101"}]',
                 keys[4]: json.dumps([{"id": "fee1", "number": "T-101", "type": "进仓费", "status": "待确认", "demo": True}])}
        status, result = self.write(first["rev"], state)
        self.assertEqual((status, result["error"]), (409, "demo_records_forbidden"))
        self.assertEqual(self.snapshot(), first)

    def test_two_writers_cannot_commit_the_same_revision_or_be_overwritten_by_legacy_import(self):
        first = self.snapshot()
        key = self.app.STATE_KEYS[0]
        barrier = Barrier(2)

        def writer(number):
            barrier.wait()
            return self.write(first["rev"], {**first["state"], key: json.dumps([{"number": number}])})[0]

        with ThreadPoolExecutor(2) as executor:
            self.assertEqual(sorted(executor.map(writer, ("T-101", "T-102"))), [200, 409])
        winner = self.snapshot()
        (self.app.DATA_DIR / "state.json").write_text(
            json.dumps({"rev": 10, "state": {key: [{"number": "LEGACY"}]}}), encoding="utf-8",
        )
        self.app._import_legacy_state_if_empty()
        self.assertEqual(self.snapshot(), winner)

    def test_receipt_legacy_boolean_revocation_takes_effect_and_replay_keeps_rev(self):
        first = self.snapshot()
        keys = self.app.STATE_KEYS
        status, saved = self.write(first["rev"], {
            **first["state"], keys[0]: '[{"number":"T-101"}]', keys[5]: '[{"number":"T-101","delivered":true}]',
        })
        self.assertEqual(status, 200)
        args = {"waybill_number": "T-101", "field": "delivered", "active": False, "expected_rev": saved["rev"]}
        response = self.app.update_receipt_status_record(self.actor, args, "receipt-revoke")
        self.assertFalse(response["active"])
        self.assertTrue(response["receipt"]["delivered"]["off"])
        self.assertEqual(self.app.update_receipt_status_record(self.actor, args, "receipt-revoke"), response)
        with self.assertRaisesRegex(ValueError, "revision_conflict"):
            self.app.update_receipt_status_record(self.actor, {**args, "active": True}, "stale-receipt")
        snapshot = self.snapshot()
        self.assertFalse(self.app._task_condition_met("receipt_delivered", "T-101", snapshot["state"]))
        self.assertEqual(snapshot["rev"], response["state_rev"])

    def test_staff_can_read_own_file_but_not_higher_rank_shared_file(self):
        from capabilities import files

        employee = self.fixture.users["employee_cond"]
        initial = self.snapshot()
        self.assertEqual(self.write(initial["rev"], {
            **initial["state"], self.app.STATE_KEYS[0]: json.dumps([{"number": "T-101", "created_by": employee["id"]}]),
        })[0], 200)
        own = files.store(self.app, employee, "receipt.csv", b"ticket\nT-101\n")
        private = files.store(self.app, self.actor, "finance.csv", b"ticket\nT-101\n")
        self.assertEqual(files.authorized(self.app, employee, own["id"])["id"], own["id"])
        with self.app.db() as con:
            con.execute("INSERT INTO case_files VALUES(?,?,?,?,?,?)",
                        (employee["tenant_id"], "T-101", private["id"], 0, self.actor["id"], self.app.now_iso()))
        with self.assertRaises(LookupError):
            files.authorized(self.app, employee, private["id"])
