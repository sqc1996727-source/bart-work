"""Company collaboration tools. Authorization is independent from model wording."""
import hashlib,json,secrets
from datetime import datetime,timezone,timedelta
from . import files
from .catalog import schema

def initialize(host):
 with host.db() as con:
  con.executescript('''
  CREATE TABLE IF NOT EXISTS case_teams(tenant_id TEXT,number TEXT,revision INTEGER NOT NULL,responsible_id TEXT NOT NULL,support_ids TEXT NOT NULL,updated_at TEXT NOT NULL,PRIMARY KEY(tenant_id,number));
  CREATE TABLE IF NOT EXISTS case_events(id INTEGER PRIMARY KEY AUTOINCREMENT,tenant_id TEXT,number TEXT,actor_id TEXT,kind TEXT,details TEXT,created_at TEXT);
  CREATE TABLE IF NOT EXISTS case_files(tenant_id TEXT,number TEXT,file_id TEXT,min_rank INTEGER NOT NULL,shared_by TEXT,created_at TEXT,PRIMARY KEY(tenant_id,number,file_id));
  CREATE TABLE IF NOT EXISTS team_commands(tenant_id TEXT,owner_id TEXT,key TEXT,request_hash TEXT,result TEXT,PRIMARY KEY(tenant_id,owner_id,key));
  CREATE TABLE IF NOT EXISTS company_knowledge(id TEXT PRIMARY KEY,tenant_id TEXT,topic TEXT,version INTEGER,title TEXT,content TEXT,owner_id TEXT,applies_to TEXT,valid_from TEXT,valid_until TEXT,min_rank INTEGER,source_ids TEXT,created_at TEXT,UNIQUE(tenant_id,topic,version));
  CREATE TABLE IF NOT EXISTS event_subscriptions(tenant_id TEXT,owner_id TEXT,enabled INTEGER,PRIMARY KEY(tenant_id,owner_id));
  CREATE TABLE IF NOT EXISTS event_observations(tenant_id TEXT,owner_id TEXT,event_key TEXT,fingerprint TEXT,episode INTEGER,active INTEGER,PRIMARY KEY(tenant_id,owner_id,event_key));
  ''')

def access(con,user,ticket):
 if not ticket:return False
 if user['role']!='employee':return True
 number=str(ticket.get('number',''))
 if not con.execute("SELECT 1 FROM sqlite_master WHERE name='case_teams'").fetchone():return ticket.get('created_by')==user['id']
 row=con.execute('SELECT * FROM case_teams WHERE tenant_id=? AND number=?',(user['tenant_id'],number)).fetchone()
 member=(user['id']==row['responsible_id'] or user['id'] in json.loads(row['support_ids'])) if row else ticket.get('created_by')==user['id']
 if member:return True
 if con.execute("SELECT 1 FROM sqlite_master WHERE name='tasks'").fetchone():
  return bool(con.execute("SELECT 1 FROM tasks t JOIN users u ON u.id=t.created_by WHERE u.tenant_id=? AND t.waybill_number=? AND t.assignee_id=? AND t.status IN ('open','in_progress') LIMIT 1",(user['tenant_id'],number,user['id'])).fetchone())
 return False

def allowed(host,user,ticket):
 with host.db() as con:return access(con,user,ticket)

def ticket(con,host,number):
 if not host._module_enabled('transport'):return None
 row=con.execute('SELECT payload FROM work_state WHERE id=1').fetchone()
 return next((x for x in json.loads(host._decode_state(row['payload'])[host.STATE_KEYS[0]]) if isinstance(x,dict) and str(x.get('number'))==number),None)

