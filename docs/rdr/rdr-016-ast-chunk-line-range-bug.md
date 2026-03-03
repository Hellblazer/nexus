---
title: "AST Chunk Line Range Bug: CodeSplitter Returns Empty Metadata, Breaking Context Prefix"
type: bug
status: open
priority: P1
author: Hal Hildebrand
date: 2026-03-03
related_issues: []
---

# RDR-016: AST Chunk Line Range Bug: CodeSplitter Returns Empty Metadata, Breaking Context Prefix

## Problem Statement

Every code chunk embedded via the AST path shows an identical, uninformative context prefix:

```
# File: src/foo/Bar.java  Class:   Method:   Lines: 1-780
```

Two defects are visible:

1. **`Class:` and `Method:` are always blank** — `_extract_context()` cannot find any enclosing definition.
2. **`Lines:` always shows the full file extent** — every chunk from the same file reports the same range.

This means the embed-only prefix (added in RDR-015 to improve recall for algorithm-level queries) provides **zero discriminative signal**. All chunks from a given file look identical to Voyage AI's embedding model. Search quality on code collections is materially degraded.

Confirmed on:
- `code__arcaneum-2ad2825c` (Python) — search for "semantic markdown chunking preserve code blocks" returned irrelevant `cli/fulltext.py` fragments
- `code__ART-8c2e74c0` (Java) — all observed prefixes show empty Class/Method
- Expected to affect all 18 AST-chunked languages (see scope below)

## Root Cause

**Single root cause, single file: `src/nexus/chunker.py` lines 210–212.**

### The Chain

1. `chunk_file()` enters the AST path for any extension in `AST_EXTENSIONS` (28 mappings, 18 languages).
2. `_make_code_splitter()` calls `llama_index.core.node_parser.CodeSplitter.get_nodes_from_documents()`.
3. **`CodeSplitter` returns nodes with `node.metadata = {}`** — it never populates `line_start` or `line_end`. This is confirmed empirically (192-line Python file → 4 nodes, every `metadata = {}`).
4. The current fallback at `chunker.py:210–212`:
   ```python
   meta.setdefault("line_start", 1)
   meta.setdefault("line_end", len(content.splitlines()))
   ```
   fires for every node, assigning every chunk the full-file extent (e.g., `line_start=1`, `line_end=780`).
5. `_index_code_file()` calls:
   ```python
   class_ctx, method_ctx = _extract_context(
       source_bytes, language, chunk["line_start"] - 1, chunk["line_end"] - 1
   )
   ```
   With `chunk_start_0idx=0` and `chunk_end_0idx=779`, the enclosing-definition condition
   (`node_start <= chunk_start AND node_end >= chunk_end`) cannot be satisfied by any
   class or method — none of them span the entire file. Both names return `""`.
6. The prefix is built with the wrong values:
   ```python
   f"# File: {rel_path}  Class: {class_ctx}  Method: {method_ctx}  Lines: {chunk['line_start']}-{chunk['line_end']}"
   # → "# File: src/foo/Bar.java  Class:   Method:   Lines: 1-780"
   ```

### What Is Correct

- `_extract_context()` itself is correct — all 5 unit tests in `tests/test_indexer_chunk_flow.py` pass when fed proper line ranges.
- `DEFINITION_TYPES` node-type tables are correct for all 14 languages.
- `_extract_name_from_node()` correctly handles decorated definitions, field-name API, and fallback scanning.
- The **line-based fallback path** (`_line_chunk()`) is unaffected — it correctly tracks `(line_start, line_end, text)` per chunk.

### Key Observation

`CodeSplitter` nodes **do** expose accurate character offsets via `node.start_char_idx` and `node.end_char_idx` (a llama-index `TextNode` attribute). Converting character offset to line number is `content[:start_char_idx].count('\n') + 1`. This gives exact 1-indexed line positions without any text-search heuristics.

## Scope

Affects all 18 languages routed through the AST path:

| Language | Extensions |
|----------|-----------|
| Python | `.py` |
| JavaScript | `.js`, `.jsx` |
| TypeScript | `.ts`, `.tsx` |
| Java | `.java` |
| Go | `.go` |
| Rust | `.rs` |
| C | `.c`, `.h` |
| C++ | `.cpp`, `.cc`, `.cxx`, `.hpp` |
| C# | `.cs` |
| Ruby | `.rb` |
| PHP | `.php` |
| Swift | `.swift` |
| Kotlin | `.kt` |
| Scala | `.scala` |
| Lua | `.lua` |
| Objective-C | `.m` |
| Bash | `.sh` |
| R | `.r` |

Unaffected: `.cl`, `.proto`, `.glsl`, `.wgsl`, `.hlsl`, `.metal`, `.frag`, `.vert`, `.comp` — these go through the line-based fallback.

## Proposed Fix

**Single change in `src/nexus/chunker.py`, `chunk_file()`, lines 208–214.**

Replace the `setdefault` fallback with character-offset-derived line ranges:

```python
# BEFORE (broken):
for i, node in enumerate(nodes):
    meta = {**base_meta, **node.metadata, "ast_chunked": True, "chunk_index": i, "chunk_count": count}
    meta.setdefault("line_start", 1)
    meta.setdefault("line_end", len(content.splitlines()))
    meta["text"] = node.text
    result.append(meta)

# AFTER (fixed):
for i, node in enumerate(nodes):
    meta = {**base_meta, **node.metadata, "ast_chunked": True, "chunk_index": i, "chunk_count": count}
    # CodeSplitter.get_nodes_from_documents() always returns metadata={} —
    # no line_start/line_end. Derive actual per-chunk line numbers from the
    # TextNode character offsets (start_char_idx is populated and accurate).
    if node.start_char_idx is not None:
        line_start = content[:node.start_char_idx].count('\n') + 1
    else:
        line_start = 1
    line_end = line_start + len(node.text.splitlines()) - 1
    meta["line_start"] = line_start
    meta["line_end"] = line_end
    meta["text"] = node.text
    result.append(meta)
```

No changes required anywhere else. `_enforce_byte_cap()` will automatically receive correct `line_start` values and compute correct sub-chunk offsets.

## Tests Required

- `test_chunk_file_ast_line_ranges` — verify `line_start`/`line_end` per chunk are correct for a multi-class Python file chunked via AST.
- `test_chunk_file_ast_class_method_nonempty` — verify that `_extract_context()` downstream returns non-empty class/method names.
- `test_chunk_file_java_line_ranges` — same for Java (requires `java` parser available in test env).
- Existing `test_indexer_chunk_flow.py` tests must continue to pass.

## Remediation Steps

1. Fix `chunk_file()` in `src/nexus/chunker.py` (8-line change).
2. Write TDD tests (red → green).
3. Force-reindex affected repos (delete `code__` collection, re-run `nx index repo`):
   - `code__arcaneum-2ad2825c`
   - `code__ART-8c2e74c0`
   - `code__Luciferase-f2d57dbc`
   - Any other `code__*` collections built with nexus ≤ 1.2.0.
4. Validate search quality improvement with before/after queries.

## Investigation Trail

Discovered 2026-03-03 while evaluating arcaneum ingest quality after force-reindex.
Root cause confirmed by:
- Reading `chunker.py:chunk_file()` and `indexer.py:_index_code_file()`
- Empirical probe: `CodeSplitter` on 192-line Python file → 4 nodes, all `metadata={}`
- Confirming `node.start_char_idx` is accurate (maps to correct actual line positions)
- Running existing `test_indexer_chunk_flow.py` — 5/5 pass with correct inputs, proving `_extract_context()` is not at fault
