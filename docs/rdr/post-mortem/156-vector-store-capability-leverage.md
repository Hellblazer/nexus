# Post-mortem: RDR-156 — Vector-Store Capability Leverage

**Closed:** 2026-06-14 (P4 gate). Epic `nexus-70r3c`. `close_reason: implemented`.

## What shipped

Five phases (P0-P4; P5 hybrid_search world-blocked), each TDD + stacked reviews + phase-review-gate:

- **P0 — FK set + empty-table hygiene** (`nexus-70r3c.1`/`.2`, PRs #1167/#1168; VALIDATE follow-on PR #1280 `nexus-70r3c.3`): five `NOT VALID` FKs in Liquibase changeset `fk-002`: `chunks_384/768/1024 (tenant_id, collection)` to `catalog_collections (tenant_id, name)`, `chash_index (tenant_id, physical_collection)` to `catalog_collections`, `topic_assignments (tenant_id, source_collection)` with `ON UPDATE CASCADE`. Also: collection registration made mandatory-and-first on every new-collection write path; `chash`/`position` CHECK constraints; `catalog_collections` temporal typing fix (`text` to `timestamptz NULL`). `VALIDATE CONSTRAINT` shipped as a separate named changeset (`fk-002-validate`) run after RDR-153 data migration completed with `total_failed == 0`. Blank-chash guard added fail-loud. Java CI required a fixture sweep (PR #1168) because P0 constraints reject pre-existing non-conformant fixture data.

- **P1 — soft delete** (`nexus-70r3c.5`/`.6`, merged `72db6476`): `deleted_at timestamptz NULL` on `catalog_documents` and `catalog_links`. Partial indexes (`WHERE deleted_at IS NULL`). Tombstone functions: `nexus.document_trash(doc)`, `nexus.document_restore(doc)`, `nexus.purge_trash(older_than interval)` with chunk-orphan anti-join sweep and `nexus.tenant` GUC guard. `live_chunks` view. All destructive catalog verbs repointed to tombstoning. Reviewer finding: `getDocument` in Java had no `deleted_at IS NULL` filter; fixed (`nexus-70r3c.6` fix commit). Absorbs RDR-106's catalog-projection scope onto the PG substrate.

- **P2 — manifest functions** (`nexus-70r3c.8`/`.9`, merged `2fe59b6a`): `nexus.manifest_orphans(dim int)`, `nexus.manifest_backfill()`, `nexus.document_text(doc_id text)` as first-class DB stored functions, replacing generated-SQL-string artifacts executed by hand. Changeset `catalog-004`. Review fixes: restore-cycle pin, GUC reader contract, dedup caveat documented.

- **P3 — `collection_vector_stats` view** (`nexus-70r3c.11`/`.12`/`.13`, merged `1d9daa0d`): per-collection chunk count, dim, last write; `security_invoker = true`; `/v1/vectors/stats` endpoint; client repointed. Review fixes: `list_collections` pin aligned with stats contract, de-voyage mocked names.

- **P4 — combined-query unification** (cluster: `nexus-70r3c.14`/`.15`, `nexus-joesk`, `nexus-zo0zt`, `nexus-houg9`, `nexus-889ff`, `nexus-rzqto`, `nexus-lcogi`; merged `dbd1c894`): metadata-scoped and topic-scoped search as combined-query SQL functions; `find-by-author` and type-scoped plan-runner repointed (`nexus-zo0zt`); graph-hop combined-query function (`nexus-houg9`, `d72db6e1`); subtree/where/author/chash combined-query shapes (`nexus-889ff`); `query()` function repoint (`nexus-rzqto`); selectivity-aware text-first plan for `hybridSearch` collapse (`nexus-lcogi`, `30515ab1` — the RRF tail-latency fix). Capability-selection discipline recorded in `src/nexus/db/AGENTS.md` (`nexus-70r3c.19`). Gate `nexus-70r3c.P4.G` PASSED 2026-06-14. Four encodings verified per-bead: (1) vector-as-arg, (2) EXPLAIN HNSW-survives-join, (3) narrow-collection exact-recall `== N`, (4) separate-commit repoint/delete.

- **P5 — `hybrid_search` stored function**: world-blocked on conexus xr7.8.9 production-scale recall + hybrid-parity go-live. Bead `nexus-2bqpn` (dance delete = the stitching deletion committed separately for rollback) world-blocked alongside it.

- **RDR-106 superseded** (`12bb07f5`): `docs(rdr): RDR-106 superseded by RDR-156`.

## What was deliberately deferred

- `nexus-2bqpn` — the P4 stitching-deletion commit (separate from the repoint, per rollback discipline). Blocked on conexus xr7.8.9. `git revert nexus-rzqto` is the rollback; no deletion commit yet, so the rollback property holds.
- P5 `hybrid_search` stored function — world-blocked on xr7.8.9 parity gate; P5 must benchmark against the service-side fusion it would replace, not assume parity.
- `nexus-aeceu` — Python local-mode `Catalog.stats()` omits `chunk_count` (pre-existing Java/PG parity gap). Not RDR-156 core scope; filed as deferred.
- UNIQUE on `catalog_documents (tenant_id, source_uri)` — required a ghost/duplicate audit against live SQLite data first; audited as a P0 item and recorded as "constraint only if the audit is clean" rather than a blind add.

## Lessons

- **The `NOT VALID` + separate `VALIDATE` two-changeset split was the correct schema discipline.** A single combined changeset would have failed on any deployment with pre-RDR-153 data (non-conformant rows from the SQLite heritage) and blocked the migration entirely. The split lets the constraint protect new writes immediately while the data-quality remediation (RDR-153) runs.

- **Java CI fixture sweep at P0 was load-bearing.** Adding FKs that reject non-conformant chash/collection values broke 400+ pre-existing Java test fixtures that used placeholder values. The fixture sweep (PR #1168, `nexus-70r3c.2`) was required before any further development could proceed; skipping it was not an option because CI runs the Java suite on every merge.

- **Separate-commit repoint/delete is the correct rollback discipline for combined-query migration.** The repoint (`nexus-rzqto`) and the stitching deletion (`nexus-2bqpn`) were committed separately by design: `git revert rzqto` is the rollback, which is safe as long as the deletion commit has not landed. This design was required by the P4 gate cross-walk and kept the rollback window open for the world-blocked P5 path.

- **The narrow-collection exact-recall pin (`== N`) is the critical safety assertion.** Research Finding 5b from the RDR spec showed that medium-selectivity fixtures pass even when narrow-collection inputs silently under-return at the `max_scan_tuples` ceiling. The exact-count gate (`== N`, not `>= threshold`) is the assertion that proves the selectivity-strategy switch actually fires. This mirrored the RDR-155 P3.E recall@10 == 1.0 pattern — the same class of silent regression defended by an exact pin.

- **`getDocument` missing `deleted_at IS NULL` was caught by the reviewer, not the tests.** The implementation added the tombstone column and partial indexes but omitted the filter on the Java read path. Green tests passed because the fixture data had no tombstoned rows. The code reviewer caught the absent filter during the P1 stacked review. This is the "default committed" enforcement-point gap: a view that filters tombstones protects consumers who use the view; a raw read-path call bypasses it.

- **`purge_trash` required an explicit `nexus.tenant` GUC guard.** Called with no tenant GUC set (e.g., a maintenance cron with a BYPASSRLS role), a purge could cross tenant boundaries. The function body checks `current_setting('nexus.tenant', true)` is non-empty and raises otherwise. This was a design requirement in the RDR Decision section, not discovered during implementation — a case where the spec explicitly anticipated the hazard.

- **The `lcogi` selectivity fix (`30515ab1`) was a real collapse case, not speculative.** `hybridSearch` with a high-selectivity text filter (the conexus-qsa p95 tail) was spending 1778ms/query because the planner chose a vector scan before the text filter. The selectivity-aware text-first plan (evaluate text gate once, not per-chunk-row) collapsed the p95 from 3159ms to 1781ms. The issue was discovered in production via the go-live correctness arc and fixed under RDR-156 rather than as a standalone bead.

## Drift classification

- **Missing failure mode**: `getDocument` absent `deleted_at IS NULL` filter is a failure class that green tests on non-tombstoned fixture data could not detect. Pattern: partial-index enforcement at the schema level does not propagate to read paths that bypass the view.
- **Framework API detail**: Postgres `FORCE ROW LEVEL SECURITY` includes the owner role, which is not obvious from the PG docs without the spike (S0.1 from RDR-152 established this). The `purge_trash` GUC guard is the follow-on consequence: any PG function that could run outside the RLS fence needs an explicit application-level guard.
- **Deferred critical constraint**: P5 `hybrid_search` and the stitching deletion (`nexus-2bqpn`) are the residual open scope. The combined-query thesis is partially implemented: the query *shapes* exist but the stitching deletion is pending world-unblock. This is explicitly tracked and not silent scope reduction.
