"""MemCore — core memory operations.

All writes: tombstone admission guard -> short transaction -> audit event.
All reads: scope enforced in SQL WHERE (never post-filtering).
"""
import hashlib
import re
import sqlite3
import uuid
import json
import unicodedata
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


def _membership_role(conn, project_id, agent_id):
    row = conn.execute(
        'SELECT role FROM project_membership WHERE project_id=? AND agent_id=?',
        (project_id, agent_id)
    ).fetchone()
    return row[0] if row else None


def _require_membership(conn, project_id, agent_id):
    role = _membership_role(conn, project_id, agent_id)
    if role is None:
        raise PermissionDenied(
            f'agent {agent_id} is not a member of project {project_id}'
        )
    return role


def _require_memory_write_access(conn, memory_id, agent_id):
    """Return memory metadata after enforcing project/private write boundaries."""
    mem = conn.execute(
        'SELECT project_id, scope, owner_agent_id, lifecycle FROM memory WHERE id=?',
        (memory_id,)
    ).fetchone()
    if not mem:
        raise NotFound(f'memory {memory_id} not found')
    project_id, scope, owner, lifecycle = mem
    role = _require_membership(conn, project_id, agent_id)
    if scope == 'private' and agent_id != owner and role != 'owner':
        raise PermissionDenied(
            f'agent {agent_id} cannot modify private memory owned by {owner}'
        )
    return project_id, scope, owner, lifecycle, role


# ── writes ─────────────────────────────────────────────────────────────

