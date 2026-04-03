---
title: "AgenticScholar-Inspired Enhancements"
id: RDR-042
type: Architecture
status: closed
closed_date: 2026-04-02
close_reason: implemented
priority: medium
author: Hal Hildebrand
reviewed-by: self
created: 2026-03-29
accepted_date: 2026-03-29
related_issues:
  - "nexus-zprl - AgenticScholar Enhancements epic"
  - "RDR-034 - MCP Server Agent Storage (closed)"
  - "RDR-041 - T1 Scratch Inter-Agent Context (closed)"
---

# RDR-042: AgenticScholar-Inspired Enhancements

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

Nexus provides semantic search and knowledge management with three storage tiers, but lacks structured analytical operations over retrieved content. The deep-research-synthesizer agent performs extraction, summarization, ranking, comparison, and generation ad-hoc on every run — with no reuse, no composability, and no plan caching.

Additionally, the PDF ingestion pipeline discards structured table data (Docling already detects tables but they're flattened to text), lacks bibliographic metadata (year, venue, citation count), and there is no retrieval quality benchmark to catch regressions when embeddings, chunking, or scoring change.

## Context

### Background

The paper "AgenticScholar: Agentic Data Management with Pipeline Orchestration for Scholarly Corpora" (arXiv 2603.13774, indexed in `knowledge__agentic-scholar`, 172 chunks/54 pages) describes a four-layer architecture for analytical query processing over scholarly corpora:

1. **Taxonomy-anchored knowledge graph** — structured knowledge organization
2. **LLM-driven query planner** — generates operator DAGs from natural language
3. **Composable operator library** — ~15 typed operators with parameter contracts
4. **Structured document ingestion** — tables, metadata, lineage

Benchmarked against Elicit, Gemini Deep Research, SmolAgent, and RAG, it achieves NDCG@3 = 0.606 (vs RAG's 0.411, +47%) and +21% relevance on knowledge generation tasks.

### Technical Environment

- **Nexus**: Python 3.12+ CLI + persistent server, ChromaDB T3, SQLite T2, EphemeralClient T1
- **MCP server**: `src/nexus/mcp_server.py` — deterministic tools only (search, store, memory, scratch)
- **Agent ecosystem**: 15 agents, orchestrator relay pattern, subagents cannot spawn subagents
- **PDF pipeline**: Docling primary extraction with `do_table_structure=True`, PyMuPDF fallback

### Research Findings

Full research synthesis stored in T3: `research-agenticscholar-nexus-applicability-2026-03-29`

Key findings:
- AgenticScholar and Nexus share common DNA (hybrid retrieval, Docling, reranking)
- AgenticScholar goes substantially further in structured knowledge organization, query planning, and composable analytical primitives
- The taxonomy approach does NOT generalize well to Nexus's mixed-corpus model (code + prose + RDRs + PDFs)
- The operator library, plan caching, and ingestion improvements transfer cleanly

## Proposed Solution

Seven enhancements adapted from AgenticScholar, scoped to what fits Nexus's mixed-corpus architecture. Core design decisions:

1. **Agent-mediated operators** — analytical operations run as agent prompts, not MCP tools. The MCP server stays LLM-free and deterministic.
2. **T2-first for plan storage** — plans are structured records (query, JSON plan, outcome, tags), not prose. SQLite + FTS5 is the natural fit. Semantic T3 layer deferred until FTS5 proves insufficient.
3. **Skill-driven execution** — the `/nx:query` skill is the loop driver for multi-step analytical queries, working around the subagent-cannot-spawn-subagent constraint.
4. **Explicit plan save** — no auto-pollution of the plan library; user prompted after successful execution.
5. **Skip full taxonomy** — AgenticScholar's 4-stage LLM taxonomy is over-engineered for mixed corpora. Deferred to a future RDR if needed.

### Component 1: Analytical Operator Agent

Single agent (`nx/agents/analytical-operator.md`) handling 5 operation types:

| Operation | Purpose | Input | Output |
|-----------|---------|-------|--------|
| extract | Structured JSON extraction from chunks | chunks + template/schema | JSON matching template |
| summarize | Summary of result set | chunks + mode (short/detailed/evidence) | text with optional citations |
| rank | LLM-scored ordering by criterion | items + criterion | ordered list with scores |
| compare | Consistency/contradiction check | items + check instruction | comparison matrix |
| generate | Evidence-grounded text generation | context + instruction | cited text |

Relay format: `{operation, inputs, params}`.

### Component 2: Structured Table Extraction

Modify `_extract_with_docling()` to detect Docling `TableItem` nodes, preserve HTML via `export_to_html()`. Tag chunks overlapping table regions with `chunk_type=table_page` metadata; all others get `chunk_type=text`. Enables `--where chunk_type=table_page` filtering.

**Granularity**: Page-level matching. A chunk is tagged `chunk_type=table_page` if it falls on a page that contains a table. This is coarse but reliable — character-offset mapping between Docling items and exported markdown is fragile. Documented as page-level granularity; future refinement to character-level is possible but not in scope.

### Component 3: Bibliographic Metadata Enrichment

New `src/nexus/bib_enricher.py` querying Semantic Scholar API. Returns `{year, venue, authors, citation_count, semantic_scholar_id}` or empty dict on failure. Integrated inline in `_pdf_chunks()` (with 3s timeout for fast-fail) and as backfill via `nx enrich <collection>` CLI command.

**Rate limiting**: `nx enrich` includes configurable `--delay` option (default 0.5s between titles). **Idempotency**: backfill skips chunks that already have `bib_semantic_scholar_id` metadata populated.

### Component 4: Plan Library (T2)

New `plans` table + FTS5 in T2Database with `save_plan()`, `search_plans()`, `list_plans()`. Explicit save triggered by `/nx:query` skill after successful execution.

### Component 5: Query Decomposition

New `query-planner` agent decomposes NL questions into ordered operator step lists. New `/nx:query` skill drives execution: search plan library → dispatch planner → iterate operator calls → collect results → prompt to save.

**Step output persistence**: After each operator step, the skill writes the step output to T1 scratch with tag `query-step,step-N`. Subsequent steps that reference `$step_N` read from scratch by tag. This leverages the T1 scratch cross-agent context bus established by RDR-041.

### Component 6: Self-Correction Loop

Prompt addition to orchestrator: failure relay protocol with `{original_task, failed_output, failure_reason}`, max 2 retries before user escalation.

**Interface with RDR-040 circuit breaker**: The orchestrator must distinguish two failure types:
1. **Routed failure** (RDR-040 `<!-- ESCALATION -->` sentinel): Do NOT retry the original agent. Route immediately to the debugger agent per RDR-040's directive.
2. **Incomplete/malformed output** (no ESCALATION sentinel): Retry up to 2× with augmented relay containing failure context.

This prevents the conflict where the orchestrator would repeatedly re-dispatch a developer agent that has already fired its circuit breaker.

### Component 7: NDCG Retrieval Benchmark

Synthetic corpus (~25 docs) + ground-truth queries (~50 tuples) in `tests/benchmarks/`. Pytest computing NDCG@5 with EphemeralClient + ONNX MiniLM. `assert mean_ndcg_at_5 >= 0.70`.

## Alternatives Considered

### MCP tools with direct LLM calls (rejected)
Operators as MCP tools that call Anthropic/OpenAI APIs directly. Rejected: couples MCP server to LLM credentials, adds failure mode, breaks the deterministic tool contract.

### One agent per operator (rejected)
Five separate agent files. Rejected: clutter (5 new agents), orchestrator routing table growth, no benefit since operations share relay format.

### T3 for plan storage (deferred)
Embedding plan JSON gives poor vector similarity. T2 FTS5 on query text is sufficient. Can add T3 semantic layer later if FTS5 matching proves inadequate.

### Full AgenticScholar taxonomy (rejected for now)
4-stage LLM-based taxonomy construction. Rejected: expensive, tuned for homogeneous scholarly corpora, doesn't generalize to Nexus's mixed content. May revisit via lightweight clustering in a future RDR.

### Automatic plan save (rejected)
Auto-saving every successful pipeline pollutes the library. Explicit save ensures quality.

## Success Criteria

- [ ] Analytical operator agent handles all 5 operations with structured input/output
- [ ] PDF table chunks tagged with `chunk_type=table_page` (page-level granularity, documented), filterable via `--where`
- [ ] Bibliographic metadata (year, venue, authors, citation_count) attached to PDF chunks
- [ ] `nx enrich <collection>` backfills metadata with configurable `--delay` (default 0.5s)
- [ ] `nx enrich` skips chunks with existing `bib_semantic_scholar_id` (idempotent backfill)
- [ ] Plan library stores and retrieves plans via FTS5 search (note: `plan_json` is not FTS-indexed)
- [ ] `/nx:query` skill executes multi-step analytical queries end-to-end
- [ ] `/nx:query` step outputs persisted to T1 scratch and correctly resolved by subsequent steps
- [ ] Orchestrator distinguishes ESCALATION sentinels (route to debugger) from incomplete output (retry up to 2×)
- [ ] NDCG@5 benchmark passes at calibrated threshold (set after first run, not hardcoded), no API keys required
- [ ] MCP server remains LLM-free
- [ ] All existing tests pass (no regressions)

## Implementation Plan

See `docs/plans/2026-03-29-agenticscholar-enhancements-impl-plan.md` (audited, corrections applied).

**Epic**: nexus-zprl
**Branch**: `feature/nexus-zprl-agenticscholar-enhancements`

4 phases, 7 tasks:

| Phase | Bead | Component | Type |
|-------|------|-----------|------|
| 1 | nexus-38lq | Self-Correction Loop | prompt |
| 1 | nexus-erim | Analytical Operator Agent | agent |
| 1 | nexus-1sz2 | Plan Library T2 | Python/TDD |
| 2 | nexus-pjz7 | Table Extraction | Python/TDD |
| 2 | nexus-dji3 | Bib Enrichment | Python/TDD |
| 3 | nexus-uffi | Query Decomposition | agent+skill |
| 4 | nexus-5hvy | NDCG Benchmark | Python/TDD |

**Dependency**: nexus-uffi depends on nexus-1sz2 + nexus-erim.

## Research Findings

### RF-1: AgenticScholar Paper Architecture Analysis (2026-03-29)

**Classification**: Verified — Source Analysis
**Method**: Semantic search across `knowledge__agentic-scholar` (172 chunks, 54 pages), 20+ targeted queries
**Confidence**: HIGH

AgenticScholar's four-layer architecture:
1. **Taxonomy-anchored knowledge graph** — per-paper LLM aspect extraction → cross-paper synonym clustering → reference taxonomy generation → relation construction. Most technically ambitious component and root cause of quality advantage.
2. **Hybrid planner** — three tiers (retrieval, extraction/aggregation, open-ended synthesis). Semantic search over `(query, plan)` library, reuse if confidence > 90% (~40% cost reduction). Self-correction loop for failed plans.
3. **~15 composable operators** — Knowledge Access (Search, FindNode, Traverse), Extraction (Extract, Summary), Analysis (Filter, Sort, Rank, GroupBy, Aggregate, Compare/Check), Synthesis (MatrixConstruct, Generate). Parallel execution with disk-persistent caching.
4. **Structured ingestion** — tables as HTML with bounding boxes, Semantic Scholar bibliographic enrichment, section detection, data lineage.

Benchmarks: NDCG@3 = 0.606 (vs RAG 0.411, +47%), +21% relevance on knowledge generation.

### RF-2: Nexus Architecture Overlap (2026-03-29)

**Classification**: Verified — Codebase Analysis
**Method**: Direct code inspection of `src/nexus/` modules
**Confidence**: HIGH

| AgenticScholar Concept | Nexus Equivalent | Match Quality |
|---|---|---|
| Hybrid retrieval | `scoring.py`: vector + frecency + ripgrep | Good — no temporal/year filtering |
| PDF extraction | `pdf_extractor.py`: Docling primary, PyMuPDF fallback | Equivalent text quality; no table structuring |
| Multi-agent orchestration | 15-agent ecosystem + orchestrator relay | Analogous but manual DAGs |
| Reranking | Voyage AI rerank-2.5 in `scoring.py` | Direct equivalent |
| Document ingestion pipeline | `indexer.py`: classify → chunk → embed → store | Similar; missing bib enrichment |

### RF-3: Docling TableItem API Verification (2026-03-29)

**Classification**: Verified — API Inspection
**Method**: Import path verification and API check during plan audit
**Confidence**: HIGH

- `TableItem` lives in `docling_core.types.doc.document` (NOT `docling.datamodel.base_models`)
- `export_to_html(self, doc=None, add_caption=True) -> str` — confirmed
- `prov[0].page_no` — `ProvenanceItem.page_no` confirmed
- `iterate_items()` — confirmed on `DoclingDocument`
- `do_table_structure = True` already set in `_get_converter()` at `pdf_extractor.py:71`
- Duck-typed detection (`type(item).__name__ == "TableItem"`) works regardless of import path

### RF-4: T2 Schema Compatibility (2026-03-29)

**Classification**: Verified — Schema Testing
**Method**: Live SQL execution test of plans table DDL + FTS5 triggers
**Confidence**: HIGH

- `_SCHEMA_SQL` is a plain string passed to `executescript()` in `_init_schema()`
- `CREATE TABLE IF NOT EXISTS` + `CREATE VIRTUAL TABLE IF NOT EXISTS` are idempotent — safe for existing databases
- FTS5 `content=plans, content_rowid=id` (unquoted) is valid SQLite syntax
- Trigger patterns (`plans_ai`, `plans_ad`, `plans_au`) exactly match existing `memory_ai/ad/au`
- `_sanitize_fts5()` exists and is reusable for `search_plans()`

### RF-5: httpx Dependency Status (2026-03-29)

**Classification**: Verified — Dependency Audit
**Method**: `pyproject.toml` and `uv.lock` inspection
**Confidence**: HIGH

httpx v0.28.1 is present in `uv.lock` as a transitive dependency of chromadb, mcp, and docling. It is NOT listed in `[project.dependencies]`. Using it directly in `bib_enricher.py` requires adding `"httpx>=0.27"` as an explicit dependency. Without this, an upstream dependency change could silently remove httpx.

### RF-6: Skill File Structure Convention (2026-03-29)

**Classification**: Verified — Plugin Structure
**Method**: Glob pattern verification of existing skills
**Confidence**: HIGH

All existing skills follow the `nx/skills/<name>/SKILL.md` directory pattern. Flat `.md` files at `nx/skills/` are NOT recognized by the plugin system. The query skill must be created at `nx/skills/query/SKILL.md`.

### RF-7: Semantic Scholar API Characteristics (2026-03-29)

**Classification**: Verified — API Documentation
**Method**: Semantic Scholar API documentation review
**Confidence**: HIGH

- Endpoint: `https://api.semanticscholar.org/graph/v1/paper/search`
- Rate limits: 100 requests/5 minutes without API key, higher with key
- Fields available: `year`, `venue`, `authors`, `citationCount`, `externalIds`, `paperId`
- Response: JSON with `data` array, each element containing requested fields
- Authors are nested objects with `name` field — must flatten to comma-separated string for ChromaDB metadata (str/int/float/bool only)

### RF-8: NDCG Benchmark Feasibility (2026-03-29)

**Classification**: Verified — Testing Infrastructure
**Method**: Code inspection of search_engine.py and test infrastructure
**Confidence**: HIGH

- `search_cross_corpus()` accepts `t3: Any` — can be wrapped or bypassed
- `ONNXMiniLM_L6_V2` confirmed in `chromadb.utils.embedding_functions` (384d)
- `tests/benchmarks/` will be auto-discovered by pytest (testpaths = ["tests"])
- Option (b) — direct `collection.query()` + manual `SearchResult` construction — avoids T3 interface coupling
- Deterministic: same corpus + queries + ONNX embeddings = same results every run

## Audit Corrections (Applied)

1. Add `httpx>=0.27` to `pyproject.toml` (currently transitive only)
2. Skill path: `nx/skills/query/SKILL.md` (directory), not `nx/skills/query.md`
3. Use `strftime("%Y-%m-%dT%H:%M:%SZ")` in `save_plan()` to match T2 convention
