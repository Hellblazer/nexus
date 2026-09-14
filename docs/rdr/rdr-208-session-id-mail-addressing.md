---
title: "Session-Id Mail Addressing: One Mailbox per Session, Names Resolved at Send Time"
id: RDR-208
type: Architecture
status: accepted
priority: medium
author: Sam
reviewed-by: self
created: 2026-09-14
accepted_date: 2026-09-14
related_issues: [nexus-6konb.21, nexus-6konb]
related_rdrs: [RDR-205, RDR-206, RDR-105, RDR-184]
---

# RDR-208: Session-Id Mail Addressing

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.
> Prose: see REGISTER.md beside this template (conexus/resources/rdr/).

This RDR amends RDR-205. RDR-205 is closed, and its own rule is that a consumer
it does not name needs its own RDR; the change below replaces the address model
of the mailbox consumer that RDR-205 §Technical Design and Phase 6 specify.

## Problem Statement

A mailbox address today is either an agent id or an *instance name*, the
session name the harness's `ListAgents` tool shows (for example `nexus-23`).
Mail to a session goes to `mailbox/<name>`. The watcher (`nx tuple watch
--instance NAME`) pings on that mailbox, and the UserPromptSubmit drain hook
delivers from it once a per-session registry file on local disk names it.

The harness gives a session three identities, and each breaks at a different
boundary. Observed on this machine with Claude Code 2.1.270 on 2026-09-14 and
checked against the harness documentation (T2 `nexus_rdr/208-research-3`):

| Identity | `/resume` | `/clear` | `/compact` |
|---|---|---|---|
| `ListAgents` name | changes: a new random suffix at every process start | kept | kept |
| Session id | kept, unless `--fork-session` or `/branch` | changes, by design (JDR-001) | kept |
| Claude process pid | changes | kept | kept |

No identity holds across both `/resume` and `/clear`. The name, the one mail is
addressed to, breaks on `/resume`, the most common boundary.

### Enumerated gaps to close

#### Gap 1: mail to a renamed session's old name is never read

After a resume renames a session, mail a peer sends to the old name lands in a
mailbox no watcher covers and no drain reads. It sits until the 7-day
retention expires. The peer-messaging skill now tells senders to look the name
up just before sending, which narrows the window to mail sent in the minutes
around a rename; it does not close it.

#### Gap 2: a name can pass to another session

The harness builds a default name from the working directory's basename and
one random byte in hex, drawn at every process start, and does not deduplicate
it, even between live sessions. That leaves 256 names per directory. A session
that later draws a name drains whatever earlier mail was addressed to it, and
two live sessions can hold the same name at once.

#### Gap 3: `/clear` strands the previous session's mail

`/clear` mints a new session id. Mail sent to the old session id, before or
during the clear, sits in a mailbox the new session's drain never reads.
Nothing records the previous id: the T1 handoff marker holds only the new
session id, and the tuple-watch marker (`session.<claude pid>`) holds only the
current one.

#### Gap 4: name resolution exists on one machine only

The only record of which session holds which name is a local file
(`<config>/tuple-watch/addresses.d/<session id>`). Sessions that share an
engine but not a file system, for example two machines pointed at one managed
service, cannot resolve a name at all. Two machines in local mode run separate
engines and share no tuple space, so no addressing scheme reaches between them;
this RDR does not change that.

## Relationship to Prior RDRs

| RDR | Relationship | Note |
|---|---|---|
| RDR-205 | Amended | Replaces the mailbox address model (§Technical Design, the `mailbox/<address>` template's `address_kind` in {agent, instance}; Phase 6's instance-name addressing). The tuple space, templates, claim and ack semantics are unchanged. |
| RDR-206 | Adjacent | Reply-in-ack writes the reply to the requester's mailbox. With this RDR the requester's address is its session id. |
| RDR-105 and JDR-001 | Same identity problem, different store | T1 keys on the session id and carries a handoff across `/clear`. This RDR keys mail the same way and adds the one link T1 does not record: the previous session id. |
| RDR-184 | Unchanged | The ledger keys on `ledger/<session_id>` already. |

## Context

### Background

