---
title: "Superseded store_put note chunks are permanently live: the manifest-less-is-live contract outlived its transition"
id: RDR-192
type: Bug Fix
status: accepted
priority: high
author: Hal Hildebrand
reviewed-by: self (solo)
created: 2026-08-12
revised: 2026-09-26
accepted_date: 2026-09-26
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
engine. Eight of those nine collapse to three distinct positions on "is this
chunk current" (enumerated in Gap 1 below); the ninth — delete-time
protection — answers a different question entirely and is treated
separately throughout this RDR (see Gap 1's own accounting and Trade-offs).
Every path that computes an answer fails open, and at least one of them
throws away its own result on failure instead of retrying or recording it.
The `manifest-less-is-live` contract this RDR originally targeted for notes
was written to protect one specific overload (a note, by design, has no
manifest row — no `catalog_document_chunks` row joining it to a document);
it now silently protects every other manifest-less chunk in the store too —
rename leftovers, quarantine rows (chunks copied into a separate
`quarantine-*` physical collection by a garbage-collection sweep, pending
review or deletion), partial documents from an interrupted indexing run,
`.nxexp` imports (`nx store export`'s portable snapshot format for a
collection, re-imported elsewhere), and, still, superseded content whenever
a sweep fails partway through. Implementation Plan Phase 1 maps each of
these five producer classes to a concrete detection bucket before any
destructive change ships.

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
   own-collection manifest row (a `catalog_document_chunks` row in the SAME
   collection as the chunk, joining it to a document) AND every owner
   referenced by that row is tombstoned. A manifest-less chunk is
   vacuously visible.
2. **`nexus.live_chunks` view** (`vectors-005-repoint-functions-views.xml:228-257`):
   visible if it has NO manifest row at all (tenant-wide, any collection) OR
   at least one live-doc manifest row (also tenant-wide). Not
   collection-scoped, unlike every other predicate here — see Gap 5.
3. **`purge_trash` step 1** (`vectors-017-3`, ~1066-1101) and
   `CatalogRepository.strandedChunkCount` (`CatalogRepository.java:2401-2439`):
   a chunk is a sweep candidate only if it has an own-collection manifest row
   AND none of that row's owners are live or recently tombstoned.
   Manifest-less chunks are never candidates.
4. **Engine superseded sweep**, `sweepChunksQuery`
   (`CatalogRepository.java:5904-5949`): deletes a dropped chash (the
   chunk's content hash, `sha256(chunk_text)`, which doubles as its T3 row
   id) only if it has NO manifest row at all (tenant-wide, any collection,
   tombstones included) AND no live note-shaped document in the same
   physical collection claims it via `meta.doc_id`.
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
silence is what hid it."* The engine's own sweep has the identical gap, not
a correct model the client failed to follow: `runSweepTransaction`'s
response map always carries a `kept` count
(`CatalogRepository.java:5756`, `out.put("kept", dropped.size() - swept)`),
but its only `log.info` call (`:5740-5742`, `event=write_manifest_many_swept`)
fires only when `swept > 0`. A run where the note guard keeps every
candidate (`swept == 0`) logs nothing at all — the same silent shape this
Gap describes in the client, sitting inside the code that inspired the fix.

#### Gap 4: The engine sweep loses its own drop set on failure

`writeManifestMany(sweep=true)` (`CatalogRepository.java:5243-5500`) captures
the before-set inside the manifest-write transaction, commits, then runs
`runSweepTransaction` (5725-5773) in a **separate** transaction: a 2000 ms
lock timeout, a 5000 ms statement timeout, and an advisory exclusive lock
(`pg_advisory_xact_lock(hashtext('sweepgate:'||tenant||'/'||collection))`)
guard a `DELETE ... RETURNING` plus a `gc_audit` row (the engine's
persistent audit-trail table for garbage-collection actions — every reap,
quarantine move, or purge writes one row here, naming the actor and the
chashes touched). On `55P03`, `57014`, or any other failure it logs
`write_manifest_many_sweep_gate_failed` and returns
`{dropped, swept, errored, reason}` — **without the chash list**. The client's
`_apply_combined_write_response` (`mcp_infra.py` ~2194-2200) records only
`doc_id` + `reason` via `_record_superseded_sweep_skip`: no retry, no chash,
no way to try again. Only the `ChunkBatcher` flush path — the client's
batched-write buffer for the indexer's combined chunk-plus-manifest requests
— calls `sweep=true` at all (`indexer.py:5378-5386`); every other write path —
`store_put`'s own reap, the non-combined indexer paths — runs the
client-side fail-open sweep instead, with the identical loss shape. Measured
2026-09-24: a sweep-gate failure left 7 superseded chunks searchable in
`knowledge__1-1`. This is the surviving half of the indexing-brittleness
proposal's P0.1 item (T2 `nexus/indexing-brittleness-proposal-2026-09-25`);
its ASSIGN half shipped as `nexus-iygza` with a state-derived drain instead
of a persisted pending table, and the SWEEP half is tracked as `nexus-2x9xa`,
explicitly pending this RDR.

#### Gap 5: `nexus.live_chunks` is tenant-wide, not collection-scoped

`vectors-005-repoint-functions-views.xml:228-257` defines `live_chunks` over
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
  cannot become a view join without breaking HNSW binds — the planner's
  ability to push a vector-distance `ORDER BY` into pgvector's HNSW index
  rather than falling back to a sequential scan), `vectors-017-1/-2/-3`
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
| The engine's own sweep has the identical logging gap | Response always carries `kept` (`CatalogRepository.java:5756`); its only `log.info` (`:5740-5742`) fires only when `swept > 0`, so a keep-everything run logs nothing | Verified |
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
- **Verified** — The engine already contains one correct pattern worth
  moving to
  (`strandedChunkCount`'s collection scoping) sitting next to client code
  that reproduces the defect it already solved. `sweepChunksQuery`'s own
  reporting is not a second example — it has the identical silent-on-empty
  gap as the client (Gap 3). The argument for moving the authoritative
  predicate into the engine, once, still holds; the engine's sweep-reporting
  code needs the same fix as the client, not a template to copy from.
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

1. **One engine `live(c)`**: define it once, in the engine, and use it
   everywhere search or get answers "is this chunk visible" (Gap 1, Gap 2,
   part of Gap 5).
   Replaces predicates 1 and 2 above and collection-scopes `live_chunks`
   (predicate 2) at the same time.
2. **One engine `reapable(c)`**: define it once, in the engine, and use it
   for every candidate-selection predicate (part of Gap 1, feeds Gap 4). Replaces
   predicates 7, 8, and 9's manifest-less handling with one rule, and backs
   a new state-derived reaper described below.
3. **State-derived reaper**: replace the post-commit sweep's persisted
   drop set with a state-derived engine reaper for every collection prefix
   (`knowledge__`, `docs__`, `code__`, `rdr__`; Sam, 2026-09-30, see the
   amendment under Step 9) (Gap 4; this is
   `nexus-2x9xa`'s content — see that bead for the day-to-day tracking).
   Instead of trying harder to retry a specific failed sweep, recompute
   "manifest-less and `reapable`" from state at each reaper run, following
   the precedent `nexus-iygza` set for the sibling taxonomy-drain half of
   the same P0.1 proposal item. Every reap writes a `gc_audit` row, closing
   the audit-trail gap the client-side reap left open.
4. **Close the silent skip** (Gap 3): log the kept/filtered count at every
   site that currently returns silently — `mcp_infra.py:2287-2288`,
   `:2437-2438`, `store_hook.py:1159-1160`, `:1172-1173`, **and** the
   engine's own `runSweepTransaction` (`CatalogRepository.java:5740-5742`),
   whose `log.info` fires only when `swept > 0` today even though its
   response map has always carried the `kept` count (`:5756`). The engine
   is not a model to match here; it has the identical gap.
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

**The predicate must be a set-returning function, not a scalar one.** A
scalar SQL function declared `RETURNS boolean`, with a body of the shape
`SELECT EXISTS (subquery)`, is never inlined by PostgreSQL: the planner's
scalar inliner requires the function's own body to have no subquery and to
touch no other table, and an `EXISTS` subquery already disqualifies it,
regardless of how the function is otherwise declared (`LANGUAGE sql`,
`STABLE`, `SECURITY INVOKER`, no `SET` clause are all necessary conditions
for inlining, none of them sufficient on their own). The shape that
actually inlines is a set-returning function, declared `RETURNS TABLE` or
`RETURNS SETOF`, whose body is the join above, called as `EXISTS (SELECT 1
FROM chunk_live_owners(tenant, collection, chash))`. PostgreSQL inlines a
set-returning function used this way and folds the resulting `EXISTS` into
the same semi-join shape `plain_search_<dim>` already uses for its own
anti-join today. This distinction is not stylistic. Bead nexus-wbfpw.9's
first implementation shipped the scalar form; the engine review that
followed captured an `EXPLAIN` plan proving it never inlined at all, and
traced a measured latency regression directly to that opaque, per-row
function call. The shipped implementation is the set-returning form; the
definition of `live(c)` above states what the predicate means, the
set-returning shape is what lets the engine evaluate it fast.

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
    AND c.created_at < now() - <grace window>   -- default 30 days
```

Use `reapable(c)` for predicates 7 (`gc_quarantine_orphans`) and 8
(`nx t3 gc`), and for the new engine reaper below. The grace window exists
for the same reason `purge_trash`'s does today: recoverability. Note that
`reapable(c)` says nothing about whether `c` is a note — that guard is
removed entirely once the legacy-note backfill (Phase 1) reaches zero,
because after backfill every current note has a manifest row and
`reapable(c)` already excludes anything with one.

**Basis change: `indexed_at` to `created_at`.** `nx t3 gc` ages its
candidates on the metadata field `indexed_at` today and skips any chunk
that lacks one (`commands/t3.py:502`, `:524-531`, `:547-549`);
`nexus.chunks.created_at` is a `NOT NULL DEFAULT now()` engine column every
chunk has (`vectors-004-unify-chunks.xml:272`). Moving predicate 8 to
`reapable(c)` therefore closes that skip gap, but it also changes the clock:
every chunk `nx t3 gc` ages on `indexed_at` today ages on `created_at`
after the migration. This is safe only if `created_at` is write-once per
row, which is confirmed by source reading: `PgVectorRepository`'s two
`ON CONFLICT ... DO UPDATE` blocks for chunk upserts — the ordinary content
path (~787-800) and the reference-only path (~838-847) — never list
`created_at` in their `.set(...)` clause, so a conflicting write to an
existing `(tenant, collection, chash)` row leaves `created_at` untouched on
every upsert path this survey found. (The one reset bug on record touching
this column, `catalog-037-1`'s bounded quarantine move, was a different
mechanism — an INSERT of a NEW row into the quarantine collection that
originally omitted `created_at` and was fixed to carry it through — not a
rewrite of an existing row's `created_at`, and is not evidence against the
write-once claim above.)

**State-derived reaper, not a persisted drop set.** The engine's post-commit
sweep transaction (`runSweepTransaction`, `CatalogRepository.java:5725-5773`)
keeps its 2 s lock / 5 s statement bound and its advisory lock — those are
correct and unrelated to this defect. What changes is what happens on
failure: instead of the client recording only `{doc_id, reason}` and never
retrying, a periodic engine-side reaper for every collection prefix (it
said `knowledge__*` until Sam's 2026-09-30 decision, Step 9 amendment)
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
   Amended 2026-10-01 (nexus-wbfpw.39; Phase 2 gate F4 and cross-walk row 9,
   T2 `nexus/critique-rdr-192-phase2`): the re-read was done in
   `nexus-wbfpw.10`, and the two do NOT agree. They differ on R3 (own-collection
   manifest row whose owners are all tombstoned) and R9: predicate 9 counts
   such a chunk because it checks manifest existence without a tombstone
   check, `live(c)` does not. Predicate 9 was kept as is (`nexus-wbfpw.10`
   close note): an assignment on a trashed chunk is invisible to
   topic-scoped search, which now uses `live(c)`. The confirmation this item
   asked for is replaced by that finding.
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
| State-derived reaper | `CatalogRepository.sweepChunksQuery` / `runSweepTransaction` | Extend with a periodic pass over every collection prefix (per-collection census gate, Step 9) driven by `reapable(c)` against current state, not the write transaction's drop set (`nexus-2x9xa`) |
| Legacy-note backfill | `manifest_backfill` (client repair script) | Reuse; add a completion census gate before Phase 4 |
| Silent-skip fix | `mcp_infra._sweep_superseded_vectors[_many]`, `store_hook._reap_superseded_note_chunks`, `CatalogRepository.runSweepTransaction` | Add an unconditional `kept`/`kept_notes` log line to all four sites — the engine sweep has the same gap, not a model to copy from |
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
  Amended 2026-10-01 (nexus-wbfpw.39, Phase 2 gate finding F4, T2
  `nexus/critique-rdr-192-phase2`): since `nexus-wbfpw.31` an `.nxexp` import
  registers an owner first, so `.nxexp` leftovers are reapable only for
  pre-.31 imports and for the chunks a keep-existing import leaves unowned
  (Sam, 2026-09-29, `nexus-wbfpw.40`; see the Step 5 amendment).
- Predicate 8's clock changes from the metadata field `indexed_at` (which
  `nx t3 gc` skips a chunk for lacking) to the engine column `created_at`
  (which every chunk has and which is confirmed write-once — see Technical
  Design). This widens candidacy rather than narrowing it: a chunk `nx t3 gc`
  silently skips today for having no `indexed_at` becomes a `reapable(c)`
  candidate the moment it passes the grace window.
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
  decomposed, so Phase 2's visibility change could hide a legacy current
  note from search before backfill completes — Phase 2 changes what
  `live(c)` returns; it deletes nothing (see Migration order above).
  **Mitigation**: Phase 1's census is a hard prerequisite gate, not
  advisory for the one step that changes visibility: Step 5 does not ship
  until the census classifies every row and the legacy class reads zero
  after backfill. Steps 4 and 6 change no search result and may ship
  earlier (Sam, 2026-09-26).
- **Risk**: A new engine reaper duplicates work the client-side reap
  already does, doubling load. **Mitigation**: the reaper only needs to run
  where the client-side reap can fail (`knowledge__*`, where
  `store_put`/`nx store put` write); it is a safety net for the failure
  case, not a replacement for the happy-path reap.
  Amended 2026-10-01: the reaper now also covers `docs__`, `code__` and
  `rdr__` (Sam, 2026-09-30, T2
  `nexus/rdr-223-192-sam-decisions-2026-09-30-reaper-staging`). Those prefixes
  are where RDR-223's multi-batch re-index leaves the previous version's
  dropped chunks ownerless after a crash, so the reaper is their only
  recovery short of `nx t3 gc`. It runs only on a collection that passes the
  per-collection census gate (Step 9), and it takes the sweep gate exclusive
  per collection so it cannot delete a chunk a running index run is about to
  reference.

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
  chunk" is Phase 4 Step 14, below.
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
four buckets — superseded / legacy-unmanifested / dead-owner / no-owner —
mapped from the five producer classes named in the Problem Statement as
follows. A lost reap (superseded content) lands in **superseded**: its
`catalog_doc_id` resolves to a live document whose current manifest names a
different chash. A legacy-current note lands in **legacy-unmanifested**: its
`catalog_doc_id`/`doc_id` resolves to a live document with no manifest row
at all. A rename-COPY leftover (`CatalogRepository.java:8101-8125`, where a
rename onto a live target repoints the manifest and the `collection` column
without moving the physical chunk rows) lands in **dead-owner**: its owning
document is live but its manifest now points at a different collection
name, so the bucket's definition is broadened to cover a repointed owner as
well as a tombstoned one. A `.nxexp` import lands in **no-owner**: its
manifest hook short-circuited on an empty `doc_id` at import time
(`exporter.py:201-215`), so no catalog document was ever registered to own
it. Amended 2026-10-01 (nexus-wbfpw.39, Phase 2 gate F4): this holds for
pre-`nexus-wbfpw.31` imports, and for the chunks a keep-existing import
(`nexus-wbfpw.40`) leaves unowned. Since .31 an import registers an owner
first, so a current import is not in the no-owner bucket. The Phase 1 census
found the no-owner bucket to be two indexed run logs, not imports (Phase 1
result below). Quarantine rows are **out of scope for this census entirely** — they
live in their own `quarantine-*` physical collection, a deliberate sweep
destination, never folded into a `knowledge__*` collection's own count.
Run `manifest_backfill` against the legacy-unmanifested class until a
follow-up census reads zero, before Phase 2 ships anything destructive.
`manifest_backfill` (`backfill_manifest_for_collection`,
`manifest_backfill.py:182`) operates on one named collection at a time and
is document-driven — it iterates a collection's catalog documents, not an
orphan scan of T3 — so an operator backfilling `knowledge__*` never targets
`quarantine-*` and touches no quarantine row, and it has nothing to iterate
for a `.nxexp` import's chunks either, since no catalog document was ever
registered to own them, for the identical reason.

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

Amended 2026-10-01 (nexus-wbfpw.39): (e) **Reaper between batches**. A
multi-batch re-index of an existing document, with a reaper pass between
batch 1 and the last batch: no chunk the run later writes or re-adds is
deleted (Step 9, sweep gate exclusive per collection and the `indexing`
skip).

### Phase 1: Census and legacy-note backfill (non-destructive)

#### Step 1: Reproduction fixture and the fixture matrix from the MVV

#### Step 2: Read-only decomposition of the 147 `knowledge__*` manifest-less rows

Classify each by `catalog_doc_id` lineage into superseded, legacy-unmanifested,
dead-owner, or no-owner, per the producer-to-bucket mapping in the MVV above.
Quarantine rows are out of scope by construction (a separate physical
collection); `.nxexp` imports and rename-COPY leftovers are not overlooked
by this census — they are the no-owner and dead-owner buckets respectively.
Amended 2026-10-01 (nexus-wbfpw.39, `nexus-wbfpw.40` critique): for `.nxexp`
that means pre-.31 imports, and the chunks a keep-existing import leaves
unowned. A document whose manifest is non-empty but incomplete (an
interrupted index run) is not made whole by an import any more; the repair
is `heal_manifest_gaps`. So the claim in `nexus-wbfpw.32`'s comment that
every chunk gets an owner holds for documents whose manifest is empty or
complete, not for a partial one.

#### Step 3: Run `manifest_backfill` against the legacy-unmanifested class

Gate: a follow-up census of the same class reads zero before Step 5 ships.
The census is an engine route plus an `nx` verb, not a one-off SQL script,
because it is re-run before Steps 5, 8, 9 and 11 (Sam, 2026-09-26). The
route runs one standalone statement, `scripts/sql/manifest_less_census.sql`,
byte-identical to the route's text. Sam decided (2026-09-26, option 1) that
the production census and every pre-merge re-check run that statement
directly, in psql as `nexus_svc`, until the final engine tag deploys (one
tag shared with RDR-223, Sam 2026-09-30; see the Revision History); the route shipped in `engine-service-v0.1.133` and the verb
(`nx t3 census-manifest-less`) is on `develop`, so later re-checks may use
either.

#### Step 3a: `store_put` leaves no manifest-less chunk on a failed catalog or manifest write

Plan audit, 2026-09-26: a census that reads zero once does not stay zero.
`mcp/core.py:4895-4906` swallows a failed catalog registration and still
writes the chunk, `catalog/store_hook.py:1022` then returns early on the
empty `catalog_doc_id`, and a failed manifest write returns "stored but NOT
cataloged" with the chunk left behind. Each of these writes a new current
note with no manifest row, which Step 5 would hide from search and Step 9
would later reap. On a failed catalog or manifest write, `store_put`
deletes the chunk it just wrote and returns an error (Sam, 2026-09-26:
rollback, not a marker column). As implemented (`nexus-wbfpw.28`,
`nexus-k54nk`), the rollback deletes only when a read-back confirms the
write did not land; an unknown outcome never deletes; and it keeps any
chunk another live document or a legacy note still owns.

#### Step 3b: A failed indexer manifest hook fails the `nx index` run

The indexer's manifest hook has the same shape: a failure leaves written
chunks without a manifest row and the run still succeeds. The run fails
instead. Steps 3a and 3b ship in a client release before the engine tag
carrying Step 5 is deployed, and the census covers every collection except
`quarantine-*`, not only `knowledge__*`.

The two steps leave different traces. After 3a, a confirmed write
failure through `store_put`, `nx store put`, `nx memory promote` or a
recovery-bundle import removes its chunk, so a new manifest-less chunk
in `knowledge__*` is an anomaly to investigate. After 3b, a failed
`nx index` run leaves its chunks manifest-less and exits non-zero, so a
manifest-less chunk in `docs__*`, `code__*` or `rdr__*` can be the
residue of a failed run that the next successful run manifests.

#### Phase 1 result (2026-09-27)

Census on the live tenant, all 95 non-quarantine collections (not only
`knowledge__*`): 542 manifest-less chunks of 337,499; superseded 139,
no-owner 395, dead-owner 7, legacy-unmanifested 1, unclassified 0 (T2
`nexus/rdr-192-census-2026-09-27`). The 147 of 2026-09-24 are the
superseded, dead-owner and legacy rows; the 395 no-owner rows are two
indexed run logs whose manifest writes were lost in the 2026-09-24
embed incident. The backfill closed the legacy row; a full re-run reads
legacy-unmanifested 0 and unclassified 0 everywhere (T2
`nexus/rdr-192-census-zero-2026-09-27`). Sam's dispositions: no-owner and
dead-owner are reaped (T2 `nexus/rdr-192-dispositions-2026-09-27`).

### Phase 2: `live(c)` and search-side migration (non-destructive)

#### Step 4: Ship `live(c)` as an inlinable engine predicate; re-EXPLAIN against the `msz9i` fixture and HNSW-filtered recall

#### Step 5: Migrate predicates 1 and 2 (search/get, `nexus.live_chunks`) to `live(c)`, collection-scoping `live_chunks` in the same change

Amendment (Sam, 2026-09-27, found implementing `nexus-wbfpw.10`): split
inventory from liveness. `nexus.collection_vector_stats` is also the
collection inventory (`list_collections`, `get_collection`, `census --all`,
the ghost sweep's dormant check), and some callers ask whether a chunk is
stored rather than whether it is visible (`existing_ids`, the
`put_note_pieces` delete guard). Moving those onto `live(c)` made a
quarantine sibling vanish from the inventory, made a collection whose first
chunk is not yet manifested read as missing, and let the delete guard treat
a stored shared chunk as absent. So:

- Content reads (search, hybrid, topic-scoped search, the get family,
  `store-list`, `live_chunks`) use `live(c)`.
- `collection_vector_stats` keeps one row per collection that physically
  holds chunks. `chunk_count` and `last_write` count live chunks; a new
  `stored_count` counts every stored chunk. Inventory readers decide
  emptiness on `stored_count`, routing readers use the live count, and the
  ghost sweep still marks a collection dormant only when it has no row.
- `store-get` with `include_non_live` returns the rows physically stored,
  ids and metadata, never content. `existing_ids` sends it, and
  `put_note_pieces` uses `existing_ids`.
- Maintenance paths enumerate stored chunks the same way (found after the
  first push by the integration-marked tests): `/v1/vectors/get` and
  `get-all-metadata` accept `include_non_live`, returning ids and metadata
  over every stored row. Manifest heal and `nx catalog reconcile`,
  `nx t3 gc`'s candidate listing, the misclassified prune, `expire`, the
  forced orphan cleanup and the manifest backfill use it, because each
  exists to handle chunks that have no live owner.

An old client against this engine loses the presence probe and the
emptiness check, so the pairing is not additive: the client release carrying
these halves, and `nexus-wbfpw.31`, ships before this engine deploys.

Amendment (2026-10-01, nexus-wbfpw.39; Phase 2 gate finding F4, T2
`nexus/critique-rdr-192-phase2`): the `.nxexp` import design that shipped,
which the Step 5 text above predates.

- **Owner first (`nexus-wbfpw.31`, Sam 2026-09-27).** Export carries an
  optional `owner` per record (`source_uri`, `title`, `content_type`,
  `position` of the chunk's live own-collection owner); older importers
  ignore it, so there is no `format_version` bump. Import finds or registers
  the owner document by `source_uri` and writes one manifest per document
  with the recorded positions. A legacy record with a `doc_id` in chunk
  metadata keeps its live document, or gets one per `doc_id`. A record with
  neither gets one document per import file, `nxexp://<target>/<file>`, with
  positions in file order, so a re-import is idempotent and nothing lands
  unowned.
- **Copy, not move (Sam, 2026-09-27).** A cross-collection import copies; it
  does not move the chunks or their documents.
- **Owner resolution (`nexus-wbfpw.33`, shipped in 7.64.1).** 7.64.0 parsed
  the owner from the collection name and aborted on slug owners, leaving
  12,495 chunks in 15 `gate-xr789` collections non-live. The owner is now the
  row's `owner_id` (or its hyphens-as-dots form) when the catalog confirms a
  registered owner, else a live document's owner in the collection, else the
  knowledge curator.
- **Deploy census (`nexus-wbfpw.32`).** The deploy condition is the
  `live(c)` census reading zero on every tenant, not the manifest-less census
  (`scripts/sql/livec_census.sql`); chunks whose only owner is tombstoned are
  let go (Sam, 2026-09-27, T2 `nexus/rdr-192-dispositions-2026-09-27`).
- **Keep existing (`nexus-wbfpw.40`, Sam 2026-09-29).** An import never
  replaces or extends the manifest of a document that already owns chunks.
  That document's chunks in the file are skipped and counted, and the CLI
  prints a runnable `nx store delete` line per affected document. The
  per-batch manifest hook is off for imports. The limit: a document whose
  manifest is non-empty but incomplete is not made whole by an import
  (`heal_manifest_gaps` repairs it), so "every chunk gets an owner" holds for
  empty or complete manifests only.

#### Step 6: Close the silent skip (Gap 3) — log `kept`/`kept_notes` at every client site named above, **and** add an unconditional log line to the engine's `runSweepTransaction` (`CatalogRepository.java:5740-5742`), which has the same gate-on-`swept>0` gap

### Phase 3: `reapable(c)` and the state-derived reaper (destructive; gated on Phase 1)

#### Step 7: Ship `reapable(c)` as an inlinable engine predicate

#### Step 8: Migrate predicates 7 and 8 (`gc_quarantine_orphans`, `nx t3 gc`) to `reapable(c)`

#### Step 9: Ship the periodic engine reaper, over every collection prefix, driven by `reapable(c)` against current state, writing `gc_audit` rows (`nexus-2x9xa`)

Defaults: the grace window is 30 days (the current `nx t3 gc
--orphan-window`); the reaper runs hourly, at most 300 chunks per collection
per pass.

Amendment (Sam, 2026-09-30; T2
`nexus/rdr-223-192-sam-decisions-2026-09-30-reaper-staging`, bead
nexus-wbfpw.39): the reaper covers ALL prefixes: `knowledge__`, `docs__`,
`code__` and `rdr__`. This step and the bead title said `knowledge__*` only
(the bead title is `nexus-2x9xa`, now amended). The reason is RDR-223's
multi-batch re-index: a client that dies after the first batch leaves the
previous version's dropped chunks ownerless in `docs__`, `code__` or `rdr__`,
and without the reaper they stay hidden until an operator runs `nx t3 gc`.
Per-collection gate: the reaper visits a collection only when (a) the
collection's `last_written_at` is older than the grace window and (b) an
in-engine census of that collection reads `legacy-unmanifested == 0`. A
collection that fails the gate is skipped with a visible refusal in the pass
log, and nothing in it is deleted.

Reaper requirements from the RDR-223 Phase 2 gate critique (T2
`nexus/rdr-223-phase2-gate-critique` S2, 2026-09-30). "Ownerless past grace is
garbage" is false while a multi-batch re-index is in flight: the old tail
chunks that a later batch re-adds are ownerless for the whole run, a writer
holds the sweep gate shared per request and not per document, and the
existence partition runs before the transaction, so a pass between batches can
delete a chunk the run is about to reference. So:

- the reaper takes the sweep gate EXCLUSIVE per collection, as
  `runSweepTransaction` does;
- it skips a chunk whose owner document is in `index_state = 'indexing'`, with
  a TTL so a document stuck `indexing` after a crash stops protecting its
  chunks (this is the recency guard the Phase 2 gate also asked for in P3-3,
  T2 `nexus/critique-rdr-192-phase2`, because `created_at` is write-once and
  gives a re-upserted old chunk no fresh grace);
- an MVV runs a reaper pass between batch 1 and batch k of a multi-batch
  re-index and asserts that no chunk the run later references is deleted;
- the bead states whether the existence-partition metadata refresh bumps
  `last_written_at` (open when this note was written).

#### Step 10: Ship `nx store list --reapable`, a read-only list of the chunks `reapable(c)` currently selects for a collection, so an operator can inspect what the reaper is about to remove before it runs

### Phase 4: Cleanup (gated on Phase 1's backfill census reading zero)

#### Step 11: Remove the notes-guard arms from predicate 4's union guard and from predicate 5 (`live_note_chashes`)

#### Step 12: Rewrite the stale documentation (Gap 6) — `catalog-003-soft-delete.xml`'s comment and `mcp/core.py:4742-4747`

#### Step 13: Add `superseded: [...]` to the `store_put` result (Gap 7)

#### Step 14: Ship a `catalog doctor` check for "title with more than one live chunk" — the divergence signature named under Failure Modes, and the one thing that currently detects nothing

Read narrowly: a split note legitimately has several live chunks, so the
check flags a chunk the census classes as superseded that raw `get()` can
still return.

### Day 2 Operations

| Resource | List | Info | Delete | Verify | Backup |
| --- | --- | --- | --- | --- | --- |
| Superseded/reapable chunks | `nx store list --reapable` (Phase 3, Step 10) | In scope | In scope (engine reaper; `nx t3 gc` for the client-driven path) | `catalog doctor` check (Phase 4, Step 14) | N/A — content lives in the current chunk |

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
- **Scenario** (added 2026-10-01): a reaper pass runs between batch 1 and the
  last batch of a multi-batch re-index in a `docs__`, `code__` or `rdr__`
  collection — **Verify**: the pass skips chunks owned by a document in
  `index_state = 'indexing'`, and a collection that fails the census gate is
  refused visibly with nothing deleted.

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

## Revision History

- 2026-09-26: Re-verified against develop `135bb38a4` / engine `v0.1.132`.
  The original Gap 1 (store_put manifest replace had no paired sweep) closed
  by `nexus-bb6n2` (7.58.0); gaps renumbered and re-evidenced; Gaps 3-6 added
  (silent skip reproduced in the reap path, engine sweep loses its drop set,
  `live_chunks`'s tenant-wide scope, stale contract documentation); Critical
  Assumptions restated (CA1/CA2 verified, CA3 unverified, CA4 obsolete);
  Proposed Solution rewritten around engine-side `live(c)`/`reapable(c)` and
  a state-derived reaper (`nexus-2x9xa`).
- 2026-09-26: Gate round 1 — PASSED (0 Critical, 6 Significant, 0
  ship-blocker(s)); commit `141684599`; critique
  `nexus_rdr/192-gate-critique-2026-09-26-r1`.
- 2026-09-26: All six gate-round-1 findings fixed in `13bcd8e5e`. CA3
  acknowledged by Sam (T2 `nexus_rdr/192-research-7`); it stays a Phase 2
  prerequisite. Accepted by Sam.
- 2026-09-26: Amended after planning (epic `nexus-wbfpw`, plan audit READY
  round 2). Added Steps 3a and 3b: three writers still leave manifest-less
  current notes after a failed catalog or manifest write, so a zero census
  did not stay zero. Sam's decisions recorded in place: only Step 5 waits for
  the census, the census is a route plus a verb, a failed `store_put` write
  rolls back. Defaults recorded: 30-day grace window, hourly reaper at 300
  chunks per collection per pass, the narrow Step 14 reading.
- 2026-09-27: Step 5 amended during `nexus-wbfpw.10` (Sam): inventory is
  split from liveness. `collection_vector_stats` keeps a row per stored
  collection with a live `chunk_count` and a physical `stored_count`, and
  `store-get` gains an `include_non_live` presence probe used by
  `existing_ids`. `.nxexp` imports register an owner first
  (`nexus-wbfpw.31`).
- 2026-09-29: Sam's keep-existing decision on `.nxexp` import
  (`nexus-wbfpw.40`): an import never replaces or extends the manifest of a
  document that already owns chunks. Recorded in the text on 2026-10-01
  (nexus-wbfpw.39): Step 5 amendment, the MVV (a) and Step 2 `.nxexp`
  passages, and the Trade-offs bullet on `.nxexp` leftovers.
- 2026-10-01: Amended for the Phase 2 gate's text findings and Sam's
  2026-09-30 decisions (bead nexus-wbfpw.39; T2
  `nexus/critique-rdr-192-phase2` F4, `nexus/rdr-223-phase2-gate-critique`
  S1 and S2, `nexus/rdr-223-192-sam-decisions-2026-09-30-reaper-staging`,
  `nexus/rdr-223-192-single-cut-decision-2026-09-30`). (1) The reaper
  (Approach item 3, Technical Design, Infrastructure Audit, Risks, Step 9)
  covers every prefix, `knowledge__`, `docs__`, `code__` and `rdr__`, behind a
  per-collection census gate; Step 9 records the three reaper requirements
  from the RDR-223 Phase 2 gate (exclusive sweep gate per collection, skip
  documents `indexing` with a TTL, an MVV with a reaper pass between
  batches), and the MVV gains item (e). (2) Step 5 records the `.nxexp`
  import design that shipped: `nexus-wbfpw.31` (owner first, copy not move),
  `.33` (owner resolution), `.32` (the `live(c)` deploy census) and `.40`
  (keep existing). (3) Migration order item 3 records that predicate 9 and
  `live(c)` differ on R3 and R9. (4) One engine tag: the engine work still
  open here, `reapable(c)` and the reaper, ships in one final engine tag and
  one paired client release shared with RDR-223 (Sam, 2026-09-30); the
  `live(c)` engine work had already been cut earlier, as v0.1.135 (never
  deployed), v0.1.136 and v0.1.137.
