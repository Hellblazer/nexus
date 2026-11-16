---
title: "Session Identity: Named Seats That Outlive Sessions, a Binding Log, One Resolver, and Fenced Identity State"
id: RDR-253
type: Architecture
status: draft
priority: high
author: Hal Hildebrand
reviewed-by: pending
created: 2026-11-16
accepted_date:
related_issues: [nexus-65a9k, nexus-gff3g, nexus-5daww, nexus-fe96p, nexus-r0d37, nexus-dmyrm, nexus-d8yul]
related: [RDR-208, RDR-252, RDR-245, JDR-001, JDR-002]
---

# RDR-253: Session Identity

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.
> Prose: see REGISTER.md beside this template. Write for a smart reader who
> may not know the jargon; define terms on first use; simplified, never
> simplistic.

Terms. A **session** is one running Claude Code instance; about a dozen run at
once on Sam's laptop, each for days. A session is **compacted** (its context
summarised), **cleared** (`/clear`: a new session id, same process),
**resumed** (`/resume`: an old session id brought back, sometimes in a new
process), **restarted** (by `claude-restart-sessions`) and **renamed**
(`/rename`). The **engine** is nexus's Java service; it stores **tuples**
(small records addressed by a **subspace** such as `mailbox/marginalia`, each
family described by a **template**). **T1** is session scratch, **T2** is
persistent notes, the **threads board** is one T2 entry per session that Sam
reads. A **token** is a credential the engine issues; a **stamp** is the
session id the engine records on a write made with one. A **seat** is this
RDR's new term: a long-lived working identity such as `marginalia` or
`halcyon`, which many sessions occupy one after another. A **generation** is
a count of the processes that have held a seat. To **fence** a write is to
refuse it because it comes from a process that no longer holds the seat. A
**compare-and-set** writes a value only if the current value is still the one
the writer read, so two writers racing cannot both succeed.

Origin. Sam, 2026-11-16, after RDR-252 failed two gate rounds on identity:
"believe we must nail identity ... let's not solve the identity problem but
solve *our* identity problem"; "identity should have stuff like own t2
records, scratch, persistent id, some such stuff to keep long term coherence
beyond a name".

## Problem Statement

Nothing in our stack says, durably, who a session is. Twenty-five separate
identifiers answer "which session is this" (research 2), none survives every
event a session goes through, and the engine trusts only two of them. State
hangs off whichever identifier was handy when it was written, so it strands,
merges or misattributes when that identifier changes. Forty-six incidents
from 2026-06 to 2026-11-16 trace to this (research 1), including the deletion
of a live session's whole scratch scope in a released version.

### Enumerated gaps to close

#### Gap 1: Several sources answer "who am I", and they disagree

`NX_SESSION_ID`, `CLAUDE_CODE_SESSION_ID`, the machine-wide
`~/.config/nexus/current_session` file (last writer wins), the hook payload,
the MCP server's spawn-time id and the Claude registry file can all name
different sessions (research 2, conflicts C1, C2, gap G5). Incidents: T1
unreachable from the CLI (nexus-gff3g, P0), T2 rows labelled with another
window's session (nexus-d8yul), a token and a `from` naming different ids
after `/clear` (research 1, I-19).

#### Gap 2: State is keyed to an identifier that does not survive the events it must

Session ids change on `/clear`; names change on each process start unless
`-n` is passed, on `/rename`, and can be held by two sessions; the threads
entry's `session:` line is written by hand and nothing rewrites it (research
2, G1, G3). Incidents: mail stranded at an old id or delivered to two
sessions or the wrong one (research 1, I-10, I-21, I-25 to I-28); 11 of 16
Desktop compactions lost their checkpoint (nexus-dmyrm); a stale board
re-sent closed work (I-35).

#### Gap 3: A child acts with its parent's authority

