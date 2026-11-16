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
`halcyon`, which many sessions occupy one after another.

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
- [x] The MCP server can tell a call made by an Agent-tool subagent from one
  made by its parent. **Status**: Refuted for Claude Code 2.1.325
  (`nexus_rdr/253-research-6`): of every tool call nexus logged, none carried
  `claudecode/agentId` in `_meta`, including the research subagents' calls,
  though the binary defines that key. The fallback applies: delegation covers
  `claude -p` children; subagent writes stay attributed to the parent seat
  with an `agent` label, as today. Re-checked on each Claude Code upgrade.
- [x] A Claude Desktop session can run the SessionStart hook and bind.
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
   row with a rising generation. The current binding is the last row.
3. **One resolver.** Every surface (MCP server, hooks, CLI, scripts) asks one
   function "which identity is this process", passing the session id it can
   see; the function answers from the binding log and refuses when two
   sources disagree.
4. **Fenced identity state.** The engine refuses a write to identity-owned
   state from a superseded generation (Sam, 2026-11-16). Elsewhere the stamp
   stays a label.
5. **Delegated children.** A Bash child acts as its session's identity; a
   `claude -p` child or subagent gets a delegated sub-identity that may read
   the parent's state and write only its own (Sam, 2026-11-16).
6. **State moves onto the identity.** Phase 1 moves the threads entry, the
   mailbox and names, durable scratch and per-identity T2, and group
   memberships and push records (Sam, 2026-11-16).

### Technical Design

**Identity records.** Three new engine templates, all tenant-scoped:

```
identity/<id>    dims: kind=seat|ephemeral|delegated  parent=<id or none>
                 body: {"created_by": "<sam|restart|session>", "created_at": "<UTC>", "ended_at": null}
alias/<name>     dims: identity=<id>  state=live|retired
                 body: {"since": "<UTC>", "retired_at": null}
binding/<id>     dims: event=start|clear|resume|restart|rename|end|lapse  generation=<n>
                 body: {"session_id": "<uuid>", "pid": <n>, "proc_start": "<t>", "pane": "<tmux pane or null>",
                        "prior_session_id": "<uuid or null>", "at": "<UTC>"}
```

The id is `i<n>`, allocated by the engine from a per-tenant counter that only
grows, so an id never names a second identity. An alias names one live
identity at a time; a rename retires the old alias and points it at the same
identity for 7 days, so mail to an old name still arrives and the sender is
told the new name. The engine keeps a session index, `session/<session id>`
to `(identity, generation)`, written with each binding row.

**Binding.** One engine route, `bind`, takes the identity (or a seat name),
the expected current generation, and the binding fields; it appends a row
with generation + 1 only if the expected generation still holds (a
compare-and-set), and returns an identity token for that generation. Two
sessions racing for one seat: one wins, the other is told who holds it.

- **Start.** The SessionStart hook (source `startup`) binds the seat the
  launch names: `-n <seat>` or `NX_SEAT`, which the restart script sets. A
  launch with neither gets an ephemeral identity.
- **Clear.** The hook (source `clear`) finds the current binding whose pid
  and process start match its own process (Critical Assumption 1) and binds
  the new session id to that identity, event `clear`.
- **Resume.** The hook (source `resume`) looks up the resumed session id in
  the session index and rebinds that identity, event `resume`.
- **Restart.** The restart script launches each seat with `NX_SEAT=<seat>`;
  the new process binds with event `restart`, superseding the old generation.
- **Rename.** `/rename` changes the session's display name; the seat's alias
  changes only through `nx identity rename`, which Sam or the seat runs.
- **End and lapse.** A clean exit appends `end`. The MCP server renews the
  binding every 60 seconds on the thread that already renews
  `directory/<name>`; after 300 seconds without renewal the engine appends
  `lapse`. Liveness is "the current binding is neither ended nor lapsed".

**The resolver.** One function, `resolve_identity()`, replaces the four-tier
session resolver and the `current_session` file. Its inputs, in order: an
explicit identity token (a delegated child), the hook payload's session id,
`CLAUDE_CODE_SESSION_ID`. It maps the session id through the session index;
if two inputs map to different identities it refuses with both, never picks.
The MCP server writes the identity token for its current generation to
`~/.config/nexus/identity/<session id>.token` (mode 0600) at each bind; a
Bash child's `nx` reads the file named by its `CLAUDE_CODE_SESSION_ID`, so
shell writes are stamped with the identity and generation. A token from a
superseded generation is refused on identity-owned subspaces.

