"""MemCore dashboard/desktop read API.

Mounted by Hermes at /api/plugins/memcore/.

Read endpoints: state counts, memories by state, search, per-memory detail
(evidence + versions + audit), project/agent listing. Write endpoints are
limited to management actions (promote / pin / disable) performed by the
human operator via the UI — audit rows record actor 'dashboard'.
"""
import json
import os
import pathlib
import re
import sys
import threading

_PLUGIN_DIR = pathlib.Path(__file__).resolve().parent
for _p in (str(_PLUGIN_DIR.parent),):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Prefer an installed engine. Development can point at a checkout with
# MEMCORE_SRC; repository-relative and legacy roots are fallback-only.
def _ensure_memcore_importable():
    try:
        import importlib.util
        if importlib.util.find_spec('memcore') is not None:
            return
    except Exception:
        pass
    candidates = []
    env_src = os.environ.get('MEMCORE_SRC')
    if env_src:
        candidates.append(pathlib.Path(env_src).expanduser())
    for parent in _PLUGIN_DIR.parents:
        if (parent / 'memcore' / 'core.py').is_file() and (parent / 'schema' / 'schema.sql').is_file():
            candidates.append(parent)
            break
    candidates.append(pathlib.Path.home() / 'Workspace' / 'memcore')
    for candidate in candidates:
        if (candidate / 'memcore' / 'core.py').is_file():
            value = str(candidate)
            if value not in sys.path:
                sys.path.insert(0, value)
            return


_ensure_memcore_importable()

try:
    from fastapi import APIRouter, HTTPException
except Exception:  # allows import outside the dashboard (tests)
    class APIRouter:  # type: ignore
        def get(self, *_a, **_k):
            return lambda fn: fn
        def post(self, *_a, **_k):
            return lambda fn: fn
    class HTTPException(Exception):  # type: ignore
        def __init__(self, status_code, detail):
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail

router = APIRouter()

# Store path resolution mirrors the agent half: env override, authoritative
# Hermes config loader, then a small standalone fallback, then the default.
_DEFAULT_DB = pathlib.Path.home() / '.memcore' / 'memory.db'


def _store_path():
    env = os.environ.get('MEMCORE_STORE_PATH')
    if env:
        return env

    # Use the same authoritative Hermes YAML loader as the agent half so the
    # dashboard cannot split onto a different DB because of quoting/nesting.
    try:
        from hermes_cli.config import load_config
        config = load_config() or {}
        plugins = config.get('plugins') or {}
        entries = plugins.get('entries') or {}
        entry = entries.get('memcore') or {}
        settings = entry.get('settings') or {}
        value = settings.get('store_path')
        if isinstance(value, str) and value.strip():
            return value.strip()
        legacy = plugins.get('memcore') or {}
        value = legacy.get('store_path') if isinstance(legacy, dict) else None
        if isinstance(value, str) and value.strip():
            return value.strip()
    except Exception:
        pass

    # Standalone/test fallback: parse only a quoted or non-space scalar.
    cfg = pathlib.Path(
        os.environ.get('HERMES_HOME', str(pathlib.Path.home() / '.hermes'))
    ) / 'config.yaml'
    try:
        text = cfg.read_text(encoding='utf-8')
        block = re.search(r'(?ms)^\s*memcore:\s*\n(?P<body>(?:\s+.*\n?)*)', text)
        if block:
            m = re.search(
                r'(?m)^\s*store_path:\s*(?:"([^"]+)"|\'([^\']+)\'|([^\s#]+))',
                block.group('body')
            )
            if m:
                return next(group for group in m.groups() if group is not None)
    except OSError:
        pass
    return str(_DEFAULT_DB)


_local = threading.local()


def _db():
    # FastAPI runs sync handlers on a thread pool — connections must be
    # thread-local (sqlite3 forbids cross-thread reuse by default). Reopen if
    # the configured store path changes while the desktop process stays alive.
    path = str(pathlib.Path(_store_path()).expanduser())
    conn = getattr(_local, 'conn', None)
    current_path = getattr(_local, 'path', None)
    if conn is not None and current_path != path:
        try:
            conn.close()
        except Exception:
            pass
        conn = None
    if conn is None:
        from memcore import store
        if not pathlib.Path(path).exists():
            return None
        conn = _local.conn = store.open_store(path)
        _local.path = path
    return conn


def _rows(sql, args=()):
    db = _db()
    if db is None:
        return []
    return db.execute(sql, args).fetchall()


