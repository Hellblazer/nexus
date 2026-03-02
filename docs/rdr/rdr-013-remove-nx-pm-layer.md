---
title: "Remove nx pm Layer — Use T2 Memory Directly"
id: RDR-013
type: architecture
status: closed
priority: medium
author: Hal Hildebrand
reviewed-by: self
created: 2026-03-01
updated: 2026-03-01
accepted_date: "2026-03-01"
closed_date: "2026-03-01"
close_reason: "implemented"
post_mortem: ""
gate_result: "PASSED 2026-03-01"
gate_date: "2026-03-01"
related_issues: []
---

## RDR-013: Remove nx pm Layer — Use T2 Memory Directly

## Summary

Nexus has a project management (`nx pm`) command group that wraps T2 memory
entries with specific naming conventions (`METHODOLOGY.md`, `BLOCKERS.md`,
`CONTEXT_PROTOCOL.md`, `phases/phase-N/context.md`) and PM-specific tags.
After a series of simplifications (archive, restore, reference, promote all
removed in prior PRs), the remaining `nx pm` commands (init, resume, status,
block, unblock, phase next, search, expire) are a thin layer that was never
adopted in practice. This RDR records the decision to remove the layer
entirely and use T2 memory directly.

## Problem

The `nx pm` layer accumulated features (archive/restore to T3, reference
search, phase promotion) that required Anthropic SDK integration and proved
unnecessary. After removing those features (PRs #52–#53), the remaining
PM layer is:

- A set of convention-aware T2 wrappers that nobody calls
- A PM detection path in `hooks.py` that checks for `BLOCKERS.md` with a
  `pm` tag to decide whether to call `pm_resume()` vs. show raw entries
- Session hooks (`session_start_hook.py`, `subagent-start.sh`) that call
  `nx pm resume` / `nx pm status` at agent spawn time
- ~20 plugin skill/command files that reference `nx pm status` for context

The user confirmed: "do we need the pm functionality at all? we just need
the rdr integration and t2 functionality, right? not really using the pm stuff?"
and "i am not [using nx pm init]".

## Options Considered

### Option A: Keep nx pm as-is
Keep the thin wrapper layer. Low cost, no disruption.

**Rejected**: The layer is unused, adds maintenance surface, and the session
hooks call `nx pm resume`/`nx pm status` on every agent spawn (adding latency
even when PM is not initialized and failing silently).

### Option B: Remove nx pm entirely (chosen)
Delete `src/nexus/pm.py` and `src/nexus/commands/pm.py`. Simplify `hooks.py`
to always show recent T2 entries. Clean all plugin references.

**Accepted**: T2 memory (`nx memory put/get/list/search`) remains fully
functional. Agents can store and retrieve project context directly. The hook
path becomes simpler and unconditional.

### Option C: Keep nx pm but make it optional / not auto-injected
Remove the session hook injection but keep the commands available.

**Rejected**: Partial cleanup adds complexity without benefit. If the user
doesn't call `nx pm init`, the commands are dead code anyway.

## Decision

**Remove the nx pm layer entirely.** T2 memory is the mechanism; PM was
a usage pattern built on it. Agents that need project context can use
`nx memory put/get` directly.

## Implementation Plan

### Phase 1: Source code removal
- Delete `src/nexus/pm.py` and `src/nexus/commands/pm.py`
- Remove `pm` from `cli.py` command registration
- Simplify `hooks.py` (remove PM detection, always show `list_entries`)
- Delete `tests/test_pm.py`, `tests/test_pm_cmd.py`
- Remove PM test from `tests/test_e2e.py`
- Update `tests/test_plugin.py` and `tests/test_hooks.py`

### Phase 2: Plugin cleanup
- Delete `nx/commands/pm-status.md`, `pm-new.md`, `pm-list.md`, `project-setup.md`
- Delete `nx/agents/project-management-setup.md`
- Delete `nx/skills/project-setup/SKILL.md`
- Remove `nx pm` calls from `session_start_hook.py` and `subagent-start.sh`
- Clean PM references from agent docs, skills, registry, and README

### Phase 3: Documentation
- Rewrite `docs/project-management.md` to cover T2 direct usage + beads
- Update `docs/cli-reference.md`, `docs/storage-tiers.md`, `docs/README.md`

## Research Findings

**Usage audit (2026-03-01)**: Searched entire codebase for `nx pm init`.
Zero calls found outside of the PM implementation itself and its tests.
The user explicitly confirmed non-usage.

**Hook latency**: `session_start_hook.py` called `nx pm resume` and
`nx pm status` on every session start. Both commands failed silently when
PM was not initialized (exit code non-zero, empty output), wasting ~2s
of hook time per session.

**Scope**: 48 files changed; 1802 lines deleted, 53 inserted. Tests went
from 1812 (post archive/restore removal) to 1724 (after PM removal) —
net decrease reflects deleted PM test files.

## Consequences

- `nx pm` commands no longer available (nx pm init/status/resume/block/unblock/phase/search/expire)
- Session hooks are simpler and faster (no PM detection path)
- Project-management-setup agent removed from plugin
- T2 memory (`nx memory put/get/list/search`) unchanged and fully functional
- RDR integration unchanged
