---
title: "Note-Backed Document Identity in the Aspect Pipeline: Stop Unmappable Aspect Orphans from MCP-Stored Knowledge Notes"
id: RDR-145
type: Architecture
status: accepted
priority: medium
author: Hal Hildebrand
reviewed-by: self
created: 2026-06-02
accepted_date: 2026-06-27
related_issues: [nexus-pfzgb]
related_rdrs: [RDR-089, RDR-096, RDR-101, RDR-108, RDR-142]
supersedes: []
related_tests: []
implementation_notes: ""
---

# RDR-145: Note-Backed Document Identity in the Aspect Pipeline: Stop Unmappable Aspect Orphans from MCP-Stored Knowledge Notes

> Revise during planning; lock at implementation.

## Problem Statement

A live-on-prod shakedown (2026-06-02, conexus 5.8.0) found the RDR-108 Phase-1c migration (switch `document_aspects` PK from `(collection, source_path)` to `(doc_id)`) permanently blocked by 296 high-volume unmapped orphan rows (`doc_id=''`). Root-causing them revealed two distinct, independent defects, one of which is a *structural, ongoing* gap rather than legacy debt.

### Enumerated gaps to close

#### Gap 1: MCP-stored knowledge notes have no chunk manifest, so their aspects are unmappable by construction

`store_put`-created knowledge notes (e.g. `knowledge__knowledge`, `knowledge__wow-addon-dev`) are registered as catalog `documents` with empty `file_path` and empty `source_uri` (title-only identity), and have **zero** rows in the catalog `document_chunks` manifest. RDR-089 aspect extraction nonetheless fires on these `knowledge__*` collections and writes `document_aspects` rows whose `source_path` is a 32-char chunk hash (chash). The RDR-108 backfill (`_backfill_doc_ids_via_catalog`, `migrations.py`) maps `doc_id` by joining `(collection, source_path) = documents.(physical_collection, file_path)` and a supersede chain. For note-backed docs there is **no joinable key**: `file_path` is empty, and the aspect's chash is absent from `document_chunks` entirely (verified: 0/111 resolve). These rows can never get a `doc_id`, and because extraction is ongoing they accumulate continuously (orphan `extracted_at` runs up to the shakedown date).

#### Gap 2: Legacy/contaminated absolute-path `source_path` in file-backed aspect rows

177 orphan rows carry an absolute `source_path` that does not match the catalog's stored `file_path` for the same physical collection: sibling-clone/worktree paths (`/Users/.../git/nexus-a2ui-patterns`, `nexus-rdr-125`, `nexus-rdr139/141`, `nexus-shakeout`, `Luciferase-rdr-pyramid`), canonical-but-absolute nexus paths where the catalog stores *relative* paths, and stale DEVONthink DB paths (`Constantine.dtBase2` vs the catalog's `Inbox.dtBase2`). This is the `nexus-3e4s` source-path-normalization class. Most are `rdr__*` (not the supported aspect surface per RDR-089) or re-extractable knowledge papers. `rdr__1-1` has 104 orphans and **0 mapped** rows.

#### Gap 3: Aspect-extraction surface includes non-paper note collections

RDR-089 fields are research-paper aspects (`problem_formulation`, `proposed_method`, `experimental_datasets`, `experimental_baselines`, `experimental_results`). `knowledge__knowledge` holds arbitrary notes (RDR analyses, session memories). Extracting paper-aspects from these is likely low-value noise and is the *source* of the Gap-1 orphan stream. Whether note collections should be in the aspect surface at all is an open scoping question.

## Context

### Background

Discovered during the 5.6-5.8 prod shakedown (T2: `nexus/prod-shakedown-5.6-5.8-2026-06-02`). `nx upgrade --dry-run` reports the deferred RDR-108 step; the daemon runs fine today because the version-gate skips it (this is the RDR-142 "version advances while a gated step remains" state). The gate only bites on a forced migration or a fresh daemon bootstrap (see `nexus-3lbhb`, degrade-not-crash).

### Technical Environment

