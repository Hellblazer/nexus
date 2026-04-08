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

### RF-7: Implementation Feasibility — Schema Changes and FTS5 Consolidation

**Source**: Codebase audit of `t1.py`, `t2.py`, `catalog_db.py` (post-v3.3.0)

**T1 access tracking (Phase 1c)**: T1 is ChromaDB `EphemeralClient`, not SQLite. ChromaDB metadata is mutable via `col.update()` — `access_count` and `last_accessed` can be stored as metadata keys on each entry. `get()` and `search()` would call `col.update(ids=[id], metadatas=[{...existing, "access_count": n+1}])` after returning results. Cost: one extra ChromaDB call per get/search hit. No schema migration — ChromaDB metadata is schemaless.

**T2 schema changes (Phase 2b/2c)**: T2 `memory` table uses `CREATE TABLE` with no `access_count` column. SQLite `ALTER TABLE memory ADD COLUMN access_count INTEGER DEFAULT 0` is non-breaking and instant (no table rewrite). Same for `last_accessed TEXT`. The `expire()` method (line 584) uses `julianday('now') - julianday(timestamp) > ttl` — swapping to `effective_ttl = ttl / (1 + log(access_count + 1))` requires changing one SQL expression, no schema change beyond the new column.

**T2 FTS5 consolidation (Phase 2b)**: `put()` currently upserts by `(project, title)` — exact key match. For semantic overlap detection, the existing `memory_fts` FTS5 table (title + content + tags) is already built with sync triggers. A pre-put similarity check would be:
```sql
SELECT id, title, rank FROM memory m
JOIN memory_fts ON memory_fts.rowid = m.id
WHERE memory_fts MATCH ? AND m.project = ?
ORDER BY rank LIMIT 3
```
FTS5 `rank` is BM25 — negative values, closer to 0 = better match. **Threshold calibration needed**: BM25 scores are query-length-dependent, not absolute. A fixed threshold will false-positive on short entries and false-negative on long ones. Options: (a) normalize by query length, (b) use relative rank (top-1 rank < 0.7 * top-2 rank → likely duplicate), (c) supplement with embedding similarity from T3 if the entry exists there.

**`formalizes` link type (Phase 1a)**: Link types are free-form `TEXT` — no enum, no validation table. Adding `formalizes` requires zero schema changes. Just use it in `catalog.link()` calls. The unique constraint is `(from_tumbler, to_tumbler, link_type)`, so the same document pair can have both `cites` and `formalizes` links.

**`formalization_level` metadata (Phase 1b)**: Catalog `documents` table has `metadata JSON` column. `formalization_level` goes in the JSON — no ALTER TABLE needed. Queryable via `json_extract(metadata, '$.formalization_level')` in SQLite. However, `where=` in ChromaDB search expects metadata keys on chunks, not catalog entries. To make `--where formalization_level>=1` work in `nx search`, the field must be added to chunk metadata at index time (like `section_type` in RDR-055), not just catalog metadata. This means the indexing pipeline needs to read formalization_level from catalog and inject it into chunk metadata — a cross-tier dependency.

**Claim annotation on links (Phase 3a)**: Links table has `metadata JSON` column. Claims can go there — no schema change. The `from_span` and `to_span` TEXT columns already exist for content-addressed span references (RDR-053). A claim annotation is a third text field: `json_extract(metadata, '$.claim')`. The `catalog_link` MCP tool already accepts arbitrary metadata via its `metadata` parameter.

**Summary of migration burden**:
| Change | Mechanism | Migration |
|--------|-----------|-----------|
| T1 access_count | ChromaDB metadata key | None (schemaless) |
| T2 access_count | ALTER TABLE ADD COLUMN | Non-breaking, instant |
| T2 effective_ttl | SQL expression change | None |
| formalizes link | Free-form link_type | None |
| formalization_level | Catalog JSON metadata + chunk metadata at index time | Requires indexer change |
| claim annotation | Links JSON metadata | None |

**Risk**: The `formalization_level` cross-tier dependency (catalog → indexer → chunk metadata) is the only non-trivial migration. All other changes are additive with zero schema migration.

### RF-8: Write-Manage-Read as Universal Memory Loop

**Source**: Memory for Autonomous LLM Agents (arxiv 2603.07670, March 2026)