def tools():
 s={'type':'string'};ids={'type':'array','items':s,'maxItems':12}
 return [
 schema('list_colleagues','查同公司在职同事的姓名和ID，供真实交接和分派使用。',{}),
 schema('get_case_workspace','查一票业务的负责人、协作人、共享原件、交接历史。私人聊天不共享。返回revision供变更时校验。',{'waybill_number':s},['waybill_number']),
 schema('handoff_case','按用户交办转交业务；老板或调度指定负责人及协作人，保留原录入者。必须先读取当前revision；原负责人未列为协作人则不再有该业务的员工访问权限。',{'waybill_number':s,'responsible_id':s,'support_ids':ids,'expected_revision':{'type':'integer'},'note':s},['waybill_number','responsible_id','expected_revision']),
 schema('share_case_file','把本人原件关联到可访问的运单，或撤回关联。默认保留原文件等级；仅按用户明确共享范围设置audience，不依据文件猜测自动扩大权限。',{'waybill_number':s,'file_id':s,'action':{'type':'string','enum':['share','unshare']},'audience':{'type':'string','enum':['employee','dispatcher','owner_finance']}},['waybill_number','file_id','action']),
 schema('preview_waybill_import','校验已上传Excel能否正式录入；只允许老板财务，返回逐行错误或预览ID。不会写入业务账。',{'file_id':s},['file_id']),
 schema('commit_waybill_import','用户已要求录入时提交已校验的预览，返回实际新增单号和版本。冲突先重新读取，不覆盖他人修改。',{'preview_id':s},['preview_id']),
 schema('search_company_knowledge','查公司资料的当前版本、负责人、适用范围、有效期和来源。返回失效/未来版本但明确标注，不把旧资料当现行规定。',{'query':s},['query']),
 schema('publish_company_knowledge','仅老板财务按用户要求新增公司资料版本，保留历史。适用范围、负责人和有效期必须明确；来源文件权限须覆盖读者。',{'topic':s,'expected_version':{'type':'integer'},'title':s,'content':s,'owner_id':s,'applies_to':s,'valid_from':s,'valid_until':s,'audience':{'type':'string','enum':['employee','dispatcher','owner_finance']},'source_ids':ids},['topic','expected_version','title','content','owner_id','applies_to','valid_from','valid_until','audience']),
 schema('manage_event_alerts','开启或关闭本人业务事件提醒：新交接、本人逾期待办、本人负责资料到期；相同异常持续存在不反复提醒。',{'enabled':{'type':'boolean'}},['enabled']),
 schema('get_agent_usage','查询真实模型用量，按任务/会话列出。员工只看自己；老板可看公司汇总。不把未知用量当0，不推算账单价格。',{'scope':{'type':'string','enum':['mine','company']},'days':{'type':'integer','minimum':1,'maximum':90}}),
 schema('get_agent_health','老板或调度查看任务排队、执行和失败计数及最早等待时间，不返回私人聊天内容。',{})]

MUTATIONS={'handoff_case','share_case_file','publish_company_knowledge','manage_event_alerts'}

def execute(host,user,name,args,key):
 definitions={t['function']['name']:t['function']['parameters'] for t in tools()}
 if name not in definitions:return None
 spec=definitions[name]
 if not isinstance(args,dict) or set(args)-set(spec['properties']) or set(spec['required'])-set(args):return {'error':'invalid_request'}
 try:
  if name in MUTATIONS:
   if not key or len(key)>160:raise ValueError('idempotency_key_required')
   digest=hashlib.sha256(json.dumps([name,args],sort_keys=True,ensure_ascii=False).encode()).hexdigest()
   with host.db() as con:
    con.execute('BEGIN IMMEDIATE')
    old=con.execute('SELECT * FROM team_commands WHERE tenant_id=? AND owner_id=? AND key=?',(user['tenant_id'],user['id'],key)).fetchone()
    if old:
     if old['request_hash']!=digest:raise ValueError('idempotency_key_reused')
     # Replay returns only a receipt, never prior private content or a renewed grant.
     return {'replayed':True,'receipt':json.loads(old['result'])}
    result=mutate(con,host,user,name,args)
    con.execute('INSERT INTO team_commands VALUES(?,?,?,?,?)',(user['tenant_id'],user['id'],key,digest,json.dumps(result,ensure_ascii=False)))
    host.audit(con,user,'agent.team.'+name,'team',str(args.get('waybill_number',args.get('topic',''))),{'receipt':result})
    return result
  if name=='list_colleagues':
   with host.db() as con:return {'items':[dict(r) for r in con.execute('SELECT id,name,role FROM users WHERE tenant_id=? AND active=1 ORDER BY name',(user['tenant_id'],))]}
  if name=='get_case_workspace':return workspace(host,user,str(args['waybill_number']))
  if name in {'preview_waybill_import','commit_waybill_import'}:
   if user['role']!='owner_finance':raise PermissionError()
   from ingestion import handler
   handler.configure(db_factory=host.db,data_dir=host.DATA_DIR,audit_fn=host.audit,state_keys=host.STATE_KEYS,now_fn=host.now_iso)
   if name=='preview_waybill_import':
    row=files.authorized(host,user,args['file_id'])
    status,result=handler.handle_preview(user,row['filename'],'application/octet-stream',files.path_for(host,row).read_bytes())
   else:status,result=handler.handle_commit(user,args['preview_id'],key)
   return {'status':status,**result}
  if name=='search_company_knowledge':return knowledge(host,user,str(args['query']))
  if name=='get_agent_usage':return usage(host,user,args)
  if name=='get_agent_health':
   if user['role']=='employee':raise PermissionError()
   with host.db() as con:
    counts=[dict(r) for r in con.execute('SELECT state,count(*) AS count,min(created_at) AS oldest FROM agent_jobs WHERE tenant_id=? GROUP BY state',(user['tenant_id'],))]
   from . import runtime
   worker=runtime._WORKERS.get(str(host.DB_PATH))
   return {'jobs':counts,'worker_alive':bool(worker and worker[0].is_alive()),'execution_slots':4,'per_person_running_limit':1,'per_person_pending_limit':20,'provider_health':'not_probed'}
 except PermissionError:return {'error':'forbidden'}
 except LookupError:return {'error':'not_found'}
 except (ValueError,TypeError,KeyError) as exc:return {'error':str(exc)[:100] if isinstance(exc,ValueError) else 'invalid_request'}