- `document_aspects` (T2/SQLite): PK `(collection, source_path)`, `doc_id TEXT NOT NULL DEFAULT ''`, `source_uri`. 620 rows on prod: 320 mapped, 300 unmapped.
- Catalog (`.catalog.db`): `documents(tumbler, physical_collection, file_path, source_uri, title, ...)`, `document_chunks(doc_id, position, chash, ...)` with `chash` 32-char (the Chroma natural id). Join key for RDR-108 backfill.
- Migration backfill + high-volume gate: `migrations.py` `_backfill_doc_ids_via_catalog`, `_check_high_volume_orphans` (threshold `_HIGH_VOLUME_ORPHAN_THRESHOLD=10`, env override `NEXUS_MIGRATION_HIGH_VOLUME_THRESHOLD`), `_hard_delete_unmapped`.
- Aspect write path: RDR-089 `aspect_worker.py` / `aspect_extractor.py`; `knowledge__*`-gated.

## Research Findings

### Investigation

Read-only queries against the live prod T2 (`~/.config/nexus/memory.db`) and catalog (`~/.config/nexus/catalog/.catalog.db`), plus `migrations.py` backfill/gate logic. Full evidence in T2 `nexus/prod-shakedown-5.6-5.8-2026-06-02`.

### Key Discoveries

- **Verified** — Bucket A (note-identity) is 122 rows: `knowledge__knowledge` (111) + `knowledge__wow-addon-dev` (11). `source_path` is a 32-hex chash; catalog docs for these collections have empty `source_uri` AND empty `file_path`.
- **Verified** — The orphan chash `0792a1aae63d0c0dd898b29ebee732f6` (and the set) resolves 0 times in `document_chunks` (exact or prefix), and the 120 catalog `knowledge__knowledge` docs have **0** `document_chunks` rows. No join key exists.
- **Verified** — `knowledge__knowledge` has 0 mapped aspect rows; orphan `extracted_at` runs to 2026-06-02 (ongoing accumulation, not legacy).
- **Verified** — Bucket B/C is 177 rows of absolute-path `source_path` mismatching the catalog's relative/`Inbox` paths; `rdr__1-1` is 104 orphans / 0 mapped.
- **Documented** — RDR-108's own plan already specifies hard-deleting test-fixture + low-count unmapped orphans and backfilling the rest; the >10 gate is an operator-review safety pause, which is where we are.

### Critical Assumptions

- [x] **CA-1: Note-backed aspects are genuinely unrecoverable by any existing key.** — **Status**: REFUTED (partial) — **Method**: Source Search (2026-06-02). A title-based resolver DOES exist: `_resolve_doc_id` → `lookup_doc_id_by_collection_and_path(collection, title)` matches on `title` (`catalog.py:1803`) and returns the note's `metadata.doc_id` (the chash). The `nx enrich aspects` CLI path reaches it (`enrich.py:1125` derives `source_path = file_path or title`). Only the *migration backfill* misses it — it joins `file_path` exclusively (`migrations.py:1410-1433`), never `title`/`source_uri`/chash. Caveat: the resolved `doc_id` is the chash, not a catalog tumbler (note docs are chash-keyed; deeper identity inconsistency).
- [ ] **CA-2: Dropping unmapped aspects is non-destructive given regeneration.** — **Status**: PARTIAL — **Method**: Source Search (Spike still useful). CLI re-extraction recovers catalog-enumerable notes to `doc_id=<chash>` via the title path, but new MCP `store_put` aspects keep writing synthetic-URI orphans → drop-then-reextract is non-destructive but **not curative on its own** (treadmill persists unless the write/enqueue path is fixed).
- [x] **CA-3: The ongoing orphan stream originates at the aspect-write path for note-backed docs.** — **Status**: VERIFIED (sharpened) — **Method**: Source Search (2026-06-02). Confirmed origin. Sharper root cause: the enqueue passes the **chash as `source_path`** (`mcp/core.py:1139-1144`, "source_path is doc_id here"), so `_resolve_doc_id`'s title probe is fed the chash, not the note title → no match → falls through to `source_uri = chroma://<coll>/<chash>`. Post-RDR-108 rows now land `doc_id="chroma://…/<chash>"` (synthetic URI); pre-migration the same defect produced `doc_id=''`.
- [x] **CA-4: Tightening the aspect surface to exclude note collections does not regress intended RDR-089 paper coverage.** — **Status**: VERIFIED — **Method**: Source Search (2026-06-02). Gate is `select_config` (`aspect_extractor.py:624-641`) over `_REGISTRY` prefixes `knowledge__` + `rdr__` — pure prefix match, no note/paper distinction; `knowledge__knowledge` (default MCP target) is in-surface. Paper collections (`dt-papers`, `augur-oracle-papers`) are distinct, file-backed names populated via `nx index pdf`; excluding `knowledge__knowledge` by name (or gating on file-backed/`file_path` present) loses zero paper coverage.

