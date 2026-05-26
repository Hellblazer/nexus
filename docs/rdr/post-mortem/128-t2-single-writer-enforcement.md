# Post-mortem: RDR-128 T2 Single-Writer Enforcement

**Closed:** 2026-05-25 (implemented)
**Epic:** nexus-sbxbe (P0 nexus-sbxbe.1, P1 keystone nexus-kg8sj, P2 nexus-sbxbe.2, P3 nexus-sbxbe.3)

## What it fixed

RDR-120 declared the T2 daemon the single writer of `memory.db` but never
enforced it. 20+ `epsilon-allow` raw connects and 30+ direct `T2Database(...)`
constructions opened the WAL database outside the daemon and contended on its
one writer lock. The contention surfaced as `database is locked` incidents
band-aided across 5.0.2 / 5.0.3 / 5.0.4, culminating in a startup-migration
crash-loop minutes after 5.0.4 shipped (the 5.0.4 lifecycle "fix" amplified it).

## How it was closed (per Problem-Statement gap)

- **Gap 1 (invariant unenforced):** the hot/automated writers now route through
  the daemon via `mcp_infra.t2_index_write` (indexer, aspect_worker poll,
  session-end flush, collection rename, scratch promote, doctor metric, enrich
  delete, collection delete). `src/nexus/mcp_infra.py:158`.
- **Gap 2 (contention hardening inconsistent):** `bootstrap_schema` got
  `busy_timeout=30000` + bounded lock-retry (P0). `src/nexus/db/t2/__init__.py:393`.
- **Gap 3 (lifecycle no interlock):** `ensure-running` probes DB-acquirability
  with a 30s bound and ABORTS the version-cycle on timeout rather than tearing
  down a working daemon (P0). `src/nexus/commands/daemon.py:811`.
- **Gap 4 (boundary lint partial):** the lint was extended to flag direct
  `T2Database(...)` construction and **flipped from counted-only to enforcing**
  in P3, so an un-annotated construction outside `db/`+`daemon/` is a hard CI
  failure. `src/nexus/storage_boundary_lint.py:384`.

## Acceptance

- **Quantitative (met):** both lint populations at the documented-irreducible
  set: 0 un-annotated violations / 30 documented constructions (each carrying an
  `epsilon-allow` lock-discipline justification) / 16 raw-connect exceptions.
  `nx doctor --check-storage-boundary` reports both.
- **Qualitative (evidenced, pending live confirmation):** the deterministic
  stress gate `TestRdr128P3SingleWriterRouting` drives 60 mixed routed writers
  (indexer + worker + session-end) against one daemon with **zero**
  `database is locked` and no lost writes. Final confirmation expected over the
  next live release cycle (5.1.x): many post-commit indexer fires + an upgrade +
  a daemon restart with no new daemon band-aid bead.

## Key design decisions

1. **Hard-fail lint, not counted-only.** A new direct writer can no longer merge
   silently; it either routes or carries a written `epsilon-allow` justification.
2. **The daemon RPC wire protocol decodes dataclasses to plain dicts**
   (`t2_daemon._t2_decode`), not reconstructed objects. Consequences:
   `document_aspects.upsert(AspectRecord)` cannot route (stays a documented-
   irreducible direct write); the aspect_worker reconstructs `QueueRow(**dict)`
   after a routed `claim_batch`.
3. **Taxonomy core-discovery restructure was deliberately NOT done.** The shared
   `_T2Database` wrapper (`commands/taxonomy_cmd.py`) is permanent: 6+ read-only
   subcommands issue raw `taxonomy.conn` SELECTs that cannot cross the daemon
   RPC. Restructuring `discover_topics`/`rebuild_taxonomy`/`split_topic` to route
   their writes would yield zero lint reduction (the wrapper survives for the
   readers) at high correctness risk, so the wrapper was annotated instead.

## Irreducible-by-design survivors (documented)

The daemon-unreachable fallback in `t2_index_write`; the `aspect_worker` persist
(`document_aspects.upsert`); bootstrap `nx upgrade`; raw-DDL/raw-cursor writers
(`enrich` promote, `aspects` gc-fixtures); read-only diagnostics/CLI reads; the
taxonomy CLI factory. Each carries an `epsilon-allow` reason at its site.

## Follow-ups filed (not blocking)

- **nexus-g25dk** (P2 bug): `index.py` auto-discover projection pass silently
  broken (`_persist_assignments` missing `source_collection`, swallowed by an
  `except`). Pre-existing; found while annotating.
- **nexus-izpcb** (P3): `nx upgrade` keeps the `apply_pending` migration conn and
  the T3-steps `T2Database` open in-process simultaneously. Pre-existing, benign
  (single-threaded sequential writes), tracked for consolidation.
- **nexus-fkq5q** (P1): "route indexer taxonomy discover writes (compute/persist
  split)" — now SUPERSEDED by the P3 decision to annotate the auto-discover path
  as documented-irreducible (ChromaDB client cannot cross the RPC). Disposition
  pending.

## Lesson reinforced

Three consecutive patch releases to one subsystem was the signal to root-cause
rather than ship patch N+1. The root cause was an asserted-but-unenforced
invariant; the cure was enforcement (the lint flip) plus routing, not another
contention band-aid.