The mailbox shipped under epic nexus-6konb: a Monitor-armed watcher that pings,
and a drain hook that claims, acks and renders at each prompt. Beads
nexus-6konb.19 and .20 (2026-09-14) made the watcher re-arm when its
SessionStart instruction is lost and told the model to take the name fresh
from `ListAgents`, because a re-arm with a stale name watched the wrong
mailbox. Gap 1 remained; a one-time name handoff was built and parked when
review showed it patches the identity this RDR demotes.

### Technical Environment

- Mailbox template: `mailbox/<address>`, keys `to`, dims `from` (required),
  `kind`, `correlation_id`, `address_kind` in {agent, instance}; `id_from:
  keys+nonce`, take enabled, retention 7 days.
- `rd` skips consumed rows and rows past their `expires_at` and returns every
  other row, claimed and dead-lettered ones included, each with its
  `claim_state`, `created_at` and `expires_at`. It orders by `created_at` then
  id, oldest first. A take-disabled template such as the directory never has a
  claimed or dead-lettered row, so for it every returned row is live. It has no descending order and no
  latest-only read.
- An `out` whose id matches an existing row adds no row. It moves the row's
  `expires_at` to the earlier of the new expiry and the row's own `created_at`
  plus the template's retention, leaves the body, dims and claim state alone,
  and wakes waiters either way.
- A per-tuple `ttl_seconds` below the template's retention is accepted.
- Templates load at engine boot from a fixed list, so a new template ships only
  in an engine release.
- The watcher resolves the session id from its environment, and a hook from its
  input payload. No environment variable or hook input carries the
  `ListAgents` name; only the model, reading `ListAgents`, can supply it.
- Local mode runs one engine per machine. Sessions on different machines share
  an engine only when both point at the same managed `service_url`.

## Research Findings

### Investigation

Four read-only passes on 2026-09-14, recorded in T2 as
`nexus_rdr/208-research-1` (engine), `-2` (client, hooks, MCP, deployment),
`-3` (harness identities), and `-4` (prior art: ZooKeeper, etcd, Consul,
Erlang `global`, Akka Receptionist, Orleans, SIP registrars). The prior-art
survey is in T3 as `tuple-space/research-rdr-208-name-directory-prior-art`.

### Key Discoveries

- **✅ Verified** (source search, research-1): `rd` cannot return the newest
  row of a subspace without paging through the older ones. It does skip
  expired rows, so the rows it returns under one name are that name's live
  entries. With entries that lapse minutes after their watcher stops, that is
  about one per live holder, and the newest is the last row of a single read.
