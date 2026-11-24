"""Human-reviewed fee transitions, backed by transactional immutable candidates."""
import hashlib, hmac, json, os, secrets

# 费用类型白名单：必须与 capabilities/routine.py 的实际比对类型一致。
# routine 按「运费」「进仓费」比对厂家账，若人审闸门不收「运费」，
# 差异会永远卡在待确认、无法留痕关闭。可由部署端 BOT_FEE_TYPES 覆盖，
# 避免再按企业写死。仅授权岗位可进入本模块，加入运费不会让员工看到金额。
def _fee_types():
    raw = os.environ.get("BOT_FEE_TYPES", "")
    if raw:
        types = frozenset(x.strip() for x in raw.split(",") if x.strip())
        if types:
            return types
    return frozenset({"运费", "进仓费", "送货费", "等时费", "其他"})


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _table(con):
    con.execute('''CREATE TABLE IF NOT EXISTS finance_candidates (
        id TEXT PRIMARY KEY, approver_id TEXT NOT NULL, fee_id TEXT NOT NULL,
        expected_rev INTEGER NOT NULL, before_json TEXT NOT NULL, target_status TEXT NOT NULL,
        created_at TEXT NOT NULL, result_json TEXT, completed_at TEXT)''')


def create(host, user, args, key):
    if user['role'] not in {'dispatcher', 'owner_finance'}: raise PermissionError('fee_update_forbidden')
    if not isinstance(args, dict) or not {'fee_id','status'} <= set(args) or set(args)-{'fee_id','status','reason'} or not isinstance(args['fee_id'], str) or not args['fee_id'].strip() or args['status'] not in {'待确认','已确认','有差异','作废'}: raise ValueError('invalid_fee_update')
    if args['status']=='作废' and (not isinstance(args.get('reason'),str) or not args['reason'].strip() or len(args['reason'])>300): raise ValueError('reversal_reason_required')
    if not key or len(key)>128: raise ValueError('idempotency_key_required')
    req=hashlib.sha256(_json({'tool':'update_fee_status','arguments':args}).encode()).hexdigest()
    with host.db() as con:
        con.execute('BEGIN IMMEDIATE'); _table(con)
        prior=host._idempotent_response(con,user['id'],key,req)
        if prior: con.rollback(); return prior[1]
        row=con.execute('SELECT rev,payload FROM work_state WHERE id=1').fetchone()
        if not row: raise RuntimeError('state_unavailable')
        state=host._decode_state(row['payload']); fees=json.loads(state[host.STATE_KEYS[4]])
        matches=[f for f in fees if isinstance(f,dict) and str(f.get('id',''))==args['fee_id'].strip()]
        if len(matches)!=1: raise ValueError('invalid_or_duplicate_fee_id')
        fee=matches[0]
        if fee.get('status')=='作废': raise ValueError('invalid_fee_status')
        if fee.get('status')=='已确认' and args['status']!='作废': raise ValueError('confirmed_fee_requires_reversal')
        if args['status']=='作废' and fee.get('status')!='已确认': raise ValueError('invalid_fee_status')
        if fee.get('type') not in _fee_types(): raise ValueError('invalid_fee_type')
        number=str(fee.get('number','')).strip()
        docs=json.loads(state[host.STATE_KEYS[0]])
        if not number or sum(isinstance(d,dict) and str(d.get('number','')).strip()==number for d in docs)!=1: raise ValueError('invalid_fee_waybill')
        import business_integrity
        if business_integrity._money(fee.get('amount')) is None: raise ValueError('invalid_fee_amount')
        def business_key(f):
            return tuple(str(f.get(k,'')).strip() for k in ('number','type','partner','payer','bearer','currency','voucher','voucher_sha256')) + (str(business_integrity._money(f.get('amount'))),)
        if sum(isinstance(f,dict) and f.get('status')!='作废' and business_key(f)==business_key(fee) for f in fees)!=1: raise ValueError('duplicate_fee')
        cid=secrets.token_urlsafe(32); now=host.now_iso()
        con.execute('INSERT INTO finance_candidates(id,approver_id,fee_id,expected_rev,before_json,target_status,created_at) VALUES(?,?,?,?,?,?,?)',(cid,user['id'],args['fee_id'].strip(),row['rev'],_json(fee),args['status'] if args['status']!='作废' else '__void__'+args['reason'].strip(),now))
        result={'candidate_id':cid}
        host._tool_idempotency_write(con,user['id'],key,req,200,result,now); con.commit(); return result


