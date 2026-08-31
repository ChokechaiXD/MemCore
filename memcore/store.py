"""
MemCore — storage layer.

open_store(): apply schema, run pending migrations under a lock, set pragmas.
All operations use short transactions and WAL + busy_timeout.
"""
import os
import sqlite3
import pathlib
import time

SCHEMA_PATH = pathlib.Path(__file__).resolve().parent.parent / 'schema' / 'schema.sql'
_MIGRATION_LOCK_VERSION = '__migration_lock__'
_MIGRATION_LOCK_TTL_SECONDS = 30.0
_MIGRATION_LOCK_WAIT_SECONDS = 10.0

# Migrations run in order; each is a list of SQL statements.
# 0001 = initial contract (schema.sql). Later migrations append here.
# schema.sql itself stays FROZEN — contract defects are reported, not patched.

_FTS_TRIGGERS = """
-- External-content FTS5 sync triggers (per schema.sql comment, Phase 1 duty).
CREATE TRIGGER IF NOT EXISTS memory_version_ai AFTER INSERT ON memory_version BEGIN
  INSERT INTO memory_version_fts(rowid, content) VALUES (new.rowid, new.content);
END;
CREATE TRIGGER IF NOT EXISTS memory_version_ad AFTER DELETE ON memory_version BEGIN
  INSERT INTO memory_version_fts(memory_version_fts, rowid, content)
  VALUES ('delete', old.rowid, old.content);
END;
CREATE TRIGGER IF NOT EXISTS memory_version_au AFTER UPDATE ON memory_version BEGIN
  INSERT INTO memory_version_fts(memory_version_fts, rowid, content)
  VALUES ('delete', old.rowid, old.content);
  INSERT INTO memory_version_fts(rowid, content) VALUES (new.rowid, new.content);
END;
"""

