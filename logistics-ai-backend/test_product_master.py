"""Independent core contracts; no provider, credentials, or production data."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import module_scope
import product_profile as profile
from capabilities import work_tasks
from industry_packs import PACKS, extension
from scripts.create_enterprise import provision


class ProductMasterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_unconfigured_is_not_pilot(self):
        p = profile.load(self.root, {})
        self.assertEqual(p['industry'], 'general')
        self.assertEqual(p['modules'], ())
        self.assertNotEqual(p['tenant_id'], profile.LEGACY_ID)
        with sqlite3.connect(':memory:') as con, self.assertRaises(RuntimeError):
            profile.bind_database(con, p)

    def test_blank_provisioning_and_binding(self):
        profiles = []
        for tenant in ('company-alpha', 'company-beta'):
            config = provision(self.root / tenant, tenant, tenant)
            data = config.parent / 'data'
            self.assertEqual(list(data.iterdir()), [])
            self.assertEqual(set(json.loads(config.read_text())), {'tenant_id', 'name', 'industry', 'modules'})
            profiles.append(profile.load(self.root, {'BOT_PROFILE': str(config), 'DATA_DIR': str(data)}))
        with sqlite3.connect(':memory:') as con:
            profile.bind_database(con, profiles[0])
            with self.assertRaises(RuntimeError):
                profile.bind_database(con, profiles[1])
        with self.assertRaises(FileExistsError):
            provision(self.root / 'company-alpha', 'company-alpha', 'duplicate')
        with self.assertRaises(ValueError):
            provision(self.root / 'invalid', profile.LEGACY_ID, 'invalid')
        self.assertFalse((self.root / 'invalid').exists())

    def test_registry_and_role_projection(self):
        p = {'industry': 'general', 'modules': (), 'name': 'Example'}
        self.assertFalse(profile.public_profile(p, 'owner_finance')['modules'])
        self.assertIn('update_fee_status', module_scope.blocked_tools(p))
        self.assertTrue(module_scope.route_denied(p, '/api/finance/candidates'))
        p.update(industry='logistics', modules=('transport', 'receipt', 'finance'))
        self.assertEqual(len(profile.public_profile(p, 'owner_finance')['modules']), 3)
        self.assertEqual(len(profile.public_profile(p, 'employee')['modules']), 2)
        p['modules'] = ('receipt',)
        self.assertEqual(profile.public_profile(p, 'employee')['modules'][0]['actions'], [])

    def test_registered_adapter_execution_and_route(self):
        # A test-only extension reads a real in-memory SQLite record. It is not a shipped industry.
        with sqlite3.connect(':memory:') as db:
            db.execute('CREATE TABLE records(id TEXT, value TEXT)')
            db.execute('INSERT INTO records VALUES(?,?)', ('R1', 'record'))
            def read(host, user, args, key):
                row = host.execute('SELECT value FROM records WHERE id=?', (args['id'],)).fetchone()
                return {'value': row[0]} if row else {'error': 'not_found'}
            path = '/api/industry/testpack/records'
            pack = {'modules': {'test-records': {'roles': ('owner_finance',)}}, 'business_tools': True,
                    'tool_groups': {'test-records': {'test_read'}}, 'route_groups': {'test-records': (path,)}, 'state_groups': {},
                    'adapter_tools': {'test-records': {'test_read': {'read_only': True, 'execute': read,
                        'schema': {'function': {'name': 'test_read', 'parameters': {'properties': {'id': {}}, 'required': ['id']}}}}}},
                    'adapter_routes': {('GET', path): {'module': 'test-records', 'execute': lambda host, user, query, body, key: (200, read(host, user, {'id': 'R1'}, key))}}}
            with patch.dict(PACKS, {'testpack': pack}):
                p = {'industry': 'testpack', 'modules': ('test-records',)}
                owner, employee = {'role': 'owner_finance'}, {'role': 'employee'}
                self.assertEqual(len(extension.list_tools(p, owner)), 1)
                self.assertEqual(extension.list_tools(p, employee), [])
                self.assertEqual(extension.execute(p, db, owner, 'test_read', {'id': 'R1'}, 'key'), (True, {'value': 'record'}))
                self.assertEqual(extension.execute(p, db, employee, 'test_read', {'id': 'R1'}, 'key'), (True, {'error': 'forbidden'}))
                self.assertFalse(extension.mutating_tool(p, 'test_read'))
                self.assertEqual(extension.route(p, db, owner, 'GET', path, {}, {}, ''), (200, {'value': 'record'}))
                p['modules'] = ()
                self.assertEqual(extension.list_tools(p, owner), [])
                self.assertEqual(extension.route(p, db, owner, 'GET', path, {}, {}, '')[0], 404)
                self.assertIn('test_read', module_scope.blocked_tools(p))

    def test_real_task_query_tenant_and_employee_scope(self):
        con = sqlite3.connect(':memory:')
        self.addCleanup(con.close)
        con.row_factory = sqlite3.Row
        con.executescript('CREATE TABLE users(id TEXT,tenant_id TEXT); CREATE TABLE tasks(id TEXT,title TEXT,description TEXT,status TEXT,assignee_id TEXT,due_at TEXT,created_at TEXT,created_by TEXT);')
        con.executemany('INSERT INTO users VALUES(?,?)', [('a','alpha'), ('b','beta')])
        con.executemany('INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?)', [
            ('1','first','', 'open','employee-a',None,'1','a'),
            ('2','second','', 'open','employee-b',None,'2','a'),
            ('3','other company','', 'open','employee-a',None,'3','b'),
            ('4','completed','', 'done','employee-a',None,'4','a')])
        class Host:
            def db(self): return con
            def _redact_tree(self, row): return row
        h = Host()
        owner = {'tenant_id':'alpha','role':'owner_finance','id':'a'}
        employee = {'tenant_id':'alpha','role':'employee','id':'employee-a'}
        self.assertEqual(work_tasks.today(h, owner)['work_summary']['total'], 2)
        self.assertEqual([r['id'] for r in work_tasks.list_tasks(h, employee, {})['items']], ['1'])
        self.assertEqual(work_tasks.list_tasks(h, employee, {'status':'all'})['total'], 2)
        self.assertEqual(work_tasks.list_tasks(h, owner, {'limit':1})['next_offset'], 1)


if __name__ == '__main__':
    unittest.main()
