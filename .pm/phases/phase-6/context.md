# Phase 6 — Hybrid Search, Cross-Corpus Reranking, Answer Mode, Output Formatters

**Bead**: nexus-683
**Blocked by**: nexus-ejp (Phase 5), nexus-rr8 (Phase 4)
**Blocks**: nexus-roj (Phase 8)

**Duration**: 2–3 weeks
**Goal**: Full-power search: ripgrep hybrid scoring, cross-corpus Voyage reranking, agentic mode, Mixedbread fan-out, and answer synthesis.

## Scope

### Beads

| Bead | Task |
|------|------|
| nexus-6ba | Hybrid search scoring: vector + frecency weighting |
| nexus-bh5 | Cross-corpus retrieval and Voyage rerank-2.5 unification |
| nexus-4p6 | Mixedbread fan-out with graceful degradation |
| nexus-8sx | Agentic search mode with Haiku-driven query refinement |
| nexus-d31 | Answer mode with Haiku synthesis and citation formatting |

### Technical Decisions

**Hybrid search scoring** (code corpus only):
- Score = 0.7 × vector_norm + 0.3 × frecency_norm
- vector_norm: distance-to-similarity mapped to [0, 1]
- frecency_norm: file frecency score normalized to [0, 1] across results
- Ripgrep line cache provides frecency scores via mmap read (per-repo RWLock; shared during read)
- If ripgrep cache missing: fall back to semantic-only with warning

**Cross-corpus reranking**:
- Retrieve top-K from each corpus separately (different embedding spaces)
- Unify via Voyage rerank-2.5: single reranker over merged candidate set
- If only one corpus: reranker still applied (optional, configurable)
- `--no-rerank` flag: falls back to round-robin interleave of per-corpus results

**Agentic search mode** (`--agentic`):
- 3-iteration Haiku loop: search → analyze gaps → refine query → search again
- Termination: Haiku emits JSON `{"done": true, "final_results": [...]}` to signal completion
- Each iteration: Haiku receives previous results + original query; outputs refined query or done signal
- Max iterations: 3 (configurable via config.yml `search.agenticMaxIterations`)
- Falls back to iteration 1 result if iterations exhausted

**Mixedbread fan-out** (`--mxbai`):
- Fan out to Mixedbread store(s) in addition to local ChromaDB T3
- Requires `MXBAI_API_KEY` and `mxbai.stores` in config.yml
- Graceful degradation: if MXBAI_API_KEY unset or API fails, log warning + continue with local results
- Fan-out results merged via Voyage rerank-2.5 (same reranker as cross-corpus)
- SDK call: `client.stores.search(store_id=..., query=..., top_k=...)`

**Answer mode** (`-a` / `--answer`):
- Haiku synthesis of top-N results
- Citation format: `<cite i="N">` referencing result index
- Plain text output with inline citations; `--json` outputs structured with citations array
- If Haiku fails: falls back to printing raw results with a warning

**Output formatters**:
- Default: plain text with syntax highlighting (bat/pygments)
- `--vimgrep`: `path:line:col:content` (pipe-friendly)
- `--json`: structured JSON array of results
- `--files`: file paths only (one per line)
- `--no-color`: plain text without ANSI codes
- `-B N`, `-A N`, `-C N`: context lines (before/after/both)

## Entry Criteria

- Phase 4 complete (nx serve running, code indexing working, ripgrep cache present)
- Phase 5 complete (PDF/markdown indexing working)
- `anthropic` SDK installed (for Haiku calls)
- `voyageai` SDK installed (for reranker)
- `VOYAGE_API_KEY` and `ANTHROPIC_API_KEY` configured

## Exit Criteria

- [ ] `nx search "query" --hybrid` merges semantic + ripgrep results with 0.7/0.3 weighting
- [ ] Hybrid warning printed if no code corpus in scope
- [ ] `nx search "query" --corpus code --corpus docs` cross-corpus reranks via Voyage rerank-2.5
- [ ] `nx search "query" -a` produces Haiku synthesis with `<cite i="N">` formatting
- [ ] `nx search "query" --agentic` does 3-iteration Haiku loop
- [ ] `nx search "query" --mxbai` fans out to Mixedbread (when configured)
- [ ] `--mxbai` without MXBAI_API_KEY: warning + local-only results (not error)
- [ ] `--vimgrep`, `--json`, `--files`, `--no-color` formatters produce correct output
- [ ] `-B`, `-A`, `-C` context lines work
- [ ] `--no-rerank` falls back to round-robin interleave
- [ ] pytest >85% coverage on search/ and answer/ modules

## Testing Strategy

**Unit tests** (`tests/unit/search/test_hybrid_scoring.py`):
- vector_norm + frecency_norm combination produces scores in [0, 1]
- If frecency score absent: falls back to semantic-only score
- Result ordering matches expected combined score

**Unit tests** (`tests/unit/search/test_cross_corpus.py`):
- Results from two corpora merged correctly
- Voyage reranker mock: verify call parameters and result mapping
- `--no-rerank`: round-robin interleave

**Integration tests** (`tests/integration/test_answer_mode.py`):
- Haiku synthesis with mocked anthropic client
- Citation format: `<cite i="N">` appears in output
- Haiku failure: falls back to raw results

## Key Files

| File | Purpose |
|------|---------|
| `src/nexus/search/hybrid.py` | Hybrid scoring (vector + frecency) |
| `src/nexus/search/cross_corpus.py` | Multi-corpus retrieval + reranking |
| `src/nexus/search/agentic.py` | Haiku-driven iterative refinement |
| `src/nexus/search/mxbai.py` | Mixedbread fan-out |
| `src/nexus/answer/synthesis.py` | Haiku answer synthesis + citations |
| `src/nexus/formatting/` | Output formatters (plain, vimgrep, json, files) |
| `tests/unit/search/test_hybrid_scoring.py` | Hybrid scoring unit tests |
| `tests/integration/test_answer_mode.py` | Answer mode with mock Haiku |
