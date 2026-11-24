"""Deterministic business evidence; no model calls, credentials or external I/O."""
import json,hashlib
from decimal import Decimal,InvalidOperation

ROLE_RANK={'employee':0,'dispatcher':1,'owner_finance':2}

def initialize(host):
    with host.db() as con:
        con.executescript('''
        CREATE TABLE IF NOT EXISTS state_history(
          rev INTEGER PRIMARY KEY, updated_at TEXT NOT NULL, updated_by TEXT NOT NULL, payload TEXT NOT NULL);
        INSERT OR IGNORE INTO state_history SELECT rev,updated_at,updated_by,payload FROM work_state WHERE id=1;
        CREATE TRIGGER IF NOT EXISTS state_history_capture AFTER UPDATE ON work_state
        WHEN NEW.rev != OLD.rev BEGIN
          INSERT OR IGNORE INTO state_history VALUES(OLD.rev,OLD.updated_at,OLD.updated_by,OLD.payload);
          INSERT INTO state_history VALUES(NEW.rev,NEW.updated_at,NEW.updated_by,NEW.payload);
        END;
        CREATE TRIGGER IF NOT EXISTS state_history_no_update BEFORE UPDATE ON state_history
          BEGIN SELECT RAISE(ABORT,'history_is_append_only'); END;
        CREATE TRIGGER IF NOT EXISTS state_history_no_delete BEFORE DELETE ON state_history
          BEGIN SELECT RAISE(ABORT,'history_is_append_only'); END;
        CREATE TABLE IF NOT EXISTS conversation_access(
          conversation_id TEXT PRIMARY KEY, role_at_creation TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS document_access(
          document_id TEXT PRIMARY KEY, financial INTEGER NOT NULL);
        ''')

def conversation_allowed(host,user,cid):
    with host.db() as con:
        row=con.execute('SELECT role_at_creation FROM conversation_access WHERE conversation_id=?',(cid,)).fetchone()
    # Unknown old context is treated as financial; never infer original access.
    original=row['role_at_creation'] if row else 'owner_finance'
    return ROLE_RANK.get(user['role'],-1)>=ROLE_RANK.get(original,2)

def ticket_history(host,user,number,limit=20):
    with host.db() as con:
        current=con.execute('SELECT payload FROM work_state WHERE id=1').fetchone()
        docs=json.loads(host._decode_state(current['payload'])[host.STATE_KEYS[0]])
        current_doc=next((d for d in docs if str(d.get('number'))==number),None)
        if not current_doc or (user['role']=='employee' and not host._case_allowed(current_doc,user)):
            return {'error':'not_found'}
        rows=con.execute('SELECT * FROM state_history ORDER BY rev DESC LIMIT 1000').fetchall()
    changes=[]; previous=None
    for row in reversed(rows):
        state=host._decode_state(row['payload'])
        doc=next((d for d in json.loads(state[host.STATE_KEYS[0]]) if str(d.get('number'))==number),None)
        if user['role']=='employee' and doc and not host._case_allowed(current_doc,user):doc=None
        # Project every revision using today's authorization, including finance.
        visible=host.V3Context._pick(doc,host.V3Context._WAYBILL_FIELDS) if doc else None
        if visible is not None:
            visible['fees']=[]
            for fee in json.loads(state[host.STATE_KEYS[4]]):
                if str(fee.get('number'))!=number:continue
                safe=host.V3Context._pick(fee,host.V3Context._FEE_FIELDS)
                host._v3_add_finance(safe,fee,user,note=False)
                visible['fees'].append(safe)
            receipt=next((x for x in json.loads(state[host.STATE_KEYS[5]]) if str(x.get('number'))==number),None)
            visible['receipt']=host.V3Context._receipt_view(receipt)
        if visible!=previous:
            changes.append({'rev':row['rev'],'at':row['updated_at'],'by':row['updated_by'],
                            'before':previous,'after':visible})
        previous=visible
    return {'waybill_number':number,'items':list(reversed(changes))[:limit],
            'history_from_rev':min((r['rev'] for r in rows),default=None)}

