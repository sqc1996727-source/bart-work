"""主动沉淀（note 通道）的针对性测试：能记、能查、不放松安全边界。"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class NoteChannelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="julong-note-")
        self.old_data_dir = os.environ.get("DATA_DIR")
        os.environ["DATA_DIR"] = self.tmp.name
        # 用 app 模块拿 db/host，但只针对 workplace_memory 单元测 note 分支
        name = f"note_app_{os.getpid()}_{id(self)}"
        spec = importlib.util.spec_from_file_location(name, ROOT / "app.py")
        self.app = importlib.util.module_from_spec(spec)
        sys.modules[name] = self.app
        spec.loader.exec_module(self.app)
        self.app.init_db()
        from capabilities import workplace_memory as wm
        wm.initialize(self.app)

    def tearDown(self):
        if self.old_data_dir is None:
            os.environ.pop("DATA_DIR", None)
        else:
            os.environ["DATA_DIR"] = self.old_data_dir
        self.tmp.cleanup()

    def _user(self, role="employee", name="测试员工"):
        self.app.add_user("note-test", name, role, "synthetic-password")
        with self.app.db() as con:
            return dict(con.execute("SELECT * FROM users WHERE username=?", ("note-test",)).fetchone())

    def test_note_saves_without_expected_version(self):
        """note 通道不需要 expected_version，自动取当前版本 +1。"""
        from capabilities import workplace_memory as wm

        user = self._user()
        # note 一条 personal experience
        result = wm.save(
            self.app, user,
            {"action": "note", "scope": "personal", "kind": "experience",
             "topic": "客户称呼", "title": "张三的称呼",
             "content": "客户张三，别叫张老板"},
            "conv-1",
        )
        self.assertEqual(result["version"], 1)
        self.assertTrue(result.get("note"))
        self.assertEqual(result["kind"], "experience")

    def test_note_rejects_rule_kind(self):
        """note 通道只许 preference/experience/lead，不能拿 note 写正式规则。"""
        from capabilities import workplace_memory as wm

        user = self._user(role="owner_finance", name="老板")
        with self.assertRaises(ValueError):
            wm.save(
                self.app, user,
                {"action": "note", "scope": "company", "kind": "rule",
                 "topic": "规则", "title": "x", "content": "y"},
                "conv-1",
            )

    def test_note_rejects_secret(self):
        """note 通道同样被 SECRET 过滤拦住，不能记密钥。"""
        from capabilities import workplace_memory as wm

        user = self._user()
        with self.assertRaises(ValueError):
            wm.save(
                self.app, user,
                {"action": "note", "scope": "personal", "kind": "experience",
                 "topic": "密钥", "title": "key", "content": "api_key=abc123"},
                "conv-1",
            )


if __name__ == "__main__":
    unittest.main()