A `claude -p` child inherits the parent's `NX_SESSION_ID` and is attributed to
the parent (research 2, G9); a subagent shares the parent's token (JDR-001).
Incident: a child's session end deleted its parent's entire T1 scope in a
released version (GH #1454, nexus-65a9k, P0); a nested child rotated the
parent's token (nexus-5daww, P0). This RDR closes the gap for `claude -p`
children; Agent-tool subagents stay attributed to the parent by decision,
until Claude Code identifies their calls (Critical Assumption 3).

#### Gap 4: Liveness and ownership are guessed from pid, name, cwd or git user

Incidents: another session's tmux server killed by name pattern (I-36); a live
agent's worktree removed because `ps` said it had gone (I-44); a session that
had run `/clear` answered "ready" while its old wait loop still ran (I-38);
every bead is claimed as the git user, so nothing records which session owns
it (I-42). A restart round leaves two holders of a name for 300 seconds
(I-22).

#### Gap 5: Writes carry no trustworthy attribution

Only the MCP server holds a session token; hooks, every Bash child (every
`nx` typed in a shell, the push script) and subagents write unstamped
(research 2, section 3). And any holder of the tenant token can mint a
session token for any session id (`SessionTokenHandler.java:97-118`), so the
stamp means "presented a token for this id". Incidents: reply squatting and
forged `from` verified on develop (nexus-b747g); RDR-252's push records would
all be unstamped (`nexus_rdr/252-fix-check-cee51c460fc1`).

## Relationship to Prior RDRs

- **RDR-208** (session-id mail addressing, closed): its identity table is the
  starting point; this RDR replaces "the session id is the address" with "the
  seat is the address".
- **JDR-001** (T1 three scopes): T1's session scope stays; this RDR adds
  identity-scoped durable scratch beside it and extends the handoff marker;
  JDR-001 gains a revision entry (Sam's edit).
- **JDR-002** (tuple sender claims): the stamp stays a label everywhere except
  identity-owned subspaces, where a superseded generation is refused (Sam,
  2026-11-16); JDR-002 gains a revision entry, Sam's edit.
- **RDR-252** (aura groups and one responder, draft): parked until this lands;
  its memberships, owners and push records name identities.
- **RDR-245** (structured asks): asks are filed under the asking seat.

## Context

### Background

Sessions are long-lived and many. A seat such as `marginalia` carries work
across days: a board entry, worktrees, beads, mail, asks, background runs.
Today that continuity exists only as convention (keep the board's `session:`
line current, name sessions with `-n`, choose your own name and check it is
free) and breaks whenever a convention is missed. Sam reads the board and
answers asks by seat name; peers address each other by seat name; the code
addresses by session id.

### Technical Environment

- Claude Code 2.1.325 (the installed binary is the source of truth; the
  `~/git/claude-code` tree is a March snapshot): the session id is minted at
  start, regenerated by `/clear`, switched by `/resume`; every Bash child
  carries `CLAUDE_CODE_SESSION_ID` set to the current id; hooks receive
  `session_id` and a `source` (startup, resume, clear, compact) on stdin; the
  registry file `~/.claude/sessions/<pid>.json` records id, name, pane and a
  process start time, and is deleted on exit (research 2).
- nexus: the MCP server mints a session token at start (`core.py:951`) and
  keeps a 60-second `directory/<name>` heartbeat (`subscriptions.py`); a
  four-tier resolver picks a session id (`session.py:121-203`); hooks write
  `current_session`, session markers and T1 handoff markers.
- The engine: tokens map to a tenant and, for session tokens, a session id;
  `sender_session` is stamped only for a minted session token
  (`TupleHandler.java:279-290`).

## Research Findings

### Investigation

Four read-only lanes on 2026-11-16, synthesised in T3 `agent-orchestration`,
"Our session identity problem: four-lane research synthesis (2026-11-16)":
lane A, 46 incidents from T2, beads, memory files, transcripts and
checkpoints (research 1); lane B, a census of every identifier from source
and the installed binary (research 2); lane C, identity models in Sam's own
code (research 3); lane D, outside systems (research 4).

### Key Discoveries

1. The session id is a key, not an identity. Stereotomy, Sam's KERI-style
   identifier library, keeps a stable identifier and rotates keys under an
   append-only event log, each rotation proving the prior one committed to it
   (`apollo/stereotomy/.../processing/Validator.java:101-110`). Our `/clear`,
   resume and restart are rotations; nothing records them (research 3).
2. Identity and activation are separate things. Orleans keeps a grain's
   identity while activations come and go, at most one at a time; Erlang
   re-points a registered name at a new process atomically
   (`re_register_name`). Using the display name as identity, as
   `claude-squad` does (`session/instance.go:31-33, 205`), is the failure
   pattern we have lived (research 4).
3. A new holder must advance an epoch. Fireflies (the membership and
   failure-detection layer of Sam's Apollo) filters a note whose epoch is not
   newer than the current one (`apollo/fireflies/.../View.java:297-301`); a
   generation number per holding process lets the engine refuse a superseded
   process's writes, which closes the two-holders window (research 3, 4).
4. Ids come from a monotone, never-reused allocator with tombstones, as
   nexus's catalog tumblers already do (`src/nexus/catalog/tumbler.py:142`)
   (research 3).
5. Only the MCP server can hold a credential today, and every Bash child
   already carries the current session id in `CLAUDE_CODE_SESSION_ID`
   (research 2). So a resolver can map any process to its identity through
   the binding log, with only a token file and two variables added
   (`NX_SEAT`, `NX_IDENTITY_TOKEN`).
6. Our threat is a confused model, not a hostile party (JDR-002). Signatures,
   witnesses, quorum admission, failure-detector math and attestation chains
   are out (research 3, 4).

### Critical Assumptions

- [x] On `/clear`, the SessionStart hook can tie the new session id to the
  prior binding by process continuity (the same Claude pid and process start
  time as the current binding). **Status**: Verified by spike
  (`nexus_rdr/253-research-5`): the `clear` hook ran under the same Claude
  pid and start time, its payload carried the new id with `source: clear`,
  and the registry file already held the new id with pid, `procStart`, pane
  and name unchanged. A `/resume` in a new process kept the session id
  (`source: resume`) and restored the name; name drift on resume comes from
  a shell wrapper passing a fresh `-n`, not from Claude Code. The registry's
  `startedAt` was the same before and after the `/clear` (1794848034834), so
  the `startedAt` that keys a process is stable across `/clear`; an in-place
  `/restart` changes it (`nexus_rdr/253-research-10` (1)).
- [x] Every Bash child of a session carries the current session id in
  `CLAUDE_CODE_SESSION_ID`. **Status**: Verified by binary read and observed
  in a subagent's environment (research 2, row 2).
- [refuted] The MCP server can tell a call made by an Agent-tool subagent from one
  made by its parent. **Status**: Refuted for Claude Code 2.1.325
  (`nexus_rdr/253-research-6`): of every tool call nexus logged, none carried
  `claudecode/agentId` in `_meta`, including the research subagents' calls,
  though the binary defines that key. The fallback applies: delegation covers
  `claude -p` children; subagent writes stay attributed to the parent seat
  with an `agent` label, as today. Re-checked on each Claude Code upgrade.
- [partly] A Claude Desktop session can run the SessionStart hook and bind.
  **Status**: Verified in part, from records. The hook runs on Desktop: the
  2026-11-03 compaction audit saw SessionStart hooks fire after all 16
  Desktop compactions (T2 `nexus/compaction-restore-audit-2026-11-03`), and
  the hook receives the session id on stdin. But the Desktop-spawned MCP
  server never sees the session id (JDR-002, F17, nexus-wkjqm), so it cannot
  pick up the identity token. A Desktop seat therefore binds through its
  hook, and its MCP-side writes stay unattested labels, never fenced, until a
  bridge carries the session id to that server.

## Proposed Solution

### Approach

1. **Seats.** An identity is an engine record with an opaque id that is never
   reused and one or more names. A seat is an identity Sam or the restart
   script creates once by name; a launch that names no seat, or gives `-n`
   a name that is not a seat's, gets an ephemeral identity that can be
   promoted to a seat (Sam, 2026-11-16).
2. **A binding log.** Each time a session takes, keeps or loses a seat
   (start, resume, clear, fork, take-over, release, rename, end, lapse,
   revive), the engine appends a binding
   row. A generation counts the processes that have held the seat: it rises
   only when a different process takes it.
3. **One resolver.** Every surface (MCP server, hooks, CLI, scripts) asks one
   function "which identity is this process", passing the session id it can
   see; the function answers from the binding log and refuses when two
   sources disagree.
4. **Fenced identity state.** On four families of identity-owned state the
   engine checks that a write comes from the owner and from its current
   generation (Sam, 2026-11-16, decision 2 as amended the same day); anyone
   may still send a seat mail. Elsewhere the stamp stays a label.
5. **Delegated children.** A Bash child acts as its session's identity; a
   `claude -p` child gets a delegated sub-identity that may read the parent's
   state and write only its own. Agent-tool subagents keep the parent's seat
   with an `agent` label until Claude Code identifies their calls (Sam,
   2026-11-16, narrowed the same day).
6. **State moves onto the identity.** Phase 1 moves the threads entry, the
   mailbox and names, durable scratch and per-identity T2, and group
   memberships and push records (Sam, 2026-11-16).

### Technical Design

**Identity records.** New engine templates, all tenant-scoped:

```
identity/<id>    dims: kind=seat|ephemeral|delegated  parent=<id or none>  generation=<n>  state=unheld|live|lapsed|ended
                 body: {"display_name": "<name or null>", "created_by": "<sam|session|dispatch>", "created_at": "<UTC>", "ended_at": null}
alias/<name>     dims: identity=<id>  state=live|retired
                 body: {"since": "<UTC>", "retired_at": null, "renamed_to": null}
binding/<id>     dims: event=start|clear|resume|fork|takeover|release|rename|end|lapse|revive  generation=<n>  lease=leased|unleased
                 body: {"session_id": "<uuid>", "host": "<hostname>", "pid": <n>, "started_at": <ms>,
                        "pane": "<tmux pane or null>", "client_version": "<conexus version>",
                        "prior_session_id": "<uuid or null>", "at": "<UTC>"}
session/<session id>   dims: identity=<id>  generation=<n>        (the session index)
member/<id>      dims: group=<name>  role=<leader|member>          (memberships; RDR-252 re-keys onto this)
work/<id>        dims: kind=<watcher|wait|labrun|worktree|tmux>  ref=<text>  state=running|done   (background work and owned resources; a label, not fenced)
```

The identity record holds the current generation and state, so pruning
binding rows never loses them. The id is `i<n>`, allocated by the engine
from a per-tenant counter that only grows, so an id never names a second
identity; names matching `i<digits>` are reserved and cannot be aliases. An
alias names one live identity at a time.

**A generation is one process holding a seat** (Sam, 2026-11-16, decision 2 as
amended). A process is one start of a Claude Code process, known by its host,
its pid and the `startedAt` its registry file records. Claude Code's in-place
`/restart` keeps the pid and the OS start time but writes a new `startedAt`,
which is how the restart script already tells a new start
(`nexus_rdr/253-research-9` (a); measured in `nexus_rdr/253-research-10` (1)). A seat's state is `unheld` when created or released, `live` while its
holder renews, `lapsed` after 300 seconds without renewal, and `ended` after a
clean exit. The generation goes up only when a different process takes the seat: a
launch, a resume in a new process, or an explicit take-over. Inside one
process the session id can change (`/clear`, `fork`, `/branch`) without
changing the generation: the binding log appends a row with the new session
id under the same generation, and the session index maps both ids to it. So
the MCP server's token, and every Bash child of the process, stay on the
current generation across `/clear`, which the `/clear` spike shows happens in
the same process (`nexus_rdr/253-research-5`). The generation check stops a
superseded process: the old process after a restart, a resume elsewhere or a
take-over.

**Binding.** One engine route, `bind`, takes the seat (or identity), the
caller's evidence (session id, host, pid, `startedAt`, pane, client version,
whether it holds a lease, and a `holder_gone` verdict naming the host, pid and
`startedAt` of the holder it found dead) and the generation it expects; it appends a binding row under a
compare-and-set (the write happens only if the expected generation is still
current, so two simultaneous binds cannot both win) and returns an identity
token carrying identity and generation. Its callers are the SessionStart
hook, the `nx identity` commands, the MCP server (renewal, refresh, `end`, the
`/branch` fork row) and the `nx` operator dispatcher (a delegated identity's
`start`, renewal and `end`); the hook and the commands authenticate with the
tenant credential the `nx` CLI already uses, the MCP server with the data
credential it holds. Any holder of that
credential can bind any seat, as any can mint a session token today
(`SessionTokenHandler.java:97-118`): the bind guards against a confused
session, not a hostile one, as JDR-002 says.

A seat is named by `NX_SEAT`, or by a `-n` that exactly matches a live seat
alias; `NX_SEAT` wins when both are given. A `-n` that matches no seat is a
display name, and the session gets an ephemeral identity that records it as
`display_name` and titles its board entry `<display name> (i<n>)`, but takes
no alias, so mail reaches it by id only: aliases belong to seats, so two launches with one
display name never collide and no name is consumed. `nx identity promote
<id> <name>` makes it a seat with that alias (Sam, 2026-11-16). The
policy, with the result for the session in the last column:

| Hook `source` | Seat state | Result |
|---|---|---|
| `startup`, a seat named | unheld or ended, or its holder no longer exists | bound, new generation, event `start` |
| `startup`, a seat named | lapsed, holder still running (same host), or lapsed less than 10 minutes ago (another host) | ephemeral, as for a live holder |
| `startup`, `NX_SEAT` naming no seat | n/a | ephemeral; the hook lists the seats |
| `startup`, a seat named | live in another process | ephemeral; the hook prints the holder and, in an interactive session only, the remedy `nx identity claim <seat> --take-over` |
| `startup`, no seat named | n/a | ephemeral, event `start` |
| `resume` | the resumed id's seat is unheld or ended, or its holder no longer exists | bound, new generation if the process is new, event `resume` |
| `resume` | live in this same process | unchanged |
| `resume` in a process that already holds a different identity | the resumed seat unheld | the process switches: its current identity is closed (`release` for a seat, `end` for an ephemeral), then it is bound to the resumed seat with a new generation; the handoff marker carries the new identity, generation, session id and token-file path, and the MCP server's heartbeat re-read covers a missed marker; Bash children started before the switch keep the old session id and are refused by the resolver until restarted |
| `resume` in a process that already holds a different identity | the resumed seat live elsewhere | unchanged: the process keeps its identity, and the hook prints the holder |
| `resume` | live in another process | ephemeral, with the holder (and, interactively, the remedy), as for `startup` |
| `resume` | the id is in no index (a session from before this RDR), or its identity has ended | treated as `startup` with the seat the launch names |
| `clear`, `fork` | live or lapsed in this process | same generation, new session id, event `clear` or `fork`; a lapsed seat is revived |
| `compact` | any | unchanged; no binding row |
| no hook (`/branch`) | live in this process | the process's token file still applies, so its Bash children keep writing; on its next renewal the MCP server maps the unknown session id through the registry's pid and `startedAt` and appends a `fork` row; the resolver never writes |

A `--fork-session` launch is a new process: it binds by the `startup` or
`resume` rows like any launch. After a `fork`, mail already in the seat's
`mailbox/<id>` follows the seat into the forked conversation; mail in the old
`mailbox/<session id>` stays where RDR-208 put it.

**A holder that no longer exists.** The check is the client's: the engine
cannot read a laptop's files, least of all in cloud mode. Before binding, the
hook (or `nx identity claim`) reads the current holder from the engine and,
when the holder is on its own host, looks for a registry file naming the
holder's pid with the holder's `startedAt` and probes that pid. If there is
no such file, or the pid is not running, the holder no longer exists (it
exited, crashed, was replaced by `/restart`, or the machine rebooted;
`nexus_rdr/253-research-9` (a), (b), `253-research-10` (1)), and the client
sends `holder_gone` with the holder it checked. The engine records the
verdict in the binding row and treats the seat as `unheld`, for any lease,
`unleased` included; the compare-and-set on the generation still decides a
race. A probe, not file presence alone, is what catches a crash, since a
crashed process leaves its registry file behind. This one rule covers every restart mode: an exit
and relaunch, an in-place `/restart` (same pid, a new `startedAt`; its hook
fires `source: resume`, `253-research-10` (1)) and a `--revive` after a reboot
(no registry files at all) all find the old holder gone and bind by the
table's `resume` row with a new generation. The restart script needs no
release step. A holder on another host is judged by its lease alone: lapsed
plus the 10-minute reservation. An in-place `/restart` of a conversation with
no exchange yet starts a new session id with `source: startup`
(`253-research-10` (2)); such a session had nothing to keep, and it binds by
the `startup` rows like any launch. The hook may run before its own registry
file is written (`253-research-10` (5)), so it waits up to 5 seconds for the
file before binding.

**Take-over.** `nx identity claim <seat> --take-over` supersedes a live
holder. It runs only from an interactive Claude session: its nearest Claude
ancestor's registry `entrypoint` is `cli` or `claude-desktop`, and a `claude
-p` child, whose `entrypoint` is `sdk-cli` though its `kind` is `interactive`
(`nexus_rdr/253-research-10` (4)), is refused. It is also refused when the
holder's process is an ancestor of the caller's, so no child can take its
parent's seat, and when the caller has no Claude ancestor with a registry
file at all (a plain shell), since `find_immediate_claude_pid` would
otherwise fall back to the shell's parent. The hook prints the take-over remedy only into interactive sessions.
The claim binds the caller's own Claude process (its nearest Claude ancestor,
`nexus_rdr/253-research-9` (c)) as a new generation and writes the token
file. The caller's previous identity is closed: an ephemeral one gets `end`
(its state is then pruned by retention), and a seat the caller held gets
`release`, leaving that seat `unheld`. `nx identity release <seat>` frees a
seat whose holder is on another host and gone; it carries the same guards as
the take-over and is refused while the holder's lease is live. A clean exit appends `end` (the MCP
server, on shutdown, with its expected generation, so a late `end` from a
superseded process is refused); a crash leaves the seat to lapse.

**Handover to the MCP server.** The hook binds, receives the token and writes
it to `~/.config/nexus/identity/<claude pid>.<startedAt>.token` (mode 0600):
one file per process start, so two processes that resume one session id
never share a file, and a superseded process's children never read the new
holder's token. On `clear`,
`resume` and `fork` it also adds the identity, the generation, the session id
and the token file's path (never the token itself) to the T1 handoff marker it
already writes (`t1_handoff.<mcp pid>`, polled every
5 seconds; `nexus_rdr/253-research-7` (a)). On `/clear` the generation is
unchanged, so the MCP server keeps writing with its current token and the
marker gives it the new session id: no write is refused in between. On
every new process start (`startup`, and a `resume`, `/restart` or `--revive`
in a new process), the new MCP server reads the token file of its own Claude
process, found by its nearest Claude ancestor's pid and that process's
registry `startedAt` (`nexus_rdr/253-research-9` (c)), polling once a second
for up to 30 seconds, since the hook and the server start together; it does
not rely on the marker, which a new server would discard
(`nexus_rdr/253-research-8` (b)). If the file has not appeared it runs
unbound and keeps checking on its heartbeat. On every heartbeat the server
also re-reads its process's token file and adopts the identity and
generation it finds there, so an identity switch (a cross-seat `/resume`, a
take-over) reaches it within a minute even if the marker write failed.

**Renewal, refresh, lapse and sleep.** The MCP server renews the binding every
60 seconds on its own heartbeat (the thread that also renews
`directory/<name>`, kept when Phase 2 retires the directory). The identity
token lives one hour, the ceiling for a token minted under the data
credential the MCP server holds (`nexus_rdr/253-research-8` (c)); the server
refreshes it at half life through `bind` with the same generation (a refresh,
not a new binding) and rewrites its process's one token file. A renewal or refresh from a superseded generation is refused, and
the server stops writing identity state and says so. The engine marks a seat
`lapsed`, without changing the generation, when it reads a binding whose last
renewal is more than 300 seconds old; the next renewal from the same process
appends `revive`. On the holder's own host the probe above decides: a running
holder keeps its seat however long its lease has lapsed (a stuck process, a
long sleep), and a gone holder frees it at once. From another host a lapsed
seat stays reserved for 10 minutes after the lapse is due (300 seconds after
the last renewal, not when it is first read), then is free. So a laptop that sleeps lapses every seat, and on wake each
process revives its own within a minute, with no generation change; a stray
launch on the same laptop finds the holder running and gets an ephemeral
identity, however long the sleep. The identity token, which lives one hour,
may expire during a long sleep; the server refreshes it on its first wake
heartbeat, and a Bash `nx` in that window re-reads the file once and, if the
token is still expired, retries on the next call. A Desktop binding is `lease=unleased`: its MCP
server cannot renew (Critical Assumption 4), so it never lapses and shows its
liveness as unknown; it is freed when the holder check finds its process gone
(Desktop writes registry files with `entrypoint: claude-desktop`), or by an
explicit take-over.

**The resolver.** One function, `resolve_identity()`, replaces the four-tier
session resolver and the `current_session` file. Its inputs, in order: an
explicit identity token (a delegated child), the hook payload's session id,
`CLAUDE_CODE_SESSION_ID`. It maps a session id through the session index to
`(identity, generation)`, and an unknown id from a live registry entry
through pid and `startedAt`. If two inputs map to different identities it
refuses with both, never picks. It only reads. A Bash child's `nx` reads the
token file of its nearest Claude ancestor process; on an expired token (HTTP
401) it re-reads the file once, and on a superseded generation (HTTP 409
with error code `generation_superseded`, distinct from the engine's other
409s) it stops and names the holder. On a fenced family a client never
retries without a token, which JDR-002's 401 fallback would otherwise do.

**Fencing.** Two checks (Sam, 2026-11-16, decision 2 as amended). The
ownership check: the token's identity is the family's owner. The generation check: the token's
generation is the owner's current generation. A write to a fenced family
with no identity token is refused, with two exceptions. A send (`out`) to a
mailbox is open to any caller, token or none: `nx ask` answers, the console
and scripts send without one (`nexus_rdr/253-research-9` (e)). And a write to
a family whose owner's current binding is `unleased` (a Desktop seat) is
accepted without an identity token and labelled unattested: the exception is
keyed on the target, since a Desktop MCP server holds no token at all
(JDR-002, F17). A person can remove the fenced state of an
identity whose state is `ended`, or `lapsed` with its holder gone, with `nx
identity purge <id> --operator`: an engine route taking the tenant
credential, refused for a `live` identity, and logged in the engine's audit
log with the caller and what it removed. A tombstoned identity's remaining
rows are already gone.

| Family | Store and handler | Who may write | Checked operations | Checks |
|---|---|---|---|---|
| Mailbox `mailbox/<id>` | tuple space, `TupleHandler` | any identity may send (`out`) | the owner's `in`, `inp`, `ack`, `nack`, `renew`, `release` | ownership and generation; a send is a label, as today |
| Membership `member/<id>` | tuple space, `TupleHandler` | the owner only | `out`, and the claim family used to remove one (`in`, `inp`, `ack`, `nack`, `renew`, `release`) | ownership and generation |
| Threads entry | T2 memory, project `threads`, `MemoryHandler` | the owner only | `put` (including `replaces`), `put_or_merge`, `merge`, `import`, `import_batch`, `delete`, `restore`, `summaries` on an entry carrying an `identity: <id>` line | ownership and generation, read against the stored entry; a new entry may name only the writer's own identity, and a write that removes or changes the line is refused; the tenant-wide sweeps `expire` and `reap` skip such entries |
| Identity scratch | T1, the identity scope, `ScratchHandler` | the owner only; a delegated child reads | `put`, `flag`, `unflag`, `delete`; the scope is never closed by `session/close` and is excluded from `sweep` | ownership and generation |

Reads are not checked, a delegated child's included. Every other subspace, project and scope keeps its
current rule: the stamp is a label (JDR-002), and `ScratchHandler`'s existing
refusal of a mismatched session on the session scope
(`nexus_rdr/253-research-8` (d)) is unchanged; for an identity token that check
accepts any session id the session index maps to the token's identity and
generation, so a session-scope write after `/clear` is not refused. JDR-002 gains a revision entry
naming these four families, and JDR-001 one for the identity T1 scope and the
extended handoff marker (both Sam's edits). The
board keeps `memory_put` as its write path.

**Delegation.** `nx` operator dispatch (`claude -p`) creates a delegated
identity, `kind=delegated parent=<id>`, with generation 1 and one `start`
binding row written by the dispatcher, and passes the child, in place of the parent's token,
`NX_IDENTITY_TOKEN` naming a token file it keeps current; the child's own SessionStart
hook sees the variable and does not bind, and the resolver takes the
explicit token first. The dispatcher renews the delegated binding while the child runs, refreshes
its token by rewriting the file `NX_IDENTITY_TOKEN` names
(`~/.config/nexus/identity/delegated/<id>.token`, mode 0600; the child reads
the token from the file, not from the variable), deletes that file and
appends `end` when the child exits; if the dispatcher dies, renewal stops and the
delegated identity lapses. A delegated
identity reads its parent's identity-owned state and writes only its own.
Agent-tool subagents keep the parent's seat with an `agent` label: their MCP
calls carry nothing that tells them from the parent's (Critical Assumption 3;
Sam accepted this narrowing on 2026-11-16). They get delegation once Claude
Code sends `claudecode/agentId` on tool calls, re-checked on each upgrade.

**Retention.** Seats, their aliases and their identity records are
permanent. Ephemeral and delegated identities are tombstoned 30 days after
they end, or after 30 days spent lapsed with their holder gone (a crashed
session's identity; a seat that lapses in a sleep and revives is not
counted), and an `unleased` ephemeral 30 days after the holder check finds
its process gone: their identity, binding, session-index, membership,
background-work and scratch rows and their threads entry are pruned, their
mailbox is emptied, and the counter keeps their numbers. Token files are
local: the MCP server deletes its own at shutdown, the dispatcher its
delegated ones, and the hook deletes any whose process the holder check finds
gone. A seat's binding rows are kept
for 180 days, never the last one, and its session-index rows as long as the
binding rows they point to. 

**Aliases and renames.** `nx identity rename <old> <new>` (Sam or the seat)
retires the old alias, sets `renamed_to`, appends a `rename` binding row, and
retitles the seat's threads entry in the same engine call, so the board never
holds two entries for one identity. Mail sent to the old name is delivered
for 7 days with a notice naming the new one, then refused with "renamed to
<new> on <date>". A retired name is never reissued: the tombstone is
permanent. Claude Code's `/rename` changes only the display name; the hook
prints a notice at the next SessionStart when the display name and the
seat's alias differ.

**State owned by the identity (Phase 1).**

- **Threads entry.** The entry carries `identity: <id>` and, through Phase 1,
  the existing `session: <id>` line too, because `nx ask` answer routing, the
  console, the due-date hook and auras read it (`nexus_rdr/253-research-8`
  (f)); Step 5 moves each of those readers to the identity line, and Phase 2
  drops the `session:` line. It is titled by the seat's live alias and
  written with `memory_put` under the fencing rule above. The compaction
  checkpoint and Desktop restore find it by identity.
- **Mailbox and names.** Mail goes to `mailbox/<id>`; a name resolves through
  `alias/<name>`. Phase 1 runs a mixed fleet, since old clients keep running
  for days: a sender resolves `alias/<name>` first and sends to
  `mailbox/<id>`, and falls back to `directory/<name>` and
  `mailbox/<session id>` when the seat has no binding by a new client; a new
  client subscribes to both `mailbox/<id>` and its `mailbox/<session id>`
  until Phase 2. `mailbox_send`'s guard against a session-shaped `from` gets
  an identity counterpart.
- **Identity scratch and per-identity T2.** The identity scope holds working
  state that must cross `/clear` and restart but is not worth a permanent
  record; it lives as long as the identity. T2 holds what is permanent:
  records a seat writes carry its identity, and a seat's own notes live in
  project `identity/<id>`, keyed by id so a rename moves nothing.
- **Memberships and push records.** Memberships are `member/<id>`; RDR-252,
  still a draft, re-keys its groups onto them, and its push records carry the
  identity token's stamp, so the owner of a red is a seat.
- **Background work.** Watchers, waits and lab runs register under
  `work/<id>` so the checkpoint lists them by identity after a `/clear`. It is
  a label, not a fenced family: a lab run on another host has no token file.
  Watchers and waits are Bash children of the seat's process and write as the
  seat across `/clear`.
- **Who reads liveness (Gap 4).** `nx identity alive <seat|pid>` answers from
  the binding and the holder check. Three consumers move onto it in Phase 1,
  each reading ownership from `work/<id>`, where a session registers what it
  owns (`kind=worktree` with the path, `kind=tmux` with the server name, and
  its running watchers, waits and lab runs): worktree removal (a worktree is
  removed only when its owning identity is not alive, not when `ps` misses
  it); the sandbox and tmux cleanup helpers (a server is stopped only when its
  owning identity's process is gone, never by name pattern); and the restart
  round's readiness check in the restart script (a session reports its
  `work/<id>` entries in state `running`). Bead ownership stays out:
  Sam ruled on 2026-10-03 that bead claims cannot name a session (project
  memory `project_bead_claims_cannot_name_a_session`; incident I-42 in
  `nexus_rdr/253-research-1`), so beads keep the git user and the board names
  who works on what.

### Existing Infrastructure Audit

| Piece | Exists? | Use |
|---|---|---|
| Session token, `sender_session` stamp | Yes (JDR-002) | Becomes an identity token carrying identity and generation |
| MCP 60-second heartbeat thread | Yes (`subscriptions.py`) | Renews the binding |
| SessionStart hook with `source` | Yes | Binds per the policy table (start, resume, clear, fork) |
| Restart script with `-n` | Yes | Restart unchanged (the new start finds the old holder gone); its readiness check reads `work/<id>` (Step 6) |
| Catalog tumbler allocator | Yes (`catalog/tumbler.py`) | Pattern for the never-reused id counter |
| T1 handoff marker | Yes (`hooks/__init__.py`, `mcp/core.py`) | Carries the binding to the MCP server; kept |
| Directory lease, session markers, `current_session` | Yes | Fallbacks in Phase 1, retired in Phase 2 |
| Identity, alias, binding, session index, membership, background-work records; bind route; four fence checks | No | New, in the engine |
| Resolver, token file, handover, delegation | No | New, in the client |

### Decision Rationale

A persistent identity over better session ids, because the session id
changes by design on `/clear` and the name changes by design on restart: no
choice of one of them can be the identity (Key Discovery 1). A binding log
over a mutable "current holder" field, because every incident class in Gap 2
and Gap 4 needed to know who held a seat when, and a log answers that after
the fact. Generation fencing over leases alone, because a lapsed lease does
not stop the old holder's late writes (research 4, Jini). Named seats over
auto-minted identities, because Sam, peers and the board already think in
seats (Sam, 2026-11-16).

## Alternatives Considered

### Alternative 1: Make the session name the identity and keep it stable

Pass `-n` everywhere, refuse duplicates, and key state by name.

- **Pro:** smallest change; names already exist.
- **Con:** `/clear`, Desktop handles and unnamed launches still break it;
  names are display text that Sam and `/rename` change; RDR-252's two failed
  rounds are this design.
- **Rejected.**

### Alternative 2: Make the session id the identity and carry state across `/clear`

Extend today's handoff markers so every id-keyed thing follows the new id.

- **Pro:** no new record type.
- **Con:** one handoff per kind of state, each a new failure point (I-14,
  I-23); a restart with `--fresh` still loses everything; names stay a
  second, disagreeing source.
- **Rejected.** The chosen design still uses the T1 marker and a token file
  to tell the MCP server and Bash children their binding, but no state rides
  on them: state is keyed to the identity id.

### Alternative 3: Full KERI-style identifiers with signatures

Self-certifying identifiers, signed binding events, witnesses.

- **Pro:** proven in Sam's own stereotomy; resists a hostile party.
- **Con:** our threat is a confused model; key management for a dozen
  sessions on one laptop is cost with no matching risk.
- **Rejected**; the event-log shape is kept without the cryptography.

### Briefly Rejected

- Binding by tmux pane: Desktop has no pane, and a pane is a place, not a
  seat (research 4, the cockpit synthesis's "identity by place").
- Per-identity namespaces enforced in the harness: we cannot enforce them in
  Claude Code.

## Trade-offs

### Consequences

- Every surface resolves identity through one function; the four-tier
  resolver and the `current_session` file go.
- A seat's mail, board entry, scratch, memberships and background work
  survive `/clear`, resume, restart, rename and reboot.
- A superseded process can no longer write a seat's identity-owned state.
- New engine routes and templates mean an engine release before the client
  work can ship.

### Risks and Mitigations

- **A launch names a live seat by mistake**: it runs ephemeral and is told the
  holder; only the one-shot `nx identity claim --take-over` supersedes a live
  process.
- **A crash leaves a seat held**: on the same host the relaunch finds the
  holder's process gone and binds at once; across hosts it waits for the
  lapse, or for a person's `nx identity release`.
- **A child takes its parent's seat**: no inherited variable carries
  take-over authority, and a delegated child's hook does not bind.
- **A token outlives its lifetime**: the MCP server refreshes it at half of
  its one-hour life and rewrites its process's one token file; the CLI
  tells an expired token (401, re-read) from a superseded one (409
  `generation_superseded`, stop).
- **Desktop MCP writes are unattested** (Critical Assumption 4): a Desktop
  seat binds through its hook as `unleased`, and its fenced families accept
  tokenless writes to its fenced families as unattested labels.
- **A mixed fleet strands mail**: senders try the identity mailbox and fall
  back to the session mailbox, and new receivers read both, until Phase 2.

### Failure Modes

- Engine unreachable at start: the session runs unbound, the resolver says
  so, and identity-owned writes fail loudly; nothing falls back to a name or a
  file.
- Token file missing for a Bash child: the CLI refuses identity-owned writes
  with the remedy, never writes unstamped.
- A seat with no live binding receives mail: it waits in `mailbox/<id>` for
  the next binding.
- The laptop sleeps: every seat lapses and revives on wake, with no change of
  generation, no refusal and no seizure.

## Implementation Plan

### Prerequisites

- [x] Sam's design decisions (2026-11-16; T2
  `nexus_rdr/253-decisions-2026-11-16`: decision 3 narrowed, decision 2
  amended, the non-seat `-n` rule).
- [x] Critical Assumptions settled (`nexus_rdr/253-research-5`, `-6`; the
  compaction audit and JDR-002 for assumption 4).

### Minimum Viable Validation

Seat `alpha` is created; a session launched with `NX_SEAT=alpha` binds
generation 1, and a second launch naming `alpha` runs ephemeral and is told
the holder. A launch with `-n scratch42` gets an ephemeral identity named
`scratch42`. Mail to `alpha` arrives. The session runs `/clear`: the binding
log gains a `clear` row under generation 1, its first write after the
`/clear` succeeds, and a shell `nx` started before the `/clear` still writes
as `alpha`. A `claude -p` child it dispatches writes its own records and is
refused on `alpha`'s scratch. The restart script restarts it by `/restart` in place: the
new start finds the old holder gone and binds a `resume` row with generation
2. A test process holding generation
1's token (kept alive for the purpose) is refused on `alpha`'s mailbox claim
with `generation_superseded`. The laptop sleeps for ten minutes and wakes:
the seat lapses and revives under generation 2, and a launch naming `alpha`
during the lapse runs ephemeral. Renaming the alias to `beta` retitles the
board entry and still delivers mail sent to `alpha`, with a notice. After a
reboot, `--revive` finds no registry files, so the old holder is gone, and
binds generation 3 to the same identity at once. A `/resume` inside `alpha`'s
session of a conversation that belongs to an unheld seat `gamma` switches the
session to `gamma`. A `claude -p` child of `alpha` that runs `nx identity
claim alpha --take-over` is refused.

### Phase 1: Identity and the state it owns

All engine work (Steps 1 and 2) ships in one engine release, which Sam cuts.
The client work ships after it: Steps 3 and 4 together in one conexus
release, since binding needs seats; Steps 5 and 6 each
in a later release.

#### Step 1: Engine records and routes

The `identity`, `alias`, `binding`, `session`, `member` and `work` templates;
the `mailbox` template's `identity` address kind; the per-tenant id counter
with `i<digits>` reserved; the compare-and-set `bind` (start, resume, clear,
fork, takeover, release, refresh, end) under the policy table, with the
`holder_gone` verdict recorded from the client's evidence (the check itself is
client work, Step 3); the routes `nx
identity` needs (create, promote, alive, delegated create, session lookup);
renewal, lazy `lapse` with its 10-minute reservation, and `revive`; the
`unleased` lease; the rename call that retitles the threads entry; retention
and tombstones, including threads entries of tombstoned identities; the
operator override.

#### Step 2: Identity tokens and the four fences

Tokens carrying identity and generation, one-hour lifetime, refreshed through
`bind`; the ownership and generation checks on every listed operation in
`TupleHandler`, `MemoryHandler` (project `threads`) and `ScratchHandler` (the
new identity scope); refusal of tokenless writes except sends and writes to
`unleased` seats; `expire`, `reap`, `sweep` and `session/close` kept off
fenced state; `generation_superseded` as its own 409 code; T2 identity
stamping; the JDR-002 and JDR-001 revision entries (Sam's edits).

#### Step 3: The resolver, hooks and the MCP server

`resolve_identity()`; SessionStart binding per the policy table, with
`NX_SEAT` and exact seat `-n` matching; the client-side holder check (the
registry file with a matching `startedAt` and a pid probe, sent as
`holder_gone` evidence; the wait for the hook's own registry file); the per-process
token file; the handoff-marker extension, including the identity switch on a
cross-seat `/resume`; the new-process token read; renewal, refresh and the
`/branch` fork row on the heartbeat; `end` on clean shutdown; the
display-name versus alias notice.

#### Step 4: Seats and delegation

`nx identity` (create, show, alive, claim, release, rename, promote, purge),
with take-over and release refused for non-interactive callers (registry
`entrypoint` other than `cli` or `claude-desktop`), descendants of the holder
and callers with no Claude ancestor;
delegated identities, their renewal and their token file for `claude -p`
dispatch.

#### Step 5: Threads entry, mailbox and names

The `identity:` line beside `session:`, and the checkpoint, `nx ask`, console,
due-date hook and aura readers moved to it; `mailbox/<id>` with alias
resolution, the 7-day notice, dual subscription and sender fallback; the
identity-shaped `from` guard.

#### Step 6: Identity scratch, per-identity T2, memberships and background work

The identity T1 scope in the client; project `identity/<id>`; memberships on
`member/<id>`; `work/<id>` registration; the three liveness consumers (worktree
removal, sandbox and tmux cleanup, the restart readiness check) moved onto
`nx identity alive`.

### Phase 2: Retirement

Remove the four-tier resolver, the `current_session` file, `directory/<name>`
and `mailbox/<session id>` as addresses, the dual subscription, and the
`session:` line, once Phase 1 has run for two weeks and no live session is a
pre-Phase-1 client: every live Claude process on the laptop (registry files)
has a binding whose `client_version` is the Step 5 release or later (the MCP
server reports its own version on every renewal, so a stale server under a
newer plugin is seen), checked by `nx
identity show --all`, which lists live processes without a binding as
well.

### Day 2 Operations

- `nx identity show [seat]`: the identity, its aliases, current binding and
  generation, liveness (live, lapsed, unleased), and recent binding rows.
- The board shows each seat's binding state.
- The binding log answers "which process held this seat at time T" for the
  last 180 days.
- After each Claude Code upgrade, check the MCP log's `_meta` keys for
  `claudecode/agentId` (subagent delegation becomes possible).

### New Dependencies

None.

## Test Plan

- Every policy-table row, including startup with no seat, a non-seat `-n`
  (ephemeral), a live seat (ephemeral with the holder), resume of an unindexed
  id, `compact`, a lapsed seat on `clear`, and `/branch`.
- First write after `/clear` succeeds; a Bash child from before the `/clear`
  still writes; a freshly started MCP server reads its token file.
- Each restart mode (exit and relaunch, in-place `/restart`, `--revive` after a
  reboot) finds the old holder gone and binds the next generation, with no
  release step.
- A cross-seat `/resume` in a running session switches its identity, and the
  MCP server follows through the marker.
- Two processes resuming one session id use separate token files.
- `claim --take-over` from a `claude -p` child, or from a descendant of the
  holder, is refused; the hook prints the remedy only interactively.
- A send without a token (as `nx ask` answers) reaches a seat's mailbox.
- The operator override deletes an ended identity's board entry.
- Two simultaneous binds: one wins.
- Each fence, for every listed operation: a peer's send to a mailbox passes,
  a peer's claim on it is refused; a non-owner's membership, threads-entry
  and scratch writes are refused; a write that drops the `identity:` line is
  refused; a tokenless write is refused except for an `unleased` seat; a
  delegated child reads and does not write its parent's state.
- Token: expired (401, re-read) versus `generation_superseded` (409); refresh
  rewrites the process's one token file.
- Sleep: lapse and revive without a generation change; a launch naming a
  seat inside the 10-minute reservation runs ephemeral.
- Mixed fleet: an old-client receiver gets mail from a new-client sender
  through the session-mailbox fallback, and the reverse.
- Aliases: rename retitles the board entry; mail to a retired alias within 7
  days with a notice, refused after; a retired name is never reissued;
  `i<digits>` refused as an alias.
- Retention: an ephemeral identity tombstoned 30 days after it ends or
  lapses, its rows and board entry gone, its number never reused; token files
  of dead processes removed by the hook; a seat's last binding row never
  pruned.
- Reboot: identity, aliases and bindings survive; `--revive` rebinds.

## Validation

### Testing Strategy

Engine tests for the templates, the bind policy and the four fences; client
tests for the resolver, hooks, handover and delegation against a real engine;
the MVV as one live run with a test seat.

### Performance Expectations

One bind per session start, resume, release or take-over, one log row per
`/clear`, `fork` or `/branch`, one renewal a minute per session on a
heartbeat that already runs, and one refresh every half hour; the resolver
is one indexed read.

## Finalization Gate

### Contradiction Check

A generation counts processes in Approach 2, the Technical Design, the
policy table, the MVV and the Test Plan. The fences are the ownership and
generation checks on the four families of the matrix; the bind route's
compare-and-set and the refusal of a superseded renewal are the generation
rule applied to the binding itself.

### Assumption Verification

All four Critical Assumptions are settled: two verified (Bash children carry
the session id; the clear rebind, by spike), one refuted with its fallback
adopted and accepted by Sam (subagent attribution), and one verified in part
from records (Desktop binds through its hook; its MCP writes stay
unattested).

### Scope Verification

In scope: identity, aliases, binding log, resolver, token file and handover,
the four fences, delegation for `claude -p`, the holder check that covers every
restart mode, and the
Phase 1 state moves Sam chose, plus background-work registration as a label.
Out: cryptographic signatures, per-principal (human) identity, Agent-tool
subagent delegation until Claude Code identifies their calls, and anything
outside one tenant.

### Cross-Cutting Concerns

Trust: the bind and the fences guard against a confused session, not a
hostile one (JDR-002); fencing is a first refusal against JDR-002's rule, on
four families only. Release: one engine release, then client releases in the
order of Phase 1. Migration: a mixed fleet keeps both mailboxes and both board
lines until Phase 2. Data lifetime: seats permanent; ephemeral and delegated
identities tombstoned 30 days after they end or stay lapsed with their holder
gone; a seat's binding rows kept 180
days, the last one always.

### Proportionality

New engine records (identity, alias, binding, session index, membership,
background work), one bind route with several events, four fences in three
handlers, one resolver and a token file, against 46 incidents in five
months, three of them P0. Phase 1 ships as one engine release and three
client releases.

## Open Questions for Sam

Answered 2026-11-16: creation (named seats; a non-seat `-n` is ephemeral),
enforcement (fence identity state, amended to a per-process generation plus
an ownership check), children (delegated, scoped; narrowed to `claude -p`
children until Claude Code identifies subagent calls), Phase 1 scope (all
four state kinds). T2 `nexus_rdr/253-decisions-2026-11-16`.

## References

- T3 `agent-orchestration`: "Our session identity problem: four-lane research
  synthesis (2026-11-16)".
- T2 `nexus_rdr/253-research-1` to `-4` (the four lanes).
- RDR-208, RDR-252, JDR-001, JDR-002.
- apollo `stereotomy`, `fireflies`; Xanadu and nexus catalog tumblers.
- Orleans grain directory; Erlang `global`; SPIFFE concepts; LangGraph
  persistence (URLs in research 4).

## Revision History

- 2026-11-16: created (draft), from the four-lane identity research and Sam's
  four design decisions.
- 2026-11-16: Critical Assumption 1 verified by a `/clear` and `/resume` spike in a throwaway session (`nexus_rdr/253-research-5`).
- 2026-11-16: Critical Assumption 3 refuted (no `claudecode/agentId` on MCP calls in 2.1.325, `nexus_rdr/253-research-6`); the stated fallback adopted.
- 2026-11-16: Critical Assumption 4 settled in part from records: Desktop runs the hook but its MCP server never sees the session id.
- 2026-11-16: Gate round 1 — BLOCKED (2 Critical, 9 Significant, 2 ship-blocker(s)); blob `b7e086a55e87`; critique `nexus_rdr/253-gate-critique-2026-11-16-r1`.
- 2026-11-16: gate round 1 findings addressed (`nexus_rdr/253-research-7`): a generation counts processes, so `/clear`, `fork` and `/branch` keep it and only a new process or a take-over advances it; the hook-to-MCP handover through the existing handoff marker; a binding policy table with take-over; lapse and revive across sleep; Desktop bindings `unleased`; fencing as an ownership check and a generation check over four named families and their handlers, with the board kept on `memory_put`; the bind caller and credential; token refresh and 401 versus 409; a mixed-fleet mail fallback; alias reuse rules; retention for ephemeral and delegated identities; `claude -p` children not binding in their own hook. Sam accepted the narrowing of decision 3 to `claude -p` children.
- 2026-11-16: fix check 1 findings addressed (`nexus_rdr/253-fix-check-fca28b6443a1`, `253-research-8`): the restart script releases the old binding, so restarts bind by the `resume` row and no inherited variable carries take-over authority; take-over only by the one-shot `nx identity claim --take-over`; `end` on clean shutdown; a fresh MCP server reads its token file; tokenless writes to the fences refused except `unleased` Desktop seats; every mutating operation listed per fence, read against the stored entry; all engine changes in Steps 1 and 2, Steps 3 and 4 shipped together; the current generation held on the identity record; one-hour tokens refreshed through `bind`; lazy lapse with a 10-minute reservation; `session:` kept beside `identity:` through Phase 1 with its readers moved in Step 5; rename retitles the board entry; names never reissued; a non-seat `-n` ephemeral and decision 2's amendment, both Sam's answers.
- 2026-11-16: Gate round 2 — PASSED (0 Critical, 8 Significant, 0 ship-blocker(s)); blob `04b6e5eb5d11`; critique `nexus_rdr/253-gate-critique-2026-11-16-r2`.
- 2026-11-16: after the round-2 pass, Sam chose to fix all ten (the two residuals and the eight Significants; `nexus_rdr/253-research-9`): a holder whose host, pid and `startedAt` match no registry file no longer exists, which covers exit-and-relaunch, in-place `/restart` and `--revive` alike, so the restart script needs no release; the Desktop exception keyed on the target seat being `unleased`; a cross-seat `/resume` switches the process's identity through the marker; token files per process start, not per session id; take-over refused for non-interactive callers and descendants of the holder, its remedy printed only interactively, and the caller's ephemeral identity ended; Gap 4's consumers (worktree removal, sandbox and tmux cleanup, the restart readiness check) on `nx identity alive`, beads out by Sam's earlier ruling; sends exempt from the tokenless refusal; one token file per process refreshed in place; an operator override and retention for ended identities' board entries; JDR-001 revision entry.
- 2026-11-16: post-pass fix check 1 findings addressed (`nexus_rdr/253-fix-check-9299310ff81f`; spikes `nexus_rdr/253-research-10`): the holder check is client work, sent to `bind` as `holder_gone` evidence, with a pid probe so a crash is caught; take-over and release keyed on registry `entrypoint` (`sdk-cli` for a `claude -p` child), refused with no Claude ancestor; in-place `/restart` measured to fire `source: resume` with a new `startedAt`; the MCP heartbeat re-reads its token file so an identity switch never depends on the marker; one marker content (identity, generation, session id, token-file path); a seat closes with `release`, an ephemeral with `end`; same-host liveness by the probe however long the lapse; Desktop holders freed by the holder check; the session-scope check accepts the identity's session ids; `display_name` for ephemerals; retention by end or lapse with the holder gone; the delegated token file's path and cleanup; `nx identity purge` for the operator override; `work/<id>` kinds for worktrees and tmux servers; the Phase 2 gate on the Step 5 release; the bead ruling cited; stale sites swept.
