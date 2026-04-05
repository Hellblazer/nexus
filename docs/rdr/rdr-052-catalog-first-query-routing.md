---
title: "Catalog-First Query Routing — Push Planning into MCP"
id: RDR-052
type: Architecture
status: draft
priority: P1
author: Hal Hildebrand
reviewed-by: ""
created: 2026-04-05
related_issues:
  - "RDR-042 - AgenticScholar Enhancements (closed)"
  - "RDR-049 - Git-Backed Xanadu-Inspired Catalog for T3 (closed)"
  - "RDR-050 - Knowledge Graph and Catalog-Aware Query Planning (closed)"
  - "RDR-051 - Link Lifecycle (closed)"
---

# RDR-052: Catalog-First Query Routing — Push Planning into MCP

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

The `/nx:query` skill dispatches an LLM-powered query-planner agent to decompose every analytical question into a step-by-step JSON plan. This made sense before the catalog existed — the only way to route a query was to ask an LLM to decide which collections to search. Now the catalog provides deterministic routing via metadata (author, content_type, corpus, file_path) and link traversal (cites, implements, relates). The planner is computing answers the catalog already knows.

Current cost of a simple scoped query ("papers by Fagin on schema mappings"):
1. Skill fires
2. `plan_search` MCP call (T2 cache lookup)
3. Dispatch query-planner agent (LLM call, ~5s)
4. Parse JSON plan
5. `catalog_search` MCP call → extract collections (done in skill markdown)
6. `search` MCP call → write to T1 scratch
7. Dispatch analytical-operator agent for summarize (LLM call, ~10s)
8. Read T1 scratch, present results
9. "Save this plan?" prompt (user friction)

Steps 3, 5, and 9 are unnecessary. The catalog routing is deterministic. The collection extraction is mechanical. The save prompt is friction — plans should auto-cache.

Additionally, 15 lines of the skill's markdown instructions describe how to extract `physical_collection` values from catalog results and pass them to search steps. This is application logic encoded as natural language instructions — fragile, token-expensive, and error-prone (the shakeout proved the planner ignores catalog-first routing without strong few-shot examples).

## Context

### Dependencies

- **RDR-049** (closed): Catalog with tumbler addressing, JSONL+SQLite, typed links
- **RDR-050** (closed): Query planner integration — `catalog_search`, `catalog_links`, `catalog_resolve` operations
- **RDR-051** (closed): `link_query`, `link_if_absent`, composable filtering

### What the Catalog Already Provides

| Need | Before catalog | After catalog |
|------|---------------|---------------|
| Search by author | LLM plans `catalog_search` step | `catalog_search(author="Fagin")` — deterministic |
| Scope to content type | LLM guesses which corpus prefix | `catalog_search(content_type="rdr")` — deterministic |
| Follow citations | Not possible | `catalog_links(tumbler, link_type="cites")` — deterministic |
| Code → design provenance | Not possible | Link chain: `implements` → `cites` — deterministic |
| Collection resolution | LLM decides corpus string | `catalog_resolve(owner="1.1")` — deterministic |

All of these are currently mediated by an LLM agent generating JSON. They should be deterministic MCP code.

### Nelson's Guidance

*"Link search is deemed to be 'free'... THE QUANTITY OF LINKS NOT SATISFYING A REQUEST DOES NOT IN PRINCIPLE IMPEDE SEARCH ON OTHERS."* (Literary Machines Ch. 4)

Link traversal is a lookup, not a planning problem. Routing through the link graph should be as cheap as a function call, not an LLM inference.

## Proposed Solution

Three changes: enhance the `query` MCP tool, pre-build plan templates, and simplify the skill.

### Component 1: Enhanced `query` MCP Tool

Add catalog routing parameters to the existing `query` tool. The tool handles scoping internally — one call replaces three.

```python
def query(
    question: str,
    corpus: str = "knowledge,code,docs",
    where: str = "",
    limit: int = 10,
    # New: catalog routing params
    author: str = "",
    content_type: str = "",
    follow_links: str = "",  # link type: "cites", "implements", etc.
    depth: int = 1,
) -> str:
```

**Routing logic** (deterministic, no LLM):

1. If `author` or `content_type` provided → `catalog_search` first → extract `physical_collection` values → scoped vector search
2. If `follow_links` provided → for each result, `catalog_links(link_type=follow_links, depth=depth)` → include linked documents in results
3. If no catalog params → broad vector search (current behavior, unchanged)

**Collection extraction** moves from skill markdown into Python:
```python
collections = {e.physical_collection for e in catalog_entries if e.physical_collection}
if collections:
    corpus = ",".join(collections)
```