_IDEMPOTENCY = """
-- Idempotency keys: unique constraint + INSERT OR IGNORE pattern (Phase 1).
CREATE TABLE IF NOT EXISTS idempotency_key (
    key         TEXT PRIMARY KEY,
    project_id  TEXT,
    memory_id   TEXT NOT NULL,
    version_id  TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

# 0004 — schema revision (ALTIMA Phase-1 verdict + MINORS V4 + minors 2/4).
# SQLite cannot ADD CONSTRAINT via ALTER: rebuild the three tables in one
# transaction, then rebuild FTS (rowids change) and enforce the rest with
# triggers where a rebuild is disproportionate.
_SCHEMA_REVISION = """
CREATE TABLE memory_new (
    id                  TEXT PRIMARY KEY,
    project_id          TEXT NOT NULL REFERENCES project(id),
    scope               TEXT NOT NULL CHECK (scope IN ('project', 'private')),
    owner_agent_id      TEXT NOT NULL REFERENCES agent(id),
    type                TEXT NOT NULL DEFAULT 'fact',
    lifecycle           TEXT NOT NULL DEFAULT 'candidate'
                            CHECK (lifecycle IN ('candidate', 'accepted', 'conflict', 'superseded', 'rejected', 'disabled')),
    verification        TEXT NOT NULL DEFAULT 'unverified'
                            CHECK (verification IN ('unverified', 'source_backed', 'runtime_verified', 'user_authoritative')),
    freshness           TEXT NOT NULL DEFAULT 'current'
                            CHECK (freshness IN ('current', 'aging', 'stale')),
    current_version_id  TEXT REFERENCES memory_version(id)
                            DEFERRABLE INITIALLY DEFERRED,
    pinned              INTEGER NOT NULL DEFAULT 0,
    critical            INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE memory_version_new (
    id                    TEXT PRIMARY KEY,
    memory_id             TEXT NOT NULL REFERENCES memory(id),
    content               TEXT NOT NULL,
    reason                TEXT,
    created_by_agent_id   TEXT NOT NULL REFERENCES agent(id),
    created_at            TEXT NOT NULL DEFAULT (datetime('now')),
    validated_at          TEXT,
    valid_from            TEXT NOT NULL DEFAULT (datetime('now')),
    valid_until           TEXT,
    supersedes_version_id TEXT REFERENCES memory_version_new(id),
    source_hash           TEXT,
    source_commit         TEXT,
    source_file           TEXT
);
CREATE TABLE audit_event_new (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    action         TEXT NOT NULL,
    actor_agent_id TEXT REFERENCES agent(id),
    memory_id      TEXT REFERENCES memory(id),
    project_id     TEXT REFERENCES project(id),
    detail         TEXT DEFAULT '{}',
    write_key      TEXT UNIQUE,
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
INSERT INTO memory_new SELECT * FROM memory;
INSERT INTO memory_version_new SELECT * FROM memory_version;
INSERT INTO audit_event_new (id, action, actor_agent_id, memory_id, project_id, detail, created_at)
    SELECT id, action, actor_agent_id, memory_id, project_id, detail, created_at FROM audit_event;
DROP TABLE audit_event;
DROP TABLE memory_version;
DROP TABLE memory;
ALTER TABLE memory_new RENAME TO memory;
ALTER TABLE memory_version_new RENAME TO memory_version;
ALTER TABLE audit_event_new RENAME TO audit_event;
CREATE INDEX IF NOT EXISTS idx_memory_project ON memory(project_id);
CREATE INDEX IF NOT EXISTS idx_memory_owner ON memory(owner_agent_id);
CREATE INDEX IF NOT EXISTS idx_memory_scope ON memory(project_id, scope);
CREATE INDEX IF NOT EXISTS idx_memory_lifecycle ON memory(lifecycle);
CREATE INDEX IF NOT EXISTS idx_memory_version_memory ON memory_version(memory_id);
CREATE INDEX IF NOT EXISTS idx_audit_project ON audit_event(project_id);
CREATE INDEX IF NOT EXISTS idx_audit_memory ON audit_event(memory_id);
CREATE INDEX IF NOT EXISTS idx_tombstone_fingerprint ON tombstone(claim_fingerprint, scope);
CREATE INDEX IF NOT EXISTS idx_project_membership_agent ON project_membership(agent_id);
CREATE INDEX IF NOT EXISTS idx_project_membership_project ON project_membership(project_id);
DROP TRIGGER IF EXISTS memory_version_ai;
DROP TRIGGER IF EXISTS memory_version_ad;
DROP TRIGGER IF EXISTS memory_version_au;
INSERT INTO memory_version_fts(memory_version_fts) VALUES ('rebuild');
CREATE TRIGGER IF NOT EXISTS memory_version_ai AFTER INSERT ON memory_version BEGIN
  INSERT INTO memory_version_fts(rowid, content) VALUES (new.rowid, new.content);
END;
CREATE TRIGGER IF NOT EXISTS memory_version_ad AFTER DELETE ON memory_version BEGIN
  INSERT INTO memory_version_fts(memory_version_fts, rowid, content)
  VALUES ('delete', old.rowid, old.content);
END;
CREATE TRIGGER IF NOT EXISTS memory_version_au AFTER UPDATE ON memory_version BEGIN
  INSERT INTO memory_version_fts(memory_version_fts, rowid, content)
  VALUES ('delete', old.rowid, old.content);
  INSERT INTO memory_version_fts(rowid, content) VALUES (new.rowid, new.content);
END;
CREATE TRIGGER IF NOT EXISTS audit_idempotency_enforce BEFORE INSERT ON audit_event
WHEN new.write_key IS NOT NULL AND EXISTS (
  SELECT 1 FROM audit_event WHERE write_key = new.write_key
) BEGIN
  SELECT RAISE(IGNORE);
END;
CREATE TRIGGER IF NOT EXISTS audit_created_at_iso BEFORE INSERT ON audit_event
WHEN new.created_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] [0-9][0-9]:[0-9][0-9]:[0-9][0-9]'
 AND new.created_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'
BEGIN
  SELECT RAISE(IGNORE);
END;
"""

# Post-revision DDL that runs AFTER the rebuild (must exist for the FTS
# rebuild to be consistent, and for normalized timestamps going forward).
_ISO_NORMALIZE = """
-- Guard: reject malformed timestamps. Engine writes ISO 8601 Z explicitly;
-- legacy datetime('now') values (defaults, manual SQL) remain accepted.
CREATE TRIGGER IF NOT EXISTS memory_version_created_at_iso BEFORE INSERT ON memory_version
WHEN new.created_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] [0-9][0-9]:[0-9][0-9]:[0-9][0-9]'
 AND new.created_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'
