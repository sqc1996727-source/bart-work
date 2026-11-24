"""Industry-neutral work queue: real assigned tasks, not simulated industry events."""
from datetime import datetime, timezone


def list_tasks(host, user, args):
    if not isinstance(args, dict) or set(args) - {"status", "query", "limit", "offset"}:
        raise ValueError("invalid_task_query")
    status = args.get("status", "active")
    if status not in {"active", "all", "open", "in_progress", "done", "cancelled"}:
        raise ValueError("invalid_task_status")
    limit, offset = int(args.get("limit", 30)), int(args.get("offset", 0))
    if not 1 <= limit <= 100 or offset < 0:
        raise ValueError("invalid_pagination")
    where, params = ["u.tenant_id=?"], [user["tenant_id"]]
    if user["role"] == "employee":
        where.append("t.assignee_id=?")
        params.append(user["id"])
    if status == "active":
        where.append("t.status IN ('open','in_progress')")
    elif status != "all":
        where.append("t.status=?")
        params.append(status)
    query = str(args.get("query", "")).strip()[:200]
    if query:
        where.append("(instr(t.title,?)>0 OR instr(coalesce(t.description,''),?)>0 OR t.id=?)")
        params.extend([query, query, query])
    clause = " FROM tasks t JOIN users u ON u.id=t.created_by WHERE " + " AND ".join(where)
    with host.db() as con:
        total = con.execute("SELECT count(*)" + clause, params).fetchone()[0]
        rows = con.execute("SELECT t.id,t.title,t.description,t.status,t.assignee_id,t.due_at,t.created_at"
                           + clause + " ORDER BY CASE WHEN t.due_at IS NULL THEN 1 ELSE 0 END,"
                           " t.due_at,t.created_at DESC LIMIT ? OFFSET ?", (*params, limit, offset)).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        if user["role"] != "owner_finance":
            item = host._redact_tree(item)
        items.append(item)
    return {"items": items, "total": total,
            "next_offset": offset + len(items) if offset + len(items) < total else None}


def today(host, user):
    work = list_tasks(host, user, {"status": "active", "limit": 20})
    return {"work_summary": work, "routine": None, "pending_state": [],
            "note": "当前仅查询已接入的企业工作任务；未接入的行业业务不能据此判断是否异常。"}
