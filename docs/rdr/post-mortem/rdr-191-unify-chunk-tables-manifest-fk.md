# Post-Mortem: RDR-191 — Unify the Dim-Sharded Chunk Tables, Enable the Manifest FK, Retire the Client-Side Integrity Apparatus

**Closed**: 2026-08-15 (implemented) · **Epic**: nexus-o8dil (all children closed) · **Accepted**: 2026-08-10 (gate PASSED run 5 after 4 BLOCKED)

## Outcome vs design

The core claim held: the three dim-sharded chunk tables existed for exactly
one reason (three pgvector column types), and one table with three nullable
typed embedding columns makes the manifest FK expressible. Shipped:

- **Phase 1** (c84480ec, 2026-08-10): prune pushed into SQL anti-joins,
  keyed on the natural chash. 11-17x faster; zero embeddings cross the wire.
- **Phase 2** (GATE-2, P0): zero live producers of dangling or
  NULL-collection manifest rows.
- **Phase 4** (engine-service-v0.1.75, deployed 2026-08-14): `nexus.chunks`
  with three FULL HNSW indexes; always-copy migration; 385,484 rows exact,
  ~627 MB reclaimed on cloud.
- **Phase 5** (engine-service-v0.1.76, deployed 2026-08-15): the manifest FK
  as a three-step Liquibase sequence (NOT VALID → logged anti-join
  remediation → VALIDATE), the unified collection FK, NULL-distance guards
  on the 9 combined-query functions. Both FKs `convalidated=true` on cloud,
  dangling rows 0 estate-wide, gate 0/113 queries moved.
- **Phase 6** (3b2901141): the subtraction — Decision item 4's list deleted
  item by item, net −3,845 lines, both exclusions pinned by tests. Its
  engine half (catalog-030, DROP of three dead SQL functions) is on develop
  and rides the next cut.
- **Phase 7**: already shipped as catalog-025 (nexus-71gw2, 2026-08-12)
  before Phase 5 planning reached it — see divergence 3.

RDR-156 Decision 2 ("do NOT FK the manifest to the chunk tables") is
superseded, as this RDR set out to do.

## Divergences from the accepted text (all disclosed at the time)

1. **Four draft positions reversed by measurement before acceptance** (in
   the T2 record and RDR body): three FULL HNSW indexes not partial (F13:
   partial buys 0.11% and seq-scans 250x); FK is `ON UPDATE CASCADE` +
   deferrable delete side, not `ON DELETE RESTRICT` (F8/F10); NULL-collection
   rows are not a blocker under `MATCH SIMPLE` (C1/F6); always-copy migration
   not adopt-in-place (F18, Hal direct — a determinism trade that cost ~85s
   over the faster path).
2. **Amendment (xi), disposition (a)** (2026-08-14, Hal direct): Phase 5's
   drafted manual, per-deployment remediation procedure was replaced by an
   in-Liquibase anti-join DELETE between NOT VALID and VALIDATE, closing the
   second boot-brick (amendment x) by construction. Priced against fk-002's
   destructive-arm refusal: that refusal protects orphan *chunks* (real data);
   dangling *manifest* rows are child bookkeeping referencing nothing.
   Corollary: beads .25/.26/.27/xehfx collapsed into the changeset.
3. **Amendment (xii) and its same-day correction**: Phase 7 was folded into
   the Phase 5 cut, then discovered to have already shipped as catalog-025.
   The fold decision was made against a stale bead board (.35/.36/.37 open
   while the work was done — the same stale-open class as .21/.22, also
   found and closed that day). The drafted catalog-030 reinforcement was
   verified vestigial and dropped; a standing pin asserts NOT NULL post-walk.
4. **Phase 6 keep-with-reason**: `nexus.manifest_verify(text)` (single-doc
   form) survived the list — `completeIndexRun` uses it for a write-path
   count-agreement check the FK does not guarantee (existence ≠ count
   agreement). Critic-adjudicated as a real distinction, not rationalized
   under-deletion.
5. **Two edge-case beads killed by directive** (61mkk, zlv38): technically-
   true findings about populations no one has observed, each with a trivial
   occurrence-time remedy (reindex/delete/re-run). Recorded in
   `feedback_no_preventive_scope_beyond_evidence`.

## What worked

- **The mandatory boot test paid for itself before merge.** Its first red
  looked like the anti-join over-deleting; root cause was the *test's* own
  verification reads running GUC-less under FORCE RLS. Without the
  falsification sibling (bare VALIDATE must throw against the same seeded
  population) the changeset's remediation step would have been theater.
- **The pre-deploy census arithmetic closed exactly on cloud**: predicted 2,
  boot-logged 2, observed 2. The boot-logged NOTICE is the decisive number
  (no concurrent writer can move it); the row delta is corroboration only.
  This is why catalog-029-1 logs its count and why fk-004 needs one
  (nexus-iq0qr).
- **The FK caught a real producer within minutes of deploy** — the partner
  instance's own STEP-6 fixture had been writing dangling rows on every run.
  Enforcement working; the fix is chunk-first, never a deferred constraint.
- **The stacked review found the Critical every round**: over-claimed
  runbook tooling (F10d), a phantom-independence test, a doc still
  describing a retired finding class as live. Never one reviewer.
- **The published-client write gate** proved 7.7.0 compatible with the FKs
  pre-tag — the leg that would have caught the v0.1.73 sh9v2 incident.

## What to carry forward

- **A stale bead board is a planning hazard.** Three phases' worth of beads
  (.21/.22/.35/.36/.37) were open for work already shipped; one decision
  (the Phase 7 fold) was made on that basis. Sweep the epic against git and
  the changelog before every phase-planning pass.
- **Same-name objects across substrates.** `manifest_backfill` named both a
  dead SQL function (dropped) and a live Python module (3n7pr's remediation
  tool). A bead's wording nearly caused over-deletion; a partner's alarm was
  aimed at the right name and the wrong object. Name the substrate.
- **Fix propagation across dual surfaces (Java vs SQL functions) does not
  happen by itself** — the gjwhu class. Grep the other substrate when fixing
  a query shape.
- **runAlways + era-gated bodies means boot 1 and boot 2 can differ** (the
  be0xb narrowing). Treat runAlways body edits as behavior changes.
- **A tag gates delivery, not work.** Everything on develop was tested
  against the dev jar; the deploy is its own cadence and never a condition
  on the next task.

## Residuals (tracked)

- nexus-iq0qr P3 — fk-004-1 reconcile logs no row count; NOTICE rides the
  next cut as a new changeset.
- nexus-3n7pr P2 — the 910 zero-manifest documents; remediation via the
  Python `manifest_backfill.py` (gvmbo/b91tv shipped) must now be FK-aware.
- nexus-4lnn1 (deferred to 2026-09-04) — HNSW REINDEX cadence, waits on
  conexus-jjxp steady-state drift data.
- catalog-030 (Phase 6 engine half) — on develop, rides the next engine cut
  with iq0qr; behavior-neutral (no remaining callers, partner-confirmed).