def create_memory(conn, project_id, agent_id, content, scope='private',
                  memory_type='fact', lifecycle='candidate', idempotency_key=None,
                  reason=None, _manage_transaction=True):
    """Create a memory + first immutable version. Tombstone guard applies.

    Returns (memory_id, version_id) or existing ids if idempotency_key replays.
    """
    if scope not in ('project', 'private'):
        raise MemCoreError(f'invalid scope: {scope}')

    if _manage_transaction:
        conn.execute('BEGIN IMMEDIATE')
    try:
        # Membership is the first project-boundary gate. Do not reveal
        # idempotency/tombstone state to a non-member by varying the error.
        role = _require_membership(conn, project_id, agent_id)

        if idempotency_key:
            row = conn.execute(
                'SELECT ik.project_id, ik.memory_id, ik.version_id, '
                '       m.scope, m.owner_agent_id, m.lifecycle, '
                '       iv.content, cv.content '
                'FROM idempotency_key ik '
                'JOIN memory m ON m.id=ik.memory_id '
                'JOIN memory_version iv ON iv.id=ik.version_id '
                'JOIN memory_version cv ON cv.id=m.current_version_id '
                'WHERE ik.key=?',
                (idempotency_key,)
            ).fetchone()
            if row:
                (existing_project, existing_memory, existing_version,
                 existing_scope, existing_owner, existing_lifecycle,
                 original_content, current_content) = row
                if existing_project != project_id:
                    raise PermissionDenied(
                        'idempotency key belongs to a different project'
                    )
                if existing_scope == 'private' and agent_id != existing_owner and role != 'owner':
                    raise PermissionDenied('idempotency replay cannot access private memory')
                if existing_scope != scope:
                    raise MemCoreError(
                        f'idempotency key reused with different scope '
                        f'({existing_scope} != {scope})'
                    )
                if fingerprint(original_content) != fingerprint(content):
                    raise MemCoreError('idempotency key reused with different content')
                if existing_lifecycle == 'rejected':
                    blocked = _tombstone_active(
                        conn, fingerprint(current_content), project_id
                    )
                    if blocked:
                        raise TombstoneBlocked(
                            fingerprint(current_content), blocked[0]
                        )
                if _manage_transaction:
                    conn.execute('ROLLBACK')
                return existing_memory, existing_version

        claim_fp = fingerprint(content)
        blocked = _tombstone_active(conn, claim_fp, project_id)
        if blocked:
            if _manage_transaction:
                conn.execute('ROLLBACK')
            raise TombstoneBlocked(claim_fp, blocked[0])

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
        if _manage_transaction:
            conn.execute('COMMIT')
        return mem_id, ver_id
    except Exception:
        if _manage_transaction:
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
        project_id, scope, owner, lifecycle, role = _require_memory_write_access(
            conn, memory_id, agent_id
        )

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

    Cross-project moves are not part of the correction model. Reject an
    explicit different new_project_id instead of silently ignoring it.
    """
    if new_project_id is not None:
        row = conn.execute(
            'SELECT project_id FROM memory WHERE id=?', (old_memory_id,)
        ).fetchone()
        if not row:
            raise NotFound(f'memory {old_memory_id} not found')
        if new_project_id != row[0]:
            raise MemCoreError(
                'supersede_memory cannot move a memory across projects'
            )
    return supersede(conn, old_memory_id, agent_id, new_content, reason)


def promote(conn, memory_id, agent_id):
    """Promote private -> project scope. Audited. Owner or project owner only."""
    conn.execute('BEGIN IMMEDIATE')
    try:
        mem = conn.execute(
            'SELECT project_id, scope, owner_agent_id, lifecycle, current_version_id '
            'FROM memory WHERE id=?',
            (memory_id,)
        ).fetchone()
        if not mem:
            conn.execute('ROLLBACK')
            raise NotFound(f'memory {memory_id} not found')
        project_id, scope, owner, lifecycle, current_version_id = mem
        role = _require_membership(conn, project_id, agent_id)
        if scope != 'private':
            conn.execute('ROLLBACK')
            raise MemCoreError('memory is not private')
        if lifecycle in ('rejected', 'disabled', 'superseded'):
            raise MemCoreError(
                f'cannot promote terminal memory (lifecycle={lifecycle})'
            )

        if agent_id != owner and role != 'owner':
            conn.execute('ROLLBACK')
            raise PermissionDenied(
                f'only the owner or a project owner may promote {memory_id}'
            )

        content = conn.execute(
            'SELECT content FROM memory_version WHERE id=?', (current_version_id,)
        ).fetchone()[0]
        blocked = _tombstone_active(conn, fingerprint(content), project_id)
        if blocked:
            raise TombstoneBlocked(fingerprint(content), blocked[0])

        conn.execute(
            "UPDATE memory SET scope='project', updated_at=? WHERE id=?",
            (_now(), memory_id)
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
        project_id, scope, owner, lifecycle, role = _require_memory_write_access(
            conn, memory_id, agent_id
        )
        if lifecycle in ('rejected', 'superseded'):
            raise MemCoreError(
                f'cannot deactivate terminal memory (lifecycle={lifecycle})'
            )
        conn.execute(
            "UPDATE memory SET lifecycle='disabled', updated_at=? WHERE id=?",
            (_now(), memory_id)
        )
        _audit(conn, 'deactivate', agent_id, memory_id, project_id, {'reason': reason})
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
        project_id, scope, owner, lifecycle, role = _require_memory_write_access(
            conn, memory_id, agent_id
        )
        if lifecycle != 'disabled':
            conn.execute('ROLLBACK')
            raise MemCoreError(f'memory is not disabled (lifecycle={lifecycle})')
        cur_ver = conn.execute(
            'SELECT current_version_id FROM memory WHERE id=?', (memory_id,)
        ).fetchone()[0]
        content = conn.execute(
            'SELECT content FROM memory_version WHERE id=?', (cur_ver,)
        ).fetchone()[0]
        blocked = _tombstone_active(conn, fingerprint(content), project_id)
        if blocked:
            raise TombstoneBlocked(fingerprint(content), blocked[0])
        conn.execute(
            "UPDATE memory SET lifecycle='candidate', updated_at=? WHERE id=?",
            (_now(), memory_id)
        )
        _audit(conn, 'restore', agent_id, memory_id, project_id, {})
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
        project_id, scope, owner, lifecycle, role = _require_memory_write_access(
            conn, memory_id, agent_id
        )
        if lifecycle == 'rejected':
            conn.execute('ROLLBACK')
            return False

        cur_ver = conn.execute(
            'SELECT current_version_id FROM memory WHERE id=?', (memory_id,)
        ).fetchone()[0]
        content = conn.execute(
            'SELECT content FROM memory_version WHERE id=?', (cur_ver,)
        ).fetchone()[0]
        claim_fp = fingerprint(content)

        conn.execute(
            "UPDATE memory SET lifecycle='rejected', updated_at=? WHERE id=?",
            (_now(), memory_id)
        )
        tombstone_created = False
        if create_tombstone and not _tombstone_active(conn, claim_fp, project_id):
            conn.execute(
                'INSERT INTO tombstone (id, claim_fingerprint, scope, reason) '
                'VALUES (?, ?, ?, ?)',
                (_new_id('tomb'), claim_fp, project_id, reason)
            )
            tombstone_created = True
        _audit(conn, 'reject', agent_id, memory_id, project_id,
               {'reason': reason, 'tombstoned': tombstone_created})
        conn.execute('COMMIT')
        return True
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
    excluded = ["'superseded'"]
    if not include_rejected:
        excluded.append("'rejected'")
    if not include_disabled:
        excluded.append("'disabled'")
    cur = conn.execute(
        'SELECT m.id, m.scope, m.lifecycle, m.verification, m.freshness, '
        '       v.content, m.owner_agent_id, m.type '
        'FROM memory m '
        'JOIN memory_version v ON v.id = m.current_version_id '
        'WHERE m.project_id = ? '
        '  AND EXISTS (SELECT 1 FROM project_membership pm '
        '              WHERE pm.project_id = m.project_id AND pm.agent_id = ?) '
        "  AND (m.scope = 'project' OR m.owner_agent_id = ?) "
        f'  AND m.lifecycle NOT IN ({", ".join(excluded)}) '
        'ORDER BY m.pinned DESC, m.created_at',
        (project_id, agent_id, agent_id)
    )
    return cur.fetchall()


def private_memories(conn, project_id, agent_id):
    """ONLY this agent's private memories in a project. Others' never appear."""
    cur = conn.execute(
        'SELECT m.id, m.scope, m.owner_agent_id, v.content '
        'FROM memory m '
        'JOIN memory_version v ON v.id = m.current_version_id '
        'WHERE m.project_id = ? '
        '  AND EXISTS (SELECT 1 FROM project_membership pm '
        '              WHERE pm.project_id = m.project_id AND pm.agent_id = ?) '
        "  AND m.scope = 'private' "
        '  AND m.owner_agent_id = ?',
        (project_id, agent_id, agent_id)
    )
    return cur.fetchall()


def _fts_query(query: str) -> str:
    """Sanitize a raw Unicode user string into a safe FTS5 expression.

    Keep Unicode letters/numbers/marks plus underscore, split on punctuation,
    then quote each token. This preserves Thai and other non-Latin scripts
    while remaining immune to FTS5 operators/apostrophes/parens.
    """
    tokens, buf = [], []
    for ch in str(query):
        category = unicodedata.category(ch)
        if ch == '_' or category[:1] in ('L', 'N', 'M'):
            buf.append(ch)
        elif buf:
            tokens.append(''.join(buf))
            buf = []
    if buf:
        tokens.append(''.join(buf))
    if not tokens:
        return ''
    return ' OR '.join('"%s"' % token for token in tokens)


def search(conn, project_id, agent_id, query, limit=20):
    """FTS5 search over memory content, scope-enforced in SQL.

    Deterministic rank: FTS bm25 + pinned + lifecycle/verification/freshness.
    For non-ASCII queries, try an exact Unicode substring match first because
    SQLite unicode61 does not segment Thai/CJK natural-language words well.
    """
    raw_query = str(query or '').strip()
    if not raw_query:
        return []
    if any(ord(ch) > 127 for ch in raw_query):
        rows = conn.execute(
            'SELECT m.id, m.scope, m.lifecycle, m.verification, m.freshness, '
            '       v.content, m.owner_agent_id, 0.0 AS rank '
            'FROM memory m JOIN memory_version v ON v.id = m.current_version_id '
            'WHERE m.project_id = ? '
            '  AND EXISTS (SELECT 1 FROM project_membership pm '
            '              WHERE pm.project_id = m.project_id AND pm.agent_id = ?) '
            "  AND (m.scope = 'project' OR m.owner_agent_id = ?) "
            "  AND m.lifecycle IN ('candidate', 'accepted', 'conflict') "
            '  AND instr(v.content, ?) > 0 '
            'ORDER BY m.pinned DESC, v.created_at DESC LIMIT ?',
            (project_id, agent_id, agent_id, raw_query, limit)
        ).fetchall()
        if rows:
            return rows
    match_expr = _fts_query(raw_query)
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
        '  AND EXISTS (SELECT 1 FROM project_membership pm '
        '              WHERE pm.project_id = m.project_id AND pm.agent_id = ?) '
        "  AND (m.scope = 'project' OR m.owner_agent_id = ?) "
        "  AND m.lifecycle IN ('candidate', 'accepted', 'conflict') "
        'ORDER BY m.pinned DESC, rank ASC '
        'LIMIT ?',
        (match_expr, project_id, agent_id, agent_id, limit)
    )
    return cur.fetchall()


def conflict_memories(conn, project_id, agent_id):
    """Readable conflict memories for one member; private scope never leaks."""
    cur = conn.execute(
        'SELECT m.id, m.owner_agent_id, v.content '
        'FROM memory m '
        'JOIN memory_version v ON v.id = m.current_version_id '
        "WHERE m.project_id = ? AND m.lifecycle = 'conflict' "
        '  AND EXISTS (SELECT 1 FROM project_membership pm '
        '              WHERE pm.project_id=m.project_id AND pm.agent_id=?) '
        "  AND (m.scope='project' OR m.owner_agent_id=?)",
        (project_id, agent_id, agent_id)
    )
    return cur.fetchall()


def superseded_history(conn, memory_id, agent_id):
    """Readable versions of one memory, oldest first; scope enforced in SQL."""
    cur = conn.execute(
        'SELECT v.id, v.content, v.created_at, v.supersedes_version_id '
        'FROM memory m JOIN memory_version v ON v.memory_id=m.id '
        'WHERE m.id=? '
        '  AND EXISTS (SELECT 1 FROM project_membership pm '
        '              WHERE pm.project_id=m.project_id AND pm.agent_id=?) '
        "  AND (m.scope='project' OR m.owner_agent_id=?) "
        'ORDER BY v.created_at',
        (memory_id, agent_id, agent_id)
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
    b) explicitly overridden tombstones older than tombstone_days
    Active tombstones are durable rejection guards and are never age-purged.
    Returns (candidates, tombstones); each row starts with the id.
    """
    if candidate_days < 0 or tombstone_days < 0:
        raise MemCoreError('GC retention days must be >= 0')
    cutoff_c = _cutoff(conn, candidate_days)
    cutoff_t = _cutoff(conn, tombstone_days)
    candidates = conn.execute(
        'SELECT m.id, m.project_id, m.owner_agent_id, v.content, m.created_at '
        'FROM memory m '
        'JOIN memory_version v ON v.id = m.current_version_id '
        "WHERE m.lifecycle = 'candidate' "
        '  AND datetime(m.created_at) < datetime(?) '
        '  AND NOT EXISTS (SELECT 1 FROM evidence_link el '
        '                  JOIN memory_version lv ON lv.id = el.memory_version_id '
        '                  WHERE lv.memory_id = m.id) '
        'ORDER BY m.created_at',
        (cutoff_c,)
    ).fetchall()
    tombstones = conn.execute(
        'SELECT t.id, t.claim_fingerprint, t.scope, t.reason, t.created_at, '
        '       t.overridden_by '
        'FROM tombstone t WHERE t.overridden_by IS NOT NULL '
        'AND datetime(t.created_at) < datetime(?) ORDER BY t.created_at',
        (cutoff_t,)
    ).fetchall()
    return candidates, tombstones


