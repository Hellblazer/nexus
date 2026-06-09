---
title: "Replace ChromaDB with pgvector for T3: Consolidate Permanent Vectors into the RDR-152 Postgres (Engine Side of conexus RDR-001)"
id: RDR-155
type: Architecture
status: draft
priority: high
author: Hal Hildebrand
created: 2026-06-09
related_rdrs: [RDR-152, RDR-108, RDR-105, RDR-101, conexus:RDR-001]
related_issues: [nexus-skp06]
related_tests: []
---

# RDR-155: pgvector T3 — Retire ChromaDB, Consolidate Vectors into the RDR-152 Postgres

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate the RDR.

> Cross-repo references prefixed `conexus:` point at RDRs in the conexus (product)
> repository. This RDR is the **engine** side of an architecture already accepted and
> validated on the **product** side (`conexus:RDR-001`).

## Problem Statement

RDR-152 moved T2 (and T1's server mode) onto a Postgres substrate owned by one strict
Java storage service. T3 — permanent vectors — is the last tier still on ChromaDB
(local `PersistentClient` or `CloudClient`). Keeping T3 on Chroma forces a split store:
two engines, two tenancy models, a cross-store doc-to-chunk lookup (the RDR-108
manifest cannot be a real FK), the ChromaDB external-quota constraint class, and no
native hybrid search.

It also leaves a **live multi-tenant hole**: `VectorHandler` does not scope Chroma ops
by `RequestContext.tenant()` — isolation rests only on the collection-name convention,
so an authenticated client could read/write another tenant's collections if the name is
known (bead `nexus-skp06`). Postgres RLS does not cover Chroma.

The product repo (`conexus`) has already decided, validated, and built around the
resolution: **T3 lives in pgvector in the same Postgres as T2.** conexus RDR-001 is
accepted; Phases 1–7 shipped; its deploy stack runs against a throwaway stub schema
that is explicitly *"replaced by the engine's real Liquibase changesets when nexus
RDR-152 ships"* (tracked `conexus-xr7.3.11`). This RDR delivers those engine changesets.

## Context

- **Engine vs product split (conexus:RDR-001).** `nexus` is the engine (the `nx` CLI,
  retrieval/knowledge core, the `dev.nexus:nexus-service` storage server). `conexus` is
  the product that operates it multi-tenant. The product *owns* hosting (infra, tenant
  lifecycle, edge auth, metering) but **the engine owns schema and RLS policy**. So the
  pgvector T3 schema, its RLS, and the Chroma retirement are this repo's responsibility.
- **Locality decision is fixed** (T2 memory `152-cloud-locality-scope`): T1 stays
  local/per-process; T2 and T3 move to cloud Postgres; T3 is consolidated into pgvector
  in the **same** instance as T2 — not kept on Chroma.
- **The embedding pipeline does not change.** The engine already owns embedding
  generation (Voyage direct + retry + embed-only-prefix; bundled ONNX MiniLM local).
  The Java service already embeds server-side in `VectorRepository.upsertChunks`. This
  is a **storage + ANN swap** (Chroma → pgvector), not a pipeline rewrite.
- **RLS posture already exists** (RDR-152): FORCE RLS, plain-LOGIN non-owner data role,
  a tenant-scope wrapper that stamps the tenant GUC inside the transaction. The chunks
  table joins that same model.
- **skp06 is subsumed.** Under pgvector with FORCE RLS by `tenant_id`, cross-tenant
  vector isolation is the same GUC boundary already enforced for memory/scratch/catalog.
  No app-layer Chroma guard is needed (or wanted — it would be deleted by this work).

## Research Findings

Carried from conexus RDR-001 (accepted 2026-06-08) and its spikes:

- **pgvector scale fit.** HNSW is appropriate well past the current corpus
  (~45k chunks / ~95 collections / 1024-dim Voyage vectors); 1024 dims is under the
  2000-dim indexed-`vector` limit. pgvector 0.8 adds iterative scan for filtered queries.
- **Filtered-recall spike PASSED at slice scale** (`conexus/spikes/pgvector-recall/`,
  5085-row `voyage-context-3` slice, verbatim-identical vectors, HNSW `m=16,
  ef_construction=64, ef_search=100`, `hnsw.iterative_scan='relaxed_order'`): pgvector
  recall@10 = 1.0 on broad/medium/narrow (≥ Chroma), p95 0.4–1.5 ms vs Chroma 4–6 ms,
  and *more* robust under tight `ef_search` (holds 1.0 at ef_search=10 where Chroma
  degrades to 0.956–0.999). Machine-checked 7/7.
- **Un-retired production-scale residual.** The slice's narrow filtered set (64 rows)
  was smaller than `ef_search`, so two of three conditions were effectively exact.
  Whether `iterative_scan` *holds recall* when the filtered set **exceeds** `ef_search`
  at production scale — RLS-narrowed selectivity, per-request multi-collection fan-out,
  `voyage-code-3` distribution — is NOT answered by the slice. This is a go-live gate
  (conexus `xr7.8.9`), and it needs the engine schema + a test-invocable seam to the
  live engine path, which this RDR provides.
- **Validated config to adopt** (conexus stub schema contract + RDR-001 §Multitenancy):
  `CREATE EXTENSION vector` (≥ 0.8) + `pg_trgm`; FORCE RLS by `tenant_id`; HNSW
  `m=16, ef_construction=64`; `iterative_scan='relaxed_order'`; plain-LOGIN
  non-owner non-BYPASSRLS data role; `SET LOCAL` tenant inside the txn.

## Proposed Solution

Ship the engine's pgvector T3: a tenant-scoped, RLS-enforced chunks table in the
RDR-152 Postgres, a `VectorRepository` backed by pgvector instead of Chroma, native
hybrid search, the RDR-108 manifest as a real FK join, retirement of Chroma and
`chroma_quotas`, and a copy-not-move migration of existing Chroma collections.

### Schema (adopt the conexus-validated contract)

- Extensions: `vector` (≥ 0.8), `pg_trgm`.
- A chunks table keyed by the existing content-addressed identity (`chash[:32]` = the
  Chroma natural ID today), carrying `tenant_id`, `collection` (the four-segment
  conformant name, now a *column*/filter rather than a separate store), document text,
  the embedding `vector(N)`, a `tsvector` (generated) for FTS, and metadata.
- FORCE RLS by `tenant_id` keyed on the engine tenant GUC (see Open Decision 1).
- HNSW index `m=16, ef_construction=64`; session `hnsw.iterative_scan='relaxed_order'`.
- The RDR-108 `document_chunks` manifest gains a real FK to the chunks table
  (`documents.tumbler → document_chunks.chash → chunk`), enabling a SQL join with
  referential integrity in place of the cross-store lookup.

### Query path

- `search`/`query`: server-side embed (unchanged) then `ORDER BY embedding <=> $q`
  with a metadata `where` predicate and the tenant RLS scope; multi-collection becomes
  a filtered union/`collection IN (...)` instead of N Chroma collections.
- **Hybrid search**: `tsvector` (+ `pg_trgm`) and vector distance fused and ranked in
  one query, replacing the engine's current FTS5 + Chroma two-path fusion.

### Retire

- Chroma client paths (local `PersistentClient`, `CloudClient`) and `chroma_quotas.py`
  (the result/concurrency/document-byte caps are Chroma-imposed and fall away).
- The `skp06` app-layer Chroma tenant guard (never built; superseded here).

### Migrate

- Copy-not-move ETL of existing local `PersistentClient` + ChromaCloud collections into
  pgvector (re-home vectors; re-embed only if a model/dim change forces it), with a
  rollback flag, mirroring conexus RDR-001 Phase 8 cutover.

## Open Decisions (to settle during research/gate)

1. **GUC name reconciliation.** The engine's RLS keys on `nexus.tenant` (T2/catalog)
   and `nexus.t1_tenant` (T1); the conexus *stub* used `app.tenant_id`. Since the engine
   owns schema + RLS policy (conexus:RDR-001), `nexus.tenant` is the canonical
   recommendation and the product consumes the engine's changesets; the stub
   (`app.tenant_id`) is throwaway. Confirm so the product's `SET LOCAL` path matches the
   shipped policy. *(Recommendation: `nexus.tenant` canonical; reconcile conexus.)*
2. **Test substrate.** Engine unit tests run on `io.zonky` EmbeddedPostgres, which has
   no pgvector extension. The affected suites must run against a pgvector-capable PG —
   switch to Testcontainers `pgvector/pgvector:pg17` (what conexus uses for jOOQ
   codegen) or source a zonky build with the extension. Gates the entire TDD loop;
   resolve first.
3. **Per-model vector dimensions.** pgvector columns are fixed-dim: 1024
   (`voyage-context-3`, `voyage-code-3`) vs 384 (local MiniLM), with cloud vs local
   mode in play. Decide per-model/per-dim table strategy (one table per dim, per-model
   tables, or a dim-tagged design) — the conexus stub deferred this ("the real engine
   changesets set the production dimension").
4. **Validation seam for the go-live gates.** conexus `xr7.8.9` requires a
   production-scale filtered-recall harness (iterative_scan recall when filtered set >
   ef_search, RLS-narrowed, multi-collection) and hybrid-search parity (tsvector+vector
   vs the current FTS5+Chroma path) against the *live* engine. This RDR must expose a
   test-invocable seam to that path.

## Research Resolutions (2026-06-09)

Recorded in T2 `nexus_rdr/155-research-1..4`.

1. **GUC — RESOLVED: `nexus.tenant` canonical.** Verified the engine uses `nexus.tenant`
   uniformly (TenantConstants.GUC_NAME; TenantScope stamps it) and `nexus.t1_tenant` for
   T1; zero `app.tenant_id` in `service/src` (it exists only in the conexus throwaway
   stub). The engine owns RLS policy (conexus:RDR-001), so the chunks table keys on
   `nexus.tenant` and `SET LOCAL` via `TenantScope.withTenant` scopes vectors with no new
   mechanism. conexus reconciles its stub path; the engine ships `nexus.tenant`.
2. **Test substrate — RESOLVED (recommendation), one CI confirmation outstanding.** The
   Liquibase **master** changelog is applied wholesale by every service test, so once the
   pgvector changeset (`CREATE EXTENSION vector` + `vector(N)` + HNSW) is in master,
   **every** test needs pgvector — which io.zonky EmbeddedPostgres does not ship.
   Recommendation: move the Java service test module to Testcontainers
   `pgvector/pgvector:pg17` (uniform, conexus-aligned; the consolidated schema cannot be
   half-applied). **Gate sub-item:** confirm nexus CI provides Docker for the service
   module (Testcontainers needs a Docker daemon). If CI has no Docker, fall back to
   Liquibase context-gating the pgvector changeset (keep zonky for non-vector suites) —
   the only thing that flips the recommendation. Resolve in Phase 1 (gates the TDD loop).
3. **Dimensions — RESOLVED (recommendation).** Verified: local = MiniLM 384 or bge-base
   768 (RDR-144 guided choice); cloud = Voyage 1024 (context/code/3 all 1024). A
   deployment uses one model, so single-dim-per-deployment is the common case; the
   collection-name model segment deterministically yields the dim. pgvector needs a
   fixed-dim column for HNSW. Recommendation: **per-dim physical tables `chunks_<dim>`**
   (384/768/1024) routed by the model segment — handles the RDR-144 384↔768 window and
   future multi-model without an ALTER. Fallback: single `chunks` table at the
   deployment dim. Confirm at gate.
4. **Validation seam — RESOLVED.** The seam is the existing `nexus-service` HTTP
   `/v1/vectors/*` API on a pgvector PG (no new surface). The engine ships a fixture-load
   + dual-run harness (engine pgvector vs a Chroma baseline on verbatim-identical
   vectors; exact-count recall + a p95 bound) and a hybrid-parity comparand. **Ordering
   constraint:** hybrid parity must be green on the live engine **before** Chroma is
   deleted (Phase 4 retire is gated behind Phase 3 parity). conexus `xr7.8.9` owns the
   production-scale gate, driving the engine artifact.

## Alternatives Considered

- **Keep T3 on Chroma, scope it app-layer (the original skp06 fix).** Rejected. Leaves
  the split store, the cross-store manifest lookup, two tenancy models, and the Chroma
  quota class; the app-layer guard is throwaway once pgvector lands. conexus RDR-001
  already rejected "keep T3 on Chroma Cloud."
- **Per-model schema/database isolation for vectors.** Rejected for the same reasons
  RDR-001 rejected schema/db-per-tenant: multiplies migration/connection overhead with
  no isolation benefit RLS does not already give.
- **A dedicated external vector engine (not pgvector).** Reconsider only if Open
  Decision 4's production-scale recall gate fails. The slice spike passed; this is the
  registered fallback, not the plan.

## Trade-offs

- **pgvector filtered-recall at production scale is the single gating risk** (Open
  Decision 4). Mitigated by the passed slice spike + documented pgvector-0.8 behavior;
  must be re-validated at scale before cutover, not assumed.
- **Test-infra churn** (Open Decision 2): moving vector suites off EmbeddedPostgres to
  Testcontainers adds Docker to those suites' loop. Accepted; it is the only way to
  exercise pgvector hermetically.
- **A data migration** (copy-not-move) of the live corpus. Accepted; bounded by
  corpus size and de-risked by copy-not-move + rollback.

## Approach — Implementation Plan

Phased; each phase gates (phase-review cross-walk + code-review-expert +
substantive-critic + suite green) before the next. Detailed planning follows accept.

1. **Schema + test substrate.** Resolve Open Decisions 1–3; land the pgvector chunks
   table (extensions, `tenant_id`, FORCE RLS, HNSW, `tsvector`) as Liquibase changesets
   carrying the `conexus_svc` role-attribute contract (`conexus-xr7.3.11`); move the
   affected suites to a pgvector test PG. RLS behavioral suite (fail-closed default,
   cross-tenant SELECT/INSERT/UPDATE WITH CHECK, `SET LOCAL`-over-pooler leak case).
2. **VectorRepository on pgvector.** Rewrite the storage/ANN path (upsert, search,
   get, list, delete, count, update-metadata) Chroma → pgvector; `collection` becomes a
   column/filter; the RDR-108 manifest FK join.
3. **Hybrid search.** `tsvector` + `pg_trgm` + vector fusion in one query; parity seam
   to the current FTS5 + Chroma path (Open Decision 4).
4. **Retire Chroma + chroma_quotas.** Remove client paths and the quota guard; supersede
   `nexus-skp06`.
5. **Migration ETL + cutover.** Copy-not-move of local + ChromaCloud collections into
   pgvector; rollback flag; production-scale recall + hybrid-parity gates
   (conexus `xr7.8.9`).

## Test Plan

- RLS behavioral suite extended to pgvector chunk rows: fail-closed default,
  cross-tenant SELECT/INSERT/UPDATE WITH CHECK, `SET LOCAL`-over-pooler leak case.
- Filtered-vector recall/latency harness with exact-count assertions vs Chroma
  baselines, re-run at production scale (the un-retired iterative_scan risk).
- Hybrid-search parity: tsvector+vector vs the current FTS5+Chroma path on a fixture
  set, with an overlap threshold (load-bearing for cutover — a divergence is a
  user-visible behavior change).
- Migration: copy-not-move integrity (row counts, vector identity, manifest FK).

## Validation

The consolidation is **gated on production-scale filtered recall** (Open Decision 4 /
conexus `xr7.8.9`). The slice spike passed; do not cut over without the at-scale recall
+ hybrid-parity harness green against the live engine path.

## Finalization Gate

_Pending — run `/conexus:rdr-gate` when the draft is complete._

## References

- `conexus:RDR-001` — Productizing nexus as a multitenant hosted service (the accepted,
  validated product-side architecture this RDR implements the engine half of).
- `conexus-xr7.3.11` — the named engine-changeset gap this RDR closes; carries the
  `conexus_svc` role-attribute contract.
- `conexus-xr7.8.9` — the production-scale recall + hybrid-parity go-live gates this
  RDR's schema + seam unblock.
- RDR-152 — Postgres + Java storage service (parent substrate).
- RDR-108 — Catalog/T3 graph-identity split (the doc-to-chunk join that goes relational).
- RDR-105 — T1 Chroma architecture (T1-local rationale; T1 is out of scope here).
- T2 memory `nexus_rdr/152-cloud-locality-scope` — the locked locality + pgvector decision.

## Revision History

- 2026-06-09: Created (draft). Engine-side counterpart to the accepted conexus RDR-001;
  supersedes `nexus-skp06` (vector tenant isolation becomes native RLS). Four open
  decisions registered (GUC reconciliation, pgvector test substrate, per-model
  dimensions, production-scale validation seam).
