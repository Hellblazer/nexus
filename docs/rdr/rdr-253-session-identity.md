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
parent's token (nexus-5daww, P0).

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
  identity-scoped durable scratch beside it.
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
3. A rebind must advance an epoch. Fireflies filters a note whose epoch is
   not newer than the current one (`apollo/fireflies/.../View.java:297-301`);
   a generation number on each binding lets the engine refuse a superseded
   holder's writes, which closes the two-holders window (research 3, 4).
4. Ids come from a monotone, never-reused allocator with tombstones, as
   nexus's catalog tumblers already do (`src/nexus/catalog/tumbler.py:142`)
   (research 3).
5. Only the MCP server can hold a credential today, and every Bash child
   already carries the current session id in `CLAUDE_CODE_SESSION_ID`
   (research 2). So a resolver can map any process to its identity through
   the binding log without new environment plumbing.
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
  a shell wrapper passing a fresh `-n`, not from Claude Code.
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
   script creates once by name; a launch that names no seat gets an
   ephemeral identity that can be promoted to a seat (Sam, 2026-11-16).
2. **A binding log.** Each time a session takes, keeps or loses a seat
   (start, clear, resume, restart, rename, end), the engine appends a binding
   row. A generation counts the processes that have held the seat: it rises
   only when a different process takes it.
3. **One resolver.** Every surface (MCP server, hooks, CLI, scripts) asks one
   function "which identity is this process", passing the session id it can
   see; the function answers from the binding log and refuses when two
   sources disagree.
4. **Fenced identity state.** On four families of identity-owned state the
   engine checks that a write comes from the owner and from its current
   generation (Sam, 2026-11-16). Elsewhere the stamp stays a label.
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
identity/<id>    dims: kind=seat|ephemeral|delegated  parent=<id or none>
                 body: {"created_by": "<sam|restart|session|dispatch>", "created_at": "<UTC>", "ended_at": null}
alias/<name>     dims: identity=<id>  state=live|retired
                 body: {"since": "<UTC>", "retired_at": null, "renamed_to": null}
binding/<id>     dims: event=start|clear|resume|fork|restart|takeover|rename|end|lapse|revive  generation=<n>
                 body: {"session_id": "<uuid>", "pid": <n>, "proc_start": "<t>", "pane": "<tmux pane or null>",
                        "prior_session_id": "<uuid or null>", "at": "<UTC>"}
