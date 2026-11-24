"""Shared enterprise module policy. Preserve raw storage; project only on reads."""
import json
import product_profile

# ---- 模块级服务端约束 --------------------------------------------------------
# INDUSTRY_PACKS 的 modules 是企业开关。关掉一个模块，必须同时关掉它的业务工具
# 和 HTTP 接口，否则「界面没有、服务端还能调」就是假隔离。同一行业内关模块同理。
# 一个工具只归属一个模块，不重复登记，避免同名不同物导致口径分叉。
LOGISTICS_MODULE_TOOLS = {
    "transport": frozenset({
        "search_waybills", "get_waybill", "get_waybill_business_status",
        "list_waybills", "get_waybill_history", "get_recent_changes",
        "get_operational_summary", "run_daily_routine", "get_daily_routine_scan",
        "generate_waybill_report", "preview_waybill_import", "commit_waybill_import",
        "get_case_workspace", "handoff_case", "share_case_file",
    }),
    "receipt": frozenset({"update_receipt_status"}),
    "finance": frozenset({"update_fee_status", "review_business_records"}),
}
LOGISTICS_MODULE_ROUTES = {
    "transport": ("/api/waybills/", "/api/agent/cases/", "/api/agent/routine-scans/", "/api/attachments/", "/api/generated-documents/", "/api/inspection"),
    "receipt": (),
    "finance": ("/api/finance/", "/api/reconciliation/", "/api/analytics/monthly"),
}
# 关掉财务模块时，状态接口也不能再吐出费用、票据、差异、月度汇总域。
LOGISTICS_MODULE_STATE_KEYS = {
    "transport": ("julong_demo_waybills_v2", "julong_demo_recipients_v2", "julong_demo_dispatch_v5", "julong_demo_delivery_v1"),
    "finance": ("julong_demo_month_v1", "julong_demo_fees_v1", "julong_demo_bills_v1",
                "julong_demo_diffs_v1", "julong_demo_ai_v1"),
    "receipt": ("julong_demo_receipts_v1",),
}



def enabled(profile):
    if not product_profile.industry_pack(profile["industry"])["business_tools"]:
        return frozenset()
    return frozenset(profile["modules"])


def blocked_tools(profile):
    return frozenset(name for module, names in LOGISTICS_MODULE_TOOLS.items()
                     if module not in enabled(profile) for name in names)


def suppressed_keys(profile):
    return frozenset(key for module, keys in LOGISTICS_MODULE_STATE_KEYS.items()
                     if module not in enabled(profile) for key in keys)


def route_denied(profile, path):
    return any(path == prefix.rstrip("/") or path.startswith(prefix)
               for module, prefixes in LOGISTICS_MODULE_ROUTES.items()
               if module not in enabled(profile) for prefix in prefixes)


def project(profile, state):
    """Return a copy, never clear persisted data."""
    return {key: "[]" if key in suppressed_keys(profile) else value
            for key, value in state.items()}


def preserve_disabled(profile, old_state, parsed, merged):
    """Legacy full-snapshot writes may send empty placeholders for disabled domains."""
    for key in suppressed_keys(profile):
        if parsed.get(key) not in (None, []):
            raise PermissionError("module_disabled")
        merged[key] = old_state[key]
    return merged
