"""调度可见性回归：费用域保留待批金额，运单域厂家运费继续锁死。

全部使用合成数据 + 临时 DATA_DIR，不读真实 data/ 目录。

覆盖：
  1. dispatcher 看 /api/state 费用域能看到每笔 fee 的 amount 原值。
  2. dispatcher 看运单域没有 amount / freightTotal / feeWarehouse。
  3. employee 看费用域没有 amount，全量 payload 不含任何金额哨兵。
  4. owner_finance 费用域全量可见（含 amount）。
  5. 费用行里非 amount 的敏感键（feeWarehouse）对调度仍隐藏；厂家运费类费用行整行不露金额。
  6. HTTP：非财务角色提交带金额字段的 payload 仍被 403 拦截（写侧不放宽）。
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from ingestion import test_host_http as http_test

ROOT = Path(__file__).resolve().parent

WAYBILL_AMOUNT = "8765.43"
WAYBILL_FREIGHT_TOTAL = "7654.32"
WAYBILL_FEE_WAREHOUSE = "6543.21"
FEE_AMOUNT = "1200.50"
FEE_OTHER_AMOUNT = "1300.75"
FEE_WAREHOUSE = "7777.77"
PROTECTED_FEE_AMOUNT = "5200.00"
BILL_AMOUNT = "999.99"


class _IsolatedApp(unittest.TestCase):
    """Load app.py against a temporary DATA_DIR; never touches the real ledger."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="julong-fee-visibility-")
        self.old_data_dir = os.environ.get("DATA_DIR")
        os.environ["DATA_DIR"] = self.tmp.name
        name = f"fee_visibility_app_{os.getpid()}_{id(self)}"
        spec = importlib.util.spec_from_file_location(name, ROOT / "app.py")
        self.app = importlib.util.module_from_spec(spec)
        sys.modules[name] = self.app
        spec.loader.exec_module(self.app)
        self.app.init_db()
        self.app.add_user("boss", "老板", "owner_finance", "password123")
        self.app.add_user("disp", "调度", "dispatcher", "password123")
        self.app.add_user("emp", "员工", "employee", "password123")
        with self.app.db() as con:
            self.users = {row["username"]: dict(row) for row in con.execute("SELECT * FROM users")}

    def tearDown(self):
        if self.old_data_dir is None:
            os.environ.pop("DATA_DIR", None)
        else:
            os.environ["DATA_DIR"] = self.old_data_dir
        self.tmp.cleanup()

    def _seed(self, *, fee_number="SYN-1", waybill_number="SYN-1"):
        employee_id = self.users["emp"]["id"]
        state = {key: "[]" for key in self.app.STATE_KEYS}
        state[self.app.STATE_KEYS[0]] = json.dumps([{
            "number": waybill_number,
            "date": "2026-11-12",
            "cargo": "合成货物",
            "created_by": employee_id,
            "amount": WAYBILL_AMOUNT,
            "freightTotal": WAYBILL_FREIGHT_TOTAL,
            "feeWarehouse": WAYBILL_FEE_WAREHOUSE,
        }], ensure_ascii=False)
        state[self.app.STATE_KEYS[4]] = json.dumps([
            {"id": "F1", "number": fee_number, "type": "进仓费", "status": "待确认",
             "amount": FEE_AMOUNT, "feeWarehouse": FEE_WAREHOUSE},
            {"id": "F2", "number": fee_number, "type": "送货费", "status": "有差异",
             "amount": FEE_OTHER_AMOUNT},
            {"id": "F3", "number": fee_number, "type": "厂家运费", "status": "待确认",
             "amount": PROTECTED_FEE_AMOUNT},
        ], ensure_ascii=False)
        state[self.app.STATE_KEYS[6]] = json.dumps([{"number": fee_number, "amount": BILL_AMOUNT}],
                                                   ensure_ascii=False)
        with self.app.db() as con:
            con.execute("UPDATE work_state SET payload=? WHERE id=1",
                        (json.dumps(state, ensure_ascii=False),))
        return state

    def _visible(self, role):
        with self.app.db() as con:
            row = con.execute("SELECT payload FROM work_state WHERE id=1").fetchone()
        principal = self.users[{"dispatcher": "disp", "employee": "emp", "owner_finance": "boss"}[role]]["id"]
        return self.app._visible_state(self.app._decode_state(row["payload"]), role, principal)


