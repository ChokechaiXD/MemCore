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


if __name__ == '__main__':
    unittest.main()
