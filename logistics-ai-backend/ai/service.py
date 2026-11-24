"""Provider-backed chat service with isolated memory and injected business tools.

This module intentionally has no knowledge of the business database. Retrieval
and tool execution are supplied by the host application, which must enforce
scope, authorization, idempotency, and audit at that boundary.
"""

from __future__ import annotations

import hashlib
import contextvars
import json
import os
import re
import sqlite3
from sqlite_connection import ClosingConnection
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol, Sequence


OBSERVER=contextvars.ContextVar('ai_observer',default=None)
TOOL_MEMORY_SCOPE = contextvars.ContextVar("ai_tool_memory_scope", default="enterprise")
def checkpoint(stage,**details):
    callback=OBSERVER.get()
    if callback:callback(stage,**details)


class AIServiceError(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool = False, conversation_id: str | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.conversation_id = conversation_id
        self.model_metrics: dict[str, Any] | None = None


@dataclass(frozen=True)
class ModelConfig:
    base_url: str
    model: str
    api_key: str = field(repr=False)
    timeout_seconds: float = 30.0
    max_tool_rounds: int = 4
    max_output_tokens: int = 8192

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "ModelConfig":
        env = os.environ if environ is None else environ
        key = env.get("JL_AI_API_KEY", "")
        url = env.get("JL_AI_BASE_URL", "").strip().rstrip("/")
        model = env.get("JL_AI_MODEL", "").strip()
        if not key or not url or not model:
            raise AIServiceError(
                "MODEL_NOT_CONFIGURED",
                "模型服务未配置：需要 JL_AI_BASE_URL、JL_AI_MODEL 和 JL_AI_API_KEY。",
            )
        try:
            timeout = float(env.get("JL_AI_TIMEOUT_SECONDS", "30"))
            rounds = int(env.get("JL_AI_MAX_TOOL_ROUNDS", "4"))
            tokens = int(env.get("JL_AI_MAX_OUTPUT_TOKENS", "8192"))
        except ValueError as exc:
            raise AIServiceError("INVALID_MODEL_CONFIG", "模型服务参数格式无效。") from exc
        if not (1 <= timeout <= 180 and 0 <= rounds <= 8 and 64 <= tokens <= 8192):
            raise AIServiceError("INVALID_MODEL_CONFIG", "模型服务参数超出允许范围。")
        parsed = urlsplit(url)
        loopback_http = parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "::1", "localhost"}
        if not (parsed.scheme == "https" or loopback_http) or not parsed.hostname or parsed.username or parsed.password:
            raise AIServiceError("INVALID_MODEL_CONFIG", "模型服务地址必须使用 HTTPS 或本机回环地址。")
        return cls(url, model, key, timeout, rounds, tokens)


@dataclass(frozen=True)
class ChatRequest:
    tenant_id: str
    principal_id: str
    message: str
    conversation_id: str | None = None
    memory_scope: str = "enterprise"


@dataclass(frozen=True)
class Evidence:
    evidence_id: str
    text: str
    source: str
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ChatResponse:
    conversation_id: str
    reply: str
    evidence: tuple[Evidence, ...]
    tool_events: tuple[Mapping[str, Any], ...]
    status: str = "ok"
    model_usage: Mapping[str, Any] = field(default_factory=dict)
    model_elapsed_ms: int = 0
    elapsed_ms: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "conversation_id": self.conversation_id,
            "reply": self.reply,
            "evidence": [
                {"evidence_id": x.evidence_id, "text": x.text, "source": x.source, "metadata": dict(x.metadata)}
                for x in self.evidence
            ],
            "tool_events": [dict(x) for x in self.tool_events],
            "model_usage": dict(self.model_usage),
            "model_elapsed_ms": self.model_elapsed_ms,
            "elapsed_ms": self.elapsed_ms,
            "cost": {"amount": None, "currency": None, "status": "UNKNOWN"},
            "status": self.status,
        }


class ContextProvider(Protocol):
    def retrieve(self, query: str, *, tenant_id: str, principal_id: str, memory_scope: str) -> Sequence[Evidence]: ...


class ToolExecutor(Protocol):
    """Host adapter must authorize, audit, deduplicate, and execute atomically.

    ``list_tools`` must return only tools visible to this principal. ``execute``
    must recheck authorization and honor the supplied idempotency key on every
    call, including recovery retries.
    """

    supports_idempotency: bool

    def list_tools(self, *, tenant_id: str, principal_id: str) -> Sequence[Mapping[str, Any]]: ...

    def execute(
        self, name: str, arguments: Mapping[str, Any], *, tenant_id: str, principal_id: str,
        idempotency_key: str,
    ) -> Mapping[str, Any]: ...


class ModelClient(Protocol):
    def complete(self, messages: Sequence[Mapping[str, Any]], tools: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]: ...


