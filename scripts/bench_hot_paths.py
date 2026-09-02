#!/usr/bin/env python3
"""Benchmark MemCore runtime hot paths without network/model dependencies.

Measures indexed private-claim lookup against the pre-0010 linear scan shape and
runtime SQLite opening against the full bootstrap/migration-checked opener.
Results are descriptive, not pass/fail thresholds.
"""
from __future__ import annotations

import os
import statistics
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memcore import core, ingest, store


def timed(fn, iterations):
    samples = []
    for _ in range(iterations):
        start = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - start) * 1000.0)
    samples.sort()
    p95 = samples[min(len(samples) - 1, int(len(samples) * 0.95))]
    return statistics.median(samples), p95


def main():
    with tempfile.TemporaryDirectory(prefix='memcore_hotbench_') as root:
        db = os.path.join(root, 'memory.db')
        conn = store.open_store(db)
        conn.execute("INSERT INTO project (id,name) VALUES ('p','bench')")
        conn.execute("INSERT INTO agent (id,name,profile_key) VALUES ('a','bench','bench')")
        conn.execute(
            "INSERT INTO project_membership (project_id,agent_id,role) "
            "VALUES ('p','a','owner')"
        )
        for index in range(2000):
            core.create_memory(
                conn, 'p', 'a', f'private benchmark claim {index}', scope='private'
            )
        target = 'private benchmark claim 1900'
        target_fp = core.fingerprint(target)

        def indexed_lookup():
            ingest._find_private_claim(conn, 'p', 'a', target_fp)

        def legacy_linear_lookup():
            rows = conn.execute(
                'SELECT m.id,v.content FROM memory m '
                'JOIN memory_version v ON v.id=m.current_version_id '
                "WHERE m.project_id='p' AND m.scope='private' AND m.owner_agent_id='a' "
                "AND m.lifecycle NOT IN ('rejected','disabled','superseded')"
            ).fetchall()
            for memory_id, content in rows:
                if core.fingerprint(content) == target_fp:
                    return memory_id
            return None

        for _ in range(10):
            indexed_lookup()
            legacy_linear_lookup()

        indexed_p50, indexed_p95 = timed(indexed_lookup, 300)
        legacy_p50, legacy_p95 = timed(legacy_linear_lookup, 60)

        conn.close()

        def runtime_open():
            handle = store.open_runtime_store_readonly(db)
            handle.close()

        def full_open():
            handle = store.open_store_readonly(db)
            handle.close()

        runtime_p50, runtime_p95 = timed(runtime_open, 300)
        full_p50, full_p95 = timed(full_open, 100)

        print('MemCore hot-path benchmark')
        print('  private claim lookup (2000 memories)')
        print(f'    indexed p50={indexed_p50:.4f} ms p95={indexed_p95:.4f} ms')
        print(f'    legacy  p50={legacy_p50:.4f} ms p95={legacy_p95:.4f} ms')
        if indexed_p50:
            print(f'    p50 speedup={legacy_p50 / indexed_p50:.1f}x')
        print('  read-only store open')
        print(f'    runtime p50={runtime_p50:.4f} ms p95={runtime_p95:.4f} ms')
        print(f'    checked p50={full_p50:.4f} ms p95={full_p95:.4f} ms')
        if runtime_p50:
            print(f'    p50 speedup={full_p50 / runtime_p50:.1f}x')


if __name__ == '__main__':
    main()
