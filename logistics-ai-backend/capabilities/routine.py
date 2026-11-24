"""主动干活引擎（对标 Grok Bot 的「自己跑、干完来报」）。

只做「读 + 产出候选」，绝不写账。每个 Routine 扫一遍当前业务状态，
把「今天该盯的事」整理成结构化候选（带证据指针），交给模型判断、汇报。

Routine 是一个注册表：新增一类「该盯的事」就加一个函数，不改现有代码。

bot_state（任务态本子）也在这里：Routine 跑完后由后端确定性代码自动记录
「扫到哪、催到哪」，是可靠的机器进度，不靠模型自觉写。
"""
import json
import secrets
from collections import Counter
from decimal import Decimal, InvalidOperation
from datetime import datetime
import receipt_state
import business_integrity

# 回单四个关键字段的中文语义（与 _v3_recent_changes 的 receipt_names 对齐）
RECEIPT_FIELDS = ("photo", "paper", "delivered", "supplement")
RECEIPT_NAMES = {"photo": "签收照片", "paper": "纸质回单", "delivered": "厂家原件交付", "supplement": "逾期补交"}


def _decode(state, key):
    try:
        return json.loads(state.get(key, "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []


def _safe_number(v):
    try:
        return round(float(v), 2)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# A. 对账差异 Routine
# --------------------------------------------------------------------------
def _diff_routine(state, fees, diffs, bills=None, host=None):
    """挑出「钱对不上、或该归属没落账」的票。

    覆盖三类，全部是候选、不自动认定对错：
      1. 运单登记了金额（amount），但费用表里没有这一票的对应费用记录 → 该收/该付的钱没落账。
      2. 差异登记表里已记录、但还没「关闭/确认」的差异 → 有人发现过问题，还没收口。
      3. 费用记录缺合作方/承担方/币种 → 结算口径不全，不能自动认定一致。
    """
    waybills = _decode(state, "julong_demo_waybills_v2")
    items = []

    # 守卫：费用表整个为空 = 费用数据还没接入，不是「票票都没落账」。
    # 此时不把 8 票全报成差异，只在结果里标明「费用数据未接入，这条暂不判」。
    guard = "fees_not_connected" if fees == [] else None
    fees_valid = fees is not None
    fees = fees or []
    diffs = diffs or []
    fee_numbers = {str(f.get("number", "")) for f in fees if isinstance(f, dict) and f.get("number") not in (None, "")}

    # 1. 有登记金额但费用表无记录的票
    for t in waybills:
        if not isinstance(t, dict):
            continue
        number = str(t.get("number", "") or "").strip()
        amount = _safe_number(t.get("amount"))
        if fees and number and amount is not None and number not in fee_numbers:
            items.append({
                "routine": "对账差异",
                "kind": "费用未落账",
                "waybill_number": number,
                "evidence": f"运单 {number} 的对应费用记录未找到。",
            })

    # 2. 差异登记表里未收口的差异
    for d in diffs:
        if not isinstance(d, dict):
            continue
        # 已「关闭/确认」的差异不算待盯；字段名兼容常见的 closed/confirmed/resolved
        closed = any(d.get(k) for k in ("closed", "confirmed", "resolved", "done"))
        number = str(d.get("number", "") or "").strip()
        if not closed and d.get("decision") and d.get("confirmed_by_id") and d.get("evidence_fingerprint") and host:
            try:
                closed = d["evidence_fingerprint"] == business_integrity.evidence_fingerprint(host, state, number)
            except (KeyError, TypeError, ValueError, AttributeError):
                closed = False
        if closed:
            continue
        items.append({
            "routine": "对账差异",
            "kind": "差异未收口",
            "waybill_number": number,
            "evidence": f"运单 {number} 有一条已记录但未确认关闭的差异。",
        })

    # 3. 费用缺合作方/承担方/币种
    for f in fees:
        if not isinstance(f, dict):
            continue
        fields = (("合作方", ("partner", "party", "合作方")),
                  ("承担方", ("payer", "bearer", "承担方")),
                  ("币种", ("currency",)))
        missing = [label for label, keys in fields if not any(str(f.get(k) or "").strip() for k in keys)]
        if missing:
            number = str(f.get("number", "") or "").strip()
            items.append({"routine":"对账差异", "kind":"费用缺归属", "waybill_number":number,
                          "evidence":f"运单 {number} 的费用缺少{'、'.join(missing)}，需核实。"})

    # Compare only a single fee of an explicitly matching type, never unrelated charges.
    for t in waybills:
        if not isinstance(t, dict):
            continue
        number = str(t.get("number", "")).strip()
        for typ, key in (("运费", "amount"), ("进仓费", "feeWarehouse")):
            matched = [f for f in fees if isinstance(f, dict) and str(f.get("number", "")).strip() == number
                       and f.get("type") == typ and f.get("status") != "作废"]
            if len(matched) != 1:
                continue
            try:
                left, right = Decimal(str(t.get(key))), Decimal(str(matched[0].get("amount")))
                if left.is_finite() and right.is_finite() and left != right:
                    items.append({"routine":"对账差异", "kind":"金额不一致", "waybill_number":number,
                                  "evidence":f"运单 {number} 的{typ}金额与对应费用记录不一致，需核实。"})
            except (InvalidOperation, ValueError):
                pass
    # Latest statement per partner; only compare a unique matching business key.
    if fees_valid and bills is not None:
        groups = {}
        for bill in bills:
            if isinstance(bill, dict):
                groups.setdefault(str(bill.get("partner") or "").strip(), []).append(bill)
        for partner, statements in groups.items():
            if len(statements) == 1:
                bill = statements[0]
            else:
                dated = []
                for statement in statements:
                    try:
                        stamp = datetime.fromisoformat(str(statement.get("at") or "").replace("Z", "+00:00"))
                        # Mixed timezone conventions cannot safely establish ordering.
                        dated.append((stamp, statement))
                    except ValueError:
                        dated = []
                        break
                try:
                    ordered = sorted(dated, key=lambda x:x[0]) if len(dated) == len(statements) else []
                except TypeError:
                    ordered = []
                if not ordered or ordered[-1][0] == ordered[-2][0]:
                    numbers = {str(r.get("number") or "").strip() for statement in statements
                               for r in (statement.get("rows") if isinstance(statement.get("rows"),list) else [])
                               if isinstance(r,dict) and r.get("number")}
                    for number in numbers:
                        items.append({"routine":"对账差异", "kind":"对账对应关系待核实", "waybill_number":number,
                                      "evidence":f"运单 {number} 的对方账单版本顺序不明确，需核实。"})
                    continue
                bill = ordered[-1][1]
            rows = bill.get("rows")
            if not isinstance(rows, list):
                continue
            grouped = {}
            for row in rows:
                if isinstance(row, dict):
                    number = str(row.get("number") or "").strip()
                    typ = str(row.get("type") or "").strip()
                    if number:
                        grouped.setdefault((number, typ), []).append(row)
            for (number, typ), entries in grouped.items():
                matches = [f for f in fees if isinstance(f, dict) and f.get("status") != "作废"
                           and str(f.get("number") or "").strip() == number
                           and str(f.get("type") or "").strip() == typ
                           and (not partner or not str(f.get("partner") or f.get("party") or "").strip()
                                or str(f.get("partner") or f.get("party") or "").strip() == partner)]
                kind = None
                if not typ or len(entries) != 1 or len(matches) > 1 or (partner and matches and not str(matches[0].get("partner") or matches[0].get("party") or "").strip()):
                    kind = "对账对应关系待核实"
                elif not matches:
                    kind = "对方账单费用未匹配"
                else:
                    try:
                        left, right = Decimal(str(entries[0].get("amount"))), Decimal(str(matches[0].get("amount")))
                        if not left.is_finite() or not right.is_finite():
                            kind = "对账对应关系待核实"
                        elif left != right:
                            kind = "对方账单金额不一致"
                    except (InvalidOperation, ValueError):
                        kind = "对账对应关系待核实"
                if kind:
                    items.append({"routine":"对账差异", "kind":kind, "waybill_number":number,
                                  "evidence":f"运单 {number} 的对方账单与费用记录需核实。"})
    return items, guard


# --------------------------------------------------------------------------
# B. 回单催办 Routine
# --------------------------------------------------------------------------
def _receipt_routine(state, receipts):
    """挑出「该走回单流程但关键件还没收」的票。

    只看 paper（纸质回单）和 delivered（厂家原件交付）两个关键件；
    这两个对账/结算靠得住，缺了就值得催。photo/supplement 不作为催办项。
    判定规则：该票有回单登记、但对应字段「未登记」或「登记又撤销（off）」→ 待催。
    """
    waybills = _decode(state, "julong_demo_waybills_v2")
    by_number = {str(t.get("number", "")): t for t in waybills if isinstance(t, dict) and t.get("number") not in (None, "")}
    items = []
    by_receipt = {str(r.get("number", "")).strip(): r for r in receipts
                  if isinstance(r, dict) and r.get("number") not in (None, "")}
    for number in by_number:
        r = by_receipt.get(number, {})
        for field in ("paper", "delivered"):
            if not receipt_state.active(r.get(field)):
                items.append({"routine":"回单催办", "kind":"回单未收", "waybill_number":number,
                              "field":field, "evidence":f"运单 {number} 的{RECEIPT_NAMES[field]}还没有落实。"})
    return items


# --------------------------------------------------------------------------
# 引擎入口
# --------------------------------------------------------------------------
def run(host, user, *, limit=20, con=None):
    if con is not None:
        return _run_in_transaction(host, user, limit=limit, con=con)
    with host.db() as owned:
        owned.execute("BEGIN IMMEDIATE")
        result = _run_in_transaction(host, user, limit=limit, con=owned)
        owned.commit()
        return result


def _run_in_transaction(host, user, *, limit, con):
    """扫一遍当前业务状态，产出「今天该盯的事」候选。

    纯读，不改任何账。返回结构化的候选列表，每个候选带证据指针，
    模型据此汇报，不自行下结论。
    """
    state_row = con.execute("SELECT payload,rev FROM work_state WHERE id=1").fetchone()
    if not state_row:
        return {"routine_candidates": [], "total": 0, "by_routine": {}, "state_rev": 0,
                "coverage": "无业务状态", "source_status": {},
                "note": "业务状态还不可用，不能视为无待办。"}
    malformed_payload = False
    try:
        state = json.loads(state_row["payload"])
        if not isinstance(state, dict):
            raise ValueError("invalid state document")
    except (TypeError, ValueError):
        malformed_payload = True
        state = {}
    rev = state_row["rev"]
    # 模块没启用时，扫账不能把该模块的业务域读出来，否则模块开关形同虚设。
    enabled = getattr(host, "_module_enabled", lambda module: True)
    finance_on = bool(enabled("finance"))
    receipt_on = bool(enabled("receipt"))
    relevant = ["julong_demo_waybills_v2"] + (["julong_demo_receipts_v1"] if receipt_on else [])
    if user.get("role") != "employee" and finance_on:
        relevant += ["julong_demo_fees_v1", "julong_demo_diffs_v1", "julong_demo_bills_v1"]
    invalid = []
    source_status = {}
    for key in relevant:
        if key not in state:
            source_status[key] = "invalid" if malformed_payload else "not_connected"
            invalid.append(key)
            continue
        try:
            records = json.loads(state[key])
            if not isinstance(records, list) or any(not isinstance(row, dict) for row in records):
                raise ValueError("invalid domain records")
        except (TypeError, ValueError):
            source_status[key] = "invalid"
            invalid.append(key)
            continue
        source_status[key] = "available" if records else "no_records"
    if user.get("role") == "employee":
        # Apply access rules before any routine sees a record, including receipts.
        allowed = [t for t in _decode(state, "julong_demo_waybills_v2")
                   if isinstance(t, dict) and host._case_allowed(t, user, con=con)]
        numbers = {str(t.get("number", "")).strip() for t in allowed if t.get("number") not in (None, "")}
        state["julong_demo_waybills_v2"] = json.dumps(allowed, ensure_ascii=False)
        receipts = [r for r in _decode(state, "julong_demo_receipts_v1")
                    if isinstance(r, dict) and str(r.get("number", "")).strip() in numbers]
        # Employees never inspect financial records or produce financial counts.
        fees = diffs = []
    else:
        receipts = _decode(state, "julong_demo_receipts_v1")

    if user.get("role") != "employee" and finance_on:
        fees = _decode(state, "julong_demo_fees_v1")
        diffs = _decode(state, "julong_demo_diffs_v1")

    if user.get("role") != "employee" and finance_on:
        safe_fees = None if "julong_demo_fees_v1" in invalid else fees
        safe_diffs = None if "julong_demo_diffs_v1" in invalid else diffs
        safe_bills = None if "julong_demo_bills_v1" in invalid else _decode(state, "julong_demo_bills_v1")
        safe_state = state if "julong_demo_waybills_v2" not in invalid else {**state, "julong_demo_waybills_v2":"[]"}
        diff_items, diff_guard = _diff_routine(safe_state, safe_fees, safe_diffs, safe_bills, host=host)
    else:
        diff_items, diff_guard = [], None
    receipt_items = (_receipt_routine(state, receipts)
                     if receipt_on and not any(k in invalid for k in ("julong_demo_waybills_v2", "julong_demo_receipts_v1"))
                     else [])
    candidates = diff_items + receipt_items

    # 按类别计数，方便模型一句话说清「今天有几类事、各几件」
    by_routine = Counter(c["routine"] for c in candidates)

    parts = ["waybills"]
    if receipt_on:
        parts.append("receipts")
    if user.get("role") != "employee" and finance_on:
        parts += ["fees", "diffs", "bills"]
    coverage = "+".join(parts)
    if not finance_on and user.get("role") != "employee":
        coverage += ";finance_module_disabled"
    if not receipt_on:
        coverage += ";receipt_module_disabled"
    note = "仅候选，未经确认不视为业务结论；涉钱差异需有人确认并留痕。"
    if user.get("role") == "employee":
        note = "仅显示本人可访问运单的回单催办候选。"
    elif diff_guard == "fees_not_connected":
        note = "费用表暂无记录，暂不逐票判定费用未落账；已登记的差异和回单仍可检查。"

    if invalid:
        coverage += "; incomplete:" + ",".join(invalid)
        note += " 部分数据缺失或格式错误，相关扫描未执行，不能视为无待办。"

    # 跑完自动记任务态（机器记录，不靠模型自觉）：扫到多少、催到哪。
    save_bot_state(
            host, user, con=con,
            subject="每日主动扫账",
            status="waiting" if candidates or invalid else "done",
            progress=f"扫出 {len(candidates)} 条待盯事项（{ '、'.join(f'{k} {v}' for k,v in by_routine.items()) or '无' }）",
            next_step=("回单需催办落实" if user.get("role") == "employee" else "涉钱差异需有人确认；回单需催办落实") if candidates else ("数据不完整，需检查后重扫" if invalid else "当日无待盯，明日再扫"),
            evidence=json.dumps({"state_rev": rev, "by_routine": dict(by_routine)}, ensure_ascii=False, separators=(",", ":")),
        )

    con.execute("""CREATE TABLE IF NOT EXISTS routine_scans (
        id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, owner_id TEXT NOT NULL,
        role TEXT NOT NULL, state_rev INTEGER NOT NULL, candidates TEXT NOT NULL,
        coverage TEXT NOT NULL, note TEXT NOT NULL, created_at TEXT NOT NULL)""")
    scan_id = secrets.token_urlsafe(18)
    con.execute("INSERT INTO routine_scans VALUES(?,?,?,?,?,?,?,?,?)",
                (scan_id,user["tenant_id"],user["id"],user.get("role", ""),rev,
                 json.dumps(candidates,ensure_ascii=False,separators=(",",":")),coverage,note,host.now_iso()))
    return {
        "scan_id": scan_id,
        "routine_candidates": candidates[:limit],
        "total": len(candidates),
        "by_routine": dict(by_routine),
        "state_rev": rev,
        "coverage": coverage,
        "source_status": source_status,
        "note": note,
    }


ROUTINE_NAMES = {
    "对账差异": "钱对不上、或该归属的钱没落账",
    "回单催办": "该收的关键回单还没收",
}


# --------------------------------------------------------------------------
# bot_state：Bot 自己的任务态本子（连续性）
# --------------------------------------------------------------------------
def _ensure_bot_state(con):
    # Legacy role-shared records cannot be attributed safely and are retained untouched.
    con.execute("""CREATE TABLE IF NOT EXISTS bot_state_v2 (
        id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, owner_id TEXT NOT NULL,
        bot_role TEXT NOT NULL, subject TEXT NOT NULL, status TEXT NOT NULL,
        progress TEXT NOT NULL, next_step TEXT NOT NULL, evidence TEXT NOT NULL,
        updated_at TEXT NOT NULL, UNIQUE(tenant_id, owner_id, bot_role, subject))""")


def save_bot_state(host, user, *, subject, status, progress="", next_step="", evidence="", bot_role="", con=None):
    if con is None:
        with host.db() as owned:
            owned.execute("BEGIN IMMEDIATE")
            result = save_bot_state(host, user, subject=subject, status=status, progress=progress,
                                    next_step=next_step, evidence=evidence, bot_role=bot_role, con=owned)
            owned.commit()
            return result
    _ensure_bot_state(con)
    role = bot_role or user.get("role", "")
    state_id = secrets.token_urlsafe(12)
    con.execute("""INSERT INTO bot_state_v2
        (id,tenant_id,owner_id,bot_role,subject,status,progress,next_step,evidence,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(tenant_id,owner_id,bot_role,subject) DO UPDATE SET
        status=excluded.status,progress=excluded.progress,next_step=excluded.next_step,
        evidence=excluded.evidence,updated_at=excluded.updated_at""",
        (state_id,user["tenant_id"],user["id"],role,subject,status,progress,next_step,evidence,host.now_iso()))
    row = con.execute("SELECT id FROM bot_state_v2 WHERE tenant_id=? AND owner_id=? AND bot_role=? AND subject=?",
                      (user["tenant_id"],user["id"],role,subject)).fetchone()
    return {"id": row["id"], "subject": subject, "status": status}


def get_bot_state(host, user, *, bot_role="", include_done=False):
    role = bot_role or user.get("role", "")
    with host.db() as con:
        if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='bot_state_v2'").fetchone():
            return []
        where = "tenant_id=? AND owner_id=? AND bot_role=?"
        params = [user["tenant_id"], user["id"], role]
        if not include_done:
            where += " AND status!='done' AND status!='cancelled'"
        rows = con.execute("SELECT subject,status,progress,next_step,evidence,updated_at FROM bot_state_v2 "
                           f"WHERE {where} ORDER BY updated_at DESC LIMIT 20", params).fetchall()
    result = [dict(r) for r in rows if r["subject"] != "每日主动扫账"]
    scan = latest_scan(host, user)
    if scan and (include_done or scan["total"] or "incomplete" in scan["coverage"]):
        result.append({"subject":"每日主动扫账", "status":"waiting" if scan["total"] or "incomplete" in scan["coverage"] else "done",
                       "progress":f"历史扫描：扫出 {scan['total']} 条当前可访问候选",
                       "scan_id":scan["scan_id"], "state_rev":scan["state_rev"],
                       "updated_at":scan["created_at"]})
    return result


def get_scan(host, user, scan_id):
    """Return one historical scan, scoped again against present-day access."""
    with host.db() as con:
        con.execute("BEGIN")
        if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='routine_scans'").fetchone():
            return None
        row = con.execute("SELECT * FROM routine_scans WHERE id=? AND tenant_id=? AND owner_id=?",
                          (scan_id,user["tenant_id"],user["id"])).fetchone()
        if not row:
            return None
        state_row = con.execute("SELECT payload,rev FROM work_state WHERE id=1").fetchone()
        try:
            current = json.loads(state_row["payload"]) if state_row else None
            if not isinstance(current, dict):
                return None
            bills = json.loads(current["julong_demo_waybills_v2"])
            if not isinstance(bills, list):
                return None
            candidates = json.loads(row["candidates"])
            if not isinstance(candidates, list):
                return None
        except (ValueError, TypeError, KeyError):
            return None
        permitted = {str(t.get("number", "")).strip() for t in bills if isinstance(t, dict)
                     and t.get("number") not in (None, "") and host._case_allowed(t,user,con=con)}
        visible = []
        for c in candidates:
            if not isinstance(c, dict) or str(c.get("waybill_number", "")).strip() not in permitted:
                continue
            if user.get("role") == "employee" and c.get("routine") != "回单催办":
                continue
            # Persisted records are generated by this module, but never return arbitrary fields.
            visible.append({k:c[k] for k in ("routine","kind","waybill_number","field","evidence") if k in c})
        counts = Counter(c.get("routine", "") for c in visible)
        return {"scan_id":row["id"],"routine_candidates":visible,"total":len(visible),
                "by_routine":dict(counts),"state_rev":row["state_rev"],"current_state_rev":state_row["rev"],
                "historical":True,"created_at":row["created_at"],
                "coverage": "historical snapshot; access filtered" + ("; incomplete" if "incomplete" in row["coverage"] else ""), "note":"历史扫描候选，当前业务状态可能已变化。" + (" 扫描数据不完整，不能视为无待办。" if "incomplete" in row["coverage"] else "")}


def latest_scan(host, user):
    with host.db() as con:
        if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='routine_scans'").fetchone():
            return None
        row=con.execute("SELECT id FROM routine_scans WHERE tenant_id=? AND owner_id=? ORDER BY rowid DESC LIMIT 1",
                        (user["tenant_id"],user["id"])).fetchone()
    return get_scan(host,user,row["id"]) if row else None
