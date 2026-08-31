"""MemCore — CLI entrypoint: python -m memcore"""
import argparse
import json
import os
import sys
import pathlib

from . import store, core


DEFAULT_DB = str(pathlib.Path.home() / '.memcore' / 'memory.db')


def _open(args):
    return store.open_store(getattr(args, 'db', DEFAULT_DB))


def _open_readonly(args):
    """Open an existing current-schema store without creating or migrating it."""
    try:
        return store.open_store_readonly(getattr(args, 'db', DEFAULT_DB))
    except store.StoreError as e:
        sys.exit(f'error: {e}')


def _out(data):
    print(json.dumps(data, indent=2, ensure_ascii=False))


def _is_network_path(path):
    raw = str(path)
    if raw.startswith('\\\\') or raw.startswith('//'):
        return True
    if os.name == 'nt':
        try:
            import ctypes
            anchor = pathlib.Path(path).anchor
            if anchor:
                return ctypes.windll.kernel32.GetDriveTypeW(anchor) == 4
        except Exception:
            pass
    return False


def _discover_hermes_memcore_bindings():
    """Best-effort read of enabled MemCore bindings from Hermes YAML."""
    try:
        import yaml
    except Exception as exc:
        return {'available': False, 'error': f'PyYAML unavailable: {exc}', 'bindings': []}

    roots = []
    env_home = os.environ.get('HERMES_HOME')
    if env_home:
        roots.append(pathlib.Path(env_home).expanduser())
    local = os.environ.get('LOCALAPPDATA')
    if local:
        roots.append(pathlib.Path(local) / 'hermes')
    roots.append(pathlib.Path.home() / '.hermes')
    root = next((r for r in roots if (r / 'config.yaml').is_file()), None)
    if root is None:
        return {'available': False, 'error': 'Hermes config.yaml not found', 'bindings': []}

    files = [('default', root / 'config.yaml')]
    profiles = root / 'profiles'
    if profiles.is_dir():
        files.extend(
            (p.name, p / 'config.yaml') for p in profiles.iterdir()
            if p.is_dir() and (p / 'config.yaml').is_file()
        )

    bindings = []
    errors = []
    for profile, cfg_path in files:
        try:
            data = yaml.safe_load(cfg_path.read_text(encoding='utf-8')) or {}
            plugins = data.get('plugins') or {}
            if 'memcore' not in (plugins.get('enabled') or []):
                continue
            entry = (plugins.get('entries') or {}).get('memcore') or {}
            settings = entry.get('settings') or {}
            agent = settings.get('agent_name') or (profile if profile != 'default' else None)
            projects = []
            default_project = settings.get('default_project')
            if isinstance(default_project, str) and default_project.strip():
                projects.append(default_project.strip())
            for binding in settings.get('path_bindings') or []:
                if isinstance(binding, dict):
                    project = binding.get('project')
                    if isinstance(project, str) and project.strip():
                        projects.append(project.strip())
            projects = list(dict.fromkeys(projects))
            store_path = settings.get('store_path') or DEFAULT_DB
            bindings.append({
                'profile': profile,
                'config': str(cfg_path),
                'agent': agent,
                'projects': projects,
                'store_path': str(store_path),
            })
        except Exception as exc:
            errors.append(f'{cfg_path}: {type(exc).__name__}: {exc}')
    return {'available': True, 'root': str(root), 'bindings': bindings, 'errors': errors}


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
    conn = _open_readonly(args)
    try:
        rows = conn.execute('SELECT id, name, description FROM project ORDER BY name').fetchall()
    finally:
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
    try:
        if not conn.execute('SELECT 1 FROM project WHERE id=?', (pid,)).fetchone():
            sys.exit(f'error: project {pid} does not exist')
        if not conn.execute('SELECT 1 FROM agent WHERE id=?', (aid,)).fetchone():
            sys.exit(f'error: agent {aid} does not exist; create it first')
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute(
            'SELECT role FROM project_membership WHERE project_id=? AND agent_id=?',
            (pid, aid)
        ).fetchone()
        if row is not None:
            current_role = row[0]
            if current_role != args.role:
                conn.execute('ROLLBACK')
                sys.exit(
                    f'error: membership already exists with role {current_role}; '
                    f'requested role {args.role} was not applied'
                )
            conn.execute('ROLLBACK')
            print(f'member: {aid} -> {pid} ({current_role})')
            return
        conn.execute(
            'INSERT INTO project_membership (project_id, agent_id, role) '
            'VALUES (?, ?, ?)',
            (pid, aid, args.role)
        )
        core._audit(
            conn, 'agent_joined', None, None, pid,
            {'source': 'memcore member CLI', 'agent_id': aid, 'role': args.role}
        )
        conn.execute('COMMIT')
        print(f'member: {aid} -> {pid} ({args.role})')
    except Exception:
        try:
            conn.execute('ROLLBACK')
        except Exception:
            pass
        raise
    finally:
        conn.close()


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
        print(f'remembered: {mem_id} (version {ver_id})')
    except core.MemCoreError as e:
        sys.exit(f'error: {e}')
    finally:
        conn.close()