def gc_apply(conn, candidate_days=30, tombstone_days=90):
    """Run the sweep: stale candidates -> rejected+tombstoned;
    old *overridden* tombstones -> purged. Active rejection guards persist until
    explicit override. Returns (tombstoned_ids, purged_ids)."""
    candidates, tombstones = gc_scan(conn, candidate_days, tombstone_days)
    cutoff_c = _cutoff(conn, candidate_days)
    cutoff_t = _cutoff(conn, tombstone_days)
    tombstoned, purged = [], []
    for stale_row in candidates:
        mem_id = stale_row[0]
        conn.execute('BEGIN IMMEDIATE')
        try:
            # Re-evaluate every destructive predicate under the write lock.
            # A memory may gain evidence, be corrected, or age across the
            # scan/apply gap; GC must act on current state/content only.
            row = conn.execute(
                'SELECT m.project_id, v.content '
                'FROM memory m '
                'JOIN memory_version v ON v.id=m.current_version_id '
                'WHERE m.id=? AND m.lifecycle=\'candidate\' '
                '  AND datetime(m.created_at) < datetime(?) '
                '  AND NOT EXISTS (SELECT 1 FROM evidence_link el '
                '                  JOIN memory_version lv ON lv.id=el.memory_version_id '
                '                  WHERE lv.memory_id=m.id)',
                (mem_id, cutoff_c)
            ).fetchone()
            if not row:
                conn.execute('ROLLBACK')
                continue
            project_id, content = row
            claim_fp = fingerprint(content)
            conn.execute(
                "UPDATE memory SET lifecycle='rejected', updated_at=? WHERE id=?",
                (_now(), mem_id)
            )
            tombstone_created = False
            if not _tombstone_active(conn, claim_fp, project_id):
                conn.execute(
                    'INSERT INTO tombstone (id, claim_fingerprint, scope, reason) '
                    'VALUES (?, ?, ?, ?)',
                    (_new_id('tomb'), claim_fp, project_id, 'gc')
                )
                tombstone_created = True
            _audit(conn, 'gc_tombstone', None, mem_id, project_id,
                   {'reason': 'gc', 'content': content,
                    'tombstoned': tombstone_created})
            conn.execute('COMMIT')
            tombstoned.append(mem_id)
        except Exception:
            try:
                conn.execute('ROLLBACK')
            except sqlite3.OperationalError:
                pass
            raise
    for stale_row in tombstones:
        tomb_id = stale_row[0]
        conn.execute('BEGIN IMMEDIATE')
        try:
            row = conn.execute(
                'SELECT claim_fingerprint, scope, reason FROM tombstone '
                'WHERE id=? AND overridden_by IS NOT NULL '
                'AND datetime(created_at) < datetime(?)',
                (tomb_id, cutoff_t)
            ).fetchone()
            if not row:
                conn.execute('ROLLBACK')
                continue
            claim_fp, scope, reason = row
            conn.execute('DELETE FROM tombstone WHERE id=?', (tomb_id,))
            _audit(conn, 'gc_purge_tombstone', None, None, None,
                   {'tombstone_id': tomb_id, 'claim_fingerprint': claim_fp,
                    'scope': scope, 'reason': reason})
            conn.execute('COMMIT')
            purged.append(tomb_id)
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


