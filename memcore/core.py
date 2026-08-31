"""MemCore — core memory operations.

All writes: tombstone admission guard -> short transaction -> audit event.
All reads: scope enforced in SQL WHERE (never post-filtering).
"""
import hashlib
import re
import sqlite3
import uuid
import json
from datetime import datetime, timedelta, timezone

from . import store


class MemCoreError(Exception):
    pass


class TombstoneBlocked(MemCoreError):
    def __init__(self, fingerprint, reason):
        self.fingerprint = fingerprint
        self.reason = reason
        super().__init__(
            f'claim blocked by active tombstone ({fingerprint[:8]}...): {reason}'
        )


class PermissionDenied(MemCoreError):
    pass


class NotFound(MemCoreError):
    pass


# ── helpers ────────────────────────────────────────────────────────────

def fingerprint(content: str) -> str:
    """Deterministic claim fingerprint: sha256 of normalized (whitespace-collapsed,
    lowercased) content, truncated to 16 hex chars — matches fixtures._fingerprint."""
    normalized = ' '.join(content.lower().strip().split())
    return hashlib.sha256(normalized.encode()).hexdigest()[:16]


def _new_id(prefix):
    return f'{prefix}-{uuid.uuid4().hex[:12]}'


def _now() -> str:
    """ISO 8601 UTC with Z suffix — the timestamp contract (migration 0005)."""
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _audit(conn, action, actor, memory_id=None, project_id=None, detail=None,
           write_key=None):
    conn.execute(
        'INSERT INTO audit_event (action, actor_agent_id, memory_id, project_id, detail, write_key) '
        'VALUES (?, ?, ?, ?, ?, ?)',
        (action, actor, memory_id, project_id, json.dumps(detail or {}), write_key)
    )


def admission_allowed(conn, content: str, project_id: str) -> bool:
    """Tombstone admission guard: True unless an active tombstone matches."""
    blocked = _tombstone_active(conn, fingerprint(content), project_id)
    return blocked is None


def _tombstone_active(conn, claim_fp, project_id):
    cur = conn.execute(
        'SELECT reason FROM tombstone '
        "WHERE claim_fingerprint = ? AND scope IN (?, 'global') AND overridden_by IS NULL",
        (claim_fp, project_id)
    )
    return cur.fetchone()


# ── writes ─────────────────────────────────────────────────────────────

def create_memory(conn, project_id, agent_id, content, scope='private',
                  memory_type='fact', lifecycle='candidate', idempotency_key=None,
                  reason=None):
    """Create a memory + first immutable version. Tombstone guard applies.

    Returns (memory_id, version_id) or existing ids if idempotency_key replays.
    """
    if scope not in ('project', 'private'):
        raise MemCoreError(f'invalid scope: {scope}')

    conn.execute('BEGIN IMMEDIATE')
    try:
        if idempotency_key:
            row = conn.execute(
                'SELECT memory_id, version_id FROM idempotency_key WHERE key = ?',
                (idempotency_key,)
            ).fetchone()
            if row:
                conn.execute('ROLLBACK')
                return row[0], row[1]

        claim_fp = fingerprint(content)
        blocked = _tombstone_active(conn, claim_fp, project_id)
        if blocked:
            conn.execute('ROLLBACK')
            raise TombstoneBlocked(claim_fp, blocked[0])

        # Project-scope writes require membership (Finding G).
        if scope == 'project':
            m = conn.execute(
                'SELECT 1 FROM project_membership WHERE project_id=? AND agent_id=?',
                (project_id, agent_id)
            ).fetchone()
            if not m:
                conn.execute('ROLLBACK')
                raise PermissionDenied(
                    f'agent {agent_id} is not a member of project {project_id}; '
                    'project-scope writes require membership'
                )

        mem_id = _new_id('mem')
        ver_id = _new_id('ver')
        conn.execute(
            'INSERT INTO memory (id, project_id, scope, owner_agent_id, type, '
            '  lifecycle, verification, freshness, current_version_id) '
            "VALUES (?, ?, ?, ?, ?, ?, 'unverified', 'current', ?)",
            (mem_id, project_id, scope, agent_id, memory_type,
             lifecycle, ver_id)
        )
        conn.execute(
            'INSERT INTO memory_version (id, memory_id, content, reason, '
            '  created_by_agent_id, created_at) VALUES (?, ?, ?, ?, ?, ?)',
            (ver_id, mem_id, content, reason, agent_id, _now())
        )
        _audit(conn, 'create', agent_id, mem_id, project_id,
               {'memory_id': mem_id, 'version_id': ver_id,
                'scope': scope, 'content': content},
               write_key=idempotency_key)
        if idempotency_key:
            conn.execute(
                'INSERT INTO idempotency_key (key, project_id, memory_id, version_id) '
                'VALUES (?, ?, ?, ?)',
                (idempotency_key, project_id, mem_id, ver_id)
            )
        conn.execute('COMMIT')
        return mem_id, ver_id
    except Exception:
        try:
            conn.execute('ROLLBACK')
        except sqlite3.OperationalError:
            pass
        raise


