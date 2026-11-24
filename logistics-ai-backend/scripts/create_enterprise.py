"""Create an empty enterprise instance outside the shared source tree.

No model calls, no credentials written, no existing data directory reused.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import product_profile


def provision(destination, tenant, name, industry="general", modules=()):
    destination = Path(destination).expanduser().resolve()
    if destination == ROOT or ROOT in destination.parents:
        raise ValueError("enterprise instance must be outside the shared source directory")
    if destination.exists() or destination.is_symlink():
        raise FileExistsError("instance directory already exists")
    if tenant == product_profile.LEGACY_ID:
        raise ValueError("legacy tenant ID is reserved")
    config = {"tenant_id": tenant, "name": name, "industry": industry, "modules": list(modules)}
    # Validate using the exact same loader as the service before creating the instance.
    with tempfile.TemporaryDirectory(prefix="enterprise-profile-") as scratch:
        profile = Path(scratch) / "profile.json"
        profile.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
        product_profile.load(ROOT, {"BOT_PROFILE": str(profile), "DATA_DIR": str(destination / "data")})
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    (destination / "data").mkdir(mode=0o700)
    profile = destination / "profile.json"
    with profile.open("x", encoding="utf-8") as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump(config, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return profile


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--instance-dir", required=True)
    p.add_argument("--tenant", required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--industry", default="general")
    p.add_argument("--modules", default="", help="Comma-separated registered module IDs")
    args = p.parse_args()
    try:
        profile = provision(args.instance_dir, args.tenant, args.name, args.industry,
                            tuple(m.strip() for m in args.modules.split(",") if m.strip()))
    except (ValueError, OSError) as exc:
        p.error(str(exc))
    print("Created empty instance (no users, no customer data):", profile.parent)
    print("export BOT_PROFILE=" + shlex.quote(str(profile)))
    print("export DATA_DIR=" + shlex.quote(str(profile.parent / "data")))
    print("Then create the first owner with app.py user-add. Enter the password interactively.")
    print("Inject the authorized model credentials at runtime and start scripts/run_master.sh.")


if __name__ == "__main__":
    main()
