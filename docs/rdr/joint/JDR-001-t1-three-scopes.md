---
id: JDR-001
title: "T1 has three scopes; the MCP scope moves only on a consumed handoff marker"
status: active
owners: [RDR-105, RDR-149, RDR-184]
created: 2026-09-04
---

# JDR-001: T1 has three scopes; the MCP scope moves only on a consumed handoff marker

## Decision

T1 (session scratch) is one PG-backed store reached through three scopes
that exist at the same time, and code that reads or writes T1 names which
one it means:

1. **MCP-tool T1** (`mcp__plugin_conexus_nexus__scratch`) is scoped to the
   session id the MCP server leased at spawn. It moves only when the
   SessionStart hook writes a `~/.config/nexus/t1_handoff.<mcp_pid>` marker
   on `/clear` or `/resume` and the MCP lifespan's watcher consumes it
   (nexus-d76vc); between markers it is frozen. Agent-tool subagents share
   it with their parent at any nesting depth.
2. **`nx` CLI T1** (`nx scratch`) is scoped to the current transcript
   session when a live `t1_session_lease.<sid>` exists. An explicit
   `NX_SESSION_ID` / `CLAUDE_CODE_SESSION_ID` with no usable lease fails
   loud (`T1ServerNotFoundError`, nexus-f7xyq); a bare invocation falls
   through to the shared CLI identity by design.
3. **`~/.config/nexus/current_session`** is a machine-wide last-writer-wins
   file, the tier-4 fallback of `resolve_active_session_id()`. Any
   concurrent session can own it; it is never a per-conversation value.

Consequences every owner relies on: a `claude -p` subprocess defaults to
its own leased scope (`owned`) and opts into the parent's with
`share_t1=True`; cross-process findings between sibling subprocesses go to
T2, never T1; the bead-close gate reads `review-completed` markers through
the CLI scope, so a marker written only through the MCP tool is invisible
to it; prior-conversation T1 is readable through MCP tools only for one
handoff poll tick after `/clear`, and the old session's rows strand under
the old id rather than migrating.

## Owners

- RDR-105 (`rdr-105-t1-chroma-architecture-env-passdown.md`): the
  sub-agent contract that first defined owned / shared / ephemeral T1.
- RDR-149 (`rdr-149-unified-service-registry-substrate.md`): the shared
  lifecycle primitive T1 leased through until the daemon leg was retired
  (nexus-8zfwv); its self-heal rules are what scope 2 inherits.
- RDR-184 (`rdr-184-orchestration-protocol-hardening.md`): the
  orchestration protocol whose review-completed markers and cross-process
  write-backs depend on knowing which scope a writer is in.

## Cited by

- `AGENTS.md` § T1 sub-agent contract (RDR-105), the "three T1 scopes"
  bullet and its corrected lesson.
- `docs/architecture.md` § T1's three scopes and the CLI/MCP split-brain
  (nexus-aj564), the measured record this rule was extracted from.
- `conexus/hooks/scripts/pre_close_verification_hook.sh`, whose
  write-side contract is consequence 3.

## Revision History

- 2026-09-04: Created (nexus-vuiid) from the seam AGENTS.md and
  docs/architecture.md each carried in full; the two texts had already
  drifted once (the "for the life of the MCP process" lesson corrected in
  AGENTS.md on nexus-d76vc). This file is now the rule; both keep their
  prose as explanation and cite this number.
