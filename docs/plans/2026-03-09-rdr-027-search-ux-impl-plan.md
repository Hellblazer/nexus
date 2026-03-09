# RDR-027 Implementation Plan: Search Results UX

**Epic**: nexus-uayl (in_progress)
**RDR**: docs/rdr/rdr-027-search-results-ux.md (accepted 2026-03-09)
**Branch**: `feature/nexus-uayl-search-results-ux`

## Executive Summary

Implement three incremental UX improvements for `nx search`:

1. **Phase 1 — Context Lines**: Keyword-matching line identification within chunks,
   `-B` (before) flag, fix `-C` semantics to match grep (before+after), bridge
   merging of nearby matches.
2. **Phase 2 — Syntax Highlighting**: `--bat` flag for language-aware coloring via
   the `bat` CLI tool, with per-file batching and graceful fallback.
3. **Phase 3 — Compact Mode**: `--compact` flag for single-line-per-result output
   compatible with grep pipelines.

Each phase is independently shippable as a separate PR.

## Dependency Graph

```
nexus-87yv  P1.1: _find_matching_lines + _extract_context (TDD)
    |  \         \
    |   \         \
    v    v         v
 mb2o  z6ea      cxy2
 P1.2  P1.4      P3
    |
    v
  pign
  P1.3: CLI -B/-C
    |
    v
  2ej2  <--- also depends on ---> zn5j
  P2.2: --bat CLI                 P2.1: bat formatter (TDD)
```

**Legend**: Arrows point from blocker to blocked task.

**Critical path**: nexus-87yv -> nexus-mb2o -> nexus-pign -> nexus-2ej2

**Parallelization opportunities**:
- nexus-zn5j (P2.1 bat formatter) has NO blockers and can start immediately,
  in parallel with all of Phase 1.
- nexus-z6ea (P1.4 vimgrep) and nexus-mb2o (P1.2 integration) can run in
  parallel once nexus-87yv completes.
- nexus-cxy2 (P3 compact) can start once nexus-87yv completes, in parallel
  with the rest of Phase 1 and all of Phase 2.

## Bead Registry

| Bead ID | Title | Phase | Priority | Depends On | Status |
|---------|-------|-------|----------|------------|--------|
| nexus-87yv | P1.1: Context Helpers — _find_matching_lines + _extract_context (TDD) | 1 | P1 | none | open |
| nexus-mb2o | P1.2: Integrate context helpers into format_plain_with_context | 1 | P1 | 87yv | open |
| nexus-pign | P1.3: CLI -B flag + change -C to before+after semantics | 1 | P1 | mb2o | open |
| nexus-z6ea | P1.4: Update format_vimgrep with query parameter | 1 | P2 | 87yv | open |
| nexus-zn5j | P2.1: bat detection + _format_with_bat formatter (TDD) | 2 | P2 | none | open |
| nexus-2ej2 | P2.2: --bat CLI flag + NO_COLOR interaction | 2 | P2 | zn5j, pign | open |
| nexus-cxy2 | P3: format_compact + --compact CLI flag (TDD) | 3 | P3 | 87yv | open |

---

## Phase 1: Context Lines

### PR Boundary

Phase 1 (tasks nexus-87yv, nexus-mb2o, nexus-pign, nexus-z6ea) ships as a
single PR. All four tasks must pass before the PR merges.

### Success Criteria

- [ ] `_find_matching_lines` returns correct 0-based indices for keyword, rg, and fallback cases
- [ ] `_extract_context` produces correct blocks with bridge merging (gap <= 2)
- [ ] `-B N` shows N lines before matching line (within-chunk)
- [ ] `-C N` produces N+1+N lines (before+match+after), matching grep semantics
- [ ] `-A N` shows N lines after matching line (changed from chunk-start-relative to match-relative)
- [ ] No flags produces identical output to current behavior (regression test)
- [ ] `format_vimgrep` with `query` uses best-matching line number
- [ ] `format_vimgrep` with `query=None` returns `line_start` (backward compat)
- [ ] `uv run pytest tests/test_formatters.py tests/test_search_cmd.py` all green

---

### Task: nexus-87yv — P1.1: _find_matching_lines + _extract_context (TDD)

**Context**
- RDR: docs/rdr/rdr-027-search-results-ux.md (Implementation Plan steps 2-3)
- SeaGOAT reference: `/Users/hal.hildebrand/git/SeaGOAT/seagoat/result.py` lines 148-177
  (bridge merging algorithm)