def supersede(conn, memory_id, agent_id, new_content, reason=None):
    """Create a new immutable version; flip lifecycle; keep history intact.

    The old memory's lifecycle becomes 'superseded' only until the caller
    marks the new state; here we keep the SAME memory id (correction model),
    set current_version_id to the new version, and leave lifecycle as-is
    unless the memory was 'superseded' (then re-activate not supported here).
    """
    conn.execute('BEGIN IMMEDIATE')
    try:
        mem = conn.execute(
            'SELECT project_id, scope, lifecycle FROM memory WHERE id=?',
            (memory_id,)
        ).fetchone()
        if not mem:
            conn.execute('ROLLBACK')
            raise NotFound(f'memory {memory_id} not found')
        project_id, scope, lifecycle = mem

        m = conn.execute(
            'SELECT 1 FROM project_membership WHERE project_id=? AND agent_id=?',
            (project_id, agent_id)
        ).fetchone()
        if not m:
            conn.execute('ROLLBACK')
            raise PermissionDenied(f'agent {agent_id} is not a member of project {project_id}')

        blocked = _tombstone_active(conn, fingerprint(new_content), project_id)
        if blocked:
            conn.execute('ROLLBACK')
            raise TombstoneBlocked(fingerprint(new_content), blocked[0])

        old_ver = conn.execute(
            'SELECT current_version_id FROM memory WHERE id=?', (memory_id,)
        ).fetchone()[0]
        new_ver = _new_id('ver')
        conn.execute(
            'INSERT INTO memory_version (id, memory_id, content, reason, '
            '  created_by_agent_id, supersedes_version_id, created_at) '
            'VALUES (?, ?, ?, ?, ?, ?, ?)',
            (new_ver, memory_id, new_content, reason, agent_id, old_ver, _now())
        )
        conn.execute(
            'UPDATE memory SET current_version_id=?, updated_at=? '
            'WHERE id=?',
            (new_ver, _now(), memory_id)
        )
        _audit(conn, 'supersede', agent_id, memory_id, project_id,
               {'new_version_id': new_ver, 'old_version_id': old_ver,
                'reason': reason})
        conn.execute('COMMIT')
        return new_ver
    except Exception:
        try:
            conn.execute('ROLLBACK')
        except sqlite3.OperationalError:
            pass
        raise


def supersede_memory(conn, old_memory_id, agent_id, new_content, reason=None,
                     new_project_id=None):
    """Correction model: supersede old memory in place.

    Creates a new version on the SAME memory, sets current_version_id,
    keeps history queryable. Lifecycle unchanged (unless caller overrides).
    """
    return supersede(conn, old_memory_id, agent_id, new_content, reason)