- [~] **CA-5: RDR-172 closes Gap-1's ongoing-orphan stream for the `store_put` path — VERIFIED ON THE FIX BRANCH; CLOSES ON MERGE.** — **Status**: VERIFIED-ON-BRANCH, MERGE-PENDING — **Method**: Source Search (2026-06-27). RDR-172 P1.1 (`nexus-pyn35`) changes `store_put` to forward `catalog_doc_id` — the real catalog tumbler returned by `catalog_store_hook` (`store_hook.py:85-90`, which registers note-backed docs with a `documents.tumbler` and returns it) — as `fire_document`'s `doc_id` kwarg, instead of the chash. So new note aspects via `store_put` carry a mappable tumbler `doc_id` at write time (no backfill; FK-satisfied in service mode). **IMPORTANT (gate Layer-3 correction):** this fix is on branch `feature/nexus-pyn35-client-catalog-doc-id` (commit `2f0ad7ab`, PR #1337) — it is **reviewed (stacked) + test-validated (full suite green) but NOT YET MERGED to `develop`.** `develop` HEAD still has `mcp/core.py:2068` = `doc_id=doc_id` (the chash). Therefore **RDR-145's one-time cleanup (Approach 4) MUST NOT run until RDR-172 is merged to `develop` and `core.py:2068` is verified to read `doc_id=catalog_doc_id` on develop HEAD** — otherwise the stream continues and the RDR-108 migration re-blocks immediately. CA-5 is a hard prerequisite, not a settled fact. CA-3's "chash-as-source_path bypasses the resolver" defect is moot for `doc_id` post-merge (source_path stays the chash but is not the mapped key; the PK migration switches PK to `doc_id`).

- [x] **CA-6: Gap-3's noise concern is already closed by `nexus-kmbys` (shape-aware routing).** — **Status**: VERIFIED — **Method**: Source Search (2026-06-27). `nexus-kmbys` (closed 2026-06-26, commit c79909c4) shipped exactly the Gap-3 "hybrid: shape-aware extraction" decision: `_classify_document_shape` (deterministic, LLM-free; `aspect_extractor.py:778`) classifies each doc paper/prose, and `_resolve_config_for_document` (`:796`) is wired into **both** extract paths — single-doc (`:1062`) and batch-partition (`:1235`) — routing paper-shaped `knowledge__` docs to `scholarly-paper-v1` and prose/note-shaped docs to `general-prose-v1` (`:337`, haiku; required fields are prose-only, datasets/baselines always `[]`, so it never fabricates paper structure on notes). Stacked-reviewed, full suite green. So note-shaped knowledge aspects are already extracted with a schema that fits — Gap-3's "paper-aspect noise on arbitrary notes" is resolved. Residual: the prose extractor still runs a (cheap, async, haiku) extraction per note — a *cost* lever, not a correctness gap; "skip note extraction entirely" remains a future option if that cost ever matters.

**Design impact (post-RDR-172):** the ongoing-accumulation half of Gap-1 is **CLOSED by RDR-172** — the dominant note path (`store_put`) now writes a mappable tumbler. RDR-145's remaining scope reduces to three items: **(1) Gap-3 — the surface-eligibility decision** (should `knowledge__` note collections be in the aspect-extraction surface at all? CA-4: excluding costs zero paper coverage; this is the decision `nexus-hpzgo` / RDR-172 Phase 2 waits on); **(2) Gap-2 — absolute-path `source_path` normalization** for file-backed rows; **(3) one-time cleanup** of the ~296 pre-RDR-172 orphans that still block the RDR-108 PK migration (unmappable + regenerable per CA-2 → drop during the migration, per RDR-108's documented intent). Full evidence: T2 `nexus_rdr/145-research-CA1-CA4`, `nexus_rdr/145-research-2`.

