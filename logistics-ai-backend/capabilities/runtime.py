from __future__ import annotations
import contextvars,hashlib,json,re,secrets,threading,time,subprocess
from datetime import datetime,timedelta,timezone
from zoneinfo import ZoneInfo
from . import catalog,files,analysis,team,workplace_memory

CURRENT_JOB=contextvars.ContextVar('julong_job',default=None)
CURRENT_CONVERSATION=contextvars.ContextVar('julong_conversation',default=None)
CURRENT_SOURCES=contextvars.ContextVar('julong_sources',default=None)
_WORKERS={};_LOCK=threading.Lock()
RANK=files.RANK

def initialize(host):
 files.initialize(host)
 team.initialize(host)
 workplace_memory.initialize(host)
 with host.db() as con:
  con.executescript('''
  CREATE TABLE IF NOT EXISTS agent_jobs(id TEXT PRIMARY KEY,tenant_id TEXT NOT NULL,owner_id TEXT NOT NULL,role_rank INTEGER NOT NULL,
    request_key TEXT NOT NULL,request_hash TEXT NOT NULL,payload TEXT NOT NULL,state TEXT NOT NULL,progress TEXT NOT NULL,
    result TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,cancel_requested INTEGER NOT NULL DEFAULT 0,
    UNIQUE(tenant_id,owner_id,request_key));
  CREATE TABLE IF NOT EXISTS agent_job_checkpoints(job_id TEXT PRIMARY KEY,stage TEXT NOT NULL,tool_started INTEGER NOT NULL DEFAULT 0,recovery_count INTEGER NOT NULL DEFAULT 0,updated_at TEXT NOT NULL);
  CREATE TABLE IF NOT EXISTS agent_preferences(owner_id TEXT NOT NULL,key TEXT NOT NULL,value TEXT NOT NULL,role_rank INTEGER NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,updated_at TEXT NOT NULL,PRIMARY KEY(owner_id,key));
  CREATE TABLE IF NOT EXISTS agent_followups(id TEXT PRIMARY KEY,tenant_id TEXT NOT NULL,owner_id TEXT NOT NULL,role_rank INTEGER NOT NULL,
    title TEXT NOT NULL,due_at TEXT NOT NULL,condition TEXT NOT NULL,waybill_number TEXT,task_id TEXT,every_minutes INTEGER NOT NULL,
    conversation_id TEXT,state TEXT NOT NULL DEFAULT 'pending',created_at TEXT NOT NULL);
  CREATE TABLE IF NOT EXISTS agent_notifications(id TEXT PRIMARY KEY,tenant_id TEXT NOT NULL,owner_id TEXT NOT NULL,role_rank INTEGER NOT NULL,
    source_id TEXT UNIQUE NOT NULL,conversation_id TEXT,text TEXT NOT NULL,created_at TEXT NOT NULL,seen INTEGER NOT NULL DEFAULT 0);
  CREATE TABLE IF NOT EXISTS agent_notification_scans(notification_id TEXT PRIMARY KEY,scan_id TEXT NOT NULL);
  CREATE TABLE IF NOT EXISTS agent_capability_calls(owner_id TEXT NOT NULL,key TEXT NOT NULL,request_hash TEXT NOT NULL,result TEXT NOT NULL,PRIMARY KEY(owner_id,key));
  CREATE TABLE IF NOT EXISTS agent_usage(id INTEGER PRIMARY KEY AUTOINCREMENT,tenant_id TEXT NOT NULL,owner_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,model_usage TEXT NOT NULL,status INTEGER NOT NULL,created_at TEXT NOT NULL);
  ''')
  if 'job_id' not in {r['name'] for r in con.execute('PRAGMA table_info(agent_usage)')}:con.execute('ALTER TABLE agent_usage ADD COLUMN job_id TEXT')
  if 'role_rank' not in {r['name'] for r in con.execute('PRAGMA table_info(agent_capability_calls)')}:con.execute('ALTER TABLE agent_capability_calls ADD COLUMN role_rank INTEGER NOT NULL DEFAULT 2')
  # 循环任务引擎扩展：schedule=调度表达式(interval:N/daily:HH:MM/weekdays:HH:MM)，routine_kind=到点要干的活类型
  if 'schedule' not in {r['name'] for r in con.execute('PRAGMA table_info(agent_followups)')}:con.execute('ALTER TABLE agent_followups ADD COLUMN schedule TEXT')
  if 'routine_kind' not in {r['name'] for r in con.execute('PRAGMA table_info(agent_followups)')}:con.execute('ALTER TABLE agent_followups ADD COLUMN routine_kind TEXT')
  if 'agent_instruction' not in {r['name'] for r in con.execute('PRAGMA table_info(agent_followups)')}:con.execute('ALTER TABLE agent_followups ADD COLUMN agent_instruction TEXT')
  if 'agent_scope' not in {r['name'] for r in con.execute('PRAGMA table_info(agent_followups)')}:con.execute('ALTER TABLE agent_followups ADD COLUMN agent_scope TEXT')

def checkpoint(stage,**details):
 ctx=CURRENT_JOB.get()
 if not ctx:return
 host,jid=ctx
 with host.db() as con:
  row=con.execute('SELECT cancel_requested FROM agent_jobs WHERE id=?',(jid,)).fetchone()
  if row and row['cancel_requested']:
   from ai.service import AIServiceError
   raise AIServiceError('REQUEST_CANCELLED','已停止后续处理，已完成的业务动作仍保留。')
  labels={'model_started':'正在处理','model_completed':'正在整理结果','tool_started':'正在核对资料','tool_completed':'资料已核对'}
  con.execute('BEGIN IMMEDIATE')
  row=con.execute('SELECT cancel_requested FROM agent_jobs WHERE id=?',(jid,)).fetchone()
  if not row or row['cancel_requested']:
   from ai.service import AIServiceError
   raise AIServiceError('REQUEST_CANCELLED','已停止后续处理，已完成的业务动作仍保留。')
  now=host.now_iso()
  con.execute("INSERT INTO agent_job_checkpoints(job_id,stage,tool_started,updated_at) VALUES(?,?,?,?) ON CONFLICT(job_id) DO UPDATE SET stage=excluded.stage,tool_started=max(agent_job_checkpoints.tool_started,excluded.tool_started),updated_at=excluded.updated_at",(jid,stage,int(stage=='tool_started'),now))
  con.execute('UPDATE agent_jobs SET progress=?,updated_at=? WHERE id=?',(labels.get(stage,'正在处理'),now,jid))

