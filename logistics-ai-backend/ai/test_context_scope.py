"""Host context memory scopes stay isolated; no provider calls or persistent data."""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class WorkbenchMemoryScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="julong-memory-scope-")
        self.old_data_dir = os.environ.get("DATA_DIR")
        os.environ["DATA_DIR"] = self.temp.name
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        name = f"julong_memory_scope_app_{os.getpid()}_{id(self)}"
        spec = importlib.util.spec_from_file_location(name, ROOT / "app.py")
        self.app = importlib.util.module_from_spec(spec)
        sys.modules[name] = self.app
        spec.loader.exec_module(self.app)
        self.app.init_db()
        self.app.add_user("owner_scope", "Scope Owner", "owner_finance", "scope-test-password")
        self.app.add_user("employee_scope_a", "Scope A", "employee", "scope-test-password")
        self.app.add_user("employee_scope_b", "Scope B", "employee", "scope-test-password")
        with self.app.db() as con:
            users = {row["username"]:dict(row) for row in con.execute("SELECT * FROM users")}
            owner, a, b = users["owner_scope"], users["employee_scope_a"], users["employee_scope_b"]
            rows = [
                ("scope-enterprise", "enterprise", None, "共享记忆", "SHARED-MEMORY-SCOPE-17", owner["id"]),
                ("scope-personal-a", "personal", a["id"], "员工A私有", "PRIVATE-A-MEMORY-SCOPE-29", a["id"]),
                ("scope-personal-b", "personal", b["id"], "员工B私有", "PRIVATE-B-MEMORY-SCOPE-38", b["id"]),
            ]
            con.executemany(
                "INSERT INTO long_term_memories(id,tenant_id,scope,principal_id,title,content,finance_only,active,created_by,created_at) VALUES(?,?,?,?,?,?,0,1,?,?)",
                [(mid,"julong-test",scope,principal,title,content,created,self.app.now_iso())
                 for mid,scope,principal,title,content,created in rows],
            )
        self.users = users

    def tearDown(self):
        if self.old_data_dir is None:
            os.environ.pop("DATA_DIR", None)
        else:
            os.environ["DATA_DIR"] = self.old_data_dir
        self.temp.cleanup()

    def test_enterprise_and_personal_scopes_are_disjoint_and_user_bound(self):
        context = self.app.WorkbenchContext()
        a = self.users["employee_scope_a"]
        b = self.users["employee_scope_b"]
        enterprise = context.retrieve("SHARED-MEMORY-SCOPE-17 PRIVATE-A-MEMORY-SCOPE-29", tenant_id="julong-test", principal_id=a["id"], memory_scope="enterprise")
        personal_a = context.retrieve("PRIVATE-A-MEMORY-SCOPE-29", tenant_id="julong-test", principal_id=a["id"], memory_scope="personal")
        personal_b = context.retrieve("PRIVATE-A-MEMORY-SCOPE-29 PRIVATE-B-MEMORY-SCOPE-38", tenant_id="julong-test", principal_id=b["id"], memory_scope="personal")
        enterprise_text = "\n".join(item.text for item in enterprise)
        personal_a_text = "\n".join(item.text for item in personal_a)
        personal_b_text = "\n".join(item.text for item in personal_b)
        self.assertIn("SHARED-MEMORY-SCOPE-17", enterprise_text)
        self.assertNotIn("PRIVATE-A-MEMORY-SCOPE-29", enterprise_text)
        self.assertNotIn("PRIVATE-B-MEMORY-SCOPE-38", enterprise_text)
        self.assertIn("PRIVATE-A-MEMORY-SCOPE-29", personal_a_text)
        self.assertNotIn("SHARED-MEMORY-SCOPE-17", personal_a_text)
        self.assertNotIn("PRIVATE-A-MEMORY-SCOPE-29", personal_b_text)
        self.assertIn("PRIVATE-B-MEMORY-SCOPE-38", personal_b_text)