This is 3 lines of Python replacing 15 lines of natural language instructions.

### Component 2: Pre-Built Plan Templates

Seed the T2 plan library with deterministic templates for common query patterns. These are matched by the skill before falling back to the query-planner agent.

| Template | Pattern | Steps |
|----------|---------|-------|
| `author-search` | Question mentions author name | `catalog_search(author=$AUTHOR)` → `search(scoped)` |
| `citation-chain` | "what cites", "cited by", "references" | `catalog_search($QUERY)` → `catalog_links(type=cites)` → `search(scoped)` |
| `provenance-chain` | "what implements", "design for", "RDR for" | `catalog_search(file_path=$PATH)` → `catalog_links(type=implements)` → `catalog_links(type=cites)` |
| `cross-corpus-compare` | "compare X in A vs B" | `search(corpus=A)` + `search(corpus=B)` → `compare` |
| `type-scoped-search` | "RDR about", "papers on", "code for" | `catalog_search(content_type=$TYPE)` → `search(scoped)` |

Templates are seeded at `nx catalog setup` time. They have no TTL — they're structural, not cached results.

### Component 3: Simplified `/nx:query` Skill

The skill becomes a thin dispatcher with three paths:

```
Path 1 — Single-tool (80% of queries):
  Question has catalog handles (author, type, links) →
  Call enhanced `query` MCP directly → present results → done

Path 2 — Template match (15% of queries):
  Question matches a pre-built template →
  Execute template steps deterministically (no LLM) → present results → done

Path 3 — Novel analytical pipeline (5% of queries):
  Question requires extract/compare/generate →
  Dispatch query-planner agent → execute plan → auto-save on success
```

**Removed**: "Save this plan?" prompt. Successful novel plans auto-save to T2 with `outcome="success"` and TTL 30 days. The plan library is a cache, not a curated collection.

**Removed**: Manual collection extraction logic (15 lines of markdown instructions). Pushed to MCP.

**Removed**: T1 scratch round-trips for single-step queries. The enhanced `query` tool returns results directly.

### Component 4: Auto-Cache for Novel Plans

When the query-planner agent generates a novel plan and execution succeeds:

```python
plan_save(
    query=original_question,
    plan_json=plan,
    outcome="success",
    tags=",".join(operation_types),
    ttl=30,  # days — auto-expire stale plans
)
```

No user prompt. No confirmation. Plans are cheap to store and expire naturally.

On next similar query, `plan_search` finds the cached plan → skill skips the planner agent → executes cached plan directly. The planner is only called once per novel pattern.

## Explicitly Deferred

### Semantic Plan Matching
Match cached plans by embedding similarity rather than FTS5 keyword matching. FTS5 is sufficient for the plan library's scale (tens to low hundreds of plans). Build only if FTS5 matching produces poor recall.

### Concept Nodes (Layer 3)
Ghost elements with `content_type="concept"` linked via `about` links. The enhanced `query` tool handles topic matching via vector search. Build concept nodes only when users need to *browse* topics, not *search* them.

### Plan Optimization
Analyze cached plans for redundant steps, reorder for efficiency. Premature — the plan library is small and plans are short (2-4 steps).

## Alternatives Considered

### Keep the query-planner agent as the default path (rejected)
The planner works, but it's expensive (~5s LLM call) for queries the catalog can route deterministically. The catalog made 80% of the planner's work redundant.

### Remove the query-planner agent entirely (rejected)
Novel analytical pipelines (extract → compare → generate) genuinely need LLM decomposition. The planner stays as the exception path.

### Build a rule engine for plan selection (rejected)
Over-engineering. The skill can pattern-match on keyword signals (author, cites, implements, compare) without a formal rule engine. Pre-built templates cover the common patterns.

### Push everything into a single MCP tool (rejected)
Multi-step analytical pipelines need inter-step state (extract results feed into compare). A single tool can't orchestrate LLM agent dispatches. The skill remains the orchestrator for multi-step flows.

## Success Criteria

- [ ] `query(question="schema mappings", author="Fagin")` returns scoped results in one MCP call
- [ ] `query(question="...", follow_links="cites")` enriches results with cited documents
- [ ] Pre-built templates seeded at `nx catalog setup`
- [ ] `/nx:query` routes simple questions to enhanced `query` MCP (no agent dispatch)
- [ ] `/nx:query` matches template patterns before falling back to planner
- [ ] Novel plans auto-saved on success (no user prompt)
- [ ] Cached plans auto-expire after 30 days
- [ ] Query planner agent dispatched only for multi-step analytical pipelines
- [ ] End-to-end latency for scoped search: <2s (vs current ~15s with planner)