## Proposed Solution

### Approach (locked)

Post-research, two of the three gaps are **already closed by landed work**, so the remaining scope is a one-time cleanup plus a forward defense. The Gap-3 surface decision was locked to **"hybrid: shape-aware extraction"** (Hal, 2026-06-27) — which `nexus-kmbys` already implements (CA-6).

0. **HARD PREREQUISITE — RDR-172 merged to `develop` (CA-5): ✅ SATISFIED 2026-06-27.** RDR-172 PR #1337 merged to `develop`; `mcp/core.py:2081` now forwards `doc_id=catalog_doc_id` (verified on develop HEAD). The write-path is correct, so the cleanup (item 4) and the "now-correct write path" assumption hold. (Kept as item 0 for the record: re-verify on the develop tip in force at implementation time.)
1. **Gap 1 — note identity at write time: closed by RDR-172 ON MERGE (no work here).** `store_put` forwards the catalog tumbler as the aspect `doc_id` (CA-5), so new note aspects are mappable + FK-satisfied. RDR-145 inherits this; it does not re-implement it. (Inherited, not landed-on-develop yet — see item 0.)
2. **Gap 3 — surface eligibility: DECIDED "shape-aware routing", already shipped by `nexus-kmbys` (CA-6).** Note-shaped `knowledge__` docs route to `general-prose-v1`, paper-shaped to `scholarly-paper-v1`. RDR-145's only Gap-3 work is a **regression test** that a representative `knowledge__knowledge` note routes to `general-prose-v1` (not the paper extractor) AND that the resulting aspect has `experimental_datasets == []`, `experimental_baselines == []`, and `experimental_results` empty/None (the `general-prose-v1` schema contract, `aspect_extractor.py:337` — the machine-checkable form of "non-fabricated") — locking the decision against regression. (Cost lever "skip note extraction entirely" is explicitly deferred — not adopted; the shipped haiku light extractor is the chosen behavior, and every note `store_put` pays one async haiku extraction. Acknowledged, accepted.)
3. **Gap 2 — absolute-path `source_path` contamination: BUILD (mechanism locked).** In `aspect_extraction_enqueue_hook` (`src/nexus/aspect_worker.py`), before writing the queue row, resolve `source_path` against the catalog: `lookup_doc_id_by_collection_and_path(collection, source_path)` / the catalog's canonical `file_path` for that physical collection; if found and the stored form is relative, replace `source_path` with the catalog's canonical relative form; if not found, leave as-is and `log.warning("aspect_source_path_uncanonical", ...)` (never silently rewrite to a guessed path — that would re-introduce the `nexus-3e4s` class). Forward-only defense; does not touch existing rows.
4. **One-time orphan cleanup: BUILD (map-what-you-can, then drop; cover synthetic-URI rows).** The pre-fix orphans (snapshot 296 = 122 note-identity + 177 absolute-path on 2026-06-02; the count GROWS until item 0 lands) block the RDR-108 Phase-1c PK migration. Cleanup steps, in order: (a) **map-what-you-can** — for Bucket-B rows whose `source_path` still resolves to an accessible file or a catalog entry, normalize + backfill `doc_id` (recoverable, not dropped); (b) **probe + sign-off** — for Bucket-B rows whose file is gone (stale sibling-clone / renamed DEVONthink DB), these are unrecoverable; report the exact count and require explicit operator sign-off before deletion (no silent permanent loss); (c) **drop the genuinely-unmappable remainder** during the migration, aligned with RDR-108's documented intent. **The delete + the high-volume gate predicate MUST cover BOTH `doc_id = ''` AND `doc_id LIKE 'chroma://%'`** (`migrations.py` `_hard_delete_unmapped` / `_check_high_volume_orphans` currently filter only `doc_id=''`; CA-3 confirmed post-RDR-108 orphans land as synthetic `chroma://…/<chash>` URIs that would otherwise survive the gate and delete, leaving permanently-corrupt rows). Then let the now-correct write path (item 0) + `nx enrich aspects` repopulate live collections.

