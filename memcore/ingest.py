"""Raw Hermes event journal and conservative admission bridge for MemCore.

The journal is append-only ingress. Raw turns are never recalled directly.
Only the analyzer may derive canonical MemCore memories, and automatic
derivations are private candidates by default.
"""
import hashlib
import json
import re

from . import core

EVENT_TYPES = {'turn', 'memory_write', 'delegation', 'session_end', 'manual'}
MAX_FIELD_CHARS = 65536

_TRIVIAL_RE = re.compile(
    r'^(?:ok|okay|yes|no|thanks|thank you|hi|hey|hello|continue|next|done|'
    r'โอเค|ครับ|ค่ะ|ขอบคุณ|ต่อ|ต่อเลย|ได้|ดี)[\s!?.…]*$', re.IGNORECASE
)
_EXPLICIT_MEMORY_RE = re.compile(
    r'(?:^|\b)(?:please\s+)?remember(?:\s+that)?\b|'
    r'\bfrom now on\b|\bi (?:prefer|always use|use)\b|\bwe (?:decided|agreed|chose)\b|'
    r'(?:^|\s)(?:จำไว้|ช่วยจำ|จำว่า|ต่อไป(?:นี้)?|ฉันชอบ|ผมชอบ|ฉันใช้|ผมใช้|'
    r'เราตกลง|เราตัดสินใจ)(?:\s|ว่า|:|,|$)', re.IGNORECASE
)
_LEADING_REMEMBER_RE = re.compile(
    r'^\s*(?:(?:please\s+)?remember(?:\s+that)?|จำไว้(?:ว่า)?|ช่วยจำ(?:ไว้)?(?:ว่า)?|จำว่า)'
    r'[\s,:-]*', re.IGNORECASE
)

def _clean_text(value):
    if value is None:
        return ''
    if not isinstance(value, str):
        raise core.MemCoreError('journal content must be a string')
    return value[:MAX_FIELD_CHARS]


