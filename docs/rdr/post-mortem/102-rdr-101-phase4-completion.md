# Post-Mortem: RDR-102 — RDR-101 Phase 4 Completion

**RDR:** [RDR-102](../rdr-102-phase4-completion.md)
**Status:** closed (implemented), 2026-05-02
**Branch:** `feature/nexus-u2n9-phase4-completion` (15 commits)
**Validation:** `~/rdr-102-phase-d-report-20260502-133459.md` (Phase D operator gate, exit 0)

## Outcome

All four phases shipped against the gaps the 2026-05-02 audit identified:

- **Phase A (Gap 1)** — doc_indexer family + streaming PDF pipeline pre-flight register catalog Documents and thread `doc_id` through `make_chunk_metadata`. Chunks land in T3 with `doc_id` at write time.
- **Phase B (Gap 2)** — `source_path` removed from `ALLOWED_TOP_LEVEL` (32→31, restoring one Chroma-cap slot); HARD-removed from `make_chunk_metadata` signature; removed from 7 writer call sites in lockstep. `_PRUNE_DEPRECATED_KEYS ∩ ALLOWED_TOP_LEVEL == ∅` enforced by unit test.
- **Phase C (Gap 3)** — doctor surfaces per-collection + global `orphan_ratio` in JSON; clarified Collections-in-log header; new "Orphan ratio" text section emits WARN > 50% with operator-runnable recovery pointer.
- **Phase D** — operator-runnable validation gate (`tests/e2e/rdr-102-phase-d-gate.sh`): write-then-verify against production T3 (5/5 writers PASS) + orphan-recovery smoke against the 2026-05-01 production catalog backup tarball (16/138,625 orphans recovered via content_hash, expected per limitations doc).

## What worked

**TDD discipline held.** Every impl bead depended on its test bead; every test was demonstrated to FAIL on baseline before the impl landed. The Phase B cross-set test (`test_prune_deprecated_keys_disjoint_from_allowed_top_level`) is the regression guard CI failed to provide pre-RDR-102 — it makes any future re-introduction of source_path into either set a build break.

**Substantive-critic gate at design time caught real issues.** The original draft had:

- Alternative A3: keep `source_path` as a deprecated no-op kwarg "for back-compat with external consumers." The critic identified this as a silent failure mode (caller passes value, normalize() drops it, downstream `where={"source_path": ...}` returns zero results — invisible). The hard-remove approach forces every call site to be edited in lockstep with a TypeError.
- The original Phase D gate ("post-sweep dry-run prune returns zero") was satisfiable by running the prune first and getting zero — exactly the tautological-PASS shape RDR-102 was closing. The critic forced the gate into "write-then-verify on freshly-created collections."
- The original RF-4 inventory missed the streaming PDF site at `pipeline_stages.py:145` (writer site #4); the critic caught it. Without that site, every PDF indexed via `nx index repo` would have continued regressing source_path post-Phase-B.

**Multi-agent code review caught one critical gap before merge.** Four parallel review agents (Phase A impl, Phase A synthesize-log, Phase B schema, Test quality) returned. The test-quality agent flagged that `_prune_deleted_files` at `indexer.py:1139` reads `meta.get("source_path", "")` and would silently no-op for post-Phase-B repos — the same RF-8 schema-vs-reader divergence shape, in a reader rather than a writer. Without the review the regression would have shipped as a deleted-file-chunks-accumulate-forever class of bug.

## What surprised me

**The synthesizer priority-0 fix was load-bearing.** After Phase B landed, `test_doctor_doc_id_coverage_e2e` failed: post-Phase-B chunks have no source_path AND their title doesn't match Document.title (chunk title is `"filename:chunk-N"`, Document title is `"filename"`), so `synthesize_t3_chunks` orphaned every chunk during the synthesize-log → t3-backfill round trip. Fix: trust `meta["doc_id"]` directly when present (priority 0). This was caught only because the e2e test went deep enough to walk the actual round trip; a unit test on `synthesize_t3_chunks` alone would not have surfaced it.

**`Catalog.update()` writes a `DocumentRegistered` event.** I initially asserted "exactly 1 DocumentRegistered after first index" in the idempotency test. That was wrong — `Catalog.update()` (called by the post-hook to write chunk_count) also writes a `DocumentRegistered` event under the lossless-replay model at `catalog.py:1865-1888`. The actual idempotency invariant is "re-index of unchanged file adds ZERO events." Code review caught the absolute-count weakness and the assertion was tightened to `1 ≤ count ≤ 2`.

**The `_catalog_markdown_hook` chunk_count regression was a self-inflicted wound from Phase A.** Pre-flight register writes a Document with `chunk_count=0`. The post-hook then called `cat.register()` unconditionally → hit the `by_file_path` early-return → returned the existing tumbler WITHOUT updating chunk_count. Every markdown Document would have stuck at chunk_count=0 in the catalog. Fix: mirror `_catalog_pdf_hook`'s if-existing/update branch (which had this guard from RDR-101 Phase 4). Caught by code review immediately after Phase A merged.

## What didn't ship and why

**Runtime warning for no-catalog re-index.** Phase B made source_path-keyed staleness check unfindable for chunks without doc_id (no catalog → no doc_id → staleness check misses → re-embed every run). The reviewer suggested a runtime warning. Documented in `docs/migration/rdr-101-phase4-orphan-recovery.md` § "No-catalog mode caveat" instead of adding a runtime warning that would also fire on genuine first-time indexes (noise concern). A targeted warning is a follow-up.

**Dead `pdf_path` parameter in `pipeline_stages._build_chunk_metadata`.** After dropping `source_path=pdf_path`, the parameter is unused for metadata production but still serves logging + the stale-prune call. Removing it would cascade through `chunker_loop` + `_embed_and_write_batch`. Phase 5b cleanup candidate.

**Exporter `--remap` end-to-end effect.** The remap logic still operates on the export stream but the imported chunks lose source_path during normalize. Two `test_remap_on_import` tests are xfail+strict marking the gap. Deprecation decision deferred.

## What's now unblocked

- **Phase 5a (`nexus-o6aa.11`)**: `[catalog].event_sourced` opt-in flag default-on. Gated on Phase B + Phase D landing; both done.
- **Phase 5b** cleanup candidates captured in `nexus/rdr-102-completion-state.md` memory entry.

## Operator surfaces shipped

- `nx catalog synthesize-log --force --chunks --prefer-live-catalog [--dry-run] --json`
- `nx catalog t3-backfill-doc-id --collection X`
- `nx catalog doctor --t3-doc-id-coverage --json | jq '.t3_doc_id_coverage.orphan_ratio'`
- `tests/e2e/rdr-102-phase-d-gate.sh [TARBALL_PATH]` — full operator validation gate
- `docs/migration/rdr-101-phase4-orphan-recovery.md` — companion operator doc

## References

- RDR file: `docs/rdr/rdr-102-phase4-completion.md`
- Audit baseline: `docs/migration/rdr-101-phase4-audit-2026-05-02.md`
- Recovery doc: `docs/migration/rdr-101-phase4-orphan-recovery.md`
- Phase D report: `~/rdr-102-phase-d-report-20260502-133459.md`
- nx memory: `nexus/rdr-102-completion-state.md`
