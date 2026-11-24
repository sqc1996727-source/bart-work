"""Create a private, verified snapshot in a new directory."""

import argparse
import os
from pathlib import Path
import sqlite3

from ledger_backup import backup


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--data-dir", type=Path, default=Path(os.environ.get("DATA_DIR", Path(__file__).resolve().parents[1] / "data")))
    args = parser.parse_args()
    try:
        backup(args.data_dir, args.destination)
    except (OSError, ValueError, sqlite3.Error) as exc:
        parser.exit(1, f"Backup failed: {exc}\n")
    print("Verified backup created.")


if __name__ == "__main__":
    main()