## Open Questions

1. **Template matching precision**: Should the skill use keyword signals ("author", "cites") or ask the LLM to classify the question type? Keyword signals are faster but may miss edge cases.
2. **Auto-cache scope**: Should ALL successful query executions auto-cache, or only planner-generated plans? Auto-caching single-tool queries is wasteful (they're already fast). Proposal: only cache plans with 2+ steps.
3. **Follow-links result format**: Should `follow_links` results be interleaved with search results or returned in a separate section? Interleaving risks confusing relevance ranking.
4. **Dynamic link types**: Link types are an open set — the API accepts arbitrary strings, and agents may create types beyond the current 7 (`cites`, `implements-heuristic`, `supersedes`, `quotes`, `relates`, `comments`, `implements`). The enhanced `query` tool's `follow_links` param must accept any string, not a fixed enum. Pre-built templates reference specific types (`cites`, `implements`) — should there be a generic "follow any link" template, or should the planner handle novel link types? Concept nodes (deferred Layer 3) would introduce `about` links; `derived_from` is proposed in RDR-050 RF-10. The routing layer must not hardcode link vocabulary.

## Implementation Plan

### Phase 1: MCP Layer (src/nexus/)

1. **Enhance `query` MCP tool** (`mcp_server.py`): add `author`, `content_type`, `follow_links`, `depth` params with internal catalog routing. Internalize collection extraction from catalog results.
2. **Seed pre-built plan templates** (`commands/catalog.py` setup command): insert 5 templates into T2 plan library at `nx catalog setup` time.
3. **Auto-cache logic** (`mcp_server.py` or new `query_cache.py`): `plan_save` on successful novel plan execution with TTL 30 days, no user prompt.
4. **Tests**: unit tests for enhanced `query` routing (catalog-scoped vs broad), template seeding, auto-cache lifecycle.

### Phase 2: Plugin Layer (nx/)

5. **Simplify `/nx:query` skill** (`nx/skills/query/SKILL.md`): three-path dispatch (single-tool / template / planner). Remove manual collection extraction instructions. Remove "save plan?" prompt.
6. **Update query-planner agent** (`nx/agents/query-planner.md`): reduce scope to exception-path. Add note that simple scoped queries go through enhanced `query` MCP directly. Update few-shot examples to emphasize catalog-first patterns.
7. **Update analytical-operator agent** (`nx/agents/analytical-operator.md`): no functional changes, but update references to the query pipeline flow.
8. **Update orchestrator agent** (`nx/agents/orchestrator.md`): route simple search questions to enhanced `query` MCP, not `/nx:query` skill.
9. **Update SubagentStart hook** (`nx/hooks/scripts/subagent-start.sh`): update `query` tool signature in the nx Storage Tools block to show new params (`author`, `content_type`, `follow_links`, `depth`).
10. **Update related skills** that reference the query pipeline:
    - `nx/skills/research-synthesis/SKILL.md` — reference enhanced `query` for scoped search
    - `nx/skills/knowledge-tidying/SKILL.md` — reference enhanced `query` for dedup checks
    - `nx/skills/deep-analysis/SKILL.md` — reference enhanced `query` for evidence gathering

### Phase 3: User-Facing Documentation (docs/)

11. **`docs/catalog.md`** — update the "Agents use the catalog" section: document the enhanced `query` tool as the primary interface, explain the three-path routing (single-tool / template / planner).
12. **`docs/cli-reference.md`** — update `nx search` / `query` MCP tool documentation with new params. Add examples: `query(question="...", author="Fagin")`.
13. **`docs/storage-tiers.md`** — update T2 plan library description: plans auto-cached on success, 30-day TTL, no user prompt.
14. **`docs/architecture.md`** — update module map: `query` tool routing logic, plan template seeding in catalog setup.
15. **`docs/getting-started.md`** — if query examples exist, update to show catalog-scoped queries.
16. **`docs/memory-and-tasks.md`** — update plan library section: auto-cache behavior, template seeding.
17. **`README.md`** — update "Analytical queries" description to reflect catalog-first routing.
18. **`CLAUDE.md`** — update MCP tool descriptions if `query` signature changes.

### Phase 4: Verification

19. **End-to-end test**: scoped query via enhanced `query` MCP → verify <2s latency, correct collection scoping.
20. **Template match test**: question with author signal → verify template selected, no planner dispatch.
21. **Auto-cache test**: novel plan execution → verify plan_save called automatically.
22. **Regression test**: existing `/nx:query` multi-step flows still work (extract → compare → generate).

## Research Findings

### RF-1: Shakeout Evidence — Planner Ignores Catalog (2026-04-05)
**Classification**: Verified — Live Testing | **Confidence**: HIGH

During v3.0.0 shakeout, the query planner was given "What RDR documents relate to the indexing pipeline in nexus?" and generated `search("indexing pipeline", corpus="rdr")` — a blind vector search. It did NOT use `catalog_search(content_type="rdr", query="pipeline")` despite having the operation available. Manual catalog_search as a supplementary step found 2 precise matches. The planner's default behavior is broad search, not catalog-first routing.

### RF-2: Catalog as Taxonomy (2026-04-05)
**Classification**: Design Analysis | **Confidence**: HIGH

The catalog provides structural navigation (author, provenance, citations) that a taxonomy provides. Vector search provides conceptual navigation (topic matching). Together they cover the retrieval use cases a taxonomy would, without the construction cost (AgenticScholar's 4-stage LLM pipeline). The gap — browsable topic index — is addressed by concept nodes (deferred Layer 3) but not needed for retrieval.

### RF-3: Plan Library Usage (2026-04-05)
**Classification**: Verified — Code Analysis | **Confidence**: HIGH

The T2 plan library (RDR-034) stores plans as FTS5-searchable entries. Current usage: the skill calls `plan_search` on every query, finds 0 matches (library is nearly empty), then dispatches the planner. The auto-save prompt was declined in every observed session. The library is a good mechanism with bad UX — auto-caching fixes it.

### RF-4: Current Query Pipeline Audit (2026-04-05)
**Classification**: Verified — Codebase Analysis (11 source files read) | **Confidence**: HIGH

Full audit of the current query pipeline implementation:

**`query` MCP tool** (`mcp_server.py`): params `question`, `corpus`, `where`, `limit`. Over-fetches chunks (limit×10), groups by document, returns best snippet per doc. **No routing intelligence** — catalog-blind, same `search_cross_corpus()` as `search`. This is the tool to enhance.

**`/nx:query` skill**: 308 lines, 5 top-level steps, 8 execution paths. **71 lines (23%) are mechanical orchestration** — collection extraction from catalog results, `$step_N` reference resolution via scratch, fanout dedup for multi-tumbler catalog_links, redundant scratch re-writes. This is deterministic pipeline logic encoded as LLM-interpreted markdown.

**Query planner agent**: knows 9 operations, has 5 few-shot examples. The catalog-first routing decision is **purely heuristic** — the LLM reads prose instructions and decides. This is the sole non-deterministic branch point with correctness risk.

**Plan library**: **0 plans stored** — completely empty. The auto-save prompt has been declined in every observed session.

**Determinism map**: Deterministic (should be MCP code): catalog_search execution, collection extraction, scratch resolution, plan-library keyword matching. LLM-decided (should stay in agent): which analytical operations to use, analytical output quality. The routing branch — catalog-first vs blind search — is the one decision currently requiring LLM that should be deterministic.

### RF-5: General Systems Theory — Boundaries, Signals, Feedback (2026-04-05)
**Classification**: Systems Analysis | **Confidence**: HIGH

**Boundaries**: 7 boundary types across 4 layers. The critical boundary is B2/B3 (Skill ↔ Planner) where deterministic routing information (author, content_type, link_type) is encoded as natural language, passed through an LLM, and decoded back into structured parameters. RDR-052 eliminates this round-trip for 80% of queries.

**Signal flows** (boundary crossings per query type):

| Query Type | Current | RDR-052 | Reduction |
|------------|:-------:|:-------:|:---------:|
| Simple scoped search | 13 | 2 | 85% |
| Citation traversal | 15 | 2 | 87% |
| Analytical pipeline | 14 | 9-11 | 21-36% |

**Feedback loops**: The plan library auto-save is the key missing positive loop — currently broken because the save prompt is always declined (RF-3). Auto-cache repairs this. **Missing feedback**: no routing quality signal. The system cannot distinguish 10 relevant results from 10 tangential results. No user satisfaction signal, no A/B comparison between routing strategies.

**Variety (Ashby)**: The planner introduces O(9^N) combinatorial variety to solve problems with O(5) structural patterns — the catalog made 80% of the planner's variety redundant. Error recovery is under-engineered: one failure path (FAILED marker, continue with broken inputs), no retry, no fallback, no user guidance.

**Homeostasis**: System is stuck in high-cost steady state (always-plan) because the save prompt blocks the positive feedback loop. Auto-cache creates the transition path to low-cost steady state (usually-template-match).
