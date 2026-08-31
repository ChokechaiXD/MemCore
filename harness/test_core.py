"""
MemCore engine unit tests — Phase 1.

Tests the real core operations the harness evaluations don't cover:
idempotency replay, promote permissions, deactivate/restore, search ranking,
FTS trigger sync, tombstone-guarded creation, migration bookkeeping.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from memcore import store, core


class CoreTestBase(unittest.TestCase):
    """Temp file store + two agents (alice owner, bob member) in one project."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='memcore_unit_')
        self.db_path = os.path.join(self.tmpdir, 'unit.db')
        self.conn = store.open_store(self.db_path)
        self.project = 'proj-alpha'
        self.alice = 'agent-alice'
        self.bob = 'agent-bob'
        self.conn.execute(
            "INSERT INTO project (id, name) VALUES (?, 'alpha')", (self.project,)
        )
        for aid, role in ((self.alice, 'owner'), (self.bob, 'member')):
            self.conn.execute(
                "INSERT INTO agent (id, name, profile_key) VALUES (?, ?, ?)",
                (aid, aid, aid)
            )
            self.conn.execute(
                'INSERT INTO project_membership (project_id, agent_id, role) '
                'VALUES (?, ?, ?)',
                (self.project, aid, role)
            )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        for suffix in ('', '-wal', '-shm'):
            try:
                os.unlink(self.db_path + suffix)
            except OSError:
                pass


class TestStore(CoreTestBase):

    def test_migrations_recorded(self):
        rows = self.conn.execute(
            'SELECT version FROM schema_migrations ORDER BY version'
        ).fetchall()
        versions = [r[0] for r in rows]
        # Contract: every declared migration is recorded, in declared order.
        expected = [name for name, _ in store.MIGRATIONS]
        self.assertEqual(versions, expected)

    def test_reopen_does_not_reapply_migrations(self):
        self.conn.close()
        conn2 = store.open_store(self.db_path)
        n = conn2.execute('SELECT COUNT(*) FROM schema_migrations').fetchone()[0]
        conn2.close()
        self.assertEqual(n, len(store.MIGRATIONS))

    def test_upgrade_from_pre_0004_db(self):
        """A DB created before the schema revision upgrades in place, data intact."""
        self.conn.close()
        real = store.MIGRATIONS
        store.MIGRATIONS = real[:3]  # simulate the old engine (0001–0003 only)
        try:
            old = store.open_store(self.db_path)
            old.execute("INSERT INTO project (id, name) VALUES ('proj-old', 'Legacy')")
            old.execute(
                "INSERT INTO agent (id, name, profile_key) VALUES ('agent-a', 'A', 'a')"
            )
            old.execute(
                "INSERT INTO memory (id, project_id, scope, owner_agent_id) "
                "VALUES ('mem-old', 'proj-old', 'project', 'agent-a')"
            )
            old.execute(
                "INSERT INTO memory_version (id, memory_id, content, "
                "  created_by_agent_id) VALUES ('ver-old', 'mem-old', 'legacy fact', 'agent-a')"
            )
            old.execute(
                "UPDATE memory SET current_version_id='ver-old' WHERE id='mem-old'"
            )
            old.close()
        finally:
            store.MIGRATIONS = real
        conn = store.open_store(self.db_path)  # new engine upgrades to 0005
        self.assertEqual(
            conn.execute('SELECT COUNT(*) FROM schema_migrations').fetchone()[0],
            len(store.MIGRATIONS),
        )
        self.assertEqual(
            conn.execute(
                'SELECT content FROM memory_version WHERE id=?', ('ver-old',)
            ).fetchone()[0],
            'legacy fact',
        )
        # FTS was rebuilt in step with the migrated rows.
        hits = conn.execute(
            "SELECT rowid FROM memory_version_fts WHERE memory_version_fts MATCH 'legacy'"
        ).fetchall()
        self.assertEqual(len(hits), 1)
        conn.close()