def review(host,user,cid):
    if user['role'] not in {'dispatcher','owner_finance'}: raise PermissionError('forbidden')
    with host.db() as con:
        _table(con); row=con.execute('SELECT * FROM finance_candidates WHERE id=? AND approver_id=?',(cid,user['id'])).fetchone()
        if not row: raise LookupError('not_found')
        before=json.loads(row['before_json'])
        if row['target_status']=='__difference__':
            if user['role'] not in {'owner_finance','dispatcher'}: raise PermissionError('forbidden')
            current=con.execute('SELECT rev,payload FROM work_state WHERE id=1').fetchone()
            state=host._decode_state(current['payload']); number=before['args']['number']
            fees=[f for f in json.loads(state[host.STATE_KEYS[4]]) if isinstance(f,dict) and str(f.get('number',''))==number]
            bills=[{'partner':b.get('partner'),'rows':[r for r in b.get('rows',[]) if str(r.get('number',''))==number]} for b in json.loads(state[host.STATE_KEYS[6]]) if isinstance(b,dict)]
            bills=[b for b in bills if b['rows']]
            return {'candidate_id':cid,'kind':'difference','difference':before['before'],'proposal':before['args'],'fees':fees,'bills':bills,'evidence_fingerprint':before['evidence_fingerprint'],'expected_rev':row['expected_rev'],'stale':current['rev']!=row['expected_rev'],'completed':bool(row['completed_at'])}
        current = con.execute("SELECT rev FROM work_state WHERE id=1").fetchone()
        return {'candidate_id':cid,'fee':before,'target_status':'作废' if row['target_status'].startswith('__void__') else row['target_status'],'reason':row['target_status'][8:] if row['target_status'].startswith('__void__') else None,'expected_rev':row['expected_rev'],'stale':not current or current['rev']!=row['expected_rev'],'completed':bool(row['completed_at'])}


def pending(host, user):
    if user["role"] not in {"dispatcher", "owner_finance"}:
        raise PermissionError("forbidden")
    with host.db() as con:
        _table(con)
        rows = con.execute(
            """SELECT c.id,c.fee_id,c.target_status,c.created_at FROM finance_candidates c
               JOIN work_state s ON s.id=1 AND s.rev=c.expected_rev
               WHERE c.approver_id=? AND c.completed_at IS NULL
               ORDER BY c.created_at DESC,c.rowid DESC LIMIT 50""",
            (user["id"],),
        ).fetchall()
    return {"items": [{
        "candidate_id": row["id"], "record_id": row["fee_id"],
        "kind": "difference" if row["target_status"] == "__difference__" else "fee",
        "created_at": row["created_at"],
    } for row in rows]}


def reauthenticate(host,user,password):
    if not isinstance(password,str) or not password or len(password)>1024: raise PermissionError('reauthentication_failed')
    with host.db() as con:
        actor=con.execute('SELECT * FROM users WHERE id=? AND active=1',(user['id'],)).fetchone()
        if not actor or actor['role'] not in {'dispatcher','owner_finance'} or not hmac.compare_digest(hashlib.pbkdf2_hmac('sha256',password.encode(),actor['password_salt'],host.PASSWORD_ITERATIONS),actor['password_hash']): raise PermissionError('reauthentication_failed')


def confirm(host,user,cid,password):
    if user['role'] not in {'dispatcher','owner_finance'}: raise PermissionError('forbidden')
    if not isinstance(password,str) or not password or len(password)>1024: raise PermissionError('reauthentication_failed')
    with host.db() as con:
        con.execute('BEGIN IMMEDIATE'); _table(con)
        actor=con.execute('SELECT * FROM users WHERE id=? AND active=1',(user['id'],)).fetchone()
        if not actor or actor['role'] not in {'dispatcher','owner_finance'} or not hmac.compare_digest(hashlib.pbkdf2_hmac('sha256',password.encode(),actor['password_salt'],host.PASSWORD_ITERATIONS),actor['password_hash']): raise PermissionError('reauthentication_failed')
        row=con.execute('SELECT * FROM finance_candidates WHERE id=? AND approver_id=?',(cid,user['id'])).fetchone()
        if not row: raise LookupError('not_found')
        if row['result_json']: con.rollback(); return json.loads(row['result_json'])
        state_row=con.execute('SELECT rev,payload FROM work_state WHERE id=1').fetchone()
        if not state_row or state_row['rev']!=row['expected_rev']: raise ValueError('revision_conflict')
        state=host._decode_state(state_row['payload'])
        if row['target_status']=='__difference__':
            import business_integrity
            if actor['role'] not in {'owner_finance','dispatcher'}: raise PermissionError('forbidden')
            envelope=json.loads(row['before_json']); args=envelope['args']
            if business_integrity.evidence_fingerprint(host,state,args['number']) != envelope['evidence_fingerprint']: raise ValueError('difference_evidence_changed')
            diffs=json.loads(state[host.STATE_KEYS[7]])
            indices=[i for i,d in enumerate(diffs) if isinstance(d,dict) and str(d.get('id',''))==args['difference_id']]
            before=envelope['before']
            if (before is None and indices) or (before is not None and (len(indices)!=1 or diffs[indices[0]]!=before)): raise ValueError('difference_evidence_changed')
            after=dict(before or {'id':args['difference_id'],'number':args['number'],'type':args['type']}); now=host.now_iso()
            after.update(decision=args['decision'],by=actor['name'],confirmed_by_id=actor['id'],at=now,evidence_fingerprint=envelope['evidence_fingerprint'])
            if indices: diffs[indices[0]]=after
            else: diffs.append(after)
            state[host.STATE_KEYS[7]]=json.dumps(diffs,ensure_ascii=False,separators=(',',':'))
            payload=json.dumps(state,ensure_ascii=False,separators=(',',':')); rev=state_row['rev']+1
            con.execute('UPDATE work_state SET rev=?,updated_at=?,updated_by=?,payload=? WHERE id=1',(rev,now,actor['id'],payload))
            host.audit(con,actor,'finance.difference.confirm','difference',args['difference_id'],{'candidate_id':cid,'before':before,'after':after,'rev':rev})
            host._sync_business_tasks(con,actor,host._decode_state(payload))
            result={'candidate_id':cid,'difference_id':args['difference_id'],'decision':args['decision'],'state_rev':rev}
            con.execute('UPDATE finance_candidates SET result_json=?,completed_at=? WHERE id=?',(_json(result),now,cid)); con.commit(); return result
        fees=json.loads(state[host.STATE_KEYS[4]])
        matches=[i for i,f in enumerate(fees) if isinstance(f,dict) and str(f.get('id',''))==row['fee_id']]
        if len(matches)!=1 or _json(fees[matches[0]])!=row['before_json']: raise ValueError('fee_evidence_changed')
        before=fees[matches[0]]; after=dict(before); now=host.now_iso()
        target='作废' if row['target_status'].startswith('__void__') else row['target_status']
        after.update(status=target,confirmBy=actor['name'],confirmed_by_id=actor['id'],confirmAt=now)
        if target=='作废': after['reversal_reason']=row['target_status'][8:]
        fees[matches[0]]=after; state[host.STATE_KEYS[4]]=json.dumps(fees,ensure_ascii=False,separators=(',',':'))
        payload=json.dumps(state,ensure_ascii=False,separators=(',',':')); rev=state_row['rev']+1
        con.execute('UPDATE work_state SET rev=?,updated_at=?,updated_by=?,payload=? WHERE id=1',(rev,now,actor['id'],payload))
        host.audit(con,actor,'finance.fee.confirm','fee',row['fee_id'],{'candidate_id':cid,'before':before,'after':after,'rev':rev})
        host._sync_business_tasks(con,actor,host._decode_state(payload))
        result={'candidate_id':cid,'fee_id':row['fee_id'],'status':target,'state_rev':rev}
        con.execute('UPDATE finance_candidates SET result_json=?,completed_at=? WHERE id=?',(_json(result),now,cid)); con.commit(); return result


