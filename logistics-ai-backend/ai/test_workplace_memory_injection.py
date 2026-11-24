"""workplace_memory 写入后能被 WorkbenchContext.retrieve 自动注入，且个人/企业隔离正确。"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class WorkplaceMemoryInjectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="julong-wm-inject-")
        self.old_data_dir = os.environ.get("DATA_DIR")
        os.environ["DATA_DIR"] = self.temp.name
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        name = f"julong_wm_inject_app_{os.getpid()}_{id(self)}"
        spec = importlib.util.spec_from_file_location(name, ROOT / "app.py")
        self.app = importlib.util.module_from_spec(spec)
        sys.modules[name] = self.app
        spec.loader.exec_module(self.app)
        self.app.init_db()
        self.app.add_user("wm_owner", "老板", "owner_finance", "wm-test-password")
        self.app.add_user("wm_emp_a", "员工A", "employee", "wm-test-password")
        self.app.add_user("wm_emp_b", "员工B", "employee", "wm-test-password")
        with self.app.db() as con:
            users = {row["username"]: dict(row) for row in con.execute("SELECT * FROM users")}
        self.users = users
        # 初始化 workplace_memory 表
        from capabilities import workplace_memory as wm
        wm.initialize(self.app)

        owner = self.users["wm_owner"]
        emp_a = self.users["wm_emp_a"]

        # 老板存一条企业记忆（工作记忆，全员可读）
        wm.save(self.app, owner,
                {"action": "note", "scope": "company", "kind": "experience",
                 "topic": "合作方简称", "title": "天运简称",
                 "content": "合作方沈阳天运，简称天运"},
                "conv-1")
        # 员工A存一条个人记忆（只本人可读）
        wm.save(self.app, emp_a,
                {"action": "note", "scope": "personal", "kind": "preference",
                 "topic": "个人偏好", "title": "A的偏好",
                 "content": "员工A喜欢用语音不用打字"},
                "conv-2")

    def tearDown(self):
        if self.old_data_dir is None:
            os.environ.pop("DATA_DIR", None)
        else:
            os.environ["DATA_DIR"] = self.old_data_dir
        self.temp.cleanup()

    def test_company_memory_injected_to_all_roles(self):
        """企业记忆（company scope）应被任何角色的 retrieve 自动注入。"""
        context = self.app.WorkbenchContext()
        owner = self.users["wm_owner"]
        emp_b = self.users["wm_emp_b"]
        for user in (owner, emp_b):
            items = context.retrieve("天运是哪家", tenant_id="julong-test",
                                     principal_id=user["id"], memory_scope="enterprise")
            text = "\n".join(item.text for item in items)
            self.assertIn("沈阳天运", text, f"{user['name']} 应能检索到企业记忆天运")

    def test_personal_memory_only_owner_sees(self):
        """个人记忆只本人能注入，别人看不到。"""
        context = self.app.WorkbenchContext()
        emp_a = self.users["wm_emp_a"]
        emp_b = self.users["wm_emp_b"]
        items_a = context.retrieve("A的偏好 语音", tenant_id="julong-test",
                                   principal_id=emp_a["id"], memory_scope="personal")
        text_a = "\n".join(item.text for item in items_a)
        items_b = context.retrieve("A的偏好 语音", tenant_id="julong-test",
                                   principal_id=emp_b["id"], memory_scope="personal")
        text_b = "\n".join(item.text for item in items_b)
        self.assertIn("语音", text_a)
        self.assertNotIn("语音", text_b)

    def test_effective_version_scope_sensitivity_and_dispatcher_notes(self):
        from capabilities import workplace_memory as wm
        from datetime import date, timedelta
        self.app.add_user('wm_dispatch','调度','dispatcher','wm-test-password')
        with self.app.db() as con: dispatch=dict(con.execute("SELECT * FROM users WHERE username='wm_dispatch'").fetchone())
        owner=self.users['wm_owner']; emp=self.users['wm_emp_a']
        old={'action':'note','scope':'company','kind':'experience','topic':'有效政策','title':'政策','content':'旧规则'}
        wm.save(self.app,owner,old,'c1')
        wm.save(self.app,owner,{**old,'content':'未来规则','valid_from':(date.today()+timedelta(days=2)).isoformat()},'c2')
        yesterday=(date.today()-timedelta(days=1)).isoformat()
        wm.save(self.app,owner,{**old,'content':'过期规则','valid_from':yesterday,'valid_until':yesterday},'c3')
        with self.app.db() as con:
            rows=self.app._workplace_memory_rows(con,'julong-test',emp['id'],'employee','enterprise')
        self.assertIn('旧规则',[r['content'] for r in rows]); self.assertNotIn('未来规则',[r['content'] for r in rows])
        self.assertNotIn('过期规则',[r['content'] for r in rows])
        wm.save(self.app,dispatch,{'action':'note','scope':'company','kind':'lead','topic':'调度记录','title':'调度记录','content':'联系天运核实'},'c3')
        with self.app.db() as con:
            row=con.execute("SELECT * FROM workplace_memory WHERE topic='调度记录'").fetchone()
            self.assertEqual(row['created_by'],dispatch['id']);self.assertGreaterEqual(row['role_rank'],1)
            self.assertNotIn('调度记录',[r['topic'] for r in self.app._workplace_memory_rows(con,'julong-test',emp['id'],'employee')])
        with self.assertRaises(PermissionError): wm.save(self.app,emp,old,'c4')
        wm.save(self.app,owner,{'action':'note','scope':'company','kind':'lead','topic':'结算提示','title':'费用金额','content':'费用金额 200 元'},'c5')
        for context in (self.app.WorkbenchContext(),self.app.V3Context()):
            text=' '.join(x.text for x in context.retrieve('费用金额',tenant_id='julong-test',principal_id=emp['id'],memory_scope='enterprise'))
            self.assertNotIn('200',text)
        with self.app.db() as con:
            rows=self.app._workplace_memory_rows(con,'julong-test',emp['id'],'employee','enterprise')
            self.assertNotIn('个人偏好',[r['topic'] for r in rows])

    def test_model_tool_search_uses_authenticated_conversation_scope(self):
        from ai.service import AIService, ChatRequest, ConversationStore
        from ai.test_integration import ScriptedModel

        user = self.users["wm_emp_a"]
        store = ConversationStore(str(self.app.DATA_DIR / "mock_chat.sqlite3"))
        for scope in ("enterprise", "personal"):
            model = ScriptedModel([{"content": None, "tool_calls": [{
                "id": "search-memory", "type": "function",
                "function": {"name": "search_workplace_memory", "arguments": '{"query":"语音"}'},
            }]}, {"content": "已完成查询。"}])
            service = AIService(store, self.app.V3Context(), self.app.V3Tools(), model)
            result = service.chat(ChatRequest("julong-test", user["id"], "查询语音偏好", memory_scope=scope)).as_dict()
            items = result["tool_events"][0]["result"]["items"]
            self.assertEqual(any(item.get("scope") == "personal" for item in items), scope == "personal")


if __name__ == "__main__":
    unittest.main()
