---
title: "No Client Walks of the Managed Catalog: the Engine Answers Tenant-Wide Questions and Runs Tenant-Wide Jobs"
id: RDR-241
type: Architecture
status: draft
priority: high
author: Sam
reviewed-by: self
created: 2026-11-02
accepted_date:
related_issues: [nexus-mq980, nexus-75w5d, nexus-ohxzu, nexus-ial1f, nexus-0lv0r]
related_rdrs: [RDR-193, RDR-146, RDR-192, RDR-168, RDR-236, RDR-155]
---

# RDR-241: No Client Walks of the Managed Catalog

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

A *client walk* is client code that pages through every catalog document of a
tenant over HTTP (`all_documents(limit=0)` and its siblings) to compute
something about the whole tenant. On a managed service the catalog lives in the
engine's Postgres, so a fact like "the highest child number under each owner"
is one indexed SQL query there, and 44 or more HTTP pages from a client. The
cost of every walk grows with the catalog, and the catalog grows: about 22,000
documents on 2026-08-02 (nexus-ohxzu), 43,745 on 2026-11-02 (the census count,
Research Findings). A walk that is fine today is a timeout at the next doubling.

At the 7.94.0 shakedown (T2 `nexus/shakedown-2026-11-02-umbrella-post-7.94.0`)
a plain `nx doctor` on the laptop's managed tenant ran past a 30-minute cap
with no output. Stack dumps showed three walks, one of them an upgrade rung's
dry-run scan that also reads manifests and T3 per batch. The fix on the
shakedown branch cuts doctor to one walk and makes it finish in 60 seconds;
the walk that remains still grows with the tenant. This RDR is about the class,
not that instance.

### Enumerated gaps to close

#### Gap 1: Tenant-wide aggregates are computed on the client

Thirteen call sites compute a count, a maximum, a set or a join over every
document (the census, Research Findings): allocator drift, stale index-run
fences, failed runs, store-put integrity, gc candidates, citation joins and
others. Each is a SQL aggregate the engine could answer in one round trip.
Some already have an engine route that answers them and were never switched:
`enrich.py`'s collection set has `distinct_doc_collections()` today, and the
census lists the routes that exist.

#### Gap 2: Tenant-wide jobs run as client loops

Thirteen call sites must touch every row: export and recovery bundles,
backfills, reconciles, prune, audit, and the upgrade ladder's data migrations.
Each runs on the client, one page at a time, with per-row follow-up requests.
A data migration such as the `manifest-section-labels` rung took about 15
minutes per session on a managed catalog (nexus-75w5d); a migration of rows
that live in the engine belongs in the engine, as a Liquibase change or an
engine job, not as a client loop.

#### Gap 3: Unattended paths can reach a walk

A walk in a command a person typed costs that person time. A walk reached
without anyone typing a command (session start through `nx upgrade --auto`,
`nx doctor` from a hook or a script, the console's health page on open, MCP
tools an agent calls, a store write that fills a collection-wide cache) costs
shared capacity for every session at once: 19 sessions started together on
2026-10-31 (nexus-75w5d). The guard that exists (`refuses_unbounded_read`,
`unattended_run`) covers rung `detect()` for `--auto` only. The census found
the rung's `verify()` reaches the same scan with no guard.

#### Gap 4: Nothing stops a new walk from landing

The class has been fixed instance by instance (nexus-ohxzu: walk once instead
of per owner; nexus-75w5d: refuse under `--auto`; the 7.94.0 shakedown: one
walk per doctor run) and recurs. No lint names the call sites, so a new
`all_documents(limit=0)` in a diagnostic or a hook passes review.

## Relationship to Prior RDRs

Searched docs/rdr for "unbounded", "walk", "all_documents", "managed" and
"aggregate route"; read the beads named in the frontmatter.