def cmd_search(args):
    conn = _open_readonly(args)
    try:
        rows = core.search(
            conn,
            project_id=f'proj-{args.project}',
            agent_id=f'agent-{args.agent}',
            query=args.query,
            limit=args.limit,
        )
    finally:
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
        print(f'promoted: {args.memory_id} -> project scope')
    except core.MemCoreError as e:
        sys.exit(f'error: {e}')
    finally:
        conn.close()


def cmd_supersede(args):
    conn = _open(args)
    try:
        new_ver = core.supersede(
            conn, args.memory_id, f'agent-{args.agent}',
            args.content, reason=args.reason
        )
        print(f'superseded: {args.memory_id} -> new version {new_ver}')
    except core.MemCoreError as e:
        sys.exit(f'error: {e}')
    finally:
        conn.close()


def cmd_deactivate(args):
    conn = _open(args)
    try:
        core.deactivate(conn, args.memory_id, f'agent-{args.agent}')
        print(f'deactivated: {args.memory_id}')
    except core.MemCoreError as e:
        sys.exit(f'error: {e}')
    finally:
        conn.close()


def cmd_reject(args):
    conn = _open(args)
    try:
        core.reject(conn, args.memory_id, f'agent-{args.agent}', args.reason)
        print(f'rejected + tombstoned: {args.memory_id}')
    except core.MemCoreError as e:
        sys.exit(f'error: {e}')
    finally:
        conn.close()


# ── operational tooling ─────────────────────────────────────────────

def cmd_gc(args):
    """GC retention sweep: dry-run by default, --apply performs actual sweep."""
    conn = None
    try:
        if args.candidate_days < 0 or args.tombstone_days < 0:
            sys.exit('error: GC retention days must be >= 0')
        conn = (_open(args) if args.apply else
                store.open_store_readonly(getattr(args, 'db', DEFAULT_DB)))
        candidates, tombstones = core.gc_scan(
            conn,
            candidate_days=args.candidate_days,
            tombstone_days=args.tombstone_days
        )

        print('GC scan:')
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
    except (core.MemCoreError, store.StoreError) as e:
        sys.exit(f'error: {e}')
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def cmd_stats(args):
    """Operational stats: lifecycle/scope counts, top agents, avg length, FTS drift."""
    conn = _open_readonly(args)
    try:
        stats = core.stats(conn)
    finally:
        conn.close()
    _out(stats)


