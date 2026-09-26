---
title: "Superseded store_put note chunks are permanently live: the manifest-less-is-live contract outlived its transition"
id: RDR-192
type: Bug Fix
status: draft
priority: high
author: Hal Hildebrand
reviewed-by: self (solo)
created: 2026-08-12
revised: 2026-09-26
accepted_date:
related_issues: [nexus-39upx, nexus-b6enc, nexus-kgos1, nexus-g6k6b, nexus-bb6n2, nexus-2x9xa, nexus-iygza]
---

# RDR-192: Superseded store_put note chunks are permanently live

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

Re-putting an MCP `store_put` / `nx store put` note under the same
`(collection, title)` now reaps the superseded chunk correctly on the happy
path: `nexus-bb6n2` (`f908ae5c3`, shipped 7.58.0) wired `store_hook.py`'s
manifest-replace to a diff-and-reap step, closing the specific defect this RDR
was originally filed for. Re-verified against develop `135bb38a4` / engine
`v0.1.132`, 2026-09-26.

What survives is bigger than the note-only case the title still names. The
underlying question — *is this T3 chunk visible to any search, delete, or
sweep path* — is answered **nine different ways** across the client and the
engine, and those nine answers reduce to only **three** distinct positions.
Every path that computes an answer fails open, and at least one of them
throws away its own result on failure instead of retrying or recording it.
The `manifest-less-is-live` contract this RDR originally targeted for notes
was written to protect one specific overload (a note, by design, has no
manifest row); it now silently protects every other manifest-less chunk in
the store too — rename leftovers, quarantine rows, partial documents from an
interrupted indexing run, `.nxexp` imports, and, still, superseded content
whenever a sweep fails partway through.

Raw vector search (`nx search`, `search()`) is where this becomes a
correctness bug rather than a disk-space one: catalog-aware paths (`query()`,
`search_metadata_scoped`, `search_aspect_scoped`, `search_graph_hop`) already
join the manifest and see only current content. Raw search reads T3 directly
and returns whatever the nine predicates leave lying around. The original
field observation — three corrected knowledge entries whose pre-correction
chunks outranked their own corrections, 0.3220 against 0.4081, lower distance
winning — is unchanged in kind: it is no longer produced by the missing sweep
wiring (that part is fixed), but by every one of the remaining fail-open
paths that can still leave a manifest-less chunk searchable.

### Enumerated gaps to close

#### Gap 1: `manifest-less-is-live` is defined nine ways, with three answers

`service/src/main/resources/db/changelog/catalog-003-soft-delete.xml:33-39`
states one rule — a chunk with no manifest row is live — but nine
independent sites in the client and engine each implement their own version
of "is this chunk current," and they disagree:

1. **Search/get visibility** — `PgVectorRepository.liveChunksCondition`
   (`PgVectorRepository.java:3624`) and the `vectors-017-1` /
   `vectors-017-2` dead-set logic: a chunk is hidden only if it has an
   own-collection manifest row AND every owner referenced by that row is
   tombstoned. A manifest-less chunk is vacuously visible.
2. **`nexus.live_chunks` view** (`vectors-005-repoint-functions-views.xml:228-252`):
   visible if it has NO manifest row at all (tenant-wide, any collection) OR
   at least one live-doc manifest row (also tenant-wide). Not
   collection-scoped, unlike every other predicate here — see Gap 5.
3. **`purge_trash` step 1** (`vectors-017-3`, ~1066-1101) and
   `CatalogRepository.strandedChunkCount` (`CatalogRepository.java:2401-2439`):
   a chunk is a sweep candidate only if it has an own-collection manifest row
   AND none of that row's owners are live or recently tombstoned.
   Manifest-less chunks are never candidates.
4. **Engine superseded sweep**, `sweepChunksQuery`
   (`CatalogRepository.java:5904-5949`): deletes a dropped chash only if it
   has NO manifest row at all (tenant-wide, any collection, tombstones
   included) AND no live note-shaped document in the same physical
   collection claims it via `meta.doc_id`.
5. **Client guards**, `indexer_utils.orphaned_chashes` (113-228,
   collection-scoped, fail-open) and `live_note_chashes` (317): feed
   `mcp_infra._sweep_superseded_vectors` (2204), `_sweep_superseded_vectors_many`
   (2320), and `store_hook._reap_superseded_note_chunks` (1119-1190).
6. **`PgVectorRepository.delete`'s anti-join** (~2508-2600): refuses to
   delete any id carrying an own-collection manifest row, tombstoned owners
   included.
7. **`gc_quarantine_orphans`** (`hygiene-005-1`, line 116ff; bounded variant
   `catalog-037-1`): a chunk is an orphan if it has NO own-collection
   manifest row, in any owner state. No notes guard. Its only caller,
   `indexer._prune_deleted_files` (`indexer.py:6225` -> `3677ff`), scopes it
   to the repo's `code`/`docs`/`rdr` collections.
8. **`nx t3 gc`** (`commands/t3.py:430-540`): a chunk is a candidate if its
   chash is absent from both the collection's tombstone-inclusive chash set
   and `live_note_chashes`, AND its `indexed_at` predates `--orphan-window`
   (default 30 days), AND the collection has no in-flight (`index_state !=
   'complete'`) document.
9. **`taxonomy_unassigned_chashes`** (`taxonomy-019`, new in v0.1.132): only
   considers chunks that already carry an own-collection manifest row, in
   any owner state; a manifest-less chunk is invisible to it either way.

