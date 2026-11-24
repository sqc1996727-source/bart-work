"""Local backup and isolated restore for the workbench SQLite databases and files."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
from contextlib import closing


LEDGER = "julong.sqlite3"
CHAT = "ai/ai.sqlite3"
MANIFEST = "manifest.json"
FILE_DIRS = {"attachments", "generated_documents", "agent_files"}


def file_path(root, name):
    relative = Path(name)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise ValueError("invalid backup path")
    path = root / relative
    if path.resolve() != path:
        raise ValueError("symbolic links cannot be backed up or restored")
    return path


def checksum(path):
    with path.open("rb") as stream:
        return {"sha256": hashlib.file_digest(stream, "sha256").hexdigest(), "size_bytes": path.stat().st_size}


def readonly(path):
    return sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=15)


def check_sqlite(path):
    with closing(readonly(path)) as con:
        if con.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise ValueError("SQLite consistency check failed")


def copy_sqlite(source, target):
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    target.touch(mode=0o600, exist_ok=False)
    with closing(readonly(source)) as db, closing(sqlite3.connect(target)) as copy:
        db.backup(copy)
        copy.execute("PRAGMA journal_mode=DELETE")
    check_sqlite(target)


def referenced_files(path):
    with closing(readonly(path)) as con:
        tables = {name for (name,) in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        definitions = (
            ("attachments", "SELECT stored_path,sha256,size_bytes FROM attachments"),
            ("generated_documents", "SELECT stored_path,sha256,size_bytes FROM generated_documents"),
            ("agent_files", "SELECT path,sha256,size_bytes FROM agent_files"),
        )
        for directory, sql in definitions:
            if directory in tables:
                for name, digest, size in con.execute(sql):
                    if not isinstance(name, str) or len(Path(name).parts) != 2 or Path(name).parts[0] != directory:
                        raise ValueError("invalid referenced file path")
                    yield name, {"sha256": digest, "size_bytes": size}


def copy_checked(source, target, expected):
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with source.open("rb") as incoming, target.open("xb") as outgoing:
        os.fchmod(outgoing.fileno(), 0o600)
        shutil.copyfileobj(incoming, outgoing)
    if checksum(target) != expected:
        raise ValueError("backup file does not match its recorded checksum")


def publish(destination, populate):
    destination = destination.absolute()
    if destination.exists() or destination.is_symlink():
        raise ValueError("destination already exists")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".julong-backup-", dir=destination.parent) as scratch:
        staged = Path(scratch).resolve()
        populate(staged)
        if destination.exists() or destination.is_symlink():
            raise ValueError("destination already exists")
        staged.rename(destination)


def backup(data_dir, destination):
    source = data_dir.resolve()
    ledger = file_path(source, LEDGER)
    if not ledger.is_file():
        raise ValueError("source ledger is missing")

    def populate(stage):
        # Keep referenced files stable against ledger deletes while taking the
        # online snapshot. SQLite reads remain available during the backup.
        with closing(sqlite3.connect(ledger, timeout=15)) as lock:
            lock.execute("BEGIN IMMEDIATE")
            copy_sqlite(ledger, stage / LEDGER)
            entries = {LEDGER: checksum(stage / LEDGER)}
            for name, expected in referenced_files(stage / LEDGER):
                if name in entries:
                    raise ValueError("duplicate backup file reference")
                copy_checked(file_path(source, name), file_path(stage, name), expected)
                entries[name] = expected
            lock.rollback()
        chat = file_path(source, CHAT)
        if chat.is_file():
            copy_sqlite(chat, stage / CHAT)
            entries[CHAT] = checksum(stage / CHAT)
        with (stage / MANIFEST).open("x", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump({"version": 1, "files": [{"path": name, **details} for name, details in sorted(entries.items())]}, stream)

    publish(destination, populate)


def restore(backup_dir, destination):
    source = backup_dir.resolve()
    with file_path(source, MANIFEST).open(encoding="utf-8") as stream:
        manifest = json.load(stream)
    if not isinstance(manifest, dict) or manifest.get("version") != 1 or not isinstance(manifest.get("files"), list):
        raise ValueError("unknown backup manifest")
    entries = {}
    for item in manifest["files"]:
        if not isinstance(item, dict) or set(item) != {"path", "sha256", "size_bytes"} or not isinstance(item["path"], str):
            raise ValueError("invalid backup manifest entry")
        name = item["path"]
        parts = Path(name).parts
        if name in entries or (name not in (LEDGER, CHAT) and (len(parts) != 2 or parts[0] not in FILE_DIRS)):
            raise ValueError("unexpected or duplicate manifest path")
        entries[name] = {"sha256": item["sha256"], "size_bytes": item["size_bytes"]}
    if LEDGER not in entries:
        raise ValueError("ledger missing from manifest")

    def populate(stage):
        for name in (LEDGER, CHAT):
            if name in entries:
                copy_checked(file_path(source, name), file_path(stage, name), entries[name])
                check_sqlite(stage / name)
        files = dict(referenced_files(stage / LEDGER))
        listed = {name: info for name, info in entries.items() if name not in (LEDGER, CHAT)}
        if files != listed:
            raise ValueError("manifest and ledger file references differ")
        for name, expected in files.items():
            copy_checked(file_path(source, name), file_path(stage, name), expected)

    publish(destination, populate)