class FeeAmountVisibilityTests(_IsolatedApp):
    def test_dispatcher_sees_fee_amount(self):
        self._seed()
        visible = self._visible("dispatcher")
        fees = json.loads(visible[self.app.STATE_KEYS[4]])
        by_id = {row["id"]: row for row in fees}
        self.assertEqual(by_id["F1"]["amount"], FEE_AMOUNT)
        self.assertEqual(by_id["F2"]["amount"], FEE_OTHER_AMOUNT)
        self.assertEqual(by_id["F1"]["status"], "待确认")

    def test_dispatcher_waybill_amounts_stay_closed(self):
        self._seed()
        visible = self._visible("dispatcher")
        waybills = json.loads(visible[self.app.STATE_KEYS[0]])
        row = next(item for item in waybills if item["number"] == "SYN-1")
        for field in ("amount", "freightTotal", "feeWarehouse"):
            self.assertNotIn(field, row)
        blob = json.dumps(visible, ensure_ascii=False)
        for sentinel in (WAYBILL_AMOUNT, WAYBILL_FREIGHT_TOTAL, WAYBILL_FEE_WAREHOUSE):
            self.assertNotIn(sentinel, blob)
        # 账单域仍整域隐藏（差异候选金额未对调度放开）。
        self.assertEqual(json.loads(visible[self.app.STATE_KEYS[6]]), [])
        self.assertNotIn(BILL_AMOUNT, blob)

    def test_dispatcher_fee_row_keeps_other_sensitive_keys_closed(self):
        self._seed()
        fees = json.loads(self._visible("dispatcher")[self.app.STATE_KEYS[4]])
        f1 = next(row for row in fees if row["id"] == "F1")
        self.assertNotIn("feeWarehouse", f1)
        self.assertNotIn(FEE_WAREHOUSE, json.dumps(fees, ensure_ascii=False))

    def test_dispatcher_protected_freight_fee_row_hides_amount(self):
        self._seed()
        visible = self._visible("dispatcher")
        protected = next(row for row in json.loads(visible[self.app.STATE_KEYS[4]]) if row["id"] == "F3")
        self.assertNotIn("amount", protected)
        self.assertNotIn(PROTECTED_FEE_AMOUNT, json.dumps(visible, ensure_ascii=False))

    def test_employee_sees_no_amount_anywhere(self):
        self._seed()
        visible = self._visible("employee")
        fees = json.loads(visible[self.app.STATE_KEYS[4]])
        self.assertEqual(len(fees), 3, "员工应看得见自己名下运单的费用行（只去掉金额）")
        for row in fees:
            self.assertNotIn("amount", row)
            self.assertNotIn("feeWarehouse", row)
        blob = json.dumps(visible, ensure_ascii=False)
        for sentinel in (FEE_AMOUNT, FEE_OTHER_AMOUNT, PROTECTED_FEE_AMOUNT, FEE_WAREHOUSE,
                         WAYBILL_AMOUNT, WAYBILL_FREIGHT_TOTAL, WAYBILL_FEE_WAREHOUSE, BILL_AMOUNT):
            self.assertNotIn(sentinel, blob)

    def test_employee_sees_no_other_waybills_fees(self):
        self._seed()
        state = {key: "[]" for key in self.app.STATE_KEYS}
        state[self.app.STATE_KEYS[0]] = json.dumps([
            {"number": "SYN-OWN", "date": "2026-11-12", "cargo": "合成货物",
             "created_by": self.users["emp"]["id"]},
            {"number": "SYN-OTHER", "date": "2026-11-12", "cargo": "合成货物",
             "created_by": self.users["boss"]["id"]},
        ], ensure_ascii=False)
        state[self.app.STATE_KEYS[4]] = json.dumps([
            {"id": "FO", "number": "SYN-OWN", "type": "进仓费", "status": "待确认", "amount": FEE_AMOUNT},
            {"id": "FX", "number": "SYN-OTHER", "type": "进仓费", "status": "待确认", "amount": FEE_OTHER_AMOUNT},
        ], ensure_ascii=False)
        with self.app.db() as con:
            con.execute("UPDATE work_state SET payload=? WHERE id=1",
                        (json.dumps(state, ensure_ascii=False),))
        visible = self._visible("employee")
        fees = json.loads(visible[self.app.STATE_KEYS[4]])
        self.assertEqual([row["id"] for row in fees], ["FO"])
        self.assertNotIn("amount", fees[0])

    def test_owner_sees_fee_domain_in_full(self):
        self._seed()
        visible = self._visible("owner_finance")
        fees = {row["id"]: row for row in json.loads(visible[self.app.STATE_KEYS[4]])}
        self.assertEqual(fees["F1"]["amount"], FEE_AMOUNT)
        self.assertEqual(fees["F1"]["feeWarehouse"], FEE_WAREHOUSE)
        self.assertEqual(fees["F3"]["amount"], PROTECTED_FEE_AMOUNT)
        bills = json.loads(visible[self.app.STATE_KEYS[6]])
        self.assertEqual(bills[0]["amount"], BILL_AMOUNT)