def _bounded_limit(value, maximum=500):
    try:
        value = int(value)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail='limit must be an integer')
    if value < 1:
        raise HTTPException(status_code=400, detail='limit must be >= 1')
    return min(value, maximum)


def _resolve_project_id(db, project_ref):
    """Resolve exact project id/UUID or a unique project name/slug."""
    direct = db.execute(
        'SELECT id FROM project WHERE id=?', (project_ref,)
    ).fetchone()
    if direct:
        return direct[0]
    by_name = db.execute(
        'SELECT id FROM project WHERE name=? ORDER BY id', (project_ref,)
    ).fetchall()
    if len(by_name) > 1:
        raise HTTPException(
            status_code=409, detail=f'ambiguous project name: {project_ref}'
        )
    if by_name:
        return by_name[0][0]
    legacy = 'proj-' + project_ref
    row = db.execute('SELECT id FROM project WHERE id=?', (legacy,)).fetchone()
    if row:
        return row[0]
    raise HTTPException(status_code=404, detail=f'project not found: {project_ref}')


_MEMORY_SELECT = (
    'SELECT m.id, m.project_id, m.scope, m.owner_agent_id, m.type, m.lifecycle, '
    '       m.verification, m.freshness, m.pinned, v.content, v.created_at '
    'FROM memory m JOIN memory_version v ON v.id = m.current_version_id '
)


def _memory_dict(r):
    return {
        'id': r[0], 'project': r[1], 'scope': r[2], 'owner': r[3], 'type': r[4],
        'lifecycle': r[5], 'verification': r[6], 'freshness': r[7],
        'pinned': bool(r[8]), 'content': r[9], 'created_at': r[10],
    }


def _audit_json(action, actor, memory_id, detail, created_at=None):
    return {'action': action, 'actor': actor, 'memory_id': memory_id,
            'detail': detail, 'created_at': created_at}


@router.get('/state')
def state(project=None):
    db = _db()
    if db is None:
        return {'store': None}
    pid = _resolve_project_id(db, project) if project else None
    if pid:
        counts = dict(db.execute(
            'SELECT lifecycle, COUNT(*) FROM memory WHERE project_id=? '
            'GROUP BY lifecycle', (pid,)).fetchall())
    else:
        counts = dict(db.execute(
            'SELECT lifecycle, COUNT(*) FROM memory GROUP BY lifecycle').fetchall())
    members = _rows(
        'SELECT p.name, a.name, pm.role FROM project_membership pm '
        'JOIN project p ON p.id = pm.project_id '
        'JOIN agent a ON a.id = pm.agent_id '
        'WHERE (? IS NULL OR pm.project_id = ?) ORDER BY p.name, a.name',
        (pid, pid))
    return {
        'store': _store_path(),
        'counts': counts,
        'memberships': [{'project': r[0], 'agent': r[1], 'role': r[2]} for r in members],
    }


@router.get('/memories')
def memories(state='candidate', project=None, limit=100):
    limit = _bounded_limit(limit)
    db = _db()
    if db is None:
        return {'state': state, 'items': []}
    pid = _resolve_project_id(db, project) if project else None
    lifecycle_by_state = {
        'candidate': ('candidate',), 'accepted': ('accepted',),
        'conflict': ('conflict',), 'stale': ('candidate', 'accepted', 'conflict'),
        'rejected': ('rejected',),
    }
    if state == 'tombstones':
        if pid:
            private_prefix = f'private:{pid}:'
            rows = db.execute(
                'SELECT id, claim_fingerprint, scope, reason, created_at FROM tombstone '
                "WHERE scope IN (?, 'global') OR substr(scope,1,length(?))=? "
                'ORDER BY datetime(created_at) DESC, id DESC LIMIT ?',
                (pid, private_prefix, private_prefix, limit)).fetchall()
        else:
            rows = _rows(
                'SELECT id, claim_fingerprint, scope, reason, created_at FROM tombstone '
                'ORDER BY datetime(created_at) DESC, id DESC LIMIT ?', (limit,))
        return {'state': state, 'items': [
            {'id': r[0], 'fingerprint': r[1], 'scope': r[2],
             'reason': r[3], 'created_at': r[4]} for r in rows]}
    lifecycles = lifecycle_by_state.get(state, ('candidate',))
    marks = ','.join('?' * len(lifecycles))
    args = list(lifecycles)
    where = 'm.lifecycle IN (%s)' % marks
    if state == 'stale':
        where += " AND m.freshness = 'stale'"
    if pid:
        where += ' AND m.project_id = ?'
        args.append(pid)
    args.append(limit)
    rows = _rows(_MEMORY_SELECT + 'WHERE ' + where +
                 ' ORDER BY m.pinned DESC, datetime(v.created_at) DESC, m.id ASC LIMIT ?', args)
    return {'state': state, 'items': [_memory_dict(r) for r in rows]}


