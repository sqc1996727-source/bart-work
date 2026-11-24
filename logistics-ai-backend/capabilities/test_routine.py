"""主动干活引擎（Routine + bot_state）的针对性测试。

覆盖：
  1. Routine 扫描：费用空时守卫（不误报「费用未落账」）、回单催办正确扫出。
  2. 费用有数据时，能扫出「费用未落账」和「费用缺归属」。
  3. bot_state 读写闭环：跑完自动记任务态、能读回、覆盖更新。
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class RoutineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="julong-routine-")
        self.old_data_dir = os.environ.get("DATA_DIR")
        os.environ["DATA_DIR"] = self.tmp.name
        name = f"routine_app_{os.getpid()}_{id(self)}"
        spec = importlib.util.spec_from_file_location(name, ROOT / "app.py")
        self.app = importlib.util.module_from_spec(spec)
        sys.modules[name] = self.app
        spec.loader.exec_module(self.app)
        self.app.init_db()
        # 建一个 owner_finance 用户，供 bot_state 的 tenant_id 使用
        self.app.add_user("boss", "老板", "owner_finance", "password123")
        with self.app.db() as con:
            self.user = dict(con.execute("SELECT * FROM users WHERE username='boss'").fetchone())

    def tearDown(self):
        if self.old_data_dir is None:
            os.environ.pop("DATA_DIR", None)
        else:
            os.environ["DATA_DIR"] = self.old_data_dir
        self.tmp.cleanup()

    def _seed_state(self, waybills=None, fees=None, receipts=None, diffs=None, bills=None):
        state = {
            "julong_demo_waybills_v2": waybills or [],
            "julong_demo_fees_v1": fees or [],
            "julong_demo_receipts_v1": receipts or [],
            "julong_demo_diffs_v1": diffs or [],
            "julong_demo_bills_v1": bills or [],
        }
        import json
        payload = {k: json.dumps(v, ensure_ascii=False, separators=(",", ":")) for k, v in state.items()}
        with self.app.db() as con:
            con.execute("UPDATE work_state SET payload=?, rev=1, updated_at=? WHERE id=1",
                        (json.dumps(payload, ensure_ascii=False), self.app.now_iso()))

    def test_employee_tool_scans_only_authorized_receipts_and_no_finance(self):
        import json
        self.app.add_user("worker", "员工", "employee", "password123")
        self.app.add_user("other", "其他", "employee", "password123")
        with self.app.db() as con:
            worker = dict(con.execute("SELECT * FROM users WHERE username='worker'").fetchone())
            other = dict(con.execute("SELECT * FROM users WHERE username='other'").fetchone())
        self._seed_state(
            waybills=[{"number":"OWN", "created_by":worker["id"], "amount":98765},
                      {"number":"SECRET", "created_by":other["id"], "amount":12345}],
            receipts=[{"number":"OWN"}, {"number":"SECRET"}, {"number":"ORPHAN"}],
            fees=[{"number":"OWN"}, {"number":"SECRET"}],
            diffs=[{"number":"OWN", "note":"secret 99999"}])
        result = self.app.V3Tools().execute("run_daily_routine", {}, tenant_id=worker["tenant_id"],
                                            principal_id=worker["id"], idempotency_key="")
        encoded = json.dumps(result, ensure_ascii=False)
        self.assertEqual(result["total"], 2)
        self.assertEqual(result["by_routine"], {"回单催办": 2})
        for forbidden in ("SECRET", "ORPHAN", "98765", "12345", "99999", "对账差异", "费用"):
            self.assertNotIn(forbidden, encoded)
        with self.app.db() as con:
            bot = json.dumps([dict(r) for r in con.execute("SELECT * FROM bot_state_v2 WHERE owner_id=?", (worker["id"],))], ensure_ascii=False)
        for forbidden in ("SECRET", "ORPHAN", "98765", "12345", "99999", "对账差异", "费用"):
            self.assertNotIn(forbidden, bot)

    def test_owner_ordinary_tool_hides_financial_values_and_notes(self):
        import json
        self._seed_state(waybills=[{"number":"OWN", "amount":98765}],
                         fees=[{"number":"OTHER", "party":"x"}],
                         diffs=[{"number":"OWN", "note":"secret price 99999"}])
        result = self.app.V3Tools().execute("run_daily_routine", {}, tenant_id=self.user["tenant_id"],
                                            principal_id=self.user["id"], idempotency_key="")
        encoded = json.dumps(result, ensure_ascii=False)
        self.assertIn("对账差异", encoded)
        self.assertNotIn("98765", encoded)
        self.assertNotIn("99999", encoded)

    def test_bot_state_is_per_user_and_legacy_hidden(self):
        from capabilities import routine
        self.app.add_user("worker1", "一", "employee", "password123")
        self.app.add_user("worker2", "二", "employee", "password123")
        with self.app.db() as con:
            a = dict(con.execute("SELECT * FROM users WHERE username='worker1'").fetchone())
            b = dict(con.execute("SELECT * FROM users WHERE username='worker2'").fetchone())
            con.execute("INSERT INTO bot_state VALUES(?,?,?,?,?,?,?,?,?,?)",
                        ("legacy",a["tenant_id"],"employee","old","waiting","secret","","","2026",a["id"]))
        self.assertEqual(routine.get_bot_state(self.app, a), [])
        routine.save_bot_state(self.app, a, subject="test", status="waiting", progress="private-a")
        routine.save_bot_state(self.app, b, subject="test", status="waiting", progress="private-b")
        self.assertEqual(routine.get_bot_state(self.app, a)[0]["progress"], "private-a")
        self.assertEqual(routine.get_bot_state(self.app, b)[0]["progress"], "private-b")

    def test_supplied_transaction_rolls_back_routine_state(self):
        from capabilities import routine
        self._seed_state(waybills=[{"number":"ONE"}], receipts=[{"number":"ONE"}])
        with self.app.db() as con:
            con.execute("BEGIN IMMEDIATE")
            result = routine.run(self.app, self.user, con=con)
            self.assertEqual(result["total"], 2)
            con.rollback()
        self.assertEqual(routine.get_bot_state(self.app, self.user, include_done=True), [])

    def test_empty_fees_keeps_open_differences(self):
        from capabilities import routine
        self._seed_state(waybills=[{"number":"A","amount":1}], diffs=[{"number":"A"}])
        result = routine.run(self.app, self.user)
        self.assertIn("差异未收口", [x["kind"] for x in result["routine_candidates"]])
        self.assertIn("费用表暂无记录", result["note"])
        self.assertEqual(result["source_status"]["julong_demo_fees_v1"], "no_records")

    def test_amount_mismatch_and_missing_owner(self):
        from capabilities import routine
        self._seed_state(waybills=[{"number":"A","amount":100}, {"number":"B","amount":80}],
                         fees=[{"number":"A","type":"运费","amount":120},
                               {"number":"B","type":"运费","amount":80,"party":"x"}])
        items = routine.run(self.app, self.user)["routine_candidates"]
        self.assertIn(("A","金额不一致"), {(x["waybill_number"], x["kind"]) for x in items})
        self.assertIn(("A","费用缺归属"), {(x["waybill_number"], x["kind"]) for x in items})
        self.assertNotIn(("B","金额不一致"), {(x["waybill_number"], x["kind"]) for x in items})

    def test_missing_receipt_and_shared_marker_semantics(self):
        from capabilities import routine
        self._seed_state(waybills=[{"number":"A"},{"number":"B"}],
                         receipts=[{"number":"B","paper":True,"delivered":{}}])
        items = routine.run(self.app, self.user)["routine_candidates"]
        self.assertEqual({(x["waybill_number"], x["field"]) for x in items if x["routine"] == "回单催办"},
                         {("A","paper"),("A","delivered"),("B","delivered")})

    def test_malformed_state_reports_incomplete_and_no_false_receipt_scan(self):
        import json
        from capabilities import routine
        self._seed_state(waybills=[{"number":"A"}])
        with self.app.db() as con:
            row = con.execute("SELECT payload FROM work_state").fetchone()
            state = json.loads(row["payload"])
            state["julong_demo_receipts_v1"] = "{bad"
            state.pop("julong_demo_bills_v1", None)
            con.execute("UPDATE work_state SET payload=?", (json.dumps(state),))
        result = routine.run(self.app, self.user)
        self.assertIn("incomplete", result["coverage"])
        self.assertIn("不能视为无待办", result["note"])
        self.assertEqual(result["source_status"]["julong_demo_receipts_v1"], "invalid")
        self.assertEqual(result["source_status"]["julong_demo_bills_v1"], "not_connected")
        self.assertFalse(any(x["routine"] == "回单催办" for x in result["routine_candidates"]))
        self.assertEqual(routine.get_bot_state(self.app, self.user)[0]["status"], "waiting")

    def test_historical_scan_tool_is_exact_owned_and_rescoped(self):
        import json
        from capabilities import routine
        self.app.add_user("worker", "员工", "employee", "password123")
        self.app.add_user("other", "其他", "employee", "password123")
        with self.app.db() as con:
            worker = dict(con.execute("SELECT * FROM users WHERE username='worker'").fetchone())
            other = dict(con.execute("SELECT * FROM users WHERE username='other'").fetchone())
        bills = [{"number":str(i),"created_by":worker["id"],"amount":99999} for i in range(12)]
        self._seed_state(waybills=bills, receipts=[{"number":str(i)} for i in range(12)])
        first = routine.run(self.app, worker, limit=1)
        self.assertEqual(len(first["routine_candidates"]), 1)
        self._seed_state(waybills=bills, receipts=[{"number":str(i),"paper":True,"delivered":True} for i in range(12)])
        second = routine.run(self.app, worker)
        tools = self.app.V3Tools()
        def lookup(user, scan):
            return tools.execute("get_daily_routine_scan", {"scan_id":scan}, tenant_id=user["tenant_id"], principal_id=user["id"], idempotency_key="")
        old = lookup(worker, first["scan_id"])
        self.assertEqual(old["total"], 24)
        self.assertEqual(lookup(worker, second["scan_id"])["total"], 0)
        self.assertEqual(lookup(other, first["scan_id"]), {"error":"not_found"})
        self.assertNotIn("99999", json.dumps(old))
        with self.app.db() as con:
            con.execute("INSERT INTO case_teams VALUES(?,?,?,?,?,?)", (worker["tenant_id"],"0",1,other["id"],"[]",self.app.now_iso()))
        filtered=lookup(worker, first["scan_id"])
        self.assertEqual(filtered["total"], 22)
        self.assertNotIn('"0"', json.dumps(filtered))
        self.assertTrue(filtered["historical"])

    def test_corrupt_outer_payload_is_incomplete(self):
        from capabilities import routine
        with self.app.db() as con:
            con.execute("UPDATE work_state SET payload='{bad',rev=rev+1 WHERE id=1")
        result = routine.run(self.app,self.user)
        self.assertIn("incomplete",result["coverage"])
        self.assertIn("不能视为无待办",result["note"])
        self.assertEqual(result["total"],0)
        self.assertEqual(routine.get_bot_state(self.app,self.user),[])

    def test_missing_fields_report_independently_and_invalid_fees_do_not_hide_diffs(self):
        import json
        from capabilities import routine
        self._seed_state(waybills=[{"number":"A"}], fees=[{"number":"A","partner":"P"}], diffs=[{"number":"A"}])
        result = routine.run(self.app,self.user)
        evidence = next(c["evidence"] for c in result["routine_candidates"] if c["kind"] == "费用缺归属")
        self.assertIn("承担方",evidence)
        self.assertIn("币种",evidence)
        self.assertNotIn("合作方",evidence)
        with self.app.db() as con:
            state=json.loads(con.execute("SELECT payload FROM work_state").fetchone()[0])
            state["julong_demo_fees_v1"]="{bad"
            con.execute("UPDATE work_state SET payload=?",(json.dumps(state),))
        result=routine.run(self.app,self.user)
        self.assertIn("差异未收口",[c["kind"] for c in result["routine_candidates"]])
        self.assertIn("incomplete",result["coverage"])

    def test_latest_partner_bills_compare_and_flag_ambiguity(self):
        from capabilities import routine
        self._seed_state(waybills=[{"number":"A"},{"number":"B"},{"number":"C"}],
            fees=[{"number":"A","type":"运费","amount":11,"partner":"P"},
                  {"number":"B","type":"运费","amount":30,"partner":"P"},
                  {"number":"B","type":"运费","amount":30,"partner":"P"}],
            bills=[{"partner":"P","at":"2026-01-01 10:00","rows":[{"number":"A","type":"运费","amount":11}]},
                   {"partner":"P","at":"2026-01-02 10:00","rows":[{"number":"A","type":"运费","amount":12},
                                             {"number":"B","type":"运费","amount":30},
                                             {"number":"C","type":"进仓费","amount":40}]}])
        result=routine.run(self.app,self.user)
        pairs={(x["waybill_number"],x["kind"]) for x in result["routine_candidates"]}
        self.assertIn(("A","对方账单金额不一致"),pairs)
        self.assertIn(("B","对账对应关系待核实"),pairs)
        self.assertIn(("C","对方账单费用未匹配"),pairs)
        self.assertNotIn("12",str(result["routine_candidates"]))

    def test_confirmed_decision_only_closes_matching_evidence(self):
        import json
        import business_integrity
        from capabilities import routine
        self._seed_state(waybills=[{"number":"A"}], fees=[{"number":"A","type":"运费","amount":10}],
                         diffs=[{"number":"A"}])
        with self.app.db() as con:
            row=con.execute("SELECT payload FROM work_state").fetchone()
            state=json.loads(row["payload"])
            fingerprint=business_integrity.evidence_fingerprint(self.app,state,"A")
            state["julong_demo_diffs_v1"]=json.dumps([{"number":"A","decision":"accepted","confirmed_by_id":"actor","evidence_fingerprint":fingerprint}])
            con.execute("UPDATE work_state SET payload=?",(json.dumps(state),))
        self.assertNotIn("差异未收口",[x["kind"] for x in routine.run(self.app,self.user)["routine_candidates"]])
        with self.app.db() as con:
            state=json.loads(con.execute("SELECT payload FROM work_state").fetchone()[0])
            state["julong_demo_fees_v1"]=json.dumps([{"number":"A","type":"运费","amount":11}])
            con.execute("UPDATE work_state SET payload=?",(json.dumps(state),))
        self.assertIn("差异未收口",[x["kind"] for x in routine.run(self.app,self.user)["routine_candidates"]])

    def test_fees_empty_guard_does_not_report_unsettled(self):
        """费用表为空时，守卫生效：不把 8 票报成「费用未落账」，只报回单催办。"""
        from capabilities import routine

        waybills = [{"number": "0001", "amount": 100, "cargo": "染料"}]
        receipts = [{"number": "0001", "paper": {"off": True}}]  # 纸本撤销 = 待催
        self._seed_state(waybills=waybills, fees=[], receipts=receipts)

        result = routine.run(self.app, self.user)
        self.assertIn("费用表暂无记录", result["note"])
        self.assertEqual(result["source_status"]["julong_demo_fees_v1"], "no_records")
        # 只有回单催办，没有「费用未落账」
        kinds = {c["kind"] for c in result["routine_candidates"]}
        self.assertNotIn("费用未落账", kinds)
        self.assertIn("回单未收", kinds)

    def test_fees_present_reports_unsettled(self):
        """费用表有数据时，能扫出「费用未落账」。"""
        from capabilities import routine

        waybills = [{"number": "0001", "amount": 100, "cargo": "染料"},
                    {"number": "0002", "amount": 200, "cargo": "染料"}]
        fees = [{"number": "0001", "type": "进仓费", "party": "某合作方"}]
        self._seed_state(waybills=waybills, fees=fees, receipts=[])

        result = routine.run(self.app, self.user)
        kinds = {c["kind"]: c["waybill_number"] for c in result["routine_candidates"]}
        # 0002 有金额但费用表无记录 → 费用未落账
        self.assertEqual(kinds.get("费用未落账"), "0002")

    def test_fee_missing_party_reported(self):
        """费用缺合作方/承担方 → 报「费用缺归属」。"""
        from capabilities import routine

        waybills = [{"number": "0001", "amount": 100, "cargo": "染料"}]
        fees = [{"number": "0001", "type": "进仓费"}]  # 无 party/payer
        self._seed_state(waybills=waybills, fees=fees, receipts=[])

        result = routine.run(self.app, self.user)
        kinds = {c["kind"] for c in result["routine_candidates"]}
        self.assertIn("费用缺归属", kinds)

    def test_bot_state_read_write_roundtrip(self):
        """bot_state：跑 Routine 自动记，能读回，覆盖更新。"""
        from capabilities import routine

        waybills = [{"number": "0001", "amount": 100, "cargo": "染料"}]
        receipts = [{"number": "0001", "paper": {"off": True}}]
        self._seed_state(waybills=waybills, fees=[], receipts=receipts)

        # 跑一次 Routine → 自动记任务态
        routine.run(self.app, self.user)
        pending = routine.get_bot_state(self.app, self.user)
        self.assertTrue(any(p["subject"] == "每日主动扫账" for p in pending))
        entry = next(p for p in pending if p["subject"] == "每日主动扫账")
        self.assertEqual(entry["status"], "waiting")  # 有待盯事项
        self.assertIn("扫出", entry["progress"])

        # 再跑一次空账 → 覆盖为 done（include_done 能读回，用于接续）
        self._seed_state(waybills=[], fees=[], receipts=[])
        routine.run(self.app, self.user)
        pending = routine.get_bot_state(self.app, self.user, include_done=True)
        entry = next(p for p in pending if p["subject"] == "每日主动扫账")
        self.assertEqual(entry["status"], "done")


if __name__ == "__main__":
    unittest.main()