### Decision Rationale

The investigation collapsed the original "data-model redesign" into a cleanup. The two structural gaps that motivated this RDR — the unmappable note-identity stream (Gap 1) and paper-aspect noise on notes (Gap 3) — were closed by RDR-172 (write-time tumbler identity) and `nexus-kmbys` (shape-aware routing) respectively. The bead's original "add a backfill pass" framing is ruled out (CA-1/CA-2: no join key for the legacy orphans, and they are regenerable). What remains is genuinely a one-time migration cleanup (drop the legacy orphans) plus a forward `source_path` normalization (Gap 2) and a regression test that pins the shipped Gap-3 routing. The "shape-aware" surface decision (vs blunt exclusion) keeps note aspects available for recall while the cheap haiku prose extractor avoids both hallucination and the cost of the heavy scholarly path.

## Alternatives Considered

### Alternative 1: Drop-only, accept the treadmill

**Description**: Hard-delete orphans to unblock the migration; do nothing about the source.

**Cons**: Knowledge-note aspects re-orphan continuously; the migration re-blocks on the next run. Treats the symptom.

**Reason for rejection (provisional)**: Leaves the structural gap (Gap 1/3) open; chosen against during brainstorming.

### Alternative 2: Add a chash/title backfill pass (the bead's original framing)

**Description**: Extend `_backfill_doc_ids_via_catalog` with a note-identity pass.

**Reason for rejection**: Verified infeasible — note chashes are absent from `document_chunks` and catalog notes have no `file_path`/`source_uri`; there is nothing to join on.

### Briefly Rejected

- **Fold into RDR-142**: RDR-142 is specifically the dry-run/version-row mechanism; this is a distinct data-model/pipeline gap.

## Trade-offs

### Failure Modes