**Fencing.** Identity-owned subspaces are `mailbox/<id>`, `member/<id>`, the
threads entry and identity-scoped scratch. On each, the engine compares the
token's generation with the identity's current generation and refuses an
older one, naming the current holder. Every other subspace keeps JDR-002's
rule: the stamp is a label, never a refusal. JDR-002 gains a revision entry
for the fenced subspaces (Sam's edit).

**Delegation.** `nx` operator dispatch (`claude -p`) creates a delegated
identity, `kind=delegated parent=<id>`, and passes its token to the child in
place of the parent's. A delegated identity reads its parent's identity-owned
state and writes only its own; it ends when the child exits. Agent-tool
subagents keep today's attribution, the parent's seat with an `agent` label:
their MCP calls carry nothing that tells them from the parent's (Critical
Assumption 3, refuted for 2.1.325). They get delegation once Claude Code sends
`claudecode/agentId` on tool calls. Hooks run under the session's identity
through the resolver and never hold a delegated token.

**State owned by the identity (Phase 1).**

- **Threads entry.** The entry is keyed by identity: written by an `nx
  threads put` that stamps the identity, titled by the seat's live alias, with
  the identity id in place of the hand-kept `session:` line. The compaction
  checkpoint and Desktop restore find it by identity.
- **Mailbox and names.** Mail goes to `mailbox/<id>`; a name resolves through
  `alias/<name>` to the identity; a retired alias resolves for 7 days and the
  sender is told the new name. `mailbox/<session id>` and `directory/<name>`
  keep working as aliases until Phase 2 retires them.
- **Durable scratch and per-identity T2.** A new T1 scope, identity scope,
  holds entries that must survive `/clear` and restart (today's flagged
  entries, background-work records); T2 records written by a seat carry its
  identity, and a seat's own records live in project `identity/<seat>`.
- **Memberships and push records.** RDR-252's memberships become
  `member/<id>` and its push records carry the identity token's stamp, so the
  owner of a red is a seat, not a name or a session id.
- **Background work.** Watchers, waits and lab runs register under the
  identity (`work/<id>`), so a `/clear` neither hides them nor makes the
  checkpoint report "none running".

### Existing Infrastructure Audit

| Piece | Exists? | Use |
|---|---|---|
| Session token, `sender_session` stamp | Yes (JDR-002) | Becomes an identity token carrying identity and generation |
| MCP 60-second heartbeat thread | Yes (`subscriptions.py`) | Renews the binding |
| SessionStart hook with `source` | Yes | Binds on start, clear and resume |
| Restart script with `-n` | Yes | Sets `NX_SEAT` |
| Catalog tumbler allocator | Yes (`catalog/tumbler.py`) | Pattern for the never-reused id counter |
| Directory lease, session markers, handoff markers, `current_session` | Yes | Aliases in Phase 1, retired in Phase 2 |
| Identity, alias, binding, session index; bind route; fencing | No | New, in the engine |
| Resolver, token file, delegation | No | New, in the client |

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
- **Rejected.**

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

- **A session binds the wrong seat** (a mistyped `-n`, two launches naming
  one seat): the compare-and-set lets one win and tells the other who holds
  it; `nx identity show` lists each seat's current binding.
- **The clear rebind misses** (Critical Assumption 1 fails): the new session
  binds as ephemeral and the board shows an unbound seat; a one-line `nx
  identity claim <seat>` rebinds it.
- **Fencing refuses a legitimate write** (a long-running background job from
  before a `/clear`): the job re-reads its token through the resolver before
  each write; a refusal names the current holder.
- **Desktop MCP writes are unattested** (Critical Assumption 4): a Desktop
  seat binds through its hook, but its MCP server cannot hold the identity
  token, so its writes to identity-owned state are labels the engine cannot
  fence. Desktop seats are named as such in `nx identity show`.

### Failure Modes

- Engine unreachable at start: the session runs unbound, the resolver says
  so, and identity-owned writes wait or fail loudly; nothing falls back to a
  name or a file.
- Token file missing for a Bash child: the CLI refuses identity-owned writes
  with the remedy, never writes unstamped.
- A seat with no live binding receives mail: it waits in `mailbox/<id>` for
  the next binding.

## Implementation Plan

### Prerequisites

- [x] Sam's design decisions (2026-11-16; T2
  `nexus_rdr/253-decisions-2026-11-16`).
- [x] Critical Assumptions 1, 3 and 4 settled (`nexus_rdr/253-research-5`, `-6`; the compaction audit and JDR-002 for 4).

### Minimum Viable Validation

Seat `alpha` is created; a session launched with `-n alpha` binds generation
1. Mail to `alpha` arrives. The session runs `/clear`: it rebinds to `alpha`
as generation 2, the mail and its board entry are still its own, and a write
carrying the generation-1 token is refused. A shell `nx` in that session
writes stamped as `alpha`. A `claude -p` child it dispatches writes its own
records and is refused on `alpha`'s scratch. The restart script restarts it:
generation 3, same seat. Renaming the alias to `beta` still delivers mail
sent to `alpha`, with a notice. After a reboot, relaunching with `-n beta`
rebinds the same identity.

