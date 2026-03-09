---
title: "Reliability Hardening — Silent Error Audit and Logging Policy"
id: RDR-030
type: Enhancement
status: draft
priority: P2
author: Hal Hildebrand
reviewed-by: self
created: 2026-03-08
related_issues: ["RDR-019", "RDR-020"]
related_tests: []
implementation_notes: ""
---

# RDR-030: Reliability Hardening — Silent Error Audit and Logging Policy

## Problem Statement

The nexus codebase has a **silent degradation** anti-pattern: at least 11 locations catch exceptions — some broadly (`Exception`) and some specifically — without any logging, causing silent degradation. Historically, 8 known P0 bugs involved silent failures where the system appeared to work but produced wrong/incomplete results.

This pattern makes debugging extremely difficult — the system silently falls back to degraded behavior with no indication to the user or in logs.

## Context

- `structlog` is already the logging framework (used throughout the codebase)
- `nx doctor` exists but only checks configuration and connectivity, not data integrity
- RDR-019 and RDR-020 established retry patterns for external APIs, but internal error handling remains inconsistent

## Research Findings

### F1: Current Silent Error Locations (Verified — source scan)

| Location | What's swallowed | Impact |
|----------|-----------------|--------|
| `indexer.py:298-302` | `get_parser()` failure (lazy import / language-pack unavailability) — catches broad `Exception` | Language-pack breakage indistinguishable from unsupported language; silent. Warrants `warning` level. |
| `indexer.py:304-307` | `parser.parse(source)` failure (malformed source bytes) — catches broad `Exception` | Parse failure on corrupt source silently skipped; appropriate for `debug` level. |
| `indexer.py:264-267` | `child.text.decode("utf-8")` failure — catches `(UnicodeDecodeError, AttributeError)` | Tree-sitter text extraction silently continues on decode failure; name extraction returns empty string. |
| `indexer.py:270-273` | `child.text.decode("utf-8")` failure — catches `(UnicodeDecodeError, AttributeError)` | Same as above, second extraction path for children with `identifier`/`name` type. |
| `session.py:199` | Corrupt session file JSON during sweep — catches `(json.JSONDecodeError, OSError)` | Stale session files with corrupt JSON are never swept; if corresponding server is still running it is not stopped. This is the sweep/cleanup path, not the session-start path. |
| `session.py:113-115` | `/proc/<pid>/status` read failure in `_ppid_of()` — catches `(OSError, ValueError)` | PPID chain walk silently stops, causing session adoption to fail without indication. |
| `hooks.py:155` | Own session record parse errors — catches `(json.JSONDecodeError, OSError)` | If only the own session file is corrupt but an ancestor session exists, the corruption is silently ignored. Note: `_log.warning(...)` at line 162 fires when no session record is found at all, so the failure is not fully silent in that fallback case. |
| `hooks.py:55` | git rev-parse failures | Repo name falls back to cwd name |
| `commands/hook.py:24` | stdin JSON parse errors | session_id silently unavailable |
| `commands/index.py:138` | Hook detection failures | Hooks silently not detected |
| `commands/doctor.py:211` | Registry load failures | Corrupt config silently ignored |

### F2: Historical P0 Silent Failures (Verified — beads)

| Bug | Silent behavior |
|-----|----------------|
| nexus-ng7 | `apply_hybrid_scoring` inverted ranking — wrong results, no error |
| nexus-rln2 | CCE query model wrong — collections unsearchable, no error |
| nexus-4qu | `--hybrid` search was a no-op — silently delivered semantic-only results |
| nexus-3rr | Missing credentials silently marked repo ready — prevented retry |
| nexus-s5k | `doc_indexer` partial failure silently emptied collection |
| nexus-9ar | Semantic chunker never wrote chunk positions — metadata silently missing |
| nexus-738 | Formatters always emitted `:0:` line numbers — wrong output |
| nexus-zmu | Pre-heading markdown content silently dropped |

### F3: ChromaDB 300-Record Pagination (Verified — production discovery)

ChromaDB Cloud's `get()` returns at most 300 entries per call. Code that calls `col.get()` without pagination silently misses data. Known fixed locations: `delete_by_source()`, `nx store delete --title`. Other call sites may still be vulnerable.

## Proposed Solution

### Policy 1: Minimum Logging Standard
Every `except` block MUST use the module-level `_log` logger with a descriptive event name and exception context, e.g., `_log.debug('event_name', error=str(exc), exc_info=True)`. The codebase pattern is `_log = structlog.get_logger()` at module level. No bare `pass` or silent returns from exception handlers.

### Policy 2: Warn on Degradation
When a fallback path is taken (e.g., EphemeralClient instead of HTTP server), emit a `structlog.warning()` that the user can see with `--verbose`.