- Search keywords: `_find_matching_lines`, `_extract_context`, `bridge merging`,
  `keyword match`, `rg_matched_lines`

**Prerequisites**
- Dependencies: none (this is the root task)
- Required state: formatters.py exists at `src/nexus/formatters.py`

**Execution Instructions**

1. Use `mcp__sequential-thinking__sequentialthinking` to design function signatures
   and edge cases before writing code.

2. **Write tests FIRST** in `tests/test_formatters.py`:

   ```python
   # --- _find_matching_lines tests ---

   def test_find_matching_lines_keyword_match():
       """Query tokens found in chunk text return correct 0-based indices."""
       # Chunk: "import os\nos.path.join(a, b)\nreturn result"
       # Query: "os.path"
       # Expected: [1] (line index 1 matches "os.path")

   def test_find_matching_lines_multiple_tokens():
       """Multiple query tokens: line matches if ANY token appears."""
       # Query: "import os path"  (tokens: ["import", "os", "path"])
       # Lines 0 and 1 both match

   def test_find_matching_lines_case_insensitive():
       """Matching is case-insensitive."""
       # Query: "IMPORT" matches line "import os"

   def test_find_matching_lines_rg_preferred():
       """When rg_matched_lines provided, prefer rg lines over keyword."""
       # rg_matched_lines=[15], chunk_line_start=10, chunk has 10 lines
       # Line 15 is index 5 within chunk -> returns [5]

   def test_find_matching_lines_rg_outside_chunk_falls_back():
       """rg lines outside chunk range fall back to keyword match."""
       # rg_matched_lines=[100], chunk_line_start=10, chunk has 5 lines
       # 100 is outside [10, 14] -> falls back to keyword

   def test_find_matching_lines_no_match_falls_back_to_zero():
       """No keyword matches -> returns [0]."""

   def test_find_matching_lines_empty_query():
       """Empty query string -> returns [0]."""

   # --- _extract_context tests ---

   def test_extract_context_single_match_middle():
       """Match at index 5 of 10 lines, before=2, after=2 -> block (3, 7)."""

   def test_extract_context_match_at_start():
       """Match at index 0, before=3 -> block starts at 0 (no negative)."""

   def test_extract_context_match_at_end():
       """Match at last index, after=3 -> block ends at last index."""

   def test_extract_context_bridge_merging():
       """Two matches 2 lines apart are bridged into one block."""
       # Matches at 3 and 6 with before=0, after=0 -> gap is 2 -> bridge
       # Result: single block (3, 6)

   def test_extract_context_no_bridge_large_gap():
       """Two matches 4 lines apart are NOT bridged."""
       # Matches at 3 and 8 with before=0, after=0 -> gap is 4 -> two blocks

   def test_extract_context_overlapping_windows():
       """Overlapping context windows from two matches merge into one block."""
       # Matches at 3 and 5, before=2, after=2 -> windows overlap -> one block

   def test_extract_context_zero_before_zero_after():
       """before=0, after=0 -> blocks are just the match lines."""
   ```

3. **Implement** in `src/nexus/formatters.py`:

   ```python
   import re

   def _find_matching_lines(
       chunk_text: str,
       query: str,
       rg_matched_lines: list[int] | None = None,
       chunk_line_start: int = 0,
   ) -> list[int]:
       """Return 0-based line indices within chunk_text that match the query.

       Priority: (1) rg_matched_lines translated to chunk-relative,
       (2) keyword match, (3) fallback to [0].
       """

   def _extract_context(
       lines: list[str],
       matches: list[int],
       before: int,
       after: int,
   ) -> list[tuple[int, int]]:
       """Return (start, end) blocks around match indices with bridge merging.

       Blocks are 0-based inclusive indices into *lines*.
       Adjacent blocks with gap <= 2 lines are bridged.
       """
   ```

4. Ensure `uv run pytest tests/test_formatters.py -k "find_matching_lines or extract_context"` passes.

**Validation**
- All new tests pass: `uv run pytest tests/test_formatters.py -v`
- Existing tests unaffected (no signature changes to public functions yet)
- Code compiles: `python -c "from nexus.formatters import _find_matching_lines, _extract_context"`

---

### Task: nexus-mb2o — P1.2: Integrate context helpers into format_plain_with_context

**Context**
- RDR: docs/rdr/rdr-027-search-results-ux.md (Implementation Plan step 1)
- Depends on: nexus-87yv (_find_matching_lines, _extract_context must exist)

