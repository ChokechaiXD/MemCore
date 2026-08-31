"""MemCore — CLI entrypoint: python -m memcore"""
import argparse
import json
import sys
import pathlib

from . import store, core


DEFAULT_DB = str(pathlib.Path.home() / '.memcore' / 'memory.db')


def _open(args):
    return store.open_store(getattr(args, 'db', DEFAULT_DB))


def _out(data):
    print(json.dumps(data, indent=2, ensure_ascii=False))


# ── setup subcommands ──────────────────────────────────────────────────

def cmd_init(args):
    conn = _open(args)
    conn.close()
    print(f'initialized store at {args.db}')


def cmd_project_add(args):
    conn = _open(args)
    pid = f'proj-{args.name}'
    conn.execute(
        'INSERT OR IGNORE INTO project (id, name, description) VALUES (?, ?, ?)',
        (pid, args.name, args.description)
    )
    conn.commit()
    conn.close()
    print(f'project: {pid}')


def cmd_project_list(args):
    conn = _open(args)
    rows = conn.execute('SELECT id, name, description FROM project ORDER BY name').fetchall()
    conn.close()
    _out([{'id': r[0], 'name': r[1], 'description': r[2]} for r in rows])


def cmd_agent_add(args):
    conn = _open(args)
    aid = f'agent-{args.name}'
    conn.execute(
        'INSERT OR IGNORE INTO agent (id, name, profile_key) VALUES (?, ?, ?)',
        (aid, args.name, args.name)
    )
    conn.commit()
    conn.close()
    print(f'agent: {aid}')


def cmd_member_add(args):
    conn = _open(args)
    pid = f'proj-{args.project}'
    aid = f'agent-{args.agent}'
    exists = conn.execute(
        'SELECT 1 FROM project WHERE id=?', (pid,)
    ).fetchone()
    if not exists:
        sys.exit(f'error: project {pid} does not exist')
    conn.execute(
        'INSERT OR IGNORE INTO project_membership (project_id, agent_id, role) '
        'VALUES (?, ?, ?)',
        (pid, aid, args.role)
    )
    conn.commit()
    conn.close()
    print(f'member: {aid} -> {pid} ({args.role})')


# ── memory subcommands ─────────────────────────────────────────────────

def cmd_remember(args):
    conn = _open(args)
    try:
        mem_id, ver_id = core.create_memory(
            conn,
            project_id=f'proj-{args.project}',
            agent_id=f'agent-{args.agent}',
            content=args.content,
            scope=args.scope,
            memory_type=args.type,
            idempotency_key=args.idempotency_key,
            reason=args.reason,
        )
        conn.close()
        print(f'remembered: {mem_id} (version {ver_id})')
    except core.MemCoreError as e:
        sys.exit(f'error: {e}')


def cmd_search(args):
    conn = _open(args)
    rows = core.search(
        conn,
        project_id=f'proj-{args.project}',
        agent_id=f'agent-{args.agent}',
        query=args.query,
        limit=args.limit,
    )
    conn.close()
    if not rows:
        print('(no results)')
        return
    for r in rows:
        scope_tag = 'SHARED' if r[1] == 'project' else 'PRIVATE'
        print(f'[{r[0]}] ({scope_tag}, {r[2]}) {r[5]}')
        print(f'    rank={r[7]:.3f}')


def cmd_promote(args):
    conn = _open(args)
    try:
        core.promote(conn, args.memory_id, f'agent-{args.agent}')
        conn.close()
        print(f'promoted: {args.memory_id} -> project scope')
    except core.MemCoreError as e:
        sys.exit(f'error: {e}')


def cmd_supersede(args):
    conn = _open(args)
    try:
        new_ver = core.supersede(
            conn, args.memory_id, f'agent-{args.agent}',
            args.content, reason=args.reason
        )
        conn.close()
        print(f'superseded: {args.memory_id} -> new version {new_ver}')
    except core.MemCoreError as e:
        sys.exit(f'error: {e}')


def cmd_deactivate(args):
    conn = _open(args)
    try:
        core.deactivate(conn, args.memory_id, f'agent-{args.agent}')
        conn.close()
        print(f'deactivated: {args.memory_id}')
    except core.MemCoreError as e:
        sys.exit(f'error: {e}')


def cmd_reject(args):
    conn = _open(args)
    try:
        core.reject(conn, args.memory_id, f'agent-{args.agent}', args.reason)
        conn.close()
        print(f'rejected + tombstoned: {args.memory_id}')
    except core.MemCoreError as e:
        sys.exit(f'error: {e}')


# ── operational tooling ─────────────────────────────────────────────