def promote(conn, memory_id, agent_id):
    """Promote private -> project scope. Audited. Owner or project owner only."""
    conn.execute('BEGIN IMMEDIATE')
    try:
        mem = conn.execute(
            'SELECT project_id, scope, owner_agent_id FROM memory WHERE id=?',
            (memory_id,)
        ).fetchone()
        if not mem:
            conn.execute('ROLLBACK')
            raise NotFound(f'memory {memory_id} not found')
        project_id, scope, owner = mem
        if scope != 'private':
            conn.execute('ROLLBACK')
            raise MemCoreError('memory is not private')

        if agent_id != owner:
            role = conn.execute(
                'SELECT role FROM project_membership WHERE project_id=? AND agent_id=?',
                (project_id, agent_id)
            ).fetchone()
            if not role or role[0] != 'owner':
                conn.execute('ROLLBACK')
                raise PermissionDenied(
                    f'only the owner or a project owner may promote {memory_id}'
                )

        conn.execute(
            "UPDATE memory SET scope='project', updated_at=datetime('now') WHERE id=?",
            (memory_id,)
        )
        _audit(conn, 'promote', agent_id, memory_id, project_id,
               {'from_scope': 'private', 'to_scope': 'project'})
        conn.execute('COMMIT')
    except Exception:
        try:
            conn.execute('ROLLBACK')
        except sqlite3.OperationalError:
            pass
        raise


def deactivate(conn, memory_id, agent_id, reason=None):
    """Soft delete: lifecycle -> disabled. Audited. Reversible via restore."""
    conn.execute('BEGIN IMMEDIATE')
    try:
        mem = conn.execute(
            'SELECT project_id FROM memory WHERE id=?', (memory_id,)
        ).fetchone()
        if not mem:
            conn.execute('ROLLBACK')
            raise NotFound(f'memory {memory_id} not found')
        conn.execute(
            "UPDATE memory SET lifecycle='disabled', updated_at=datetime('now') WHERE id=?",
            (memory_id,)
        )
        _audit(conn, 'deactivate', agent_id, memory_id, mem[0], {'reason': reason})
        conn.execute('COMMIT')
    except Exception:
        try:
            conn.execute('ROLLBACK')
        except sqlite3.OperationalError:
            pass
        raise


def restore(conn, memory_id, agent_id):
    """Undo deactivate: disabled -> candidate."""
    conn.execute('BEGIN IMMEDIATE')
    try:
        mem = conn.execute(
            'SELECT project_id, lifecycle FROM memory WHERE id=?', (memory_id,)
        ).fetchone()
        if not mem:
            conn.execute('ROLLBACK')
            raise NotFound(f'memory {memory_id} not found')
        if mem[1] != 'disabled':
            conn.execute('ROLLBACK')
            raise MemCoreError(f'memory is not disabled (lifecycle={mem[1]})')
        conn.execute(
            "UPDATE memory SET lifecycle='candidate', updated_at=datetime('now') WHERE id=?",
            (memory_id,)
        )
        _audit(conn, 'restore', agent_id, memory_id, mem[0], {})
        conn.execute('COMMIT')
    except Exception:
        try:
            conn.execute('ROLLBACK')
        except sqlite3.OperationalError:
            pass
        raise


def reject(conn, memory_id, agent_id, reason, create_tombstone=True):
    """Reject a memory: lifecycle -> rejected + optional tombstone fingerprint."""
    conn.execute('BEGIN IMMEDIATE')
    try:
        mem = conn.execute(
            'SELECT project_id, current_version_id FROM memory WHERE id=?',
            (memory_id,)
        ).fetchone()
        if not mem:
            conn.execute('ROLLBACK')
            raise NotFound(f'memory {memory_id} not found')
        project_id, cur_ver = mem
        content = conn.execute(
            'SELECT content FROM memory_version WHERE id=?', (cur_ver,)
        ).fetchone()[0]

        conn.execute(
            "UPDATE memory SET lifecycle='rejected', updated_at=datetime('now') WHERE id=?",
            (memory_id,)
        )
        if create_tombstone:
            conn.execute(
                'INSERT INTO tombstone (id, claim_fingerprint, scope, reason) '
                'VALUES (?, ?, ?, ?)',
                (_new_id('tomb'), fingerprint(content), project_id, reason)
            )
        _audit(conn, 'reject', agent_id, memory_id, project_id,
               {'reason': reason, 'tombstoned': create_tombstone})
        conn.execute('COMMIT')
    except Exception:
        try:
            conn.execute('ROLLBACK')
        except sqlite3.OperationalError:
            pass
        raise


