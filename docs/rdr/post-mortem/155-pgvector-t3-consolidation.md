# Post-mortem: RDR-155 — pgvector T3 Consolidation

**Closed in two stages.** Stage 1, 2026-06-10: Chroma *serving* retired (accepted
2026-06-09), P4b deletion world-blocked. Stage 2, 2026-07-28: the final deletion
shipped and the RDR closed — see **P4b: the final deletion** at the end of this
document. Everything above that section is the stage-1 record, left as written.

Epic `nexus-skp06` (superseded); the P4b arc ran under epic `nexus-rn3wo`.
Phase beads: `nexus-mf447` (P1), `nexus-tqeg6` (P2),
`nexus-eap5l`/`nexus-sbvg0`/`nexus-h3ked` (P3), `nexus-655hc`/`nexus-1k8s1`/`nexus-2ba3x`
(P4a), `nexus-unp61`/`nexus-9n4pn`/`nexus-a0i5u` (P5),
`nexus-19svb`/`nexus-g37fr`/`nexus-8zpmf` (P4b). `close_reason: implemented`.

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

---

# P4b: the final deletion (2026-07-25 → 2026-07-28)

The stage-1 record above closes with P4b "world-blocked on `nexus-luxe6`". That
blocker cleared 2026-06-29; the deletion landed in PR #1422 on 2026-07-25 and
the gate (`nexus-8zpmf`) closed 2026-07-28.

## What P4b delivered

§Retire's three bullets, verified independently by three reviewers against the
tree rather than against the gate's own record: zero live `chromadb` imports in
`src/`, `ModuleNotFoundError` on import, zero `uv.lock` entries;
`chroma_quotas.py` gone with only prose and deliberate negative-assertion hits
remaining; no reachable `PersistentClient`/`CloudClient`/`EphemeralClient`
construction; `service/` Java clean but for the declared survivor. `skp06`
closed as superseded, exactly as §Retire specified.

Deliberate survivors, recorded so a future reader does not file them as misses:
`ChromaSchemeHandler.java` and `chroma://` URI literals resolve persisted data
out of pgvector (RDR-169 G3) — a data format, not a dependency. Frozen Chroma
directories remain orphan-by-design under the copy-not-move contract; deleting
that data is a separately consented act.

## The failure mode worth writing down: verifying the claim, not the enforcement

Three incidents in this phase share one shape. A statement about coverage was
verified by *reading the statement* rather than by checking whether anything
enforced it.

**1. A guarantee traded for a test that did not exist.**
`test_storage_boundary_lint.py::test_chromadb_arms_retired_from_banlist`
justifies REMOVING the `chromadb` arms from the storage-boundary BANLIST on the
explicit grounds that "tests/test_rdr155_p4b_deletion_gate.py bans any chromadb
IMPORT anywhere in the package". It did not. That gate banned ~30 dead
`nexus.*` modules and never `chromadb` itself; the only chromadb-import bans
were single-file scoped. So this RDR's *primary* claim — §Retire bullet 1 — was
hand-verified at every gate pass and enforced by nothing, while a real
guarantee had been given up in exchange for it. Three gate passes missed it.
Fixed by writing the missing test (`test_no_chromadb_import_anywhere_in_src`,
AST over all of `src/nexus/`, any nesting depth), mutation-verified against a
*function-local* import — the shape an anchored regex misses.

**2. A verification run against the wrong knob.** `de07b4f1` recorded verifying
with `NX_STORAGE_BACKEND=sqlite`. The conftest predicate is
`NX_TEST_T2_SUBSTRATE`, so that setting leaves `engine_substrate_selected()`
True and *skips the entire dies-rostered class it claimed to exercise*.
Measured both ways: `NX_STORAGE_BACKEND=sqlite` gives 5 passed / 3 skipped;
`NX_TEST_T2_SUBSTRATE=sqlite` gives 2 FAILED. It shipped a refusal with no test
of its own and left two tests red — under a commit message carrying an explicit
"COVERAGE CAVEAT" about the suite overstating what was checked.

**3. Ledger rows asserting evidence the tree did not support.** Row 67's
`validated-by` said "workflow deleted" for a workflow that is in-tree; row 68
claimed "nightly green post-edit" while the gate was red; row 28 claimed "chroma
labeling retired" while `doctor.py` still emitted a top-level `"chromadb"` JSON
key, with the code's own comment saying it would stay "until P4b renames it".

