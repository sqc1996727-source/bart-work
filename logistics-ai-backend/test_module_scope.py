"""模块级隔离与记忆写入契约的针对性回归测试（不打模型、不碰真账）。"""
import sqlite3
import unittest

import app
from capabilities import workplace_memory


class ModuleScopeTests(unittest.TestCase):
    """同一行业内关掉模块，服务端工具与接口必须一起关闭。"""

    def setUp(self):
        self.original = dict(app.PRODUCT)
        self.addCleanup(lambda: (app.PRODUCT.clear(), app.PRODUCT.update(self.original)))

    def test_finance_disabled_blocks_finance_only(self):
        app.PRODUCT["industry"] = "logistics"
        app.PRODUCT["modules"] = ("transport", "receipt")
        blocked = app._blocked_tool_names()
        self.assertIn("update_fee_status", blocked)
        self.assertIn("review_business_records", blocked)
        self.assertNotIn("list_waybills", blocked)
        self.assertNotIn("update_receipt_status", blocked)
        self.assertTrue(app.Handler._industry_route_denied("/api/finance/candidates"))
        self.assertTrue(app.Handler._industry_route_denied("/api/reconciliation/waybills"))
        self.assertFalse(app.Handler._industry_route_denied("/api/waybills/import/preview"))
        self.assertFalse(app.Handler._industry_route_denied("/api/agent/today"))

    def test_receipt_disabled_blocks_receipt_route_and_tool(self):
        app.PRODUCT["industry"] = "logistics"
        app.PRODUCT["modules"] = ("transport", "finance")
        blocked = app._blocked_tool_names()
        self.assertIn("update_receipt_status", blocked)
        self.assertNotIn("update_fee_status", blocked)
        self.assertIn("julong_demo_receipts_v1", app._module_state_keys())
        self.assertNotIn("julong_demo_fees_v1", app._module_state_keys())

    def test_transport_disabled_blocks_waybill_surface_and_team_tools(self):
        app.PRODUCT["industry"] = "logistics"
        app.PRODUCT["modules"] = ("finance",)
        blocked = app._blocked_tool_names()
        for name in ("list_waybills", "get_waybill", "get_case_workspace",
                     "handoff_case", "share_case_file", "commit_waybill_import",
                     "run_daily_routine"):
            self.assertIn(name, blocked)
        self.assertNotIn("update_fee_status", blocked)
        self.assertTrue(app.Handler._industry_route_denied("/api/agent/cases/WB-1"))
        self.assertTrue(app.Handler._industry_route_denied("/api/agent/routine-scans/x"))

    def test_all_modules_disabled_blocks_every_business_tool(self):
        app.PRODUCT["industry"] = "logistics"
        app.PRODUCT["modules"] = ()
        blocked = app._blocked_tool_names()
        every = set().union(*app.LOGISTICS_MODULE_TOOLS.values())
        self.assertEqual(set(blocked), every)

    def test_other_industry_blocks_every_business_tool(self):
        app.PRODUCT["industry"] = "retail"
        app.PRODUCT["modules"] = ()
        every = set().union(*app.LOGISTICS_MODULE_TOOLS.values())
        self.assertEqual(set(app._blocked_tool_names()), every)
        for path in ("/api/finance/candidates", "/api/waybills/x/attachments",
                     "/api/agent/cases/WB-1", "/api/reconciliation/waybills"):
            self.assertTrue(app.Handler._industry_route_denied(path), path)

    def test_memory_capability_schema_requires_action(self):
        tools = {t["function"]["name"]: t for t in app.capability_catalog.tools()}
        required = tools["save_workplace_memory"]["function"]["parameters"]["required"]
        self.assertIn("action", required)


class _Host:
    """最小宿主：只提供 workplace_memory 需要的事务、留痕和时钟。"""

    def __init__(self):
        # isolation_level=None：让 workplace_memory 自己发 BEGIN IMMEDIATE，
        # 与线上用法一致，避免驱动层隐式事务把 BEGIN 顶掉。
        self.con = sqlite3.connect(":memory:", isolation_level=None)
        self.con.row_factory = sqlite3.Row
        self.con.execute("CREATE TABLE users(id TEXT, tenant_id TEXT, role TEXT, active INTEGER)")
        self.con.execute("INSERT INTO users VALUES('u-owner','julong-test','owner_finance',1)")
        self.audits = []
        workplace_memory.initialize(self)

    class _Ctx:
        def __init__(self, con):
            self.con = con

        def __enter__(self):
            return self.con

        def __exit__(self, *exc):
            return False

    def db(self):
        return self._Ctx(self.con)

    def audit(self, con, user, action, kind, ref, detail=None):
        self.audits.append((action, ref))

    def now_iso(self):
        return "2026-11-25T01:00:00+08:00"


class MemoryContractTests(unittest.TestCase):
    def setUp(self):
        self.host = _Host()
        self.user = {"id": "u-owner", "tenant_id": "julong-test", "role": "owner_finance"}

    def base(self, **over):
        args = {"scope": "company", "kind": "rule", "topic": "回单口径",
                "title": "回单以厂家原件为准", "content": "以厂家原件签收日期为回单完成日",
                "action": "save", "expected_version": 0}
        args.update(over)
        return args

    def test_missing_action_is_rejected_not_inferred(self):
        args = self.base()
        args.pop("action")
        with self.assertRaises(ValueError) as caught:
            workplace_memory.save(self.host, self.user, args, "conv-1")
        self.assertEqual(str(caught.exception), "explicit_action_required")

    def test_missing_action_also_rejected_for_note_kinds(self):
        args = self.base(kind="experience", expected_version=None)
        args.pop("action")
        with self.assertRaises(ValueError) as caught:
            workplace_memory.save(self.host, self.user, args, "conv-1")
        self.assertEqual(str(caught.exception), "explicit_action_required")

    def test_rule_note_still_rejected(self):
        with self.assertRaises(ValueError) as caught:
            workplace_memory.save(self.host, self.user,
                                  self.base(action="note", expected_version=None), "conv-1")
        self.assertEqual(str(caught.exception), "rule_requires_explicit_save_with_version")

    def test_explicit_save_writes_formal_version(self):
        result = workplace_memory.save(self.host, self.user, self.base(), "conv-1")
        self.assertEqual(result["version"], 1)
        self.assertFalse(result["note"])
        again = workplace_memory.save(self.host, self.user,
                                      self.base(expected_version=1), "conv-2")
        self.assertEqual(again["version"], 2)

    def test_explicit_note_creates_working_memory(self):
        result = workplace_memory.save(self.host, self.user,
                                       self.base(kind="experience", action="note", expected_version=None),
                                       "conv-3")
        self.assertTrue(result["note"])


if __name__ == "__main__":
    unittest.main()
