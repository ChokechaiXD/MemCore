"""MemCore Ã¢â‚¬â€ core memory operations.

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


# Ã¢â€â‚¬Ã¢â€â‚¬ helpers Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬

def fingerprint(content: str) -> str:
    """Deterministic claim fingerprint: sha256 of normalized (whitespace-collapsed,
    lowercased) content, truncated to 16 hex chars Ã¢â‚¬â€ matches fixtures._fingerprint."""
    normalized = ' '.join(content.lower().strip().split())
    return hashlib.sha256(normalized.encode()).hexdigest()[:16]


def _new_id(prefix):
    return f'{prefix}-{uuid.uuid4().hex[:12]}'


def _now() -> str:
    """ISO 8601 UTC with Z suffix Ã¢â‚¬â€ the timestamp contract (migration 0005)."""
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _audit(conn, action, actor, memory_id=None, project_id=None, detail=None,
           write_key=None):
    conn.execute(
        'INSERT INTO audit_event (action, actor_agent_id, memory_id, project_id, detail, write_key, created_at) '
        'VALUES (?, ?, ?, ?, ?, ?, ?)',
        (action, actor, memory_id, project_id, json.dumps(detail or {}), write_key, _now())
    )


def _private_tombstone_scope(project_id: str, agent_id: str) -> str:
    return f'private:{project_id}:{agent_id}'


def _tombstone_scope(project_id: str, memory_scope: str, owner_agent_id: str) -> str:
    if memory_scope == 'private':
        return _private_tombstone_scope(project_id, owner_agent_id)
    return project_id


def admission_allowed(conn, content: str, project_id: str,
                      scope: str = 'project', agent_id: str | None = None) -> bool:
    """Tombstone admission guard with project/private scope hierarchy."""
    blocked = _tombstone_active(
        conn, fingerprint(content), project_id, scope=scope, agent_id=agent_id
    )
    return blocked is None


def _tombstone_active(conn, claim_fp, project_id, scope='project', agent_id=None):
    scopes = [project_id, 'global']
    if scope == 'private':
        if not agent_id:
            raise MemCoreError('private tombstone lookup requires agent_id')
        scopes.insert(0, _private_tombstone_scope(project_id, agent_id))
    elif scope != 'project':
        raise MemCoreError(f'invalid tombstone lookup scope: {scope}')
    marks = ','.join('?' for _ in scopes)
    cur = conn.execute(
        'SELECT reason FROM tombstone '
        f'WHERE claim_fingerprint = ? AND scope IN ({marks}) '
        'AND overridden_by IS NULL ORDER BY '
        "CASE scope WHEN 'global' THEN 0 ELSE 1 END, created_at DESC LIMIT 1",
        (claim_fp, *scopes)
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
    """Return metadata only when the caller is a member of the memory project.

    The membership join deliberately makes nonexistent and inaccessible memory
    ids indistinguishable to non-members, avoiding a cross-project existence
    oracle at mutation boundaries.
    """
    mem = conn.execute(
        'SELECT m.project_id, m.scope, m.owner_agent_id, m.lifecycle, pm.role '
        'FROM memory m JOIN project_membership pm '
        'ON pm.project_id=m.project_id AND pm.agent_id=? '
        'WHERE m.id=?',
        (agent_id, memory_id)
    ).fetchone()
    if not mem:
        raise PermissionDenied('memory is not accessible to this agent')
    project_id, scope, owner, lifecycle, role = mem
    if scope == 'private' and agent_id != owner and role != 'owner':
        raise PermissionDenied(
            f'agent {agent_id} cannot modify private memory owned by {owner}'
        )
    return project_id, scope, owner, lifecycle, role


# Ã¢â€â‚¬Ã¢â€â‚¬ writes Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬

def create_memory(conn, project_id, agent_id, content, scope='private',
                  memory_type='fact', lifecycle='candidate', idempotency_key=None,
                  reason=None, _manage_transaction=True):
    """Create a memory + first immutable version. Tombstone guard applies.

    Returns (memory_id, version_id) or existing ids if idempotency_key replays.
    """
    if scope not in ('project', 'private'):
        raise MemCoreError(f'invalid scope: {scope}')
    if not isinstance(content, str) or not content.strip():
        raise MemCoreError('content must be a non-empty string')
    if lifecycle not in ('candidate', 'accepted', 'conflict'):
        raise MemCoreError(
            f'invalid initial lifecycle: {lifecycle}; use an explicit transition'
        )

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
                original_fp = fingerprint(original_content)
                if original_fp != fingerprint(content):
                    raise MemCoreError('idempotency key reused with different content')
                # Policy changes after the original request still apply to a
                # replay. In particular, correction/rejection may tombstone the
                # original claim; returning a stale success would undermine the
                # refusal fingerprint without creating a new row.
                blocked = _tombstone_active(
                    conn, original_fp, project_id,
                    scope=existing_scope, agent_id=existing_owner
                )
                if blocked:
                    raise TombstoneBlocked(original_fp, blocked[0])
                if existing_lifecycle in ('disabled', 'rejected', 'superseded'):
                    raise MemCoreError(
                        f'idempotent target is terminal (lifecycle={existing_lifecycle})'
                    )
                if _manage_transaction:
                    conn.execute('ROLLBACK')
                return existing_memory, existing_version

        claim_fp = fingerprint(content)
        blocked = _tombstone_active(
            conn, claim_fp, project_id, scope=scope, agent_id=agent_id
        )
        if blocked:
            if _manage_transaction:
                conn.execute('ROLLBACK')
            raise TombstoneBlocked(claim_fp, blocked[0])

        mem_id = _new_id('mem')
        ver_id = _new_id('ver')
        now = _now()
        conn.execute(
            'INSERT INTO memory (id, project_id, scope, owner_agent_id, type, '
            '  lifecycle, verification, freshness, current_version_id, created_at, updated_at) '
            "VALUES (?, ?, ?, ?, ?, ?, 'unverified', 'current', ?, ?, ?)",
            (mem_id, project_id, scope, agent_id, memory_type,
             lifecycle, ver_id, now, now)
        )
        conn.execute(
            'INSERT INTO memory_version (id, memory_id, content, reason, '
            '  created_by_agent_id, created_at, valid_from) VALUES (?, ?, ?, ?, ?, ?, ?)',
            (ver_id, mem_id, content, reason, agent_id, now, now)
        )
        _audit(conn, 'create', agent_id, mem_id, project_id,
               {'memory_id': mem_id, 'version_id': ver_id,
                'scope': scope, 'content': content},
               write_key=idempotency_key)
        if idempotency_key:
            conn.execute(
                'INSERT INTO idempotency_key (key, project_id, memory_id, version_id, created_at) '
                'VALUES (?, ?, ?, ?, ?)',
                (idempotency_key, project_id, mem_id, ver_id, now)
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
    """Correct a memory in place while preserving immutable version history.

    The old claim receives a scope-appropriate refusal fingerprint, its
    validity interval closes, and the replacement returns to candidate /
    unverified so trust is never inherited across changed content.
    """
    if not isinstance(new_content, str) or not new_content.strip():
        raise MemCoreError('new_content must be a non-empty string')
    conn.execute('BEGIN IMMEDIATE')
    try:
        project_id, scope, owner, lifecycle, role = _require_memory_write_access(
            conn, memory_id, agent_id
        )

        if lifecycle in ('rejected', 'disabled', 'superseded'):
            raise MemCoreError(
                f'cannot correct terminal memory (lifecycle={lifecycle})'
            )

        new_fp = fingerprint(new_content)
        blocked = _tombstone_active(
            conn, new_fp, project_id, scope=scope, agent_id=owner
        )
        if blocked:
            raise TombstoneBlocked(new_fp, blocked[0])

        old_row = conn.execute(
            'SELECT m.current_version_id, v.content FROM memory m '
            'JOIN memory_version v ON v.id=m.current_version_id WHERE m.id=?',
            (memory_id,)
        ).fetchone()
        old_ver, old_content = old_row
        old_fp = fingerprint(old_content)
        if old_fp == new_fp:
            raise MemCoreError('new_content is equivalent to the current claim')
        now = _now()
        new_ver = _new_id('ver')

        # Close the old world-validity interval at the same instant the new
        # version begins. Evidence remains version-specific and is NOT copied.
        conn.execute(
            'UPDATE memory_version SET valid_until=? '
            'WHERE id=? AND valid_until IS NULL',
            (now, old_ver)
        )
        conn.execute(
            'INSERT INTO memory_version (id, memory_id, content, reason, '
            '  created_by_agent_id, supersedes_version_id, created_at, valid_from) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
            (new_ver, memory_id, new_content, reason, agent_id, old_ver, now, now)
        )

        # A changed claim does not inherit acceptance/verification from the old
        # version. It must earn trust again through feedback/evidence.
        conn.execute(
            "UPDATE memory SET current_version_id=?, lifecycle='candidate', "
            "verification='unverified', freshness='current', updated_at=? "
            'WHERE id=?',
            (new_ver, now, memory_id)
        )

        tombstone_created = False
        if old_fp != new_fp and not _tombstone_active(
            conn, old_fp, project_id, scope=scope, agent_id=owner
        ):
            refusal_scope = _tombstone_scope(project_id, scope, owner)
            conn.execute(
                'INSERT INTO tombstone (id, claim_fingerprint, scope, reason, created_at) '
                'VALUES (?, ?, ?, ?, ?)',
                (_new_id('tomb'), old_fp, refusal_scope,
                 'corrected' if not reason else f'corrected: {reason}', now)
            )
            tombstone_created = True
        _audit(conn, 'supersede', agent_id, memory_id, project_id,
               {'new_version_id': new_ver, 'old_version_id': old_ver,
                'reason': reason, 'old_claim_tombstoned': tombstone_created,
                'lifecycle_reset': 'candidate',
                'verification_reset': 'unverified'})
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
        project_id, _scope, _owner, _lifecycle, _role = _require_memory_write_access(
            conn, old_memory_id, agent_id
        )
        if new_project_id != project_id:
            raise MemCoreError(
                'supersede_memory cannot move a memory across projects'
            )
    return supersede(conn, old_memory_id, agent_id, new_content, reason)


def promote(conn, memory_id, agent_id):
    """Promote private -> project scope. Audited. Owner or project owner only."""
    conn.execute('BEGIN IMMEDIATE')
    try:
        project_id, scope, owner, lifecycle, role = _require_memory_write_access(
            conn, memory_id, agent_id
        )
        current_version_id = conn.execute(
            'SELECT current_version_id FROM memory WHERE id=?', (memory_id,)
        ).fetchone()[0]
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
        blocked = _tombstone_active(
            conn, fingerprint(content), project_id,
            scope='private', agent_id=owner
        )
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
        if lifecycle == 'disabled':
            raise MemCoreError('memory is already disabled')
        conn.execute(
            "UPDATE memory SET lifecycle='disabled', updated_at=? WHERE id=?",
            (_now(), memory_id)
        )
        _audit(conn, 'deactivate', agent_id, memory_id, project_id,
               {'reason': reason, 'previous_lifecycle': lifecycle})
        conn.execute('COMMIT')
    except Exception:
        try:
            conn.execute('ROLLBACK')
        except sqlite3.OperationalError:
            pass
        raise


def restore(conn, memory_id, agent_id):
    """Undo disable, restoring the lifecycle that was disabled when known."""
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
        blocked = _tombstone_active(
            conn, fingerprint(content), project_id,
            scope=scope, agent_id=owner
        )
        if blocked:
            raise TombstoneBlocked(fingerprint(content), blocked[0])
        target_lifecycle = 'candidate'
        event = conn.execute(
            "SELECT action, detail FROM audit_event WHERE memory_id=? "
            "AND action IN ('deactivate','disable','gc_disable') "
            "ORDER BY id DESC LIMIT 1",
            (memory_id,)
        ).fetchone()
        if event and event[0] in ('deactivate', 'disable'):
            try:
                previous = json.loads(event[1] or '{}').get('previous_lifecycle')
            except (TypeError, ValueError, json.JSONDecodeError):
                previous = None
            if previous in ('candidate', 'accepted', 'conflict'):
                target_lifecycle = previous
        conn.execute(
            'UPDATE memory SET lifecycle=?, updated_at=? WHERE id=?',
            (target_lifecycle, _now(), memory_id)
        )
        _audit(conn, 'restore', agent_id, memory_id, project_id,
               {'restored_lifecycle': target_lifecycle})
        conn.execute('COMMIT')
    except Exception:
        try:
            conn.execute('ROLLBACK')
        except sqlite3.OperationalError:
            pass
        raise


def reject(conn, memory_id, agent_id, reason, create_tombstone=True):
    """Reject a memory and always leave a refusal fingerprint."""
    if not create_tombstone:
        raise MemCoreError('rejection requires a tombstone refusal guard')
    conn.execute('BEGIN IMMEDIATE')
    try:
        project_id, scope, owner, lifecycle, role = _require_memory_write_access(
            conn, memory_id, agent_id
        )
        cur_ver = conn.execute(
            'SELECT current_version_id FROM memory WHERE id=?', (memory_id,)
        ).fetchone()[0]
        content = conn.execute(
            'SELECT content FROM memory_version WHERE id=?', (cur_ver,)
        ).fetchone()[0]
        claim_fp = fingerprint(content)

        if lifecycle == 'rejected':
            if create_tombstone and not _tombstone_active(
                conn, claim_fp, project_id, scope=scope, agent_id=owner
            ):
                refusal_scope = _tombstone_scope(project_id, scope, owner)
                conn.execute(
                    'INSERT INTO tombstone (id, claim_fingerprint, scope, reason, created_at) '
                    'VALUES (?, ?, ?, ?, ?)',
                    (_new_id('tomb'), claim_fp, refusal_scope, reason, _now())
                )
                _audit(conn, 'reject_tombstone_repair', agent_id, memory_id, project_id,
                       {'reason': reason, 'scope': refusal_scope})
                conn.execute('COMMIT')
            else:
                conn.execute('ROLLBACK')
            return False

        conn.execute(
            "UPDATE memory SET lifecycle='rejected', updated_at=? WHERE id=?",
            (_now(), memory_id)
        )
        tombstone_created = False
        if create_tombstone and not _tombstone_active(
            conn, claim_fp, project_id, scope=scope, agent_id=owner
        ):
            refusal_scope = _tombstone_scope(project_id, scope, owner)
            conn.execute(
                'INSERT INTO tombstone (id, claim_fingerprint, scope, reason, created_at) '
                'VALUES (?, ?, ?, ?, ?)',
                (_new_id('tomb'), claim_fp, refusal_scope, reason, _now())
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


def override_tombstone(conn, tombstone_id, agent_id):
    """Explicitly override one active refusal guard without resurrecting history.

    Project guards require a project owner. Private guards may be overridden by
    the private owner or a project owner. Global guards fail closed in v1
    because there is no global-admin identity model. Returns False if already
    overridden, True when this call performs the override.
    """
    conn.execute('BEGIN IMMEDIATE')
    try:
        row = conn.execute(
            'SELECT claim_fingerprint, scope, reason, overridden_by '
            'FROM tombstone WHERE id=?', (tombstone_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f'tombstone {tombstone_id} not found')
        claim_fp, refusal_scope, reason, overridden_by = row
        if overridden_by is not None:
            conn.execute('ROLLBACK')
            return False
        if refusal_scope == 'global':
            raise PermissionDenied('global tombstone override requires an admin identity')
        if refusal_scope.startswith('private:'):
            parts = refusal_scope.split(':', 2)
            if len(parts) != 3:
                raise MemCoreError(f'invalid private tombstone scope: {refusal_scope}')
            project_id, owner_agent_id = parts[1], parts[2]
            role = _require_membership(conn, project_id, agent_id)
            if agent_id != owner_agent_id and role != 'owner':
                raise PermissionDenied(
                    'only the private owner or a project owner may override this tombstone'
                )
        else:
            project_id = refusal_scope
            role = _require_membership(conn, project_id, agent_id)
            if role != 'owner':
                raise PermissionDenied('only a project owner may override a project tombstone')
        cur = conn.execute(
            'UPDATE tombstone SET overridden_by=? WHERE id=? AND overridden_by IS NULL',
            (agent_id, tombstone_id)
        )
        if cur.rowcount != 1:
            raise MemCoreError('tombstone state changed during override')
        _audit(conn, 'tombstone_override', agent_id, None, project_id, {
            'tombstone_id': tombstone_id, 'fingerprint': claim_fp,
            'scope': refusal_scope, 'reason': reason})
        conn.execute('COMMIT')
        return True
    except Exception:
        try:
            conn.execute('ROLLBACK')
        except sqlite3.OperationalError:
            pass
        raise


# Ã¢â€â‚¬Ã¢â€â‚¬ reads (scope enforced in SQL WHERE) Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬

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
        'ORDER BY m.pinned DESC, datetime(m.updated_at) DESC, m.id ASC',
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
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        raise MemCoreError('search limit must be an integer')
    if limit < 1:
        raise MemCoreError('search limit must be >= 1')
    limit = min(limit, 500)
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
            'ORDER BY m.pinned DESC, ' +
            "CASE m.lifecycle WHEN 'accepted' THEN 0 WHEN 'conflict' THEN 1 ELSE 2 END, " +
            "CASE m.verification WHEN 'user_authoritative' THEN 0 WHEN 'runtime_verified' THEN 1 WHEN 'source_backed' THEN 2 ELSE 3 END, " +
            "CASE m.freshness WHEN 'current' THEN 0 WHEN 'aging' THEN 1 ELSE 2 END, " +
            'datetime(m.updated_at) DESC, m.id ASC LIMIT ?',
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
        'ORDER BY m.pinned DESC, ' +
        "CASE m.lifecycle WHEN 'accepted' THEN 0 WHEN 'conflict' THEN 1 ELSE 2 END, " +
        "CASE m.verification WHEN 'user_authoritative' THEN 0 WHEN 'runtime_verified' THEN 1 WHEN 'source_backed' THEN 2 ELSE 3 END, " +
        "CASE m.freshness WHEN 'current' THEN 0 WHEN 'aging' THEN 1 ELSE 2 END, " +
        'rank ASC, datetime(m.updated_at) DESC, m.id ASC LIMIT ?',
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
        "  AND (m.scope='project' OR m.owner_agent_id=?) "
        'ORDER BY datetime(m.updated_at) DESC, m.id ASC',
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
        'ORDER BY datetime(v.created_at), v.rowid',
        (memory_id, agent_id, agent_id)
    )
    return cur.fetchall()


# Ã¢â€â‚¬Ã¢â€â‚¬ ops: gc / stats / import Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬

def _cutoff(conn, days):
    """Cutoff timestamp in sqlite 'YYYY-MM-DD HH:MM:SS' UTC Ã¢â‚¬â€ the format
    datetime('now') DEFAULTs actually store, so string comparison is exact."""
    return conn.execute(
        "SELECT strftime('%Y-%m-%d %H:%M:%S', 'now', ?)", (f'-{days} days',)
    ).fetchone()[0]


def gc_scan(conn, candidate_days=30, tombstone_days=90):
    """List retention candidates WITHOUT touching anything.

    a) inactive-looking candidate memories (old updated_at, no evidence,
       not pinned/critical) eligible for reversible disable
    b) explicitly overridden tombstones older than tombstone_days
    Age alone is never grounds for truth rejection or a refusal fingerprint.
    Active tombstones are durable rejection guards and are never age-purged.
    Returns (candidates, tombstones); each row starts with the id.
    """
    if candidate_days < 0 or tombstone_days < 0:
        raise MemCoreError('GC retention days must be >= 0')
    cutoff_c = _cutoff(conn, candidate_days)
    cutoff_t = _cutoff(conn, tombstone_days)
    candidates = conn.execute(
        'SELECT m.id, m.project_id, m.owner_agent_id, v.content, m.updated_at '
        'FROM memory m '
        'JOIN memory_version v ON v.id = m.current_version_id '
        "WHERE m.lifecycle = 'candidate' "
        '  AND m.pinned = 0 AND m.critical = 0 '
        '  AND datetime(m.updated_at) < datetime(?) '
        '  AND NOT EXISTS (SELECT 1 FROM evidence_link el '
        '                  WHERE el.memory_version_id = m.current_version_id) '
        'ORDER BY datetime(m.updated_at), m.id',
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
    """Run retention cleanup without turning age into a truth judgment.

    Old unevidenced, unpinned, non-critical candidates are disabled so a human
    can restore them later. Only explicitly overridden tombstones are purged.
    Returns (disabled_ids, purged_tombstone_ids).
    """
    candidates, tombstones = gc_scan(conn, candidate_days, tombstone_days)
    cutoff_c = _cutoff(conn, candidate_days)
    cutoff_t = _cutoff(conn, tombstone_days)
    disabled, purged = [], []
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
                '  AND m.pinned=0 AND m.critical=0 '
                '  AND datetime(m.updated_at) < datetime(?) '
                '  AND NOT EXISTS (SELECT 1 FROM evidence_link el '
                '                  WHERE el.memory_version_id=m.current_version_id)',
                (mem_id, cutoff_c)
            ).fetchone()
            if not row:
                conn.execute('ROLLBACK')
                continue
            project_id, content = row
            conn.execute(
                "UPDATE memory SET lifecycle='disabled', updated_at=? WHERE id=?",
                (_now(), mem_id)
            )
            _audit(conn, 'gc_disable', None, mem_id, project_id,
                   {'reason': 'retention', 'content': content})
            conn.execute('COMMIT')
            disabled.append(mem_id)
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
    return disabled, purged


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
        kind = ev.get('kind')
        if kind is not None and kind not in ('file', 'commit', 'test', 'observation',
                                             'user_input', 'external', 'source'):
            return None, 'invalid_evidence'
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


def _import_idempotency_key(project_id, claim_fp, scope='project', agent_id=None):
    if scope == 'project':
        # Preserve the Phase-1 key shape for existing project imports.
        return f'import:{project_id}:{claim_fp}'
    if scope == 'private':
        if not agent_id:
            raise MemCoreError('agent_id is required for private import idempotency')
        return f'import:{project_id}:private:{agent_id}:{claim_fp}'
    raise MemCoreError(f'invalid scope: {scope}')


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
            ikey = _import_idempotency_key(
                project_id, fp, scope=scope, agent_id=agent_id
            )
            if conn.execute('SELECT 1 FROM idempotency_key WHERE key=?', (ikey,)).fetchone():
                reason = 'already_imported'
            elif _tombstone_active(
                conn, fp, project_id, scope=scope, agent_id=agent_id
            ):
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
        ikey = _import_idempotency_key(
            project_id, fp, scope=scope, agent_id=agent_id
        )
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
            if _tombstone_active(
                conn, fp, project_id, scope=scope, agent_id=agent_id
            ):
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
                raw_kind = ev.get('kind')
                kind = 'external' if raw_kind in (None, 'source') else raw_kind
                metadata = {'original_kind': raw_kind} if raw_kind == 'source' else {}
                ev_id = _new_id('ev')
                conn.execute(
                    'INSERT INTO evidence (id, kind, source_uri, source_label, metadata, captured_at) '
                    'VALUES (?, ?, ?, ?, ?, ?)',
                    (ev_id, kind, ev.get('source_uri'), ev.get('source_label'),
                     json.dumps(metadata), _now())
                )
                conn.execute(
                    'INSERT INTO evidence_link (evidence_id, memory_version_id, relation, created_at) '
                    "VALUES (?, ?, 'supports', ?)",
                    (ev_id, ver_id, _now())
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