The correction is cheap and worth making routine: **when a record cites coverage
elsewhere, open that file.** All three survived because the citation was read as
the evidence.

## The most expensive defect came through a shape, not a deletion

The 71-row capability-disposition ledger (T2 `[21097]`) was built as an
anti-silent-capability-loss mechanism and earned its keep. But the costliest
find came from outside it: `HttpTaxonomyStore.get_topic_link_pairs` returned
triples where the oracle returns a mapping, for the entire life of the RDR-152
port. Every consumer annotates the mapping, so `scoring.apply_topic_boost`
raised `ValueError` inside `search_engine`'s best-effort `except Exception`, and
the *whole* topic boost — same-topic half included — was silently discarded in
service mode, the default substrate. `tests/db/t2_store_contract.py` could not
see it: it asserts parameter NAMES and never return shapes.

That is the ledger's own failure class arriving through a shape mismatch rather
than a deletion, which is why no row covered it. Tracked as `nexus-b8a5a`,
raised to P1 and sequenced ahead of `nexus-i711w` — the differential oracle
exists only while both twins do.

## The phase removed its own test fixture

P4b deleted `nx guided-upgrade`, the verb the migrated-box rehearsals
(`--guided`/`--cold`/`--hole-punch`) drive. So the final phase removed the
fixture its own pre-cut shakeout requirement depended on. The shakeout was
performed as a **substitution** — containerized `--shakeout` (32/32: provision,
14-verb matrix, incremental re-index 3≪41, concurrent load with zero 5xx) plus
`--package-upgrade` — and recorded as a substitution, not as satisfaction.
Neither exercises large, real, long-lived migrated data; nothing currently does.

Running `--package-upgrade` once by hand surfaced `nexus-gu4xd` (P1):
`restart-stale` installs the new engine binary (sha256 provably changed) while
the running service keeps answering the old version. It went unseen because
**there is no scheduled workflow for that leg at all** — the only two rehearsal
workflows are disabled and red respectively. The ungated axis is the more
important finding than the bug on it.

## Divergences at close

Neither forces `partial`; both are recorded rather than absorbed.

- The pre-cut live shakeout ran on containers, not a real migrated box —
  because P4b deleted the tooling that would have simulated one, and the single
  available real box was an unsafe target (client-side T2 migrations plus 11
  Liquibase changes between its installed version and develop, a moved
  `REQUIRED_ENGINE_VERSION`, live MCP servers).
- "The inverse-grep audit ran clean across ALL surfaces" is **not literally
  true**: `tests/e2e/migration-rehearsal/` carries real executable
  `chromadb.PersistentClient(...)` that the deletion gate's inverse-grep never
  sweeps. Inert (separate pinned-chromadb venv, self-guarding driver) and owned
  by `nexus-8nlj4`.

## Residuals (tracked, not silent)

`nexus-gu4xd` (restart-stale non-convergence) · `nexus-8nlj4` (ungated upgrade
axis, the migration-rehearsal Chroma surface, the large-migrated-data residual)
· `nexus-b8a5a` (T2 return-shape parity — **before** `nexus-i711w`) ·
`nexus-sghyo` and `nexus-4lkmz` (client Voyage credential, tier-0 EF, isolated-T1
leg: vestiges of a client that no longer embeds) · `nexus-vod2b` (the
`NX_SERVICE_URL` mode-split tripwire) · `nexus-qvs2h`/`nexus-12m77` (gate-visible
halves cleared; non-gate items remain theirs) · ledger rows 38/59/60
cut-deferred until `LAST_MIGRATION_CAPABLE` is stamped at the 7.0.0 cut,
verified honest rather than smuggled · `NX_GATE_FLOOR` re-pin to ~474 in the
develop→main promotion commit.

## One thing the RDR got right eleven weeks early

§Migrate specified that if collection-name normalization is ever required, it
must update `topic_assignments.source_collection` **in lock-step** — "the same
string-copy-orphan class RDR-108 fixed". That is exactly the constraint binding
any future 384→768 centroid migration, written long before anyone needed it.
