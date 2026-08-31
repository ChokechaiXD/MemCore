"""
MemCore — storage layer.

open_store(): apply schema, run pending migrations under a lock, set pragmas.
All operations use short transactions and WAL + busy_timeout.
"""
import sqlite3
import pathlib
import time

SCHEMA_PATH = pathlib.Path(__file__).resolve().parent.parent / 'schema' / 'schema.sql'

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
        'SELECT version FROM schema_migrations ORDER BY applied_at DESC, rowid DESC LIMIT 1'
    )
    row = cur.fetchone()
    return row[0] if row else None


def open_store(db_path: str) -> sqlite3.Connection:
    """
    Open (creating if needed) a MemCore store.

    Returns a sqlite3.Connection with pragmas set and all migrations applied,
    under a migration lock so two processes booting concurrently are safe.
    """
    p = pathlib.Path(db_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=10, isolation_level=None)
    # Pragmas are per-connection: set on every open.
    conn.execute('PRAGMA journal_mode = WAL')
    conn.execute('PRAGMA busy_timeout = 5000')
    conn.execute('PRAGMA foreign_keys = ON')
    conn.execute('PRAGMA synchronous = NORMAL')
    conn.execute('PRAGMA synchronous = NORMAL')
    apply_migrations(conn)
    return conn


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


def _run_migrations(conn):
    """Apply pending migrations under a transient lock row.

    SQLite serializes writers anyway; the lock row exists so a crashed
    migration boot can be diagnosed and so 'doctor' can report it.
    """
    version = _current_version(conn)
    if version is None:
        # Fresh DB (or pre-bookkeeping legacy): apply full contract first,
        # then continue with any statement migrations that follow it.
        _apply_migration(conn, '0001_initial_contract', SCHEMA_PATH.read_text(encoding='utf-8'))
        pending = MIGRATIONS[1:]
    else:
        known = [v for v, _ in MIGRATIONS]
        try:
            idx = known.index(version)
        except ValueError:
            # DB is at a version this code doesn't know — never downgrade silently.
            return
        pending = MIGRATIONS[idx + 1:]
    for name, sql in pending:
        _apply_migration(conn, name, sql)


def _apply_migration(conn, name, sql):
    """Apply one migration, recording it in schema_migrations.

    schema.sql migrations use executescript(), which issues an implicit
    COMMIT first (python sqlite3 semantics) — so they CANNOT be wrapped in
    an explicit transaction. Statement migrations run transactionally.
    Both paths are idempotent via the schema_migrations re-check.
    """
    # Re-check without an open transaction first (another process may have raced us)
    already = conn.execute(
        'SELECT 1 FROM schema_migrations WHERE version = ?', (name,)
    ).fetchone()
    if already:
        return
    if sql is None:
        # Contract migration: full schema file via executescript (self-committing).
        conn.executescript(SCHEMA_PATH.read_text(encoding='utf-8'))
    else:
        conn.execute('BEGIN IMMEDIATE')
        try:
            conn.executescript(sql)  # still self-committing; keep simple
        except Exception:
            try:
                conn.execute('ROLLBACK')
            except sqlite3.OperationalError:
                pass
            raise
    # Record the migration atomically
    for attempt in range(5):
        try:
            conn.execute('BEGIN IMMEDIATE')
            already = conn.execute(
                'SELECT 1 FROM schema_migrations WHERE version = ?', (name,)
            ).fetchone()
            if already:
                conn.execute('ROLLBACK')
                return
            conn.execute(
                'INSERT INTO schema_migrations (version, applied_at) '
                "VALUES (?, datetime('now'))",
                (name,)
            )
            conn.execute('COMMIT')
            return
        except sqlite3.OperationalError:
            if attempt == 4:
                raise
            time.sleep(0.2)


def check_migration_lock(conn):
    """Return any active (stale) migration lock info for doctor."""
    try:
        cur = conn.execute(
            'SELECT version, lock_holder, lock_until FROM schema_migrations '
            "WHERE lock_holder IS NOT NULL AND lock_until > ?",
            (time.time(),)
        )
        return cur.fetchall()
    except sqlite3.OperationalError:
        return []
