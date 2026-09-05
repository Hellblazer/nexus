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

## Mechanism of the MCP-scope handoff (nexus-d76vc, 2026-08-07)

The MCP protocol carries no per-request session id, so a long-lived MCP
server samples the id once at spawn. The handoff supplies the missing
signal instead of accepting the freeze. The conexus SessionStart hook
(`nx hook session-start`, matcher `startup|resume|clear|compact`) writes
`~/.config/nexus/t1_handoff.<mcp_pid>` naming the NEW session id on
`source=clear` or `source=resume` only, for every live `nx-mcp` /
`nx-mcp-catalog` sibling of the hook's own claude ancestor
(`nexus.session.find_mcp_sibling_pids`); `startup` spawns fresh servers
and `compact` keeps the id, so neither writes one. The MCP lifespan's
watcher (`nexus.mcp.core._t1_handoff_watch_loop`, its own poll,
`_T1_HANDOFF_WATCH_INTERVAL_S` = 5s, independent of the hours-scale token
refresh) re-derives its OWN ancestry rather than trusting the marker,
validates the id and freshness, then re-leases: mint-or-borrow through
`nexus.db.t1._lock_guarded_mint_or_borrow`, swap `NX_T1_SESSION` /
`NX_T1_SESSION_ID`, drop the T1 singleton
(`nexus.mcp_infra.reset_t1_for_release`), stop refreshing the old lease.
A rejected marker is logged and deleted, never retried forever. The old
session's rows are not migrated: `/clear` separated two conversations on
purpose. Agent-tool subagents need no handling because they share the
parent's MCP process and see the process-wide swap.

## Standing falsification: respawn-on-`/clear` is FALSE (nexus-ggvi0, 2026-08-22)

A proposal to delete the handoff layer on the premise that Claude Code
respawns MCP servers on every `/clear` / `/resume` was disproved from the
live install's `~/.config/nexus/logs/mcp.log`: `t1_handoff_released`
events show the SAME `mcp_pid` releasing DIFFERENT `old_session_id`s hours
apart. Anyone re-proposing the deletion must first re-prove respawn on the
then-current Claude Code by the same grep. Inventory and the seven
guarantees a re-proposal must test: T2
`nexus/s1-t1-identity-inventory-2026-08-22` [23344].

## Measured record

Probes of 2026-08-03 (nexus-aj564; T2
`nexus/subagent-reliability-findings-2026-08-03` [21371], homework
[21370]): `nx scratch list` returned 2 entries while the MCP scratch list
returned 39 in the same instant and conversation; the 2 were the
`review-completed` markers, a convention that had adapted to the split
before the split was understood. The lesson "prior-session T1 is never
searchable" was traced to a timing race, not a scope mix-up: the search
ran before the writing agent had terminated, and the write landed moments
later in the scope already checked (T2 [21373] reproduces the class).
Confirm an agent has terminated before declaring its write-back lost.

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
  bullet (a summary and this pointer).
- `docs/architecture.md` § T1's three scopes and the CLI/MCP split-brain
  (a summary and this pointer).
- `conexus/hooks/scripts/pre_close_verification_hook.sh`, whose
  write-side contract is consequence 3.

## Revision History

- 2026-09-04: Created (nexus-vuiid) from the seam AGENTS.md and
  docs/architecture.md each carried in full; the two texts had already
  drifted once (the "for the life of the MCP process" lesson corrected in
  AGENTS.md on nexus-d76vc). This file is now the rule; both keep their
  prose as explanation and cite this number.
- 2026-09-04: Sam's ruling on the critic's finding [24392] that three
  full copies drift: AGENTS.md and docs/architecture.md are trimmed to a
  summary plus this pointer, and the handoff mechanism (nexus-d76vc), the
  respawn falsification (nexus-ggvi0) and the measured record they carried
  move here, condensed. This file is the only full text.
