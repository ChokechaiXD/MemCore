# MemCore — Phase 0 Evaluation Harness

Phase 0: fixtures + automated negative-test suite validating the 12 integration
evaluations from the research audit. The core engine does not exist yet.

## What's here

```
memcore/
├── schema/schema.sql        ← minimal SQLite schema (Phase 1 contract)
├── fixtures/fixtures.py     ← 2 agents, 2 projects, evidence, tombstone, conflicts
├── harness/
│   └── test_evaluations.py  ← 21 tests: 12 evaluations + schema/concurrency checks (2 xfail)
│   └── __main__.py          ← python -m harness entrypoint
└── README.md
```

## How to run

```bash
cd C:/Users/BlankScreen/Workspace/memcore
python -m unittest discover -v
```

Expected output: pass/xfail/skip summary. Two xfail tests (the E12 token-budget pair)
await the real core; E1–E8 pass against the query-stub spec (fail loudly if the spec
drifts); E9/E10/E11 run for real against SQLite WAL.

## What the 12 evaluations test

| # | Name | Status | What it proves when passing |
|---|------|--------|-----------------------------|
| E1 | Shared decision recall | pass (spec) | A writes, B in same project recalls it |
| E2 | Private isolation | pass (spec) | B never retrieves A's private memory |
| E3 | Project isolation | pass (spec) | Project B can't see Project A memory |
| E4 | Irrelevant recall | pass (spec) | Unrelated project memory not injected |
| E5 | Correction/supersede | pass (spec) | New version supersedes old; history survives |
| E6 | Staleness on source change | pass (spec) | Code-backed memory goes stale on commit change |
| E7 | Tombstone resurrection | pass (spec) | Rejected claim can't silently re-add |
| E8 | Conflict abstention | pass (spec) | Conflicting claims expose, not auto-resolve |
| E9 | Concurrency | PASS | Two writers → no corrupt/duplicate rows |
| E10 | Crash recovery | PASS | Kill mid-write → DB usable, audit coherent |
| E11 | Daemonless | PASS | No persistent process after all clients close |
| E12 | Token budget | xfail | Recall block stays under defined ceiling |

## How to flip tests as the core lands

Phase 1 (DONE): the query stubs are now thin adapters over `memcore.core`
(`visible_memories`, `private_memories`, `admission_allowed`, `conflict_memories`,
`supersede`). Assertions are unchanged. E12 stays `@expectedFailure` until the
Phase 2 `pre_llm_call` budget enforcement lands; cross-project discovery
(`_query_cross_project`) is still raw SQL pending Phase 7's reusable flag.

## Design notes

- Python 3.11, stdlib only (unittest + sqlite3). No pytest, no pip deps.
- All tests use isolated in-memory DBs except concurrency/crash tests which use
  real temp files with WAL mode.
- Query stubs in `test_evaluations.py` are the SPEC, not the engine — they encode
  what the core must do. Phase 1 replaces them with real core queries.