- **✅ Verified** (source search, research-1): A4 as first drafted ("leaves the
  first tuple unchanged") is wrong in one detail: a repeated `out` moves
  `expires_at` and wakes waiters. The design needs only the body and dims to
  stay unchanged, and they do.
- **✅ Verified** (source search, research-1): the directory template needs no
  schema change. Take disabled is `take.enabled: false`, which the ledger
  template already ships. A session id and a name both satisfy the subspace
  address grammar.
- **✅ Verified** (source search, research-1): the engine checks `address_kind`
  against the template's value list in application code, with no database
  constraint. Adding `session` is safe for old clients. A new client that sends
  it to an old engine gets `SchemaViolation`, so the engine ships first.
- **✅ Verified** (observed and read from the harness, research-3): names are a
  random byte per process start and are not deduplicated. Four renames on
  resume were observed on this machine, each with the session id unchanged.
  The harness carries an unshipped flag, `tengu_session_stable_address`, that
  derives the name from the session id and adds a `sid:<session id>` address
  to `ListAgents` rows. It is off here.
- **✅ Verified** (observed, research-3): two terminals that resume one session
  share its id, so two live processes can drain one session-id mailbox. Each
  message still reaches exactly one of them, because delivery is a claim.
- **⚠️ Documented** (harness code and hooks documentation, research-3): no hook
  input carries a previous session id. On `/clear` the harness runs SessionEnd
  with reason `clear` before it mints the new id, in the same process, so that
  hook sees the old id. Reading the pid-keyed watch marker at SessionStart gives
  the same link without a second hook.
- **✅ Verified** (source search, research-2): SessionStart overwrites
  `tuple-watch/session.<claude pid>` without reading it first. Recording the
  previous id is new code, not an existing step.
- **✅ Verified** (source search, research-2): the drain's delivery loop stops
  when a claim returns nothing. That, not a short `rd` page, is the signal that
  a mailbox is empty. The cleared-record delete rule in the Technical Design adds two
  more conditions to it.
- **✅ Verified** (source search, research-2): the watcher already covers the
  session-id mailbox by default; `--instance` adds the name's mailbox.
- **⚠️ Documented** (specifications and documentation, research-4): every
  lease-based naming system surveyed (ZooKeeper, etcd, Consul, Jini) ties an
  entry's life to a heartbeat measured in seconds and keeps one current entry
  per name. A fixed 7-day window has the shape of a DNS TTL: an ended session's
  entry would resolve for a week, and reads would grow with every arm. None of
  the surveyed systems lets a conflict pass without a signal.
- **⚠️ Documented** (research-4): no surveyed naming system drains an old
  address once and then forgets it. The closest pattern is one-pass
  dead-letter redelivery, which fits a claim-and-ack mailbox.

### Critical Assumptions

- **A1**: the session id is unchanged across `/resume`. Status: ✅ Verified
  (research-3: four resumed processes, one id per transcript; also
  documented). Exceptions: `--fork-session` and `/branch` mint a new id.
- **A2**: the claude pid is unchanged across `/clear`. Status: ✅ Verified
  (research-3: one process observed running through a `/clear`, its watch
  marker firing as designed).
- **A3**: `ListAgents` names are reused across sessions. Status: ✅ Verified
  by design (research-3): a random byte per process start, never
  deduplicated. Gap 2 is real.
- **A4**: a second `out` with an existing id leaves the body and dims
  unchanged. Status: ✅ Verified (research-1), with the correction that it
  moves `expires_at`.
- **A5**: `rd` skips expired rows, so a lapsed directory entry stops resolving
  without waiting for the purge. Status: ✅ Verified (source search of the
  read query).

## Proposed Solution

### Approach

Every session has one mailbox, keyed by its session id. Names become aliases a
sender resolves at send time through a directory in the tuple space whose
entries live only while their watcher does. `/clear` drains the previous
session's mailbox once.

### Technical Design

**Mailbox.** Mail to a session goes to `mailbox/<session id>`. The watcher and
the drain cover that mailbox and no instance-name mailbox, once the transition
in Phase 3 ends. `address_kind` keeps `agent` and gains `session`; `instance`
is retired.

**Directory.** A new template `directory/<name>`: keys `name`; dims
`session_id` (required); `id_from: keys+nonce` with `id_dims: [session_id]`,
so two sessions arming one name with the same nonce still write two rows; take
disabled; retention 7 days. The watcher armed with `--instance NAME` writes an
entry whose nonce is its arm time, finer than one second and joined with its
pid, with a short `ttl_seconds`, and re-sends the same entry on a heartbeat. A re-send has the same id, so it moves the entry's expiry forward
instead of adding a row. Values, decided 2026-09-14: a 300-second TTL, re-sent
every 60 seconds. An entry stops resolving within one TTL after
its watcher stops. On a `/clear` self-stop the watcher releases its entry by
re-sending it with `ttl_seconds=1`, so it lapses within about a second; on any other exit it leaves the
entry to lapse, so a `/resume` inside the TTL still finds the name resolving. A re-send cannot move expiry past the entry's `created_at`
plus the 7-day retention, so a watcher that runs that long writes a fresh entry
with a new nonce before then.

**Resolution.** A sender reads `directory/<name>` once. `rd` returns only live
entries. When they all name one session, that session is the recipient. When
they name more than one session, `mailbox_send` writes nothing and returns an
error that lists each holder's session id, and the sender resends to the
session id it means (Sam's decision, 2026-09-14, T2
`nexus_rdr/208-decision-gate-2026-09-14`).

**Rename on resume.** The old process ends, its watcher stops re-sending, and
the old name resolves to the same session for at most one TTL, then stops
resolving. Mail to an old name therefore either arrives or is refused with an
error naming the name; it is never written to a mailbox nobody reads.

**Reuse.** A session that arms a name another live session holds adds its own
entry. Until the earlier holder's entry lapses, the name resolves to two
sessions, and `mailbox_send` refuses it and names both.

**Sending.** A new MCP tool
`mailbox_send(to, body, kind, correlation_id, from_address)` resolves `to`: a
session-id shape is used as is; an agent-id shape is used as is, with
`address_kind: agent` and no directory lookup; anything else is looked up in
the directory; an unresolvable name is an error naming the name, and a name
held by more than one live session is an error naming every holder. Neither is
ever a silent write to a mailbox nobody reads. The mailbox and peer-messaging skills
send through it; raw `tuple_out` to `mailbox/` stays possible and documented
as the low-level path. The sender's own address (`from`) is the session id in
the tuple-watch session marker, then `NX_T1_SESSION_ID`; with neither, the call
is refused. `from_address` overrides it with a session-id or agent-id shape,
and a subagent passes its own agent id.

**`/clear`.** On `source=clear`, SessionStart reads the previous session id
from `tuple-watch/session.<claude pid>` before overwriting it, and writes
`<config>/tuple-watch/cleared.<new session id>` naming that id. The drain for
the new session drains the named mailbox inside its usual budget. It deletes the
record only when, for every mailbox the record names, the claim loop ended on an
empty claim, a read returns no row except dead-lettered ones, and that
mailbox's pending file is empty. Any other outcome (the budget running out, a
refused ack, a row still claimed under lease) keeps the record for the next prompt. A
second `/clear` before that carries the ids the record names forward into its
own record. `/resume` and `/compact` need nothing.

