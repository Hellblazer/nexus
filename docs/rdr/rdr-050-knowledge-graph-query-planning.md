---
title: "Knowledge Graph and Catalog-Aware Query Planning"
id: RDR-050
type: Architecture
status: closed
accepted_date: 2026-04-05
closed_date: 2026-04-05
close_reason: implemented
priority: medium
author: Hal Hildebrand
reviewed-by: self
created: 2026-04-04
related_issues:
  - "RDR-049 - Git-Backed Xanadu-Inspired Catalog for T3 (closed, implemented)"
  - "RDR-042 - AgenticScholar Enhancements (closed)"
---

# RDR-050: Knowledge Graph and Catalog-Aware Query Planning

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

Nexus has a query planner (RDR-042) that decomposes analytical questions into `search → extract → summarize → rank → compare → generate` steps, and a catalog (RDR-049) that provides permanent tumbler addressing and typed bidirectional links across the entire docuverse. These two systems don't know about each other.

Today's query planner can only search — it has no way to navigate relationships. "What papers cite Fagin 2007?" requires knowing which collections to search. "What research informed the chunker module?" is unanswerable. "Compare Fagin and Bernstein's approaches" hits all 95 `docs__` collections blindly.

The catalog's link graph is a knowledge graph waiting to be used. But without integration into the query planner and agent workflows, it's just metadata sitting in JSONL files.

This RDR defines what to build on top of the catalog (RDR-049) to turn it into a navigable knowledge engine: link creation workflows, query planner integration, and the disciplined deferral of things that sound good but aren't yet proven useful.

## Context

### Dependencies

**RDR-049 (Git-Backed Catalog)** must be implemented first. This RDR assumes:
- Tumbler addressing: `store.owner.document[.chunk]`
- Typed bidirectional links with optional span granularity
- Catalog API: `register`, `resolve`, `find`, `link`, `links_from`, `links_to`, `graph`
- MCP tools: `catalog_search`, `catalog_show`, `catalog_link`, `catalog_links`, `catalog_resolve`
- Git-backed JSONL persistence with SQLite query cache

### Existing Query Planning (RDR-042)

| Component | What it does | Limitation |
|---|---|---|
| Query planner agent | Decomposes NL → step-by-step JSON plan | No catalog awareness; can only plan `search` steps |
| Analytical operator agent | Executes extract/summarize/rank/compare/generate | Operates on retrieved chunks only; no graph navigation |
| `/nx:query` skill | Orchestrates plan execution via T1 scratch | Sequential only; no catalog resolution |
| Plan library (T2) | FTS5 search over saved plans | No link back to catalog entries |
| Search MCP tools | Semantic chunk/document retrieval | Corpus resolution via prefix matching only |

### Nelson's Guidance (Literary Machines)

**Link search is "free"**: *"Because of our unusual algorithms, link-search is deemed to be 'free'... THE QUANTITY OF LINKS NOT SATISFYING A REQUEST DOES NOT IN PRINCIPLE IMPEDE SEARCH ON OTHERS."* (Ch. 4, p. 4/60)

**The junk link problem**: *"Filtering out junk links in a universe full of them is a vital aspect of system performance. Control of incoming links by their origin is a key to eliminating garbage."* (Ch. 4, p. 4/60)

**Link types reduce noise**: *"Typed links allow the user to reduce the context of what is shown."* (Ch. 3, p. 3/12)

**Categories are user business**: *"Keep categorizing directories out of the system level."* (Ch. 2, p. 2/49) — concept nodes are fine as user/agent-created links, not system-imposed taxonomy.

**Design it in from the start**: *"As soon as you start adding features like networking and link types and historical backtrack and framing, it becomes a complex morass, and what you really need is a system designed from the start to have all these features."* (Ch. 3, p. 3/15)

## Proposed Solution

Three layers, built in order of proven utility.

### Layer 1: Link Creation Workflows (Build First)

The catalog is only as good as the links in it. Layer 1 populates the link graph through concrete, well-understood mechanisms.

#### 1a. Citation Auto-Generation from bib_enricher

