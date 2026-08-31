"""Native MemoryProvider integration tests against a real temp MemCore store."""
import json
import os
import pathlib
import tempfile
import unittest
import sys

PLUGIN_ROOT = pathlib.Path(__file__).resolve().parents[1]
REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]
for path in (REPO_ROOT, PLUGIN_ROOT):
    value = str(path)
    if value not in sys.path:
        sys.path.insert(0, value)

from memcore import core, store
from native_provider import MemCoreMemoryProvider, agent_plugin


def config_for(path, agent='alice'):
    return {
        'plugins': {'entries': {'memcore': {'settings': {
            'store_path': path,
            'default_project': 'demo',
            'agent_name': agent,
            'inject': {'budget_chars': 1200, 'max_items': 8},
        }}}}
    }


class NativeProviderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='memcore_native_')
        self.db = os.path.join(self.tmp.name, 'memory.db')
        conn = store.open_store(self.db)
        conn.execute("INSERT INTO project (id, name) VALUES ('proj-demo','demo')")
        conn.execute("INSERT INTO agent (id, name, profile_key) VALUES ('agent-alice','alice','alice')")
        conn.execute("INSERT INTO project_membership (project_id, agent_id, role) VALUES ('proj-demo','agent-alice','owner')")
        conn.close()

    def provider(self):
        p = MemCoreMemoryProvider()
        p._load_config = lambda: config_for(self.db)
        p.initialize('session-1', hermes_home=self.tmp.name,
                     platform='cli', agent_identity='alice')
        return p

    def tearDown(self):
        agent_plugin.reset_conn()
        self.tmp.cleanup()

    def test_provider_contract_and_tools(self):
        p = self.provider()
        self.assertEqual(p.name, 'memcore')
        self.assertTrue(p.is_available())
        names = {s['name'] for s in p.get_tool_schemas()}
        self.assertEqual(names, {
            'memory_remember', 'memory_search', 'memory_promote',
            'memory_supersede', 'memory_reject', 'memory_feedback',
            'memory_review_queue', 'memory_review_decide'
        })
        self.assertIn('journal', p.system_prompt_block().lower())
        self.assertIn('untrusted historical data', p.system_prompt_block().lower())

    def test_prefetch_preserves_governed_trust_labels(self):
        conn = store.open_store(self.db)
        candidate, _ = core.create_memory(
            conn, 'proj-demo', 'agent-alice', 'nebula candidate marker', scope='project'
        )
        accepted, _ = core.create_memory(
            conn, 'proj-demo', 'agent-alice', 'nebula accepted marker', scope='project'
        )
        conn.execute("UPDATE memory SET lifecycle='accepted' WHERE id=?", (accepted,))
        conn.close()
        p = self.provider()
        out = p.prefetch('nebula marker')
        self.assertIn('nebula accepted marker', out)
        self.assertIn('nebula candidate marker', out)
        self.assertIn('candidate | unverified | current', out)
        self.assertIn('accepted | unverified | current', out)
        self.assertIn('per-item MemCore labels are the governing trust semantics',
                      p.system_prompt_block())
        self.assertEqual(p.recall_status().count, 2)

    def test_sync_turn_journals_before_admission(self):
        p = self.provider()
        p.sync_turn(
            'project auroracat may change next week', 'I can review that.',
            session_id='session-1', messages=[{'role': 'user'}, {'role': 'assistant'}]
        )
        conn = store.open_store(self.db)
        try:
            row = conn.execute(
                "SELECT status, decision FROM ingest_event WHERE event_type='turn'"
            ).fetchone()
            self.assertEqual(row, ('pending', 'semantic_review_required'))
            self.assertEqual(core.search(
                conn, 'proj-demo', 'agent-alice', 'auroracat'
            ), [])
        finally:
            conn.close()

    def test_semantic_review_tools_curate_pending_journal(self):
        p = self.provider()
        p.sync_turn(
            'project auroracat may change after the migration window',
            'I can review that later.', session_id='semantic-tool-session'
        )
        queued = json.loads(p.handle_tool_call('memory_review_queue', {'limit': 5}))
        self.assertTrue(queued['success'], queued)
        self.assertEqual(len(queued['events']), 1)
        event_id = queued['events'][0]['event_id']
        self.assertIn('auroracat', queued['events'][0]['user_content'])
        decided = json.loads(p.handle_tool_call(
            'memory_review_decide', {
                'event_id': event_id,
                'verdict': 'remember',
                'content': 'auroracat migration window is operationally significant',
                'confidence': 0.9,
                'rationale': 'Stable project constraint worth retaining as a candidate.',
            }
        ))
        self.assertTrue(decided['success'], decided)
        self.assertEqual(decided['decision'], 'semantic_private_candidate')
        empty = json.loads(p.handle_tool_call('memory_review_queue', {}))
        self.assertEqual(empty['events'], [])
        conn = store.open_store(self.db)
        try:
            results = core.search(
                conn, 'proj-demo', 'agent-alice', 'auroracat migration'
            )
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0][1:3], ('private', 'candidate'))
        finally:
            conn.close()

    def test_explicit_sync_turn_derives_private_candidate(self):
        p = self.provider()
        p.sync_turn('remember that I prefer oolong tea', 'Understood.',
                    session_id='session-2', messages=[{'role': 'user'}])
        conn = store.open_store(self.db)
        try:
            event = conn.execute(
                "SELECT status, decision FROM ingest_event WHERE session_id='session-2'"
            ).fetchone()
            self.assertEqual(event, ('processed', 'private_candidate'))
            memory = conn.execute(
                "SELECT scope, lifecycle, owner_agent_id FROM memory"
            ).fetchone()
            self.assertEqual(memory, ('private', 'candidate', 'agent-alice'))
        finally:
            conn.close()

    def test_sync_retry_does_not_duplicate_event_or_memory(self):
        p = self.provider()
        messages = [{'role': 'user'}, {'role': 'assistant'}]
        for _ in range(2):
            p.sync_turn('remember that I use compact mode', 'Noted.',
                        session_id='session-3', messages=messages)
        conn = store.open_store(self.db)
        try:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM ingest_event').fetchone()[0], 1)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM memory').fetchone()[0], 1)
        finally:
            conn.close()

    def test_builtin_memory_write_is_explicit_private_candidate(self):
        p = self.provider()
        p.on_memory_write('add', 'user', 'Preferred editor is Helix', {'tool_name': 'memory'})
        conn = store.open_store(self.db)
        try:
            event = conn.execute(
                "SELECT event_type, status, decision FROM ingest_event"
            ).fetchone()
            self.assertEqual(event, ('memory_write', 'processed', 'private_candidate'))
            content = conn.execute(
                'SELECT v.content FROM memory m JOIN memory_version v '
                'ON v.id=m.current_version_id'
            ).fetchone()[0]
            self.assertEqual(content, 'Preferred editor is Helix')
        finally:
            conn.close()

    def test_native_tool_routes_to_existing_governed_handler(self):
        p = self.provider()
        out = json.loads(p.handle_tool_call(
            'memory_remember', {'content': 'native tool marker'}
        ))
        self.assertTrue(out['success'])
        result = json.loads(p.handle_tool_call(
            'memory_search', {'query': 'native tool marker'}
        ))
        self.assertTrue(result['success'])
        self.assertTrue(any('native tool marker' in r['content'] for r in result['results']))

    def test_builtin_memory_remove_rejects_exact_mirrored_claim(self):
        p = self.provider()
        p.on_memory_write(
            'add', 'user', 'Preferred editor is Helix', {'tool_name': 'memory'}
        )
        p.on_memory_write(
            'remove', 'user', '',
            {'tool_name': 'memory', 'old_text': 'Preferred editor is Helix'}
        )
        conn = store.open_store(self.db)
        try:
            events = conn.execute(
                "SELECT status, decision FROM ingest_event ORDER BY created_at, rowid"
            ).fetchall()
            self.assertEqual(events, [
                ('processed', 'private_candidate'),
                ('processed', 'builtin_memory_removed'),
            ])
            lifecycle = conn.execute('SELECT lifecycle FROM memory').fetchone()[0]
            self.assertEqual(lifecycle, 'rejected')
            self.assertEqual(core.search(
                conn, 'proj-demo', 'agent-alice', 'Preferred editor is Helix'
            ), [])
            tombstones = conn.execute('SELECT COUNT(*) FROM tombstone').fetchone()[0]
            self.assertEqual(tombstones, 1)
        finally:
            conn.close()

    def test_builtin_memory_replace_supersedes_exact_mirrored_claim(self):
        p = self.provider()
        p.on_memory_write(
            'add', 'user', 'legacyhelix editor preference', {'tool_name': 'memory'}
        )
        p.on_memory_write(
            'replace', 'user', 'freshzed editor preference',
            {'tool_name': 'memory', 'old_text': 'legacyhelix editor preference'}
        )
        conn = store.open_store(self.db)
        try:
            event = conn.execute(
                "SELECT status, decision FROM ingest_event ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
            self.assertEqual(event, ('processed', 'builtin_memory_replaced'))
            row = conn.execute(
                'SELECT m.lifecycle, v.content FROM memory m JOIN memory_version v '
                'ON v.id=m.current_version_id'
            ).fetchone()
            self.assertEqual(row, ('candidate', 'freshzed editor preference'))
            self.assertEqual(core.search(
                conn, 'proj-demo', 'agent-alice', 'legacyhelix'
            ), [])
            self.assertEqual(len(core.search(
                conn, 'proj-demo', 'agent-alice', 'freshzed'
            )), 1)
        finally:
            conn.close()