**Fork.** `/branch` and `--fork-session` mint a new session id, but the parent
session still exists and can be resumed, so its mailbox stays with it. A forked
session starts with an empty mailbox and is reachable by name once its watcher
arms.

**Two processes on one session id.** Two terminals resuming one session both
drain its mailbox, and each message is claimed by one of them. When one of them
runs `/clear`, only that process's SessionStart runs, so only it writes a
cleared record. The other process still holds the old session id and keeps
draining the old mailbox as its own. Both then claim from that mailbox, and
each message is still delivered exactly once, to one of them.

**Scope.** Resolution reaches every session that shares an engine: all
sessions on one machine in local mode, and every session pointed at one managed
`service_url`. Two machines in local mode share no tuple space, before or after
this RDR.

### Existing Infrastructure Audit

| Piece | Where | Change |
|---|---|---|
| Mailbox template | `service/src/main/resources/tuples/templates/mailbox.yaml` | `address_kind` values gain `session`; `instance` retired after Phase 3 |
| Directory template | new, same directory, added to the engine's boot list | new template; an engine release |
| Watcher | `src/nexus/tuple_watch.py`, `src/nexus/commands/tuple_cmd.py` | writes its directory entry at arm and re-sends it on a heartbeat; stops watching instance mailboxes after Phase 3 |
| Drain hook | `conexus/hooks/scripts/mailbox_drain.py` | drains each mailbox the cleared record names, and deletes the record only under the three conditions in the Technical Design's `/clear` paragraph; drops instance mailboxes after Phase 3 |
| SessionStart | `src/nexus/hooks.py`, `src/nexus/tuple_watch.py` | on `source=clear`, reads the previous id from the pid marker before overwriting it |
| MCP | `src/nexus/mcp/core.py`; name pins in `tests/test_mcp_package.py` and `tests/test_mcp_tuple_tools.py`; `tests/test_mcp_tool_description_lint.py` | `mailbox_send` |

### Decision Rationale

The session id is the identity the harness keeps across the most boundaries,
and it is already a watched mailbox. Moving the address there removes the
rename and reuse cases instead of handing mail between names, and the one
boundary it does not survive, `/clear`, is a boundary the harness reports
explicitly, so it can be handled at a single, known moment. Directory entries
that live only while their watcher re-sends them follow every lease-based
naming system surveyed, and they keep resolution to one `rd`.

## Alternatives Considered

### Alternative 1: one-time handoff of a renamed session's old name

**Description**: when a session re-arms under a new name, record the old name;
the drain empties the old name's mailbox once, then forgets it. Built on
2026-09-14 and parked, uncommitted.

**Pros**: small; no engine change.

**Cons**: leaves names as the address; does nothing for `/clear` (Gap 3) or
for other machines (Gap 4); a guard against a name another session now holds
is needed and only works on one machine.