`nx enrich` already queries Semantic Scholar and attaches `bib_semantic_scholar_id`, `bib_year`, `bib_citation_count`, `bib_authors` to chunk metadata. Extend it:

- After enriching a document, query Semantic Scholar for its `references` field
- For each reference that matches a document already in the catalog (by title or Semantic Scholar ID), auto-create a `cites` link
- Mark auto-generated links with `created_by: "bib_enricher"` in metadata

This is the lowest-hanging fruit — structured citation data already exists in an API, and the catalog already has document entries. Connecting them is mechanical.

#### 1b. Manual Link Creation During Work

As you work, create links:
```
nx catalog link 1.1.44 1.2.1 --type cites --meta '{"note":"RDR-049 inspired by Xanadu"}'
nx catalog link 1.1.45 1.1.44 --type implements --meta '{"note":"catalog.py implements RDR-049"}'
```

Agent-created links during research sessions:
```
catalog_link(from="1.2.5", to="1.2.8", type="cites", meta='{"note":"Fagin 2005 builds on data exchange"}')
```

#### 1c. Post-Index Code ↔ RDR Linking

When `nx index repo` registers files in the catalog, check RDR documents for bead references or file mentions. If RDR-049 mentions `catalog.py`, auto-create an `implements` link from the code file to the RDR. Heuristic, not perfect — but bootstraps the code-to-design provenance graph.

#### 1d. `created_by` on All Links — Junk Filtering from Day One

Every link carries `created_by` in metadata:
- `"user"` — manual link creation
- `"bib_enricher"` — citation auto-generation
- `"index_hook"` — post-index code ↔ RDR linking
- Agent name — links created by agents during research

Nelson says: "Control of incoming links by their origin is a key to eliminating garbage." Filter by `created_by` when the link graph gets noisy.

### Layer 2: Catalog-Aware Query Planning (Build Second)

Extend the query planner's vocabulary with three new operations. Each operation maps 1:1 to an existing MCP tool.

#### 2a. New Operations

| Plan Operation | MCP Tool | Input | Output | Purpose |
|---|---|---|---|---|
| `catalog_search` | `catalog_search` | Query string + structured filters | List of catalog entry dicts (each has `tumbler`, `physical_collection`, etc.) | Find documents by metadata, not content |
| `catalog_links` | `catalog_links` | Tumbler + direction + type + depth | List of link dicts (`from`, `to`, `type`) | Navigate relationships via graph traversal |
| `catalog_resolve` | `catalog_resolve` | Owner or corpus string | List of collection name strings | Map catalog namespace to T3 collection names |

**Design principle**: Plan operation names match MCP tool names exactly — no translation layer in the skill dispatch. The `/nx:query` skill dispatches catalog operations by calling the MCP tools directly (same pattern as `search` steps).

#### 2b. Operation Schemas

##### catalog_search
Find catalog entries by metadata. Supports both FTS5 free-text and structured SQL filters.

**Fields**: `query` (FTS5 text), `author`, `corpus`, `owner`, `file_path`, `content_type` — all optional but at least one required. Structured filters (`author`, `corpus`, `owner`, `file_path`) are exact SQL matches; `query` is FTS5 free-text over title/author/corpus/file_path.

**Output**: List of catalog entry dicts. Each dict includes `tumbler`, `title`, `author`, `year`, `content_type`, `file_path`, `corpus`, `physical_collection`, `chunk_count`.

**Examples**:
```json
{"step": 1, "operation": "catalog_search", "params": {"author": "Fagin", "corpus": "schema-evolution"}}
{"step": 1, "operation": "catalog_search", "params": {"query": "Inverting Schema Mappings"}}
{"step": 1, "operation": "catalog_search", "params": {"file_path": "src/nexus/chunker.py", "owner": "1.1"}}
```

##### catalog_links
Navigate the link graph from a tumbler. Maps directly to `catalog_links` MCP tool which calls `Catalog.graph()`.