**Prerequisites**
- Dependencies: nexus-87yv (closed)
- Required state: `_find_matching_lines` and `_extract_context` implemented in formatters.py

**Execution Instructions**

1. **Write tests FIRST** in `tests/test_formatters.py`:

   ```python
   def test_context_with_query_centers_on_match():
       """With query, context windows center on matching lines, not chunk start."""
       # 10-line chunk, query matches line 7, lines_after=2
       # Should show lines 7, 8, 9 (not lines 0, 1, 2)

   def test_context_with_query_and_before():
       """lines_before shows lines before the match within the chunk."""
       # 10-line chunk, query matches line 5, lines_before=2, lines_after=1
       # Shows lines 3, 4, 5, 6

   def test_context_with_rg_matched_lines_in_metadata():
       """rg_matched_lines in result metadata are used for line identification."""
       # Result has metadata["rg_matched_lines"] = [15]
       # chunk_line_start=10, query matches nothing
       # Context should center on line index 5 (abs 15 - start 10)

   def test_context_backward_compat_no_query():
       """query=None preserves current behavior (first N lines)."""

   def test_context_backward_compat_no_flags():
       """lines_before=0, lines_after=0, query provided -> format_plain output."""

   def test_context_bridge_in_output():
       """Two nearby matches within chunk produce bridged output block."""
   ```

2. **Modify** `format_plain_with_context` in `src/nexus/formatters.py`:

   - Extend signature:
     ```python
     def format_plain_with_context(
         results: list[SearchResult],
         lines_after: int = 0,
         lines_before: int = 0,
         query: str | None = None,
     ) -> list[str]:
     ```
   - When `query` is provided AND (`lines_before > 0` or `lines_after > 0`):
     1. Call `_find_matching_lines(r.content, query, r.metadata.get("rg_matched_lines"), r.metadata.get("line_start", 0))`
     2. Call `_extract_context(chunk_lines, matches, lines_before, lines_after)`
     3. Emit lines within the context blocks with `source_path:line_no:content` format
   - When `query` is None: preserve existing behavior (first N lines fallback)
   - When both are 0: delegate to `format_plain` as before

3. Ensure all existing tests still pass: `uv run pytest tests/test_formatters.py -v`

**Validation**
- New tests pass
- Existing tests unmodified and still green
- `format_plain_with_context([result], lines_after=0)` still equals `format_plain([result])`

---

### Task: nexus-pign — P1.3: CLI -B flag + change -C to before+after semantics

**Context**
- RDR: docs/rdr/rdr-027-search-results-ux.md (DD1, DD2)
- Depends on: nexus-mb2o (format_plain_with_context has new signature)
- File: `src/nexus/commands/search_cmd.py`

**Prerequisites**
- Dependencies: nexus-mb2o (closed)
- Required state: format_plain_with_context accepts `lines_before`, `query`

**Execution Instructions**

1. **Write/update tests** in `tests/test_search_cmd.py`:

   ```python
   def test_B_flag_accepted():
       """-B N is accepted as a valid integer flag."""

   def test_C_means_before_and_after():
       """-C 3 on a 20-line chunk with keyword match at line 10 produces 7 lines."""
       # Verify output has 3 before + 1 match + 3 after = 7 content lines

   def test_B_at_chunk_start_no_pre_context():
       """-B 3 when match is at first line of chunk -> no before-context."""

   def test_regression_no_flags_same_output():
       """No -A/-B/-C flags produce identical output to baseline."""

   # UPDATE existing test:
   # test_context_C_sets_lines_after -> rename/rewrite to test -C sets both before+after
   ```

2. **Modify** `src/nexus/commands/search_cmd.py`:

   - Add `-B` Click option:
     ```python
     @click.option("-B", "lines_before", default=0, type=int, metavar="N",
                   help="Show N lines of context before each matching line (within-chunk)")
     ```

   - Change `-C` help text:
     ```python
     @click.option("-C", "lines_context", default=0, type=int, metavar="N",
                   help="Show N lines before and after each match (equivalent to -B N -A N)")
     ```

   - Update the `-C` alias logic (currently line 142-144):
     ```python
     # -C N = -B N -A N (grep semantics)
     if lines_context:
         lines_before = lines_context
         lines_after = lines_context
     ```

   - Thread `query` and `lines_before` to `format_plain_with_context`:
     ```python
     for line in format_plain_with_context(
         [result], lines_after=lines_after, lines_before=lines_before, query=query
     ):
     ```

   - Add `lines_before` to the `search_cmd` function signature.

