---
title: "Superseded store_put note chunks are permanently live: the manifest-less-is-live contract outlived its transition"
id: RDR-192
type: Bug Fix
status: draft
priority: high
author: Hal Hildebrand
reviewed-by: self (solo)
created: 2026-08-12
accepted_date:
related_issues: [nexus-39upx, nexus-b6enc, nexus-kgos1, nexus-g6k6b]
---

# RDR-192: Superseded store_put note chunks are permanently live

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

Re-putting an MCP `store_put` / `nx store put` note under the same
`(collection, title)` reconciles catalog identity correctly and leaves the
**previous chunk permanently live and searchable**. It is not merely un-swept —
it is actively *protected* from every sweep in the system, by a safety contract
that was written as transitional and whose transition has since completed.

The catalog looks clean. `query()` and catalog-scoped search follow the manifest
and return only the new content. Raw vector search (`nx search`, `search()`)
reads T3 directly and returns both versions, forever.

Observed in the field (inviscid project, 2026-08-12): three corrected knowledge
entries each left their pre-correction chunk live, and **the stale chunks ranked
above their own corrections** — 0.3220 against 0.4081, lower distance winning.
The retracted claims were more retrievable than the retractions. Remediation was
a hand-run procedure recorded in a project memo: search the title, delete stale
hashes by exact id via `nx store delete --id`. Correctness currently depends on
an operator remembering that memo.

### Enumerated gaps to close

#### Gap 1: The store_put manifest replace has no paired sweep

`catalog/store_hook.py::store_put_manifest_direct` (src/nexus/catalog/store_hook.py:492)
calls `atomic_manifest_replace` — the same write the indexer path pairs with
`_sweep_superseded_vectors` at `src/nexus/mcp_infra.py:1960` — but performs no
sweep. `_sweep_superseded_vectors` has exactly two production call sites
(`mcp_infra.py:1875`, `:1960`), both in the indexer's manifest-write path, both
gated on `any(c["position"] == 0 ...)`. The store_put path reaches neither.

`store_put`'s own MCP docstring already documents the consequence as expected
behaviour: *"the OLD T3 chunk itself is not deleted and may remain independently
visible via raw vector search until a future sweep (nexus-39upx class) reaps
it."* That sweep was built and never wired here.

#### Gap 2: `manifest-less-is-live` outlived the transition it was guarding

`service/src/main/resources/db/changelog/catalog-003-soft-delete.xml:33-39`
states the contract and its own expiry:

> Orphan chunk sweep in purge_trash: a chunk is sweepable ONLY IF it has at
> least one manifest row AND none of its manifest rows belong to a live doc.
> Manifest-less chunks (MCP store_put / nx store put notes — no
> catalog_document_chunks rows) are never swept. **Contract: manifest-backed
> identity for notes is draft RDR-145 scope; this arm is the safety contract
> until then.**
>
> live_chunks view: a chunk is visible if it has NO manifest rows at all (note
> chunk) OR has at least one live-doc manifest row.

Notes now **do** have manifest rows — RDR-145 landed and `nexus-b6enc` gave the
store_put path a fail-loud direct manifest write. The transition completed; the
transitional rule did not retire.

The interaction is now inverted. A re-put *replaces* the manifest, so the
superseded chunk loses its manifest rows and becomes manifest-less — at which
point the rule written to protect notes classifies it as a live note and
protects it permanently. The predicate cannot distinguish
manifest-less-and-current from manifest-less-and-superseded, because the note
contract has no notion of note *versioning*.

This also puts a question mark on `nexus-39upx`'s shakeout finding that
`knowledge__knowledge`'s 23.6% unjoined rate is "partly legitimate note load."
Some unknown fraction is superseded note versions — defect load counted as
legitimate, in the one collection holding hand-authored knowledge.

#### Gap 3: Raw vector search bypasses the manifest, so deletion is load-bearing for correctness