### Policy 3: nx doctor Data Integrity Checks
Expand `nx doctor` to validate:
- All collections have current `pipeline_version` (ties to RDR-029)
- No orphan T1 server processes
- T2 database integrity (FTS5 index consistency)
- ChromaDB collection record counts match expected pagination
- Registry repos.json parseable
- Config files valid YAML/JSON

### Policy 4: Stub Code Must Raise
Any unimplemented code path must `raise NotImplementedError("...")` rather than silently returning empty results. The `pm_reference` stub and `--hybrid` no-op bugs would have been caught immediately.

## Implementation Plan

### Phase 1: Silent Error Audit (11 locations)
1. Add `_log.debug` (or `_log.warning` where degradation is user-visible) to all 11 identified catch-and-pass blocks
2. Review each for appropriate recovery behavior
3. Add warning-level logs for fallback paths (session.py EphemeralClient fallback)

### Phase 2: nx doctor Expansion
4. Add collection pipeline_version check
5. Add orphan T1 process detection
6. Add T2 database integrity check
7. Add ChromaDB pagination audit (spot-check record counts)

### Phase 2.5: ChromaDB Pagination Audit
8. Audit all `col.get()` call sites in `db/t3.py` and `doc_indexer.py` for missing `limit=` / pagination loop; fix any found.

### Phase 3: Codebase Sweep
9. grep for `except.*pass` and `except.*return` patterns
10. Verify each has appropriate logging
11. Add a comment explaining why silence is correct for intentional silent catches, e.g., `# intentional: <reason>`. For linter enforcement, annotate with `B110` (flake8-bugbear blind `except: pass`) or ruff `PERF` rules as applicable.

## Test Plan

- Unit: each formerly-silent location now emits log at appropriate level
- Unit: nx doctor detects known integrity issues (inject corrupt config, stale pipeline version)
- Unit: stub code raises NotImplementedError
- Integration: trigger each fallback path, verify warning emitted

## Finalization Gate

### Contradiction Check
No contradictions found between the three proposed policies. Policy 1 (minimum logging) and Policy 2 (warn on degradation) are complementary: Policy 1 sets the floor (`debug`), Policy 2 raises it to `warning` for user-visible fallbacks. Policy 4 (stub code must raise) applies to unimplemented paths, not to the same catch blocks that Policies 1-2 cover — they target different code. The `nx doctor` expansion (Policy 3) is additive and does not conflict with logging changes.

### Assumption Verification
- **Assumption: structlog is universally available.** Verified: `structlog` is a hard dependency in `pyproject.toml` and `_log = structlog.get_logger()` exists in all affected modules (`indexer.py`, `session.py`, `hooks.py`).
- **Assumption: All 11 silent catch sites are reachable.** Verified by source inspection. Each corresponds to a real try/except block in current `main`. The two `indexer.py:264-273` sites and `session.py:113-115` were initially missed but confirmed present.
- **Assumption: P0 bug list is accurate.** Bead IDs are cited; the specific count "8 of 22" was softened to "8 known P0 bugs" since the total denominator is not independently verifiable from the current bead store.

### Scope Verification
This RDR is scoped to (a) cataloguing silent catches, (b) establishing logging policy, and (c) expanding `nx doctor`. It does not propose changes to retry logic (covered by RDR-019/020), error recovery strategies, or user-facing CLI error messages. The ChromaDB pagination audit (Phase 2.5) is tightly scoped to `col.get()` call sites only, not a general API audit.

### Cross-Cutting Concerns
- **Performance**: Adding `_log.debug()` calls has negligible overhead — structlog short-circuits when the level is not active. No hot-path concern.
- **Testing**: Each new log emission is testable via `structlog.testing.capture_logs()`, already used in the test suite. No new test infrastructure needed.
- **Backwards compatibility**: No CLI interface or API changes. Log output is additive. Users who do not set `--verbose` will see no difference for `debug`-level additions; `warning`-level additions (Policy 2) are intentionally user-visible.

### Proportionality
The effort is modest — each silent catch site needs 1-2 lines of logging added. The `nx doctor` expansion (Phase 2) is the largest component but builds on existing infrastructure. The historical cost of silent failures (8 P0 bugs requiring multi-hour debugging each) far exceeds the implementation cost. Risk is low: adding logging cannot break existing behavior, and `nx doctor` checks are purely diagnostic.

## References

- Bead history: nexus-ng7, nexus-rln2, nexus-4qu, nexus-3rr, nexus-s5k, nexus-9ar, nexus-738, nexus-zmu
- RDR-019: ChromaDB retry patterns
- RDR-020: Voyage AI timeout patterns
- RDR-029: Pipeline version tracking (referenced by Policy 3 / nx doctor expansion)