**Required params**: `tumbler` — starting point. **Optional params**: `direction` (`in`/`out`/`both`, default `both`), `link_type` (link type filter), `depth` (default 1).

**Fanout rule**: When `inputs` references a prior step that returned a list (e.g., `catalog_search` results), the skill extracts the **first entry's tumbler** and uses it. If the planner needs to traverse from multiple starting points, it must use separate `catalog_links` steps with explicit `tumbler` params.

**Output**: List of link dicts (`from`, `to`, `type`, `from_span`, `to_span`, `created_by`).

**Examples**:
```json
{"step": 2, "operation": "catalog_links", "inputs": "$step_1", "params": {"direction": "in", "link_type": "cites", "depth": 2}}
{"step": 2, "operation": "catalog_links", "params": {"tumbler": "1.2.5", "direction": "out", "link_type": "implements"}}
```

##### catalog_resolve
Map a catalog namespace (owner tumbler or corpus name) to physical T3 collection names. Used before `search` steps to scope the search corpus.

**Params**: `owner` (tumbler prefix) or `corpus` (corpus tag) — at least one required.

**Output**: List of collection name strings (e.g., `["docs__schema-evolution", "docs__data-exchange"]`).

**Note**: Often unnecessary — `catalog_search` results already include `physical_collection`. Use `catalog_resolve` when you need all collections for an owner/corpus without knowing specific documents. The skill can also extract `physical_collection` values directly from a prior `catalog_search` step's output and pass them as the `corpus` for the next `search` step.

**Example**:
```json
{"step": 2, "operation": "catalog_resolve", "params": {"corpus": "schema-evolution"}}
```

#### 2c. Plan Patterns

**Narrow-then-search** (most common): Use `catalog_search` to find entries by metadata, then `search` their collections.
```json
{"steps": [
  {"step": 1, "operation": "catalog_search", "params": {"author": "Fagin", "corpus": "schema-evolution"}},
  {"step": 2, "operation": "search", "search_query": "chase procedure optimization", "corpus": "$step_1.collections"},
  {"step": 3, "operation": "summarize", "inputs": "$step_2", "params": {"mode": "evidence"}}
]}
```
The skill extracts distinct `physical_collection` values from step 1's results and passes them as the corpus for step 2.

**Citation traversal**: Find a paper, follow its citation graph, then search the cited works.
```json
{"steps": [
  {"step": 1, "operation": "catalog_search", "params": {"query": "Inverting Schema Mappings"}},
  {"step": 2, "operation": "catalog_links", "inputs": "$step_1", "params": {"direction": "in", "link_type": "cites", "depth": 2}},
  {"step": 3, "operation": "search", "search_query": "novel contribution", "corpus": "$step_2.collections"},
  {"step": 4, "operation": "summarize", "inputs": "$step_3", "params": {"mode": "short"}}
]}
```
The skill extracts `physical_collection` from the link targets (`to` tumblers resolved via catalog).

**Cross-type provenance**: Follow code → RDR → paper chain.
```json
{"steps": [
  {"step": 1, "operation": "catalog_search", "params": {"file_path": "src/nexus/chunker.py", "owner": "1.1"}},
  {"step": 2, "operation": "catalog_links", "inputs": "$step_1", "params": {"direction": "out", "link_type": "implements"}},
  {"step": 3, "operation": "catalog_links", "inputs": "$step_2", "params": {"direction": "out", "link_type": "cites"}},
  {"step": 4, "operation": "extract", "inputs": "$step_3", "params": {"template": {"title": "", "key_contribution": ""}}}
]}
```

**Corpus-scoped search**: Resolve an entire corpus without knowing specific documents.
```json
{"steps": [
  {"step": 1, "operation": "catalog_resolve", "params": {"corpus": "distributed-systems"}},
  {"step": 2, "operation": "search", "search_query": "consensus protocol Byzantine fault", "corpus": "$step_1.collections"},
  {"step": 3, "operation": "rank", "inputs": "$step_2", "params": {"criterion": "relevance to practical BFT implementations"}}
]}
```

#### 2d. Skill Dispatch Rules

