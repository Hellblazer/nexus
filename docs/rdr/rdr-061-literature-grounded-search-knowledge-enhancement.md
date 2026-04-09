---
title: "Literature-Grounded Search and Knowledge Enhancement Roadmap"
id: RDR-061
type: design
status: draft
priority: P1
author: Hal Hildebrand
created: 2026-04-09
related_issues: [RDR-055, RDR-056, RDR-057, RDR-058, RDR-049, RDR-051, RDR-052, RDR-053]
related_notes: >
  RDR-055 (closed): Section-type metadata — foundation for E1.
  RDR-056 (closed): Search robustness — foundation for E1, E2.
  RDR-057 (draft): Progressive formalization — IS E7.
  RDR-058 (accepted): Pipeline orchestration — related to E4.
  RDR-049/051/052/053 (closed): Catalog infrastructure — foundation for E3, E5.
---

# RDR-061: Literature-Grounded Search and Knowledge Enhancement Roadmap

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

A comprehensive survey of 10 papers in the nexus T3 knowledge store (AgenticScholar, EvidenceNet, Semantic Ladder, Memory in LLM Era/VLDB 2026, HoldUp, BBC, Robustness-delta@K, DeepEye/SIGMOD 2026, LitMOF, Compass) identified 7 enhancement gaps between current nexus capabilities and the state of the art. While nexus already implements strong foundations (hybrid search, section-type metadata, typed link graph, catalog-aware routing, plan caching, 3-tier memory), several high-value improvements are grounded in published research and ready for implementation.

## Design: 7 Enhancements in 3 Phases

### Phase 1: Quick Wins (parallelizable, start immediately)

#### E1: Section-Type Filter at Query Time

- `section_type` metadata is indexed on every chunk (RDR-055) but not exposed as a search filter
- Wire `section_type` through `filters.py` WHERE clause parsing into `search_engine.py`
- Source: EvidenceNet excludes methods/references sections from evidence extraction
- Key files: `src/nexus/filters.py`, `src/nexus/search_engine.py`
- Effort: ~2 hours

#### E2: Retrieval Feedback Loop (Phase 1: logging)

- No signal is captured about which search results agents actually use
- Log implicit relevance signal when a search->store_put or search->catalog_link pattern is detected within a session
- Future phases: extend `frecency.py` to T3 chunks (Phase 2), feed back as re-ranking signal in `scoring.py` (Phase 3)
- Source: No paper solves this well — differentiation opportunity
- Key files: `src/nexus/db/t3.py`, `src/nexus/search_engine.py`, `src/nexus/frecency.py`, `src/nexus/scoring.py`
- Effort: ~2 hours (Phase 1 logging only)

### Phase 2: Foundation Builders (after Phase 1 review)

#### E3: Cross-Collection Entity Resolution

- Same concept indexed in `code__`/`docs__`/`rdr__` collections is unconnected — no cross-corpus links
- Extend auto_linker with symbol-name matching: when a code symbol name appears in a prose chunk, create a `mentions` link in the catalog
- Add semantic concept dedup: detect when two chunks across collections discuss the same concept
- Source: EvidenceNet cross-document duplicate resolution, AgenticScholar taxonomy alignment
- Key files: `src/nexus/catalog/auto_linker.py`, `src/nexus/catalog/link_generator.py`
- Effort: ~5 hours (symbol matching + semantic dedup)

#### E4: Composable Query Operators

- Current `nx:query` skill decomposes into ad-hoc natural language steps, not typed operators with defined I/O
- Define 5-6 composable operators with typed signatures:
  - `Search(query, corpus, filters) -> ChunkSet`
  - `Traverse(entry, link_type, depth) -> EntrySet`
  - `Filter(set, predicate) -> Set`
  - `Summarize(chunks, focus) -> Text`
  - `Compare(chunk_sets[], dimensions) -> Matrix`
  - `Generate(context, prompt) -> Text`
- Plan library stores operator DAGs instead of prose; `nx:query` skill becomes an operator compiler
- Source: AgenticScholar operator algebra beats raw RAG by 45-56% on NDCG
- Key files: `src/nexus/search_engine.py`, `src/nexus/scoring.py`, new `src/nexus/operators.py`
- Effort: ~5 hours (typed pipeline + MCP integration)

### Phase 3: Differentiators (after Phase 2 review)

#### E5: Automatic Taxonomy / Topic Hierarchy

