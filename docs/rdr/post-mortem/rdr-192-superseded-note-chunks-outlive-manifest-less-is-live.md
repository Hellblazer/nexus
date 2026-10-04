# Post-Mortem: RDR-192 Superseded store_put note chunks are permanently live

**Filed** 2026-08-12 · **Accepted** 2026-09-26 · **Closed** 2026-10-04 · **Epic** nexus-wbfpw (76 numbered children, plus nexus-2x9xa and nexus-0rxvg) · **Shipped** engine-service-v0.1.143 (first deploy with the reaper, 2026-10-02), v0.1.144, v0.1.145, v0.1.146 (2026-10-04), with the paired conexus releases through 7.71.0

## RDR Summary

A chunk is one stored piece of text in T3 (the vector store), identified by its
chash (the SHA-256 of its text). A manifest row joins a chunk to the catalog
document that owns it. The RDR found that nine places in the client and the
engine (the Java service over Postgres) each answered "is this chunk current"
for themselves, and that a chunk with no manifest row was treated as live by
search. So any sweep that failed partway left the replaced text searchable, and
it could outrank its own correction.

The fix it proposed: one engine predicate for visibility, `live(c)` (a chunk is
visible only while a live document's manifest names it in the same collection);
one for garbage, `reapable(c)` (manifest-less past a grace window); and a reaper,
an hourly engine task that recomputes the garbage from current state rather than
retrying a lost sweep. Non-destructive phases first, the destructive reaper last,
behind a census (a classification of every manifest-less chunk by cause) that
had to read zero for the one class that would wrongly be reaped.

## Implementation Status

Implemented. All seven gaps are closed, all four phase gates passed (nexus-wbfpw.8,
.14, .20, .27), and the reaper has run hourly in production since 2026-10-02. It
moves nothing until about 2026-11-01, because nothing is reapable until a chunk
has been ownerless for 30 days.

Four items were detached from the epic by Sam's decision on 2026-10-04 so the RDR
could close (nexus-wbfpw.54 carries its detach comment of that date). They continue as standalone beads:

- nexus-wbfpw.54: the Day-2 checkpoints, counted from the 2026-10-02 deploy: a
  preview at D+19, the first move and refusal rows in the audit table at D+30,
  and the first expiry rows at D+44.
- nexus-wbfpw.61: measure the first cold move pass in production. On a
  point-in-time-recovery copy of production (a "PITR fork"), a 300-chunk move
  took 20.5 s against a 25 s statement bound.
- nexus-wbfpw.63: a comment reword in `ChunkReaper.java` and the vectors-024
  header ("equal to" becomes "not below").
- nexus-wbfpw.64: let the engine resolve quarantine siblings inside two more
  routes. Its production value is nil today; the population it would reach is empty.

Production evidence on closing day: engine-service-v0.1.146 (vectors-027, -028, -029) went
live at 19:02Z, paired with conexus 7.71.0, and `nx catalog doctor
--visible-outside-manifest` run against production through the public edge passed: 139 of
139 census-superseded chunks, across 34 `knowledge__` collections, were hidden by `live(c)`.
That is Step 14's "only proof on real data". The cloud-client-path gate legs J and K also
passed through the edge (9/9). The reaper half has no production evidence yet (below).

nexus-wbfpw.59 (restore cannot reach chunks of a collection renamed within the
14-day retention) was closed as documented, with the manual path in
`docs/operations/engine-reaper.md`.

---

## Implementation vs. Plan

### What Was Implemented as Planned

- `live(c)` as one inlinable SQL predicate (Step 4, nexus-wbfpw.9) on every
  content read, with `nexus.live_chunks` scoped to its collection (Step 5,
  nexus-wbfpw.10). Gaps 1, 2 and 5 closed.
- The census as an engine route plus an `nx` verb (nexus-wbfpw.4, .5), run on the
  live tenant: 542 manifest-less chunks of 337,499, and the one legacy note
  backfilled to zero (nexus-wbfpw.6, .7).
- The silent-skip logging at the client sweep sites and in the engine's sweep
  (Step 6, nexus-wbfpw.12, .13).
- `reapable(c)` (Step 7, nexus-wbfpw.15), `gc_quarantine_orphans` and `nx t3 gc`
  moved onto it (Step 8, .16, .18), the listing route and `nx store list --reapable`
  (Step 10, .17, .19).
- The state-derived reaper over every collection prefix with an audit row per
  action (Step 9, nexus-2x9xa), the hourly cadence, 300 chunks per collection per
  pass, a per-collection census gate re-run on every pass.
- The stale comments rewritten (Step 12), a `Superseded:` line in the `store_put`
  result (Step 13), and the operator check
  `nx catalog doctor --visible-outside-manifest` (Step 14).
- The fixture matrices for engine and client (nexus-wbfpw.1, .2) held every
  predicate to one verdict per fixture row through the migration.

### What Diverged from the Plan

- **The reaper quarantines; it does not delete.** The RDR planned a reaper that
  deletes. Sam ruled on 2026-10-01 that it moves a chunk to its quarantine sibling
  (a parallel `quarantine-<collection>` holding moved chunks), restorable for 14
  days, then expires it. That ruling created the restore verb (nexus-wbfpw.49), a
  floor on the move, and the question of who expires what, which took most of
  the later bug beads.
- **The grace clock moved twice.** The plan keyed the 30-day grace on
  `chunks.created_at`. That column is write-once, so a re-upserted old chunk got no
  fresh grace and the reaper could delete it between a re-index's upsert and its
  manifest write (nexus-wbfpw.43). The fix added `last_written_at`. A review then
  showed that an old chunk dropped by batch 1 of a multi-batch re-index is
  ownerless for the whole run, so the grace now counts from when the chunk lost
  its owner. Stamping that on the chunk row re-inserted it into the 1024-d HNSW
  index (the approximate nearest-neighbor index pgvector uses for vector search;
  11 s per 5,000 chunks), so the stamp moved to a side table,
  `nexus.chunk_orphaned_at` (50 ms).
- **The deploy census was the wrong census.** The plan made "the manifest-less
  census reads zero" the condition for shipping `live(c)`. A walk on a PITR fork
  showed `live(c)` would hide 13,151 chunks in the main tenant and all 116,492 in
  the `gate-xr789` tenant, which the census never ran on. The census counted any
  manifest row as ownership; `live(c)` requires a document that is not
  tombstoned (soft-deleted). The deploy condition became a census on the exact
  `live(c)` test, per tenant (nexus-wbfpw.32). Sam let the tombstoned-owner chunks
  go; `gate-xr789` was re-seeded with owners.
- **`.nxexp` imports needed an owner design.** An `.nxexp` file is `nx store
  export`'s portable snapshot of a collection. The RDR treated imports as a census
  bucket. Under `live(c)` an ownerless import is invisible, so the import now
  registers an owner first (nexus-wbfpw.31). Its first version parsed the owner
  from the collection name and aborted on 15 collections (nexus-wbfpw.33). A
  second version replaced an existing document's manifest with the file's rows,
  which hid that document's other chunks (nexus-wbfpw.40). Sam ruled that an import
  never replaces or extends an existing owner's manifest.
- **Step 3a's rollback was overtaken.** The plan had `store_put` delete its own
  chunk after a failed catalog or manifest write (shipped as nexus-wbfpw.28).
  RDR-223 then made a note's chunks and manifest one engine request, so there is
  no chunk to roll back; the client removes only the catalog row it created.
- **Step 11 was retained, not done.** The plan removed the sweeps' legacy-note
  guard once the backfill read zero. A test built the case the RDR's Critical
  Assumption ruled out (a legacy note's chunk entering a sweep's drop set through
  an unrelated document that shared its text). Sam kept the guard permanently
  (nexus-wbfpw.21, .22).
- **Step 13 is a line, not a field.** `store_put` returns a plain string, so a
  `superseded: [...]` field would have changed its return type. The result gains a
  second line instead.
- **The route-level floor is a wrapper.** A floor here refuses a move that would
  take more than a fraction of a collection (default 25%, from 100 chunks up). The
  engine floor on `gc_quarantine_orphans` (the engine function that moves
  ownerless chunks to quarantine, called by `nx t3 gc` and `nx index repo`; nexus-wbfpw.52) was built as a wrapper
  function, `nexus.gc_quarantine_orphans_floored` (vectors-027), rather than new
  parameters on the released functions. `nx t3 gc` keeps its client-side listing
  check even against an engine that echoes the floor, because the echo arrives
  only with the move (follow-up nexus-teduy). The floor applies to the first
  bounded batch only, and refused-move audit rows are deduplicated within an hour.
- **"Each side expires only what it moved" broke for knowledge.** The design split
  quarantine expiry: the engine expires rows the reaper tagged, the client
  (`nx index repo`, `nx t3 gc`) expires rows it moved itself (vectors-026). But
  `nx index repo` never runs on `knowledge__*` collections, so client-moved
  knowledge quarantine waited for a hand-run `nx t3 gc` and its floor. Sam decided
  on 2026-10-04 that the engine also expires those rows (nexus-wbfpw.75,
  vectors-028), scoped by the registry's content type and by checks that the
  sibling is a registered quarantine collection and the origin is live. The user's
  `NX_GC_QUARANTINE_DAYS` no longer governs them; nexus-alsq3 would stamp the
  client's retention on the row at move time. Non-repo `docs__`/`rdr__` quarantine
  still has no automatic expirer (nexus-b6ags).
- **The fraction floor came off expiry on both sides.** The engine's expiry floor
  wedged every drain: a bulk move ages out as one burst, which is close to 100% of
  the tagged rows. It was removed before release. The client's floor then wedged
  the same way in production (11,837 rows force-expired on Sam's decision,
  nexus-wbfpw.69) and was removed too (nexus-wbfpw.74). The floor stays on the move.

### Existing Infrastructure Reused Instead of New Code

- The reaper takes the same per-collection advisory lock as the post-commit sweep
  (`runSweepTransaction`), so it cannot run between a writer's batches.
- Moves go through the existing quarantine siblings and `gc_audit` (the engine's
  audit table for every garbage-collection action), not a new store.
- The legacy-note fix is the existing `manifest_backfill`, behind a new upgrade
  rung (`rdr192-manifest-backfill`, a step `nx upgrade` runs once per tenant and
  records as complete).
- The "recompute from state, do not persist the drop set" design follows
  nexus-iygza, the taxonomy drain from the same indexing-brittleness proposal.

### What Was Added Beyond the Plan

- The restore verb `nx t3 quarantine restore`, with `--reattach` to write the
  manifest row back where the chunk's own metadata names its document
  (nexus-wbfpw.49), and engine-side sibling resolution for it (nexus-wbfpw.55).
- Reaper liveness reporting: `Throwable` caught at every scheduled task, a
  `reaper{}` block on `/v1/status`, and an `nx doctor` row for a stale pass
  (nexus-wbfpw.56).
- Search settings for pgvector (`hnsw.max_scan_tuples`, a scan memory budget) so
  recall (the share of the true nearest neighbors a search returns) holds when
  most of a region is hidden (nexus-wbfpw.47).
- Quarantine guards on collection delete, store-delete, rename and rehome, with
  audit rows (nexus-wbfpw.71), and Sam's ruling that rows whose origin collection
  is gone are removed only by a deliberate, audited collection delete
  (nexus-wbfpw.68).
- An empty tenant passes the reaper's backfill gate (nexus-wbfpw.73).
- The hybrid search text gate rebuilt to use its indexes under row-level security
  (nexus-wbfpw.48; see below).
- Cloud gate legs for the `/v1/vectors/gc/*` routes through the public edge
  (nexus-wbfpw.50), and `DeadlockRetry` around every manifest write (nexus-wbfpw.66).

### Ratified by Sam on 2026-10-04

The closure critique found four readings of the build that were not recorded as decisions of
Sam. He ratified each as the design of record (RDR Scope Verification):

- Predicate 9 (`taxonomy_unassigned_chashes`) stays as it is, though it differs from `live(c)`
  on R3 and R9.
- The indexer's non-combined client sweeps (`_sweep_superseded_vectors[_many]` in
  `mcp_infra.py`) still hard-delete, fail open and write no `gc_audit` row; the reaper
  recovers their misses after the 30-day grace.
