# MemCore

Local-first shared project memory for Hermes profiles. MemCore uses one SQLite
store with WAL + FTS5, immutable memory versions, tombstones, audit events,
project membership, and explicit private/project lanes. It is daemonless and
uses the Python standard library for the core engine.

## Current status

- Phase 1 core correctness: implemented and covered by the integration harness.
- Hermes integration: native `MemCoreMemoryProvider` is active as `memory.provider: memcore`.
- Automatic Hermes lifecycle writes enter an append-only ingest journal first; raw journal rows are never recalled directly.
- Canonical recall preserves scope/lifecycle/verification/freshness trust labels across profiles.
- Schema contract remains frozen; revisions are applied through migrations. Current schema revision: `0009_semantic_analysis`.

## Repository layout

```text
memcore/
├── memcore/core.py          # memory operations, search, GC, import, stats
├── memcore/ingest.py        # append-only Hermes ingest journal + deterministic gate
├── memcore/store.py         # SQLite/WAL setup + migrations
├── memcore/__main__.py      # CLI + doctor
├── schema/schema.sql        # frozen initial contract
├── fixtures/fixtures.py     # deterministic evaluation fixtures
├── harness/                 # unit + integration evaluation suite
└── scripts/                 # scratch setup + search benchmark
```

## Run the test suite
```powershell
cd C:\Users\BlankScreen\Workspace\memcore
python -m unittest discover -v
```

Current full core gate: 164 tests pass, with the E12 token-budget pair remaining expected failures.
The E12 token-budget pair remains an expected failure in the core harness
because prompt-size enforcement lives in the Hermes plugin's recall builder.
The plugin has its own budget regression tests.

## Operational CLI

```powershell
python -m memcore doctor
python -m memcore stats
python -m memcore gc
python -m memcore gc --apply
python -m memcore restore <memory_id> --agent mika
python -m memcore tombstone override <tombstone_id> --agent pchoke
python -m memcore import --file batch.json --agent mika --project shared-platform --dry-run
python -m memcore import --file batch.json --agent mika --project shared-platform
```

`gc` is dry-run by default. Age-based GC disables old unevidenced candidates reversibly; it never age-rejects or creates tombstones. Active tombstones persist until explicit override; only old overridden tombstones are purgeable.

`import --dry-run` validates the batch, reports
within-batch duplicates / prior imports / tombstone blocks, and performs zero
domain writes (no agent, membership, memory, or audit rows). Imported memories
enter as candidates. Real import is idempotent per project + content fingerprint,
and each memory plus all evidence links commits atomically as one item.

## Safety invariants

- Project membership is required at read and write boundaries.
- Private memory is writable only by its owner or a project owner.
- Plugin-bound mutations cannot target another project by memory ID.
- Rejected/corrected claims create scope-aware tombstones that block silent resurrection; explicit override is owner-audited and does not resurrect the old rejected row.
- Deactivate/restore is reversible: accepted/conflict/candidate lifecycle is preserved across manual disable/restore.
- Search ranking is deterministic and exposes lifecycle, verification, and freshness rather than treating candidate memory as verified fact.
- Search returns only the current memory version; history remains queryable.