def mutate(con,host,user,name,args):
 tenant=user['tenant_id'];uid=user['id'];now=host.now_iso()
 if name=='manage_event_alerts':
  if type(args['enabled']) is not bool:raise ValueError('invalid_enabled')
  con.execute('INSERT INTO event_subscriptions VALUES(?,?,?) ON CONFLICT(tenant_id,owner_id) DO UPDATE SET enabled=excluded.enabled',(tenant,uid,int(args['enabled'])))
  return {'enabled':args['enabled']}
 if name=='publish_company_knowledge':
  if user['role']!='owner_finance':raise PermissionError()
  topic=str(args['topic']).strip();content=str(args['content']);title=str(args['title']);scope=str(args['applies_to']).strip()
  if not topic or len(topic)>120 or not title or len(title)>160 or not content or len(content)>8000 or not scope or len(scope)>500:raise ValueError('invalid_knowledge')
  start=datetime.fromisoformat(args['valid_from']).date();end=datetime.fromisoformat(args['valid_until']).date()
  if end<start:raise ValueError('invalid_validity')
  if args['audience'] not in files.RANK:raise ValueError('invalid_audience')
  owner=con.execute('SELECT role FROM users WHERE id=? AND tenant_id=? AND active=1',(args['owner_id'],tenant)).fetchone()
  if not owner:raise ValueError('invalid_owner')
  if files.RANK[owner['role']]<files.RANK[args['audience']]:raise ValueError('owner_cannot_access_knowledge')
  latest=con.execute('SELECT coalesce(max(version),0) FROM company_knowledge WHERE tenant_id=? AND topic=?',(tenant,topic)).fetchone()[0]
  if type(args['expected_version']) is not int or args['expected_version']!=latest:raise ValueError('version_conflict')
  sources=args.get('source_ids',[])
  if not isinstance(sources,list) or len(sources)>12:raise ValueError('invalid_sources')
  for fid in sources:
   row=files.authorized(host,user,fid)
   if row['role_rank']>files.RANK[args['audience']]:raise ValueError('source_access_exceeds_audience')
  kid=secrets.token_urlsafe(16)
  con.execute('INSERT INTO company_knowledge VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',(kid,tenant,topic,latest+1,title,content,args['owner_id'],scope,start.isoformat(),end.isoformat(),files.RANK[args['audience']],json.dumps(sources),now))
  return {'id':kid,'topic':topic,'version':latest+1}
 number=str(args['waybill_number']).strip();doc=ticket(con,host,number)
 if not access(con,user,doc):raise LookupError()
 if name=='handoff_case':
  if user['role']=='employee':raise PermissionError()
  old=con.execute('SELECT * FROM case_teams WHERE tenant_id=? AND number=?',(tenant,number)).fetchone();rev=old['revision'] if old else 0
  if type(args['expected_revision']) is not int or args['expected_revision']!=rev:raise ValueError('revision_conflict')
  support=args.get('support_ids',[])
  if not isinstance(support,list) or len(support)>12:raise ValueError('invalid_support')
  support=sorted(set(support)-{args['responsible_id']})
  for person in [args['responsible_id']]+support:
   if not con.execute('SELECT 1 FROM users WHERE id=? AND tenant_id=? AND active=1',(person,tenant)).fetchone():raise ValueError('invalid_assignee')
  con.execute('INSERT INTO case_teams VALUES(?,?,?,?,?,?) ON CONFLICT(tenant_id,number) DO UPDATE SET revision=excluded.revision,responsible_id=excluded.responsible_id,support_ids=excluded.support_ids,updated_at=excluded.updated_at',(tenant,number,rev+1,args['responsible_id'],json.dumps(support),now))
  details={'revision':rev+1,'responsible_id':args['responsible_id'],'support_ids':support,'note':str(args.get('note',''))[:2000]}
  # Existing open tasks follow the previous responsible person; collaborators' tasks stay assigned.
  prior=old['responsible_id'] if old else doc.get('created_by')
  con.execute("UPDATE tasks SET assignee_id=?,updated_at=? WHERE waybill_number=? AND assignee_id=? AND status IN ('open','in_progress') AND created_by IN (SELECT id FROM users WHERE tenant_id=?)",(args['responsible_id'],now,number,prior,tenant))
  con.execute('INSERT INTO case_events(tenant_id,number,actor_id,kind,details,created_at) VALUES(?,?,?,?,?,?)',(tenant,number,uid,'handoff',json.dumps(details,ensure_ascii=False),now))
  return {'number':number,'revision':rev+1,'responsible_id':args['responsible_id'],'support_ids':support}
 if args['action'] not in {'share','unshare'}:raise ValueError('invalid_action')
 row=con.execute('SELECT * FROM agent_files WHERE id=? AND tenant_id=? AND owner_id=?',(args['file_id'],tenant,uid)).fetchone()
 if not row or files.RANK[user['role']]<row['role_rank']:raise LookupError()
 if args['action']=='unshare':con.execute('DELETE FROM case_files WHERE tenant_id=? AND number=? AND file_id=?',(tenant,number,row['id']))
 else:
  audience=args.get('audience',user['role'])
  if audience not in files.RANK or not row['role_rank']<=files.RANK[audience]<=files.RANK[user['role']]:raise ValueError('invalid_audience')
  # Sharing is explicit; never transfer ownership of the private original.
  con.execute('INSERT INTO case_files VALUES(?,?,?,?,?,?) ON CONFLICT(tenant_id,number,file_id) DO UPDATE SET min_rank=excluded.min_rank,shared_by=excluded.shared_by,created_at=excluded.created_at',(tenant,number,row['id'],files.RANK[audience],uid,now))
 con.execute('INSERT INTO case_events(tenant_id,number,actor_id,kind,details,created_at) VALUES(?,?,?,?,?,?)',(tenant,number,uid,'file_'+args['action'],json.dumps({'file_id':row['id']}),now))
 return {'number':number,'file_id':row['id'],'action':args['action']}

