"""Productization regression tests: no model, no production ledger."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
import product_profile as p

class ProductProfileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.file = self.root / "profile.json"
    def profile(self, tenant="customer-alpha", name="示例企业 A", modules=None, data=None):
        self.file.write_text(json.dumps({"tenant_id":tenant,"name":name,"industry":"logistics",
                                          "modules": modules if modules is not None else list(p.MODULES)}), encoding="utf-8")
        return p.load(self.root, {"BOT_PROFILE":str(self.file), "DATA_DIR":str(data or self.root/"isolated")})
    def test_legacy_defaults(self):
        a=p.load(self.root,{})
        self.assertEqual(a["tenant_id"],p.LEGACY_ID)
        # macOS /tmp 是指向 /private/tmp 的软链；load() 按设计 resolve，
        # 断言也要 resolve，否则是环境差异不是产品缺陷。
        self.assertEqual(a["data_dir"],(self.root/"data").resolve())
    def test_role_filtered_modules_and_enterprise_name(self):
        a=self.profile()
        self.assertEqual(p.public_profile(a,"owner_finance")["organization"]["name"],"示例企业 A")
        self.assertEqual([m["id"] for m in p.public_profile(a,"employee")["modules"]],["transport","receipt"])
        self.assertEqual([m["id"] for m in p.public_profile(self.profile(modules=["receipt"]),"owner_finance")["modules"]],["receipt"])
        self.assertEqual(p.public_profile(a,"unknown")["modules"],[])
    def test_invalid_config(self):
        for kw in ({"modules":["unknown"]},{"modules":["receipt","receipt"]},{"tenant":"bad id"},{"data":self.root/"data"}):
            with self.subTest(kw=kw), self.assertRaises(ValueError): self.profile(**kw)
        self.file.write_text(json.dumps({"tenant_id":"customer-alpha"}))
        with self.assertRaises(ValueError): p.load(self.root,{"BOT_PROFILE":str(self.file)})
    def test_isolation_guard(self):
        a=self.profile()
        b=self.profile(tenant="customer-beta")
        with sqlite3.connect(":memory:") as con:
            p.bind_database(con,a)
            p.bind_database(con,a)
            with self.assertRaises(RuntimeError): p.bind_database(con,b)
            self.assertEqual(con.execute("SELECT tenant_id FROM product_installation").fetchone()[0],"customer-alpha")
    def test_no_unlabelled_adoption(self):
        with sqlite3.connect(":memory:") as con:
            con.execute("CREATE TABLE users(id TEXT, tenant_id TEXT)")
            con.execute("INSERT INTO users VALUES('1','julong-test')")
            with self.assertRaises(RuntimeError): p.bind_database(con,self.profile())
            self.assertFalse(con.execute("SELECT name FROM sqlite_master WHERE name='product_installation'").fetchone())
    def test_old_database_retained(self):
        with sqlite3.connect(":memory:") as con:
            con.execute("CREATE TABLE users(id TEXT, tenant_id TEXT)")
            con.execute("INSERT INTO users VALUES('1','julong-test')")
            p.bind_database(con,p.load(self.root,{}))
            self.assertEqual(con.execute("SELECT tenant_id FROM users").fetchone()[0],"julong-test")

if __name__ == "__main__":
    unittest.main()