Catalog-aware paths already follow the manifest and see only current content.
The entire hazard lives in raw vector search reading T3 directly. While that is
true, *every* stale chunk is a live retrieval hazard and deletion is the only
remedy — which is why a missed sweep is a correctness bug rather than a disk-space
bug. Making retrieval current-aware would demote deletion to garbage collection
and would neutralise the historical load already in the store without deleting
anything.

#### Gap 4: The sweep's note guard can suppress the whole sweep silently

In `_sweep_superseded_vectors` (src/nexus/mcp_infra.py:1342), the note filter
`orphaned = [h for h in orphaned if h not in notes]` is followed by
`if not orphaned: return` with **no log line**. `kept_notes` is computed but
reported only on the success path. If the guard eats every candidate, the sweep
emits nothing at all.

This contradicts the function's own stated contract — *"a skip must never be
silent (nexus-39upx hazard 4)"* — and reproduces the shape of `nexus-kgos1`,
whose comment at `mcp_infra.py:1944` records the prior instance: *"It had never
deleted a row. The silence is what hid it."* Any fix to Gap 1 lands directly on
top of this blind spot.

#### Gap 5: Supersession is invisible to the caller

`store_put` returns only the new chash. A caller has no way to learn that it just
orphaned a chunk, so the only available remediation is the out-of-band procedure
described above. A returned `superseded: [...]` list would make the hazard
visible at the call site with no sweep, no deletion, and no memo.

## Context

### Background

Discovered 2026-08-12 while correcting three knowledge entries in an unrelated
project. The corrections were written, re-put under their existing titles, and
the pre-correction chunks remained independently searchable. The project's own
memo carried a hand-written hygiene procedure for exactly this, which is the only
reason it was caught.

The ranking inversion is the part that makes this more than untidiness, and it is
unlikely to be incidental. A retraction is necessarily *about* the claim it
retracts, plus qualifications, history, and hedging; the original is shorter and
more purely on-topic. Corrections are therefore systematically **less**
retrievable than what they correct. Stale-chunk leakage is not neutral noise — it
is biased toward resurfacing precisely the claims someone took the trouble to
kill, and the more carefully the retraction is written, the worse it loses.

### Technical Environment

- nx 7.6.1; T3 on pgvector Postgres (service mode) and Chroma (local).
- `catalog-003-soft-delete.xml` — `nexus.live_chunks` view, `purge_trash`.
- RDR-145 — note-backed document identity (delivered).
- RDR-191 — chunk-table unification; `collection` made a required arg on the
  store_put manifest write (Hal ruling, 2026-08-12).
- `nexus-39upx` (CLOSED) — the re-index orphan class, its in-band sweep, and the
  RDR-145 note protection that this RDR identifies as now over-broad.

## Research Findings

### Investigation

Verified by source reading in `~/git/nexus` at 7.6.1, plus live store inspection.

| Claim | Evidence | Basis |
| --- | --- | --- |
| Sweep has two production call sites, both in the indexer path | `grep _sweep_superseded_vectors src/` → `mcp_infra.py:1875`, `:1960`; all other hits are comments | Verified |
| store_put writes its manifest elsewhere, without a sweep | `store_hook.py:492 store_put_manifest_direct` → `atomic_manifest_replace`, no sweep call in file | Verified |
| A store_put entry is note-shaped | `indexer_utils.py:154 is_note_shaped` = no `file_path` AND truthy `meta["doc_id"]` | Verified |
| Note chunks are never swept | `catalog-003-soft-delete.xml:33-37`; `live_note_chashes` guard in the Python sweep | Verified |
| The contract was explicitly transitional | `catalog-003-soft-delete.xml:36-37` — "until then" | Verified |
| Note manifests now exist | `store_hook.py:492` docstring (nexus-b6enc C3/F2); RDR-145 landed | Verified |
| Stale chunk survives a same-title re-put | Three re-puts in `knowledge`, old chash retrieved by exact id afterwards; deleted manually | Verified |
| Stale chunks outrank their corrections | 0.3220 vs 0.4081, recorded in the consuming project's memo | Documented (field observation, not reproduced here) |
| No sweep telemetry in the MCP process | `grep superseded_ ~/.config/nexus/logs/mcp.log` → 0 | Verified |