def workspace(host,user,number):
 with host.db() as con:
  doc=ticket(con,host,number)
  if not access(con,user,doc):raise LookupError()
  row=con.execute('SELECT * FROM case_teams WHERE tenant_id=? AND number=?',(user['tenant_id'],number)).fetchone()
  result={'number':number,'revision':row['revision'] if row else 0,'responsible_id':row['responsible_id'] if row else doc.get('created_by'),'support_ids':json.loads(row['support_ids']) if row else []}
  result['task_assignee_ids']=[r[0] for r in con.execute("SELECT DISTINCT t.assignee_id FROM tasks t JOIN users u ON u.id=t.created_by WHERE u.tenant_id=? AND t.waybill_number=? AND t.assignee_id IS NOT NULL AND t.status IN ('open','in_progress')",(user['tenant_id'],number))]
  state_rev=con.execute('SELECT rev FROM work_state WHERE id=1').fetchone()['rev']
  result['evidence']=[{'evidence_id':f'v3:waybill:{number}:r{state_rev}','source':'waybill_case'}]
  rank=files.RANK[user['role']]
  result['files']=[files.public(r) for r in con.execute('SELECT f.* FROM agent_files f JOIN case_files c ON c.file_id=f.id AND c.tenant_id=f.tenant_id WHERE c.tenant_id=? AND c.number=? AND c.min_rank<=? AND f.role_rank<=?',(user['tenant_id'],number,rank,rank))]
  result['history']=[{'kind':r['kind'],'actor_id':r['actor_id'],'created_at':r['created_at'],'details':host._redact_tree(json.loads(r['details'])) if user['role']!='owner_finance' else json.loads(r['details'])} for r in con.execute('SELECT * FROM case_events WHERE tenant_id=? AND number=? ORDER BY id DESC LIMIT 50',(user['tenant_id'],number))]
  return result

