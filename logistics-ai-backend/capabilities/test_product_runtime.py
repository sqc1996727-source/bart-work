"""No model calls or production data: durable scheduling and restart behavior."""
import json
from datetime import datetime, timezone, timedelta
from capabilities import runtime as rt
from capabilities.test_routine_engine import RoutineEngineTests

class ProductRuntimeTests(RoutineEngineTests):
    def test_scheduled_agent_queues_once_and_pauses(self):
        cid=self.app._v3_new_conversation(self.user,"enterprise")
        token=rt.CURRENT_CONVERSATION.set(cid)
        try:
            now=datetime.now(timezone.utc)
            item=rt.schedule(self.app,self.user,{"title":"业务检查","due_at":(now-timedelta(minutes=1)).isoformat(),"condition":"run_agent","agent_instruction":"核查业务并汇报","schedule":"interval:30"},"test-schedule")
        finally:rt.CURRENT_CONVERSATION.reset(token)
        rt.tick(self.app,now)
        rt.tick(self.app,now)
        with self.app.db() as con:
            rows=con.execute("SELECT * FROM agent_jobs").fetchall()
            self.assertEqual(len(rows),1)
            self.assertEqual(rows[0]["state"],"queued")
            self.assertIn("核查业务并汇报",json.loads(rows[0]["payload"])["text"])
            self.assertEqual(json.loads(rows[0]["payload"])["conversation_id"],cid)
        rt.followups_manage(self.app,self.user,{"action":"pause","id":item["id"]})
        with self.app.db() as con:
            self.assertEqual(con.execute("SELECT state FROM agent_jobs").fetchone()[0],"cancelled")
    def test_restart_requeues_only_proven_pretool_work(self):
        now=self.app.now_iso()
        cid=self.app._v3_new_conversation(self.user,"enterprise")
        with self.app.db() as con:
            for jid,stage,started in [("safe","model_started",0),("unsafe","tool_started",1),("answer","model_completed",0),("old",None,0)]:
                con.execute("INSERT INTO agent_jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,0)",(jid,self.user["tenant_id"],self.user["id"],2,jid,jid,json.dumps({"conversation_id":cid,"text":"test","memory_scope":"enterprise"}),"running","running",None,now,now))
                if stage:con.execute("INSERT INTO agent_job_checkpoints(job_id,stage,tool_started,updated_at) VALUES(?,?,?,?)",(jid,stage,started,now))
        rt.recover_interrupted(self.app)
        with self.app.db() as con:
            states={r["id"]:r["state"] for r in con.execute("SELECT id,state FROM agent_jobs")}
        self.assertEqual(states,{"safe":"queued","unsafe":"interrupted","answer":"interrupted","old":"interrupted"})
