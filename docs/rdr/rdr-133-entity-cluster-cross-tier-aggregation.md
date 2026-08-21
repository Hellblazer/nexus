---
title: "Entity-Cluster Cross-Tier Aggregation: A First-Class Entity Handle Unifying T2 Memory, T3 Catalog, and T3 Chunks"
id: RDR-133
type: Architecture
status: draft
priority: medium
author: Hal Hildebrand
reviewed-by: self
created: 2026-05-27
accepted_date:
related_issues: []
related_rdrs: [RDR-073, RDR-050, RDR-108]
related_tests: []
implementation_notes: ""
---

# RDR-133: Entity-Cluster Cross-Tier Aggregation

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

> **STUB** captured 2026-05-27 from the MemForest research synthesis
> (T3: `research-memforest-nexus-leverage-2026-05-27`, idea #7, flagged as the
> genuinely-novel-for-nexus idea; MemForest paper at catalog tumbler `1.14.4`).
> Problem Statement and Approach are sketched; deeper gate sections await
> `/conexus:rdr-research`. **High scope-creep risk** (see Gap 2 and the
> overlap note with RDR-073) — this stub deliberately bounds the idea before
> any implementation.

## Problem Statement

No nexus data structure aggregates everything that relates to a named entity
(e.g. `RDR-112`, `daemon`, `aspect_worker`) across tiers. BERTopic taxonomy
operates at collection granularity; the catalog link graph covers T3 catalog
nodes only; T2 memory is a separate flat store. A question like "give me all
context on RDR-112" must be assembled by hand from three disjoint stores.

### Enumerated gaps to close

#### Gap 1: Entity context is fragmented across T2 memory, T3 catalog, and T3 chunks

There is no single handle that returns the T2 memory entries, the T3 catalog
nodes/links, and the T3 chunk content that all concern one entity. Every other
idea in the synthesis extends an existing subsystem; this one introduces a new
first-class concept (the Entity Cluster), so the gap is genuine rather than a
missing knob.

#### Gap 2: Bounding the concept so it does not re-implement the catalog inside T2

The tempting failure mode is to grow an Entity Cluster into a parallel graph
store that duplicates catalog responsibilities. This RDR must define hard scope
boundaries: the cluster is an aggregation/index over existing tiers, not a new
source of truth.

## Context

### Background

MemForest (tumbler `1.14.4`) maintains entity trees that collect all evidence
about a recurring subject regardless of which session produced it. The synthesis
flagged the nexus analog as novel and high-value but high-effort: a cross-tier
Entity Cluster keyed by entity name, driven automatically by the relay `Bead`
field. Strongly overlaps RDR-073 (Temporal Entity Knowledge Graph, currently
deferred) — research must reconcile the two before either proceeds.

### Technical Environment

- T2 `memory` store (`db/t2/memory_store.py`).
- Catalog graph + typed links (`src/nexus/catalog/`, RDR-108 identity model).
- T3 chunks (content-addressed; `document_chunks` manifest joins doc to chunks).
- BERTopic taxonomy (`db/t2/catalog_taxonomy.py`) — collection granularity only.

## Research Findings

### Investigation

[To be completed during `/conexus:rdr-research`: reconcile with RDR-073;
evaluate entity-resolution quality (bead-ID regex vs LLM extraction); decide
whether the cluster is a materialized T2 table or a query-time view.]

### Key Discoveries

- **Documented**: MemForest entity-tree cross-session aggregation (tumbler `1.14.4`).
- **Assumed**: entity resolution can be bootstrapped from bead-ID mentions.
  Resolution quality is the load-bearing risk.

### Critical Assumptions

- [ ] A cross-tier entity handle delivers materially better context bundles
  than separate per-tier queries — **Status**: Unverified — **Method**: Spike
- [ ] The cluster can stay an aggregation/index (not a source of truth) and
  avoid duplicating the catalog — **Status**: Unverified — **Method**: Spike
- [ ] This does not duplicate or conflict with RDR-073 — **Status**: Unverified
  — **Method**: Source Search (RDR-073)

## Proposed Solution

### Approach

New T2 table `entity_clusters(entity_name, scope, member_type [memory |
catalog | chunk], member_id, added_at)`. Entity resolution at write time (a
bead-ID mention auto-tags membership). New MCP endpoint `entity_get(entity_name)`
returns a cross-tier context bundle (T2 entries + catalog nodes/links + chunk
snippets). SubagentStart populates the bundle from the relay `Bead` field.

Pairs with RDR-132: scope-routed promotion is the write-side partition; this is
the read-side cross-tier aggregation. Research must decide whether they merge.

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| `entity_clusters` table | `db/t2/` | New table (aggregation index only) |
| `entity_get` endpoint | `src/nexus/mcp/` | New MCP tool |
| entity resolution | RDR-132 scope tagging | Reuse / share |

[Decision rationale, alternatives, trade-offs, test plan, finalization gate:
to be completed during research. Gate must hold the line on Gap 2 scope
boundaries.]

## References

- T3 synthesis: `research-memforest-nexus-leverage-2026-05-27` (idea #7, novel)
- MemForest paper, catalog tumbler `1.14.4`
- RDR-073 (Temporal Entity Knowledge Graph, deferred — reconcile), RDR-050
  (knowledge-graph query planning), RDR-108 (graph identity normalization)