Defines six atomic memory operations: **consolidation, updating, indexing, forgetting, retrieval, compression**. The manage phase (between write and read) is where value is created — "summarize, deduplicate, score priority, resolve contradictions, and delete when appropriate." RDR-057 should specify which operations apply at each tier boundary rather than treating promotion as a single action. Forgetting complements heat-based promotion: low-heat items are candidates for eviction at T1/T2, not just low-priority items.

### RF-9: Inter-Context Contradiction Detection is Tractable

**Source**: Knowledge Conflicts for LLMs (arxiv 2403.08319, EMNLP 2024), FaithfulRAG (arxiv 2506.08938, ACL 2025)

Three conflict types: context-parametric, inter-context, intra-memory. Inter-context (new chunk vs. existing T3 chunks) is the most tractable — embed the candidate, search existing T3, extract fact-level conflicts. FaithfulRAG's self-fact mining externalizes facts before comparison. TruthfulRAG (arxiv 2511.10375) uses entropy-based filtering on KG triples — entropy spike = contradiction signal. Bayesian update principle (arxiv 2503.10996): single-agent contradiction triggers a flag, not a rewrite; multi-agent convergence reinforces. Start with inter-context conflicts in Phase 3.

### RF-10: Multi-Agent Semantic Consistency (Not a Locking Problem)

**Source**: Multi-Agent Memory from a Computer Architecture Perspective (arxiv 2603.10062, March 2026, SIGARCH); codebase audit of concurrency primitives

Frames multi-agent memory as a hardware architecture problem. However, Nexus's **structural concurrency is already handled**: T3 cloud uses REST API with atomic upserts (ChromaDB Cloud); T2 SQLite uses WAL mode + file-level locks for cross-process safety; T3 local has per-collection `BoundedSemaphore` for in-process threading. No locking primitives are missing.

The real gap is **semantic consistency**: agent A writes "caching uses Redis" while agent B writes "caching uses Memcached" — neither checks for contradictions. This is RF-9's domain (inter-context contradiction detection), not a concurrency control problem. Provenance metadata (`source_agent`, `session_id`) already exists on T3 chunks — it just isn't used for conflict detection.

### RF-11: JIT Formalization as Competing Design Point

**Source**: General Agentic Memory Via Deep Research (arxiv 2511.18423, November 2025)

Proposes JIT compilation instead of transform-at-boundary: keep raw data in a page-store (Memorizer), compute formalization at query time (Researcher agent). Achieves >90% accuracy on RULER Multi-Hop Tracing vs. <60% for transform-at-write approaches. This is the design tension RDR-057 must resolve: eager formalization (transform at tier boundary) vs. lazy formalization (transform at query time). Nexus's `query()` MCP tool with catalog routing is already partially lazy — it composes search scopes at query time rather than pre-computing them. The right answer may be hybrid: cheap transforms (section_type, dedup) at write time, expensive transforms (claim extraction, contradiction detection) at query time.

### RF-12: GST System/Boundary Analysis

**Source**: General Systems Theory critique of proposed design

Six boundaries where signals cross in the formalization system:

| Boundary | Direction | Current Signal | Gap |
|----------|-----------|---------------|-----|
| Agent → T1 | write | tags, persist flag | No heat signal — agent doesn't know what's frequently retrieved |
| T1 → T2 | promote | verbatim copy | No transformation, no feedback ("this duplicates existing T2 entry X") |
| T2 → T3 | store_put | raw upsert | No consolidation check, no contradiction check against existing T3 |
| T3 → Agent | search/query | ranked chunks | No confidence/staleness/contradiction signal on returned results |
| Agent → Agent | shared T3 | source_agent metadata | No mutual awareness of concurrent writes (RF-10 semantic gap) |
| Catalog → Search | pre-filter | source_path routing | No formalization-level signal in routing decisions |

**Three GST findings**:

1. **The flywheel is open-loop.** No feedback from later tiers to earlier ones. An agent writing to T1 never learns "this was promoted and became valuable" or "this contradicted existing knowledge." Every boundary crossing needs a feedback channel for the system to self-correct. Minimum viable feedback: `promote()` returns a consolidation report (merged/new/conflicting), `store_put` returns a contradiction flag.

2. **`formalization_level` should be derived, not assigned.** A static label set at index time is a declaration, not a measurement. The level should emerge from content structure: does it have extracted entities (L1)? Subject-predicate-object triples (L2)? Validated claims with provenance (L3)? `section_type` (RDR-055) is the right pattern — it's derived from content, not declared.

