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

`nx search` already provides basic context windowing via `-A N` and `-C N` flags (search_cmd.py:92-95), which show the first 1+N lines of each chunk through `format_plain_with_context` (formatters.py:54-78). However, these flags lack several capabilities users expect from a grep-like experience:

1. **No keyword-matching line identification**: The current `-A`/`-C` always window from the first line of the chunk, not from the most relevant line. If the matching term appears at line 80 of a 150-line chunk, `-A 3` still shows lines 1-4.
2. **No `-B` (lines before match)**: There is no way to show context lines before a matching line.
3. **No bridge merging**: When two nearby matches within a chunk are separated by 1-2 lines, there is no mechanism to merge them into a single contiguous block.
4. **No syntax highlighting**: Results are plain text with no language-aware coloring.

Two proven UX improvements from SeaGOAT and standard CLI tools address these gaps:

1. **Smarter context lines**: Identify the best-matching lines within a chunk, then apply `-A`, `-B`, `-C` windowing around those lines (not the chunk start)
2. **Syntax highlighting** (via `bat`): When `bat` is installed, pipe results through it for language-aware syntax coloring with line ranges

## Context

- Current formatters (`formatters.py`) display chunks in `path:line_no:content` format (one line per content line, no score displayed)
- `-A N` and `-C N` exist but `-C` is implemented as an alias for `-A` (after-context only), which deviates from grep's `-C N` meaning (before AND after)
- `format_plain_with_context` operates on `r.content` (chunk text from ChromaDB) and has no access to the original source file or the query string
- SeaGOAT (`seagoat/result.py:51-73`) implements `ResultLine` dataclass with types: RESULT, CONTEXT, BRIDGE — tracks per-line scores and merges nearby blocks
- SeaGOAT (`seagoat/utils/cli_display.py:71-132`) detects `bat` via `subprocess.run(["bat", "--version"])` with `FileNotFoundError` handling, and uses `bat {full_path} --file-name {file_name} --paging never --line-range {start}:{end}` for syntax highlighting
- Chunks already carry `line_start`/`line_end` metadata (fixed in RDR-016)

## Research Findings

### F1: Line-Level Scoring Within Chunks (Design needed)

Unlike SeaGOAT which stores line-level embeddings, nexus stores chunk-level embeddings. To identify the most relevant lines within a chunk, options:

a. **Keyword highlighting**: Use the query terms to find matching lines within the chunk (cheap, works for literal matches)
b. **Embedding sub-scoring**: Embed individual lines and compare to query (expensive, defeats the purpose of chunking)
c. **Heuristic center**: Show the middle N lines of the chunk (simple, often wrong)
d. **Combined**: Use keyword matching when available (especially with RDR-026 hybrid search), fall back to showing the first N lines

Recommendation: Option (d) — keyword match when available, first-N-lines fallback.

### F2: bat Integration (SeaGOAT source review)

SeaGOAT's implementation at `cli_display.py:101-132`:
1. Check if `bat` is installed via `subprocess.run(["bat", "--version"])`, catching `FileNotFoundError` (lines 101-111)
2. Call `bat {full_path} --file-name {file_name} --paging never --line-range {start}:{end}` (lines 114-132) — no `--style` or `--color` flags are passed; bat uses its own defaults
3. Fall back to Pygments-based highlighting if `bat` is not available (lines 10-31)

This is ~20 lines of code and provides immediate UX improvement.

### F3: Bridge Line Merging (SeaGOAT source review)

When two result lines are separated by 2 or fewer non-matching lines, SeaGOAT bridges them into a single block (gap lines become BRIDGE type). This prevents fragmented output. The algorithm at `result.py:148-177` (`_merge_almost_touching_blocks`) is straightforward.

## Design Decisions

### DD1: `-B` (lines before match) — within-chunk only

`-B N` (lines before match) would ideally read N lines before the match from the original source file. However, the nexus display pipeline operates entirely on chunk text from ChromaDB — `format_plain_with_context` only has access to `r.content` (chunk text), not the source file. Reading from `r.metadata["source_path"]` would introduce file I/O, source-file-not-found failure modes, and content drift (file changed since indexing).

**Decision**: Implement `-B` as within-chunk lines only. If the matching line is at position K within the chunk, `-B N` shows `max(0, K-N)` through `K-1`. If the match is on the first line of the chunk, `-B` produces no additional output. This is a pragmatic limitation that avoids source-file I/O while still being useful for the common case (matches in the middle of multi-line chunks).

### DD2: `-C` semantic — change to before+after (breaking change)

The current `-C N` implementation (search_cmd.py:94-95, 127-128) aliases `-C` to `-A` (after-context only). This deviates from the universal grep convention where `-C N` means N lines before AND after the match. The Test Plan specifies `-C 3` producing `3+1+3` lines (before+match+after), which contradicts the current after-only implementation.

**Decision**: Change `-C N` to mean before+after (equivalent to `-B N -A N`), matching grep semantics. This is a breaking change for any user relying on the current `-C`-as-after-alias behavior, but: (a) the feature is new and unlikely to have dependents, (b) the current behavior is surprising and undocumented as a deviation, and (c) aligning with grep reduces cognitive load. The help text will be updated from "alias for -A N" to "Show N lines of context before and after each match (equivalent to -B N -A N)".

## Proposed Solution

### Phase 1: Extend Context Lines

Extend the existing `-A`/`-C` implementation with keyword-matching line identification, `-B` support, and bridge merging:
- `-A N`: Show N lines after each matching line (currently shows N lines after chunk start; change to match-relative)
- `-B N`: Show N lines before each matching line (within-chunk only; see DD1)
- `-C N`: Show N lines before and after each matching line (changed from after-alias to before+after; see DD2)
- Default (no flags): show entire chunk (backward compatible)
- Line identification: keyword match against query terms to highlight those lines; fall back to first line of chunk
- Bridge gaps of <=2 lines between nearby matches