| Prior RDR | Relationship | What it means for this one |
| --- | --- | --- |
| RDR-193 (deferred) | Adjacent draft | Same diagnosis for INDEX-TIME work: the per-owner reconcile loop, housekeeping and taxonomy compute move to the engine. Deferred 2026-09-16 with reopen trigger "an index wall-time or catalog-phase cost regression". Scope boundary below. |
| RDR-146 (closed) | Precedent | Gap 4 was an unbounded client BFS on the link graph (nexus-41otf), fixed by routing it to the engine. Same class, same remedy, shipped. |
| RDR-192 (closed) | Adjacent | Keyset cursor on the listing route; makes a walk correct, not cheap. Kept for BULK JOB exports. |
| RDR-168 (closed) | Origin | Defined the `all_documents(limit=0)` contract this RDR retires for managed tenants. |
| RDR-236 (accepted) | Adjacent | Engine batch routes capped at 1000 per call; catalog-owned document metadata makes several aggregates answerable in SQL. |
| RDR-155 (closed) | Context | T3 lives in engine PG, so T3 group-by aggregates are engine-answerable too. |

Scope boundary with RDR-193:

| Concern | RDR-193 | RDR-241 |
| --- | --- | --- |
| Per-owner reconcile, housekeeping, rename detection in `nx index` | yes | no |
| Taxonomy discover compute | yes | no |
| Tenant-wide aggregates in doctor, console, catalog verbs, MCP tools | no | yes |
| Upgrade-ladder data migrations | no | yes |
| Link generation on the index path (census HOT PATH, `link_generator.py:60`) | yes, as index-time linking | names it; leaves it to RDR-193 |
| The ratchet lint over call sites | no | yes, covering both |

## Context

### Background

Found by the 7.94.0 shakedown (above). The doctor timing, live: killed at
1800 s with no output on 7.94.0; 60 s on the shakedown branch (one walk).

### Technical Environment

Managed service: engine (Java, Postgres 17) behind the public edge
`api.conexus-nexus.com`; the client reaches the catalog through
`HttpCatalogClient` (`src/nexus/catalog/http_catalog_client.py`). Local mode
runs the same engine on the laptop, so the same routes serve both.

## Research Findings

### Investigation

Census of every whole-tenant walk on develop dfcd64679, read-only: T2
`nexus/rdr-241-walk-census` (36176). It traced each call site to the command,
hook, MCP tool or daemon that reaches it and classified it.

### Key Discoveries

- **Verified (source):** `all_documents(` appears on 40 lines; 30 are live
  calls in 17 files. Classes: AGGREGATE 13, BULK JOB 13, BOUNDED 3, HOT PATH 1.
  About 15 further whole-tenant shapes (list loops paged to the end, link
  audits, T3 group-bys) are listed in the census and are mostly AGGREGATE.
  These are "the census counts" cited below.
- **Verified (source):** the engine already exposes `/v1/catalog/stats`,
  `/docs/collection-counts`, `/docs/distinct-collections`, `/docs/orphaned`,
  `/docs/absolute-paths`, `/owners/all-with-roots`, `/links/orphaned` and
  `/verify/relation-counts`, with client wrappers for most.
- **Verified (source):** missing routes: a read-only per-owner high-water
  (only the write verb `owners/sweep_next_seq_drift`, although
  `highestChildSeq` is already SQL), an `index_state` census, an author
  filter, a `meta` key filter, link group-by counts, and a T3 group-by
  document.
- **Verified (live):** doctor 1800+ s with three walks; 60 s with one
  (a9d3a4732 on develop).
- **Verified (source), needs a test:** `ManifestSectionLabelsRung.verify()`
  (`manifest_section_labels.py:724-733`) calls `_dry_run()` with no deadline
  and no unattended check, and the runner calls `verify()` after `detect()`
  reports converged from a memo (`runner.py:329`).
- **Verified (source):** `nx catalog suggest-links` reads one page of 10,000
  (`links.py:571`) and silently truncates a larger tenant.
- **Documented:** catalog size 22k (2026-08-02) to 43.7k (2026-11-02). No
  repo document states a growth rate.

### Critical Assumptions

- [ ] Every AGGREGATE site's answer is expressible as SQL over the engine's
  catalog, manifest and T3 tables without reading files on the client's disk
  — **Status**: Unverified for #6 (citation join on bib ids) and #18 (etags)
  — **Method**: Source Search
- [ ] Engine-side data migrations can run inside the existing Liquibase
  changeset flow or an engine job without holding a long transaction over
  the whole catalog — **Status**: Unverified — **Method**: Spike
- [ ] BULK JOB commands a person types (export, recovery bundle) are
  acceptable as streamed engine responses rather than client page loops
  — **Status**: Unverified — **Method**: Source Search

