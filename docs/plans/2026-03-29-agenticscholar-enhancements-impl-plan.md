# AgenticScholar Enhancements — Implementation Plan

**Date**: 2026-03-29
**Epic**: nexus-zprl (AgenticScholar Enhancements)
**Design**: `docs/plans/2026-03-29-agenticscholar-enhancements-design.md`
**Branch**: `feature/nexus-zprl-agenticscholar-enhancements`

## Executive Summary

Seven enhancements adapted from AgenticScholar (arXiv 2603.13774) for Nexus:
agent-mediated analytical operators, structured table extraction, bibliographic
enrichment, a T2 plan library, query decomposition with plan reuse, orchestrator
self-correction, and an NDCG retrieval benchmark.

## Dependency Graph

```
Phase 1 (all independent — parallelize freely):
  nexus-38lq  #6  Self-Correction Loop        (prompt only)
  nexus-erim  #1  Analytical Operator Agent    (agent .md)
  nexus-1sz2  #4  Plan Library T2              (Python + TDD)

Phase 2 (independent of each other, parallel within phase):
  nexus-pjz7  #2  Structured Table Extraction  (Python + TDD)
  nexus-dji3  #3  Bibliographic Enrichment     (Python + TDD)

Phase 3 (depends on nexus-erim + nexus-1sz2):
  nexus-uffi  #5  Query Decomposition          (agent + skill .md)

Phase 4 (independent — placed last for baseline capture):
  nexus-5hvy  #7  NDCG Retrieval Benchmark     (Python + TDD)
```

**Critical path**: nexus-1sz2 (Plan Library) + nexus-erim (Operator Agent) --> nexus-uffi (Query Decomposition)

**Parallelization**: Phase 1 tasks are fully independent. Phase 2 tasks are
independent of each other and of Phase 1 (can start immediately). Phase 4 is
independent but strategically last to capture the post-enhancement baseline.

---

## Phase 1: Foundation — Agent Definitions + Schema

### Task 1A: Self-Correction Loop (nexus-38lq)

**Design ref**: #6
**Type**: Prompt-only change (no Python)
**Files modified**: `nx/agents/orchestrator.md`

#### Steps

1. Open `nx/agents/orchestrator.md`
2. Add a new `## Failure Relay Protocol` section after `## Anti-Patterns to Avoid` (line ~175)
3. Content:
   - When a downstream agent returns an error or incomplete/unusable result, the
     orchestrator re-dispatches with an augmented relay
   - Augmented relay contains: `{original_task, failed_output, failure_reason}`
   - The orchestrator includes a `retry_count` field (starts at 0)
   - Max 2 retries (`retry_count < 2`), then escalate to user with context
   - On retry, append failure context to the relay's `### Context Notes` section
4. Add a row to the Agent Ecosystem Knowledge table for `analytical-operator`
   (forward reference — will be created in Task 1B)

#### Success Criteria

- [ ] `nx/agents/orchestrator.md` contains `## Failure Relay Protocol` section
- [ ] Retry logic specifies max 2 retries with augmented relay format
- [ ] Escalation path to user is documented

#### Test Strategy

Manual review — no Python code changed. Validate markdown renders correctly.

---

### Task 1B: Analytical Operator Agent (nexus-erim)

**Design ref**: #1
**Type**: Agent definition (no Python)
**Files created**: `nx/agents/analytical-operator.md`

#### Steps

1. Create `nx/agents/analytical-operator.md` with standard agent frontmatter:
   ```yaml
   name: analytical-operator
   version: "1.0"
   description: Executes analytical operations (extract, summarize, rank, compare, generate) on retrieved content.
   model: sonnet
   color: cyan
   effort: low
   ```
2. Define relay format:
   ```json
   {
     "operation": "extract|summarize|rank|compare|generate",
     "inputs": ["<chunk texts or search result content>"],
     "params": {"template": "...", "mode": "short|detailed|evidence", "criterion": "..."}
   }
   ```
3. Define per-operation behavior:
   - **extract**: Apply caller-provided JSON template/schema to inputs, return structured JSON
   - **summarize**: Produce summary (mode: short=1-2 sentences, detailed=paragraph, evidence=with citations)
   - **rank**: Score and order inputs by specified criterion, return ordered list with scores
   - **compare**: Cross-reference inputs for consistency/contradictions, return comparison matrix
   - **generate**: Produce evidence-grounded text from context, cite source chunks
