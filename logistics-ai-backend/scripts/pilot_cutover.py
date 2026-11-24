"""Pilot rollback preservation: full, checked snapshots, not timestamp deltas.

Run with the service stopped. Keeping the entire small enterprise database is
simpler and safer than inferring changes from optional timestamps.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile

if __package__:
    from .ledger_backup import backup, restore, LEDGER, CHAT, checksum, referenced_files, file_path
else:
    from ledger_backup import backup, restore, LEDGER, CHAT, checksum, referenced_files, file_path


def rows_by_table(path):
    if not path.is_file():
        return None
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as con:
        tables = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        result = {}
        for name in tables:
            quoted = '"' + name.replace('"', '""') + '"'
            cursor = con.execute("SELECT * FROM " + quoted)
            columns = [c[0] for c in cursor.description]
            def encode(value):
                return {"blob_hex": value.hex()} if isinstance(value, bytes) else value
            result[name] = Counter(
                json.dumps(dict(zip(columns, map(encode, row))), sort_keys=True,
                           ensure_ascii=False, separators=(",", ":"))
                for row in cursor
            )
        return result


def compare_directories(expected, actual):
    differences = []
    for relative in (LEDGER, CHAT):
        left, right = rows_by_table(expected / relative), rows_by_table(actual / relative)
        if left is None or right is None:
            if left is not right:
                differences.append(f"{relative}: database missing on one side")
            continue
        for table in sorted(left.keys() | right.keys()):
            if table not in left or table not in right:
                differences.append(f"{relative}#{table}: table differs")
                continue
            missing = sum((left[table] - right[table]).values())
            extra = sum((right[table] - left[table]).values())
            if missing or extra:
                differences.append(f"{relative}#{table}: {missing} snapshot rows missing/changed, {extra} different/additional rows")
    if (expected / LEDGER).is_file():
        for name, details in referenced_files(expected / LEDGER):
            try:
                target = file_path(actual.resolve(), name)
                if not target.is_file() or checksum(target) != details:
                    differences.append(f"referenced file differs: {name}")
            except (OSError, ValueError):
                differences.append(f"referenced file unavailable: {name}")
    return differences


def export(data_dir, since_text, out_dir):
    # --since is retained for old runbooks only; all rows and referenced files
    # are preserved, including updates, deletions, and tables without timestamps.
    backup(data_dir.resolve(), out_dir.resolve())
    print("Full checked snapshot saved:", out_dir)
    return 0


def verify(data_dir, delta_dir):
    return compare(delta_dir, data_dir)


def compare(delta_dir, restored_dir):
    with tempfile.TemporaryDirectory(prefix="pilot-verify-") as scratch:
        expected = Path(scratch) / "snapshot"
        restore(delta_dir.resolve(), expected)
        differences = compare_directories(expected, restored_dir.resolve())
    for line in differences:
        print(line)
    if differences:
        print("Data differs. Do not perform a data-level rollback without reconciliation.")
        return 1
    print("Snapshot and comparison directory match in all tables and referenced files.")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("export")
    p.add_argument("--data-dir", required=True)
    p.add_argument("--since", default=None, help="Legacy option; full snapshot is always preserved")
    p.add_argument("--out", required=True)
    for command in ("verify", "compare"):
        p = sub.add_parser(command)
        p.add_argument("--data-dir", required=True)
        p.add_argument("--delta", required=True, help="Full snapshot directory")
    args = parser.parse_args(argv)
    try:
        if args.command == "export":
            return export(Path(args.data_dir), args.since, Path(args.out))
        return compare(Path(args.delta), Path(args.data_dir))
    except (ValueError, OSError, sqlite3.Error) as exc:
        print("Snapshot operation failed:", exc)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