The `/nx:query` skill handles catalog operations as follows:

1. **`catalog_search`**: Call `catalog_search` MCP tool with params. Store result list in T1 scratch. Extract distinct `physical_collection` values into `$step_N.collections` for downstream `search` steps.
2. **`catalog_links`**: If `inputs` references a prior step, extract first entry's `tumbler`. Call `catalog_links` MCP tool. Resolve link target tumblers to `physical_collection` via `catalog_show`. Store collections in `$step_N.collections`.
3. **`catalog_resolve`**: Call `catalog_resolve` MCP tool. Store collection name list directly — already in the right format for `corpus` parameter of `search` steps.
4. **`$step_N.collections`**: When a `search` step's `corpus` is `$step_N.collections`, the skill substitutes the comma-separated collection names extracted from step N.

#### 2e. Few-Shot Plan Library

Seed the T2 plan library with catalog-aware plan templates tagged `catalog`. The query planner's existing `few_shot_plans` mechanism surfaces these when the question involves relationship navigation, author filtering, or citation traversal.

Tag convention: all catalog-aware plans saved with `tags="catalog,<operation_types>"` (e.g., `tags="catalog,catalog_search,search"`). The planner's few-shot lookup includes `catalog`-tagged plans when the question mentions authors, citations, relationships, provenance, or specific documents.

#### 2f. Files to Update

- `nx/agents/query-planner.md` — add `catalog_search`, `catalog_links`, `catalog_resolve` to operation list + schemas
- `nx/skills/query/SKILL.md` — add dispatch rules for catalog operations
- `src/nexus/mcp_server.py` — `catalog_search` already accepts structured filters (added in this iteration)

### Layer 3: Concept Nodes (Build When Needed, Not Before)

Ghost elements with `content_type="concept"` — addressable nodes with no content, linked to documents via `about` links.

```
catalog_register(title="chase procedure", owner="concepts", content_type="concept")
catalog_link(from="1.2.5", to="1.99.1", type="about")
```

Over time, a concept graph emerges: papers linked to concepts, concepts linked to each other.

**When to build**: Only after Layer 1 and 2 are in use and you find yourself wanting "show me everything about X" queries that can't be answered by FTS5 search over titles. Not before.

**How it stays faithful to Nelson**: Concept nodes are user/agent-created links, not system-imposed taxonomy. Anyone can create concept nodes. They're not authoritative — they're one person's organizational view. Nelson: "Let them handle it and collect royalties."

## Explicitly Deferred

These ideas sound good but lack a proven use case. Do not build them until demonstrated need.

### Computed Similarity Index
Compute pairwise document similarity from T3 embeddings, store as `similar` links. **Problem**: Expensive (O(n^2) documents), noisy (what threshold?), and semantic search already answers "what's similar to X?" without a secondary index. Build only if you find yourself needing "similar documents that share no citation links."

### Authority Scoring
Combine citation_count + inbound link count + frecency into a per-document authority score. **Problem**: Thin signal at current corpus size. Semantic Scholar citation_count alone is probably sufficient for ordering. Build only if you find the query planner making bad routing decisions because it treats all documents equally.

### Materialized View Documents
Nelson's "intercomparison documents" — documents that exist solely to express relationships. **Problem**: Premature abstraction. The link graph is the intercomparison. A saved query plan in the T2 library is a materialized view of a specific question. Build only if you find yourself recreating the same link traversal pattern repeatedly.

### Home-Set Filtering
Nelson's FEBE protocol uses a 4-dimensional search: home-set (where links live), from, to, type. Our links are free-floating rows — no home document. **Problem**: Home-set matters when you have multiple competing organizational views of the same documents. Build only if concept nodes from different agents/sessions conflict and need disambiguation.

### Links to Links
In Xanadu, links are addressable and can themselves be linked. **Problem**: No use case yet. Our links are rows in a table; making them addressable requires giving them tumblers. Build only if you need meta-commentary on relationships ("this citation link is disputed").

## Alternatives Considered

