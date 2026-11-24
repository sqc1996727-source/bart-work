"""Cross-industry memory, with explicit scope and versioned changes."""
import json,re,secrets
from datetime import date
from rapidfuzz import fuzz
from . import files

SECRET=re.compile(r'(?i)(password|api[_ -]?key|secret|token|私钥|密码|-----BEGIN)')

def initialize(host):
 with host.db() as con:
  con.execute('''CREATE TABLE IF NOT EXISTS workplace_memory (
    id TEXT PRIMARY KEY,tenant_id TEXT NOT NULL,owner_id TEXT NOT NULL,scope TEXT NOT NULL,
    topic TEXT NOT NULL,version INTEGER NOT NULL,kind TEXT NOT NULL,title TEXT NOT NULL,content TEXT NOT NULL,
    applies_to TEXT NOT NULL,valid_from TEXT NOT NULL,valid_until TEXT,role_rank INTEGER NOT NULL,
    source_conversation TEXT,created_at TEXT NOT NULL,active INTEGER NOT NULL,
    UNIQUE(tenant_id,owner_id,scope,topic,version))''')
  if 'created_by' not in {r['name'] for r in con.execute('PRAGMA table_info(workplace_memory)')}:
   con.execute('ALTER TABLE workplace_memory ADD COLUMN created_by TEXT')

