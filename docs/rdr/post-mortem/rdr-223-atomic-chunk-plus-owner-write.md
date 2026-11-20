# Post-Mortem: RDR-223 Atomic Chunk-Plus-Owner Write on Every Client Path

> Prose: see REGISTER.md in the parent directory. The reader is the next person
> about to make the same mistake: what we expected, what happened, what to
> check first next time.

## RDR Summary

Search already hid chunks no live manifest owns (`live(c)`, RDR-192), and a reaper deleted them,
but most client paths still wrote a chunk in one request and its owner rows in a later one. A client
that died between the two left chunks nobody owned. RDR-223 set out to prevent orphans instead of
cleaning them up: every client path writes a chunk and its owner rows in one transaction (Gaps 1 and
2), the engine refuses ownerless chunk writes (Gap 3), and `nx index repo` stops writing chunks for
files with no catalog document (Gap 4).

## Implementation Status

Implemented. Every client path writes chunks and owner rows together: the combined write for small
documents, append with chunks for multi-batch ones, with the completion stamp moved after the
post-store hooks on every writer path. The engine refuses ownerless `upsert-chunks` writes in
enforce mode; the cloud has read `ownerless_write_mode=enforce` since 2026-10-12 (cloud
client-path gate, nexus-z0o2p.40), most recently in the 0.1.198 gate's B3 leg on 2026-11-20.
`store-put` and `upsert-reference-only` answer 410. The close-time critique read PARTIAL
(0 critical, 4 significant, 0 ship-blockers); three findings were text and were amended at close, and
Sam ruled on the fourth (T2 `nexus/sam-ruling-rdr223-decision3-cut-2026-11-19`).

One measurement is still running, and the RDR closed before it under Sam's batch ruling
(T2 `nexus/sam-ruling-rdr-closeout-batch-2026-11-19`): the Day 2 census exit. Bob reads the reaper
on 2026-12-03, the first day after the grace ages its first candidates (bead nexus-z0o2p.29; its
2026-12-09 deferral is only the date it resurfaces if the read has not happened). If raw no-manifest levels off or
falls and `reaper_quarantine` rows appear, the exit is met. If it keeps climbing past the 30-day
grace, it is a leak to chase. That result is appended below.

---

## Implementation vs. Plan

### What Was Implemented as Planned

- Append with chunks, the `append_many` form, and client-supplied vectors with a model and
  dimension check, on the engine (Phase 1).
- Every split client path moved onto a one-transaction write: the PDF pipeline, the `nx index repo`
  oversize fallbacks, `.nxexp` import, MCP `store_put`, `nx store put`, `nx memory promote`, recovery
  import and the markdown indexer (Phase 2).
- The ownership guard (a read before the embed, a recheck inside the write transaction), log-only
  then enforce, and its counters (Phase 3).

### What Diverged from the Plan

- **Gap 3 retired two routes instead of refusing three.** `upsert-reference-only` was retired at
  Phase 3 (Sam, 2026-10-01), and `store-put` answered 410 from 2026-10-21 (nexus-nt76j), so only
  `upsert-chunks` needed the guard.
- **One engine cut, not one per phase** (Sam, 2026-09-30), and staging was retired on measurement.
- **The completion stamp moved last on every writer path**, not only PDF and markdown (Sam,
  2026-09-30, found by the Phase 2 gate).
- **Decision 3 was half built.** `nx index repo` refuses a file with no catalog document
  (`9163f348e`), but the "find why they lack identity" half was never done, because no such file was
  ever observed. Critical Assumption 3 stayed unmeasured. The close-time critique found no record of
  Sam accepting that, so it was put to him at close, and he accepted the cut.
- **Three holes in the invariant surfaced after the build** (wren, 2026-11-15, on the Day 2 bead,
  reproduced by `OrphanChunkRaceTest`): `write_many` refreshed payload chunks no document listed,
  resetting their reaper grace; an append with two rows at one position inserted both chunks and kept
  one row; and the manifest-drop triggers skipped chunks younger than one hour, so a drop whose sweep
  then timed out left an ownerless chunk with no orphan record. Fixed in `b43cc8c44`, `efbd1d926` and
  `21bf869f5` (engine-service-v0.1.194, live 2026-11-16). The new triggers' cost on a re-index was
  recovered by `301b9451b` (a replace writes only changed rows). The RDR text was not updated until
  the close-time critique.

### What Was Added Beyond the Plan

- `chunks_unreferenced` on `write_many` and a 400 for a repeated position on append, from the
  2026-11-15 fixes.
- `vectors-061`: every manifest drop records the chunk as orphaned in the dropping transaction, and a
  re-listed chunk's record is cleared. This changed the Day 2 census: the no-manifest bucket became
  the reaper's queue, which grows until the grace ages its first candidates.

### What Was Planned but Not Implemented

- The registration-gap fix behind Gap 4 (Decision 3's second half): cut by Sam, 2026-11-19.
- A stamp-throughput measurement: Technical Design 6 cited one that did not exist. Stamp cost is
  counted in requests only.

## Drift Classification

| Divergence | Category |
|---|---|
| Two routes retired instead of refused | Over-specified code |
| Decision 3's "find why" half never built, A-1 never measured | Unvalidated assumption |
| `write_many` refreshing unlisted chunks | Missing failure mode |
| Duplicate append position keeping one row of two | Missing failure mode |
| Drop inside the one-hour window left unrecorded | Missing failure mode |
| RDR text stale for four days after the engine fixes | Missing Day 2 operation |
| A cited measurement that did not exist | Internal contradiction |

## RDR Quality Assessment

### What the RDR Got Right

- Moving the protocol, not the cleanup: after Phase 3 a dying client cannot leave an ownerless chunk
  on the guarded route, and the census became a check of that claim instead of the only defence.
- Refuse last. Clients moved first, log-only measured the residue, then enforce, so no released
  client broke.
- An operational exit (the weekly census), which is what found the three holes.

### What the RDR Missed

- The interaction of the reaper's grace window with the drop triggers. The one-hour skip predates the
  RDR, and nothing in it asked what a drop inside that hour records.
- The "metadata refresh" half of the combined write. F-2 checked what is inserted, not what is
  touched.
- Duplicate positions in one document's rows. The client refused them; the engine did not.

### What the RDR Over-specified

- Three route refusals, where two retirements were simpler.

## Key Takeaways for RDR Process Improvement

- An invariant stated as "only X is inserted" needs the same check for every row the transaction
  updates, not just the ones it inserts.
- When a Day 2 finding changes the engine, amend the RDR in the same push, not at close.
- A decision with two halves ("stop X and find why X") needs each half on its own bead, so a half that
  never gets built shows up as an open bead and not as silence.

## Follow-up (dated)

- 2026-12-03, bob: the reaper drain read on nexus-z0o2p.29 (bead resurfaces 2026-12-09 if not read). Result: _(appended when read)_.
