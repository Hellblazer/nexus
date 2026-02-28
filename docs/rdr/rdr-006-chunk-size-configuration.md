---
title: "Chunk Size Configuration and File-Size Scoring for Code Search"
type: feature
status: draft
priority: P2
author: Hal Hildebrand
date: 2026-02-28
related_issues: []
---

# RDR-006: Chunk Size Configuration and File-Size Scoring for Code Search

## Problem

`nx index repo` uses a fixed chunk size when splitting source files into embedding units.
Large files (e.g. `main.py`, `doctor.py`) produce broad chunks that score highly on almost
any semantic query, drowning out smaller, more focused files that are the canonical answer.

Observed symptoms in the `code__arcaneum-2ad2825c` collection:

- Query `"embedding model GPU acceleration FastEmbed"` returns `doctor.py`, `errors.py`,
  `main.py` — but not `embeddings/client.py`
- Query `"chunk overlap tokenizer source code indexing"` returns `analyze_profile.py`,
  `qdrant-monitor-segments.py` — but not `indexing/markdown/chunker.py`
- Query `"MeiliSearch full text search index documents"` returns `qdrant-restore.sh`,
  `command_wrapper.py` — but not `fulltext/client.py`

Root cause: large files chunked at the default size produce chunks that span many unrelated
topics, inflating their semantic surface area. Smaller chunks confine each embedding to a
tighter conceptual scope, improving precision.

## Proposed Solution

Two complementary tracks that address the same root cause at different layers:

**Track A — Search-time: file-size scoring penalty** (no re-indexing, immediate effect)
**Track B — Index-time: chunk-size presets for code** (requires `--force` re-index, improves long-term precision)

Both tracks are code-only (`code__*` collections). Markdown and PDF chunkers use different
units (tokens/chars) and are out of scope.

### Track A: File-Size Scoring Penalty

Every chunk already stores `chunk_count` (total chunks the source file was split into) as
metadata. Large files produce many chunks; a file with `chunk_count=80` is dominating results
because all 80 of its chunks have broad semantic surface area.

Apply a `file_size_factor` multiplier to `hybrid_score` for `code__` results in
`scoring.py:apply_hybrid_scoring()`:

```python
import math

_FILE_SIZE_THRESHOLD = 10  # chunks; files ≤ this are not penalised

def _file_size_factor(chunk_count: int) -> float:
    """Return a [0,1] penalty: 1.0 for small files, diminishing for large."""
    return min(1.0, _FILE_SIZE_THRESHOLD / max(1, chunk_count))
```

With this factor:
- `chunk_count ≤ 10` → factor = 1.0 (no penalty)
- `chunk_count = 20` → factor = 0.50
- `chunk_count = 50` → factor = 0.20
- `chunk_count = 80` → factor = 0.125

The final `code__` hybrid score becomes:
```
hybrid_score = (0.7 * vector_norm + 0.3 * frecency_norm) * file_size_factor(chunk_count)
```

This is applied **before** Voyage reranking, so the reranker still has final say over order
but works from a pre-filtered set that doesn't skew toward large files.

An optional `--max-file-chunks N` flag on `nx search` can add a hard `where` filter:
`{"chunk_count": {"$lte": N}}`, eliminating very large files entirely from candidate set.

### Track B: Chunk-Size Presets for Code

Expose named presets on `nx index repo`:

```bash
nx index repo <path> [--chunk-size small|medium|default] [--force]

# Examples
nx index repo .                          # default (150 lines, current behaviour)
nx index repo . --chunk-size small       # 60 lines, high precision
nx index repo . --chunk-size medium      # 100 lines, balanced
nx index repo . --chunk-size small --force  # re-index entire code collection
```

Preset line counts (code only, `_CHUNK_LINES` in `chunker.py`):

| Preset    | Lines | Overlap (15%) | Use case |
|-----------|-------|---------------|----------|
| `small`   | 60    | 9 lines       | Large repos with many files; maximise precision |
| `medium`  | 100   | 15 lines      | Balanced: good precision, fewer chunks to embed |
| `default` | 150   | 22 lines      | Current behaviour (backward-compatible) |

When `--chunk-size` differs from `default`, `--force` is **required** (not implied):
the user must explicitly pass `--force` to delete-and-recreate the code collection.
This avoids silent destructive re-index on a mistyped flag.

### Implementation Notes