# ── reads (scope enforced in SQL WHERE) ───────────────────────────────

def visible_memories(conn, project_id, agent_id, include_disabled=False,
                     include_rejected=False):
    """All memories agent_id may read in project_id.

    Scope rule lives in the WHERE clause, never in Python post-filtering:
      project scope -> every member reads it
      private scope -> owner only
    Excludes rejected/superseded/disabled from 'current truth' by default.
    """
    excluded = ["'rejected'", "'superseded'"]
    if not include_disabled:
        excluded.append("'disabled'")
    cur = conn.execute(
        'SELECT m.id, m.scope, m.lifecycle, m.verification, m.freshness, '
        '       v.content, m.owner_agent_id, m.type '
        'FROM memory m '
        'JOIN memory_version v ON v.id = m.current_version_id '
        'WHERE m.project_id = ? '
        "  AND (m.scope = 'project' OR m.owner_agent_id = ?) "
        f'  AND m.lifecycle NOT IN ({", ".join(excluded)}) '
        'ORDER BY m.pinned DESC, m.created_at',
        (project_id, agent_id)
    )
    return cur.fetchall()


def private_memories(conn, project_id, agent_id):
    """ONLY this agent's private memories in a project. Others' never appear."""
    cur = conn.execute(
        'SELECT m.id, m.scope, m.owner_agent_id, v.content '
        'FROM memory m '
        'JOIN memory_version v ON v.id = m.current_version_id '
        'WHERE m.project_id = ? '
        "  AND m.scope = 'private' "
        '  AND m.owner_agent_id = ?',
        (project_id, agent_id)
    )
    return cur.fetchall()


def _fts_query(query: str) -> str:
    """Sanitize a raw user string into a safe FTS5 MATCH expression.

    Extracts simple tokens (letters/digits/underscore) and wraps each in
    double quotes — FTS5 phrase syntax, immune to apostrophes, parens and
    operators. Tokens join with OR (any-match semantics; AND would drop
    results for multi-word queries that only partially overlap).
    """
    tokens = re.findall(r'[A-Za-z0-9_]+', query)
    if not tokens:
        return ''
    return ' OR '.join('"%s"' % t for t in tokens)


def search(conn, project_id, agent_id, query, limit=20):
    """FTS5 search over memory content, scope-enforced in SQL.

    Deterministic rank: FTS bm25 + pinned + lifecycle/verification/freshness
    as simple SQL-level factors. No model, no floats stored.
    """
    match_expr = _fts_query(query)
    if not match_expr:
        return []
    cur = conn.execute(
        'SELECT m.id, m.scope, m.lifecycle, m.verification, m.freshness, '
        '       v.content, m.owner_agent_id, '
        '       bm25(memory_version_fts) AS rank '
        'FROM memory_version_fts fts '
        'JOIN memory_version v ON v.rowid = fts.rowid '
        'JOIN memory m ON m.id = v.memory_id '
        'WHERE memory_version_fts MATCH ? '
        '  AND v.id = m.current_version_id '
        '  AND m.project_id = ? '
        "  AND (m.scope = 'project' OR m.owner_agent_id = ?) "
        "  AND m.lifecycle IN ('candidate', 'accepted', 'conflict') "
        'ORDER BY m.pinned DESC, rank ASC '
        'LIMIT ?',
        (match_expr, project_id, agent_id, limit)
    )
    return cur.fetchall()


def conflict_memories(conn, project_id):
    """Memories in conflict state — for abstain/expose-conflict behavior."""
    cur = conn.execute(
        'SELECT m.id, m.owner_agent_id, v.content '
        'FROM memory m '
        'JOIN memory_version v ON v.id = m.current_version_id '
        "WHERE m.project_id = ? AND m.lifecycle = 'conflict'",
        (project_id,)
    )
    return cur.fetchall()


def superseded_history(conn, memory_id):
    """All versions of a memory, oldest first — historical truth query."""
    cur = conn.execute(
        'SELECT id, content, created_at, supersedes_version_id '
        'FROM memory_version WHERE memory_id = ? ORDER BY created_at',
        (memory_id,)
    )
    return cur.fetchall()