## Proposed Solution

### Approach

One rule, stated for managed and local alike because both run the same
engine: **client code does not page through every document of a tenant to
answer a question or to change rows.** The engine answers tenant-wide
questions through aggregate routes and changes tenant-wide rows itself. A
client walk survives only where a person typed a command whose job is to read
every row (export), and then it streams.

Applied per census class:

- **AGGREGATE:** switch to an existing route where one exists; add a route
  where none does (high-water per owner, `index_state` census, author filter,
  meta filter, link group-by, T3 group-by document).
- **BULK JOB:** data migrations (upgrade rungs, backfills) move into the
  engine; reconciles and audits whose per-row step needs the client's disk
  (`prune-stale`, `remediate-paths`) keep a client loop but read only the
  rows the engine says are candidates; exports stream.
- **BOUNDED:** keep, with the bound stated where it is set; a bound that
  truncates silently (suggest-links) reports the truncation.
- **HOT PATH:** left to RDR-193 (index-time linking).
- **Unattended paths** never walk: they call an aggregate route or report
  "not checked" with the command that would check.
- **Ratchet:** a lint lists every surviving call site of `all_documents` and
  the other walk shapes with its class, refuses a new one, and only lets the
  list shrink.

### Technical Design

Interfaces, not implementations; each route is tenant-scoped like every
catalog route.

- `GET /v1/catalog/owners/high-water` → `{owner_prefix: max_child_seq}`,
  tombstones included (what `_highest_child_seqs` computes).
- `GET /v1/catalog/docs/index-state-census?stale_after_s=` → counts by
  `index_state`, and the ids of `indexing` rows older than the cutoff and of
  `failed` rows, capped with a stated limit.
- `GET /v1/catalog/docs?author=`, `?meta.<key>=` → server-side filters on the
  existing list route.
- A labels-pending count for the `manifest-section-labels` rung, so its
  `detect()` is one call (nexus-mq980 names it).
- Upgrade rungs whose rows live in the engine gain an engine-side converge;
  the client rung becomes "ask the engine to run it, then read its status".

The ratchet: a test under `tests/` that collects call sites by AST (callee
name `all_documents` and the paging-loop helpers the census names), compares
them with a checked table in `src/nexus/tables/` (site, class, reason), and
fails on a new site or a removed site still in the table. An aliased or
unpacked call escapes an AST lint (known blind spot), so the table also lists
the helpers that wrap a walk.

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| Aggregate routes | `CatalogHandler`, `CatalogRepository` (stats, collection-counts, orphaned) | Extend |
| Unattended guard | `upgrade_ladder/auto_guard.py`, `unattended.py` | Extend to `verify()` and to doctor and console paths |
| Engine-side migrations | Liquibase changesets under `service/`, engine jobs | Reuse |
| Ratchet table | `src/nexus/tables/` checked tables | Reuse the pattern |

### Decision Rationale

The engine already holds every row these walks read. Moving the computation
there turns a cost that grows with the tenant into one indexed query, and
removes the per-page round trip the client pays from every box. The
instance fixes so far (ohxzu, 75w5d, the shakedown) each bought a constant
factor and left the class in place, which is the same argument RDR-193 made
for index time.

## Alternatives Considered

### Alternative 1: Keep the walks and bound them with deadlines

**Description**: every walk takes a deadline and reports partial results.

**Pros**: no engine work; works today.

**Cons**: a bounded walk answers a different question (a sample) and still
costs shared capacity up to the deadline on every session; the shakedown's
doctor fix is this alternative, and its cost still grows with the tenant.

**Reason for rejection**: it moves the failure from "never finishes" to
"answers partially", which the doctor's rows cannot honestly present as a
verdict.

### Alternative 2: Cache the walk on the client

**Description**: one walk per day, cached on disk, read by every check.

**Reason for rejection**: stale by design, one walk per box per day is still
O(tenant), and a cache of catalog rows on the client is state the engine
already owns.

### Briefly Rejected

- **Bigger pages**: a constant factor; the engine batch cap is 1000 (RDR-236).

## Trade-offs

### Consequences

- Positive: doctor, console and MCP costs stop growing with the catalog.
- Positive: data migrations run once per tenant, not once per client session.
- Negative: more engine routes, each with a wire-contract entry and an engine
  release; client changes that use them need the engine first (the paired
  release choreography).