### Key Discoveries

- **Verified** — The failure is *two* independent mechanisms that compound: a
  missing wire (Gap 1) and an over-broad protection (Gap 2). Fixing only the
  first yields a sweep that runs and then declines to delete anything, silently
  (Gap 4). This is the single most important finding for implementation
  sequencing.
- **Verified** — `atomic_manifest_replace` is common to both the indexer path
  and the store_put path; only the indexer path pairs it with a sweep. The
  asymmetry is the bug, and it argues for pairing at a lower level than either
  call site.
- **Documented** — The prior instance of this failure class (`nexus-kgos1`) was
  hidden by exactly the silence described in Gap 4.

### Critical Assumptions

- [ ] Re-putting a note leaves the superseded chunk with **zero** manifest rows
  (rather than a stale row pointing at it) — **Status**: Unverified
  — **Method**: Spike. `atomic_manifest_replace` semantics imply it; the whole
  of Gap 2 rests on it and it must be confirmed against both the local Catalog
  and `HttpCatalogClient` before implementation.
- [ ] `meta["doc_id"]` on the catalog row is updated to the new chash on re-put,
  and the ordering relative to any added sweep is deterministic —
  **Status**: Unverified — **Method**: Source Search + Spike. If it still holds
  the old chash when the sweep runs, `live_note_chashes` protects exactly the
  chunk being reaped, and the fix is a silent no-op.
- [ ] No consumer depends on retrieving superseded note versions from raw search
  — **Status**: Unverified — **Method**: Source Search.
- [ ] The `knowledge__knowledge` 23.6% unjoined figure contains superseded note
  versions and not only legitimate notes — **Status**: Unverified
  — **Method**: Spike (measure re-put counts against unjoined rate per collection).

## Proposed Solution

### Approach

Four changes, deliberately ordered so that the **non-destructive** ones land
first and the destructive one lands last, behind verified assumptions.

1. **Make raw search current-aware** (Gap 3). Filter raw vector search to
   manifest-current chunks by default, with an explicit opt-out for
   history/forensics. Neutralises the hazard for every stale chunk already in
   the store, deletes nothing, and demotes sweeping from correctness to GC.
2. **Return supersession from `store_put`** (Gap 5). Add `superseded: [chash…]`
   to the result. Cheapest item, and it makes the hazard visible at the call site
   even where nothing else lands.
3. **Close the silent skip** (Gap 4). Log `kept_notes` when the note guard
   removes candidates, on the same "a skip must never be silent" contract the
   module already states.
4. **Narrow `manifest-less-is-live` and wire the sweep** (Gaps 2, 1). Replace
   "no manifest rows ⇒ live" with a positive currency signal, then pair the
   sweep with the store_put manifest replace.

### Technical Design

**Currency signal.** The root defect is that *absence of a manifest row* is
overloaded: it means both "a note, by design" and "superseded, by replacement".
Those must become distinguishable. Two candidate encodings, to be settled during
implementation:

- *(a)* Retain a manifest row for note chunks and mark supersession explicitly
  (e.g. a `superseded_at` column), making currency a positive fact rather than
  an inference from absence.
- *(b)* Keep the manifest-less shape and record supersession on the catalog row
  (the existing `supersedes` catalog link type is a candidate carrier).

Preference is *(a)*: it makes the `live_chunks` view a simple predicate over a
column that exists, and it removes the overload rather than adding a second
inference on top of it. *(b)* keeps more of the current shape but leaves the
liveness predicate deriving meaning from absence, which is what failed here.

**Sweep pairing.** Rather than adding a third call site, pair the sweep with
`atomic_manifest_replace` itself so any future replace path inherits it. The
existing fail-open contract, union guard (`orphaned_chashes`), and
`index_state='complete'` circuit breaker (`nexus-g6k6b`) are reused unchanged.