**Reason for rejection**: patches the identity this RDR demotes.

### Alternative 2: keep old names registered

**Description**: a session keeps draining every name it has held for the
registration retention.

**Reason for rejection**: one session answering to several names, and a pooled
name held by two sessions at once, is the confusion Sam named when rejecting
it (2026-09-14).

### Briefly Rejected

- Session-id addressing with no directory: senders cannot learn session ids.
- Sender discipline only (look the name up just before sending): leaves Gaps 2
  to 4 open.
- Directory entries that live the full 7 days: an ended session's entry would
  resolve for a week, and every arm would add a row to every later read.
- One entry per name (`id_from: keys`): a re-send by a new holder moves only the
  expiry, so the first holder would keep the name.
- Recording the previous id in SessionEnd with reason `clear`: that hook sees
  the old id, but so does the pid marker SessionStart already rewrites, and a
  second hook adds a second timeout.
- Waiting for the harness's own session-id addresses (`sid:`): they sit behind
  a flag that is off here, in an internal format. If they ship, senders could
  address session ids directly and the directory becomes optional.

## Trade-offs

### Consequences

- One engine template and an engine release; the client halves pair with it.
- A new MCP tool (the core server grows by one).
- The watcher sends one small `out` per heartbeat for its name.
- Old instance-name mailboxes drain for one retention window, then stop.

### Risks and Mitigations

- **Directory entries for ended sessions**: an entry lapses within one TTL
  after its watcher stops, so an ended session stops resolving in minutes.
- **A watcher that stops while its session lives**: its entry lapses too, and
  the name stops resolving; mail sent by session id is unaffected. The drain
  hook runs at each prompt and prints a re-arm instruction when it finds no
  live watcher (nexus-6konb.19). It tries only at a prompt at least 600 seconds
  after its last re-arm and at least 60 seconds after its last attempt
  (`_REARM_INTERVAL_S` and `_REARM_RETRY_S` in
  `conexus/hooks/scripts/mailbox_drain.py`), so a session with no prompts stays
  unresolvable by name until its next one. The per-session registry file this
  replaces kept a name registered until another session's arm pruned it, at
  least 7 days after the holder's last arm (`REGISTRATION_RETENTION_S` and
  `prune_stale_registrations` in `src/nexus/tuple_watch.py`), so mail sent to
  the name in that window still landed and waited for the next prompt; here the
  sender gets an error and can resend by session id.
- **Engine and client skew**: a client that sends `address_kind: session` to an
  engine without it gets `SchemaViolation`. The engine deploys before the client
  release that sends it; old clients are unaffected.

### Failure Modes

- The directory is unreachable at send: `mailbox_send` fails loudly; nothing is
  written.
- A heartbeat fails: the entry lapses and the name stops resolving until the
  next successful re-send. Mail to the session id is unaffected.
- SessionStart's output is lost at a clear: the cleared record is written
  before any output, so the drain still finds it.

## Implementation Plan

### Prerequisites

None outstanding: A1 to A5 are verified, and Sam decided the three items the
gate left open (2026-09-14, T2 `nexus_rdr/208-decision-gate-2026-09-14`): a name
held by two live sessions is refused, a fork leaves the parent's mailbox with
the parent, and the lease is a 300-second TTL re-sent every 60 seconds.

### Minimum Viable Validation

Two real sessions on one machine. Session A arms under its name; B sends to
that name through `mailbox_send`; A resumes and is renamed; B sends to the new
name and A receives it; B sends to the old name within one TTL, which arrives,
and after it, which is refused with the name in the error. A runs `/clear`;
mail B sent to A's old session id before the clear arrives at A's first prompt
after it, once. When a second machine shares A's managed `service_url`, a
session there resolves A's name and reaches it; two local-mode machines are
out of scope.

### Phase 1: Engine

#### Step 1: directory template

`directory/<name>` as specified, added to the boot list, with engine tests for
a re-send moving expiry, a lapsed entry dropping out of `rd`, and two sessions
arming one name.

#### Step 2: `address_kind` gains `session`

Additive; `instance` still accepted.

### Phase 2: Client

#### Step 1: the watcher writes its directory entry at arm and re-sends it on a heartbeat