3. Run full test suite: `uv run pytest tests/test_search_cmd.py tests/test_formatters.py -v`

**Validation**
- `-B 3` accepted without error
- `-C 3` produces 3+1+3 = 7 lines around keyword match
- No-flag output unchanged from baseline
- All existing tests pass (some updated for new -C semantics)

---

### Task: nexus-z6ea — P1.4: Update format_vimgrep with query parameter

**Context**
- RDR: docs/rdr/rdr-027-search-results-ux.md (Implementation Plan step 9)
- Depends on: nexus-87yv (_find_matching_lines must exist)
- Files: `src/nexus/formatters.py`, `src/nexus/commands/search_cmd.py`

**Prerequisites**
- Dependencies: nexus-87yv (closed)
- Required state: `_find_matching_lines` exists in formatters.py

**Execution Instructions**

1. **Write tests FIRST** in `tests/test_formatters.py`:

   ```python
   def test_vimgrep_with_query_uses_matching_line():
       """format_vimgrep with query reports matching line number, not line_start."""
       # 10-line chunk starting at line 10, query "return" matches line 17
       # Vimgrep output: "path:17:0:return result"

   def test_vimgrep_without_query_uses_line_start():
       """format_vimgrep with query=None returns line_start (backward compat)."""
   ```

2. **Modify** `format_vimgrep` in `src/nexus/formatters.py`:

   ```python
   def format_vimgrep(results: list[SearchResult], query: str | None = None) -> list[str]:
   ```
   - When `query` is provided, use `_find_matching_lines` to find best line,
     report that line number and text
   - When `query` is None, preserve current behavior (first line, line_start)

3. **Update call site** in `search_cmd.py` (~line 283):
   ```python
   for line in format_vimgrep(results, query=query):
   ```

4. Run: `uv run pytest tests/test_formatters.py -k "vimgrep" -v`

**Validation**
- New tests pass
- Existing vimgrep tests still pass (query=None backward compat)

---

## Phase 2: Syntax Highlighting

### PR Boundary

Phase 2 (tasks nexus-zn5j, nexus-2ej2) ships as a single PR after Phase 1
merges.

### Success Criteria

- [ ] `_is_bat_installed()` returns False when bat not on PATH (no crash)
- [ ] `_format_with_bat` groups results by source_path (one subprocess per file)
- [ ] Overlapping/adjacent line ranges merged before bat invocation
- [ ] `line_end` absent or 0 -> derived as `line_start + len(content.splitlines()) - 1`
- [ ] FileNotFoundError, CalledProcessError, OSError caught with per-file fallback
- [ ] `--bat` with `--no-color` or `NO_COLOR` env skips bat entirely
- [ ] `--bat` with `--json`/`--vimgrep`/`--files` silently ignored
- [ ] bat not installed + `--bat` explicit -> warning + plain fallback

---

### Task: nexus-zn5j — P2.1: bat detection + _format_with_bat formatter (TDD)

**Context**
- RDR: docs/rdr/rdr-027-search-results-ux.md (Phase 2, Implementation Plan step 6)
- SeaGOAT reference: `/Users/hal.hildebrand/git/SeaGOAT/seagoat/utils/cli_display.py`
  lines 101-133 (bat detection and line-range invocation)
- No Phase 1 dependency — can start immediately in parallel
- Search keywords: `bat`, `subprocess`, `line-range`, `syntax highlighting`

**Prerequisites**
- Dependencies: none
- Required state: formatters.py exists

**Execution Instructions**

1. Use `mcp__sequential-thinking__sequentialthinking` to design the range-merge
   algorithm and fallback strategy.

2. **Write tests FIRST** in `tests/test_formatters.py`:

   ```python
   def test_is_bat_installed_not_found(monkeypatch):
       """_is_bat_installed returns False when bat binary missing."""
       # Monkeypatch subprocess.run to raise FileNotFoundError

   def test_is_bat_installed_found(monkeypatch):
       """_is_bat_installed returns True when bat responds."""

   def test_merge_line_ranges_overlapping():
       """Overlapping ranges [(1,5), (3,8)] merge to [(1,8)]."""

   def test_merge_line_ranges_adjacent():
       """Adjacent ranges [(1,5), (6,10)] merge to [(1,10)]."""

   def test_merge_line_ranges_gap():
       """Non-adjacent ranges [(1,5), (8,10)] stay separate."""

   def test_format_with_bat_groups_by_file(monkeypatch):
       """Results from same file batched into single bat call."""
       # Mock subprocess.run, verify called once per unique source_path

   def test_format_with_bat_subprocess_error_fallback(monkeypatch):
       """CalledProcessError falls back to plain format for that file."""

   def test_format_with_bat_line_end_fallback():
       """When line_end missing, derived from line_start + line count."""

   def test_format_with_bat_with_context_blocks(monkeypatch):
       """When context_blocks provided, bat uses those ranges."""
   ```