These collapse to three families: **manifest-less is LIVE** at {1, 2, 3,
4's-notes-arm, 5's-notes-arm}; **manifest-less is DEAD** at {7, 8 minus note
identities, 9}; predicate 6 answers a different question (delete-time
protection, kept separate by design — see Trade-offs). Scope also disagrees:
predicate 2 and predicate 4's union guard are tenant-wide; every other
predicate is collection-scoped. A chunk can therefore be simultaneously
"live" under search, "dead" under `nx t3 gc`, and outside `nexus.live_chunks`'s
own collection boundary — three different verdicts about the same row, with
no single place that reconciles them.

#### Gap 2: Raw vector search bypasses the manifest

Unchanged from the original filing, and confirmed still true: predicate 1
above governs raw `search()`/`get()`. Every catalog-aware path already joins
the manifest and returns only current content; the entire hazard lives in
raw search reading T3 directly. This is why a missed reap anywhere in the
nine-predicate surface is a correctness bug and not a disk-space bug —
making retrieval current-aware fixes it for every stale chunk already in the
store, with no deletion and no risk of removing live knowledge.

#### Gap 3: The silent note-guard skip was carried into the new reap path

The original defect — a sweep that silently returns nothing when its own
note guard removes every candidate — was not fixed when `nexus-bb6n2` wired
the store_put reap; it was copied. `mcp_infra.py:2287-2288` (per-document
sweep) and `:2437-2438` (batch sweep) still filter and return with no log
line on empty. `store_hook.py:1172-1173` (the `bb6n2` reap's own note-guard
filter) and `:1159-1160` (its all-shared-chash early return) do the same.
Both reproduce the shape of `nexus-kgos1`: *"It had never deleted a row. The
silence is what hid it."* The engine's own sweep does not have this defect —
`CatalogRepository.java:5756` logs `kept` counts on every run — which means
the client had a correct model sitting beside the code it was editing and
did not follow it.

#### Gap 4: The engine sweep loses its own drop set on failure

`writeManifestMany(sweep=true)` (`CatalogRepository.java:5243-5500`) captures
the before-set inside the manifest-write transaction, commits, then runs
`runSweepTransaction` (5725-5773) in a **separate** transaction: a 2000 ms
lock timeout, a 5000 ms statement timeout, and an advisory exclusive lock
(`pg_advisory_xact_lock(hashtext('sweepgate:'||tenant||'/'||collection))`)
guard a `DELETE ... RETURNING` plus a `gc_audit` row. On `55P03`, `57014`, or
any other failure it logs `write_manifest_many_sweep_gate_failed` and returns
`{dropped, swept, errored, reason}` — **without the chash list**. The client's
`_apply_combined_write_response` (`mcp_infra.py` ~2194-2200) records only
`doc_id` + `reason` via `_record_superseded_sweep_skip`: no retry, no chash,
no way to try again. Only the `ChunkBatcher` flush path calls
`sweep=true` at all (`indexer.py:5378-5386`); every other write path —
`store_put`'s own reap, the non-combined indexer paths — runs the
client-side fail-open sweep instead, with the identical loss shape. Measured
2026-09-24: a sweep-gate failure left 7 superseded chunks searchable in
`knowledge__1-1`. This is the surviving half of the indexing-brittleness
proposal's P0.1 item (T2 `nexus/indexing-brittleness-proposal-2026-09-25`);
its ASSIGN half shipped as `nexus-iygza` with a state-derived drain instead
of a persisted pending table, and the SWEEP half is tracked as `nexus-2x9xa`,
explicitly pending this RDR.

#### Gap 5: `nexus.live_chunks` is tenant-wide, not collection-scoped

`vectors-005-repoint-functions-views.xml:228-252` defines `live_chunks` over
the whole tenant, with no collection filter — the one predicate in the
nine-site survey that isn't scoped to the chunk's own collection.
`vectors-017` collection-scoped the equivalent defect in predicates 1 and 3
and in `strandedChunkCount`, but missed this view. Its only current consumer
is `collection_vector_stats`, so today's exposure is a miscounted statistic,
not a wrong deletion — but it is one more site where "is this chunk live"
disagrees with the other eight for a chunk sharing a chash across
collections.

#### Gap 6: The contract's own documentation is now stale

`catalog-003-soft-delete.xml:33-39`'s comment still describes the pre-RDR-145
rationale in the present tense, though RDR-145 landed and `nexus-b6enc` gave
`store_put` a manifest row for every note going forward. `mcp/core.py:4742-4747`'s
HISTORY comment (describing the pre-`bb6n2` "no sweep on this path" behavior)
is flatly false since `nexus-bb6n2` (`f908ae5c3`, 7.58.0) wired the reap. A
reader trusting either comment today would misdiagnose the actual remaining
gap as the one this RDR was originally filed to fix, rather than the
nine-predicate disagreement above.

#### Gap 7: Supersession is invisible to the caller (unchanged, low priority)

`store_put` still returns only `"Stored: <id> -> <collection>"`. A caller has
no way to learn that it just orphaned a chunk. Cheapest item in this RDR and
still worth doing, but no longer load-bearing for correctness now that Gap 1
covers the case where a caller never finds out.

## Context

### Background

Discovered 2026-08-12 while correcting three knowledge entries in an
unrelated project; re-verified 2026-09-26 against develop `135bb38a4` /
engine `v0.1.132` while triaging the indexing-brittleness proposal's P0
tier (T2 `nexus/indexing-brittleness-proposal-2026-09-25`,
`nexus/indexing-brittleness-p0-status-2026-09-25`). That triage separately
confirmed the sweep-drop-set loss (Gap 4) on 2026-09-24 and filed it as
`nexus-2x9xa`, explicitly deferred to this RDR's acceptance.

