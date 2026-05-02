---
title: "RDR-102: RDR-101 Phase 4 Completion — doc_indexer Wiring, Write-Side source_path Retirement, Doctor Visibility"
id: RDR-102
type: Architecture
status: closed
close_reason: implemented
priority: medium
author: Hal Hildebrand
reviewed-by: self
created: 2026-05-02
accepted_date: 2026-05-02
closed_date: 2026-05-02
post_mortem: post-mortem/102-rdr-101-phase4-completion.md
gap_pointers:
  Gap1: src/nexus/doc_indexer.py:112
  Gap2: src/nexus/metadata_schema.py:50
  Gap3: src/nexus/commands/catalog.py:4647
related_issues: [nexus-o6aa.10, nexus-u2n9, nexus-wb3c, nexus-ht8j, nexus-uusi, nexus-4uv5, nexus-ejs4, nexus-2nls, nexus-wc3f]
related_tests: [tests/e2e/rdr-102-phase-d-gate.sh]
related: [RDR-101]
---

# RDR-102: RDR-101 Phase 4 Completion — doc_indexer Wiring, Write-Side source_path Retirement, Doctor Visibility

RDR-101 Phase 4 was declared complete on 2026-05-02 after the prune verb shipped, the t3-backfill drained, and `nx catalog doctor --t3-doc-id-coverage --replay-equality` returned PASS on production. An operator-driven audit immediately afterwards found that PASS was tautological: the gate is "every non-orphan chunk has doc_id" and 84% of T3 chunks (309,681 of ~370K) are classified as `synthesized_orphan`. Underneath the green checkmark, three structural gaps were live:

- **`nx index pdf`, `nx index md`, and `nx index rdr` standalone** (the doc_indexer paths) build chunk metadata via `make_chunk_metadata()` *without* passing `doc_id`. Catalog registration fires *after* the upsert. Existing chunks survive only because ChromaDB upsert merges metadata — pre-existing doc_ids from synthesize-log persist, but no fresh write ever supplies one.
- **`make_chunk_metadata()` keeps writing `source_path`** because `ALLOWED_TOP_LEVEL` still includes it. The prune verb (`_PRUNE_DEPRECATED_KEYS`) strips it post-write. Every reindex cycles the regression: index → strip → reindex → re-strip. The reader-audit doc explicitly flagged this as a cleanup item but the cleanup never landed.
- **Doctor's coverage gate** counts only chunks whose ChunkIndexed events were not flagged orphan — the 84% orphan ratio never appears. "Collections in log: 23" is the count of collections with at least one non-orphan event, and 760 collections in `events.jsonl` (mostly knowledge papers and mirrored repos) are silently absent from the report.

The audit numbers and the affected paths are recorded in `docs/migration/rdr-101-phase4-audit-2026-05-02.md`. This RDR converts that audit into an executable closeout: pre-flight catalog registration in the doc_indexer family, retire `source_path` from the write path, surface orphan ratio, and add cross-validation tests so the same divergence cannot land again.

This RDR is bounded to closing Phase 4 and **does not** move into Phase 5 (the `[catalog].event_sourced` opt-in flag, RDR-101 nexus-o6aa.11). Phase 5 should not start until the live catalog re-prunes cleanly after a fresh reindex — that is the single observable invariant that says Phase 4 is structurally complete.

## Problem Statement

### Gap 1: doc_indexer.* upserts chunks before catalog registration

`indexer.py:run` (the `nx index repo` orchestrator) calls `_catalog_hook()` *upfront*, builds a `dict[Path, str]` mapping every file in the indexed set to its catalog `doc_id`, and threads it through `_index_code_file`, `_index_prose_file`, and `_index_pdf_file` as a `doc_id_resolver` closure. Each per-format indexer resolves doc_id once per file and passes it to `make_chunk_metadata()` at chunk-write time. Result: chunks land in T3 with `doc_id` populated.

The doc_indexer family (`doc_indexer.index_pdf`, `doc_indexer.index_markdown` → `_index_document`, `doc_indexer.batch_index_markdowns`) does the inverse:

1. Build chunk metadata via `make_chunk_metadata()` *without* passing `doc_id`.
2. Upsert via `db.upsert_chunks_with_embeddings(...)`.
3. Fire `_catalog_pdf_hook` / `_catalog_markdown_hook` (registers the Document in the catalog).
4. Fire `fire_post_document_hooks(...)` with `doc_id=_lookup_existing_doc_id(...)` (just-registered).

The chunk is in T3 by step 2; doc_id arrives at step 3. The gap closes only by virtue of an undocumented ChromaDB property: `col.upsert(metadatas=[{...}])` *merges* the new metadata into existing chunks rather than replacing it. So a re-indexed chunk that already has doc_id (from synthesize-log or prior backfill) keeps it. A first-time-indexed chunk gets nothing.

Verified empirically on 2026-05-02:

