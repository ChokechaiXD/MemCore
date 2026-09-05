"""Semantic review queue and governed verdict application tests."""
import os
import tempfile
import unittest

from memcore import core, ingest, store


class SemanticAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='memcore_semantic_')
        self.db_path = os.path.join(self.tmpdir, 'semantic.db')
        self.conn = store.open_store(self.db_path)
        self.project = 'proj-demo'
        self.alice = 'agent-alice'
        self.bob = 'agent-bob'
        self.conn.execute("INSERT INTO project (id, name) VALUES (?, 'demo')", (self.project,))
        for aid, role in ((self.alice, 'owner'), (self.bob, 'member')):
            name = aid.removeprefix('agent-')
            self.conn.execute(
                'INSERT INTO agent (id, name, profile_key) VALUES (?, ?, ?)',
                (aid, name, name)
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

    def pending_event(self, text='The deployment behavior changed after migration.',
                      session_id='semantic-review'):
        event_id, _ = ingest.append_event(
            self.conn, self.project, self.alice, 'turn', session_id=session_id,
            user_content=text, assistant_content='Acknowledged.'
        )
        result = ingest.process_event(self.conn, event_id)
        self.assertEqual(result['decision'], 'semantic_review_required')
        return event_id

    def test_pending_queue_is_owner_scoped(self):
        event_id = self.pending_event()
        queue = ingest.pending_semantic_events(self.conn, self.project, self.alice)
        self.assertEqual([item['event_id'] for item in queue], [event_id])
        self.assertEqual(ingest.pending_semantic_events(
            self.conn, self.project, self.bob
        ), [])
        with self.assertRaises(core.PermissionDenied):
            ingest.semantic_analysis_history(self.conn, event_id, self.bob)

    def test_remember_creates_only_private_candidate(self):
        event_id = self.pending_event()
        result = ingest.apply_semantic_analysis(
            self.conn, event_id, self.alice,
            analyzer='hermes-semantic-v1', verdict='remember',
            candidate_content='deployment now requires the migration step',
            confidence=0.91, rationale='Durable operational constraint.'
        )
        self.assertEqual(result['decision'], 'semantic_private_candidate')
        memory = self.conn.execute(
            'SELECT scope,lifecycle,owner_agent_id FROM memory WHERE id=?',
            (result['memory_id'],)
        ).fetchone()
        self.assertEqual(memory, ('private', 'candidate', self.alice))
        history = ingest.semantic_analysis_history(self.conn, event_id, self.alice)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]['verdict'], 'remember')
        self.assertEqual(history[0]['memory_id'], result['memory_id'])

    def test_oversized_semantic_candidate_is_rejected_without_side_effects(self):
        event_id = self.pending_event()
        with self.assertRaises(core.MemCoreError):
            ingest.apply_semantic_analysis(
                self.conn, event_id, self.alice, analyzer='test', verdict='remember',
                candidate_content='x' * 4001,
            )
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM memory').fetchone()[0], 0)
        self.assertEqual(len(ingest.pending_semantic_events(self.conn, self.project, self.alice)), 1)

    def test_ignore_is_terminal_without_memory(self):
        event_id = self.pending_event()
        result = ingest.apply_semantic_analysis(
            self.conn, event_id, self.alice,
            analyzer='hermes-semantic-v1', verdict='ignore',
            rationale='Transient conversational detail.'
        )
        self.assertEqual(result['status'], 'ignored')
        self.assertEqual(result['decision'], 'semantic_ignored')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM memory').fetchone()[0], 0)

    def test_defer_stays_in_queue_and_can_later_remember(self):
        event_id = self.pending_event()
        deferred = ingest.apply_semantic_analysis(
            self.conn, event_id, self.alice,
            analyzer='hermes-semantic-v1', verdict='defer',
            rationale='Need more context.'
        )
        self.assertEqual(deferred['decision'], 'semantic_deferred')
        self.assertEqual(len(ingest.pending_semantic_events(
            self.conn, self.project, self.alice
        )), 1)
        remembered = ingest.apply_semantic_analysis(
            self.conn, event_id, self.alice,
            analyzer='hermes-semantic-v2', verdict='remember',
            candidate_content='deployment requires migration context before restart'
        )
        self.assertEqual(remembered['decision'], 'semantic_private_candidate')
        self.assertEqual(len(ingest.semantic_analysis_history(
            self.conn, event_id, self.alice
        )), 2)

    def test_duplicate_links_existing_private_claim(self):
        first = self.pending_event(
            'A durable deployment detail changed.', session_id='semantic-review-1'
        )
        created = ingest.apply_semantic_analysis(
            self.conn, first, self.alice, analyzer='a1', verdict='remember',
            candidate_content='unique semantic duplicate marker'
        )
        second = self.pending_event(
            'Another ambiguous deployment observation.', session_id='semantic-review-2'
        )
        duplicate = ingest.apply_semantic_analysis(
            self.conn, second, self.alice, analyzer='a2', verdict='remember',
            candidate_content='unique semantic duplicate marker'
        )
        self.assertEqual(duplicate['decision'], 'semantic_duplicate')
        self.assertEqual(duplicate['memory_id'], created['memory_id'])

    def test_review_rejects_cross_agent_and_non_review_events(self):
        event_id = self.pending_event()
        with self.assertRaises(core.PermissionDenied):
            ingest.apply_semantic_analysis(
                self.conn, event_id, self.bob, analyzer='bad', verdict='ignore'
            )
        explicit, _ = ingest.append_event(
            self.conn, self.project, self.alice, 'turn', session_id='explicit-semantic',
            user_content='remember that semantic direct writes are guarded'
        )
        ingest.process_event(self.conn, explicit)
        with self.assertRaises(core.MemCoreError):
            ingest.apply_semantic_analysis(
                self.conn, explicit, self.alice, analyzer='late', verdict='ignore'
            )

    def test_pending_queue_can_filter_review_vs_deferred_decisions(self):
        event_id = self.pending_event()
        review_only = ingest.pending_semantic_events(
            self.conn, self.project, self.alice,
            decisions=('semantic_review_required',)
        )
        self.assertEqual([item['event_id'] for item in review_only], [event_id])
        ingest.apply_semantic_analysis(
            self.conn, event_id, self.alice,
            analyzer='filter-test', verdict='defer', rationale='later'
        )
        self.assertEqual(ingest.pending_semantic_events(
            self.conn, self.project, self.alice,
            decisions=('semantic_review_required',)
        ), [])
        deferred = ingest.pending_semantic_events(
            self.conn, self.project, self.alice,
            decisions=('semantic_deferred',)
        )
        self.assertEqual([item['event_id'] for item in deferred], [event_id])
        with self.assertRaises(core.MemCoreError):
            ingest.pending_semantic_events(
                self.conn, self.project, self.alice,
                decisions=('builtin_memory_replace_unresolved_target',)
            )

    def test_validation_fails_closed(self):
        event_id = self.pending_event()
        with self.assertRaises(core.MemCoreError):
            ingest.apply_semantic_analysis(
                self.conn, event_id, self.alice, analyzer='', verdict='remember',
                candidate_content='x'
            )
        with self.assertRaises(core.MemCoreError):
            ingest.apply_semantic_analysis(
                self.conn, event_id, self.alice, analyzer='a', verdict='remember'
            )
        with self.assertRaises(core.MemCoreError):
            ingest.apply_semantic_analysis(
                self.conn, event_id, self.alice, analyzer='a', verdict='ignore', confidence=1.5
            )

    def test_defer_confidence_changes_are_distinct_audit_records(self):
        event_id = self.pending_event()
        first = ingest.apply_semantic_analysis(
            self.conn, event_id, self.alice, analyzer='same-analyzer', verdict='defer',
            confidence=0.4, rationale='same rationale'
        )
        second = ingest.apply_semantic_analysis(
            self.conn, event_id, self.alice, analyzer='same-analyzer', verdict='defer',
            confidence=0.8, rationale='same rationale'
        )
        self.assertNotEqual(first['analysis_id'], second['analysis_id'])
        history = ingest.semantic_analysis_history(self.conn, event_id, self.alice)
        self.assertEqual([item['confidence'] for item in history], [0.4, 0.8])

    def test_replay_after_processed_is_idempotent(self):
        event_id = self.pending_event()
        first = ingest.apply_semantic_analysis(
            self.conn, event_id, self.alice, analyzer='a', verdict='remember',
            candidate_content='semantic replay marker'
        )
        replay = ingest.apply_semantic_analysis(
            self.conn, event_id, self.alice, analyzer='a', verdict='remember',
            candidate_content='semantic replay marker'
        )
        self.assertEqual(replay['status'], 'processed')
        self.assertEqual(replay['memory_id'], first['memory_id'])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM memory').fetchone()[0], 1)


if __name__ == '__main__':
    unittest.main()