BEGIN
  SELECT RAISE(IGNORE);
END;
"""

MIGRATIONS = [
    ('0001_initial_contract', None),  # None = apply schema.sql verbatim
    ('0002_fts_sync_triggers', _FTS_TRIGGERS),
    ('0003_idempotency_keys', _IDEMPOTENCY),
    ('0004_schema_revision', _SCHEMA_REVISION),
    ('0005_iso_timestamps', _ISO_NORMALIZE),
    ('0006_tombstone_override_fk', """
CREATE TABLE tombstone_new (
    id                TEXT PRIMARY KEY,
    claim_fingerprint TEXT NOT NULL,
    scope             TEXT NOT NULL,
    reason            TEXT NOT NULL,
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    overridden_by     TEXT REFERENCES agent(id)
);
INSERT INTO tombstone_new SELECT * FROM tombstone;
DROP TABLE tombstone;
ALTER TABLE tombstone_new RENAME TO tombstone;
CREATE INDEX IF NOT EXISTS idx_tombstone_fingerprint ON tombstone(claim_fingerprint, scope);
"""),
]


class StoreError(Exception):
    pass


def _table_exists(conn, table):
    cur = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    )
    return cur.fetchone() is not None


def _current_version(conn):
    if not _table_exists(conn, 'schema_migrations'):
        return None
    cur = conn.execute(
        'SELECT version FROM schema_migrations WHERE version != ? '
        'ORDER BY applied_at DESC, rowid DESC LIMIT 1',
        (_MIGRATION_LOCK_VERSION,)
    )
    row = cur.fetchone()
    return row[0] if row else None


def open_store(db_path: str, check_same_thread: bool = True) -> sqlite3.Connection:
    """
    Open (creating if needed) a MemCore store.

    Returns a sqlite3.Connection with pragmas set and all migrations applied,
    under a migration lock so two processes booting concurrently are safe.
    """
    p = pathlib.Path(db_path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        str(p), timeout=10, isolation_level=None,
        check_same_thread=check_same_thread
    )
    try:
        # Set the busy timeout before WAL negotiation: concurrent first boots
        # can otherwise race on PRAGMA journal_mode before SQLite has a chance
        # to wait for the other connection's schema lock.
        conn.execute('PRAGMA busy_timeout = 10000')
        deadline = time.time() + 10.0
        while True:
            try:
                conn.execute('PRAGMA journal_mode = WAL')
                break
            except sqlite3.OperationalError as e:
                if 'locked' not in str(e).lower() and 'busy' not in str(e).lower():
                    raise
                if time.time() >= deadline:
                    raise
                time.sleep(0.05)
        conn.execute('PRAGMA foreign_keys = ON')
        conn.execute('PRAGMA synchronous = NORMAL')
        apply_migrations(conn)
        return conn
    except Exception:
        conn.close()
        raise


def open_store_readonly(db_path: str) -> sqlite3.Connection:
    """Open an existing, current MemCore store without schema/domain writes."""
    p = pathlib.Path(db_path).expanduser().resolve()
    if not p.is_file():
        raise StoreError(f'store does not exist: {p}')
    conn = sqlite3.connect(p.as_uri() + '?mode=ro', uri=True, timeout=10,
                           isolation_level=None)
    try:
        conn.execute('PRAGMA busy_timeout = 5000')
        conn.execute('PRAGMA foreign_keys = ON')
        conn.execute('PRAGMA query_only = ON')
        current = _current_version(conn)
        expected = MIGRATIONS[-1][0]
        if current != expected:
            raise StoreError(
                f'read-only store schema is {current or "unversioned"}; '
                f'expected {expected}. Open normally to migrate first.'
            )
        return conn
    except Exception:
        conn.close()
        raise


def apply_migrations(conn):
    """Bootstrap migration bookkeeping and apply all pending migrations.

    Shared by open_store() and the fixtures (which bring their own connection).
    """
    if not _table_exists(conn, 'schema_migrations'):
        conn.execute(
            'CREATE TABLE IF NOT EXISTS schema_migrations ('
            '  version TEXT PRIMARY KEY,'
            "  applied_at TEXT NOT NULL DEFAULT (datetime('now')), "
            '  lock_holder TEXT,'
            '  lock_until REAL)'
        )
    _run_migrations(conn)


def _migration_lock_row(conn):
    return conn.execute(
        'SELECT lock_holder, lock_until FROM schema_migrations WHERE version=?',
        (_MIGRATION_LOCK_VERSION,)
    ).fetchone()


def _acquire_migration_lock(conn):
    """Acquire a crash-visible migration lock row, reclaiming only expired locks."""
    holder = f'pid:{os.getpid()}:conn:{id(conn)}'
    deadline = time.time() + _MIGRATION_LOCK_WAIT_SECONDS
    while True:
        now = time.time()
        try:
            conn.execute('BEGIN IMMEDIATE')
            row = _migration_lock_row(conn)
            if row and row[0] and row[1] is not None and row[1] > now and row[0] != holder:
                conn.execute('ROLLBACK')
                if time.time() >= deadline:
                    raise StoreError(
                        f'migration lock held by {row[0]} until {row[1]:.3f}'
                    )
                time.sleep(0.05)
                continue
            conn.execute(
                'INSERT INTO schema_migrations '
                '(version, applied_at, lock_holder, lock_until) '
                "VALUES (?, datetime('now'), ?, ?) "
                'ON CONFLICT(version) DO UPDATE SET '
                "applied_at=datetime('now'), lock_holder=excluded.lock_holder, "
                'lock_until=excluded.lock_until',
                (_MIGRATION_LOCK_VERSION, holder,
                 now + _MIGRATION_LOCK_TTL_SECONDS)
            )
            conn.execute('COMMIT')
            return holder
        except StoreError:
            raise
        except sqlite3.OperationalError as e:
            try:
                conn.execute('ROLLBACK')
            except sqlite3.OperationalError:
                pass
            if time.time() >= deadline:
                raise StoreError(f'could not acquire migration lock: {e}') from e
            time.sleep(0.05)


def _release_migration_lock(conn, holder):
    deadline = time.time() + _MIGRATION_LOCK_WAIT_SECONDS
    while True:
        try:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute(
                'DELETE FROM schema_migrations WHERE version=? AND lock_holder=?',
                (_MIGRATION_LOCK_VERSION, holder)
            )
            conn.execute('COMMIT')
            return
        except sqlite3.OperationalError as e:
            try:
                conn.execute('ROLLBACK')
            except sqlite3.OperationalError:
                pass
            if time.time() >= deadline:
                raise StoreError(f'could not release migration lock: {e}') from e
            time.sleep(0.05)


def _renew_migration_lock(conn, holder):
    """Renew only while this process still owns the migration lease."""
    conn.execute('BEGIN IMMEDIATE')
    try:
        cur = conn.execute(
            'UPDATE schema_migrations SET lock_until=? '
            'WHERE version=? AND lock_holder=?',
            (time.time() + _MIGRATION_LOCK_TTL_SECONDS,
             _MIGRATION_LOCK_VERSION, holder)
        )
        if cur.rowcount != 1:
            raise StoreError('migration lock ownership was lost')
        conn.execute('COMMIT')
    except Exception:
        try:
            conn.execute('ROLLBACK')
        except sqlite3.OperationalError:
            pass
        raise


def _run_migrations(conn):
    """Apply pending migrations under one crash-visible process lock."""
    known = [v for v, _ in MIGRATIONS]
    version = _current_version(conn)
    if version is not None and version not in known:
        raise StoreError(f'unsupported schema migration version: {version}')

    has_pending = version is None or version != known[-1]
    if not has_pending and _migration_lock_row(conn) is None:
        return

    holder = _acquire_migration_lock(conn)
    try:
        # Re-read after acquiring: another process may have completed while
        # this connection was waiting for the lock.
        version = _current_version(conn)
        if version is None:
            _apply_migration(
                conn, '0001_initial_contract',
                SCHEMA_PATH.read_text(encoding='utf-8')
            )
            pending = MIGRATIONS[1:]
        else:
            if version not in known:
                raise StoreError(f'unsupported schema migration version: {version}')
            pending = MIGRATIONS[known.index(version) + 1:]
        for name, sql in pending:
            # Renew transactionally and verify ownership before every step.
            # If another process reclaimed an expired lease after a long
            # migration, this process must stop instead of interleaving.
            _renew_migration_lock(conn, holder)
            _apply_migration(conn, name, sql)
    finally:
        _release_migration_lock(conn, holder)


def _script_statements(sql):
    """Yield complete SQLite statements, preserving trigger BEGIN/END blocks."""
    buf = ''
    for line in sql.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            stmt = buf.strip()
            if stmt:
                yield stmt
            buf = ''
    if buf.strip():
        raise StoreError('incomplete SQL statement in migration')


def _apply_migration(conn, name, sql):
    """Apply one migration and its bookkeeping safely.

    The frozen schema bootstrap still uses executescript because it contains
    connection PRAGMAs. Later migrations are executed statement-by-statement
    inside one explicit transaction, with the migration row committed in the
    same transaction. Table-rebuild migration 0004 temporarily disables FK
    enforcement and runs foreign_key_check before commit, per SQLite guidance.
    """
    already = conn.execute(
        'SELECT 1 FROM schema_migrations WHERE version = ?', (name,)
    ).fetchone()
    if already:
        return

    if sql is None:
        conn.executescript(SCHEMA_PATH.read_text(encoding='utf-8'))
        for attempt in range(5):
            try:
                conn.execute('BEGIN IMMEDIATE')
                if conn.execute(
                    'SELECT 1 FROM schema_migrations WHERE version=?', (name,)
                ).fetchone():
                    conn.execute('ROLLBACK')
                    return
                conn.execute(
                    'INSERT INTO schema_migrations (version, applied_at) '
                    "VALUES (?, datetime('now'))", (name,)
                )
                conn.execute('COMMIT')
                return
            except sqlite3.OperationalError:
                try:
                    conn.execute('ROLLBACK')
                except sqlite3.OperationalError:
                    pass
                if attempt == 4:
                    raise
                time.sleep(0.2)
        return

    rebuild_fk = name == '0004_schema_revision'
    for attempt in range(5):
        if rebuild_fk:
            conn.execute('PRAGMA foreign_keys = OFF')
        try:
            conn.execute('BEGIN IMMEDIATE')
            if conn.execute(
                'SELECT 1 FROM schema_migrations WHERE version=?', (name,)
            ).fetchone():
                conn.execute('ROLLBACK')
                return
            for stmt in _script_statements(sql):
                conn.execute(stmt)
            if rebuild_fk:
                violations = conn.execute('PRAGMA foreign_key_check').fetchall()
                if violations:
                    raise StoreError(
                        f'foreign-key violations after {name}: {violations[:5]}'
                    )
            conn.execute(
                'INSERT INTO schema_migrations (version, applied_at) '
                "VALUES (?, datetime('now'))", (name,)
            )
            conn.execute('COMMIT')
            return
        except sqlite3.OperationalError:
            try:
                conn.execute('ROLLBACK')
            except sqlite3.OperationalError:
                pass
            if attempt == 4:
                raise
            time.sleep(0.2)
        except Exception:
            try:
                conn.execute('ROLLBACK')
            except sqlite3.OperationalError:
                pass
            raise
        finally:
            if rebuild_fk:
                conn.execute('PRAGMA foreign_keys = ON')


def check_migration_lock(conn):
    """Return active or stale crash-visible migration lock info for doctor."""
    try:
        cur = conn.execute(
            'SELECT version, lock_holder, lock_until FROM schema_migrations '
            'WHERE version=? AND lock_holder IS NOT NULL',
            (_MIGRATION_LOCK_VERSION,)
        )
        return cur.fetchall()
    except sqlite3.OperationalError:
        return []
