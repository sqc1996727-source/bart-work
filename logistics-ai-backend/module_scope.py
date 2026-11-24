"""Shared enterprise module policy. Preserve raw storage; project only on reads."""
import json
import product_profile

# ---- 模块级服务端约束 --------------------------------------------------------
# INDUSTRY_PACKS 的 modules 是企业开关。关掉一个模块，必须同时关掉它的业务工具
# 和 HTTP 接口，否则「界面没有、服务端还能调」就是假隔离。同一行业内关模块同理。
# 一个工具只归属一个模块，不重复登记，避免同名不同物导致口径分叉。
from industry_packs.logistics import LOGISTICS_MODULE_TOOLS, LOGISTICS_MODULE_ROUTES, LOGISTICS_MODULE_STATE_KEYS

# Registered industry names are separate; disabled packs never advertise their tools.
from industry_packs import PACKS


def enabled(profile):
    if not product_profile.industry_pack(profile["industry"])["business_tools"]:
        return frozenset()
    return frozenset(profile["modules"])


def blocked_tools(profile):
    active = enabled(profile)
    return frozenset(name for industry, pack in PACKS.items()
                     for module, names in pack["tool_groups"].items()
                     if industry != profile["industry"] or module not in active
                     for name in names)


def suppressed_keys(profile):
    active = enabled(profile)
    return frozenset(key for industry, pack in PACKS.items()
                     for module, keys in pack["state_groups"].items()
                     if industry != profile["industry"] or module not in active for key in keys)


def route_denied(profile, path):
    return any(path == prefix.rstrip("/") or path.startswith(prefix)
               for industry, pack in PACKS.items()
               for module, prefixes in pack["route_groups"].items()
               if industry != profile["industry"] or module not in enabled(profile) for prefix in prefixes)


def project(profile, state):
    """Return a copy, never clear persisted data."""
    hidden = suppressed_keys(profile)
    result = {key: "[]" if key in hidden else value for key, value in state.items()}
    if "finance" not in enabled(profile) and "julong_demo_waybills_v2" in result:
        try:
            rows = json.loads(result["julong_demo_waybills_v2"])
            for row in rows:
                for key in ("amount", "feeWarehouse", "freightTotal"):
                    row.pop(key, None)
            result["julong_demo_waybills_v2"] = json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
        except (ValueError, TypeError, AttributeError):
            result["julong_demo_waybills_v2"] = "[]"
    return result


def preserve_disabled(profile, old_state, parsed, merged):
    """Legacy full-snapshot writes may send empty placeholders for disabled domains."""
    for key in suppressed_keys(profile):
        if parsed.get(key) not in (None, []):
            raise PermissionError("module_disabled")
        merged[key] = old_state[key]
    return merged
