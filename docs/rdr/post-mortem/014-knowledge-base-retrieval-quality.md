---
rdr: RDR-014
title: "Post-Mortem: Knowledge Base Retrieval Quality — Code Context and Docs Deduplication"
date: "2026-03-02"
author: Hal Hildebrand
prs: ["#57 (P1 fixes: dedup, context prefix, AST infra)", "#58 (P1+P2: code block preservation, AST expansion)"]
---

# Post-Mortem: RDR-014 — Knowledge Base Retrieval Quality

## Summary

RDR-014 identified two concrete retrieval quality defects and specified their fixes.
Both fixes were implemented as part of the RDR-015 execution (PRs #57 and #58).
The research and design process was notably effective: all three major design
recommendations made during research were validated correct by implementation and
code review. The RDR also had an outsized consequence — auditing arcaneum's codebase
to settle the "regex vs tree-sitter" question surfaced the broader pipeline gap that
became RDR-015.

---

## What Was Planned vs. What Was Implemented

### Fix 1 — Code chunk context prefix injection

**Planned:** Prepend `// File: path  Class: X  Method: Y  Lines: N-M` to each chunk's
embedded text only (raw text stored in ChromaDB). Use tree-sitter `DEFINITION_TYPES`
from arcaneum to extract class/method context for 14 languages.

**Implemented:** Exactly as planned. `_extract_context(source, language, start, end)`
written from scratch — this function did not exist in arcaneum. `DEFINITION_TYPES`
and `_extract_name_from_node()` copied from arcaneum. Separate `embed_texts` / `documents`
lists maintained in `_index_code_file()`, no interface changes to `t3.py`.

**Divergence (minor):** Comment character (`//` vs `#`) is selected per language as
designed, but the prefix format was simplified to `File: path  Lines: N-M` when no
class/method context is available, rather than omitting the prefix entirely. This is
strictly better — even file+lines context improves embedding quality.

### Fix 2 — SemanticMarkdownChunker deduplication

**Planned:** Add `_STRUCTURAL_TOKEN_TYPES` blocklist to `_build_sections()`, skip
structural open/close tokens that have `.map` but no content of their own.

**Implemented:** Exactly as planned. `_STRUCTURAL_TOKEN_TYPES` frozenset added, token
check added to `_build_sections()`. Code review (C3) caught one additional case:
`list_item_open` was missing from the initial `_STRUCTURAL_TOKEN_TYPES`. Added before
merge. Final blocklist is complete for all CommonMark structural tokens.

**Divergence (Fix C2, unplanned addition):** `preserve_code_blocks` option added to
`SemanticMarkdownChunker` (PR #58). This was listed as P2 in RDR-015, not RDR-014,
but was implemented together as the markdown chunker was already being edited.

---

## Key Research Decisions — All Vindicated

### R7: Tree-sitter over regex (HIGH confidence, confirmed correct)

The original Fix 1 proposal used a 50-line backwards regex scan for Java class/method
extraction. Research (R7) recommended tree-sitter instead: already a pinned dependency,
covers 14 languages via arcaneum's `DEFINITION_TYPES`, handles generics/annotations/
inner classes correctly. Confidence was rated 90%+.

Code review confirmed: the regex approach would have had silent failures on decorated
Python functions (`decorated_definition` nodes have no direct identifier child — the
name is on the inner `function_definition`). The `_extract_context` implementation
with `_extract_name_from_node()` recursion handles this correctly. The regex could not
have done so without language-specific special-casing.

**Test added:** `test_extract_context_decorated_python_function` (Fix E5) — this exact
case where `@staticmethod def process(data):` should return `("MyService", "process")`.

### R9: Embed-only prefix, store raw text (confirmed correct)

Two concrete regressions from storing the prefix were identified during research:
1. `format_vimgrep` uses `content.splitlines()[0]` — the prefix line would become
   the editor jump target instead of the first line of code
2. `-c` 200-char preview loses ~65 chars to the prefix

Both were validated by code review (findings I2, I4) before any real-world consequence.
The separate `embed_texts` / `documents` list approach requires no `t3.py` interface
changes and was confirmed in the implementation.

### R10: Blocklist over allowlist for Fix 2 (confirmed correct)

Research recommended blocklist (`_STRUCTURAL_TOKEN_TYPES`) over allowlist because
allowlist silently drops unknown future token types (math blocks, footnotes) if
`mdit_py_plugins` is added later. Blocklist correctly defaults to INCLUDE for
new token types.

Code review identified that the initial implementation was missing `list_item_open`
(finding C3). With blocklist approach, this was caught immediately by tests and fixed
before merge. With an allowlist, `list_item_open` content would have been silently
dropped — a regression that would have been invisible until markdown with bulleted
lists was searched.

---

## Code Review Findings (PRs #57 / #58)

Five findings from code review were fixed before merge:

| Finding | Description | Fix |
|---------|-------------|-----|
| C1 | `_extract_name_from_node` did not recurse into `decorated_definition` inner node | Added recursion into `function_definition` child |
| C3 | `list_item_open` missing from `_STRUCTURAL_TOKEN_TYPES` | Added to blocklist |
| I2 | `embed_texts` prefix used wrong comment char for Python (used `//` instead of `#`) | Fixed comment char selection logic |
| I4 | Prefix stored in ChromaDB `documents` in an intermediate commit | Separated `embed_texts`/`documents` lists correctly |
| S3 | `_extract_context` swallowed all exceptions silently | Narrowed exception scope; only `Exception` from tree-sitter parse caught |

Finding C1 is notable: the regex approach would have had the same `decorated_definition`
bug with no test to catch it, and it would have been language-specific (Python only).
The tree-sitter approach made the bug visible through the Fix E5 test suite.

---

## Serendipitous Consequence: RDR-015

The research for R7 (tree-sitter vs. regex) required auditing arcaneum's `ast_extractor.py`.
This audit revealed:
- `DEFINITION_TYPES` existed in arcaneum as of 2026-01-15 — five weeks before nexus was initialized
- Nexus had independently built its own, less complete pipeline without consulting arcaneum's work
- AST language coverage: nexus 16 extensions vs arcaneum 53
- Internal inconsistency: `chunker.py:AST_EXTENSIONS` (16 entries) ≠ `indexer.py:_EXT_TO_LANGUAGE` (23 entries)

This led directly to RDR-015 (Indexing Pipeline Rethink), which governed the scope of
the full alignment effort. RDR-014's research phase was the trigger.

---

## What Went Well

- All three major design decisions in the research phase were correct.
- Code review caught real bugs (C1, C3) before merge — the TDD approach meant failing
  tests immediately confirmed the fixes were necessary.
- The `_extract_context()` function, written from scratch (no arcaneum equivalent),
  handles multi-method spans correctly by design: `methods[-1]` after a walk means the
  innermost fully-enclosing method; empty list means no single method encloses the chunk.
- Fix E5 tests (`test_indexer_chunk_flow.py`) drive the entire context extraction path
  end-to-end with real tree-sitter parsing — these tests would catch regressions in
  any of the 14 supported languages.
- The `preserve_code_blocks` fix (Fix C2) was bundled with the markdown deduplication
  work cleanly, since the same code path was being modified.

## What Could Be Improved

- **The regex approach should have been eliminated earlier.** R7 is 40 lines of research
  notes. The tree-sitter approach was obvious in retrospect given it was already a
  production dependency. The RDR process should encourage checking existing dependencies
  for capability before proposing new solutions.
- **Inner class / decorated function cases should be in the initial test specification.**
  Fix E5 added `test_extract_context_decorated_python_function` after code review caught
  C1. This test should have been in the RDR's Fix E test table from the start —
  decorated functions and inner classes are common patterns.

---

## Timeline

| Event | Commit / PR |
|-------|-------------|
| RDR-014 created, researched | docs commits |
| R7 research triggers RDR-015 audit | in-session |
| RDR-014 gated, accepted | docs commits |
| Fix 2 (dedup blocklist) + Fix E early tests | PR #57 (`a5d...`) |
| Fix A (DEFINITION_TYPES + `_extract_context`) + Fix E5 | PR #57 |
| Fix C2 (preserve_code_blocks) + Fix B (AST expansion) | PR #58 |
| Code review C1, C3, I2, I4, S3 fixed | `d255340` |

---

## Artifacts

- **PR #57**: Fix 2 (dedup), Fix A (`_extract_context`, DEFINITION_TYPES), Fix E5 tests
- **PR #58**: Fix C2 (`preserve_code_blocks`), Fix B (AST expansion), code review fixes
- **Implementation:** `src/nexus/indexer.py` (Fix 1, Fix A), `src/nexus/md_chunker.py` (Fix 2, Fix C2), `src/nexus/chunker.py` (Fix B)
- **Tests:** `tests/test_indexer_chunk_flow.py` (Fix E5), `tests/test_md_chunker_semantic_integrity.py` (Fix E)
- **Superseded by RDR-015** for the broader pipeline direction; implementation was absorbed into RDR-015 execution