### Full AgenticScholar taxonomy (rejected)
RDR-042 already rejected this: 4-stage LLM taxonomy construction is expensive, tuned for homogeneous scholarly corpora, doesn't generalize to mixed content. Concept nodes via ghost elements are the lightweight alternative.

### Graph database (Neo4j, etc.) for link storage (rejected)
Adds infrastructure. SQLite with indexed `links` table handles thousands of links trivially. Reconsider only at 100K+ links.

### Embedding link types (rejected)
Embed link type descriptions and do semantic matching for "find related" queries. Over-engineering — explicit type filtering is sufficient and predictable.

## Success Criteria

### Layer 1 (Link Creation) — COMPLETE via RDR-049
- [x] `nx enrich` auto-creates `cites` links from Semantic Scholar references — `_catalog_enrich_hook` in `commands/enrich.py`
- [x] All auto-generated links have `created_by` in metadata — RF-8 enforced: `bib_enricher`, `index_hook`
- [x] Manual `nx catalog link` works end-to-end — CLI + MCP `catalog_link` tool
- [x] Post-index hook creates `implements` links between code files and RDRs — `generate_code_rdr_links()` in `link_generator.py`
- [x] Citation auto-generation from `bib_semantic_scholar_id` cross-matching — `generate_citation_links()` in `link_generator.py`
- [ ] Link count grows meaningfully after enriching existing paper collections — requires running `nx catalog backfill` then `nx catalog generate-links` on production data

### Layer 2 (Query Planning)
- [x] Query planner generates valid plans with `catalog_search`, `catalog_links`, `catalog_resolve` steps
- [x] `nx/agents/query-planner.md` updated with the three new operation schemas
- [x] `/nx:query` skill dispatches catalog operations correctly (dispatch rules §2d)
- [x] `$step_N.collections` extraction works: catalog results → collection names → search corpus — validated against 109-document catalog
- [x] On 5 reference queries with known relevant documents, catalog-scoped search retrieves a relevant document in fewer MCP calls than unconstrained search — "Schema Mappings" narrows 83→9 collections
- [x] Few-shot plan templates seeded in T2 plan library with `catalog` tag (IDs 18-21)
- [x] T1 scratch correctly passes catalog results between steps

### Layer 3 (Concept Nodes) — future, criteria TBD
- [ ] Ghost element concept nodes creatable and linkable
- [ ] `catalog_links(concept, direction="in")` returns all documents about that concept
- [ ] Concept-to-concept links navigable

## Open Questions

1. ~~**Citation matching precision**~~: **RESOLVED** — RDR-049 uses `bib_semantic_scholar_id` exact cross-matching (audit F2), not fuzzy title matching. No false positives from matching strategy. FTS title search used only for PDF dedup (approximate, acceptable).
2. ~~**Code ↔ RDR linking heuristic**~~: **RESOLVED** — `generate_code_rdr_links()` matches module names (>3 chars) against RDR titles. `created_by="index_hook"` enables filtering false positives. Accepted tradeoff per RF-8.
3. ~~**Plan library integration**~~: **RESOLVED** — Catalog-aware plans tagged with `catalog` plus operation-type tags (e.g., `tags="catalog,catalog_search,search"`). Planner's few-shot lookup filters for `catalog`-tagged plans when question involves relationships, authors, citations, or provenance. No change to planner internal logic needed — tag-based routing is sufficient.
4. ~~**Link type extensibility**~~: **RESOLVED** — Fixed set (`cites`, `supersedes`, `quotes`, `relates`, `comments`, `implements`) enforced by CLI `click.Choice`. `LinkRecord` accepts arbitrary strings for programmatic use. Extension requires CLI update only.
5. ~~**Agent link discipline**~~: **RESOLVED** — Trust agent, filter later. `created_by` field is mandatory (no default on `Catalog.link()`). Nelson's junk filtering principle applied: origin-based filtering, not approval gates.

## Implementation Plan

