---
title: "Retire the SQLite T2 Backend: Make the PG Service the Only T2 Path"
id: RDR-158
type: Architecture
status: draft
priority: medium
author: Hal Hildebrand
reviewed-by: self
created: 2026-06-12
related_issues: [nexus-luxe6, nexus-gmiaf]
related: [RDR-152, RDR-153, RDR-154, RDR-155]
---

# RDR-158: Retire the SQLite T2 Backend

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

RDR-152 (nexus-fjwxh) flipped the T2 hard default from `SQLITE` to `SERVICE`, so the
seven domain stores (memory, plans, taxonomy, telemetry, chash, aspects, aspect_queue)
now route to the PG-backed Java service by default. But SQLite is not gone, it is
*demoted*. It remains load-bearing in three distinct roles, and "all local PG, no
SQLite" is not achievable until each is addressed:

1. **The `=sqlite` opt-out backend.** `storage_mode.py` resolves `SERVICE` as the hard
   default but `NX_STORAGE_BACKEND[_<domain>]=sqlite` still selects the local SQLite
   path. As long as that path exists, the SQLite store classes, schema, and migrations
   must be maintained and tested.

2. **The migration source.** The SQLite→PG migration (RDR-153) reads from the local
   SQLite stores. Deleting SQLite before every user has migrated would strand data.
   This is the same copy-not-move / two-release-deprecation constraint RDR-155 P4b hit
   for Chroma: the migration reader can only be deleted in the release *after* the one
   that ships the migration.

3. **The `CatalogTaxonomy` parity oracle (subtle, load-bearing at runtime).** RDR-152
   nexus-1di3r made `HttpTaxonomyStore` a full drop-in, but it does so by *delegating*
   the heavy compute statics (`compute_discovered_topics`, `compute_rebuild_plan`,
   `compute_split`) verbatim to `CatalogTaxonomy`. Those statics are backend-agnostic
   (numpy/sklearn/HDBSCAN, no `self.conn`), yet they live inside the SQLite-coupled
   class. The signature-parity tripwire (`tests/db/test_http_t2_store_parity.py`) also
   compares the HTTP store *against* `CatalogTaxonomy` as the contract oracle. So
   "delete SQLite" cannot naively mean "delete `CatalogTaxonomy`": the compute statics
   and the contract definition must survive in a backend-neutral form.

### Evidence

- `storage_mode.py:167-179`: `SERVICE` is the hard default; `=sqlite` is the documented
  opt-out, still wired.
- `src/nexus/db/t2/http_taxonomy_store.py` imports `CatalogTaxonomy` and calls its
  `compute_*` statics at runtime even in pure service mode (RDR-152 nexus-1di3r.7,
  delegate-thin design).
- `tests/db/test_http_t2_store_parity.py` parametrizes the SQLite store class as the
  oracle every HTTP store must match; with SQLite deleted the tripwire loses its
  reference and must be re-grounded.
- The migration engine is production-proven (T2 `nexus_rdr/153-production-t2-migration-complete`),
  but per RDR-157 / nexus-luxe6 a *user-survivable* install + `nx upgrade` migration
  does not yet ship.

## Decision (draft — options to resolve in research)

The end state: PG-service is the only T2 backend; the SQLite store classes, schema,
migrations, and the SQLite-coupled half of `CatalogTaxonomy` are deleted; the parity
tripwire is re-grounded on an explicit interface rather than a live SQLite oracle.

Draft sequencing decisions (to lock at gate):

- **D1 — Extract the backend-neutral compute core.** Move the `CatalogTaxonomy`
  `compute_*` statics (and any other pure numpy/sklearn helpers the HTTP store
  delegates to) into a backend-agnostic module (e.g. `nexus.db.t2.taxonomy_compute`)
  that neither store imports a DB connection for. Both the (doomed) SQLite store and
  the HTTP store import from there. This decouples "delete SQLite" from "keep the
  compute pipeline."
- **D2 — Re-ground the parity tripwire.** Replace the live-SQLite-oracle comparison
  with an explicit `Protocol` / frozen contract (the method set + signatures the HTTP
  store must satisfy), so the tripwire survives oracle deletion. Open question whether
  to keep a thin in-memory reference impl purely for tests.
- **D3 — Two-phase deletion mirroring RDR-155.** Phase A: remove `=sqlite` as a
  selectable backend (hard-fail on the opt-out) once all domains have service parity
  AND nexus-luxe6 ships. Phase B (the release *after*): delete the SQLite store classes,
  schema, migrations, and the SQLite migration *reader* — gated on the deprecation
  window closing, never in the migration-shipping release.

## Approach (phased, draft)

1. **P1 — Compute-core extraction (unblocked now).** D1: lift the backend-neutral
   statics out of `CatalogTaxonomy` into a shared module; both stores delegate.
   Behavior-preserving refactor; full Java + Python suites green. Independent of the
   install/migration gate.
2. **P2 — Contract re-grounding.** D2: convert the parity oracle to an explicit
   interface; tripwire passes without importing the SQLite class for comparison.
3. **P3 — Opt-out removal (GATED on nexus-luxe6 + all-domain service parity).** Make
   `=sqlite` a hard error with a migration pointer; service is the only path.
4. **P4 — Source deletion (GATED on the two-release deprecation window).** Delete the
   SQLite store classes, schema, migration reader, and the SQLite-coupled remainder of
   `CatalogTaxonomy`. Inverse-grep clean across `src/` + `tests/`.

## Alternatives considered

- **Keep SQLite as a permanent local-only fallback.** Rejected as the end goal: it
  doubles the maintenance surface (two schemas, two migration paths, the parity
  tripwire forever) and is the thing this RDR exists to remove. May survive as a
  *transitional* state, not a terminal one.
- **Delete `CatalogTaxonomy` wholesale.** Rejected: the HTTP store delegates its
  compute statics; deleting it would force a reimplementation of the HDBSCAN/c-TF-IDF
  pipeline. D1 (extract, don't delete) avoids that.

## Consequences

- Single T2 substrate: one schema, one migration path, no SQLite single-writer class,
  no dual-backend test matrix.
- The parity tripwire loses its free oracle and must be maintained as an explicit
  contract (a small ongoing cost, but it makes the contract first-class).
- Hard dependency on nexus-luxe6: users cannot lose SQLite until they can survivably
  install + migrate into the service stack.

## Open Questions

- Does P1 (compute-core extraction) belong here or fold into a taxonomy-refactor bead
  independent of the retirement?
- Is a thin in-memory reference T2 impl worth keeping purely as a test oracle after
  the SQLite class is deleted, or is a frozen `Protocol` enough?
- Telemetry/chash/aspect_queue: do all seven domains have verified service parity, or
  is taxonomy (just completed) the last? An audit precedes P3.
- Coordination with conexus RDR-001 (`nx upgrade`): the user-facing migration is
  conexus-owned; this RDR consumes it as a gate, it does not define it.

## Research Findings

_None yet — RDR is draft. Verify the seven-domain parity status, the exact
`CatalogTaxonomy` static surface the HTTP store delegates to, and the deprecation-window
constraint against the RDR-155 P4b precedent before gate._