class TestCreateMemory(CoreTestBase):

    def test_create_private_memory(self):
        mem_id, ver_id = core.create_memory(
            self.conn, self.project, self.alice, 'Alice private scratch note'
        )
        row = self.conn.execute(
            'SELECT scope, lifecycle, owner_agent_id FROM memory WHERE id=?',
            (mem_id,)
        ).fetchone()
        self.assertEqual(row[0], 'private')
        self.assertEqual(row[1], 'candidate')
        self.assertEqual(row[2], self.alice)

    def test_create_project_memory_requires_membership(self):
        self.conn.execute(
            "INSERT INTO agent (id, name, profile_key) VALUES ('agent-mallory', 'mallory', 'mallory')"
        )
        self.conn.commit()
        with self.assertRaises(core.PermissionDenied):
            core.create_memory(
                self.conn, self.project, 'agent-mallory', 'injected claim',
                scope='project'
            )

    def test_idempotency_key_replays_same_ids(self):
        r1 = core.create_memory(
            self.conn, self.project, self.alice, 'shared decision',
            scope='project', idempotency_key='key-1'
        )
        r2 = core.create_memory(
            self.conn, self.project, self.alice, 'shared decision',
            scope='project', idempotency_key='key-1'
        )
        self.assertEqual(r1, r2)
        n = self.conn.execute(
            "SELECT COUNT(*) FROM audit_event WHERE action='create'"
        ).fetchone()[0]
        self.assertEqual(n, 1, 'replayed create must not write a second audit row')

    def test_tombstone_blocks_identical_claim(self):
        claim = 'Alpha uses PostgreSQL for storage.'
        core.create_memory(self.conn, self.project, self.alice, claim)
        mem_id = self.conn.execute(
            'SELECT id FROM memory ORDER BY created_at DESC LIMIT 1'
        ).fetchone()[0]
        core.reject(self.conn, mem_id, self.alice, 'wrong storage engine')
        with self.assertRaises(core.TombstoneBlocked):
            core.create_memory(
                self.conn, self.project, self.bob, claim, scope='project'
            )

    def test_tombstone_blocks_whitespace_case_variant(self):
        claim = 'Alpha uses PostgreSQL for storage.'
        core.create_memory(self.conn, self.project, self.alice, claim)
        mem_id = self.conn.execute(
            'SELECT id FROM memory ORDER BY created_at DESC LIMIT 1'
        ).fetchone()[0]
        core.reject(self.conn, mem_id, self.alice, 'wrong')
        variant = '  ALPHA   uses postgresql\nfor storage.  '
        with self.assertRaises(core.TombstoneBlocked):
            core.create_memory(self.conn, self.project, self.bob, variant)


class TestPromote(CoreTestBase):

    def test_owner_promotes_own_private_memory(self):
        mem_id, _ = core.create_memory(
            self.conn, self.project, self.alice, 'promotable insight'
        )
        core.promote(self.conn, mem_id, self.alice)
        row = self.conn.execute(
            'SELECT scope FROM memory WHERE id=?', (mem_id,)
        ).fetchone()
        self.assertEqual(row[0], 'project')

    def test_non_owner_member_cannot_promote_anothers_memory(self):
        mem_id, _ = core.create_memory(
            self.conn, self.project, self.alice, 'alice private'
        )
        with self.assertRaises(core.PermissionDenied):
            core.promote(self.conn, mem_id, self.bob)

    def test_promotion_is_audited(self):
        mem_id, _ = core.create_memory(
            self.conn, self.project, self.alice, 'audited promotion'
        )
        core.promote(self.conn, mem_id, self.alice)
        row = self.conn.execute(
            "SELECT action, actor_agent_id FROM audit_event "
            'WHERE memory_id=? ORDER BY id DESC LIMIT 1',
            (mem_id,)
        ).fetchone()
        self.assertEqual(row[0], 'promote')
        self.assertEqual(row[1], self.alice)