### Risks and Mitigations

- **Risk**: a client must keep working against an older engine.
  **Mitigation**: each switch reads a `/version` flag and falls back to
  "not checked" (never to a walk) when the route is absent.
- **Risk**: an engine-side migration holds locks on a large table.
  **Mitigation**: batch inside the engine job with its own progress record.

### Failure Modes

A missing route shows as a "not checked" row naming the engine version that
has it. A ratchet failure names the new call site and its file:line.

## Implementation Plan

### Prerequisites

- [ ] Critical Assumptions verified
- [x] RDR-193 reopened (Sam, 2026-11-03); the HOT PATH row stays with RDR-193

### Minimum Viable Validation

`nx doctor` on the laptop's managed tenant makes no `all_documents` request
(counted at the HTTP client) and finishes, with every row it used to compute
from the walk answered from an engine route.

### Phase 1: Code Implementation

#### Step 1: Ratchet first

Land the checked table and the lint with today's census as the frozen list,
so nothing new lands while the rest moves.

#### Step 2: Guard the unattended paths

`verify()` and every doctor and console path go under the unattended guard;
`suggest-links` reports truncation. Client-only; ships at once.

#### Step 3: Switch to existing routes

The AGGREGATE sites an existing route already answers (the census names them).
Client-only.

#### Step 4: New aggregate routes

High-water, index-state census, author and meta filters, link group-by, T3
group-by; client switches behind `/version` flags. Engine release, then
client.

#### Step 5: Engine-side migrations and streamed exports

The upgrade rungs whose rows live in the engine, and the export and recovery
bundle as streamed routes.

### Day 2 Operations

| Resource | List | Info | Delete | Verify | Backup |
| --- | --- | --- | --- | --- | --- |
| Ratchet table | In scope (the table) | In scope | N/A | In scope (the lint) | N/A |
| New routes | N/A | `/version` flags | N/A | engine tests | N/A |

### New Dependencies

None.

## Test Plan

- **Scenario**: doctor against a fake 43,700-document catalog — **Verify**: zero
  `all_documents` calls once Step 4 lands; one before.
- **Scenario**: a new `all_documents(limit=0)` added in a diagnostic —
  **Verify**: the ratchet fails naming the site.
- **Scenario**: `nx upgrade --auto` on a managed fake with a memo standing and
  no record — **Verify**: `verify()` makes no scan.
- **Scenario**: client against an engine without a new route — **Verify**: the
  row reads "not checked", never a walk.
- **Scenario**: suggest-links on 10,001 documents — **Verify**: it reports the
  truncation, or pages correctly.

## Validation

### Testing Strategy

Engine routes get repository tests against real PG; client switches get
tests that count HTTP calls on a fake catalog; the MVV runs live on the
laptop's managed tenant.

### Performance Expectations

Doctor: 1800+ s (three walks), 60 s (one walk, shakedown branch); the target
is no walk, so its cost no longer depends on the catalog size.

## Finalization Gate

### Contradiction Check

To be written at gate.

### Assumption Verification

To be written at gate.

### Scope Verification

To be written at gate.

### Cross-Cutting Concerns

- **Versioning**: new routes are additive `/version` flags; wire-contract entries.
- **Deployment model**: engine release first, client after (paired choreography).
- **Incremental adoption**: Steps 1 to 3 are client-only and ship before any engine change.
- **Memory management**: exports stream rather than hold every row (census #5 holds the list in memory today).
- Build tool compatibility, Licensing, IDE compatibility, Secret/credential lifecycle: N/A.

### Proportionality

To be written at gate.

## References

- T2 `nexus/rdr-241-walk-census` (36176): the census and prior art.
- T2 `nexus/shakedown-2026-11-02-umbrella-post-7.94.0` (36168): the finding.
- Beads nexus-mq980, nexus-75w5d, nexus-ohxzu, nexus-ial1f, nexus-0lv0r.
- `src/nexus/health.py`, `src/nexus/upgrade_ladder/`, `src/nexus/catalog/http_catalog_client.py`,
  `service/src/main/java/.../CatalogRepository.java`.

## Revision History

- 2026-11-02: created (draft) after the 7.94.0 shakedown.