def cmd_import(args):
    """Import memories from JSON file with --agent and --project options."""
    conn = None
    try:
        dry_run = getattr(args, 'dry_run', False)
        conn = (store.open_store_readonly(getattr(args, 'db', DEFAULT_DB))
                if dry_run else _open(args))
        with open(args.file, 'r', encoding='utf-8') as f:
            items = json.load(f)
        
        if not isinstance(items, list):
            sys.exit('error: import file must contain a JSON array of items')
        
        project_id = f'proj-{args.project}'
        agent_id = f'agent-{args.agent}'

        # Operator-run CLI: project identity is never invented during import.
        # The agent may be created, but membership is explicit and audited.
        if not conn.execute(
            'SELECT 1 FROM project WHERE id=?', (project_id,)
        ).fetchone():
            conn.close()
            sys.exit(f'error: project {project_id} does not exist; create it first')

        if dry_run:
            plan = core.plan_import(
                conn, items, project_id, scope=args.scope, agent_id=agent_id
            )
            agent_exists = bool(conn.execute(
                'SELECT 1 FROM agent WHERE id=?', (agent_id,)
            ).fetchone())
            member_exists = bool(conn.execute(
                'SELECT 1 FROM project_membership WHERE project_id=? AND agent_id=?',
                (project_id, agent_id)
            ).fetchone())
            conn.close()
            print('import dry-run:')
            print(f'  total: {plan["total"]}')
            print(f'  would add: {plan["would_add"]}')
            print(f'  skipped: {plan["skipped"]}')
            for reason, count in sorted(plan['reasons'].items()):
                print(f'    {reason}: {count}')
            print(f'  agent: {"existing" if agent_exists else "would create"}')
            print(f'  membership: {"existing" if member_exists else "would join"}')
            print('  writes performed: 0')
            return

        conn.execute('BEGIN IMMEDIATE')
        try:
            conn.execute(
                'INSERT OR IGNORE INTO agent (id, name, profile_key) VALUES (?, ?, ?)',
                (agent_id, args.agent, args.agent)
            )
            joined = conn.execute(
                'INSERT OR IGNORE INTO project_membership (project_id, agent_id, role) '
                "VALUES (?, ?, 'member')",
                (project_id, agent_id)
            )
            if joined.rowcount:
                core._audit(conn, 'agent_joined', agent_id, None, project_id,
                            {'source': 'memcore import CLI'})
            conn.execute('COMMIT')
        except Exception:
            try:
                conn.execute('ROLLBACK')
            except Exception:
                pass
            raise

        result = core.import_memories(conn, items, project_id, agent_id, scope=args.scope)
        conn.close()
        
        print('import complete:')
        print(f'  added: {result["added"]}')
        print(f'  skipped: {result["skipped"]}')
        if result['created']:
            print('  created memories:')
            for mem_id, ver_id in result['created']:
                print(f'    {mem_id} (version {ver_id})')
    except (core.MemCoreError, store.StoreError) as e:
        sys.exit(f'error: {e}')
    except FileNotFoundError:
        sys.exit(f'error: file not found: {args.file}')
    except json.JSONDecodeError as e:
        sys.exit(f'error: invalid JSON: {e}')
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


# ── doctor ─────────────────────────────────────────────────────────────