class TestSupersedeAndDeactivate(CoreTestBase):

    def test_supersede_keeps_history_and_flips_current(self):
        mem_id, v1 = core.create_memory(
            self.conn, self.project, self.alice, 'original decision', scope='project'
        )
        v2 = core.supersede(
            self.conn, mem_id, self.bob, 'corrected decision', reason='wrong info'
        )
        cur = self.conn.execute(
            'SELECT current_version_id FROM memory WHERE id=?', (mem_id,)
        ).fetchone()[0]
        self.assertEqual(cur, v2)
        versions = core.superseded_history(self.conn, mem_id)
        self.assertEqual(len(versions), 2)
        self.assertEqual(versions[0][0], v1)

    def test_supersede_audited(self):
        mem_id, _ = core.create_memory(
            self.conn, self.project, self.alice, 'to correct', scope='project'
        )
        core.supersede(self.conn, mem_id, self.alice, 'corrected', reason='fix')
        action = self.conn.execute(
            "SELECT action FROM audit_event WHERE memory_id=? "
            'ORDER BY id DESC LIMIT 1',
            (mem_id,)
        ).fetchone()[0]
        self.assertEqual(action, 'supersede')

    def test_deactivate_restore_roundtrip(self):
        mem_id, _ = core.create_memory(
            self.conn, self.project, self.alice, 'temporary note'
        )
        core.deactivate(self.conn, mem_id, self.alice, reason='cleanup')
        row = self.conn.execute(
            'SELECT lifecycle FROM memory WHERE id=?', (mem_id,)
        ).fetchone()
        self.assertEqual(row[0], 'disabled')
        # Disabled memories are excluded from visible recall
        visible = core.visible_memories(self.conn, self.project, self.alice)
        self.assertNotIn(mem_id, [r[0] for r in visible])
        core.restore(self.conn, mem_id, self.alice)
        row = self.conn.execute(
            'SELECT lifecycle FROM memory WHERE id=?', (mem_id,)
        ).fetchone()
        self.assertEqual(row[0], 'candidate')


class TestSearch(CoreTestBase):

    def test_fts_finds_and_respects_scope(self):
        core.create_memory(
            self.conn, self.project, self.alice,
            'The deploy pipeline uses GitHub Actions runners',
            scope='project'
        )
        core.create_memory(
            self.conn, self.project, self.alice,
            'Secret deploy pipeline token stored privately',
            scope='private'
        )
        # bob searches: sees project memory, never alice's private one
        hits = core.search(self.conn, self.project, self.bob, 'deploy pipeline')
        contents = [r[5] for r in hits]
        self.assertEqual(len(contents), 1)
        self.assertIn('GitHub Actions', contents[0])

    def test_fts_trigger_keeps_index_in_sync(self):
        core.create_memory(
            self.conn, self.project, self.alice, 'unique xyzzy content', scope='project'
        )
        mem_id, ver_id = core.create_memory(
            self.conn, self.project, self.alice, 'second memory about quux', scope='project'
        )
        fts_rows = self.conn.execute('SELECT COUNT(*) FROM memory_version_fts').fetchone()[0]
        ver_rows = self.conn.execute('SELECT COUNT(*) FROM memory_version').fetchone()[0]
        self.assertEqual(fts_rows, ver_rows, 'FTS index out of sync after inserts')

        # UPDATE path: supersede rewrites content; FTS must follow
        core.supersede(self.conn, mem_id, self.alice, 'rewritten content about quux2')
        hits = core.search(self.conn, self.project, self.alice, 'quux2')
        self.assertEqual(len(hits), 1, 'FTS did not pick up updated content')

    def test_search_survives_fts_operators_in_query(self):
        """Regression: raw user strings with FTS5 operator chars must not raise.

        MIKA reproduced: search "bob's CLI taste" crashed with
        sqlite3.OperationalError: fts5: syntax error near "'".
        """
        core.create_memory(
            self.conn, self.project, self.alice,
            "bob's CLI taste is questionable", scope='project'
        )
        # apostrophe, quotes, parens, hyphen — all previously fatal
        hits = core.search(self.conn, self.project, self.bob, "bob's CLI taste")
        contents = [r[5] for r in hits]
        self.assertEqual(len(contents), 1)
        self.assertIn('CLI taste', contents[0])
        # punctuation-only query: no crash, no results
        self.assertEqual(core.search(self.conn, self.project, self.bob, '!!! ???'), [])

    def test_search_returns_only_current_version(self):
        mem_id, _ = core.create_memory(
            self.conn, self.project, self.alice, 'old claim about deploy', scope='project'
        )
        core.supersede(
            self.conn, mem_id, self.alice, 'new claim about deploy', reason='update'
        )
        rows = core.search(self.conn, self.project, self.alice, 'deploy')
        contents = [r[5] for r in rows if r[0] == mem_id]
        # current truth only — superseded content must not surface in retrieval
        self.assertEqual(contents, ['new claim about deploy'])

    def test_rank_prefers_pinned(self):
        mem_a, _ = core.create_memory(
            self.conn, self.project, self.alice, 'kubernetes deployment notes alpha',
            scope='project'
        )
        mem_b, _ = core.create_memory(
            self.conn, self.project, self.alice, 'kubernetes deployment notes beta',
            scope='project'
        )
        self.conn.execute('UPDATE memory SET pinned=1 WHERE id=?', (mem_b,))
        self.conn.commit()
        hits = core.search(self.conn, self.project, self.alice, 'kubernetes deployment')
        self.assertEqual(hits[0][0], mem_b, 'pinned memory must rank first')


