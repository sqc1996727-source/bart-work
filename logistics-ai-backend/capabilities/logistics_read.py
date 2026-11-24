"""Logistics read-side projections and deterministic business summaries.

Host access is explicit; no duplicate database configuration or cached state.
"""
from collections import Counter
from datetime import datetime, timedelta
import hashlib
import json
import re

import module_scope
from module_scope import LOGISTICS_MODULE_TOOLS
import receipt_state
from . import team as agent_team

def _v3_operational_summary(host, user,*,record_audit=True):
    db = host.db
    _business_state = host._business_state
    STATE_KEYS = host.STATE_KEYS
    _case_allowed = host._case_allowed
    V3Context = host.V3Context
    audit = host.audit
    _enabled_modules = host._enabled_modules
    _module_enabled = host._module_enabled
    with db() as con:
        state_row=con.execute("SELECT rev,payload FROM work_state WHERE id=1").fetchone()
        if not state_row: return {"error":"state_unavailable"}
        state=_business_state(state_row["payload"])
        try: waybills=json.loads(state[STATE_KEYS[0]])
        except Exception: waybills=[]
        try: fees=json.loads(state[STATE_KEYS[4]])
        except Exception: fees=[]
        try: receipts=json.loads(state[STATE_KEYS[5]])
        except Exception: receipts=[]
        if user["role"]=="employee":
            waybills=[x for x in waybills if isinstance(x,dict) and _case_allowed(x,user)]
            numbers={str(x.get("number")) for x in waybills}
            tasks=[dict(x) for x in con.execute("SELECT status,waybill_number FROM tasks WHERE assignee_id=?",(user["id"],))]
        else:
            numbers={str(x.get("number")) for x in waybills if isinstance(x,dict) and x.get("number") is not None}
            tasks=[dict(x) for x in con.execute("SELECT t.status,t.waybill_number FROM tasks t JOIN users u ON u.id=t.created_by WHERE u.tenant_id=?",(user["tenant_id"],))]
        visible_fees=[x for x in fees if isinstance(x,dict) and str(x.get("number","")) in numbers and not V3Context._protected_freight_type(x.get("type"))]
        visible_receipts=[x for x in receipts if isinstance(x,dict) and str(x.get("number","")) in numbers]
        rev=state_row["rev"]
        if record_audit: audit(con,user,"v3.operational_summary.read","state",str(rev),{"scope":"own" if user["role"]=="employee" else "enterprise","finance_fields":False})
    receipt_counts={"photo_received":0,"paper_returned":0,"delivered_to_factory":0,"supplemented":0}
    marker_names={"photo":"photo_received","paper":"paper_returned","delivered":"delivered_to_factory","supplement":"supplemented"}
    for receipt in visible_receipts:
        for source,target in marker_names.items():
            marker=receipt.get(source)
            if receipt_state.active(marker): receipt_counts[target]+=1
    fee_counts=Counter(str(item.get("status","未标记"))[:40] for item in visible_fees)
    task_counts=Counter(str(item.get("status","未标记"))[:40] for item in tasks)
    weight=0.0
    for ticket in waybills:
        try: weight+=float(ticket.get("weight") or 0)
        except (TypeError,ValueError): pass
    result={"scope":"own" if user["role"]=="employee" else "enterprise","state_rev":rev,
            "waybills":{"count":len(waybills),"weight_tonnes":round(weight,3)},
            "receipts":receipt_counts,"fees":{"records":len(visible_fees),"by_status":dict(fee_counts)},
            "tasks":{"total":len(tasks),"by_status":dict(task_counts)},
            "note":"此汇总只包含运营数量与状态，不包含厂家运费、费用金额或利润；数据来自当前work_state与tasks快照。"}
    result["disabled_modules"]=sorted(set(LOGISTICS_MODULE_TOOLS)-_enabled_modules())
    for module,key in (("receipt","receipts"),("finance","fees")):
        if not _module_enabled(module): result.pop(key,None)
    result["evidence"]=[{"evidence_id":f"v3:operational-summary:r{rev}","text":json.dumps(result,ensure_ascii=False,separators=(",",":")),"source":"operational_summary","metadata":{"scope":result["scope"],"state_rev":rev}}]
    return result


