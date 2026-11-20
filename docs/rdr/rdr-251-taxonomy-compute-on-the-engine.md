---
title: "Taxonomy Compute on the Engine: Discover, Labels and Cross-Links Without Downloading the Collection (RDR-193 Part B)"
id: RDR-251
type: Architecture
status: deferred
priority: medium
author: Hal Hildebrand
reviewed-by: pending
created: 2026-11-16
accepted_date:
related_issues: []
related: [RDR-193, RDR-070, RDR-243]
---

# RDR-251: Taxonomy Compute on the Engine

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.
> Prose: see REGISTER.md beside this template. Write for a smart reader who
> may not know the jargon; define terms on first use; simplified, never
> simplistic.

This RDR carries Part B of RDR-193. Sam held Part B when he accepted RDR-193
(2026-11-08, open question 3) and moved it out when RDR-193 closed (2026-11-16,
"Close A, move Part B out"). RDR-193 shipped Parts A and A4: catalog reconcile
and index-time linking on the engine. Its Part B design sections are this RDR's
starting design and stay in RDR-193 as written: Part B1 (taxonomy SQL), Part B2
(the discover job), Steps 4, 8 and 10 to 12, and the held Test Plan scenarios.
They passed RDR-193's round-3 gate but were never built, and the code has moved
since, so this RDR re-checks them before its own gate.

## Problem Statement

### Enumerated gaps to close

#### Gap 1: Taxonomy discover downloads the entire collection to cluster it, though embeddings are born on the engine

This is RDR-193's Gap 2, as written on 2026-08-15. Its line references and its
order-of-magnitude figures date from then and are re-checked before this RDR's
gate.

The flush-time assign step is already server-side
(`nexus.assign_from_chashes_<dim>`, Liquibase `taxonomy-006`, shipped
conexus 7.4.0 and engine v0.1.68; its cross-collection pass was rewritten
onto the HNSW index, pgvector's graph index for approximate nearest
neighbours, by `taxonomy-018` and pinned by `taxonomy-020`). What
remains on the client is four orchestrators (discover, rebuild, split,
`project_against`), the labeler's per-topic reads and writes, and per-pair
cross-link writes:

- `discover_for_collection` (`commands/taxonomy_cmd.py` :296, through
  `_discover_via_service` :180 and `_fetch_service_vectors` :100) pages
  every chunk's text to the client (250 per request, with
  `sensitivity="exclude"` so no sensitive chunk is read, RDR-243) and then
  every embedding (`t3.get_embeddings`), runs sklearn HDBSCAN for n ≤ 5000
  (`min_cluster_size=5`, `max_cluster_size` from `_max_cluster_size`, "eom"
  (excess of mass) selection of clusters from its tree, centroid centres) or
  MiniBatchKMeans above that
  (`k = max(10, √n/3)`, seed 42), plus c-TF-IDF labels (class-based term
  weighting, `CountVectorizer(stop_words="english")`, top 3 terms) in
  `db/t2/taxonomy_compute.py` (`_cluster` :204), and ships every assignment
  back (`persist_discovered_topics` carries each topic's full chash list).
- Cross-links: `compute_cross_links` (`db/t2/http_taxonomy_store.py`
  :1067) downloads all foreign centroid vectors and does the cosine matmul
  on the client; `persist_cross_links` (:1355) posts one
  `/links/upsert` per pair.
- `project_against` (:2091, through `_svc_fetch_all_embeddings` :1843)
  downloads every embedding of every source collection a second time under
  `--all`.
- The labeler `relabel_topics` (`taxonomy_cmd.py` :2542) makes one
  `get_topic_doc_ids` read per topic (:2624) and one `update_topic_label`
  write per topic (:2719).

Order of magnitude for a 100k-chunk, 1024-dimension collection (estimated
2026-08-15, not re-measured): about 740 bulk round trips and 1.2-1.6 GB of
egress as JSON float arrays (about 400 MB even as binary float32) for one
discover, roughly double under `--all`. Embeddings are computed on the
engine in every mode (`http_vector_client.py:4014`
`upsert_chunks_with_embeddings`: "forward chunk text, ignore caller's
embeddings"), so the download carries data that never needed to leave the
host. No clustering exists anywhere in `service/`; pgvector exposes no
k-means or HDBSCAN primitive (IVFFlat's build-time k-means is internal).

## Relationship to Prior RDRs

- **RDR-193** (closed 2026-11-16) is the parent. Its Part B1 (taxonomy SQL,
  changeset `taxonomy-022-serverside-projection-links.xml`) and Part B2 (a Java
  discover job on the shared `EngineJobs` registry that Part A built) are the
  starting design.
- **RDR-070** is the taxonomy subsystem whose discover pipeline this moves.
- **RDR-243**: discover never reads a sensitive chunk (`sensitivity="exclude"`),
  and a job on the engine must keep that rule.

## Context

### Background

RDR-193 Part A gave the engine a shared job registry (`EngineJobs`, with a poll
that checks the tenant) and landing tables for staged client input. Part B was
designed to reuse the registry, with a `discoverJobs` instance; its discover job
reads chunk and embedding rows from `nexus.chunks` and does not use the landing
tables, which hold catalog rows only. It was held at accept because Parts A and A4 fixed a
correctness defect (RDR-193 Gap 4), while Part B fixed a cost that nothing yet
felt.

### Technical Environment

Re-read before research: the client's taxonomy orchestrators in
`src/nexus/commands/taxonomy_cmd.py` and `src/nexus/db/t2/`, the engine's
`EngineJobs` and its taxonomy changesets, and the pgvector version the engine
bundles.

## Research Findings

### Investigation

Not started in this RDR. RDR-193's research records 1 to 38 (T2
`nexus_rdr/193-research-*`) cover Part B's design as it stood from 2026-08 to
2026-11.

### Key Discoveries

None yet.

### Critical Assumptions

Carried from RDR-193 and not verified here: a Java port of the clustering
matches sklearn's HDBSCAN (RDR-193 Step 10's gate, held).

## Proposed Solution

### Approach

Start from RDR-193 Parts B1 and B2. Check each against the current code before
changing anything.

### Technical Design

See RDR-193 § Part B1 and § Part B2.

## Alternatives Considered

The Part B alternatives in RDR-193 § Alternatives Considered are carried here
and revisited.

## Trade-offs

### Consequences

To be stated after research.

### Risks and Mitigations

To be stated after research.

## Implementation Plan

### Prerequisites

- [ ] A trigger to start: a measured discover cost that someone feels. RDR-193
  rested Part B on an estimate, not a measurement.
- [ ] Each item of the carry-list below is resolved or carried into this RDR's
  design.

#### Carry-list from RDR-193 (comment of 2026-11-11 on epic nexus-u5m5g)

1. B1 omits the supersession skip that already shipped (vectors-038) and the
   model and tenant predicates (vectors-031).
2. The `too_large` heap share is never stated.
3. No engine cut is named for Step 4's routes.
4. `taxonomy-022` must be checked again that it is free.
5. `TenantKeyedStateGateTest` maps every `EngineJobs.*` field to one evictor
   name, `EngineJobs.reconcile`, so a third instance (`discoverJobs`, Step 4)
   would pass the gate with no evictor registered. Step 4 adds its own evictor
   name, and a check that every `EngineJobs` instance `NexusService` builds is
   registered in `tenantKeyedState()`.

### Phase 1: Code Implementation

RDR-193 Steps 4, 8 and 10 to 12, renumbered when this RDR's plan is written.

## Test Plan

RDR-193's held discover scenarios, checked again.

## Validation

### Performance Expectations

None claimed until the discover cost is measured.

## Finalization Gate

Not yet run.

## References

- RDR-193: § Gap 2, § Part B1, § Part B2, Steps 4, 8 and 10 to 12.
- Epic nexus-u5m5g, comment of 2026-11-11 (the carry-list).

## Revision History

- 2026-11-16: created (draft) from RDR-193 Part B when RDR-193 closed, on Sam's
  answer "Close A, move Part B out" (relayed by halcyon).
- 2026-11-19: deferred (draft -> deferred) on Sam's ruling "go with all of it" (T2 `nexus/sam-ruling-rdr-closeout-batch-2026-11-19`): dormant, so it is deferred instead of held open as a draft.
