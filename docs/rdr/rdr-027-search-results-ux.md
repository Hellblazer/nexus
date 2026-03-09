---
title: "Search Results UX — Context Lines and Syntax Highlighting"
id: RDR-027
type: Feature
status: closed
accepted_date: 2026-03-09
close_date: 2026-03-09
close_reason: implemented
priority: P2
author: Hal Hildebrand
reviewed-by: self
created: 2026-03-08
related_issues: ["RDR-026"]
related_tests: ["tests/test_formatters.py"]
implementation_notes: "All 3 phases in PR #91 — Phase 1 (context lines, -B, -C fix, bridge merging), Phase 2 (bat highlighting), Phase 3 (compact mode)"
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
- Detect `bat` availability via `subprocess.run(["bat", "--version"])` with `FileNotFoundError` handling; cache result for the session
- Batch results by `source_path`: group all result blocks for the same file, then call `bat` once per file with multiple `--line-range {start}:{end}` arguments. This avoids O(N) subprocess spawns for N results from the same file. Before constructing the bat command, sort line ranges by start and merge overlapping/adjacent ranges into contiguous spans (if `end[i] >= start[i+1] - 1`, merge them) to prevent duplicate output lines.
- **Line range source**: When no context flags (`-A`/`-B`/`-C`) are set, bat receives `--line-range {line_start}:{line_end}` from chunk metadata. When context flags are active, `_format_with_bat` receives the pre-computed context blocks (from `_extract_context`) and uses their start/end line numbers for `--line-range`. If `line_end` is absent or 0 in metadata, derive it as `line_start + len(content.splitlines()) - 1`.
- Pass `--file-name {source_path}` for language detection, `--paging never`, `--style=plain`
- **Format interaction**: `--bat` applies only to the default plain output path. When `--json`, `--vimgrep`, or `--files` is active, `--bat` is silently ignored.
- **Fallback behavior**: When `bat` is not installed and `--bat` is explicitly requested, emit a one-time warning ("bat not found; showing plain output") and fall back to `format_plain_with_context`. Do not error.
- **Subprocess error handling**: Catch `subprocess.CalledProcessError` and `OSError` around each `bat` invocation; on failure, fall back to plain formatting for that file and log at debug level via structlog.
- **`--no-color` interaction**: When `--no-color` is passed or `NO_COLOR` env var is set, skip `bat` entirely (bat respects `NO_COLOR` but skipping avoids the subprocess overhead). Remove the `--no-color` flag from being passed through to bat since bat handles `NO_COLOR` natively.

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
- `bat` adds subprocess overhead (~50ms per unique file, mitigated by per-file batching with `--line-range`)

## Implementation Plan

1. Extend `format_plain_with_context` signature to accept `query: str | None = None`; pass query from `search_cmd` to formatter
2. Add `_find_matching_lines(chunk_text: str, query: str, rg_matched_lines: list[int] | None = None, chunk_line_start: int = 0)` -> list of 0-based line indices within the chunk. When `rg_matched_lines` is provided (from RDR-026 hybrid search), translate absolute line numbers to chunk-relative indices and prefer those over keyword matching. Fall back to keyword match against query terms (split on `\W+`, case-insensitive substring match, line matches if any token appears), then to line 0 if no matches found.
3. Add `_extract_context(lines, matches, before, after)` -> focused line blocks with bridge merging (within-chunk only)
4. Add `-B` flag to `nx search` CLI
5. Change `-C N` from after-alias to before+after (`-B N -A N`), update help text
6. Add `_format_with_bat(results: list[SearchResult], context_blocks: dict[str, list[tuple[int,int]]] | None = None)` -> syntax-highlighted output. Groups results by `source_path`, merges overlapping/adjacent line ranges, calls `bat` once per file with deduplicated `--line-range` args. When `context_blocks` is provided (from Phase 1 `_extract_context`), uses those line ranges instead of full chunk metadata. Handles `FileNotFoundError`, `CalledProcessError`, and `OSError` with fallback to plain formatting per file. If `line_end` is absent or 0, derives it as `line_start + len(content.splitlines()) - 1`.
7. Add `--bat` flag to `nx search` CLI. When `--no-color` or `NO_COLOR` is set, skip bat entirely.
8. Add `--compact` flag for single-line-per-result output (format: `{source_path}:{line_no}:{best_matching_line}`)
9. Update `format_vimgrep` to accept `query` parameter and use `_find_matching_lines` for line identification (same signature extension as `format_plain_with_context` in step 1). The vimgrep format becomes `{source_path}:{match_line_no}:{col}:{line_text}` where `match_line_no` is the best-matching line rather than always `line_start`.

## Test Plan

