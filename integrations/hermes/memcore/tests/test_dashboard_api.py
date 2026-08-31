"""Regression tests for the MemCore dashboard API."""
import importlib.util
import os
import pathlib
import tempfile
import unittest

API_PATH = pathlib.Path(__file__).resolve().parents[1] / 'dashboard' / 'plugin_api.py'
spec = importlib.util.spec_from_file_location('memcore_dashboard_api', API_PATH)
api = importlib.util.module_from_spec(spec)
spec.loader.exec_module(api)

from memcore import core, store  # noqa: E402


class DashboardApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='memcore_dashboard_')
        self.db_path = str(pathlib.Path(self.tmp.name) / 'memory.db')
        os.environ['MEMCORE_STORE_PATH'] = self.db_path
        api._local = api.threading.local()
        self.conn = store.open_store(self.db_path)
        for pid, name in (('proj-one', 'one'), ('proj-two', 'two')):
            self.conn.execute('INSERT INTO project (id, name) VALUES (?, ?)', (pid, name))
        self.conn.execute("INSERT INTO agent (id, name, profile_key) VALUES ('agent-a','a','a')")
        for pid in ('proj-one', 'proj-two'):
            self.conn.execute("INSERT INTO project_membership (project_id,agent_id,role) VALUES (?, 'agent-a','owner')", (pid,))
        self.conn.commit()
    def tearDown(self):
        conn = getattr(api._local, 'conn', None)
        if conn is not None:
            conn.close()
        self.conn.close()
        os.environ.pop('MEMCORE_STORE_PATH', None)
        self.tmp.cleanup()

    def remember(self, project, content, lifecycle='candidate'):
        return core.create_memory(
            self.conn, project, 'agent-a', content,
            scope='project', lifecycle=lifecycle
        )[0]

    def test_state_counts_are_project_scoped(self):
        self.remember('proj-one', 'one candidate')
        self.remember('proj-two', 'two candidate')
        self.remember('proj-two', 'two accepted', lifecycle='accepted')
        result = api.state(project='one')
        self.assertEqual(result['counts'], {'candidate': 1})

    def test_store_path_reads_quoted_path_with_spaces(self):
        config_home = pathlib.Path(self.tmp.name) / 'hermes-config'
        config_home.mkdir()
        expected = 'C:/Temp/My Memory/memory.db'
        (config_home / 'config.yaml').write_text(
            'plugins:\n  entries:\n    memcore:\n      settings:\n'
            f'        store_path: "{expected}"\n',
            encoding='utf-8'
        )
        old_home = os.environ.get('HERMES_HOME')
        old_override = os.environ.pop('MEMCORE_STORE_PATH', None)
        os.environ['HERMES_HOME'] = str(config_home)
        try:
            self.assertEqual(api._store_path(), expected)
        finally:
            if old_override is not None:
                os.environ['MEMCORE_STORE_PATH'] = old_override
            if old_home is None:
                os.environ.pop('HERMES_HOME', None)
            else:
                os.environ['HERMES_HOME'] = old_home

    def test_punctuation_only_search_returns_empty(self):
        self.remember('proj-one', 'searchable dashboard fact')
        self.assertEqual(api.search(q='!!! ???', project='one')['items'], [])

    def test_dashboard_search_supports_thai_unicode(self):
        self.remember('proj-one', 'ระบบความจำร่วมสำหรับเอเจนต์')
        result = api.search(q='ระบบความจำร่วม', project='one')
        self.assertEqual(len(result['items']), 1)
        self.assertIn('ระบบความจำร่วม', result['items'][0]['content'])

    def test_promote_does_not_resurrect_rejected_memory(self):
        mem_id = self.remember('proj-one', 'wrong rejected claim')
        core.reject(self.conn, mem_id, 'agent-a', 'wrong')
        result = api.promote({'memory_id': mem_id})
        self.assertEqual(result['error'], 'invalid state')
        row = self.conn.execute(
            'SELECT lifecycle FROM memory WHERE id=?', (mem_id,)
        ).fetchone()
        self.assertEqual(row[0], 'rejected')

    def test_promote_blocked_by_tombstone_from_duplicate_claim(self):
        content = 'dashboard duplicate candidate tombstone'
        first = self.remember('proj-one', content)
        second = self.remember('proj-one', content)
        core.reject(self.conn, second, 'agent-a', 'wrong')
        result = api.promote({'memory_id': first})
        self.assertEqual(result['error'], 'tombstone blocked')
        row = self.conn.execute(
            'SELECT lifecycle, scope FROM memory WHERE id=?', (first,)
        ).fetchone()
        self.assertEqual(row[0], 'candidate')
        self.assertEqual(row[1], 'project')

    def test_tombstones_filter_by_project(self):
        one = self.remember('proj-one', 'rejected in project one')
        two = self.remember('proj-two', 'rejected in project two')
        core.reject(self.conn, one, 'agent-a', 'one wrong')
        core.reject(self.conn, two, 'agent-a', 'two wrong')
        result = api.memories(state='tombstones', project='one')
        self.assertEqual(len(result['items']), 1)
        self.assertEqual(result['items'][0]['scope'], 'proj-one')

    def test_project_filter_accepts_exact_project_id(self):
        pid = 'uuid-project-123'
        self.conn.execute('INSERT INTO project (id,name) VALUES (?,?)', (pid, 'renamed'))
        self.conn.execute(
            "INSERT INTO project_membership (project_id,agent_id,role) "
            "VALUES (?, 'agent-a', 'owner')", (pid,)
        )
        self.remember(pid, 'uuid project candidate')
        self.assertEqual(api.state(project=pid)['counts'], {'candidate': 1})
        self.assertEqual(len(api.memories(project=pid)['items']), 1)
        self.assertEqual(len(api.search(q='uuid project', project=pid)['items']), 1)

    def test_ambiguous_project_slug_fails_closed(self):
        self.conn.execute('DROP INDEX IF EXISTS idx_project_name_unique')
        self.conn.execute("INSERT INTO project (id,name) VALUES ('project-a','duplicate')")
        self.conn.execute("INSERT INTO project (id,name) VALUES ('project-b','duplicate')")
        with self.assertRaises(api.HTTPException) as cm:
            api.state(project='duplicate')
        self.assertEqual(cm.exception.status_code, 409)

    def test_read_limits_reject_nonpositive_values(self):
        for fn in (
            lambda: api.memories(limit=0),
            lambda: api.memories(limit=-1),
            lambda: api.search(q='anything', limit=0),
            lambda: api.search(q='anything', limit=-1),
        ):
            with self.subTest(fn=fn), self.assertRaises(api.HTTPException) as cm:
                fn()
            self.assertEqual(cm.exception.status_code, 400)

    def test_project_tombstones_include_global_guards(self):
        self.conn.execute(
            "INSERT INTO tombstone (id,claim_fingerprint,scope,reason) "
            "VALUES ('t-global','abcdef0123456789','global','globally rejected')"
        )
        result = api.memories(state='tombstones', project='one')
        scopes = {item['scope'] for item in result['items']}
        self.assertIn('global', scopes)

    def test_project_tombstones_include_private_owner_guards_for_operator(self):
        mem_id, _ = core.create_memory(
            self.conn, 'proj-one', 'agent-a', 'private dashboard rejection', scope='private'
        )
        core.reject(self.conn, mem_id, 'agent-a', 'private wrong')
        result = api.memories(state='tombstones', project='one')
        scopes = {item['scope'] for item in result['items']}
        self.assertIn(core._private_tombstone_scope('proj-one', 'agent-a'), scopes)

    def test_dashboard_disable_records_previous_lifecycle_for_restore(self):
        mem_id = self.remember('proj-one', 'dashboard reversible accepted', lifecycle='accepted')
        result = api.disable({'memory_id': mem_id})
        self.assertTrue(result['success'])
        detail = self.conn.execute(
            "SELECT detail FROM audit_event WHERE memory_id=? AND action='disable' ORDER BY id DESC LIMIT 1",
            (mem_id,)
        ).fetchone()[0]
        self.assertIn('accepted', detail)
        core.restore(self.conn, mem_id, 'agent-a')
        self.assertEqual(self.conn.execute(
            'SELECT lifecycle FROM memory WHERE id=?', (mem_id,)
        ).fetchone()[0], 'accepted')

    def test_disable_does_not_mutate_rejected_memory(self):
        mem_id = self.remember('proj-one', 'rejected terminal dashboard claim')
        core.reject(self.conn, mem_id, 'agent-a', 'wrong')
        result = api.disable({'memory_id': mem_id})
        self.assertEqual(result['error'], 'invalid state')
        lifecycle = self.conn.execute(
            'SELECT lifecycle FROM memory WHERE id=?', (mem_id,)
        ).fetchone()[0]
        self.assertEqual(lifecycle, 'rejected')

    def test_pin_rolls_back_if_audit_fails(self):
        mem_id = self.remember('proj-one', 'atomic dashboard pin')
        original = api._audit
        api._audit = lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError('audit fail'))
        try:
            with self.assertRaises(RuntimeError):
                api.pin({'memory_id': mem_id, 'pinned': True})
        finally:
            api._audit = original
        pinned = self.conn.execute(
            'SELECT pinned FROM memory WHERE id=?', (mem_id,)
        ).fetchone()[0]
        self.assertEqual(pinned, 0)

    def test_dashboard_mutations_write_iso_z_timestamps(self):
        mem_id = self.remember('proj-one', 'iso timestamp dashboard mutation')
        result = api.pin({'memory_id': mem_id, 'pinned': True})
        self.assertTrue(result['success'])
        updated_at = self.conn.execute(
            'SELECT updated_at FROM memory WHERE id=?', (mem_id,)
        ).fetchone()[0]
        audit_created = self.conn.execute(
            "SELECT created_at FROM audit_event WHERE memory_id=? AND action='pin' "
            'ORDER BY id DESC LIMIT 1', (mem_id,)
        ).fetchone()[0]
        self.assertTrue(updated_at.endswith('Z'), updated_at)
        self.assertTrue(audit_created.endswith('Z'), audit_created)

    def test_memory_detail_deduplicates_shared_evidence_and_keeps_audit_actions(self):
        mem_id, first_ver = core.create_memory(
            self.conn, 'proj-one', 'agent-a', 'detail version one', scope='project'
        )
        second_ver = core.supersede(
            self.conn, mem_id, 'agent-a', 'detail version two', reason='detail correction'
        )
        evidence_id = core._new_id('ev')
        self.conn.execute(
            'INSERT INTO evidence (id,kind,source_uri,source_label) VALUES (?,?,?,?)',
            (evidence_id, 'test', 'test://shared', 'shared evidence')
        )
        for version_id in (first_ver, second_ver):
            self.conn.execute(
                "INSERT INTO evidence_link (evidence_id,memory_version_id,relation) "
                "VALUES (?,?,'supports')", (evidence_id, version_id)
            )
        self.conn.execute(
            "UPDATE memory_version SET created_at='2026-01-01T00:00:00Z' "
            'WHERE memory_id=?', (mem_id,)
        )
        self.conn.execute(
            "UPDATE audit_event SET created_at='2026-01-01T00:00:00Z' "
            'WHERE memory_id=?', (mem_id,)
        )
        detail = api.memory_detail(mem_id)
        self.assertEqual(len(detail['evidence']), 1)
        self.assertEqual(
            [v['id'] for v in detail['versions']], sorted([first_ver, second_ver])
        )
        actions = [row['action'] for row in detail['audit']]
        self.assertIn('create', actions)
        self.assertIn('supersede', actions)

    def test_dashboard_audit_includes_project_id(self):
        mem_id = self.remember('proj-one', 'pin this dashboard memory')
        result = api.pin({'memory_id': mem_id, 'pinned': True})
        self.assertTrue(result['success'])
        row = self.conn.execute(
            "SELECT project_id FROM audit_event WHERE memory_id=? "
            "AND action='pin' ORDER BY id DESC LIMIT 1", (mem_id,)
        ).fetchone()
        self.assertEqual(row[0], 'proj-one')


if __name__ == '__main__':
    unittest.main()