3. **Contradiction detection belongs at retrieval too, not just write.** The proposed design checks for contradictions only at the T2→T3 write boundary. But two contradictory entries may both enter T3 via different paths (direct indexing, different agents). The retrieval boundary (search/query) should flag when contradictory entries appear together in results — aligning with RF-11's JIT formalization. Cost: one embedding similarity check between returned chunks, ~2ms for N=10.

## Proposed Design

### Architecture: Closed-Loop Formalization

The system operates at six boundaries (RF-12). Each boundary has a **forward path** (data flows
toward higher formalization) and a **feedback path** (signals flow back to inform earlier tiers).
Cheap transforms happen at write time; expensive transforms happen at query time (RF-11 hybrid).

```
                          ┌─── feedback: consolidation report ───┐
                          │                                      │
T1 scratch (L0)           ▼                                      │
  ──write──► access tracking ──promote──► T2 memory (L1)         │
                                           │                     │
                          ┌── feedback: contradiction flag ──┐   │
                          │                                  │   │
                          ▼                                  │   │
               T2 ──store_put──► T3 knowledge ──────────────►│   │
                                   │                         │   │
                     ┌─ retrieval ──┘                         │   │
                     │  + contradiction check (JIT)          │   │
                     ▼                                       │   │
                   Agent ◄── confidence/staleness signal ────┘   │
                     │                                           │
                     └── next write to T1 ───────────────────────┘
```

### Design Principle: Derive, Don't Declare

Formalization level is **computed from content structure**, not assigned as a static label.
Following the `section_type` pattern (RDR-055), which derives type from heading text:

| Level | Detection Rule | Example |
|-------|---------------|---------|
| L0 | Raw text, no structural markup | T1 scratch entries |
| L1 | Has extracted entities or section_type annotations | Markdown chunks with `section_type != ""` |
| L2 | Has subject-predicate-object triples or claim spans | Entries with `formalizes` links to L0 sources |
| L3 | Has validated claims with provenance chain | Entries with `formalizes` links + contradiction-free status |

`formalization_level` is recomputed on read (or cached and invalidated when links change),
not stamped at index time. This means it's always current — a chunk that gains a `formalizes`
link automatically moves from L0 to L2 without re-indexing.

### Phase 1: Foundation (hours-days)

**1a. `formalizes` link type**

Link types are free-form TEXT — zero schema changes. Just use `formalizes` in `catalog.link()`
calls. The unique constraint `(from_tumbler, to_tumbler, link_type)` allows the same document
pair to have both `cites` and `formalizes` links. ~10 LOC.

**1b. Derived `formalization_level` function**

Add `formalization_level(entry: CatalogEntry, catalog: Catalog) -> int` to `catalog.py`:
```python
def formalization_level(entry: CatalogEntry, catalog: Catalog) -> int:
    """Derive formalization level from content structure and link graph."""
    formalizes_links = catalog.links_from(entry.tumbler, link_type="formalizes")
    if formalizes_links:
        # Has validated claims? Check for contradiction-free status
        return 3 if entry.meta.get("contradiction_free") else 2
    if entry.meta.get("section_type") or entry.meta.get("entities"):
        return 1
    return 0
```
No static metadata field. Queryable via a helper that computes on demand or via a catalog
view that caches levels (invalidated on link changes).

**1c. T1 access tracking**

T1 is ChromaDB EphemeralClient — metadata is schemaless. Add `access_count` and `last_accessed`
as metadata keys. Increment in `get()` and `search()` via `col.update()`. No schema migration.

**1d. Boundary feedback: promote() returns report**

Change `T1Database.promote()` return type from `None` to a `PromotionReport`:
```python
@dataclass
class PromotionReport:
    action: str          # "new", "merged", "conflicting"
    existing_title: str | None  # title of the entry it merged with or conflicts with
    merged: bool
```
Before upserting to T2, FTS5-search the target project for overlap. If found, report it.
The agent decides whether to proceed, merge, or abort.

### Phase 2: Consolidation and Boundary Transforms (weeks)

**2a. T2 consolidation on put()**

Before upserting, FTS5 search same project for semantic overlap (RF-7 feasibility analysis):
```sql
SELECT id, title, rank FROM memory m
JOIN memory_fts ON memory_fts.rowid = m.id
WHERE memory_fts MATCH ? AND m.project = ?
ORDER BY rank LIMIT 3
```
BM25 `rank` is query-length-dependent — use relative ranking (top-1 rank < 0.7 × top-2 rank)
rather than a fixed threshold. When overlap detected, return a `ConsolidationReport` to the
caller. Append `consolidated_from` to tags on merge.

