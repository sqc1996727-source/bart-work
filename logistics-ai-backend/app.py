#!/usr/bin/env python3
"""Isolated single-enterprise test backend for the Julong V9 workbench.

Only Python's standard library is required. Run `python3 app.py serve`.
"""
from __future__ import annotations

import argparse
import business_integrity as integrity
import receipt_state
from capabilities import logistics_read
from capabilities import runtime as agent_runtime, files as agent_files, catalog as capability_catalog, team as agent_team, routine as agent_routine
import base64
from collections import Counter
import csv
from email import policy
from email.parser import BytesParser
import hashlib
import hmac
import http.server
import io
import contextvars
import json
import os
import re
import secrets
import sqlite3
from sqlite_connection import ClosingConnection
import sys
import time
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

ROOT = Path(__file__).resolve().parent
import product_profile
PRODUCT = product_profile.load(ROOT)
TENANT_ID = PRODUCT["tenant_id"]
DATA_DIR = PRODUCT["data_dir"]
DB_PATH = DATA_DIR / "julong.sqlite3"
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "3000"))
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "1").lower() not in {"0", "false", "no"}
SESSION_COOKIE = "julong_enterprise_session"
SESSION_SECONDS = 30 * 24 * 60 * 60
PASSWORD_ITERATIONS = 310_000
MAX_BODY = 2 * 1024 * 1024
MAX_MULTIPART_BODY = 11 * 1024 * 1024

# Compatibility exports for existing tests and adapters.
import module_scope
from module_scope import LOGISTICS_MODULE_TOOLS, LOGISTICS_MODULE_ROUTES, LOGISTICS_MODULE_STATE_KEYS

def _enabled_modules():
    return module_scope.enabled(PRODUCT)

def _module_enabled(module):
    return module in _enabled_modules()

def _blocked_tool_names():
    return module_scope.blocked_tools(PRODUCT)

def _module_state_keys():
    return module_scope.suppressed_keys(PRODUCT)

def _business_state(payload):
    return module_scope.project(PRODUCT, _decode_state(payload))


STATE_KEYS = (
    "julong_demo_waybills_v2", "julong_demo_recipients_v2",
    "julong_demo_dispatch_v5", "julong_demo_month_v1",
    "julong_demo_fees_v1", "julong_demo_receipts_v1",
    "julong_demo_bills_v1", "julong_demo_diffs_v1",
    "julong_demo_delivery_v1", "julong_demo_ai_v1",
)
TASK_COMPLETION_CONDITIONS = {"receipt_delivered", "fees_confirmed"}
ROLES = {"employee", "dispatcher", "owner_finance"}
ROLE_PERMISSIONS = {
    "employee": ["waybills.read", "waybills.write_own", "tasks.read_assigned", "tasks.update_own"],
    "dispatcher": ["waybills.read", "waybills.write", "dispatch.manage", "receipts.manage", "tasks.manage", "finance.read"],
    "owner_finance": ["waybills.read", "waybills.write", "dispatch.manage", "tasks.manage", "finance.read", "finance.write", "users.manage", "audit.read"],
}
SENSITIVE_KEYS = {"amount", "feeWarehouse", "freightTotal"}
# 费用记录域里每笔 fee 的 amount 是"待批费用金额"，与运单的厂家运费成本价是两笔账。
# 调度要批钱就得看见批多少，所以只对费用域放行 amount；运单域仍按 SENSITIVE_KEYS 全锁。
_DISPATCHER_FEE_VISIBLE_KEYS = frozenset({"amount"})
_FINANCE_FIELDS = ("amount", "feeWarehouse", "freightTotal")
_ORDINARY_WAYBILL_FIELDS = (
    "number", "date", "receiver", "recipient", "phone", "address", "destination",
    "cargo", "qty", "weight", "carrier", "created", "updated", "created_by",
)

def _finance_scope(user) -> str:
    """财务数据可见范围：all=老板全量 / detail=管理层单票与线路 / none=员工不可见。"""
    try: role = user if isinstance(user,str) else user["role"]
    except (KeyError,IndexError,TypeError): role = ""
    if role == "owner_finance": return "all"
    if role == "dispatcher": return "detail"
    return "none"

_V3_SCOPE = contextvars.ContextVar("v3_finance_scope", default="none")
_V3_AUDIO = contextvars.ContextVar("v3_audio_input", default=False)
_V3_AUDIO_CANDIDATES = contextvars.ContextVar("v3_audio_candidates", default=())
LOGIN_FAILS: dict[str, tuple[int, float]] = {}
_AI_SERVICE = None
_V3_AI_SERVICE = None
_V3_CONVERSATION_PREFIX = "v3_"


def _v3_recent_change_intent(text):
    text=str(text or "")
    return any(term in text for term in ("最近有什么变化","最近的变化","最近变化","变化汇总","最近做了什么","最近更新","近几天变化","这周有什么变化","本周变化","最近动态"))


def _v3_summary_intent(text):
    text=str(text or "")
    return any(term in text for term in ("运营总览","运营概况","整体情况","企业情况","今天怎么样","当前情况汇总","业务汇总","工作汇总","整体汇总"))


def _v3_requested_days(text):
    match=re.search(r"(?:最近|近|过去)\s*(\d{1,2})\s*天",str(text or ""))
    if match: return min(30,max(1,int(match.group(1))))
    if "一个月" in str(text or "") or "30天" in str(text or ""): return 30
    return 7


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def db() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None, factory=ClosingConnection)
    try:
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA busy_timeout=10000")
        return con
    except BaseException:
        con.close()
        raise


def init_db() -> None:
    with db() as con:
        product_profile.bind_database(con, PRODUCT)
        con.executescript("""
        CREATE TABLE IF NOT EXISTS users (
          id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL DEFAULT 'julong-test', username TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
          role TEXT NOT NULL CHECK(role IN ('employee','dispatcher','owner_finance')),
          password_salt BLOB NOT NULL, password_hash BLOB NOT NULL,
          active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sessions (
          token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
          expires_at INTEGER NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS work_state (
          id INTEGER PRIMARY KEY CHECK(id=1), rev INTEGER NOT NULL,
          updated_at TEXT NOT NULL, updated_by TEXT NOT NULL, payload TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS tasks (
          id TEXT PRIMARY KEY, title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
          assignee_id TEXT REFERENCES users(id), due_at TEXT, waybill_number TEXT,
          status TEXT NOT NULL CHECK(status IN ('open','in_progress','done','cancelled')),
          completion_condition TEXT, completion_origin TEXT,
          created_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS audit (
          seq INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL,
          user_id TEXT NOT NULL, user_name TEXT NOT NULL, action TEXT NOT NULL,
          entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, details TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS ai_call_log (
          seq INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL,
          tenant_id TEXT NOT NULL, user_id TEXT NOT NULL, user_name TEXT NOT NULL,
          role TEXT NOT NULL, conversation_id TEXT NOT NULL,
          status INTEGER NOT NULL, error_code TEXT NOT NULL DEFAULT '',
          question_digest TEXT NOT NULL DEFAULT '', answer_digest TEXT NOT NULL DEFAULT '',
          prompt_tokens INTEGER NOT NULL DEFAULT 0, completion_tokens INTEGER NOT NULL DEFAULT 0,
          elapsed_ms INTEGER NOT NULL DEFAULT 0, tool_names TEXT NOT NULL DEFAULT '[]',
          attachment_count INTEGER NOT NULL DEFAULT 0, finance_fields INTEGER NOT NULL DEFAULT 0,
          job_id TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS idempotency (
          user_id TEXT NOT NULL REFERENCES users(id), key TEXT NOT NULL,
          request_hash TEXT NOT NULL, response_status INTEGER NOT NULL,
          response_json TEXT NOT NULL, created_at TEXT NOT NULL,
          PRIMARY KEY(user_id,key)
        );
        CREATE TABLE IF NOT EXISTS business_rules (
          id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, rule_key TEXT NOT NULL,
          layer TEXT NOT NULL CHECK(layer IN ('foundation','calculation','conversation')),
          title TEXT NOT NULL, content TEXT NOT NULL, version INTEGER NOT NULL,
          effective_at TEXT NOT NULL, finance_only INTEGER NOT NULL DEFAULT 0,
          created_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL,
          UNIQUE(tenant_id,rule_key,version)
        );
        CREATE TABLE IF NOT EXISTS long_term_memories (
          id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
          scope TEXT NOT NULL CHECK(scope IN ('enterprise','personal')),
          principal_id TEXT REFERENCES users(id), title TEXT NOT NULL, content TEXT NOT NULL,
          finance_only INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1,
          created_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL,
          deactivated_by TEXT REFERENCES users(id), deactivated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS generated_documents (
          id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, created_by TEXT NOT NULL REFERENCES users(id),
          filename TEXT NOT NULL, format TEXT NOT NULL CHECK(format IN ('csv','markdown')),
          stored_path TEXT NOT NULL UNIQUE, sha256 TEXT NOT NULL, size_bytes INTEGER NOT NULL CHECK(size_bytes>=0),
          waybill_numbers_json TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS bot_state (
          id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, bot_role TEXT NOT NULL,
          subject TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('todo','in_progress','waiting','done','cancelled')),
          progress TEXT NOT NULL DEFAULT '', next_step TEXT NOT NULL DEFAULT '',
          evidence TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL, updated_by TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_sessions_exp ON sessions(expires_at);
        CREATE INDEX IF NOT EXISTS idx_tasks_assignee ON tasks(assignee_id,status);
        CREATE INDEX IF NOT EXISTS idx_audit_seq ON audit(seq DESC);
        CREATE INDEX IF NOT EXISTS idx_ai_call_log_at ON ai_call_log(at DESC);
        CREATE INDEX IF NOT EXISTS idx_rules_key_version ON business_rules(tenant_id,rule_key,version DESC);
        CREATE INDEX IF NOT EXISTS idx_memories_scope ON long_term_memories(tenant_id,scope,principal_id,active);
        CREATE INDEX IF NOT EXISTS idx_bot_state ON bot_state(tenant_id,bot_role,status,updated_at);
        """)
        cols = {row["name"] for row in con.execute("PRAGMA table_info(users)")}
        if "tenant_id" not in cols:
            con.execute("ALTER TABLE users ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'julong-test'")
        task_cols = {row["name"] for row in con.execute("PRAGMA table_info(tasks)")}
        if "completion_condition" not in task_cols:
            con.execute("ALTER TABLE tasks ADD COLUMN completion_condition TEXT")
        if "completion_origin" not in task_cols:
            con.execute("ALTER TABLE tasks ADD COLUMN completion_origin TEXT")
        con.execute("INSERT OR IGNORE INTO work_state(id,rev,updated_at,updated_by,payload) VALUES(1,0,'','','{}')")
    integrity.initialize(sys.modules[__name__])
    agent_runtime.initialize(sys.modules[__name__])
    if TENANT_ID == product_profile.LEGACY_ID:
        _import_legacy_state_if_empty()
    from ingestion.handler import configure as configure_ingestion
    configure_ingestion(db_factory=db,data_dir=DATA_DIR,audit_fn=audit,state_keys=STATE_KEYS,now_fn=now_iso)



def _import_legacy_state_if_empty():
    """部署到服务器后，把服务器上那份真账 state.json 一次性接进来，之后以本库为准。
    只在本库还是空账时导入，绝不覆盖已有数据。"""
    legacy = DATA_DIR / "state.json"
    if not legacy.is_file():
        return
    try:
        raw = json.loads(legacy.read_text(encoding="utf-8"))
        state = raw.get("state", raw)
        if not isinstance(state, dict):
            return
        domains = _parse_state_domains(state, allow_legacy_missing=True)
        if not any(domains.values()):
            return
        _reject_new_demo_rows({key: "[]" for key in STATE_KEYS}, domains)
        payload = json.dumps({key: json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
                              for key, rows in domains.items()}, ensure_ascii=False, separators=(",", ":"))
        rev = max(1, int(raw.get("rev") or 0))
        updated = str(raw.get("updated") or "")
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return
    with db() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT rev,payload FROM work_state WHERE id=1").fetchone()
        if not row or row["rev"] != 0:
            return
        try:
            current = _parse_state_domains(json.loads(row["payload"]), allow_legacy_missing=True)
        except (TypeError, ValueError):
            return
        if any(current.values()):
            return
        con.execute("UPDATE work_state SET payload=?,rev=?,updated_at=? WHERE id=1 AND rev=0",
                    (payload, rev, updated))
        con.commit()


def hash_password(password: str, salt: bytes | None = None) -> tuple[bytes, bytes]:
    if len(password) < 8:
        raise ValueError("密码至少 8 个字符")
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PASSWORD_ITERATIONS)
    return salt, digest


def add_user(username: str, name: str, role: str, password: str) -> None:
    init_db()
    if role not in ROLES:
        raise ValueError("role 必须是 employee、dispatcher 或 owner_finance")
    if not re.fullmatch(r"[A-Za-z0-9_.@-]{3,80}", username):
        raise ValueError("username 只能包含字母、数字、点、下划线、@、连字符，长度 3-80")
    salt, digest = hash_password(password)
    with db() as con:
        con.execute("INSERT INTO users(id,tenant_id,username,name,role,password_salt,password_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (secrets.token_urlsafe(16), TENANT_ID, username, name.strip()[:80] or username,
                     role, salt, digest, now_iso()))


def audit(con: sqlite3.Connection, user: sqlite3.Row, action: str,
          entity_type: str, entity_id: str, details: dict | None = None) -> None:
    con.execute("INSERT INTO audit(at,user_id,user_name,action,entity_type,entity_id,details) VALUES(?,?,?,?,?,?,?)",
                (now_iso(), user["id"], user["name"], action, entity_type, entity_id,
                 json.dumps(details or {}, ensure_ascii=False, separators=(",", ":"))))


def _digest(text, limit=160):
    """对话摘要：只留前若干字用于追溯，不存问答全文。"""
    if not isinstance(text, str):
        return ""
    clean = " ".join(text.split())
    return clean[:limit] + ("…" if len(clean) > limit else "")


def log_ai_call(con, user, *, conversation_id, status, error_code="",
                question="", answer="", model_usage=None, elapsed_ms=0,
                tool_names=None, attachment_count=0, finance_fields=False, job_id=""):
    """记录一次 AI 对话摘要（不存问答全文）。写失败不能影响业务回答。"""
    try:
        usage = model_usage if isinstance(model_usage, dict) else {}
        prompt = usage.get("prompt_tokens") or usage.get("input_tokens") or 0
        completion = usage.get("completion_tokens") or usage.get("output_tokens") or 0
        known = usage.get("known_total_tokens")
        if not prompt and not completion and isinstance(known, int):
            completion = known
        names = tool_names if isinstance(tool_names, list) else []
        con.execute(
            "INSERT INTO ai_call_log(at,tenant_id,user_id,user_name,role,conversation_id,status,error_code,"
            "question_digest,answer_digest,prompt_tokens,completion_tokens,elapsed_ms,tool_names,"
            "attachment_count,finance_fields,job_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (now_iso(), user["tenant_id"], user["id"], user["name"], user["role"],
             str(conversation_id or ""), int(status), str(error_code or "")[:60],
             _digest(question), _digest(answer), int(prompt or 0), int(completion or 0),
             int(elapsed_ms or 0), json.dumps(names, ensure_ascii=False, separators=(",", ":")),
             int(attachment_count or 0), 1 if finance_fields else 0, str(job_id or "")))
    except Exception:
        pass


def _idempotent_response(con, user_id, key, request_hash):
    row = con.execute("SELECT request_hash,response_status,response_json FROM idempotency WHERE user_id=? AND key=?", (user_id,key)).fetchone()
    if not row: return None
    if row["request_hash"] != request_hash: raise ValueError("idempotency_key_reused")
    return row["response_status"], json.loads(row["response_json"])


def _current_task_replay(con, prior):
    """Preserve the original request result while returning the task's live progress."""
    status, response = prior
    item = response.get("item") if isinstance(response, dict) else None
    if isinstance(item, dict) and item.get("id"):
        row = con.execute("SELECT * FROM tasks WHERE id=?", (item["id"],)).fetchone()
        if row:
            current = dict(row)
            current["due_date"] = current.get("due_at")
            current["waybill_id"] = current.get("waybill_number")
            response["item"] = current
    return status, response


def _tool_idempotency_write(con, user_id, key, request_hash, response_status, response, created_at):
    con.execute("INSERT INTO idempotency(user_id,key,request_hash,response_status,response_json,created_at) VALUES(?,?,?,?,?,?)",
                (user_id,key,request_hash,response_status,json.dumps(response,ensure_ascii=False,separators=(",",":")),created_at))


def update_reconciliation_amount(user, body, key):
    if user["role"] != "owner_finance":
        return 403, {"error": "forbidden"}
    if set(body) != {"number", "amount", "base_rev"} or not 1 <= len(key) <= 128:
        return 400, {"error": "invalid_reconciliation_update"}
    number, amount, base_rev = (body[name] for name in ("number", "amount", "base_rev"))
    if (
        not isinstance(number, str) or not 1 <= len(number) <= 80 or number != number.strip()
        or type(base_rev) is not int or base_rev < 0
        or type(amount) not in (str, int, float)
        or not re.fullmatch(r"[0-9]{1,12}(?:\.[0-9]{1,2})?", str(amount))
    ):
        return 400, {"error": "invalid_reconciliation_update"}
    amount = format(Decimal(str(amount)), ".2f")
    digest = hashlib.sha256(json.dumps(
        ["reconciliation.amount", number, amount, base_rev], ensure_ascii=False
    ).encode()).hexdigest()
    with db() as con:
        con.execute("BEGIN IMMEDIATE")
        actor = con.execute(
            "SELECT * FROM users WHERE tenant_id=? AND id=? AND active=1 AND role='owner_finance'",
            (user["tenant_id"], user["id"]),
        ).fetchone()
        if not actor:
            return 403, {"error": "forbidden"}
        try:
            prior = _idempotent_response(con, actor["id"], key, digest)
        except ValueError as exc:
            return 409, {"error": str(exc)}
        if prior:
            return prior
        row = con.execute("SELECT rev,payload FROM work_state WHERE id=1").fetchone()
        if row["rev"] != base_rev:
            return 409, {"error": "revision_conflict", "rev": row["rev"]}
        state = _decode_state(row["payload"])
        docs = json.loads(state[STATE_KEYS[0]])
        matches = [item for item in docs if isinstance(item, dict) and item.get("number") == number]
        if len(matches) != 1:
            return (409 if matches else 404), {"error": "ambiguous_waybill" if matches else "not_found"}
        item = matches[0]
        before = item.get("amount")
        rev, updated = base_rev, now_iso()
        if before != amount or "freightTotal" in item:
            item.pop("freightTotal", None)
            item["amount"] = amount
            state[STATE_KEYS[0]] = json.dumps(docs, ensure_ascii=False, separators=(",", ":"))
            rev += 1
            con.execute(
                "UPDATE work_state SET rev=?,payload=?,updated_at=?,updated_by=? WHERE id=1",
                (rev, json.dumps(state, ensure_ascii=False, separators=(",", ":")), updated, actor["id"]),
            )
            audit(con, actor, "reconciliation.amount.update", "waybill", number,
                  {"rev": rev, "before": before, "after": amount})
        result = {"number": number, "amount": amount, "rev": rev, "updated": updated}
        _tool_idempotency_write(con, actor["id"], key, digest, 200, result, updated)
        con.commit()
    return 200, result


def _merge_ordinary_waybill_writes(before, after):
    prior = {str(row.get("number")): row for row in before if isinstance(row, dict)}
    for row in after:
        old = prior.get(str(row.get("number")), {})
        for field in SENSITIVE_KEYS:
            if field in row and row[field] != old.get(field):
                if row[field] not in (None, "", 0, "0", "0.00"):
                    raise ValueError("use_reconciliation_api")
            if field in old:
                row[field] = old[field]
            else:
                row.pop(field, None)
        for field in set(row) | set(old):
            if field in SENSITIVE_KEYS:
                continue
            if field not in _ORDINARY_WAYBILL_FIELDS:
                if field == "remark" and isinstance(row.get(field), str) and row[field].strip():
                    continue
                if field in old:
                    row[field] = old[field]
                else:
                    row.pop(field, None)
            elif field not in row and field in old and field not in _ordinary_waybill(old):
                row[field] = old[field]
            elif isinstance(row.get(field), str) and _v3_output_has_explicit_price(row[field]) and row[field] != old.get(field):
                raise ValueError("price_text_in_waybill")


def _parse_state_domains(incoming, *, allow_legacy_missing=False):
    """Check a full work-state snapshot before it enters the write transaction."""
    keys = set(STATE_KEYS)
    if not isinstance(incoming, dict) or set(incoming) - keys:
        raise ValueError("invalid_state_domains")
    if not allow_legacy_missing and set(incoming) != keys:
        raise ValueError("missing_state_domains")
    result = {}
    for key in STATE_KEYS:
        raw = incoming.get(key, "[]")
        if not allow_legacy_missing and not isinstance(raw, str):
            raise ValueError("invalid_state_domain")
        rows = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError("invalid_state_rows")
        # Fail before storage on non-finite JSON numbers, including nested data.
        json.dumps(rows, ensure_ascii=False, allow_nan=False)
        result[key] = rows
    for key in (STATE_KEYS[0], STATE_KEYS[5]):
        seen = set()
        for row in result[key]:
            number = row.get("number")
            if not isinstance(number, str) or not 1 <= len(number) <= 80 or number.strip() != number:
                raise ValueError("invalid_waybill_number")
            if number in seen:
                raise ValueError("duplicate_waybill_number")
            seen.add(number)
            if key == STATE_KEYS[0]:
                for field in ("date", "receiver", "recipient", "phone", "address", "destination", "cargo", "carrier", "created_by"):
                    if field in row and row[field] is not None and not isinstance(row[field], str):
                        raise ValueError("invalid_waybill_field")
            else:
                for field in ("photo", "paper", "delivered", "supplement"):
                    marker = row.get(field)
                    if marker is not None and type(marker) not in (dict, bool):
                        raise ValueError("invalid_receipt_marker")
    return result


def _reject_new_demo_rows(old_state, domains):
    for key in (STATE_KEYS[0], STATE_KEYS[4], STATE_KEYS[6], STATE_KEYS[7], STATE_KEYS[9]):
        rows = domains[key]
        marked = [row for row in rows if row.get("demo") is True or
                  str(row.get("by", "")).startswith("AI 模拟")]
        if not marked:
            continue
        historical = json.loads(old_state[key])
        if any(row not in historical for row in marked):
            raise ValueError("demo_records_forbidden")