def restore_revision(host,user,body,key):
    if user['role']!='owner_finance':return 403,{'error':'forbidden'}
    try:
        if set(body)-{'rev','base_rev','reason'}:raise ValueError()
        target=int(body['rev']);base=int(body['base_rev']);reason=str(body['reason']).strip()
        if not reason or len(reason)>300 or not key or len(key)>128:raise ValueError()
    except (KeyError,TypeError,ValueError):return 400,{'error':'invalid_restore_request'}
    digest=hashlib.sha256(json.dumps(body,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    with host.db() as con:
        con.execute('BEGIN IMMEDIATE')
        try:prior=host._idempotent_response(con,user['id'],key,digest)
        except ValueError:return 409,{'error':'idempotency_key_reused'}
        if prior:return prior
        current=con.execute('SELECT rev,payload FROM work_state WHERE id=1').fetchone()
        if current['rev']!=base:return 409,{'error':'revision_conflict','rev':current['rev']}
        old=con.execute('SELECT payload FROM state_history WHERE rev=?',(target,)).fetchone()
        if not old:return 404,{'error':'revision_not_found'}
        current_state=host._decode_state(current['payload']); target_state=host._decode_state(old['payload'])
        if any(json.loads(current_state[host.STATE_KEYS[i]]) != json.loads(target_state[host.STATE_KEYS[i]]) for i in (4,7)):
            return 409,{'error':'finance_restore_requires_manual_reconciliation'}
        def freight(state):
            return {row["number"]: {key: row[key] for key in host.SENSITIVE_KEYS if key in row}
                    for row in json.loads(state[host.STATE_KEYS[0]]) if isinstance(row, dict)
                    and isinstance(row.get("number"), str) and any(key in row for key in host.SENSITIVE_KEYS)}
        if freight(current_state) != freight(target_state):
            return 409,{'error':'finance_restore_requires_manual_reconciliation'}
        try:
            host._parse_state_domains(target_state)
        except (ValueError, TypeError):
            return 409,{'error':'invalid_restore_state'}
        rev=base+1;at=host.now_iso()
        con.execute('UPDATE work_state SET rev=?,payload=?,updated_at=?,updated_by=? WHERE id=1',(rev,old['payload'],at,user['id']))
        host._sync_business_tasks(con,user,host._decode_state(old['payload']))
        host.audit(con,user,'state.restore','state',str(rev),{'source_rev':target,'reason':reason})
        response={'rev':rev,'restored_from_rev':target,'updated':at}
        host._tool_idempotency_write(con,user['id'],key,digest,200,response,at)
        con.commit()
    return 200,response

def _money(value):
    try:
        amount=Decimal(str(value))
        return amount.quantize(Decimal('.01')) if amount.is_finite() and amount>=0 else None
    except (InvalidOperation,ValueError):return None

def review(host,user,number='',limit=30,offset=0):
    with host.db() as con:
        row=con.execute('SELECT * FROM work_state WHERE id=1').fetchone()
        raw=host._decode_state(row['payload'])
        tables={x['name'] for x in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        attached=con.execute('SELECT waybill_number,sha256 FROM attachments WHERE tenant_id=?',(user['tenant_id'],)).fetchall() if 'attachments' in tables else []
    docs=json.loads(raw[host.STATE_KEYS[0]])
    docs=[d for d in docs if isinstance(d,dict) and (user['role']!='employee' or host._case_allowed(d,user)) and (not number or str(d.get('number'))==number)]
    numbers={str(d.get('number')) for d in docs}
    fees=[f for f in json.loads(raw[host.STATE_KEYS[4]]) if isinstance(f,dict) and str(f.get('number')) in numbers and f.get('status')!='作废']
    bills=[b for b in json.loads(raw[host.STATE_KEYS[6]]) if isinstance(b,dict)]
    receipts={str(r.get('number')):r for r in json.loads(raw[host.STATE_KEYS[5]]) if isinstance(r,dict)}
    findings=[];confirmed=[]
    decisions=[d for d in json.loads(raw[host.STATE_KEYS[7]]) if isinstance(d,dict)]
    def add(kind,n,detail,**extra):
        item={'kind':kind,'waybill_number':str(n),'detail':detail,**extra}
        finance_kind=kind in {'amount_difference','fee_type_mismatch','identity_incomplete','missing_fee'}
        fingerprint=evidence_fingerprint(host,raw,str(n))
        resolved=finance_kind and any(str(d.get('number'))==str(n) and str(d.get('type',''))==str(extra.get('fee_type','')) and d.get('decision') and d.get('evidence_fingerprint')==fingerprint and d.get('confirmed_by_id') for d in decisions)
        if resolved:
            decision=next(d for d in reversed(decisions) if str(d.get('number'))==str(n) and str(d.get('type',''))==str(extra.get('fee_type','')) and d.get('evidence_fingerprint')==fingerprint and d.get('confirmed_by_id'))
            item['human_decision']=decision['decision'];item['confirmed_by_id']=decision['confirmed_by_id']
            item['settlement_changed']=False
        (confirmed if resolved else findings).append(item)
    for d in docs:
        n=str(d.get('number'));r=receipts.get(n,{})
        if not (r.get('paper') is True or (isinstance(r.get('paper'),dict) and not r['paper'].get('off'))):add('missing_paper',n,'纸质回单尚未登记收到')
        if not any(str(a['waybill_number'])==n for a in attached):add('missing_attachment',n,'这票的运单附件列表里还没有上传图片；其他地方是否有资料未在此核实')
    groups={};proofs={}
    for f in fees:
        identity=tuple(str(f.get(k,'')).strip() for k in ('number','type','partner','payer','currency'))
        groups.setdefault(identity,[]).append(f)
        proof=str(f.get('voucher_sha256') or f.get('sha256') or '').strip()
        if proof:proofs.setdefault(proof,[]).append(f)
    for identity,items in groups.items():
        if len(items)>1:add('possible_duplicate',identity[0],'同一运单、费用类型及承担关系有多条记录，需要核对是否同一笔费用',count=len(items))
    for proof,items in proofs.items():
        if len(items)>1:
            for n in sorted({str(f.get('number')) for f in items}):add('reused_proof',n,'多条费用记录可能关联了同一份资料；是否真是同一份、原件是否存在及内容是否相同尚未核实',count=len(items))
    # Compare the latest statement per partner, rather than all historical copies.
    latest={}
    for bill in bills:latest[str(bill.get('partner','')).strip()]=bill
    for partner,bill in latest.items():
        for b in bill.get('rows',[]):
            n=str(b.get('number',''))
            if n not in numbers:continue
            typ=str(b.get('type','')).strip();ours=[f for f in fees if str(f.get('number'))==n]
            matched=[f for f in ours if str(f.get('type','')).strip()==typ]
            if not matched:
                if ours:
                    our_types=sorted({str(f.get('type') or '未填写').strip() for f in ours})
                    partner_type=typ or '未填写'
                    detail='我方登记：'+'、'.join(our_types)+'；对方账单：'+partner_type+'。费用性质不同，不能直接相减；先核实原始记录，不为对平而改记录。'
                    evidence={'fee_type':typ,'our_fee_types':our_types,'partner_fee_type':partner_type}
                    if user['role']!='employee':
                        evidence['our_entries']=[{'type':str(f.get('type') or '未填写').strip(),'amount':str(v) if (v:=_money(f.get('amount'))) is not None else None} for f in ours]
                        value=_money(b.get('amount'))
                        evidence['partner_entry']={'type':partner_type,'amount':str(value) if value is not None else None}
                    add('fee_type_mismatch',n,detail,**evidence)
                else:
                    add('missing_fee',n,'对方账单有此费用，我方尚无同类记录',fee_type=typ,partner_fee_type=typ)
                continue
            # No automatic settlement if identity is incomplete or ambiguous.
            if len(matched)!=1:
                add('identity_incomplete',n,'同类费用有多条记录，需要核对对应关系',fee_type=typ);continue
            f=matched[0]
            theirs={**b,'partner':b.get('partner') or bill.get('partner')}
            conflicts=[k for k in ('partner','bearer','currency') if f.get(k) and theirs.get(k) and str(f[k]).strip()!=str(theirs[k]).strip()]
            if conflicts:
                add('attribution_mismatch',n,'合作方、最终承担方或币种不同，不能直接认定同一笔费用',fee_type=typ,fields=conflicts);continue
            missing=[k for k in ('partner','bearer','currency') if not (f.get(k) and theirs.get(k))]
            eligible=matched
            f=eligible[0];left=_money(f.get('amount'));right=_money(b.get('amount'))
            if left is None or right is None:add('invalid_amount',n,'金额无法用于核对',fee_type=typ);continue
            if missing and left==right:add('identity_incomplete',n,'金额相同，但合作方、最终承担方或币种仍有缺项，尚不能确认对平',fee_type=typ,fields=missing)
            if left!=right:
                extra={'fee_type':typ,'attribution_unconfirmed':missing}
                if user['role']!='employee':extra.update(our_amount=str(left),partner_amount=str(right),difference=str(right-left))
                add('amount_difference',n,'同一费用金额不一致，需要有权人员确认',**extra)
    if user['role']=='employee':
        # Remove finance descriptions as well as values from the staff view.
        findings=[f for f in findings if f['kind'] in {'missing_paper','missing_attachment'}]
    priority={'amount_difference':0,'fee_type_mismatch':1,'reused_proof':2,'possible_duplicate':3,'identity_incomplete':4,'missing_fee':5,'invalid_amount':6,'missing_paper':7,'missing_attachment':8}
    findings.sort(key=lambda f:(priority.get(f['kind'],9),f['waybill_number']))
    return {'state_rev':row['rev'],'items':findings[offset:offset+limit],'total':len(findings),
            'offset':offset,'next_offset':offset+limit if offset+limit<len(findings) else None,
            'coverage':{'source':'local_ledger','external_collection':'not_connected',
                        'ledger_updated_at':row['updated_at'],'waybill_count':len(docs)},
            'confirmed_items':confirmed,'confirmed_count':len(confirmed),'financial_calculation':'not_calculated'}

def suggestions(host,user):
    result=review(host,user,limit=200)
    kinds={f['kind'] for f in result['items']+result['confirmed_items']};items=[]
    if kinds & {'amount_difference','fee_type_mismatch','identity_incomplete','missing_fee'}:
        items.append({'label':'看看这次对账差在哪','prompt':'帮我核对账单和费用，先把差异和依据查清楚。'})
    if kinds & {'possible_duplicate','reused_proof'}:
        items.append({'label':'核对疑似重复费用','prompt':'哪些费用可能重复了？帮我查清证据，先别改账。'})
    if 'missing_paper' in kinds:items.append({'label':'哪些回单还要跟','prompt':'帮我看看哪些纸质回单还没收到，接下来该找谁跟进。'})
    items.append({'label':'今天先盯什么','prompt':'结合当前业务记录和待办，帮我看看今天先盯哪几件事。'})
    return items[:4]

def validate_state(host,user,old,new):
    old_fees=json.loads(old[host.STATE_KEYS[4]]);new_fees=json.loads(new[host.STATE_KEYS[4]])
    # Snapshots cannot attest a human approval. Legacy rows without IDs may be
    # retained unchanged; they cannot be used to acquire confirmation metadata.
    protected_status = ('status', 'confirmBy', 'confirmAt', 'confirmed_by_id')
    for f in new_fees:
        if not isinstance(f, dict): raise ValueError('invalid_fee')
        fid = str(f.get('id') or '')
        candidates = [x for x in old_fees if isinstance(x, dict) and fid and str(x.get('id') or '') == fid]
        if len(candidates) > 1: raise ValueError('duplicate_fee_id')
        prior = candidates[0] if candidates else next((x for x in old_fees if x == f), None)
        if prior is None:
            if f.get('status') in {'已确认','有差异','作废'} or any(f.get(k) for k in protected_status[1:]): raise ValueError('human_fee_confirmation_required')
        elif any(prior.get(k) != f.get(k) for k in protected_status):
            raise ValueError('human_fee_confirmation_required')
    for prior in old_fees:
        if isinstance(prior,dict) and prior.get('status')=='已确认' and prior not in new_fees: raise ValueError('confirmed_fee_requires_reversal')
    ids = [str(x.get('id')) for x in new_fees if isinstance(x,dict) and x.get('id')]
    if len(ids) != len(set(ids)): raise ValueError('duplicate_fee_id')
    old_by={str(f.get('id')):f for f in old_fees if isinstance(f,dict) and f.get('id')}
    new_by={str(f.get('id')):f for f in new_fees if isinstance(f,dict) and f.get('id')}
    for fid,f in old_by.items():
        if f.get('status')=='已确认':
            n=new_by.get(fid)
            protected=('number','type','amount','partner','payer','bearer','currency','voucher','voucher_sha256')
            if not n or n.get('status') not in {'已确认','作废'} or any(f.get(k)!=n.get(k) for k in protected):raise ValueError('confirmed_fee_requires_reversal')
    def key(f):
        amount=_money(f.get('amount'))
        if amount is None:amount='invalid:'+str(f.get('amount',''))
        return (str(f.get('number','')).strip(),str(f.get('type','')).strip(),str(amount),
                str(f.get('partner','')).strip(),str(f.get('payer','')).strip(),str(f.get('currency','')).strip(),
                str(f.get('voucher_sha256') or f.get('sha256') or f.get('voucher') or '').strip())
    prior={key(f) for f in old_fees if isinstance(f,dict) and f.get('status')!='作废'}
    seen=set()
    for f in new_fees:
        if not isinstance(f,dict):raise ValueError('invalid_fee')
        if f.get('status')=='作废':continue
        fid=str(f.get('id',''));k=key(f)
        old_f=old_by.get(fid) if fid else next((x for x in old_fees if x==f),None)
        changed=old_f is None or key(old_f)!=k
        if changed and (k in prior or k in seen):raise ValueError('duplicate_fee')
        seen.add(k)
        if f.get('status')=='已确认' and (not old_f or old_f.get('status')!='已确认'):
            f['confirmBy']=user['name'];f['confirmed_by_id']=user['id'];f['confirmAt']=host.now_iso()
    old_diffs=json.loads(old[host.STATE_KEYS[7]])
    new_diffs=json.loads(new[host.STATE_KEYS[7]])
    for d in new_diffs:
        if not isinstance(d,dict): raise ValueError('invalid_difference')
        did=str(d.get('id') or '')
        matches=[x for x in old_diffs if isinstance(x,dict) and did and str(x.get('id') or '')==did]
        if len(matches)>1: raise ValueError('duplicate_difference_id')
        prior=matches[0] if matches else next ( (x for x in old_diffs if x==d), None )
        fields=('decision','by','confirmed_by_id','at','evidence_fingerprint','closed','confirmed','resolved','done')
        if prior is None:
            if any(d.get(k) for k in fields): raise ValueError('human_difference_confirmation_required')
        elif any(prior.get(k)!=d.get(k) for k in fields): raise ValueError('human_difference_confirmation_required')
        if prior and prior.get('decision') and prior!=d: raise ValueError('confirmed_decision_is_immutable')
    for prior in old_diffs:
        if isinstance(prior,dict) and prior.get('decision') and prior not in new_diffs: raise ValueError('confirmed_decision_is_immutable')
    new[host.STATE_KEYS[4]]=json.dumps(new_fees,ensure_ascii=False,separators=(',',':'))
    new[host.STATE_KEYS[7]]=json.dumps(new_diffs,ensure_ascii=False,separators=(',',':'))


def evidence_fingerprint(host,state,number):
    fees=[f for f in json.loads(state[host.STATE_KEYS[4]]) if str(f.get('number'))==number]
    bills=[]
    for b in json.loads(state[host.STATE_KEYS[6]]):
        rows=[r for r in b.get('rows',[]) if str(r.get('number'))==number]
        if rows:bills.append({'partner':b.get('partner'),'rows':rows})
    return hashlib.sha256(json.dumps([fees,bills],sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def record_conversation_access(host,user,cid):
    with host.db() as con:
        old=con.execute('SELECT role_at_creation FROM conversation_access WHERE conversation_id=?',(cid,)).fetchone()
        role=user['role']
        if old and ROLE_RANK.get(old['role_at_creation'],2)>ROLE_RANK.get(role,0):return
        con.execute('INSERT INTO conversation_access VALUES(?,?) ON CONFLICT(conversation_id) DO UPDATE SET role_at_creation=excluded.role_at_creation',(cid,role))