class OpenAICompatibleClient:
    """Small OpenAI-compatible Chat Completions client using the stdlib."""

    def __init__(self, config: ModelConfig, opener: Callable[..., Any] | None = None):
        self.config = config
        self._opener = opener or urllib.request.urlopen

    def complete(self, messages: Sequence[Mapping[str, Any]], tools: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": list(messages),
            "max_tokens": self.config.max_output_tokens,
        }
        if tools:
            payload["tools"] = list(tools)
            payload["tool_choice"] = "auto"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.config.base_url + "/chat/completions", data=body,
            headers={"Authorization": "Bearer " + self.config.api_key, "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self._opener(req, timeout=self.config.timeout_seconds) as response:
                raw = response.read(2_000_001)
            if len(raw) > 2_000_000:
                raise AIServiceError("MODEL_RESPONSE_TOO_LARGE", "模型响应超过大小限制。")
            result = json.loads(raw.decode("utf-8"))
            choice = result["choices"][0]
            message = choice["message"]
            if not isinstance(message, dict):
                raise ValueError("message shape")
            normalized = dict(message)
            # 保留结束原因：length 表示回答被 max_tokens 掐断，不能当完整答复返回。
            finish_reason = choice.get("finish_reason")
            if isinstance(finish_reason, str) and finish_reason:
                normalized["_finish_reason"] = finish_reason
            usage = result.get("usage")
            if isinstance(usage, Mapping):
                normalized["_model_usage"] = {
                    key: usage.get(key)
                    for key in ("prompt_tokens", "completion_tokens", "total_tokens")
                }
            model = result.get("model")
            if isinstance(model, str) and model:
                normalized["_provider_model"] = model[:160]
            return normalized
        except AIServiceError:
            raise
        except urllib.error.HTTPError as exc:
            # Never include response body or request headers: either may contain secrets.
            raise AIServiceError("MODEL_HTTP_ERROR", f"模型服务返回 HTTP {exc.code}。", retryable=exc.code >= 500 or exc.code == 429) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise AIServiceError("MODEL_CONNECTION_ERROR", "无法连接模型服务。", retryable=True) from exc
        except (ValueError, KeyError, IndexError, TypeError, UnicodeError) as exc:
            raise AIServiceError("MODEL_BAD_RESPONSE", "模型服务响应格式无效。") from exc


class ConversationStore:
    """SQLite conversation and tool journal, isolated by tenant/user/scope."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), mode=0o700, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10, factory=ClosingConnection)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            return conn
        except BaseException:
            conn.close()
            raise

    def _init_db(self) -> None:
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, principal_id TEXT NOT NULL,
                    memory_scope TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT NOT NULL REFERENCES conversations(id),
                    role TEXT NOT NULL, content TEXT NOT NULL, created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS messages_conversation_idx ON messages(conversation_id, id);
                CREATE TABLE IF NOT EXISTS tool_events (
                    id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id),
                    call_id TEXT NOT NULL, name TEXT NOT NULL, arguments_json TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE, state TEXT NOT NULL,
                    result_json TEXT, error_code TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL,
                    UNIQUE(conversation_id, call_id)
                );
                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, memory_scope TEXT NOT NULL,
                    principal_id TEXT NOT NULL, content TEXT NOT NULL, source TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL, updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS memories_scope_idx
                    ON memories(tenant_id,memory_scope,principal_id,active,updated_at);
            """)
        try:
            os.chmod(self.db_path, 0o600)
        except OSError:
            pass

    def create_or_check(self, request: ChatRequest) -> str:
        cid = request.conversation_id or str(uuid.uuid4())
        now = time.time()
        with self._connect() as db:
            row = db.execute("SELECT tenant_id, principal_id, memory_scope FROM conversations WHERE id=?", (cid,)).fetchone()
            if row is None:
                if request.conversation_id:
                    raise AIServiceError("CONVERSATION_NOT_FOUND", "会话不存在或无权访问。")
                db.execute("INSERT INTO conversations VALUES(?,?,?,?,?,?)", (cid, request.tenant_id, request.principal_id, request.memory_scope, now, now))
            elif (row["tenant_id"], row["principal_id"], row["memory_scope"]) != (request.tenant_id, request.principal_id, request.memory_scope):
                raise AIServiceError("CONVERSATION_SCOPE_MISMATCH", "会话不存在或无权访问。")
            return cid

    def messages(self, conversation_id: str, limit: int = 24) -> list[dict[str, str]]:
        with self._connect() as db:
            rows = db.execute("SELECT role,content FROM (SELECT id,role,content FROM messages WHERE conversation_id=? ORDER BY id DESC LIMIT ?) ORDER BY id", (conversation_id, limit)).fetchall()
        return [{"role": row["role"], "content": row["content"]} for row in rows]

    def list_conversations(
        self, *, tenant_id: str, principal_id: str, memory_scope: str | None = None,
        limit: int = 30, offset: int = 0,
    ) -> list[dict[str, Any]]:
        sql = """SELECT c.id,c.memory_scope,c.created_at,c.updated_at,
                    (SELECT count(*) FROM messages m WHERE m.conversation_id=c.id) AS message_count,
                    (SELECT role FROM messages m WHERE m.conversation_id=c.id ORDER BY m.id DESC LIMIT 1) AS last_role,
                    (SELECT substr(content,1,160) FROM messages m WHERE m.conversation_id=c.id ORDER BY m.id DESC LIMIT 1) AS preview
                 FROM conversations c WHERE c.tenant_id=? AND c.principal_id=?"""
        args: list[Any] = [tenant_id, principal_id]
        if memory_scope is not None:
            sql += " AND c.memory_scope=?"
            args.append(memory_scope)
        sql += " ORDER BY c.updated_at DESC,c.id DESC LIMIT ? OFFSET ?"
        args.extend((limit, offset))
        with self._connect() as db:
            rows = db.execute(sql, args).fetchall()
        return [{
            "conversation_id": row["id"], "memory_scope": row["memory_scope"],
            "created_at": row["created_at"], "updated_at": row["updated_at"],
            "message_count": row["message_count"], "last_role": row["last_role"],
            "preview": row["preview"] or "",
        } for row in rows]

    def get_conversation(
        self, *, tenant_id: str, principal_id: str, conversation_id: str,
        limit: int = 50, before_message_id: int | None = None,
    ) -> dict[str, Any] | None:
        with self._connect() as db:
            conversation = db.execute(
                "SELECT id,memory_scope,created_at,updated_at FROM conversations WHERE id=? AND tenant_id=? AND principal_id=?",
                (conversation_id, tenant_id, principal_id),
            ).fetchone()
            if conversation is None:
                return None
            if before_message_id is None:
                rows = db.execute(
                    "SELECT id,role,content,created_at FROM messages WHERE conversation_id=? ORDER BY id DESC LIMIT ?",
                    (conversation_id, limit + 1),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT id,role,content,created_at FROM messages WHERE conversation_id=? AND id<? ORDER BY id DESC LIMIT ?",
                    (conversation_id, before_message_id, limit + 1),
                ).fetchall()
        has_older = len(rows) > limit
        page = list(reversed(rows[:limit]))
        return {
            "conversation_id": conversation["id"],
            "memory_scope": conversation["memory_scope"],
            "created_at": conversation["created_at"],
            "updated_at": conversation["updated_at"],
            "messages": [{
                "id": row["id"], "role": row["role"], "content": row["content"],
                "created_at": row["created_at"],
            } for row in page],
            "next_before_message_id": page[0]["id"] if has_older and page else None,
        }

    def append_message(self, conversation_id: str, role: str, content: str) -> int:
        if role not in {"user", "assistant", "tool"}:
            raise ValueError("invalid role")
        with self._connect() as db:
            now = time.time()
            cursor=db.execute("INSERT INTO messages(conversation_id,role,content,created_at) VALUES(?,?,?,?)", (conversation_id, role, content, now))
            db.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now, conversation_id))
            return int(cursor.lastrowid)

    def last_unanswered_user_turn(self, conversation_id: str, content: str) -> int | None:
        """Reuse the prior user-message ID when retrying a turn with no final assistant reply."""
        with self._connect() as db:
            row=db.execute("SELECT id,role,content FROM messages WHERE conversation_id=? ORDER BY id DESC LIMIT 1",(conversation_id,)).fetchone()
        if row and row["role"]=="user" and row["content"]==content:
            return int(row["id"])
        return None

    def pending_events(self, conversation_id: str) -> list[sqlite3.Row]:
        with self._connect() as db:
            return db.execute("SELECT * FROM tool_events WHERE conversation_id=? AND state IN ('pending','running') ORDER BY created_at", (conversation_id,)).fetchall()

    def completed_events(self, conversation_id: str) -> list[sqlite3.Row]:
        with self._connect() as db:
            return db.execute("SELECT name,arguments_json,result_json,state,error_code FROM tool_events WHERE conversation_id=? ORDER BY created_at", (conversation_id,)).fetchall()

    def save_memory(
        self, tenant_id: str, principal_id: str, memory_scope: str, content: str,
        *, source: str, memory_id: str | None = None,
    ) -> str:
        """Persist an explicitly authorized memory in its tenant/user scope.

        For enterprise memories principal_id is normalized to ``*`` so team
        context is shared within one tenant; personal memory is user-bound.
        Callers must obtain user authorization and audit before saving.
        """
        if memory_scope not in {"enterprise", "personal"}:
            raise AIServiceError("INVALID_MEMORY_SCOPE", "记忆范围必须是 enterprise 或 personal。")
        if not tenant_id or not principal_id or not content.strip() or len(content) > 4000:
            raise AIServiceError("INVALID_MEMORY", "记忆内容、企业或用户范围无效。")
        mid = memory_id or str(uuid.uuid4())
        owner = "*" if memory_scope == "enterprise" else principal_id
        now = time.time()
        with self._connect() as db:
            row = db.execute("SELECT tenant_id,memory_scope,principal_id FROM memories WHERE id=?", (mid,)).fetchone()
            if row and (row["tenant_id"], row["memory_scope"], row["principal_id"]) != (tenant_id, memory_scope, owner):
                raise AIServiceError("MEMORY_SCOPE_MISMATCH", "记忆标识已属于其他范围。")
            db.execute(
                "INSERT INTO memories(id,tenant_id,memory_scope,principal_id,content,source,active,created_at,updated_at) VALUES(?,?,?,?,?,?,1,?,?) "
                "ON CONFLICT(id) DO UPDATE SET content=excluded.content,source=excluded.source,active=1,updated_at=excluded.updated_at",
                (mid, tenant_id, memory_scope, owner, content.strip(), source[:200], now, now),
            )
        return mid

    def retrieve_memories(self, query: str, *, tenant_id: str, principal_id: str, memory_scope: str, limit: int = 5) -> list[Evidence]:
        owner = "*" if memory_scope == "enterprise" else principal_id
        with self._connect() as db:
            rows = db.execute(
                "SELECT id,content,source FROM memories WHERE tenant_id=? AND memory_scope=? AND principal_id=? AND active=1 ORDER BY updated_at DESC LIMIT 200",
                (tenant_id, memory_scope, owner),
            ).fetchall()
        terms: set[str] = set()
        for chunk in re.findall(r"[\u4e00-\u9fff]+|[a-zA-Z0-9_]+", query.lower()):
            if re.fullmatch(r"[\u4e00-\u9fff]+", chunk):
                terms.add(chunk)
                terms.update(chunk[index:index + 2] for index in range(len(chunk) - 1))
            elif len(chunk) > 1:
                terms.add(chunk)
        scored = []
        for row in rows:
            content = row["content"].lower()
            score = sum(1 for term in terms if term in content)
            # Keep a small recent-memory fallback if no query tokens overlap;
            # retrieval remains tenant and scope constrained.
            if score or not terms:
                scored.append((score, Evidence("memory:" + row["id"], content, row["source"] or "memory")))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [item[1] for item in scored[:limit]]

    def prepare_tool(self, conversation_id: str, call_id: str, name: str, args: Mapping[str, Any], *, idempotency_key: str | None = None) -> sqlite3.Row:
        args_json = json.dumps(args, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        idem = idempotency_key or hashlib.sha256((conversation_id + "\0" + call_id + "\0" + name + "\0" + args_json).encode()).hexdigest()
        now = time.time()
        with self._connect() as db:
            existing=db.execute("SELECT * FROM tool_events WHERE idempotency_key=?",(idem,)).fetchone()
            if existing:
                if existing["name"]!=name or existing["arguments_json"]!=args_json:
                    raise AIServiceError("TOOL_IDEMPOTENCY_COLLISION","工具幂等键冲突，操作已阻止。")
                return existing
            db.execute("INSERT OR IGNORE INTO tool_events(id,conversation_id,call_id,name,arguments_json,idempotency_key,state,created_at,updated_at) VALUES(?,?,?,?,?,?,'pending',?,?)", (str(uuid.uuid4()), conversation_id, call_id, name, args_json, idem, now, now))
            row = db.execute("SELECT * FROM tool_events WHERE conversation_id=? AND call_id=?", (conversation_id, call_id)).fetchone()
            if row["name"] != name or row["arguments_json"] != args_json:
                raise AIServiceError("TOOL_CALL_ID_COLLISION", "工具调用标识冲突，操作已阻止。")
            return row

    def mark_tool_running(self, event_id: str) -> None:
        with self._connect() as db:
            db.execute("UPDATE tool_events SET state='running',updated_at=? WHERE id=? AND state IN ('pending','running')", (time.time(), event_id))

    def finish_tool(self, event_id: str, result: Mapping[str, Any]) -> None:
        result_json = json.dumps(result, ensure_ascii=False)
        with self._connect() as db:
            db.execute("UPDATE tool_events SET state='succeeded',result_json=?,error_code=NULL,updated_at=? WHERE id=?", (result_json, time.time(), event_id))

    def fail_tool(self, event_id: str, code: str) -> None:
        with self._connect() as db:
            db.execute("UPDATE tool_events SET state='failed',error_code=?,updated_at=? WHERE id=?", (code[:80], time.time(), event_id))


SYSTEM_TEMPLATE = """你是当前已认证企业的业务助手。企业身份、业务口径和岗位权限以当前服务端上下文为准。只根据提供的证据回答；查询正常且授权范围内没有记录时可以直接回答没有；查询失败或无权访问要如实区分；确实需要补充信息时简短问一句。
你可以通过获准工具处理调用者有权限的业务请求。工具会再次校验权限、幂等并写审计；不要声称工具成功，除非收到成功结果。
写工具返回成功后，应直接向用户总结该结果；不要重复提交相同参数的写操作。写工具返回错误后，不要用相同参数重试，应说明拒绝或缺少的信息。文件生成成功后，应返回工具给出的下载地址。
不臆造金额、运单状态或客户规则；金额只以工具按授权范围返回的字段为准。厂家运费属于内部信息，不得出现在给司机、中转或收货方的单据与对外文件中。
当前记忆范围：{memory_scope}。企业上下文仅供本企业使用；个人上下文仅供当前用户使用。
"""

_WRITE_TOOL_NAMES = frozenset({
    "update_fee_status", "send_message",
    "create_task", "update_task", "update_receipt_status",
    "create_business_rule_version", "generate_waybill_report", "create_document", "manage_preference", "schedule_followup", "manage_followups",
})


def _tool_args_fingerprint(name: str, arguments: Mapping[str, Any]) -> str:
    payload = json.dumps({"name":name,"arguments":dict(arguments)},sort_keys=True,
                         ensure_ascii=False,separators=(",",":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _write_tool_summary(name: str, result: Mapping[str, Any]) -> str | None:
    """Render a deterministic final answer if the model repeats a completed write."""
    evidence = result.get("evidence")
    citation = ""
    if isinstance(evidence,list):
        ids=[x.get("evidence_id") for x in evidence if isinstance(x,Mapping) and isinstance(x.get("evidence_id"),str)]
        if ids: citation=" ["+ids[0]+"]"
    if name == "create_business_rule_version":
        item=result.get("item")
        if isinstance(item,Mapping) and item.get("rule_key") and item.get("version"):
            return (f"已新增企业规则版本：{item.get('title') or item['rule_key']} "
                    f"（{item['rule_key']} v{item['version']}，生效日期 {item.get('effective_at','未提供')}）。"
                    f"旧版本已保留。{citation}")
    if name == "update_receipt_status" and result.get("waybill_number"):
        verb="已登记" if result.get("active") else "已撤销登记"
        return f"{verb}运单 {result['waybill_number']} 的 {result.get('field','回单')} 状态。{citation}"
    if name == "generate_waybill_report":
        document=result.get("document")
        if isinstance(document,Mapping) and document.get("download_url"):
            return f"文件已生成，下载地址：{document['download_url']}。{citation}"
    if name in {"create_task","update_task"}:
        item=result.get("item")
        if isinstance(item,Mapping) and item.get("id"):
            return f"任务操作已完成，任务编号 {item['id']}，当前状态 {item.get('status','已更新')}。{citation}"
    return None


def _aggregate_model_usage(records: Sequence[Mapping[str, Any] | None], models: Sequence[str]) -> dict[str, Any]:
    keys = ("prompt_tokens", "completion_tokens", "total_tokens")
    parsed: list[dict[str, int] | None] = []
    for record in records:
        if not isinstance(record, Mapping):
            parsed.append(None)
            continue
        normalized: dict[str, int] = {}
        for key in keys:
            value = record.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                normalized[key] = value
        parsed.append(normalized if normalized else None)
    reported = sum(record is not None for record in parsed)
    complete = bool(parsed) and reported == len(parsed) and all(
        record is not None and all(key in record for key in keys) for record in parsed
    )
    totals = {
        key: sum(record[key] for record in parsed if record is not None)
        if complete else None
        for key in keys
    }
    return {
        "calls": len(records),
        "reported_calls": reported,
        "complete": complete,
        **totals,
        "provider_model": models[-1] if models and len(set(models)) == 1 else None,
    }


def _response_metrics(
    records: Sequence[Mapping[str, Any] | None], models: Sequence[str],
    model_elapsed: float, chat_started: float,
) -> dict[str, Any]:
    return {
        "model_usage": _aggregate_model_usage(records, models),
        "model_elapsed_ms": round(model_elapsed * 1000),
        "elapsed_ms": round((time.monotonic() - chat_started) * 1000),
    }


def _tool_evidence(result: Mapping[str, Any]) -> list[Evidence]:
    """Normalize small, host-authorized evidence records returned by tools."""
    raw = result.get("evidence")
    if not isinstance(raw, list):
        return []
    items: list[Evidence] = []
    for row in raw[:20]:
        if not isinstance(row, Mapping):
            continue
        evidence_id, text, source = row.get("evidence_id"), row.get("text"), row.get("source")
        if not all(isinstance(value, str) and value.strip() for value in (evidence_id, text, source)):
            continue
        metadata = row.get("metadata")
        safe_metadata: dict[str, Any] = {}
        if isinstance(metadata, Mapping):
            for key, value in list(metadata.items())[:20]:
                if not isinstance(key, str) or not key or len(key) > 80:
                    continue
                if value is None or isinstance(value, (bool, int, float)):
                    safe_metadata[key] = value
                elif isinstance(value, str):
                    safe_metadata[key] = value[:256]
        items.append(Evidence(evidence_id[:200], text[:4000], source[:80], safe_metadata))
    return items


class AIService:
    def __init__(
        self, store: ConversationStore, context_provider: ContextProvider,
        tool_executor: ToolExecutor, model_client: ModelClient | None,
        *, max_message_chars: int = 8000, max_history_messages: int = 24,
        max_tool_rounds: int = 4,
    ):
        self.store = store
        self.context_provider = context_provider
        self.tool_executor = tool_executor
        self.model_client = model_client
        self.max_message_chars = max_message_chars
        self.max_history_messages = max_history_messages
        self.max_tool_rounds = max_tool_rounds

    def list_conversations(
        self, *, tenant_id: str, principal_id: str, memory_scope: str | None = None,
        limit: int = 30, offset: int = 0,
    ) -> list[dict[str, Any]]:
        if memory_scope not in {None, "enterprise", "personal"}:
            raise AIServiceError("INVALID_MEMORY_SCOPE", "记忆范围必须是 enterprise 或 personal。")
        return self.store.list_conversations(
            tenant_id=tenant_id, principal_id=principal_id, memory_scope=memory_scope,
            limit=limit, offset=offset,
        )

    def get_conversation(
        self, *, tenant_id: str, principal_id: str, conversation_id: str,
        limit: int = 50, before_message_id: int | None = None,
    ) -> dict[str, Any] | None:
        return self.store.get_conversation(
            tenant_id=tenant_id, principal_id=principal_id, conversation_id=conversation_id,
            limit=limit, before_message_id=before_message_id,
        )

    def chat(self, request: ChatRequest) -> ChatResponse:
        chat_started = time.monotonic()
        usage_records: list[Mapping[str, Any] | None] = []
        provider_models: list[str] = []
        model_elapsed = 0.0
        if not request.tenant_id or not request.principal_id:
            raise AIServiceError("IDENTITY_REQUIRED", "缺少企业或用户身份。")
        if request.memory_scope not in {"enterprise", "personal"}:
            raise AIServiceError("INVALID_MEMORY_SCOPE", "记忆范围必须是 enterprise 或 personal。")
        message = request.message.strip()
        if not message or len(message) > self.max_message_chars:
            raise AIServiceError("INVALID_MESSAGE", "消息为空或超过长度限制。")
        if self.model_client is None:
            raise AIServiceError("MODEL_NOT_CONFIGURED", "模型服务未配置，当前不能生成回答或执行模型工具调用。")
        cid = self.store.create_or_check(request)
        def bind(error: AIServiceError) -> AIServiceError:
            error.conversation_id = cid
            if usage_records:
                error.model_metrics = _response_metrics(
                    usage_records, provider_models, model_elapsed, chat_started,
                )
            return error
        try:
            tools = list(self.tool_executor.list_tools(tenant_id=request.tenant_id, principal_id=request.principal_id))
        except Exception as exc:
            raise AIServiceError("TOOL_DISCOVERY_FAILED", "当前用户的业务工具授权检查失败。", conversation_id=cid) from exc
        allowed_names = set()
        for tool in tools:
            if not isinstance(tool, Mapping) or not isinstance(tool.get("function"), Mapping):
                continue
            name = tool["function"].get("name")
            if isinstance(name, str) and name:
                allowed_names.add(name)
        # Recover interrupted calls before accepting a new model turn. The same
        # host executor receives the exact durable idempotency key on retry.
        try:
            self._recover_tools(cid, request)
        except AIServiceError as exc:
            raise bind(exc)
        tool_history = self.store.completed_events(cid)[-30:]
        try:
            business_evidence = self.context_provider.retrieve(
                message, tenant_id=request.tenant_id, principal_id=request.principal_id,
                memory_scope=request.memory_scope,
            )
        except Exception as exc:
            raise AIServiceError("CONTEXT_RETRIEVAL_FAILED", "业务证据检索失败。", conversation_id=cid) from exc
        # 记忆检索已统一由 context_provider.retrieve 覆盖（含 workplace_memory 与 long_term_memories），
        # 不再单独读已废弃的 memories 表（该表无写入口，永远为空）。
        evidence_items = list(business_evidence)
        unanswered_turn = self.store.last_unanswered_user_turn(cid, message)
        user_turn_id = unanswered_turn if unanswered_turn is not None else self.store.append_message(cid, "user", message)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_TEMPLATE.format(memory_scope=request.memory_scope)},
        ]
        persona_provider = getattr(self.context_provider, "authenticated_persona", None)
        if persona_provider is not None:
            persona = persona_provider(tenant_id=request.tenant_id, principal_id=request.principal_id)
            messages.append({"role": "system", "content": persona})
        if evidence_items:
            evidence_text = "\n".join(f"[{e.evidence_id}] 来源={e.source} 内容={e.text}" for e in evidence_items)
            messages.append({"role": "system", "content": "本轮检索证据（回答中的事实应引用方括号ID）：\n" + evidence_text})
        if tool_history:
            history_text = "\n".join(
                f"{row['name']} [{row['state']}]: " + (row["result_json"] or row["error_code"] or "执行状态待核实")
                for row in tool_history
            )
            messages.append({"role": "system", "content": "本会话此前的工具操作记录（仅可据此描述已返回结果）：\n" + history_text})
        messages.extend(self.store.messages(cid, self.max_history_messages))
        tool_events: list[Mapping[str, Any]] = []
        completed_write_results: dict[str, tuple[str, Mapping[str, Any]]] = {}
        for round_index in range(self.max_tool_rounds + 1):
            model_started = time.monotonic()
            try:
                checkpoint("model_started")
                response = self.model_client.complete(messages, tools)
            except AIServiceError as exc:
                raise bind(exc)
            finally:
                model_elapsed += time.monotonic() - model_started
            raw_usage = response.get("_model_usage", response.get("usage"))
            usage_records.append(raw_usage if isinstance(raw_usage, Mapping) else None)
            provider_model = response.get("_provider_model")
            if isinstance(provider_model, str) and provider_model:
                provider_models.append(provider_model)
            try:
                checkpoint("model_completed")
            except AIServiceError as exc:
                raise bind(exc)
            content = response.get("content")
            calls = response.get("tool_calls") or []
            if not calls:
                reply = content if isinstance(content, str) else ""
                # 回答被输出上限掐断时，让模型接着写完（最多续一次）。
                # 不把半截话当完整答复存下来——客户看到断句我们这儿还标成功，很糟。
                if response.get("_finish_reason") == "length" and reply.strip():
                    continue_messages = list(messages) + [
                        {"role": "assistant", "content": reply},
                        {"role": "user", "content": "上一条回答因长度限制被截断。请从断开处接着写，把没说完的部分补完；不要重复已经说过的内容。"},
                    ]
                    cont_started = time.monotonic()
                    try:
                        checkpoint("model_started")
                        continued = self.model_client.complete(continue_messages, [])
                    except AIServiceError as exc:
                        raise bind(exc)
                    finally:
                        model_elapsed += time.monotonic() - cont_started
                    cont_usage = continued.get("_model_usage", continued.get("usage"))
                    usage_records.append(cont_usage if isinstance(cont_usage, Mapping) else None)
                    cont_model = continued.get("_provider_model")
                    if isinstance(cont_model, str) and cont_model:
                        provider_models.append(cont_model)
                    try:
                        checkpoint("model_completed")
                    except AIServiceError as exc:
                        raise bind(exc)
                    extra = continued.get("content") if isinstance(continued.get("content"), str) else ""
                    if extra.strip():
                        reply = reply + extra
                    # 续写后仍被截断：不再无限续，明确报错，绝不返回半截话。
                    if continued.get("_finish_reason") == "length":
                        raise bind(AIServiceError("MODEL_OUTPUT_TRUNCATED", "这次内容太长没能写完，可以让我分几部分再说。"))
                if not reply.strip():
                    # A provider can return an empty final message after tools ran.
                    # Ask once for a plain answer, with no tools, so no side effect is repeated.
                    recovery_messages = list(messages) + [{"role":"system", "content":"刚才没有返回可显示的答复。请仅根据已有记录回答用户；已经做过的操作按结果如实说明，不要重复执行，也不要编造。"}]
                    recovery_started = time.monotonic()
                    try:
                        checkpoint("model_started")
                        recovered = self.model_client.complete(recovery_messages, [])
                    except AIServiceError as exc:
                        raise bind(exc)
                    finally:
                        model_elapsed += time.monotonic() - recovery_started
                    recovery_usage = recovered.get("_model_usage", recovered.get("usage"))
                    usage_records.append(recovery_usage if isinstance(recovery_usage, Mapping) else None)
                    recovery_model = recovered.get("_provider_model")
                    if isinstance(recovery_model, str) and recovery_model:
                        provider_models.append(recovery_model)
                    try:
                        checkpoint("model_completed")
                    except AIServiceError as exc:
                        raise bind(exc)
                    reply = recovered.get("content") if isinstance(recovered.get("content"), str) else ""
                    if recovered.get("tool_calls") or not reply.strip():
                        raise bind(AIServiceError("MODEL_EMPTY_RESPONSE", "这次没能整理出完整答复，已完成的操作仍保留，可以接着问我。"))
                self.store.append_message(cid, "assistant", reply)
                metrics = _response_metrics(usage_records, provider_models, model_elapsed, chat_started)
                return ChatResponse(cid, reply, tuple(evidence_items), tuple(tool_events), **metrics)
            if round_index >= self.max_tool_rounds:
                raise bind(AIServiceError("TOOL_ROUND_LIMIT", "模型工具调用轮次超过限制，未执行新操作。"))
            messages.append({"role": "assistant", "content": content, "tool_calls": calls})
            for call in calls:
                try:
                    name, call_id, args = self._parse_tool_call(call)
                except AIServiceError as exc:
                    raise bind(exc)
                if name not in allowed_names:
                    raise bind(AIServiceError("TOOL_NOT_AUTHORIZED", "模型请求了当前用户不可用的业务工具，操作已阻止。"))
                fingerprint = _tool_args_fingerprint(name, args) if name in _WRITE_TOOL_NAMES else None
                if fingerprint and fingerprint in completed_write_results:
                    _, prior_result = completed_write_results[fingerprint]
                    result = dict(prior_result)
                    known_evidence_ids = {item.evidence_id for item in evidence_items}
                    for item in _tool_evidence(result):
                        if item.evidence_id not in known_evidence_ids and len(evidence_items) < 40:
                            evidence_items.append(item)
                            known_evidence_ids.add(item.evidence_id)
                    tool_events.append({"name":name,"state":"deduplicated","result":result})
                    summary = _write_tool_summary(name, result)
                    if summary:
                        self.store.append_message(cid, "assistant", summary)
                        metrics = _response_metrics(usage_records, provider_models, model_elapsed, chat_started)
                        return ChatResponse(cid, summary, tuple(evidence_items), tuple(tool_events), **metrics)
                    messages.append({"role":"tool","tool_call_id":call_id,"content":json.dumps(result,ensure_ascii=False)})
                    continue
                try:
                    durable_key = hashlib.sha256(
                        f"ai-write-intent-v1\0{cid}\0{user_turn_id}\0{fingerprint}".encode("utf-8")
                    ).hexdigest() if fingerprint else None
                    event = self.store.prepare_tool(cid, call_id, name, args, idempotency_key=durable_key)
                    checkpoint("tool_started",name=name)
                    result = self._execute_event(event, request)
                    checkpoint("tool_completed",name=name)
                except AIServiceError as exc:
                    raise bind(exc)
                known_evidence_ids = {item.evidence_id for item in evidence_items}
                for item in _tool_evidence(result):
                    if item.evidence_id not in known_evidence_ids and len(evidence_items) < 40:
                        evidence_items.append(item)
                        known_evidence_ids.add(item.evidence_id)
                tool_events.append({"name": name, "state": "succeeded", "result": dict(result)})
                if fingerprint and not result.get("error"):
                    completed_write_results[fingerprint] = (name, dict(result))
                messages.append({"role": "tool", "tool_call_id": call_id, "content": json.dumps(result, ensure_ascii=False)})
        raise bind(AIServiceError("TOOL_ROUND_LIMIT", "模型工具调用轮次超过限制，未生成最终答复。"))

    @staticmethod
    def _parse_tool_call(call: Mapping[str, Any]) -> tuple[str, str, Mapping[str, Any]]:
        try:
            name = call["function"]["name"]
            call_id = call["id"]
            raw = call["function"].get("arguments", "{}")
            args = json.loads(raw) if isinstance(raw, str) else raw
            if not isinstance(name, str) or not name or not isinstance(call_id, str) or not call_id or not isinstance(args, dict):
                raise ValueError("invalid call")
            return name, call_id, args
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise AIServiceError("MODEL_BAD_TOOL_CALL", "模型生成的工具调用格式无效。") from exc

    def _execute_event(self, event: sqlite3.Row, request: ChatRequest) -> Mapping[str, Any]:
        if event["state"] == "succeeded":
            try:
                cached=json.loads(event["result_json"] or "{}")
            except json.JSONDecodeError as exc:
                raise AIServiceError("TOOL_RECOVERY_BLOCKED","已完成工具结果不可读取，未重新执行。") from exc
            if not isinstance(cached,Mapping):
                raise AIServiceError("TOOL_RECOVERY_BLOCKED","已完成工具结果格式无效，未重新执行。")
            return cached
        if event["state"] == "failed":
            raise AIServiceError("TOOL_RECOVERY_BLOCKED","该工具调用已失败，未在同一请求中重复执行。")
        if not getattr(self.tool_executor, "supports_idempotency", False):
            self.store.fail_tool(event["id"], "IDEMPOTENCY_REQUIRED")
            raise AIServiceError("TOOL_EXECUTOR_UNSAFE", "工具执行器未声明幂等保护，操作已阻止。")
        self.store.mark_tool_running(event["id"])
        try:
            scope_token = TOOL_MEMORY_SCOPE.set(request.memory_scope)
            try:
                result = self.tool_executor.execute(
                    event["name"], json.loads(event["arguments_json"]),
                    tenant_id=request.tenant_id, principal_id=request.principal_id,
                    idempotency_key=event["idempotency_key"],
                )
            finally:
                TOOL_MEMORY_SCOPE.reset(scope_token)
            if not isinstance(result, Mapping):
                raise AIServiceError("TOOL_BAD_RESULT", "工具返回格式无效。")
            self.store.finish_tool(event["id"], result)
            return result
        except AIServiceError as exc:
            self.store.fail_tool(event["id"], exc.code)
            raise
        except (LookupError, PermissionError):
            # These are expected business outcomes for scoped reads and
            # authorization checks. Return the same opaque result in both
            # cases so the model can explain the denial without learning
            # whether a foreign record exists. Keep genuine faults fail closed.
            result = {"error": "not_found"}
            self.store.finish_tool(event["id"], result)
            return result
        except Exception as exc:
            self.store.fail_tool(event["id"], "TOOL_EXECUTION_FAILED")
            # Do not surface exception strings; host errors can contain data.
            raise AIServiceError("TOOL_EXECUTION_FAILED", "业务工具执行失败。") from exc

    def _recover_tools(self, conversation_id: str, request: ChatRequest) -> None:
        pending = self.store.pending_events(conversation_id)
        if pending and not getattr(self.tool_executor, "supports_idempotency", False):
            raise AIServiceError("TOOL_RECOVERY_BLOCKED", "存在未完成工具事件，且执行器未声明幂等保护。")
        for event in pending:
            self._execute_event(event, request)


def build_service(
    data_dir: str,
    *,
    context_provider: ContextProvider,
    tool_executor: ToolExecutor,
    model_client: ModelClient | None = None,
    model_config: ModelConfig | None = None,
) -> AIService:
    """Create the AI service with its own durable store under ``data_dir/ai``.

    In an unconfigured environment the service still starts, but every chat
    returns ``MODEL_NOT_CONFIGURED``. No fallback or synthetic response exists.
    """
    resolved_client = model_client
    if resolved_client is None:
        if model_config is None:
            try:
                model_config = ModelConfig.from_env()
            except AIServiceError as exc:
                if exc.code != "MODEL_NOT_CONFIGURED":
                    raise
        if model_config is not None:
            resolved_client = OpenAICompatibleClient(model_config)
    return AIService(
        ConversationStore(os.path.join(data_dir, "ai", "ai.sqlite3")),
        context_provider,
        tool_executor,
        resolved_client,
        max_tool_rounds=(model_config.max_tool_rounds if model_config else 4),
    )
