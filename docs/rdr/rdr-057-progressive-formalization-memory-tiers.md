---
title: "Progressive Formalization Across Memory Tiers"
id: RDR-057
type: Feature
status: draft
priority: high
author: Hal Hildebrand
created: 2026-04-07
related_issues: [RDR-053, RDR-055, RDR-056]
---

# RDR-057: Progressive Formalization Across Memory Tiers

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

Nexus's T1→T2→T3 tier system promotes containers, not representations. `T1.promote()` copies raw text verbatim — no transformation, no summarization, no entity extraction. All three tiers hold L0 (raw text) content in different-durability stores. Meanwhile:

1. **No heat-based promotion**: No tracking of which T1 entries are retrieved frequently. At session close, all unpromoted T1 entries are silently lost regardless of usage.
2. **No consolidation**: T2 `put()` upserts by title (replaces) or creates duplicates. No detection of semantically overlapping entries.
3. **No contradiction detection**: Two conflicting knowledge entries in T3 coexist indefinitely without any system awareness.
4. **No claim-level links**: Catalog edges are document-level. A `cites` link with no claim annotation is a bibliography entry, not a propositional assertion.

The Semantic Ladder (2603.22136) demonstrates that semantic value is created at transformation boundaries, not at storage boundaries.

## Research Findings

### RF-1: Hierarchical Abstraction in SOTA Memory Systems

**Source**: Memory in the LLM Era (arxiv 2604.01707, indexed: docs__default)