3. **Implement** in `src/nexus/formatters.py`:

   ```python
   import subprocess
   from functools import cache

   @cache
   def _is_bat_installed() -> bool:
       """Check if bat is available on PATH. Result is cached."""

   def _merge_line_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
       """Sort and merge overlapping/adjacent line ranges."""

   def _format_with_bat(
       results: list[SearchResult],
       context_blocks: dict[str, list[tuple[int, int]]] | None = None,
   ) -> str:
       """Format results with bat syntax highlighting.

       Groups results by source_path. For each file:
       1. Compute line ranges from context_blocks or chunk metadata
       2. Merge overlapping ranges
       3. Call bat with --line-range args
       4. On error, fall back to plain formatting for that file
       """
   ```

4. Run: `uv run pytest tests/test_formatters.py -k "bat or merge_line" -v`

**Validation**
- All bat-related tests pass
- No import errors: `python -c "from nexus.formatters import _is_bat_installed, _format_with_bat"`

---

### Task: nexus-2ej2 — P2.2: --bat CLI flag + NO_COLOR interaction

**Context**
- RDR: docs/rdr/rdr-027-search-results-ux.md (Implementation Plan step 7)
- Depends on: nexus-zn5j (bat formatter) + nexus-pign (CLI -B/-C for context_blocks)
- File: `src/nexus/commands/search_cmd.py`

**Prerequisites**
- Dependencies: nexus-zn5j (closed), nexus-pign (closed)
- Required state: `_format_with_bat` and `_is_bat_installed` exist in formatters.py;
  format_plain_with_context returns context blocks or can be queried for them

**Execution Instructions**

1. **Write tests** in `tests/test_search_cmd.py`:

   ```python
   def test_bat_flag_with_no_color_skips_bat(runner, monkeypatch):
       """--bat + --no-color -> bat is not invoked."""

   def test_bat_flag_with_NO_COLOR_env_skips_bat(runner, monkeypatch):
       """--bat + NO_COLOR env var -> bat is not invoked."""

   def test_bat_flag_with_json_ignored(runner, monkeypatch):
       """--bat + --json -> bat silently ignored, JSON output."""

   def test_bat_flag_with_vimgrep_ignored(runner, monkeypatch):
       """--bat + --vimgrep -> bat silently ignored."""

   def test_bat_not_installed_warning(runner, monkeypatch):
       """--bat when bat not on PATH -> warning + plain fallback."""
   ```

2. **Modify** `src/nexus/commands/search_cmd.py`:

   - Add `--bat` Click option:
     ```python
     @click.option("--bat", "use_bat", is_flag=True, default=False,
                   help="Syntax highlight with bat (ignored with --json/--vimgrep/--files)")
     ```

   - Add import: `from nexus.formatters import _is_bat_installed, _format_with_bat`

   - In the output format section (~line 279), before the plain-text path:
     ```python
     # Check bat applicability
     use_bat_effective = (
         use_bat
         and not json_out
         and not vimgrep
         and not files_only
         and not no_color
         and not os.environ.get("NO_COLOR")
     )
     if use_bat_effective and not _is_bat_installed():
         click.echo("Warning: bat not found; showing plain output", err=True)
         use_bat_effective = False

     if use_bat_effective:
         click.echo(_format_with_bat(results, context_blocks=...))
     else:
         # existing plain output path
     ```

3. Run: `uv run pytest tests/test_search_cmd.py -v`

**Validation**
- --bat with plain output invokes bat formatter
- --bat silently ignored for --json/--vimgrep/--files
- NO_COLOR skips bat
- Warning emitted when bat not installed

---

## Phase 3: Compact Mode

### PR Boundary

Phase 3 (task nexus-cxy2) ships as its own PR, independently of Phase 2.

### Success Criteria