def _v3_recent_changes(host, user,arguments,*,record_audit=True):
    db = host.db
    STATE_KEYS = host.STATE_KEYS
    _business_state = host._business_state
    _case_allowed = host._case_allowed
    _module_enabled = host._module_enabled
    _module_state_keys = host._module_state_keys
    _v3_safe_case = host._v3_safe_case
    audit = host.audit
    if not isinstance(arguments,dict) or set(arguments)-{"days","limit"}: return {"error":"invalid_request"}
    days=arguments.get("days",7); limit=arguments.get("limit",20)
    if isinstance(days,bool) or not isinstance(days,int) or not 1<=days<=30 or isinstance(limit,bool) or not isinstance(limit,int) or not 1<=limit<=30:
        return {"error":"invalid_request"}
    cutoff=datetime.now().astimezone()-timedelta(days=days)
    domain_names={STATE_KEYS[0]:"运单信息",STATE_KEYS[1]:"收件信息",STATE_KEYS[2]:"发运安排",STATE_KEYS[3]:"月度业务记录",STATE_KEYS[4]:"费用状态",STATE_KEYS[5]:"回单状态",STATE_KEYS[6]:"账单核对",STATE_KEYS[7]:"差异登记",STATE_KEYS[8]:"交付状态",STATE_KEYS[9]:"AI记录"}
    receipt_names={"photo":"签收照片","paper":"纸质回单","delivered":"厂家原件交付","supplement":"逾期补交"}
    with db() as con:
        state_row=con.execute("SELECT payload,rev FROM work_state WHERE id=1").fetchone()
        state=_business_state(state_row["payload"] if state_row else "{}")
        try: tickets=json.loads(state[STATE_KEYS[0]])
        except Exception: tickets=[]
        ticket_owner={str(row.get("number")):user["id"] for row in tickets if isinstance(row,dict) and _case_allowed(row,user)}
        rows=con.execute("SELECT a.seq,a.at,a.user_id,a.user_name,a.action,a.entity_type,a.entity_id,a.details FROM audit a JOIN users u ON u.id=a.user_id WHERE u.tenant_id=? ORDER BY a.seq DESC LIMIT 1000",(user["tenant_id"],)).fetchall()
        tasks={row["id"]:dict(row) for row in con.execute("SELECT t.id,t.assignee_id,t.waybill_number,t.status FROM tasks t JOIN users u ON u.id=t.created_by WHERE u.tenant_id=?",(user["tenant_id"],))}
        attachment_exists=con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='attachments'").fetchone() is not None
        attachments={}
        if attachment_exists:
            attachments={row["id"]:row["waybill_number"] for row in con.execute("SELECT id,waybill_number FROM attachments WHERE tenant_id=?",(user["tenant_id"],))}
    result_items=[]; included_numbers=[]
    for row in rows:
        try:
            at=datetime.fromisoformat(str(row["at"]).replace("Z","+00:00"))
            if at.tzinfo is None: at=at.astimezone()
        except (TypeError,ValueError): continue
        if at<cutoff: continue
        action=row["action"]; entity=row["entity_type"]; ident=str(row["entity_id"] or "")
        try: details=json.loads(row["details"] or "{}")
        except (TypeError,json.JSONDecodeError): details={}
        number=None; summary=None; category=None
        if action=="ai.tool.receipt_status.update" and entity=="waybill":
            if not _module_enabled("receipt"): continue
            number=ident
            if user["role"]=="employee" and ticket_owner.get(number)!=user["id"]: continue
            field=receipt_names.get(str(details.get("field")),"回单")
            verb="已登记" if details.get("active") is True else "已撤销"
            category="回单"; summary=f"{number} {field}状态{verb}。"
        elif action in {"task.create","task.update","task.business_complete","task.business_reopen"} and entity=="task":
            task=tasks.get(ident)
            if task is None: continue
            if user["role"]=="employee" and task.get("assignee_id")!=user["id"]: continue
            number=task.get("waybill_number")
            labels={"task.create":"创建待办","task.update":"更新待办","task.business_complete":"业务条件满足，待办自动完成","task.business_reopen":"业务条件变化，待办重新打开"}
            category="待办"; summary=labels[action]+(f"（关联运单 {number}）" if number else "。")
            status_value=details.get("status")
            if action=="task.create": status_value="open"
            elif action=="task.business_complete": status_value="done"
            elif action=="task.business_reopen": status_value="in_progress"
            if status_value: summary+=f" 状态：{status_value}。"
        elif action=="waybill.import" and entity=="waybill_batch":
            if user["role"]=="employee": continue
            count=details.get("row_count",0)
            category="运单导入"; summary=f"批量导入运单记录 {count} 票。"
        elif action=="state.update" and entity=="state":
            if user["role"]=="employee" and row["user_id"]!=user["id"]: continue
            keys=details.get("keys",[])
            labels=[domain_names[key] for key in keys if key in domain_names and key not in _module_state_keys()] if isinstance(keys,list) else []
            if not labels: continue
            category="工作台更新"; summary="业务账本更新"+("："+"、".join(labels) if labels else "。")
        elif action in {"attachment.upload","attachment.delete"} and entity=="attachment":
            number=details.get("waybill_number") or attachments.get(ident)
            if not number or (user["role"]=="employee" and ticket_owner.get(str(number))!=user["id"]): continue
            category="资料"; summary=f"运单 {number} 相关图片"+("已上传。" if action=="attachment.upload" else "已删除。")
        else: continue
        if number: included_numbers.append(str(number))
        result_items.append({"at":at.astimezone().isoformat(timespec="seconds"),"actor":row["user_name"],"category":category,"waybill_number":number,"summary":summary,"source":"audit","audit_seq":row["seq"],"state_rev":details.get("rev") if action=="state.update" else None,"detail_limit":"仅记录动作/变更域，不一定保存字段前后值"})
        if len(result_items)>=limit: break
    limitation="审计表记录的是动作、时间和有限变更域，不是完整字段差异；没有前后值证据时不能推断具体数据变化。"
    evidence=[{"evidence_id":f"v3:recent-changes:{days}d:{(result_items[0]['audit_seq'] if result_items else 0)}","text":json.dumps({"days":days,"count":len(result_items),"items":result_items,"limitation":limitation},ensure_ascii=False,separators=(",",":")),"source":"business_change_log","metadata":{"scope":"own" if user["role"]=="employee" else "enterprise","days":days}}]
    seen=set()
    for number in included_numbers:
        if number in seen: continue
        seen.add(number)
        case=_v3_safe_case(user,number)
        if case:
            text=json.dumps({"title":case["title"],"groups":case["groups"]},ensure_ascii=False,separators=(",",":"))
            evidence.append({"evidence_id":case["evidence_id"],"text":text,"source":"waybill_case","metadata":{"waybill_number":number}})
        if len(evidence)>=21: break
    if record_audit:
        with db() as con:
            audit(con,user,"v3.recent_changes.read","audit","",{"days":days,"count":len(result_items),"scope":"own" if user["role"]=="employee" else "enterprise"})
    return {"days":days,"scope":"own" if user["role"]=="employee" else "enterprise","count":len(result_items),"items":result_items,"source":"audit","limitation":limitation,"evidence":evidence}


