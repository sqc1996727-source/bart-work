"""On-demand recall of completed file work, scoped to current principal and permissions."""
import json

from ai.agent_policy import query_terms, relevance
from ai.service import TOOL_MEMORY_SCOPE
from . import files, workplace_memory

# Business state must be read again, not recovered from an obsolete tool snapshot.
# File-backed results can be returned after verifying all of their current grants.
RECALLABLE = frozenset({"read_files", "analyze_table", "create_document"})


def recall(host, user, args):
    query = str(args.get("query", "")).strip()[:200]
    event_id = str(args.get("event_id", "")).strip()[:100]
    if not query and not event_id:
        raise ValueError("query_or_event_id_required")
    scope = TOOL_MEMORY_SCOPE.get()
    store = host._v3_ai_service().store
    prefix = host._V3_CONVERSATION_PREFIX
    params = [user["tenant_id"], user["id"], scope, len(prefix), prefix]
    sql = """SELECT e.*,c.memory_scope FROM tool_events e JOIN conversations c
             ON e.conversation_id=c.id
             WHERE c.tenant_id=? AND c.principal_id=? AND c.memory_scope=?
               AND substr(c.id,1,?)=? AND e.state='succeeded' AND e.result_json IS NOT NULL"""
    if event_id:
        sql += " AND e.id=?"
        params.append(event_id)
    sql += " ORDER BY e.created_at DESC LIMIT 500"
    with store._connect() as con:
        rows = con.execute(sql, params).fetchall()
    candidates = []
    terms = query_terms(query)
    for row in rows:
        if row["name"] not in RECALLABLE or not host.integrity.conversation_allowed(host, user, row["conversation_id"]):
            continue
        try:
            result = json.loads(row["result_json"])
            arguments = json.loads(row["arguments_json"])
            if not isinstance(result, dict) or result.get("error"):
                continue
            ids = list(arguments.get("file_ids", []))
            if row["name"] == "create_document":
                doc = result.get("document", {})
                ids += [doc["id"]] + list(doc.get("source_ids", []))
            if not ids:
                continue
            for fid in set(ids):
                files.authorized(host, user, fid)
            # Old unstructured results cannot bypass present financial rules.
            serialized = json.dumps(result, ensure_ascii=False)
            if user["role"] == "employee" and workplace_memory.sensitive(host, serialized):
                continue
        except (LookupError, ValueError, TypeError, KeyError):
            continue
        if event_id:
            offset = max(0, int(args.get("offset", 0)))
            return {"event_id": row["id"], "tool": row["name"], "historical": True,
                    "result_json": serialized[offset:offset+12000],
                    "next_offset": offset+12000 if len(serialized)>offset+12000 else None,
                    "note": "历史文件工作结果；当前业务事实需要重新读取。"}
        score = relevance(terms, row["name"]+" "+row["arguments_json"]+" "+serialized)
        if score:
            candidates.append((score, {"event_id": row["id"], "tool": row["name"],
                "conversation_id": row["conversation_id"], "created_at": row["created_at"],
                "summary": serialized[:240]}))
    candidates.sort(key=lambda pair: pair[0], reverse=True)
    return {"items": [item for _, item in candidates[:8]], "historical": True,
            "coverage": "authorized_file_work_only",
            "note": "选择 event_id 按需读取历史原始结果；运单、财务与回单状态必须重新查询。"}