def _import_item_summary(item):
    """Validate one import item without mutating the store."""
    if not isinstance(item, dict):
        return None, 'invalid_item'
    summary = item.get('summary')
    if not isinstance(summary, str) or not summary.strip():
        return None, 'empty_summary'
    evidence = item.get('evidence') or []
    if not isinstance(evidence, list) or any(not isinstance(ev, dict) for ev in evidence):
        return None, 'invalid_evidence'
    for ev in evidence:
        for field in ('source_uri', 'source_label'):
            value = ev.get(field)
            if value is not None and not isinstance(value, str):
                return None, 'invalid_evidence'
    return summary, None


def _claim_already_present(conn, project_id, claim_fp, scope='project', agent_id=None):
    """Check current non-rejected memories in the same visibility scope."""
    if scope not in ('project', 'private'):
        raise MemCoreError(f'invalid scope: {scope}')
    sql = (
        'SELECT v.content FROM memory m '
        'JOIN memory_version v ON v.id=m.current_version_id '
        'WHERE m.project_id=? AND m.scope=? AND m.lifecycle != \'rejected\' '
    )
    args = [project_id, scope]
    if scope == 'private':
        if not agent_id:
            raise MemCoreError('agent_id is required when planning private import')
        sql += 'AND m.owner_agent_id=? '
        args.append(agent_id)
    for (content,) in conn.execute(sql, args):
        if fingerprint(content) == claim_fp:
            return True
    return False