**Interfaces** (signatures to verify during implementation):

```text
// store_put result gains:
//   superseded: list[str]   # chashes this put orphaned; [] when none
// raw search gains:
//   include_superseded: bool = False
```

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| store_put sweep | `mcp_infra._sweep_superseded_vectors` | Reuse — proven, fail-open, union-guarded; the gap is wiring, not capability |
| Orphan proof | `indexer_utils.orphaned_chashes` | Reuse unchanged |
| Note protection | `indexer_utils.live_note_chashes` | **Narrow** — must stop protecting superseded notes; this is the behavioural change |
| Liveness view | `nexus.live_chunks` (catalog-003) | Replace predicate — retire the transitional arm |
| Operator cleanup | `nx t3 gc` | Extend — must cover the note class it currently exempts |
| Supersession edge | catalog `supersedes` link type | Reuse if encoding *(b)* is chosen |

### Decision Rationale

Sequencing is the substance of this design. The obvious fix — "wire the sweep" —
is the one that must land **last**, because on its own it produces a sweep that
runs, is blocked by the note guard, and reports nothing. That is strictly worse
than today: the hazard persists and now looks addressed.

The non-destructive fixes also carry more value than they appear to. Making raw
search current-aware fixes the *entire historical corpus* — including the ~8,234
orphans `nexus-39upx` measured and handed off — with no deletion and no risk of
removing live knowledge, which is the failure mode that arc worried about most.

## Alternatives Considered

### Alternative 1: Sweep synchronously on every store_put, and nothing else

**Description**: Delete superseded chunks inside the put transaction.

**Pros**: Smallest change; no window during which a stale chunk exists.

**Cons**: Blocked by the note guard, so it silently does nothing until Gap 2 is
also fixed. Destructive-by-default on the one collection class holding
hand-authored knowledge. Does nothing for the historical load. Inverts the
codebase's stated preference — *"over-retention is recoverable; over-deletion is
not."*

**Reason for rejection**: It is the last step, not the first, and shipping it
alone would look like a fix while changing nothing.

### Alternative 2: Leave the store as-is; document the manual procedure better

**Description**: Keep the memo-based `nx store delete --id` remediation and make
it more discoverable.

**Pros**: Zero code change.

**Cons**: Correctness depends on an operator remembering a procedure. It was
followed once, today, only because the memo happened to be read in the same
session. It does not scale past one careful user.

**Reason for rejection**: Hygiene-by-documentation is what produced the incident.

### Briefly Rejected

- **Content-hash the title into the chunk id**: does not help — identity is
  already stable; currency is the missing concept, not identity.
- **Delete-then-put in the MCP tool**: creates a window where the entry does not
  exist, and loses the old content if the put fails.

## Trade-offs

### Consequences

- Raw search stops returning superseded content by default — a **behaviour
  change** for any consumer relying on it, and the reason `include_superseded`
  exists.
- Narrowing `manifest-less-is-live` makes a class of chunk newly deletable. The
  blast radius is `knowledge__knowledge` — hand-authored notes, the least
  recoverable content in the store.
- Retiring a transitional contract requires a schema/view change and a migration.

### Risks and Mitigations

- **Risk**: The narrowed predicate deletes a legitimate current note.
  **Mitigation**: Land the read-side fix first; keep the sweep fail-open; require
  a positive supersession signal to delete, never an inference from absence.
- **Risk**: The wired sweep is a silent no-op (the `nexus-kgos1` shape).
  **Mitigation**: Gap 4 lands before Gap 1, and the acceptance test asserts a
  *deletion*, not a green run.
- **Risk**: `meta["doc_id"]` ordering makes the guard protect the target.
  **Mitigation**: Critical Assumption 2, verified by spike before implementation.

### Failure Modes

- **Fails visibly**: sweep raises → existing fail-open path logs and skips.
- **Fails silently — the one to design against**: note guard eats all candidates
  and returns with no telemetry (Gap 4), or the sweep is never called at all
  (Gap 1, today). Both present as "clean catalog, stale search results."