**Track A touch-points (scoring.py only):**
- `src/nexus/scoring.py:apply_hybrid_scoring()` — add `_file_size_factor` call for `code__` results
- `src/nexus/commands/search_cmd.py` — add `--max-file-chunks INT` option, pass as `where` filter

**Track B touch-points:**
1. `src/nexus/commands/index.py:index_repo_cmd()` — add `--chunk-size [small|medium|default]` option
2. `src/nexus/indexer.py:index_repository()` — add `chunk_lines: int` kwarg
3. `src/nexus/indexer.py:_run_index()` — thread through to file dispatchers
4. `src/nexus/indexer.py:_index_code_file()` — pass to `chunk_file()`
5. `src/nexus/chunker.py:chunk_file()` — accept `chunk_lines` / `overlap` override params

ChromaDB Cloud document hard limit: 16,384 bytes (RDR-005). Max safe preset is `default`
(150 lines ≈ 3,600 bytes typical). The `small` preset (60 lines) is well within limits.

## Alternatives Considered

### Re-index with `--force` at identical chunk size (rejected)
Re-embedding at the same 150-line size produces identical embeddings and identical precision.
No improvement.

### `--hybrid` search mode (insufficient)
Tested: hybrid search (semantic + ripgrep) returned the same files as semantic-only for the
failing queries. The problem is that broad chunks score high semantically; ripgrep re-ranking
does not overcome a dominant vector score.

### Raw `--chunk-size INT` instead of presets (rejected for UX)
Exposes an internal implementation detail (line count) without guidance on what values work.
Presets (`small`/`medium`/`default`) provide guardrails and encode empirically validated
configurations. A `--chunk-lines INT` escape hatch can be added later if needed.

### Voyage reranker as sole fix (insufficient)
The reranker operates on the top-K candidates returned by ChromaDB. If the top-K is already
dominated by large-file chunks (because ChromaDB returns the highest-similarity results
regardless of source size), the reranker has no small-file candidates to surface. The scoring
penalty must be applied before or instead of relying on the reranker.

### Accept current behaviour (rejected)
Workaround is to write more specific, term-rich queries. This shifts the burden to users and
doesn't fix the underlying structural issue.

## Research Findings

### R1: Current chunking implementation (Confirmed)

**Source:** `src/nexus/chunker.py`

Code files use `chunk_file()` (line 162) which dispatches to:
- **AST path** (supported languages): llama-index `CodeSplitter` with `chunk_lines=_CHUNK_LINES` (150) and overlap computed as `150 × _OVERLAP` (0.15) = 22 lines
- **Fallback path**: `_line_chunk(content, chunk_lines=150, overlap=0.15)` (line 63)

Both paths enforce `_CHUNK_MAX_BYTES = 16_000` via `_enforce_byte_cap()`.

Module-level constants (lines 30–34):
```python
_CHUNK_LINES = 150
_OVERLAP     = 0.15   # 15 % overlap → 22-line windows
_CHUNK_MAX_BYTES = 16_000
```

**`chunk_file()` currently accepts no chunk_size parameter** — it reads the module constants directly.

### R2: Call chain from CLI → chunker (Confirmed)

```
nx index repo PATH
  commands/index.py:index_repo_cmd()           # no chunk params today
    indexer.py:index_repository()              # no chunk params today
      indexer.py:_run_index()
        indexer.py:_index_code_file()          # no chunk params today
          chunker.py:chunk_file(file, content) # reads module constants
```

Markdown prose uses `SemanticMarkdownChunker(chunk_size=512, chunk_overlap=50)` (tokens, `md_chunker.py:58`).
Other prose uses `_line_chunk()` with the same 150-line default.
PDFs use `PDFChunker(chunk_chars=1500, overlap_percent=0.15)` (`pdf_chunker.py:22`).

### R3: Scope of change for `--chunk-size` / `--chunk-overlap` (Confirmed)

Five touch-points to thread the parameters through:
1. `commands/index.py:index_repo_cmd()` — add Click options
2. `indexer.py:index_repository()` — add `chunk_lines`/`chunk_overlap` kwargs
3. `indexer.py:_run_index()` — pass through to file dispatchers
4. `indexer.py:_index_code_file()` — pass to `chunk_file()`
5. `chunker.py:chunk_file()` — accept and forward to `CodeSplitter` / `_line_chunk()`

Markdown and PDF chunkers use different units (tokens and chars); they are **not** in scope for the initial implementation — `--chunk-size` applies to **code files only** (contrary to the Implementation Notes in the Proposed Solution which says "prose/docs files use the same values").

### R4: Byte-limit guard (Confirmed)

