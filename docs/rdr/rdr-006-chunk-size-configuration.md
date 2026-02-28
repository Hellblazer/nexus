---
title: "Chunk Size Configuration for nx index repo"
type: feature
status: draft
priority: P2
author: Hal Hildebrand
date: 2026-02-28
related_issues: []
---

# RDR-006: Chunk Size Configuration for `nx index repo`

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

Expose `--chunk-size` (and optionally `--chunk-overlap`) as options on `nx index repo`,
passing them through to the underlying code indexer.

### Interface

```bash
nx index repo <path> [--chunk-size INT] [--chunk-overlap INT]

# Examples
nx index repo .                          # default (current behaviour)
nx index repo . --chunk-size 150         # tighter chunks, higher precision
nx index repo . --chunk-size 150 --chunk-overlap 20
```

### Behaviour

- `--chunk-size` controls the target token count per chunk (default: current indexer default,
  likely 400)
- `--chunk-overlap` controls token overlap between adjacent chunks (default: current indexer
  default)
- Both parameters are forwarded to the code indexer; prose/docs files use the same values
- When `--chunk-size` is specified, the collection is re-indexed from scratch for the code
  collection (implies `--force` semantics for that collection)

### Implementation Notes

`nx index repo` delegates to the internal `IndexRepo` pipeline in
`src/nexus/commands/index.py` (or equivalent). The chunk size needs to be threaded through
to wherever `chromadb_index_code()` (or equivalent) splits file content into chunks before
embedding.

The ChromaDB Cloud hard limit on document size is 16,384 bytes (RDR-005). Chunk size
validation should guard against values that could produce chunks exceeding this limit given
typical token-to-byte ratios (~4 bytes/token → max safe chunk size ≈ 4,000 tokens).

## Alternatives Considered

### Re-index with `--force` (rejected)
Re-embedding with identical chunk sizes produces identical embeddings and identical precision.
No improvement.

### `--hybrid` search mode (insufficient)
Tested: hybrid search (semantic + ripgrep) returned the same files as semantic-only for the
failing queries. The problem is that the broad chunks score high semantically; ripgrep
re-ranking does not overcome this.

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

## Open Questions

1. **What chunk-size actually fixes the failing queries?** The Validation table uses `--chunk-size 150` (current default). Should it be 60 or 80? Needs empirical testing against the Arcaneum collection.
2. **Scope for prose/docs:** Should `--chunk-size` also control `SemanticMarkdownChunker` and `PDFChunker`? They use different units (tokens/chars vs lines). Recommend separate flags (`--prose-chunk-size`, `--pdf-chunk-size`) or restricting to code only for v1.
3. **Collection invalidation:** When chunk size changes, should `nx index repo` warn + require `--force`, or auto-clear? Auto-clear is safer UX but destructive.

## Validation

Re-index `code__arcaneum` with `--chunk-size 150` and verify the following queries return
the canonical file as a top-3 result:

| Query | Expected canonical file |
|-------|------------------------|
| `"embedding model GPU acceleration FastEmbed"` | `embeddings/client.py` |
| `"chunk overlap tokenizer source code indexing"` | `indexing/markdown/chunker.py` |
| `"MeiliSearch full text search index documents"` | `fulltext/client.py` |
| `"class EmbeddingClient"` | `embeddings/client.py` |
| `"class SourceCodePipeline"` | `indexing/source_code_pipeline.py` |