- Visible: forced migration or fresh daemon bootstrap raises `MigrationError` on the high-volume gate (today's state).
- Silent: knowledge-note aspects accumulate as `doc_id=''` orphans indefinitely; no current surface alerts on the count beyond `nx upgrade --dry-run`.

## Implementation Plan

### Prerequisites

- [x] CA-1 (REFUTED-partial), CA-2 (PARTIAL), CA-3 (VERIFIED), CA-4 (VERIFIED), CA-6 (VERIFIED) — research complete.
- [ ] **CA-5 — RDR-172 (PR #1337) merged to `develop` and `mcp/core.py` verified forwarding `doc_id=catalog_doc_id` on develop HEAD.** HARD gate on the cleanup (Approach 0/4). VERIFIED-on-branch, merge-pending.
- [ ] CA-2 spike (optional): confirm per-bucket regenerability before the drop (Approach 4b probe).

### Minimum Viable Validation

After the fix: `nx upgrade --dry-run` reports **0** deferred/gated RDR-108 steps on a prod-like fixture, AND a freshly extracted aspect for a note-backed `knowledge__*` doc lands with a non-empty `doc_id` (or is correctly excluded from extraction).

## Test Plan

- **Scenario**: note-backed doc gets an aspect extracted — **Verify**: row has resolvable `doc_id` OR collection is excluded from the aspect surface (per locked decision).
- **Scenario**: migration over a fixture with note-identity + absolute-path orphans — **Verify**: orphans hard-deleted (or mapped), PK migration completes, no `MigrationError`.
- **Scenario**: file-backed aspect written from a non-canonical CWD — **Verify**: `source_path` normalized to the catalog's canonical relative form (Gap 2 defense).

## Finalization Gate

### Contradiction Check

- **vs RDR-108** (the PK migration + `_hard_delete_unmapped`/`_check_high_volume_orphans` this unblocks): consistent — RDR-108 documents hard-deleting low-count/test-fixture orphans + backfilling the rest; RDR-145 extends the *predicate* (add `chroma://%`) and adds a map-what-you-can + sign-off step. No contradiction; an extension.
- **vs RDR-172** (the write-path fix): consistent and now explicitly *dependent* (Approach 0 / CA-5). RDR-172 leaves `source_path` as the chash and fixes only `doc_id`; RDR-145 Gap-2 normalizes `source_path` for *file-backed* rows (a different population than note rows, whose source_path is a chash by design). No conflict — they touch disjoint identity fields/populations.
- **vs RDR-089** (aspect surface): consistent — Gap-3 keeps `knowledge__` in-surface via shape-aware routing (kmbys), no surface removal that would regress paper coverage (CA-4).
- **vs RDR-142** (version-advances-while-gated): the cleanup is what lets the gated RDR-108 step finally complete; it does not advance the version row independently. Consistent.

## References

- T2: `nexus/prod-shakedown-5.6-5.8-2026-06-02` (full orphan evidence), `nexus/prod-shakedown` deep-dive queries.
- Bead `nexus-pfzgb`.
- RDR-089 (aspect extraction), RDR-108 (graph identity normalization, backfill + gate), RDR-096 (URI source identity), RDR-101 (immutable document identity), RDR-142 (migration completeness vs version row).
- Related beads: `nexus-3lbhb` (degrade-not-crash on gated MigrationError), `nexus-1714` (flag legacy metadata).
- `migrations.py` `_backfill_doc_ids_via_catalog`, `_check_high_volume_orphans`, `_hard_delete_unmapped`.

## Revision History

- 2026-06-27 (gate Layer-3 fixes): corrected CA-5 from "landed" to VERIFIED-ON-BRANCH/MERGE-PENDING (RDR-172 PR #1337 not yet on develop) and made it a HARD prerequisite (Approach 0); extended the orphan-cleanup predicate to cover synthetic-URI rows (`doc_id LIKE 'chroma://%'`, CA-3) — they would otherwise survive `_hard_delete_unmapped`/`_check_high_volume_orphans` (`doc_id=''`-only); added map-what-you-can + file-existence probe + operator sign-off for unrecoverable Bucket-B rows (no silent loss); locked the Gap-2 normalization mechanism (function + algorithm); made the Gap-3 regression test field-level (`datasets/baselines/results == []`); updated prerequisites + Contradiction Check. Critic verdict pre-fix: BLOCKED (2 Critical).
- 2026-06-27: Research refresh (CA-5, CA-6) + Gap-3 decision locked + Approach locked. **CA-5 VERIFIED** — RDR-172 (landed) closes Gap-1's ongoing stream: `store_put` forwards the catalog tumbler as the aspect `doc_id`, so new note aspects are mappable. **CA-6 VERIFIED** — Gap-3's noise concern was already closed by `nexus-kmbys` (shape-aware routing to `general-prose-v1`). Gap-3 decision locked to **"hybrid: shape-aware extraction"** (Hal), which is the shipped behavior. Scope collapsed: Gap-1 + Gap-3 closed by landed work; remaining = Gap-2 `source_path` normalization + one-time RDR-108 orphan cleanup + a Gap-3 regression test. Evidence: T2 `nexus_rdr/145-research-2`.
- 2026-06-02: Research pass (CA-1..CA-4). **CA-1 REFUTED (partial)** — a title→doc_id resolver exists; only the migration backfill misses it. **CA-3 VERIFIED + sharpened** — root cause is the enqueue passing chash-as-source_path, bypassing the title resolver; post-migration orphans are synthetic `chroma://` URIs. **CA-2 PARTIAL** — drop+reextract non-destructive but not curative alone. **CA-4 VERIFIED** — excluding note collections costs no paper coverage. Design reframed: not "must drop"; two clean root-cause fixes (enqueue-title resolver reuse, or surface exclusion). Evidence: T2 `nexus_rdr/145-research-CA1-CA4` (two codebase-deep-analyzer source-search passes).
- 2026-06-02: Draft. Originated from the 5.6-5.8 prod shakedown. The deferred RDR-108 Phase-1c migration is blocked by 300 unmapped `document_aspects` orphans, root-caused into (Gap 1) structurally-unmappable note-backed aspects with ongoing accumulation, (Gap 2) absolute-path `source_path` contamination, and (Gap 3) the aspect surface including non-paper note collections. Brainstorming gate chose "escalate to RDR" after verifying the bead's original backfill-pass framing was infeasible (note chashes absent from `document_chunks`). Supersedes the implementation framing of `nexus-pfzgb`. CA-1..CA-4 pending research.