- `nx index pdf ~/Downloads/delos-papers/permission-systems.pdf --collection knowledge__delos`. Inspect chunk metadata: `source_path` regressed (had been stripped by the prune verb 30 minutes earlier), `doc_id` is missing.
- `nx index md ~/git/nexus/README.md --corpus nexus-571b8edd --force`. Same pattern: `source_path` rewritten, `doc_id` absent.
- ChromaDB merge confirmed via `EphemeralClient`: `col.upsert(ids=['x'], metadatas=[{'a': 99}])` against a record `{'a': 1, 'b': 2}` yields `{'a': 99, 'b': 2}` — `b` survives.

This is not a transient bug. It is the structural mismatch between the two indexer families. Phase 4 declared the projection-via-doc_id model as canonical but only converted one of the two writer trees.

### Gap 2: ALLOWED_TOP_LEVEL ⊇ {source_path}, _PRUNE_DEPRECATED_KEYS ⊇ {source_path}

`metadata_schema.py:50–108` defines `ALLOWED_TOP_LEVEL`, the canonical chunk-metadata key set. Every `_write_batch` call routes through `normalize()`, which drops any key not in this set. `source_path` is at line 52, so it survives normalize().

`commands/catalog.py:5136–5142` defines `_PRUNE_DEPRECATED_KEYS = {"source_path", "git_branch", "git_commit_hash", "git_project_name", "git_remote_url"}` — the keys `nx catalog prune-deprecated-keys` strips. The four `git_*` keys are not in `ALLOWED_TOP_LEVEL` (they get repacked into `git_meta` at normalize time), so the prune is structurally one-shot for them. `source_path` is the exception: it is in both sets. The reader audit (`docs/migration/rdr-101-phase4-reader-audit.md:155`) flagged this:

> "After prune-deprecated-keys lands AND ALLOWED_TOP_LEVEL removes source_path, this write becomes a no-op via normalize(). Until then, dual-write is harmless."