- **Diagnosis**: `nx catalog show` reports a clean manifest while raw search
  returns two versions of one title. That divergence is the signature, and no
  current check surfaces it.
- **Recovery**: over-retention is recoverable by a later sweep; over-deletion of
  a note is not recoverable at all. This asymmetry sets every default here.

## Implementation Plan

### Prerequisites

- [ ] All Critical Assumptions verified — particularly assumptions 1 and 2.
- [ ] A reproduction fixture: put a titled note, re-put it changed, assert the
      old chash is still retrievable by raw search.

### Minimum Viable Validation

Re-put a titled note with changed content, then assert that raw search returns
**only** the new content, and that `store_put`'s result names the superseded
chash. Both halves in scope; neither deferred.

### Phase 1: Read-side and observability (non-destructive)

#### Step 1: Reproduction fixture

Pin today's behaviour as a failing test before changing anything.

#### Step 2: Current-aware raw search + `include_superseded` opt-out

#### Step 3: `superseded: [...]` in the store_put result

#### Step 4: Log `kept_notes` on the guard-suppressed path

### Phase 2: Currency signal and sweep (destructive; behind verified assumptions)

#### Step 5: Positive supersession signal; retire the transitional arm of `live_chunks`

#### Step 6: Pair the sweep with `atomic_manifest_replace`

#### Step 7: Extend `nx t3 gc` to the note class it currently exempts

### Day 2 Operations

| Resource | List | Info | Delete | Verify | Backup |
| --- | --- | --- | --- | --- | --- |
| Superseded note chunks | In scope (`nx store list --superseded`) | In scope | In scope (`nx t3 gc`) | In scope (catalog doctor check) | N/A — content lives in the current chunk |

A `catalog doctor` check for "title with more than one live chunk" is in scope:
it is the divergence signature named under Failure Modes, and nothing currently
detects it.

### New Dependencies

None.

## Test Plan

- **Scenario**: Re-put a titled note with changed content — **Verify**: raw
  search returns only the new text; old chash absent.
- **Scenario**: Same, with `include_superseded=true` — **Verify**: both versions
  returned.
- **Scenario**: A genuine current note that has never been re-put —
  **Verify**: never deleted by any sweep or GC pass. Non-negotiable.
- **Scenario**: Two documents sharing identical chunk text, one re-put —
  **Verify**: the shared chunk survives (union guard holds).
- **Scenario**: Note guard removes every candidate — **Verify**: `kept_notes`
  is logged; no silent return.
- **Scenario**: Sweep wired but `meta["doc_id"]` still old at sweep time —
  **Verify**: test fails loudly rather than passing as a no-op.
- **Scenario**: Collection containing a non-`complete` document —
  **Verify**: circuit breaker refuses (`nexus-g6k6b` precondition holds).

## Validation

### Testing Strategy

1. **Scenario**: The MVV above, run against both the local Catalog and
   `HttpCatalogClient`. **Expected**: identical behaviour on both backends —
   the store_put manifest write already claims parity, and this must not be the
   place the claim first fails.
2. **Scenario**: Corpus measurement before and after Phase 1, per collection.
   **Expected**: the unjoined-rate figure for `knowledge__knowledge` decomposes
   into legitimate notes vs superseded versions, settling the open question from
   `nexus-39upx`'s shakeout.

### Performance Expectations

The `live_chunks` EXPLAIN evidence in catalog-003 shows SubPlan 2 short-circuits
for manifest-less chunks, so today's hot path never fires the live-doc join for
notes. A currency predicate changes that path and its cost must be re-measured
against the same production shape, not assumed.

## Finalization Gate

> Complete each item with a written response before marking this RDR as
> **Accepted**.

### Contradiction Check

To be completed at gate.

### Assumption Verification

Four Critical Assumptions are currently **Unverified**. Assumptions 1 and 2 are
load-bearing for Phase 2 and must be verified by spike before it begins; Phase 1
does not depend on them.

### Scope Verification

To be completed at gate.