def create_difference(host,user,args,key):
    import business_integrity
    if user['role'] not in {'owner_finance','dispatcher'}: raise PermissionError('forbidden')
    if not isinstance(args,dict) or set(args)!={'difference_id','number','type','decision'} or any(not isinstance(args[k],str) or not args[k].strip() or len(args[k])>300 for k in args): raise ValueError('invalid_difference')
    if not key or len(key)>128: raise ValueError('idempotency_key_required')
    args={k:v.strip() for k,v in args.items()}
    digest=hashlib.sha256(_json({'kind':'difference','arguments':args}).encode()).hexdigest()
    with host.db() as con:
        con.execute('BEGIN IMMEDIATE'); _table(con)
        prior=host._idempotent_response(con,user['id'],key,digest)
        if prior: con.rollback(); return prior[1]
        row=con.execute('SELECT rev,payload FROM work_state WHERE id=1').fetchone()
        state=host._decode_state(row['payload']); diffs=json.loads(state[host.STATE_KEYS[7]])
        docs=json.loads(state[host.STATE_KEYS[0]])
        if sum(isinstance(d,dict) and str(d.get('number',''))==args['number'] for d in docs)!=1: raise ValueError('invalid_difference_waybill')
        if args['type'] not in _fee_types(): raise ValueError('invalid_difference_type')
        fees=json.loads(state[host.STATE_KEYS[4]]); bills=json.loads(state[host.STATE_KEYS[6]])
        if not any(isinstance(f,dict) and str(f.get('number',''))==args['number'] and str(f.get('type',''))==args['type'] for f in fees) and not any(isinstance(b,dict) and any(isinstance(r,dict) and str(r.get('number',''))==args['number'] and str(r.get('type',''))==args['type'] for r in b.get('rows',[])) for b in bills): raise ValueError('difference_evidence_missing')
        matches=[d for d in diffs if isinstance(d,dict) and str(d.get('id',''))==args['difference_id']]
        if len(matches)>1: raise ValueError('duplicate_difference_id')
        before=matches[0] if matches else None
        if before and (before.get('decision') or str(before.get('number',''))!=args['number'] or str(before.get('type',''))!=args['type']): raise ValueError('invalid_difference')
        evidence=business_integrity.evidence_fingerprint(host,state,args['number'])
        envelope={'kind':'difference','before':before,'args':args,'evidence_fingerprint':evidence}
        cid=secrets.token_urlsafe(32); now=host.now_iso()
        con.execute('INSERT INTO finance_candidates(id,approver_id,fee_id,expected_rev,before_json,target_status,created_at) VALUES(?,?,?,?,?,?,?)',(cid,user['id'],args['difference_id'],row['rev'],_json(envelope),'__difference__',now))
        result={'candidate_id':cid}; host._tool_idempotency_write(con,user['id'],key,digest,200,result,now); con.commit(); return result
