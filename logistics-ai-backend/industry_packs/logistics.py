"""Built-in logistics industry manifest. Common code consumes the registry.
The existing logistics implementation remains a reference adapter, not a requirement for generic tenants.
"""
ROLES = ("employee", "dispatcher", "owner_finance")

PACK = {
        "modules": {
            "transport": {"label": "运单", "description": "运输记录与进度", "icon": "truck", "roles": ROLES, "prompt": "查看当前运输业务", "actions": [{"kind": "link", "label": "人工录入与业务操作", "href": "/manual-v9"}]},
            "receipt": {"label": "回单", "description": "签收凭证与回单跟进", "icon": "file", "roles": ROLES, "prompt": "查看待收回单", "actions": [{"kind": "link", "label": "人工录入与业务操作", "href": "/manual-v9", "requires": "transport"}]},
            "finance": {"label": "财务", "description": "费用核查与内部对账", "icon": "money", "roles": ("dispatcher", "owner_finance"), "prompt": "查看当前费用异常", "actions": [{"kind": "finance_review", "label": "审阅费用与对账候选"}, {"kind": "fee_void", "label": "作废已确认费用"}]},
        },
        "business_tools": True,   # 有物流业务工具包
}

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
    "transport": ("julong_demo_waybills_v2", "julong_demo_recipients_v2", "julong_demo_dispatch_v5"),
    "finance": ("julong_demo_month_v1", "julong_demo_fees_v1", "julong_demo_bills_v1",
                "julong_demo_diffs_v1", "julong_demo_ai_v1"),
    "receipt": ("julong_demo_receipts_v1", "julong_demo_delivery_v1"),
}

PACK.update(tool_groups=LOGISTICS_MODULE_TOOLS, route_groups=LOGISTICS_MODULE_ROUTES,
            state_groups=LOGISTICS_MODULE_STATE_KEYS)
