"""Transactional message effects using synthetic users and a temporary database."""
import concurrent.futures
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest

from capabilities import runtime

class MessageTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory(); self.previous=os.environ.get('DATA_DIR'); os.environ['DATA_DIR']=self.temp.name
  name='message_app_'+str(id(self)); spec=importlib.util.spec_from_file_location(name,Path(__file__).resolve().parents[1]/'app.py')
  self.host=importlib.util.module_from_spec(spec);sys.modules[name]=self.host;spec.loader.exec_module(self.host);self.host.init_db()
  for name,role in [('boss','owner_finance'),('worker','employee'),('dispatch','dispatcher')]: self.host.add_user(name,name,role,'password123')
  with self.host.db() as con:self.users={r['username']:dict(r) for r in con.execute('SELECT * FROM users')}
 def tearDown(self):
  if self.previous is None:os.environ.pop('DATA_DIR',None)
  else:os.environ['DATA_DIR']=self.previous
  self.temp.cleanup()
 def test_concurrent_idempotency_and_role_checks(self):
  args={'to_user_id':self.users['worker']['id'],'text':'请查看回单'}
  def send(_):return runtime.execute(self.host,self.users['boss'],'send_message',args,'same-effect')
  with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool: results=list(pool.map(send,range(16)))
  self.assertEqual(len({r['id'] for r in results}),1)
  with self.host.db() as con:
   self.assertEqual(con.execute("SELECT count(*) FROM agent_notifications WHERE source_id LIKE 'message:%'").fetchone()[0],1)
   self.assertEqual(con.execute("SELECT count(*) FROM audit WHERE action='agent.message.send'").fetchone()[0],1)
  self.assertIn('error',runtime.execute(self.host,self.users['boss'],'send_message',{**args,'text':'不同消息'},'same-effect'))
  self.assertIn('error',runtime.execute(self.host,self.users['worker'],'send_message',args,'worker-effect'))
  self.assertIn('error',runtime.execute(self.host,self.users['boss'],'send_message',{**args,'text':'运费金额 200 元'},'price'))
  with self.host.db() as con:con.execute("UPDATE users SET active=0 WHERE id=?",(self.users['boss']['id'],))
  self.assertIn('error',send(0))
 def test_failure_rolls_back_and_retry(self):
  args={'to_user_id':self.users['worker']['id'],'text':'请查看回单'}; original=self.host.audit
  def fail(*a,**kw):raise RuntimeError('synthetic failure')
  self.host.audit=fail
  with self.assertRaises(RuntimeError): runtime.execute(self.host,self.users['boss'],'send_message',args,'recover')
  self.host.audit=original
  with self.host.db() as con:
   self.assertEqual(con.execute("SELECT count(*) FROM agent_notifications WHERE source_id LIKE 'message:%'").fetchone()[0],0)
   self.assertEqual(con.execute("SELECT count(*) FROM agent_capability_calls WHERE key='recover'").fetchone()[0],0)
  self.assertTrue(runtime.execute(self.host,self.users['boss'],'send_message',args,'recover')['sent'])