def update_receipt_status_record(user, arguments, idempotency_key):
    if user["role"] not in {"employee","dispatcher","owner_finance"}:
        raise PermissionError("receipt_update_forbidden")
    if not isinstance(arguments, dict) or set(arguments) != {"waybill_number", "field", "active", "expected_rev"}:
        raise ValueError("invalid_receipt_update")
    number, field, active, expected_rev = (arguments[key] for key in ("waybill_number", "field", "active", "expected_rev"))
    if (not isinstance(number, str) or not 1 <= len(number) <= 80 or number != number.strip()
        or field not in receipt_state.FIELDS or type(active) is not bool
        or type(expected_rev) is not int or expected_rev < 0):
        raise ValueError("invalid_receipt_update")
    if not idempotency_key or len(idempotency_key)>128:
        raise ValueError("idempotency_key_required")
    request_hash=hashlib.sha256(json.dumps({"tool":"update_receipt_status","arguments":arguments},sort_keys=True,ensure_ascii=False,separators=(",",":")).encode()).hexdigest()
    now=now_iso()
    with db() as con:
        con.execute("BEGIN IMMEDIATE")
        actor = con.execute("SELECT * FROM users WHERE id=? AND tenant_id=? AND active=1", (user["id"], user["tenant_id"])).fetchone()
        if not actor:
            raise PermissionError("receipt_update_forbidden")
        user = actor
        row=con.execute("SELECT rev,payload FROM work_state WHERE id=1").fetchone()
        if not row:
            raise RuntimeError("state_unavailable")
        state=_decode_state(row["payload"]); docs=json.loads(state[STATE_KEYS[0]])
        matches = [doc for doc in docs if isinstance(doc, dict) and doc.get("number") == number]
        if len(matches) != 1 or not _case_allowed(matches[0], user, con=con):
            raise LookupError("not_found")
        prior = _idempotent_response(con, user["id"], idempotency_key, request_hash)
        if prior:
            con.rollback()
            return prior[1]
        if row["rev"] != expected_rev:
            raise ValueError("revision_conflict")
        receipts=json.loads(state[STATE_KEYS[5]])
        matches = [item for item in receipts if isinstance(item, dict) and item.get("number") == number]
        if len(matches) > 1:
            raise ValueError("duplicate_receipt_number")
        receipt = matches[0] if matches else None
        marker = (receipt or {}).get(field)
        rev = row["rev"]
        if active != receipt_state.active(marker):
            if receipt is None:
                receipt = {"number": number}
                receipts.append(receipt)
            receipt[field] = receipt_state.transition(marker, active, user["name"], now)
            state[STATE_KEYS[5]]=json.dumps(receipts,ensure_ascii=False,separators=(",",":"))
            payload=json.dumps(state,ensure_ascii=False,separators=(",",":"))
            rev += 1
            con.execute("UPDATE work_state SET rev=?,updated_at=?,updated_by=?,payload=? WHERE id=1",(rev,now,user["id"],payload))
            audit(con,user,"ai.tool.receipt_status.update","waybill",number,{"field":field,"active":active,"rev":rev})
            _sync_business_tasks(con,user,_decode_state(payload))
        current = receipt or {}
        response={"waybill_number":number,"field":field,"active":receipt_state.active(current.get(field)),"receipt":_redact_tree(current),"state_rev":rev}
        _tool_idempotency_write(con,user["id"],idempotency_key,request_hash,200,response,now)
        con.commit()
    return response


def update_fee_status_record(user, arguments, idempotency_key):
    import finance_confirmation
    return finance_confirmation.create(sys.modules[__name__], user, arguments, idempotency_key)