def cmd_gc(args):
    """GC retention sweep: dry-run by default, --apply performs actual sweep."""
    conn = _open(args)
    candidates, tombstones = core.gc_scan(
        conn,
        candidate_days=args.candidate_days,
        tombstone_days=args.tombstone_days
    )
    
    print(f'GC scan:')
    print(f'  candidates (lifecycle=candidate, no evidence, age >{args.candidate_days}d): {len(candidates)}')
    if candidates:
        for c in candidates:
            print(f'    {c[0]} (project={c[1]}, created={c[4]})')
    
    print(f'  tombstones (age >{args.tombstone_days}d): {len(tombstones)}')
    if tombstones:
        for t in tombstones:
            print(f'    {t[0]} (fingerprint={t[1][:8]}..., reason={t[3]})')
    
    if args.apply:
        tombstoned, purged = core.gc_apply(
            conn,
            candidate_days=args.candidate_days,
            tombstone_days=args.tombstone_days
        )
        print(f'  applied: {len(tombstoned)} tombstoned, {len(purged)} purged')
        if tombstoned:
            for tid in tombstoned:
                print(f'    tombstoned: {tid}')
        if purged:
            for tid in purged:
                print(f'    purged: {tid}')
    
    conn.close()


def cmd_stats(args):
    """Operational stats: lifecycle/scope counts, top agents, avg length, FTS drift."""
    conn = _open(args)
    stats = core.stats(conn)
    conn.close()
    _out(stats)


def cmd_import(args):
    """Import memories from JSON file with --agent and --project options."""
    conn = _open(args)
    try:
        with open(args.file, 'r', encoding='utf-8') as f:
            items = json.load(f)
        
        if not isinstance(items, list):
            sys.exit('error: import file must contain a JSON array of items')
        
        project_id = f'proj-{args.project}'
        agent_id = f'agent-{args.agent}'

        # Operator-run CLI: explicitly ensure the importing agent exists and
        # is a member before bulk write (ALTIMA gate #3 — explicit, auditable).
        conn.execute(
            'INSERT OR IGNORE INTO agent (id, name, profile_key) VALUES (?, ?, ?)',
            (agent_id, args.agent, args.agent)
        )
        conn.execute(
            'INSERT OR IGNORE INTO project_membership (project_id, agent_id, role) '
            "VALUES (?, ?, 'member')",
            (project_id, agent_id)
        )
        conn.commit()

        result = core.import_memories(conn, items, project_id, agent_id, scope=args.scope)
        conn.close()
        
        print('import complete:')
        print(f'  added: {result["added"]}')
        print(f'  skipped: {result["skipped"]}')
        if result['created']:
            print('  created memories:')
            for mem_id, ver_id in result['created']:
                print(f'    {mem_id} (version {ver_id})')
    except core.MemCoreError as e:
        sys.exit(f'error: {e}')
    except FileNotFoundError:
        sys.exit(f'error: file not found: {args.file}')
    except json.JSONDecodeError as e:
        sys.exit(f'error: invalid JSON: {e}')


# ── doctor ─────────────────────────────────────────────────────────────

