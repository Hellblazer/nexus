# Post-mortem: RDR-155 — pgvector T3 Consolidation

**Closed:** 2026-06-10 (Chroma serving retired; accepted 2026-06-09). Epic `nexus-skp06` (superseded). Phase beads: `nexus-mf447` (P1), `nexus-tqeg6` (P2), `nexus-eap5l`/`nexus-sbvg0`/`nexus-h3ked` (P3), `nexus-655hc`/`nexus-1k8s1`/`nexus-2ba3x` (P4a), `nexus-unp61`/`nexus-9n4pn`/`nexus-a0i5u` (P5). `close_reason: implemented`.

## What shipped

Six phases (P1-P5 + P4a gate = the close boundary; P4b world-blocked):

- **P1 — pgvector schema substrate** (`nexus-mf447`, PR #1146): `chunks_384`/`chunks_768`/`chunks_1024` per-dim tables, PK `(tenant_id, collection, chash)`, HNSW cosine index (`m=16, ef_construction=64`), GIN `tsvector` and GIN trigram indexes, FORCE RLS by `tenant_id`. Liquibase changeset `vectors-001`. Schema-component of RDR-152 Phase 3 Seam B.

- **P2 — `VectorRepository` Chroma-to-pgvector** (`nexus-tqeg6`, PR #1147): Python `T3Database` and Java `VectorRepository` repointed from `chromadb.PersistentClient`/`CloudClient` to the HTTP service pgvector endpoints. `HttpVectorClient` drop-in parity verified. Service-mode embed stubs in `prose_indexer` and `code_indexer` (ending double Voyage spend, `nexus-fsquc`).

- **P3 — hybrid parity (the P4a gate requirement)** (`nexus-sbvg0`, `nexus-eap5l`, `nexus-h3ked`; branch `feature/nexus-sbvg0-hybrid-parity-tests`): `hybridSearch` function + `vectors-002` trgm GIN changeset, `HybridParityIntegrationTest` (8/8: engine pgvector hybrid vs legacy Chroma + SQLite FTS5, ordered-equal, stemmer delta exactly 1/3, aggregate exactly 2/3), `DualRunHarnessIntegrationTest` (6/6: recall@10 = 1.0 over 20 queries, p95 < 250ms). Gate `nexus-thp60` PASSED 2026-06-09 — authorized P4a Chroma serving retire.

- **P4a — Chroma serving retire** (`nexus-655hc` P4a.1, `nexus-1k8s1` P4a.2; PR #1149): serving paths repointed to pgvector; minimal Chroma READ client (`chroma_read.py`) kept alive for the P5 copy-not-move ETL and rollback. Gate `nexus-2ba3x` PASSED 2026-06-10. PR #1149 merged to develop; release-boundary record filed (`nexus-luxe6`, `a123d426`).

- **P5 — copy-not-move ETL** (`nexus-unp61` P5.1, `nexus-9n4pn` P5.2, `nexus-a0i5u` P5.G; branch `feature/nexus-unp61-etl-integrity-suite`, PR #1150): idempotent Chroma-to-pgvector ETL for both local PersistentClient leg and ChromaCloud REST leg, rollback guard, direct-SQL manifest-orphan validation. Gate `nexus-a0i5u` PASSED 2026-06-10.

- **Production migration run** (2026-06-10, ~10:46-15:05 PT): 115,716 chunks migrated to pgvector. Local leg: 1 non-empty conformant collection (1/1). Cloud leg: 49 conformant collections, 115,715 chunks. Three collections failed deterministically and were fixed and re-run: `docs__1-16` and `docs__1-1` (client timeout 120s too short for slow CCE batches; fixed to 600s, bead `nexus-rvfwj`), and `knowledge__dt-papers` (62 NUL-bearing chunks killed batches; fixed by service-side NUL sanitization, PR #1152). Final: EXIT=0, 0 chunks lost. Chroma sources untouched (copy-not-move; free rollback target). Cost: approximately $4-6 Voyage re-embedding.

- **P4b (Chroma deletion)**: world-blocked on `nexus-luxe6` release blocker. Beads `nexus-19svb`/`nexus-g37fr`/`nexus-8zpmf` bead-READY but held.

## What was deliberately deferred

- `nexus-19svb`/`nexus-g37fr`/`nexus-8zpmf` (P4b Chroma deletion) — world-blocked on the release-boundary prerequisite chain: (1) epic `nexus-pebfx` install story, (2) conexus RDR-001 migration orchestration, (3) conexus xr7.8.9 production-scale recall + hybrid-parity go-live, (4) two-release deprecation window. P4b deletes the `chroma_read.py` migration module itself and cannot ship in the same release users migrate with.
- `nexus-xg6em` — low-gap pinning for `chroma_read.py` coverage (filed from the P4a gate test-validator medium gap).
- `nexus-hss21` — reverse-orphan sweep (chunks with no manifest entry, identified during production run validation check).
- Production-scale recall + hybrid-parity go-live gate — owned by conexus xr7.8.9, not the engine side.
- Manifest-orphan validation ran vacuously against empty catalog tables during the production run (the RDR-153 SQLite-to-Postgres data migration had not yet run). A non-vacuous re-run is a forward obligation of RDR-156 P2 (`nexus.manifest_orphans(dim)` function).

## Lessons

- **The P3 hybrid-parity gate was the correct sequencing constraint.** Authorizing Chroma serving retire (P4a) only after the pgvector hybrid path was green on the live engine prevented a scenario where serving was retired before the replacement path met the parity bar. The dual-run harness (recall@10 = 1.0, p95 < 250ms) gave a quantified baseline, not just a "seems to work" assertion.

- **Copy-not-move was the correct ETL discipline.** Chroma sources (both local `PersistentClient` and ChromaCloud) were left untouched through the production migration. This gave a free rollback target at zero time pressure. The incremental fix-and-rerun loop for the 3 failed collections would have been unrecoverable under a destructive ETL.

- **NUL bytes in chunk text (0x00) are a real corpus hazard.** 62 chunks in `knowledge__dt-papers` contained UTF8-NUL, which Postgres rejects at the protocol level. Discovered during the production run; required a service-side sanitization fix (`nexus-rvfwj`, PR #1152) and a mid-run JAR swap. The 62 chashes are permanently recorded as `155-nul-sanitization-delta` because `sha256(stored_text) != chash` for exactly those rows by design.

- **Client timeout calibration is a real production parameter.** The default 120s timeout was too short for slow CCE embedding batches during cloud leg ETL. Fixed to 600s (upsert) and 600s (per-op). This is the same class of timeout failure the aspect-worker had; it recurs at every new high-latency boundary.

- **The first-run gauntlet exposed the install gap.** On Hal's own machine, 5 first-run failures during `nx init --service` (pgvector extension not auto-created, NX_VOYAGE_API_KEY not plumbed causing silent ONNX-384 fallback, JAR needing a distribution channel). These became epic `nexus-pebfx` (6 children) and the release-boundary hold `nexus-luxe6` — the reason develop is unreleasable since P4a.

- **The manifest-orphan check ran vacuously.** `manifest_orphan_sql(dim)` was executed against empty `catalog_documents`/`document_chunks` tables (the RDR-153 data migration had not run), so the check returned 0 orphans for structural reasons, not data-quality reasons. Integrity checks as generated-SQL-strings executed by hand do not self-document their preconditions. RDR-156 P2 promotes this to a first-class DB function callable by doctor and migration validation.

- **VectorHandler rewrite was not a drop-in.** No shared interface existed between the Chroma path and the pgvector path; tenant-first signatures diverged. `FakeEmbedder` shared-helper extraction was deferred from P3 to avoid scope creep. Noted in the P3 gate as a forward obligation on the P4a bead.

- **The trgm index existence is a guard assertion, not an assumption.** The `DualRunHarnessIntegrationTest` asserts the trgm GIN index exists before running the hybrid path. This prevents the harness from silently testing a degraded path if migration ordering is wrong.

## Drift classification

- **Missing failure mode**: NUL bytes in corpus text were not anticipated in the ETL design. The data quality hazard was discovered in production, not in the test suite.
- **Missing failure mode**: client timeout defaults calibrated for normal operations were too short for ETL batch operations. Not a new class of failure, but not proactively addressed.
- **Deferred critical constraint**: the release-boundary hold (`nexus-luxe6`) was not anticipated at RDR accept time. P4a's decision to retire the only user-accessible T3 path (ChromaDB) without a complete install story for the replacement path locked develop unreleasable.
- **Missing Day 2 operation**: manifest-orphan validation preconditions (catalog tables populated) were not checked before running the validation against empty tables.