def save(host,user,args,conversation_id):
 scope=args['scope'];kind=args['kind'];topic=str(args['topic']).strip()[:100]
 title=str(args['title']).strip()[:150];content=str(args['content']).strip()[:3000]
 applies=str(args.get('applies_to','')).strip()[:300]
 if scope not in ('personal','company') or kind not in ('preference','experience','lead','rule') or not topic or not title or not content:raise ValueError()
 if scope=='company' and user['role'] not in {'owner_finance','dispatcher'}:raise PermissionError()
 if kind=='rule' and scope!='company':raise ValueError()
 if SECRET.search(topic+' '+title+' '+content):raise ValueError()
 start=str(args.get('valid_from') or date.today().isoformat());end=str(args.get('valid_until') or '')
 date.fromisoformat(start)
 if end and date.fromisoformat(end)<date.fromisoformat(start):raise ValueError()
 action=args.get('action')
 if action is not None and action not in {'save','note','forget'}:raise ValueError()
 # forget 独立处理：撤回不需要 expected_version 匹配当前版本，
 # 也不应被 save/rule 的契约约束误伤。个人偏好本人可撤回，
 # 企业记忆仅授权岗位可撤回，全部留痕。
 if action=='forget':
  scope=args['scope'];topic=str(args.get('topic','')).strip()[:100]
  if scope not in ('personal','company') or not topic:raise ValueError('forget_requires_scope_topic')
  if scope=='company' and user['role'] not in {'owner_finance','dispatcher'}:raise PermissionError()
  owner=user['id'] if scope=='personal' else ''
  with host.db() as con:
   con.execute('BEGIN IMMEDIATE')
   actor=con.execute('SELECT role FROM users WHERE id=? AND tenant_id=? AND active=1',(user['id'],user['tenant_id'])).fetchone()
   if not actor or actor['role']!=user['role']:raise PermissionError()
   if scope=='company' and user['role']=='dispatcher':
    owned=con.execute('SELECT 1 FROM workplace_memory WHERE tenant_id=? AND owner_id=? AND scope=? AND topic=? AND created_by=? AND active=1',(user['tenant_id'],owner,scope,topic,user['id'])).fetchone()
    if not owned:raise PermissionError()
   cur=con.execute('SELECT active FROM workplace_memory WHERE tenant_id=? AND owner_id=? AND scope=? AND topic=? ORDER BY version DESC LIMIT 1',(user['tenant_id'],owner,scope,topic)).fetchone()
   if cur and cur['active']:
    con.execute('UPDATE workplace_memory SET active=0 WHERE tenant_id=? AND owner_id=? AND scope=? AND topic=?',(user['tenant_id'],owner,scope,topic))
    host.audit(con,user,'memory.forget','workplace_memory',topic,{'scope':scope})
   con.commit()
  return {'forgotten':True,'topic':topic}
 # 契约层防御：Agent 常把"主动沉淀"调成 save 却漏 expected_version。
 # 实际行为（与 catalog schema 一致）：
 #  - action=note：仅限 preference/experience/lead，无需版本，走同一套权限/隔离/SECRET/留痕；
 #  - action=save：仅限 kind=rule 且带合法 expected_version，按版本并发控制写入正式规则；
 #  - 缺省 action：kind=rule 且带合法版本才按 save，否则按 note；
 #  - action=forget：上面已独立处理，不经过本节版本校验。
 expected=args.get('expected_version')
 if action is None:
  action='save' if (kind=='rule' and type(expected) is int and expected>=0) else 'note'
 if action=='save' and kind!='rule':raise ValueError('formal_save_requires_rule_kind')
 if action=='save' and type(expected) is not int:raise ValueError('formal_save_requires_expected_version')
 if scope=='company' and user['role']=='dispatcher' and action!='note':raise PermissionError()
 note=action=='note'
 if note and kind=='rule':raise ValueError('rule_requires_explicit_save_with_version')
 if note and kind not in ('preference','experience','lead'):raise ValueError()
 if note and scope=='company' and kind=='rule':raise ValueError()
 if note and not applies:applies=args.get('applies_to','')
 expected=args.get('expected_version')
 if note:
  expected=None
 elif type(expected) is not int or expected<0:raise ValueError()
 owner=user['id'] if scope=='personal' else ''
 if note:
  # 「随口说的」工作记忆/个人偏好默认全员可读（或本人），除非显式指定 audience。
  # 工作记忆是企业通用的，不该默认只老板能看。
  rank=files.RANK[user['role']] if scope=='personal' else files.RANK.get(args.get('audience','employee'),-1)
 else:
  rank=files.RANK[user['role']] if scope=='personal' else files.RANK.get(args.get('audience','owner_finance'),-1)
 if rank<0:raise ValueError()
 if scope=='company' and user['role']=='dispatcher': rank=max(rank,files.RANK['dispatcher'])
 with host.db() as con:
  con.execute('BEGIN IMMEDIATE')
  actor=con.execute('SELECT role FROM users WHERE id=? AND tenant_id=? AND active=1',(user['id'],user['tenant_id'])).fetchone()
  if not actor or actor['role']!=user['role']: raise PermissionError()
  if note:
   row=con.execute('SELECT version,kind,created_by,role_rank FROM workplace_memory WHERE tenant_id=? AND owner_id=? AND scope=? AND topic=? ORDER BY version DESC LIMIT 1',(user['tenant_id'],owner,scope,topic)).fetchone()
   if row and (row['kind']=='rule' or (scope=='company' and user['role']=='dispatcher' and row['created_by']!=user['id'])):
    raise PermissionError('formal_memory_requires_explicit_version')
   expected=row['version'] if row else 0
   if row:
    # Repeated learning must not create endless versions or silently reduce visibility rank.
    rank=max(rank,row['role_rank'])
    latest=con.execute("SELECT * FROM workplace_memory WHERE tenant_id=? AND owner_id=? AND scope=? AND topic=? AND version=?",(user['tenant_id'],owner,scope,topic,expected)).fetchone()
    if latest and latest['active'] and all(latest[k]==v for k,v in {'kind':kind,'title':title,'content':content,'applies_to':applies,'valid_from':start,'valid_until':end or None,'role_rank':rank}.items()):
     return {'id':latest['id'],'topic':topic,'version':expected,'scope':scope,'kind':kind,'note':True,'unchanged':True}
  else:
   row=con.execute('SELECT coalesce(max(version),0) FROM workplace_memory WHERE tenant_id=? AND owner_id=? AND scope=? AND topic=?',(user['tenant_id'],owner,scope,topic)).fetchone()
   if row[0]!=expected:raise ValueError('version_conflict')
  mid=secrets.token_urlsafe(16)
  con.execute('INSERT INTO workplace_memory(id,tenant_id,owner_id,scope,topic,version,kind,title,content,applies_to,valid_from,valid_until,role_rank,source_conversation,created_at,active,created_by) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(mid,user['tenant_id'],owner,scope,topic,expected+1,kind,title,content,applies,start,end or None,rank,conversation_id,host.now_iso(),1,user['id']))
  host.audit(con,user,'memory.save','workplace_memory',mid,{'scope':scope,'kind':kind,'version':expected+1})
  con.commit()
 return {'id':mid,'topic':topic,'version':expected+1,'scope':scope,'kind':kind,'note':note}

def sensitive(host,text):
 text=str(text or '')
 return bool(SECRET.search(text) or host._v3_finance_request(text) or re.search(r'(?i)(?:金额|费用|价格|报价|成本|利润|价款|price|amount|cost|profit|运费|底价|运价|结算)|[¥￥$]|(?:[0-9０-９]+(?:\.[0-9]+)?\s*(?:元|块钱|人民币))',text))


def current_rows(host,con,tenant_id,principal_id,role,scope=None,allow_finance=False):
 if role not in files.RANK: return []
 today=date.today().isoformat();rank=files.RANK[role]
 rows=con.execute("""SELECT m.* FROM workplace_memory m WHERE m.tenant_id=? AND m.active=1
 AND (m.scope='company' OR (m.scope='personal' AND m.owner_id=?))
 AND m.valid_from<=? AND (m.valid_until IS NULL OR m.valid_until='' OR m.valid_until>=?)
 AND m.version=(SELECT max(v.version) FROM workplace_memory v WHERE v.tenant_id=m.tenant_id
 AND v.owner_id=m.owner_id AND v.scope=m.scope AND v.topic=m.topic AND v.active=1 AND v.valid_from<=?
 AND (v.valid_until IS NULL OR v.valid_until='' OR v.valid_until>=?))
 AND m.role_rank<=?""",(tenant_id,principal_id,today,today,today,today,rank)).fetchall()
 return [r for r in rows if (scope!='enterprise' or r['scope']=='company') and (allow_finance and role=='owner_finance' or not sensitive(host,' '.join(str(r[k] or '') for k in ('topic','title','content','applies_to'))))]


def search(host,user,query):
 query=str(query).strip()[:200]
 if not query:raise ValueError()
 today=date.today().isoformat();rank=files.RANK[user['role']];items=[]
 def add(item,text):
  if user['role']!='owner_finance' and sensitive(host,text):return
  from ai.agent_policy import query_terms,relevance
  score=fuzz.partial_ratio(query.casefold(),text.casefold())
  lexical=relevance(query_terms(query),text)
  if lexical>=2:score=max(score,min(95,55+lexical))
  if score>=55:items.append({**item,'match_score':round(score,1),'candidate_only':True})
 with host.db() as con:
  actor=con.execute('SELECT role FROM users WHERE id=? AND tenant_id=? AND active=1',(user['id'],user['tenant_id'])).fetchone()
  if not actor or actor['role']!=user['role']:raise PermissionError()
  from ai.service import TOOL_MEMORY_SCOPE
  scope=TOOL_MEMORY_SCOPE.get()
  rows=current_rows(host,con,user['tenant_id'],user['id'],user['role'],scope=scope)
  for r in rows:
   item={k:r[k] for k in ('scope','topic','version','kind','title','content','applies_to','valid_from','valid_until','source_conversation','created_at','created_by')};item['status']='current'
   add(item,' '.join(str(item[k] or '') for k in ('topic','title','content','applies_to')))
  for r in con.execute('SELECT key,value FROM agent_preferences WHERE owner_id=? AND active=1 AND role_rank<=?',(user['id'],rank)) if scope=='personal' else []:
   add({'source':'personal_preference','scope':'personal','kind':'preference','title':r['key'],'content':r['value'],'status':'current'},r['key']+' '+r['value'])
  for r in con.execute("SELECT * FROM long_term_memories WHERE tenant_id=? AND active=1 AND (scope='enterprise' OR (scope='personal' AND principal_id=? AND ?='personal')) AND (finance_only=0 OR ?=2)",(user['tenant_id'],user['id'],scope,rank)):
   add({'source':'legacy_memory','scope':r['scope'],'title':r['title'],'content':r['content'],'status':'current'},r['title']+' '+r['content'])
  for r in con.execute("SELECT r.* FROM business_rules r WHERE r.tenant_id=? AND r.effective_at<=? AND r.version=(SELECT max(v.version) FROM business_rules v WHERE v.tenant_id=r.tenant_id AND v.rule_key=r.rule_key AND v.effective_at<=?) AND (r.finance_only=0 OR ?=2)",(user['tenant_id'],today,today,rank)):
   add({'source':'business_rule','scope':'company','kind':'rule','title':r['title'],'content':r['content'],'version':r['version'],'status':'current'},r['title']+' '+r['content'])
  rows=con.execute("SELECT k.* FROM company_knowledge k JOIN users u ON u.id=k.owner_id AND u.tenant_id=k.tenant_id AND u.active=1 WHERE k.tenant_id=? AND k.min_rank<=? AND k.valid_from<=? AND (k.valid_until IS NULL OR k.valid_until='' OR k.valid_until>=?) AND k.version=(SELECT max(v.version) FROM company_knowledge v WHERE v.tenant_id=k.tenant_id AND v.topic=k.topic AND v.valid_from<=?)",(user['tenant_id'],rank,today,today,today)).fetchall()
  for r in rows:
   item={'source':'company_knowledge','scope':'company','kind':'rule','topic':r['topic'],'title':r['title'],'content':r['content'],'applies_to':r['applies_to'],'version':r['version'],'valid_from':r['valid_from'],'valid_until':r['valid_until'],'owner_id':r['owner_id'],'source_ids':json.loads(r['source_ids']),'status':'current'}
   add(item,' '.join(str(item[k] or '') for k in ('topic','title','content','applies_to')))
 items.sort(key=lambda x:x['match_score'],reverse=True)
 return {'items':items[:20],'coverage':'authorized_current_memory_and_rules','candidate_only':True,'note':'近似匹配仅是候选；核对适用范围、有效期和来源。未找到不代表没有规定。'}