def _v3_safe_case(host, user, number):
    db = host.db
    _business_state = host._business_state
    STATE_KEYS = host.STATE_KEYS
    _case_allowed = host._case_allowed
    V3Context = host.V3Context
    number=str(number or "").strip()[:80]
    if not number: return None
    with db() as con:
        row=con.execute("SELECT payload,rev FROM work_state WHERE id=1").fetchone()
        if not row: return None
        state=_business_state(row["payload"])
        try: docs=json.loads(state[STATE_KEYS[0]])
        except Exception: docs=[]
        ticket=next((x for x in docs if isinstance(x,dict) and str(x.get("number",""))==number),None)
        if not ticket or (user["role"]=="employee" and not _case_allowed(ticket,user)): return None
        try: fees=json.loads(state[STATE_KEYS[4]])
        except Exception: fees=[]
        try: receipts=json.loads(state[STATE_KEYS[5]])
        except Exception: receipts=[]
        if user["role"]=="employee":
            tasks=[dict(x) for x in con.execute("SELECT status,due_at,completion_condition,waybill_number FROM tasks WHERE waybill_number=? AND assignee_id=? ORDER BY created_at DESC",(number,user["id"]))]
        else:
            tasks=[dict(x) for x in con.execute("SELECT status,due_at,completion_condition,waybill_number FROM tasks WHERE waybill_number=? ORDER BY created_at DESC",(number,))]
        rev=row["rev"]
    ticket=V3Context._pick(ticket,V3Context._WAYBILL_FIELDS)
    fee_rows=[]
    for item in fees:
        if not isinstance(item,dict) or str(item.get("number",""))!=number: continue
        fee=V3Context._pick(item,V3Context._FEE_FIELDS)
        if re.search(r"厂家.{0,8}(?:运费|运价|底价)|(?:运费|运价|底价).{0,8}厂家",str(fee.get("type",""))): continue
        fee_rows.append(fee)
    receipt=next((x for x in receipts if isinstance(x,dict) and str(x.get("number",""))==number),None)
    receipt=V3Context._receipt_view(receipt) or {}
    groups=[{"title":"运单信息","rows":[{"label":label,"value":str(ticket[key])} for key,label in (("number","运单号"),("date","日期"),("receiver","收货人"),("phone","联系电话"),("address","收货地址"),("destination","到站"),("cargo","货物"),("qty","件数"),("weight","重量（吨）"),("carrier","承运人")) if key in ticket]}]
    workspace=agent_team.workspace(host,dict(user),number)
    with db() as con:
        people={r['id']:r['name'] for r in con.execute('SELECT id,name FROM users WHERE tenant_id=?',(user['tenant_id'],))}
    groups.append({'title':'业务协作','rows':[{'label':'负责人','value':people.get(workspace['responsible_id'],'尚未指定')},{'label':'协作同事','value':'、'.join(people.get(uid,'已停用账号') for uid in sorted(set(workspace['support_ids']+workspace['task_assignee_ids'])-{workspace['responsible_id']})) or '暂无'}]})
    if fee_rows:
        rows=[]
        for index,item in enumerate(fee_rows):
            rows.extend([{"label":f"费用{index+1}类别","value":str(item.get("type","其他费用"))},
                         {"label":f"费用{index+1}状态","value":str(item.get("status","未标记"))}])
            if item.get("at"): rows.append({"label":f"费用{index+1}登记时间","value":str(item["at"])})
        groups.append({"title":"费用进度","rows":rows})
    if receipt:
        labels={"photo":"签收照片","paper":"纸质回单","delivered":"厂家原件交付","supplement":"逾期补交"}
        rows=[]
        for key,label in labels.items():
            marker=receipt.get(key)
            if not isinstance(marker,dict): continue
            value=marker.get("status","未登记")
            if marker.get("at"): value+=f" · {marker['at']}"
            rows.append({"label":label,"value":value})
        if rows: groups.append({"title":"回单状态","rows":rows})
    if tasks:
        groups.append({"title":"待办状态","rows":[{"label":f"任务{index+1} {key}","value":str(value)} for index,item in enumerate(tasks) for key,value in item.items() if key!="waybill_number"]})
    return {"title":f"运单 {number}","files":workspace["files"],"subtitle":"V3安全案卷 · 金额与备注不展示","groups":groups,"note":"信息来自当前业务账本；本案卷不展示运费金额和自由备注。","evidence_id":f"v3:waybill:{number}:r{rev}"}


