---
title: "Superseded store_put note chunks are permanently live: the manifest-less-is-live contract outlived its transition"
id: RDR-192
type: Bug Fix
status: accepted
priority: high
author: Hal Hildebrand
reviewed-by: self (solo)
created: 2026-08-12
revised: 2026-10-02
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

As built, 2026-10-02: that `store_hook.py` reap no longer exists. RDR-223 deleted it
(`nexus-z0o2p.12`, `.32`); a note re-put now writes its chunks and manifest as one
`write_manifest_many` with `sweep=True` and the engine sweeps the superseded chunk in the
same request (Gap 3's amendment). The paragraph above is the state at filing.

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

*Pointer convention (2026-10-02).* File and line pointers in the Problem Statement, the
Critical Assumptions and the Technical Design are as of the filing re-verification, develop
`135bb38a4`, and several have drifted (`CatalogRepository.java`, `PgVectorRepository.java`,
`note_write.py` and `mcp_infra.py` have all moved). Where a statement is written in the present
tense about the filing-time code, read it as past tense. Find the code by the method or test
name; text amended after the filing names tests and methods, not lines, and any line number
left in an amendment is as of develop `e26cd7381`.

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
   'complete'`) document. Amended 2026-10-02 (Phase 3 gate, T2
   `nexus/review-rdr-192-phase3-critique` D2, D4): this is the filing-time
   shape. Phase 3 (`nexus-wbfpw.18`) removed the flag, the tombstone-inclusive
   chash set and the `live_note_chashes` arm; `nx t3 gc` takes its candidates
   from `reapable(c)` and refuses the whole collection on the census gate
   (`src/nexus/commands/t3.py:549-571`).
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

Amended 2026-10-02 (Phase 3 gate, T2 `nexus/review-rdr-192-phase3-critique` D5).
The client-side reap this Gap describes is gone: `store_hook._reap_superseded_note_chunks`
and its guards at `store_hook.py:1159-1160` and `:1172-1173` were deleted with the split
note write by RDR-223 (`nexus-z0o2p.12` and `.32`, commit `a92a02279`), and a note re-put
now sweeps through the engine (`catalog/note_write.py`: `write_note` and its resend after a
lost acknowledgement both send `write_manifest_many` with `sweep=True` through
`write_one_request`). `mcp_infra._sweep_superseded_vectors`
(`mcp_infra.py:2835`) and `_sweep_superseded_vectors_many` (`:2972`) remain, for the
indexer's non-combined write paths, and since `nexus-wbfpw.12` log `superseded_sweep_kept`
at their guard returns (`:2915`, `:2937`, `:3079`, `:3101`). The engine's half of the gap was
Step 6's, and is closed (`nexus-wbfpw.13`): `runSweepTransaction` logs `write_manifest_many_swept`
on every sweep run, with `kept`.

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

Amended 2026-10-02 (Phase 3 gate, D5): the sentence above that sends `store_put`'s own
reap through the client-side fail-open sweep no longer holds. `put_note` writes a note's
chunks and manifest as one `write_manifest_many` with `sweep=True`, so a re-put takes the
engine sweep, and a sweep that errors is reported as `sweep_skipped`
(`catalog/note_write.py`, `_warn_if_sweep_skipped`, which logs `note_sweep_skipped`). The indexer's non-combined paths still run the
client-side sweep (`mcp_infra.py:3484`, `:3583`). Both lose their drop set on failure the
same way, and the state-derived reaper (Step 9) is what recovers it.

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

Amended 2026-10-02 (Phase 3 gate, D6): the `store_put` comment is now at
`src/nexus/mcp/core.py:5322-5340`, and the bead's first proposed wording ("`store_put` reaps
superseded chunks client-side") is itself false after RDR-223. Both comments were rewritten
to the behavior as built (`nexus-wbfpw.23`, `nexus-wbfpw.24`): the engine sweeps the superseded
chunk in the note's own write; if that sweep errors, `live(c)` hides the old chunk; the engine
reaper then MOVES it to quarantine once it has been ownerless for 30 days (it does not delete
it) and deletes it 14 days later.

#### Gap 7: Supersession is invisible to the caller (unchanged, low priority)

`store_put` still returns only `"Stored: <id> -> <collection>"`. A caller has
no way to learn that it just orphaned a chunk. Cheapest item in this RDR and
still worth doing, but no longer load-bearing for correctness now that Gap 1
covers the case where a caller never finds out.

Amended 2026-10-02 (Step 13, `nexus-wbfpw.25`; Phase 4 critique S1, S2): built, and as a second
line of text rather than a `superseded: [...]` field, because `store_put` returns a plain string
(`structured_output=False`) and a new field would be a change of return type. The engine's
`sweep_detail` entry gains `swept_chashes` (the chashes the sweep DELETED, not the manifest's drop
list) and `swept_chashes_truncated`, capped at 300 per document, `[additive]`. `store_put` and
`nx store put` print `Superseded: N chunk(s) removed: [<chash>, ...]` (three chashes, then
`(and M more)`) only when the sweep removed something. A sweep that errors (`sweep_skipped`)
prints `Superseded: the sweep did not finish; up to N replaced chunk(s) were not removed. ...`,
because the purpose of this Gap, that the caller learns it left a chunk behind, is unmet exactly
where something went wrong if the failure stays in a log. A put whose acknowledgement was lost
and resent prints neither: the resend's sweep removes nothing and the first attempt's answer is
gone, so the client cannot name what was removed and does not claim that nothing was.

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
- [x] No consumer depends on retrieving superseded note versions from raw
  search — **Status**: Verified (2026-09-26, `nexus-wbfpw.3`, closed; T2
  `nexus_rdr/192-research-11`; Sam, 2026-09-26) — **Method**: Source Search at
  filing, then a GitHub issue and CHANGELOG scan, a T2/T3 search and
  `nexus.search_telemetry`. No `include_superseded` parameter or call site exists in the
  client, and the scan found zero named consumers: no issue, no CHANGELOG entry, no stored
  finding and no plugin code reads superseded or `quarantine-*` content as a feature. The
  collection that holds superseded notes, `quarantine-knowledge__1-1`, has no
  `search_telemetry` row at all. **Residual**: telemetry records the collection hit, not the
  caller, so anonymous historical hits on some `quarantine-code`, `-docs` and `-rdr`
  collections cannot be attributed or ruled out; that was surfaced to Sam rather than
  rounded to zero, and `live(c)` shipped as the search default with no opt-out.
- [x] The `knowledge__knowledge` 23.6% unjoined figure contains superseded
  note versions and not only legitimate notes — **Status**: Obsolete. The
  population this figure was measured against no longer exists:
  `gc_quarantine_orphans` moved 5,831 `knowledge__1-1` rows on 2026-09-16,
  and the live manifest-less population there on 2026-09-24 was 147 rows
  tenant-wide, all `knowledge__*`. The original question (is unjoined load
  "legitimate" or defect) is superseded by a smaller, undecomposed
  population — see Phase 1 below, which replaces this assumption with a
  concrete census requirement.
- [ ] A legacy note's chunk cannot enter a sweep's dropped set, so the notes-guard
  arm is removable — **Status**: REFUTED — **Method**: Spike (amended 2026-10-02,
  Step 11). A dropped set is a document's previous manifest minus its new rows, so an
  unrelated document that shared the note's text and then dropped it puts the note's chash
  there. `CatalogManifestSweepRepositoryTest` Order 12
  (`writeManifestMany_sweepTrue_genuineManifestLessNote_notSwept`) builds that shape against
  the real engine and reads the chunk kept by the notes arm alone. The arm is therefore
  retained (Step 11); the cost is bounded over-retention. A genuine legacy note is never
  collected (the census gate refuses its collection); only a chunk a dangling stamp
  names, whose owner has manifest rows, is later collected by the reaper.

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
   the audit-trail gap the client-side reap left open. (Amended 2026-10-02: RDR-223 deleted
   that client-side reap, so the audit trail is the engine's own: the sweep, the quarantine
   move and the expiry each write a `gc_audit` row.)
4. **Close the silent skip** (Gap 3): log the kept/filtered count at every
   site that returned silently at filing (`mcp_infra.py:2287-2288`,
   `:2437-2438`, `store_hook.py:1159-1160`, `:1172-1173`, **and** the
   engine's own `runSweepTransaction`, `CatalogRepository.java:5740-5742`),
   whose `log.info` fired only when `swept > 0` even though its
   response map has always carried the `kept` count (`:5756`). The engine
   was not a model to match here; it had the identical gap. (Done: Step 6,
   `nexus-wbfpw.12` for the client sites and `.13` for the engine.)
5. **Fix the stale documentation** (Gap 6) and **report supersession in the
   `store_put` result** (Gap 7, cheapest, non-blocking). As built (Step 13): a
   `Superseded:` line after `Stored:`, not a `superseded: [...]` field, naming the chunks the
   sweep removed or saying that it did not finish.

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
    NOT EXISTS (                                  -- 1. no own-collection manifest row,
        SELECT 1 FROM catalog_document_chunks m   --    in ANY owner state
        WHERE m.tenant_id = c.tenant_id
          AND m.collection = c.collection
          AND m.chash = c.chash
    )
    AND GREATEST(c.last_written_at,                     -- 2. default 30 days, counted from the later of
                 (SELECT o.orphaned_at                  --    the last client write and the last time the chunk
                    FROM chunk_orphaned_at o            --    lost an owner row (NULL when it never did)
                   WHERE o.tenant_id = c.tenant_id
                     AND o.collection = c.collection
                     AND o.chash = c.chash)) < now() - <grace window>
    AND NOT EXISTS (                                    -- 3. not a quarantine sibling,
        SELECT 1 FROM catalog_collections cc            --    by lifecycle_state, not by name
        WHERE cc.tenant_id = c.tenant_id
          AND cc.name = c.collection
          AND cc.lifecycle_state = 'quarantine'
    )
```

It ships as `nexus.chunk_is_reapable(tenant, collection, chash, last_written_at,
grace)` (vectors-021, nexus-wbfpw.15): a set-returning function for the reason
`live(c)` is one, returning one row when the chunk is reapable and none otherwise,
so a caller writes `EXISTS (SELECT 1 FROM nexus.chunk_is_reapable(c.tenant_id,
c.collection, c.chash, c.last_written_at, NULL))`. NULL for the grace means the
default, which lives in the function and nowhere else. The row's own
`last_written_at` is passed in, so the grace comparison lands on the caller's own
row; as the WHERE of a DELETE it is a qual on the DELETE's target, and a client
write that refreshes `last_written_at` while the DELETE waits on the row lock is
seen at the READ COMMITTED recheck. The manifest sub-query is not rechecked that
way: a manifest row committed during the DELETE is the sweep gate's concern, and
every destructive consumer takes the gate exclusively (the gc functions and
`runSweepTransaction` already do; the reaper must). `reapable(c)` takes no lock.
The grace window is injectable for tests and the SQL stays pure; a delete path that
accepts a user-supplied window must clamp it to a floor before it reaches the
function (a small user-chosen window reopens the race the grace exists to close). The
gc functions pass NULL, so they have no user-tunable window to clamp.
`ReapableConsumersScanTest` makes both obligations mechanical: every statement that
deletes or copies chunks under the predicate sits in a function that takes the
exclusive sweep gate and passes NULL, with two named exemptions. The read-only listing
route is one. The other is `reaper_quarantine_chunks`, whose grace is an injectable
`p_grace` so a test can pass zero; production passes NULL, which the engine reaper does
(the scan waives the literal-NULL rule for that one call text, and only for `p_grace` passed
straight through, never a clamp or another expression).

Use `reapable(c)` for predicates 7 (`gc_quarantine_orphans` and its bounded variant,
vectors-022, nexus-wbfpw.16), the read-only listing route `POST /v1/vectors/reapable`
(nexus-wbfpw.17, an advisory listing for `nx store list --reapable` and a dry run; the
destructive `nx t3 gc` of predicate 8 does not delete by the ids it returns but calls an
engine route whose own statement carries the predicate, nexus-wbfpw.18), and the new
engine reaper below (nexus-2x9xa). The grace window exists
for the same reason `purge_trash`'s does today: recoverability. It covers every
collection prefix (`knowledge__`, `docs__`, `code__`, `rdr__`; Sam, 2026-09-30), and
nothing in the predicate names one.

**`reapable(c)` is not "no live owner needs this chunk".** Matrix row R8, a live
legacy note stored before nexus-b6enc that has no manifest row, reads
`reapable = true` once aged: the predicate says nothing about whether `c` is a
note. The guard is the census gate, not the predicate: a consumer that acts on the
list must first see `legacy-unmanifested == 0` for the collection (and the reaper
re-runs that census in the engine on every pass; see Step 9). The notes guard in
the two sweeps is retained, not removed (Step 11, decision of Sam 2026-10-02):
after backfill every current note has a manifest row and `reapable(c)` already
excludes anything with one, but the sweeps hard-delete without a census, so the
arm stays as defense in depth.

**The grace is time since the chunk became ownerless (the orphaning record).** An
earlier draft excluded chunks whose metadata `catalog_doc_id` named a document in
`index_state = 'indexing'`. That pin was empty for every chunk the multi-batch
writers produce: RDR-108 removed `doc_id` from chunk metadata and no writer stamps
`catalog_doc_id` on indexer chunks, so an old tail chunk carried no key, the pin saw
nothing, and its tests passed only on hand-stamped fixtures (review T2
`nexus/review-wbfpw15-17-code` C1). It is removed. What protects an old tail chunk
is a record of WHEN it last lost an owner, kept in its own table. vectors-021 adds
`nexus.chunk_orphaned_at (tenant_id, collection, chash, orphaned_at)`, keyed like the
chunk, and two statement-level triggers on `catalog_document_chunks`, one AFTER DELETE
and one AFTER UPDATE. They upsert `orphaned_at = now()` for every chunk KEY the
statement dropped from the manifest (for an UPDATE, the old keys minus the new keys),
in the manifest change's own transaction. The UPDATE trigger fires on every manifest
UPDATE, because a transition-table trigger cannot name columns; a statement that keeps
every key drops nothing and records nothing. The grace is then counted from
`GREATEST(last_written_at, orphaned_at)`: how long the chunk has gone without a write
or an owner.

*Why a side table, not `last_written_at` (Sam, 2026-10-01, option b).* The first
version of the stamp UPDATEd `nexus.chunks.last_written_at`. A chunk-row UPDATE writes a
new row version, and `nexus.chunks` carries three full HNSW indexes plus two GIN
indexes, so each stamp re-inserted the row into the 1024-d HNSW graph for a vector that
had not changed. Measured: 11 s for 5000 chunks, against about 50 ms for the side table
(one laptop-container run; see Cost, below). The side table has a primary key and nothing else. `last_written_at`
keeps its vectors-020 meaning, "client wrote this chunk", with no exception, and the
vectors-020 header says so.

The triggers do not read the manifest to ask whether the chunk is "really" ownerless.
An earlier version did (stamp only when no owner remains), and two concurrent writers
dropping the last two owners of a shared chunk each saw the other's uncommitted row and
stamped nothing, which left the chunk reapable at once (verification review T2
`nexus/review-wbfpw15-17-verify-code` I1; reproduced red by
`ChunkIsReapableIntegrationTest.aConcurrentDropOfASharedChunkLeavesItStamped`). Recording
every dropped key is safe because `reapable(c)` needs no owner anyway: a recorded chunk
that is still owned fails condition 1, and one that loses its other owners later has its
record refreshed.

*Locking.* Each trigger first locks the chunk rows it will record, `ORDER BY tenant_id,
collection, chash FOR NO KEY UPDATE OF c`, then upserts in the same key order. A chunk
row exists for every recorded key, is the one row every writer of the key shares, and
needs no insert, so the lock pass is one ordered walk over rows that already exist. This
buys three things. Two triggers that share chunks lock in the same total order, so they
cannot deadlock on chunk rows. The order is chunk row, then side-table row, which is what
the foreign key's own cascade takes when a chunk is deleted, so a trigger and a chunk
delete cannot deadlock on that pair. And the insert's foreign-key check, which takes a
share lock on the chunk row, cannot fail with 23503 against a chunk deleted concurrently:
the pre-lock waits on the chunk row, and the later insert statement takes a fresh
snapshot that no longer sees the deleted chunk, so it skips it and the manifest
statement succeeds. The lock is not what keeps two concurrent drops from losing a record.
The `ON CONFLICT` upsert already waits on the other statement's uncommitted index entry
and re-evaluates its `DO UPDATE` guard against the committed row, so the one-hour guard
sees the first record with or without the lock (removing it leaves
`aConcurrentDropOfASharedChunkLeavesItStamped` green). An earlier version of this section
said the lock prevented a lost record; that was wrong. It does not claim to order against
other writers. The content upserts sort their chunk writes by chash, so those passes are
monotone in chash too, but nothing pins that, and the manifest write is not under
`DeadlockRetry`; a 40P01 against another writer would be loud and retryable, not a lost
record. `ChunkIsReapableIntegrationTest` pins the `ORDER BY` and the lock clause in both
function bodies with `pg_get_functiondef` (no behavioural test can fail on their removal,
so the definition is the only place to hold them), pins that the bodies never UPDATE
`nexus.chunks`, and pins the concurrent drop.

*Security.* The functions are SECURITY INVOKER like every function in the changelog, run
under the writer's FORCE RLS and tenant GUC, and join on `tenant_id`; EXECUTE is revoked
from PUBLIC; a pg_proc pin asserts `prosecdef = false`. The explicit tenant equality is
defence in depth for the service role (RLS already binds it) and the only barrier for a
role that bypasses RLS, so it has its own test, run as the container superuser (the production owner role,
`nexus_admin`, has no `BYPASSRLS` and is bound by the policies): removing the equality
from the upsert's join in either trigger turns that test red, and the definition pin
asserts it appears twice in each function, once in the lock pass and once in the upsert.

*The one-hour guard.* A chunk is recorded only when both its `last_written_at` and its
existing `orphaned_at` are older than one hour (the select filters on the first, the
upsert's `DO UPDATE ... WHERE` on the second). The writer has usually just refreshed the
chunks it then drops from the manifest, and such a chunk is already inside its grace by
30 days less an hour. The price: a chunk written within the hour before it loses its
owner is reapable up to one hour earlier than a recorded one, never at once and never
later.

*Stale records.* The table cannot hold a record for a chunk that is gone: its foreign key
to `nexus.chunks` is `ON DELETE CASCADE ON UPDATE CASCADE` (the `topic_assignments`
precedent, taxonomy-012), so the reaper's delete, the quarantine move, delete-collection
and `purge_trash` remove it, and a collection rename rewrites it. A record that is no
longer true cannot make a chunk reapable wrongly, because it only ever bounds the grace
from below: a re-owned chunk fails condition 1, a re-written chunk has a newer
`last_written_at` that wins in `GREATEST`, and a chunk that loses its owner again has its
record refreshed. Each of those, and the cascade, has a test.

Doing it in the database covers the DML paths that drop an owner row
(`writeManifestRows`' replace, append's upsert of a position, `purgeManifest`,
delete-collection, the FK cascade of a hard document delete, the SQL maintenance
functions, the `.nxexp` import, whatever is added later), where a list of Java call sites
would rot. "Covers every path" is not "by construction": the table below lists the paths
it does not cover, with what each costs and what limits it.

*Per-path consequences.* The columns: the consequence is one of never reaped, reaped
late, reaped early, or reaped wrongly (a live chunk taken). "Early" and "wrongly" are
bounded by the consumer's own gates (the census gate, the fraction floor on the move, the
14 day quarantine).

| Path | What the predicate sees | Consequence | Mitigation, and its owner |
| --- | --- | --- | --- |
| `TRUNCATE` of the manifest (a superuser; no code path does it; `nexus_svc` holds no `TRUNCATE`, pinned with `has_table_privilege`) | Statement DELETE triggers do not fire, so no record; every chunk reads ownerless with its old `last_written_at` | Reaped early, for a live chunk reaped wrongly, all at once | A floor ON THE MOVE (refuse a pass that would take more than a fraction of a collection) and the 14 day quarantine, from which `nx t3 quarantine restore` brings a chunk back (`nexus-wbfpw.49`; with `--reattach` it also writes the manifest row when the chunk's own metadata names a live document, so the chunk is visible again; that reaches only chunks that name their document (legacy and `store_put` chunks, single-chunk legacy notes), and a chunk it cannot attach, every file-derived chunk included, comes back as bytes, stays hidden, and needs the file re-indexed): the reaper's move has a floor, `NX_REAPER_FLOOR_FRACTION` with a `NX_REAPER_FLOOR_MIN_CHUNKS` minimum, judged on the whole reapable set (`ChunkReaperIntegrationTest`: `aPassThatWouldTakeMoreThanTheFloorFractionOfACollectionIsRefusedAndCounted`, `aPassUnderTheFloorFractionMoves`, `aReapableSetBelowTheFloorMinimumIsExemptFromTheFraction`, `theFloorIsConfigurable`, `theMoveFunctionItselfRefusesOnTheWholeSet_notOnlyTheJavaPreJudgement`); only the `gc_quarantine_orphans` route has none (`nx t3 gc` keeps an advisory client-side floor, the indexer's prune none), `nexus-wbfpw.52` |
| `session_replication_role = replica` (`pg_restore`, logical apply) | Triggers and the FK cascade skipped | A restore inserts and drops nothing. A manual replica-mode DELETE: reaped early | None beyond the consumer gates; accepted |
| Manifest DML with no tenant GUC, or a role exempt from RLS | Subject to RLS on both tables with no GUC: zero manifest rows deleted, nothing dropped (pinned). Exempt from the manifest policy only: rows deleted, `nexus.chunks` hidden from the trigger, nothing recorded (reasoned; cannot be built without `NO FORCE` on the manifest alone). Exempt from both (`BYPASSRLS`, superuser): recorded correctly (pinned, run as the container superuser). The table owner is not exempt in production: `nexus_admin`, the Liquibase owner role, has no `BYPASSRLS` (catalog-016, catalog-025 headers), so under `FORCE ROW LEVEL SECURITY` it is bound by the policies like `nexus_svc` | Reaped early in the exempt-from-one case | A migration or DBA fix that deletes manifest rows runs as `nexus_admin` and must set the tenant GUC. No bead |
| The one-hour guard | A chunk written or recorded within the hour is not recorded | Reaped up to one hour early | Accepted; stated in the vectors-021 header |
| Staging promote over an existing chunk (`StagingPromoteOps`, `ON CONFLICT DO NOTHING`) | Retired. It kept the old clocks and recorded no orphaning, so an aged ownerless chunk re-promoted was reapable between promote and finalize. The `/v1/staging` routes and the `staging` schema were removed (`nexus-z0o2p.27`, `8a7831a3d`), so no such path remains | None | None needed |
| R8: a live legacy note, no manifest row | Reapable once aged; every row took the vectors-020 migration time | Reaped wrongly at deploy + 30 days if a consumer acts without the census gate | The census-zero gate, re-run in the engine on every reaper pass (`nexus-2x9xa`); `nx t3 gc` requires `legacy-unmanifested == 0` before acting (`nexus-wbfpw.18`). The quarantine move (`gc_quarantine_orphans`) carries no gate of its own and its only caller passes code, docs and rdr collections. The way back for a note the reaper took wrongly is `nx t3 quarantine restore` with `--reattach` (`nexus-wbfpw.49`), which attaches the chunk to its still-live document unless that document has moved on (`superseded`, with a reason); that is the R8 population (a legacy note whose chunk names its document, or a single-chunk legacy note), which is exactly what the census gate is meant to have cleared first. Without a live owner the chunk is restored as bytes only and stays hidden from search and get (exit status 3); in a `knowledge__` collection the output prints the re-put-under-the-same-title recipe, in a file collection it says to re-index the file. `nx t3 backfill-manifest` does nothing for this class |
| A tombstoned owner | Still a manifest row, so condition 1 fails | Never reaped by this predicate (`purge_trash` ages the tombstone) | `purge_trash`, unchanged |
| A floor-refused quarantine chunk; a collection that fails the census | Quarantine siblings are excluded by `lifecycle_state`; a failing collection is refused visibly | Never reaped by the reaper | `gc_expire_quarantine` has its own clock and floor; fix the census |
| Everything pre-existing at deploy | `vectors-020` gave every row the migration time | Reaped late: nothing existing is reapable for 30 days, then all of it at once (a one-shot cliff at deploy + 30 days, which the RDR-223 Day-2 baseline must carry) | The floor on the move bounds the cliff; baseline update in `nexus-2x9xa` |
| A deleted file's chunks; a re-index's old tail | Clock starts at the purge, or at batch 1 | Reaped late by design: 30 days after the manifest change | None needed |

**Cost, measured.** One Docker pgvector 17.11 container on an Apple-silicon laptop at
load average 10 to 13, so the absolute figures carry contention noise and the ratios are
the evidence; three runs each, range given. Table: 35,000 chunks of 1024 dimensions in
one collection, about 1.6 KB of text each, all three HNSW indexes (m = 16,
ef_construction = 64), both GIN indexes and the two btrees, the manifest with its foreign
key to `nexus.chunks`, row level security forced on all three tables, statements run by a
NOSUPERUSER NOBYPASSRLS role with the tenant GUC set, `shared_buffers` 128 MB, all 5000
affected chunks last written 40 days before, statements rolled back. "Old" is the first
version (UPDATE of `nexus.chunks`), "new" is this design, "off" is the triggers dropped.

| Statement | off | old | new |
| --- | --- | --- | --- |
| DELETE of 5000 manifest rows, one statement | 1 to 2 ms | 11.1 to 13.9 s | 51 to 60 ms |
| the same, the 5000 chunks refreshed in the same transaction (the guard path) | 3 ms | 39 to 41 ms | 43 to 48 ms |
| 1000 documents x 5 chunks, 1000 statements (the FK cascade of a hard delete fires one per document) | 14 to 15 ms | 11.0 to 12.1 s | 77 to 84 ms |
| batch 1 of a multi-batch re-index drops 5000 old tail chunks, then a later batch refreshes them, one transaction: the drop alone | 2.5 ms | 10.9 s | 44 ms |

The refresh in the last row is the client's ordinary chunk upsert, 10.5 to 11.8 s for
5000 rows on this table in every column; it is not a cost of this change, and the first
version paid it twice. The critique measured the old figure independently on the same
shape (17 to 19 s at heavier load; 5.3 s on a 7000-row table with smaller text). A
10,000-chunk old tail dropped at batch 1 costs about 0.1 s inside the shared sweep gate
and the index-run lock, not the tens of seconds the first version cost. Chunk-row
locking accounts for about 12 ms of the new figure (51 to 60 ms against 39 to 48 ms for
the upsert alone). Two plan facts the measurement found: the predicate reads the record
with a scalar subquery on the primary key, not a LEFT JOIN, because the join form's plan
depended on the side table's statistics and went quadratic (3.2 s against 80 ms) when a
statement grew the table while it ran (`ChunkIsReapableIntegrationTest` pins the scalar form
in the function definition, because the plan test's fixture analyzes an 8000-row side
table, where the join plans well too); and the table carries
`autovacuum_analyze_scale_factor = 0.02` so autoanalyze follows a burst. The plan under
`nexus_svc` is in `ChunkIsReapablePlanIntegrationTest`: `chunks_pk` range, then
`idx_catalog_chunks_chash`, the `catalog_collections` primary key and
`chunk_orphaned_at_pk` as index probes, no sequential scan of the side table.

**Why condition 3.** A quarantine sibling never has manifest rows and its rows take
a fresh `last_written_at` on the move, so after 30 days every quarantined chunk would
satisfy conditions 1 and 2, and a reaper that visits `quarantine-*` collections would
move them out of quarantine past `gc_expire_quarantine`'s own clock and safety floor
(the floor that exists because a manifest defect once deleted 6 live documents). The exclusion is
by `lifecycle_state`, not by name: RDR-204 retired parsing names. The listing route
also refuses a `quarantine-` name with 400.

**Basis change: `indexed_at` to `last_written_at`.** `nx t3 gc` ages its
candidates on the metadata field `indexed_at` today and skips any chunk that lacks
one (`commands/t3.py:502`, `:524-531`, `:547-549`). The grace anchor is the engine
column `nexus.chunks.last_written_at` (vectors-020, nexus-wbfpw.43; `NOT NULL
DEFAULT now()`), which every chunk has, so moving predicate 8 onto `reapable(c)`
closes that skip gap, but it changes the clock to the later of `last_written_at` and
the orphaning record. It is not `created_at`: that is
write-once, which `ChunkLastWrittenAtIntegrationTest` pins on both
`PgVectorRepository` `ON CONFLICT ... DO UPDATE` paths (the content path and the
reference-only path never list `created_at` in their `.set(...)`), so a chunk that a
re-index re-writes would otherwise get no fresh grace and the reaper could take it
between the client's chunk write and its manifest write. `last_written_at` is
refreshed by the client paths that re-write an existing chunk (the content upsert,
the reference-only upsert, `batchUpdateMetadata`, the existence-partition metadata
refresh: the have-vector branch of `upsert-chunks` and the identical-text branch of
the combined write, so **yes, the existence-partition refresh bumps it**, and the
combined write's chunk upsert) and never by frecency, enrichment or rename
maintenance, which would keep dead chunks alive. The orphaning record above is a
separate table of facts and does not touch it. A move into or out of quarantine
resets it (the quarantine INSERT takes the default), which only delays a reap. **At deploy**, existing rows take the migration
time, so for 30 days after the engine carrying vectors-020 is deployed nothing that
already exists is reapable: the cleanups after the upgrade move nothing, and a
reaper MVV or census reads zero for that long. The refresh uses `now()`, the
transaction's start, and the writers take the sweep gate SHARED first, so a wait on
the gate eats into the grace; with a 30 day window that is immaterial. It is also why a
destructive caller that accepts a user-supplied window must clamp it to a floor before
it reaches the function (the grace paragraph above): a window of minutes would let that
wait decide the race. (The one reset bug on record touching `created_at`,
`catalog-037-1`'s bounded quarantine move, was a different mechanism: an INSERT of a
NEW row into the quarantine collection that originally omitted `created_at`, fixed to
carry it through.)

**No index on `last_written_at`.** A btree on it would stop HOT updates for every
client re-write. Measured on `nexus.chunks` with `fillfactor = 40` so each page has
room (production keeps the default 100, where metadata-refresh updates are mostly
non-HOT anyway, so this is the upper bound of what an index would cost): 100% of
`last_written_at` refreshes HOT without the index, 0% with it. The candidate scan is
already bounded by the `(tenant_id, collection, chash)` primary-key prefix, and each
pass is O(collection) with an index probe per row (60,000 chunks about 0.4 s in the
scratch harness). `ChunkIsReapablePlanIntegrationTest` asserts the schema has no such
index and pins the plan under `nexus_svc`: a `chunks_pk` range scan,
`idx_catalog_chunks_chash` for the manifest probe, the `catalog_collections` primary
key for the quarantine probe, no function scan.

**One predicate, and what stays separate.** `live(c)` and `reapable(c)` are each
defined once, in vectors-018 and vectors-021, and reused by every consumer.
`CatalogRepository.strandedChunkCount` (purge_trash Step 1) does not move onto
`reapable(c)`, and `purge_trash` keeps its own `deleted_at` grace: it counts chunks
that HAVE a manifest row whose owners are all aged tombstones, the complement of
condition 1, so the two are disjoint by design and together make the dead set (R3
and R9 in the S1a table are dead, not reapable). Folding them together would make
`reapable(c)` delete a chunk whose only owner was soft-deleted a minute ago.
Predicate 9 (`taxonomy_unassigned_chashes`) is unchanged, as above. When
`nexus-z0o2p.24` makes the engine refuse ownerless writes, the client race the
column was added for is gone; whether to drop it then is that bead's decision, and
until it ships the column is the grace anchor.

**State-derived reaper, not a persisted drop set.** The engine's post-commit
sweep transaction (`CatalogRepository.runSweepTransaction`; lines 5725-5773 at filing)
kept its 2 s lock / 5 s statement bound and its advisory lock — those were
correct and unrelated to this defect, and are unchanged. What changes is what happens on
failure: instead of the client recording only `{doc_id, reason}` and never
retrying, a periodic engine-side reaper for every collection prefix (it
said `knowledge__*` until Sam's 2026-09-30 decision, Step 9 amendment)
queries `reapable(c)` directly against current state, with no dependency on
which write transaction produced the manifest-less row or whether that
transaction's sweep succeeded. This is the same shape `nexus-iygza` already
shipped for the taxonomy-assignment half of P0.1 (a state-derived drain
route, not a durable pending table) and gives Gap 4 an answer without
inventing a new retry protocol. Every reap writes a `gc_audit` row, parity
with the engine's other sweep call site (the `gc_audit` insert inside
`runSweepTransaction`), `purge_trash`, and quarantine. (Amended 2026-10-02: the client-side reap this
closed the audit gap of is gone, deleted by RDR-223; the indexer's non-combined
client sweeps remain and still hard-delete with no audit row.) Tracked day-to-day
as `nexus-2x9xa`.

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
   and the sweep transaction itself). Their notes-guard arms (4's union
   guard, 5) were to be deleted in Phase 4 once the legacy-note backfill
   reaches zero. Amended 2026-10-02 (Step 11, decision of Sam): they are
   RETAINED. The engine sweep's arm and the client sweeps' `live_note_chashes`
   guard stay as defense in depth; see Step 11 for why neither removal nor a
   gate on the rung record is safe.
   Amended 2026-10-02 (Phase 3 gate, D4): `nx t3 gc` no longer reads `live_note_chashes`.
   Phase 3 (`nexus-wbfpw.18`) replaced its candidate logic with `reapable(c)` and a census
   gate that refuses the whole collection while any `legacy-unmanifested` or `unclassified`
   chunk exists (`src/nexus/commands/t3.py:549-571`), which is coarser than excluding note
   identities and safer. What still reads it is `mcp_infra`'s client-side sweeps
   (`mcp_infra.py:2785`, `:3249`).

**Legacy-note backfill prerequisite.** `reapable(c)` and the reaper's
consumers (the reaper, `nx t3 gc`) must not act while a legacy note is
manifest-less: notes stored before `nexus-b6enc` and never backfilled are
still manifest-less and still current, and `reapable(c)` as defined above
treats them as garbage once aged. `manifest_backfill` must be run to
completion and its own census (a count of legacy-current notes with no
manifest row) must read **zero** before a consumer acts, which is the census
gate Step 9 describes. This is a hard prerequisite, not a nice-to-have, and
is the reason Phase 1 exists as its own phase below rather than folding into
Phase 2. Amended 2026-10-02 (Step 11): the prerequisite no longer gates
removing the sweeps' notes-guard arms, because they are not removed.

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| `live(c)` predicate | `PgVectorRepository.liveChunksCondition`, `nexus.live_chunks` | Replace both with one shared, inlinable predicate; collection-scope `live_chunks` in the same change |
| `reapable(c)` predicate | `indexer_utils.orphaned_chashes`, `gc_quarantine_orphans`, `nx t3 gc`'s candidate logic | Consolidate into one engine-side predicate; client tools call it rather than re-deriving it |
| State-derived reaper | `CatalogRepository.sweepChunksQuery` / `runSweepTransaction` | Extend with a periodic pass over every collection prefix (per-collection census gate, Step 9) driven by `reapable(c)` against current state, not the write transaction's drop set (`nexus-2x9xa`) |
| Legacy-note backfill | `manifest_backfill` (client repair script) | Reuse; the census gate is the reaper's per-collection census (Step 9). The Phase 4 guard removal it was to gate was dropped: Step 11 is retained by decision (Sam, 2026-10-02) |
| Silent-skip fix | `mcp_infra._sweep_superseded_vectors[_many]`, `CatalogRepository.runSweepTransaction` (`store_hook._reap_superseded_note_chunks` was deleted by RDR-223) | Add an unconditional `kept`/`kept_notes` log line to every site that remains — the engine sweep has the same gap, not a model to copy from |
| Stale docs | `catalog-003-soft-delete.xml` comment, `mcp/core.py:5322-5340` | Rewrite to the behavior as built (engine sweep, `live(c)`, quarantining reaper), not the post-`b6enc`/`bb6n2` behavior this row first named |
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
procedure can reliably navigate — even the census tooling as it stood
at filing (`nx t3 gc` with a tunable window) required knowing which of the nine
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
  `nx t3 gc` skips a chunk for lacking) to the later of the engine column
  `last_written_at` (which every chunk has) and the orphaning record
  (`nexus.chunk_orphaned_at`); it is not `created_at`, which is write-once, so a
  re-write would get no fresh grace — see Technical Design. This widens
  candidacy rather than narrowing it: a chunk `nx t3 gc`
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
- **Risk**: The legacy-note backfill never reaches zero. **Mitigation**: nothing
  waits on it any more. Step 11 (retained by decision, Sam 2026-10-02) keeps the
  sweeps' notes-guard arms permanently, so there is no guard removal for a nonzero
  count to block; the reaper's per-collection census gate refuses a collection while
  it reads any legacy-unmanifested chunk, which is the protection that matters for a
  manifest-less note.
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
  Amended 2026-10-02 (Phase 3 gate, D5): the premise of this Risk is gone. RDR-223
  deleted the client-side reap (`nexus-z0o2p.12`, `.32`), so the reaper duplicates no
  client work; it is the only backstop for a failed engine sweep, `store_put`'s
  included, and for a crashed multi-request run. The load concern stands as a bound:
  at most 300 chunks per collection per hourly pass, behind the floor and the census.

### Failure Modes

- **Fails visibly**: `live(c)`/`reapable(c)` evaluation errors → existing
  query fails loudly rather than silently returning a wrong verdict (no
  behavior change from today on this axis).
- **Fails silently — the one to design against**: the reaper's periodic
  pass itself is skipped or errors with no log line (the same shape Gap 3
  and Gap 4 already describe, one level up). **Mitigation**: the reaper
  logs its own run, its candidate count, and its `gc_audit` row count on
  every pass, success or failure — never a bare early return.
  Amended 2026-10-02 (Phase 3 gate S5, `nexus-wbfpw.56`): the log is not enough for a
  cloud operator, who has no engine log, and a pass that throws an `Error` rather than an
  `Exception` escapes the scheduled task and suppresses every later run without a trace
  (`NexusService.java` wraps the pass in `catch (Exception)`). The fix, catching
  `Throwable` at each scheduled task and exposing the last completed pass time with an
  `nx doctor` row for a stale one, is built (`nexus-wbfpw.56`): `GET /v1/status` carries
  `reaper.last_completed_pass_at` and the row warns on a dead reaper. A pass completes
  whatever its tenants did, so a reaper that is alive but refusing every tenant (the
  backfill rung not run) or erroring on every collection (a grants regression) still
  stamps the time; `reaper.last_pass` (`tenants_visited`, `tenants_errored`,
  `tenants_refused`) closes that, and the row warns "alive but doing nothing" when a
  recent pass visited tenants and none worked. Not yet closed: nothing automated runs
  `nx doctor` against a cloud engine, and the cloud gate has no leg that asserts the
  `reaper` key survives the edge (`nexus-wbfpw.50`).
- **The census reads a live document as `no-owner`** (Phase 3 gate O2). The census
  resolves a chunk's owner from its metadata (`catalog_doc_id`, then `doc_id`) or a
  note-shaped reverse match. A `docs__` or `code__` chunk written after RDR-108 carries no
  document id, so a live document whose manifest rows were lost reads `no-owner`, not
  `legacy-unmanifested`, and the census gate passes: those chunks are reapable once they
  are 30 days old. The restore verb cannot reattach them either (a chunk cut from a file
  carries no key, so it comes back as bytes and stays hidden); re-indexing the owning file
  recovers them. The backstops are the 30 day grace, the floor, and the 14 day quarantine.
  `docs/operations/engine-reaper.md` § Known limits states it for operators.
- **Diagnosis**: `nx catalog show` reports a clean manifest while raw search
  returns two versions of one title — same signature as the original
  filing. The `catalog doctor` check for it is Phase 4 Step 14, below: as
  built it is `nx catalog doctor --visible-outside-manifest`, which probes
  census-superseded chunks (not "title with more than one live chunk", which a
  split note satisfies legitimately), for `knowledge__` collections, through
  `get` only.
- **Recovery**: over-retention is recoverable by a later reaper pass.
  Over-deletion by the reaper is now recoverable for the 14 day quarantine, because the reaper
  moves and does not delete (Step 9): `nx t3 quarantine restore` (`nexus-wbfpw.49`) moves the chunk
  back, and with `--reattach` (the default) writes the owning document's manifest row so the
  chunk is returned by search and get again, for a chunk whose own metadata names its document
  (legacy and `store_put` chunks, single-chunk legacy notes). A chunk cut from a file carries no
  such key and comes back as bytes that stay hidden until the file is re-indexed; a chunk with no
  live owner in a `knowledge__` collection stays hidden until its note is re-put under the same
  title, and one whose document was re-indexed since (`superseded`) needs nothing, since the
  document's current text is live. The verb exits 3 whenever a restored chunk stays hidden. After the 14 days `gc_expire_quarantine` deletes the chunk, and
  over-deletion of a note is then not recoverable at all. This asymmetry still sets every default
  here, unchanged from the original filing.

## Implementation Plan

### Prerequisites

- [x] Critical Assumption 3 (no consumer depends on superseded raw-search
      results) reconfirmed against usage data, not just source search, before
      Phase 2 ships `live(c)` as the search-time default with no opt-out
      (`nexus-wbfpw.3`, closed; T2 `nexus_rdr/192-research-11`).
- [x] A reproduction fixture per manifest state: manifest-less; own-collection
      live; own-collection tombstoned only; cross-collection-only manifest.
      All nine sites (post-migration: `live(c)`, `reapable(c)`, and the
      unchanged predicates 3/4/5/6) must agree on each fixture row's status
      relative to what they are each supposed to answer. Delivered as the
      engine matrix `Rdr192EngineLivenessMatrixIntegrationTest` (`nexus-wbfpw.1`) and
      the client matrix `tests/test_wbfpw2_client_liveness_matrix.py` (`nexus-wbfpw.2`).

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
force the sweep to fail (simulate the sweep-gate contention). Assert that raw
`search()` returns **only** the new text — via `live(c)`, not via the sweep
having succeeded — while the superseded row still exists physically.
Amended 2026-10-02 (Phase 3 gate, T2 `nexus/review-rdr-192-phase3-critique` S2, D1):
the reaper's next pass does NOT remove it. A chunk is reapable only after 30 days
without an owner, and the reaper moves it to quarantine and writes a
`reaper_quarantine` `gc_audit` row; it deletes nothing on that pass. So (b) is
demonstrated in two halves. The engine half is
`ChunkReaperIntegrationTest.aFailedPostCommitSweepLeavesTheOldChunk_theReaperQuarantinesItAndTheAuditRowNamesIt`: a peer holds the sweep gate through a real
advisory lock, `writeManifestMany` with `sweep=true` returns `sweep_skipped=1`, the
superseded row is still physically present and hidden by `live(c)`, and one pass with the
grace injected as zero moves it to quarantine, with an audit row that names its chash. That
the move tags what it moves (`quarantined_by: engine-reaper`) is asserted in a different
test of the same class,
`aChunkTheReaperMovedItself_isKeptAt13Days_andExpiredAtTheRetention_endToEndThroughTheRealMove`.
The client half is `tests/test_wbfpw11_reap_fail_visibility.py`:
since RDR-223 `store_put`'s own engine sweep leaves nothing to strand, it strands the
chunk with a write that has no sweep and asserts raw `search()` returns only the current
note while the census still counts the old chunk. The first production evidence is the
first `reaper_quarantine` or `reaper_refused` row at deploy plus 30 days; nothing is
reapable before then.

(c) **No-regression set**: a never-re-put note, a combined-write document,
and an imported-with-manifest chunk all stay visible under `live(c)`.

(d) **Fixture matrix**: manifest-less; own-collection live; own-collection
tombstoned only; cross-collection-only manifest — every site using `live(c)`
or `reapable(c)` gives the same answer for the same row. The one difference is
by design (Step 11, 2026-10-02): for R8 (a manifest-less current note) the
sweeps keep the chunk through their retained notes-guard arm, where `reapable(c)`
selects it and the reaper's census gate is what protects it.

All four in scope; none deferred.

Amended 2026-10-01 (nexus-wbfpw.39): (e) **Reaper between batches**. A
multi-batch re-index of an existing document, with a reaper pass between
batch 1 and the last batch: no chunk the run later writes or re-adds is
deleted (Step 9: the sweep gate exclusive per collection, and the orphaning
record, which gives the old tail a fresh grace at batch 1; there is no `indexing`
skip). The fixture seeds the old tail the way a real run leaves it, with
`last_written_at` old and no metadata key, so it fails if the record is removed.

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
`mcp/core.py` swallowed a failed catalog registration and still wrote the chunk,
`catalog/store_hook.py` then returned early on the empty `catalog_doc_id`, and a failed manifest
write returned "stored but NOT cataloged" with the chunk left behind. Each of these wrote a new
current note with no manifest row, which Step 5 would hide from search and Step 9 would later
reap. The decision (Sam, 2026-09-26) was rollback, not a marker column: a failed catalog or
manifest write must leave no chunk behind. It first shipped as a client-side rollback
(`nexus-wbfpw.28`, `nexus-k54nk`): delete the chunk only when a read-back confirms the write did
not land, never on an unknown outcome, and never a chunk another live document or a legacy note
still owns.

As built, 2026-10-02, the mechanism is different and the invariant is stronger. RDR-223
(`nexus-z0o2p.12`, `.32`) removed the step that could strand a chunk: no chunk is written ahead
of its owner. `note_write.write_note` sends the note's chunks and manifest as one
`write_manifest_many` request, so the engine commits them together or not at all, and the client
has no chunk to delete. What the client still removes is the catalog row it minted for the note
(`store_hook.rollback_minted_catalog_entry`, called from `note_write.put_note`), and only when the
note's manifest is confirmed empty, so a row that holds a landed note is never deleted. The
read-back rule survives as that confirmation; an unknown outcome removes nothing. Every note
producer (MCP `store_put`, `nx store put`, `nx memory promote`, the recovery-bundle import) goes
through `put_note`, so the invariant holds on all of them. The one thing a refused request can
leave is a metadata refresh of chunks the engine already held, never a chunk without an owner.
The tests are in
`tests/test_z0o2p12_note_write.py`:
`TestFailedRequestLeavesNothing.test_first_write_failure_leaves_no_chunk_and_no_manifest`,
`TestFailedRequestLeavesNothing.test_a_failed_reput_leaves_the_old_manifest_and_chunks_intact`,
`TestClientDeath.test_client_dies_after_the_request_no_chunk_is_without_an_owner` and
`TestPutNote.test_a_minted_row_is_removed_only_when_its_manifest_is_empty`.

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

#### Step 6: Close the silent skip (Gap 3) — log `kept`/`kept_notes` at every client site named above, **and** add an unconditional log line to the engine's `runSweepTransaction` (its `write_manifest_many_swept` event, `CatalogRepository.java:5740-5742` at filing), which had the same gate-on-`swept>0` gap

### Phase 3: `reapable(c)` and the state-derived reaper (destructive; gated on Phase 1)

#### Step 7: Ship `reapable(c)` as an inlinable engine predicate

#### Step 8: Migrate predicates 7 and 8 (`gc_quarantine_orphans`, `nx t3 gc`) to `reapable(c)`

#### Step 9: Ship the periodic engine reaper, over every collection prefix, driven by `reapable(c)` against current state, writing `gc_audit` rows (`nexus-2x9xa`)

Defaults: the grace window is 30 days, per chunk, fixed in the predicate with no
setting to tune (it is the number `nx t3 gc`'s window defaulted to before Phase 3);
the reaper runs hourly, at most 300 chunks per collection per pass.

Amendment (Sam, 2026-09-30; T2
`nexus/rdr-223-192-sam-decisions-2026-09-30-reaper-staging`, bead
nexus-wbfpw.39): the reaper covers ALL prefixes: `knowledge__`, `docs__`,
`code__` and `rdr__`. This step and the bead title said `knowledge__*` only
(the bead title is `nexus-2x9xa`, now amended). The reason is RDR-223's
multi-batch re-index: a client that dies after the first batch leaves the
previous version's dropped chunks ownerless in `docs__`, `code__` or `rdr__`,
and without the reaper they stay hidden until an operator runs `nx t3 gc`.
Per-collection gate: the reaper visits a collection only when an in-engine
census of that collection reads `legacy-unmanifested == 0`. A collection that
fails the gate is skipped with a visible refusal in the pass log, and nothing in
it is deleted. The grace window is per CHUNK, not per collection (orchestrator
ruling, 2026-10-01, recorded here; the first wording read "the collection's
`last_written_at` is older than the grace window"): a collection written to in the
last 30 days, as every collection a crashed run just wrote to is, must still have
its OLD ownerless chunks reaped, which is the premise of RDR-223's reliance on the
reaper. `reapable(c)` already is that per-chunk test, so the gate has no
collection-level recency arm.

Reaper requirements from the RDR-223 Phase 2 gate critique (T2
`nexus/rdr-223-phase2-gate-critique` S2, 2026-09-30). "Ownerless past grace is
garbage" is false while a multi-batch re-index is in flight: the old tail
chunks that a later batch re-adds are ownerless for the whole run, a writer
holds the sweep gate shared per request and not per document, and the
existence partition runs before the transaction, so a pass between batches can
delete a chunk the run is about to reference. So:

- the reaper takes the sweep gate EXCLUSIVE per collection, as
  `runSweepTransaction` does;
- it needs no in-flight-document skip: the orphaning record (vectors-021-1 and -3,
  see the Technical Design) gives an old tail chunk a fresh `orphaned_at` the
  moment batch 1 drops it from the manifest, so a pass between batches finds it
  inside the grace (a metadata-key pin on `index_state = 'indexing'` was tried and
  removed: no writer stamps the key). A document stuck `indexing` after a crash
  therefore stops protecting its chunks 30 days after the manifest change, with no
  TTL to tune;
- an MVV runs a reaper pass between batch 1 and batch k of a multi-batch
  re-index, through the real combined writer, and asserts that no chunk the run
  later references is deleted and the run completes
  (`ReapableMidRunJourneyTest` does this for the gc pair and the listing today);
- the existence-partition metadata refresh DOES bump `last_written_at`
  (answered at nexus-wbfpw.15);
- it never visits a `quarantine-*` collection (the predicate excludes it by
  `lifecycle_state`, and the reaper's collection selection must too), and a
  floor-refused quarantine chunk aged 40 days is left alone;
- R8 (a live legacy note with no manifest row) reads `reapable = true`, so the
  per-collection census-zero gate is mandatory and is re-run in the engine on every
  pass, not trusted from a stored record.

Amendment (Sam, 2026-10-01; T2 `nexus/rdr-192-reaper-quarantine-decision-2026-10-01`):
the reaper QUARANTINES; it does not hard-delete. A reapable chunk moves to its
quarantine sibling, restorable for 14 days (by `nx t3 quarantine restore`, below) and then expired (as built: by the engine's
own `reaper_expire_quarantine`, not the existing `gc_expire_quarantine`; see "As built"
below), for every prefix. The reaper's statement carries
`chunk_is_reapable(..., NULL)` in its own predicate (no list-then-delete-by-id) and
writes the `gc_audit` rows. Two requirements follow; both are built (see "As built").

- A FLOOR ON THE MOVE. A pass that would take more than a configured fraction of a
  collection is refused and reported, with `NX_GC_FLOOR_FRACTION` semantics (default
  0.25, from 100 chunks up; as built the reaper reads its own `NX_REAPER_FLOOR_FRACTION`
  and `NX_REAPER_FLOOR_MIN_CHUNKS` in the engine's environment, and `nx t3 gc` keeps
  `NX_GC_FLOOR_FRACTION` as its own client-side floor). At the time of the ruling that variable gated quarantine EXPIRY only: the client
  passes it to `expire_quarantine_serverside` (`indexer.py`), which `gc_expire_quarantine`
  enforces on the 14 day hard delete. The move (`gc_quarantine_orphans`, both forms, and
  `nx index repo`'s call of it) has no fraction floor anywhere, so the reaper's is new
  work, recorded as a requirement on `nexus-2x9xa`.
- The collection selection: enumerate collections from `nexus.chunks`, refuse visibly any
  collection with no `catalog_collections` row or a `lifecycle_state` other than live,
  and skip `quarantine-` names (the predicate's `NOT EXISTS` passes for an unregistered
  sibling); measure the predicate scan at `code__1-1` scale and put a statement timeout
  on it before an hourly run; update the RDR-223 Day-2 baseline for the one-shot cliff at
  deploy + 30 days. (`nexus-z0o2p.27` retired staging promote, the one write path that skipped the clock refresh, at `8a7831a3d`; the reaper has no dependency on it.)

As built (nexus-2x9xa, rounds 3 and 4; Sam's rulings of 2026-10-01; text only, no status
change). Where this step says the reaper's quarantine is "expired by the existing
`gc_expire_quarantine`", the design that shipped is:

- Every chunk the reaper moves is tagged in its metadata, `quarantined_by =
  'engine-reaper'` and `reaper_quarantined_at` equal to the `quarantined_at` stamp the
  move wrote. The stamp is stored twice on purpose: the restore functions strip only
  `quarantined_at` and `origin_collection`, and a client that moves a restored chunk
  again writes a NEW `quarantined_at`, so the stamps disagree and the chunk is the
  client's, not the engine's.
- The engine expires with its own function, `nexus.reaper_expire_quarantine`
  (`vectors-024-2`), and only chunks that carry a valid tag, after
  `NX_REAPER_QUARANTINE_RETENTION_DAYS` (default 14, 1 to 3650). It rechecks the
  manifest at expiry (a chunk a manifest row of the origin names is never deleted, and
  is counted as `expiry_protected`), writes one `reaper_expire_quarantine` `gc_audit` row
  naming the deleted chashes, and deletes at most 5,000 rows per origin per pass.
- **There is NO fraction floor on engine expiry.** An earlier build judged a floor on
  the tagged rows and it wedged every drain (a burst of 100 or more moved chunks ages out
  together and is all of the tagged rows). The floor on the MOVE stays; what protects a
  wrongly moved chunk is the manifest recheck, the retention window, the audit row and the
  restore verb. This supersedes this step's "same floor semantics" for expiry.
- **The split is symmetric** (`vectors-026`): the client's expiry, `gc_expire_quarantine`
  (what `nx index repo` calls, and the `gc/expire-quarantine` route with or without
  `force`), skips tagged rows and deletes only untagged, client-moved rows, with its floor
  judged on those rows alone. Each side expires only what it moved. One exception, a known gap
  (`nexus-wbfpw.58`, open): the client derives the sibling's name from the catalog row, and
  `catalog-044` rewrote that row's owner, so a client-moved chunk (untagged, which the engine's
  expiry skips) whose origin was renamed that way has no expirer on either side. The cost is
  storage: the chunk stays hidden, and `nx t3 quarantine restore` still reaches it through the
  engine-resolved sibling set.
- The engine's settings are in `docs/operations/engine-reaper.md` § Settings
  (`NX_REAPER_ENABLED`, `_INTERVAL_SECONDS`, `_BATCH_SIZE`, `_FLOOR_FRACTION`,
  `_FLOOR_MIN_CHUNKS`, `_FLOOR_EXEMPT_COLLECTIONS`, `_QUARANTINE_RETENTION_DAYS`,
  `_WALL_CLOCK_BUDGET_SECONDS`, `_CENSUS_TIMEOUT_SECONDS`). Two of them were decisions
  recorded here: `NX_REAPER_QUARANTINE_RETENTION_DAYS`, and
  `NX_REAPER_FLOOR_EXEMPT_COLLECTIONS` (`tenant/collection` entries, or a bare collection
  name that matches in every tenant), which waives the MOVE floor for the named
  collections only, logged at boot and on every pass that uses it, so a collection that is
  legitimately mostly garbage can be drained at 300 chunks per pass.
- The restore verb, `nx t3 quarantine restore`, is bead `nexus-wbfpw.49` and must be in
  the deployed engine before the first drain at deploy + 30 days.
- The decision on `nexus.chunks.last_written_at` (the 2026-09-30 comment's item 8, whether
  to drop the column once `nexus-z0o2p.24` refuses ownerless writes): KEEP. It is the only
  clock for a chunk that never had an owner (the debris of a crashed multi-request run,
  which `nexus-z0o2p.24` does not prevent), and `chunk_is_reapable`'s grace is
  `GREATEST(last_written_at, chunk_orphaned_at)`.

The post-commit-sweep-failure debris the reaper exists for is therefore reaped 30 days
after the failure, not on the next pass, and the MVV (b) reaper test injects a grace of
zero.

Step 9 deliverable, the way back (bead `nexus-wbfpw.49`, Sam 2026-10-01; engine changeset
`vectors-025`, route `POST /v1/vectors/gc/quarantine-restore`, client `nx t3 quarantine
restore`; it must be on `develop` and in the engine that is deployed before the reaper's first
drain at deploy plus 30 days). Before it the only restore, `gc_restore_rereferenced`, needed a
manifest row naming the chunk, which a chunk the reaper took wrongly lacks by definition. The
verb moves the chunks the operator names (chashes, a `gc_audit` id, or a `quarantined_at`
window) from the sibling back to the collection in one statement under the exclusive sweep
gate, never overwrites a chunk the collection already holds (`present`), starts a fresh 30 day
grace on the restored row (so the next hourly pass does not take it again), and writes one
`quarantine_restore` `gc_audit` row. A held gate or a statement past its bound is a typed,
retryable 503 with nothing moved. Restoring bytes is not enough, because since Phase 2 a chunk
with no live owning manifest row is hidden from search and get (`live(c)`). So `--reattach`,
the default, also writes the manifest row when the chunk's own metadata names a document that
is still live in the collection (the census's two owner paths, forward by `catalog_doc_id` or
`doc_id`, reverse by a single live note's own `doc_id`), at the chunk's `chunk_index` (0 for a
one-chunk document). It reaches only chunks whose own metadata names their document: chunks
written before RDR-108 and chunks `store_put` wrote (`catalog_doc_id`), and single-chunk legacy
notes (the reverse path). A chunk the indexer cut from a file carries neither key
(`metadata_schema` has no `doc_id`, `catalog_doc_id` or `chunk_index`), reads `no_live_owner` even
when its file's document is live, and comes back as bytes: the remedy for a `docs__`, `code__` or
`rdr__` chunk is re-indexing the owning file, not a re-put. It writes nothing, and reports
`superseded` with a reason, when the document is stamped complete (its manifest is verified and
authoritative, so a manifest-less chunk of it is stale, a document emptied on purpose included), is
in the middle of an index run or its last index run failed (a partial manifest), was cut from another content hash, already holds a chunk at that
position, has the position past its registered chunk count or manifest rows under another
collection, or when another stored chunk, in the collection or in the quarantine sibling, names the
same document and position (judged against what is stored, so two versions split across pages or
calls are both refused). The manifest is never changed to make room, and
`documents.chunk_count` is not touched; the output says `M of N attached` when a multi-chunk
document ends with fewer rows than it registers. A chunk with no live owner (`no_live_owner`) or
with a live owner but no knowable position (`no_position`) is restored as bytes only; the output
says plainly that it stays hidden from search and get, for a `knowledge__` collection prints
`nx store put - --collection C --title 'T'` with the owner's title and tumbler, and for a file
collection says to re-index the file; a `superseded` chunk gets neither, since its document's
current text is live. The exit status is 3 when any restored or present chunk stays hidden.
`nx t3 backfill-manifest` does nothing for this class. `--no-reattach` moves bytes only, and a
second run with reattach on attaches a chunk that is already present. The restore strips the
reaper's own `quarantined_by` and `reaper_quarantined_at` tags along with `quarantined_at` and
`origin_collection`. No end-to-end gate covers `/v1/vectors/gc/*` through the public edge
(`tests/e2e/cloud-client-path-gate.sh` asserts none of them); a leg for the restore and the
other sweep routes is bead `nexus-wbfpw.50`.

#### Step 10: Ship `nx store list --reapable`, a read-only list of the chunks `reapable(c)` currently selects for a collection, so an operator can inspect what the reaper is about to remove before it runs

### Phase 4: Cleanup (Step 11's guard removal, once gated on the backfill census, was dropped: retained by decision, Sam 2026-10-02)

#### Step 11: RETAINED BY DECISION: the notes-guard arms of predicate 4's union guard and of predicate 5 (`live_note_chashes`) stay in both sweeps

Decision of Sam, 2026-10-02 (beads `nexus-wbfpw.21` engine, `nexus-wbfpw.22` client; T2
`nexus/review-wbfpw21-22-critique`). This step was to remove the arms once the legacy-note
backfill read zero. It does not, and nothing changes in behavior:

- The engine sweep (`CatalogRepository.sweepChunksQuery`) keeps its `NOT EXISTS` over a
  live note-shaped document whose identity chash matches, and the client sweeps
  (`mcp_infra._sweep_superseded_vectors`, through `live_note_chashes`) keep their notes
  guard. `restore_pre_call_stamp` and `pre_call_doc_id_out` stay with it.
- **Removal is unsafe.** A legacy note's chunk CAN enter a sweep's dropped set: an
  unrelated document that shared the note's text, then dropped it, puts the chash there.
  `CatalogManifestSweepRepositoryTest` Order 12 builds exactly that against the real
  engine and reads the chunk kept by the notes arm alone. The client delete
  (`PgVectorRepository.delete`'s own-collection anti-join) has no notes guard of its own.
- **Gating the arm on the `rdr192-manifest-backfill` rung record is rejected.** The record
  is an attestation (it says a census read zero once, not that none has appeared since),
  nothing revokes it, and a sweep is a hard `DELETE` with no grace, no quarantine and no
  census, where the reaper re-runs the census per collection per pass. Trusting the record
  would give the sweep more destructive reach than the reaper has.
- **Cost:** bounded over-retention, and the two cases differ. A GENUINE legacy note (no
  manifest row anywhere) is kept by the sweep and is never collected: the reaper's census
  gate refuses the collection while `legacy-unmanifested` is above zero
  (`ChunkReaper.java`, the `CENSUS_LEGACY_UNMANIFESTED` refusal in the per-collection pass), so only the operator's backfill clears it. A chunk a dangling
  `meta.doc_id` stamp names, whose owner has manifest rows and so is censused as
  superseded, is kept by the sweep until the reaper (30-day grace by default, 14-day
  restorable quarantine) collects it. Over-retention is recoverable; over-deletion is not.
- The sweep and the reaper therefore differ on R8 by design (see MVV (d)).

#### Step 12: Rewrite the stale documentation (Gap 6) — `catalog-003-soft-delete.xml`'s comment and `mcp/core.py:5322-5340`

Done, 2026-10-02: `nexus-wbfpw.23` rewrote the `catalog-003-soft-delete.xml` header (a comment
outside every changeset, so no checksum moved) and `nexus-wbfpw.24` the `store_put` HISTORY
comment in `mcp/core.py`, both to the behavior as built (engine sweep, `live(c)`, the
quarantining reaper with its floor and census refusals and its defaults).

#### Step 13: Report supersession in the `store_put` result (Gap 7)

Done, 2026-10-02: `nexus-wbfpw.25`. Wire (engine, `[additive]`, in the wire-contract ledger):
each `sweep_detail` entry of `POST /v1/catalog/manifest/write_many`, `/append` and
`/append_many` gains `swept_chashes` (hex, ascending, at most 300, from the sweep DELETE's
`RETURNING`, so a dropped chash another document owns, or a live note's identity, is not in it)
and `swept_chashes_truncated`; `swept` stays the exact count. Errored entries carry `[]` and
`false`. It is not the response's `dropped_chashes`, which is the manifest's drop list. Client
(`note_write.superseded_line`, shared by MCP `store_put` and `nx store put`), one extra line
after `Stored:`:

- the sweep removed chunks: `Superseded: N chunk(s) removed: [<chash>, <chash>, <chash>] (and M
  more)`; an engine without the field gives `Superseded: N chunks removed`;
- the sweep did not finish (`sweep_skipped`, any reason: gate_timeout, statement_timeout,
  sweep_failed, before_read_failed): `Superseded: the sweep did not finish; up to N replaced
  chunk(s) were not removed. Those no other document owns are hidden from search; the engine
  reaper collects them later, once the collection passes its census and floor.` The reaper
  promise is conditional on purpose: it moves a chunk only after 30 days without an owner and
  refuses a collection that fails the floor or holds any legacy-unmanifested chunk (Step 9),
  and a genuine legacy note is never collected (Step 11). `up to N` is the manifest's drop list,
  an upper bound, since the sweep never ran; with no drop list (the previous manifest could not
  be read) the line names no count and says which chunks the put replaced is not known (an
  identical re-put replaced none, so it does not claim the replaced chunks were not removed);
- nothing removed and nothing left behind (a first put, an identical re-put, a dropped chunk
  another document owns), or a resend after a lost acknowledgement: no line.

#### Step 14: Ship a `catalog doctor` check for census-superseded chunks the normal reader still returns — the divergence signature named under Failure Modes (as built: `nx catalog doctor --visible-outside-manifest`, `nexus-wbfpw.26`)

Read narrowly: a split note legitimately has several live chunks, so the
check flags a chunk the census classes as superseded that the normal reader
can still return. As built (`nexus-wbfpw.26`):

- **Scope: `knowledge__` collections only.** The signature was filed against
  re-put notes, which live in `knowledge__`. `docs__`, `code__` and `rdr__`
  chunks sit under the same `live(c)` predicate and the same census bucket, so
  widening is a prefix-list change plus its tests (the census costs p50 23 ms).
  It was not widened: those censuses were not measured as a set, and a large
  no-owner bucket makes a collection's census cost total/300 calls.
- **Reader: `get` only, never the physical one.** The probe is a plain
  `get(ids=...)` of at most 300 chashes per call, filtered by `live(c)`; it never
  passes `include_non_live`. There is no search probe. A search probe would query
  with the superseded chunk's text, and the only read of a non-live chunk,
  `include_non_live`, returns ids and metadata and never content, by engine design
  (`VectorHandler` refuses `documents` with it; a substrate test pins that
  `get(include_non_live=True)` returns no documents for a stranded chunk). So a
  drift in the search-path functions alone, which also call `chunk_live_owners`
  (`vectors-019`), is not caught here; the engine's own tests cover that path.
- **Budgets and verdicts.** At most 5,000 chunks and a 120 s budget checked
  between calls (one call can run to the 120 s HTTP timeout, so a run can take
  about 240 s); each census page is probed as it lands, so a stop mid-paging still
  probes what was listed. A run that probes nothing before a budget stop is
  INCONCLUSIVE and exits 1. With the superseded bucket empty everywhere it reads
  "nothing to probe", exits 0, prints no PASS word and carries `vacuous: true` in
  `--json`: the steady state once the reaper has drained the bucket, and a run
  that could not have detected a regression. Not applicable (exit 0) with no
  `knowledge__` collection or an engine without the census route.
- **Operator-pull canary.** Not wired into `nx doctor`, health, CI or the release
  sandbox: it is heavy and near-vacuous on a healthy box. The one non-vacuous use
  is a single post-deploy run at D+0 or D+1 (`nexus-wbfpw.54`), from a
  cloud-mode box, asserting `checked >= 1`: production holds census-superseded
  `knowledge__` chunks until the reaper moves them at D+30, so that run is the only
  proof on real data that `live(c)` hides them through the public edge.

### Day 2 Operations

| Resource | List | Info | Delete | Verify | Backup |
| --- | --- | --- | --- | --- | --- |
| Superseded/reapable chunks | `nx store list --reapable` (Phase 3, Step 10) | In scope | In scope: the engine reaper and `nx t3 gc` QUARANTINE (restorable 14 days, then expired by the engine's `reaper_expire_quarantine` for chunks the reaper moved and by the client's `gc_expire_quarantine` for chunks the client moved) under a fraction floor on the move, and no floor on the engine's expiry (Sam, 2026-10-01; Step 9; the client keeps its floor on the untagged rows it expires itself) | `catalog doctor` check (Phase 4, Step 14) | N/A — content lives in the current chunk. Restore: `nx t3 quarantine restore` (`nexus-wbfpw.49`, Step 9), with `--reattach` so the chunk is visible again; a chunk with no live owner comes back hidden |

### New Dependencies

None.

## Test Plan

- **Scenario**: Re-put a titled note with changed content — **Verify**: raw
  search returns only the new text via `live(c)`; the old chash is absent.
- **Scenario**: The sweep is forced to fail (simulated sweep-gate contention)
  — **Verify**, in two halves (Phase 3 gate, 2026-10-02). Engine half,
  `ChunkReaperIntegrationTest.aFailedPostCommitSweepLeavesTheOldChunk_theReaperQuarantinesItAndTheAuditRowNamesIt`: the superseded row is still physically
  present and hidden by `live(c)`, and one pass with the grace injected as zero moves it
  to quarantine and writes a `reaper_quarantine` `gc_audit` row naming its chash.
  Client half, `tests/test_wbfpw11_reap_fail_visibility.py`: raw search returns only the
  current text (via `live(c)`, not via a successful sweep) while the census still counts
  the old chunk. Production shows the move at deploy plus 30 days, not on the next pass.
- **Scenario**: A genuine current note that has never been re-put —
  **Verify**: no consumer deletes or moves it, before or after the legacy-note
  backfill census reads zero. Non-negotiable at every consumer, and the protection is each
  consumer's own gate, not the predicate: `reapable(c)` itself selects a manifest-less legacy
  note once aged (matrix R8). The sweeps keep it through their retained notes-guard arm
  (`CatalogManifestSweepRepositoryTest` Order 12); the reaper refuses its collection while
  `legacy-unmanifested` is above zero
  (`ChunkReaperIntegrationTest.aStaleRecordWithAResidualLegacyNote_refusesThatCollectionAndDeletesNothing`,
  `aCurrentNoteNeverReput_isNeverMoved`); `nx t3 gc` refuses on the same census. The
  `gc_quarantine_orphans` route has no gate of its own (`nexus-wbfpw.52`): its only caller that
  moves without a census, the indexer's prune, scopes it to `code__`, `docs__` and `rdr__`.
- **Scenario**: Two documents sharing identical chunk text, one re-put —
  **Verify**: the shared chunk survives under both `live(c)` and
  `reapable(c)`.
- **Scenario**: A sweep's notes guard (retained, Step 11) or the reaper's census
  gate removes every candidate on a given run — **Verify**: the `kept`/`kept_notes`/reap
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
  collection — **Verify**: the old tail chunks the run has not yet re-added are
  not reaped (the orphaning record gave them a fresh grace when batch 1 dropped them), the run completes,
  and a collection that fails the census gate is refused visibly with nothing
  deleted.
- **Scenario** (Step 13, added 2026-10-02): re-put a titled note with changed content —
  **Verify**: the result is `Stored: ...` and a second line `Superseded: 1 chunk removed:
  [<old chash>]` (`tests/test_wbfpw25_superseded_report.py`, real engine, MCP and CLI); a first
  put and an identical re-put print `Stored:` alone; a re-put whose dropped chunk another
  document owns prints `Stored:` alone; engine half, `CatalogManifestSweepRepositoryTest`
  Order 60-62: `swept_chashes` is exactly the deleted set, the 300 lowest at the cap with
  `swept` exact, empty when nothing was deleted.
- **Scenario** (Step 13, failed sweep): the sweep errors (gate timeout, statement timeout,
  failed delete, unreadable previous manifest) — **Verify**: the engine entry is errored with
  `swept_chashes` `[]` and `swept_chashes_truncated` false for every reason
  (`CatalogManifestSweepRepositoryTest`, where each failure is forced on the real engine), and
  the client prints `Superseded: the sweep did not finish; ...` and not `removed:` (the same
  test file; the engine runs the write with the sweep off and the response is rewritten to the
  errored shape, since a real sweep failure on the shared test substrate would leak into other
  tests). A skipped sweep with a drop list known to be empty prints nothing.
- **Scenario** (Step 13, lost acknowledgement): the first attempt commits and sweeps, its answer
  is lost, the client resends — **Verify**: no `Superseded:` line is printed, since the resend
  swept nothing and the first attempt's answer is unknown.

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

Gate round 1 (2026-09-26, PASSED: 0 Critical, 6 Significant, 0 ship-blockers; commit
`141684599`; T2 `nexus_rdr/192-gate-critique-2026-09-26-r1`) found no contradiction left after
its six findings were fixed in `13bcd8e5e`. The later phase gates (Phase 2, Phase 3, Phase 4;
T2 `nexus/critique-rdr-192-phase2`, `nexus/review-rdr-192-phase3-critique`,
`nexus/review-rdr-192-phase4`) each found text that the build had overtaken, and each was
amended in place with a dated "Amended" note and a Revision History row, not left as a silent
contradiction: the client-side reap deleted by RDR-223 (Gap 3, Gap 4, Step 3a), the reaper
moving rather than deleting, the retained notes-guard arms (Step 11), the `--orphan-window`
removal, and the Phase 4 reconciliation of 2026-10-02.

### Assumption Verification

Of the four original Critical Assumptions: CA1 and CA2 were **Verified** by source reading at
filing, against `nexus-bb6n2`'s manifest-replace-then-reap sequence; RDR-223 replaced that
sequence with one `write_manifest_many` request (Step 3a), so their evidence is filing-time and
the invariant is now pinned by `CatalogManifestSweepRepositoryTest` and
`tests/test_z0o2p12_note_write.py`. CA3 (no consumer depends on superseded raw-search results)
is **Verified** (`nexus-wbfpw.3`, Sam, 2026-09-26) with the unattributable-telemetry residual
named in the assumption. CA4 is **Obsolete**: the population it was measured against no longer
exists, and Phase 1's census replaced the question with a concrete, gated decomposition
requirement. One assumption added later, that a legacy note's chunk cannot enter a sweep's
dropped set, was **REFUTED** (Step 11).

### Scope Verification

Phase 4 gate cross-walk (2026-10-02, T2 `nexus/review-rdr-192-phase4`): every Step, Gap, Test
Plan scenario and Minimum Viable Validation item has a closed bead with code and a test behind
it, or a named open carrier. Nothing is missing. The open carriers are post-deploy or follow-up
work, not unbuilt scope: `nexus-wbfpw.51` (the PITR-fork write-path rehearsal, a deploy gate),
`.54` (the Day-2 to D+44 production evidence), `.50` and `nexus-z0o2p.29.1` (edge legs and the
deploy-time rung assertion), and the follow-ups `.52` (a census gate on the `gc_quarantine_orphans`
route), `.58`, `.53` and `.48`. Where scope was reduced it was by decision of Sam and is recorded
in place: the notes-guard arms retained (Step 11), the reaper quarantining instead of deleting,
the doctor check narrowed to `knowledge__` (Step 14).

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
- 2026-10-01: Phase 3 engine halves (nexus-wbfpw.15, .16, .17) reworked after
  review (T2 `nexus/review-wbfpw15-17-code`, `-critique`). The in-flight-index pin
  on chunk metadata is removed (no writer stamps the key); the grace now means time
  since the chunk became ownerless, via an orphaning stamp in the database
  (vectors-021-2); `reapable(c)` excludes quarantine siblings by `lifecycle_state`;
  the listing route gains a keyset cursor; the unbounded gc chooses its candidate set
  once. Step 9's gate (a) is per CHUNK, not per collection (orchestrator ruling,
  2026-10-01). R8 and the 30 days of nothing-reapable after deploy are stated.
- 2026-10-01: Phase 3 engine halves reworked again after the verification review
  (T2 `nexus/review-wbfpw15-17-verify-code`, `-verify-critique`). (1) The orphaning
  stamp no longer reads the manifest: the triggers stamp every dropped chunk key and
  pre-lock the chunk rows in key order, which closes a race where two concurrent
  writers dropping the last two owners of a shared chunk each stamped nothing; the
  functions are SECURITY INVOKER. (2) The stamp is guarded by
  `last_written_at < now() - 1 hour`; measured cost, before and after, is in the
  Technical Design. (3) "Covers every path by construction" is withdrawn and the
  blind spots are listed (TRUNCATE, `session_replication_role = replica`, manifest
  DML with no tenant GUC). (4) MVV (e) no longer names an `indexing` skip. (5) Sam
  ruled on 2026-10-01 (T2 `nexus/rdr-192-reaper-quarantine-decision-2026-10-01`) that
  the reaper QUARANTINES, it does not hard-delete, with the `NX_GC_FLOOR_FRACTION`
  floor, for every prefix; the periodic reaper (`nexus-2x9xa`) quarantines with
  `chunk_is_reapable` in its own statement and takes no list-then-delete-by-id path.
  (Corrected in the next row: `nexus-wbfpw.18` is the `nx t3 gc` client verb, the reaper is
  `nexus-2x9xa`, and `NX_GC_FLOOR_FRACTION` gates quarantine expiry today, not the move.)
- 2026-10-01: Phase 3 engine halves reworked a third time (T2
  `nexus/review-wbfpw15-17-rework-code`, `-rework-critique`; Sam's option b). (1) The
  orphaning record moved out of `nexus.chunks.last_written_at` into a side table,
  `nexus.chunk_orphaned_at`, because the chunk-row UPDATE re-inserted the row into the
  1024-d HNSW index (11 s for 5000 chunks; 50 ms for the side table, measured on a 35,000-row
  table, Technical Design); `reapable(c)` counts the grace from
  `GREATEST(last_written_at, orphaned_at)`; vectors-021 and -022 were unreleased and are
  edited in place. (2) The ordered pre-lock is pinned in the live function bodies and the
  no-deadlock claim is narrowed to trigger-versus-trigger. (3) Stale prose ("loses its last
  manifest row") is corrected: every dropped key is recorded, whether or not another owner
  remains. (4) Step 9 and the Day-2 table carry the quarantine ruling; the floor on the
  MOVE is a new requirement (`NX_GC_FLOOR_FRACTION` gates expiry today); the Consequences
  bullet's clock is `last_written_at` and the record, not `created_at`; the per-path
  consequence table is added. (5) The listing route accepts an unclamped `grace_seconds`
  and is advisory below the default; its equality with the gc functions holds only when
  the grace is absent.
- 2026-10-01: Phase 2 gate minors disposed (nexus-wbfpw.35, nexus-wbfpw.37; T2
  `nexus/review-rdr-192-phase2-code` M1-M9, `nexus/critique-rdr-192-phase2` row 11).
  `text_gate_probe_<dim>`, the hybrid dispatch gate, moves onto `live(c)`
  (`vectors-023`, a P1p column in the S1a matrix), closing the tenth liveness
  definition. The maintenance readers that discover unowned chunks or guard a
  delete (`nx catalog backfill`, `nx collection reindex`'s pre-delete scan) read
  stored rows (`include_non_live`), and the backfills filter collections on
  `stored_count`. Decisions: `nexus.live_chunks` stays as a view with no
  production reader (pinned by the P2 column and granted to PUBLIC, so an operator can
  read it), it is not dropped; `fetchChunkText`, the `chroma://` permalink resolver, is a
  physical read (M7, Sam's option A: a `chroma://` permalink keeps resolving a chunk that
  no live document owns, so a link to a chunk of a trashed or superseded document does not
  break; the consequence is that such a chunk's text stays readable through the permalink,
  inside the tenant, until `purge_trash` or the reaper reclaims it, and `live(c)` does not
  hide it from that one reader); `collection_vector_stats` is the inventory, so a
  collection whose chunks are all hidden is not dormant until its chunks are
  purged. Predicate 9 keeps its own shape; Migration order item 3 records the
  difference from `live(c)` on R3 and R9 and the matrix's P9 column pins it.
  Plan evidence for the topic-scoped, hybrid, gate-probe, by-chash and
  `collection_vector_stats` paths, taken as `nexus_svc` under RLS, is in T2
  `nexus/rdr-192-live-c-explain-evidence-2026-10-01`;
  `Rdr192LiveCExplainEvidenceIntegrationTest` pins that `chunk_live_owners`
  inlines on each and that the stats view probes once per chunk.
- 2026-10-01: Phase 2 gate minors, fix rounds 2 to 4 (nexus-wbfpw.34, .35, .37; T2
  `nexus/wbfpw34-35-37-fix-round-2`, `-round-3`, `-round-4`). Decisions.
  (1) The maintenance verb that WRITES, `nx collection re-embed`, stays on live rows. A client re-write refreshes
  `chunks.last_written_at`, so for a chunk no document ever owned (R1) it would hand the
  reaper's grace window a fresh start. For a chunk owned only by a tombstoned document
  (R3) that argument does not hold under the final design of nexus-wbfpw.15 (side table
  `nexus.chunk_orphaned_at`, `reapable(c)` keyed on `GREATEST(last_written_at,
  orphaned_at)`): the chunk is not reapable until `purge_trash` drops its manifest rows,
  which stamps `orphaned_at` after any earlier refresh, and `purge_trash` keys on
  `deleted_at`. For R3 the reasons are the billed Voyage embed (re-embed) and that
  patching a chunk of a deleted document is pointless. `re-embed` reports how many stored
  chunks it left on their old vectors. (2) Reading stored chunks brings back the chunks of
  a document that was deleted and not yet purged, so every verb that registers or
  re-indexes from stored chunks (`nx catalog backfill`, `nx collection reindex`,
  `nx catalog orphan-backfill`) checks the trash first. The match is exact on the
  catalog's own path and title, never a suffix: a source path equals a tombstone's
  `file_path`, made relative with that tombstone owner's own `repo_root`, and an
  orphan-backfill title group matches on the title. Whether a matched path is dropped
  depends on the verb. `reindex` drops it only when NO live catalog document in the same
  collection names it, under any owner and in either path form, because one file can be
  catalogued under two owners (nexus-z0lu4) and the stored chunk is then one row a live
  document owns; the backfills skip it unless a live document holds it under the same
  owner (a delete and a re-index leave a tombstone and a live row at one path). And the
  guard fails closed: a catalog that cannot be read, an engine whose
  `GET /v1/catalog/trash` entries carry no `file_path`, or a `reindex` whose every source is
  a deleted document, refuses the verb. (3) `GET /v1/catalog/trash` entries carry
  `file_path` and order by `deleted_at` then tumbler, so an `OFFSET` page boundary inside a
  batch delete cannot hide a tombstone from the client's guard. The paging is still
  offset-based, so a restore or purge that lands between two pages can make the next page
  skip a row; that shows the guard one tombstone fewer (a document may be revived, nothing
  is dropped), the same effect as a delete that lands after the read, which no paging can
  see. A keyset cursor would need a new engine parameter and a client loop guard against an
  engine that ignores it, and is not done. The guard therefore needs
  an engine newer than `engine-service-v0.1.142`. `manifest_backfill` and `manifest_heal`
  write manifest rows for documents that already exist and are live, so they cannot revive
  a deleted one and are unchanged. The wire-ledger entry for the new `file_path` field
  waits for the engine commit to be published (nexus-wbfpw.35 depends on the bead that
  files it).
- 2026-10-01: Step 9 widened to the reaper as built (nexus-2x9xa rounds 3 and 4; T2
  `nexus/review-reaper-2x9xa-round3-critique` S4, `-round3-code` M1; Sam's two rulings of
  2026-10-01; text only, no status change). The engine expires only the chunks it tagged,
  with its own `reaper_expire_quarantine`, after `NX_REAPER_QUARANTINE_RETENTION_DAYS`; there
  is NO fraction floor on that expiry (it wedged every drain), the floor on the move stays;
  the client's `gc_expire_quarantine` skips tagged rows in turn (`vectors-026`), so each side
  expires only what it moved; `NX_REAPER_FLOOR_EXEMPT_COLLECTIONS` waives the move floor for
  named collections; the restore verb is `nexus-wbfpw.49`. The `last_written_at` column is
  kept (the only clock for a chunk that never had an owner). The first line of Step 9 that
  says the quarantine is "expired by the existing `gc_expire_quarantine`" and the Day-2
  table row are corrected in place.
- 2026-10-01: The quarantine restore verb and `--reattach` (bead `nexus-wbfpw.49`; T2
  `nexus/quarantine-restore-verb`, `nexus/review-quarantine-restore-code`,
  `-critique`, `nexus/quarantine-restore-round2`; Sam's decision). (1) Step 9 gains the way back,
  `nx t3 quarantine restore` (`vectors-025`, `POST /v1/vectors/gc/quarantine-restore`), which makes
  the "restorable for 14 days" of the quarantine ruling operable for a chunk with no manifest row.
  (2) The review's critical finding (a restore that moved bytes only returned text no read surface
  shows, since a chunk with no live owning manifest row is hidden by `live(c)`) is closed by
  `--reattach` for chunks whose own metadata names their document and for single-chunk legacy notes,
  and NOT for chunks cut from files, which carry no such key (see the 2026-10-02 entry below): with
  the flag, a chunk whose metadata names a live document also gets that document's manifest row, or
  `superseded` when the document's manifest already holds a different chunk there, and a chunk with
  no live owner is restored as bytes only.
  (3) The Failure Modes Recovery bullet, the Day-2 table and the risk rows for the `TRUNCATE`
  path and R8 name the verb. (4) The restore strips `quarantined_by` and `reaper_quarantined_at`,
  a held lock is a typed retryable 503, and a failure on a later page of the client still reports
  what the earlier pages committed. (5) No end-to-end gate covers `/v1/vectors/gc/*` through the
  public edge; filed as `nexus-wbfpw.50`.
- 2026-10-02: The restore verb's reach, refusals and exit status, after round 2's reviews (bead
  `nexus-wbfpw.49`; T2 `nexus/review-quarantine-restore-round2-code`, `-critique`,
  `nexus/quarantine-restore-round3`). (1) Reach stated: reattach serves chunks whose own metadata names
  their document and single-chunk legacy notes, the R8 population the census gate is meant to have
  cleared; a `docs__`, `code__` or `rdr__` chunk carries no key, comes back as bytes, and needs the
  owning file re-indexed (the output and the runbook say so; a re-put there would mint a stray note).
  The Failure Modes Recovery bullet, the risk rows and Step 9 say it too. (2) A document stamped
  complete is never a reattach target (a manifest-less chunk of it is stale, an emptied document
  included), and neither is a position another stored chunk, in the collection or in quarantine, also
  names, nor a chunk whose content hash differs from the document's: the version is ambiguous and the
  operator decides. (3) The verb exits 3 when a restored or present chunk stays hidden, and reports
  `M of N attached` for a partly attached document. (4) A deadlock is the typed retryable 503
  (`quarantine_restore_busy`) like a held lock.
- 2026-10-02: Text amended to the Phase 3 as built (Phase 3 gate, bead `nexus-wbfpw.20`;
  T2 `nexus/review-rdr-192-phase3-critique` S2, D1-D6, O2 and `-code` I-2, M2, M3, M8; text
  only, no status change). (1) MVV (b) and Test Plan row 2 no longer say the reaper's next
  pass removes the old row: it is moved to quarantine after the 30 day grace, so (b) is
  shown in two halves, the engine half `ChunkReaperIntegrationTest.java:212-253` and the
  client visibility half `tests/test_wbfpw11_reap_fail_visibility.py`. (2) The
  `--orphan-window` references (Step 9, Alternative 3, the clamp sentence) are removed; the
  flag was removed in Phase 3. (3) The reaper's floor settings are
  `NX_REAPER_FLOOR_FRACTION` and `NX_REAPER_FLOOR_MIN_CHUNKS`, not `NX_GC_FLOOR_FRACTION`,
  which `nx t3 gc` keeps for itself; the full list is the runbook's Settings table. (4) Gap
  3, Gap 4, Approach item 3 and the Risk row on the reaper duplicating the client reap are
  amended: RDR-223 deleted the client-side reap. (5) Gap 6 and Step 12 point at
  `src/nexus/mcp/core.py:5322-5340`. (6) Failure Modes gains the census no-owner blind spot
  (manifest-lost live chunks are reapable, restore cannot reattach them, re-indexing recovers
  them) and the dead-reaper mitigation now tracked as `nexus-wbfpw.56`. (7) `nx t3 gc`
  dropped `live_note_chashes` in Phase 3 for the census gate (Gap 1 item 8, Migration order
  item 4). The 2026-10-01 row above that says "skip documents `indexing` with a TTL" is
  superseded by the orphaning record in Step 9 and is left as history.
- 2026-10-02: Step 11 retained by decision of Sam (beads `nexus-wbfpw.21`, `.22`; T2
  `nexus/review-wbfpw21-22-critique`, `nexus/wbfpw21-22-r2`; text only, no status change). The
  notes-guard arms of the engine sweep and the client sweeps stay as defense in depth, instead of
  being removed once the legacy-note backfill reads zero (Migration order item 4, the Legacy-note
  backfill prerequisite, Step 11). Removal is unsafe, because a legacy note's chunk can enter a
  dropped set (Critical Assumptions, REFUTED, `CatalogManifestSweepRepositoryTest` Order 12), and
  gating on the `rdr192-manifest-backfill` rung record is rejected, because the record is an
  attestation and a sweep hard-deletes with no census. The cost is bounded over-retention, which
  the reaper collects. MVV (d) notes that the sweeps and the reaper differ on R8 by design.
- 2026-10-02: Text reconciled with Step 11 retained and Step 12 done (beads `nexus-wbfpw.23`,
  `.24`; T2 `nexus/review-wbfpw23-24-docs-critique`, `nexus/wbfpw23-24-r2`; text only, no
  status change). The Risk row, the Existing Infrastructure Audit row, the Test Plan row and
  the Phase 4 heading no longer say guard removal waits on the backfill census. Step 11's
  Cost and the Critical Assumption split two cases: a genuine legacy note is never collected
  (the reaper's census gate refuses its collection), and only a dangling-stamp chunk is. The
  `mcp_infra.py` pointers in Gap 3, Gap 4 and Migration order item 4 are re-pointed to the
  current lines, MVV (b) names the test that asserts the reaper's tag, and the Problem
  Statement gains an as-built note that the `bb6n2` store_hook reap is gone.
- 2026-10-02: Step 14 and the Failure Modes Diagnosis bullet amended to the check as built
  (bead `nexus-wbfpw.26`, round 2; text only, no status change). The check is
  `nx catalog doctor --visible-outside-manifest`: census-superseded chunks, `knowledge__`
  only, `get` reader only (no search probe, because the engine never returns text for a
  non-live chunk), bounded by a row and a time budget, operator-pull with one post-deploy run
  in `nexus-wbfpw.54`, and a vacuous run says so instead of passing.
- 2026-10-02: Step 13 built (bead `nexus-wbfpw.25`; T2 `nexus/review-wbfpw25-code`,
  `nexus/review-wbfpw25-critique`, `nexus/wbfpw25-r2`; text only, no status change). Gap 7 and
  Approach item 5 say the result is a `Superseded:` line, not a `superseded: [...]` field, and
  Step 13 and the Test Plan carry the wire shape, the three lines (removed, sweep did not
  finish, none) and the lost-acknowledgement case.
- 2026-10-02: Phase 4 gate reconciliation (bead `nexus-wbfpw.39`, `nexus-wbfpw.27`; T2
  `nexus/review-rdr-192-phase4`, `nexus/review-rdr-192-phase4-code`; text only, no status
  change). (1) Step 3a is rewritten to the as-built mechanism: since RDR-223 no chunk is written
  ahead of its owner, so there is nothing to roll back, and the client removes only the catalog row it
  minted. (2) CA3 is marked verified (`nexus-wbfpw.3`), the two Prerequisite boxes are checked
  (`.1`, `.2`, `.3`), and the Finalization Gate sections are filled in. (3) Statements the build
  overtook: the reaper's move has a floor and only the `gc_quarantine_orphans` route has none
  (`nexus-wbfpw.52`); "each side expires only what it moved" gains the `nexus-wbfpw.58` exception;
  Day-2 "none on expiry" is the engine's expiry only; Test Plan scenario 3 names each consumer's
  gate as the protection; the `reaper_quarantine_chunks` waiver in `ReapableConsumersScanTest` is
  stated. (4) Drifted file and line pointers are replaced by test and method names, and a pointer
  convention note heads the Problem Statement. (5) The did-not-finish `Superseded:` line no longer
  promises the reaper collects the chunk unconditionally, and with no drop list it no longer claims
  replaced chunks were not removed (Step 13). The operator runbook gains the rename-then-restore
  limit.
