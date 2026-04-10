---
title: "Literature-Grounded Search and Knowledge Enhancement Roadmap"
id: RDR-061
type: design
status: accepted
accepted_date: 2026-04-09
reviewed_by: self
priority: P1
author: Hal Hildebrand
created: 2026-04-09
related_issues: [RDR-055, RDR-056, RDR-057, RDR-058, RDR-049, RDR-051, RDR-052, RDR-053]
related_notes: >
  RDR-055 (closed): Section-type metadata — E1 already shipped as part of this work.
  RDR-056 (closed): Search robustness — foundation for E2.
  RDR-057 (draft): Progressive formalization — IS E6 (tier promotion) AND owns T2 access-tracking schema.
  RDR-058 (closed): Pipeline orchestration — foundation for E4.
  RDR-049/051/052/053 (closed): Catalog infrastructure — foundation for E3, E5.
---

# RDR-061: Literature-Grounded Search and Knowledge Enhancement Roadmap

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

A comprehensive survey of 10+ papers in the nexus T3 knowledge store (AgenticScholar, EvidenceNet, HybridRAG, Semantic Ladder, Memory in LLM Era/VLDB 2026, HoldUp, BBC, Robustness-delta@K, DeepEye/SIGMOD 2026, LitMOF, Compass) identified 6 enhancement gaps between current nexus capabilities and the state of the art. While nexus already implements strong foundations (hybrid search, section-type metadata and filtering, typed link graph, catalog-aware routing, plan caching, 3-tier memory), several high-value improvements are grounded in published research and ready for implementation.

> **Note**: The original survey identified 7 gaps. E1 (section-type filter at query time) was found to be already fully implemented during the gate review — `section_type` is filterable via `where="section_type!=references"` since v3.3.0 (RDR-055). E1 has been removed from this roadmap.

## Design: 6 Enhancements in 3 Phases

### Phase 1: Quick Win (start immediately)

#### E2: Retrieval Feedback Loop (Phase 1: logging) — DEFERRED

> **Status**: Deferred. RDR-057's `access_count` tracking (T1 + T2) provides a proxy
> for retrieval signal. The richer session-correlation logging (`relevance_log` table)
> is deferred until access_count data validates the need. E6 (memory consolidation)
> shipped using RDR-057's access_count schema instead of E2's relevance_log.

- No signal is captured about which search results agents actually use
- Log implicit relevance signal when a search->store_put or search->catalog_link pattern is detected within a session
- **Detection point**: T1 scratch correlation — when `store_put` or `catalog_link` is called, check T1 for recent search results in the same session; if overlap detected, log the (query, chunk_id, action) triple to a new `relevance_log` table in T2
- Future phases: extend `frecency.py` to T3 chunks (Phase 2), feed back as re-ranking signal in `scoring.py` (Phase 3)
- Source: No paper solves this well — differentiation opportunity
- Key files: `src/nexus/mcp/core.py` (store_put/catalog_link entry points), `src/nexus/db/t2.py` (relevance_log table), `src/nexus/db/t1.py` (recent search lookup)
- Effort: ~3 hours

### Phase 2: Foundation Builders (after Phase 1 review)

#### E3: Cross-Collection Entity Resolution

- Same concept indexed in `code__`/`docs__`/`rdr__` collections is unconnected — no cross-corpus links
- **Phase 2a — Symbol-name matching** (~3h): Extend `link_generator.py` with a new batch linker that scans prose/RDR chunks for code symbol names (function, class, module names from `code__` collections) and creates `mentions` links in the catalog
- **Phase 2b — LLM-driven hybrid extraction** (~4h): At `store_put` time for `knowledge__*` collections, run the EvidenceNet-inspired hybrid pipeline (see RF-061-8): (1) heuristic pass — title/keyword overlap against catalog → candidate (doc, target) pairs; (2) LLM verification pass (uncertain candidates only) — classify as {cites, implements, supersedes, relates, none} with confidence score; (3) filter confidence >= 0.7 → call `auto_link()`. Adopt EvidenceNet hybrid over HybridRAG pure-LLM to contain per-document API cost. Nexus entity types: Module, Function, Concept, Design Decision, Paper.
- Source: EvidenceNet cross-document duplicate resolution (RF-061-6), HybridRAG KG+vector fusion (RF-061-1), LLM entity extraction (RF-061-8)
- Key files: `src/nexus/catalog/auto_linker.py`, `src/nexus/catalog/link_generator.py`
- Effort: ~7 hours (3h symbol matching + 4h LLM hybrid)

