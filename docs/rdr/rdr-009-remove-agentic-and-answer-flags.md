---
title: "Remove --agentic and --answer Flags from nx search"
id: RDR-009
type: cleanup
status: open
priority: medium
author: Hal Hildebrand
reviewed-by: self
created: 2026-03-01
related_issues:
  - RDR-008
---

## RDR-009: Remove --agentic and --answer Flags from nx search

## Summary

The `nx search` command exposes two flags — `--agentic` and `--answer` (`-a`) —
that invoke a remote Haiku model call (multi-step query refinement and cited
answer synthesis respectively). These flags were ported directly from mgrep,
where they served the same purpose: allowing AI-assisted search output from a
plain shell invocation outside of any Claude session.

In the nx context, both flags are vestigial. nx is designed as a tool *for*
Claude sessions. When invoked from within a Claude session, the AI reasoning
capability already exists in the calling model — delegating to a second Haiku
call adds latency, cost, and an unnecessary external dependency for a capability
that the host model already provides. Outside a Claude session, the use case is
not part of nx's intended operational envelope.

**Decision**: Remove both flags, their backing modules (`nexus/answer.py`,
`nexus/search_engine.py::agentic_search`), and associated tests. No
deprecation, migration path, or legacy shim is required — this is a pre-1.0
(rc) cleanup.

## Motivation

1. **Wrong abstraction layer.** `--agentic` embeds multi-step query planning
   inside the CLI tool. In practice this planning belongs to the Claude session
   that calls nx. Duplicating it in a subprocess Haiku call adds overhead with
   no benefit.

2. **`--answer` conflicts with output contract.** The answer flag swaps the
   structured retrieval output for prose synthesis. Any Claude session consuming
   `nx search` results expects the normal ranked-result format. The flag creates
   a foot-gun: an accidental `-a` in a skill or hook produces prose instead of
   citable chunks.

3. **External API dependency with no guarding.** Both flags call out to the
   Anthropic API. There is no rate-limit handling, cost cap, or graceful
   degradation. This is tolerable in mgrep (where it was the primary value-add)
   but is unacceptable as a side-effect of a flag in a search utility.

4. **Pre-1.0 window.** The project is still in release-candidate state. This is
   the correct moment to excise inherited complexity before it becomes a
   documented, tested, supported surface.

## Scope

### Remove
- `--agentic` flag and `agentic` parameter in `search_cmd.py`
- `--answer` / `-a` flag and `answer` parameter in `search_cmd.py`
- Import of `answer_mode` from `nexus.answer` in `search_cmd.py`
- Import of `agentic_search` from `nexus.search_engine` in `search_cmd.py`
- `agentic_search` function in `src/nexus/search_engine.py` (the full function
  plus its `__all__` export)
- `src/nexus/answer.py` module (entire file)
- All tests covering `--agentic` and `--answer` paths in:
  - `tests/test_search_cmd.py`
  - `tests/test_search_engine.py`
  - `tests/test_search_modules.py`
  - `tests/test_integration.py`

### Not in scope
- Any other search flags or retrieval logic
- `--mxbai` flag (separate concern, retained)
- mgrep (separate repo — no changes needed there)

## Implementation Notes

The removal is mechanical:
1. Delete `src/nexus/answer.py`.
2. In `search_cmd.py`: remove the two `@click.option` decorators, remove the
   two parameters from the function signature, remove the `if agentic:` block
   and `if answer:` block, remove both imports.
3. In `search_engine.py`: remove `agentic_search` from `__all__` and delete the
   function body (roughly lines 108–end of `agentic_search`).
4. Delete or update all associated tests.
5. Run full test suite — expect no failures if removal is clean.

No behaviour change for callers that do not pass these flags.

## Research Findings

### Finding 009-01: Deletion surface is larger than initially scoped

The RDR Scope section listed 4 test files as affected. Codebase audit found 2
additional files not initially listed:

- `tests/test_answer.py` — 33 lines, dedicated answer module test
- `tests/test_answer_extended.py` — 159 lines, extended answer test suite
- `tests/test_e2e.py` — contains `test_answer_mode_returns_synthesis_with_citations`

**Total test code to remove**: ~220+ lines across 6 test files (not 4).

The `tests/e2e/scenarios/03_skills.sh` script has **no** `--agentic` or
`--answer` references — no change needed there.

**Impact**: Scope in the RDR Scope section should list 6 test files, not 4.

---

### Finding 009-02: `answer.py` is imported by `search_engine.py` — not just `search_cmd.py`

`src/nexus/search_engine.py:11` contains:

```python
from nexus.answer import _haiku_refine
```

The `agentic_search` function in `search_engine.py` calls `_haiku_refine` which
lives in `answer.py`. This means the import dependency is:

```
search_cmd.py → answer.py (answer_mode)
search_cmd.py → search_engine.py (agentic_search)
search_engine.py → answer.py (_haiku_refine)
```

Deleting `answer.py` without simultaneously removing `agentic_search` (and its
import of `_haiku_refine`) from `search_engine.py` will break the test suite.
The two removals must be atomic.

**Impact**: Implementation must remove `agentic_search` and its `_haiku_refine`
import from `search_engine.py` before or simultaneously with deleting `answer.py`.

---

### Finding 009-03: `test_search_engine.py` imports `answer_mode` directly

`tests/test_search_engine.py:7`:

```python
from nexus.answer import answer_mode
```

This import is at module level in the test file covering the search engine.
Tests like `test_answer_mode_produces_cite_tags`, `test_answer_mode_includes_citation_footer`,
and `test_haiku_answer_returns_empty_string_on_empty_content` live in this file
alongside unrelated search engine tests.

**Impact**: Cannot simply delete the `test_answer_*.py` files — must also purge
the answer-specific tests from `test_search_engine.py`, which is otherwise kept.

---

### Finding 009-04: `test_search_modules.py` has an explicit `answer` isolation test

`tests/test_search_modules.py:121` contains:
```python
"""scoring, answer, and formatters must not import from search_engine."""
```
and line 136:
```python
answer = importlib.import_module("nexus.answer")
```

This test verifies that `answer.py` exists and obeys import hygiene. After
removal, this test must be deleted (the module it guards no longer exists).

The same file also includes `test_answer_imports` (line 23) and an end-to-end
`answer_mode` integration test (line 175). All three must be removed.

**Impact**: ~30 lines to remove from `test_search_modules.py`; the rest of the
file is valid and retained.

---

### Finding 009-05: `NX_ANSWER` environment variable in `search_cmd.py`

The `--answer` flag exposes `envvar="NX_ANSWER"` (line 83 of `search_cmd.py`).
This means the flag can be activated by setting the environment variable without
passing the flag explicitly. No other code references `NX_ANSWER`. Removing the
flag removes this env var activation automatically with no additional cleanup.

**Impact**: None beyond removing the click option. No shell profile, dotfile,
or documentation references `NX_ANSWER`.

## Acceptance Criteria

- [ ] `nx search --help` shows neither `--agentic` nor `--answer`/`-a`
- [ ] `src/nexus/answer.py` does not exist
- [ ] `agentic_search` is not exported from `search_engine.py`
- [ ] `nx search <query>` produces identical output before and after (no flags)
- [ ] Full test suite passes with no skips or xfails added for removed paths