### Phase 2: Syntax Highlighting
Add `--bat` flag to `nx search`:
- Detect `bat` availability via `subprocess.run(["bat", "--version"])` with `FileNotFoundError` handling
- Pipe each result through `bat` with appropriate `--file-name` for language detection and `--paging never`
- Respect `--no-color` / `NO_COLOR` environment variable

### Phase 3: Compact Mode
Add `--compact` flag:
- Show only file path, line number, and the single best-matching line (like grep output)
- Useful for piping into other tools

## Alternatives Considered

**A. Always show full chunks**: Current default behavior. Too verbose for scanning. Users read 150 lines to find 3 relevant ones.

**B. Truncate chunks to N lines**: Loses context. If the match is at line 140 of a 150-line chunk, you'd never see it.

**C. Integrate with `fzf`**: Interactive fuzzy filtering of results. Good for interactive use but doesn't help non-interactive workflows. Could be added as `--fzf` flag in a future phase.

**D. Read source files for `-B`**: Would allow `-B` to show lines before the chunk boundary, but adds file I/O, staleness risk, and `FileNotFoundError` handling. Rejected in favor of within-chunk-only (DD1).

## Trade-offs

**Benefits**:
- Results become scannable (grep-like experience developers expect)
- `-C` aligns with grep semantics, reducing cognitive load
- Syntax highlighting is proven to improve code comprehension speed
- All flags are additive — default behavior (no flags) is unchanged

**Risks**:
- `-C` semantic change is a breaking change (but feature is new, low impact)
- Line identification heuristic may highlight wrong lines for semantic-only queries (no keyword overlap)
- `-B` limited to within-chunk context (no pre-chunk lines)
- `bat` adds subprocess overhead (~50ms per result)

## Implementation Plan

1. Extend `format_plain_with_context` signature to accept `query: str | None = None`; pass query from `search_cmd` to formatter
2. Add `_find_matching_lines(chunk_text, query)` -> list of line numbers (keyword match with first-line fallback)
3. Add `_extract_context(lines, matches, before, after)` -> focused line blocks with bridge merging (within-chunk only)
4. Add `-B` flag to `nx search` CLI
5. Change `-C N` from after-alias to before+after (`-B N -A N`), update help text
6. Add `_format_with_bat(file_path, line_start, line_end)` -> syntax-highlighted output
7. Add `--bat` flag to `nx search` CLI
8. Add `--compact` flag for single-line-per-result output
9. Update `format_plain` and `format_vimgrep` formatters

## Test Plan

- Unit: `_find_matching_lines` with known query/chunk pairs
- Unit: `_extract_context` with various before/after/bridge scenarios
- Unit: `-B` on match at chunk start produces no pre-context (within-chunk boundary)
- Unit: bat detection when bat not installed -> graceful fallback
- Integration: search with `-C 3` produces exactly 3+1+3 lines per match (before+match+after)
- Integration: `-C N` is equivalent to `-B N -A N`
- Integration: `--compact` produces grep-compatible output format
- Regression: search with no flags produces identical output to current behavior

## Finalization Gate

### Contradiction Check
The `-C` semantic contradiction between the current implementation (after-alias) and grep convention (before+after) is resolved in DD2: `-C` will be changed to before+after, matching grep and the Test Plan's `3+1+3` expectation. The Problem Statement no longer falsely claims `-A`/`-C` don't exist — it acknowledges them and identifies the specific gaps (no keyword-matching, no `-B`, no bridge merging). The format_plain output description is corrected to `path:line_no:content` (no score).

### Assumption Verification
Key assumption validated: `format_plain_with_context` operates solely on `r.content` (chunk text from ChromaDB) with no source-file access. This is confirmed by reading formatters.py:54-78. The `-B` design decision (DD1) explicitly addresses this constraint by limiting to within-chunk context. The query string is not currently available in the formatter — Implementation Plan step 1 adds this parameter threading.

### Scope Verification
Phase 1 is scoped to extending the existing `-A`/`-C` implementation, not building from scratch. The formatter signature change (adding `query` parameter) and `-C` semantic change are the two interface-level breaking changes, both documented. `-B` is new but constrained to within-chunk. Bridge merging is additive. Phase 2 (bat) and Phase 3 (compact) remain independent.

### Cross-Cutting Concerns
The `query` parameter must be threaded from `search_cmd.search_cmd()` through to `format_plain_with_context`. This touches the call site at search_cmd.py:225-227 and the formatter signature at formatters.py:54-56. No other callers of `format_plain_with_context` exist in the codebase. The `-C` semantic change requires updating the Click help text at search_cmd.py:94-95 and the alias logic at search_cmd.py:127-128.

### Proportionality
This is a P2 UX enhancement. Phase 1 (context lines with keyword matching) is the highest-value change and can land independently. The design decisions (DD1: within-chunk `-B`, DD2: `-C` as before+after) are pragmatic choices that avoid over-engineering while delivering grep-familiar behavior. Phase 2 (bat) is ~20 lines of subprocess plumbing. Phase 3 (compact) is a formatting variant. Total scope is proportionate to the improvement in daily search ergonomics.

## References

- SeaGOAT result.py: `/Users/hal.hildebrand/git/SeaGOAT/seagoat/result.py`
- SeaGOAT cli_display.py: `/Users/hal.hildebrand/git/SeaGOAT/seagoat/utils/cli_display.py`
- nexus formatters.py: `src/nexus/formatters.py`
- nexus search_cmd.py: `src/nexus/commands/search_cmd.py`
