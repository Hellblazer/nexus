---
title: "Search Results UX — Context Lines and Syntax Highlighting"
id: RDR-027
type: Feature
status: draft
priority: P2
author: Hal Hildebrand
reviewed-by: self
created: 2026-03-08
related_issues: ["RDR-026"]
related_tests: []
implementation_notes: ""
---

# RDR-027: Search Results UX — Context Lines and Syntax Highlighting

## Problem Statement

`nx search` returns entire chunks (up to 150 lines of code or ~1500 chars of prose). This makes results hard to scan, especially when the relevant match is a few lines within a large chunk. Users must mentally grep within each result to find the salient portion.

Two proven UX improvements from SeaGOAT and standard CLI tools:

1. **Context lines** (`-A`, `-B`, `-C` flags like grep): Show only the most relevant lines within a chunk, with configurable context above/below
2. **Syntax highlighting** (via `bat`): When `bat` is installed, pipe results through it for language-aware syntax coloring with line ranges

## Context

- Current formatters (`formatters.py`) display chunks as plain text blocks with file path, line range, and score
- SeaGOAT (`seagoat/result.py:42-229`) implements `ResultLine` with types: RESULT, CONTEXT, BRIDGE — tracks per-line scores and merges nearby blocks
- SeaGOAT (`seagoat/utils/cli_display.py:92-133`) detects `bat` and uses `bat --line-range {start}:{end} --file-name {path}` for syntax highlighting
- Chunks already carry `line_start`/`line_end` metadata (fixed in RDR-016)

## Research Findings

### F1: Line-Level Scoring Within Chunks (Design needed)

Unlike SeaGOAT which stores line-level embeddings, nexus stores chunk-level embeddings. To identify the most relevant lines within a chunk, options:

a. **Keyword highlighting**: Use the query terms to find matching lines within the chunk (cheap, works for literal matches)
b. **Embedding sub-scoring**: Embed individual lines and compare to query (expensive, defeats the purpose of chunking)
c. **Heuristic center**: Show the middle N lines of the chunk (simple, often wrong)
d. **Combined**: Use keyword matching when available (especially with RDR-026 hybrid search), fall back to showing the first N lines

Recommendation: Option (d) — keyword match when available, first-N-lines fallback.

### F2: bat Integration (Verified — SeaGOAT source)

SeaGOAT's implementation at `cli_display.py:92-133`:
1. Check if `bat` is installed via `shutil.which("bat")`
2. Call `bat --line-range {start}:{end} --file-name {path} --style=numbers,changes --color=always`
3. Fall back to plain display if `bat` is not available

This is ~20 lines of code and provides immediate UX improvement.

### F3: Bridge Line Merging (Verified — SeaGOAT source)

When two result lines are separated by 2 or fewer non-matching lines, SeaGOAT bridges them into a single block (gap lines become BRIDGE type). This prevents fragmented output. The algorithm at `result.py:166-205` is straightforward.

## Proposed Solution

### Phase 1: Context Lines
Add `-A` (after), `-B` (before), `-C` (context) flags to `nx search`:
- Default: show entire chunk (backward compatible)
- With flags: extract a focused window around the best-matching lines
- Line identification: keyword match against query terms → highlight those lines
- Bridge gaps of ≤2 lines between nearby matches

### Phase 2: Syntax Highlighting
Add `--bat` flag to `nx search`:
- Detect `bat` availability at startup
- Pipe each result through `bat` with appropriate `--file-name` for language detection
- Respect `--no-color` / `NO_COLOR` environment variable

### Phase 3: Compact Mode
Add `--compact` flag:
- Show only file path, line number, and the single best-matching line (like grep output)
- Useful for piping into other tools

## Alternatives Considered

**A. Always show full chunks**: Current behavior. Too verbose for scanning. Users read 150 lines to find 3 relevant ones.

**B. Truncate chunks to N lines**: Loses context. If the match is at line 140 of a 150-line chunk, you'd never see it.

**C. Integrate with `fzf`**: Interactive fuzzy filtering of results. Good for interactive use but doesn't help non-interactive workflows. Could be added as `--fzf` flag in a future phase.

## Trade-offs

**Benefits**:
- Results become scannable (grep-like experience developers expect)
- Syntax highlighting is proven to improve code comprehension speed
- All flags are additive — no existing behavior changes

**Risks**:
- Line identification heuristic may highlight wrong lines for semantic-only queries (no keyword overlap)
- `bat` adds subprocess overhead (~50ms per result)

## Implementation Plan

1. Add `_find_matching_lines(chunk_text, query)` → list of line numbers
2. Add `_extract_context(lines, matches, before, after)` → focused line blocks with bridge merging
3. Add `-A`, `-B`, `-C` flags to `nx search` CLI
4. Add `_format_with_bat(file_path, line_start, line_end)` → syntax-highlighted output
5. Add `--bat` flag to `nx search` CLI
6. Add `--compact` flag for single-line-per-result output
7. Update `format_plain` and `format_vimgrep` formatters

## Test Plan

- Unit: `_find_matching_lines` with known query/chunk pairs
- Unit: `_extract_context` with various before/after/bridge scenarios
- Unit: bat detection when bat not installed → graceful fallback
- Integration: search with `-C 3` produces exactly 3+1+3 lines per match
- Integration: `--compact` produces grep-compatible output format

## References

- SeaGOAT result.py: `/Users/hal.hildebrand/git/SeaGOAT/seagoat/result.py`
- SeaGOAT cli_display.py: `/Users/hal.hildebrand/git/SeaGOAT/seagoat/utils/cli_display.py`
- nexus formatters.py: `src/nexus/formatters.py`
