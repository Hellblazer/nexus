---
title: "RDR-070 Phase 5: Taxonomy-Aware Recall in nx_answer — Teach the Composed-Retrieval Path to Read the Taxonomy It Already Has"
id: RDR-134
type: Architecture
status: draft
priority: high
author: Hal Hildebrand
reviewed-by: self
created: 2026-05-27
accepted_date:
related_issues: [nexus-9napz, nexus-n1908]
related_rdrs: [RDR-070, RDR-075, RDR-080]
related_tests: []
implementation_notes: ""
---

# RDR-134: RDR-070 Phase 5 — Taxonomy-Aware Recall in nx_answer

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

> **STUB** captured 2026-05-27 from an investigation into why nexus is not
> taking full advantage of RDR-070. Absorbs and supersedes bead `nexus-9napz`
> ("nx_answer two-phase recall"), which was filed as a bead from the MemForest
> synthesis (idea #2) but is not bead-sized: it touches the retrieval hot path
> and carries a real prefilter-granularity fork. Problem Statement and Approach
> are sketched; deeper gate sections await `/conexus:rdr-research`.

## Problem Statement

RDR-070 built a taxonomy: HDBSCAN topic discovery, per-topic centroids in a
`taxonomy__centroids` ChromaDB collection, durable `topic_assignments` in T2,
clustered search, and a `topic=` prefilter parameter. The **discovery and
assignment** half runs (post-index hook, incremental `post_store_hook`,
`nx taxonomy` CLI). The **retrieval-consumption** half is largely dormant: the
canonical analytical entry point, `nx_answer`, composes `search` + operators
but never passes a taxonomy instance or uses topic/centroid structure to narrow
recall. Every `nx_answer` query still fans out blind across all collections,
which is the exact flat-search problem RDR-070 set out to fix, one layer up
from where RDR-070 landed its fix.

### Enumerated gaps to close

#### Gap 1: nx_answer ignores the taxonomy entirely

`search_cross_corpus` can group by topic, but only when a caller passes a
`taxonomy` instance AND >50% of results already carry assignments; otherwise it
falls back to Ward clustering. `nx_answer`'s composed retrieval does not pass a
taxonomy or use topic assignments / centroids to prefilter the collections it
queries. The highest-traffic query path is the one not reading the structure.

#### Gap 2: prefilter-granularity fork — per-topic centroids exist, per-collection summaries do not

RDR-070 produces per-**topic** centroids (`taxonomy__centroids`, MiniLM 384d),
not per-**collection** summary embeddings. A recall prefilter can either (a)
rank candidate **topics** by query-to-centroid similarity and retrieve within
the topics' documents, or (b) synthesize per-**collection** representative
embeddings (e.g. aggregate a collection's topic centroids) and prefilter at
collection granularity. This RDR must pick one (or define when each applies).

#### Gap 3: clustered-search UX is opt-in at the MCP surface

`search_cross_corpus` defaults `cluster_by` to semantic at the engine level,
but the MCP `search` tool defaults `cluster_by=""` (off). Reconcile so the
shipped capability is actually reachable by default for tool callers.

## Context

### Background

Investigation 2026-05-27 (this session): a scoped analytical question about
RDR-070 routed through `nx_answer` and returned a summary of the SessionStart
hook payload instead of an RDR-070 answer (see `nexus-n1908`). That whiff is a
separate honesty bug, but it is a **prerequisite** for this RDR: a recall
prefilter that selects an empty scope must fail honestly ("no matching
evidence"), not let an operator synthesize from ambient context. Fix
`nexus-n1908` before or alongside this work.

RDR-070's own Problem Statement is the precedent: its features "exist behind
config flags nobody sets and CLI commands nobody runs." This RDR is the same
diagnosis one layer up — the taxonomy is built, the recall path does not read it.

### Technical Environment

- `src/nexus/search_engine.py` `search_cross_corpus(... cluster_by, taxonomy, topic ...)`
  — already accepts a `taxonomy` instance and a `topic` prefilter; default
  `cluster_by=_CLUSTER_DEFAULT` ("semantic").
- `src/nexus/db/t2/catalog_taxonomy.py` `CatalogTaxonomy` — `top_topics_for_collection`,
  `get_topic_docs`, `project_against`, `get_distinct_collections`; centroids in
  `taxonomy__centroids`.
- `nx_answer` / `plan_run` — the composed-retrieval path whose `search` steps do
  not currently inject a taxonomy or topic scope.
- RDR-075 (Cross-Collection Topic Projection) — relevant prior art for the
  centroid-projection mechanics (`project_against`).

## Research Findings

### Investigation

[To be completed during `/conexus:rdr-research`: confirm exactly where
`nx_answer`/`plan_run` build the `search` step args and whether a taxonomy can
be injected there; measure recall/latency of a topic-centroid prefilter vs the
current blind fan-out; decide Gap-2 granularity empirically; evaluate top-K vs
**band-similarity** centroid-neighbor selection per Dehghankar et al. 2026
("Random-Access Ranked Retrieval and Similarity Search", catalog tumbler
`1.12.6`, §2 Example 2 and §6 Stripe Range Retrieval) — selecting all topics
whose centroid similarity falls in a band around the best avoids silently
dropping equally-relevant boundary clusters that strict top-K drops.]

### Key Discoveries

- **Documented**: per-topic centroids exist; per-collection summaries do not
  (RDR-070 Data Model; verified against `catalog_taxonomy.py` 2026-05-27).
- **Documented**: `search_cross_corpus` already has the `taxonomy`/`topic`
  hooks; the gap is the caller (`nx_answer`) not using them.
- **Assumed**: a topic-centroid prefilter improves precision without dropping
  cross-cutting recall (needs the `all` escape hatch). Needs a spike.

### Critical Assumptions

- [ ] A taxonomy-aware recall prefilter measurably improves `nx_answer`
  answer quality / latency vs blind fan-out — **Status**: Unverified
  — **Method**: Spike
- [ ] Per-topic centroids are sufficient as the prefilter index (no need to
  build per-collection summaries) — **Status**: Unverified — **Method**: Spike
- [ ] An empty/low-confidence prefilter fails honestly rather than degrading
  (depends on `nexus-n1908`) — **Status**: Unverified — **Method**: Spike
- [ ] Top-K centroid neighbor selection does not silently drop equally-relevant
  boundary clusters; evaluate band-similarity selection (per Dehghankar et al.
  2026, catalog tumbler `1.12.6`) as a design variant before committing to
  top-K — **Status**: Unverified — **Method**: Spike

## Proposed Solution

### Approach

Add a recall-prefilter stage to `nx_answer`'s composed retrieval: embed the
query (local MiniLM, the same space as the centroids), rank candidate topics
(or collections) via the existing centroid index / `project_against`, select
top-M, and run the `search` step scoped to that selection, with an explicit
`all` escape hatch for cross-cutting queries. Resolve Gap 2 (topic vs
collection granularity) with a spike. Reconcile Gap 3 (MCP `cluster_by`
default). Depends on `nexus-n1908` for honest empty-scope behavior.

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| recall prefilter stage | `search_cross_corpus` taxonomy/topic params | Reuse (inject from nx_answer) |
| centroid ranking | `CatalogTaxonomy.project_against` / `top_topics_for_collection` | Reuse |
| query embedding | local MiniLM (RF-070-4) | Reuse |

[Decision rationale, alternatives, trade-offs, test plan, finalization gate:
to be completed during research.]

## References

- RDR-070 (Incremental Taxonomy & Clustered Search) — the substrate this
  consumes; this is its natural Phase 5.
- RDR-075 (Cross-Collection Topic Projection), RDR-080 (nx_answer / retrieval
  consolidation).
- Beads: `nexus-9napz` (superseded by this RDR), `nexus-n1908` (nx_answer
  empty-retrieval honesty — prerequisite).
- Dehghankar, Asudeh, Mittal, Shetiya, Das. 2026. "Random-Access Ranked
  Retrieval and Similarity Search." Catalog tumbler `1.12.6`, T3 collection
  `knowledge__dt-papers__voyage-context-3__v1`. Source for the band-similarity
  centroid-neighbor selection question (§2 Example 2 and §6 Stripe Range
  Retrieval). See T3 `research-random-access-ranked-retrieval-nexus-leverage-2026-05-28`
  (synthesis) + T2 `nexus_rdr/rar-synthesis-critique-2026-05-28` (critique).
- T3 synthesis: `research-memforest-nexus-leverage-2026-05-27` (idea #2 origin).