def create_business_rule_version_record(user, arguments, idempotency_key):
    if user["role"]!="owner_finance": raise PermissionError("rule_management_forbidden")
    allowed={"rule_key","layer","title","content","effective_at","finance_only"}
    if not isinstance(arguments,dict) or set(arguments)-allowed: raise ValueError("invalid_rule")
    key=str(arguments.get("rule_key","")); layer=str(arguments.get("layer","")); title=str(arguments.get("title","")).strip(); content=str(arguments.get("content","")).strip()
    effective=str(arguments.get("effective_at") or datetime.now().date().isoformat()); finance_only=arguments.get("finance_only",False)
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{1,79}",key) or layer not in {"foundation","calculation","conversation"}:
        raise ValueError("invalid_rule")
    if not title or len(title)>160 or not content or len(content)>8000 or not isinstance(finance_only,bool) or _contains_sensitive(arguments):
        raise ValueError("invalid_rule")
    try: datetime.strptime(effective,"%Y-%m-%d")
    except ValueError as exc: raise ValueError("invalid_rule") from exc
    if not idempotency_key or len(idempotency_key)>128: raise ValueError("idempotency_key_required")
    request_hash=hashlib.sha256(json.dumps({"tool":"create_business_rule_version","arguments":arguments},sort_keys=True,ensure_ascii=False,separators=(",",":")).encode()).hexdigest()
    created=now_iso()
    with db() as con:
        con.execute("BEGIN IMMEDIATE")
        try: prior=_idempotent_response(con,user["id"],idempotency_key,request_hash)
        except ValueError:
            con.rollback(); raise
        if prior:
            con.rollback(); return prior[1]
        row=con.execute("SELECT MAX(version) AS version FROM business_rules WHERE tenant_id=? AND rule_key=?",(user["tenant_id"],key)).fetchone()
        version=(row["version"] or 0)+1; rule_id=secrets.token_urlsafe(16)
        con.execute("INSERT INTO business_rules(id,tenant_id,rule_key,layer,title,content,version,effective_at,finance_only,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (rule_id,user["tenant_id"],key,layer,title,content,version,effective,int(finance_only),user["id"],created))
        item={"id":rule_id,"rule_key":key,"layer":layer,"title":title,"content":content,"version":version,"effective_at":effective,"finance_only":finance_only,"created_at":created}
        response={"item":item,"evidence":[{"evidence_id":f"business_rule:{key}:v{version}","text":f"[{layer}] {title}: {content}","source":"business_rule","metadata":{"rule_key":key,"version":version,"effective_at":effective,"finance_only":finance_only}}]}
        audit(con,user,"ai.tool.rule.version.create","business_rule",rule_id,{"rule_key":key,"version":version,"effective_at":effective,"finance_only":finance_only})
        _tool_idempotency_write(con,user["id"],idempotency_key,request_hash,201,response,created)
        con.commit()
    return response


def generate_waybill_report_record(user, arguments, idempotency_key):
    if not isinstance(arguments,dict) or set(arguments)-{"waybill_numbers","format"}: raise ValueError("invalid_document_request")
    numbers=arguments.get("waybill_numbers"); fmt=str(arguments.get("format","csv"))
    if fmt not in {"csv","markdown"} or not isinstance(numbers,list) or not 1<=len(numbers)<=100:
        raise ValueError("invalid_document_request")
    numbers=[str(x).strip()[:40] for x in numbers]
    if any(not x for x in numbers) or len(set(numbers))!=len(numbers): raise ValueError("invalid_document_request")
    if not idempotency_key or len(idempotency_key)>128: raise ValueError("idempotency_key_required")
    request_hash=hashlib.sha256(json.dumps({"tool":"generate_waybill_report","arguments":arguments},sort_keys=True,ensure_ascii=False,separators=(",",":")).encode()).hexdigest()
    now=now_iso(); document_id=secrets.token_urlsafe(18); extension="csv" if fmt=="csv" else "md"
    created_file=None
    try:
        with db() as con:
            con.execute("BEGIN IMMEDIATE")
            try: prior=_idempotent_response(con,user["id"],idempotency_key,request_hash)
            except ValueError:
                con.rollback(); raise
            if prior:
                con.rollback(); return prior[1]
            state_row=con.execute("SELECT payload FROM work_state WHERE id=1").fetchone()
            state=_decode_state(state_row["payload"] if state_row else "{}")
            docs=json.loads(state[STATE_KEYS[0]])
            by_number={str(x.get("number")):x for x in docs if isinstance(x,dict) and x.get("number") is not None}
            if any(number not in by_number for number in numbers):
                con.rollback(); raise LookupError("not_found")
            selected=[by_number[n] for n in numbers]
            if user["role"]=="employee" and any(not _case_allowed(x,user) for x in selected):
                con.rollback(); raise LookupError("not_found")
            visible=[_ordinary_waybill(x) for x in selected]
            if user["role"]!="owner_finance":
                for item in visible: item.pop("created_by",None)
            headers=[("运单编号","number"),("日期","date"),("到站","destination"),("货物","cargo"),("件数","qty"),("重量（吨）","weight"),("收货人","receiver"),("电话","phone"),("地址","address"),("承运方","carrier")]
            if fmt=="csv":
                output=io.StringIO(newline=""); writer=csv.writer(output); writer.writerow([label for label,_ in headers])
                for row_item in visible:
                    cells=[]
                    for _,field in headers:
                        value=row_item.get(field,"")
                        if isinstance(value,str) and value.lstrip().startswith(("=","+","-","@")):
                            value="'"+value
                        cells.append(value)
                    writer.writerow(cells)
                content=output.getvalue().encode("utf-8-sig")
            else:
                lines=["# 运单清单","",f"生成时间：{now}","","|"+"|".join(label for label,_ in headers)+"|","|"+"|".join("---" for _ in headers)+"|"]
                for row_item in visible:
                    cells=[str(row_item.get(field,"")).replace("|","\\|").replace("\n"," ") for _,field in headers]
                    lines.append("|"+"|".join(cells)+"|")
                content=("\n".join(lines)+"\n").encode("utf-8")
            digest=hashlib.sha256(content).hexdigest(); filename=f"waybill-report-{datetime.now().strftime('%Y%m%d')}-{document_id[:6]}.{extension}"
            root=DATA_DIR/"generated_documents"
            if root.is_symlink(): con.rollback(); raise RuntimeError("unsafe_document_root")
            root.mkdir(parents=True,exist_ok=True,mode=0o700)
            target=root/(document_id+"."+extension)
            with open(target,"xb") as handle:
                os.chmod(target,0o600); handle.write(content)
            created_file=target
            relative=f"generated_documents/{target.name}"
            con.execute("INSERT INTO generated_documents(id,tenant_id,created_by,filename,format,stored_path,sha256,size_bytes,waybill_numbers_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (document_id,user["tenant_id"],user["id"],filename,fmt,relative,digest,len(content),json.dumps(numbers,ensure_ascii=False),now))
            con.execute("INSERT INTO document_access VALUES(?,?)",(document_id,0))
            item={"id":document_id,"filename":filename,"format":fmt,"sha256":digest,"size_bytes":len(content),"waybill_numbers":numbers,"download_url":f"/api/generated-documents/{document_id}","created_at":now}
            response={"document":item,"evidence":[{"evidence_id":f"generated-document:{document_id}","text":f"已生成文件 {filename}，包含 {len(numbers)} 票运单；下载地址 {item['download_url']}。","source":"generated_document","metadata":{"document_id":document_id,"format":fmt,"sha256":digest,"waybill_count":len(numbers)}}]}
            audit(con,user,"ai.tool.document.generate","generated_document",document_id,{"format":fmt,"waybill_count":len(numbers),"sha256":digest})
            _tool_idempotency_write(con,user["id"],idempotency_key,request_hash,201,response,now)
            con.commit()
    except Exception:
        if created_file:
            try: created_file.unlink(missing_ok=True)
            except OSError: pass
        raise
    return response


def _task_condition_met(condition, waybill_number, state):
    if not condition or not waybill_number:
        return False
    try:
        receipts = json.loads(state.get(STATE_KEYS[5], "[]"))
        fees = json.loads(state.get(STATE_KEYS[4], "[]"))
    except (TypeError, json.JSONDecodeError):
        return False
    number = str(waybill_number)
    if condition == "receipt_delivered":
        return any(
            isinstance(row, dict) and str(row.get("number", "")) == number
            and receipt_state.active(row.get("delivered"))
            for row in receipts
        )
    if condition == "fees_confirmed":
        active = [row for row in fees if isinstance(row, dict)
                  and str(row.get("number", "")) == number and row.get("status") != "作废"]
        return bool(active) and all(row.get("status") == "已确认" for row in active)
    return False


def _sync_business_tasks(con, actor, state):
    """Advance only tasks with explicit structured conditions, inside caller transaction."""
    rows = con.execute("SELECT * FROM tasks WHERE completion_condition IS NOT NULL OR completion_origin='business'").fetchall()
    changed_at = now_iso()
    for row in rows:
        if row["completion_condition"]=="receipt_delivered" and not _module_enabled("receipt"): continue
        if row["completion_condition"]=="fees_confirmed" and not _module_enabled("finance"): continue
        met = _task_condition_met(row["completion_condition"], row["waybill_number"], state)
        if met and row["status"] in {"open", "in_progress"}:
            con.execute("UPDATE tasks SET status='done',completion_origin='business',updated_at=? WHERE id=?",
                        (changed_at, row["id"]))
            audit(con, actor, "task.business_complete", "task", row["id"],
                  {"completion_condition": row["completion_condition"], "waybill_number": row["waybill_number"]})
        elif not met and row["status"] == "done" and row["completion_origin"] == "business":
            con.execute("UPDATE tasks SET status='in_progress',completion_origin=NULL,updated_at=? WHERE id=?",
                        (changed_at, row["id"]))
            audit(con, actor, "task.business_reopen", "task", row["id"],
                  {"completion_condition": row["completion_condition"], "waybill_number": row["waybill_number"]})


def _validate_task_condition(con, condition, waybill_number):
    if condition is None:
        return
    if condition not in TASK_COMPLETION_CONDITIONS:
        raise ValueError("invalid_completion_condition")
    if not _module_enabled("transport") or not _module_enabled("receipt" if condition=="receipt_delivered" else "finance"):
        raise ValueError("module_disabled")
    if not waybill_number:
        raise ValueError("completion_condition_requires_waybill")
    row = con.execute("SELECT payload FROM work_state WHERE id=1").fetchone()
    try:
        state = _decode_state(row["payload"] if row else "{}")
        waybills = json.loads(state.get(STATE_KEYS[0], "[]"))
    except Exception:
        waybills = []
    if not any(isinstance(item, dict) and str(item.get("number", "")) == str(waybill_number) for item in waybills):
        raise ValueError("completion_waybill_not_found")


def create_task_record(user, body, idem_key):
    if set(body)-{"title","description","assignee_id","due_at","due_date","waybill_number","waybill_id","completion_condition"}:
        raise ValueError("invalid_task")
    title = str(body.get("title", "")).strip()[:160]
    if not title: raise ValueError("invalid_task")
    if not idem_key or len(idem_key) > 128: raise ValueError("idempotency_key_required")
    if user["role"] == "employee": raise PermissionError("forbidden")
    if _contains_sensitive(body): raise PermissionError("finance_fields_forbidden")
    description = str(body.get("description", ""))[:4000]
    assignee = str(body["assignee_id"]) if body.get("assignee_id") not in (None, "") else None
    raw_due=body.get("due_at",body.get("due_date"))
    raw_number=body.get("waybill_number",body.get("waybill_id"))
    due_at = str(raw_due)[:40] if raw_due not in (None, "") else None
    number = str(raw_number)[:40] if raw_number not in (None, "") else None
    condition = str(body.get("completion_condition") or "").strip() or None
    request_hash = hashlib.sha256(json.dumps(body,sort_keys=True,ensure_ascii=False,separators=(",", ":")).encode()).hexdigest()
    task_id, ts = secrets.token_urlsafe(12), now_iso()
    with db() as con:
        con.execute("BEGIN IMMEDIATE")
        prior = _idempotent_response(con,user["id"],idem_key,request_hash)
        if prior:
            replay=_current_task_replay(con,prior)
            con.rollback(); return replay
        if assignee and not con.execute("SELECT 1 FROM users WHERE id=? AND tenant_id=? AND active=1", (assignee,user["tenant_id"])).fetchone():
            con.rollback(); raise ValueError("invalid_assignee")
        try: _validate_task_condition(con,condition,number)
        except ValueError:
            con.rollback(); raise
        con.execute("INSERT INTO tasks(id,title,description,assignee_id,due_at,waybill_number,status,completion_condition,completion_origin,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,'open',?,NULL,?,?,?)",
                    (task_id,title,description,assignee,due_at,number,condition,user["id"],ts,ts))
        audit(con,user,"task.create","task",task_id,{"assignee_id":assignee,"waybill_number":number,"completion_condition":condition})
        if condition:
            state_row=con.execute("SELECT payload FROM work_state WHERE id=1").fetchone()
            _sync_business_tasks(con,user,_decode_state(state_row["payload"] if state_row else "{}"))
        item=dict(con.execute("SELECT * FROM tasks WHERE id=?",(task_id,)).fetchone())
        item.update({"due_date":due_at,"waybill_id":number})
        response={"item":item}; response_json=json.dumps(response,ensure_ascii=False,separators=(",", ":"))
        con.execute("INSERT INTO idempotency(user_id,key,request_hash,response_status,response_json,created_at) VALUES(?,?,?,?,?,?)",(user["id"],idem_key,request_hash,201,response_json,ts))
        con.commit()
    return 201,response


def update_task_record(user, task_id, body, idem_key=""):
    manager = user["role"] in {"dispatcher","owner_finance"}
    request_hash = hashlib.sha256(json.dumps({"task_id":task_id,"body":body},sort_keys=True,ensure_ascii=False,separators=(",", ":")).encode()).hexdigest()
    if idem_key and len(idem_key) > 128: raise ValueError("invalid_idempotency_key")
    with db() as con:
        con.execute("BEGIN IMMEDIATE")
        if idem_key:
            prior = _idempotent_response(con,user["id"],idem_key,request_hash)
            if prior:
                replay=_current_task_replay(con,prior)
                con.rollback(); return replay
        row = con.execute("SELECT * FROM tasks WHERE id=?",(task_id,)).fetchone()
        if not row: con.rollback(); raise LookupError("not_found")
        if not manager and row["assignee_id"] != user["id"]: con.rollback(); raise LookupError("not_found")
        allowed = {"status"} if not manager else {"title","description","assignee_id","due_at","due_date","waybill_number","waybill_id","status","completion_condition"}
        if set(body)-allowed: con.rollback(); raise PermissionError("task_field_forbidden")
        status = str(body.get("status",row["status"]))
        if status not in {"open","in_progress","done","cancelled"}: con.rollback(); raise ValueError("invalid_status")
        if not manager and status not in {"in_progress","done"}: con.rollback(); raise PermissionError("status_transition_forbidden")
        assignee = body.get("assignee_id",row["assignee_id"])
        if assignee and not con.execute("SELECT 1 FROM users WHERE id=? AND tenant_id=? AND active=1",(assignee,user["tenant_id"])).fetchone(): con.rollback(); raise ValueError("invalid_assignee")
        values = {
            "title":str(body.get("title",row["title"]))[:160],
            "description":str(body.get("description",row["description"]))[:4000],
            "assignee_id":assignee,
            "due_at":body.get("due_at",body.get("due_date",row["due_at"])),
            "waybill_number":body.get("waybill_number",body.get("waybill_id",row["waybill_number"])),
            "status":status,
            "completion_condition":(str(body.get("completion_condition") or "").strip() or None) if "completion_condition" in body else row["completion_condition"],
        }
        if not values["title"]: con.rollback(); raise ValueError("invalid_task")
        try: _validate_task_condition(con,values["completion_condition"],values["waybill_number"])
        except ValueError:
            con.rollback(); raise
        changed = any(values[k] != row[k] for k in values)
        if changed:
            updated = now_iso()
            origin = row["completion_origin"]
            if "status" in body:
                origin = "manual" if status == "done" else None
            con.execute("UPDATE tasks SET title=?,description=?,assignee_id=?,due_at=?,waybill_number=?,status=?,completion_condition=?,completion_origin=?,updated_at=? WHERE id=?",
                        (values["title"],values["description"],values["assignee_id"],values["due_at"],values["waybill_number"],values["status"],values["completion_condition"],origin,updated,task_id))
            changed_fields=sorted(k for k in values if values[k] != row[k])
            audit(con,user,"task.update","task",task_id,{"fields":changed_fields,"status":status,"completion_condition":values["completion_condition"]})
            state_row=con.execute("SELECT payload FROM work_state WHERE id=1").fetchone()
            _sync_business_tasks(con,user,_decode_state(state_row["payload"] if state_row else "{}"))
        result=dict(con.execute("SELECT * FROM tasks WHERE id=?",(task_id,)).fetchone())
        result["due_date"]=result.get("due_at"); result["waybill_id"]=result.get("waybill_number")
        response={"item":result}
        if idem_key:
            status_code=200; con.execute("INSERT INTO idempotency(user_id,key,request_hash,response_status,response_json,created_at) VALUES(?,?,?,?,?,?)",(user["id"],idem_key,request_hash,status_code,json.dumps(response,ensure_ascii=False,separators=(",", ":")),now_iso()))
        else: status_code=200
        con.commit()
    return status_code,response


def _redact_tree(value, keep_keys=frozenset()):
    """Remove finance-only keys and scrub price text.

    keep_keys applies to this dict only: it lets the caller pass one specific
    sensitive key through (the fee domain for dispatchers), while nested
    structures are always redacted in full.
    """
    if isinstance(value, list):
        return [_redact_tree(item) for item in value]
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if key in SENSITIVE_KEYS and key not in keep_keys:
                continue
            if isinstance(item,str) and key in {"beforeAfter","raw","note","remark","description","content","title"}:
                item = re.sub(r"(?:¥|￥)\s*[\d,]+(?:\.\d{1,2})?", "[仅财务可见]", item)
                item = re.sub(r"(?:运费|费用|成本|金额|报价|对方记|我方记)\s*[:：]?\s*[¥￥]?\s*[\d,]+(?:\.\d{1,2})?", "[仅财务可见]", item)
                item = re.sub(r"\b[\d,]+(?:\.\d{1,2})?\s*(?:元|块)\b", "[仅财务可见]", item)
            out[key] = _redact_tree(item)
        return out
    return value


def _redact_state_fee_row(row):
    """费用域单行的脱敏：调度要看见每笔待批费用的 amount；厂家运费类费用行仍锁死。"""
    keep = _DISPATCHER_FEE_VISIBLE_KEYS
    if isinstance(row, dict) and V3Context._protected_freight_type(row.get("type")):
        keep = frozenset()
    return _redact_tree(row, keep_keys=keep)


def _contains_sensitive(value) -> bool:
    if isinstance(value, dict):
        return any(k in SENSITIVE_KEYS or _contains_sensitive(v) for k, v in value.items())
    if isinstance(value, list):
        return any(_contains_sensitive(v) for v in value)
    return False


def _contains_sensitive_value(value) -> bool:
    """Zero/blank placeholders carry no price; any real financial value is forbidden to staff."""
    if isinstance(value, dict):
        for key, item in value.items():
            if key in SENSITIVE_KEYS and item not in (None, "", 0, 0.0, False, "0", "0.00"):
                return True
            if _contains_sensitive_value(item): return True
    elif isinstance(value, list):
        return any(_contains_sensitive_value(item) for item in value)
    return False


def _merge_protected(old, incoming):
    """Preserve finance-only values omitted by a non-finance client on saves."""
    if isinstance(incoming, list):
        old_rows = old if isinstance(old, list) else []
        indexes = {}
        for row in old_rows:
            if isinstance(row, dict):
                for k in ("id", "number"):
                    if row.get(k) is not None:
                        indexes[(k, str(row[k]))] = row
        result = []
        for row in incoming:
            match = None
            if isinstance(row, dict):
                match = next((indexes[(k, str(row[k]))] for k in ("id", "number")
                              if row.get(k) is not None and (k, str(row[k])) in indexes), None)
            result.append(_merge_protected(match or {}, row))
        return result
    if isinstance(incoming, dict):
        old_obj = old if isinstance(old, dict) else {}
        out = {}
        for key, val in incoming.items():
            if key in SENSITIVE_KEYS:
                continue
            out[key] = _merge_protected(old_obj.get(key), val)
        for key in SENSITIVE_KEYS:
            if key in old_obj:
                out[key] = old_obj[key]
        return out
    return incoming


def _json_equal(a, b) -> bool:
    return json.dumps(a, ensure_ascii=False, sort_keys=True, separators=(",", ":")) == json.dumps(b, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _case_allowed(ticket,user=None,principal_id=None,con=None):
    if user is None:
        with db() as identity_con:
            user=identity_con.execute('SELECT * FROM users WHERE id=? AND active=1',(principal_id,)).fetchone()
        if not user:return False
    if con is not None:return agent_team.access(con,user,ticket)
    return agent_team.allowed(sys.modules[__name__],user,ticket)


def _employee_waybills(old_rows, new_rows, principal_id):
    """Employees may add rows and edit rows stamped as theirs; deletes and other owners' rows are retained."""
    old_rows = old_rows if isinstance(old_rows, list) else []
    new_rows = new_rows if isinstance(new_rows, list) else []
    by_number = {str(row.get("number")): row for row in old_rows if isinstance(row, dict) and row.get("number") is not None}
    output, seen = [], set()
    for item in new_rows:
        if not isinstance(item, dict) or item.get("number") is None: continue
        number = str(item["number"])
        if number in seen: continue
        seen.add(number)
        prior = by_number.get(number)
        if prior:
            if not _case_allowed(prior,principal_id=principal_id):
                visible_prior = _redact_tree(prior)
                visible_incoming = {k:v for k,v in item.items() if k not in SENSITIVE_KEYS}
                if not _json_equal(visible_prior, visible_incoming):
                    raise PermissionError("waybill_not_owned")
                output.append(prior)
            else:
                merged = dict(item)
                # 金额字段对员工永远只读：原票有则用原值覆盖，原票没有则直接剔除。
                # 防止员工先建一张无金额票、再编辑塞入金额绕过价格保密。
                for key in SENSITIVE_KEYS:
                    if key in prior: merged[key] = prior[key]
                    else: merged.pop(key, None)
                merged["created_by"] = prior.get("created_by",principal_id)
                output.append(merged)
        else:
            created = dict(item)
            for key in SENSITIVE_KEYS: created.pop(key, None)
            created["created_by"] = principal_id
            output.append(created)
    for prior in old_rows:
        if isinstance(prior, dict) and prior.get("number") is not None and str(prior["number"]) not in seen:
            if _case_allowed(prior,principal_id=principal_id):
                raise PermissionError("waybill_delete_forbidden")
            output.append(prior)
    return output


def _employee_append_only(old_rows, new_rows, principal_id):
    """Keep historical recipient rows immutable while allowing additive autocomplete history."""
    old_rows = old_rows if isinstance(old_rows, list) else []
    new_rows = new_rows if isinstance(new_rows, list) else []
    keys = {json.dumps(row, ensure_ascii=False, sort_keys=True) for row in old_rows}
    added = []
    for row in new_rows:
        if not isinstance(row, dict): continue
        signature = json.dumps(row, ensure_ascii=False, sort_keys=True)
        if signature not in keys:
            clean = dict(row); clean["created_by"] = principal_id
            added.append(clean); keys.add(signature)
    return old_rows + added


def _employee_owned_numbers(waybills,principal_id):
    return {str(row.get("number")) for row in waybills if isinstance(row,dict) and _case_allowed(row,principal_id=principal_id) and row.get("number") is not None}


_CJK_SPAN_RE = re.compile(r"[\u3400-\u9fff]+")
_LATIN_TERM_RE = re.compile(r"[a-z0-9][a-z0-9._-]+", re.IGNORECASE)
_PERSONAL_PREFERENCE_TERMS = ("偏好", "习惯", "喜欢", "倾向", "个人风格", "回答风格", "表达方式", "工作方式", "怎么称呼", "记住我", "preference", "prefer", "habit", "style")
_PREFERENCE_MEMORY_TERMS = ("偏好", "习惯", "喜欢", "倾向", "风格", "表达", "回复", "沟通", "措辞", "格式", "preference", "preferred", "habit", "style", "writing")
_BUSINESS_QUERY_TERMS = ("运单", "货物", "收货", "物流", "回单", "费用", "账单", "对账", "任务", "分派", "状态", "异常", "到站", "承运", "发货", "司机", "发票", "运费", "清单", "件数", "重量", "地址", "电话", "客户", "进仓", "交付", "厂家")


def _retrieval_features(value):
    """Produce simple Chinese/English features without a third-party segmenter."""
    text=str(value or "").casefold()
    features=set(_LATIN_TERM_RE.findall(text))
    for span in _CJK_SPAN_RE.findall(text):
        if len(span)==1:
            features.add(span)
            continue
        for size in (2,3):
            if len(span)>=size:
                features.update(span[index:index+size] for index in range(len(span)-size+1))
    return features


def _personal_preference_query(query):
    return any(term in str(query or "") for term in _PERSONAL_PREFERENCE_TERMS)


def _preference_memory(memory):
    title=str(memory["title"] or "")
    return any(term in title for term in _PREFERENCE_MEMORY_TERMS)


def _workplace_memory_rows(con, tenant_id, principal_id, role, scope='enterprise', allow_finance=False):
    from capabilities import workplace_memory
    return workplace_memory.current_rows(sys.modules[__name__],con,tenant_id,principal_id,role,scope,allow_finance)


def _employee_receipts(old_rows,new_rows,owned_numbers,user_name):
    old_rows=old_rows if isinstance(old_rows,list) else []; new_rows=new_rows if isinstance(new_rows,list) else []
    before={str(row.get("number")):row for row in old_rows if isinstance(row,dict)}
    after={str(row.get("number")):row for row in new_rows if isinstance(row,dict)}
    allowed={"number","photo","paper","delivered","supplement"}
    out=[]
    for number, prior in before.items():
        submitted=after.get(number)
        if not submitted: out.append(prior); continue
        if number not in owned_numbers:
            if not _json_equal(_redact_tree(prior),_redact_tree(submitted)): raise PermissionError("receipt_not_owned")
            out.append(prior); continue
        if set(submitted)-allowed: raise PermissionError("receipt_field_forbidden")
        merged=dict(prior)
        for field in ("photo","paper","delivered","supplement"):
            newval=submitted.get(field); oldval=prior.get(field)
            if _json_equal(oldval,newval): continue
            if newval is None:
                # Preserve revocation history; deleting a marker is not a valid undo.
                raise PermissionError("receipt_history_required")
            if not isinstance(newval,dict): raise ValueError("invalid_receipt")
            merged[field]=receipt_state.transition(oldval, receipt_state.active(newval), user_name, now_iso())
        out.append(merged)
    for number, submitted in after.items():
        if number in before: continue
        if number not in owned_numbers: raise PermissionError("receipt_not_owned")
        if set(submitted)-allowed: raise PermissionError("receipt_field_forbidden")
        clean={"number":number}
        for field in ("photo","paper","delivered","supplement"):
            if receipt_state.active(submitted.get(field)): clean[field]={"by":user_name,"at":now_iso()}
        out.append(clean)
    return out


def _employee_fee_updates(old_rows,new_rows,owned_numbers,user_name):
    old_rows=old_rows if isinstance(old_rows,list) else []; new_rows=new_rows if isinstance(new_rows,list) else []
    old_by={str(row.get("id")):row for row in old_rows if isinstance(row,dict) and row.get("id") is not None}
    new_by={str(row.get("id")):row for row in new_rows if isinstance(row,dict) and row.get("id") is not None}
    out=[]
    for fee_id,prior in old_by.items():
        submitted=new_by.get(fee_id)
        if submitted is None: out.append(prior); continue
        if str(prior.get("number","")) not in owned_numbers:
            if not _json_equal(_redact_tree(prior),_redact_tree(submitted)): raise PermissionError("fee_not_owned")
            out.append(prior); continue
        old_safe=_redact_tree(prior); new_safe=_redact_tree(submitted)
        editable={"status","confirmBy","confirmAt"}
        if {k:v for k,v in old_safe.items() if k not in editable}!={k:v for k,v in new_safe.items() if k not in editable}:
            raise PermissionError("fee_field_forbidden")
        status=str(submitted.get("status",prior.get("status","待确认")))
        if status not in {"待确认","已确认","有差异"}: raise ValueError("invalid_fee_status")
        merged=dict(prior); merged["status"]=status
        if status != prior.get("status"):
            merged["confirmBy"]=user_name; merged["confirmAt"]=now_iso()
        out.append(merged)
    for fee_id in set(new_by)-set(old_by):
        raise PermissionError("fee_creation_forbidden")
    return out


def _decode_state(payload: str) -> dict[str, str]:
    try:
        state = json.loads(payload)
    except (TypeError, json.JSONDecodeError):
        state = {}
    if not isinstance(state, dict):
        state = {}
    return {key: state.get(key, "[]") for key in STATE_KEYS}


def _ordinary_waybill(value):
    """Manufacturer freight is confined to the reconciliation surface."""
    if not isinstance(value, dict): return value
    return {k:value[k] for k in _ORDINARY_WAYBILL_FIELDS if k in value and isinstance(value[k], (str, int, float, bool, type(None))) and not (isinstance(value[k],str) and _v3_output_has_explicit_price(value[k]))}


def _visible_state(state: dict[str, str], role: str, principal_id: str | None = None) -> dict[str, str]:
    result = {}
    suppressed = _module_state_keys()
    own_numbers=None
    if role=="employee" and principal_id:
        own_numbers=_employee_owned_numbers(json.loads(state.get(STATE_KEYS[0],"[]")),principal_id)
    for key in STATE_KEYS:
        try: value = json.loads(state[key])
        except Exception: value = []
        if key == STATE_KEYS[0] and isinstance(value,list):
            value = [_ordinary_waybill(item) for item in value]
        if role != "owner_finance":
            if role == "dispatcher" and key == STATE_KEYS[4] and isinstance(value, list):
                # 费用域：调度看得见每笔待批金额（厂家运费类行除外），其余照旧脱敏。
                value = [_redact_state_fee_row(item) for item in value]
            else:
                value = _redact_tree(value)
            # These V9 records contain amounts embedded in free-form invoice rows,
            # reconciliation explanations, or monthly totals. Hide the domains
            # from non-finance principals instead of relying on text scrubbing.
            if key in {STATE_KEYS[3],STATE_KEYS[6],STATE_KEYS[7],STATE_KEYS[9]}:
                value=[]
        if own_numbers is not None and key in {STATE_KEYS[0],STATE_KEYS[4],STATE_KEYS[5]}:
            value=[x for x in value if isinstance(x,dict) and str(x.get("number","")) in own_numbers]
        if key in suppressed:
            # 模块没启用，这条业务域不属于本企业：返回空域，不泄露记录存在。
            value = []
        result[key] = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return result


def _authenticated_persona(*, tenant_id, principal_id):
    with db() as con:
        row = con.execute("SELECT role FROM users WHERE id=? AND tenant_id=? AND active=1", (principal_id, tenant_id)).fetchone()
    names = {"owner_finance": "01-老板-私人商业幕僚.md", "dispatcher": "02-管理层-高能力执行助理.md", "employee": "03-员工-超级靠谱的同事.md"}
    if not row or row["role"] not in names:
        raise PermissionError("authenticated_role_required")
    folder = ROOT / "ai" / "persona"
    core = (folder / "00-统一人格内核.md").read_text(encoding="utf-8")
    role = (folder / names[row["role"]]).read_text(encoding="utf-8")
    # Examples illustrate tone only; names and fictitious outcomes must never
    # become facts or stock replies in a live conversation.
    role = re.sub(r"## 角色示例（仅展示语气，不能当作真实业务结果）.*?(?=\n## 特别注意)", "", role, flags=re.S)
    role = role.replace('例如配置为“孙总”时自然称呼', '以已认证账号或用户偏好为准')
    with db() as con:person=con.execute('SELECT * FROM users WHERE id=? AND tenant_id=?',(principal_id,tenant_id)).fetchone()
    from zoneinfo import ZoneInfo
    info={'name':person['name'],'preferences':agent_runtime.preferences(sys.modules[__name__],dict(person)),'current_time':datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(timespec='seconds'),'timezone':'Asia/Shanghai'}
    # 带上当前 Bot 的任务态（干到哪、催到哪），让下一轮对话能「接着上次干」。
    bot_state_text = ""
    try:
        pending = agent_routine.get_bot_state(sys.modules[__name__], dict(person), include_done=True)
        if pending:
            bot_state_text = "\n\n你最近的任务状态（历史扫描可用 get_daily_routine_scan 按 scan_id 读取；状态可能变化）：" + json.dumps(pending, ensure_ascii=False)
    except Exception:
        pass
    return "当前企业："+PRODUCT["name"]+"；行业："+PRODUCT["industry"]+"。\n"+core + "\n\n" + role + "\n当前本人信息："+json.dumps(info,ensure_ascii=False)+bot_state_text+"\n称呼使用已认证账号或本人明确偏好。你可以按需要使用list_capabilities/read_skill发现和阅读技能；技能是专业方法，不是固定话术。涉及用户偏好、以往经验或企业规定时，主动按需使用search_workplace_memory寻找相关信息并核实适用范围和时效；候选不是已确认的事实。业务的实际状态以业务记录为准。\n\n记忆是活的：每轮对话结束前，主动判断这轮有没有值得沉淀的东西，有就自己记下来，不等用户开口——① 用户纠正了你（叫法、事实、规则、偏好），把更正后的结论记下，下次不再犯；② 你这次干成了一件活、发现了一条新的企业规则或口径，把结论记下，下次能接上。沉淀用 save_workplace_memory（每次必须显式给 action，不能省略；记住的用 note 动作，无需版本号；正式规则才用 save 并带 expected_version；撤回用 forget）。守住底线：不存密码/密钥/API key/私钥，不存凭证明文，不存业务账副本；涉钱的事实只记结论和口径，不记原文明细；个人隐私不共享；未经确认的信息只当线索(kind=lead)记，绝不升级成规则。没有值得记的就不记，别为记而记。\n\n"+ ("你是会主动干活的 Bot，不是问一句答一句。老板/管理层打开工作台、或问「今天该盯什么」「有什么要处理的」「最近要办什么」时，主动调用 run_daily_routine 扫一遍今天该盯的事（对账差异＋回单催办），用大白话把结果汇报出来：先一句总数，再挑最要紧的几条说清是哪票、什么问题、要谁确认。结果是候选不是结论，涉钱的差异必须有人确认，你只报不替人拍板。汇报完把「扫到哪、催到哪」用 save_workplace_memory 的 note 动作记进工作记忆，下次接得上。"
       if product_profile.industry_pack(PRODUCT["industry"])["business_tools"] else
       "你是会主动干活的 Bot，不是问一句答一句。本企业未启用行业业务工具包，只有通用能力（记忆、任务、文件、表格分析、定时跟进、团队协作）。老板或管理层问「今天该盯什么」时，用现有通用能力核对真实工作任务与已记规则，如实说明哪些业务数据尚未接入，不虚构行业结果。")


class WorkbenchContext:
    authenticated_persona = staticmethod(_authenticated_persona)
    """Read-only evidence adapter; every lookup is scoped to the logged-in enterprise."""
    def retrieve(self, query, *, tenant_id, principal_id, memory_scope):
        if tenant_id != TENANT_ID: return []
        with db() as con:
            user=con.execute("SELECT id,name,role,tenant_id FROM users WHERE id=? AND tenant_id=? AND active=1",(principal_id,tenant_id)).fetchone()
            row=con.execute("SELECT payload FROM work_state WHERE id=1").fetchone()
            today=datetime.now().date().isoformat()
            # Active enterprise rules are always available to both chat
            # scopes; finance-only rules are filtered below by role.
            rules=con.execute("SELECT r.* FROM business_rules r JOIN (SELECT rule_key,MAX(version) version FROM business_rules WHERE tenant_id=? AND effective_at<=? GROUP BY rule_key) c ON c.rule_key=r.rule_key AND c.version=r.version WHERE r.tenant_id=? ORDER BY r.layer,r.rule_key LIMIT 40",(tenant_id,today,tenant_id)).fetchall()
            memories=con.execute("SELECT * FROM long_term_memories WHERE tenant_id=? AND active=1 AND (scope='enterprise' OR (scope='personal' AND principal_id=? AND ?='personal')) ORDER BY created_at DESC LIMIT 100",(tenant_id,principal_id,memory_scope)).fetchall()
            if not user: return []
            workplace_rows=_workplace_memory_rows(con,tenant_id,principal_id,user["role"],memory_scope,allow_finance=(user["role"]=="owner_finance"))
        if not user or not row: return []
        state=_visible_state(_decode_state(row["payload"]),user["role"],principal_id)
        query_text=str(query or "")
        query_features=_retrieval_features(query_text)
        preference_intent=memory_scope=="personal" and _personal_preference_query(query_text)
        has_business_intent=any(term in query_text for term in _BUSINESS_QUERY_TERMS)
        suppress_state_evidence=preference_intent and not has_business_intent
        if user["role"] == "employee":
            try:
                own_docs=[x for x in json.loads(state[STATE_KEYS[0]]) if isinstance(x,dict) and _case_allowed(x,principal_id=principal_id)]
                own_numbers={str(x.get("number")) for x in own_docs if x.get("number") is not None}
                state[STATE_KEYS[0]]=json.dumps(own_docs,ensure_ascii=False,separators=(",",":"))
                for key in (STATE_KEYS[4],STATE_KEYS[5]):
                    rows=json.loads(state[key])
                    state[key]=json.dumps([x for x in rows if isinstance(x,dict) and str(x.get("number","")) in own_numbers],ensure_ascii=False,separators=(",",":"))
            except Exception:
                state[STATE_KEYS[0]]=state[STATE_KEYS[4]]=state[STATE_KEYS[5]]="[]"
        from ai.service import Evidence
        found=[]
        for key in (() if suppress_state_evidence else (STATE_KEYS[0],STATE_KEYS[1],STATE_KEYS[2],STATE_KEYS[4],STATE_KEYS[5],STATE_KEYS[8])):
            try: entries=json.loads(state[key])
            except Exception: continue
            if not isinstance(entries,list): continue
            for ix,item in enumerate(entries):
                if not isinstance(item,dict): continue
                visible=item if user["role"]=="owner_finance" else _redact_tree(item)
                if key==STATE_KEYS[0]:
                    visible=_ordinary_waybill(visible)
                    visible=WorkbenchTools._waybill_with_units(visible)
                    if user["role"]!="owner_finance": visible.pop("created_by",None)
                hay=json.dumps(visible,ensure_ascii=False,sort_keys=True).lower()
                record_number=str(item.get("number") or "").casefold()
                explicitly_named=bool(record_number and record_number in query_text.casefold())
                score=len(query_features & _retrieval_features(hay))
                if explicitly_named or score >= min(2,max(1,len(query_features))):
                    label=item.get("number") or item.get("id") or str(ix+1)
                    text=json.dumps(visible,ensure_ascii=False,sort_keys=True)
                    found.append((score, Evidence(
                        f"{key}:{label}",text,key,{"record_id":str(label),"tenant_id":tenant_id})))
        # Business memory follows the explicit chat scope. Enterprise requests
        # see shared memories only; personal requests see only this principal's
        # personal memories. The two scopes are never merged implicitly.
        from capabilities import workplace_memory as wm_policy
        for rule in rules:
            if user["role"]!="owner_finance" and (rule["finance_only"] or wm_policy.sensitive(sys.modules[__name__],str(rule["title"])+" "+str(rule["content"]))): continue
            title=rule["title"] if user["role"]=="owner_finance" else _redact_tree({"title":rule["title"]})["title"]
            content=rule["content"] if user["role"]=="owner_finance" else _redact_tree({"content":rule["content"]})["content"]
            found.append((1000,Evidence(f"rule:{rule['rule_key']}:v{rule['version']}",
                f"[{rule['layer']}] {title}: {content}","business_rule",
                {"rule_key":rule["rule_key"],"version":rule["version"],"effective_at":rule["effective_at"]})))
        for memory in memories:
            if user["role"]!="owner_finance" and (memory["finance_only"] or wm_policy.sensitive(sys.modules[__name__],str(memory["title"])+" "+str(memory["content"]))): continue
            title=memory["title"] if user["role"]=="owner_finance" else _redact_tree({"title":memory["title"]})["title"]
            content=memory["content"] if user["role"]=="owner_finance" else _redact_tree({"content":memory["content"]})["content"]
            text=f"{title}: {content}"
            score=len(query_features & _retrieval_features(text))
            if score==0 and preference_intent and _preference_memory(memory):
                # Preference questions commonly use synonyms absent from the
                # stored wording. In explicit personal scope, a titled
                # preference/communication memory is a safe fallback.
                score=1
            if score:
                found.append((score,Evidence(f"memory:{memory['id']}",text,"business_memory",
                    {"scope":memory["scope"],"created_at":memory["created_at"]})))
        for wm in workplace_rows:
            if wm["scope"]=="personal" and wm["owner_id"]!=principal_id: continue
            text=f"{wm['topic']}: {wm['title']}: {wm['content']}"
            score=len(query_features & _retrieval_features(text))
            if score or (preference_intent and wm["kind"] in ("preference","experience")):
                found.append((score,Evidence(f"workplace_memory:{wm['id']}",text,"business_memory",
                    {"scope":wm["scope"],"kind":wm["kind"],"created_at":wm["created_at"]})))
        found.sort(key=lambda row:row[0],reverse=True)
        return [item for _,item in found[:20]]


class WorkbenchTools:
    supports_idempotency = True

    def _principal(self, tenant_id, principal_id):
        if tenant_id != TENANT_ID: return None
        with db() as con:
            return con.execute("SELECT * FROM users WHERE id=? AND tenant_id=? AND active=1",(principal_id,tenant_id)).fetchone()

    def list_tools(self, *, tenant_id, principal_id):
        user=self._principal(tenant_id,principal_id)
        if not user: return []
        functions=[
            {"type":"function","function":{"name":"search_waybills","description":"按编号、收货人、到站或货物查找运单","parameters":{"type":"object","properties":{"query":{"type":"string","maxLength":200}},"required":["query"]}}},
            {"type":"function","function":{"name":"get_waybill","description":"查看一票运单基础字段；重量单位为吨。查询该票费用、回单、待办与系统提示请用 get_waybill_business_status。员工只能查询本人负责的运单。","parameters":{"type":"object","properties":{"waybill_number":{"type":"string","maxLength":40}},"required":["waybill_number"]}}},
            {"type":"function","function":{"name":"get_waybill_business_status","description":"确定性查询一票真实运单及关联费用状态、回单状态、待办和系统提示。财务字段按当前账号授权范围由服务端返回；员工仅可查询本人运单且不含金额。重量单位为吨。所有事实依据应引用返回证据ID。","parameters":{"type":"object","properties":{"waybill_number":{"type":"string","maxLength":40}},"required":["waybill_number"]}}},
            {"type":"function","function":{"name":"update_receipt_status","description":"记录已核实的回单事件。先用 get_waybill_business_status 获取 state_rev，原样作为 expected_rev；active=false 撤销错误标记。","parameters":{"type":"object","properties":{"waybill_number":{"type":"string","maxLength":80},"field":{"type":"string","enum":list(receipt_state.FIELDS)},"active":{"type":"boolean"},"expected_rev":{"type":"integer","minimum":0}},"required":["waybill_number","field","active","expected_rev"],"additionalProperties":False}}},
            {"type":"function","function":{"name":"generate_waybill_report","description":"按用户明确指定的真实运单号生成本地运单清单文件；只生成CSV或Markdown并保存到当前企业本地存储，不外发。员工只能生成本人运单，非财务角色文件不含金额。","parameters":{"type":"object","properties":{"waybill_numbers":{"type":"array","items":{"type":"string","maxLength":40},"minItems":1,"maxItems":100},"format":{"type":"string","enum":["csv","markdown"]}},"required":["waybill_numbers"]}}},
        ]
        if user["role"] in {"dispatcher","owner_finance"}:
            functions.append({"type":"function","function":{"name":"create_task","description":"创建工作任务。只有用户明确要求跟踪结构化业务结果时才设置 completion_condition；不从备注或含糊描述推断。","parameters":{"type":"object","properties":{"title":{"type":"string","maxLength":160},"description":{"type":"string","maxLength":4000},"assignee_id":{"type":["string","null"]},"due_at":{"type":["string","null"]},"waybill_number":{"type":["string","null"],"description":"设置自动完成条件时必须提供真实关联运单号"},"completion_condition":{"type":["string","null"],"enum":["receipt_delivered","fees_confirmed",None],"description":"receipt_delivered: 该票厂家原件回单已交付；fees_confirmed: 该票全部非作废费用均已确认；省略/null表示手动任务"}},"required":["title"]}}})
        task_properties={"task_id":{"type":"string"},"title":{"type":"string"},"description":{"type":"string"},"assignee_id":{"type":["string","null"]},"due_at":{"type":["string","null"]},"waybill_number":{"type":["string","null"]},"status":{"type":"string","enum":["open","in_progress","done","cancelled"]}}
        if user["role"] in {"dispatcher","owner_finance"}:
            task_properties["completion_condition"]={"type":["string","null"],"enum":["receipt_delivered","fees_confirmed",None],"description":"仅用户明确提出结构化完成条件时设置；否则保持 null 的手动任务。设置后需绑定真实运单号。"}
        functions.append({"type":"function","function":{"name":"update_task","description":"更新任务字段；员工只能更新分配给自己的任务状态，任务管理员可变更结构化完成条件。不要根据备注猜测完成状态。","parameters":{"type":"object","properties":task_properties,"required":["task_id"]}}})
        if user["role"]=="owner_finance":
            functions.append({"type":"function","function":{"name":"create_business_rule_version","description":"仅按owner明确要求新增一条企业规则版本；规则不可覆盖旧版本。不要将聊天建议自动保存为规则。rule_key仅允许小写字母、数字、点、下划线和连字符，且必须以小写字母或数字开头。","parameters":{"type":"object","properties":{"rule_key":{"type":"string","maxLength":80,"pattern":"^[a-z0-9][a-z0-9._-]{1,79}$","description":"小写英数开头；仅小写英数、点、下划线、连字符，长度2-80"},"layer":{"type":"string","enum":["foundation","calculation","conversation"]},"title":{"type":"string","maxLength":160},"content":{"type":"string","maxLength":8000},"effective_at":{"type":"string","description":"YYYY-MM-DD，可省略为今天"},"finance_only":{"type":"boolean"}},"required":["rule_key","layer","title","content"]}}})
        blocked = _blocked_tool_names()
        return [f for f in functions if f["function"]["name"] not in blocked]

    def execute(self, name, arguments, *, tenant_id, principal_id, idempotency_key):
        user=self._principal(tenant_id,principal_id)
        if not user: raise PermissionError("identity is not active")
        if name in _blocked_tool_names(): return {"error":"forbidden"}
        args=dict(arguments)
        if name in {"search_waybills","get_waybill"}:
            with db() as con:
                row=con.execute("SELECT payload FROM work_state WHERE id=1").fetchone()
                state=_business_state(row["payload"] if row else "{}")
            try: rows=json.loads(state[STATE_KEYS[0]])
            except Exception: rows=[]
            if user["role"]=="employee": rows=[x for x in rows if isinstance(x,dict) and _case_allowed(x,user)]
            if name=="get_waybill":
                number=str(args.get("waybill_number",""))
                matches=[x for x in rows if isinstance(x,dict) and str(x.get("number",""))==number]
            else:
                query=str(args.get("query",""))[:200].lower()
                terms=[x for x in re.findall(r"[\w-]+",query) if len(x)>1]
                matches=[x for x in rows if isinstance(x,dict) and all(t in json.dumps(_ordinary_waybill(_redact_tree(x) if user["role"]!="owner_finance" else x),ensure_ascii=False).lower() for t in terms)] if terms else []
            safe=[]
            for item in matches[:10]:
                visible=self._waybill_with_units(_ordinary_waybill(item if user["role"]=="owner_finance" else _redact_tree(item)))
                if user["role"]!="owner_finance": visible.pop("created_by",None)
                safe.append(visible)
            return {"items":safe,"count":len(safe)}
        if name=="get_waybill_business_status":
            number=str(args.get("waybill_number","")).strip()[:40]
            if not number: raise ValueError("waybill_number required")
            with db() as con:
                con.execute("BEGIN IMMEDIATE")
                state_row=con.execute("SELECT rev,payload FROM work_state WHERE id=1").fetchone()
                if not state_row:
                    con.rollback(); raise LookupError("not_found")
                state=_business_state(state_row["payload"])
                try: docs=json.loads(state[STATE_KEYS[0]])
                except Exception: docs=[]
                ticket=next((x for x in docs if isinstance(x,dict) and str(x.get("number",""))==number),None)
                if not ticket:
                    audit(con,user,"ai.tool.read_denied","waybill",hashlib.sha256(number.encode()).hexdigest()[:16],{"tool":"get_waybill_business_status","reason":"not_registered"})
                    con.commit(); return {"error":"not_found","record_state":"not_registered","fact":"系统里没有这票的登记记录"}
                if user["role"]=="employee" and not _case_allowed(ticket,user):
                    audit(con,user,"ai.tool.read_denied","waybill",hashlib.sha256(number.encode()).hexdigest()[:16],{"tool":"get_waybill_business_status","reason":"not_authorized"})
                    con.commit(); return {"error":"not_found_or_not_authorized","record_state":"unknown","fact":"该查询未获授权或记录不在本人名下"}
                try: receipts=json.loads(state[STATE_KEYS[5]])
                except Exception: receipts=[]
                try: fees=json.loads(state[STATE_KEYS[4]])
                except Exception: fees=[]
                receipt=next((x for x in receipts if isinstance(x,dict) and str(x.get("number",""))==number),None)
                ticket_fees=[x for x in fees if isinstance(x,dict) and str(x.get("number",""))==number]
                fields = V3Context._FEE_FIELDS + (("id",) if user["role"] != "employee" else ())
                ticket_fees = [V3Context._pick(x, fields) for x in ticket_fees
                               if not V3Context._protected_freight_type(x.get("type"))]
                if user["role"]!="owner_finance" and receipt is not None: receipt=_redact_tree(receipt)
                if user["role"]=="employee":
                    tasks=[dict(x) for x in con.execute("SELECT * FROM tasks WHERE waybill_number=? AND assignee_id=? ORDER BY created_at DESC",(number,user["id"]))]
                else:
                    tasks=[dict(x) for x in con.execute("SELECT * FROM tasks WHERE waybill_number=? ORDER BY created_at DESC",(number,))]
                if user["role"]!="owner_finance": tasks=[_redact_tree(x) for x in tasks]
                if user["role"]!="owner_finance":
                    safe_ticket=_ordinary_waybill(_redact_tree(ticket))
                    safe_ticket.pop("created_by",None)
                    for task in tasks:
                        task.pop("created_by",None); task.pop("assignee_id",None)
                else: safe_ticket=_ordinary_waybill(ticket)
                con.execute("INSERT INTO audit(at,user_id,user_name,action,entity_type,entity_id,details) VALUES(?,?,?,?,?,?,?)",
                    (now_iso(),user["id"],user["name"],"ai.tool.read_waybill_status","waybill",number,
                     json.dumps({"domains":["waybill","fees","receipts","tasks","inspection"],"state_rev":state_row["rev"],"finance_visible":user["role"]=="owner_finance"},ensure_ascii=False,separators=(",",":"))))
                con.commit()
            safe_ticket=self._waybill_with_units(safe_ticket)
            inspection=inspect_business(user,snapshot=state,tasks_snapshot=tasks)
            issues=[x for x in inspection["issues"] if x.get("waybill_number")==number]
            result={"waybill":safe_ticket,"fees":ticket_fees,"receipt":receipt,"tasks":tasks,
                    "inspection":{"issues":issues},"state_rev":state_row["rev"]}
            text=json.dumps(result,ensure_ascii=False,separators=(",",":"))
            result["evidence"]=[{"evidence_id":f"waybill-status:{number}:r{state_row['rev']}","text":text,
                                 "source":"waybill_business_status","metadata":{"waybill_number":number,"state_rev":state_row["rev"],"finance_visible":user["role"]=="owner_finance"}}]
            return result
        if name=="update_receipt_status":
            try: return update_receipt_status_record(user,args,idempotency_key)
            except PermissionError: return {"error":"forbidden"}
            except LookupError: return {"error":"not_found_or_not_authorized"}
            except ValueError: return {"error":"invalid_request"}
        if name=="update_fee_status":
            try: return update_fee_status_record(user,args,idempotency_key)
            except PermissionError: return {"error":"forbidden"}
            except LookupError: return {"error":"not_found_or_not_authorized"}
            except ValueError: return {"error":"invalid_request"}
        if name=="create_business_rule_version":
            try: return create_business_rule_version_record(user,args,idempotency_key)
            except PermissionError: return {"error":"forbidden"}
            except ValueError: return {"error":"invalid_request"}
        if name=="generate_waybill_report":
            try: return generate_waybill_report_record(user,args,idempotency_key)
            except PermissionError: return {"error":"forbidden"}
            except LookupError: return {"error":"not_found_or_not_authorized"}
            except ValueError: return {"error":"invalid_request"}
        if name=="create_task":
            if user["role"] not in {"dispatcher","owner_finance"}: return {"error":"forbidden"}
            try:
                status,response=create_task_record(user,args,idempotency_key)
                return response
            except PermissionError: return {"error":"forbidden"}
            except ValueError: return {"error":"invalid_request"}
        if name=="update_task":
            task_id=str(args.pop("task_id",""))
            if not task_id: return {"error":"invalid_request"}
            try:
                _status,response=update_task_record(user,task_id,args,idempotency_key)
                return response
            except LookupError: return {"error":"not_found_or_not_authorized"}
            except PermissionError: return {"error":"forbidden"}
            except ValueError: return {"error":"invalid_request"}
        raise PermissionError("tool not available")

    @staticmethod
    def _waybill_with_units(item):
        result=dict(item)
        if result.get("weight") not in (None,""):
            try:
                weight=float(result["weight"])
                result["weight_unit"]="吨"
                result["weight_display"]=(f"{weight:g} 吨")
            except (TypeError,ValueError):
                result["weight_unit"]="吨"
        return result


class V3Context:
    authenticated_persona = staticmethod(_authenticated_persona)
    """V3 uses a strict, non-financial projection of the primary work ledger."""
    _WAYBILL_FIELDS = ("number", "date", "receiver", "phone", "address", "destination", "cargo", "qty", "weight", "carrier")
    _FEE_FIELDS = ("number", "type", "status", "at")
    _RECEIPT_FIELDS = ("number", "photo", "paper", "delivered", "supplement", "at", "off", "offAt")

    @staticmethod
    def _protected_freight_type(value):
        return bool(re.search(r"厂家.{0,8}(?:运费|运价|底价)|(?:运费|运价|底价).{0,8}厂家",str(value or "")))

    @staticmethod
    def _pick(row, fields):
        return {key: row[key] for key in fields if key in row and isinstance(row[key], (str, int, float, bool)) and row[key] != "" and not (fields == V3Context._WAYBILL_FIELDS and isinstance(row[key],str) and _v3_output_has_explicit_price(row[key]))}

    @staticmethod
    def _receipt_view(row):
        if not isinstance(row,dict): return None
        result={"number":row.get("number")} if row.get("number") not in (None,"") else {}
        for key in ("photo","paper","delivered","supplement"):
            marker=row.get(key)
            if not marker: continue
            if isinstance(marker,dict):
                result[key]={"status":"已撤销" if marker.get("off") else "已登记"}
                if marker.get("off"):result[key]["meaning"]="当前登记已撤销，不代表相关事实从未发生；历史情况需另查依据。"
                at=marker.get("offAt") if marker.get("off") else marker.get("at")
                if at: result[key]["at"]=at
            elif receipt_state.active(marker): result[key]={"status":"已登记"}
        return result

    def retrieve(self, query, *, tenant_id, principal_id, memory_scope):
        if tenant_id != TENANT_ID: return []
        with db() as con:
            user=con.execute("SELECT id,name,role,tenant_id FROM users WHERE id=? AND tenant_id=? AND active=1",(principal_id,tenant_id)).fetchone()
            state_row=con.execute("SELECT payload,rev FROM work_state WHERE id=1").fetchone()
            rules=con.execute("SELECT r.* FROM business_rules r JOIN (SELECT rule_key,MAX(version) version FROM business_rules WHERE tenant_id=? AND effective_at<=? GROUP BY rule_key) c ON c.rule_key=r.rule_key AND c.version=r.version WHERE r.tenant_id=? ORDER BY r.layer,r.rule_key LIMIT 40",(tenant_id,datetime.now().date().isoformat(),tenant_id)).fetchall()
            memories=con.execute("SELECT * FROM long_term_memories WHERE tenant_id=? AND active=1 AND ((scope='enterprise') OR (scope='personal' AND principal_id=? AND ?='personal')) ORDER BY created_at DESC LIMIT 100",(tenant_id,principal_id,memory_scope)).fetchall()
            if not user: return []
            workplace_rows=_workplace_memory_rows(con,tenant_id,principal_id,user["role"],memory_scope)
        if not user or not state_row: return []
        state=_business_state(state_row["payload"])
        try: waybills=json.loads(state[STATE_KEYS[0]])
        except Exception: waybills=[]
        try: fees=json.loads(state[STATE_KEYS[4]])
        except Exception: fees=[]
        try: receipts=json.loads(state[STATE_KEYS[5]])
        except Exception: receipts=[]
        if user["role"]=="employee":
            waybills=[x for x in waybills if isinstance(x,dict) and _case_allowed(x,principal_id=principal_id)]
        query_text=str(query or "").casefold()
        from ai.agent_policy import query_terms, relevance
        tokens=query_terms(query_text)
        found=[]
        from ai.service import Evidence
        for ticket in waybills:
            number=str(ticket.get("number", ""))
            projected=self._pick(ticket,self._WAYBILL_FIELDS)
            if projected.get("weight") not in (None,""): projected["weight_unit"]="吨"
            associated_fees=[self._pick(x,self._FEE_FIELDS) for x in fees if isinstance(x,dict) and str(x.get("number",""))==number and not self._protected_freight_type(x.get("type"))]
            receipt=next((x for x in receipts if isinstance(x,dict) and str(x.get("number",""))==number),None)
            projected_record={"waybill":projected,"fees":associated_fees,"receipt":self._receipt_view(receipt)}
            searchable=json.dumps(projected_record,ensure_ascii=False,sort_keys=True).casefold()
            score=relevance(tokens,searchable)
            if (number and re.search(r"(?<![0-9A-Za-z_-])"+re.escape(number.casefold())+r"(?![0-9A-Za-z_-])",query_text)) or score>=2:
                text=json.dumps(projected_record,ensure_ascii=False,separators=(",",":"))
                found.append(Evidence(f"v3:waybill:{number}:r{state_row['rev']}",text,"waybill_case",{"waybill_number":number,"state_rev":state_row["rev"]}))
        from capabilities import workplace_memory as wm_policy
        for rule in rules:
            if rule["finance_only"] or wm_policy.sensitive(sys.modules[__name__],str(rule["title"])+" "+str(rule["content"])): continue
            found.append(Evidence(f"rule:{rule['rule_key']}:v{rule['version']}",f"[{rule['layer']}] {rule['title']}: {rule['content']}","business_rule",{"rule_key":rule["rule_key"],"version":rule["version"]}))
        for memory in memories:
            if memory["finance_only"] or wm_policy.sensitive(sys.modules[__name__],str(memory["title"])+" "+str(memory["content"])): continue
            # Enterprise scope sees shared policy only; personal scope additionally sees this user's preferences.
            if memory["scope"]=="personal" and memory_scope!="personal": continue
            text=f"{memory['title']}: {memory['content']}"
            score=relevance(tokens,text)
            if score or (memory_scope=="personal" and any(word in query_text for word in ("偏好","习惯","喜欢","怎么称呼","表达方式"))):
                found.append(Evidence(f"memory:{memory['id']}",text,"business_memory",{"scope":memory["scope"]}))
        for wm in workplace_rows:
            # 工作记忆（company）全员可读；个人记忆（personal）只本人可读。涉钱明细则不进普通检索。
            text=f"{wm['topic']}: {wm['title']}: {wm['content']}"
            if wm["scope"]=="personal" and wm["owner_id"]!=principal_id: continue
            if re.search(r"厂家.{0,8}(?:运费|运价|底价|结算|费用|价格)|(?:运费|运价|底价).{0,8}厂家|运费金额",text): continue
            score=relevance(tokens,text)
            if score or (memory_scope=="personal" and any(word in query_text for word in ("偏好","习惯","喜欢","怎么称呼","表达方式"))):
                found.append(Evidence(f"workplace_memory:{wm['id']}",text,"business_memory",{"scope":wm["scope"],"kind":wm["kind"]}))
        # Reserve room for relevant evidence and current rules independently.
        rules_found=[e for e in found if e.source=="business_rule"]
        other=[e for e in found if e.source!="business_rule"]
        other.sort(key=lambda e:relevance(tokens,e.text),reverse=True)
        rules_found.sort(key=lambda e:relevance(tokens,e.text),reverse=True)
        selected=rules_found[:8]+other[:12]
        return (selected+rules_found[8:]+other[12:])[:20]


def _v3_operational_summary(user,*,record_audit=True):
    return logistics_read._v3_operational_summary(sys.modules[__name__],user,record_audit=record_audit)


def _v3_recent_changes(user,arguments,*,record_audit=True):
    return logistics_read._v3_recent_changes(sys.modules[__name__],user,arguments,record_audit=record_audit)


def _v3_raw_rows(domain=0):
    """从主账本读原始记录；老 AI 的脱敏副本不参与，财务字段只在服务端这一层补。"""
    with db() as con:
        row=con.execute("SELECT payload FROM work_state WHERE id=1").fetchone()
    state=_business_state(row["payload"] if row else "{}")
    try: return [x for x in json.loads(state[STATE_KEYS[domain]]) if isinstance(x,dict)]
    except (TypeError,ValueError,KeyError,IndexError): return []


def _v3_add_finance(target, raw, user, *, note=True):
    """按账号授权范围补财务字段。财务口径尚未确认，只返回已登记数值，不计算毛利。"""
    if not _module_enabled("finance") or _finance_scope(user)=="none" or not isinstance(raw,dict) or not isinstance(target,dict): return target
    for key in _FINANCE_FIELDS:
        value=raw.get(key)
        if value not in (None,"",0,"0"): target[key]=value
    if note and any(k in target for k in _FINANCE_FIELDS):
        target["finance_scope"]=_finance_scope(user)
        target["finance_note"]="这些金额来自登记记录，不能单凭它们判断最终利润。"
    return target


class V3Tools:
    """Authorized V3 business tools; financial fields are projected by server-side scope."""
    supports_idempotency=True
    # Only transactional idempotency or safely repeatable reads can resume
    # a running journal event after an uncertain interruption.
    recovery_safe_tools=frozenset({
        "list_waybills","get_waybill_history","get_recent_changes","get_operational_summary",
        "get_daily_routine_scan","review_business_records","list_capabilities","read_skill",
        "find_records","search_workplace_memory","search_company_knowledge","list_colleagues",
        "get_case_workspace","get_agent_usage","get_agent_health","analyze_table",
        "create_task","update_task","update_receipt_status","update_fee_status","send_message",
        "handoff_case","share_case_file","publish_company_knowledge","manage_event_alerts",
        "commit_waybill_import","schedule_followup"
    })
    def __init__(self): self.base=WorkbenchTools()
    def recovery_permitted(self, event, request):
        if not event["job_id"]:
            return not os.environ.get("BOT_STRICT_PILOT", "").lower() in {"1","true","yes"}
        with db() as con:
            row=con.execute("SELECT state,cancel_requested FROM agent_jobs WHERE id=? AND tenant_id=? AND owner_id=?",
                            (event["job_id"],request.tenant_id,request.principal_id)).fetchone()
        return bool(row and not row["cancel_requested"] and row["state"]!="cancelled")

    def list_tools(self, *, tenant_id, principal_id):
        user=self.base._principal(tenant_id,principal_id)
        if not user: return []
        tools=[tool for tool in self.base.list_tools(tenant_id=tenant_id,principal_id=principal_id)
               if tool.get("function",{}).get("name")!="create_business_rule_version"]
        recent_schema={"type":"function","function":{"name":"get_recent_changes","description":"查询最近真实业务变化汇总（默认近7天）。老板和调度可看全企业非金额运营变化；员工只看本人运单及分配给本人的任务。用户问‘最近有什么变化/最近做了什么/变化汇总’时优先调用，不要拿当前状态冒充历史变化。","parameters":{"type":"object","properties":{"days":{"type":"integer","minimum":1,"maximum":30,"description":"回看天数，默认7"},"limit":{"type":"integer","minimum":1,"maximum":30}},"additionalProperties":False}}}
        summary_schema={"type":"function","function":{"name":"get_operational_summary","description":"查询当前业务运行汇总：运单数量、重量（吨）、回单进度、费用记录状态数量和待办状态数量，不包含任何金额。老板和调度看全企业；员工仅统计本人负责运单和分配给本人的任务。","parameters":{"type":"object","properties":{},"additionalProperties":False}}}
        list_schema={"type":"function","function":{"name":"list_waybills","description":"按账号权限列出运单；不需要事先知道运单号，可按日期或关键词过滤。结果分页，重量单位为吨。财务字段仅在账号授权范围内返回。","parameters":{"type":"object","properties":{"query":{"type":"string","maxLength":200},"date_from":{"type":"string","description":"YYYY-MM-DD"},"date_to":{"type":"string","description":"YYYY-MM-DD"},"limit":{"type":"integer","minimum":1,"maximum":50},"offset":{"type":"integer","minimum":0}},"additionalProperties":False}}}
        review_schema={"type":"function","function":{"name":"review_business_records","description":"核对真实业务记录、材料与运单附件登记情况、费用归属及对账差异，查询当前要跟进的事情。费用类型不同不相减；缺合作方、承担方、币种时不自动认定结算一致。仅查询，不改账。外部采集未接入时明确返回实际覆盖状态。","parameters":{"type":"object","properties":{"waybill_number":{"type":"string","maxLength":80},"limit":{"type":"integer","minimum":1,"maximum":100},"offset":{"type":"integer","minimum":0}},"additionalProperties":False}}}
        history_schema={"type":"function","function":{"name":"get_waybill_history","description":"查运单自版本留存启用以来的实际修改记录、前后内容、时间和操作者，按当前权限过滤。","parameters":{"type":"object","properties":{"waybill_number":{"type":"string","maxLength":80}},"required":["waybill_number"],"additionalProperties":False}}}
        routine_schema={"type":"function","function":{"name":"run_daily_routine","description":"主动扫一遍当前业务状态，挑出今天该盯的事：对账差异（钱对不上或该归属没落账）和回单催办（该收的关键回单还没收）。纯读、不改账，结果是候选不是结论，涉钱差异需有人确认。老板打开工作台、或用户问‘今天该盯什么/有什么要处理’时主动调用。","parameters":{"type":"object","properties":{"limit":{"type":"integer","minimum":1,"maximum":50}},"additionalProperties":False}}}
        scan_schema={"type":"function","function":{"name":"get_daily_routine_scan","description":"按扫描 ID 查询本人历史扫描候选，结果重新核查当前访问权限，历史状态不代表现在。","parameters":{"type":"object","properties":{"scan_id":{"type":"string","maxLength":160}},"required":["scan_id"],"additionalProperties":False}}}
        capabilities = capability_catalog.tools()
        if user["role"] == "employee":
            capabilities = [tool for tool in capabilities
                            if tool["function"]["name"] not in {"update_fee_status", "send_message"}]
        extra = [recent_schema,summary_schema,list_schema,review_schema,history_schema,routine_schema,scan_schema]
        # 团队/协作工具里也混着物流专属工具（案卷交接、运单关联、运单导入），
        # 必须按同一张模块表过滤，不能因为来自 team 模块就漏掉。
        team_tools = agent_team.tools()
        blocked = _blocked_tool_names()
        def _keep(tool):
            return tool.get("function",{}).get("name") not in blocked
        return ([t for t in tools if _keep(t)] + [t for t in extra if _keep(t)]
                + [t for t in capabilities if _keep(t)] + [t for t in team_tools if _keep(t)])
    def execute(self, name, arguments, *, tenant_id, principal_id, idempotency_key):
        user=self.base._principal(tenant_id,principal_id)
        if not user: return {"error":"forbidden"}
        if name in _blocked_tool_names():
            # 模块没启用：即使模型硬报工具名也不执行，不按工具不存在报错以免泄露存在性。
            return {"error":"forbidden"}
        if _finance_scope(user)=="none" and _v3_output_has_explicit_price(json.dumps(arguments,ensure_ascii=False)):
            return {"error":"v3_financial_content_not_allowed"}
        user=dict(user)
        if name=="update_fee_status":
            try: return update_fee_status_record(user,arguments,idempotency_key)
            except PermissionError: return {"error":"forbidden"}
            except LookupError: return {"error":"not_found_or_not_authorized"}
            except ValueError: return {"error":"invalid_request"}
        team_result=agent_team.execute(sys.modules[__name__],user,name,arguments,idempotency_key)
        if team_result is not None:return team_result
        capability=agent_runtime.execute(sys.modules[__name__],user,name,arguments,idempotency_key)
        if capability is not None:return capability
        if name in {"review_business_records","get_waybill_history"}:
            allowed={"waybill_number","limit","offset"} if name=="review_business_records" else {"waybill_number"}
            if not isinstance(arguments,dict) or set(arguments)-allowed:return {"error":"invalid_request"}
            number=str(arguments.get("waybill_number", "")).strip()
            if len(number)>80:return {"error":"invalid_request"}
            if name=="get_waybill_history":
                if not number:return {"error":"invalid_request"}
                return integrity.ticket_history(sys.modules[__name__],user,number)
            try:limit=int(arguments.get("limit",30));offset=int(arguments.get("offset",0))
            except (ValueError,TypeError):return {"error":"invalid_request"}
            if not 1<=limit<=100 or offset<0:return {"error":"invalid_request"}
            result=integrity.review(sys.modules[__name__],user,number,limit,offset)
            with db() as con:audit(con,user,"ai.tool.business.review","waybill",number,{"state_rev":result["state_rev"],"finding_count":result["total"]})
            return result
        if name=="list_waybills":
            try:
                limit=int(arguments.get("limit",20)); offset=int(arguments.get("offset",0))
                if not 1<=limit<=50 or offset<0: raise ValueError()
                start=str(arguments.get("date_from", "")); end=str(arguments.get("date_to", ""))
                for value in (start,end):
                    if value: datetime.strptime(value,"%Y-%m-%d")
                query=str(arguments.get("query", "")).strip().casefold()
                if len(query)>200 or set(arguments)-{"query","date_from","date_to","limit","offset"}: raise ValueError()
            except (ValueError,TypeError): return {"error":"invalid_request"}
            with db() as con:
                row=con.execute("SELECT payload FROM work_state WHERE id=1").fetchone()
            state=_decode_state(row["payload"] if row else "{}")
            try: rows=json.loads(state[STATE_KEYS[0]])
            except (ValueError,TypeError,KeyError): return {"error":"state_unavailable"}
            items=[]
            for item in rows:
                if not isinstance(item,dict) or (user["role"]=="employee" and not _case_allowed(item,user)): continue
                day=str(item.get("date", ""))
                if (start and day<start) or (end and day>end): continue
                safe=V3Context._pick(item,V3Context._WAYBILL_FIELDS)
                if query and query not in json.dumps(safe,ensure_ascii=False).casefold(): continue
                if safe.get("weight") not in (None,""): safe["weight_unit"]="吨"
                items.append(safe)
            total=len(items); page=items[offset:offset+limit]
            return {"items":page,"count":len(page),"total":total,"offset":offset,"next_offset":offset+len(page) if offset+len(page)<total else None}
        if name=="get_recent_changes": return _v3_recent_changes(user,arguments)
        if name=="get_operational_summary": return _v3_operational_summary(user)
        if name=="get_daily_routine_scan":
            if not isinstance(arguments,dict) or set(arguments)!={"scan_id"} or not isinstance(arguments["scan_id"],str) or not 1<=len(arguments["scan_id"])<=160:
                return {"error":"invalid_request"}
            return agent_routine.get_scan(sys.modules[__name__],user,arguments["scan_id"]) or {"error":"not_found"}
        if name=="run_daily_routine":
            if not isinstance(arguments,dict) or set(arguments)-{"limit"}:return {"error":"invalid_request"}
            try:limit=int(arguments.get("limit",20))
            except (ValueError,TypeError):return {"error":"invalid_request"}
            if not 1<=limit<=50:return {"error":"invalid_request"}
            result=agent_routine.run(sys.modules[__name__],user,limit=limit)
            with db() as con:
                audit(con,user,"ai.tool.routine.run","routine","daily",{"candidates":result["total"],"by_routine":result["by_routine"],"state_rev":result["state_rev"]})
            return result
        if name=="generate_waybill_report" and user["role"]=="owner_finance":
            # Keep document generation while using the non-financial report schema.
            user["role"]="dispatcher"
            try: result=generate_waybill_report_record(user,dict(arguments),idempotency_key)
            except PermissionError: return {"error":"forbidden"}
            except LookupError: return {"error":"not_found_or_not_authorized"}
            except ValueError: return {"error":"invalid_request"}
        else:
            result=self.base.execute(name,arguments,tenant_id=tenant_id,principal_id=principal_id,idempotency_key=idempotency_key)
        if not isinstance(result,dict): return {"error":"invalid_result"}
        if result.get("error"):
            err={"error":str(result["error"])[:80]}
            for key in ("record_state","fact"):
                if key in result: err[key]=result[key]
            return err
        if name in {"search_waybills","get_waybill"}:
            items=result.get("items",[])
            safe=[V3Context._pick(item,V3Context._WAYBILL_FIELDS) for item in items if isinstance(item,dict)]
            for item in safe:
                if item.get("weight") not in (None,""): item["weight_unit"]="吨"
            return {"count":len(safe),"items":safe}
        if name=="get_waybill_business_status":
            waybill=result.get("waybill")
            if not isinstance(waybill,dict):
                return {"error":"not_found","record_state":"not_registered","fact":"系统里没有这票的登记记录"}
            number=str(waybill.get("number",""))
            raw_waybill=next((x for x in _v3_raw_rows(0) if str(x.get("number"))==number),{})
            safe_waybill=V3Context._pick(raw_waybill or waybill,V3Context._WAYBILL_FIELDS)
            safe_waybill["weight_unit"]="吨"
            safe_fees=[]
            for item in result.get("fees",[]):
                if not isinstance(item,dict): continue
                fee=V3Context._pick(item,V3Context._FEE_FIELDS)
                if V3Context._protected_freight_type(fee.get("type")): continue
                if user["role"] != "employee" and item.get("id"):
                    fee["fee_id"] = item["id"]
                safe_fees.append(fee)
            receipt=result.get("receipt")
            safe_receipt=V3Context._receipt_view(receipt)
            safe_tasks=[{k:item[k] for k in ("status","due_at","completion_condition") if k in item} for item in result.get("tasks",[]) if isinstance(item,dict)]
            issues=[]
            for issue in result.get("inspection",{}).get("issues",[]):
                if isinstance(issue,dict):
                    issues.append({k:issue[k] for k in ("code","severity","waybill_number") if k in issue})
            safe={"waybill":safe_waybill,"fees":safe_fees,"receipt":safe_receipt,"tasks":safe_tasks,"inspection":{"issues":issues},"state_rev":result.get("state_rev")}
            number=str(safe_waybill.get("number","")); evidence_id=f"v3:waybill:{number}:r{result.get('state_rev',0)}"
            safe["evidence"]=[{"evidence_id":evidence_id,"text":json.dumps(safe,ensure_ascii=False,separators=(",",":")),"source":"waybill_case","metadata":{"waybill_number":number,"state_rev":result.get("state_rev")}}]
            return safe
        if name=="update_receipt_status":
            return {k:result[k] for k in ("waybill_number","field","active","state_rev") if k in result}
        if name in {"create_task","update_task"}:
            item=result.get("item",result)
            safe={k:item[k] for k in ("id","status","due_at","waybill_number","completion_condition","completion_origin") if k in item}
            safe["assignee_id"] = item.get("assignee_id")
            return {"item":safe}
        if name=="generate_waybill_report":
            document=result.get("document",{})
            safe_doc={k:document[k] for k in ("id","filename","format","size_bytes","waybill_numbers","download_url","created_at") if k in document}
            evidence=[]
            for item in result.get("evidence",[]):
                if isinstance(item,dict):
                    evidence.append({"evidence_id":item.get("evidence_id"),"text":f"已生成不含金额的运单文件，包含 {len(safe_doc.get('waybill_numbers',[]))} 票。","source":"generated_document","metadata":{"document_id":safe_doc.get("id"),"format":safe_doc.get("format"),"waybill_count":len(safe_doc.get("waybill_numbers",[]))}})
            return {"document":safe_doc,"evidence":evidence}
        return {"error":"tool_not_available"}


def _v3_ai_service():
    global _V3_AI_SERVICE
    if _V3_AI_SERVICE is None:
        from ai.service import AIService
        from ai.agent_policy import ExecutionPolicy
        base=_ai_service()
        _V3_AI_SERVICE=AIService(base.store,V3Context(),V3Tools(),V3SafeModel(base.model_client) if base.model_client is not None else None,
                                 max_message_chars=base.max_message_chars,
                                 max_history_messages=base.max_history_messages,
                                 execution_policy=ExecutionPolicy.from_env())
    return _V3_AI_SERVICE


def _v3_safe_case(user, number):
    return logistics_read._v3_safe_case(sys.modules[__name__],user,number)


def _v3_shared_file_blocks(result,user):
    candidates=list(result.get('files',[])) if isinstance(result.get('files'),list) else []
    if isinstance(result.get('document'),dict):candidates.append(result['document'])
    blocks=[];seen=set()
    for doc in candidates:
        try:
            row=agent_files.authorized(sys.modules[__name__],user,doc['id'])
            if row['id'] in seen:continue
            seen.add(row['id']);blocks.append({'type':'file','label':row['filename'],'file_id':row['id']})
        except (KeyError,TypeError,LookupError):continue
    return blocks


def _v3_history_time(value):
    try: return datetime.fromtimestamp(float(value),timezone.utc).astimezone().isoformat(timespec="seconds")
    except (TypeError,ValueError,OSError): return ""


def _v3_new_conversation(user,scope):
    service=_v3_ai_service()
    cid=_V3_CONVERSATION_PREFIX+secrets.token_urlsafe(24)
    now=time.time()
    with service.store._connect() as con:
        con.execute("INSERT INTO conversations(id,tenant_id,principal_id,memory_scope,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                    (cid,str(user["tenant_id"]),str(user["id"]),scope,now,now))
    with db() as con:
        con.execute("INSERT INTO conversation_access VALUES(?,?)",(cid,user["role"]))
    return cid


def _v3_finance_request(text):
    text=str(text or "")
    finance_terms=("厂家运费","厂家运价","底价","运费金额","运费多少钱","运费多少","运费明细","厂家结算价","运费单价","结算金额","应付金额")
    return any(term in text for term in finance_terms) or _v3_output_has_explicit_price(text)


def _v3_output_has_explicit_price(text):
    # Context-aware guard: currency notation, or a number adjacent to explicit
    # monetary language. Ordinary quantities, weights, and waybill IDs remain valid.
    text=str(text or "")
    numeric=r"(?:\d+(?:\.\d+)?|[零〇一二三四五六七八九十百千万亿两]+)"
    # A count of fee records is not a monetary amount.  Remove only the
    # complete count phrase before checking; adjacent monetary expressions
    # remain subject to the guard.
    text=re.sub(r"费用(?:记录)?\s*[:：]?\s*"+numeric+r"\s*(?:条|笔|项)(?![\d元角分])", "费用记录若干条", text)
    money=r"(?:金额|价格|运费|运价|底价|成本|结算|费用|货款|价款|报价|freight|price|cost|amount)"
    connector=r"\s*(?:(?:为|是|约|共|合计)|[:：=]|人民币)*\s*"
    patterns=(r"(?:[¥￥]\s*\d|\d+(?:\.\d+)?\s*(?:元|人民币|块钱)|[零〇一二三四五六七八九十百千万亿两]+\s*(?:元|块|角|分))",
              money+connector+numeric+r"(?:\s*(?:元|人民币|块|角|分))?",
              numeric+r"\s*(?:元|人民币|块|角|分)?\s*(?:(?:是|为|的)|[:：=])*\s*"+money)
    return any(re.search(pattern,text,re.I) for pattern in patterns)


def _v3_case_blocks(evidence_ids,user=None):
    blocks=[]; seen=set()
    for evidence_id in evidence_ids:
        match=re.fullmatch(r"v3:waybill:([^:]{1,80}):r\d+",str(evidence_id))
        if match and match.group(1) not in seen and (user is None or _v3_safe_case(user,match.group(1)) is not None):
            seen.add(match.group(1)); blocks.append({"type":"case","case_id":match.group(1),"label":"查看运单案卷"})
    return blocks


def _v3_tool_evidence_ids(result):
    ids=[]
    if not isinstance(result,dict): return ids
    for item in result.get("evidence",[]):
        if isinstance(item,dict) and item.get("source")=="waybill_case": ids.append(item.get("evidence_id",""))
    return ids


_V3_PRICE_REFUSAL="这部分金额不在当前账号的查看范围内，我没有把它写进对话。"


class V3SafeModel:
    """Reject explicit price output before AIService can persist it."""
    def __init__(self,base): self.base=base
    def complete(self,messages,tools):
        scoped_messages=[dict(message) for message in messages]
        v3_policy=("数据以服务端按当前账号授权范围返回的字段为准。查询正常且授权范围内没有记录时，可以直接回答没有；"
                   "查询失败也不等于没有业务。信息不足时先自主查找其他相关记录，确实无法确认再简短说明已知事实与不确定之处。"
                   "与普通业务人员交流，默认用日常用语，不主动使用哈希、凭证、字段名、英文状态或错误代码；材料具体类型有依据时说签收照片、发票、付款记录等，没有依据就说相关资料，不猜材料是什么。"
                   "系统里没有回单登记，只说明系统没有记录，不能推断实物没收到或对方没交；系统里没有待办，也不能推断实际没人跟进。资料记录的标记相同不等于同一份原件存在或内容已经核实。"
                   "如果当前没有能执行某项操作的工具，只能说明目前不能直接处理，不能擅自说用户或自己没有权限；只有服务端明确返回权限不足时才说权限不足。用户主动询问专业词或技术细节时可以准确使用，并结合语境解释。"
                   "不要把内部工具名、英文类别标识或数据库字段当作给用户看的依据；系统要求的标准方括号来源引用仍须保留，界面会把运单引用显示为可点开的依据。")
        if _V3_AUDIO.get():
            v3_policy += ("本轮附有语音，文字可能来自自动转写，地名、人名、单号可能出现同音字或错字。"
                          "找不到时先结合当前有权查看的业务记录，自主核对合理的同音或近似候选；"
                          "把推测与已查事实分开，确实不确定再简短确认，不能仅因识别错字就断言业务不存在。")
            phonetic=_V3_AUDIO_CANDIDATES.get()
            if phonetic:
                v3_policy += ("按发音从当前账号有权查看的业务记录中找到以下候选，供自主核对，并非确定结论："
                              +json.dumps(phonetic,ensure_ascii=False)+"。请结合上下文和工具结果判断；多个合理候选时确认，不能仅凭发音改动记录。")
        if scoped_messages and scoped_messages[0].get("role")=="system":
            scoped_messages[0]["content"]=str(scoped_messages[0].get("content",""))+"\n"+v3_policy
        else:
            scoped_messages.insert(0,{"role":"system","content":v3_policy})
        response=self.base.complete(scoped_messages,tools)
        if not isinstance(response,dict): return response
        content=response.get("content")
        raw_calls=response.get("tool_calls") or []
        if _V3_SCOPE.get()=="none" and ((isinstance(content,str) and _v3_output_has_explicit_price(content)) or
                _v3_output_has_explicit_price(json.dumps(raw_calls,ensure_ascii=False))):
            response=dict(response); response["content"]=_V3_PRICE_REFUSAL; response["tool_calls"]=[]
        return response


def _v3_chat(user,payload):
    from ai.service import AIServiceError,ChatRequest
    text=payload.get("text")
    original_question = text if isinstance(text, str) else ""
    if not isinstance(text,str) or not text.strip() or len(text)>8000:
        return 400,{"error":"invalid_message"}
    scope=payload.get("memory_scope","enterprise")
    if scope not in {"enterprise","personal"}: return 400,{"error":"invalid_memory_scope"}
    ids=payload.get('attachment_ids',[])
    if not isinstance(ids,list) or len(ids)>12:return 400,{'error':'invalid_attachments'}
    try: attached=[agent_files.public(agent_files.authorized(sys.modules[__name__],user,fid)) for fid in ids]
    except (LookupError,TypeError):return 404,{'error':'not_found'}
    if attached:text+='\n\n本轮附件（由用户提供，可按需使用read_files读取）：'+json.dumps(attached,ensure_ascii=False)
    if len(text)>8000:return 400,{'error':'invalid_message'}
    conversation_id=payload.get("conversation_id")
    if conversation_id is not None and (not isinstance(conversation_id,str) or not conversation_id.startswith(_V3_CONVERSATION_PREFIX)):
        return 409,{"error":"legacy_history_unavailable_in_v3","message":"该旧会话仍保留在原AI历史中，但V3不会读取或注入旧消息；请新建V3会话。"}
    service=_v3_ai_service()
    if conversation_id is None:
        try: conversation_id=_v3_new_conversation(user,scope)
        except Exception: return 500,{"error":"conversation_unavailable"}
    _scope_token=_V3_SCOPE.set(_finance_scope(user))
    has_audio=any(Path(item.get('filename','')).suffix.lower() in agent_files.AUDIO for item in attached)
    voice_candidates=[]
    if has_audio:
        from capabilities.voice_match import candidates as voice_match_candidates
        with db() as con:
            authorized_rows=[item for item in _v3_raw_rows(0) if agent_team.access(con,user,item)]
        voice_candidates=voice_match_candidates(payload['text'],authorized_rows)
    _audio_token=_V3_AUDIO.set(has_audio)
    _audio_candidates_token=_V3_AUDIO_CANDIDATES.set(voice_candidates)
    _conversation_token=agent_runtime.CURRENT_CONVERSATION.set(conversation_id)
    _sources_token=agent_runtime.CURRENT_SOURCES.set([])
    try:
        if _finance_scope(user)=="none" and any(_v3_output_has_explicit_price(message["content"]) for message in service.store.messages(conversation_id,service.max_history_messages)):
            return 409,{"error":"unsafe_history_quarantined","message":"该V3会话包含不可安全注入的旧消息，原历史已保留；请新建会话。"}
        if not integrity.conversation_allowed(sys.modules[__name__],user,conversation_id):
            return 409,{"error":"conversation_permission_changed","message":"这段对话包含原岗位的信息，已保留。请开启新对话继续。"}
        integrity.record_conversation_access(sys.modules[__name__],user,conversation_id)
        result=service.chat(ChatRequest(str(user["tenant_id"]),str(user["id"]),text,conversation_id,scope)).as_dict()
        reply=str(result.get("reply", ""))
        if _finance_scope(user)=="none" and _v3_output_has_explicit_price(reply):
            # 仅对未授权角色做持久化前的兜底；已授权财务分析不拦截。
            reply="这部分金额不在当前账号的查看范围内，我没有把它写进对话。"
            with service.store._connect() as con:
                con.execute("UPDATE messages SET content=? WHERE conversation_id=? AND role='assistant' AND id=(SELECT MAX(id) FROM messages WHERE conversation_id=? AND role='assistant')",(reply,result["conversation_id"],result["conversation_id"]))
        evidence_ids=[]
        cited_ids=set(re.findall(r"\[?(v3:waybill:[^:\]\s]+:r\d+)\]?",reply))
        for evidence in result.get("evidence",[]):
            if evidence.get("source")=="waybill_case" and evidence.get("evidence_id") in cited_ids:
                evidence_ids.append(evidence.get("evidence_id",""))
        for event in result.get("tool_events",[]):
            if isinstance(event,dict): evidence_ids.extend(_v3_tool_evidence_ids(event.get("result",{})))
        blocks=_v3_case_blocks(evidence_ids,user)
        for event in result.get('tool_events',[]):
            if isinstance(event,dict):blocks.extend(_v3_shared_file_blocks(event.get('result',{}),user))
        _job = agent_runtime.CURRENT_JOB.get()[1] if agent_runtime.CURRENT_JOB.get() else ""
        _tool_names = [e.get("name") for e in result.get("tool_events", []) if isinstance(e, dict) and isinstance(e.get("name"), str)]
        with db() as con:
            con.execute('INSERT INTO agent_usage(tenant_id,owner_id,conversation_id,model_usage,status,created_at,job_id) VALUES(?,?,?,?,?,?,?)',(user['tenant_id'],user['id'],result['conversation_id'],json.dumps(result.get('model_usage',{})),200,now_iso(),_job))
            log_ai_call(con, user, conversation_id=result["conversation_id"], status=200,
                        question=original_question, answer=reply,
                        model_usage=result.get("model_usage", {}), elapsed_ms=result.get("elapsed_ms", 0),
                        tool_names=_tool_names, attachment_count=len(attached),
                        finance_fields=(_finance_scope(user) != "none"), job_id=_job)
        return 200,{"conversation_id":result["conversation_id"],"message":{"id":secrets.token_urlsafe(12),"role":"assistant","text":reply,"blocks":blocks}}
    except AIServiceError as exc:
        metrics=getattr(exc,'model_metrics',{})
        usage=metrics.get('model_usage',{}) if isinstance(metrics,dict) else {}
        _err_status=409 if exc.code=='REQUEST_CANCELLED' else 503 if exc.code=='MODEL_NOT_CONFIGURED' else 502
        _job = agent_runtime.CURRENT_JOB.get()[1] if agent_runtime.CURRENT_JOB.get() else ""
        with db() as con:
            con.execute('INSERT INTO agent_usage(tenant_id,owner_id,conversation_id,model_usage,status,created_at,job_id) VALUES(?,?,?,?,?,?,?)',(user['tenant_id'],user['id'],conversation_id,json.dumps(usage),_err_status,now_iso(),_job))
            log_ai_call(con, user, conversation_id=conversation_id, status=_err_status, error_code=str(exc.code or ""),
                        question=original_question, answer=str(getattr(exc, 'message', '') or ''),
                        model_usage=usage, elapsed_ms=metrics.get('elapsed_ms', 0) if isinstance(metrics, dict) else 0,
                        attachment_count=len(attached), finance_fields=(_finance_scope(user) != "none"), job_id=_job)
        if exc.code in {"CONVERSATION_NOT_FOUND","CONVERSATION_SCOPE_MISMATCH"}:
            return 404,{"error":"not_found"}
        status=409 if exc.code=="REQUEST_CANCELLED" else 503 if exc.code=="MODEL_NOT_CONFIGURED" else 502
        friendly={"MODEL_EMPTY_RESPONSE":"这次没能整理出完整答复，已完成的操作仍保留，可以接着问我。",
                  "MODEL_HTTP_ERROR":"这次暂时没能完成，已完成的操作仍保留，可以稍后继续。",
                  "MODEL_CONNECTION_ERROR":"这次暂时没能连上服务，已完成的操作仍保留，可以稍后继续。",
                  "MODEL_NOT_CONFIGURED":"这次暂时无法连接智能助理，请联系管理员检查服务。"}
        return status,{"error":exc.code.lower(),"message":friendly.get(exc.code,str(exc))}
    finally:
        _V3_AUDIO_CANDIDATES.reset(_audio_candidates_token)
        _V3_AUDIO.reset(_audio_token)
        agent_runtime.CURRENT_SOURCES.reset(_sources_token)
        agent_runtime.CURRENT_CONVERSATION.reset(_conversation_token)
        _V3_SCOPE.reset(_scope_token)


def _v3_history_list(user,query):
    try: limit=min(max(int(query.get("limit",["30"])[0]),1),100); offset=max(int(query.get("offset",["0"])[0]),0)
    except (ValueError,TypeError): return 400,{"error":"invalid_pagination"}
    scope=query.get("memory_scope",[None])[0]
    if scope not in {None,"enterprise","personal"}: return 400,{"error":"invalid_memory_scope"}
    service=_v3_ai_service()
    rows=service.store.list_conversations(tenant_id=str(user["tenant_id"]),principal_id=str(user["id"]),memory_scope=scope,limit=limit,offset=offset)
    items=[]
    for row in rows:
        own=row["conversation_id"].startswith(_V3_CONVERSATION_PREFIX)
        items.append({"conversation_id":row["conversation_id"],"memory_scope":row["memory_scope"],
                      "created_at":_v3_history_time(row["created_at"]),"updated_at":_v3_history_time(row["updated_at"]),
                      "message_count":row["message_count"],"preview":"V3对话" if own else "旧AI会话内容在V3中不可安全展示，原始历史仍保留。",
                      "redacted":not own,"legacy_usable":False})
    return 200,{"items":items,"limit":limit,"offset":offset}


def _v3_history_detail(user,conversation_id,query):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}",conversation_id): return 404,{"error":"not_found"}
    try: limit=min(max(int(query.get("limit",["50"])[0]),1),100)
    except (ValueError,TypeError): return 400,{"error":"invalid_pagination"}
    service=_v3_ai_service()
    record=service.store.get_conversation(tenant_id=str(user["tenant_id"]),principal_id=str(user["id"]),conversation_id=conversation_id,limit=limit)
    if record is None: return 404,{"error":"not_found"}
    if not conversation_id.startswith(_V3_CONVERSATION_PREFIX):
        return 200,{"conversation_id":conversation_id,"memory_scope":record["memory_scope"],"redacted":True,"messages":[{"id":"legacy-redacted","role":"system","text":"此旧AI会话仍保留在原历史库中。其原始消息可能含财务信息，V3不会展示或注入；请在原工作台查看。","created_at":"","redacted":True}]}
    if not integrity.conversation_allowed(sys.modules[__name__],user,conversation_id):
        return 200,{"conversation_id":conversation_id,"memory_scope":record["memory_scope"],"redacted":True,"messages":[]}
    messages=[]
    raw_messages=record["messages"]
    by_index={};documents_by_index={}
    try:
        with service.store._connect() as con:
            event_rows=con.execute("SELECT result_json,updated_at FROM tool_events WHERE conversation_id=? AND state='succeeded' ORDER BY updated_at",(conversation_id,)).fetchall()
        assistant_rows=[(index,row) for index,row in enumerate(raw_messages) if row["role"]=="assistant"]
        for event in event_rows:
            try: event_result=json.loads(event["result_json"] or "{}")
            except (TypeError,json.JSONDecodeError): continue
            ids=_v3_tool_evidence_ids(event_result)
            target=next((index for index,row in assistant_rows if float(row["created_at"])>=float(event["updated_at"])),None)
            if target is not None:
                by_index.setdefault(target,[]).extend(ids)
                documents_by_index.setdefault(target,[]).extend(_v3_shared_file_blocks(event_result,user))
    except Exception:
        by_index={}
    for index,row in enumerate(raw_messages):
        content=row["content"]
        redacted=_finance_scope(user)=="none" and _v3_output_has_explicit_price(content)
        source_blocks=[]
        if row['role']=='user' and '\n\n本轮附件（由用户提供，可按需使用read_files读取）：' in content:
            display,source_json=content.rsplit('\n\n本轮附件（由用户提供，可按需使用read_files读取）：',1)
            try:
                for source in json.loads(source_json):
                    source_row=agent_files.authorized(sys.modules[__name__],user,source['id'])
                    source_blocks.append({'type':'file','file_id':source_row['id'],'label':source_row['filename']})
                content=display
            except (ValueError,TypeError,KeyError,LookupError):content=display
        display_content=re.sub(r"\n\n（当前手动台：(查看运单|编辑运单|录入运单（尚未保存）) \d{1,32}。这是当前页面定位，实际业务信息以授权查询结果为准。）$","",content) if row["role"]=="user" else content
        item={"id":str(row["id"]),"role":row["role"],"text":"此消息可能包含价格内容，V3已隐藏；原始消息仍保留。" if redacted else display_content,"created_at":_v3_history_time(row["created_at"]),"redacted":redacted}
        ids=list(by_index.get(index,[]))
        if row["role"]=="assistant" and not redacted:
            ids.extend(re.findall(r"\[?\b(v3:waybill:[^:\]\s]+:r\d+)\]?",content))
        item["blocks"]=_v3_case_blocks(ids,dict(user))+([] if redacted else documents_by_index.get(index,[]))+source_blocks
        messages.append(item)
    return 200,{"conversation_id":conversation_id,"memory_scope":record["memory_scope"],"messages":messages,"redacted":False}


def _ai_service():
    global _AI_SERVICE
    if _AI_SERVICE is None:
        from ai.service import build_service
        _AI_SERVICE=build_service(str(DATA_DIR),context_provider=WorkbenchContext(),tool_executor=WorkbenchTools())
    return _AI_SERVICE


def inspect_business(user, *, snapshot=None, tasks_snapshot=None):
    return logistics_read.inspect_business(sys.modules[__name__],user,snapshot=snapshot,tasks_snapshot=tasks_snapshot)


def monthly_analytics(user,month):
    return logistics_read.monthly_analytics(sys.modules[__name__],user,month)


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "JulongEnterpriseTest/1.0"

    def log_message(self, fmt, *args):
        # Never log request bodies, cookies, or query strings.
        print(f"{now_iso()} {self.command} {urlsplit(self.path).path} {fmt % args}", flush=True)

    def _send(self, code: int, obj, content_type="application/json; charset=utf-8", headers=()):
        if isinstance(obj, (dict, list)):
            body = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode()
        elif isinstance(obj, str):
            body = obj.encode()
        else:
            body = obj
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "SAMEORIGIN" if urlsplit(self.path).path in {"/agent-v3", "/agent-v3/"} else "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        for key, value in headers:
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json_body(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ValueError("invalid Content-Length")
        if length <= 0 or length > MAX_BODY:
            raise ValueError("body size must be 1..2MiB")
        data = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("JSON object required")
        return data

    def _multipart_file(self):
        try: length=int(self.headers.get("Content-Length","0"))
        except ValueError: raise ValueError("invalid Content-Length")
        if length<=0 or length>MAX_MULTIPART_BODY: raise ValueError("multipart body size out of range")
        content_type=self.headers.get("Content-Type","")
        if not content_type.lower().startswith("multipart/form-data;"):
            raise ValueError("multipart/form-data required")
        envelope=(f"MIME-Version: 1.0\r\nContent-Type: {content_type}\r\n\r\n").encode()+self.rfile.read(length)
        message=BytesParser(policy=policy.default).parsebytes(envelope)
        if not message.is_multipart(): raise ValueError("invalid multipart form")
        matches=[]
        for part in message.iter_parts():
            if part.get_param("name",header="content-disposition")=="file" and part.get_filename() is not None:
                matches.append((part.get_filename(),part.get_content_type(),part.get_payload(decode=True) or b""))
        if len(matches)!=1: raise ValueError("exactly one file field is required")
        return matches[0]

    def _user(self):
        try:
            c = SimpleCookie(); c.load(self.headers.get("Cookie", ""))
            token = c.get(SESSION_COOKIE).value if c.get(SESSION_COOKIE) else ""
        except Exception:
            token = ""
        if not token:
            return None
        digest = hashlib.sha256(token.encode()).hexdigest()
        with db() as con:
            row = con.execute("SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires_at>? AND u.active=1",
                              (digest, int(time.time()))).fetchone()
        return row

    def _require_user(self):
        user = self._user()
        if not user:
            self._send(401, {"error": "authentication_required"})
            return None
        return user

    def _deny(self, code=403, error="forbidden"):
        self._send(code, {"error": error})

    def _cookie_header(self, token, max_age):
        secure = "; Secure" if COOKIE_SECURE else ""
        return f"{SESSION_COOKIE}={token}; Path=/; Max-Age={max_age}; HttpOnly; SameSite=Lax{secure}"

    @staticmethod
    def _industry_route_denied(path):
        return module_scope.route_denied(PRODUCT, path)

    def do_GET(self):
        path = urlsplit(self.path).path
        if self._industry_route_denied(path): self._send(404,{"error":"not_found"}); return
        if path == "/api/finance/candidates" or path.startswith('/api/finance/candidates/'):
            user=self._require_user()
            if not user: return
            try:
                import finance_confirmation
                host = sys.modules[__name__]
                if path == "/api/finance/candidates":
                    result = finance_confirmation.pending(host, dict(user))
                else:
                    result = finance_confirmation.review(host, dict(user), path.rsplit('/',1)[-1])
                self._send(200, result)
            except PermissionError: self._send(403,{'error':'forbidden'})
            except LookupError: self._send(404,{'error':'not_found'})
            return
        if path == "/health":
            try:
                with db() as con:
                    con.execute("SELECT rev FROM work_state WHERE id=1").fetchone()
                    con.execute("SELECT count(*) FROM agent_jobs").fetchone()
                worker=agent_runtime._WORKERS.get(str(DB_PATH))
                worker_alive=bool(worker and worker[0].is_alive())
                configured=all(os.environ.get(k) for k in ("JL_AI_BASE_URL","JL_AI_MODEL","JL_AI_API_KEY"))
                strict=os.environ.get("BOT_STRICT_PILOT","").lower() in {"1","true","yes"}
                ready=worker_alive and configured
                self._send(200 if ready or not strict else 503,{"status":"ok" if ready else "degraded","database":"ok","worker_alive":worker_alive,"model_configured":configured,"provider_health":"not_probed"})
            except sqlite3.Error:
                self._send(503,{"status":"unavailable","database":"unavailable"})
            return
        if path in {"/workspace","/workspace/","/workspace/live.js"}:
            name = "live.js" if path.endswith("/live.js") else "index.html"
            folder = ROOT / "frontend" / "shared"
            target = folder / name
            try:
                if folder.is_symlink() or target.is_symlink():
                    raise OSError("symlinked static root")
                resolved = target.resolve(strict=True)
                resolved.relative_to(folder.resolve(strict=True))
                self._send(200, resolved.read_bytes(), "text/javascript; charset=utf-8" if name.endswith(".js") else "text/html; charset=utf-8")
            except (OSError, ValueError):
                self._send(503, {"error": "frontend_not_ready"})
            return
        if path in {"/agent-v4","/agent-v4/"} or path.startswith("/agent-v4/assets/"):
            allowed={"/agent-v4/assets/window.css":("window.css","text/css; charset=utf-8"),
                     "/agent-v4/assets/app.js":("app.js","text/javascript; charset=utf-8"),
                     "/agent-v4/assets/adapter.js":("adapter.js","text/javascript; charset=utf-8")}
            if path in {"/agent-v4","/agent-v4/"}:
                target=ROOT/"frontend"/"v4"/"index.html"; mime="text/html; charset=utf-8"
            elif path in allowed:
                name,mime=allowed[path]; target=ROOT/"frontend"/"v4"/"assets"/name
            else:
                self._send(404,{"error":"not_found"}); return
            try:
                v4_root=ROOT/"frontend"/"v4"
                if v4_root.is_symlink() or (v4_root/"assets").is_symlink() or target.is_symlink(): raise OSError("symlinked static root")
                resolved=target.resolve(strict=True)
                resolved.relative_to(v4_root.resolve(strict=True))
                if not resolved.is_file(): raise OSError("not a file")
                self._send(200,resolved.read_bytes(),mime); return
            except (OSError,ValueError):
                self._send(503,{"error":"frontend_not_ready"}); return
        if path in {"/agent-v3","/agent-v3/"} or path.startswith("/agent-v3/assets/"):
            allowed={"/agent-v3/assets/theme.css":("theme.css","text/css; charset=utf-8"),
                     "/agent-v3/assets/app.js":("app.js","text/javascript; charset=utf-8"),
                     "/agent-v3/assets/backend-adapter.js":("backend-adapter.js","text/javascript; charset=utf-8")}
            if path in {"/agent-v3","/agent-v3/"}:
                target=ROOT/"frontend"/"v3"/"index.html"; mime="text/html; charset=utf-8"
            elif path in allowed:
                name,mime=allowed[path]; target=ROOT/"frontend"/"v3"/"assets"/name
            else:
                self._send(404,{"error":"not_found"}); return
            try:
                v3_root=ROOT/"frontend"/"v3"
                if v3_root.is_symlink() or (v3_root/"assets").is_symlink() or target.is_symlink(): raise OSError("symlinked static root")
                resolved=target.resolve(strict=True)
                resolved.relative_to(v3_root.resolve(strict=True))
                if not resolved.is_file(): raise OSError("not a file")
                self._send(200,resolved.read_bytes(),mime); return
            except (OSError,ValueError):
                self._send(503,{"error":"frontend_not_ready"}); return
        if path.startswith("/api/agent/"):
            user=self._require_user()
            if not user: return
            query=parse_qs(urlsplit(self.path).query)
            if path.startswith('/api/agent/files/'):
                try:
                    row=agent_files.authorized(sys.modules[__name__],dict(user),unquote(path.rsplit('/',1)[-1]));file_path=agent_files.path_for(sys.modules[__name__],row)
                    mime=__import__('mimetypes').guess_type(row['filename'])[0] or 'application/octet-stream'
                    disposition='inline' if mime in {'application/pdf','image/png','image/jpeg','image/webp','audio/wav','audio/mpeg','audio/mp4','video/webm','audio/ogg','text/plain'} else 'attachment'
                    self._send(200,file_path.read_bytes(),mime,(("Content-Disposition",disposition+"; filename*=UTF-8''"+quote(row['filename'],safe='')),));return
                except (LookupError,ValueError,OSError):self._send(404,{'error':'not_found'});return
            if path=='/api/agent/event-alerts':
                with db() as con:row=con.execute('SELECT enabled FROM event_subscriptions WHERE tenant_id=? AND owner_id=?',(user['tenant_id'],user['id'])).fetchone()
                self._send(200,{'enabled':bool(row and row['enabled'])});return
            if path in {'/api/agent/health','/api/agent/usage'}:
                name='get_agent_health' if path.endswith('/health') else 'get_agent_usage'
                params=parse_qs(urlsplit(self.path).query)
                args={'scope':params.get('scope',['mine'])[0]} if name=='get_agent_usage' else {}
                result=agent_team.execute(sys.modules[__name__],dict(user),name,args,'')
                self._send(403 if result.get('error')=='forbidden' else 200,result);return
            if path=='/api/agent/notifications':self._send(200,agent_runtime.notifications(sys.modules[__name__],dict(user)));return
            if path=='/api/agent/followups':self._send(200,agent_runtime.followups_list(sys.modules[__name__],dict(user)));return
            if path=='/api/agent/jobs':
                with db() as con:rows=con.execute('SELECT id FROM agent_jobs WHERE owner_id=? AND tenant_id=? AND role_rank<=? ORDER BY created_at DESC LIMIT 20',(user['id'],user['tenant_id'],agent_files.RANK[user['role']])).fetchall()
                self._send(200,{'items':[agent_runtime.job_view(sys.modules[__name__],dict(user),x['id']) for x in rows]});return
            if path.startswith('/api/agent/jobs/'):
                jid=path.split('/')[4]
                try:info=agent_runtime.job_view(sys.modules[__name__],dict(user),jid)
                except LookupError:self._send(404,{'error':'not_found'});return
                if path.endswith('/events'):
                    self.send_response(200);self.send_header('Content-Type','text/event-stream');self.send_header('Cache-Control','no-cache');self.send_header('X-Accel-Buffering','no');self.end_headers()
                    try:
                        last=None
                        for _ in range(120):
                            current=self._user()
                            if not current:break
                            info=agent_runtime.job_view(sys.modules[__name__],dict(current),jid)
                            data=json.dumps(info,ensure_ascii=False)
                            if data!=last:self.wfile.write(('data: '+data+'\n\n').encode());self.wfile.flush();last=data
                            if info['state'] not in {'queued','running'}:break
                            time.sleep(.5)
                    except (BrokenPipeError,ConnectionResetError,LookupError):pass
                    return
                self._send(200,info);return
            if path=='/api/agent/ui/bootstrap':
                # 能力声明必须与本企业实际启用的模块一致：企业没配财务模块时，
                # 即便角色有财务权限也不能对外宣称 financial_analysis 可用。
                _enabled_modules=set(PRODUCT['modules'])
                _fin_enabled=('finance' in _enabled_modules and _finance_scope(dict(user))!="none")
                self._send(200,{**product_profile.public_profile(PRODUCT, user['role']),"user":{"id":user["id"],"display_name":user["name"],"salutation":agent_runtime.preferences(sys.modules[__name__],dict(user)).get("salutation",user["name"]),"role":user["role"]},"suggestions":integrity.suggestions(sys.modules[__name__],dict(user)),"capabilities":{"attachments":True,"voice":(ROOT/"models"/"whisper-tiny"/".ready.json").exists(),"financial_analysis":_fin_enabled,"skills":True,"durable_jobs":True,"team_operations":True}}); return
            if path=='/api/agent/today':
                # 「Bot 先开口」：确定性扫账，不烧 token，给前端第一屏直接展示「今天该盯的事」。
                result=agent_routine.run(sys.modules[__name__],dict(user),limit=20)
                pending=agent_routine.get_bot_state(sys.modules[__name__],dict(user),include_done=True)
                self._send(200,{"routine":result,"pending_state":pending}); return
            if path.startswith("/api/agent/routine-scans/"):
                scan_id=unquote(path[len("/api/agent/routine-scans/"):])
                if not scan_id or "/" in scan_id or len(scan_id)>160:
                    self._send(404,{"error":"not_found"}); return
                scan=agent_routine.get_scan(sys.modules[__name__],dict(user),scan_id)
                self._send(200 if scan else 404,scan or {"error":"not_found"}); return
            if path=="/api/agent/conversations":
                status,body=_v3_history_list(dict(user),query); self._send(status,body); return
            if path.startswith("/api/agent/conversations/"):
                cid=unquote(path.rsplit("/",1)[-1]); status,body=_v3_history_detail(dict(user),cid,query); self._send(status,body); return
            if path.startswith("/api/agent/cases/"):
                number=unquote(path[len("/api/agent/cases/"):])
                if "/" in number: self._send(404,{"error":"not_found"}); return
                item=_v3_safe_case(dict(user),number)
                if item is None: self._send(404,{"error":"not_found"}); return
                with db() as con: audit(con,user,"v3.case.read","waybill",number,{"evidence_id":item["evidence_id"],"finance_fields":False})
                self._send(200,item); return
            self._send(404,{"error":"not_found"}); return
        if path == "/api/session":
            user = self._require_user()
            if user:
                self._send(200, {"user": {"id": user["id"], "username": user["username"], "name": user["name"], "role": user["role"]}, "permissions": ROLE_PERMISSIONS[user["role"]]})
            return
        if path == "/api/reconciliation/waybills":
            user = self._require_user()
            if not user: return
            if user["role"] != "owner_finance":
                self._send(403, {"error":"forbidden"}); return
            with db() as con:
                row=con.execute("SELECT rev,payload FROM work_state WHERE id=1").fetchone()
            state=_decode_state(row["payload"])
            docs=json.loads(state[STATE_KEYS[0]])
            fees=json.loads(state[STATE_KEYS[4]])
            items=[]
            for d in docs:
                if not isinstance(d,dict): continue
                item={k:d[k] for k in ("number","amount","freightTotal") if k in d}
                selected=[f for f in fees if isinstance(f,dict) and str(f.get("number",""))==str(d.get("number","")) and f.get("type")=="进仓费" and f.get("status")=="已确认"]
                amounts=[integrity._money(f.get("amount")) for f in selected]
                item["feeWarehouse"]=str(sum(amounts,Decimal(0))) if selected and all(a is not None for a in amounts) else ""
                item["feeWarehouseIncomplete"]=any(a is None for a in amounts)
                items.append(item)
            self._send(200, {"rev":row["rev"], "items":items}); return
        if path == "/api/state":
            user = self._require_user()
            if not user: return
            with db() as con:
                row = con.execute("SELECT rev,updated_at,payload FROM work_state WHERE id=1").fetchone()
            state = _visible_state(_decode_state(row["payload"]), user["role"], user["id"])
            result = {"rev": row["rev"], "updated": row["updated_at"], "state": state}
            if "brief" in parse_qs(urlsplit(self.path).query):
                result.pop("state")
            self._send(200, result); return
        if path.startswith("/api/generated-documents/"):
            user=self._require_user()
            if not user: return
            document_id=path.rsplit("/",1)[-1]
            if not re.fullmatch(r"[A-Za-z0-9_-]{16,80}",document_id):
                self._send(404,{"error":"not_found"}); return
            with db() as con:
                row=con.execute("SELECT * FROM generated_documents WHERE id=? AND tenant_id=? AND created_by=?",
                                (document_id,user["tenant_id"],user["id"])).fetchone()
            if not row:
                self._send(404,{"error":"not_found"}); return
            with db() as con:
                access=con.execute("SELECT financial FROM document_access WHERE document_id=?",(document_id,)).fetchone()
                current=con.execute("SELECT payload FROM work_state WHERE id=1").fetchone()
            if access is None or access['financial']:
                self._send(404,{"error":"not_found"});return
            if user['role']=='employee':
                docs=json.loads(_decode_state(current['payload'])[STATE_KEYS[0]])
                own={str(x.get('number')) for x in docs if _case_allowed(x,user)}
                if not set(json.loads(row['waybill_numbers_json']))<=own:
                    self._send(404,{"error":"not_found"});return
            relative=Path(row["stored_path"])
            if relative.is_absolute() or ".." in relative.parts or not relative.parts or relative.parts[0]!="generated_documents":
                self._send(404,{"error":"not_found"}); return
            root=DATA_DIR/"generated_documents"
            if root.is_symlink():
                self._send(404,{"error":"not_found"}); return
            file_path=(DATA_DIR/relative).resolve()
            try: file_path.relative_to(root.resolve())
            except ValueError:
                self._send(404,{"error":"not_found"}); return
            try: data=file_path.read_bytes()
            except OSError:
                self._send(404,{"error":"not_found"}); return
            if len(data)!=row["size_bytes"] or hashlib.sha256(data).hexdigest()!=row["sha256"]:
                self._send(409,{"error":"document_integrity_failed"}); return
            with db() as con:
                audit(con,user,"document.download","generated_document",document_id,{"format":row["format"],"sha256":row["sha256"]})
            content_type="text/csv; charset=utf-8" if row["format"]=="csv" else "text/markdown; charset=utf-8"
            disposition=f"attachment; filename=report-{document_id[:8]}.{row['format'] if row['format']=='csv' else 'md'}; filename*=UTF-8''{quote(row['filename'],safe='')}"
            self._send(200,data,content_type,(("Content-Disposition",disposition),)); return
        if path.startswith("/api/waybills/") and path.endswith("/attachments"):
            user=self._require_user()
            if not user: return
            from ingestion.handler import handle_list_attachments
            number=unquote(path[len("/api/waybills/"):-len("/attachments")].strip("/"))
            status,body=handle_list_attachments(dict(user),number); self._send(status,body); return
        if path.startswith("/api/attachments/"):
            user=self._require_user()
            if not user: return
            from ingestion.handler import handle_get_attachment
            status,headers,data=handle_get_attachment(dict(user),path.rsplit("/",1)[-1])
            if status!=200: self._send(status,{"error":"attachment_unavailable"}); return
            extra=[(k,v) for k,v in headers.items() if k.lower()!="content-length"]
            self._send(status,data,headers=extra,content_type=headers.get("Content-Type","application/octet-stream")); return
        if path == "/api/inspection":
            user=self._require_user()
            if user: self._send(200,inspect_business(user))
            return
        if path == "/api/analytics/monthly":
            user=self._require_user()
            if not user: return
            month=parse_qs(urlsplit(self.path).query).get("month",[datetime.now().date().isoformat()[:7]])[0]
            try: self._send(200,monthly_analytics(user,month))
            except ValueError as exc: self._send(400,{"error":str(exc)})
            return
        if path == "/api/rules":
            user=self._require_user()
            if not user: return
            query=parse_qs(urlsplit(self.path).query); history=query.get("history",["0"])[0]=="1"
            if history and user["role"]!="owner_finance": self._deny(); return
            with db() as con:
                if history:
                    rows=con.execute("SELECT * FROM business_rules WHERE tenant_id=? ORDER BY rule_key,version DESC",(user["tenant_id"],)).fetchall()
                else:
                    today=datetime.now().date().isoformat()
                    rows=con.execute("SELECT r.* FROM business_rules r JOIN (SELECT rule_key,MAX(version) version FROM business_rules WHERE tenant_id=? AND effective_at<=? GROUP BY rule_key) c ON c.rule_key=r.rule_key AND c.version=r.version WHERE r.tenant_id=? ORDER BY r.layer,r.rule_key",(user["tenant_id"],today,user["tenant_id"])).fetchall()
            items=[]
            for row in rows:
                if row["finance_only"] and user["role"]!="owner_finance": continue
                item={k:row[k] for k in ("id","rule_key","layer","title","content","version","effective_at","finance_only","created_at")}
                if user["role"]!="owner_finance": item["title"]=_redact_tree({"title":item["title"]})["title"]; item["content"]=_redact_tree({"content":item["content"]})["content"]
                items.append(item)
            self._send(200,{"items":items,"history":history}); return
        if path.startswith("/api/rules/") and path.endswith("/history"):
            user=self._require_user()
            if not user: return
            if user["role"]!="owner_finance": self._deny(); return
            rule_key=path.split("/")[-2]
            with db() as con: rows=con.execute("SELECT * FROM business_rules WHERE tenant_id=? AND rule_key=? ORDER BY version DESC",(user["tenant_id"],rule_key)).fetchall()
            self._send(200,{"rule_key":rule_key,"items":[dict(r) for r in rows]}); return
        if path == "/api/memories":
            user=self._require_user()
            if not user: return
            scope=parse_qs(urlsplit(self.path).query).get("scope",["enterprise"])[0]
            if scope not in {"enterprise","personal"}: self._send(400,{"error":"invalid_scope"}); return
            with db() as con:
                if scope=="enterprise": rows=con.execute("SELECT * FROM long_term_memories WHERE tenant_id=? AND scope='enterprise' AND active=1 ORDER BY created_at DESC",(user["tenant_id"],)).fetchall()
                else: rows=con.execute("SELECT * FROM long_term_memories WHERE tenant_id=? AND scope='personal' AND principal_id=? AND active=1 ORDER BY created_at DESC",(user["tenant_id"],user["id"])).fetchall()
            items=[]
            for row in rows:
                if row["finance_only"] and user["role"]!="owner_finance": continue
                item={k:row[k] for k in ("id","scope","principal_id","title","content","finance_only","created_by","created_at")}
                if user["role"]!="owner_finance": item["title"]=_redact_tree({"title":item["title"]})["title"]; item["content"]=_redact_tree({"content":item["content"]})["content"]
                items.append(item)
            self._send(200,{"items":items,"scope":scope}); return
        if path == "/api/assignees":
            user=self._require_user()
            if not user: return
            if user["role"] not in {"dispatcher","owner_finance"}: self._deny(); return
            with db() as con:
                rows=con.execute("SELECT id,name,role,active FROM users WHERE tenant_id=? AND role='employee' AND active=1 ORDER BY name,id",(user["tenant_id"],)).fetchall()
            self._send(200,{"items":[dict(r) for r in rows]}); return
        if path == "/api/ai/conversations":
            user=self._require_user()
            if not user: return
            from ai.handler import handle_list_conversations
            status,body=handle_list_conversations(_ai_service(),dict(user),parse_qs(urlsplit(self.path).query))
            if status==200 and isinstance(body,dict) and isinstance(body.get("items"),list):
                body["items"]=[item for item in body["items"] if not str(item.get("conversation_id","")).startswith(_V3_CONVERSATION_PREFIX) and integrity.conversation_allowed(sys.modules[__name__],dict(user),str(item.get("conversation_id","")))]
            self._send(status,body); return
        if path.startswith("/api/ai/conversations/"):
            user=self._require_user()
            if not user: return
            from ai.handler import handle_get_conversation
            conversation_id=path.rsplit("/",1)[-1]
            if not integrity.conversation_allowed(sys.modules[__name__],dict(user),conversation_id):self._send(404,{"error":"not_found"});return
            if conversation_id.startswith(_V3_CONVERSATION_PREFIX): self._send(404,{"error":"not_found"}); return
            status,body=handle_get_conversation(_ai_service(),conversation_id,dict(user),parse_qs(urlsplit(self.path).query))
            self._send(status,body); return
        if path == "/manual-v9":
            if not _module_enabled("transport"): self._send(404,{"error":"not_found"}); return
            user = self._user()
            if user:
                self._manual_v9(user)
            else:
                # 页面路由未登录：不能再吐裸 JSON（浏览器会把它当整页显示）。
                # 网关会剥掉后端的 query，sharecode 只能从浏览器地址栏取，
                # 故返回一段用 location.search 跳转的小页面，把原参数原样带去登录页。
                bounce = (b'<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
                          b'<title>\xe6\xad\xa3\xe5\x9c\xa8\xe7\x99\xbb\xe5\xbd\x95...</title></head>'
                          b'<body><script>location.replace("/" + location.search)</script>'
                          b'</body></html>')
                self._send(200, bounce, "text/html; charset=utf-8")
            return
        if path == "/api/users":
            user = self._require_user()
            if not user: return
            if user["role"] not in {"owner_finance","dispatcher"}: self._deny(); return
            with db() as con:
                if user["role"]=="owner_finance":
                    rows = con.execute("SELECT id,username,name,role,active,created_at FROM users WHERE tenant_id=? ORDER BY created_at",(user["tenant_id"],)).fetchall()
                else:
                    rows = con.execute("SELECT id,username,name,role,active,created_at FROM users WHERE tenant_id=? AND role='employee' ORDER BY created_at",(user["tenant_id"],)).fetchall()
            self._send(200, {"items": [dict(r) for r in rows]}); return
        if path == "/api/tasks":
            user = self._require_user()
            if not user: return
            with db() as con:
                if user["role"] == "employee":
                    rows = con.execute("SELECT * FROM tasks WHERE assignee_id=? ORDER BY created_at DESC", (user["id"],)).fetchall()
                else:
                    rows = con.execute("SELECT * FROM tasks ORDER BY created_at DESC").fetchall()
            items=[]
            for row in rows:
                item=dict(row); item["due_date"]=item.get("due_at"); item["waybill_id"]=item.get("waybill_number"); items.append(item)
            self._send(200, {"items": items}); return
        if path == "/api/audit":
            user = self._require_user()
            if not user: return
            if user["role"] != "owner_finance":
                self._deny(); return
            query = parse_qs(urlsplit(self.path).query)
            try: limit = min(max(int(query.get("limit", ["100"])[0]), 1), 500)
            except ValueError: limit = 100
            with db() as con:
                rows = con.execute("SELECT seq,at,user_id,user_name,action,entity_type,entity_id,details FROM audit ORDER BY seq DESC LIMIT ?", (limit,)).fetchall()
            self._send(200, {"items": [dict(r) for r in rows]}); return
        if path == "/api/ai-call-log":
            user = self._require_user()
            if not user: return
            if user["role"] != "owner_finance":
                self._deny(); return
            query = parse_qs(urlsplit(self.path).query)
            try:
                limit = min(max(int(query.get("limit", ["100"])[0]), 1), 500)
                offset = max(int(query.get("offset", ["0"])[0]), 0)
            except ValueError:
                limit, offset = 100, 0
            where, args = ["tenant_id=?"], [user["tenant_id"]]
            status_filter = query.get("status", [None])[0]
            if status_filter and status_filter.isdigit():
                where.append("status=?"); args.append(int(status_filter))
            if query.get("since", [None])[0]:
                where.append("at>=?"); args.append(query["since"][0][:40])
            if query.get("user", [None])[0]:
                where.append("user_name=?"); args.append(query["user"][0][:80])
            args.extend([limit, offset])
            with db() as con:
                rows = con.execute(
                    "SELECT seq,at,user_name,role,conversation_id,status,error_code,question_digest,answer_digest,"
                    "prompt_tokens,completion_tokens,elapsed_ms,tool_names,attachment_count,finance_fields,job_id "
                    "FROM ai_call_log WHERE " + " AND ".join(where) + " ORDER BY seq DESC LIMIT ? OFFSET ?", args).fetchall()
                total = con.execute("SELECT COUNT(*) FROM ai_call_log WHERE " + " AND ".join(where), args[:-2]).fetchone()[0]
            self._send(200, {"items": [dict(r) for r in rows], "total": total, "limit": limit, "offset": offset}); return
        if path.startswith("/api/tasks/"):
            user = self._require_user()
            if not user: return
            task_id = path.rsplit("/", 1)[-1]
            with db() as con:
                row = con.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not row or (user["role"] == "employee" and row["assignee_id"] != user["id"]):
                self._deny(404, "not_found"); return
            item=dict(row); item["due_date"]=item.get("due_at"); item["waybill_id"]=item.get("waybill_number")
            self._send(200, {"item": item}); return
        if path in ("/", "/workbench"):
            if self._user():
                self._send(302, "", headers=(("Location", "/manual-v9"),)); return
            login = ROOT / "frontend" / "login.html"
            if login.is_file():
                self._send(200, login.read_bytes(), "text/html; charset=utf-8"); return
            self._send(503, {"error": "frontend_not_ready"}); return
        self._send(404, {"error": "not_found"})

    def do_POST(self):
        path = urlsplit(self.path).path
        if self._industry_route_denied(path): self._send(404,{"error":"not_found"}); return
        if path == "/api/login":
            self._login(); return
        if path == "/api/logout":
            user = self._user()
            try:
                c = SimpleCookie(); c.load(self.headers.get("Cookie", "")); token = c.get(SESSION_COOKIE).value if c.get(SESSION_COOKIE) else ""
            except Exception: token = ""
            if token:
                with db() as con:
                    con.execute("DELETE FROM sessions WHERE token_hash=?", (hashlib.sha256(token.encode()).hexdigest(),))
                    if user: audit(con, user, "logout", "session", "", {})
            self._send(200, {"ok": True}, headers=(("Set-Cookie", self._cookie_header("", 0)),)); return
        user = self._require_user()
        if not user: return
        if path == "/api/reconciliation/waybills":
            try:
                body = self._json_body()
            except ValueError:
                self._send(400, {"error": "invalid_request"})
                return
            status, result = update_reconciliation_amount(user, body, self.headers.get("Idempotency-Key", ""))
            self._send(status, result)
            return
        if path == '/api/finance/candidates':
            try:
                import finance_confirmation
                body=self._json_body()
                if not isinstance(body,dict) or 'password' not in body: raise ValueError('invalid_request')
                finance_confirmation.reauthenticate(sys.modules[__name__],dict(user),body.pop('password'))
                kind=body.pop('kind','fee')
                if kind=='difference': result=finance_confirmation.create_difference(sys.modules[__name__],dict(user),body,self.headers.get('Idempotency-Key',''))
                elif kind=='fee': result=finance_confirmation.create(sys.modules[__name__],dict(user),body,self.headers.get('Idempotency-Key',''))
                else: raise ValueError('invalid_request')
                self._send(201,result)
            except PermissionError: self._send(403,{'error':'reauthentication_failed'})
            except ValueError as exc: self._send(409,{'error':str(exc)})
            return
        if path.startswith('/api/finance/candidates/') and path.endswith('/confirm'):
            try:
                import finance_confirmation
                body=self._json_body()
                if not isinstance(body,dict) or set(body)!={'password'}: raise ValueError('invalid_request')
                self._send(200,finance_confirmation.confirm(sys.modules[__name__],dict(user),path.split('/')[-2],body['password']))
            except PermissionError: self._send(403,{'error':'reauthentication_failed'})
            except LookupError: self._send(404,{'error':'not_found'})
            except ValueError as exc: self._send(409,{'error':str(exc)})
            return
        if path=='/api/agent/event-alerts':
            try:args=self._json_body()
            except Exception:self._send(400,{'error':'invalid_request'});return
            result=agent_team.execute(sys.modules[__name__],dict(user),'manage_event_alerts',args,self.headers.get('Idempotency-Key',''))
            self._send(400 if result.get('error') else 200,result);return
        if path=='/api/agent/files':
            try:
                name,mime,data=self._multipart_file();item=agent_files.upload(sys.modules[__name__],dict(user),name,mime and data or data)
                self._send(201,{'file':item});return
            except (ValueError,OSError,Exception) as exc:
                self._send(400,{'error':'file_upload_failed','message':'这个文件未能接收，请检查格式或大小（最多10MB）。'});return
        if path=='/api/agent/transcribe':
            try:
                body=self._json_body();fid=body['file_id'];row=agent_files.authorized(sys.modules[__name__],dict(user),fid)
                if Path(row['filename']).suffix.lower() not in agent_files.AUDIO:raise ValueError()
                result=agent_files.extract(sys.modules[__name__],dict(user),fid)
                self._send(200,{'text':result['text'],'file_id':fid});return
            except Exception:self._send(422,{'error':'transcription_failed','message':'这段语音没能转成文字，录音仍保留。'});return
        if path=='/api/agent/jobs':
            try:
                item=agent_runtime.submit(sys.modules[__name__],dict(user),self._json_body(),self.headers.get('Idempotency-Key',''));self._send(202,item)
            except LookupError:self._send(404,{'error':'not_found'})
            except ValueError as exc:
                if str(exc)=='too_many_pending_jobs':self._send(429,{'error':'too_many_pending_jobs','message':'您还有20件事正在处理或排队，可以先等一件完成，或停止不再需要的任务。'})
                else:self._send(400,{'error':'invalid_request'})
            return
        if path.startswith('/api/agent/jobs/') and path.endswith('/cancel'):
            try:self._send(200,agent_runtime.cancel(sys.modules[__name__],dict(user),path.split('/')[4]))
            except LookupError:self._send(404,{'error':'not_found'})
            return
        if path=='/api/agent/notifications/seen':
            try:
                body=self._json_body()
                with db() as con:con.execute('UPDATE agent_notifications SET seen=1 WHERE id=? AND owner_id=? AND tenant_id=?',(body['id'],user['id'],user['tenant_id']))
                self._send(200,{'ok':True})
            except Exception:self._send(400,{'error':'invalid_request'})
            return
        if path=='/api/agent/followups':
            try:
                result=agent_runtime.followups_manage(sys.modules[__name__],dict(user),self._json_body())
                self._send(200,result)
            except LookupError:self._send(404,{'error':'not_found'})
            except (ValueError,KeyError):self._send(400,{'error':'invalid_request'})
            return
        if path == "/api/state":
            self._save_state(user); return
        if path == "/api/state/restore":
            try: body=self._json_body()
            except Exception:self._send(400,{"error":"invalid_request"});return
            status,result=integrity.restore_revision(sys.modules[__name__],user,body,self.headers.get("Idempotency-Key", ""))
            self._send(status,result);return
        if path == "/api/tasks":
            self._create_task(user); return
        if path == "/api/ai/chat":
            if os.environ.get("BOT_STRICT_PILOT","").lower() in {"1","true","yes"}:
                self._send(410,{"error":"legacy_chat_disabled","message":"请使用企业工作空间。"}); return
            self._chat(user); return
        if path == "/api/agent/chat":
            if os.environ.get("BOT_STRICT_PILOT","").lower() in {"1","true","yes"}:
                self._send(410,{"error":"use_durable_jobs","message":"请使用企业工作空间的持久任务入口。"}); return
            try: payload=self._json_body()
            except Exception: self._send(400,{"error":"invalid_request"}); return
            status,body=_v3_chat(dict(user),payload)
            with db() as con: audit(con,user,"v3.ai.chat","conversation",str(body.get("conversation_id") or ""),{"status":status,"error":body.get("error"),"attachments":False,"finance_tools":False})
            self._send(status,body); return
        if path == "/api/agent/conversations":
            try: payload=self._json_body()
            except Exception: self._send(400,{"error":"invalid_request"}); return
            scope=payload.get("memory_scope","enterprise")
            if scope not in {"enterprise","personal"}: self._send(400,{"error":"invalid_memory_scope"}); return
            try: cid=_v3_new_conversation(dict(user),scope)
            except Exception: self._send(500,{"error":"conversation_unavailable"}); return
            self._send(201,{"conversation_id":cid}); return
        if path == "/api/rules":
            self._create_rule(user); return
        if path == "/api/memories":
            self._create_memory(user); return
        if path == "/api/users":
            self._create_user(user); return
        if path == "/api/waybills/import/preview":
            try: filename,content_type,data=self._multipart_file()
            except Exception: self._send(400,{"error":"invalid_upload"}); return
            from ingestion.handler import handle_preview
            status,body=handle_preview(dict(user),filename,content_type,data); self._send(status,body); return
        if path == "/api/waybills/import/commit":
            try: body=self._json_body()
            except Exception: self._send(400,{"error":"invalid_import_commit"}); return
            from ingestion.handler import handle_commit
            status,resp=handle_commit(dict(user),str(body.get("preview_id","")),self.headers.get("Idempotency-Key","")); self._send(status,resp); return
        if path.startswith("/api/waybills/") and path.endswith("/attachments"):
            try: filename,content_type,data=self._multipart_file()
            except Exception: self._send(400,{"error":"invalid_upload"}); return
            from ingestion.handler import handle_upload_attachment
            number=unquote(path[len("/api/waybills/"):-len("/attachments")].strip("/"))
            status,body=handle_upload_attachment(dict(user),number,filename,content_type,data); self._send(status,body); return
        if path.startswith("/api/users/") and path.endswith("/password"):
            self._reset_user_password(user,path.split("/")[-2]); return
        self._send(404, {"error": "not_found"})

    def do_PUT(self):
        if self._industry_route_denied(urlsplit(self.path).path): self._send(404,{"error":"not_found"}); return
        user = self._require_user()
        if not user: return
        if urlsplit(self.path).path == "/api/state":
            self._save_state(user); return
        self._send(404, {"error": "not_found"})

    def do_PATCH(self):
        user = self._require_user()
        if not user: return
        path = urlsplit(self.path).path
        if path.startswith("/api/users/"):
            self._update_user(user,path.rsplit("/",1)[-1]); return
        if path.startswith("/api/tasks/"):
            self._update_task(user, path.rsplit("/", 1)[-1]); return
        self._send(404, {"error": "not_found"})

    def do_DELETE(self):
        user=self._require_user()
        if not user: return
        path=urlsplit(self.path).path
        if self._industry_route_denied(path): self._send(404,{"error":"not_found"}); return
        if path.startswith("/api/memories/"):
            self._deactivate_memory(user,path.rsplit("/",1)[-1]); return
        if path.startswith("/api/attachments/"):
            from ingestion.handler import handle_delete_attachment
            status,body=handle_delete_attachment(dict(user),path.rsplit("/",1)[-1]); self._send(status,body); return
        self._send(404,{"error":"not_found"})

    def _login(self):
        ip = self.client_address[0]
        fails, until = LOGIN_FAILS.get(ip, (0, 0))
        if until > time.time():
            self._send(429, {"error": "login_rate_limited"}); return
        try:
            body = self._json_body(); username = str(body.get("username", ""))[:80]; password = str(body.get("password", ""))
        except Exception:
            self._send(400, {"error": "invalid_request"}); return
        with db() as con:
            row = con.execute("SELECT * FROM users WHERE username=? AND active=1", (username,)).fetchone()
        ok = False
        if row:
            candidate = hashlib.pbkdf2_hmac("sha256", password.encode(), row["password_salt"], PASSWORD_ITERATIONS)
            ok = hmac.compare_digest(candidate, row["password_hash"])
        if not ok:
            fails += 1; LOGIN_FAILS[ip] = (fails, time.time() + 60 if fails >= 5 else 0)
            self._send(401, {"error": "invalid_credentials"}); return
        LOGIN_FAILS.pop(ip, None)
        token = secrets.token_urlsafe(32); digest = hashlib.sha256(token.encode()).hexdigest(); expires = int(time.time()) + SESSION_SECONDS
        with db() as con:
            con.execute("INSERT INTO sessions(token_hash,user_id,expires_at,created_at) VALUES(?,?,?,?)", (digest, row["id"], expires, now_iso()))
            audit(con, row, "login", "session", "", {})
        self._send(200, {"user": {"id": row["id"], "username": row["username"], "name": row["name"], "role": row["role"]}, "permissions": ROLE_PERMISSIONS[row["role"]]}, headers=(("Set-Cookie", self._cookie_header(token, SESSION_SECONDS)),))

    def _chat(self,user):
        try: payload=self._json_body()
        except Exception: self._send(400,{"error":{"code":"INVALID_MESSAGE","message":"请求格式无效。","retryable":False},"conversation_id":None}); return
        if isinstance(payload,dict) and str(payload.get("conversation_id") or "").startswith(_V3_CONVERSATION_PREFIX):
            self._send(409,{"error":{"code":"CONVERSATION_SCOPE_MISMATCH","message":"V3会话不能从原AI接口访问。","retryable":False},"conversation_id":None}); return
        from ai.handler import handle_chat_request
        cid=payload.get('conversation_id') if isinstance(payload,dict) else None
        if cid and not integrity.conversation_allowed(sys.modules[__name__],dict(user),cid):self._send(404,{'error':'not_found'});return
        if cid:integrity.record_conversation_access(sys.modules[__name__],dict(user),cid)
        status,body=handle_chat_request(_ai_service(),payload,dict(user))
        if status==200 and body.get('conversation_id'):integrity.record_conversation_access(sys.modules[__name__],dict(user),body['conversation_id'])
        with db() as con:
            audit(con,user,"ai.chat","conversation",str(body.get("conversation_id") or ""),{"status":status,"code":(body.get("error") or {}).get("code")})
        self._send(status,body)

    def _create_rule(self,user):
        if user["role"]!="owner_finance": self._deny(); return
        try:
            body=self._json_body(); key=str(body.get("rule_key","")); layer=str(body.get("layer","")); title=str(body.get("title"," ")).strip(); content=str(body.get("content"," ")).strip()
            effective=str(body.get("effective_at",datetime.now().date().isoformat()))
            finance_only=body.get("finance_only",False)
            if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{1,79}",key) or layer not in {"foundation","calculation","conversation"}: raise ValueError
            if not title or len(title)>160 or not content or len(content)>8000 or not isinstance(finance_only,bool): raise ValueError
            datetime.strptime(effective,"%Y-%m-%d")
        except Exception: self._send(400,{"error":"invalid_rule"}); return
        idem=self.headers.get("Idempotency-Key",""); reqhash=hashlib.sha256(json.dumps({"endpoint":"rule","body":body},sort_keys=True,ensure_ascii=False,separators=(",",":")).encode()).hexdigest()
        with db() as con:
            con.execute("BEGIN IMMEDIATE")
            if idem:
                try: prior=_idempotent_response(con,user["id"],idem,reqhash)
                except ValueError: con.rollback(); self._send(409,{"error":"idempotency_key_reused"}); return
                if prior: con.rollback(); self._send(*prior); return
            row=con.execute("SELECT MAX(version) AS version FROM business_rules WHERE tenant_id=? AND rule_key=?",(user["tenant_id"],key)).fetchone()
            version=(row["version"] or 0)+1; rule_id=secrets.token_urlsafe(16); created=now_iso()
            con.execute("INSERT INTO business_rules(id,tenant_id,rule_key,layer,title,content,version,effective_at,finance_only,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",(rule_id,user["tenant_id"],key,layer,title,content,version,effective,int(finance_only),user["id"],created))
            item={"id":rule_id,"rule_key":key,"layer":layer,"title":title,"content":content,"version":version,"effective_at":effective,"finance_only":finance_only,"created_at":created}
            response={"item":item}; response_json=json.dumps(response,ensure_ascii=False,separators=(",",":"))
            audit(con,user,"rule.version.create","business_rule",rule_id,{"rule_key":key,"version":version,"effective_at":effective,"finance_only":finance_only})
            if idem: con.execute("INSERT INTO idempotency(user_id,key,request_hash,response_status,response_json,created_at) VALUES(?,?,?,?,?,?)",(user["id"],idem,reqhash,201,response_json,created))
            con.commit()
        self._send(201,response)

    def _create_memory(self,user):
        try:
            body=self._json_body(); scope=str(body.get("scope","personal")); title=str(body.get("title"," ")).strip(); content=str(body.get("content"," ")).strip(); finance_only=body.get("finance_only",False)
            if scope not in {"personal","enterprise"} or not title or len(title)>160 or not content or len(content)>4000 or not isinstance(finance_only,bool): raise ValueError
            if scope=="enterprise" and user["role"]!="owner_finance": raise PermissionError
            if finance_only and user["role"]!="owner_finance": raise PermissionError
        except PermissionError: self._deny(); return
        except Exception: self._send(400,{"error":"invalid_memory"}); return
        idem=self.headers.get("Idempotency-Key",""); reqhash=hashlib.sha256(json.dumps({"endpoint":"memory","body":body},sort_keys=True,ensure_ascii=False,separators=(",",":")).encode()).hexdigest()
        memory_id=secrets.token_urlsafe(16); created=now_iso(); principal=user["id"] if scope=="personal" else None
        with db() as con:
            con.execute("BEGIN IMMEDIATE")
            if idem:
                try: prior=_idempotent_response(con,user["id"],idem,reqhash)
                except ValueError: con.rollback(); self._send(409,{"error":"idempotency_key_reused"}); return
                if prior: con.rollback(); self._send(*prior); return
            con.execute("INSERT INTO long_term_memories(id,tenant_id,scope,principal_id,title,content,finance_only,active,created_by,created_at) VALUES(?,?,?,?,?,?,?,1,?,?)",(memory_id,user["tenant_id"],scope,principal,title,content,int(finance_only),user["id"],created))
            item={"id":memory_id,"scope":scope,"principal_id":principal,"title":title,"content":content,"finance_only":finance_only,"created_by":user["id"],"created_at":created}
            response={"item":item}; response_json=json.dumps(response,ensure_ascii=False,separators=(",",":"))
            audit(con,user,"memory.create","long_term_memory",memory_id,{"scope":scope,"finance_only":finance_only})
            if idem: con.execute("INSERT INTO idempotency(user_id,key,request_hash,response_status,response_json,created_at) VALUES(?,?,?,?,?,?)",(user["id"],idem,reqhash,201,response_json,created))
            con.commit()
        self._send(201,response)

    def _deactivate_memory(self,user,memory_id):
        with db() as con:
            con.execute("BEGIN IMMEDIATE")
            row=con.execute("SELECT * FROM long_term_memories WHERE id=? AND tenant_id=? AND active=1",(memory_id,user["tenant_id"])).fetchone()
            if not row: con.rollback(); self._deny(404,"not_found"); return
            allowed=(row["scope"]=="personal" and row["principal_id"]==user["id"]) or (row["scope"]=="enterprise" and user["role"]=="owner_finance")
            if not allowed: con.rollback(); self._deny(); return
            at=now_iso(); con.execute("UPDATE long_term_memories SET active=0,deactivated_by=?,deactivated_at=? WHERE id=?",(user["id"],at,memory_id))
            audit(con,user,"memory.deactivate","long_term_memory",memory_id,{"scope":row["scope"]})
            con.commit()
        self._send(200,{"ok":True,"id":memory_id,"active":False,"deactivated_at":at})

    def _manual_v9(self,user):
        page=(ROOT/"manual_v9"/"workbench.html").read_text(encoding="utf-8")
        shim=(ROOT/"manual_v9"/"shared-storage.js").read_text(encoding="utf-8")
        with db() as con:
            snap=con.execute("SELECT rev,updated_at,payload FROM work_state WHERE id=1").fetchone()
        state=_visible_state(_decode_state(snap["payload"]),user["role"],user["id"])
        boot={"me":user["name"],"role":user["role"],"share":"","rev":snap["rev"],"updated":snap["updated_at"],"byMe":False,"state":state}
        safe=json.dumps(boot,ensure_ascii=False,separators=(",", ":"))
        safe=safe.replace("<","\\u003c").replace(">","\\u003e").replace("&","\\u0026")
        employee_guard="" if user["role"]=="owner_finance" else """
<script>(function(){document.addEventListener('DOMContentLoaded',function(){
 var input=document.getElementById('freightTotal');
 if(input){input.required=false;input.disabled=true;var field=input.closest('.field');if(field)field.hidden=true;}
 var form=document.getElementById('waybill-form');if(form)form.addEventListener('submit',function(){if(input&&!input.value)input.value='0';},true);
});})();</script>"""
        injection="<script>window.__JL_CLOUD="+safe+";</script><script>"+shim+"</script>"+employee_guard
        page=page.replace("</head>",injection+"</head>",1)
        page=page.replace("前端业务工作台 · 离线原型","前端业务工作台 · 企业测试")
        self._send(200,page,"text/html; charset=utf-8")

    def _save_state(self, user):
        try:
            body = self._json_body()
            if set(body) != {"base_rev", "state"} or type(body["base_rev"]) is not int or body["base_rev"] < 0:
                raise ValueError("invalid_base_rev")
            base_rev = body["base_rev"]
            parsed = _parse_state_domains(body["state"])
        except (ValueError, TypeError, RecursionError):
            self._send(400, {"error": "invalid_state"}); return
        with db() as con:
            con.execute("BEGIN IMMEDIATE")
            current = con.execute("SELECT rev,updated_at,updated_by,payload FROM work_state WHERE id=1").fetchone()
            old_state = _decode_state(current["payload"])
            visible_old = _visible_state(old_state,user["role"],user["id"])
            if base_rev != current["rev"]:
                con.rollback()
                self._send(409, {"error": "revision_conflict", "rev": current["rev"], "updated": current["updated_at"], "state": visible_old}); return
            if any(parsed.get(key) not in (None, []) for key in _module_state_keys()):
                con.rollback(); self._deny(403, "module_disabled"); return
            try:
                _reject_new_demo_rows(old_state, parsed)
            except ValueError as exc:
                con.rollback()
                self._send(409, {"error": str(exc)})
                return
            if user["role"] != "owner_finance":
                unauthorized_finance_payload = any(
                    _contains_sensitive(parsed[key]) for key in STATE_KEYS[1:]
                ) or _contains_sensitive_value(parsed[STATE_KEYS[0]])
                if unauthorized_finance_payload:
                    con.rollback(); self._deny(403, "finance_fields_forbidden"); return
                merged = {}
                allowed_dispatcher = {STATE_KEYS[0], STATE_KEYS[1], STATE_KEYS[2], STATE_KEYS[5], STATE_KEYS[8]}
                try: old_waybills=json.loads(old_state[STATE_KEYS[0]])
                except Exception: old_waybills=[]
                try: submitted_waybills=parsed[STATE_KEYS[0]]
                except Exception: submitted_waybills=[]
                if user["role"] == "employee":
                    try: owned_numbers=_employee_owned_numbers(_employee_waybills(old_waybills,submitted_waybills,user["id"]),user["id"])
                    except PermissionError:
                        con.rollback(); self._deny(403,"waybill_not_owned"); return
                else:
                    owned_numbers=set()
                for key in STATE_KEYS:
                    try: old_value = json.loads(old_state[key])
                    except Exception: old_value = []
                    new_value = parsed[key]
                    if user["role"] == "employee":
                        if key == STATE_KEYS[0]:
                            try: new_value = _employee_waybills(old_value, new_value, user["id"])
                            except PermissionError:
                                con.rollback(); self._deny(403, "waybill_not_owned"); return
                        elif key == STATE_KEYS[1]:
                            new_value = _employee_append_only(old_value, new_value, user["id"])
                        elif key == STATE_KEYS[4]:
                            try: new_value = _employee_fee_updates(old_value,new_value,owned_numbers,user["name"])
                            except PermissionError as exc:
                                con.rollback(); self._deny(403,str(exc)); return
                            except ValueError:
                                con.rollback(); self._send(400,{"error":"invalid_fee_status"}); return
                        elif key == STATE_KEYS[5]:
                            try: new_value = _employee_receipts(old_value,new_value,owned_numbers,user["name"])
                            except PermissionError as exc:
                                con.rollback(); self._deny(403,str(exc)); return
                            except ValueError:
                                con.rollback(); self._send(400,{"error":"invalid_receipt"}); return
                        elif key in {STATE_KEYS[3],STATE_KEYS[6],STATE_KEYS[7],STATE_KEYS[9]}:
                            if not _json_equal(json.loads(visible_old[key]),new_value):
                                con.rollback(); self._deny(403,"state_domain_forbidden"); return
                            new_value=old_value
                        elif not _json_equal(json.loads(visible_old[key]), new_value):
                            con.rollback(); self._deny(403, "state_domain_forbidden"); return
                    elif user["role"] == "dispatcher" and key not in allowed_dispatcher:
                        if not _json_equal(json.loads(visible_old[key]),new_value):
                            con.rollback(); self._deny(403,"state_domain_forbidden"); return
                        new_value=old_value
                    new_value = _merge_protected(old_value, new_value)
                    merged[key] = json.dumps(new_value, ensure_ascii=False, separators=(",", ":"))
            else:
                merged = {key: json.dumps(parsed[key], ensure_ascii=False, separators=(",", ":")) for key in STATE_KEYS}
            try:
                merged = module_scope.preserve_disabled(PRODUCT, old_state, parsed, merged)
                waybills = json.loads(merged[STATE_KEYS[0]])
                if _module_enabled("transport"):
                    _merge_ordinary_waybill_writes(json.loads(old_state[STATE_KEYS[0]]), waybills)
                merged[STATE_KEYS[0]] = json.dumps(waybills, ensure_ascii=False, separators=(",", ":"))
                integrity.validate_state(sys.modules[__name__],dict(user),old_state,merged)
            except ValueError as exc:
                con.rollback();self._send(409,{"error":str(exc)});return
            new_payload = json.dumps(merged, ensure_ascii=False, separators=(",", ":"))
            if new_payload == current["payload"]:
                con.commit(); self._send(200, {"rev": current["rev"], "updated": current["updated_at"]}); return
            updated = now_iso(); rev = current["rev"] + 1
            con.execute("UPDATE work_state SET rev=?,updated_at=?,updated_by=?,payload=? WHERE id=1", (rev, updated, user["id"], new_payload))
            _sync_business_tasks(con,user,_decode_state(new_payload))
            audit(con, user, "state.update", "state", "1", {"rev": rev, "keys": sorted(k for k in STATE_KEYS if old_state[k] != merged[k])})
            con.commit()
        self._send(200, {"rev": rev, "updated": updated})

    def _create_task(self, user):
        try:
            body=self._json_body()
        except Exception:
            self._send(400, {"error": "invalid_task"}); return
        try:
            status,response=create_task_record(user,body,self.headers.get("Idempotency-Key", ""))
        except PermissionError as exc:
            self._deny(403,str(exc)); return
        except ValueError as exc:
            error=str(exc)
            code=428 if error=="idempotency_key_required" else 409 if error=="idempotency_key_reused" else 400
            self._send(code,{"error":error}); return
        self._send(status,response)

    def _create_user(self, user):
        if user["role"] not in {"owner_finance","dispatcher"}: self._deny(); return
        try:
            body=self._json_body(); username=str(body.get("username","")); name=str(body.get("name",username)); role=str(body.get("role","")); password=str(body.get("password",""))
            if role not in ROLES: raise ValueError
            if user["role"]=="dispatcher" and role!="employee": raise PermissionError
            salt,digest=hash_password(password)
            if not re.fullmatch(r"[A-Za-z0-9_.@-]{3,80}", username): raise ValueError
        except PermissionError:
            self._deny(); return
        except Exception:
            self._send(400,{"error":"invalid_user"}); return
        user_id=secrets.token_urlsafe(16)
        try:
            with db() as con:
                con.execute("INSERT INTO users(id,tenant_id,username,name,role,password_salt,password_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",(user_id,TENANT_ID,username,name.strip()[:80] or username,role,salt,digest,now_iso()))
                audit(con,user,"user.create","user",user_id,{"username":username,"role":role})
        except sqlite3.IntegrityError:
            self._send(409,{"error":"username_exists"}); return
        self._send(201,{"user":{"id":user_id,"username":username,"name":name.strip()[:80] or username,"role":role}})

    def _update_user(self,actor,user_id):
        try: body=self._json_body()
        except Exception: self._send(400,{"error":"invalid_user"}); return
        if not body or set(body)-{"active","role","name"}: self._send(400,{"error":"invalid_user_fields"}); return
        with db() as con:
            con.execute("BEGIN IMMEDIATE")
            target=con.execute("SELECT * FROM users WHERE id=? AND tenant_id=?",(user_id,actor["tenant_id"])).fetchone()
            if not target: con.rollback(); self._deny(404,"not_found"); return
            owner=actor["role"]=="owner_finance"
            manager=actor["role"]=="dispatcher" and target["role"]=="employee"
            if not owner and (not manager or set(body)-{"active"}): con.rollback(); self._deny(); return
            active=body.get("active",bool(target["active"]))
            if not isinstance(active,bool): con.rollback(); self._send(400,{"error":"invalid_active"}); return
            role=body.get("role",target["role"])
            name=str(body.get("name",target["name"])).strip()[:80]
            if role not in ROLES or not name: con.rollback(); self._send(400,{"error":"invalid_user"}); return
            if target["role"]=="owner_finance" and target["active"] and (not active or role!="owner_finance"):
                other=con.execute("SELECT 1 FROM users WHERE tenant_id=? AND id!=? AND role='owner_finance' AND active=1",(actor["tenant_id"],user_id)).fetchone()
                if not other: con.rollback(); self._send(409,{"error":"last_owner_required"}); return
            con.execute("UPDATE users SET active=?,role=?,name=? WHERE id=?",(int(active),role,name,user_id))
            if not active or role!=target["role"]:
                con.execute("DELETE FROM sessions WHERE user_id=?",(user_id,))
            audit(con,actor,"user.update","user",user_id,{"active":active,"role":role})
            con.commit()
        self._send(200,{"user":{"id":user_id,"username":target["username"],"name":name,"role":role,"active":active}})

    def _reset_user_password(self,actor,user_id):
        try:
            body=self._json_body(); password=body.get("password")
            if not isinstance(password,str): raise ValueError
            salt,digest=hash_password(password)
        except Exception:
            self._send(400,{"error":"invalid_password"}); return
        with db() as con:
            con.execute("BEGIN IMMEDIATE")
            target=con.execute("SELECT id,username,role FROM users WHERE id=? AND tenant_id=?",(user_id,actor["tenant_id"])).fetchone()
            if not target: con.rollback(); self._deny(404,"not_found"); return
            allowed=actor["role"]=="owner_finance" or (actor["role"]=="dispatcher" and target["role"]=="employee")
            if not allowed: con.rollback(); self._deny(); return
            con.execute("UPDATE users SET password_salt=?,password_hash=? WHERE id=?",(salt,digest,user_id))
            con.execute("DELETE FROM sessions WHERE user_id=?",(user_id,))
            audit(con,actor,"user.password_reset","user",user_id,{"username":target["username"]})
            con.commit()
        self._send(200,{"ok":True,"user_id":user_id,"sessions_revoked":True})

    def _update_task(self, user, task_id):
        try: body = self._json_body()
        except Exception: self._send(400,{"error":"invalid_task"}); return
        key=self.headers.get("Idempotency-Key", "")
        try:
            status,response=update_task_record(user,task_id,body,key)
        except LookupError:
            self._deny(404,"not_found"); return
        except PermissionError as exc:
            self._deny(403,str(exc)); return
        except ValueError as exc:
            error=str(exc)
            self._send(409 if error=="idempotency_key_reused" else 400,{"error":error}); return
        self._send(status,response)


def main():
    parser = argparse.ArgumentParser(description="Julong enterprise isolated test backend")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve")
    p_user = sub.add_parser("user-add")
    p_user.add_argument("--username", required=True); p_user.add_argument("--name", required=True)
    p_user.add_argument("--role", required=True, choices=sorted(ROLES)); p_user.add_argument("--password-stdin", action="store_true")
    args = parser.parse_args()
    if args.command == "user-add":
        password = sys.stdin.readline().rstrip("\r\n") if args.password_stdin else __import__("getpass").getpass("Password (12+ chars): ")
        add_user(args.username,args.name,args.role,password)
        print(f"created user {args.username} role={args.role}")
        return
    init_db()
    agent_runtime.start(sys.modules[__name__])
    class Server(http.server.ThreadingHTTPServer):
        daemon_threads = True
        allow_reuse_address = True
    print(f"Julong test backend listening on {HOST}:{PORT}; database={DB_PATH}",flush=True)
    Server((HOST,PORT),Handler).serve_forever()


if __name__ == "__main__":
    main()