- Collections are flat bags of chunks with no topical organization
- Run AgenticScholar Algorithm 3 (incremental top-down taxonomy refinement with LLM clustering) over knowledge collections
- Use catalog tumbler tree as backbone; auto-generate topic labels for subtrees
- Enables "what's underexplored?" queries via MatrixConstruct pattern
- Requires: E3 (entity resolution) + E4 (operators) as prerequisites
- Source: AgenticScholar taxonomy construction
- Key files: new `src/nexus/taxonomy.py`, `src/nexus/catalog/catalog.py`
- Effort: ~5 hours

#### E6: Memory Consolidation and Relevance Decay

- T2 memories accumulate without bound; TTL is time-based only, not relevance-based
- Add access tracking on T2 memory entries (which are consulted by agents)
- Flag memories not accessed in N sessions for review/consolidation
- Merge overlapping memories covering the same topic
- Requires: E2 (feedback loop) for access data
- Source: HoldUp (2604.02655), Memory in LLM Era VLDB framework
- Key files: `src/nexus/db/t2.py`, new consolidation logic
- Effort: ~4 hours

#### E7: Content Transformation on Tier Promotion

- Already covered by RDR-057 (draft) — referenced here for completeness, not re-designed
- JIT strategy decided: formalize on access rather than eagerly

## Reranking Scores

Weights: A=user-visible value 25%, B=foundation built 20%, C=leverage 25%, D=literature evidence 15%, E=differentiation 15%.

| Rank | Enhancement | A | B | C | D | E | Composite |
|------|------------|---|---|---|---|---|-----------|
| 1 | E3: Cross-collection entity resolution | 4 | 4 | 5 | 4 | 4 | 4.25 |
| 2 | E1: Section-type filter | 5 | 5 | 3 | 4 | 3 | 4.05 |
| 3 | E7: Tier promotion (RDR-057) | 4 | 4 | 3 | 5 | 4 | 3.90 |
| 4 | E4: Composable operators | 3 | 3 | 4 | 5 | 5 | 3.85 |
| 5 | E2: Retrieval feedback | 3 | 3 | 5 | 3 | 5 | 3.80 |
| 6 | E5: Taxonomy | 4 | 2 | 2 | 5 | 5 | 3.40 |
| 7 | E6: Consolidation | 3 | 3 | 3 | 4 | 4 | 3.30 |

## Dependencies

- E5 requires E3 + E4
- E6 requires E2
- E7 is RDR-057 (separate)
- E1 and E2 are independent (Phase 1)
- E3 and E4 are independent (Phase 2)

## Related Work

- RDR-055 (closed): section-type metadata — foundation for E1
- RDR-056 (closed): search robustness — foundation for E1, E2
- RDR-057 (draft): progressive formalization — IS E7
- RDR-058 (accepted): pipeline orchestration — related to E4
- RDR-049/051/052/053 (closed): catalog infrastructure — foundation for E3, E5

## Tracking

- Epic bead: nexus-n94d
- T3 synthesis: tumbler 1.10.403
- T3 decision: decision-planner-enhancement-prioritization-2026-04-09

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
**Nexus relevance**: Validates E1 (section-type filter). RDR-055 already indexes section_type metadata — just needs filter plumbing.

### RF-061-4: Memory systems need access-aware decay, not just time-based TTL (HIGH confidence, upgraded)
**Source**: Memory in LLM Era (2604.01707, VLDB 2026), HoldUp (2604.02655)
**Finding**: VLDB framework classifies memory operations into five verbs: Store, Retrieve, Update, **Forget**, **Summarize/Reflect**. Most systems implement only the first two. It identifies three Forget mechanisms: (1) time-based TTL (nexus current state), (2) access-frequency decay — items not retrieved lose salience, (3) consolidation-summarize — overlapping entries merge into summaries. The paper explicitly states time-TTL alone is insufficient because it assigns identical survival probability to a never-read entry and a heavily-consulted one. HoldUp formalizes access decay as `I(m) = recency × relevance × access_count`, decaying multiplicatively per session as `I_new = I_old × λ^(sessions_since_access)` with λ ∈ [0.7, 0.9], plus a retention floor: entries with `access_count >= 3` are never evicted.
**Nexus T2 gap (confirmed by code inspection)**: `db/t2.py` schema has `timestamp` (write-time only, reads never update it) and `ttl` (integer days from write). No `last_accessed` column. No `access_count` column. `get()` and `search()` have zero side effects on rows. The TTL clock is write-anchored and blind to access patterns.
**Proposed schema change**: Add `access_count INTEGER NOT NULL DEFAULT 0` and `last_accessed TEXT` (ISO, NULL = never read). Add `_touch(ids)` method called by `get()`/`search()`. Upgrade `expire()` to respect retention floor: entries with `access_count >= 3` survive even past TTL unless also idle for 30 days. Two ALTER TABLE statements, one UPDATE per read, zero migration risk (DEFAULT 0/NULL initializes existing rows).
**Confidence upgrade justification**: VLDB explicitly names access-frequency as recommended Forget signal with comparative evidence. HoldUp provides tested formula. T2 gap confirmed by direct schema inspection. Upgraded MEDIUM-HIGH → HIGH.