The ranking-inversion observation from the original filing still holds and
is still unlikely to be incidental: a retraction is necessarily *about* the
claim it retracts, plus qualifications, history, and hedging; the original
is shorter and more purely on-topic. Corrections are therefore
systematically **less** retrievable than what they correct. Stale-chunk
leakage is not neutral noise — it is biased toward resurfacing precisely the
claims someone took the trouble to kill, and the more carefully the
retraction is written, the worse it loses. Only the causes are now more
numerous than the original filing knew: a failed sweep-gate transaction, a
silent note-guard skip, a rename-COPY leftover, or an interrupted indexing
run can each produce the same searchable ghost.

### Technical Environment

- Client at develop `135bb38a4`; engine source at `engine-service-v0.1.132`
  (client's pinned floor: `v0.1.131`). T3 on pgvector Postgres in both local
  and service mode — ChromaDB is not a live substrate in any mode
  (RDR-155 P4b).
- `catalog-003-soft-delete.xml` — `nexus.live_chunks` view, `purge_trash`
  step 1.
- `vectors-005-repoint-functions-views.xml`, `vectors-009` (why `live_chunks`
  cannot become a view join without breaking HNSW binds), `vectors-017-1/-2/-3`
  (collection-scoping of predicates 1, 3, and `strandedChunkCount` — but not
  of `live_chunks` itself), `hygiene-005-1` / `catalog-037-1`
  (`gc_quarantine_orphans`), `taxonomy-019` (`taxonomy_unassigned_chashes`).
- RDR-145 — note-backed document identity (delivered).
- RDR-191 — chunk-table unification; `collection` made a required arg on the
  store_put manifest write (Hal ruling, 2026-08-12).
- `nexus-39upx` (CLOSED) — the re-index orphan class, its in-band sweep, and
  the RDR-145 note protection this RDR originally identified as over-broad.
- `nexus-bb6n2` (`f908ae5c3`, 7.58.0) — closed the original Gap 1
  (store_put manifest replace had no paired sweep). Still fail-open, still
  no `gc_audit` row on the client-side reap path.
- `nexus-iygza` — the sibling P0.1 half (taxonomy assignment drain), shipped
  as a state-derived recompute rather than a persisted pending table; the
  precedent this RDR's proposed engine reaper follows for Gap 4.
- `nexus-2x9xa` — the tracked, currently-open SWEEP half of P0.1, explicitly
  deferred to this RDR.

## Research Findings

### Investigation

Original verification (7.6.1) plus re-verification by source reading against
develop `135bb38a4` / engine `v0.1.132`, 2026-09-26.

| Claim | Evidence | Basis |
| --- | --- | --- |
| The original store_put manifest replace had no paired sweep | `store_hook.py` (7.6.1) called `atomic_manifest_replace` with no sweep call in the file | Verified (historical; see next row) |
| That gap is now closed | `store_hook.py:1044-1190` reads the manifest before `atomic_manifest_replace`, diffs after verify, reaps via `_reap_superseded_note_chunks`; covers MCP `store_put` (`mcp/core.py:4969`), `nx store put` (`commands/store.py:233`), memory promote (`commands/memory.py:642`), `recovery_bundle.py:458` | Verified |
| The store_put reap is client-side, fail-open, gate-less, and writes no `gc_audit` row | `/v1/vectors/delete` writes no audit row; only the engine's own sweep (`CatalogRepository.java:5749`), `purge_trash`, quarantine, and `/gc_audit/record` do | Verified |
| Liveness/orphan status is computed at nine independent sites with three distinct answers | Enumerated in Gap 1 above, one file:line citation per site | Verified |
| Two of those nine sites are collection-scoped inconsistently with the rest | `nexus.live_chunks` (tenant-wide) vs. predicates 1, 3, and `strandedChunkCount` (collection-scoped, fixed by `vectors-017`) | Verified |
| The silent note-guard skip from `nexus-kgos1` was reproduced in the new reap path | `mcp_infra.py:2287-2288`, `:2437-2438`; `store_hook.py:1159-1160`, `:1172-1173` — all silent on empty/all-filtered | Verified |
| The engine's own sweep does not have this defect | `CatalogRepository.java:5756` logs `kept` on every run | Verified |
| The post-commit sweep loses its drop set on any gate failure | `CatalogRepository.java:5725-5773` returns `{dropped, swept, errored, reason}` with no chash list; client records only `doc_id` + `reason` (`mcp_infra.py` ~2194-2200), no retry | Verified |
| A sweep-gate failure produced 7 searchable superseded chunks in production | Measured 2026-09-24, T2 `nexus/indexing-brittleness-proposal-2026-09-25` | Verified (field measurement) |
| The historical 55k-row manifest-less population no longer exists | `gc_quarantine_orphans` moved 41,545 (`code__1-1`) + 5,831 (`knowledge__1-1`) + 5,681 (`docs__1-1`) rows on 2026-09-16; live population 2026-09-24 is 147 tenant-wide, all `knowledge__*` | Verified |
| The 147-row population's decomposition (superseded vs. legacy-unmanifested vs. failed put) is not established | No engine route exposes an unfiltered count; the 2026-09-24 census used a direct database query, not a client tool | Verified as an open question |
| The original candidate currency encodings — a `superseded_at` column, or the `supersedes` catalog link — are obsolete | The manifest row itself is now the positive currency signal for every non-legacy chunk; neither encoding is needed | Verified |
| No consumer depends on retrieving superseded note versions from raw search | No `include_superseded` parameter or equivalent exists anywhere in the client; `quarantine-*` reached via `corpus="all"` is the only history-shaped surface found | Documented (absence of a call site, not a proof of absence of demand) |

### Key Discoveries

- **Verified** — The original two-mechanism failure (a missing wire plus an
  over-broad protection) is now three: the missing wire is fixed
  (`nexus-bb6n2`), the over-broad protection is unchanged and now provably
  wider than "notes" (nine sites, three answers), and a **new** failure
  mode appeared in the fix itself — the fail-open reap loses its drop set on
  failure with no retry (Gap 4), which is the general shape `nexus-kgos1`
  already warned about, arriving again in the code written to close the
  first instance of it.
- **Verified** — Liveness is not one predicate with edge cases; it is nine
  independently maintained predicates that happen to agree most of the
  time. Fixing "the" predicate requires naming and replacing all nine, not
  patching the one raw search reads.
- **Verified** — The engine already contains a correct pattern
  (`sweepChunksQuery`'s reporting, `strandedChunkCount`'s collection
  scoping) sitting next to the client code that reproduces the defects
  those patterns already solved. The asymmetry argues for moving the
  authoritative predicate into the engine, once, rather than patching each
  client call site to match engine behavior it can drift from again.
- **Verified** — `nexus-iygza` (the sibling half of the same P0.1 proposal
  item) already answered "persist a drop set or recompute from state" for
  taxonomy assignment: Sam's ruling was recompute from state, not a durable
  pending table. This RDR's proposed engine reaper for Gap 4 follows that
  precedent rather than reopening the question.
- **Documented** — The historical 55k-row manifest-less population this RDR
  was originally scoped against has been superseded by events: a
  `gc_quarantine_orphans` sweep on 2026-09-16 moved nearly all of it. The
  live population (147 rows, all `knowledge__*`) is two orders of magnitude
  smaller and its composition is not yet known, which changes the migration
  risk calculus (see Trade-offs) without changing the shape of the fix.

### Critical Assumptions

- [x] Re-putting a note leaves the superseded chunk with **zero** manifest
  rows (rather than a stale row pointing at it) — **Status**: Verified —
  **Method**: Source Search. `/manifest/write` is a per-document
  DELETE+INSERT (`catalog/http_catalog_client.py:3607-3611`); a chunk keeps a
  manifest row only if another document shares its chash.
  `tests/test_bb6n2_supersede_reap.py` exercises this against the real
  substrate.
- [x] `meta["doc_id"]` on the catalog row is updated to the new chash on
  re-put, and the ordering relative to any added sweep is deterministic —
  **Status**: Verified — **Method**: Source Search. `store_hook.py:769-773`,
  `:814-818`, `:843-848` stamp `meta.doc_id` before `core.py:4896` calls the
  manifest write; the reap runs at the end of
  `store_put_manifest_direct` (`core.py:4969`), after the stamp. Ordering is
  deterministic within one call. **Residual**: concurrent same-title re-puts
  are not serialized against each other — this is a new, narrower risk than
  the one the original assumption named, and is carried forward as a Risk
  under Trade-offs rather than a blocking assumption, since it requires two
  writers racing the same title, not a single-writer ordering defect.
- [ ] No consumer depends on retrieving superseded note versions from raw
  search — **Status**: Unverified — **Method**: Source Search. No
  `include_superseded` parameter or call site was found anywhere in the
  client; absence of a found consumer is evidence, not proof. This remains
  the one assumption load-bearing for making raw search current-aware by
  default (Gap 2's fix), and should be confirmed by a change-log / support
  scan before Phase 2 ships, not merely re-asserted.
- [x] The `knowledge__knowledge` 23.6% unjoined figure contains superseded
  note versions and not only legitimate notes — **Status**: Obsolete. The
  population this figure was measured against no longer exists:
  `gc_quarantine_orphans` moved 5,831 `knowledge__1-1` rows on 2026-09-16,
  and the live manifest-less population there on 2026-09-24 was 147 rows
  tenant-wide, all `knowledge__*`. The original question (is unjoined load
  "legitimate" or defect) is superseded by a smaller, undecomposed
  population — see Phase 1 below, which replaces this assumption with a
  concrete census requirement.

## Proposed Solution

### Approach

Five changes, ordered so the non-destructive ones land first, the shared
predicate lands before anything depends on it, and the destructive step
lands last, behind a completed backfill:

1. **Define `live(c)` once, in the engine, and use it everywhere search or
   get answers "is this chunk visible"** (Gap 1, Gap 2, part of Gap 5).
   Replaces predicates 1 and 2 above and collection-scopes `live_chunks`
   (predicate 2) at the same time.
2. **Define `reapable(c)` once, in the engine, and use it for every
   candidate-selection predicate** (part of Gap 1, feeds Gap 4). Replaces
   predicates 7, 8, and 9's manifest-less handling with one rule, and backs
   a new state-derived reaper described below.
3. **Replace the post-commit sweep's persisted drop set with a
   state-derived engine reaper for `knowledge__*`** (Gap 4; this is
   `nexus-2x9xa`'s content — see that bead for the day-to-day tracking).
   Instead of trying harder to retry a specific failed sweep, recompute
   "manifest-less and `reapable`" from state at each reaper run, following
   the precedent `nexus-iygza` set for the sibling taxonomy-drain half of
   the same P0.1 proposal item. Every reap writes a `gc_audit` row, closing
   the audit-trail gap the client-side reap left open.
4. **Close the silent skip** (Gap 3): log the kept/filtered count at every
   site that currently returns silently — `mcp_infra.py:2287-2288`,
   `:2437-2438`, `store_hook.py:1159-1160`, `:1172-1173` — matching the
   report line the engine's own sweep already emits
   (`CatalogRepository.java:5756`).
5. **Fix the stale documentation** (Gap 6) and **add `superseded: [...]`
   to the `store_put` result** (Gap 7, cheapest, non-blocking).

### Technical Design

**`live(c)`.** The root defect restated for the engine: liveness must be one
predicate, not nine. Define, in the engine:

```sql
live(c) :=
    EXISTS (
        SELECT 1
        FROM catalog_document_chunks m
        JOIN catalog_documents d
          ON d.tenant_id = m.tenant_id AND d.tumbler = m.doc_id
        WHERE m.tenant_id = c.tenant_id
          AND m.collection = c.collection
          AND m.chash = c.chash
          AND d.deleted_at IS NULL
    )
```

This must ship as an **inlinable** construct — a `LANGUAGE sql` function
marked inlinable, or a SQL fragment the search functions include verbatim at
build/codegen time — **never a view join**. `vectors-009` recorded why: a
view join over this predicate breaks the HNSW index binds the search
functions depend on, which is exactly the failure mode `catalog-003`'s own
EXPLAIN evidence shows today's SubPlan-2 short-circuit avoiding for
manifest-less chunks. Replacing predicates 1 and 2 (search/get visibility,
`nexus.live_chunks`) with `live(c)` also collection-scopes `live_chunks` for
free, since the predicate itself is collection-scoped — this closes Gap 5 as
a side effect of the Gap 1 fix rather than as a separate change.

**`reapable(c)`.** A chunk is a garbage-collection candidate, independent of
whether it is currently live:

```sql
reapable(c) :=
    NOT EXISTS (
        SELECT 1 FROM catalog_document_chunks m
        WHERE m.tenant_id = c.tenant_id
          AND m.collection = c.collection
          AND m.chash = c.chash
    )
    AND c.created_at < now() - <grace window>
```

Use `reapable(c)` for predicates 7 (`gc_quarantine_orphans`) and 8
(`nx t3 gc`), and for the new engine reaper below. The grace window exists
for the same reason `purge_trash`'s does today: recoverability. Note that
`reapable(c)` says nothing about whether `c` is a note — that guard is
removed entirely once the legacy-note backfill (Phase 1) reaches zero,
because after backfill every current note has a manifest row and
`reapable(c)` already excludes anything with one.

**State-derived reaper, not a persisted drop set.** The engine's post-commit
sweep transaction (`runSweepTransaction`, `CatalogRepository.java:5725-5773`)
keeps its 2 s lock / 5 s statement bound and its advisory lock — those are
correct and unrelated to this defect. What changes is what happens on
failure: instead of the client recording only `{doc_id, reason}` and never
retrying, a periodic engine-side reaper for `knowledge__*` collections
queries `reapable(c)` directly against current state, with no dependency on
which write transaction produced the manifest-less row or whether that
transaction's sweep succeeded. This is the same shape `nexus-iygza` already
shipped for the taxonomy-assignment half of P0.1 (a state-derived drain
route, not a durable pending table) and gives Gap 4 an answer without
inventing a new retry protocol. Every reap writes a `gc_audit` row, parity
with the engine's other sweep call site (`CatalogRepository.java:5749`),
`purge_trash`, and quarantine — closing the audit gap the client-side reap
left open. Tracked day-to-day as `nexus-2x9xa`.

**Migration order for the nine sites**, read-side before destructive:

1. Predicates 1 and 2 (search/get, `live_chunks`) → `live(c)`. Non-destructive:
   changes what is returned, deletes nothing.
2. Predicates 7, 8, and the new engine reaper → `reapable(c)`. Destructive,
   gated on the Phase 1 backfill census below.
3. Predicate 9 (`taxonomy_unassigned_chashes`) is unaffected in kind — it
   already ignores manifest-less chunks — but should be re-read against
   `live(c)`'s definition once shipped, to confirm "has an own-collection
   manifest row" and "is live" agree for its purposes.
4. Predicates 3, 4, 5, 6 keep their current shape (delete-time protection
   and the sweep transaction itself); only their notes-guard arms (4's
   union guard, 5) are deleted, in Phase 4, once the legacy-note backfill
   reaches zero.

**Legacy-note backfill prerequisite.** Deleting the notes-guard arms is safe
only once every currently-live note has a manifest row. Notes stored before
`nexus-b6enc` and never backfilled are still manifest-less and still
current; `reapable(c)` as defined above would treat them as garbage the
instant the notes guard is removed. `manifest_backfill` must be run to
completion and its own census (a count of legacy-current notes with no
manifest row) must read **zero** before Phase 4 removes the guard — this is
a hard prerequisite, not a nice-to-have, and is the reason Phase 1 exists as
its own phase below rather than folding into Phase 2.

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| `live(c)` predicate | `PgVectorRepository.liveChunksCondition`, `nexus.live_chunks` | Replace both with one shared, inlinable predicate; collection-scope `live_chunks` in the same change |
| `reapable(c)` predicate | `indexer_utils.orphaned_chashes`, `gc_quarantine_orphans`, `nx t3 gc`'s candidate logic | Consolidate into one engine-side predicate; client tools call it rather than re-deriving it |
| State-derived reaper | `CatalogRepository.sweepChunksQuery` / `runSweepTransaction` | Extend with a periodic `knowledge__*` pass driven by `reapable(c)` against current state, not the write transaction's drop set (`nexus-2x9xa`) |
| Legacy-note backfill | `manifest_backfill` (client repair script) | Reuse; add a completion census gate before Phase 4 |
| Silent-skip fix | `mcp_infra._sweep_superseded_vectors[_many]`, `store_hook._reap_superseded_note_chunks` | Add the `kept`/`kept_notes` log line the engine sweep already has |
| Stale docs | `catalog-003-soft-delete.xml` comment, `mcp/core.py:4742-4747` | Rewrite to describe post-`b6enc`/`bb6n2` behavior |
| Operator cleanup | `nx t3 gc` | Point its candidate logic at `reapable(c)` once shipped |
| Currency signal (obsolete) | `superseded_at` column / `supersedes` catalog link | **Drop both candidates** — the manifest row itself is the positive signal; no new column or link type is needed |

### Decision Rationale

Sequencing is still the substance of this design, for the same reason the
original filing gave: the obvious-looking fix — "wire the sweep" — already
shipped once (`nexus-bb6n2`) and reintroduced the exact silent-failure shape
this RDR exists to prevent (Gap 3, Gap 4). That is the evidence for landing
the shared, engine-side predicate **before** touching any more call sites:
patching each of the nine sites individually is how the codebase arrived at
nine sites in the first place.

The non-destructive read-side fix (`live(c)` for predicates 1 and 2) is
worth landing on its own even if Phase 2 onward stalls: it fixes the entire
historical corpus of stale chunks with no deletion and no risk to live
knowledge, which remains the failure mode this design worries about most.
The destructive step (reapable-driven deletion of manifest-less chunks) is
explicitly gated on the legacy-note backfill reaching zero, because the
population that would be affected — 147 `knowledge__*` rows as of
2026-09-24 — is known to contain an unknown mixture of "wanted gone" and
"still current, never backfilled," and the codebase's own stated preference
is unchanged: over-retention is recoverable, over-deletion of a note is not.

### Alternatives Considered

#### Alternative 1: Sweep synchronously on every store_put, and nothing else

**Description**: Delete superseded chunks inside the put transaction, with
no shared predicate and no engine reaper.

**Pros**: Smallest change; no window during which a stale chunk exists on
the happy path.

**Cons**: This is what `nexus-bb6n2` already shipped, and it reproduced the
silent-skip and lost-drop-set defects (Gaps 3, 4) instead of avoiding them.
Doing more of the same thing produces more of the same failure mode.

**Reason for rejection**: Already tried, in effect; the failure mode it
produces is why this RDR still exists.

#### Alternative 2: Persist the sweep's drop set in a durable pending table

**Description**: When a sweep-gate transaction fails, write the dropped
chash list to a Postgres table for a later retry pass, instead of losing it.

**Pros**: Preserves exactly which chunks a specific failed transaction
intended to reap; a retry pass can be precise rather than a full rescan.

**Cons**: `nexus-iygza`, the sibling half of the same P0.1 proposal item
(taxonomy-assignment drain), already faced this exact choice and Sam ruled
for state-derived recomputation over a persisted pending table. A pending
table also needs its own cleanup, its own idempotency story against
concurrent re-puts of the same title, and a second source of truth that can
itself drift from the manifest.

**Reason for rejection**: Recomputing `reapable(c)` from current state at
each reaper run gives the same outcome without a second stateful structure
to keep correct, and matches the precedent already set for the sibling
problem.

#### Alternative 3: Leave the store as-is; document the manual procedure better

**Description**: Keep the memo-based `nx store delete --id` remediation and
`nx t3 gc` dry-run inspection, and make them more discoverable.

**Pros**: Zero code change.

**Cons**: Correctness depends on an operator remembering a procedure, and
the nine-predicate disagreement (Gap 1) is not something any single
procedure can reliably navigate — even the current census tooling
(`nx t3 gc --orphan-window 1h`) requires knowing which of the nine
predicates it implements to interpret its output correctly.

**Reason for rejection**: Hygiene-by-documentation is what produced the
original incident, and the surface it would need to document has only
grown.

### Briefly Rejected

- **Content-hash the title into the chunk id**: does not help — identity is
  already stable; currency is the missing concept, not identity.
- **Delete-then-put in the MCP tool**: creates a window where the entry does
  not exist, and loses the old content if the put fails.
- **A `superseded_at` column, or a `supersedes` catalog link, as the
  currency signal**: both were the original design's two candidate
  encodings; both are now obsolete, because `nexus-bb6n2` and `nexus-b6enc`
  together made the manifest row itself the positive currency signal for
  every non-legacy chunk. Adding either encoding on top would be a second
  inference layered on a predicate the codebase already computes correctly
  in the manifest join.

## Trade-offs

### Consequences

- Raw search stops returning content the manifest no longer references, for
  every collection, the moment `live(c)` ships — a **behavior change** for
  any consumer relying on the current vacuously-visible-if-manifest-less
  default. No `include_superseded` opt-out is proposed (Critical Assumption
  3 found no consumer depending on this), but the assumption should be
  reconfirmed against usage before shipping, not merely re-asserted at gate
  time.
- Migrating predicates 7, 8, and the new reaper to `reapable(c)` makes a
  wider class of chunk newly deletable than "superseded notes" alone: rename
  leftovers, `.nxexp` imports, and interrupted-run partials all satisfy
  `reapable(c)` once they age past the grace window. This is intentional —
  it is the same rule doing the same job everywhere — but it means the
  blast radius of Phase 2 onward is not scoped to notes the way the
  original filing assumed.
- `nexus.live_chunks` becoming collection-scoped changes `collection_vector_stats`'s
  output for any tenant with a chash shared across collections; this should
  be called out to anyone consuming that stat.
- Retiring the transitional contract requires a schema/function change and
  a migration, same as the original filing anticipated.

### Risks and Mitigations

- **Risk**: `live(c)` as a view join breaks HNSW binds. **Mitigation**: ship
  it as an inlinable `LANGUAGE sql` function or generated SQL fragment, per
  `vectors-009`; re-EXPLAIN against the `msz9i` fixture before merging, not
  after.
- **Risk**: The legacy-note backfill never reaches zero, so Phase 4's
  guard-removal is blocked indefinitely. **Mitigation**: this is the correct
  failure mode — the guard stays in place until backfill is complete, by
  design, rather than being removed on a schedule.
- **Risk**: Two concurrent re-puts of the same title race the manifest
  read/diff/reap window in `store_hook.py`, since nothing serializes them
  (Critical Assumption 2's residual). **Mitigation**: out of scope for this
  RDR's predicate-consolidation work; flag as a follow-up if a production
  case surfaces, since today's window is at least no wider than it was
  before `nexus-bb6n2`.
- **Risk**: The 147-row `knowledge__*` manifest-less population is not yet
  decomposed, so Phase 2's destructive migration could delete a legacy
  current note before backfill completes. **Mitigation**: Phase 1's census
  is a hard prerequisite gate, not advisory — Phase 2 does not start until
  the census classifies every row and the legacy class reads zero after
  backfill.
- **Risk**: A new engine reaper duplicates work the client-side reap
  already does, doubling load. **Mitigation**: the reaper only needs to run
  where the client-side reap can fail (`knowledge__*`, where
  `store_put`/`nx store put` write); it is a safety net for the failure
  case, not a replacement for the happy-path reap.

### Failure Modes

- **Fails visibly**: `live(c)`/`reapable(c)` evaluation errors → existing
  query fails loudly rather than silently returning a wrong verdict (no
  behavior change from today on this axis).
- **Fails silently — the one to design against**: the reaper's periodic
  pass itself is skipped or errors with no log line (the same shape Gap 3
  and Gap 4 already describe, one level up). **Mitigation**: the reaper
  logs its own run, its candidate count, and its `gc_audit` row count on
  every pass, success or failure — never a bare early return.
- **Diagnosis**: `nx catalog show` reports a clean manifest while raw search
  returns two versions of one title — same signature as the original
  filing. A `catalog doctor` check for "title with more than one live
  chunk" remains in scope and is not yet built (Day 2 Operations, below).
- **Recovery**: over-retention is recoverable by a later reaper pass;
  over-deletion of a note is not recoverable at all. This asymmetry still
  sets every default here, unchanged from the original filing.

## Implementation Plan

### Prerequisites

- [ ] Critical Assumption 3 (no consumer depends on superseded raw-search
      results) reconfirmed against usage data, not just source search, before
      Phase 2 ships `live(c)` as the search-time default with no opt-out.
- [ ] A reproduction fixture per manifest state: manifest-less; own-collection
      live; own-collection tombstoned only; cross-collection-only manifest.
      All nine sites (post-migration: `live(c)`, `reapable(c)`, and the
      unchanged predicates 3/4/5/6) must agree on each fixture row's status
      relative to what they are each supposed to answer.

### Minimum Viable Validation

(a) **Read-only decomposition**, per `knowledge__*` collection: classify
every manifest-less chunk, by following its metadata `catalog_doc_id`, into
superseded / legacy-unmanifested / dead-owner / no-owner. Run
`manifest_backfill` against the legacy class until a follow-up census reads
zero, before Phase 2 ships anything destructive.

(b) **Forced-failure reap**: re-put a titled note with the change, then
force the reap to fail (simulate the sweep-gate contention). Assert that raw
`search()` returns **only** the new text — via `live(c)`, not via the reap
having succeeded — while the superseded row still exists physically, and
that the reaper's next pass removes it and writes a `gc_audit` row.

(c) **No-regression set**: a never-re-put note, a combined-write document,
and an imported-with-manifest chunk all stay visible under `live(c)`.

(d) **Fixture matrix**: manifest-less; own-collection live; own-collection
tombstoned only; cross-collection-only manifest — every site using `live(c)`
or `reapable(c)` gives the same answer for the same row.

All four in scope; none deferred.

### Phase 1: Census and legacy-note backfill (non-destructive)

#### Step 1: Reproduction fixture and the fixture matrix from the MVV

#### Step 2: Read-only decomposition of the 147 `knowledge__*` manifest-less rows

Classify each by `catalog_doc_id` lineage: superseded, legacy-unmanifested,
dead owner, no owner.

#### Step 3: Run `manifest_backfill` against the legacy-unmanifested class

Gate: a follow-up census of the same class reads zero before Phase 2 begins.

### Phase 2: `live(c)` and search-side migration (non-destructive)

#### Step 4: Ship `live(c)` as an inlinable engine predicate; re-EXPLAIN against the `msz9i` fixture and HNSW-filtered recall

#### Step 5: Migrate predicates 1 and 2 (search/get, `nexus.live_chunks`) to `live(c)`, collection-scoping `live_chunks` in the same change

#### Step 6: Close the silent skip (Gap 3) — log `kept`/`kept_notes` at every site named above

### Phase 3: `reapable(c)` and the state-derived reaper (destructive; gated on Phase 1)

#### Step 7: Ship `reapable(c)` as an inlinable engine predicate

#### Step 8: Migrate predicates 7 and 8 (`gc_quarantine_orphans`, `nx t3 gc`) to `reapable(c)`

#### Step 9: Ship the periodic `knowledge__*` engine reaper driven by `reapable(c)` against current state, writing `gc_audit` rows (`nexus-2x9xa`)

### Phase 4: Cleanup (gated on Phase 1's backfill census reading zero)

#### Step 10: Remove the notes-guard arms from predicate 4's union guard and from predicate 5 (`live_note_chashes`)

#### Step 11: Rewrite the stale documentation (Gap 6) — `catalog-003-soft-delete.xml`'s comment and `mcp/core.py:4742-4747`

#### Step 12: Add `superseded: [...]` to the `store_put` result (Gap 7)

### Day 2 Operations

| Resource | List | Info | Delete | Verify | Backup |
| --- | --- | --- | --- | --- | --- |
| Superseded/reapable chunks | In scope (`nx store list --reapable`) | In scope | In scope (engine reaper; `nx t3 gc` for the client-driven path) | In scope (`catalog doctor` check, below) | N/A — content lives in the current chunk |

A `catalog doctor` check for "title with more than one live chunk" is still
in scope — it is the divergence signature named under Failure Modes, and
nothing currently detects it.

### New Dependencies

None.

## Test Plan

- **Scenario**: Re-put a titled note with changed content — **Verify**: raw
  search returns only the new text via `live(c)`; the old chash is absent.
- **Scenario**: The reap is forced to fail (simulated sweep-gate contention)
  — **Verify**: raw search still returns only the new text (via `live(c)`,
  not via a successful reap); the reaper's next pass removes the old row and
  writes a `gc_audit` row.
- **Scenario**: A genuine current note that has never been re-put —
  **Verify**: never deleted by any predicate, before or after the legacy-note
  backfill census reads zero. Non-negotiable.
- **Scenario**: Two documents sharing identical chunk text, one re-put —
  **Verify**: the shared chunk survives under both `live(c)` and
  `reapable(c)`.
- **Scenario**: Note guard (pre-Phase-4) or the reaper (post-Phase-4) removes
  every candidate on a given run — **Verify**: the `kept`/`kept_notes`/reap
  count is logged; no silent return, at any of the sites named in Gap 3.
- **Scenario**: `meta["doc_id"]` still holds the old chash when a reap runs —
  **Verify**: test fails loudly rather than passing as a no-op (unchanged
  intent from the original filing, now aimed at the reaper's own read path
  too).
- **Scenario**: Collection containing a non-`complete` document —
  **Verify**: `nx t3 gc`'s circuit breaker still refuses
  (`nexus-g6k6b` precondition holds) once its candidate logic points at
  `reapable(c)`.
- **Scenario**: Fixture matrix (manifest-less; own-collection live;
  own-collection tombstoned only; cross-collection-only manifest) run
  against `live(c)`, `reapable(c)`, and predicates 3/4/5/6/9 unchanged —
  **Verify**: every site gives a self-consistent answer to the question it
  is actually supposed to answer (liveness vs. reapability vs. delete-time
  protection), with no cross-predicate disagreement on the same row for the
  same question.
- **Scenario**: A chunk shared across two collections, one of which has a
  live manifest row for it — **Verify**: `live_chunks` (now collection-scoped)
  agrees with `live(c)` for the collection that lacks the manifest row.

## Validation

### Testing Strategy

1. **Scenario**: The MVV above, run against the engine (there is no
   remaining local Catalog / `HttpCatalogClient` split to validate parity
   against — both modes route through the same engine `/v1` surface).
   **Expected**: identical behavior across local and service deployment
   modes, since both are pgvector-backed engine installs.
2. **Scenario**: Corpus measurement before and after Phase 1's census.
   **Expected**: the 147-row `knowledge__*` manifest-less population
   decomposes fully into superseded / legacy-unmanifested / dead-owner /
   no-owner, with the legacy class reaching zero after backfill — this
   replaces the original filing's now-obsolete 23.6%-unjoined-rate question
   (Critical Assumption 4).

### Performance Expectations

`catalog-003`'s `live_chunks` EXPLAIN evidence shows SubPlan 2 short-circuits
for manifest-less chunks today, so the hot path never fires the live-doc
join for notes currently. `live(c)` changes that path for every chunk, not
only notes, and its cost must be re-measured against the same production
shape (the `msz9i` fixture, HNSW-filtered recall), not assumed to match the
old predicate's cost profile — this is unchanged from the original filing's
caution, now scoped to a query that runs for every search rather than only
for note-shaped rows.

## Finalization Gate

> Complete each item with a written response before marking this RDR as
> **Accepted**.

### Contradiction Check

To be completed at gate (Layer 3 AI critique).

### Assumption Verification

Of the four Critical Assumptions: CA1 and CA2 are **Verified** by source
reading against current code (`nexus-bb6n2`'s manifest-replace-then-reap
sequence). CA3 (no consumer depends on superseded raw-search results) is
**Unverified** by source search alone — no call site was found, which is
evidence but not proof, and it is the one assumption load-bearing for
shipping `live(c)` as a default with no opt-out; it should be reconfirmed
against usage/support data before Phase 2, not waved through on a source-search
negative result. CA4 is **Obsolete**: the population it was measured against
no longer exists, and Phase 1's census replaces the question it was asking
with a concrete, gated decomposition requirement.

### Scope Verification

To be completed at gate (Layer 3 AI critique).
