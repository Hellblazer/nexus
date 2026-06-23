# Post-mortem: RDR-152 — Postgres + Java Storage Service

**Closed:** 2026-06-12 (T2 default flip; accepted 2026-06-06). Epic `nexus-gmiaf`. `close_reason: implemented`.

## What shipped

Five phases, each driven TDD + stacked reviews (code-review-expert + substantive-critic) + phase-review-gate, covering approximately 140 commits:

- **Phase 0 — critical-assumption spikes** (`nexus-gmiaf.1`, S0.1-S0.4): four spikes locked before any implementation. S0.1: RLS-via-GUC isolation verified on PostgreSQL 16.13 — cross-tenant write rejected by `WITH CHECK`, zero-rows-without-GUC safe default, `set_config('nexus.tenant', ?, true)` is the bind-safe txn-local form, `FORCE ROW LEVEL SECURITY` gates the owner. S0.2: JVM embedding equivalence (Voyage REST + ONNX-in-JVM produce vectors equivalent to the Python path). S0.3: ChromaDB HTTP API parity from JVM confirmed. S0.4: GraalVM native-image vs jlink — verdict: native-image.

- **Phase 1 — service spine + T2 memory MVV** (gate `nexus-gmiaf.10`): FTS5-to-tsvector parity contract locked (`nexus-gmiaf.2`), memory store Postgres migration, service HTTP spine with Liquibase changesets, jOOQ type-safe queries from the live schema.

- **Phase 2 — relational store ladder** (`nexus-gmiaf.8` through `nexus-gmiaf.18`): memory ETL (idempotent, fidelity-preserving), plans store migration (critic fixes: fidelity + metadata column), chash index, catalog store via jOOQ `CatalogRepository`, aspects SQL fast-path port to Java (`nexus-l9hd8`), SQLite-to-Postgres catalog ETL (`nexus-bdaxz`).

- **Phase 3 — Seam B vector path** (schema component via RDR-155): chunking stayed in Python; embed+quota+Chroma-write moved to JVM. Inherited the embedding-equivalence parity gate from S0.2.

- **Phase 4 — decommission** (partial): the sentinel bead `nexus-gmiaf.24` (SQLite daemon retire) remained deferred at close, blocked by the open taxonomy parity gap (bead `nexus-1di3r`). `HttpTaxonomyStore` full parity shipped later as a follow-on arc.

- **Phase 5 — operational** (`nexus-gmiaf.30` through `.37`): storage-service supervisor on the RDR-149 lifecycle substrate (`.30`), nx-managed Postgres provisioner with two-role net63 model (`.31`), nx doctor service/migration/RLS checks (`.33`), token lifecycle arc (`.32` Phases A-F: bridge-token credential schema, AuthFilter server-side resolution, per-session mint/close, admin CLI, derived-token retire, adversarial security matrix). RLS + wildcard-sentinel rejection hardened (`nexus-45ykb`).

- **T2 default flip** (`nexus-fjwxh`, 2026-06-12, merge `766b8e29`): `storage_backend_for` default flipped SQLITE to SERVICE; T2/memory now serves from Postgres by default. Taxonomy full parity follow-on (`nexus-1di3r`, 9 sub-beads) merged after the flip.

## What was deliberately deferred

- `nexus-gmiaf.24` — SQLite daemon retire. Gated on `nexus-1di3r` full taxonomy parity; annotated as documented-irreducible-by-design (taxonomy CLI subcommands require shared SQLite reads that cannot cross the daemon RPC).
- `nexus-9n485` — T2 tombstone-vs-absent probe (soft delete analog of RDR-156). Filed as follow-on.
- T1 scratch `HttpScratchStore` service flip — forward-declared incomplete; `HttpScratchStore` requires a minted session the CLI does not provision.
- Windows distribution and aarch64/mac-arm64 binary packaging — deferred to epic `nexus-pebfx`/RDR-157.

## Lessons

- **The circuit-breaker trigger was correct.** Five consecutive patch releases (5.0.2 through 5.10.5) to the single-writer daemon subsystem was the documented signal to root-cause rather than patch again. The root cause was structural: SQLite has exactly one writer, so every concurrency property nexus needed (concurrent writers, fair scheduling, no spawn election, no version-skew double-writer) had to be *simulated* by a hand-rolled daemon. That simulation was the bug factory.

- **Substrate-only scope (the RDR-120 law) was the constraint that made a 140-commit arc land.** RDRs 110-119 died because storage shipped intertwined with new consumer abstractions (tuplespace, ORB, cockpit). RDR-152 held the substrate-only discipline across all five phases: every commit was plumbing only, no co-shipped consumers.

- **Seam B was the correct cut point.** Moving only embed+quota+Chroma-write to JVM reduced the dominant risk from full-pipeline parity to a single verifiable property (embedding equivalence, S0.2). Seam A (moving chunking too) was rejected because it would have made the scope unverifiable.

- **The T2 default flip surfaced a systemic parity class the individual store migrations had all missed.** `JsonInclude.NON_NULL` vs `JsonInclude.ALWAYS` was a Java serialization default that silently dropped null fields from JSON responses; the Python caller KeyError'd on absent columns. SQLite returns all columns always; Postgres + JSON serialization must be explicit. Fixed with `NON_NULL->ALWAYS` across 11 Java handlers and a standing parity tripwire (`tests/db/test_http_t2_store_parity.py`, 8 stores locked).

- **Taxonomy CLI writes cannot cross the daemon RPC** because 6+ read-only subcommands issue raw `taxonomy.conn` SELECTs that need a shared connection. Restructuring `discover_topics`/`rebuild_taxonomy`/`split_topic` to route their writes would yield zero lint reduction at high correctness risk. The correct answer was annotation (documented-irreducible), not routing. The `nexus-1di3r` follow-on arc then closed the read+write gap methodically via 9 sub-beads.

- **Token lifecycle (`.32` Phases A-F) was the largest single-bead arc in Phase 5.** Six sequential sub-phases because each security boundary (credential schema, server-side resolution, per-session mint, admin surface, derived-token retire, adversarial matrix) was a real security surface requiring its own stacked-review cycle. The adversarial matrix (Phase F) caught a wildcard-sentinel escape that Phases A-E had not covered.

- **First-run failures (`nexus-jdpn9`) were evidence the install story was not ready.** On the production machine, `nx init --service` hit 5 first-run failures: pgvector extension not auto-created, NX_VOYAGE_API_KEY not plumbed causing silent ONNX-384 fallback, the JAR needing a distribution channel. These became epic `nexus-pebfx` (6 children) and the release blocker `nexus-luxe6`.

## Drift classification

- **Missing failure mode**: `JsonInclude.NON_NULL` null-field KeyError is a failure class the individual store migrations could not detect, because they tested happy-path non-null data only.
- **Scope underestimation**: taxonomy CLI surface required a 9-bead follow-on (`nexus-1di3r`) because the taxonomy subcommand's mixed read/write connection pattern was not audited before the default flip. The P1 gate cross-walk passed but the taxonomy gap surfaced only at the flip boundary.
- **Missing Day 2 operation**: install story (JAR distribution, pgvector extension auto-creation, Voyage key plumbing) was underspecified until the production first-run. Became epic `nexus-pebfx` and release blocker `nexus-luxe6`.