session/<session id>   dims: identity=<id>  generation=<n>        (the session index)
```

The id is `i<n>`, allocated by the engine from a per-tenant counter that only
grows, so an id never names a second identity; names matching `i<digits>` are
reserved and cannot be aliases. An alias names one live identity at a time.

**A generation is one process holding a seat.** A process is one Claude Code
process, known by its pid and start time. The generation goes up only when a
different process takes the seat: a launch, a resume in a new process, a
restart, or an explicit take-over. Inside one process the session id can
change (`/clear`, `fork`, `/branch`) without changing the generation: the
binding log appends a row with the new session id under the same generation,
and the session index maps both the old and the new id to that generation.
So the MCP server's token, and every Bash child started before a `/clear`,
stay valid for the life of their process, which the `/clear` spike shows is
the same process (`nexus_rdr/253-research-5`). What the generation fences is
a superseded process: the old process after a restart, a resume elsewhere or
a take-over.

**Binding.** One engine route, `bind`, takes the seat (or identity), the
caller's evidence (session id, pid, start time, pane) and the generation it
expects; it appends a binding row under a compare-and-set (the write happens
only if the expected generation is still current, so two simultaneous binds
cannot both win) and returns an identity token carrying identity and
generation. The caller is the SessionStart hook, authenticated with the same
tenant credential the `nx` CLI uses. Any holder of that credential can bind
any seat, as any can mint a session token today (`SessionTokenHandler.java:97-118`):
the bind guards against a confused session, not a hostile one, as JDR-002
says. The binding policy:

| Hook `source` | Seat named | Seat state | Result |
|---|---|---|---|
| `startup` | `-n <seat>` or `NX_SEAT` | unheld, ended or lapsed | bind, new generation, event `start` |
| `startup` | same | live in another process | refused, naming the holder; `NX_SEAT_TAKEOVER=1` (which the restart script sets) supersedes it, event `takeover` |
| `startup` | a name with no seat | n/a | refused, listing the seats; never creates one by typo |
| `startup` | none | n/a | a new ephemeral identity, event `start` |
| `clear` | n/a | live in this process | same generation, new session id, event `clear` |
| `fork` | n/a | live in this process | same generation, new session id, event `fork` (the seat follows the process; RDR-208's rule that the parent's old mail stays with the old id still holds, since it is read through the alias) |
| `resume` | n/a | the resumed id's seat is unheld, or held by this id's own process | new generation if the process is new, event `resume` |
| `resume` | n/a | the seat is live in another process | refused, naming the holder; the session runs ephemeral until `nx identity claim --take-over` |
| no hook (`/branch`) | n/a | live in this process | the resolver maps the unknown session id through the registry's pid and start time to this process's binding and appends a `fork` row on first use |

A rename is `nx identity rename <old> <new>` (Sam or the seat); Claude Code's
`/rename` changes only the display name, and the hook prints a notice when the
display name and the seat's alias differ.

**Handover to the MCP server.** The hook binds, receives the token, writes it
to `~/.config/nexus/identity/<session id>.token` (mode 0600), and adds the
identity, generation and token to the T1 handoff marker it already writes for
the MCP server (`t1_handoff.<mcp pid>`, polled every 5 s;
`nexus_rdr/253-research-7` (a)). On `/clear` the generation is unchanged, so
the MCP server's existing token stays valid and the marker only tells it the
new session id: there is no window in which its writes are refused. After a
new-process event the MCP server is itself new and binds through its own
start-up hook.

**Renewal, lapse and sleep.** The MCP server renews the binding every 60
seconds on the thread that already renews `directory/<name>`, and refreshes
the identity token at half its 24-hour lifetime, rewriting the token file. A
renewal carrying a superseded generation is refused, and the server re-reads
its binding (it has been taken over and stops writing). After 300 seconds
without renewal the engine appends `lapse` without changing the generation;
the next renewal from the same process appends `revive` and continues. So a
laptop that sleeps lapses every seat and revives every seat on wake, with no
fencing. A lapsed seat may be taken by a new launch without
`NX_SEAT_TAKEOVER`. A Desktop session has no MCP renewal path (Critical
Assumption 4): its binding is of kind `unleased`, never lapses, and shows its
liveness as unknown; taking it over needs the explicit flag.

**The resolver.** One function, `resolve_identity()`, replaces the four-tier
session resolver and the `current_session` file. Its inputs, in order: an
explicit identity token (a delegated child), the hook payload's session id,
`CLAUDE_CODE_SESSION_ID`. It maps a session id through the session index to
`(identity, generation)`; an unknown id from a live registry entry maps
through pid and start time (`/branch`). If two inputs map to different
identities it refuses with both, never picks. A Bash child's `nx` reads the
token file named by its `CLAUDE_CODE_SESSION_ID`; if the token has expired
(HTTP 401) the CLI re-reads the file, which the MCP server refreshes; if it is
superseded (HTTP 409, generation) the CLI stops and names the holder.

**Fencing.** Two checks, applied per family by the engine handler that owns
it. The ownership check: the token's identity is the family's owner (or, for
reads only, a delegated child of the owner). The generation check: the
token's generation is the owner's current generation.

| Family | Store and handler | Who may write | Checked operations | Checks |
|---|---|---|---|---|
| Mailbox `mailbox/<id>` | tuple space, `TupleHandler` | any identity may send (`out`) | the owner's `in`, `ack`, `nack`, `renew`, `release` | ownership and generation on the owner's operations; a send is a label, as today |
| Membership `member/<id>` | tuple space, `TupleHandler` | the owner only | `out`, removal | ownership and generation |
| Threads entry | T2 memory, project `threads`, the engine's memory handler | the owner only | `memory_put`, delete of an entry whose first line is `identity: <id>` | ownership and generation; an entry without the line is unchecked until Phase 2 |
| Identity scratch | T1, new identity scope, the scratch handler | the owner only; a delegated child reads | put, delete, flag | ownership and generation |

Every other subspace, project and scope keeps JDR-002's rule: the stamp is a
label, never a refusal. JDR-002 gains a revision entry naming these four
families (Sam's edit). The board keeps `memory_put` as its write path: the
existing tool, with the engine reading the identity from the token. No new
board verb.

**Delegation.** `nx` operator dispatch (`claude -p`) creates a delegated
identity, `kind=delegated parent=<id>`, and passes its token to the child as
`NX_IDENTITY_TOKEN` in place of the parent's; the child's own SessionStart
hook sees the variable and does not bind, and the resolver takes the explicit
token first. A delegated identity reads its parent's identity-owned state and
writes only its own; it ends when the child exits. Agent-tool subagents keep
the parent's seat with an `agent` label: their MCP calls carry nothing that
tells them from the parent's (Critical Assumption 3; Sam accepted this
narrowing on 2026-11-16). They get delegation once Claude Code sends
`claudecode/agentId` on tool calls, re-checked on each upgrade.

**Retention.** Seats and their aliases are permanent. Ephemeral and delegated
identities are tombstoned 30 days after their last binding ends: their rows
are pruned and the counter keeps their numbers. Binding rows of a seat are
kept for 180 days for incident review.

**Aliases and renames.** A rename retires the old alias, sets `renamed_to`,
and keeps delivering mail sent to it for 7 days with a notice to the sender
naming the new name. After 7 days a send to it is refused with "renamed to
<new> on <date>"; the tombstone is permanent, and a retired name cannot be
reissued to another identity within the 7 days.

**State owned by the identity (Phase 1).**

- **Threads entry.** The entry's first line is `identity: <id>` (replacing
  the hand-kept `session:` line); it is titled by the seat's live alias and
  written with `memory_put` under the fencing rule above. The compaction
  checkpoint and Desktop restore find it by identity.
- **Mailbox and names.** Mail goes to `mailbox/<id>`; a name resolves through
  `alias/<name>`. Phase 1 runs a mixed fleet, since old clients keep running
  for days: a sender resolves `alias/<name>` first and sends to
  `mailbox/<id>`, and falls back to `directory/<name>` and
  `mailbox/<session id>` when the seat has no binding by a new client; a new
  client subscribes to both `mailbox/<id>` and its `mailbox/<session id>`
  until Phase 2. The `mailbox` template's address kind gains `identity`.
  `mailbox_send`'s guard against a session-shaped `from` gets an identity
  counterpart.
- **Identity scratch and per-identity T2.** The identity scope holds working
  state that must cross `/clear` and restart but is not worth a permanent
  record: background-work entries, in-flight notes a session would otherwise
  flag. T2 holds what is permanent: records a seat writes carry its identity,
  and a seat's own notes live in project `identity/<seat>`.
- **Memberships and push records.** RDR-252's memberships are `member/<id>`,
  and its push records carry the identity token's stamp, so the owner of a red
  is a seat.
- **Background work.** Watchers, waits and lab runs register under the
  identity (`work/<id>`), and since they run in the seat's own process their
  writes stay valid across `/clear`; the checkpoint lists them by identity.

### Existing Infrastructure Audit

| Piece | Exists? | Use |
|---|---|---|
| Session token, `sender_session` stamp | Yes (JDR-002) | Becomes an identity token carrying identity and generation |
| MCP 60-second heartbeat thread | Yes (`subscriptions.py`) | Renews the binding |
| SessionStart hook with `source` | Yes | Binds on start, clear and resume |
| Restart script with `-n` | Yes | Sets `NX_SEAT` |
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
- **Rejected.** The chosen design keeps one handoff, the T1 marker, to tell
  the MCP server its new session id; state itself is keyed to the identity
  and moves with nothing.

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
- A superseded session can no longer write a seat's identity-owned state.
- New engine routes and templates mean an engine release before the client
  work can ship.

### Risks and Mitigations

- **A launch names a live seat by mistake** (a mistyped `-n`): it is refused,
  naming the holder; only `NX_SEAT_TAKEOVER=1` or `nx identity claim
  --take-over` supersedes a live seat.
- **A crash leaves a seat held** (the process died, the lease has not yet
  lapsed): the relaunch is refused for up to 300 seconds with the remedy
  (`--take-over`); the restart script always takes over.
- **A resume of an old session id would steal a seat**: it is refused when
  another process holds the seat, and the session runs ephemeral until a
  take-over.
- **A token outlives its lifetime**: the MCP server refreshes it at half life
  and rewrites the file; the CLI tells an expired token (401, re-read) from a
  superseded one (409, stop).
- **Desktop MCP writes are unattested** (Critical Assumption 4): a Desktop
  seat binds through its hook as `unleased`; its MCP server cannot hold the
  token, so its writes to identity-owned state are labels the engine cannot
  fence. Desktop seats are named as such in `nx identity show`.
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
  generation and no refusal.

## Implementation Plan

### Prerequisites

- [x] Sam's design decisions (2026-11-16; T2
  `nexus_rdr/253-decisions-2026-11-16`, decision 3 narrowed the same day).
- [x] Critical Assumptions settled (`nexus_rdr/253-research-5`, `-6`; the
  compaction audit and JDR-002 for assumption 4).

### Minimum Viable Validation

Seat `alpha` is created; a session launched with `-n alpha` binds generation
1, and a second launch with `-n alpha` is refused, naming the first. Mail to
`alpha` arrives. The session runs `/clear`: the binding log gains a `clear`
row under generation 1, its first write after the `/clear` succeeds, and a
shell `nx` started before the `/clear` still writes as `alpha`. A shell `nx`
started after it writes stamped as `alpha`. A `claude -p` child it dispatches
writes its own records and is refused on `alpha`'s scratch. The restart
script restarts it with a take-over: generation 2, and a write from the old
process is refused, naming the new holder. The laptop sleeps for ten minutes
and wakes: the seat lapses and revives under generation 2. Renaming the alias
to `beta` still delivers mail sent to `alpha`, with a notice. After a reboot,
relaunching with `-n beta` binds generation 3 to the same identity.

### Phase 1: Identity and the state it owns

Engine work (Steps 1 and 2) ships in one engine release, which Sam cuts;
client work (Steps 3 to 6) ships in conexus releases that pin it, in step
order, each step usable on its own.

#### Step 1: Identity, alias, binding and session-index templates; the bind route

The per-tenant id counter with `i<digits>` reserved; the compare-and-set bind
under the policy table; renewal, `lapse` and `revive`; the `unleased` kind;
retention and tombstones.

#### Step 2: Identity tokens and the four fences

Tokens carrying identity and generation, refreshed at half life; the
ownership and generation checks in `TupleHandler` (mailbox owner operations,
membership), the memory handler (project `threads`) and the scratch handler
(identity scope); 401 versus 409; the JDR-002 revision entry (Sam's edit).

#### Step 3: The resolver, hooks and the MCP server

`resolve_identity()`; SessionStart binding per the policy table; the token
file; the handoff-marker extension; renewal and refresh on the heartbeat
thread; the display-name versus alias notice.

#### Step 4: Seats, restart and delegation

`nx identity` (create, show, claim, rename, promote); `NX_SEAT` and
`NX_SEAT_TAKEOVER` in the restart script; delegated identities and
`NX_IDENTITY_TOKEN` for `claude -p` dispatch.

#### Step 5: Threads entry, mailbox and names

The `identity:` line and the checkpoint lookup by identity; `mailbox/<id>`,
the template's `identity` address kind, alias resolution with the 7-day
notice, dual subscription and sender fallback for the mixed fleet; the
identity-shaped `from` guard.

#### Step 6: Identity scratch, per-identity T2, memberships, push records, background work

The identity T1 scope; T2 identity stamping and project `identity/<seat>`;
RDR-252's memberships and push records on identities; `work/<id>`.

### Phase 2: Retirement

Remove the four-tier resolver, the `current_session` file, `directory/<name>`
and `mailbox/<session id>` as addresses, the dual subscription, and unchecked
threads entries without an `identity:` line, once every live session runs a
Phase 1 client and Phase 1 has run for two weeks.

### Day 2 Operations

- `nx identity show [seat]`: the identity, its aliases, current binding and
  generation, liveness (live, lapsed, unleased), and recent binding rows.
- The board shows each seat's binding state.
- The binding log answers "which process held this seat at time T" for any
  incident review.
- After each Claude Code upgrade, check the MCP log's `_meta` keys for
  `claudecode/agentId` (subagent delegation becomes possible).

### New Dependencies

None.

## Test Plan

- Each policy-table row: startup on an unheld, live (refused, then take-over),
  lapsed and unknown seat; clear; fork; resume unheld and resume against a
  live holder; `/branch` with no hook.
- First write after `/clear` succeeds; a Bash child from before the `/clear`
  still writes; a write from a superseded process is refused (409) naming the
  holder.
- Two simultaneous binds: one wins.
- Each fence: a peer's send to a mailbox passes and a peer's claim on it is
  refused; a non-owner's membership, threads-entry and scratch writes are
  refused; a delegated child reads and does not write its parent's state.
- Token: expired (401, re-read) versus superseded (409); refresh at half life
  rewrites the file.
- Sleep: lapse and revive without a generation change; a lapsed seat taken
  without the flag.
- Desktop: an `unleased` binding never lapses; its MCP writes are labels.
- Mixed fleet: an old-client receiver gets mail from a new-client sender
  through the session-mailbox fallback, and the reverse.
- Aliases: mail to a retired alias within 7 days with a notice; refused after;
  a retired name cannot be reissued within the window; `i<digits>` refused as
  an alias.
- A `claude -p` child with `NX_IDENTITY_TOKEN` does not bind in its own hook.
- Retention: an ephemeral identity tombstoned after 30 days; its number never
  reused.
- Reboot: identity, aliases and bindings survive; the next launch rebinds.

## Validation

### Testing Strategy

Engine tests for the templates, the bind policy and the four fences; client
tests for the resolver, hooks, handover and delegation against a real engine;
the MVV as one live run with a test seat.

### Performance Expectations

One bind per session start, resume or new process, one log row per `/clear`,
and one renewal a minute per session on a heartbeat that already runs; the
resolver is one indexed read.

## Finalization Gate

### Contradiction Check

Generations count processes everywhere: Approach 2, Technical Design, the
policy table, the MVV and the Test Plan. Fencing is the ownership and
generation checks on the four families in the matrix, and nothing else.

### Assumption Verification

All four Critical Assumptions are settled: two verified (Bash children carry
the session id; the clear rebind, by spike), one refuted with its fallback
adopted and accepted by Sam (subagent attribution), and one verified in part
from records (Desktop binds through its hook; its MCP writes stay
unattested).

### Scope Verification

In scope: identity, aliases, binding log, resolver, token file and handover,
the four fences, delegation for `claude -p`, and the Phase 1 state moves Sam
chose. Out: cryptographic signatures, per-principal (human) identity,
Agent-tool subagent delegation until Claude Code identifies their calls, and
anything outside one tenant.

### Cross-Cutting Concerns

Trust: the bind and the fences guard against a confused session, not a
hostile one (JDR-002); fencing is a first refusal against JDR-002's rule, on
four families only. Release: engine first, then client steps in order.
Migration: a mixed fleet keeps both mailboxes until Phase 2. Data lifetime:
seats permanent; ephemeral and delegated identities tombstoned after 30
days; binding rows kept 180 days.

### Proportionality

New engine records (identity, alias, binding, session index, membership,
background work), one bind route, four fence checks in three handlers, one
resolver and a token file, against 46 incidents in five months, three of them
P0. Phase 1 ships in six steps, each usable on its own.

## Open Questions for Sam

Answered 2026-11-16: creation (named seats), enforcement (fence identity
state), children (delegated, scoped; narrowed the same day to `claude -p`
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