def inspect_business(host, user, *, snapshot=None, tasks_snapshot=None):
    db = host.db
    _business_state = host._business_state
    STATE_KEYS = host.STATE_KEYS
    PRODUCT = host.PRODUCT
    _case_allowed = host._case_allowed
    _redact_tree = host._redact_tree
    now_iso = host.now_iso
    _module_enabled = host._module_enabled
    if snapshot is None or tasks_snapshot is None:
        with db() as con:
            state_row=con.execute("SELECT payload FROM work_state WHERE id=1").fetchone()
            if user["role"]=="employee":
                tasks=con.execute("SELECT * FROM tasks WHERE assignee_id=?",(user["id"],)).fetchall()
            else:
                tasks=con.execute("SELECT * FROM tasks").fetchall()
        raw=_business_state(state_row["payload"] if state_row else "{}")
    else:
        tasks=tasks_snapshot
        raw=_business_state(snapshot) if not isinstance(snapshot,dict) else {key:snapshot.get(key,"[]") for key in STATE_KEYS}
    raw=module_scope.project(PRODUCT,raw)
    values={}
    for i,key in enumerate(STATE_KEYS):
        try: values[key]=json.loads(raw[key])
        except Exception: values[key]=[]
    docs=values[STATE_KEYS[0]] if isinstance(values[STATE_KEYS[0]],list) else []
    receipts=values[STATE_KEYS[5]] if isinstance(values[STATE_KEYS[5]],list) else []
    fees=values[STATE_KEYS[4]] if isinstance(values[STATE_KEYS[4]],list) else []
    bills=values[STATE_KEYS[6]] if isinstance(values[STATE_KEYS[6]],list) else []
    diffs=values[STATE_KEYS[7]] if isinstance(values[STATE_KEYS[7]],list) else []
    receipt_by={str(x.get("number")):x for x in receipts if isinstance(x,dict)}
    own_numbers={str(x.get("number")) for x in docs if isinstance(x,dict) and _case_allowed(x,user)}
    visible_docs=docs if user["role"]!="employee" else [x for x in docs if isinstance(x,dict) and str(x.get("number")) in own_numbers]
    issues=[]
    def add(kind,number,title,evidence,severity="warning",occurred_at=""):
        if user["role"]=="employee" and number and str(number) not in own_numbers: return
        evidence=dict(evidence or {})
        if user["role"]!="owner_finance": evidence=_redact_tree(evidence)
        key=json.dumps([kind,str(number or ""),title,evidence],ensure_ascii=False,sort_keys=True)
        issues.append({"id":hashlib.sha256(key.encode()).hexdigest()[:16],"kind":kind,"severity":severity,
                       "waybill_number":str(number or ""),"title":title,"occurred_at":occurred_at or now_iso(),"evidence":evidence})
    today=datetime.now().date()
    number_counts=Counter(str(x.get("number","")) for x in docs if isinstance(x,dict) and x.get("number"))
    required=("date","receiver","phone","address","destination","cargo","qty")
    reported_duplicates=set()
    for doc in visible_docs:
        if not isinstance(doc,dict): continue
        number=str(doc.get("number",""))
        if not number:
            add("waybill_anomaly",number,"运单编号缺失",{"fields":["number"]},"critical",str(doc.get("created","")))
        elif number_counts[number]>1 and number not in reported_duplicates:
            reported_duplicates.add(number)
            add("waybill_anomaly",number,"运单编号重复",{"fields":["number"],"duplicate_count":number_counts[number]},"critical",str(doc.get("created","")))
        missing=[field for field in required if doc.get(field) in (None,"")]
        try:
            if doc.get("weight") in (None,"") or not float(doc.get("weight"))>=0: missing.append("weight")
        except (TypeError,ValueError): missing.append("weight")
        if user["role"]=="owner_finance" and _module_enabled("finance") and doc.get("amount") in (None,""): missing.append("amount")
        if missing: add("waybill_anomaly",number,"运单缺少必填字段",{"fields":sorted(set(missing))},"warning",str(doc.get("created","")))
        rec=receipt_by.get(number,{})
        delivered=rec.get("delivered") if isinstance(rec,dict) else None
        if _module_enabled("receipt") and not receipt_state.active(delivered):
            add("unreturned_receipt",number,"厂家回单原件尚未交付",{"photo":receipt_state.active(rec.get("photo")),"paper":receipt_state.active(rec.get("paper")),"delivered":False},"warning",str(doc.get("date","")))
    active_fees=[x for x in fees if isinstance(x,dict) and x.get("status")!="作废"]
    fee_groups={}
    for fee in active_fees:
        ident=(str(fee.get("number","")),str(fee.get("type","")),str(fee.get("amount","")),str(fee.get("voucher","")).strip())
        fee_groups.setdefault(ident,[]).append(fee)
    for ident,group in fee_groups.items():
        if len(group)>1:
            n,typ,amount,voucher=ident
            evidence={"type":typ,"voucher":voucher,"duplicate_count":len(group)}
            if user["role"]=="owner_finance": evidence["amount"]=amount
            add("duplicate_fee",n,"发现疑似重复费用",evidence,"critical",str(group[0].get("at","")))
    if bills:
        last=bills[-1] if isinstance(bills[-1],dict) else {}
        rows=last.get("rows") or []
        for row in rows:
            if not isinstance(row,dict): continue
            number=str(row.get("number","")); typ=str(row.get("type","")); amount=row.get("amount")
            exact=any(str(f.get("number",""))==number and str(f.get("type",""))==typ and str(f.get("amount",""))==str(amount) and f.get("status")!="作废" for f in active_fees)
            decided=any(isinstance(d,dict) and str(d.get("number",""))==number and str(d.get("type",""))==typ and d.get("decision") for d in diffs)
            if not exact and not decided:
                evidence={"type":typ,"source":"latest_partner_bill"}
                if user["role"]=="owner_finance": evidence["amount"]=amount
                add("open_difference",number,"对账差异尚无人工结论",evidence,"warning",str(last.get("at","")))
    for task in tasks:
        if task["status"] in {"done","cancelled"}: continue
        raw_due=task["due_at"] or ""
        try: due=datetime.fromisoformat(raw_due[:10]).date()
        except (ValueError,TypeError): continue
        if due<today:
            add("overdue_task",task["waybill_number"],"待办已超过截止日期",{"task_id":task["id"],"title":task["title"],"due_at":raw_due,"status":task["status"]},"warning",task["created_at"])
    issues.sort(key=lambda x:(x["severity"]!="critical",x["kind"],x["waybill_number"],x["id"]))
    counts=Counter(x["kind"] for x in issues)
    summary={"unreturned_receipts":counts["unreturned_receipt"],"duplicate_fees":counts["duplicate_fee"],"open_differences":counts["open_difference"],"waybill_anomalies":counts["waybill_anomaly"],"overdue_tasks":counts["overdue_task"],"total":len(issues)}
    return {"generated_at":now_iso(),"summary":summary,"issues":issues}