# ── ops: gc / stats / import ──────────────────────────────────────────

def _cutoff(conn, days):
    """Cutoff timestamp in sqlite 'YYYY-MM-DD HH:MM:SS' UTC — the format
    datetime('now') DEFAULTs actually store, so string comparison is exact."""
    return conn.execute(
        "SELECT strftime('%Y-%m-%d %H:%M:%S', 'now', ?)", (f'-{days} days',)
    ).fetchone()[0]


def gc_scan(conn, candidate_days=30, tombstone_days=90):
    """List gc candidates WITHOUT touching anything.

    a) candidate memories older than candidate_days with zero evidence links
    b) tombstones older than tombstone_days
    Returns (candidates, tombstones); each row starts with the id.
    """
    cutoff_c = _cutoff(conn, candidate_days)
    cutoff_t = _cutoff(conn, tombstone_days)
    candidates = conn.execute(
        'SELECT m.id, m.project_id, m.owner_agent_id, v.content, m.created_at '
        'FROM memory m '
        'JOIN memory_version v ON v.id = m.current_version_id '
        "WHERE m.lifecycle = 'candidate' "
        '  AND m.created_at < ? '
        '  AND NOT EXISTS (SELECT 1 FROM evidence_link el '
        '                  JOIN memory_version lv ON lv.id = el.memory_version_id '
        '                  WHERE lv.memory_id = m.id) '
        'ORDER BY m.created_at',
        (cutoff_c,)
    ).fetchall()
    tombstones = conn.execute(
        'SELECT t.id, t.claim_fingerprint, t.scope, t.reason, t.created_at, '
        '       t.overridden_by '
        'FROM tombstone t WHERE t.created_at < ? ORDER BY t.created_at',
        (cutoff_t,)
    ).fetchall()
    return candidates, tombstones


def gc_apply(conn, candidate_days=30, tombstone_days=90):
    """Run the sweep: gc candidates -> tombstoned via reject();
    old tombstones -> purged. Same short-transaction + audit discipline
    as every other write. Returns (tombstoned_ids, purged_ids)."""
    candidates, tombstones = gc_scan(conn, candidate_days, tombstone_days)
    tombstoned, purged = [], []
    for row in candidates:
        mem_id = row[0]
        conn.execute('BEGIN IMMEDIATE')
        try:
            # Re-check lifecycle inside the transaction (state may have moved).
            lc = conn.execute(
                'SELECT lifecycle FROM memory WHERE id=?', (mem_id,)
            ).fetchone()
            if not lc or lc[0] != 'candidate':
                conn.execute('ROLLBACK')
                continue
            conn.execute(
                "UPDATE memory SET lifecycle='rejected', updated_at=? WHERE id=?",
                (_now(), mem_id)
            )
            conn.execute(
                'INSERT INTO tombstone (id, claim_fingerprint, scope, reason) '
                'VALUES (?, ?, ?, ?)',
                (_new_id('tomb'), fingerprint(row[3]), row[1], 'gc')
            )
            _audit(conn, 'gc_tombstone', None, mem_id, row[1],
                   {'reason': 'gc', 'content': row[3]})
            conn.execute('COMMIT')
            tombstoned.append(mem_id)
        except Exception:
            try:
                conn.execute('ROLLBACK')
            except sqlite3.OperationalError:
                pass
            raise
    for row in tombstones:
        conn.execute('BEGIN IMMEDIATE')
        try:
            conn.execute('DELETE FROM tombstone WHERE id=?', (row[0],))
            _audit(conn, 'gc_purge_tombstone', None, None, None,
                   {'tombstone_id': row[0], 'claim_fingerprint': row[1],
                    'scope': row[2], 'reason': row[3]})
            conn.execute('COMMIT')
            purged.append(row[0])
        except Exception:
            try:
                conn.execute('ROLLBACK')
            except sqlite3.OperationalError:
                pass
            raise
    return tombstoned, purged


