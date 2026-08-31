"""CLI regression tests for safe import preview."""
import contextlib
import io
import json
import os
import sqlite3
import tempfile
import unittest

from memcore import __main__ as cli
from memcore import core, store


class CliImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='memcore_cli_')
        self.db = os.path.join(self.tmp.name, 'memory.db')
        self.batch = os.path.join(self.tmp.name, 'batch.json')
        conn = store.open_store(self.db)
        conn.execute("INSERT INTO project (id, name) VALUES ('proj-demo', 'demo')")
        conn.commit()
        conn.close()
        with open(self.batch, 'w', encoding='utf-8') as f:
            json.dump([
                {'summary': 'safe import claim', 'type': 'fact'},
                {'summary': 'safe import claim', 'type': 'fact'},
                {'summary': '   ', 'type': 'fact'},
                {'summary': 'bad evidence', 'evidence': 'not-a-list'},
            ], f)

    def tearDown(self):
        self.tmp.cleanup()

    def test_import_dry_run_performs_zero_domain_writes(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            cli.main([
                '--db', self.db, 'import', '--file', self.batch,
                '--agent', 'previewer', '--project', 'demo', '--dry-run'
            ])
        text = output.getvalue()
        self.assertIn('would add: 1', text)
        self.assertIn('duplicate_input: 1', text)
        self.assertIn('empty_summary: 1', text)
        self.assertIn('invalid_evidence: 1', text)
        self.assertIn('writes performed: 0', text)

        conn = store.open_store(self.db)
        try:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM memory').fetchone()[0], 0)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM agent WHERE id='agent-previewer'"
            ).fetchone()[0], 0)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM project_membership WHERE agent_id='agent-previewer'"
            ).fetchone()[0], 0)
        finally:
            conn.close()

    def test_import_setup_rolls_back_if_join_audit_fails(self):
        original = core._audit
        core._audit = lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError('audit fail'))
        try:
            with self.assertRaises(RuntimeError):
                cli.main([
                    '--db', self.db, 'import', '--file', self.batch,
                    '--agent', 'brokenjoin', '--project', 'demo'
                ])
        finally:
            core._audit = original
        conn = store.open_store(self.db)
        try:
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM agent WHERE id='agent-brokenjoin'"
            ).fetchone()[0], 0)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM project_membership WHERE agent_id='agent-brokenjoin'"
            ).fetchone()[0], 0)
        finally:
            conn.close()

    def test_dry_run_missing_store_does_not_create_database(self):
        missing = os.path.join(self.tmp.name, 'missing.db')
        output = io.StringIO()
        with self.assertRaises(SystemExit), contextlib.redirect_stdout(output):
            cli.main([
                '--db', missing, 'import', '--file', self.batch,
                '--agent', 'previewer', '--project', 'demo', '--dry-run'
            ])
        self.assertFalse(os.path.exists(missing))

    def test_gc_dry_run_missing_store_does_not_create_database(self):
        missing = os.path.join(self.tmp.name, 'missing-gc.db')
        with self.assertRaises(SystemExit):
            cli.main(['--db', missing, 'gc'])
        self.assertFalse(os.path.exists(missing))

    def test_doctor_missing_store_does_not_create_database(self):
        missing = os.path.join(self.tmp.name, 'missing-doctor.db')
        with self.assertRaises(SystemExit):
            cli.main(['--db', missing, 'doctor'])
        self.assertFalse(os.path.exists(missing))

    def test_read_commands_do_not_create_missing_store(self):
        cases = [
            ['stats'],
            ['project', 'list'],
            ['search', '--project', 'demo', '--agent', 'previewer', 'anything'],
        ]
        for index, command in enumerate(cases):
            missing = os.path.join(self.tmp.name, f'missing-read-{index}.db')
            with self.subTest(command=command), self.assertRaises(SystemExit):
                cli.main(['--db', missing] + command)
            self.assertFalse(os.path.exists(missing))

    def test_member_add_missing_agent_is_clear_and_releases_store(self):
        with self.assertRaises(SystemExit) as cm:
            cli.main(['--db', self.db, 'member', 'add', 'demo', 'ghost'])
        self.assertIn('agent agent-ghost does not exist', str(cm.exception))
        moved = self.db + '.moved'
        os.replace(self.db, moved)
        os.replace(moved, self.db)

    def test_member_add_does_not_claim_role_change_it_did_not_apply(self):
        conn = store.open_store(self.db)
        conn.execute("INSERT INTO agent (id,name,profile_key) VALUES ('agent-a','a','a')")
        conn.execute(
            "INSERT INTO project_membership (project_id,agent_id,role) "
            "VALUES ('proj-demo','agent-a','member')"
        )
        conn.close()
        with self.assertRaises(SystemExit) as cm:
            cli.main([
                '--db', self.db, 'member', 'add', 'demo', 'a', '--role', 'owner'
            ])
        self.assertIn('already exists with role member', str(cm.exception))
        conn = store.open_store(self.db)
        try:
            role = conn.execute(
                "SELECT role FROM project_membership WHERE project_id='proj-demo' "
                "AND agent_id='agent-a'"
            ).fetchone()[0]
            self.assertEqual(role, 'member')
        finally:
            conn.close()

    def test_failed_memory_write_command_releases_store(self):
        with self.assertRaises(SystemExit):
            cli.main([
                '--db', self.db, 'remember', '--project', 'demo',
                '--agent', 'ghost', 'must fail closed'
            ])
        moved = self.db + '.moved'
        os.replace(self.db, moved)
        os.replace(moved, self.db)

    def test_doctor_detects_orphaned_idempotency_rows(self):
        conn = store.open_store(self.db)
        conn.execute(
            "INSERT INTO idempotency_key (key,project_id,memory_id,version_id) "
            "VALUES ('broken','proj-demo','mem-missing','ver-missing')"
        )
        conn.close()
        output = io.StringIO()
        with self.assertRaises(SystemExit) as cm, contextlib.redirect_stdout(output):
            cli.main(['--db', self.db, 'doctor'])
        self.assertEqual(cm.exception.code, 1)
        self.assertIn('idempotency violations: 1', output.getvalue())

    def test_doctor_detects_crash_visible_migration_lock(self):
        conn = store.open_store(self.db)
        conn.execute(
            'INSERT INTO schema_migrations '
            '(version, applied_at, lock_holder, lock_until) '
            "VALUES (?, datetime('now'), 'crashed-worker', 0)",
            (store._MIGRATION_LOCK_VERSION,)
        )
        conn.close()
        output = io.StringIO()
        with self.assertRaises(SystemExit) as cm, contextlib.redirect_stdout(output):
            cli.main(['--db', self.db, 'doctor'])
        self.assertEqual(cm.exception.code, 1)
        self.assertIn('crashed-worker', output.getvalue())

    def _write_hermes_config(self, agent, project='demo'):
        home = os.path.join(self.tmp.name, 'hermes-home')
        os.makedirs(home, exist_ok=True)
        with open(os.path.join(home, 'config.yaml'), 'w', encoding='utf-8') as f:
            f.write(
                'plugins:\n  enabled: [memcore]\n  entries:\n    memcore:\n'
                '      settings:\n'
                f'        agent_name: {agent}\n'
                f'        default_project: {project}\n'
                f'        store_path: {json.dumps(self.db)}\n'
            )
        return home

    def test_doctor_accepts_matching_enabled_profile_binding(self):
        home = self._write_hermes_config('checker')
        conn = store.open_store(self.db)
        conn.execute("INSERT INTO agent (id,name,profile_key) VALUES ('agent-checker','checker','checker')")
        conn.execute(
            "INSERT INTO project_membership (project_id,agent_id,role) "
            "VALUES ('proj-demo','agent-checker','member')"
        )
        conn.close()
        old = os.environ.get('HERMES_HOME')
        os.environ['HERMES_HOME'] = home
        output = io.StringIO()
        try:
            with contextlib.redirect_stdout(output):
                cli.main(['--db', self.db, 'doctor'])
        finally:
            if old is None:
                os.environ.pop('HERMES_HOME', None)
            else:
                os.environ['HERMES_HOME'] = old
        self.assertIn('default: OK', output.getvalue())

    def test_doctor_detects_enabled_profile_membership_drift(self):
        home = self._write_hermes_config('missing-agent')
        old = os.environ.get('HERMES_HOME')
        os.environ['HERMES_HOME'] = home
        output = io.StringIO()
        try:
            with self.assertRaises(SystemExit) as cm, contextlib.redirect_stdout(output):
                cli.main(['--db', self.db, 'doctor'])
            self.assertEqual(cm.exception.code, 1)
        finally:
            if old is None:
                os.environ.pop('HERMES_HOME', None)
            else:
                os.environ['HERMES_HOME'] = old
        self.assertIn('missing_membership:missing-agent->demo', output.getvalue())

    def test_doctor_detects_agent_name_collision(self):
        home = self._write_hermes_config('checker')
        conn = store.open_store(self.db)
        conn.execute("INSERT INTO agent (id,name,profile_key) VALUES ('agent-checker','checker','checker')")
        conn.execute("INSERT INTO agent (id,name,profile_key) VALUES ('agent-other','checker','other')")
        conn.execute("INSERT INTO project_membership VALUES ('proj-demo','agent-checker','member',datetime('now'))")
        conn.execute("INSERT INTO project_membership VALUES ('proj-demo','agent-other','member',datetime('now'))")
        conn.close()
        old = os.environ.get('HERMES_HOME')
        os.environ['HERMES_HOME'] = home
        output = io.StringIO()
        try:
            with self.assertRaises(SystemExit), contextlib.redirect_stdout(output):
                cli.main(['--db', self.db, 'doctor'])
        finally:
            if old is None:
                os.environ.pop('HERMES_HOME', None)
            else:
                os.environ['HERMES_HOME'] = old
        self.assertIn("('demo', 'checker', 2)", output.getvalue())

    def test_doctor_fails_if_journal_mode_is_not_wal(self):
        raw = sqlite3.connect(self.db)
        try:
            mode = raw.execute('PRAGMA journal_mode=DELETE').fetchone()[0]
            self.assertEqual(mode.lower(), 'delete')
        finally:
            raw.close()
        output = io.StringIO()
        with self.assertRaises(SystemExit) as cm, contextlib.redirect_stdout(output):
            cli.main(['--db', self.db, 'doctor'])
        self.assertEqual(cm.exception.code, 1)
        self.assertIn('journal mode: delete', output.getvalue())

    def test_gc_rejects_negative_retention_without_mutating_store(self):
        conn = store.open_store(self.db)
        conn.execute("INSERT INTO agent (id,name,profile_key) VALUES ('agent-a','a','a')")
        conn.execute(
            "INSERT INTO project_membership (project_id,agent_id,role) "
            "VALUES ('proj-demo','agent-a','member')"
        )
        mem_id, _ = core.create_memory(
            conn, 'proj-demo', 'agent-a', 'must survive negative gc', scope='project'
        )
        conn.close()
        with self.assertRaises(SystemExit):
            cli.main(['--db', self.db, 'gc', '--candidate-days', '-1', '--apply'])
        conn = store.open_store(self.db)
        try:
            lifecycle = conn.execute(
                'SELECT lifecycle FROM memory WHERE id=?', (mem_id,)
            ).fetchone()[0]
            self.assertEqual(lifecycle, 'candidate')
        finally:
            conn.close()


if __name__ == '__main__':
    unittest.main()
