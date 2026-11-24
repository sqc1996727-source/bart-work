"""试点切换/回滚的数据保全工具：导出切换后新增的数据，并做回滚前核对。

用法（只读源库，不删不改）：
  python3 scripts/pilot_cutover.py export --data-dir D --since 2026-11-26T00:00:00+08:00 --out DIR
  python3 scripts/pilot_cutover.py verify --data-dir D --delta DIR
  python3 scripts/pilot_cutover.py compare --delta DIR --data-dir RESTORED_DIR

export  把切换时间点之后新增/更新的行按表导出成 JSONL，并写 manifest（含每文件 SHA256、
        每表行数、源库 SHA256）。这是"回滚前必须先存的试点增量包"。
verify  重新扫源库，核对增量包的行数与哈希是否仍与源库一致；任何一处对不上就非零退出。
compare 拿增量包和"已恢复出来的旧库"比对，列出只存在于增量包里的行（回滚会丢的就是这些），
        供人工决定合并方式；不自动写库。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

LEDGER = "julong.sqlite3"
CHAT = "ai/ai.sqlite3"
MANIFEST = "delta-manifest.json"
TIME_COLUMNS = ("created_at", "updated_at", "effective_at", "at")
# 库里时间戳有的带时区、有的不带；不带的按北京时间解释，避免"naive vs aware"比较报错。
CST = timezone(timedelta(hours=8))


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def readonly(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=15)
    con.row_factory = sqlite3.Row
    return con


def timestamp(value):
    if value in (None, ""):
        return None
    text = str(value)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.fromtimestamp(int(float(text)), CST)
        except (ValueError, OSError, OverflowError):
            return None
    return parsed.replace(tzinfo=CST) if parsed.tzinfo is None else parsed


def tables(con):
    return [row[0] for row in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]


def table_columns(con, table):
    return [row[1] for row in con.execute(f"PRAGMA table_info({table})")]


def delta_rows(con, table, since):
    """按任意一个时间列 >= since 取出该表增量行（行身份用 rowid）。"""
    columns = table_columns(con, table)
    stamps = [c for c in TIME_COLUMNS if c in columns]
    if not stamps:
        return []
    where = " OR ".join(f"COALESCE({c},'') <> ''" for c in stamps)
    rows = con.execute(f"SELECT rowid AS __rowid, * FROM {table} WHERE {where}").fetchall()
    picked = {}
    for row in rows:
        if any((parsed := timestamp(row[c])) is not None and parsed >= since for c in stamps):
            picked[row["__rowid"]] = {k: row[k] for k in row.keys()}
    return list(picked.values())


def export(data_dir: Path, since_text: str, out_dir: Path) -> int:
    since = timestamp(since_text)
    if since is None:
        print(f"切换时间点无法解析：{since_text}", file=sys.stderr)
        return 2
    if not data_dir.is_dir():
        print(f"数据目录不存在：{data_dir}", file=sys.stderr)
        return 2
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = {"since": since_text, "data_dir": str(data_dir), "databases": {}}
    for relative in (LEDGER, CHAT):
        path = data_dir / relative
        if not path.is_file():
            continue
        entry = {"sha256": sha256(path), "tables": {}, "files": {}}
        with readonly(path) as con:
            for table in tables(con):
                rows = delta_rows(con, table, since)
                if not rows:
                    continue
                name = f"{relative.replace('/', '_')}__{table}.jsonl"
                target = out_dir / name
                with target.open("w", encoding="utf-8") as stream:
                    for row in rows:
                        stream.write(json.dumps(row, ensure_ascii=False, default=str, separators=(",", ":")) + "\n")
                entry["tables"][table] = {"rows": len(rows), "file": name}
                entry["files"][name] = sha256(target)
        manifest["databases"][relative] = entry
    (out_dir / MANIFEST).write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    total = sum(t["rows"] for db in manifest["databases"].values() for t in db["tables"].values())
    print(f"增量导出完成：{total} 行，{len(manifest['databases'])} 个库 → {out_dir}")
    for relative, db in manifest["databases"].items():
        for table, info in db["tables"].items():
            print(f"  {relative}#{table}: {info['rows']}")
    return 0


def load_manifest(delta_dir: Path):
    path = delta_dir / MANIFEST
    if not path.is_file():
        print(f"缺少 {MANIFEST}：{delta_dir}", file=sys.stderr)
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def verify(data_dir: Path, delta_dir: Path) -> int:
    manifest = load_manifest(delta_dir)
    if manifest is None:
        return 2
    since = timestamp(manifest["since"])
    problems = []
    for relative, db in manifest["databases"].items():
        path = data_dir / relative
        if not path.is_file():
            problems.append(f"{relative} 不存在")
            continue
        with readonly(path) as con:
            for table, info in db["tables"].items():
                if table not in tables(con):
                    problems.append(f"{relative}#{table} 表已不存在")
                    continue
                current = len(delta_rows(con, table, since))
                if current != info["rows"]:
                    problems.append(f"{relative}#{table} 源库现有 {current} 行，增量包记录 {info['rows']} 行")
        for name, digest in db["files"].items():
            target = delta_dir / name
            if not target.is_file() or sha256(target) != digest:
                problems.append(f"增量文件损坏或被改动：{name}")
    if problems:
        print("核对未通过：", file=sys.stderr)
        for item in problems:
            print(f"  - {item}", file=sys.stderr)
        return 1
    print("核对通过：增量包与源库一致，可以进入回滚/合并流程。")
    return 0


def _key_of(row):
    for key in ("id", "key", "__rowid"):
        if key in row:
            return key, row[key]
    return None, None


def compare(delta_dir: Path, restored_dir: Path) -> int:
    manifest = load_manifest(delta_dir)
    if manifest is None:
        return 2
    lost = {}
    for relative, db in manifest["databases"].items():
        path = restored_dir / relative
        if not path.is_file():
            print(f"恢复目录缺少 {relative}", file=sys.stderr)
            return 2
        with readonly(path) as con:
            existing = set(tables(con))
            for table, info in db["tables"].items():
                source = delta_dir / info["file"]
                rows = [json.loads(line) for line in source.read_text(encoding="utf-8").splitlines() if line.strip()]
                if table not in existing:
                    lost[f"{relative}#{table}"] = len(rows)
                    continue
                key, _ = _key_of(rows[0]) if rows else (None, None)
                if key is None:
                    lost[f"{relative}#{table}"] = len(rows)
                    continue
                identity = "rowid" if key == "__rowid" else key
                present = {row[0] for row in con.execute(f"SELECT {identity} FROM {table}")}
                missing = sum(1 for row in rows if row.get(key) not in present)
                if missing:
                    lost[f"{relative}#{table}"] = missing
    if not lost:
        print("恢复出来的旧库已包含全部试点增量，回滚不会丢数据。")
        return 0
    print("回滚会丢以下试点新增数据，必须先合并再切回：")
    for name, count in lost.items():
        print(f"  - {name}: {count} 行")
    return 1


def main(argv=None):
    parser = argparse.ArgumentParser(description="试点切换/回滚数据保全")
    sub = parser.add_subparsers(dest="command", required=True)
    p_export = sub.add_parser("export")
    p_export.add_argument("--data-dir", required=True)
    p_export.add_argument("--since", required=True)
    p_export.add_argument("--out", required=True)
    p_verify = sub.add_parser("verify")
    p_verify.add_argument("--data-dir", required=True)
    p_verify.add_argument("--delta", required=True)
    p_compare = sub.add_parser("compare")
    p_compare.add_argument("--delta", required=True)
    p_compare.add_argument("--data-dir", required=True)
    args = parser.parse_args(argv)
    if args.command == "export":
        return export(Path(args.data_dir), args.since, Path(args.out))
    if args.command == "verify":
        return verify(Path(args.data_dir), Path(args.delta))
    return compare(Path(args.delta), Path(args.data_dir))


if __name__ == "__main__":
    sys.exit(main())