The prune verb shipped (PR #480) and ran on production. The `ALLOWED_TOP_LEVEL` update was filed as deferred and never landed. The dual-write is no longer harmless: every chunk indexed after the prune carries the deprecated key again, and the next prune has work to do. The cycle never terminates without a writer-side fix.

### Gap 3: Doctor's coverage gate hides the orphan ratio

`commands/catalog.py:_run_t3_doc_id_coverage` builds `expected: dict[coll_id, dict[chunk_id, doc_id]]` from non-orphan `ChunkIndexed` events and `expected_orphans: dict[coll_id, set[chunk_id]]` separately. The check loops over `expected` only:

```python
for coll_idx, (coll_name, expected_chunks) in enumerate(expected.items(), start=1):
    # ... compare each chunk's stored doc_id against expected_chunks ...
```

`expected_orphans` is built but never examined for coverage purposes. The PASS gate is:

```
every chunk in expected (i.e., non-orphan ChunkIndexed event) has matching doc_id in T3
```

When the orphan set vastly outnumbers the non-orphan set (309,681 vs 5,400 on the live host catalog), the gate is vacuous. The text output reinforces the misread: `Collections in log: 23` is `len(expected)`, not the count of distinct `coll_id` values in `events.jsonl` (which is 783 — 760 contain only orphan markers and never appear in the report).

The audit doc captures the per-collection coverage (e.g. `knowledge__art` 1/5725 = 0.02%, `docs__Luciferase-f2d57dbc` 1/4197 = 0.02%); operators reading the doctor output today see these as ✓ PASS rows because the gate evaluates only the 1-of-N non-orphan slice.

## Research Findings

Empirical investigation 2026-05-02 against the live host catalog and source tree. Each finding has a verifiable reproducer; results are durable across sessions.

### RF-1 — Confirmed: nx index pdf writes no doc_id, regresses source_path

**Setup:** `nx catalog prune-deprecated-keys` (strips `source_path` + 4 `git_*` keys; baseline = 0 deprecated keys on knowledge__delos chunks).

**Action:** `nx index pdf ~/Downloads/delos-papers/permission-systems.pdf --collection knowledge__delos`. Output: `Indexed 13 chunk(s).`

**Verification:**
```python
col.get(where={'source_path': {'$eq': '/Users/.../permission-systems.pdf'}}, ...)
# 13 chunks returned (i.e., source_path WAS written by the indexer)
# m.get('doc_id', '<MISSING>') → <MISSING>  (no doc_id field)
# m.get('source_uri', '<MISSING>') → <MISSING>  (modern key not written)
```

**Evidence trail:** `events.jsonl` shows `DocumentRegistered` for `1.653.84` immediately after the indexer, but no `ChunkIndexed` events with `synthesized_orphan=false` were generated for the 13 fresh chunks. The Document was registered post-upsert; the chunks lost the chance to learn their doc_id.

### RF-2 — Confirmed: nx index md regresses source_path on every reindex

**Setup:** `nx catalog prune-deprecated-keys` ran 30 minutes prior on docs__nexus-571b8edd. Baseline: 0 chunks with source_path on README.md content_hash.

**Action:** `nx index md /Users/hal.hildebrand/git/nexus/README.md --corpus nexus-571b8edd --force` at 16:47. Output: `Force re-indexed 14 chunk(s).`

**Verification:**
```python
col.get(where={'source_path': {'$eq': '/Users/.../README.md'}}, ...)
# 3 chunks returned (regression confirmed)
# indexed_at = '2026-05-02T16:47:17.39363'  (matches force-reindex)
# 'source_path' in keys: True
# 'doc_id' in keys: False  (MISSING)
```

### RF-3 — ChromaDB upsert MERGES metadata (undocumented behavior)

The reason existing rdr__nexus-571b8edd chunks show 100% doc_id coverage despite the doc_indexer family writing no doc_id: ChromaDB's `col.upsert()` does not replace metadata; it merges. Verified with EphemeralClient:

```python
col.upsert(ids=['x'], documents=['hello'], metadatas=[{'a': 1, 'b': 2}], embeddings=[[0.1]*5])
# After: {'b': 2, 'a': 1}

col.upsert(ids=['x'], documents=['hello2'], metadatas=[{'a': 99}], embeddings=[[0.2]*5])
# After: {'b': 2, 'a': 99}  ←  'b' survives the upsert
```

Implication: existing chunks with doc_id from synthesize-log or prior backfill *retain* the doc_id across re-indexes, even when the new metadata payload omits it. Fresh chunks (first-time indexed via the broken paths) get nothing. This explains the asymmetric coverage seen in production: collections fully reindexed via `nx index repo` (which writes doc_id) are 100%; collections seeded via `nx index pdf` (which doesn't) hover at 0.02%–4%.

### RF-4 — Source-of-truth inventory: 6 T3-bound `make_chunk_metadata` writer sites

Verified by grepping every `make_chunk_metadata` call site that passes a non-empty `source_path=` argument. The substantive-critic gate caught a missing entry in the original inventory (`pipeline_stages.py:145`) and corrected the line numbers for the prose indexer; the table below is the verified authoritative list.

| Site | File:line | Path | Notes |
|---|---|---|---|
| 1 | `src/nexus/code_indexer.py:402` | `nx index repo` (code) | `source_path=str(file_path)` |
| 2 | `src/nexus/prose_indexer.py:103` | `nx index repo` (prose, branch A) | `source_path=str(file_path)` |
| 3 | `src/nexus/prose_indexer.py:183` | `nx index repo` (prose, branch B) | `source_path=str(file_path)` |
| 4 | `src/nexus/pipeline_stages.py:145` | `nx index repo` PDF (streaming pipeline) | `source_path=pdf_path` — **PDF-in-repo write path** |
| 5 | `src/nexus/doc_indexer.py:794` | `nx index pdf` standalone (`_pdf_chunks`) | `source_path=str(pdf_path)` |
| 6 | `src/nexus/doc_indexer.py:874` | `nx index md/rdr` standalone (`_markdown_chunks`) | `source_path=sp` |

**Critic-corrected inventory** — the original RDR draft listed `indexer.py:909` as the PDF-in-repo writer; line 909 actually reads `"doc_id": catalog_doc_id` (the doc_id augmentation at the consumer side of `_pdf_chunks`). The actual writer is upstream in `pipeline_stages._pdf_chunks`. Missing this site would have left every PDF indexed via `nx index repo` carrying `source_path` after Phase B merged.

Other call sites grouped by role (verified out-of-scope):

- **Stale-detection where-filters (4 sites)** that already have doc_id-first fallback shape: `doc_indexer.py:109`, `indexer_utils.py:208`, `indexer.py:653,1098`. These continue to work; the source_path fallback branch becomes dead code post-prune-cycle and can be cleaned up in Phase 5b.
- **Display formatters** `formatters.py:30,35,277,308,358,394` — all route through `_display_path()` which prefers `title` and falls back to `source_path`. New chunks have no source_path; display falls through to title (universally populated).
- **Exporter back-compat** `exporter.py:188,364,366,391,411` — handles legacy `.nxexp` files; out of scope for this RDR.
- **Aspect / catalog readers** `aspect_extractor.py:213,773,1085`, `aspect_promotion.py:64`, `aspect_readers.py:165`, `operators/aspect_sql.py:127,454`, `commands/catalog.py:2230,2535` — read source_path from catalog file_path or queue rows, not chunk metadata. Out of scope.
- **Synthetic search records** `commands/search_cmd.py:134` — in-memory `SearchResult` for ripgrep hits, never written to T3. Out of scope.

The 6 writer sites are the entire D2 scope. The fallback shape in stale-detection is preserved as defense-in-depth.

### RF-5 — ALLOWED_TOP_LEVEL is at the Chroma 32-key cap

`metadata_schema.py:117` documents `MAX_SAFE_TOP_LEVEL_KEYS = 32`. The set currently has 32 entries; the comment at line 118-119 explicitly notes:

> "Phase 5b plans to drop legacy source_path in favour of source_uri (RDR-096 P5.1/P5.2), which restores headroom."

Removing source_path now (per D2) restores that headroom one phase early. No new key is added to the schema by RDR-102.

### RF-6 — Doctor PASS gate is tautological at production scale

Production state 2026-05-02 16:30, post-migrate-finisher:

```
ChunkIndexed events (events.jsonl)
  total                                315,081
  synthesized_orphan=true              309,681  (98.3%)
  synthesized_orphan=false              5,400   ( 1.7%)

T3 chunks (estimated total)            ~370,000
  with doc_id metadata                 ~ 60,000  (16%)
  without doc_id (orphan + missed)     ~310,000  (84%)

Doctor "Collections in log" filter       23 (i.e., have at least one non-orphan event)
Distinct coll_id values in events.jsonl  783
  → 760 collections (mostly knowledge papers + mirrored repos) never appear in the report
```

The PASS gate `every chunk in expected (non-orphan ChunkIndexed event) has matching doc_id in T3` is satisfied by the 5,400-event slice, not the 315,081-event population. The 310K orphan chunks are not invalid; they are correctly classified as not-tied-to-a-Document. But the doctor output gives operators no signal that 84% of T3 lives outside the projection.

### RF-7 — The doc_id formats coexist in events.jsonl

A single source file can have multiple `DocumentRegistered` events with different doc_id formats:

- `019de646-6e74-7092-91a2-ae5bed0230bc` — UUID7, generated by `synthesize-log` walking T3 chunks
- `1.1.1240`, `1.1.1405`, `1.1.1480` — tumbler format from `Catalog.register()`

`rdr-101-catalog-t3-metadata-design.md` for example has all three tumblers from successive registrations and at least one UUID from synthesize-log. The catalog accepts both forms; the Document.tumbler is canonical for live registrations and the synthesize-log UUID is a fallback for chunks that pre-date the catalog hook.

For RDR-102's purposes: pre-flight registration uses tumbler; pre-existing chunks may carry UUID; readers use whatever is in the chunk. No format-conversion work is required.

### RF-8 — `_PRUNE_DEPRECATED_KEYS ∩ ALLOWED_TOP_LEVEL = {source_path}`

Direct set comparison via Python REPL on the imported modules:

```python
from nexus.metadata_schema import ALLOWED_TOP_LEVEL
from nexus.commands.catalog import _PRUNE_DEPRECATED_KEYS
ALLOWED_TOP_LEVEL & _PRUNE_DEPRECATED_KEYS
# frozenset({'source_path'})
```

The four `git_*` keys are not in `ALLOWED_TOP_LEVEL` (they are repacked into `git_meta` JSON before the normalize step), so the prune is one-shot for them. Only `source_path` cycles. The proposed unit test (D4 item 1) is a direct expression of this finding.

## Decision

### D1 — `doc_indexer.*` registers the catalog Document upfront and threads doc_id to chunk-write time

The fix mirrors the structural pattern that already works in `indexer.py:run`. Before any chunk metadata is built, the doc_indexer must:

1. Open the catalog (graceful no-op when absent — preserves the no-catalog ingest contract).
2. Resolve the owner for the corpus.
3. Look up an existing Document by `(owner, file_path)`. If found, use its tumbler as the doc_id. If not, register a fresh Document and use the new tumbler.
4. Pass the resolved doc_id to `_pdf_chunks(...)` / `_markdown_chunks(...)` via a new keyword parameter.
5. The chunk-prep functions pass it to `make_chunk_metadata(..., doc_id=doc_id)`.

The post-store `fire_post_document_hooks` chain stays in place — it is responsible for triggering aspect-extraction enqueue and other downstream consumers, not for chunk metadata.

The streaming PDF path (`pipeline_stages.pipeline_index_pdf`) has the same shape and gets the same treatment: register the Document before the streaming upload begins, not at the tail.

A pre-flight registration changes one observable: the catalog gains a Document entry even when the staleness check would otherwise skip the upsert. This is intentional — the catalog is the join authority for everything Phase 4+ relies on, and a "skipped re-index" should still result in a Document row that downstream readers can reference. The chunk-level work (embed + upsert) is still skipped on staleness; only the catalog row is materialised.

Verified idempotency for re-registration: `Catalog.register()` (catalog.py:1218-1234) returns the existing tumbler without writing an event when `by_file_path(owner, file_path)` finds an existing row. So re-running `nx index pdf foo.pdf` produces zero new `events.jsonl` lines once `foo.pdf` is registered — only the first registration writes.

**Edge case the audit-trail invariant must accept.** A *first-time* index that is then skipped by the staleness check (rare but possible: identical content_hash already present from a different source path, or a chunk-level dedup elsewhere) emits a `DocumentRegistered` event with no companion `ChunkIndexed` events. This is a valid state — the Document is canonically tracked even though its chunks were de-duplicated against another row's content. The doctor must not flag `DocumentRegistered`-without-`ChunkIndexed` as drift; replay equality is unaffected (the Document row is reproducible from the event). D4 adds a regression test that exercises this scenario explicitly.

### D2 — Stop writing source_path at chunk-write time

The audit's reader inventory (`docs/migration/rdr-101-phase4-reader-audit.md`) classified every `source_path` consumer into three buckets:

- **Migrated (read doc_id first, source_path fallback):** indexer.py incremental-sync, doc_indexer staleness, prose_indexer staleness, code_indexer staleness, search_engine catalog prefilter, link-audit, etc. These already work without source_path.
- **Display formatters:** `formatters._display_path()` already prefers `title` and falls back to source_path; new chunks won't have source_path so display will use title (which is universally populated). No code change required for new chunks.
- **Legacy / out-of-scope reads:** `aspect_promotion._RESERVED` (defensive guard list — kept), `aspect_extractor` LLM prompt template (catalog `file_path` not chunk metadata — out of scope), `exporter.py` filter predicates (operator-facing patterns — kept; `.nxexp` files exported pre-prune still carry source_path for back-compat).

Removing `source_path` from `ALLOWED_TOP_LEVEL` immediately drops it on every write because `normalize()` filters every record by that set. Existing chunks that already carry `source_path` keep it until their next write or the next prune; that matches the reader expectations above.

**Hard-remove the `source_path` parameter from `make_chunk_metadata`.** The first draft of this RDR proposed keeping `source_path` as a deprecated no-op kwarg "for back-compat with external consumers." The substantive-critic gate flagged this as a silent failure mode: a caller passes `source_path=...` expecting it to be stored, the call succeeds, the value is silently dropped, and a downstream reader using `where={"source_path": ...}` returns zero results — the failure is invisible all the way down. There is no demonstrated external consumer (the reader audit covered every `src/` and `tests/` site exhaustively). Hard-removing the parameter forces every call site to be edited in lockstep with a `TypeError`, which is the honest signal. Phase B removes the parameter entirely; the cross-set test (D4 item 1) catches anyone re-introducing it.

A new unit test enforces the invariant going forward: `_PRUNE_DEPRECATED_KEYS ∩ ALLOWED_TOP_LEVEL == ∅`. CI failed to catch the original divergence; this test makes the divergence a build break.

The five `git_*` keys already pass through this gate — they are stripped from `ALLOWED_TOP_LEVEL` and packed into `git_meta` JSON. `source_path` does not need a JSON-pack equivalent; the doc_id is the canonical reference and `source_path` is reproducible by the catalog projection.

### D3 — Doctor surfaces orphan ratio with a soft warn threshold

The PASS gate stays as it is — tightening it would invalidate the production "complete" claim and is a Phase 5 concern. The doctor output gains:

- A new section `=== Orphan ratio ===` printing per-collection: `total_t3 / non_orphan_in_log / orphan_in_log` and a `WARN:` line when `orphan_in_log / (orphan_in_log + non_orphan_in_log) > 0.50`.
- The "Collections in log: N" line clarifies its filter: `Collections with non-orphan ChunkIndexed events: N (total in events.jsonl: M)`.
- The JSON payload gains a `t3_doc_id_coverage.orphan_ratio` field for programmatic consumption.

The threshold (50%) is a soft signal, not a gate. Operators can ignore it for collections they know are orphan-by-design (`docs__stale`, `docs__shakedown_*`, etc.). For knowledge papers and mirrored repos that *should* be tracked, the warn is the visible nudge to file an orphan-recovery action.

### D4 — Validation layer: cross-set test, write-then-verify integration tests

Four test additions, in order of value:

1. `tests/test_metadata_schema.py::test_prune_deprecated_keys_disjoint_from_allowed_top_level` — imports both sets and asserts intersection is empty. CI break on regression.
2. `tests/test_doc_indexer.py::test_*_writes_doc_id_when_catalog_initialized` — three subtests covering `index_pdf`, `index_markdown`, `batch_index_markdowns` (RDR mode). Build a tmp catalog, run the indexer, assert chunk metadata contains `doc_id` AND lacks `source_path`. EphemeralClient + tmp_path SQLite, no network.
3. `tests/test_catalog_doctor.py::test_orphan_ratio_section` — synthesize a coverage report with 80% orphan ratio, assert doctor text contains `WARN:` and the JSON payload contains `orphan_ratio`.
4. `tests/test_doc_indexer.py::test_preflight_registration_idempotent_on_staleness_skip` — index a file once, then re-index without `--force` so staleness skips the chunk upsert. Assert exactly one `DocumentRegistered` event in `events.jsonl` for that file (the first call) and zero on the second. Asserts the idempotency claim in R1 directly. Also asserts `nx catalog doctor --replay-equality` does not flag the orphan-Document state as drift.

### D5 — Orphan recovery: documented operator-runnable path

The substantive-critic gate flagged that adding a WARN signal for collections >50% orphan ratio is operationally hostile if no recovery path exists — operators with `knowledge__art` (5,725 chunks, 0.02% coverage) will see a permanent WARN they cannot resolve via reindex alone, and will train themselves to ignore the doctor.

The recovery path is non-trivial because re-indexing produces a *new* tumbler, while the existing 309K orphan `ChunkIndexed` events in `events.jsonl` carry the synthesize-log UUID7 markers. A naive `nx index pdf` would create a duplicate Document (one tumbler, one UUID7) for the same source file.

This RDR documents the recovery procedure in `docs/migration/rdr-101-phase4-orphan-recovery.md` (companion to this RDR; ships in the same PR set as Phase A):

```bash
# 1. Run synthesize-log in catalog-preferring mode (existing verb, new flag).
#    For each T3 chunk, look up Document by content_hash; if found, replace the
#    orphan UUID7 marker in events.jsonl with the live tumbler. Otherwise leave
#    the orphan marker (we genuinely don't know what Document it belongs to).
nx catalog synthesize-log --prefer-live-catalog --collection <coll>

# 2. Run t3-backfill-doc-id to write the now-resolved doc_id to T3 chunk
#    metadata.
nx catalog t3-backfill-doc-id --collection <coll>

# 3. Re-run doctor; orphan ratio drops to whatever fraction is genuinely
#    unrecoverable (chunks whose source_path is not in any catalog Document).
nx catalog doctor --t3-doc-id-coverage
```

The `--prefer-live-catalog` flag on `synthesize-log` is **new** but small (a few-line edit to the existing verb). Phase A ships it alongside the doc_indexer fixes since the recovery procedure is part of "what an operator running RDR-102 needs to do" and bundling it keeps the rollout one operation rather than two. Operators who do not need orphan recovery (clean catalog) skip step 1 and 2.

This is the documented escape valve for the WARN signal: "see this WARN? run the three commands above to drop it." That converts the WARN from operationally hostile to actionable.

**Phase D gate — write-then-verify, on a fresh collection.** The first draft's gate was "post-sweep dry-run prune returns zero," which is satisfiable trivially: prune everything first, run the dry-run, get zero, declare done — exactly the failure shape that produced the original tautological PASS this RDR is closing. The substantive-critic gate flagged this. The actual gate is **index a fresh file through each fixed path, then verify the resulting chunks carry no `source_path`**:

```bash
# Set up: fresh collection so we measure writers, not pre-prune residue
TEST_COLL="docs__rdr102-gate-$(date +%s)"

# Write through each fixed path
nx index pdf <test-paper.pdf>     --collection knowledge__rdr102-gate
nx index md  <test-md-file.md>    --corpus rdr102-gate
nx index rdr <test-rdr.md>        # standalone path (uses batch_index_markdowns)
nx index repo <test-repo>         # exercises code_indexer + prose_indexer + pipeline_stages

# Verify no writer leaked source_path
for COLL in knowledge__rdr102-gate docs__rdr102-gate code__rdr102-gate rdr__rdr102-gate; do
  nx catalog prune-deprecated-keys --dry-run --collection "$COLL"
  # Required: 'chunks_updated: 0' for source_path on every collection
done

# Verify doc_id is populated on the same fresh writes
nx catalog doctor --t3-doc-id-coverage  # all four collections at 100% (no orphans by construction)
```

A passing gate means: every writer path produces chunks that satisfy the canonical schema (no `source_path`) AND populates `doc_id` (no orphans). Pre-existing chunks on the host catalog are out of scope for this gate — they are the orphan-recovery problem covered by D5 below, not a Phase 4 invariant.

## Implementation Plan

Phased to land in three small PRs, each independently shippable and testable.

### Phase A — doc_indexer pre-flight registration + orphan recovery (closes nexus-u2n9)

Files:

- `src/nexus/doc_indexer.py` — `_register_or_lookup_doc_id()` helper; `_pdf_chunks()` and `_markdown_chunks()` accept `doc_id` kwarg; `_index_document()` and `index_pdf()` register upfront and pass through; `_lookup_existing_doc_id()` becomes the read-only path used by the post-hook chain.
- `src/nexus/pipeline_stages.py` — `pipeline_index_pdf()` registers upfront before the streaming upload; `_catalog_pdf_hook` becomes idempotent for the already-registered case.
- `src/nexus/catalog/synthesizer.py` (or wherever `synthesize-log` lives) — add `--prefer-live-catalog` flag per D5. When set, content_hash matches against live Documents take precedence over synthesized UUID7 orphan markers.
- `docs/migration/rdr-101-phase4-orphan-recovery.md` — companion doc with the three-command recovery procedure.
- `tests/test_doc_indexer.py` — new tests per D4 items 2 and 4.
- `tests/test_pipeline_stages.py` — streaming-path test mirrors the batch test.
- `tests/test_synthesize_log.py` — assert `--prefer-live-catalog` replaces orphan markers when content_hash hits a live Document.

Acceptance: each of `nx index pdf`, `nx index md`, `nx index rdr` (standalone) writes `doc_id` to chunk metadata on first index; verified by EphemeralClient inspection in tests; verified empirically on host catalog with a fresh PDF. Orphan-recovery procedure smoke-tested on `knowledge__delos` (drops the collection's orphan ratio).

### Phase B — write-side source_path retirement (closes nexus-wb3c)

Files:

- `src/nexus/metadata_schema.py` — drop `"source_path"` from `ALLOWED_TOP_LEVEL`; **remove the `source_path` parameter from `make_chunk_metadata()`** (hard delete, not deprecated kwarg — see D2 rationale).
- `src/nexus/code_indexer.py:402` — drop `source_path=str(file_path)` from `make_chunk_metadata` call.
- `src/nexus/prose_indexer.py:103, 183` — drop `source_path=str(file_path)` (two call sites).
- `src/nexus/pipeline_stages.py:145` — drop `source_path=pdf_path` (PDF-in-repo writer; **was missing from the original draft**).
- `src/nexus/doc_indexer.py:794, 874` — drop `source_path=str(pdf_path)` and `source_path=sp` (PDF + markdown standalone chunkers).
- `tests/test_metadata_schema.py` — new test per D4 item 1; existing `test_canonical_shape` updated to assert `source_path` is *absent* from new writes; new test asserting `make_chunk_metadata(source_path=...)` raises `TypeError` (signature contract).
- `tests/test_code_indexer.py`, `tests/test_prose_indexer.py`, `tests/test_doc_indexer.py`, `tests/test_pipeline_stages.py` — assert chunks lack `source_path` after a fresh write through each path.

Acceptance: each of the 6 writer call sites edited and verified; `_PRUNE_DEPRECATED_KEYS ∩ ALLOWED_TOP_LEVEL == ∅` in unit test; the structural gate for Phase B is the write-then-verify sequence in Phase D (not a post-sweep dry-run, which is satisfiable trivially).

### Phase C — doctor visibility (closes nexus-ht8j)

Files:

- `src/nexus/commands/catalog.py:_run_t3_doc_id_coverage` — count `total_t3` and `orphan_in_log` per collection; build `report["orphan_ratio"]` and per-collection `report["per_coll"][name]["orphan_ratio"]`.
- `src/nexus/commands/catalog.py:_print_t3_doc_id_coverage_text` — new "Orphan ratio" section; threshold WARN line; clarified "Collections in log" header.
- `tests/test_catalog_doctor.py` — new test per D4 item 3.

Acceptance: `nx catalog doctor --t3-doc-id-coverage --json | jq '.t3_doc_id_coverage.orphan_ratio'` returns a number; text output shows WARN for `knowledge__art` (~99.98% orphan) on host catalog.

### Phase D — Final validation

After A/B/C are merged:

1. `uv run pytest` — full unit suite green.
2. `uv run pytest -m integration` — integration suite green.
3. **Write-then-verify gate** (per the D4 sequence above): index a fresh file through each of the 4 paths into a freshly-created collection, then dry-run prune those collections. Required: zero `source_path` updates per collection. This is the structural PASS gate — it measures writer correctness on isolated fresh state, not the result of a sweep on pre-existing data.
4. Orphan-recovery smoke (per D5): run the three-command procedure on one previously-orphan collection (e.g., `knowledge__delos`); assert orphan ratio drops measurably.
5. After 1–4 pass, `nexus-o6aa.10` closes for real.

## Alternatives Considered

### A1 — Make `make_chunk_metadata` raise on missing doc_id

Rejected. Hard-failing on missing doc_id breaks tests that don't initialise a catalog and breaks the no-catalog ingest contract documented in the project's local-mode behaviour. The doc_id field stays optional at the schema level; the writers are the policy enforcer.

### A2 — Backfill doc_id post-upsert in the doc_indexer paths

Rejected. The MERGE behaviour of ChromaDB upsert is undocumented and could change. Adding a separate post-upsert `col.update(metadatas=[{"doc_id": ...}])` doubles the write count for every PDF/markdown ingest. The pre-flight registration is the same number of catalog operations (one per file) and produces the doc_id at chunk-prep time.

### A3 — Keep `source_path` as a deprecated no-op kwarg on `make_chunk_metadata`

**Rejected after substantive-critic gate.** The first draft proposed keeping `source_path` as a deprecated parameter (silently dropped at `normalize()`) "for back-compat with external consumers." The critic identified this as a silent failure mode: caller passes `source_path=...`, call succeeds, value is silently discarded, downstream `where={"source_path": ...}` returns zero results — failure is invisible all the way through. There is no demonstrated external consumer (the reader audit covered every site exhaustively).

D2 hard-removes the parameter. The signature changes to `TypeError` on the deprecated argument. Every caller is edited in lockstep within Phase B; the cross-set test catches future re-introduction. This is the "honest signal" approach.

### A4 — Tighten the doctor PASS gate to require <50% orphan ratio

Rejected for this RDR. Tightening would invalidate the host catalog's current PASS, which is operationally awkward and does not advance the actual fix (orphan recovery is a separate, larger effort). The WARN signal is sufficient as a visible nudge.

## Risks and Mitigations

- **R1: Pre-flight registration changes the staleness contract.** A skipped re-index now still produces a `DocumentRegistered` event for first-time-registered files (re-registrations are idempotent at `Catalog.register()` lines 1218-1234). Edge case: a first-time index that is then skipped by the staleness check (identical content_hash already present, e.g. shared content across paths) will produce a `DocumentRegistered` event with no companion `ChunkIndexed` events. The doctor's `--replay-equality` check is unaffected (the Document row is reproducible from the event). **Mitigation:** D4 item 4 adds `test_preflight_registration_idempotent_on_staleness_skip` which asserts (a) one event on first call, zero on second, and (b) doctor `--replay-equality` does not flag the orphan-Document state as drift.
- **R2: Removing `source_path` from `ALLOWED_TOP_LEVEL` breaks readers we missed.** Mitigation: the reader audit was exhaustive (`docs/migration/rdr-101-phase4-reader-audit.md`); RF-4 in this RDR is the verified writer inventory. The cross-set unit test catches future regressions. A grep + targeted unit-test pass before merge confirms no `metadata["source_path"]` reads break with `KeyError` — the existing readers all use `meta.get("source_path", "")` shape.
- **R3: Streaming PDF pipeline registration is racy.** Mitigation: `_catalog_pdf_hook` is already concurrent-safe (catalog SQLite uses WAL + retries). The pipeline upload is gated on a content_hash advisory lock; pre-flight registration happens inside that lock window.
- **R4: Tests against a real catalog can flake on filesystem timing.** Mitigation: tests use `tmp_path` for the catalog directory and `EphemeralClient` for T3, both deterministic. No integration tests in this scope.
- **R5: Phase D gate satisfied trivially via post-sweep.** The first draft's gate ("dry-run prune returns zero") was satisfiable by running `prune-deprecated-keys` first and then dry-run, with zero proof of writer correctness. **Mitigation:** the Phase D gate is now a write-then-verify sequence on freshly-created collections — the dry-run check measures *only chunks the writers under test produced*, not the result of any prior sweep. See D4 gate spec.

## Dependencies and Sequencing

- **Predecessor:** RDR-101 Phase 3 (already complete). Phase 3 shipped doc_id in `code_indexer` and `prose_indexer` via `doc_id_resolver`. This RDR extends the same pattern to `doc_indexer.*` and `pipeline_stages.*`.
- **Blocks:** RDR-101 Phase 5a (`nexus-o6aa.11`, `[catalog].event_sourced` opt-in flag). The actual block is **Phase B + the Phase D write-then-verify gate** — Phase 5a depends on the structural invariant ("new writes are clean") which is established by B/D. Phase C (doctor visibility) is an independent observability enhancement; Phase 5a does not require it. If schedule pressure forces it, Phase 5a can start after B+D and Phase C can land in parallel.
- **Independent of:** Orphan recovery for chunks indexed before this RDR (the synthesize-log `--prefer-live-catalog` recovery procedure). The procedure is shipped as part of D5 but operators may run it independently of the Phase 5a rollout.

## Out of Scope

- ~~Reindexing the 309,681 orphan chunks on the host catalog.~~ **In scope** (added per substantive-critic gate). D5 documents the three-command operator-runnable recovery procedure. The `--prefer-live-catalog` flag on `synthesize-log` ships with Phase A.
- Any change to T1 (scratch) or T2 (notes/plans/aspects/etc.) wiring.
- Phase 5a opt-in flag (`[catalog].event_sourced`), Phase 5b legacy-surface drops (e.g., the source_path readers in the stale-detection fallback shape).
- Any CLI surface change beyond the doctor output and the new `synthesize-log --prefer-live-catalog` flag (`prune-deprecated-keys` and `migrate` commands keep their current signatures).
- Tightening the doctor PASS gate to require <50% orphan ratio. Tightening would invalidate the host catalog's current PASS state and is a Phase 5 concern. The WARN signal added by D3, paired with the recovery procedure documented by D5, is the actionable signal for this RDR's scope.