### RF-061-5: Progressive formalization L0→L3 is the differentiator (HIGH confidence)
**Source**: Semantic Ladder (2603.22136), Formalization Flywheel synthesis
**Finding**: "The era of store and retrieve is over." Next-gen systems must transform content as it moves through tiers — from raw text (L0) to linked entities (L1) to typed relations (L2) to formal ontology (L3).
**Nexus relevance**: Validates E7/RDR-057. Currently T1→T2→T3 is copy, not transform.

### RF-061-6: Cross-document duplicate resolution critical for multi-source KGs (HIGH confidence)
**Source**: EvidenceNet (arxiv 2603.28325)
**Finding**: Same evidence indexed from different documents must be deduplicated via entity normalization and semantic similarity. Without dedup, graph density inflates artificially and retrieval quality degrades.
**Nexus relevance**: Validates E3 (entity resolution). Same concept in code__/docs__/rdr__ is currently 3 unrelated chunks.

### RF-061-7: Bucket-based collection + robustness metrics for ANN search (MEDIUM confidence)
**Source**: BBC (2604.01960), Robustness-δ@K (2507.00379)
**Finding**: BBC achieves 3.8x speedup at recall@0.95 for large-k ANN. Robustness-δ@K provides a formal metric for ANN stability. Both address tail-failure problems in HNSW.
**Nexus relevance**: Background for RDR-056 (closed). No new action needed — over-fetch+threshold already addresses this.

### RF-061-8: LLM-driven entity-relation extraction — EvidenceNet hybrid approach preferred (HIGH confidence, deepened)
**Source**: HybridRAG (arxiv 2408.04948), EvidenceNet (arxiv 2603.28325)
**Finding**: HybridRAG uses a two-tiered LLM chain: Tier 1 refines chunk text, Tier 2 extracts SPO triples via prompt engineering over typed entity classes (company, financial metric, event, legal) with free-form NL predicates. Post-processed for coreference disambiguation and redundancy removal. **Key weakness**: free-form predicates don't map to nexus `follow_links` by type. EvidenceNet's hybrid strategy is more applicable: heuristics generate candidate pairs (semantic similarity + shared entity overlap), then LLM classifies uncertain pairs into a **closed relation vocabulary** (SUPPORTS, REFINES, EXTENDS, REPLICATES, CAUSAL_CHAIN) — cheaper than pure-LLM all-pairs.
**Nexus current state (confirmed by code inspection)**: `auto_linker.py` creates zero content-derived links — purely mechanical, instantiates links pre-seeded by caller via T1 scratch `link-context`. `link_generator.py` uses three batch heuristics only: bib cross-match (`cites`), regex file-path extraction (`implements`), module-name substring match (`implements-heuristic`). No LLM involved anywhere.
**Proposed LLM extractor for nexus**: At `store_put` time for `knowledge__*` collections: (1) heuristic pass — title/keyword overlap against catalog → candidate (doc, target) pairs; (2) LLM verification pass (uncertain candidates only) — classify as {cites, implements, supersedes, relates, none} with confidence score; (3) filter confidence >= 0.7 → call `auto_link()`. This enables content-derived link discovery without caller pre-seeding, closing the gap where auto_linker requires skills to know target tumblers in advance. Adopt EvidenceNet hybrid over HybridRAG pure-LLM to contain per-document API cost.
**Domain adaptation**: Nexus entity types are Module, Function, Concept, Design Decision, Paper — not financial entities. Relation types map directly to existing catalog vocabulary.

## Risks

1. E4 (composable operators) may need its own sub-RDR if the design space proves large
2. E3 quality depends on metadata consistency across collections
3. E5 needs enough documents per collection for meaningful clusters
4. E6 must not penalize newly-added documents
