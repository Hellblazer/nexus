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
   on `/clear`, `/resume` or a fork (`/branch` and `--fork-session`, which
   Claude Code reports as `source=fork`) and the MCP lifespan's watcher
   consumes it (nexus-d76vc; fork added at nexus-kdxyv, Sam 2026-09-18);
   between markers it is frozen. A fork's rows do not migrate, exactly as
   a `/clear`'s do not: scratch written before the fork stays under the
   parent's session id. Agent-tool subagents share
   it with their parent at any nesting depth.
2. **`nx` CLI T1** (`nx scratch`) is scoped to the current transcript
   session when a live `t1_session_lease.<sid>` exists. An explicit
   `NX_SESSION_ID` / `CLAUDE_CODE_SESSION_ID` with no usable lease fails
   loud (`T1ServerNotFoundError`, nexus-f7xyq); a bare invocation falls
   through to the shared CLI identity by design.
3. **`~/.config/nexus/current_session`** is a machine-wide last-writer-wins
   file, the tier-4 fallback of `resolve_active_session_id()`. Any
   concurrent session can own it; it is never a per-conversation value.
   An MCP server that Claude Code did not start never takes its T1 scope
   from it (nexus-xs7fp, below).

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
(`nx hook session-start`, matcher `startup|resume|clear|compact|fork`)
writes `~/.config/nexus/t1_handoff.<mcp_pid>` naming the NEW session id on
`source=clear`, `source=resume` or `source=fork` only, for every live `nx-mcp` /
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

A process that BORROWED its lease (`USE_LEASED`, the mint-race loser, a
deferred mint or a handoff that borrowed) shares the owner's token, and the
owner's teardown revokes it. The borrower's next T1 call is HTTP 401; once the
lease re-read and the bearer re-mint have both failed to cure it, the borrower
re-mints its own token for the same session id under the same mint flock
(`nexus.mcp.core._recover_t1_session`, nexus-k9sec, Sam's decision B
2026-09-29) and becomes the owner: its own teardown then revokes and clears
through `_OWNED_T1_SESSION`. A recoverer that loses the flock race adopts the
winner's lease and keeps its role, so N recoverers mint once. Only a process
recorded as owner or borrower of that session id may mint; teardown keys on
ownership alone, so a borrower never revokes or clears what it does not own.

The 401 body is `{"error":"unauthorized"}` for every cause, so a client cannot
tell a revoked token from an expired one, and a live owner can be displaced:
its token expires before its refresh tick, or a lease publish is late or fails
and a borrower re-mints under it. Two things bound the damage. An OWNER whose
token 401s recovers the same way (same flock, same `stale_token` rule), so a
successor's later revoke-by-session-id does not leave it dead. Every recovery
mint, failed or completed, starts a 30s per-session cooldown, and at most 3
recovery mints run per 10 minutes, so two processes that each think the
other's token is dead cannot rotate each other faster than that. Both limits
are per process: two processes can mint up to 6 times per 10 minutes between
them. Owner
teardown revokes and clears only when the published lease names a token this
process minted (`_OWNED_T1_MINTED`, not `NX_T1_SESSION`, which the store's
own 401 self-heal overwrites with an adopted token); when the lease names a
successor's token it leaves lease and token alone. What remains: a displaced
live owner and its displacer both keep a refresh loop and settle on whichever
minted last, the other adopting that lease on its next 401, so the churn is
one rotation per refresh tick, not an outage; and a token that was never the
cause of a 401 costs at most one mint per cooldown before the store's own
60s latch (`_RECOVERY_FUTILE_WINDOW_S`) stops asking.

Rows. A revoke does not hide a session's rows from a new token for the same
id (they are keyed by tenant and session id; measured against the engine).
The owner's clean exit no longer deletes them: it used to, which emptied the
pad before the flagged-row drain ran (so flagged notes were lost on the
stdin-EOF path) and deleted it under the surviving borrower. The SIGTERM path
never deleted rows, and the engine's scheduled sweep
(`NexusService.runScheduledSweep`: 24h TTL, run every 6h) reaps what nobody
claims, so a recovering borrower inherits the owner's pad. Unflagged scratch
now outlives a clean owner exit by that bound, about 30h at worst (24h plus
up to one 6h sweep interval). Nothing migrates rows.

Two residuals are accepted and not closed. A recovery that starts while
SIGTERM is already tearing the process down can leave a minted token and its
lease unowned: `_t1_shutdown` returns early when nothing is owned and cannot
set the shutdown flag then, because the SIGTERM handler treats that flag as
"someone else is exiting" and would return without exiting; the token ages
out on its TTL. And owner teardown unlinks the per-session mint-lock file
when it clears the lease, so a same-id process that starts between that
clear and the revoke can mint under a fresh lock inode and then have its
token revoked by the owner's revoke-by-session-id; the displaced-teardown
guard narrows the window but does not close every ordering.

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

## Scope 3 is a T1 source for Claude Code hosts only, for an MCP server (nexus-xs7fp, 2026-10-17)