4. Include output format specification for each operation
5. Add a note: "This agent is dispatched by the `/nx:query` skill — it does not spawn sub-agents"

#### Success Criteria

- [ ] `nx/agents/analytical-operator.md` exists with valid frontmatter
- [ ] All 5 operations defined with input/output contracts
- [ ] Relay format documented with examples
- [ ] No LLM calls in MCP server (agent-mediated only)

#### Test Strategy

Structural validation: file exists, frontmatter parses, all 5 operations documented.

---

### Task 1C: Plan Library T2 (nexus-1sz2)

**Design ref**: #4
**Type**: Python (TDD)
**Files modified**: `src/nexus/db/t2.py`
**Files created**: `tests/test_plan_library.py`
**Test command**: `uv run pytest tests/test_plan_library.py -v`

#### Steps

1. **Write tests FIRST** — `tests/test_plan_library.py`:
   ```python
   # Test save_plan() — insert a plan, verify row exists
   # Test save_plan() — verify plan_json stored correctly (JSON string)
   # Test search_plans() — FTS5 match on query text
   # Test search_plans() — FTS5 match on tags
   # Test search_plans() — empty result for non-matching query
   # Test list_plans() — returns all plans ordered by created_at DESC
   # Test list_plans() — empty database returns empty list
   # Test list_plans(limit=N) — respects limit parameter
   ```
   Use `tmp_path` fixture to create ephemeral T2Database instances.