- Unit: `_find_matching_lines` with known query/chunk pairs (keyword match)
- Unit: `_find_matching_lines` with `rg_matched_lines` provided — prefers rg lines over keyword match
- Unit: `_find_matching_lines` with `rg_matched_lines` outside chunk range — falls back to keyword match
- Unit: `_extract_context` with various before/after/bridge scenarios
- Unit: `-B` on match at chunk start produces no pre-context (within-chunk boundary)
- Unit: bat detection when bat not installed -> graceful fallback with warning
- Unit: bat subprocess error (CalledProcessError) -> falls back to plain formatting
- Unit: `_format_with_bat` groups results by source_path (batches per file)
- Integration: search with `-C 3` produces exactly 3+1+3 lines per match (before+match+after)
- Integration: `-C N` is equivalent to `-B N -A N`
- Integration: `--compact` produces grep-compatible output format (`path:line:text`)
- Integration: `--bat` with `NO_COLOR` set skips bat entirely
- Regression: search with no flags produces identical output to current behavior
- Regression: `format_vimgrep` with `query=None` returns `line_start` as match line (backward-compat)

## Finalization Gate

### Contradiction Check
The `-C` semantic contradiction between the current implementation (after-alias) and grep convention (before+after) is resolved in DD2: `-C` will be changed to before+after, matching grep and the Test Plan's `3+1+3` expectation. The Problem Statement no longer falsely claims `-A`/`-C` don't exist — it acknowledges them and identifies the specific gaps (no keyword-matching, no `-B`, no bridge merging). The format_plain output description is corrected to `path:line_no:content` (no score).

### Assumption Verification
Key assumption validated: `format_plain_with_context` operates solely on `r.content` (chunk text from ChromaDB) with no source-file access. This is confirmed by reading formatters.py:54-78. The `-B` design decision (DD1) explicitly addresses this constraint by limiting to within-chunk context. The query string is not currently available in the formatter — Implementation Plan step 1 adds this parameter threading.

### Scope Verification
Phase 1 is scoped to extending the existing `-A`/`-C` implementation, not building from scratch. The formatter signature change (adding `query` parameter) and `-C` semantic change are the two interface-level breaking changes, both documented. `-B` is new but constrained to within-chunk. Bridge merging is additive. Phase 2 (bat) and Phase 3 (compact) remain independent.

### Cross-Cutting Concerns
Two parameter-threading changes are required:

1. **`query` parameter**: Must be threaded from `search_cmd.search_cmd()` through to `format_plain_with_context`. This touches the call site at search_cmd.py:225-227 and the formatter signature at formatters.py:54-56. No other callers of `format_plain_with_context` exist in the codebase.

2. **`rg_matched_lines` from RDR-026**: The hybrid search pipeline (search_cmd.py) already captures `rg_matched_lines` per result as metadata (dict mapping `source_path` to list of matched line numbers). This metadata must be threaded through to `_find_matching_lines` so that ripgrep-identified lines take priority over keyword heuristics. The threading path is: `search_cmd` → formatter → `_find_matching_lines(chunk_text, query, rg_matched_lines, chunk_line_start)`. The `chunk_line_start` parameter (from `r.metadata["line_start"]`) is needed to translate absolute rg line numbers to chunk-relative indices.

3. **`-C` semantic change**: Requires updating the Click help text at search_cmd.py:94-95 and the alias logic at search_cmd.py:127-128.

### Proportionality
This is a P2 UX enhancement. Phase 1 (context lines with keyword matching) is the highest-value change and can land independently. The design decisions (DD1: within-chunk `-B`, DD2: `-C` as before+after) are pragmatic choices that avoid over-engineering while delivering grep-familiar behavior. Phase 2 (bat) is ~20 lines of subprocess plumbing. Phase 3 (compact) is a formatting variant. Total scope is proportionate to the improvement in daily search ergonomics.

## Revision History

- **2026-03-09 (Gate 1 — BLOCKED)**: Substantive-critic identified 2 critical issues: (1) `_find_matching_lines` signature missing `rg_matched_lines` parameter from RDR-026, (2) bat invoked per-result instead of per-file. Also 4 significant: bat fallback unspecified, bat subprocess exceptions unhandled, `--no-color` dead code, `format_vimgrep` step 9 underspecified. All addressed in this revision.
- **2026-03-09 (Gate 2 — PASSED)**: Re-gate found 0 critical, 2 significant (bat line-range source unspecified, overlapping ranges in per-file batching), 5 observations (vimgrep regression test, tokenization spec, format interaction, line_end fallback, stale docstring). All significant issues addressed; observations noted for implementation.

## References

- SeaGOAT result.py: `/Users/hal.hildebrand/git/SeaGOAT/seagoat/result.py`
- SeaGOAT cli_display.py: `/Users/hal.hildebrand/git/SeaGOAT/seagoat/utils/cli_display.py`
- nexus formatters.py: `src/nexus/formatters.py`
- nexus search_cmd.py: `src/nexus/commands/search_cmd.py`
