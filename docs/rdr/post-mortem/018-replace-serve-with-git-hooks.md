---
rdr: RDR-018
title: "Replace nx serve Polling Server with Git Hooks"
closed_date: 2026-03-04
close_reason: implemented
---

# Post-Mortem: RDR-018 — Replace nx serve with Git Hooks

## RDR Summary

Deleted the Flask + Waitress polling server (`nx serve`) and replaced it with git hooks (`nx hooks install/uninstall/status`). Removed ~411 lines of daemon/REST/polling code; added ~250 lines of event-driven hook management.

## Implementation Status

Fully implemented. All success criteria met.

## Implementation vs. Plan

| Item | Planned | Delivered | Drift |
|------|---------|-----------|-------|
| Delete `server.py`, `polling.py`, `commands/serve.py` | Remove ~411 lines | ✓ Deleted | None |
| `nx hooks install/uninstall/status` | New `commands/hooks.py` | ✓ Implemented | None |
| Worktree-aware hooks dir (`git rev-parse --git-common-dir`) | Resolve effective hooks directory | ✓ Implemented | None |
| `core.hooksPath` support | Install into configured path with warning if non-writable | ✓ Implemented | None |
| Sentinel-based coexistence | Append/remove nexus stanza in existing hooks | ✓ `# >>> nexus managed begin >>>` sentinel | None |
| `--on-locked={skip,wait}` flag | Per-repo file lock with configurable behaviour | ✓ Lock guard in `index_repository`; hooks use `skip` | None |
| `head_hash` update in `index_repository` | Moved from caller to callee after successful run | ✓ Implemented | None |
| `nx index repo` reminder | Print reminder if nexus sentinel absent from effective hooks dir | ✓ Implemented | None |
| `nx doctor` hooks + log check | Per-repo hook status and index log last-modified | ✓ Implemented | None |
| `chmod +x` on new hook files | Set executable bit | ✓ Implemented | None |

## Drift Classification

None. Every success criterion in the RDR was delivered.

## RDR Quality Assessment

- The decision to delete `nx serve` entirely (vs. keeping it alongside hooks) proved correct: no backward-compatibility surface to maintain
- The acknowledged regression (credential failure silently drops hooks, requires manual `nx index repo`) is acceptable and documented
- Gate review caught no issues requiring rework — the design was clean before implementation

## Key Takeaways

- Sentinel-based coexistence (`# >>> nexus managed begin >>>`) is robust: enables install, uninstall, and status checks with a single string match
- The `--on-locked=skip` default for hooks prevents cascading lock contention during `git rebase -i` without user intervention
- Event-driven hooks + auditable log (`~/.config/nexus/index.log`) are strictly better than polling: zero idle CPU, exact semantics, no daemon lifecycle
- `nx doctor` integration turns a previously invisible process into a visible, diagnosable one