#### Step 2: `mailbox_send`, with its name pins and description lint

#### Step 3: the previous-id read at SessionStart, the cleared record, and its one-time drain

### Phase 3: Transition

Senders move to `mailbox_send`. The drain keeps draining registered
instance-name mailboxes for one retention window (7 days) after the release
that ships Phase 2. Then a client release (R3) stops draining and sending on
them, and only after R3's tag does an engine release (R4) refuse
`address_kind: instance`. Old clients must stop sending it before the engine
refuses it, and a client release tree carries no engine source past its pinned
engine tag (T2 `nexus/plan-rdr-208-implementation-2026-09-14`).

### Day 2 Operations

`nx tuple` gains a `directory` read for inspecting who holds a name.

### New Dependencies

None.

## Test Plan

- A re-send moves an entry's expiry; after its watcher stops, the entry lapses
  within one TTL and the name stops resolving.
- Resolution: when every live entry names one session, mail reaches it; when
  live entries name two sessions, `mailbox_send` writes nothing and its error
  names both.
- Rename: mail to the new name reaches the session; mail to the old name
  arrives within one TTL and is refused after it.
- `/clear`: SessionStart records the previous id before its output; the drain
  empties the previous mailbox and deletes the record only under the three
  conditions above; the budget running out, a refused ack or a row under lease
  keeps it; two `/clear`s before a prompt drain both old mailboxes.
- Two sessions arming one name with the same nonce write two entries.
- A `/clear` self-stop releases the watcher's entry within about a second; an
  ordinary exit leaves it for one TTL.
- `mailbox_send`: an agent-id `to` skips the directory; a subagent's
  `from_address` sends as its agent id; with no session marker and no
  `NX_T1_SESSION_ID` the call is refused.
- Two processes on one session id, one of which runs `/clear`: each message in
  the old mailbox is delivered exactly once, to one of them.
- A watcher that stops while its session lives: the name stops resolving
  within one TTL, mail by session id still arrives, and the name resolves again
  after the re-arm.
- Fork: a forked session writes no cleared record, and the parent's mailbox is
  untouched.
- `mailbox_send` refuses an unresolvable name and writes nothing.
- Transition: a registered instance mailbox is drained until the window ends.

## Validation

### Testing Strategy

Engine tests for the template, client tests against the engine substrate, the
drain hook's subprocess harness for the cleared record, and the MVV above on
real sessions.

## Finalization Gate

### Contradiction Check

Checked section against section after the research pass:

- The identity table, the four gaps, the Technical Design and the Test Plan use
  the same facts: the name changes on `/resume` and is kept on `/clear`, and the
  session id is kept on `/resume` except under `--fork-session` and `/branch`.
- The directory's 7-day retention appears only as the template's ceiling.
  Everywhere an entry's life is described, it is the short TTL the watcher
  renews.
- A cleared record is deleted only under the three conditions in the Technical
  Design's `/clear` paragraph, and the Test Plan states the same rule (both
  amended after the gate).
- Gap 4 and the MVV both limit cross-machine resolution to sessions that share
  a managed `service_url`.

No contradiction found.

### Assumption Verification

A1 to A5 are verified: A1 and A2 by observation on this machine (research-3),
A3 from the harness code and its documentation (research-3), and A4 and A5 by
reading `TupleRepository` (research-1). Nothing the design rests on is assumed.
Three items were open decisions at the gate, not assumptions; Sam decided them
on 2026-09-14 (see Prerequisites).

### Scope Verification

The MVV is in scope and runs on real sessions; nothing in it is deferred. Its
second-machine step runs only where a second machine shares the managed
`service_url`. Two local-mode machines are out of scope, because they share no
tuple space.

### Cross-Cutting Concerns

- **Versioning**: the directory template and `address_kind: session` ship in an
  engine release. A client that sends `address_kind: session` to an older engine
  gets `SchemaViolation`, so the engine deploys before the client release that
  sends it. Old clients are unaffected.
- **Deployment model**: for Phases 1 and 2, engine tag first, then the client
  release, paired through the wire ledger. The Phase 3 retirement reverses the
  order: the client release that stops sending `address_kind: instance` ships
  before the engine release that refuses it.