### Phase 1: Identity and the state it owns

Engine work (Steps 1 and 2) ships in one engine release, which Sam cuts;
client work (Steps 3 to 6) ships in a conexus release that pins it.

#### Step 1: Identity, alias, binding and session-index templates; the bind route

The per-tenant id counter, the compare-and-set bind returning an identity
token, lapse after 300 seconds without renewal.

#### Step 2: Identity tokens and fencing

Tokens carry identity and generation; refusal of superseded generations on
identity-owned subspaces; the JDR-002 revision entry (Sam's edit).

#### Step 3: The resolver, hooks and the MCP server

`resolve_identity()`; SessionStart binding on start, clear and resume; the
heartbeat renewal; the token file for Bash children.

#### Step 4: Seats, restart and delegation

`nx identity` (create, show, claim, rename, promote); `NX_SEAT` in the restart
script; delegated identities for `claude -p` dispatch.

#### Step 5: Threads entry, mailbox and names

The identity-keyed threads entry and checkpoint lookup; `mailbox/<id>` with
alias resolution and the 7-day retired-alias notice.

#### Step 6: Durable scratch, per-identity T2, memberships and push records

The identity T1 scope; T2 identity stamping and project `identity/<seat>`;
RDR-252's memberships and push records on identities; `work/<id>` for
background work.

### Phase 2: Retirement

Remove the four-tier resolver, the `current_session` file, name-as-address
(`directory/<name>` as an address), session-id mailboxes as the primary
address and the hand-kept `session:` line, once Phase 1 has run for two
weeks.

### Day 2 Operations

- `nx identity show [seat]`: the identity, its aliases, current binding and
  generation, liveness, and recent binding rows.
- The board shows each seat's binding state.
- The binding log answers "which session held this seat at time T" for any
  incident review.

### New Dependencies

None.

## Test Plan

- Bind, clear, resume, restart, rename, end and lapse each append the right
  row and generation.
- Two sessions racing for one seat: one wins, the other is told the holder.
- A superseded generation is refused on each identity-owned subspace and
  accepted elsewhere as a label.
- The resolver refuses when the hook payload and `CLAUDE_CODE_SESSION_ID` map
  to different identities.
- A Bash child's `nx` stamps the identity; with no token file it refuses.
- A delegated child writes its own state and is refused on its parent's.
- Mail to a retired alias arrives within 7 days with a notice, and is
  refused after.
- The checkpoint finds the threads entry by identity after `/clear` and on
  Desktop.
- Reboot: identity, aliases and bindings survive; the next launch rebinds.

## Validation

### Testing Strategy

Engine tests for the templates, the bind route and fencing; client tests for
the resolver, hooks and delegation against a real engine; the MVV as one live
run with a test seat.

### Performance Expectations

One bind per session start, clear or resume, and one renewal a minute per
session, on a heartbeat that already runs; the resolver is one indexed read.

## Finalization Gate

### Contradiction Check

Seats, aliases, bindings and the session index read the same in Approach,
Technical Design, the MVV and the Test Plan. Fencing applies to the four
identity-owned subspace families named under Fencing and nowhere else.

### Assumption Verification

All four Critical Assumptions are settled: two verified (Bash children carry
the session id; the clear rebind, by spike), one refuted with its fallback
adopted (subagent attribution), and one verified in part from records
(Desktop binds through its hook, but its MCP writes stay unattested).

### Scope Verification

In scope: identity, aliases, binding log, resolver, token file, fencing,
delegation for `claude -p`, and the Phase 1 state moves Sam chose. Out:
cryptographic signatures, per-principal (human) identity, Agent-tool
subagent delegation (Critical Assumption 3 refuted for 2.1.325), and anything
outside one tenant.

### Cross-Cutting Concerns

Trust: fencing is a first refusal against JDR-002's rule, on four subspace
families only. Release: engine first, then client. Migration: Phase 1 keeps
every old address working as an alias. Data lifetime: identity and alias
records are permanent; binding rows are kept for incident review.

### Proportionality

Three templates, one route, one resolver and a token file replace twenty-five
identifiers' worth of conventions and handoffs, against 46 incidents in five
months, three of them P0.

## Open Questions for Sam

Answered 2026-11-16: creation (named seats), enforcement (fence identity
state), children (delegated, scoped), Phase 1 scope (all four state kinds).
T2 `nexus_rdr/253-decisions-2026-11-16`.

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
