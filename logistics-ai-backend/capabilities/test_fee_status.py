"""涉钱确认闭环（update_fee_status）的针对性测试。

覆盖：
  1. 老板确认一笔费用：status 待确认→已确认，记 confirmBy/confirmAt 留痕，rev+1，审计留痕。
  2. 老板标差异：status→有差异。
  3. 员工不能确认涉金额的费用（finance_confirm_forbidden）。
  4. 幂等：同 key 重放返回原结果，不重复改。
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class FeeStatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="julong-fee-")
        self.old_data_dir = os.environ.get("DATA_DIR")
        os.environ["DATA_DIR"] = self.tmp.name
        name = f"fee_app_{os.getpid()}_{id(self)}"
        spec = importlib.util.spec_from_file_location(name, ROOT / "app.py")
        self.app = importlib.util.module_from_spec(spec)
        sys.modules[name] = self.app
        spec.loader.exec_module(self.app)
        self.app.init_db()
        self.app.add_user("boss", "老板", "owner_finance", "password123")
        self.app.add_user("emp", "员工", "employee", "password123")
        with self.app.db() as con:
            self.boss = dict(con.execute("SELECT * FROM users WHERE username='boss'").fetchone())
            self.emp = dict(con.execute("SELECT * FROM users WHERE username='emp'").fetchone())

    def tearDown(self):
        if self.old_data_dir is None:
            os.environ.pop("DATA_DIR", None)
        else:
            os.environ["DATA_DIR"] = self.old_data_dir
        self.tmp.cleanup()

    def _seed(self, fees):
        state = {
            "julong_demo_waybills_v2": [{"number": "0001", "amount": 100, "cargo": "染料", "created_by": "x"}],
            "julong_demo_fees_v1": fees,
            "julong_demo_receipts_v1": [],
            "julong_demo_diffs_v1": [],
        }
        payload = {k: json.dumps(v, ensure_ascii=False, separators=(",", ":")) for k, v in state.items()}
        with self.app.db() as con:
            con.execute("UPDATE work_state SET payload=?, rev=rev+1 WHERE id=1",
                        (json.dumps(payload, ensure_ascii=False),))

    def _fees_from_db(self):
        with self.app.db() as con:
            row = con.execute("SELECT payload FROM work_state WHERE id=1").fetchone()
        state = self.app._decode_state(row["payload"])
        return json.loads(state["julong_demo_fees_v1"])

    def test_candidate_requires_human_confirmation(self):
        import finance_confirmation
        self._seed([{"id":"f1","number":"0001","type":"进仓费","amount":50,"status":"待确认"}])
        r=self.app.update_fee_status_record(self.boss,{"fee_id":"f1","status":"已确认"},"one")
        self.assertEqual(self._fees_from_db()[0]["status"],"待确认")
        self.assertEqual(finance_confirmation.review(self.app,self.boss,r['candidate_id'])['fee']['amount'],50)
        with self.assertRaises(PermissionError): finance_confirmation.confirm(self.app,self.boss,r['candidate_id'],'wrong')
        self.assertEqual(self._fees_from_db()[0]["status"],"待确认")
        result=finance_confirmation.confirm(self.app,self.boss,r['candidate_id'],'password123')
        self.assertEqual(result['status'],'已确认')
        self.assertEqual(self._fees_from_db()[0]['confirmed_by_id'],self.boss['id'])
        self.assertEqual(result,finance_confirmation.confirm(self.app,self.boss,r['candidate_id'],'password123'))
        with self.app.db() as con:
            self.assertEqual(con.execute("SELECT count(*) FROM audit WHERE action='finance.fee.confirm'").fetchone()[0],1)

    def test_stale_and_duplicate_candidates_rejected(self):
        import finance_confirmation
        self._seed([{"id":"f1","number":"0001","type":"进仓费","amount":50,"status":"待确认"}])
        r=self.app.update_fee_status_record(self.boss,{"fee_id":"f1","status":"有差异"},"one")
        self.assertEqual(r,self.app.update_fee_status_record(self.boss,{"fee_id":"f1","status":"有差异"},"one"))
        with self.app.db() as con: con.execute('UPDATE work_state SET rev=rev+1')
        with self.assertRaises(ValueError): finance_confirmation.confirm(self.app,self.boss,r['candidate_id'],'password123')
        self._seed([{"id":"f1"},{"id":"f1"}])
        with self.assertRaises(ValueError): self.app.update_fee_status_record(self.boss,{"fee_id":"f1","status":"已确认"},"two")

    def test_employee_cannot_propose(self):
        with self.assertRaises(PermissionError): self.app.update_fee_status_record(self.emp,{"fee_id":"f1","status":"已确认"},"one")

    def test_difference_confirmation_and_staleness(self):
        import finance_confirmation
        self._seed([{"id":"f1","number":"0001","type":"进仓费","amount":50,"status":"待确认"}])
        args={'difference_id':'d1','number':'0001','type':'进仓费','decision':'核实后采用我方金额'}
        c=finance_confirmation.create_difference(self.app,self.boss,args,'diff1')
        review=finance_confirmation.review(self.app,self.boss,c['candidate_id'])
        self.assertEqual(review['fees'][0]['amount'],50)
        with self.assertRaises(PermissionError): finance_confirmation.confirm(self.app,self.boss,c['candidate_id'],'bad')
        result=finance_confirmation.confirm(self.app,self.boss,c['candidate_id'],'password123')
        self.assertEqual(result,finance_confirmation.confirm(self.app,self.boss,c['candidate_id'],'password123'))
        with self.app.db() as con:
            state=self.app._decode_state(con.execute('SELECT payload FROM work_state').fetchone()[0])
            decision=json.loads(state[self.app.STATE_KEYS[7]])[0]
            self.assertEqual(decision['confirmed_by_id'],self.boss['id'])
            self.assertTrue(decision['evidence_fingerprint'])
            self.assertEqual(con.execute("SELECT count(*) FROM audit WHERE action='finance.difference.confirm'").fetchone()[0],1)
        args['difference_id']='d2'; c=finance_confirmation.create_difference(self.app,self.boss,args,'diff2')
        with self.app.db() as con: con.execute('UPDATE work_state SET rev=rev+1')
        with self.assertRaises(ValueError): finance_confirmation.confirm(self.app,self.boss,c['candidate_id'],'password123')

    def test_confirmed_fee_needs_reasoned_reversal(self):
        import finance_confirmation
        self._seed([{'id':'f1','number':'0001','type':'进仓费','amount':50,'status':'已确认'}])
        with self.assertRaises(ValueError): self.app.update_fee_status_record(self.boss,{'fee_id':'f1','status':'待确认'},'reopen')
        with self.assertRaises(ValueError): finance_confirmation.create(self.app,self.boss,{'fee_id':'f1','status':'作废'},'void1')
        c=finance_confirmation.create(self.app,self.boss,{'fee_id':'f1','status':'作废','reason':'重复登记'},'void2')
        finance_confirmation.confirm(self.app,self.boss,c['candidate_id'],'password123')
        self.assertEqual(self._fees_from_db()[0]['reversal_reason'],'重复登记')
        with self.assertRaises(ValueError): self.app.update_fee_status_record(self.boss,{'fee_id':'f1','status':'已确认'},'reopen2')

    def test_raw_snapshot_cannot_confirm_or_change_metadata(self):
        import business_integrity
        old={k:'[]' for k in self.app.STATE_KEYS}
        old[self.app.STATE_KEYS[4]]=json.dumps([{'id':'f1','status':'待确认'}])
        new=dict(old);new[self.app.STATE_KEYS[4]]=json.dumps([{'id':'f1','status':'已确认'}])
        with self.assertRaises(ValueError): business_integrity.validate_state(self.app,self.boss,old,new)
        old[self.app.STATE_KEYS[4]]=json.dumps([{'id':'f1','status':'已确认','confirmBy':'a'}])
        new=dict(old);new[self.app.STATE_KEYS[4]]=json.dumps([{'id':'f1','status':'已确认','confirmBy':'b'}])
        with self.assertRaises(ValueError): business_integrity.validate_state(self.app,self.boss,old,new)
        new=dict(old);new[self.app.STATE_KEYS[7]]=json.dumps([{'id':'d1','decision':'ok'}])
        with self.assertRaises(ValueError): business_integrity.validate_state(self.app,self.boss,old,new)


if __name__ == "__main__":
    unittest.main()