At 4 bytes/token and typical prose, 150 lines ≈ 600–900 tokens ≈ 2,400–3,600 bytes — well under 16 KB.
Maximum safe `--chunk-size` is ≈ 4,000 lines (very conservative; in practice a 4,000-line Python file is rare). The validation upper bound should be `4_000` lines with a warning at `> 500`.

### R5: `--chunk-size` semantics clarification

The RDR proposes `--chunk-size 150` as an example for "tighter chunks" — but 150 **is the current default**. The Validation table re-indexes with `--chunk-size 150` expecting improvement, which contradicts the claim that 150-line chunks are the problem.

**Likely intent:** the problem is actually that large files produce very long chunks even at 150 lines (a 3,000-line file makes 20 chunks of 150 lines, each covering many unrelated symbols). The fix is a **smaller** chunk size, e.g. 50–80 lines. The Validation section should use a value like `--chunk-size 60` or `--chunk-size 80`, not 150.

This needs clarification before implementation (see Open Questions).

### R6: `--force` semantics for re-index (Assumed)

The RDR says specifying `--chunk-size` "implies `--force` semantics for that collection." Currently `nx index repo` is incremental — it skips files whose `content_hash` hasn't changed. Re-chunking at a new size requires full re-index because the chunk IDs embed line ranges and must be regenerated. **Implementation must clear/recreate the collection when chunk_size differs from the indexed default.** Mechanism: delete-and-recreate the collection before indexing.

### R7: `chunk_count` metadata available for filtering and scoring (Confirmed)

**Source:** `src/nexus/indexer.py:269`, `src/nexus/db/t3.py:306–366`, `src/nexus/scoring.py`

`chunk_count` is stored on every code chunk at index time. The `search()` method spreads all
metadata into results, so `chunk_count` is available in every `SearchResult.metadata`.

`where` filters are fully supported and passed directly to ChromaDB:
```python
# Hard filter: exclude files with > 20 chunks
t3.search(query, collections, where={"chunk_count": {"$lte": 20}})
```

The `apply_hybrid_scoring()` function in `scoring.py` already receives full `SearchResult`
objects including metadata — adding a `file_size_factor` requires only adding the penalty
formula and one multiply at line 76.

### R8: Scoring pipeline integration point (Confirmed)

**Source:** `src/nexus/scoring.py:40–80`

The penalty slot is at line 76 inside `apply_hybrid_scoring()`:
```python
# Current (line 76):
r.hybrid_score = hybrid_score(v_norm, f_norm)

# Proposed:
chunk_count = r.metadata.get("chunk_count", 1)
size_factor = min(1.0, _FILE_SIZE_THRESHOLD / max(1, chunk_count))
r.hybrid_score = hybrid_score(v_norm, f_norm) * size_factor
```

The Voyage reranker (`rerank_results()`, lines 100–134) runs after `apply_hybrid_scoring()`
and overwrites `hybrid_score` with its own `relevance_score`. This means the size penalty
affects **which chunks enter the reranker's candidate window** (via pre-sort and top-K
selection) but not the final reranker scores. This is the correct integration point.

## Open Questions (Resolved)

1. **What chunk size fixes the failing queries?** *(Open — empirical)* The Validation table
   must use `small` (60 lines) not `default` (150) — spec defect confirmed in R5. Empirical
   validation against the Arcaneum collection is part of the Validation section.
2. **Scope for prose/docs:** *(Resolved)* **Code only.** Markdown and PDF chunkers are out of
   scope for this RDR.
3. **Collection invalidation:** *(Resolved)* **Require explicit `--force`.** Users must pass
   `--force` when specifying a non-default chunk size. Silent auto-clear is too destructive.

## Validation

**Track A (scoring penalty):** Enable the `file_size_factor` penalty and verify on the
existing `code__arcaneum` collection (no re-index required) that the following queries return
the canonical file as a top-3 result.

**Track B (chunk size):** Re-index `code__arcaneum` with `--chunk-size small --force` and
verify the same queries:

| Query | Expected canonical file |
|-------|------------------------|
| `"embedding model GPU acceleration FastEmbed"` | `embeddings/client.py` |
| `"chunk overlap tokenizer source code indexing"` | `indexing/markdown/chunker.py` |
| `"MeiliSearch full text search index documents"` | `fulltext/client.py` |
| `"class EmbeddingClient"` | `embeddings/client.py` |
| `"class SourceCodePipeline"` | `indexing/source_code_pipeline.py` |
