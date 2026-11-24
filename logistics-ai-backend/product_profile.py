"""Deployment-owned product profile; one isolated data directory per enterprise.

No customer-facing mutation API. A module entry controls presentation, not
authorization: existing server permissions remain authoritative.
"""
import json
import os
import re
from pathlib import Path

LEGACY_ID = "julong-test"
ROLES = ("employee", "dispatcher", "owner_finance")
MODULES = {
    "transport": {"label": "运单", "description": "运输记录与进度", "icon": "truck", "roles": ROLES, "prompt": "查看当前运输业务"},
    "receipt": {"label": "回单", "description": "签收凭证与回单跟进", "icon": "file", "roles": ROLES, "prompt": "查看待收回单"},
    "finance": {"label": "财务", "description": "费用核查与内部对账", "icon": "money", "roles": ("dispatcher", "owner_finance"), "prompt": "查看当前费用异常"},
}
def load(root, environ=None):
    env = os.environ if environ is None else environ
    file = env.get("BOT_PROFILE")
    raw = json.loads(Path(file).read_text(encoding="utf-8")) if file else {}
    if not isinstance(raw, dict) or set(raw) - {"tenant_id", "name", "industry", "modules"}:
        raise ValueError("invalid product profile")
    tenant = raw.get("tenant_id", LEGACY_ID)
    name = raw.get("name", "巨龙物流")
    industry = raw.get("industry", "logistics")
    if not isinstance(tenant, str) or not re.fullmatch(r"[a-z][a-z0-9-]{2,63}", tenant):
        raise ValueError("invalid tenant_id")
    if not isinstance(name, str) or not name.strip() or len(name) > 80:
        raise ValueError("invalid enterprise name")
    if industry != "logistics":
        raise ValueError("industry pack not implemented")
    ids = raw.get("modules", list(MODULES))
    if not isinstance(ids, list) or any(not isinstance(x, str) or x not in MODULES for x in ids) or len(ids) != len(set(ids)):
        raise ValueError("invalid module list")
    if tenant != LEGACY_ID and not env.get("DATA_DIR"):
        raise ValueError("new enterprise requires explicit DATA_DIR")
    data_dir = Path(env.get("DATA_DIR") or Path(root) / "data").expanduser().resolve()
    legacy_dir = (Path(root) / "data").resolve()
    if tenant != LEGACY_ID and (data_dir == legacy_dir or legacy_dir in data_dir.parents):
        raise ValueError("new enterprise must not use legacy data directory")
    return {"tenant_id": tenant, "name": name.strip(), "industry": industry, "modules": tuple(ids), "data_dir": data_dir}

def bind_database(con, profile):
    """Refuse mismatched or unlabelled preexisting DBs before schema migration."""
    tenant = profile["tenant_id"]
    tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    if "product_installation" in tables:
        row = con.execute("SELECT tenant_id FROM product_installation WHERE id=1").fetchone()
        if not row or row[0] != tenant:
            raise RuntimeError("database belongs to a different enterprise")
    elif tables and tenant != LEGACY_ID:
        raise RuntimeError("refusing to adopt an unlabelled existing database")
    if "users" in tables:
        cols = {row[1] for row in con.execute("PRAGMA table_info(users)")}
        if "tenant_id" in cols and con.execute("SELECT 1 FROM users WHERE tenant_id IS NULL OR tenant_id != ? LIMIT 1", (tenant,)).fetchone():
            raise RuntimeError("database contains a different enterprise")
    con.execute("CREATE TABLE IF NOT EXISTS product_installation(id INTEGER PRIMARY KEY CHECK(id=1), tenant_id TEXT NOT NULL)")
    con.execute("INSERT OR IGNORE INTO product_installation VALUES(1,?)", (tenant,))
    if con.execute("SELECT tenant_id FROM product_installation WHERE id=1").fetchone()[0] != tenant:
        raise RuntimeError("database belongs to a different enterprise")

def public_profile(profile, role):
    return {"organization": {"name": profile["name"], "industry": profile["industry"]},
            "modules": [{"id": key, **{k: v for k, v in MODULES[key].items() if k != "roles"}}
                        for key in profile["modules"] if role in MODULES[key]["roles"]]}
