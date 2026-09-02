#!/usr/bin/env python3
"""Benchmark bulk-import preview against an already populated store."""
import os
import statistics
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memcore import core, store


def main():
    with tempfile.TemporaryDirectory(prefix='memcore_importbench_') as root:
        db = os.path.join(root, 'memory.db')
        conn = store.open_store(db)
        conn.execute("INSERT INTO project (id,name) VALUES ('p','bench')")
        conn.execute("INSERT INTO agent (id,name,profile_key) VALUES ('a','bench','bench')")
        conn.execute(
            "INSERT INTO project_membership (project_id,agent_id,role) "
            "VALUES ('p','a','owner')"
        )
        for index in range(2000):
            core.create_memory(conn, 'p', 'a', f'existing claim {index}', scope='project')
        items = [
            {'summary': f'new import claim {index}'} for index in range(80)
        ] + [
            {'summary': f'existing claim {index * 17}'} for index in range(20)
        ]
        core.plan_import(conn, items[:5], 'p')
        samples = []
        for _ in range(5):
            started = time.perf_counter()
            plan = core.plan_import(conn, items, 'p')
            samples.append((time.perf_counter() - started) * 1000.0)
        samples.sort()
        print('Import preview benchmark (2000 existing, 100 input items)')
        print(f'  p50: {statistics.median(samples):.2f} ms')
        print(f'  min: {samples[0]:.2f} ms')
        print(f'  max: {samples[-1]:.2f} ms')
        print(f"  would_add: {plan['would_add']}  skipped: {plan['skipped']}")
        conn.close()


if __name__ == '__main__':
    main()