def cmd_doctor(args):
    conn = None
    try:
        conn = store.open_store_readonly(getattr(args, 'db', DEFAULT_DB))
    except store.StoreError as e:
        sys.exit(f'error: {e}')
    report = {}

    # 1. SQLite structural + foreign-key integrity.
    report['integrity_check'] = conn.execute('PRAGMA integrity_check').fetchone()[0]
    report['foreign_key_violations'] = len(
        conn.execute('PRAGMA foreign_key_check').fetchall()
    )

    # 2. Journal mode is the health contract. -wal/-shm files are only
    # transient diagnostics and may legitimately disappear when the DB is idle.
    db_path = pathlib.Path(getattr(args, 'db', DEFAULT_DB)).expanduser()
    report['journal_mode'] = conn.execute('PRAGMA journal_mode').fetchone()[0].lower()
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
    # idempotency_key intentionally predates FK constraints, so foreign_key_check
    # cannot detect drift here. Validate both references and cross-row identity.
    report['idempotency_violations'] = conn.execute(
        'SELECT COUNT(*) FROM idempotency_key ik '
        'LEFT JOIN project p ON p.id=ik.project_id '
        'LEFT JOIN memory m ON m.id=ik.memory_id '
        'LEFT JOIN memory_version v ON v.id=ik.version_id '
        'WHERE ik.project_id IS NULL OR p.id IS NULL '
        '   OR m.id IS NULL OR v.id IS NULL '
        '   OR (v.id IS NOT NULL AND v.memory_id != ik.memory_id) '
        '   OR (m.id IS NOT NULL AND ik.project_id IS NOT NULL '
        '       AND m.project_id != ik.project_id)'
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

    # 5. Membership listing + identity collision check.
    report['memberships'] = conn.execute(
        'SELECT p.name, a.name, pm.role FROM project_membership pm '
        'JOIN project p ON p.id = pm.project_id '
        'JOIN agent a ON a.id = pm.agent_id ORDER BY p.name, a.name'
    ).fetchall()
    report['agent_name_collisions'] = conn.execute(
        'SELECT p.name, a.name, COUNT(*) FROM project_membership pm '
        'JOIN project p ON p.id=pm.project_id '
        'JOIN agent a ON a.id=pm.agent_id '
        'GROUP BY pm.project_id, a.name HAVING COUNT(*) > 1 '
        'ORDER BY p.name, a.name'
    ).fetchall()

    # 6. Enabled Hermes profile bindings must resolve into this exact store.
    config_check = _discover_hermes_memcore_bindings()
    report['config_check'] = config_check
    report['binding_drift'] = []
    current_db = db_path.resolve(strict=False)
    if config_check.get('available'):
        for binding in config_check.get('bindings', []):
            reasons = []
            configured_db = pathlib.Path(binding['store_path']).expanduser().resolve(strict=False)
            if configured_db != current_db:
                reasons.append(f'store_mismatch:{configured_db}')
            agent = binding.get('agent')
            projects = binding.get('projects') or []
            if not agent:
                reasons.append('missing_agent_identity')
            if not projects:
                reasons.append('missing_project_binding')
            for project in projects:
                pid = f'proj-{project}'
                if not conn.execute('SELECT 1 FROM project WHERE id=?', (pid,)).fetchone():
                    reasons.append(f'missing_project:{project}')
                    continue
                if agent:
                    aid = f'agent-{agent}'
                    if not conn.execute(
                        'SELECT 1 FROM project_membership '
                        'WHERE project_id=? AND agent_id=?', (pid, aid)
                    ).fetchone():
                        reasons.append(f'missing_membership:{agent}->{project}')
            if reasons:
                report['binding_drift'].append({
                    'profile': binding['profile'],
                    'agent': agent,
                    'projects': projects,
                    'reasons': reasons,
                })

    report['network_path'] = _is_network_path(db_path)
    report['store_parent_writable'] = os.access(db_path.parent, os.W_OK)

    # 7. Migration lock check
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
    print(f"foreign-key violations: {report['foreign_key_violations']}")
    print(f"journal mode: {report['journal_mode']}")
    print(f"wal file: present={report['wal_file']['exists']}")
    print(f"orphaned versions: {report['orphaned_memory_versions']}")
    print(f"orphaned audit: {report['orphaned_audit_events']}")
    print(f"idempotency violations: {report['idempotency_violations']}")
    print(f"tombstones: {report['tombstones']['active']} active, "
          f"{report['tombstones']['overridden']} overridden")
    print('memberships:')
    for name, agent, role in report['memberships']:
        print(f'  {name}: {agent} ({role})')
    print(f"agent name collisions: {report['agent_name_collisions'] or 'none'}")
    if report['config_check'].get('available'):
        print('config bindings:')
        drift_by_profile = {
            item['profile']: item for item in report['binding_drift']
        }
        for binding in report['config_check'].get('bindings', []):
            drift = drift_by_profile.get(binding['profile'])
            if drift:
                print(f"  {binding['profile']}: DRIFT - {', '.join(drift['reasons'])}")
            else:
                print(f"  {binding['profile']}: OK")
        for err in report['config_check'].get('errors', []):
            print(f'  config error: {err}')
    else:
        print(f"config bindings: unavailable ({report['config_check'].get('error')})")
    print(f"network path: {report['network_path']}")
    print(f"store parent writable: {report['store_parent_writable']}")
    print(f"fts index: in_sync={report['fts_index']['in_sync']}")
    print(f"migration locks: {report['migration_locks']}")

    unhealthy = (
        report['integrity_check'] != 'ok'
        or report['foreign_key_violations'] > 0
        or report['journal_mode'] != 'wal'
        or report['orphaned_memory_versions'] > 0
        or report['orphaned_audit_events'] > 0
        or report['idempotency_violations'] > 0
        or report['agent_name_collisions']
        or report['binding_drift']
        or report['config_check'].get('errors')
        or not report['store_parent_writable']
        or not report['fts_index']['in_sync']
        or report['migration_locks'] != 'none'
    )
    if unhealthy:
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
    p.add_argument('--dry-run', action='store_true',
                   help='preview validation/dedup results without writing anything')
    p.set_defaults(func=cmd_import)

    sub.add_parser('doctor', help='integrity + drift checks').set_defaults(func=cmd_doctor)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == '__main__':
    main()