def monthly_analytics(host, user,month):
    db = host.db
    _business_state = host._business_state
    STATE_KEYS = host.STATE_KEYS
    _case_allowed = host._case_allowed
    now_iso = host.now_iso
    if not re.fullmatch(r"\d{4}-\d{2}",month): raise ValueError("month must be YYYY-MM")
    try: datetime.strptime(month,"%Y-%m")
    except ValueError as exc: raise ValueError("invalid month") from exc
    with db() as con:
        state_row=con.execute("SELECT rev,payload FROM work_state WHERE id=1").fetchone()
    state=_business_state(state_row["payload"])
    def rows(key):
        try: value=json.loads(state[key]); return value if isinstance(value,list) else []
        except Exception: return []
    docs=[x for x in rows(STATE_KEYS[0]) if isinstance(x,dict) and str(x.get("date",""))[:7]==month]
    if user["role"]=="employee": docs=[x for x in docs if _case_allowed(x,user)]
    numbers={str(x.get("number")) for x in docs}
    receipts={str(x.get("number")):x for x in rows(STATE_KEYS[5]) if isinstance(x,dict)}
    fee_rows=[x for x in rows(STATE_KEYS[4]) if isinstance(x,dict) and (str(x.get("date",""))[:7]==month or (not x.get("date") and str(x.get("number")) in numbers))]
    if user["role"]=="employee": fee_rows=[x for x in fee_rows if str(x.get("number")) in numbers]
    status_counts=Counter(str(x.get("status","待确认")) for x in fee_rows)
    def numeric(value):
        try:
            number=float(value or 0)
            return number if __import__("math").isfinite(number) else 0.0
        except (TypeError,ValueError): return 0.0
    def valid_numeric(value):
        try: return __import__("math").isfinite(float(value))
        except (TypeError,ValueError): return False
    receipt_summary={"photo_received":0,"paper_returned":0,"delivered_to_factory":0,"outstanding":0}
    for doc in docs:
        rec=receipts.get(str(doc.get("number")),{})
        def active(k): return bool(isinstance(rec.get(k),dict) and not rec[k].get("off"))
        receipt_summary["photo_received"]+=int(active("photo")); receipt_summary["paper_returned"]+=int(active("paper")); receipt_summary["delivered_to_factory"]+=int(active("delivered")); receipt_summary["outstanding"]+=int(not active("delivered"))
    destinations=Counter(str(x.get("destination","")) for x in docs)
    activity={"waybills":len(docs),"weight_tonnes":round(sum(numeric(x.get("weight")) for x in docs),3),
              "by_destination":[{"destination":k,"count":v} for k,v in sorted(destinations.items()) if k]}
    result={"month":month,"generated_at":now_iso(),"state_rev":state_row["rev"],"activity":activity,"receipts":receipt_summary,
            "fees":{"records":len(fee_rows),"confirmed":status_counts["已确认"],"pending":status_counts["待确认"],"disputed":status_counts["有差异"]}}
    if user["role"]=="owner_finance":
        bad_waybill_amounts=sum(not valid_numeric(x.get("amount")) for x in docs)
        confirmed_fees=[x for x in fee_rows if x.get("status")=="已确认"]
        bad_fee_amounts=sum(not valid_numeric(x.get("amount")) for x in confirmed_fees)
        result["finance"]={
            "manufacturer_freight_total":None if bad_waybill_amounts else round(sum(numeric(x.get("amount")) for x in docs),2),
            "confirmed_partner_fees_total":None if bad_fee_amounts else round(sum(numeric(x.get("amount")) for x in confirmed_fees),2),
            "incomplete_waybill_amounts":bad_waybill_amounts,"incomplete_confirmed_fee_amounts":bad_fee_amounts,
        }
    return result


