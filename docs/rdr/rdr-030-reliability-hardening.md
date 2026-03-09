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

The nexus codebase has a **silent degradation** anti-pattern: at least 7 locations catch broad `Exception` and either return silently or pass without any logging. Historically, 8 of 22 P0 bugs were silent failures where the system appeared to work but produced wrong/incomplete results.

This pattern makes debugging extremely difficult — the system silently falls back to degraded behavior with no indication to the user or in logs.

## Context

- `structlog` is already the logging framework (used throughout the codebase)
- `nx doctor` exists but only checks configuration and connectivity, not data integrity
- RDR-019 and RDR-020 established retry patterns for external APIs, but internal error handling remains inconsistent

## Research Findings

### F1: Current Silent Error Locations (Verified — source scan)

| Location | What's swallowed | Impact |
|----------|-----------------|--------|
| `indexer.py:245-251` | tree-sitter parse failures | Context extraction silently degrades, no indication |
| `session.py:199` | Corrupt session file JSON | Orphan T1 server processes leak |
| `hooks.py:155` | Session record parse errors | Session cleanup fails silently |
| `hooks.py:55` | git rev-parse failures | Repo name falls back to cwd name |
| `commands/hook.py:24` | stdin JSON parse errors | session_id silently unavailable |
| `commands/index.py:120` | Hook detection failures | Hooks silently not detected |
| `commands/doctor.py:173` | Registry load failures | Corrupt config silently ignored |

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
Every `except` block MUST have at least `structlog.get_logger().debug()` with the exception and context. No bare `pass` or silent returns from exception handlers.

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

### Phase 1: Silent Error Audit (7 locations)
1. Add `structlog.debug` to all 7 identified catch-and-pass blocks
2. Review each for appropriate recovery behavior
3. Add warning-level logs for fallback paths (session.py EphemeralClient fallback)

### Phase 2: nx doctor Expansion
4. Add collection pipeline_version check
5. Add orphan T1 process detection
6. Add T2 database integrity check
7. Add ChromaDB pagination audit (spot-check record counts)

### Phase 3: Codebase Sweep
8. grep for `except.*pass` and `except.*return` patterns
9. Verify each has appropriate logging
10. Add `# noqa: silent-ok` comment for intentional silent catches (with justification)

## Test Plan

- Unit: each formerly-silent location now emits log at appropriate level
- Unit: nx doctor detects known integrity issues (inject corrupt config, stale pipeline version)
- Unit: stub code raises NotImplementedError
- Integration: trigger each fallback path, verify warning emitted

## References

- Bead history: nexus-ng7, nexus-rln2, nexus-4qu, nexus-3rr, nexus-s5k, nexus-9ar, nexus-738, nexus-zmu
- RDR-019: ChromaDB retry patterns
- RDR-020: Voyage AI timeout patterns