def plan_import(conn, items, project_id, scope='project', agent_id=None):
    """Read-only import preview: classify every item and perform zero writes."""
    if scope not in ('project', 'private'):
        raise MemCoreError(f'invalid scope: {scope}')
    if scope == 'private' and not agent_id:
        raise MemCoreError('agent_id is required when planning private import')
    plan = {'total': len(items), 'would_add': 0, 'skipped': 0,
            'reasons': {}, 'items': []}
    seen = set()
    for index, item in enumerate(items):
        summary, reason = _import_item_summary(item)
        fp = fingerprint(summary) if summary is not None else None
        if reason is None and fp in seen:
            reason = 'duplicate_input'
        if reason is None:
            ikey = f'import:{project_id}:{fp}'
            if conn.execute('SELECT 1 FROM idempotency_key WHERE key=?', (ikey,)).fetchone():
                reason = 'already_imported'
            elif _tombstone_active(conn, fp, project_id):
                reason = 'tombstone_blocked'
            elif _claim_already_present(
                conn, project_id, fp, scope=scope, agent_id=agent_id
            ):
                reason = 'already_present'
        if reason is None:
            seen.add(fp)
            plan['would_add'] += 1
            status = 'would_add'
        else:
            if reason == 'already_imported' and fp is not None:
                seen.add(fp)
            plan['skipped'] += 1
            plan['reasons'][reason] = plan['reasons'].get(reason, 0) + 1
            status = reason
        plan['items'].append({'index': index, 'status': status,
                              'fingerprint': fp})
    return plan