**RDR-049 dependency: SATISFIED** (closed 2026-04-05, PR Hellblazer/nexus#126).

### Already Implemented (via RDR-049 Phases 3-4)
1. ~~**Citation auto-generation**~~: `generate_citation_links()` — SS ID cross-matching
2. ~~**`created_by` metadata**~~: Mandatory on all link creation paths (no default)
3. ~~**Post-index linking**~~: `generate_code_rdr_links()` heuristic + indexer/PDF/store/enrich hooks
4. ~~**Manual link creation**~~: `nx catalog link` CLI + `catalog_link` MCP tool

### Remaining (Layer 2 — Query Planner Integration)
5. **Query planner agent update**: Add `catalog_search`, `catalog_links`, `catalog_resolve` to `nx/agents/query-planner.md` operation list with schemas (§2b)
6. **Skill dispatch update**: `/nx:query` handles catalog operations per dispatch rules (§2d) — collection extraction, fanout, T1 scratch
7. **Few-shot templates**: Seed T2 plan library with narrow-then-search, citation traversal, cross-type provenance, corpus-scoped patterns (§2c) tagged `catalog`

### Future (Layer 3 — When Needed)
8. **Concept nodes**: Ghost elements with `content_type="concept"` + `about` links — build when demonstrated need arises

## Research Findings

### RF-1: Nelson on Link Search Cost (2026-04-04)
**Classification**: Verified — Literary Machines Ch. 4 | **Confidence**: HIGH

Link search designed to be "free" — sublinear scaling via enfilade algorithms. Quantity of non-matching links doesn't impede search on others. At nexus scale (thousands of links, not billions), SQLite indexed queries are more than sufficient.

### RF-2: Nelson on Junk Links (2026-04-04)
**Classification**: Verified — Literary Machines Ch. 4 | **Confidence**: HIGH

"Control of incoming links by their origin is a key to eliminating garbage." Link types and origin filtering are the design answers. We need `created_by` on every link from day one.

### RF-3: Nelson on Categories vs. Links (2026-04-04)
**Classification**: Verified — Literary Machines Ch. 2 | **Confidence**: HIGH

"Keep categorizing directories out of the system level." Concept nodes are valid as user/agent-created links (additive, optional). They would violate Nelson if made into a mandatory system-level taxonomy.

### RF-4: Nelson on Designing In Features (2026-04-04)
**Classification**: Verified — Literary Machines Ch. 3 | **Confidence**: HIGH

"Trying to add such things later is very different from designing them in at the start." The catalog (RDR-049) IS the "design it in" moment. This RDR (knowledge graph on top) is additive — it uses the catalog's designed-in link primitives, it doesn't require retrofitting.

### RF-5: Query Planner Capabilities Audit (2026-04-04)
**Classification**: Verified — Codebase Analysis | **Confidence**: HIGH

Query planner supports 6 operations (search, extract, summarize, rank, compare, generate). Plans are sequential JSON, dispatched by `/nx:query` skill via T1 scratch. Plan library is T2 FTS5. Adding 3 catalog operations (catalog_search, catalog_links, catalog_resolve) is additive — same relay format, same T1 scratch bus, same plan JSON structure.

### RF-6: FEBE Search Protocol (2026-04-04)
**Classification**: Verified — Literary Machines Ch. 4 + Java implementation | **Confidence**: HIGH

Nelson's most powerful command is `FINDLINKSFROMTOTHREE(home-set, from-set, to-set, type-set)` — 4-dimensional link filter. Our `catalog_links(tumbler, direction, type)` is 3-dimensional (no home-set). Sufficient for current needs; home-set filtering deferred to when intercomparison documents create competing organizational views.

### RF-7: Scenario Validation — Layer Ordering (2026-04-04)
**Classification**: Verified — Design Analysis against current nexus capabilities | **Confidence**: HIGH

Seven scenarios were evaluated against current nexus capabilities (no catalog) vs. catalog + knowledge graph. Each scenario was assessed for which layers it requires and whether the value is immediate or speculative.

**Layer 1 (link creation) enables immediately:**
- Citation-aware navigation: follow `cites` links from known papers to foundational papers — pure graph traversal, no vector search needed for the navigation step
- Code ↔ research traceability: `code → implements → RDR → cites → paper` — 3-hop provenance chain currently impossible
- Cross-type reasoning: "what code is grounded in published research?" — link traversal across content types

**Layer 2 (query planner) enables:**
- Narrow-then-search: `catalog_search(author="Fagin")` → `catalog_resolve()` → targeted `search()` over specific collections instead of all 95 `docs__` — dramatic precision improvement
- Temporal knowledge evolution: catalog metadata (year) + citation links → chronological traversal of a field's development
- Comparative plans: planner routes to separate author-filtered corpora, then compares — currently requires blind search across everything

**Layer 3 (concept nodes) enables (when needed):**
- Emergent taxonomy: agents create `about` links from papers to concept ghost elements — AgenticScholar's taxonomy but incremental, not a 4-stage LLM pipeline
- Concept graph: "chase procedure" ←about← [5 papers, 2 code files] — discoverable via link traversal

**Explicitly deferred (no scenario demonstrated need):**
- Computed similarity index: semantic search already answers "what's similar?" — secondary index adds cost without proven benefit
- Authority scoring: Semantic Scholar citation_count is sufficient at current corpus size
- Materialized views: the link graph IS the intercomparison — saved query plans in T2 cover the reuse case

This scenario analysis directly shaped the three-layer ordering: Layer 1 has immediate, concrete value from day one. Layer 2 makes the query planner smarter. Layer 3 waits for demonstrated need.

### RF-8: AgenticScholar Taxonomy vs. Emergent Concept Nodes (2026-04-04)
**Classification**: Verified — Cross-referencing RDR-042 findings with Nelson's principles | **Confidence**: HIGH

RDR-042 rejected AgenticScholar's 4-stage LLM taxonomy as "expensive, tuned for homogeneous scholarly corpora, doesn't generalize to Nexus's mixed-corpus model." The catalog's concept nodes (ghost elements with `about` links) achieve the same organizational result through a different mechanism:

| Aspect | AgenticScholar taxonomy | Catalog concept nodes |
|---|---|---|
| Construction | 4-stage LLM pipeline (extract → cluster → reference → construct) | Incremental: agents create `about` links as they work |
| Cost | High (LLM calls for every document) | Near-zero (link creation is a JSONL append) |
| Maintainability | Static snapshot; must rerun on new documents | Grows naturally as new documents are linked |
| Authoritativeness | System-level taxonomy | User/agent-level views — Nelson's "categories are user business" |
| Generalization | Tuned for scholarly corpora | Works across code, prose, RDRs, papers, knowledge |

The concept node approach is faithful to Nelson ("keep categorizing directories out of the system level") while achieving the organizational benefits AgenticScholar's taxonomy provides. The tradeoff: no single authoritative taxonomy, but that's the point — Nelson argues authoritative taxonomies rot.

### RF-9: RDR-049 Implementation Retrospective — Layer 1 Complete (2026-04-05)
**Classification**: Verified — Implementation Analysis | **Confidence**: HIGH

RDR-049 was fully implemented on 2026-04-05 (18 beads, 5 phases, ~180 tests, PR #126). The implementation resolved Layer 1 of this RDR entirely and provides the concrete API surface for Layer 2.

**What shipped that directly enables RDR-050:**

| Capability | Module | RDR-050 Layer |
|---|---|---|
| Tumbler addressing + resolve | `catalog/tumbler.py`, `catalog.py` | Foundation for all layers |
| Typed bidirectional links | `catalog.py` link/unlink/links_from/links_to | Layer 1 |
| BFS graph traversal (depth, direction, type filter) | `catalog.py` graph() | Layer 1 + 2 |
| Citation auto-generation (SS ID cross-match) | `catalog/link_generator.py` | Layer 1a |
| Code ↔ RDR heuristic linking | `catalog/link_generator.py` | Layer 1c |
| `created_by` on all links (RF-8) | All link creation paths | Layer 1d |
| FTS5 search over catalog metadata | `catalog_db.py` + `catalog_search` MCP | Layer 2 prerequisite |
| `catalog_resolve` MCP tool | `mcp_server.py` | Layer 2a |
| Collection consolidation | `catalog/consolidation.py` | Corpus management |

**Open questions resolved by implementation:**
- Q1 (citation matching): Uses `bib_semantic_scholar_id` exact match, not fuzzy title — zero false positives from matching
- Q2 (code-RDR heuristic): Module name >3 chars in RDR title, `created_by="index_hook"` for filtering
- Q4 (link types): Fixed set in CLI, arbitrary in API — extensible without schema change
- Q5 (agent discipline): `created_by` mandatory, no approval gate — filter junk later per Nelson

**What remains for Layer 2:**
The 8 MCP catalog tools (`catalog_search`, `catalog_show`, `catalog_list`, `catalog_register`, `catalog_update`, `catalog_link`, `catalog_links`, `catalog_resolve`) are the dispatch targets. The query planner needs 3 new plan step types that call these tools. The `/nx:query` skill's operator relay pattern already supports adding new operations — the catalog operations follow the same `T1 scratch → dispatch → harvest` pattern as existing `search`/`extract`/`summarize` steps.

**Key architectural insight:** The catalog's `graph()` method with `depth`, `direction`, and `link_type` parameters maps directly to the `catalog_links` plan operation. No adapter needed — the MCP `catalog_links` tool already exposes this.

### RF-10: Non-Document Knowledge — Facts and Insights in the Catalog (2026-04-05)
**Classification**: Design Analysis | **Confidence**: HIGH

T3 `knowledge__*` collections store working knowledge (facts, insights, decisions, observations) via `nx store put` — not documents with authors and publication years. The catalog already registers these as entries under a "knowledge" curator owner (via `_catalog_store_hook`), with `content_type="knowledge"` and `meta.doc_id` for dedup. But the catalog's document-centric metadata (author, year, file_path) is a poor fit for atomic facts.

**What already works:**
- `_catalog_store_hook` registers every `nx store put` with `meta.doc_id` — facts are already in the catalog
- Ghost elements (`physical_collection=""`, `chunk_count=0`) can represent facts with no T3 backing
- `meta` dict is arbitrary — can carry `kind`, `source_agent`, `confidence`, `evidence_refs`
- Links work: `fact → derived_from → paper`, `fact → supports → decision`

**What's needed for seamless integration:**
- Use `meta.kind` to subdivide knowledge entries: `fact`, `insight`, `decision`, `observation` — avoids proliferating `content_type` values while maintaining queryability via `json_extract(metadata, '$.kind')`
- New link type `derived_from` for provenance: when an agent produces an insight during `/nx:query`, it links the catalog entry to the source documents that produced it
- The query planner can then navigate: `fact → derived_from → paper → cites → foundational_paper` — a provenance chain from working knowledge back to primary sources

**Why `meta.kind` not `content_type`:**
- `content_type` drives physical routing (code → `code__`, prose → `docs__`, etc.) and is a first-class SQL column
- `meta.kind` is organizational metadata — Nelson's "categories are user business" principle (RF-3)
- Adding `content_type="fact"` would require updating every hook, CLI filter, and MCP tool that switches on content_type
- `meta.kind` is queryable via `json_extract` (already used by `by_doc_id`) with zero schema changes

**Integration with Layer 3 (concept nodes):**
Concept nodes are ghost elements representing abstract topics. Facts are ghost elements representing concrete claims. Both are addressable via tumblers, both carry links. The difference is link semantics: concepts use `about` links (documents are about a concept), facts use `derived_from` and `supports` links (facts are derived from documents, facts support decisions). Same mechanism, different vocabulary — no new infrastructure needed.

**No schema changes required.** The existing catalog API handles this today:
```python
# Agent discovers a fact during research
fact = cat.register(knowledge_owner, "Chase procedure terminates in polynomial time for full TGDs",
                    content_type="knowledge", meta={"kind": "fact", "confidence": "high"})
cat.link(fact, paper_tumbler, "derived_from", created_by="query-agent")
```