# ── operational tooling tests ──────────────────────────────────────────

class TestGC(CoreTestBase):
    """Tests for gc_scan (dry-run) and gc_apply (retention sweep)."""

    def test_gc_scan_finds_old_unlinked_candidates(self):
        """candidate memory older than cutoff with no evidence is a gc candidate."""
        mem_id, ver_id = core.create_memory(
            self.conn, self.project, self.alice, 'old claim with no evidence', scope='project'
        )
        # Backdate the memory's created_at beyond the candidate-days cutoff.
        self.conn.execute(
            "UPDATE memory SET created_at=datetime('now', '-40 days') WHERE id=?",
            (mem_id,)
        )
        self.conn.commit()
        candidates, tombstones = core.gc_scan(self.conn, candidate_days=30, tombstone_days=90)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0][0], mem_id)
        self.assertEqual(len(tombstones), 0)

    def test_gc_scan_skips_memories_with_evidence(self):
        """A candidate memory that has an evidence_link is not gc-eligible."""
        mem_id, ver_id = core.create_memory(
            self.conn, self.project, self.alice, 'evidenced claim', scope='project'
        )
        self.conn.execute(
            "UPDATE memory SET created_at=datetime('now', '-40 days') WHERE id=?",
            (mem_id,)
        )
        ev_id = core._new_id('ev')
        self.conn.execute(
            'INSERT INTO evidence (id, kind, source_uri, source_label) VALUES (?, ?, ?, ?)',
            (ev_id, 'file', 'file:///repo/main.py', 'main.py')
        )
        self.conn.execute(
            'INSERT INTO evidence_link (evidence_id, memory_version_id, relation) VALUES (?, ?, ?)',
            (ev_id, ver_id, 'supports')
        )
        self.conn.commit()
        candidates, tombstones = core.gc_scan(self.conn, candidate_days=30, tombstone_days=90)
        self.assertEqual(len(candidates), 0)
        self.assertEqual(len(tombstones), 0)

    def test_gc_scan_finds_old_tombstones(self):
        """tombstones older than cutoff are gc candidates for purging."""
        mem_id, ver_id = core.create_memory(
            self.conn, self.project, self.alice, 'rejected claim for tombstone', scope='project'
        )
        core.reject(self.conn, mem_id, self.alice, 'outdated')
        tomb_id = self.conn.execute('SELECT id FROM tombstone').fetchone()[0]
        self.conn.execute(
            "UPDATE tombstone SET created_at=datetime('now', '-100 days') WHERE id=?",
            (tomb_id,)
        )
        self.conn.commit()
        candidates, tombstones = core.gc_scan(self.conn, candidate_days=30, tombstone_days=90)
        self.assertEqual(len(candidates), 0)
        self.assertEqual(len(tombstones), 1)
        self.assertEqual(tombstones[0][0], tomb_id)

    def test_gc_scan_does_not_touch_anything(self):
        """gc_scan is a pure read — no data should change."""
        mem_id, _ = core.create_memory(
            self.conn, self.project, self.alice, 'young candidate', scope='project'
        )
        candidates, tombstones = core.gc_scan(self.conn)
        self.assertEqual(len(candidates), 0)
        self.assertEqual(len(tombstones), 0)
        # Memory still candidate.
        lc = self.conn.execute(
            'SELECT lifecycle FROM memory WHERE id=?', (mem_id,)
        ).fetchone()[0]
        self.assertEqual(lc, 'candidate')

    def test_gc_apply_tombstones_old_candidates(self):
        """gc_apply tombstones eligible candidates with reason 'gc'."""
        mem_id, ver_id = core.create_memory(
            self.conn, self.project, self.alice, 'gc me', scope='project'
        )
        self.conn.execute(
            "UPDATE memory SET created_at=datetime('now', '-40 days') WHERE id=?",
            (mem_id,)
        )
        self.conn.commit()
        tombstoned, purged = core.gc_apply(self.conn, candidate_days=30, tombstone_days=90)
        self.assertEqual(len(tombstoned), 1)
        self.assertEqual(tombstoned[0], mem_id)
        self.assertEqual(len(purged), 0)
        lc = self.conn.execute(
            'SELECT lifecycle FROM memory WHERE id=?', (mem_id,)
        ).fetchone()[0]
        self.assertEqual(lc, 'rejected')
        reason = self.conn.execute(
            'SELECT reason FROM tombstone WHERE claim_fingerprint=?',
            (core.fingerprint('gc me'),)
        ).fetchone()[0]
        self.assertEqual(reason, 'gc')

    def test_gc_apply_purges_old_tombstones(self):
        """gc_apply removes tombstones past the tombstone-days cutoff."""
        mem_id, ver_id = core.create_memory(
            self.conn, self.project, self.alice, 'gone soon', scope='project'
        )
        core.reject(self.conn, mem_id, self.alice, 'obsolete')
        tomb_id = self.conn.execute('SELECT id FROM tombstone').fetchone()[0]
        self.conn.execute(
            "UPDATE tombstone SET created_at=datetime('now', '-100 days') WHERE id=?",
            (tomb_id,)
        )
        self.conn.commit()
        tombstoned, purged = core.gc_apply(self.conn, candidate_days=30, tombstone_days=90)
        self.assertEqual(len(tombstoned), 0)
        self.assertEqual(len(purged), 1)
        self.assertEqual(purged[0], tomb_id)
        count = self.conn.execute(
            'SELECT COUNT(*) FROM tombstone WHERE id=?', (tomb_id,)
        ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_gc_apply_does_not_touch_recent_memories(self):
        """gc_apply must not affect young candidates or recent tombstones."""
        mem_id, _ = core.create_memory(
            self.conn, self.project, self.alice, 'keep me', scope='project'
        )
        tombstoned, purged = core.gc_apply(self.conn)
        self.assertEqual(len(tombstoned), 0)
        self.assertEqual(len(purged), 0)
        lc = self.conn.execute(
            'SELECT lifecycle FROM memory WHERE id=?', (mem_id,)
        ).fetchone()[0]
        self.assertEqual(lc, 'candidate')


class TestStats(CoreTestBase):
    """Tests for core.stats()."""

    def setUp(self):
        super().setUp()
        core.create_memory(self.conn, self.project, self.alice,
                           'first project note here', scope='project')
        core.create_memory(self.conn, self.project, self.bob,
                           'second private note', scope='private')
        core.create_memory(self.conn, self.project, self.alice,
                           'third project note', scope='project')

    def test_stats_contains_lifecycle_counts(self):
        s = core.stats(self.conn)
        self.assertIn('by_lifecycle', s)
        self.assertGreaterEqual(s['memories_total'], 3)
        self.assertGreaterEqual(s['by_lifecycle'].get('candidate', 0), 3)

    def test_stats_contains_scope_counts(self):
        s = core.stats(self.conn)
        self.assertIn('by_scope', s)
        self.assertEqual(s['by_scope'].get('project', 0), 2)
        self.assertEqual(s['by_scope'].get('private', 0), 1)

    def test_stats_top_agents(self):
        s = core.stats(self.conn)
        self.assertTrue(s['top_agents'])
        top = s['top_agents'][0]
        self.assertEqual(top['agent'], 'agent-alice')
        self.assertEqual(top['memories'], 2)

    def test_stats_avg_summary_length(self):
        s = core.stats(self.conn)
        self.assertIn('avg_summary_length', s)
        self.assertGreater(s['avg_summary_length'], 0.0)

    def test_stats_fts_drift_check(self):
        s = core.stats(self.conn)
        self.assertIn('fts', s)
        fts_info = s['fts']
        self.assertEqual(fts_info['fts_rows'], fts_info['version_rows'])
        self.assertTrue(fts_info['in_sync'])


class TestImport(CoreTestBase):
    """Tests for core.import_memories()."""

    def setUp(self):
        super().setUp()
        self.items = [
            {'title': 'Item 1', 'summary': 'alpha deploy uses containers',
             'type': 'fact', 'evidence': [{'kind': 'file', 'source_uri': 'f1', 'source_label': 'l1'}]},
            {'title': 'Item 2', 'summary': 'beta deploy uses serverless',
             'type': 'observation'},
            {'title': 'Dup', 'summary': 'alpha deploy uses containers',
             'type': 'fact'},
            {'title': 'Empty', 'summary': '   ', 'type': 'fact'},
        ]

    def test_import_creates_and_skips_correctly(self):
        result = core.import_memories(
            self.conn, self.items, self.project, self.alice, scope='project'
        )
        self.assertEqual(result['added'], 2)
        self.assertEqual(result['skipped'], 2)
        self.assertEqual(len(result['created']), 2)

    def test_import_via_create_requires_membership(self):
        """non-member agent cannot import project-scope memories."""
        self.conn.execute(
            "INSERT INTO agent (id, name, profile_key) VALUES ('agent-mallory', 'mallory', 'mallory')"
        )
        self.conn.commit()
        result = core.import_memories(
            self.conn, self.items, self.project, 'agent-mallory', scope='project'
        )
        self.assertEqual(result['added'], 0)
        self.assertEqual(result['skipped'], 4)

    def test_import_evidence_is_linked(self):
        """imported evidence rows are linked to the correct memory version."""
        result = core.import_memories(
            self.conn, self.items, self.project, self.alice, scope='project'
        )
        mem_id, ver_id = result['created'][0]
        links = self.conn.execute(
            'SELECT evidence_id FROM evidence_link WHERE memory_version_id=?',
            (ver_id,)
        ).fetchall()
        self.assertEqual(len(links), 1)
        ev = self.conn.execute(
            'SELECT kind FROM evidence WHERE id=?', (links[0][0],)
        ).fetchone()[0]
        self.assertEqual(ev, 'file')

    def test_import_dedup_by_fingerprint(self):
        """sha256 fingerprint dedup: same summary text -> one memory."""
        single = [{'summary': 'shared claim', 'type': 'fact'}]
        result = core.import_memories(
            self.conn, single, self.project, self.alice, scope='project'
        )
        self.assertEqual(result['added'], 1)
        self.assertEqual(result['skipped'], 0)


if __name__ == '__main__':
    unittest.main()