@router.get('/search')
def search(q='', project=None, limit=25):
    limit = _bounded_limit(limit)
    if not q.strip():
        return {'query': q, 'items': []}
    from memcore import core
    db = _db()
    if db is None:
        return {'query': q, 'items': []}
    pid = _resolve_project_id(db, project) if project else None
    raw_query = str(q).strip()
    # unicode61 does not segment Thai/CJK natural-language substrings well.
    # Mirror core.search(): exact Unicode substring first, then normal FTS.
    if any(ord(ch) > 127 for ch in raw_query):
        sql = _MEMORY_SELECT + 'WHERE instr(v.content, ?) > 0 '
        args = [raw_query]
        if pid:
            sql += 'AND m.project_id = ? '
            args.append(pid)
        sql += 'ORDER BY m.pinned DESC, datetime(v.created_at) DESC, m.id ASC LIMIT ?'
        args.append(limit)
        rows = db.execute(sql, args).fetchall()
        if rows:
            return {'query': q, 'items': [_memory_dict(r) for r in rows]}
    match_expr = core._fts_query(raw_query)
    if not match_expr:
        return {'query': q, 'items': []}
    sql = (_MEMORY_SELECT +
           'JOIN memory_version_fts fts ON fts.rowid = v.rowid '
           'WHERE memory_version_fts MATCH ? ')
    args = [match_expr]
    if pid:
        sql += 'AND m.project_id = ? '
        args.append(pid)
    # ponytail: operator-facing dashboard — no scope filtering by design;
    # human sees everything, unlike agent tools which filter by scope.
    sql += 'ORDER BY m.pinned DESC, rank, m.id ASC LIMIT ?'
    args.append(limit)
    rows = db.execute(sql, args).fetchall()
    return {'query': q, 'items': [_memory_dict(r) for r in rows]}


@router.get('/memory/{memory_id}')
def memory_detail(memory_id):
    rows = _rows(_MEMORY_SELECT + 'WHERE m.id = ?', (memory_id,))
    if not rows:
        return {'error': 'not found', 'id': memory_id}
    versions = _rows(
        'SELECT id, content, created_at, supersedes_version_id '
        'FROM memory_version WHERE memory_id = ? '
        'ORDER BY datetime(created_at), id', (memory_id,))
    audit = _rows(
        'SELECT id, action, actor_agent_id, detail, created_at FROM audit_event '
        'WHERE memory_id = ? ORDER BY datetime(created_at), id', (memory_id,))
    evidence = _rows(
        'SELECT DISTINCT e.id, e.kind, e.source_uri, e.source_label FROM evidence e '
        'JOIN evidence_link el ON el.evidence_id = e.id '
        'JOIN memory_version mv ON mv.id = el.memory_version_id '
        'WHERE mv.memory_id = ? ORDER BY e.id', (memory_id,))
    return {
        'memory': _memory_dict(rows[0]),
        'versions': [{'id': v[0], 'content': v[1], 'created_at': v[2],
                      'supersedes': v[3]} for v in versions],
        'audit': [_audit_json(a[1], a[2], memory_id, a[3], a[4]) for a in audit],
        'evidence': [{'id': e[0], 'kind': e[1], 'uri': e[2], 'note': e[3]}
                     for e in evidence],
    }


@router.get('/projects')
def projects():
    projs = _rows('SELECT id, name, description FROM project ORDER BY name')
    agents = _rows('SELECT id, name FROM agent ORDER BY name')
    return {
        'projects': [{'id': p[0], 'name': p[1], 'description': p[2]} for p in projs],
        'agents': [{'id': a[0], 'name': a[1]} for a in agents],
    }


# -- Management actions (human operator via UI; audited as 'dashboard') ------

def _audit(action, memory_id, detail):
    db = _db()
    if db is None:
        return
    # actor_agent_id is an FK to agent(id) since migration 0004 — the human
    # operator is not an agent row, so record NULL and keep the operator
    # provenance in detail.source instead.
    row = db.execute(
        'SELECT project_id FROM memory WHERE id=?', (memory_id,)
    ).fetchone()
    project_id = row[0] if row else None
    from memcore import core
    db.execute(
        'INSERT INTO audit_event '
        '(action, actor_agent_id, memory_id, project_id, detail, created_at) '
        'VALUES (?, NULL, ?, ?, ?, ?)',
        (action, memory_id, project_id, json.dumps(detail), core._now()))