#### E4: Composable Query Operators

> **Requires sub-RDR before implementation.** The design space (6 typed operators, DAG execution engine, plan library format migration, MCP integration) is too large for a bead-level specification. Create a dedicated design RDR to resolve: operator I/O type system, DAG serialization format, backward compatibility with existing prose plans, MCP tool surface (new tool vs. extending `query`).

- Current `nx:query` skill decomposes into ad-hoc natural language steps, not typed operators with defined I/O
- Target operators (pending sub-RDR design):
  - `Search(query, corpus, filters) -> ChunkSet`
  - `Traverse(entry, link_type, depth) -> EntrySet`
  - `Filter(set, predicate) -> Set`
  - `Summarize(chunks, focus) -> Text`
  - `Compare(chunk_sets[], dimensions) -> Matrix`
  - `Generate(context, prompt) -> Text`
- Plan library would store operator DAGs instead of prose; `nx:query` skill becomes an operator compiler
- Source: AgenticScholar operator algebra beats raw RAG by 45-56% on NDCG (RF-061-2)
- Key files: `src/nexus/search_engine.py`, `src/nexus/scoring.py`, new `src/nexus/operators.py`
- Effort: ~15 hours (requires sub-RDR for design; estimate covers operator schema + MCP interface + DAG engine + plan library migration)

### Phase 3: Differentiators (after Phase 2 review)

#### E5: Automatic Taxonomy / Topic Hierarchy

- Collections are flat bags of chunks with no topical organization
- Run AgenticScholar Algorithm 3 (incremental top-down taxonomy refinement with LLM clustering) over knowledge collections
- Use catalog tumbler tree as backbone; auto-generate topic labels for subtrees
- Enables "what's underexplored?" queries via MatrixConstruct pattern
- Requires: E3 (entity resolution). E4 dependency is **incidental, not architectural** — taxonomy construction can use existing `search_cross_corpus` + `cluster_results()` from `search_clusterer.py` if E4 is not yet available
- Source: AgenticScholar taxonomy construction (RF-061-2)
- Key files: new `src/nexus/taxonomy.py`, `src/nexus/catalog/catalog.py`
- Effort: ~5 hours

#### E6: Memory Consolidation (merging and dedup only)

- T2 memories accumulate without bound; overlapping entries on the same topic are never merged
- Detect overlapping memories via FTS5 similarity and offer consolidation (merge into single entry, delete originals)
- Flag memories not accessed in N sessions for review
- **T2 access-tracking schema (access_count, last_accessed, decay formula) is delegated to RDR-057 Phase 2.** E6 consumes that data but does not define the schema. The HoldUp vs. log-TTL formula discrepancy must be arbitrated in RDR-057 before E6 ships.
- Requires: E2 (feedback loop) for access data; RDR-057 Phase 2 for access-tracking columns
- Source: HoldUp (2604.02655), Memory in LLM Era VLDB framework (RF-061-4)
- Key files: `src/nexus/db/t2.py` (consumes access_count/last_accessed), new consolidation logic
- Effort: ~4 hours (consolidation/merging only; schema work is RDR-057)

#### E7: Content Transformation on Tier Promotion

- Delegated to RDR-057 (draft) — referenced here for completeness, not re-designed
- JIT strategy decided: formalize on access rather than eagerly

## Reranking Scores

Weights: A=user-visible value 25%, B=foundation built 20%, C=leverage 25%, D=literature evidence 15%, E=differentiation 15%.

> E1 removed (already shipped). E7 excluded (delegated to RDR-057). Remaining 5 enhancements ranked:

| Rank | Enhancement | A | B | C | D | E | Composite |
|------|------------|---|---|---|---|---|-----------|
| 1 | E3: Cross-collection entity resolution | 4 | 4 | 5 | 4 | 4 | 4.25 |
| 2 | E4: Composable operators | 3 | 3 | 4 | 5 | 5 | 3.85 |
| 3 | E2: Retrieval feedback | 3 | 3 | 5 | 3 | 5 | 3.80 |
| 4 | E5: Taxonomy | 4 | 2 | 2 | 5 | 5 | 3.40 |
| 5 | E6: Consolidation | 3 | 3 | 3 | 4 | 4 | 3.30 |

## Dependencies

- E5 requires E3 (architectural). E5's E4 dependency is incidental — can use existing cluster functions.
- E6 requires E2 (access data) AND RDR-057 Phase 2 (access-tracking schema)
- E7 is RDR-057 (separate)
- E2 is independent (Phase 1)
- E3 and E4 are independent (Phase 2), but E4 requires sub-RDR before beads are created
- ~~E1 removed — already shipped~~