- [ ] `--compact` produces one line per result: `{source_path}:{line_no}:{line_text}`
- [ ] With query, reports the best-matching line (not always line_start)
- [ ] Without query, reports first line at line_start (backward compat)
- [ ] Mutually exclusive with `--bat` (compact is plain text)

---

### Task: nexus-cxy2 — P3: format_compact + --compact CLI flag (TDD)

**Context**
- RDR: docs/rdr/rdr-027-search-results-ux.md (Phase 3, Implementation Plan step 8)
- Depends on: nexus-87yv (_find_matching_lines for best-line selection)
- Files: `src/nexus/formatters.py`, `src/nexus/commands/search_cmd.py`

**Prerequisites**
- Dependencies: nexus-87yv (closed)
- Required state: `_find_matching_lines` exists in formatters.py

**Execution Instructions**

1. **Write tests FIRST** in `tests/test_formatters.py`:

   ```python
   def test_compact_basic_format():
       """format_compact produces path:line:text for each result."""

   def test_compact_with_query_best_line():
       """With query, compact reports best-matching line."""
       # 10-line chunk, query matches line 7 -> output uses line 7

   def test_compact_without_query_first_line():
       """Without query, compact reports first line at line_start."""
   ```

2. **Write tests** in `tests/test_search_cmd.py`:

   ```python
   def test_compact_flag_produces_grep_output(runner, monkeypatch):
       """--compact produces grep-compatible single-line output."""
   ```

3. **Implement** `format_compact` in `src/nexus/formatters.py`:

   ```python
   def format_compact(
       results: list[SearchResult],
       query: str | None = None,
   ) -> list[str]:
       """One line per result: source_path:line_no:best_matching_line."""
   ```

4. **Add** `--compact` Click option in `search_cmd.py` and wire to `format_compact`.

5. Run: `uv run pytest tests/test_formatters.py tests/test_search_cmd.py -v`

**Validation**
- `--compact` output matches `path:line:text` format
- Pipeable to grep, fzf, etc.

---

## Risk Factors and Mitigations

| Risk | Impact | Likelihood | Mitigation |
|------|--------|------------|------------|
| `-C` breaking change | Users relying on `-C` as after-alias | Low (feature is new) | Update help text; document in CHANGELOG |
| Keyword heuristic misses | Semantic queries with no keyword overlap | Medium | Falls back to [0] (current behavior) |
| bat subprocess overhead | ~50ms per unique file | Low | Per-file batching with `--line-range` |
| `line_end` absent in metadata | rg-only and markdown chunks | Medium | Derive from `line_start + len(lines) - 1` |
| rg_matched_lines not present | Non-hybrid searches | Expected | Keyword fallback handles this |

## Test Commands Reference

```bash
# Full suite
uv run pytest tests/test_formatters.py tests/test_search_cmd.py -v

# Phase 1 only
uv run pytest tests/test_formatters.py -k "find_matching or extract_context or context_with" -v
uv run pytest tests/test_search_cmd.py -k "context" -v

# Phase 2 only
uv run pytest tests/test_formatters.py -k "bat or merge_line" -v
uv run pytest tests/test_search_cmd.py -k "bat" -v

# Phase 3 only
uv run pytest tests/test_formatters.py -k "compact" -v
uv run pytest tests/test_search_cmd.py -k "compact" -v

# Regression (no new tests should break these)
uv run pytest tests/test_formatters.py -k "not find_matching and not extract_context and not bat and not compact and not context_with" -v

# Full project
uv run pytest
```

## File Modification Map

| File | Phase | Changes |
|------|-------|---------|
| `src/nexus/formatters.py` | 1, 2, 3 | Add `_find_matching_lines`, `_extract_context`, extend `format_plain_with_context`, extend `format_vimgrep`, add `_is_bat_installed`, `_merge_line_ranges`, `_format_with_bat`, `format_compact` |
| `src/nexus/commands/search_cmd.py` | 1, 2, 3 | Add `-B`, change `-C`, add `--bat`, add `--compact`, thread `query` to formatters |
| `tests/test_formatters.py` | 1, 2, 3 | ~30 new tests across all phases |
| `tests/test_search_cmd.py` | 1, 2, 3 | ~10 new tests, ~2 updated tests |

## Continuation State

After each task completes, update:
```bash
nx memory put "RDR-027 progress: {task_id} done, next: {next_task}" \
  --project nexus_rdr --title 027-continuation.md --ttl 30d
```
