# AgenticScholar-Inspired Enhancements for Nexus — Design

**Date**: 2026-03-29
**Source**: arXiv 2603.13774 — "AgenticScholar: Agentic Data Management with Pipeline Orchestration for Scholarly Corpora"
**Indexed**: `knowledge__agentic-scholar` (172 chunks, 54 pages)

## Overview

Seven enhancements adapted from AgenticScholar's architecture to Nexus. Core principle: agent-mediated operators (no LLM calls in the MCP server), T2-first for structured plan storage, LLM-free ingestion improvements where possible.

## 1. Analytical Operator Agent

**File**: `nx/agents/analytical-operator.md`

A single agent handling 5 operation types via a relay `operation` field:

- **extract** — Structured JSON extraction from retrieved chunks using a caller-provided template/schema
- **summarize** — Short/detailed/evidence-backed summary of a result set
- **rank** — LLM-scored ordering of items by a specified criterion
- **compare** — Consistency/contradiction check across a set of items
- **generate** — Evidence-grounded text generation from context

Relay format:
```json
{
  "operation": "extract|summarize|rank|compare|generate",
  "inputs": ["<chunk texts or search result IDs>"],
  "params": {"template": "...", "mode": "short|detailed|evidence", "criterion": "..."}
}
```

The agent reads inputs, applies the operation, returns structured output. The MCP layer stays deterministic — no LLM calls in `mcp_server.py`.

## 2. Structured Table Extraction

**Modified**: `src/nexus/pdf_extractor.py`, `src/nexus/pdf_chunker.py`

Modify `_extract_with_docling()` to detect Docling `TableItem` nodes and preserve their HTML representation. In `PDFChunker.chunk()`, tag table-containing chunks with `"chunk_type": "table"` metadata. Non-table chunks get `"chunk_type": "text"`.

Enables `nx search --where chunk_type=table` filtering. No new files.

## 3. Bibliographic Metadata Enrichment

**New file**: `src/nexus/bib_enricher.py`
- `enrich(title: str) -> dict` — queries Semantic Scholar API (`api.semanticscholar.org/graph/v1/paper/search`), returns `{year, venue, authors, citation_count, semantic_scholar_id}` or empty dict on failure
- Graceful degradation: timeout/404/rate-limit → empty dict, indexing proceeds without enrichment

**Inline integration**: `_pdf_chunks()` in `doc_indexer.py` calls `enrich()` after title extraction, merges fields into chunk metadata.

**Backfill command**: `nx enrich <collection>` — new Click command in `src/nexus/commands/enrich.py`. Iterates chunks in a collection, groups by `source_title`, calls `enrich()` per unique title, batch-updates metadata via ChromaDB `update()`.

## 4. Plan Library (T2 `plans` table)

**Modified**: `src/nexus/db/t2.py`

Schema:
```sql
CREATE TABLE IF NOT EXISTS plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    query TEXT NOT NULL,
    plan_json TEXT NOT NULL,
    outcome TEXT DEFAULT 'success',
    tags TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS plans_fts USING fts5(
    query, tags, content=plans, content_rowid=id
);
```

T2 API additions: `save_plan()`, `search_plans()`, `list_plans()`.

Explicit save: The `/nx:query` skill prompts after successful execution.

Retrieval: Before planning, the query-planner agent searches T2 plans via FTS5. Top matches injected as few-shot examples.

## 5. Query Decomposition

**New agent**: `nx/agents/query-planner.md` — receives a natural-language analytical question, returns a JSON plan (ordered list of operator steps). Each step specifies: `operation`, `inputs` (literal or reference to prior step output), `params`.

**New skill**: `nx/skills/query.md` — the execution driver:
1. Search T2 plan library for similar queries → inject as few-shot context
2. Dispatch `query-planner` agent with the question + few-shot examples
3. Parse returned plan (list of steps)
4. For each step: dispatch `analytical-operator` agent with the operation + accumulated context
5. Collect final output, present to user
6. Prompt: "Save this plan to the library?"

## 6. Self-Correction Loop in Orchestrator

**Modified**: `nx/agents/orchestrator.md`

Add a "Failure Relay" section. When a downstream agent returns an error or incomplete result, the orchestrator re-dispatches with an augmented relay containing `{original_task, failed_output, failure_reason}`. Max 2 retries before escalating to user.

Purely a prompt/agent-definition change — no Python code.

## 7. NDCG Retrieval Benchmark

**New directory**: `tests/benchmarks/`

- `tests/benchmarks/corpus.json` — ~25 synthetic documents spanning code, prose, and mixed content
- `tests/benchmarks/queries.json` — ~50 `(query, expected_results_with_relevance_grades)` tuples
- `tests/benchmarks/test_retrieval_ndcg.py`:
  - Loads corpus into `EphemeralClient` with ONNX MiniLM embeddings
  - Runs each query through `search_cross_corpus()`
  - Computes NDCG@5 per query, averages
  - `assert mean_ndcg_at_5 >= 0.70` (baseline calibrated on first run)

No API keys needed — local ONNX embeddings like existing unit tests.

## Dependency Graph

```
#2 (tables) ──────────┐
#3 (bib enrichment) ──┤── independent, can parallelize
#6 (self-correction) ─┤
#7 (NDCG benchmark) ──┘

#4 (plan library T2) ─→ #5 (query planner) ─→ #1 (analytical operator)
```

Items 2, 3, 6, 7 are fully independent. Item 5 depends on 4 (plan retrieval) and 1 (operator execution).

## Files Changed/Created

| Item | Modified | Created |
|------|----------|---------|
| #1 | — | `nx/agents/analytical-operator.md` |
| #2 | `src/nexus/pdf_extractor.py`, `src/nexus/pdf_chunker.py` | — |
| #3 | `src/nexus/doc_indexer.py`, `src/nexus/cli.py` | `src/nexus/bib_enricher.py`, `src/nexus/commands/enrich.py` |
| #4 | `src/nexus/db/t2.py` | — |
| #5 | — | `nx/agents/query-planner.md`, `nx/skills/query.md` |
| #6 | `nx/agents/orchestrator.md` | — |
| #7 | — | `tests/benchmarks/corpus.json`, `tests/benchmarks/queries.json`, `tests/benchmarks/test_retrieval_ndcg.py` |
