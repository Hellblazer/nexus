# RDR-225 Post-Mortem: One Vector Table per Embedding Model, with Tenant Isolation

**Closed** 2026-10-22 · **Accepted** 2026-10-05 · **Epic** nexus-3wh8d (all children closed; .3 and .4 cancelled with protocol steps 3 and 4) · **Shipped** engine-service-v0.1.150 (cutover, 2026-10-07), v0.1.151 (`ef_search` floor 600), v0.1.162 (retired tables dropped, 2026-10-21); conexus 7.83.1 pins v0.1.162

## What the RDR set out to do

The engine kept every 1024-dimension embedding in one column under one HNSW
graph (the approximate nearest-neighbour index pgvector builds). Two Voyage
models wrote into it, `voyage-code-3` for code and `voyage-context-3` for
prose, and every tenant's rows shared the same graph. A graph links vectors by
distance, and the distance between a code vector and a prose vector means
nothing, so the graph was two islands joined by arbitrary links, and a
tenant's search walked other tenants' rows.

The RDR list-partitioned `nexus.chunks` by embedding model and then by tenant,
so each (model, tenant) leaf table has its own graph. A trigger on
`service_tokens` creates a new tenant's leaves. Row-level security stays on
every leaf. The per-model split had to be shown not to lose recall against the
old tenant-only layout (the evaluation protocol's step 3).

## Implementation status

Implemented and live in the cloud since 2026-10-07. The walk ran on a
production fork first and in the cloud in 7 min 53 s with exact row counts.
Each search plan touches one (model, tenant) leaf (156 of 156 checks). The old
tables were kept for 14 days and dropped in v0.1.162. Local installs get the
layout through the client release that pins that engine.

## Implementation vs plan

### As planned

- The partitioned layout, the tenant trigger, FORCE row-level security on the
  parent and every leaf, and the four-column deferrable foreign keys from the
  manifest and topic tables.
- A walk rehearsed on a point-in-time copy of production before the cloud
  deploy, with row reconciliation per (model, tenant).
- A 14-day hold on the retired tables, then a drop rehearsed the same way.

### Diverged

1. **The recall proof was dropped, and a recall miss reached the cloud.**
   Protocol step 3 compared recall on the new layout with the old one on real
   vectors before deploy. It was dropped on 2026-10-06, along with step 4
   (replaced by the fork walk). The stated basis for dropping step 3 was the
   60,000-row router threshold, which was the step-1 census's harness parameter
   (T2 `nexus/rdr225-step1-census-and-h5-inventory-2026-10-05`: "T=60000"), not
   the router that shipped: the cloud deploy ran 10,000, the default was 60,000
   for engine-service-v0.1.155 only, then 30,000, and is 20,000 now (RDR-225,
   Decision note). The first cloud check after deploy found recall@10 of 0.90
   on one of twelve queries (`code-filtered-001`) at `ef_search` 200. The cause
   is not established. On synthetic data a per-leaf graph scored 0.966 against
   0.919 for the global table at ef 200, and the analysis infers tail variance
   for the production miss, which was not tested on production (T2
   `nexus_rdr/225-recall-per-leaf-analysis`). The fix was an engine default
   floor of 600 (v0.1.151, nexus-3wh8d.31), after which all twelve read 1.00.
   The epic's acceptance still names step 3's rule; Sam accepted the cloud-gate
   recall in its place at this close. The four test gaps that validation left
   open each have a bead (nexus-3wh8d.34, .35, .36, .37).
   *Drift class: unvalidated assumption.*

2. **The minimum viable validation shrank to a smoke test.** The RDR's MVV
   asked for the protocol's frozen query set served by the engine on a fork.
   What ran after the walk was the deploy runbook's twelve-search smoke test,
   which is the check that found item 1, after the deploy rather than before.
   *Drift class: deferred critical constraint.*

3. **The bulk-insert write cost was deferred to a rehearsal that never
   measured it.** Step 2 left bulk insert at 1.02 with CI [0.82, 1.28], over
   the 1.25 rule because of one disturbed pair, and the RDR deferred it to the
   step-3 fork, then to the Phase 3 rehearsal. Neither measured it. The
   phase-review cross-walk caught the gap; it was measured on 2026-10-21 on
   the local substrate at 0.93, CI [0.92, 0.94] (nexus-3wh8d.32), under the
   rule. The layout is cheaper because each leaf's graph is smaller, and it
   writes about 8% more WAL per row.
   *Drift class: deferred critical constraint.*

4. **The lock table bounds tenants per migration.** A walk that creates
   leaves for many tenants in one transaction holds about 27 relation locks
   per leaf to the commit. With four models and two parents a tenant is about
   216 locks, so the default lock table (`max_locks_per_transaction` 64 x 100
   connections) covers about 29 tenants. Found while measuring tenant creation
   (TS1), not in the design.
   *Drift class: missing failure mode.*

5. **Placeholder models for disputed dimensions were removed.** The first
   build carried three placeholder models (fourteen leaves per tenant); they
   were taken out on 2026-10-06, so the layout has four models.
   *Drift class: over-specified code.*

## What to carry forward

- A rebuilt HNSW layout can move recall on individual queries, not only cost:
  here one query of twelve fell to 0.90 at `ef_search` 200, and the cause is
  not established. Measure recall on real vectors before deploy, or set the
  search floor before the cutover, not after.
- A deferred measurement needs a bead the day it is deferred. H4 moved twice
  in the text and nothing tracked it until the phase-review cross-walk.
- Size `max_locks_per_transaction` for any migration that creates partitions
  per tenant inside one transaction.

## Records

- Gate: T2 `nexus_rdr/225-gate-latest` (PASSED round 2)
- Phase review: T2 `nexus_rdr/225-phase-review`, `nexus_rdr/225-phase-review-close`
- H4: T2 `nexus_rdr/225-h4-bulk-insert-remeasure-2026-10-21`; raw data in `~/nexus-evidence/rdr-225-h4-bulk-insert-2026-10-21/` (22 files, `SHA256SUMS` verifies)
- Recall cause analysis: T2 `nexus_rdr/225-recall-per-leaf-analysis`
- Test-gap beads: nexus-3wh8d.34 (WP6), .35 (M4), .36 (DP3), .37 (TS1)
- Rehearsals: T2 `nexus_rdr/225-rehearsal-2026-10-07`; conexus `rehearsal-v0162-walk-2026-10-21`