class StateWriteGateHTTPTests(unittest.TestCase):
    """Real HTTP on a free port against a temporary DATA_DIR; write gate must stay closed."""

    setUp = http_test.IngestionHostHTTPTests.setUp
    tearDown = http_test.IngestionHostHTTPTests.tearDown
    _start_server = http_test.IngestionHostHTTPTests._start_server

    def test_dispatcher_fee_amount_visible_but_amount_writes_rejected(self):
        owner, dispatcher, employee = self.clients["owner"], self.clients["dispatcher"], self.clients["employee"]
        status, snapshot, _, _ = owner.request("GET", "/api/state")
        self.assertEqual(status, 200)
        key_waybill = "julong_demo_waybills_v2"
        key_fees = "julong_demo_fees_v1"
        state = snapshot["state"]
        state[key_waybill] = json.dumps([{
            "number": "SYN-HTTP-1", "date": "2026-11-12", "receiver": "合成",
            "recipient": "合成", "phone": "13000000000", "address": "合成路1号",
            "destination": "测试", "cargo": "合成货物", "carrier": "合成承运",
            "created_by": "synthetic-owner",
        }], ensure_ascii=False)
        state[key_fees] = json.dumps([{
            "id": "HF1", "number": "SYN-HTTP-1", "type": "进仓费", "status": "待确认",
            "amount": FEE_AMOUNT,
        }], ensure_ascii=False)
        status, result, _, _ = owner.request("PUT", "/api/state", {"base_rev": snapshot["rev"], "state": state})
        self.assertEqual(status, 200, result)
        rev = result["rev"]
        status, recon_result, _, _ = owner.request(
            "POST", "/api/reconciliation/waybills",
            {"number": "SYN-HTTP-1", "amount": WAYBILL_AMOUNT, "base_rev": rev},
            {"Idempotency-Key": "synthetic-fee-visibility-1"},
        )
        self.assertEqual(status, 200, recon_result)

        status, dispatcher_state, _, _ = dispatcher.request("GET", "/api/state")
        self.assertEqual(status, 200)
        fees = json.loads(dispatcher_state["state"][key_fees])
        self.assertEqual(fees[0]["amount"], FEE_AMOUNT)
        waybill = json.loads(dispatcher_state["state"][key_waybill])[0]
        for field in ("amount", "freightTotal", "feeWarehouse"):
            self.assertNotIn(field, waybill)
        self.assertNotIn(WAYBILL_AMOUNT, json.dumps(dispatcher_state, ensure_ascii=False))

        # 调度把带 amount 的费用域原样回推 => 仍被写侧拦截
        status, _, _, _ = dispatcher.request("PUT", "/api/state", {
            "base_rev": dispatcher_state["rev"], "state": dispatcher_state["state"],
        })
        self.assertEqual(status, 403)

        # 调度给运单塞 amount => 同样被拦截
        tampered = dict(dispatcher_state["state"])
        rows = json.loads(tampered[key_waybill])
        rows[0]["amount"] = "9999.99"
        tampered[key_waybill] = json.dumps(rows, ensure_ascii=False)
        tampered[key_fees] = json.dumps(
            [{k: v for k, v in fees[0].items() if k != "amount"}], ensure_ascii=False)
        status, _, _, _ = dispatcher.request("PUT", "/api/state", {
            "base_rev": dispatcher_state["rev"], "state": tampered,
        })
        self.assertEqual(status, 403)

        status, employee_state, _, _ = employee.request("GET", "/api/state")
        self.assertEqual(status, 200)
        employee_blob = json.dumps(employee_state, ensure_ascii=False)
        self.assertNotIn(FEE_AMOUNT, employee_blob)
        self.assertNotIn(WAYBILL_AMOUNT, employee_blob)


if __name__ == "__main__":
    unittest.main()