def import_memories(conn, items, project_id, agent_id, scope='project'):
    """Bulk import candidate memories with per-item atomicity.

    Each memory, audit/idempotency row, and all of its evidence links commit in
    ONE transaction. If evidence insertion fails, the whole item rolls back.
    Re-imports are idempotent by ``import:<project>:<fingerprint>`` and exact
    claims already present in the same visibility scope are not duplicated.
    """
    if scope not in ('project', 'private'):
        raise MemCoreError(f'invalid scope: {scope}')
    added, skipped, created = 0, 0, []
    seen = set()
    for item in items:
        summary, invalid_reason = _import_item_summary(item)
        if invalid_reason is not None:
            skipped += 1
            continue
        fp = fingerprint(summary)
        if fp in seen:
            skipped += 1
            continue
        ikey = f'import:{project_id}:{fp}'
        conn.execute('BEGIN IMMEDIATE')
        try:
            already = conn.execute(
                'SELECT 1 FROM idempotency_key WHERE key = ?', (ikey,)
            ).fetchone()
            if already:
                conn.execute('ROLLBACK')
                seen.add(fp)
                skipped += 1
                continue
            if _tombstone_active(conn, fp, project_id):
                conn.execute('ROLLBACK')
                seen.add(fp)
                skipped += 1
                continue
            if _claim_already_present(
                conn, project_id, fp, scope=scope, agent_id=agent_id
            ):
                conn.execute('ROLLBACK')
                seen.add(fp)
                skipped += 1
                continue
            mem_id, ver_id = create_memory(
                conn, project_id, agent_id,
                summary, scope=scope,
                memory_type=item.get('type') or 'fact',
                idempotency_key=ikey,
                _manage_transaction=False,
            )
            for ev in item.get('evidence') or []:
                kind = ev.get('kind')
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
            conn.execute('COMMIT')
        except (TombstoneBlocked, PermissionDenied, MemCoreError):
            try:
                conn.execute('ROLLBACK')
            except sqlite3.OperationalError:
                pass
            skipped += 1
            continue
        except Exception:
            try:
                conn.execute('ROLLBACK')
            except sqlite3.OperationalError:
                pass
            raise
        seen.add(fp)
        added += 1
        created.append((mem_id, ver_id))
    return {'added': added, 'skipped': skipped, 'created': created}