def knowledge(host,user,query):
 today=datetime.now().astimezone().date().isoformat()
 with host.db() as con:rows=con.execute('SELECT k.*,u.name AS owner_name,u.active AS owner_active FROM company_knowledge k JOIN users u ON u.id=k.owner_id WHERE k.tenant_id=? AND (k.version=(SELECT max(v.version) FROM company_knowledge v WHERE v.tenant_id=k.tenant_id AND v.topic=k.topic AND v.valid_from<=?) OR k.version=(SELECT max(v.version) FROM company_knowledge v WHERE v.tenant_id=k.tenant_id AND v.topic=k.topic)) ORDER BY k.created_at DESC',(user['tenant_id'],today)).fetchall()
 result=[]
 for row in rows:
  if row['min_rank']>files.RANK[user['role']]:continue
  if user['role']=='employee' and host._v3_output_has_explicit_price(' '.join(str(row[k] or '') for k in ('title','content','applies_to','topic'))):continue
  if query.casefold() not in (row['title']+' '+row['content']+' '+row['applies_to']+' '+row['topic']).casefold():continue
  status='future' if row['valid_from']>today else 'expired' if row['valid_until']<today else 'owner_inactive' if not row['owner_active'] else 'current'
  result.append({k:row[k] for k in ('id','topic','version','title','content','owner_id','owner_name','applies_to','valid_from','valid_until')}|{'status':status,'source_ids':json.loads(row['source_ids'])})
 return {'items':result[:30],'total':len(result),'coverage':'current_and_latest_scheduled_version','as_of':today}

def usage(host,user,args):
 scope=args.get('scope','mine');days=args.get('days',30)
 if scope not in {'mine','company'} or type(days) is not int or not 1<=days<=90:raise ValueError('invalid_request')
 if scope=='company' and user['role']!='owner_finance':raise PermissionError()
 cutoff=(datetime.now(timezone.utc)-timedelta(days=days)).isoformat()
 with host.db() as con:
  rows=con.execute('SELECT a.*,u.name AS owner_name FROM agent_usage a LEFT JOIN users u ON u.id=a.owner_id AND u.tenant_id=a.tenant_id WHERE a.tenant_id=? AND julianday(a.created_at)>=julianday(?)'+(' AND a.owner_id=?' if scope=='mine' else '')+' ORDER BY a.id DESC',(user['tenant_id'],cutoff,*([user['id']] if scope=='mine' else []))).fetchall()
 items=[];known=0;unknown=0;input_total=0;output_total=0;input_unknown=0;output_unknown=0
 for row in rows:
  raw=json.loads(row['model_usage']);total=raw.get('total_tokens')
  if type(total) is int and total>=0:known+=total
  else:unknown+=1
  for field in ('prompt_tokens','completion_tokens'):
   value=raw.get(field)
   if field=='prompt_tokens':
    if type(value) is int and value>=0:input_total+=value
    else:input_unknown+=1
   else:
    if type(value) is int and value>=0:output_total+=value
    else:output_unknown+=1
  item={'id':row['id'],'owner_id':row['owner_id'],'owner_name':row['owner_name'],'created_at':row['created_at'],'status':row['status'],'model_usage':raw,'job_id':row['job_id']}
  if scope=='mine':item['conversation_id']=row['conversation_id']
  items.append(item)
 return {'scope':scope,'days':days,'calls':len(rows),'known_total_tokens':known,'unknown_usage_calls':unknown,'known_input_tokens':input_total,'known_output_tokens':output_total,'unknown_input_calls':input_unknown,'unknown_output_calls':output_unknown,'balance':None,'balance_status':'not_configured','items':items[:100],'items_truncated':len(items)>100,'billing':'pricing_not_configured'}

