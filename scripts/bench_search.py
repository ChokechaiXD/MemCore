#!/usr/bin/env python3
"""Search latency benchmark: time 1000 search calls on a scratch DB.

Reports p50 / p95 latency in milliseconds.
Usage: python scripts/bench_search.py
"""
import os
import sys
import tempfile
import time
import random

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memcore import store, core


def main():
    # Create a scratch directory for the temp DB
    tmpdir = tempfile.mkdtemp(prefix='memcore_bench_')
    db_path = os.path.join(tmpdir, 'bench.db')
    
    # Open store and set up test data
    conn = store.open_store(db_path)
    
    # Create project and agent
    conn.execute("INSERT INTO project (id, name) VALUES ('proj-bench', 'bench')")
    conn.execute("INSERT INTO agent (id, name, profile_key) VALUES ('agent-bench', 'bench', 'bench')")
    conn.execute(
        "INSERT INTO project_membership (project_id, agent_id, role) VALUES ('proj-bench', 'agent-bench', 'owner')"
    )
    conn.commit()
    
    # Seed some memories with varied content
    sample_words = [
        'deployment', 'container', 'kubernetes', 'docker', 'server',
        'database', 'postgres', 'mysql', 'sqlite', 'api',
        'gateway', 'router', 'auth', 'token', 'session',
        'cache', 'redis', 'memory', 'storage', 'pipeline',
        'queue', 'worker', 'async', 'sync', 'parallel',
        'microservice', 'latency', 'throughput', 'scaling', 'load'
    ]
    
    memories = []
    for i in range(500):
        words = random.sample(sample_words, k=random.randint(3, 8))
        content = ' '.join(words)
        mem_id, ver_id = core.create_memory(
            conn, 'proj-bench', 'agent-bench', content, scope='project'
        )
        memories.append(mem_id)
    
    conn.commit()
    
    # Warm up: 10 searches
    for _ in range(10):
        core.search(conn, 'proj-bench', 'agent-bench', random.choice(sample_words))
    
    # Benchmark: 1000 searches
    times = []
    for _ in range(1000):
        query = random.choice(sample_words) + ' ' + random.choice(sample_words)
        start = time.perf_counter()
        core.search(conn, 'proj-bench', 'agent-bench', query)
        elapsed = (time.perf_counter() - start) * 1000  # ms
        times.append(elapsed)
    
    times.sort()
    p50 = times[50]  # median of 1000
    p95 = times[949]  # 95th percentile
    
    conn.close()
    
    # Cleanup
    for suffix in ('', '-wal', '-shm'):
        try:
            os.unlink(db_path + suffix)
        except OSError:
            pass
    os.rmdir(tmpdir)
    
    print(f'Search latency benchmark (1000 calls):')
    print(f'  p50: {p50:.2f} ms')
    print(f'  p95: {p95:.2f} ms')
    print(f'  min: {times[0]:.2f} ms')
    print(f'  max: {times[-1]:.2f} ms')


if __name__ == '__main__':
    main()