@router.post('/promote')
def promote(body: dict):
    db = _db()
    if db is None:
        return {'error': 'store unavailable'}
    memory_id = (body or {}).get('memory_id')
    if not memory_id:
        return {'error': 'memory_id required'}
    # core.promote() only changes scope; dashboard adds lifecycle='accepted'
    # (operator accept) — documented divergence from agent-side promote.
    db.execute('BEGIN IMMEDIATE')
    try:
        row = db.execute(
            'SELECT m.project_id, m.scope, m.owner_agent_id, m.lifecycle, v.content '
            'FROM memory m JOIN memory_version v ON v.id=m.current_version_id '
            'WHERE m.id=?', (memory_id,)
        ).fetchone()
        if row is None:
            db.execute('ROLLBACK')
            return {'error': 'not found', 'memory_id': memory_id}
        project_id, scope, owner_agent_id, lifecycle, content = row
        if lifecycle != 'candidate':
            db.execute('ROLLBACK')
            return {'error': 'invalid state', 'memory_id': memory_id,
                    'lifecycle': lifecycle, 'required': 'candidate'}
        from memcore import core
        blocked = core._tombstone_active(
            db, core.fingerprint(content), project_id,
            scope=scope, agent_id=owner_agent_id
        )
        if blocked:
            db.execute('ROLLBACK')
            return {'error': 'tombstone blocked', 'memory_id': memory_id,
                    'reason': blocked[0]}
        cur = db.execute(
            "UPDATE memory SET scope='project', lifecycle='accepted', "
            "updated_at=? WHERE id=? AND lifecycle='candidate'",
            (core._now(), memory_id))
        if cur.rowcount == 0:
            raise RuntimeError('memory state changed during promote transaction')
        _audit('promote', memory_id, {'source': 'dashboard'})
        db.execute('COMMIT')
        return {'success': True, 'memory_id': memory_id}
    except Exception:
        try:
            db.execute('ROLLBACK')
        except Exception:
            pass
        raise


@router.post('/pin')
def pin(body: dict):
    db = _db()
    if db is None:
        return {'error': 'store unavailable'}
    memory_id = (body or {}).get('memory_id')
    pinned = 1 if (body or {}).get('pinned', True) else 0
    if not memory_id:
        return {'error': 'memory_id required'}
    db.execute('BEGIN IMMEDIATE')
    try:
        from memcore import core
        cur = db.execute(
            "UPDATE memory SET pinned=?, updated_at=? WHERE id=?",
            (pinned, core._now(), memory_id))
        if cur.rowcount == 0:
            db.execute('ROLLBACK')
            return {'error': 'not found', 'memory_id': memory_id}
        _audit('pin' if pinned else 'unpin', memory_id, {'source': 'dashboard'})
        db.execute('COMMIT')
        return {'success': True, 'memory_id': memory_id, 'pinned': bool(pinned)}
    except Exception:
        try:
            db.execute('ROLLBACK')
        except Exception:
            pass
        raise


@router.post('/disable')
def disable(body: dict):
    db = _db()
    if db is None:
        return {'error': 'store unavailable'}
    memory_id = (body or {}).get('memory_id')
    if not memory_id:
        return {'error': 'memory_id required'}
    db.execute('BEGIN IMMEDIATE')
    try:
        from memcore import core
        row = db.execute(
            'SELECT lifecycle FROM memory WHERE id=?', (memory_id,)
        ).fetchone()
        if row is None:
            db.execute('ROLLBACK')
            return {'error': 'not found', 'memory_id': memory_id}
        previous_lifecycle = row[0]
        if previous_lifecycle not in ('candidate', 'accepted', 'conflict'):
            db.execute('ROLLBACK')
            return {'error': 'invalid state', 'memory_id': memory_id,
                    'lifecycle': previous_lifecycle,
                    'allowed': ['candidate', 'accepted', 'conflict']}
        db.execute(
            "UPDATE memory SET lifecycle='disabled', updated_at=? WHERE id=?",
            (core._now(), memory_id))
        _audit('disable', memory_id,
               {'source': 'dashboard', 'previous_lifecycle': previous_lifecycle})
        db.execute('COMMIT')
        return {'success': True, 'memory_id': memory_id}
    except Exception:
        try:
            db.execute('ROLLBACK')
        except Exception:
            pass
        raise