`nx-mcp` under Cursor, Codex CLI or Gemini CLI has neither `NX_SESSION_ID`
nor `CLAUDE_CODE_SESSION_ID`, so tier 4 used to hand it whichever Claude
Code session wrote the file last, possibly one that had ended: the host's
scratch landed in a Claude conversation's scope. Measured on live
processes (presence of variables only): the `nx-mcp` that the Claude Code
CLI spawns carries `CLAUDE_CODE_SESSION_ID`, `CLAUDECODE` and
`CLAUDE_CODE_ENTRYPOINT`; the one Claude Desktop spawns for a chat carries
none of them. So tier 3 already answers for every Claude Code MCP server
and tier 4 was never what served them.

An MCP server process (it calls `session.mark_mcp_server_process()` from
`main()`) now takes a T1 session id from the file only when `CLAUDECODE` or
`CLAUDE_CODE_ENTRYPOINT` is set, which keeps a Claude Code launch that
drops `CLAUDE_CODE_SESSION_ID` working. Otherwise it resolves no session
id and takes the existing fail-loud branch of `_t1_lifespan` ("T1
unavailable this process"); `NX_SESSION_ID` in the host's server entry
names a scope. The CLI in a terminal, the hooks and `claude -p` children
are not MCP server processes and keep the file.

The gate sits in `resolve_active_session_id` tier 4 only. T2 attribution
(`resolve_attribution`, reached by `memory_put`) resolves through
`resolve_active_session_id` and so inherits the gate: a gated server's rows
carry a NULL `session` label (nexus-d8yul). Only operator dispatch (the
`NX_SESSION_ID` injected into `claude -p` children) keeps a raw file read,
as its last tier after `NX_SESSION_ID` and `CLAUDE_CODE_SESSION_ID`: a child
`claude -p` with no injected `NX_SESSION_ID` makes its SessionStart hook
overwrite the file. That read is mostly a label for the child; on a
dispatch-level T1 mint failure the forwarded `NX_SESSION_ID` lets the child
borrow that session's lease (nexus-ylof9). A bare CLI in a terminal still
labels its T2 writes with the file's last writer; the CLI relies on the file
here.

Rejected: gating the resolver for every process (a bare `nx scratch` beside
a Claude session relies on the file); a liveness check (a live session is
still not this host's); a host-local minted id (the locked nexus-4lkmz
decision 2 is "never a shared identity, never a silent private store", and
a host-local id is either shared across that host's servers or private to
one process); walking the process tree for a `claude` ancestor (the
binary's process name is its version number on some installs).

Residual: a host launched from inside a Claude Code shell inherits that
shell's `CLAUDE_CODE_SESSION_ID` (Claude Code sets it on every subprocess,
Bash included), so tier 3 serves it before the file or this gate is
consulted, and it keeps the launching session's scope as it did before.
The gate cannot close that case; it is the cost of an env-based
discriminator. Claude Desktop chat, which has no Claude Code session, now
gets T1 unavailable instead of borrowing one. Cowork is measured too (2026-10-21, a Cowork/desktop session through the remote-devices bridge): T1 unavailable. Claude Desktop sets no session id for the server it spawns, but the conversation's cloud container carries `CLAUDE_CODE_SESSION_ID`, `CLAUDECODE=1` and `CLAUDE_CODE_ENTRYPOINT=remote_cowork` together with `CLAUDE_CODE_SKIP_PLUGIN_MCP_SERVERS=1`, so `nx-mcp` runs on the Mac, spawned by Desktop, and never sees them.

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
- 2026-09-18: A fork (`/branch`, `--fork-session`) joins `/clear` and
  `/resume` as a source that moves MCP-tool T1 (bead nexus-kdxyv; Sam's
  ruling the same day). Claude Code has reported forks to SessionStart as
  `source=fork` since 2.1.213, and the conexus matcher
  `startup|resume|clear|compact` had excluded it, so a fork fired no hook
  at all: the session marker kept naming the parent, and the parent's
  channel waiter kept pushing the parent's mail into the fork. The same
  bead makes the handoff re-key the channel waiter and the subscription
  set, which it had never done. Recorded here rather than only in the
  bead because this file is the rule and its enumeration was short by one.
  conexus was notified the same day (the joint half of this record).
- 2026-10-17: Scope 3 is a T1 source for Claude Code hosts only, for an
  MCP server (bead nexus-xs7fp): `nx-mcp` under Cursor, Codex or Gemini no
  longer borrows the last Claude session's scope through
  `current_session`. T2 attribution still reads the file for a label
  (nexus-d8yul). See "Scope 3 is a T1 source for Claude Code hosts only".
- 2026-10-17 (later): nexus-d8yul closed the T2 attribution clause of the
  line above. T2 attribution now resolves through
  `resolve_active_session_id` and inherits the gate (a gated server's rows
  carry NULL); only operator dispatch keeps a raw file read, as its last
  tier. A bare CLI in a terminal still labels with the file's last writer.