def stats(conn):
    """Operational stats: lifecycle/scope counts, top authors, avg summary
    length, FTS drift check. Dict out, no printing (CLI renders)."""
    def one(sql, args=()):
        return conn.execute(sql, args).fetchone()[0]

    by_lifecycle = dict(conn.execute(
        'SELECT lifecycle, COUNT(*) FROM memory GROUP BY lifecycle').fetchall())
    by_scope = dict(conn.execute(
        'SELECT scope, COUNT(*) FROM memory GROUP BY scope').fetchall())
    top_agents = conn.execute(
        'SELECT a.name, COUNT(*) AS n FROM memory m '
        'JOIN agent a ON a.id = m.owner_agent_id '
        'GROUP BY m.owner_agent_id ORDER BY n DESC LIMIT 5'
    ).fetchall()
    avg_len = conn.execute(
        'SELECT AVG(LENGTH(v.content)) FROM memory m '
        'JOIN memory_version v ON v.id = m.current_version_id'
    ).fetchone()[0]
    fts_rows = one('SELECT COUNT(*) FROM memory_version_fts')
    ver_rows = one('SELECT COUNT(*) FROM memory_version')
    return {
        'memories_total': one('SELECT COUNT(*) FROM memory'),
        'by_lifecycle': by_lifecycle,
        'by_scope': by_scope,
        'top_agents': [{'agent': n, 'memories': c} for n, c in top_agents],
        'avg_summary_length': round(avg_len, 1) if avg_len is not None else 0.0,
        'fts': {'fts_rows': fts_rows, 'version_rows': ver_rows,
                'in_sync': fts_rows == ver_rows},
    }


def import_memories(conn, items, project_id, agent_id, scope='project'):
    """Bulk import: each item {title, summary, type, evidence:[...]} becomes a
    CANDIDATE through core.create_memory (transaction + audit, unchanged).

    Idempotent: per-item key ``import:<project_id>:<fingerprint(summary)>``
    replays return the existing memory (ALTIMA gate #1). Dedup within the
    batch uses the same fingerprint. Per-item try/except also covers the
    evidence inserts, so a failing item leaves no partial rows (gate #4).
    NOTE: membership is NOT auto-granted here (Finding G — non-member import
    must fail closed); the CLI layer ensures membership explicitly (gate #3).
    Returns dict {'added': n, 'skipped': n, 'created': [(mem_id, ver_id)...]}.
    """
    added, skipped, created = 0, 0, []
    seen = set()
    for item in items:
        summary = item.get('summary') or ''
        if not summary.strip():
            skipped += 1
            continue
        fp = fingerprint(summary)
        if fp in seen:
            skipped += 1
            continue
        ikey = f'import:{project_id}:{fp}'
        already = conn.execute(
            'SELECT 1 FROM idempotency_key WHERE key = ?', (ikey,)
        ).fetchone()
        if already:
            skipped += 1
            continue
        try:
            mem_id, ver_id = create_memory(
                conn, project_id, agent_id,
                summary, scope=scope,
                memory_type=item.get('type') or 'fact',
                idempotency_key=ikey,
            )
            for ev in item.get('evidence') or []:
                kind = ev.get('kind')
                # Trust boundary: normalize unknown kinds to the schema's
                # catch-all instead of crashing mid-batch (batch1 used 'source').
                if kind not in ('file', 'commit', 'test', 'observation',
                                'user_input', 'external'):
                    kind = 'external'
                ev_id = _new_id('ev')
                conn.execute(
                    'INSERT INTO evidence (id, kind, source_uri, source_label) '
                    'VALUES (?, ?, ?, ?)',
                    (ev_id, kind, ev.get('source_uri'), ev.get('source_label'))
                )
                conn.execute(
                    'INSERT INTO evidence_link (evidence_id, memory_version_id, relation) '
                    "VALUES (?, ?, 'supports')",
                    (ev_id, ver_id)
                )
            conn.commit()
        except (TombstoneBlocked, PermissionDenied, MemCoreError):
            skipped += 1
            continue
        seen.add(fp)
        added += 1
        created.append((mem_id, ver_id))
    return {'added': added, 'skipped': skipped, 'created': created}
