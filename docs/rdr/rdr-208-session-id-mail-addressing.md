---
title: "Session-Id Mail Addressing: One Mailbox per Session, Names Resolved at Send Time"
id: RDR-208
type: Architecture
status: draft
priority: medium
author: Sam
reviewed-by: self
created: 2026-09-14
accepted_date:
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
boundary. Measured on one session on 2026-09-14:

| Identity | `/resume` | `/clear` | `/compact` |
|---|---|---|---|
| `ListAgents` name | changed (nexus-58 to nexus-03) | not measured | kept |
| Session id | kept (the same id before and after) | changes, by design (JDR-001) | kept |
| Claude process pid | changed (47502 to 765) | kept (the watcher's stop marker relies on it) | kept |

No identity the harness provides holds across both `/resume` and `/clear`. The
name, the one mail is addressed to, breaks on the most common boundary.

### Enumerated gaps to close

#### Gap 1: mail to a renamed session's old name is never read

After a resume renames a session, mail a peer sends to the old name lands in a
mailbox no watcher covers and no drain reads. It sits until the 7-day
retention expires. The peer-messaging skill now tells senders to look the name
up just before sending, which narrows the window to mail sent in the minutes
around a rename; it does not close it.

#### Gap 2: a name can pass to another session

Names appear to come from a small pool (two digits after the repo name). A
session that later receives a name drains whatever earlier mail was addressed
to it, and two sessions that each held the name at different times both have a
claim on its mailbox. Unverified: whether the harness actually reuses names.

#### Gap 3: `/clear` strands the previous session's mail

`/clear` mints a new session id. Mail sent to the old session id, before or
during the clear, sits in a mailbox the new session's drain never reads.
Nothing records the previous id: the T1 handoff marker holds only the new
session id, and the tuple-watch marker (`session.<claude pid>`) holds only the
current one.

#### Gap 4: name resolution exists on one machine only

The only record of which session holds which name is a local file
(`<config>/tuple-watch/addresses.d/<session id>`). A sender on another machine,
or a cloud session, cannot resolve a name at all.

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
- `out` is idempotent by construction: a tuple's id comes from the template's
  `id_from` fields, so a retried `out` is the same tuple and does not update it.
- Watcher and drain resolve the session id from the process environment and
  the hook payload; neither needs the name for the session-id mailbox.

## Research Findings

### Key Discoveries

- The session id survives `/resume` and `/compact` and is already a mailbox
  every watcher covers by default. Measured on one session.
- `/clear` changes the session id, but SessionStart runs and overwrites the
  tuple-watch marker for the same claude pid, so the previous id is available
  at exactly the moment it is replaced; today it is discarded.
- Senders know names, not session ids: `ListAgents` shows a name and a short
  ref, never the session id. Any session-id scheme needs a name directory.

### Critical Assumptions

- **A1**: the session id is unchanged across `/resume`. Status: verified once
  (this session). Method: record the id before and after a resume in the MVV.
- **A2**: the claude pid is unchanged across `/clear`. Status: relied on by
  the shipped watcher stop marker (nexus-6konb.12). Method: the MVV.
- **A3**: `ListAgents` names are reused across sessions. Status: unverified.
  The design is correct either way (newest directory entry wins); only Gap 2's
  severity depends on it.
- **A4**: a second `out` with the same id leaves the first tuple unchanged.
  Status: stated in RDR-205; to verify against `TupleRepository` before Phase 1.

## Proposed Solution

### Approach

Every session has one mailbox, keyed by its session id. Names become aliases a
sender resolves at send time through a directory in the tuple space. `/clear`
drains the previous session's mailbox once.

### Technical Design

**Mailbox.** Mail to a session goes to `mailbox/<session id>`. The watcher and
the drain cover that mailbox and no instance-name mailbox, once the transition
in Phase 3 ends. `address_kind` keeps `agent` and gains `session`; `instance`
is retired.

**Directory.** A new template `directory/<name>`: keys `name`; dims
`session_id` (required); `id_from: keys+nonce`, the nonce the arm time, so
every arm writes a new entry rather than being absorbed by idempotency (A4);
take disabled; retention 7 days, matching the mailbox. `nx tuple watch
--instance NAME` writes an entry at every arm. To resolve a name, a sender
reads `directory/<name>` and takes the entry with the newest `created_at`.
Rename on resume: the session re-arms under the new name, and the old name's
newest entry still names the same session, so mail to either name arrives.
Reuse: a session that later arms under a pooled name writes a newer entry and
wins. No local file is consulted, so resolution works from any machine
(Gap 4).

**Sending.** A new MCP tool `mailbox_send(to, body, kind, correlation_id)`
resolves `to`: a session-id shape is used as is; anything else is looked up in
the directory; an unresolvable name is an error naming the name, never a
silent write to a mailbox nobody reads. The mailbox and peer-messaging skills
send through it; raw `tuple_out` to `mailbox/` stays possible and documented
as the low-level path.

**`/clear`.** On `source=clear`, SessionStart, which already overwrites the
tuple-watch marker for its claude pid, first reads the previous session id from
it and writes `<config>/tuple-watch/cleared.<new session id>` naming that id.
The drain for the new session drains the named mailbox once, forgetting it
only when a pass leaves it empty (the rule `_drain_address` already reports),
then deletes the record. `/resume` and `/compact` need nothing.

### Existing Infrastructure Audit

| Piece | Where | Change |
|---|---|---|
| Mailbox template | `service/src/main/resources/tuples/templates/mailbox.yaml` | `address_kind` values gain `session`; `instance` retired after Phase 3 |
| Directory template | new, same directory | new template; an engine release |
| Watcher | `src/nexus/tuple_watch.py`, `commands/tuple_cmd.py` | writes a directory entry at arm; stops watching instance mailboxes after Phase 3 |
| Drain hook | `conexus/hooks/scripts/mailbox_drain.py` | drains the cleared record's mailbox once; drops instance mailboxes after Phase 3 |
| SessionStart | `src/nexus/hooks.py` | records the previous session id on `source=clear` |
| MCP | `src/nexus/mcp/core.py` | `mailbox_send` |

### Decision Rationale

The session id is the identity the harness keeps across the most boundaries,
and it is already a watched mailbox. Moving the address there removes the
rename and reuse cases instead of handing mail between names, and the one
boundary it does not survive, `/clear`, is a boundary the harness reports
explicitly, so it can be handled at a single, known moment.

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

## Trade-offs

### Consequences

- One engine template and an engine release; the client halves pair with it.
- A new MCP tool (the core server grows by one).
- Old instance-name mailboxes drain for one retention window, then stop.

### Risks and Mitigations

- **Directory entries for ended sessions**: an entry outlives its session for
  up to 7 days. Mitigation: newest entry wins, so a live holder always
  outranks an ended one; mail to a name only an ended session held goes to
  that session's mailbox and expires, as mail to an ended session does today.
- **Clock ordering**: resolution orders by the engine's `created_at`, one
  clock, not the senders'.

### Failure Modes

- The directory is unreachable at send: `mailbox_send` fails loudly; nothing is
  written.
- SessionStart's output is lost at a clear: the cleared record is written
  before any output, so the drain still finds it.

## Implementation Plan

### Prerequisites

- Verify A4 against `TupleRepository`.

### Minimum Viable Validation

Two real sessions on one box and one on a second machine. Session A arms under
its name; B sends to that name through `mailbox_send`; A resumes and is
renamed; B sends to the old name and to the new one; A receives both. A runs
`/clear`; mail B sent to A's old session id before the clear arrives at A's
first prompt after it, once. C, on the second machine, resolves A's name and
reaches it.

### Phase 1: Engine

#### Step 1: directory template

`directory/<name>` as specified, with engine tests for the newest-entry read
and for two sessions arming the same name.

#### Step 2: `address_kind` gains `session`

Additive; `instance` still accepted.

### Phase 2: Client

#### Step 1: the watcher writes a directory entry at every arm

#### Step 2: `mailbox_send`

#### Step 3: the cleared record and its one-time drain

### Phase 3: Transition

Senders move to `mailbox_send`. The drain keeps draining registered
instance-name mailboxes for one retention window (7 days) after the release,
then stops, and `address_kind: instance` is refused.

### Day 2 Operations

`nx tuple` gains a `directory` read for inspecting who holds a name.

### New Dependencies

None.

## Test Plan

- Resolution picks the newest entry; a re-arm under the same name by the same
  session writes a new entry.
- Rename: mail to the old and the new name both reach the session.
- Reuse: after another session arms a name, mail resolves to it, not the
  earlier holder.
- `/clear`: the cleared record is written before SessionStart output; the
  drain empties the previous mailbox once and deletes the record; a pass that
  does not empty it keeps the record.
- `mailbox_send` refuses an unresolvable name and writes nothing.
- Transition: a registered instance mailbox is drained until the window ends.

## Validation

### Testing Strategy

Engine tests for the template, client tests against the engine substrate, the
drain hook's subprocess harness for the cleared record, and the MVV above on
real sessions.

## Finalization Gate

Not yet run.

## References

- RDR-205 §Technical Design, Phase 6
- JDR-001, the T1 three-scopes record
- Beads nexus-6konb.19, .20, .21

## Revision History

### 2026-09-14 — Created

Drafted at Sam's direction after the name-handoff build for nexus-6konb.21 was
paused: the name is the least stable of the three identities the harness
provides, and the session id plus a recorded `/clear` link covers what the
name cannot.
