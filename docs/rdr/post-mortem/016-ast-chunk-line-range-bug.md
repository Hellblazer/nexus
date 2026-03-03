# Post-Mortem: RDR-016 — AST Chunk Line Range Bug

**RDR**: [RDR-016](../rdr-016-ast-chunk-line-range-bug.md)
**Closed**: 2026-03-03
**Reason**: Implemented
**Fix commit**: `595c807`

---

## What Happened

Every code chunk indexed via the AST path received an identical, uninformative embed-only
context prefix: `# File: src/foo/Bar.java  Class:   Method:   Lines: 1-780`. The Class and
Method fields were always blank; the Lines field always showed the full file extent. This
defeated the purpose of the context prefix introduced in RDR-015.

The bug was discovered during a post-ingest search quality evaluation on
`code__arcaneum-2ad2825c` (Python) and confirmed on `code__ART-8c2e74c0` (Java).

---

## Root Cause

`llama_index.core.node_parser.CodeSplitter.get_nodes_from_documents()` always returns
`node.metadata = {}`. A `setdefault` fallback in `chunk_file()` (chunker.py:210-212)
assigned every chunk the full-file extent:

```python
meta.setdefault("line_start", 1)
meta.setdefault("line_end", len(content.splitlines()))
```

`_extract_context()` received `(0, N-1)` — a span no definition can enclose — and returned
empty strings for both class and method names. The function itself was correct; only the
inputs were wrong.

---

## Fix

Single change in `chunk_file()`: derive per-chunk line ranges from `node.start_char_idx`
(a `TextNode` attribute that IS accurately populated):

```python
if node.start_char_idx is not None:
    line_start = content[:node.start_char_idx].count("\n") + 1
else:
    line_start = 1  # defensive fallback; None not observed empirically
line_end = max(line_start, line_start + len(node.text.splitlines()) - 1)
```

The `max()` guard was added during the gate review — `str.splitlines()` returns `[]` for
empty text, which would produce `line_end < line_start` without it.

**Tests added** (3 new in `tests/test_chunker.py`):
- `test_chunk_file_ast_line_ranges` — core correctness (was RED before fix)
- `test_chunk_file_ast_empty_text_node` — max() guard regression
- `test_chunk_file_ast_none_start_char_idx` — None fallback safety

**Pre-existing mocks updated**: two tests used `MagicMock()` nodes without setting
`start_char_idx`; after the fix `content[:MagicMock()]` would raise `TypeError`. Both
mocks were updated to set `start_char_idx` as integers.

---

## Gate Findings (Incorporated)

The first gate pass returned **BLOCKED** with two critical issues found by the
`substantive-critic` agent:

1. **`line_end` formula**: `line_start + len("".splitlines()) - 1` = `line_start - 1` for
   empty-text nodes. Fixed with `max()` guard.

2. **Existing mocks break after fix**: `MagicMock().start_char_idx` is not an integer;
   `content[:MagicMock()]` raises `TypeError`. The prerequisite of updating mocks before
   implementing was missing from the original RDR. Added to Tests Required section and
   completed during implementation.

Significant issues also caught:
- Scope table missing `.kts`, `.sc`, `.bash` — corrected
- Extension count "28 mappings" → 27 — corrected
- `DEFINITION_TYPES` count "14 languages" → 16 — corrected
- `_enforce_byte_cap()` inherits broken `line_start=1` if `start_char_idx` is None —
  documented as known limitation

---

## Discovered Remediation Requirement: `--force` Flag

After fixing `chunk_file()`, re-embedding the affected collections revealed a gap: the
staleness check (`content_hash + embedding_model` both match → skip) has no override.
Since file content did not change — only the indexing logic improved — all files were
silently skipped and the collections remained stale.

The only workaround was to delete each collection and re-ingest from scratch:
```
nx collection delete -y code__arcaneum-2ad2825c
nx index repo ~/git/arcaneum
```

This is disruptive (collection empty during reingest) and risky (interrupted run loses
everything). A `--force` flag would bypass the staleness check in-place via upsert,
preserving collection availability.

**This spawned a new epic**: `nexus-dp08` — `--force` flag on all four `nx index`
subcommands (`repo`, `pdf`, `md`, `rdr`).

**Implementation plan**: `docs/plans/2026-03-03-force-reindex-impl-plan.md`

**Beads**:
- nexus-jazw (Phase 1: indexer.py leaf helpers)
- nexus-mj98 (Phase 2: doc_indexer.py pipeline)
- nexus-5aoy (Phase 3: orchestration threading)
- nexus-wk85 (Phase 4: CLI flags)

---

## Affected Collections (still needing re-index after `--force` ships)

| Collection | Repo | Language | Bead |
|-----------|------|----------|------|
| `code__arcaneum-2ad2825c` | ~/git/arcaneum | Python | nexus-4iti |
| `code__ART-8c2e74c0` | ~/git/ART | Java | nexus-4iti |
| `code__Luciferase-f2d57dbc` | ~/git/Luciferase | Python | nexus-4iti |

Once `--force` ships: `nx index repo ~/git/arcaneum --force` etc.

---

## Scope Confirmed

Affected all 18 AST-chunked languages. Line-based fallback (`_line_chunk`) was unaffected.
`_extract_context()` itself was correct throughout — the bug was entirely in the inputs
fed to it.

---

## Timeline

| Time | Event |
|------|-------|
| 2026-03-03 | Discovered during arcaneum search quality evaluation |
| 2026-03-03 | Root cause confirmed (empirical CodeSplitter probe) |
| 2026-03-03 | RDR-016 created, gated (BLOCKED → fixed → PASSED), accepted |
| 2026-03-03 | Fix implemented, 3 tests added, all 1790 suite tests passing |
| 2026-03-03 | `--force` gap discovered, nexus-dp08 epic created |
| 2026-03-03 | RDR-016 closed |
