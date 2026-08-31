"""Tests for raw Hermes ingress journal and conservative analyzer."""
import os
import tempfile
import unittest

from memcore import core, ingest, store


class IngestTestBase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='memcore_ingest_')
        self.db_path = os.path.join(self.tmpdir, 'ingest.db')
        self.conn = store.open_store(self.db_path)
        self.project = 'proj-demo'
        self.alice = 'agent-alice'
        self.bob = 'agent-bob'
        self.conn.execute("INSERT INTO project (id, name) VALUES (?, 'demo')", (self.project,))
        for aid, role in ((self.alice, 'owner'), (self.bob, 'member')):
            self.conn.execute(
                'INSERT INTO agent (id, name, profile_key) VALUES (?, ?, ?)',
                (aid, aid.removeprefix('agent-'), aid.removeprefix('agent-'))
            )
            self.conn.execute(
                'INSERT INTO project_membership (project_id, agent_id, role) VALUES (?, ?, ?)',
                (self.project, aid, role)
            )

    def tearDown(self):
        self.conn.close()
        for suffix in ('', '-wal', '-shm'):
            try:
                os.unlink(self.db_path + suffix)
            except OSError:
                pass


class TestIngestJournal(IngestTestBase):
    def test_migration_creates_journal_tables(self):
        names = {r[0] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        self.assertIn('ingest_event', names)
        self.assertIn('ingest_derivation', names)
        self.assertEqual(store.MIGRATIONS[-1][0], '0008_ingest_journal')

    def test_append_is_retry_safe(self):
        kwargs = dict(
            session_id='s1', user_content='remember that tea is preferred',
            assistant_content='Understood.', metadata={'message_count': 4}
        )
        first, created1 = ingest.append_event(
            self.conn, self.project, self.alice, 'turn', **kwargs
        )
        second, created2 = ingest.append_event(
            self.conn, self.project, self.alice, 'turn', **kwargs
        )
        self.assertEqual(first, second)
        self.assertTrue(created1)
        self.assertFalse(created2)
        self.assertEqual(self.conn.execute(
            'SELECT COUNT(*) FROM ingest_event').fetchone()[0], 1)

    def test_nonmember_cannot_write_journal(self):
        self.conn.execute(
            "INSERT INTO agent (id, name, profile_key) VALUES ('agent-eve','eve','eve')"
        )
        with self.assertRaises(core.PermissionDenied):
            ingest.append_event(
                self.conn, self.project, 'agent-eve', 'turn', user_content='remember this'
            )

    def test_trivial_turn_is_ignored_without_memory(self):
        event_id, _ = ingest.append_event(
            self.conn, self.project, self.alice, 'turn',
            session_id='s2', user_content='โอเค', assistant_content='ครับ'
        )
        result = ingest.process_event(self.conn, event_id)
        self.assertEqual(result['status'], 'ignored')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM memory').fetchone()[0], 0)

    def test_ambiguous_turn_stays_pending_and_is_not_recalled(self):
        text = 'project quasarphoenix may need a different deployment window tomorrow'
        event_id, _ = ingest.append_event(
            self.conn, self.project, self.alice, 'turn',
            session_id='s3', user_content=text, assistant_content='Noted.'
        )
        result = ingest.process_event(self.conn, event_id)
        self.assertEqual(result['status'], 'pending')
        self.assertEqual(result['decision'], 'semantic_review_required')
        self.assertEqual(core.search(
            self.conn, self.project, self.alice, 'quasarphoenix'
        ), [])

    def test_explicit_remember_becomes_private_candidate(self):
        event_id, _ = ingest.append_event(
            self.conn, self.project, self.alice, 'turn', session_id='s4',
            user_content='จำไว้ว่าฉันชอบชาอู่หลง', assistant_content='รับทราบ'
        )
        result = ingest.process_event(self.conn, event_id)
        self.assertEqual(result['decision'], 'private_candidate')
        row = self.conn.execute(
            'SELECT scope, lifecycle, owner_agent_id FROM memory WHERE id=?',
            (result['memory_id'],)
        ).fetchone()
        self.assertEqual(row, ('private', 'candidate', self.alice))
        content = self.conn.execute(
            'SELECT v.content FROM memory m JOIN memory_version v '
            'ON v.id=m.current_version_id WHERE m.id=?', (result['memory_id'],)
        ).fetchone()[0]
        self.assertEqual(content, 'ฉันชอบชาอู่หลง')

    def test_assistant_only_event_never_creates_memory(self):
        event_id, _ = ingest.append_event(
            self.conn, self.project, self.alice, 'turn', session_id='s5',
            assistant_content='We decided to deploy every Friday.'
        )
        result = ingest.process_event(self.conn, event_id)
        self.assertEqual(result['status'], 'ignored')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM memory').fetchone()[0], 0)

    def test_same_claim_from_later_turn_links_as_duplicate(self):
        first, _ = ingest.append_event(
            self.conn, self.project, self.alice, 'turn', session_id='s6',
            user_content='remember that I prefer dark mode',
            metadata={'message_count': 2}
        )
        r1 = ingest.process_event(self.conn, first)
        second, _ = ingest.append_event(
            self.conn, self.project, self.alice, 'turn', session_id='s6',
            user_content='remember that I prefer dark mode',
            metadata={'message_count': 4}
        )
        r2 = ingest.process_event(self.conn, second)
        self.assertEqual(r1['memory_id'], r2['memory_id'])
        self.assertEqual(r2['decision'], 'duplicate')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM memory').fetchone()[0], 1)
        relation = self.conn.execute(
            'SELECT relation FROM ingest_derivation WHERE event_id=?', (second,)
        ).fetchone()[0]
        self.assertEqual(relation, 'duplicate')

    def test_analyzer_failure_preserves_raw_event(self):
        claim = 'I prefer forbidden zebra mode'
        fp = core.fingerprint(claim)
        self.conn.execute(
            'INSERT INTO tombstone (id, claim_fingerprint, scope, reason, created_at) '
            'VALUES (?, ?, ?, ?, ?)',
            ('ts-test', fp, f'private:{self.project}:{self.alice}', 'test', core._now())
        )
        event_id, _ = ingest.append_event(
            self.conn, self.project, self.alice, 'turn', session_id='s7',
            user_content=claim
        )
        with self.assertRaises(core.TombstoneBlocked):
            ingest.process_event(self.conn, event_id)
        row = self.conn.execute(
            'SELECT status, user_content FROM ingest_event WHERE id=?', (event_id,)
        ).fetchone()
        self.assertEqual(row, ('failed', claim))

    def test_stats_exposes_journal_health(self):
        ingest.append_event(
            self.conn, self.project, self.alice, 'turn', session_id='stats',
            user_content='a normal ambiguous turn for later review'
        )
        stats = core.stats(self.conn)
        self.assertEqual(stats['journal'].get('pending'), 1)

    def test_builtin_remove_stays_pending_instead_of_recreating_memory(self):
        event_id, _ = ingest.append_event(
            self.conn, self.project, self.alice, 'memory_write', session_id='rm1',
            user_content='old built-in memory entry', metadata={'action': 'remove'}
        )
        result = ingest.process_event(self.conn, event_id)
        self.assertEqual(result['status'], 'pending')
        self.assertEqual(result['decision'], 'builtin_memory_remove_requires_review')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM memory').fetchone()[0], 0)

    def test_builtin_replace_stays_pending_for_correction_analysis(self):
        event_id, _ = ingest.append_event(
            self.conn, self.project, self.alice, 'memory_write', session_id='rp1',
            user_content='replacement built-in memory entry', metadata={'action': 'replace'}
        )
        result = ingest.process_event(self.conn, event_id)
        self.assertEqual(result['status'], 'pending')
        self.assertEqual(result['decision'], 'builtin_memory_replace_requires_review')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM memory').fetchone()[0], 0)