def cmd_doctor(args):
    conn = _open(args)
    report = {}

    # 1. integrity_check
    report['integrity_check'] = conn.execute('PRAGMA integrity_check').fetchone()[0]

    # 2. WAL files present
    db_path = pathlib.Path(getattr(args, 'db', DEFAULT_DB))
    report['wal_file'] = {
        'exists': db_path.with_suffix(db_path.suffix + '-wal').exists(),
        'shm_file': db_path.with_suffix(db_path.suffix + '-shm').exists(),
    }

    # 3. Orphaned rows (integrity of references)
    report['orphaned_memory_versions'] = conn.execute(
        'SELECT COUNT(*) FROM memory_version v '
        'LEFT JOIN memory m ON m.id = v.memory_id WHERE m.id IS NULL'
    ).fetchone()[0]
    report['orphaned_audit_events'] = conn.execute(
        'SELECT COUNT(*) FROM audit_event a '
        'LEFT JOIN memory m ON m.id = a.memory_id '
        'LEFT JOIN project p ON p.id = a.project_id '
        'WHERE (a.memory_id IS NOT NULL AND m.id IS NULL) '
        '   OR (a.project_id IS NOT NULL AND p.id IS NULL)'
    ).fetchone()[0]

    # 4. Tombstone stats
    report['tombstones'] = {
        'active': conn.execute(
            'SELECT COUNT(*) FROM tombstone WHERE overridden_by IS NULL'
        ).fetchone()[0],
        'overridden': conn.execute(
            'SELECT COUNT(*) FROM tombstone WHERE overridden_by IS NOT NULL'
        ).fetchone()[0],
    }

    # 5. Membership listing
    report['memberships'] = conn.execute(
        'SELECT p.name, a.name, pm.role FROM project_membership pm '
        'JOIN project p ON p.id = pm.project_id '
        'JOIN agent a ON a.id = pm.agent_id ORDER BY p.name, a.name'
    ).fetchall()

    # 6. Migration lock check
    locks = store.check_migration_lock(conn)
    report['migration_locks'] = locks if locks else 'none'

    # 7. FTS index consistency
    fts_rows = conn.execute('SELECT COUNT(*) FROM memory_version_fts').fetchone()[0]
    ver_rows = conn.execute('SELECT COUNT(*) FROM memory_version').fetchone()[0]
    report['fts_index'] = {
        'fts_rows': fts_rows,
        'version_rows': ver_rows,
        'in_sync': fts_rows == ver_rows,
    }

    conn.close()

    print(f"integrity: {report['integrity_check']}")
    print(f"wal: present={report['wal_file']['exists']}")
    print(f"orphaned versions: {report['orphaned_memory_versions']}")
    print(f"orphaned audit: {report['orphaned_audit_events']}")
    print(f"tombstones: {report['tombstones']['active']} active, "
          f"{report['tombstones']['overridden']} overridden")
    print('memberships:')
    for name, agent, role in report['memberships']:
        print(f'  {name}: {agent} ({role})')
    print(f"fts index: in_sync={report['fts_index']['in_sync']}")
    print(f"migration locks: {report['migration_locks']}")

    if report['integrity_check'] != 'ok' or report['orphaned_memory_versions'] > 0:
        sys.exit(1)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog='memcore',
        description='MemCore — shared project memory core + CLI'
    )
    parser.add_argument('--db', default=DEFAULT_DB, help='store path (default ~/.memcore/memory.db)')
    sub = parser.add_subparsers(dest='command', required=True)

    sub.add_parser('init', help='create store').set_defaults(func=cmd_init)

    p = sub.add_parser('project', help='project management')
    psub = p.add_subparsers(dest='subcommand', required=True)
    pa = psub.add_parser('add')
    pa.add_argument('name')
    pa.add_argument('--description', default='')
    pa.set_defaults(func=cmd_project_add)
    psub.add_parser('list').set_defaults(func=cmd_project_list)

    p = sub.add_parser('agent', help='agent management')
    psub = p.add_subparsers(dest='subcommand', required=True)
    pa = psub.add_parser('add')
    pa.add_argument('name')
    pa.set_defaults(func=cmd_agent_add)

    p = sub.add_parser('member', help='membership management')
    psub = p.add_subparsers(dest='subcommand', required=True)
    pa = psub.add_parser('add')
    pa.add_argument('project')
    pa.add_argument('agent')
    pa.add_argument('--role', default='member', choices=['member', 'owner'])
    pa.set_defaults(func=cmd_member_add)

    p = sub.add_parser('remember', help='store a memory')
    p.add_argument('--project', required=True)
    p.add_argument('--agent', required=True)
    p.add_argument('content')
    p.add_argument('--scope', default='private', choices=['project', 'private'])
    p.add_argument('--type', default='fact')
    p.add_argument('--idempotency-key', default=None)
    p.add_argument('--reason', default=None)
    p.set_defaults(func=cmd_remember)

    p = sub.add_parser('search', help='FTS5 search over memories')
    p.add_argument('--project', required=True)
    p.add_argument('--agent', required=True)
    p.add_argument('query')
    p.add_argument('--limit', type=int, default=20)
    p.set_defaults(func=cmd_search)

    p = sub.add_parser('promote', help='private -> project scope')
    p.add_argument('memory_id')
    p.add_argument('--agent', required=True)
    p.set_defaults(func=cmd_promote)

    p = sub.add_parser('supersede', help='correct a memory (new version)')
    p.add_argument('memory_id')
    p.add_argument('--agent', required=True)
    p.add_argument('content')
    p.add_argument('--reason', default=None)
    p.set_defaults(func=cmd_supersede)

    p = sub.add_parser('deactivate', help='soft delete a memory')
    p.add_argument('memory_id')
    p.add_argument('--agent', required=True)
    p.set_defaults(func=cmd_deactivate)

    p = sub.add_parser('reject', help='reject a memory and create a tombstone')
    p.add_argument('memory_id')
    p.add_argument('--agent', required=True)
    p.add_argument('reason')
    p.set_defaults(func=cmd_reject)

    p = sub.add_parser('gc', help='garbage collection: retention sweep')
    p.add_argument('--candidate-days', type=int, default=30,
                   help="candidate memories older than N days with no evidence are gc'd (default 30)")
    p.add_argument('--tombstone-days', type=int, default=90,
                   help='tombstones older than N days are purged (default 90)')
    p.add_argument('--apply', action='store_true',
                   help='perform the sweep (dry-run otherwise)')
    p.set_defaults(func=cmd_gc)

    sub.add_parser('stats', help='operational statistics').set_defaults(func=cmd_stats)

    p = sub.add_parser('import', help='import memories from JSON')
    p.add_argument('--file', required=True, help='JSON file path')
    p.add_argument('--agent', required=True, help='agent name')
    p.add_argument('--project', required=True, help='project name')
    p.add_argument('--scope', default='project', choices=['project', 'private'],
                   help='scope for imported memories (default project)')
    p.set_defaults(func=cmd_import)

    sub.add_parser('doctor', help='integrity + drift checks').set_defaults(func=cmd_doctor)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == '__main__':
    main()