def get_user(host,uid,tenant):
 with host.db() as con:row=con.execute('SELECT * FROM users WHERE id=? AND tenant_id=? AND active=1',(uid,tenant)).fetchone()
 return dict(row) if row else None

def job_view(host,user,jid):
 with host.db() as con:row=con.execute('SELECT * FROM agent_jobs WHERE id=? AND tenant_id=? AND owner_id=?',(jid,user['tenant_id'],user['id'])).fetchone()
 if not row or RANK[user['role']]<row['role_rank']:raise LookupError('not_found')
 result=json.loads(row['result']) if row['result'] else None
 payload=json.loads(row['payload'])
 title=str(payload.get('text',''))[:100]
 if user['role']=='employee' and getattr(host, '_v3_output_has_explicit_price', lambda _text: True)(title):title='工作任务'
 return {'id':row['id'],'state':row['state'],'progress':row['progress'],'result':result,'updated_at':row['updated_at'],'cancel_requested':bool(row['cancel_requested']),'conversation_id':payload.get('conversation_id'),'title':title}

def submit(host,user,payload,key):
 if not key or len(key)>128 or key.startswith('scheduled:'):raise ValueError('request_key_required')
 if not isinstance(payload,dict) or set(payload)-{'conversation_id','text','memory_scope','attachment_ids'}:raise ValueError('invalid_request')
 if not isinstance(payload.get('text'),str) or len(payload['text'])>8000:raise ValueError('invalid_message')
 ids=payload.get('attachment_ids',[])
 if not isinstance(ids,list) or len(ids)>12:raise ValueError('invalid_attachments')
 for fid in ids:files.authorized(host,user,fid)
 # Hash the client request, including its selected conversation. Replays do not create empty conversations.
 digest=hashlib.sha256(json.dumps(payload,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
 with _LOCK:
  with host.db() as con:
   old=con.execute('SELECT * FROM agent_jobs WHERE tenant_id=? AND owner_id=? AND request_key=?',(user['tenant_id'],user['id'],key)).fetchone()
  if old:
   if old['request_hash']!=digest or RANK[user['role']]<old['role_rank']:raise ValueError('request_key_reused')
   return {'job_id':old['id'],'conversation_id':json.loads(old['payload'])['conversation_id']}
 with host.db() as con:
  if con.execute("SELECT count(*) FROM agent_jobs WHERE tenant_id=? AND owner_id=? AND state IN ('queued','running')",(user['tenant_id'],user['id'])).fetchone()[0]>=20:raise ValueError('too_many_pending_jobs')
 cid=payload.get('conversation_id')
 if cid:
  record=host._v3_ai_service().store.get_conversation(tenant_id=user['tenant_id'],principal_id=user['id'],conversation_id=cid,limit=1)
  if not record or not host.integrity.conversation_allowed(host,user,cid):raise LookupError('not_found')
  if payload.get('memory_scope', 'enterprise') != record['memory_scope']:raise ValueError('conversation_scope_mismatch')
 else:payload={**payload,'conversation_id':host._v3_new_conversation(user,payload.get('memory_scope','enterprise'))}
 now=host.now_iso();jid=secrets.token_urlsafe(18)
 with host.db() as con:
  con.execute('BEGIN IMMEDIATE')
  old=con.execute('SELECT * FROM agent_jobs WHERE tenant_id=? AND owner_id=? AND request_key=?',(user['tenant_id'],user['id'],key)).fetchone()
  if old:
   if old['request_hash']!=digest:raise ValueError('request_key_reused')
   return {'job_id':old['id'],'conversation_id':json.loads(old['payload'])['conversation_id']}
  if con.execute("SELECT count(*) FROM agent_jobs WHERE tenant_id=? AND owner_id=? AND state IN ('queued','running')",(user['tenant_id'],user['id'])).fetchone()[0]>=20:raise ValueError('too_many_pending_jobs')
  con.execute('INSERT INTO agent_jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,0)',(jid,user['tenant_id'],user['id'],RANK[user['role']],key,digest,json.dumps(payload,ensure_ascii=False),'queued','已接到',None,now,now))
  con.commit()
 start(host)
 return {'job_id':jid,'conversation_id':payload['conversation_id']}

def cancel(host,user,jid):
 info=job_view(host,user,jid)
 with host.db() as con:
  con.execute("UPDATE agent_jobs SET state='cancelled',cancel_requested=1,progress='已取消',updated_at=? WHERE id=? AND state='queued'",(host.now_iso(),jid))
  con.execute("UPDATE agent_jobs SET cancel_requested=1,progress='正在停止后续处理',updated_at=? WHERE id=? AND state='running'",(host.now_iso(),jid))
 return job_view(host,user,jid)

def run_one(host):
 with host.db() as con:
  con.execute('BEGIN IMMEDIATE')
  row=con.execute("""SELECT q.* FROM agent_jobs q WHERE q.state='queued'
    AND NOT EXISTS(SELECT 1 FROM agent_jobs r WHERE r.state='running'
      AND (json_extract(r.payload,'$.conversation_id')=json_extract(q.payload,'$.conversation_id') OR (r.tenant_id=q.tenant_id AND r.owner_id=q.owner_id)))
    ORDER BY COALESCE((SELECT max(h.updated_at) FROM agent_jobs h WHERE h.tenant_id=q.tenant_id AND h.owner_id=q.owner_id AND h.state!='queued'),''),q.created_at,q.rowid LIMIT 1""").fetchone()
  if not row:return False
  now=host.now_iso()
  con.execute("UPDATE agent_jobs SET state='running',progress='正在处理',updated_at=? WHERE id=?",(now,row['id']))
  con.execute("INSERT INTO agent_job_checkpoints(job_id,stage,tool_started,updated_at) VALUES(?,'claimed',0,?) ON CONFLICT(job_id) DO UPDATE SET stage='claimed',tool_started=0,updated_at=excluded.updated_at",(row['id'],now));con.commit()
 user=get_user(host,row['owner_id'],row['tenant_id']);token=CURRENT_JOB.set((host,row['id']))
 from ai.service import OBSERVER, EXECUTION_JOB_ID
 observer_token=OBSERVER.set(checkpoint)
 execution_token=EXECUTION_JOB_ID.set(row['id'])
 try:
  if not user or RANK[user['role']]<row['role_rank'] or (row['request_key'].startswith('scheduled:') and RANK[user['role']]!=row['role_rank']):status,result=403,{'error':'permission_changed','message':'账号权限已变化，请重新交办。'}
  else:status,result=host._v3_chat(user,json.loads(row['payload']))
  state='succeeded' if status==200 else 'cancelled' if result.get('error')=='request_cancelled' else 'failed'
  with host.db() as con:
   con.execute('UPDATE agent_jobs SET state=?,progress=?,result=?,updated_at=? WHERE id=?',(state,'已完成' if state=='succeeded' else '已停止' if state=='cancelled' else '这次未完成',json.dumps({'status':status,**result},ensure_ascii=False),host.now_iso(),row['id']))
   if state in {'succeeded','failed'}:
    notice='交办的事情已处理完，点开接着看。' if state=='succeeded' else '工作未完成，已保留实际执行记录，请查看后续事项。'
    con.execute('INSERT OR IGNORE INTO agent_notifications VALUES(?,?,?,?,?,?,?,?,0)',(secrets.token_urlsafe(16),row['tenant_id'],row['owner_id'],row['role_rank'],'job:'+row['id'],json.loads(row['payload'])['conversation_id'],notice,host.now_iso()))
 except Exception:
  import logging
  logging.getLogger(__name__).error("Job failed outside normal completion: %s", row['id'])
  with host.db() as con:
   con.execute("UPDATE agent_jobs SET state='failed',progress='处理遇到问题，原资料仍保留',updated_at=? WHERE id=?",(host.now_iso(),row['id']))
   con.execute('INSERT OR IGNORE INTO agent_notifications VALUES(?,?,?,?,?,?,?,?,0)',(secrets.token_urlsafe(16),row['tenant_id'],row['owner_id'],row['role_rank'],'job:'+row['id'],json.loads(row['payload'])['conversation_id'],'工作未完成，请查看任务状态。',host.now_iso()))
 finally:CURRENT_JOB.reset(token);OBSERVER.reset(observer_token);EXECUTION_JOB_ID.reset(execution_token)
 return True

def tick(host,now=None):
 now=now or datetime.now(timezone.utc)
 with host.db() as con:
  rows=con.execute("SELECT id,due_at FROM agent_followups WHERE state='pending' AND due_at<=? ORDER BY due_at",(now.astimezone(timezone.utc).isoformat(),)).fetchall()
 for selected in rows:
  try:
   with host.db() as con:
    con.execute('BEGIN IMMEDIATE')
    row=con.execute("SELECT * FROM agent_followups WHERE id=? AND state='pending' AND due_at=?",(selected['id'],selected['due_at'])).fetchone()
    if not row:continue
    user_row=con.execute('SELECT * FROM users WHERE id=? AND tenant_id=? AND active=1',(row['owner_id'],row['tenant_id'])).fetchone()
    user=dict(user_row) if user_row else None
    if not user or RANK[user['role']]<row['role_rank']:
     con.execute("UPDATE agent_followups SET state='permission_changed' WHERE id=?",(row['id'],));continue
    if row['condition']=='run_agent':
     _enqueue_scheduled_agent(host,con,row,user,now)
     continue
    ready=row['condition']=='remind'
    if row['condition'] in {'paper_received','receipt_delivered'}:
     state=host._decode_state(con.execute('SELECT payload FROM work_state WHERE id=1').fetchone()['payload'])
     tickets=json.loads(state[host.STATE_KEYS[0]])
     case=next((t for t in tickets if isinstance(t,dict) and str(t.get('number',''))==row['waybill_number']),None)
     if not case or (user['role']=='employee' and not host._case_allowed(case,user,con=con)):
      con.execute("UPDATE agent_followups SET state='permission_changed' WHERE id=?",(row['id'],));continue
     receipt=next((r for r in json.loads(state[host.STATE_KEYS[5]]) if str(r.get('number'))==row['waybill_number']),{})
     marker=receipt.get('paper' if row['condition']=='paper_received' else 'delivered')
     ready=host.receipt_state.active(marker)
    if row['condition']=='task_done':
     task=con.execute('SELECT * FROM tasks WHERE id=?',(row['task_id'],)).fetchone()
     if not task or (user['role']=='employee' and task['assignee_id']!=user['id']):
      con.execute("UPDATE agent_followups SET state='permission_changed' WHERE id=?",(row['id'],));continue
     ready=task['status']=='done'
    if row['condition']=='run_routine':
     ready=True
     text,scan_id=_run_routine_followup(host,row,user,con)
    else:text=row['title']+('，到提醒时间了。' if row['condition']=='remind' else '，跟进条件已满足。')
    if ready:
     # due_at is the slot identity. Both outcome and advance commit atomically.
     slot=datetime.fromisoformat(row['due_at'].replace('Z','+00:00')).astimezone(timezone.utc).isoformat()
     source='followup:'+row['id']+':'+slot
     nid=secrets.token_urlsafe(16)
     inserted=con.execute('INSERT OR IGNORE INTO agent_notifications VALUES(?,?,?,?,?,?,?,?,0)',(nid,row['tenant_id'],row['owner_id'],row['role_rank'],source,row['conversation_id'],text,host.now_iso())).rowcount
     if inserted and row['condition']=='run_routine' and scan_id:
      con.execute('INSERT INTO agent_notification_scans VALUES(?,?)',(nid,scan_id))
     nxt=parse_schedule(row['schedule'],now) if row['condition']=='run_routine' else None
     con.execute('UPDATE agent_followups SET state=?,due_at=? WHERE id=?',('pending' if nxt else 'done',nxt.astimezone(timezone.utc).isoformat() if nxt else row['due_at'],row['id']))
    else:
     con.execute('UPDATE agent_followups SET due_at=? WHERE id=?',((now+timedelta(minutes=row['every_minutes'])).astimezone(timezone.utc).isoformat(),row['id']))
  except Exception:
   import logging
   logging.getLogger(__name__).exception('Followup processing failed for %s',selected['id'])

def recover_interrupted(host):
 """Requeue only work that provably never reached a tool or final-answer phase."""
 with host.db() as con:
  con.execute('BEGIN IMMEDIATE')
  jobs=con.execute("SELECT j.*,c.stage,c.tool_started,c.recovery_count FROM agent_jobs j LEFT JOIN agent_job_checkpoints c ON c.job_id=j.id WHERE j.state='running'").fetchall()
  for row in jobs:
   safe=not row['cancel_requested'] and row['stage'] in {'claimed','model_started'} and row['tool_started']==0 and row['recovery_count']<2
   state='queued' if safe else 'cancelled' if row['cancel_requested'] else 'interrupted'
   progress='服务恢复，等待继续处理' if safe else '已停止' if state=='cancelled' else '服务重启，原任务与已完成操作保留；请核查后继续'
   con.execute("UPDATE agent_jobs SET state=?,progress=?,updated_at=? WHERE id=?",(state,progress,host.now_iso(),row['id']))
   if safe:con.execute("UPDATE agent_job_checkpoints SET recovery_count=recovery_count+1 WHERE job_id=?",(row['id'],))
   else:
    con.execute('INSERT OR IGNORE INTO agent_notifications VALUES(?,?,?,?,?,?,?,?,0)',(secrets.token_urlsafe(16),row['tenant_id'],row['owner_id'],row['role_rank'],'job:'+row['id'],json.loads(row['payload'])['conversation_id'],progress,host.now_iso()))
   actor=con.execute("SELECT * FROM users WHERE id=? AND tenant_id=?",(row['owner_id'],row['tenant_id'])).fetchone()
   if actor:host.audit(con,actor,'agent.job.recovery','agent_job',row['id'],{'state':state,'stage':row['stage']})

def _enqueue_scheduled_agent(host,con,row,user,now):
 """Insert a due job and advance the schedule in the same SQLite transaction."""
 if RANK[user['role']]!=row['role_rank'] or not row['conversation_id'] or not row['agent_instruction'] or row['agent_scope'] not in {'enterprise','personal'}:
  con.execute("UPDATE agent_followups SET state='permission_changed' WHERE id=?",(row['id'],));return
 slot=datetime.fromisoformat(row['due_at'].replace('Z','+00:00')).astimezone(timezone.utc).isoformat()
 prefix='scheduled:'+row['id']+':'
 request_key=prefix+slot
 if con.execute("SELECT 1 FROM agent_jobs WHERE owner_id=? AND tenant_id=? AND request_key LIKE ? AND state IN ('queued','running')",(row['owner_id'],row['tenant_id'],prefix+'%')).fetchone():
  nxt=parse_schedule(row['schedule'],now)
  if nxt:con.execute("UPDATE agent_followups SET due_at=? WHERE id=?",(nxt.astimezone(timezone.utc).isoformat(),row['id']))
  else:con.execute("UPDATE agent_followups SET state='done' WHERE id=?",(row['id'],))
  return
 pending=con.execute("SELECT count(*) FROM agent_jobs WHERE owner_id=? AND tenant_id=? AND state IN ('queued','running')",(row['owner_id'],row['tenant_id'])).fetchone()[0]
 if pending>=20:return
 text='执行已安排的工作（当前时间 '+now.isoformat()+'）：\n'+row['agent_instruction']+'\n只执行原交办目标范围内的操作；不要创建新的定时任务。'
 payload={'conversation_id':row['conversation_id'],'text':text,'memory_scope':row['agent_scope'],'attachment_ids':[]}
 digest=hashlib.sha256(json.dumps(payload,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
 at=host.now_iso()
 con.execute("INSERT OR IGNORE INTO agent_jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,0)",(secrets.token_urlsafe(18),row['tenant_id'],row['owner_id'],row['role_rank'],request_key,digest,json.dumps(payload,ensure_ascii=False),'queued','定时工作已排队',None,at,at))
 nxt=parse_schedule(row['schedule'],now)
 con.execute("UPDATE agent_followups SET state=?,due_at=? WHERE id=?",('pending' if nxt else 'done',nxt.astimezone(timezone.utc).isoformat() if nxt else row['due_at'],row['id']))

def start(host):
 key=str(host.DB_PATH)
 with _LOCK:
  if key in _WORKERS and _WORKERS[key][0].is_alive():return
  stop_event=threading.Event()
  def worker():
   import fcntl
   from concurrent.futures import ThreadPoolExecutor
   while not stop_event.is_set():
    try:
     with (host.DATA_DIR/'agent_worker.lock').open('a') as lock_file:
      while not stop_event.is_set():
       try:
        fcntl.flock(lock_file,fcntl.LOCK_EX|fcntl.LOCK_NB);break
       except BlockingIOError:stop_event.wait(.5)
      else:return
      recover_interrupted(host)
      with ThreadPoolExecutor(max_workers=4,thread_name_prefix='julong-job') as pool:
       pending=[];last_team_tick=0
       while not stop_event.is_set():
        try:
         pending=[f for f in pending if not f.done()]
         for _ in range(4-len(pending)):pending.append(pool.submit(run_one,host))
         tick(host);deliver_followups(host)
         if time.monotonic()-last_team_tick>10:team.tick(host);last_team_tick=time.monotonic()
        except Exception as exc:
         import logging
         logging.getLogger(__name__).error('Capability worker error: %s',type(exc).__name__)
        stop_event.wait(.5)
    except Exception:
     import logging
     logging.getLogger(__name__).exception('Capability worker startup failed; retrying')
     stop_event.wait(.5)
  thread=threading.Thread(target=worker,daemon=True,name='julong-capabilities');_WORKERS[key]=(thread,stop_event);thread.start()

def deliver_followups(host):
 # A small outbox bridges the business and conversation databases. The delivery
 # marker and message commit together, so a crash never duplicates the reply.
 with host.db() as con:rows=con.execute("SELECT * FROM agent_notifications WHERE source_id LIKE 'followup:%' AND conversation_id IS NOT NULL").fetchall()
 if not rows:return
 store=host._v3_ai_service().store
 with store._connect() as con:
  con.execute('CREATE TABLE IF NOT EXISTS followup_deliveries(notification_id TEXT PRIMARY KEY,message_id INTEGER NOT NULL)')
 for row in rows:
  user=get_user(host,row['owner_id'],row['tenant_id'])
  if not user or RANK[user['role']]<row['role_rank'] or not host.integrity.conversation_allowed(host,user,row['conversation_id']):continue
  with store._connect() as con:
   con.execute('BEGIN IMMEDIATE')
   if con.execute('SELECT 1 FROM followup_deliveries WHERE notification_id=?',(row['id'],)).fetchone():continue
   if not con.execute('SELECT 1 FROM conversations WHERE id=? AND tenant_id=? AND principal_id=?',(row['conversation_id'],row['tenant_id'],row['owner_id'])).fetchone():continue
   cursor=con.execute("INSERT INTO messages(conversation_id,role,content,created_at) VALUES(?,'assistant',?,?)",(row['conversation_id'],row['text'],time.time()))
   con.execute('INSERT INTO followup_deliveries VALUES(?,?)',(row['id'],cursor.lastrowid))
   con.execute('UPDATE conversations SET updated_at=? WHERE id=?',(time.time(),row['conversation_id']))
   con.commit()

def notifications(host,user):
 with host.db() as con:rows=con.execute('SELECT n.*,s.scan_id FROM agent_notifications n LEFT JOIN agent_notification_scans s ON s.notification_id=n.id WHERE n.tenant_id=? AND n.owner_id=? AND n.role_rank<=? AND n.seen=0 ORDER BY n.created_at DESC LIMIT 30',(user['tenant_id'],user['id'],RANK[user['role']])).fetchall()
 return {'items':[{k:r[k] for k in ('id','conversation_id','text','created_at','scan_id')} for r in rows]}

def preferences(host,user):
 with host.db() as con:rows=con.execute('SELECT key,value FROM agent_preferences WHERE owner_id=? AND active=1 AND role_rank<=?',(user['id'],RANK[user['role']])).fetchall()
 return {r['key']:r['value'] for r in rows}

def execute(host,user,name,args,key):
 allowed={t['function']['name']:t['function']['parameters'] for t in catalog.tools()}
 if name not in allowed:return None
 props=allowed[name]
 if not isinstance(args,dict) or set(args)-set(props['properties']) or any(k not in args for k in props.get('required',[])):return {'error':'invalid_request'}
 if name=='send_message':
  try:return send_message(host,user,args,key)
  except (LookupError,PermissionError):return {'error':'not_found'}
  except (ValueError,TypeError,KeyError):return {'error':'invalid_request_or_file_unavailable'}
 if name=='create_document' and CURRENT_SOURCES.get():
  requested=args.get('file_ids',[])
  if not isinstance(requested,list):return {'error':'invalid_request'}
  combined=list(dict.fromkeys(requested+CURRENT_SOURCES.get()))
  if len(combined)>12:return {'error':'too_many_sources'}
  args={**args,'_provenance_ids':combined}
 mutating=name in {'create_document','manage_preference','save_workplace_memory','schedule_followup','manage_followups','send_message'}
 if mutating and (not key or len(key)>160):return {'error':'idempotency_key_required'}
 digest=hashlib.sha256(json.dumps([name,args],sort_keys=True,ensure_ascii=False).encode()).hexdigest()
 with host.db() as con:
  old=con.execute('SELECT * FROM agent_capability_calls WHERE owner_id=? AND key=?',(user['id'],key)).fetchone() if mutating else None
 if old:
  if RANK[user['role']]<old['role_rank']:return {'error':'not_found'}
  if old['request_hash']!=digest:return {'error':'idempotency_key_reused'}
  return json.loads(old['result'])
 try:
  if name=='list_capabilities':result={'skills':[{'name':k,'title':v[0],'description':v[1]} for k,v in catalog.SKILLS.items()]}
  elif name=='read_skill':result=catalog.read(args['name'])
  elif name=='read_files':
   ids=args['file_ids']
   if not isinstance(ids,list) or not 1<=len(ids)<=12:raise ValueError()
   result={'items':[]}
   offset=int(args.get('text_offset',0));sheet=int(args.get('sheet_index',0));row_offset=int(args.get('row_offset',0))
   if min(offset,sheet,row_offset)<0:raise ValueError()
   for fid in ids:
    row=files.authorized(host,user,fid);content=files.extract(host,user,fid)
    sources=CURRENT_SOURCES.get()
    if sources is not None and fid not in sources:sources.append(fid)
    tables=[{'sheet':t['sheet'],'sheet_index':i,'rows':t['rows'][row_offset:row_offset+80],'row_offset':row_offset,'total_rows':len(t['rows'])} for i,t in enumerate(content['tables']) if i==sheet]
    result['items'].append({**files.public(row),'text':content['text'][offset:offset+12000],'text_offset':offset,'next_text_offset':offset+12000 if len(content['text'])>offset+12000 else None,'locations':content['locations'][:60],'tables':tables,'sheets':[{'index':i,'name':t['sheet'],'total_rows':len(t['rows'])} for i,t in enumerate(content['tables'])],'truncated':len(content['text'])>offset+12000 or content.get('truncated',False)})
  elif name=='analyze_table':
   result=analysis.run(host,user,args)
   sources=CURRENT_SOURCES.get()
   if sources is not None:
    for fid in args['file_ids']:
     if fid not in sources:sources.append(fid)
  elif name=='find_records':result=find_records(host,user,args)
  elif name=='create_document':result={'document':files.make_artifact(host,user,args,key)}
  elif name=='manage_preference':result=preference_action(host,user,args)
  elif name=='search_workplace_memory':result=workplace_memory.search(host,user,args['query'])
  elif name=='save_workplace_memory':result=workplace_memory.save(host,user,args,CURRENT_CONVERSATION.get())
  elif name=='schedule_followup':result=schedule(host,user,args,key)
  elif name=='send_message':result=send_message(host,user,args,key)
  else:
   if args['action']=='cancel':followups_manage(host,user,args)
   result=followups_list(host,user)
  with host.db() as con:
   host.audit(con,user,'agent.capability.'+name,'capability',name,{'request_hash':digest})
   if mutating:con.execute('INSERT OR IGNORE INTO agent_capability_calls(owner_id,key,request_hash,result,role_rank) VALUES(?,?,?,?,?)',(user['id'],key,digest,json.dumps(result,ensure_ascii=False),RANK[user['role']]))
  return result
 except (LookupError,PermissionError):return {'error':'not_found'}
 except ValueError as exc:
  # 具体契约错误让模型能自检修正，不再统一吞成笼统的 unavailable。
  code=str(exc) or 'invalid_request'
  return {'error':code[:80]}
 except (TypeError,KeyError,TimeoutError,subprocess.TimeoutExpired):return {'error':'invalid_request_or_file_unavailable'}

def find_records(host,user,args):
 """Search scoped business records, authorized file names, and safe own history.
 Full file contents remain behind read_files and its authorization checks.
 """
 from rapidfuzz import fuzz
 from ai.agent_policy import query_terms,relevance
 from ai.service import TOOL_MEMORY_SCOPE
 query=str(args['query']).strip()[:200];kind=args.get('kind','all');limit=int(args.get('limit',10))
 if not query or kind not in {'all','waybills','files','conversations'} or not 1<=limit<=20:raise ValueError()
 terms=query_terms(query)
 candidates=[]
 def score(text):
  lexical=relevance(terms,text)
  fuzzy=round(fuzz.partial_ratio(query.casefold(),text.casefold()),1)
  return max(fuzzy,min(100,50+lexical)) if lexical else fuzzy
 def safe(text):
  # Historical unstructured material is not a route around finance boundaries.
  return not workplace_memory.sensitive(host,text)
 if kind in {'all','waybills'} and host._module_enabled('transport'):
  for raw in host._v3_raw_rows(0):
   if not team.allowed(host,user,raw):continue
   item=host.V3Context._pick(raw,host.V3Context._WAYBILL_FIELDS);text=json.dumps(item,ensure_ascii=False)
   match=score(text)
   if match>=35:candidates.append({'kind':'waybill','id':raw.get('number'),'text':text,'score':match})
 if kind in {'all','files'}:
  # Search metadata first; never parse or reveal unauthorized documents.
  with host.db() as con:
   rows=con.execute('SELECT id,filename FROM agent_files WHERE tenant_id=? AND role_rank<=? ORDER BY created_at DESC',(user['tenant_id'],RANK[user['role']])).fetchall()
  ranked=sorted(((score(r['filename']),r) for r in rows if safe(r['filename'])),key=lambda item:item[0],reverse=True)
  count=0
  for match,row in ranked:
   if match<45 or count>=limit:break
   try:doc=files.public(files.authorized(host,user,row['id']))
   except LookupError:continue
   candidates.append({'kind':'file','id':doc['id'],'text':doc['filename'],'score':match,'file_id':doc['id']})
   count+=1
 if kind in {'all','conversations'} and terms:
  store=host._v3_ai_service().store
  current=CURRENT_CONVERSATION.get() or ''
  scope=TOOL_MEMORY_SCOPE.get()
  # Use only this principal's current-format conversation scope. The raw
  # legacy history and other principals never enter the candidate search.
  with store._connect() as con:
   rows=con.execute("""SELECT m.id,m.conversation_id,m.role,m.content,m.created_at FROM messages m
       JOIN conversations c ON c.id=m.conversation_id
       WHERE c.tenant_id=? AND c.principal_id=? AND c.memory_scope=?
       AND (c.id!=? OR m.id<(SELECT max(id) FROM messages WHERE conversation_id=c.id))
       AND substr(c.id,1,?)=? AND m.role='user'
       ORDER BY m.id DESC LIMIT 2000""",(user['tenant_id'],user['id'],scope,current,len(host._V3_CONVERSATION_PREFIX),host._V3_CONVERSATION_PREFIX)).fetchall()
  ranked=[]
  for row in rows:
   content=str(row['content']).split('\n\n本轮附件（由用户提供，可按需使用read_files读取）：',1)[0]
   if relevance(terms,content)<2 or not safe(content):continue
   ranked.append((score(content),row,content))
  ranked.sort(key=lambda item:item[0],reverse=True)
  checked={};seen=set()
  for match,row,content in ranked:
   cid=row['conversation_id']
   if cid in seen:continue
   if cid not in checked:checked[cid]=host.integrity.conversation_allowed(host,user,cid)
   if not checked[cid]:continue
   seen.add(cid)
   candidates.append({'kind':'conversation','id':cid,'message_id':row['id'],'role':row['role'],'text':content[:600],'score':match,'created_at':row['created_at']})
   if len(seen)>=limit:break
 candidates.sort(key=lambda c:c['score'],reverse=True)
 return {'items':candidates[:limit],'candidate_only':True,'coverage':{'waybills':'current_authorized_records','files':'authorized_nonsensitive_filenames_only','conversations':'own_current_scope_nonsensitive_user_history_only'},'note':'匹配仅是候选；历史原话和文件名不是当前业务事实，需重新核查。'}


def preference_action(host,user,args):
 action=args['action'];key=str(args.get('key','')).strip()[:60];value=str(args.get('value','')).strip()
 if action=='list':return {'preferences':preferences(host,user)}
 if action not in {'remember','forget'} or not key:raise ValueError()
 if action=='remember':
  if not value or len(value)>300 or re.search(r'(?i)(password|api[_ -]?key|secret|token|私钥|密码|-----BEGIN|已结算|实付|应付|应收)',key+' '+value):raise ValueError()
 with host.db() as con:
  if action=='forget':con.execute('UPDATE agent_preferences SET active=0 WHERE owner_id=? AND key=?',(user['id'],key))
  else:con.execute('INSERT INTO agent_preferences VALUES(?,?,?,?,1,?) ON CONFLICT(owner_id,key) DO UPDATE SET value=excluded.value,role_rank=excluded.role_rank,active=1,updated_at=excluded.updated_at',(user['id'],key,value,RANK[user['role']],host.now_iso()))
 return {'preferences':preferences(host,user)}

def parse_schedule(schedule, now):
    """解析调度表达式，返回「下次触发时间」。返回 None 表示表达式非法。

    支持三种：
      interval:N   —— 每 N 分钟触发一次（N=1..1440）
      daily:HH:MM  —— 每天 HH:MM（本地时间）
      weekdays:HH:MM —— 周一至周五 HH:MM
    兼容旧数据：schedule 为空时回退到 due_at + every_minutes 的旧逻辑（由调用方处理）。
    """
    if not schedule:
        return None
    s = str(schedule).strip()
    if s.startswith("interval:"):
        try:
            n = int(s.split(":", 1)[1])
        except (ValueError, IndexError):
            return None
        if not 1 <= n <= 1440:
            return None
        return now + timedelta(minutes=n)
    if s.startswith("daily:") or s.startswith("weekdays:"):
        kind, hhmm = s.split(":", 1)
        try:
            hh, mm = hhmm.split(":")
            hour, minute = int(hh), int(mm)
        except (ValueError, IndexError):
            return None
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return None
        candidate = now.astimezone(ZoneInfo("Asia/Shanghai")).replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= now.astimezone(ZoneInfo("Asia/Shanghai")):
            candidate += timedelta(days=1)
        if kind == "weekdays":
            while candidate.weekday() >= 5:  # 周六=5，周日=6
                candidate += timedelta(days=1)
        return candidate
    return None


def _run_routine_followup(host, row, user, con):
    """run_routine 类型到点：真正去干活（扫账），不是发一句通知。

    确定性扫账（agent_routine.run），返回一句大白话结果文字。
    扫账的详细候选已由 routine.run 写入 bot_state（任务态本子），前端/对话可从那里取详情。
    不烧模型 token，符合「确定性活不换给模型」的铁律。
    """
    from . import routine as agent_routine
    result = agent_routine.run(host, user, con=con)
    total = result["total"]
    by_routine = result["by_routine"]
    if "incomplete" in result.get("coverage", "") or result.get("coverage")=="无业务状态":
        return "自动扫账数据不完整，请检查业务数据后再确认待办。",result.get("scan_id")
    if total == 0:
        return "今天的自动扫账跑完了，已覆盖的数据没有待盯候选。",result.get('scan_id')
    parts = [f"{k} {v} 条" for k, v in by_routine.items()]
    return "今天的自动扫账跑完了，有 " + "、".join(parts) + " 要盯。",result.get("scan_id")


def schedule(host,user,args,key):
 due=datetime.fromisoformat(str(args['due_at']).replace('Z','+00:00'))
 if due.tzinfo is None:raise ValueError('timezone_required')
 condition=args.get('condition','remind');number=str(args.get('waybill_number',''));tid=str(args.get('task_id',''));minutes=int(args.get('every_minutes',15))
 sched=str(args.get('schedule','') or '').strip()
 routine_kind=str(args.get('routine_kind','') or '').strip()
 if condition not in {'remind','paper_received','receipt_delivered','task_done','run_routine','run_agent'} or not 1<=minutes<=1440:raise ValueError()
 if condition=='run_routine' and not host._module_enabled('transport'):raise PermissionError('module_disabled')
 if condition in {'paper_received','receipt_delivered'} and not host._module_enabled('receipt'):raise PermissionError('module_disabled')
 if condition=='run_routine':
  # run_routine 到点是「干活」不是「发提醒」；routine_kind 目前只支持 daily_scan（每日扫账）。
  if routine_kind not in {'daily_scan'}:raise ValueError()
  # 必须有 schedule 表达式（interval/daily/weekdays），否则退化为一次性，语义不清。
  if not sched or parse_schedule(sched, due.astimezone(timezone.utc)) is None:raise ValueError('schedule_required')
 if condition in {'paper_received','receipt_delivered'} and not host._v3_safe_case(user,number):raise LookupError()
 if condition=='task_done':
  with host.db() as con:t=con.execute('SELECT * FROM tasks WHERE id=?',(tid,)).fetchone()
  if not t or (user['role']=='employee' and t['assignee_id']!=user['id']):raise LookupError()
 title=str(args['title']).strip()[:200]
 if not title:raise ValueError()
 ctx=CURRENT_JOB.get();cid=CURRENT_CONVERSATION.get()
 if ctx:
  with host.db() as con:current_job=con.execute("SELECT request_key FROM agent_jobs WHERE id=?",(ctx[1],)).fetchone()
  if current_job and current_job['request_key'].startswith('scheduled:'):raise PermissionError('scheduled_job_cannot_schedule')
 if ctx and not cid:
  with host.db() as con:row=con.execute('SELECT payload FROM agent_jobs WHERE id=?',(ctx[1],)).fetchone()
  if row:cid=json.loads(row['payload']).get('conversation_id')
 instruction=None;agent_scope=None
 if condition=='run_agent':
  instruction=args.get('agent_instruction')
  if not isinstance(instruction,str) or not instruction.strip() or len(instruction)>6000 or not cid:raise ValueError('agent_instruction_and_conversation_required')
  if sched and parse_schedule(sched,due) is None:raise ValueError('invalid_schedule')
  conversation=host._v3_ai_service().store.get_conversation(tenant_id=user['tenant_id'],principal_id=user['id'],conversation_id=cid,limit=1)
  if not conversation or not host.integrity.conversation_allowed(host,user,cid):raise LookupError()
  agent_scope=conversation['memory_scope']
 fid=hashlib.sha256((user['tenant_id']+'|'+user['id']+'|'+key).encode()).hexdigest()[:32]
 with host.db() as con:
  con.execute('INSERT OR IGNORE INTO agent_followups(id,tenant_id,owner_id,role_rank,title,due_at,condition,waybill_number,task_id,every_minutes,conversation_id,state,created_at,schedule,routine_kind,agent_instruction,agent_scope) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(fid,user['tenant_id'],user['id'],RANK[user['role']],title,due.astimezone(timezone.utc).isoformat(),condition,number,tid,minutes,cid,'pending',host.now_iso(),sched or None,routine_kind or None,instruction,agent_scope))
 return {'id':fid,'title':title,'due_at':due.isoformat(),'state':'pending','condition':condition,'schedule':sched or None,'routine_kind':routine_kind or None}


def stop(host):
 pair=_WORKERS.get(str(host.DB_PATH))
 if pair:pair[1].set();pair[0].join(timeout=3)


def followups_list(host,user):
 """列出当前用户创建的跟进/循环任务，供管理面板展示。"""
 with host.db() as con:
  rows=con.execute('SELECT id,title,due_at,state,condition,waybill_number,task_id,every_minutes,schedule,routine_kind,created_at FROM agent_followups WHERE owner_id=? AND tenant_id=? AND role_rank<=? ORDER BY created_at DESC LIMIT 200',(user['id'],user['tenant_id'],RANK[user['role']])).fetchall()
 return {'items':[dict(r) for r in rows]}


def followups_manage(host,user,args):
 action=str(args.get('action',''))
 if action=='create':
  if set(args)-{'action','title','due_at','condition','waybill_number','task_id','every_minutes','schedule','routine_kind','agent_instruction','request_key'}:raise ValueError()
  key=str(args.get('request_key',''))
  if not key or len(key)>128:raise ValueError()
  return schedule(host,user,{k:v for k,v in args.items() if k not in {'action','request_key'}},key)
 fid=str(args.get('id',''))
 if not fid or action not in {'cancel','delete','pause','resume','edit'}:raise ValueError()
 with host.db() as con:
  con.execute('BEGIN IMMEDIATE')
  row=con.execute('SELECT * FROM agent_followups WHERE id=? AND owner_id=? AND tenant_id=? AND role_rank<=?',(fid,user['id'],user['tenant_id'],RANK[user['role']])).fetchone()
  if not row:raise LookupError()
  if action in {'cancel','delete'}:
   if row['state'] not in {'pending','paused'}:raise ValueError('invalid_state')
   state='cancelled'
  elif action=='pause':
   if row['state']!='pending':raise ValueError('invalid_state')
   state='paused'
  elif action=='resume':
   if row['state']!='paused':raise ValueError('invalid_state')
   state='pending'
  else:
   if row['state'] not in {'pending','paused'} or set(args)-{'action','id','title','due_at','schedule'}:raise ValueError()
   title=str(args.get('title',row['title'])).strip()
   if not title or len(title)>200:raise ValueError()
   due=datetime.fromisoformat(str(args.get('due_at',row['due_at'])).replace('Z','+00:00'))
   if due.tzinfo is None:raise ValueError()
   sched=str(args.get('schedule',row['schedule']) or '').strip()
   if row['condition']=='run_routine' and (not sched or parse_schedule(sched,due) is None):raise ValueError()
   if sched and parse_schedule(sched,due) is None:raise ValueError()
   con.execute('UPDATE agent_followups SET title=?,due_at=?,schedule=? WHERE id=?',(title,due.astimezone(timezone.utc).isoformat(),sched or None,fid))
   return {'id':fid,'action':action,'ok':True}
  con.execute('UPDATE agent_followups SET state=? WHERE id=?',(state,fid))
  if row['condition']=='run_agent' and state in {'paused','cancelled'}:
   con.execute("UPDATE agent_jobs SET state='cancelled',cancel_requested=1,progress='定时工作已停止',updated_at=? WHERE tenant_id=? AND owner_id=? AND request_key LIKE ? AND state='queued'",(host.now_iso(),user['tenant_id'],user['id'],'scheduled:'+fid+':%'))
 return {'id':fid,'action':action,'ok':True}


def send_message(host,user,args,key):
 """Commit the notification, audit and idempotency receipt as one effect."""
 if not key or len(key)>160: raise ValueError('idempotency_key_required')
 if not isinstance(args,dict) or set(args)!={'to_user_id','text'} or not all(isinstance(args[k],str) for k in args): raise ValueError('invalid_request')
 to_id=args['to_user_id'].strip(); text=args['text'].strip()
 if not to_id or not text or len(text)>1000: raise ValueError('invalid_request')
 digest=hashlib.sha256(json.dumps(['send_message',args],sort_keys=True,ensure_ascii=False).encode()).hexdigest()
 with host.db() as con:
  con.execute('BEGIN IMMEDIATE')
  actor=con.execute('SELECT * FROM users WHERE id=? AND tenant_id=? AND active=1',(user['id'],user['tenant_id'])).fetchone()
  if not actor or actor['role'] not in {'dispatcher','owner_finance'}: raise PermissionError()
  prior=con.execute('SELECT * FROM agent_capability_calls WHERE owner_id=? AND key=?',(actor['id'],key)).fetchone()
  if prior:
   if RANK[actor['role']]<prior['role_rank']: raise PermissionError()
   if prior['request_hash']!=digest: raise ValueError('idempotency_key_reused')
   con.rollback(); return json.loads(prior['result'])
  target=con.execute('SELECT id,tenant_id,role FROM users WHERE id=? AND tenant_id=? AND active=1',(to_id,actor['tenant_id'])).fetchone()
  if not target or target['role'] not in RANK: raise LookupError()
  # Staff reminders contain no user-provided free text or monetary values.
  if target['role']=='employee' and text not in {'请查看回单','请核对运单','请查看工作台待办'}:
   raise PermissionError()
  nid=secrets.token_urlsafe(16); now=host.now_iso()
  effect=hashlib.sha256((actor['tenant_id']+'\0'+actor['id']+'\0'+key).encode()).hexdigest()
  con.execute('INSERT INTO agent_notifications(id,tenant_id,owner_id,role_rank,source_id,conversation_id,text,created_at,seen) VALUES(?,?,?,?,?,NULL,?,?,0)',(nid,actor['tenant_id'],to_id,RANK[target['role']],'message:'+effect,text,now))
  host.audit(con,actor,'agent.message.send','user',to_id,{'text_len':len(text),'effect_key':effect})
  result={'id':nid,'to_user_id':to_id,'sent':True}
  con.execute('INSERT INTO agent_capability_calls(owner_id,key,request_hash,result,role_rank) VALUES(?,?,?,?,?)',(actor['id'],key,digest,json.dumps(result,ensure_ascii=False),RANK[actor['role']]))
  con.commit(); return result
