"""Deployment-owned product profile; one isolated data directory per enterprise.

No customer-facing mutation API. Module scope is enforced by module_scope.py; existing role permissions still apply.
"""
import json
import os
import re
from pathlib import Path

LEGACY_ID = "julong-test"
ROLES = ("employee", "dispatcher", "owner_finance")

# 行业能力包注册表。通用内核提供：会话、长期记忆、偏好、任务、文件、
# 表格分析、定时/跟进、团队与权限。行业包只叠加本行业的业务模块和工具。
# 未注册的行业不虚构业务能力，仅以通用内核运行（modules 为空）。
from industry_packs import PACKS as INDUSTRY_PACKS, get_pack
MODULES = INDUSTRY_PACKS["logistics"]["modules"]  # 兼容旧引用

def industry_pack(industry):
    """返回行业包；未注册行业返回空包（通用内核能力仍完整）。"""
    return get_pack(industry)

def load(root, environ=None):
    env = os.environ if environ is None else environ
    file = env.get("BOT_PROFILE")
    raw = json.loads(Path(file).read_text(encoding="utf-8")) if file else {}
    if not isinstance(raw, dict) or set(raw) - {"tenant_id", "name", "industry", "modules"}:
        raise ValueError("invalid product profile")
    if file and not {"tenant_id", "name", "industry", "modules"} <= set(raw):
        raise ValueError("profile requires tenant_id, name, industry, modules")
    tenant = raw.get("tenant_id", LEGACY_ID)
    name = raw.get("name", "巨龙物流")
    industry = raw.get("industry", "logistics")
    if not isinstance(tenant, str) or not re.fullmatch(r"[a-z][a-z0-9-]{2,63}", tenant):
        raise ValueError("invalid tenant_id")
    if not isinstance(name, str) or not name.strip() or len(name) > 80:
        raise ValueError("invalid enterprise name")
    if not isinstance(industry, str) or not industry.strip() or len(industry) > 40:
        raise ValueError("invalid industry")
    pack = industry_pack(industry)
    available = pack["modules"]
    ids = raw.get("modules", list(available))
    if not isinstance(ids, list) or any(not isinstance(x, str) or x not in available for x in ids) or len(ids) != len(set(ids)):
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
    visible = []
    definitions = industry_pack(profile["industry"])["modules"]
    for key in profile["modules"]:
        definition = definitions[key]
        if role not in definition["roles"]:
            continue
        item = {"id": key, **{k: v for k, v in definition.items() if k not in {"roles", "actions"}}}
        item["actions"] = [{k:v for k,v in action.items() if k != "requires"}
                           for action in definition.get("actions", [])
                           if not action.get("requires") or action["requires"] in profile["modules"]]
        visible.append(item)
    return {"organization": {"name": profile["name"], "industry": profile["industry"]},
            "modules": visible}