- **Incremental adoption**: instance-name mailboxes keep draining for one
  retention window after the release (Phase 3).
- **Memory management**: the live directory rows under a name are about one per
  live watcher that holds it. Lapsed rows are deleted by the existing expiry
  purge.
- **Build tool compatibility, licensing, IDE compatibility, secret lifecycle**:
  N/A.

### Proportionality

One template, one address kind, one MCP tool, one SessionStart read and one
drain record. No new store and no new table.

## References

- RDR-205 §Technical Design, Phase 6
- JDR-001, the T1 three-scopes record
- Beads nexus-6konb.19, .20, .21
- T2 `nexus_rdr/208-research-1` to `-4` (2026-09-14); T3
  `tuple-space/research-rdr-208-name-directory-prior-art`
- Claude Code documentation: sessions (code.claude.com/docs/en/sessions),
  hooks (code.claude.com/docs/en/hooks), CLI reference
  (code.claude.com/docs/en/cli-reference)
- ZooKeeper programmer's guide (sessions and ephemeral nodes); etcd leases;
  Consul sessions; Erlang `global` (name conflict resolution); RFC 3261
  (registrar bindings)

## Revision History

### 2026-09-14 — Created

Drafted at Sam's direction after the name-handoff build for nexus-6konb.21 was
paused: the name is the least stable of the three identities the harness
provides, and the session id plus a recorded `/clear` link covers what the
name cannot.

### 2026-09-14 — Research pass (four records, T2 `nexus_rdr/208-research-1` to `-4`)

Engine, client, harness-identity and prior-art passes. Design changes from the
findings: directory entries live only while their watcher re-sends them,
instead of 7 days, which also makes the newest-entry read one `rd`;
`mailbox_send` reports a name held by two live sessions; SessionStart gains the
previous-id read the draft assumed already existed; the drain forgets a cleared
mailbox when a claim returns nothing; a fork leaves its parent's mailbox with
the parent; Gap 4 and the MVV are scoped to sessions that share an engine; the
identity table records that names are kept across `/clear`. A3 moved from
unverified to verified, and A4 was corrected. Open for the gate: the conflict
behavior, the fork rule, and the TTL and heartbeat values.

- 2026-09-14: Gate round 1 — PASSED (0 Critical, 3 Significant, 0 ship-blocker(s)); commit `3b07bee53`; critique `nexus_rdr/208-gate-critique-2026-09-14`.
- 2026-09-14: Post-accept amendment: Sam's three gate decisions (T2 `nexus_rdr/208-decision-gate-2026-09-14`), so a name held by two live sessions is refused rather than delivered to the newest; and gate Significants 1 and 2 (a watcher that stops while its session lives; `/clear` with two processes on one session id). Fix check recorded in T2 as `nexus_rdr/208-fix-check-<tip>`.
- 2026-09-14: Post-accept amendment: the Risks bullet on a stopped watcher states the registry file's 7-day retention instead of "never expired" (observation d1 of fix check `nexus_rdr/208-fix-check-0bfcc6bb7`). Fix check recorded in T2 as `nexus_rdr/208-fix-check-<tip>`.
- 2026-09-14: Post-accept amendment: six design items from the implementation plan's audit (T2 `nexus/plan-rdr-208-implementation-2026-09-14`): `id_dims` on `session_id`; agent-id routing and `from_address` in `mailbox_send`; `mailbox_send`'s default sender; the watcher's release on a `/clear` self-stop; the three-condition delete rule; chained `/clear`s. Fix check recorded in T2 as `nexus_rdr/208-fix-check-<tip>`.
- 2026-09-14: Fix round on amendment 2: the watcher's release lapses within about a second rather than at once; the Existing Infrastructure Audit's drain row and the Finalization Gate's Contradiction Check state the three-condition rule; a row still claimed under lease is named as an outcome, not a claim-loop ending; the Technical Environment says `rd` returns claimed and dead-lettered rows with their `claim_state`; `from_address` takes a session-id or agent-id shape; Phase 3 and the Deployment model give the client-before-engine order for retiring `address_kind: instance` (fix check `nexus_rdr/208-fix-check-a91e461ea`). Fix check recorded in T2 as `nexus_rdr/208-fix-check-<tip>`.