def _payload_hash(event_type, user_content, assistant_content, metadata):
    payload = json.dumps({
        'event_type': event_type,
        'user_content': user_content,
        'assistant_content': assistant_content,
        'metadata': metadata or {},
    }, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def append_event(conn, project_id, agent_id, event_type, *, session_id='',
                 user_content='', assistant_content='', metadata=None):
    """Durably append one raw ingress event; retry-safe within its session."""
    if event_type not in EVENT_TYPES:
        raise core.MemCoreError(f'invalid ingest event type: {event_type}')
    user_content = _clean_text(user_content)
    assistant_content = _clean_text(assistant_content)
    if not user_content and not assistant_content:
        raise core.MemCoreError('journal event must contain user or assistant content')
    metadata = metadata if isinstance(metadata, dict) else {}
    session_id = str(session_id or '')[:512]
    content_hash = _payload_hash(event_type, user_content, assistant_content, metadata)
    event_id = 'evt-' + hashlib.sha256(
        f'{project_id}\0{agent_id}\0{session_id}\0{content_hash}'.encode('utf-8')
    ).hexdigest()[:24]
    conn.execute('BEGIN IMMEDIATE')
    try:
        core._require_membership(conn, project_id, agent_id)
        now = core._now()
        cur = conn.execute(
            'INSERT OR IGNORE INTO ingest_event '
            '(id, project_id, agent_id, session_id, event_type, user_content, '
            ' assistant_content, metadata, content_hash, status, created_at) '
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
            (event_id, project_id, agent_id, session_id, event_type,
             user_content, assistant_content,
             json.dumps(metadata, ensure_ascii=False, sort_keys=True),
             content_hash, now)
        )
        created = cur.rowcount == 1
        conn.execute('COMMIT')
        return event_id, created
    except Exception:
        try:
            conn.execute('ROLLBACK')
        except Exception:
            pass
        raise


def classify_user_text(text):
    """Deterministic first-stage gate. It never trusts assistant output."""
    text = (text or '').strip()
    if not text or text.startswith('/') or '<memory-context>' in text.lower():
        return 'ignore', 'non_user_signal', ''
    if _TRIVIAL_RE.fullmatch(text):
        return 'ignore', 'trivial', ''
    if _EXPLICIT_MEMORY_RE.search(text):
        candidate = _LEADING_REMEMBER_RE.sub('', text).strip() or text
        return 'candidate', 'explicit_durable_signal', candidate[:4000]
    return 'review', 'semantic_review_required', ''

def _find_private_claim(conn, project_id, agent_id, claim_fp):
    rows = conn.execute(
        'SELECT m.id, v.content FROM memory m '
        'JOIN memory_version v ON v.id=m.current_version_id '
        "WHERE m.project_id=? AND m.scope='private' AND m.owner_agent_id=? "
        "AND m.lifecycle NOT IN ('rejected','disabled','superseded')",
        (project_id, agent_id)
    ).fetchall()
    for memory_id, content in rows:
        if core.fingerprint(content) == claim_fp:
            return memory_id
    return None


def process_event(conn, event_id):
    """Analyze one pending event after it is durable in the journal.

    Explicit durable user signals become private candidate memories. Ambiguous
    events stay pending for a future semantic analyzer or operator review.
    """
    conn.execute('BEGIN IMMEDIATE')
    try:
        row = conn.execute(
            'SELECT project_id, agent_id, event_type, user_content, metadata, status '
            'FROM ingest_event WHERE id=?', (event_id,)
        ).fetchone()
        if row is None:
            raise core.MemCoreError(f'ingest event not found: {event_id}')
        project_id, agent_id, event_type, user_content, metadata_raw, status = row
        try:
            metadata = json.loads(metadata_raw or '{}')
        except (TypeError, ValueError, json.JSONDecodeError):
            metadata = {}
        core._require_membership(conn, project_id, agent_id)
        if status != 'pending':
            conn.execute('ROLLBACK')
            return {'event_id': event_id, 'status': status}

        if event_type == 'memory_write' and user_content.strip():
            action = str(metadata.get('action') or 'add').strip().lower()
            if action == 'add':
                decision, reason, candidate = (
                    'candidate', 'explicit_memory_write_add', user_content.strip()[:4000]
                )
            else:
                decision, reason, candidate = (
                    'review', f'builtin_memory_{action or "unknown"}_requires_review', ''
                )
        else:
            decision, reason, candidate = classify_user_text(user_content)
        now = core._now()
        if decision == 'ignore':
            conn.execute(
                "UPDATE ingest_event SET status='ignored', decision=?, processed_at=? WHERE id=?",
                (reason, now, event_id)
            )
            conn.execute('COMMIT')
            return {'event_id': event_id, 'status': 'ignored', 'decision': reason}
        if decision == 'review':
            conn.execute(
                'UPDATE ingest_event SET decision=? WHERE id=?', (reason, event_id)
            )
            conn.execute('COMMIT')
            return {'event_id': event_id, 'status': 'pending', 'decision': reason}
        claim_fp = core.fingerprint(candidate)
        existing = _find_private_claim(conn, project_id, agent_id, claim_fp)
        if existing:
            conn.execute(
                "INSERT OR IGNORE INTO ingest_derivation "
                "(event_id, memory_id, relation, created_at) VALUES (?, ?, 'duplicate', ?)",
                (event_id, existing, now)
            )
            conn.execute(
                "UPDATE ingest_event SET status='processed', decision='duplicate', processed_at=? "
                'WHERE id=?', (now, event_id)
            )
            conn.execute('COMMIT')
            return {'event_id': event_id, 'status': 'processed',
                    'decision': 'duplicate', 'memory_id': existing}

        memory_id, _version_id = core.create_memory(
            conn, project_id, agent_id, candidate,
            scope='private', memory_type='observation', lifecycle='candidate',
            idempotency_key=f'ingest:{event_id}',
            reason='native provider journal analysis', _manage_transaction=False
        )
        conn.execute(
            "INSERT INTO ingest_derivation "
            "(event_id, memory_id, relation, created_at) VALUES (?, ?, 'created', ?)",
            (event_id, memory_id, now)
        )
        conn.execute(
            "UPDATE ingest_event SET status='processed', decision='private_candidate', processed_at=? "
            'WHERE id=?', (now, event_id)
        )
        conn.execute('COMMIT')
        return {'event_id': event_id, 'status': 'processed',
                'decision': 'private_candidate', 'memory_id': memory_id}
    except Exception as exc:
        try:
            conn.execute('ROLLBACK')
        except Exception:
            pass
        try:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute(
                "UPDATE ingest_event SET status='failed', error=?, processed_at=? WHERE id=?",
                (f'{type(exc).__name__}: {exc}'[:2000], core._now(), event_id)
            )
            conn.execute('COMMIT')
        except Exception:
            try:
                conn.execute('ROLLBACK')
            except Exception:
                pass
        raise


def journal_stats(conn, project_id=None):
    where = ''
    args = []
    if project_id is not None:
        where = ' WHERE project_id=?'
        args.append(project_id)
    return dict(conn.execute(
        'SELECT status, COUNT(*) FROM ingest_event' + where + ' GROUP BY status', args
    ).fetchall())
