"""Tenant-bound ingestion and private image attachment storage.

The host must call ``configure`` after initializing its SQLite schema and only
call request handlers after authenticating the session. These functions never
open a listener or inspect credentials.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
import time
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import quote

from .parser import ImportValidationError, MAX_FILE_BYTES, parse_workbook

MAX_IMAGE_BYTES = 10 * 1024 * 1024
PREVIEW_TTL_SECONDS = 20 * 60
ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{16,80}$")
_HOST = None


@dataclass(frozen=True)
class HostConfig:
    db_factory: Callable
    data_dir: Path
    audit_fn: Callable
    state_keys: tuple[str, ...]
    now_fn: Callable


def configure(*, db_factory, data_dir, audit_fn, state_keys, now_fn) -> None:
    """Bind the isolated module to host persistence; no implicit app import."""
    global _HOST
    if not callable(db_factory) or not callable(audit_fn) or not callable(now_fn):
        raise TypeError("invalid host configuration")
    keys = tuple(state_keys)
    if not keys or any(not isinstance(k, str) for k in keys):
        raise TypeError("invalid host state keys")
    root = Path(data_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    _HOST = HostConfig(db_factory, root, audit_fn, keys, now_fn)


def _host():
    if _HOST is None:
        raise RuntimeError("ingestion host is not configured")
    return _HOST


def _actor_allowed(user, *, owner_only=False):
    if not isinstance(user, dict) or not user.get("id") or not user.get("tenant_id"):
        return False
    role = user.get("role")
    return role == "owner_finance" if owner_only else role in {"employee", "dispatcher", "owner_finance"}


def _ensure_schema(con):
    con.executescript("""
    CREATE TABLE IF NOT EXISTS attachments (
      id TEXT PRIMARY KEY,
      tenant_id TEXT NOT NULL,
      waybill_number TEXT NOT NULL,
      stored_path TEXT NOT NULL UNIQUE,
      original_name TEXT NOT NULL,
      content_type TEXT NOT NULL,
      size_bytes INTEGER NOT NULL CHECK(size_bytes > 0),
      sha256 TEXT NOT NULL,
      uploaded_by TEXT NOT NULL REFERENCES users(id),
      created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_attachments_ticket
      ON attachments(tenant_id,waybill_number,created_at,id);
    CREATE TABLE IF NOT EXISTS ingestion_previews (
      id TEXT PRIMARY KEY,
      tenant_id TEXT NOT NULL,
      user_id TEXT NOT NULL REFERENCES users(id),
      source_sha256 TEXT NOT NULL,
      original_name TEXT NOT NULL,
      base_rev INTEGER NOT NULL,
      rows_json TEXT NOT NULL,
      expires_at REAL NOT NULL,
      created_at TEXT NOT NULL,
      idempotency_key TEXT,
      response_json TEXT,
      response_status INTEGER
    );
    CREATE INDEX IF NOT EXISTS idx_ingestion_previews_owner
      ON ingestion_previews(tenant_id,user_id,expires_at);
    CREATE UNIQUE INDEX IF NOT EXISTS idx_ingestion_previews_idempotency
      ON ingestion_previews(tenant_id,user_id,idempotency_key)
      WHERE idempotency_key IS NOT NULL;
    """)


def _state(con, host):
    row = con.execute("SELECT rev,updated_at,payload FROM work_state WHERE id=1").fetchone()
    if not row:
        raise RuntimeError("work state unavailable")
    payload = json.loads(row["payload"])
    state = {key: payload.get(key, "[]") for key in host.state_keys}
    rows = json.loads(state[host.state_keys[0]])
    if not isinstance(rows, list):
        raise RuntimeError("waybill state invalid")
    return row, state, rows


def _safe_filename(filename: str) -> str:
    value = str(filename or "upload").replace("\\", "/").rsplit("/", 1)[-1]
    value = "".join(c for c in value if c >= " " and c not in "\x7f\r\n")
    value = value.strip(" .")[:255]
    return value or "upload"


def handle_preview(user: dict, filename: str, content_type: str, data: bytes) -> tuple[int, dict]:
    """Validate a complete V9 workbook and persist a short-lived owner preview."""
    if not _actor_allowed(user, owner_only=True):
        return 403, {"error": "forbidden"}
    try:
        host = _host()
    except RuntimeError:
        return 503, {"error": "ingestion_unavailable"}
    try:
        rows = parse_workbook(filename, content_type, data)
    except ImportValidationError as exc:
        codes = {item.get("code") for item in exc.errors}
        status = 413 if "file_too_large" in codes else 415 if "unsupported_file_type" in codes else 422
        return status, {"error": "invalid_import", "errors": exc.errors}
    except Exception:
        return 400, {"error": "invalid_import"}

    source_hash = hashlib.sha256(data).hexdigest()
    safe_name = _safe_filename(filename)
    preview_id = secrets.token_urlsafe(24)
    now = host.now_fn()
    try:
        with host.db_factory() as con:
            _ensure_schema(con)
            row, state, waybills = _state(con, host)
            present = {str(item.get("number")) for item in waybills if isinstance(item, dict) and item.get("number")}
            errors = [{"code": "waybill_already_exists", "row": item["source_row"], "field": "number"}
                      for item in rows if item["number"] in present]
            if errors:
                return 409, {"error": "invalid_import", "errors": errors}
            con.execute("DELETE FROM ingestion_previews WHERE expires_at<=?", (time.time(),))
            con.execute("""INSERT INTO ingestion_previews
              (id,tenant_id,user_id,source_sha256,original_name,base_rev,rows_json,expires_at,created_at)
              VALUES(?,?,?,?,?,?,?,?,?)""",
              (preview_id, user["tenant_id"], user["id"], source_hash, safe_name, row["rev"],
               json.dumps(rows, ensure_ascii=False, separators=(",", ":")),
               time.time() + PREVIEW_TTL_SECONDS, now))
    except Exception:
        return 500, {"error": "ingestion_storage_error"}
    public_rows = [{key: value for key, value in item.items() if key != "source_row"} for item in rows]
    return 200, {"preview_id": preview_id, "source_sha256": source_hash,
                 "filename": safe_name, "row_count": len(rows), "rows": public_rows,
                 "expires_in_seconds": PREVIEW_TTL_SECONDS}


def handle_commit(user: dict, preview_id: str, idempotency_key: str) -> tuple[int, dict]:
    """Commit all preview rows atomically against the saved state revision."""
    if not _actor_allowed(user, owner_only=True):
        return 403, {"error": "forbidden"}
    try:
        host = _host()
    except RuntimeError:
        return 503, {"error": "ingestion_unavailable"}
    key = str(idempotency_key or "")
    if not 1 <= len(key) <= 128:
        return 428, {"error": "idempotency_key_required"}
    if not isinstance(preview_id, str) or not _SAFE_ID.fullmatch(preview_id):
        return 404, {"error": "preview_not_found"}
    now = host.now_fn()
    try:
        with host.db_factory() as con:
            _ensure_schema(con)
            con.execute("BEGIN IMMEDIATE")
            preview = con.execute("SELECT * FROM ingestion_previews WHERE id=? AND tenant_id=? AND user_id=?",
                                  (preview_id, user["tenant_id"], user["id"])).fetchone()
            if not preview:
                con.rollback()
                return 404, {"error": "preview_not_found"}
            if preview["expires_at"] <= time.time():
                con.execute("DELETE FROM ingestion_previews WHERE id=?", (preview_id,))
                con.commit()
                return 410, {"error": "preview_expired"}
            if preview["idempotency_key"]:
                if preview["idempotency_key"] == key and preview["response_json"]:
                    response = json.loads(preview["response_json"])
                    con.rollback()
                    return int(preview["response_status"] or 201), response
                con.rollback()
                return 409, {"error": "preview_already_committed"}
            row, state, waybills = _state(con, host)
            if row["rev"] != preview["base_rev"]:
                con.rollback()
                return 409, {"error": "revision_conflict", "rev": row["rev"]}
            parsed_rows = json.loads(preview["rows_json"])
            existing = {str(item.get("number")) for item in waybills if isinstance(item, dict) and item.get("number")}
            if any(item["number"] in existing for item in parsed_rows):
                con.rollback()
                return 409, {"error": "waybill_already_exists"}
            committed, source_lines = [], []
            for imported in parsed_rows:
                item = {key: value for key, value in imported.items() if key != "source_row"}
                item["created_by"] = user["id"]
                item["created"] = now
                committed.append(item)
                source_lines.append(imported["source_row"])
            waybills.extend(committed)
            state[host.state_keys[0]] = json.dumps(waybills, ensure_ascii=False, separators=(",", ":"))
            payload = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
            new_rev = row["rev"] + 1
            con.execute("UPDATE work_state SET rev=?,updated_at=?,updated_by=?,payload=? WHERE id=1",
                        (new_rev, now, user["id"], payload))
            host.audit_fn(con, user, "waybill.import", "waybill_batch", preview_id, {
                "source_sha256": preview["source_sha256"], "row_count": len(committed),
                "source_rows": source_lines,
            })
            response = {"imported_count": len(committed), "waybill_numbers": [x["number"] for x in committed],
                        "rev": new_rev, "updated": now, "source_sha256": preview["source_sha256"]}
            con.execute("UPDATE ingestion_previews SET idempotency_key=?,response_json=?,response_status=201 WHERE id=?",
                        (key, json.dumps(response, ensure_ascii=False, separators=(",", ":")), preview_id))
            con.commit()
            return 201, response
    except sqlite3.IntegrityError:
        return 409, {"error": "idempotency_conflict"}
    except Exception:
        return 500, {"error": "ingestion_storage_error"}


def _waybill_access(con, user, number, host):
    row, _state_payload, waybills = _state(con, host)
    match = next((item for item in waybills if isinstance(item, dict) and str(item.get("number", "")) == str(number)), None)
    if match is None:
        return None
    from capabilities.team import access
    if not access(con,user,match):
        return None
    return match


def _sniff_image(data: bytes) -> str | None:
    if len(data) < 16 or not data:
        return None
    if data.startswith(b"\xff\xd8") and data.endswith(b"\xff\xd9"):
        pos, has_frame, has_scan = 2, False, False
        while pos < len(data) - 2:
            if data[pos] != 0xff:
                return None
            while pos < len(data) and data[pos] == 0xff:
                pos += 1
            if pos >= len(data):
                return None
            marker = data[pos]
            pos += 1
            if marker in {0xd8, 0xd9} or 0xd0 <= marker <= 0xd7 or marker == 0x01:
                continue
            if pos + 2 > len(data):
                return None
            segment_length = int.from_bytes(data[pos:pos + 2], "big")
            if segment_length < 2 or pos + segment_length > len(data):
                return None
            if marker in {0xc0, 0xc1, 0xc2, 0xc3, 0xc5, 0xc6, 0xc7, 0xc9, 0xca, 0xcb, 0xcd, 0xce, 0xcf}:
                if segment_length < 8:
                    return None
                height = int.from_bytes(data[pos + 3:pos + 5], "big")
                width = int.from_bytes(data[pos + 5:pos + 7], "big")
                components = data[pos + 7]
                if width == 0 or height == 0 or width * height > 100_000_000 or components == 0 or components > 4:
                    return None
                has_frame = True
            if marker == 0xda:
                if not has_frame:
                    return None
                scan_start = pos + segment_length
                end = data.rfind(b"\xff\xd9")
                if end <= scan_start:
                    return None
                entropy = data[scan_start:end]
                if not entropy:
                    return None
                # JPEG entropy may contain byte-stuffed FF00 and restart
                # markers, but any other unescaped FF means a broken scan.
                i = 0
                while i < len(entropy):
                    if entropy[i] == 0xff:
                        if i + 1 >= len(entropy) or entropy[i + 1] not in ({0x00} | set(range(0xd0, 0xd8))):
                            return None
                        i += 2
                    else:
                        i += 1
                has_scan = True
                pos = end
                break
            pos += segment_length
        if has_frame and has_scan and pos == len(data) - 2:
            return "image/jpeg"
        return None
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP" and int.from_bytes(data[4:8], "little") + 8 == len(data):
        pos, has_image_data, dimensions_ok = 12, False, False
        while pos + 8 <= len(data):
            kind = data[pos:pos + 4]
            size = int.from_bytes(data[pos + 4:pos + 8], "little")
            start, end = pos + 8, pos + 8 + size
            if end > len(data):
                return None
            chunk = data[start:end]
            if kind == b"VP8X" and size >= 10:
                width = 1 + int.from_bytes(chunk[4:7], "little")
                height = 1 + int.from_bytes(chunk[7:10], "little")
                dimensions_ok = 0 < width * height <= 100_000_000
            elif kind == b"VP8 " and size >= 10 and chunk[3:6] == b"\x9d\x01\x2a":
                width = int.from_bytes(chunk[6:8], "little") & 0x3fff
                height = int.from_bytes(chunk[8:10], "little") & 0x3fff
                dimensions_ok = 0 < width * height <= 100_000_000
                has_image_data = True
            elif kind == b"VP8L" and size >= 5 and chunk[0] == 0x2f:
                width = 1 + chunk[1] + ((chunk[2] & 0x3f) << 8)
                height = 1 + (chunk[2] >> 6) + (chunk[3] << 2) + ((chunk[4] & 0x0f) << 10)
                dimensions_ok = 0 < width * height <= 100_000_000
                has_image_data = True
            pos = end + (size & 1)
        if pos == len(data) and has_image_data and dimensions_ok:
            return "image/webp"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        pos, seen_ihdr, seen_idat = 8, False, False
        while pos + 12 <= len(data):
            length = int.from_bytes(data[pos:pos + 4], "big")
            kind = data[pos + 4:pos + 8]
            end = pos + 12 + length
            if end > len(data):
                return None
            chunk_data = data[pos + 8:pos + 8 + length]
            crc = int.from_bytes(data[pos + 8 + length:end], "big")
            if zlib.crc32(kind + chunk_data) & 0xffffffff != crc:
                return None
            if not seen_ihdr:
                if kind != b"IHDR" or length != 13:
                    return None
                width = int.from_bytes(chunk_data[:4], "big")
                height = int.from_bytes(chunk_data[4:8], "big")
                if width == 0 or height == 0 or width * height > 100_000_000:
                    return None
                seen_ihdr = True
            if kind == b"IDAT":
                seen_idat = True
            if kind == b"IEND":
                return "image/png" if length == 0 and seen_ihdr and seen_idat and end == len(data) else None
            pos = end
    return None


def handle_upload_attachment(user: dict, waybill_number: str, filename: str,
                             content_type: str, data: bytes) -> tuple[int, dict]:
    if not _actor_allowed(user):
        return 403, {"error": "forbidden"}
    try:
        host = _host()
    except RuntimeError:
        return 503, {"error": "ingestion_unavailable"}
    number = str(waybill_number or "").strip()
    if not number or len(number) > 80:
        return 400, {"error": "invalid_waybill_number"}
    if not isinstance(data, bytes) or not data:
        return 400, {"error": "empty_file"}
    if len(data) > MAX_IMAGE_BYTES:
        return 413, {"error": "file_too_large", "max_bytes": MAX_IMAGE_BYTES}
    detected = _sniff_image(data)
    supplied = str(content_type or "").split(";", 1)[0].strip().lower()
    if detected not in ALLOWED_IMAGE_TYPES or supplied not in {detected, "application/octet-stream", ""}:
        return 415, {"error": "unsupported_image_type"}
    extension = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}[detected]
    safe_name = _safe_filename(filename)
    if Path(safe_name).suffix.lower() not in ({".jpg", ".jpeg"} if detected == "image/jpeg" else {extension}):
        return 415, {"error": "image_extension_mismatch"}
    digest = hashlib.sha256(data).hexdigest()
    attachment_id = secrets.token_urlsafe(24)
    relative = Path("attachments") / (attachment_id + extension)
    root = (host.data_dir / "attachments").resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = (host.data_dir / relative).resolve()
    if target.parent != root:
        return 400, {"error": "invalid_storage_path"}
    temporary = root / ("." + attachment_id + ".tmp")
    try:
        with host.db_factory() as con:
            _ensure_schema(con)
            if not _waybill_access(con, user, number, host):
                return 404, {"error": "waybill_not_found"}
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            raise
        os.replace(temporary, target)
        os.chmod(target, 0o600)
        created_at = host.now_fn()
        try:
            with host.db_factory() as con:
                _ensure_schema(con)
                con.execute("BEGIN IMMEDIATE")
                if not _waybill_access(con, user, number, host):
                    con.rollback()
                    target.unlink(missing_ok=True)
                    return 404, {"error": "waybill_not_found"}
                con.execute("""INSERT INTO attachments
                  (id,tenant_id,waybill_number,stored_path,original_name,content_type,size_bytes,sha256,uploaded_by,created_at)
                  VALUES(?,?,?,?,?,?,?,?,?,?)""",
                  (attachment_id, user["tenant_id"], number, str(relative), safe_name,
                   detected, len(data), digest, user["id"], created_at))
                host.audit_fn(con, user, "attachment.upload", "attachment", attachment_id, {
                    "waybill_number": number, "content_type": detected,
                    "size_bytes": len(data), "sha256": digest,
                })
                con.commit()
        except Exception:
            target.unlink(missing_ok=True)
            raise
    except Exception:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        return 500, {"error": "attachment_storage_error"}
    return 201, {"item": _attachment_public({
        "id": attachment_id, "waybill_number": number, "original_name": safe_name,
        "content_type": detected, "size_bytes": len(data), "sha256": digest,
        "uploaded_by": user["id"], "created_at": created_at,
    })}


def _attachment_public(row):
    return {"id": row["id"], "waybill_number": row["waybill_number"],
            "filename": row.get("original_name") if isinstance(row, dict) else row["original_name"],
            "content_type": row["content_type"], "size_bytes": row["size_bytes"],
            "sha256": row["sha256"], "uploaded_by": row["uploaded_by"], "created_at": row["created_at"]}


def handle_list_attachments(user: dict, waybill_number: str) -> tuple[int, dict]:
    if not _actor_allowed(user):
        return 403, {"error": "forbidden"}
    try:
        host = _host()
    except RuntimeError:
        return 503, {"error": "ingestion_unavailable"}
    number = str(waybill_number or "").strip()
    if not number or len(number) > 80:
        return 400, {"error": "invalid_waybill_number"}
    try:
        with host.db_factory() as con:
            _ensure_schema(con)
            if not _waybill_access(con, user, number, host):
                return 404, {"error": "waybill_not_found"}
            rows = con.execute("SELECT * FROM attachments WHERE tenant_id=? AND waybill_number=? ORDER BY created_at,id",
                               (user["tenant_id"], number)).fetchall()
        return 200, {"items": [_attachment_public(dict(row)) for row in rows]}
    except Exception:
        return 500, {"error": "attachment_storage_error"}


def handle_get_attachment(user: dict, attachment_id: str) -> tuple[int, dict, bytes]:
    if not _actor_allowed(user):
        return 403, {}, b""
    try:
        host = _host()
    except RuntimeError:
        return 503, {}, b""
    if not isinstance(attachment_id, str) or not _SAFE_ID.fullmatch(attachment_id):
        return 404, {}, b""
    try:
        with host.db_factory() as con:
            _ensure_schema(con)
            row = con.execute("SELECT * FROM attachments WHERE id=? AND tenant_id=?",
                              (attachment_id, user["tenant_id"])).fetchone()
            if not row or not _waybill_access(con, user, row["waybill_number"], host):
                return 404, {}, b""
            relative = Path(row["stored_path"])
        if relative.is_absolute() or ".." in relative.parts:
            return 404, {}, b""
        root = (host.data_dir / "attachments").resolve()
        path = (host.data_dir / relative).resolve()
        unresolved = host.data_dir / relative
        if unresolved.is_symlink() or path.parent != root or not path.is_file():
            return 404, {}, b""
        data = path.read_bytes()
        if len(data) != row["size_bytes"] or hashlib.sha256(data).hexdigest() != row["sha256"]:
            return 500, {}, b""
        safe_name = _safe_filename(row["original_name"])
        headers = {
            "Content-Type": row["content_type"],
            "Content-Length": str(len(data)),
            "Content-Disposition": "attachment; filename*=UTF-8''" + quote(safe_name, safe=""),
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
        }
        return 200, headers, data
    except Exception:
        return 500, {}, b""


def handle_delete_attachment(user: dict, attachment_id: str) -> tuple[int, dict]:
    """Delete an attachment only when the caller can access its linked ticket."""
    if not _actor_allowed(user):
        return 403, {"error": "forbidden"}
    try:
        host = _host()
    except RuntimeError:
        return 503, {"error": "ingestion_unavailable"}
    if not isinstance(attachment_id, str) or not _SAFE_ID.fullmatch(attachment_id):
        return 404, {"error": "attachment_not_found"}
    try:
        with host.db_factory() as con:
            _ensure_schema(con)
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT * FROM attachments WHERE id=? AND tenant_id=?",
                              (attachment_id, user["tenant_id"])).fetchone()
            if not row or not _waybill_access(con, user, row["waybill_number"], host):
                con.rollback()
                return 404, {"error": "attachment_not_found"}
            relative = Path(row["stored_path"])
            host.audit_fn(con, user, "attachment.delete", "attachment", attachment_id,
                          {"waybill_number": row["waybill_number"], "sha256": row["sha256"]})
            con.execute("DELETE FROM attachments WHERE id=?", (attachment_id,))
            con.commit()
        if not relative.is_absolute() and ".." not in relative.parts:
            root = (host.data_dir / "attachments").resolve()
            path = (host.data_dir / relative).resolve()
            unresolved = host.data_dir / relative
            if not unresolved.is_symlink() and path.parent == root and path.is_file():
                path.unlink(missing_ok=True)
        return 200, {"ok": True}
    except Exception:
        return 500, {"error": "attachment_storage_error"}