## Related Work

- RDR-055 (closed): section-type metadata — E1 already shipped as part of this work
- RDR-056 (closed): search robustness — foundation for E2
- RDR-057 (draft): progressive formalization — IS E7, AND owns T2 access-tracking schema for E6
- RDR-058 (closed): pipeline orchestration — foundation for E4
- RDR-049/051/052/053 (closed): catalog infrastructure — foundation for E3, E5

## Tracking

- Epic bead: nexus-n94d
- T3 synthesis: tumbler 1.10.403
- T3 decision: decision-planner-enhancement-prioritization-2026-04-09
- Estimated effort: ~34 hours (3h E2 + 7h E3 + 15h E4 + 5h E5 + 4h E6; E7 is RDR-057)

## Research Findings

### RF-061-1: KG+Vector hybrid retrieval outperforms either alone (HIGH confidence)
**Source**: HybridRAG (arxiv 2408.04948, Sarmah et al. 2024, BlackRock/NVIDIA)
**Collection**: knowledge__hybridrag (50 chunks)
**Finding**: On financial earnings call transcripts (Q&A format), HybridRAG combining KG-based GraphRAG with VectorRAG outperforms both individually at retrieval accuracy and answer generation. KG captures entity relationships and structural context that vector similarity misses; vector search captures semantic similarity that rigid graph traversal misses.
**Nexus relevance**: Directly validates E3 (entity resolution) and E4 (composable operators). Nexus already has both a vector search engine and a catalog link graph — the missing piece is a query pipeline that fuses results from both. The composable operator `Traverse` + `Search` composition is exactly this pattern.

### RF-061-2: AgenticScholar operator pipeline beats RAG by 45-56% NDCG (HIGH confidence)
**Source**: AgenticScholar (knowledge__agentic-scholar, 172 chunks)
**Finding**: Taxonomy-anchored KG with composable operators (Search, Traverse, FindNode, Summarize, MatrixConstruct, Generate) and plan caching achieves 0.606-0.655 NDCG@3-7 vs 0.411-0.447 for plain RAG. Key differentiator: operators have typed I/O signatures and can be composed into DAGs.
**Nexus relevance**: Validates E4 (composable operators) and E5 (taxonomy). The plan library (plan_save/plan_search) already caches operator sequences — extend to typed operator DAGs.

### RF-061-3: Section-aware chunking improves evidence extraction (HIGH confidence)
**Source**: EvidenceNet (arxiv 2603.28325, knowledge__biomedical_kg, 136 chunks)
**Finding**: Excluding methods/references/acknowledgements sections from evidence extraction improves precision. Section-aware design reflects document structure where relevant content is concentrated in specific sections.
**Nexus relevance**: Validated E1 (section-type filter), which is now confirmed shipped since v3.3.0. This finding is retained as background evidence.

### RF-061-4: Memory systems need access-aware decay, not just time-based TTL (HIGH confidence, upgraded)
**Source**: Memory in LLM Era (2604.01707, VLDB 2026), HoldUp (2604.02655)
**Finding**: VLDB framework classifies memory operations into five verbs: Store, Retrieve, Update, **Forget**, **Summarize/Reflect**. Most systems implement only the first two. It identifies three Forget mechanisms: (1) time-based TTL (nexus current state), (2) access-frequency decay — items not retrieved lose salience, (3) consolidation-summarize — overlapping entries merge into summaries. The paper explicitly states time-TTL alone is insufficient because it assigns identical survival probability to a never-read entry and a heavily-consulted one. HoldUp formalizes access decay as `I(m) = recency × relevance × access_count`, decaying multiplicatively per session as `I_new = I_old × λ^(sessions_since_access)` with λ ∈ [0.7, 0.9], plus a retention floor: entries with `access_count >= 3` are never evicted.
**Nexus T2 gap (confirmed by code inspection)**: `db/t2.py` schema has `timestamp` (write-time only, reads never update it) and `ttl` (integer days from write). No `last_accessed` column. No `access_count` column. `get()` and `search()` have zero side effects on rows. The TTL clock is write-anchored and blind to access patterns.
**Proposed schema change**: Add `access_count INTEGER NOT NULL DEFAULT 0` and `last_accessed TEXT` (ISO, NULL = never read). Add `_touch(ids)` method called by `get()`/`search()`. Upgrade `expire()` to respect retention floor: entries with `access_count >= 3` survive even past TTL unless also idle for 30 days. Two ALTER TABLE statements, one UPDATE per read, zero migration risk (DEFAULT 0/NULL initializes existing rows).
**Schema ownership**: T2 access-tracking schema is owned by RDR-057 Phase 2. The HoldUp formula (multiplicative decay with retention floor) and the RDR-057 formula (log-based effective TTL) must be arbitrated there before implementation.
**Confidence upgrade justification**: VLDB explicitly names access-frequency as recommended Forget signal with comparative evidence. HoldUp provides tested formula. T2 gap confirmed by direct schema inspection. Upgraded MEDIUM-HIGH → HIGH.

