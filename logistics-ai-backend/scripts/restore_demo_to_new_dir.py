"""Verify and restore a snapshot into a new directory."""

import argparse
from pathlib import Path
import sqlite3

from ledger_backup import restore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("backup", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    try:
        restore(args.backup, args.destination)
    except (OSError, ValueError, sqlite3.Error) as exc:
        parser.exit(1, f"Restore failed: {exc}\n")
    print("Verified restore created.")


if __name__ == "__main__":
    main()
