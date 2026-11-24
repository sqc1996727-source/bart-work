"""循环任务引擎（run_routine）的针对性测试。

覆盖：
  1. parse_schedule：interval / daily / weekdays 三种调度表达式正确解析。
  2. schedule 建 run_routine 循环任务：需要 schedule 表达式，写入带 schedule/routine_kind。
  3. tick 触发 run_routine：到点真去扫账，写通知，并推进到下次调度继续跑（不终态）。
  4. followups 管理：pause / resume / cancel。
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class RoutineEngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="julong-rt-")
        self.old_data_dir = os.environ.get("DATA_DIR")
        os.environ["DATA_DIR"] = self.tmp.name
        name = f"rt_app_{os.getpid()}_{id(self)}"
        spec = importlib.util.spec_from_file_location(name, ROOT / "app.py")
        self.app = importlib.util.module_from_spec(spec)
        sys.modules[name] = self.app
        spec.loader.exec_module(self.app)
        self.app.init_db()
        self.app.add_user("boss", "老板", "owner_finance", "password123")
        with self.app.db() as con:
            self.user = dict(con.execute("SELECT * FROM users WHERE username='boss'").fetchone())

    def tearDown(self):
        if self.old_data_dir is None:
            os.environ.pop("DATA_DIR", None)
        else:
            os.environ["DATA_DIR"] = self.old_data_dir
        self.tmp.cleanup()

    def _seed_state(self, waybills=None, receipts=None):
        import json
        state = {
            "julong_demo_waybills_v2": waybills or [],
            "julong_demo_fees_v1": [],
            "julong_demo_receipts_v1": receipts or [],
            "julong_demo_diffs_v1": [],
        }
        payload = {k: json.dumps(v, ensure_ascii=False, separators=(",", ":")) for k, v in state.items()}
        with self.app.db() as con:
            con.execute("UPDATE work_state SET payload=?, rev=1, updated_at=? WHERE id=1",
                        (json.dumps(payload, ensure_ascii=False), self.app.now_iso()))

    def test_parse_schedule_variants(self):
        from capabilities import runtime as rt
        now = datetime(2026, 11, 23, 10, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(rt.parse_schedule("interval:30", now), now + timedelta(minutes=30))
        self.assertEqual(rt.parse_schedule("daily:08:00", now), datetime(2026, 11, 24, 0, 0, tzinfo=timezone.utc))
        self.assertEqual(rt.parse_schedule("weekdays:08:00", now), datetime(2026, 11, 24, 0, 0, tzinfo=timezone.utc))
        self.assertIsNone(rt.parse_schedule("bad", now))

    def test_schedule_run_routine_requires_schedule(self):
        from capabilities import runtime as rt
        due = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        # 没有 schedule → 报错
        with self.assertRaises(ValueError):
            rt.schedule(self.app, self.user,
                        {"title": "扫账", "due_at": due, "condition": "run_routine", "routine_kind": "daily_scan"},
                        "key1")
        # 有 schedule → 成功
        result = rt.schedule(self.app, self.user,
                             {"title": "扫账", "due_at": due, "condition": "run_routine",
                              "routine_kind": "daily_scan", "schedule": "daily:08:00"},
                             "key2")
        self.assertEqual(result["condition"], "run_routine")
        self.assertEqual(result["schedule"], "daily:08:00")

    def test_tick_run_routine_scans_and_reschedules(self):
        from capabilities import runtime as rt

        self._seed_state(waybills=[{"number": "0001", "amount": 100, "cargo": "染料"}],
                         receipts=[{"number": "0001", "paper": {"off": True}}])
        # 建一个已到期的 run_routine 任务
        past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        rt.schedule(self.app, self.user,
                    {"title": "每日扫账", "due_at": past, "condition": "run_routine",
                     "routine_kind": "daily_scan", "schedule": "daily:08:00"},
                    "key-tick")
        # tick 触发
        rt.tick(self.app)
        # 检查通知产生（含扫账结果）
        with self.app.db() as con:
            notifs = con.execute("SELECT * FROM agent_notifications").fetchall()
            self.assertTrue(len(notifs) >= 1)
            text = notifs[0]["text"]
            self.assertIn("自动扫账", text)
            # 循环任务被推进到下次调度，而非终态
            row = con.execute("SELECT state, schedule FROM agent_followups").fetchone()
            self.assertEqual(row["schedule"], "daily:08:00")
            self.assertIn(row["state"], {"pending", "done"})  # 下次到点继续

    def test_weekdays_uses_shanghai_calendar(self):
        from capabilities import runtime as rt
        # Friday 23:30 Shanghai; next working day is Monday 08:00 Shanghai.
        now = datetime(2026, 11, 27, 15, 30, tzinfo=timezone.utc)
        self.assertEqual(rt.parse_schedule("weekdays:08:00", now),
                         datetime(2026, 11, 30, 0, 0, tzinfo=timezone.utc))

    def test_concurrent_tick_and_rollback(self):
        from capabilities import runtime as rt, routine
        from unittest.mock import patch
        import threading
        now = datetime.now(timezone.utc)
        rt.schedule(self.app, self.user, {"title":"scan", "due_at":(now-timedelta(minutes=1)).isoformat(),
                    "condition":"run_routine", "routine_kind":"daily_scan", "schedule":"interval:1"}, "race")
        original = routine.run
        def fail(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("simulated crash")
        with patch.object(routine, 'run', fail):
            rt.tick(self.app, now)
        with self.app.db() as con:
            self.assertEqual(con.execute('SELECT count(*) FROM agent_notifications').fetchone()[0], 0)
            self.assertEqual(con.execute('SELECT state FROM agent_followups').fetchone()[0], 'pending')
        threads = [threading.Thread(target=rt.tick, args=(self.app, now)) for _ in range(2)]
        for t in threads:t.start()
        for t in threads:t.join()
        with self.app.db() as con:
            self.assertEqual(con.execute('SELECT count(*) FROM agent_notifications').fetchone()[0], 1)
        rt.tick(self.app, now + timedelta(minutes=2))
        with self.app.db() as con:
            self.assertEqual(con.execute('SELECT count(*) FROM agent_notifications').fetchone()[0], 2)

    def test_worker_loop_delivers_due_routine(self):
        from capabilities import runtime as rt
        from unittest.mock import patch
        import time
        now = datetime.now(timezone.utc)
        rt.schedule(self.app, self.user, {"title":"loop", "due_at":(now-timedelta(seconds=1)).isoformat(),
                    "condition":"run_routine", "routine_kind":"daily_scan", "schedule":"interval:1"}, "loop")
        try:
            with patch.object(rt, 'deliver_followups'), patch.object(rt.team, 'tick'):
                rt.start(self.app)
                deadline = time.monotonic() + 4
                count = 0
                while time.monotonic() < deadline:
                    with self.app.db() as con:
                        count = con.execute('SELECT count(*) FROM agent_notifications').fetchone()[0]
                    if count: break
                    time.sleep(.05)
                self.assertEqual(count, 1)
                rt.stop(self.app)
        finally:
            rt.stop(self.app)

    def test_worker_retries_lock_and_takes_over(self):
        from capabilities import runtime as rt
        from unittest.mock import patch
        import fcntl, threading, time
        lock = (self.app.DATA_DIR / 'agent_worker.lock').open('a')
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        ran = threading.Event()
        try:
            with patch.object(rt, 'run_one', side_effect=lambda host: ran.set()), \
                 patch.object(rt, 'tick'), patch.object(rt, 'deliver_followups'), \
                 patch.object(rt.team, 'tick'):
                rt.start(self.app)
                self.assertFalse(ran.wait(.7))
                fcntl.flock(lock, fcntl.LOCK_UN)
                self.assertTrue(ran.wait(3), 'waiting worker failed to take over')
                rt.stop(self.app)
        finally:
            lock.close()
            rt.stop(self.app)

    def test_management_role_transition_and_scan_link(self):
        from capabilities import runtime as rt, routine
        self._seed_state()
        now=datetime.now(timezone.utc)
        result=rt.followups_manage(self.app,self.user,{'action':'create','title':'扫描','due_at':(now-timedelta(seconds=1)).isoformat(),'condition':'run_routine','schedule':'interval:1','routine_kind':'daily_scan','request_key':'crud'})
        fid=result['id']
        rt.followups_manage(self.app,self.user,{'action':'edit','id':fid,'title':'新名称'})
        downgraded={**self.user,'role':'employee'}
        with self.assertRaises(LookupError):rt.followups_manage(self.app,downgraded,{'action':'pause','id':fid})
        rt.tick(self.app,now)
        notice=rt.notifications(self.app,self.user)['items'][0]
        self.assertTrue(notice['scan_id'])
        rt.tick(self.app,now+timedelta(minutes=2))
        self.assertEqual(routine.get_scan(self.app,self.user,notice['scan_id'])['scan_id'],notice['scan_id'])
        self.assertIsNone(routine.get_scan(self.app,{**self.user,'id':'other'},notice['scan_id']))
        rt.followups_manage(self.app,self.user,{'action':'cancel','id':fid})
        with self.assertRaises(ValueError):rt.followups_manage(self.app,self.user,{'action':'resume','id':fid})

    def test_followups_http_route(self):
        import hashlib, http.server, json, threading, time, urllib.request, urllib.error
        token='isolated-test-token'
        with self.app.db() as con:
            con.execute('INSERT INTO sessions(token_hash,user_id,expires_at,created_at) VALUES(?,?,?,?)',
                        (hashlib.sha256(token.encode()).hexdigest(),self.user['id'],int(time.time())+300,self.app.now_iso()))
        server=http.server.ThreadingHTTPServer(('127.0.0.1',0),self.app.Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        base='http://127.0.0.1:%s/api/agent/followups'%server.server_port
        def request(data=None):
            body=json.dumps(data).encode() if data is not None else None
            req=urllib.request.Request(base,data=body,headers={'Cookie':self.app.SESSION_COOKIE+'='+token,'Content-Type':'application/json'})
            with urllib.request.urlopen(req,timeout=3) as response:return json.load(response)
        try:
            item=request({'action':'create','title':'route','due_at':(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat(),'condition':'run_routine','routine_kind':'daily_scan','schedule':'daily:08:00','request_key':'route'})
            fid=item['id']
            request({'action':'edit','id':fid,'title':'changed'})
            self.assertEqual(request()['items'][0]['title'],'changed')
            request({'action':'pause','id':fid});request({'action':'resume','id':fid});request({'action':'delete','id':fid})
            with self.assertRaises(urllib.error.HTTPError) as err:request({'action':'resume','id':fid})
            self.assertEqual(err.exception.code,400)
        finally:
            server.shutdown();server.server_close();thread.join()

    def test_followups_manage_pause_resume_cancel(self):
        from capabilities import runtime as rt

        due = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        rt.schedule(self.app, self.user,
                    {"title": "扫账", "due_at": due, "condition": "run_routine",
                     "routine_kind": "daily_scan", "schedule": "daily:08:00"},
                    "key-m")
        with self.app.db() as con:
            fid = con.execute("SELECT id FROM agent_followups").fetchone()["id"]
        # pause
        rt.followups_manage(self.app, self.user, {"action": "pause", "id": fid})
        with self.app.db() as con:
            self.assertEqual(con.execute("SELECT state FROM agent_followups WHERE id=?", (fid,)).fetchone()["state"], "paused")
        # resume
        rt.followups_manage(self.app, self.user, {"action": "resume", "id": fid})
        with self.app.db() as con:
            self.assertEqual(con.execute("SELECT state FROM agent_followups WHERE id=?", (fid,)).fetchone()["state"], "pending")
        # cancel
        rt.followups_manage(self.app, self.user, {"action": "cancel", "id": fid})
        with self.app.db() as con:
            self.assertEqual(con.execute("SELECT state FROM agent_followups WHERE id=?", (fid,)).fetchone()["state"], "cancelled")


if __name__ == "__main__":
    unittest.main()