Top-performing systems (MemoryOS, the paper's own new method) hold content at different abstraction levels per tier: short-term = raw dialogue, mid-term = LLM-generated segment summaries, long-term = community-aggregated abstractions. Lesson L1: "raw content retention is necessary but not sufficient." L2: "information completeness requires structured representations alongside raw content."

### RF-2: The Semantic Ladder's Four Levels

**Source**: Semantic Ladder (arxiv 2603.22136, indexed: docs__default)

L0 (free text) → L1 (entity-linked, vocabulary-tagged) → L2 (RDF subject-predicate-object / Rosetta Statements) → L3 (OWL Description Logics). Transformations extend, not replace — all representations coexist. Multi-representation semantic equivalence is a first-class property.

### RF-3: Heat-Based Memory Management

**Source**: Memory in the LLM Era (arxiv 2604.01707)

The paper's new SOTA method promotes entries based on heat score: `heat = f(access_frequency, recency)`. Relevance-decay expiry: `effective_ttl = base_ttl / (1 + log(access_count + 1))` — highly accessed entries survive longer. Entries never accessed expire sooner.

### RF-4: Schema Evolution Convergence

**Source**: Schema evolution corpus (65 papers, knowledge__knowledge)

The Semantic Ladder's monotonic-extension property (transformations extend, not replace) converges with the schema evolution corpus's core thesis: additive changes are safer than breaking changes. Both BFDB (bottom-up SPO triples) and the Semantic Ladder (bottom-up L0→L3) agree on pragmatic bottom-up direction.

### RF-5: `formalizes` Link Type and RDR-053 Connection

**Source**: 720 synthesis knowledge-graph axis

Adding `formalizes` as a catalog link type operationalizes multi-representation equivalence in ~10 LOC. RDR-053's `chunk_text_hash` (RF-6) is prerequisite for stable `formalizes` span links — L2 representations pointing to specific L0 chunks need content-addressed spans to survive re-indexing.

### RF-6: RDR-055/056 Infrastructure Accelerates Phases 1-4

**Source**: Codebase audit post-v3.3.0 (RDR-055 section_type, RDR-056 search robustness)

RDR-055 and RDR-056 shipped infrastructure that directly serves RDR-057's phases:

**Phase 1 (Foundation) — already partially delivered:**
- `section_type` metadata (RDR-055) is an L1 annotation on L0 chunks — the first Semantic Ladder transformation already exists in production. `classify_section_type()` in `md_chunker.py` is the pattern for future L1 classifiers. Phase 1b's `formalization_level` field can seed `1` for chunks with `section_type != ""` instead of universally `0`, giving immediate queryable signal.

**Phase 2 (Tier Boundary Transformations) — informed by thresholds:**
- Per-corpus distance thresholds (RDR-056 P1c) provide empirically validated semantic boundaries: knowledge/docs noise starts at distance 0.67, relevant content ends at 0.59. These values directly calibrate Phase 2b's T2 consolidation similarity threshold — entries within 0.59 distance are candidates for merge.
- `_prefilter_from_catalog()` (RDR-056 P3) demonstrates the pattern for routing through catalog SQLite before vector operations — same architecture T2 consolidation would use (query FTS5 for overlap candidates, then vector-verify).

**Phase 4 (Community Detection) — module already exists:**
- `search_clusterer.py` (RDR-056 P2b) implements Ward hierarchical clustering with numpy k-means fallback. Phase 4's `generate_community_links()` can call `cluster_results()` directly — the algorithm, determinism guarantees, and scipy/numpy fallback are already tested (16 tests).
- `T3Database.get_embeddings()` (RDR-056 P2c) solves the embedding post-fetch problem. Phase 4 needs document embeddings for community detection — `get_embeddings()` already handles per-collection batching with `_chroma_with_retry`.
- `Catalog.doc_count()` (RDR-056 P3) enables selectivity calculations reusable for consolidation threshold tuning.

**Net effect**: Phase 1 scope shrinks (~10 LOC for `formalizes` link + metadata field, vs building L1 classification from scratch). Phase 4 becomes integration work rather than greenfield (wire existing `cluster_results()` into `link_generator.py`). Phase 2 consolidation has empirical threshold data instead of guesswork.

## Proposed Design

### The Formalization Flywheel

```
T1 scratch (L0 raw)
  → heat-based promotion + LLM summarization
    → T2 memory (L1 annotated)
      → consolidation + entity linking
        → T3 knowledge (L1→L2 structured claims)
          → community detection + graph-based RAG
            → Catalog links with claim annotations (L2)
              → cross-validation against sources
```

### Phase 1: Foundation (hours-days)

**1a. `formalizes` link type**

Add to valid link types in `catalog.py`. Update link_generator.py docs. ~10 LOC.

**1b. `formalization_level` metadata field**

Add `formalization_level` (int, 0-3) as recognized key in `CatalogEntry.meta`. Seed `formalization_level=0` for all existing entries via one-time catalog update.

**1c. T1 access tracking**

Add `access_count INTEGER DEFAULT 0` and `last_accessed TEXT` to T1 schema. Increment on every `get()` and `search()` that returns the entry.

### Phase 2: Tier Boundary Transformations (weeks)

**2a. Summarization on T1→T2 promotion**

In `T1Database.promote()`, run LLM summarization pass before storing to T2. The summary (not raw text) persists. Keep original in T1 until session close.

**2b. T2 consolidation on put()**

Before upserting, run FTS5 `search()` against same project. If similarity > threshold, append/merge rather than replace. Add `consolidated_from` list to tags.

**2c. Relevance-decay expiry for T2**

Add `access_count` to T2 schema. Track in `get()` and `search()`. Modify `expire()`:
```python
effective_ttl = base_ttl / (1 + math.log(access_count + 1))
```

### Phase 3: Claim-Level Links (weeks)

**3a. Claim annotation on catalog links**

Extend `catalog_db.py` link schema with `claim TEXT DEFAULT ""` column. Update `auto_link()` to store claiming passage from `link-context` scratch. Update `catalog_link` MCP tool to accept `claim` parameter.

**3b. Edge-type weights in follow_links**

Add per-link-type weights to result scoring in catalog-aware search:
- `formalizes`: 1.0 (exact semantic content at different formalization level)
- `implements`: 0.9
- `cites`: 0.7
- `relates`: 0.5

### Phase 4: Community Detection (medium-term)

Add `generate_community_links()` to `link_generator.py`. Fetch document embeddings, k-means cluster, create synthetic catalog entries per community. Enable two-stage retrieval: community summary → drill into members.

## Risks and Mitigations

| Risk | Mitigation |
|------|------------|
| LLM summarization adds latency to T1→T2 promotion | Make async; queue summarization for session-close hook |
| FTS5 consolidation may false-positive merge distinct entries | Conservative threshold; log consolidations for review |
| Schema migration for access_count columns | SQLite ALTER TABLE ADD COLUMN is non-breaking |
| Seed formalization_level=0 for existing entries | Batch catalog update script; idempotent |

## Success Criteria

- [ ] `formalizes` link type accepted by catalog
- [ ] `formalization_level` field queryable via `where=` in search/query
- [ ] T1 entries with access_count > 3 auto-promoted at session close
- [ ] T2 put() detects and merges semantically overlapping entries
- [ ] Catalog links carry claim annotations (at least for auto-linker path)