**2b. Boundary feedback: store_put() returns contradiction flag**

In `store_put` MCP handler, after upserting to T3, embed the new content and search existing
T3 entries in the same collection. If any result has distance < 0.3 (very similar) but
content diverges (edit distance > 50%), flag as potential contradiction:
```python
return f"Stored. ⚠ Potential contradiction with: {conflicting_title} (distance: {d:.3f})"
```
The agent sees this in the MCP response and can investigate. No automatic resolution —
Bayesian update principle (RF-9): single contradiction = flag, multi-agent convergence = reinforce.

**2c. Relevance-decay expiry for T2**

`ALTER TABLE memory ADD COLUMN access_count INTEGER DEFAULT 0` (non-breaking, instant).
Track in `get()` and `search()`. Modify `expire()`:
```python
effective_ttl = base_ttl / (1 + math.log(access_count + 1))
```

**2d. Summarization on T1→T2 promotion (optional)**

In `promote()`, optionally run LLM summarization before storing to T2. Make async via
session-close hook. The summary persists; original stays in T1 until session end.

### Phase 3: Retrieval-Time Contradiction Detection (weeks)

**3a. JIT contradiction check in search results** (RF-11 + RF-12 correction 3)

In `search_cross_corpus()`, after threshold filtering and optional clustering, check
returned results for pairwise contradiction. Cheap approach: if two results from the same
collection have distance < 0.3 to each other but different `source_agent` provenance,
add `_contradiction_flag: true` to their metadata. The agent sees the flag and can
investigate. Cost: O(N²) distance check for N results — ~2ms for N=10, negligible.

More expensive (Phase 3b): extract key claims from top-K results via LLM, compare
pairwise. Only trigger when `_contradiction_flag` is set from the cheap check.

**3b. Claim annotation on catalog links**

Use existing `metadata JSON` column on links table — no schema change needed. Store
claiming passage via `json_extract(metadata, '$.claim')`. Update `auto_link()` to store
claim text from `link-context` scratch. Update `catalog_link` MCP tool docs to show
the `claim` metadata key.

**3c. Edge-type weights in follow_links**

Add per-link-type weights to result scoring in catalog-aware search:
- `formalizes`: 1.0 (exact semantic content at different formalization level)
- `implements`: 0.9
- `cites`: 0.7
- `relates`: 0.5

### Phase 4: Community Detection (medium-term)

Wire existing `search_clusterer.py` (RDR-056 P2b) into `link_generator.py` as
`generate_community_links()`. Fetch document embeddings via `T3Database.get_embeddings()`
(RDR-056 P2c). Create synthetic catalog entries per community. Enable two-stage retrieval:
community summary → drill into members. All infrastructure exists — this is integration work.

## Risks and Mitigations

| Risk | Mitigation |
|------|------------|
| LLM summarization adds latency to T1→T2 promotion | Make async; queue for session-close hook. Phase 2d is optional. |
| FTS5 consolidation false-positives | Relative BM25 ranking (not fixed threshold); log consolidations for review |
| Schema migration for T2 access_count | SQLite ALTER TABLE ADD COLUMN is non-breaking, instant |
| Derived formalization_level is expensive to compute | Cache in catalog JSON metadata; invalidate on link changes |
| JIT contradiction check adds query latency | O(N²) for N=10 is ~2ms. LLM claim extraction (Phase 3b) only triggers on flag |
| Feedback signals overwhelm agents | Reports are informational, not blocking. Agent decides action. |

## Success Criteria

- [ ] `formalizes` link type accepted by catalog
- [ ] `formalization_level()` function returns correct level based on content structure + links
- [ ] `promote()` returns `PromotionReport` with action (new/merged/conflicting)
- [ ] `store_put` MCP returns contradiction flag when similar-but-divergent content detected
- [ ] T1 entries with access_count > 3 auto-promoted at session close
- [ ] T2 `put()` detects and reports semantically overlapping entries
- [ ] Search results carry `_contradiction_flag` when conflicting entries co-occur
- [ ] Catalog links carry claim annotations (at least for auto-linker path)
- [ ] Phase 4 community detection uses existing `search_clusterer.py` module