### RF-061-5: Progressive formalization L0→L3 is the differentiator (HIGH confidence)
**Source**: Semantic Ladder (2603.22136), Formalization Flywheel synthesis
**Finding**: "The era of store and retrieve is over." Next-gen systems must transform content as it moves through tiers — from raw text (L0) to linked entities (L1) to typed relations (L2) to formal ontology (L3).
**Nexus relevance**: Validates E7/RDR-057. Currently T1→T2→T3 is copy, not transform.

### RF-061-6: Cross-document duplicate resolution critical for multi-source KGs (HIGH confidence)
**Source**: EvidenceNet (arxiv 2603.28325)
**Finding**: Same evidence indexed from different documents must be deduplicated via entity normalization and semantic similarity. Without dedup, graph density inflates artificially and retrieval quality degrades.
**Nexus relevance**: Validates E3 (entity resolution). Same concept in code__/docs__/rdr__ is currently 3 unrelated chunks.

### RF-061-8: LLM-driven entity-relation extraction — EvidenceNet hybrid approach preferred (HIGH confidence, deepened)
**Source**: HybridRAG (arxiv 2408.04948), EvidenceNet (arxiv 2603.28325)
**Finding**: HybridRAG uses a two-tiered LLM chain: Tier 1 refines chunk text, Tier 2 extracts SPO triples via prompt engineering over typed entity classes (company, financial metric, event, legal) with free-form NL predicates. Post-processed for coreference disambiguation and redundancy removal. **Key weakness**: free-form predicates don't map to nexus `follow_links` by type. EvidenceNet's hybrid strategy is more applicable: heuristics generate candidate pairs (semantic similarity + shared entity overlap), then LLM classifies uncertain pairs into a **closed relation vocabulary** (SUPPORTS, REFINES, EXTENDS, REPLICATES, CAUSAL_CHAIN) — cheaper than pure-LLM all-pairs.
**Nexus current state (confirmed by code inspection)**: `auto_linker.py` creates zero content-derived links — purely mechanical, instantiates links pre-seeded by caller via T1 scratch `link-context`. `link_generator.py` uses three batch heuristics only: bib cross-match (`cites`), regex file-path extraction (`implements`), module-name substring match (`implements-heuristic`). No LLM involved anywhere.
**Proposed LLM extractor for nexus**: At `store_put` time for `knowledge__*` collections: (1) heuristic pass — title/keyword overlap against catalog → candidate (doc, target) pairs; (2) LLM verification pass (uncertain candidates only) — classify as {cites, implements, supersedes, relates, none} with confidence score; (3) filter confidence >= 0.7 → call `auto_link()`. This enables content-derived link discovery without caller pre-seeding, closing the gap where auto_linker requires skills to know target tumblers in advance. Adopt EvidenceNet hybrid over HybridRAG pure-LLM to contain per-document API cost.
**Domain adaptation**: Nexus entity types are Module, Function, Concept, Design Decision, Paper — not financial entities. Relation types map directly to existing catalog vocabulary.

## Risks

1. E4 (composable operators) **requires its own sub-RDR** — the design space (6 operators, DAG engine, plan library migration, MCP surface) is too large for bead-level specification. E4 beads are blocked until the sub-RDR is created and gated.
2. E3 quality depends on metadata consistency across collections
3. E5 needs enough documents per collection for meaningful clusters
4. E6 must not penalize newly-added documents
5. E6 depends on RDR-057 Phase 2 for T2 access-tracking schema — if RDR-057 stalls, E6 is blocked

## Gate History

- **2026-04-09 Gate 1: BLOCKED** — C1: E1 already implemented (section_type filter live since v3.3.0). S1: E6/RDR-057 schema conflict. S2: E1 ghost in reranking. S3: E4 underestimated. S4: RF-061-8 not cross-referenced from E3. All issues resolved in this revision.
