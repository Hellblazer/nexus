---
title: "Note-Backed Document Identity in the Aspect Pipeline: Stop Unmappable Aspect Orphans from MCP-Stored Knowledge Notes"
id: RDR-145
type: Architecture
status: draft
priority: medium
author: Hal Hildebrand
reviewed-by: self
created: 2026-06-02
accepted_date:
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

**Design impact (post-research):** the draft's "unmappable, must drop" premise is wrong (CA-1). Two clean root-cause fixes now in play — **(A)** enqueue the note title (or resolve `doc_id` at write time) so the *existing* title resolver maps note aspects; **(B)** exclude non-paper note collections from the aspect surface (CA-4 — paper-aspects on arbitrary notes are noise). Both stop the stream; (B) is simpler. Gap-2 (absolute-path contamination) remains a separate `source_path`-normalization fix. Full evidence: T2 `nexus_rdr/145-research-CA1-CA4`.

## Proposed Solution

### Approach

_Draft — not locked. Two coordinated fixes, plus a one-time cleanup:_

1. **Stop the source (Gap 1 + Gap 3):** decide whether note-backed `knowledge__*` collections belong in the aspect-extraction surface. If they do, give note-backed docs a resolvable identity at write time (so aspects carry a mappable `doc_id`); if they do not, gate them out of extraction. Either way the ongoing orphan stream stops.
2. **Resolve the migration block (Gap 1 + Gap 2):** since the existing orphans are unmappable by any key and aspects are regenerable (CA-2), hard-delete the unmapped orphans during the RDR-108 Phase-1c migration (aligned with RDR-108's documented intent), then re-extract for live paper collections.
3. **Defend Gap 2 going forward:** normalize `source_path` to the catalog's relative/canonical form at the aspect-write path so file-backed aspects stop contaminating on absolute/sibling-clone paths.

### Decision Rationale

_To be completed at gate. The investigation already rules out the bead's original "add a backfill pass" framing: there is no key to backfill on for note-backed docs._

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

- [ ] CA-1..CA-4 verified.

### Minimum Viable Validation

After the fix: `nx upgrade --dry-run` reports **0** deferred/gated RDR-108 steps on a prod-like fixture, AND a freshly extracted aspect for a note-backed `knowledge__*` doc lands with a non-empty `doc_id` (or is correctly excluded from extraction).

## Test Plan

- **Scenario**: note-backed doc gets an aspect extracted — **Verify**: row has resolvable `doc_id` OR collection is excluded from the aspect surface (per locked decision).
- **Scenario**: migration over a fixture with note-identity + absolute-path orphans — **Verify**: orphans hard-deleted (or mapped), PK migration completes, no `MigrationError`.
- **Scenario**: file-backed aspect written from a non-canonical CWD — **Verify**: `source_path` normalized to the catalog's canonical relative form (Gap 2 defense).

## Finalization Gate

### Contradiction Check

_To complete at gate._

## References

- T2: `nexus/prod-shakedown-5.6-5.8-2026-06-02` (full orphan evidence), `nexus/prod-shakedown` deep-dive queries.
- Bead `nexus-pfzgb`.
- RDR-089 (aspect extraction), RDR-108 (graph identity normalization, backfill + gate), RDR-096 (URI source identity), RDR-101 (immutable document identity), RDR-142 (migration completeness vs version row).
- Related beads: `nexus-3lbhb` (degrade-not-crash on gated MigrationError), `nexus-1714` (flag legacy metadata).
- `migrations.py` `_backfill_doc_ids_via_catalog`, `_check_high_volume_orphans`, `_hard_delete_unmapped`.

## Revision History

- 2026-06-02: Research pass (CA-1..CA-4). **CA-1 REFUTED (partial)** — a title→doc_id resolver exists; only the migration backfill misses it. **CA-3 VERIFIED + sharpened** — root cause is the enqueue passing chash-as-source_path, bypassing the title resolver; post-migration orphans are synthetic `chroma://` URIs. **CA-2 PARTIAL** — drop+reextract non-destructive but not curative alone. **CA-4 VERIFIED** — excluding note collections costs no paper coverage. Design reframed: not "must drop"; two clean root-cause fixes (enqueue-title resolver reuse, or surface exclusion). Evidence: T2 `nexus_rdr/145-research-CA1-CA4` (two codebase-deep-analyzer source-search passes).
- 2026-06-02: Draft. Originated from the 5.6-5.8 prod shakedown. The deferred RDR-108 Phase-1c migration is blocked by 300 unmapped `document_aspects` orphans, root-caused into (Gap 1) structurally-unmappable note-backed aspects with ongoing accumulation, (Gap 2) absolute-path `source_path` contamination, and (Gap 3) the aspect surface including non-paper note collections. Brainstorming gate chose "escalate to RDR" after verifying the bead's original backfill-pass framing was infeasible (note chashes absent from `document_chunks`). Supersedes the implementation framing of `nexus-pfzgb`. CA-1..CA-4 pending research.