def tick(host):
 """Transition notifications, explicitly enabled; no model calls or external sends."""
 from .runtime import get_user
 with host.db() as con:subs=con.execute('SELECT * FROM event_subscriptions WHERE enabled=1').fetchall()
 for sub in subs:
  user=get_user(host,sub['owner_id'],sub['tenant_id'])
  if not user:continue
  current={}
  with host.db() as con:
   if getattr(host, '_module_enabled', lambda _: True)('transport'):
    for row in con.execute('SELECT * FROM case_teams WHERE tenant_id=? AND responsible_id=?',(user['tenant_id'],user['id'])):
     current['case:'+row['number']]=(str(row['revision']),f"运单 {row['number']} 已交给你负责，可以直接继续处理。")
   for row in con.execute("SELECT t.* FROM tasks t JOIN users u ON u.id=t.created_by WHERE u.tenant_id=? AND t.assignee_id=? AND t.status IN ('open','in_progress') AND t.due_at IS NOT NULL",(user['tenant_id'],user['id'])):
    try:
     due=datetime.fromisoformat(row['due_at']);due=due if due.tzinfo else due.astimezone()
     if due>datetime.now(timezone.utc):continue
    except ValueError:continue
    current['overdue:'+row['id']]=(row['due_at'],f"有一项分配给你的待办已到期：{row['title'][:160]}")
   today=datetime.now().astimezone().date()
   for row in con.execute('SELECT k.* FROM company_knowledge k WHERE k.tenant_id=? AND k.owner_id=? AND k.min_rank<=? AND k.version=(SELECT max(version) FROM company_knowledge v WHERE v.tenant_id=k.tenant_id AND v.topic=k.topic AND v.valid_from<=?)',(user['tenant_id'],user['id'],files.RANK[user['role']],today.isoformat())):
    remaining=(datetime.fromisoformat(row['valid_until']).date()-today).days
    if remaining<=7:
     kind='knowledge_expired:' if remaining<0 else 'knowledge_due:'
     current[kind+row['topic']]=(str(row['version'])+':'+row['valid_until'],('你负责的公司资料已过期：' if remaining<0 else '你负责的公司资料将在7天内到期：')+row['title'][:160]+'。请核对并更新有效版本。')
   con.execute('BEGIN IMMEDIATE')
   old={r['event_key']:r for r in con.execute('SELECT * FROM event_observations WHERE tenant_id=? AND owner_id=?',(user['tenant_id'],user['id']))}
   for event,(fingerprint,text) in current.items():
    prior=old.get(event)
    if prior and prior['active'] and prior['fingerprint']==fingerprint:continue
    episode=prior['episode']+1 if prior else 1
    source=hashlib.sha256(json.dumps([user['tenant_id'],user['id'],event,episode]).encode()).hexdigest()
    if user['role']!='owner_finance':text=str(host._redact_tree({'title':text})['title'])
    con.execute('INSERT OR IGNORE INTO agent_notifications VALUES(?,?,?,?,?,?,?,?,0)',(secrets.token_urlsafe(16),user['tenant_id'],user['id'],files.RANK[user['role']],'event:'+source,None,text,host.now_iso()))
    con.execute('INSERT INTO event_observations VALUES(?,?,?,?,?,1) ON CONFLICT(tenant_id,owner_id,event_key) DO UPDATE SET fingerprint=excluded.fingerprint,episode=excluded.episode,active=1',(user['tenant_id'],user['id'],event,fingerprint,episode))
   for event in set(old)-set(current):
    source=hashlib.sha256(json.dumps([user['tenant_id'],user['id'],event,old[event]['episode']]).encode()).hexdigest()
    con.execute('UPDATE agent_notifications SET seen=1 WHERE source_id=?',('event:'+source,))
    con.execute('UPDATE event_observations SET active=0 WHERE tenant_id=? AND owner_id=? AND event_key=?',(user['tenant_id'],user['id'],event))