- The `chroma://` permalink resolver reads physical chunks, a deliberate exception to Gap 2.
- The sweeps and the reaper answer R8 (a manifest-less current note) differently, by design.

### What Was Planned but Not Implemented

- Step 11's guard removal, retained by decision (above).
- Consolidating `indexer_utils.orphaned_chashes` onto `reapable(c)`: the Infrastructure Audit
  row listed it, but predicate 5 kept its shape (it is still present and fails open; ratified,
  above).
- The production half of MVV (b): the first `reaper_quarantine` audit row cannot
  exist before about D+30. It is carried by nexus-wbfpw.54, outside the epic.

### The hybrid text gate (nexus-wbfpw.48)

Found while measuring query plans, not caused by `live(c)`. Production serves as
the role `nexus_svc` under FORCE row-level security (RLS: Postgres adds a tenant
filter to every query, even for the table owner). Postgres will not use an index
for an operator that is not marked leakproof (proven not to reveal row contents
through errors) on a table with a security filter, and the full-text and trigram
operators are not. So the gate scanned every row of the tenant: 77 ms against 2 ms
with RLS bypassed, on one 24,000-chunk fixture.

The fix (vectors-029) makes the probe functions SECURITY DEFINER (they run with
their owner's rights and apply the tenant filter themselves) and adds a SELECT
policy for the owner. The first version had a hole the security review rated
critical: where one role both migrates and serves, `nexus_svc` would be the owner,
and the new policy would let it read every tenant. The changeset (one schema migration step, applied by
Liquibase at engine boot) now skips itself, through a MARK_RAN precondition, in
that posture, a post-condition aborts the
walk if the policy would reach the serving role, and the engine refuses to boot if
any extra permissive policy applies to `nexus_svc`. `/v1/status` reports
`chunks_tenant_isolation_intact`. Measured on the PITR fork: a rare token in one
collection 1,176 to 93 ms, across all collections 25.7 s to 157 ms, a common token
1,032 to 514 ms. Production client p95 for hybrid search fell from 1,724 to 1,161 ms.

### Process incidents

Two develop reds during the final burndown, both from scoped pre-push runs: a
repo-wide lint that a scoped lint run did not reach (fixed in 70b29f6b0), and an
engine `*ScanTest` that is not in the `*GateTest` set run before push (fixed in
078774e2f).

---

## Drift Classification

| Category | Count | Examples | Preventable? |
| --- | --- | --- | --- |
| **Unvalidated assumption** | 4 | Census zero implies `live(c)` keeps every wanted chunk (.32); a legacy note's chunk cannot enter a drop set (Step 11, refuted by test); "ownerless past grace is garbage" during a multi-batch re-index (RDR-223 critique, .42); `live(c)` keeps recall at high dead fractions (.44, .45, .47) | Yes, spike: each was a query against a fork or a fixture |
| **Framework API detail** | 3 | Non-leakproof operators cannot be index conditions under RLS (.48); pgvector's scan cap starves filtered HNSW search past 95% dead (.47); updating a chunk row re-inserts it into the HNSW index (side table) | Yes, spike |
| **Missing failure mode** | 9 | Census counted join rows, not chunks (.60, then .62 for released engines); import aborted on non-numeric owner names (.33); import replaced a live manifest (.40); write-once grace clock (.43); restore and reaper named the sibling differently after a catalog rewrite (.55, .58); mixed indexer flush hid chunks and exited 0 (.34); manifest writes not under deadlock retry (.66) | Mostly yes, source search; .55 and .58 needed knowledge of catalog-044's owner rewrite, landed after acceptance |
| **Missing Day 2 operation** | 9 | Local installs got `live(c)` with no census or backfill (.41); a dead reaper was invisible (.56); every bulk move wedged the expiry floor (.74, .69); knowledge quarantine had no automatic expirer (.75); dead-origin rows had no expirer (.68); delete and rename moved quarantine rows unaudited (.71); empty tenants refused forever (.73); restore after rename (.59) | Yes, source search for .41, .56, .71, .73; .74 was predictable from arithmetic once quarantine was chosen |
| **Deferred critical constraint** | 1 | First production move at D+30, after close (.54, .61) | No: inherent in a 30-day grace |
| **Over-specified code** | 2 | Step 3a client rollback (overtaken by RDR-223); Step 13 result field | No: RDR-223 and the string return type were not visible at filing |
| **Under-specified architecture** | 3 | Delete versus quarantine; who expires which quarantine rows (vectors-026, then .75); where the fraction floor applies (move, expiry, route: .52, .74) | Yes, at design review: all three follow from choosing quarantine |
| **Scope underestimation** | 2 | Step 9 grew into a quarantine lifecycle (restore, expiry, dead origins, audit guards: about 25 beads); `.nxexp` owner design (.31, .33, .40) | Partly |
| **Internal contradiction** | 0 | | |
| **Missing cross-cutting concern** | 3 | Tenant coverage: the census ran on one tenant (.32), empty and service tenants blocked the reaper (.73); security posture of the RLS fix in a single-role migration (.48, review C1); release notes for engines that shipped the census miscount (.62) | Yes, source search |

### Pattern References

No `SYNTHESIS.md` exists in `docs/rdr/post-mortem/`, so none is referenced. Two
patterns repeat within this RDR. A gate that measured a proxy instead of the
predicate it guarded (.32 census versus `live(c)`, .60 join rows versus chunks).
And a safety floor placed where a bulk event always trips it (engine expiry
floor, then client expiry floor).

---

## RDR Quality Assessment

### What the RDR Got Right

- Ordering non-destructive work first, with the destructive reaper behind a
  census. Every serious defect found (.32, .40, .43, .55, .60) surfaced before
  anything was deleted, and the quarantine ruling means nothing has been deleted
  that cannot come back.
- Making liveness one engine predicate. Gap 2 closed for every stale chunk already
  in the store, with no deletion.
- Recomputing from state instead of persisting failed sweeps. No later bead
  questioned it.
- The fixture matrices, which pinned every predicate's verdict per manifest state
  and caught drift in each migration.
- Phase gates with a scope cross-walk. Each gate filed its findings as beads under
  the epic, and none recorded a silent scope reduction.

### What the RDR Missed

- The quarantine lifecycle. Once moves are reversible, expiry, restore, retention,
  dead origins and audit coverage become part of the design. The RDR had one
  Day-2 row for them.
- Tenants other than the main one: the census, the deploy condition and the
  reaper's backfill gate were all first designed against a single tenant.
- The interaction with RDR-223's multi-batch writes, raised by RDR-223's own
  critique after acceptance.
- Postgres and pgvector behavior under the new predicate: index use under RLS and
  filtered HNSW recall at high dead fractions.

### What the RDR Over-specified

- **Code samples rewritten**: none of significance.
- **Deferred feature code unused**: none.
- **Config/schema never implemented**: the `superseded: [...]` result field.
- **Performance targets unvalidated**: none set; the RDR asked for re-measurement,
  which was done (.9, .36, .37, .44, .45).
- **Alternative analysis disproportionate**: no.
- **File and line pointers**: a dozen drifted within a week and were replaced by
  test and method names.

---

## Key Takeaways for RDR Process Improvement

1. **Gate on the predicate itself, not a proxy for it**: the deploy census and the
   reaper's scope count both measured something near the thing they protected, and
   both were wrong by thousands of chunks. When a gate protects predicate P, the
   gate's query should call P.
2. **When a design makes deletion reversible, write the reversal's lifecycle in the
   RDR**: restore, retention, who expires what, and what happens when the origin is
   renamed or deleted. Here each of those arrived as a separate bug after the
   ruling.
3. **Put safety floors where single events are normal-sized**: a fraction floor on
   a move catches a misclassification; on expiry, a bulk move always trips it.
   Ask, for each floor, which routine event reaches 100%.
4. **Enumerate tenants and install modes in the census and deploy conditions**:
   the main tenant, gate tenants, empty and service tenants, and local installs
   each needed their own answer.
5. **Run the full gate and scan test sets before pushing engine changes**: a
   scoped pre-push run missed a repo-wide lint and a `*ScanTest`, and develop went
   red twice in one day.