2. **Add plans schema to `_SCHEMA_SQL`** in `src/nexus/db/t2.py`:
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

   CREATE TRIGGER IF NOT EXISTS plans_ai AFTER INSERT ON plans BEGIN
       INSERT INTO plans_fts(rowid, query, tags) VALUES (new.id, new.query, new.tags);
   END;

   CREATE TRIGGER IF NOT EXISTS plans_ad AFTER DELETE ON plans BEGIN
       INSERT INTO plans_fts(plans_fts, rowid, query, tags)
           VALUES ('delete', old.id, old.query, old.tags);
   END;

   CREATE TRIGGER IF NOT EXISTS plans_au AFTER UPDATE ON plans BEGIN
       INSERT INTO plans_fts(plans_fts, rowid, query, tags)
           VALUES ('delete', old.id, old.query, old.tags);
       INSERT INTO plans_fts(rowid, query, tags) VALUES (new.id, new.query, new.tags);
   END;
   ```

3. **Implement T2Database methods**:
   - `save_plan(query: str, plan_json: str, outcome: str = "success", tags: str = "") -> int`
     Returns the new row ID. Sets `created_at` to `datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")`
     (matching existing t2.py timestamp convention — Audit Correction #3).
   - `search_plans(query: str, limit: int = 5) -> list[dict[str, Any]]`
     FTS5 search on plans_fts. Returns plans ordered by rank. Use `_sanitize_fts5()`.
   - `list_plans(limit: int = 20) -> list[dict[str, Any]]`
     Returns most recent plans ordered by `created_at DESC`.

4. **Run tests**: `uv run pytest tests/test_plan_library.py -v`

5. **Run full test suite**: `uv run pytest` to verify no regressions in existing T2 tests.

#### Success Criteria

- [ ] `tests/test_plan_library.py` written before implementation
- [ ] All plan library tests pass
- [ ] Existing T2 tests (`tests/test_t2.py`, `tests/test_t2_prefix_scan.py`) still pass
- [ ] Schema uses `CREATE TABLE IF NOT EXISTS` (safe for existing databases)
- [ ] FTS5 triggers follow existing `memory_fts` pattern

#### Test Strategy

Unit tests with ephemeral SQLite databases via `tmp_path`. Tests cover:
save, search (match + no-match), list (with/without limit), empty DB edge case.

---

## Phase 2: Ingestion Pipeline Enhancements

### Task 2A: Structured Table Extraction (nexus-pjz7)

**Design ref**: #2
**Type**: Python (TDD)
**Files modified**: `src/nexus/pdf_extractor.py`, `src/nexus/pdf_chunker.py`
**Files created**: `tests/test_table_extraction.py`
**Test command**: `uv run pytest tests/test_table_extraction.py -v`

#### Steps

1. **Write tests FIRST** — `tests/test_table_extraction.py`:
   ```python
   # Test _extract_with_docling returns table_regions in metadata
   # Test table_regions contain {page, html, start_char, end_char}
   # Test PDFChunker.chunk() assigns chunk_type="table" for table-overlapping chunks
   # Test PDFChunker.chunk() assigns chunk_type="text" for non-table chunks
   # Test mixed document: some chunks table, some text
   # Test document with no tables: all chunks get chunk_type="text"
   ```
   Mock Docling document objects since unit tests cannot load the neural model.

2. **Modify `_extract_with_docling()`** in `pdf_extractor.py`:
   - After iterating pages for markdown, do a second pass over `doc.iterate_items()`
   - Detect items where `type(item).__name__ == "TableItem"` or label contains "table"
   - For each TableItem, capture:
     - `html`: call `item.export_to_html()` if available, else use markdown repr
     - `page`: from `item.prov[0].page_no`
     - Character position range in the exported markdown text
   - Add `"table_regions": [...]` to the returned `ExtractionResult.metadata`

3. **Modify `PDFChunker.chunk()`** in `pdf_chunker.py`:
   - Extract `table_regions` from `extraction_metadata` (default `[]`)
   - For each chunk, check overlap with any table_region's char range
   - Set `chunk.metadata["chunk_type"] = "table"` if overlap, else `"text"`

4. **Verify** that `_pdf_chunks()` in `doc_indexer.py` already passes
   `result.metadata` through — the `chunk_type` field will flow into stored
   metadata automatically via `chunk.metadata`.

5. **Run tests**: `uv run pytest tests/test_table_extraction.py -v`

6. **Run related tests**: `uv run pytest tests/test_doc_indexer.py -v`

#### Success Criteria

- [ ] Tests written before implementation
- [ ] Table chunks tagged with `chunk_type=table`
- [ ] Non-table chunks tagged with `chunk_type=text`
- [ ] HTML representation preserved in extraction metadata
- [ ] Existing PDF extraction tests still pass
- [ ] ChromaDB `where={"chunk_type": "table"}` filter works

#### Test Strategy

Unit tests with mocked Docling document objects. No real PDFs needed. Test both
the extractor (table_regions in metadata) and the chunker (chunk_type tagging).

#### Implementation Notes

- Check Docling docs via Context7 for `TableItem` API and HTML export method
- The `_extract_normalized` (PyMuPDF fallback) path produces no table_regions — all chunks get `chunk_type="text"`, which is correct
- Character position mapping between Docling items and exported markdown is approximate — use page-level matching as fallback

---

### Task 2B: Bibliographic Metadata Enrichment (nexus-dji3)

**Design ref**: #3
**Type**: Python (TDD)
**Files created**: `src/nexus/bib_enricher.py`, `src/nexus/commands/enrich.py`, `tests/test_bib_enricher.py`
**Files modified**: `src/nexus/doc_indexer.py`, `src/nexus/cli.py`, `pyproject.toml`
**Test command**: `uv run pytest tests/test_bib_enricher.py -v`

#### Pre-requisite (Audit Correction #1)

Add `"httpx>=0.27"` to `[project.dependencies]` in `pyproject.toml` and run `uv sync`.
httpx is currently only a transitive dependency via chromadb/mcp/docling — using it directly
requires an explicit dependency declaration.

#### Steps

1. **Write tests FIRST** — `tests/test_bib_enricher.py`:
   ```python
   # Test enrich() with mocked successful API response
   # Test enrich() returns {year, venue, authors, citation_count, semantic_scholar_id}
   # Test enrich() returns {} on HTTP timeout
   # Test enrich() returns {} on 404
   # Test enrich() returns {} on rate limit (429)
   # Test enrich() returns {} on network error
   # Test enrich() returns {} when no results match
   # Test _pdf_chunks integration: enriched fields appear in chunk metadata
   # Test enrich CLI command: iterates collection, groups by title, updates metadata
   ```
   Mock `httpx.get()` for all API calls. For _pdf_chunks integration, mock both
   PDFExtractor and bib_enricher.enrich.

2. **Create `src/nexus/bib_enricher.py`**:
   ```python
   import httpx
   import structlog

   _log = structlog.get_logger(__name__)
   _BASE_URL = "https://api.semanticscholar.org/graph/v1/paper/search"
   _FIELDS = "year,venue,authors,citationCount,externalIds"
   _TIMEOUT = 10.0

   def enrich(title: str) -> dict:
       """Query Semantic Scholar for bibliographic metadata.

       Returns {year, venue, authors, citation_count, semantic_scholar_id}
       or empty dict on any failure.
       """
       try:
           resp = httpx.get(
               _BASE_URL,
               params={"query": title, "fields": _FIELDS, "limit": 1},
               timeout=_TIMEOUT,
           )
           resp.raise_for_status()
           data = resp.json().get("data", [])
           if not data:
               return {}
           paper = data[0]
           return {
               "year": paper.get("year", 0) or 0,
               "venue": paper.get("venue", "") or "",
               "authors": ", ".join(a.get("name", "") for a in paper.get("authors", [])[:5]),
               "citation_count": paper.get("citationCount", 0) or 0,
               "semantic_scholar_id": paper.get("paperId", ""),
           }
       except Exception as exc:
           _log.debug("bib_enricher: lookup failed", title=title, error=str(exc))
           return {}
   ```

3. **Modify `_pdf_chunks()`** in `doc_indexer.py`:
   - After computing `source_title` (around line 299), call `enrich(source_title)`
   - Merge returned dict into each chunk's metadata:
     ```python
     from nexus.bib_enricher import enrich as bib_enrich
     bib = bib_enrich(source_title)
     ```
   - In the metadata dict for each chunk, add:
     ```python
     "bib_year": bib.get("year", 0),
     "bib_venue": bib.get("venue", ""),
     "bib_authors": bib.get("authors", ""),
     "bib_citation_count": bib.get("citation_count", 0),
     "bib_semantic_scholar_id": bib.get("semantic_scholar_id", ""),
     ```
   - Call `enrich()` once per PDF (not per chunk) — all chunks share the same title

4. **Create `src/nexus/commands/enrich.py`**:
   ```python
   @click.command()
   @click.argument("collection")
   def enrich(collection: str) -> None:
       """Backfill bibliographic metadata for chunks in COLLECTION."""
   ```
   - Get all chunks from the collection (paginated, 300 per batch)
   - Group by `source_title`
   - For each unique title, call `bib_enrich(title)`
   - Batch-update metadata via ChromaDB `col.update(ids=..., metadatas=...)`
   - Report progress: `{enriched}/{total} titles enriched`

5. **Register in `cli.py`**:
   ```python
   from nexus.commands.enrich import enrich
   main.add_command(enrich)
   ```

6. **Run tests**: `uv run pytest tests/test_bib_enricher.py -v`

7. **Run related tests**: `uv run pytest tests/test_doc_indexer.py -v`

#### Success Criteria

- [ ] Tests written before implementation
- [ ] `enrich()` returns correct fields on success
- [ ] `enrich()` returns `{}` on all failure modes (timeout, 404, 429, network error)
- [ ] `_pdf_chunks()` includes `bib_*` metadata fields
- [ ] `nx enrich <collection>` backfill command works
- [ ] Existing doc_indexer tests still pass
- [ ] No API calls in tests (all mocked)

#### Test Strategy

Unit tests with mocked httpx responses. Integration test for `_pdf_chunks` with
mocked PDFExtractor + mocked enrich(). CLI test with mocked T3 collection.

#### Implementation Notes

- Use `httpx` (already a project dependency) for HTTP calls
- Semantic Scholar API is rate-limited (100 requests/5 min without key). The backfill
  command should include a sleep between titles if needed.
- ChromaDB metadata values must be str, int, float, or bool — no nested dicts.
  The `authors` field is a comma-separated string, not a list.

---

## Phase 3: Query Pipeline

### Task 3A: Query Decomposition (nexus-uffi)

**Design ref**: #5
**Type**: Agent + Skill definitions (no Python)
**Dependencies**: nexus-1sz2 (Plan Library T2), nexus-erim (Analytical Operator)
**Files created**: `nx/agents/query-planner.md`, `nx/skills/query/SKILL.md`

#### Steps

1. **Create `nx/agents/query-planner.md`**:
   - Frontmatter:
     ```yaml
     name: query-planner
     version: "1.0"
     description: Decomposes analytical questions into step-by-step execution plans with operator references.
     model: sonnet
     color: blue
     effort: medium
     ```
   - Input: natural-language question + optional few-shot plan examples from T2
   - Output: JSON plan — ordered list of steps:
     ```json
     {
       "query": "original question",
       "steps": [
         {"step": 1, "operation": "extract", "search_query": "...", "params": {...}},
         {"step": 2, "operation": "summarize", "inputs": "$step_1", "params": {"mode": "detailed"}},
         {"step": 3, "operation": "compare", "inputs": ["$step_1", "$step_2"], "params": {...}}
       ]
     }
     ```
   - Include guidance on when to use each operation type
   - Reference `search_query` for steps that need retrieval (the skill will execute the search)
   - Reference `$step_N` for steps that consume prior step output

2. **Create `nx/skills/query/SKILL.md`** — the execution driver (Audit Correction #2: skills must be directory/SKILL.md, not flat files):
   - Skill metadata:
     ```yaml
     name: query
     description: Execute multi-step analytical queries with plan reuse
     ```
   - Execution flow:
     1. Receive user question
     2. Search T2 plan library: `search_plans(question, limit=3)` → inject as few-shot context
     3. Dispatch `query-planner` agent with question + few-shot examples
     4. Parse returned JSON plan
     5. For each step in plan:
        a. If step has `search_query`: execute `nx search` and collect results
        b. Dispatch `analytical-operator` agent with `{operation, inputs, params}`
        c. Store step output for reference by subsequent steps (`$step_N` resolution)
     6. Collect final output from last step (or all steps if multi-output)
     7. Present results to user
     8. Prompt: "Save this plan to the library? (y/n)"
     9. If yes: call `save_plan(question, plan_json, outcome="success", tags=...)`
   - Key constraint: **The skill is the loop driver**. Subagents (query-planner,
     analytical-operator) cannot spawn other subagents. The skill dispatches them
     sequentially.
   - Error handling: If an operator step fails, log the error and continue with
     remaining steps (partial results are better than none). Set `outcome="partial"`
     when saving to plan library.

3. **Verify skill integration**:
   - The skill must reference T2 plan library via nx memory tools
   - The skill dispatches agents, not the other way around

#### Success Criteria

- [ ] `nx/agents/query-planner.md` exists with valid frontmatter and plan output schema
- [ ] `nx/skills/query.md` exists with complete execution flow
- [ ] Skill is the loop driver (no agent-to-agent spawning)
- [ ] Plan library integration: search before planning, prompt to save after
- [ ] `$step_N` reference resolution documented
- [ ] Error handling for partial failures documented

#### Test Strategy

Structural validation: files exist, frontmatter parses, all execution steps documented.
End-to-end validation requires manual testing with a real LLM.

---

## Phase 4: Quality Assurance

### Task 4A: NDCG Retrieval Benchmark (nexus-5hvy)

**Design ref**: #7
**Type**: Python (TDD)
**Files created**: `tests/benchmarks/__init__.py`, `tests/benchmarks/corpus.json`, `tests/benchmarks/queries.json`, `tests/benchmarks/test_retrieval_ndcg.py`
**Test command**: `uv run pytest tests/benchmarks/test_retrieval_ndcg.py -v`

#### Steps

1. **Create directory**: `tests/benchmarks/`

2. **Write NDCG math tests FIRST** in `tests/benchmarks/test_retrieval_ndcg.py`:
   ```python
   # Test ndcg_at_k() with perfect ranking → 1.0
   # Test ndcg_at_k() with reversed ranking → < 1.0
   # Test ndcg_at_k() with no relevant results → 0.0
   # Test ndcg_at_k() with k > len(results) → handles gracefully
   # Test dcg() computation matches known values
   ```

3. **Implement NDCG computation** as pure functions in the test file (or a
   small helper module):
   ```python
   import math

   def dcg_at_k(relevances: list[int], k: int) -> float:
       return sum(
           (2**rel - 1) / math.log2(i + 2)
           for i, rel in enumerate(relevances[:k])
       )

   def ndcg_at_k(relevances: list[int], ideal: list[int], k: int) -> float:
       dcg = dcg_at_k(relevances, k)
       idcg = dcg_at_k(sorted(ideal, reverse=True), k)
       return dcg / idcg if idcg > 0 else 0.0
   ```

4. **Create `tests/benchmarks/corpus.json`** — ~25 synthetic documents:
   - Cover topics: authentication, caching, search/indexing, database design,
     API patterns, testing strategies, error handling, concurrency, logging,
     configuration management
   - Mix of code snippets, prose explanations, and mixed content
   - Each document: `{"id": "doc_01", "content": "...", "type": "code|prose|mixed"}`
   - Documents should be 200-500 characters (typical chunk size for ONNX MiniLM)

5. **Create `tests/benchmarks/queries.json`** — ~50 query-relevance tuples:
   - Each: `{"query": "...", "expected": [{"doc_id": "doc_01", "relevance": 3}, ...]}`
   - Relevance grades: 0=irrelevant, 1=marginal, 2=relevant, 3=highly_relevant
   - Cover: specific single-concept queries, broad multi-concept queries,
     code-specific queries, prose-specific queries

6. **Create `tests/benchmarks/test_retrieval_ndcg.py`** — main benchmark test:
   ```python
   def test_retrieval_ndcg_at_5():
       # 1. Load corpus.json
       # 2. Create EphemeralClient + collection with ONNX MiniLM embeddings
       # 3. Index all documents
       # 4. Load queries.json
       # 5. For each query:
       #    a. Run search_cross_corpus(query, [collection], n_results=5, t3=client)
       #    b. Map returned doc IDs to relevance grades
       #    c. Compute NDCG@5
       # 6. Compute mean NDCG@5 across all queries
       # 7. assert mean_ndcg >= 0.70
   ```

7. **Calibration run**: Execute once, record actual NDCG. If below 0.70, adjust
   corpus/queries to establish a meaningful baseline. The threshold should be
   `actual_ndcg - 0.05` to avoid flaky tests.

8. **Run**: `uv run pytest tests/benchmarks/test_retrieval_ndcg.py -v`

#### Success Criteria

- [ ] NDCG math tests pass independently
- [ ] Benchmark runs without API keys (EphemeralClient + ONNX MiniLM)
- [ ] Corpus covers diverse content types
- [ ] Queries cover diverse search patterns
- [ ] `mean_ndcg_at_5 >= 0.70` assertion passes
- [ ] No test pollution — EphemeralClient is isolated per test

#### Test Strategy

Two levels:
1. **Unit**: NDCG math functions tested with known input/output pairs
2. **Benchmark**: Full retrieval pipeline with synthetic corpus. Deterministic
   (same corpus + queries + embeddings = same results every run).

#### Implementation Notes

- Use `chromadb.EphemeralClient()` — no persistence, no cleanup needed
- Use `chromadb.utils.embedding_functions.ONNXMiniLM_L6_V2` for embeddings (384d)
- The `search_cross_corpus()` function in `search_engine.py` accepts a `t3` parameter
  but expects a T3-like object with a `.search()` method. For the benchmark, either:
  (a) wrap the EphemeralClient to match the T3 interface, or
  (b) call `collection.query()` directly and construct SearchResult objects manually
- Option (b) is simpler and avoids coupling to T3 internals

---

## Risk Register

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Docling TableItem API differs from assumption | Medium | Medium | Check Docling docs via Context7 before implementing. Fallback: label-based detection. |
| Semantic Scholar rate limiting during backfill | High | Low | Add configurable delay between requests in `nx enrich`. Default 0.5s. |
| NDCG threshold too aggressive | Medium | Low | Calibrate on first run, set threshold = actual - 0.05 margin. |
| T2 schema migration on existing databases | Low | Medium | `CREATE TABLE IF NOT EXISTS` is idempotent. No ALTER needed. |
| Query skill loop complexity | Medium | Medium | Keep the skill simple: linear step execution, no branching. |

## Execution Guidance

### For All Python Tasks

- Use `mcp__sequential-thinking__sequentialthinking` for non-trivial design decisions
- Write the test file FIRST (TDD). Confirm tests fail before implementing.
- Run `uv run pytest` (full suite) after each task to catch regressions
- Use `structlog` for logging, never `print()`
- Type hints on all public functions

### Parallelization

- Phase 1 tasks (1A, 1B, 1C) can be assigned to parallel agents
- Phase 2 tasks (2A, 2B) can be assigned to parallel agents
- Phase 3 (3A) must wait for Phase 1B and 1C completion
- Phase 4 (4A) can start anytime but benefits from running last

### Branch Strategy

Single feature branch: `feature/nexus-zprl-agenticscholar-enhancements`
One commit per task is ideal. PR when all phases complete.

---

## Bead Summary

| Bead | Design # | Phase | Type | Priority | Dependencies |
|------|----------|-------|------|----------|--------------|
| nexus-zprl | — | — | epic | P1 | — |
| nexus-38lq | #6 | 1A | task | P2 | — |
| nexus-erim | #1 | 1B | task | P2 | — |
| nexus-1sz2 | #4 | 1C | task | P1 | — |
| nexus-pjz7 | #2 | 2A | task | P2 | — |
| nexus-dji3 | #3 | 2B | task | P2 | — |
| nexus-uffi | #5 | 3A | task | P1 | nexus-1sz2, nexus-erim |
| nexus-5hvy | #7 | 4A | task | P2 | — |
