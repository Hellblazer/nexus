---
title: "Channel Delivery Without a Proof Gate: Notify a Reference, Let the Session Claim"
id: RDR-213
type: Architecture
status: draft
priority: high
author: Sam
reviewed-by: self
created: 2026-09-17
accepted_date:
related_issues: [nexus-tk2cz]
related_rdrs: [RDR-211, RDR-205, RDR-206, RDR-208]
---

# RDR-213: Channel Delivery Without a Proof Gate: Notify a Reference, Let the Session Claim

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.
> Prose: see REGISTER.md beside this template. Write for a smart reader who
> may not know the jargon; define terms on first use; simplified, never
> simplistic.

**Provenance.** Sam, 2026-09-17, after the channel-waiter defects of bead
nexus-tk2cz were fixed and verified live: "have we solved this or just fixed a
bug", then "prep 7.51.1 with the bug fixes, then draft the amendment." On
reading the draft: "frankly, I thought that's what I asked for arch wise. the
idea is the channel just notifies and the session has to claim. this solves
this." So this design is Sam's original intent for channel delivery; RDR-211's
claim-at-delivery was the drafting rounds' reading of the back-pressure ruling
(one message outstanding, the ack as the credit), not the ruling itself. This
RDR amends the Delivery design of RDR-211, which is closed; it changes the
client only, no engine or wire change.

Terms used throughout:

- **Channel**: Claude Code's research-preview path by which an MCP server
  pushes a notification (`notifications/claude/channel`) into a running
  session. The session opts in per launch with a `--channels` flag.
- **Waiter**: the background task in the session's own nexus MCP server
  (`src/nexus/mcp/channel.py`) that parks a `wait` over the session's
  subscriptions and pushes what arrives through the channel.
- **Reference**: what a notification carries, per RDR-211 (Sam's ruling of
  2026-09-17): the subspace and tuple id, never the body.
- **Claim**: the tuple-space `in` operation, which leases a row to one
  claimant until it is acked, nacked, released or the lease lapses.
- **Proof**: RDR-211's precondition for claiming. The waiter must first
  establish that the session can hear the channel, by reading the parent
  `claude` process's command line or by a probe notification the session
  answers with a tool call.
- **Drain hook**: `mailbox_drain.py`, run on every prompt, which claims,
  acks and renders whatever is waiting. It is the floor under the channel.

## Problem Statement

RDR-211 has the waiter claim a mailbox message at delivery, then push the
reference and hold the claim until the session acks or nacks it, re-sending up
to five times and releasing after that. A claim made for a session that cannot
hear the channel strands the message for the lease, so RDR-211 gates every
claim on proof that the channel is live. Both proofs are heuristics, and a
heuristic that fails leaves a session with no push at all, silently, for the
life of its MCP server process. Nothing in the engine or the wire requires
this: board posts already travel as notify-only references with no claim, in
the same waiter.

### Enumerated gaps to close

#### Gap 1: A claim needs a proof the client cannot give

Claude Code's handshake and environment are identical with and without the
channel flag (RDR-211 Step 0 spike, 2026-09-17), so the waiter reads the
parent process's command line for known flag spellings, and falls back to a
probe notification the model must act on. On 2026-09-17 a real session
launched with the plugin form of the flag matched neither spelling the gate
knew, and its startup probe was sent before Claude Code had registered the
channel and was lost (bead nexus-tk2cz). That session would never have
claimed a message. The fix added spellings and a second probe; a wrapper
script, a renamed marketplace, or a session busy at both probe instants
reproduces the failure.

#### Gap 2: A lost notification costs the message its lease

With claim-at-delivery, a notification Claude Code drops (the registration
race above, or any future drop) leaves the message claimed by a waiter that
has no way to know the drop happened. The re-send cadence recovers it after
150 seconds; a waiter that dies with the claim open (the empty-wait defect of
nexus-tk2cz) leaves it for the full 300-second lease before the drain hook
can take it. The message was never at risk of loss; it was at risk of a
delay the session did not choose.

#### Gap 3: The proof machinery is most of the waiter

The gate, the probe tool (`tuple_channel_probe`), the flag table, the
waiter's claimant identity, its lease and renew loop, the release-after-five
count, the persisted outstanding claim and its adoption at restart, and the
doctor row's `proof`, `unacked` and `released` facts all exist to make
claiming safe. Every one is a place the 2026-09-17 finding could recur.

## Relationship to Prior RDRs

| Prior RDR | Relationship | What it means for this one |
| --- | --- | --- |
| RDR-211 | Origin | It chose claim-at-delivery so a re-send is bounded and the drain hook never renders a message the channel already delivered, and gated the claim on proof because a stranded claim is worse than no push. The rationale for bounding re-sends holds and is kept by other means (Approach item 2). The rationale for the proof gate has expired: the client cannot give the proof (Step 0), so the gate can only be a heuristic. This RDR amends RDR-211's Technical Design (Delivery, Subscriptions) and Approach item 7. |
| RDR-211 board delivery | Precedent | Board posts are pushed as references with no claim, with a cursor so a post is announced once. This RDR extends that shape to mailboxes. |
| RDR-205 | Origin of the floor | The drain hook claims and renders at every prompt regardless of the channel. Unchanged; it is what makes a notify-only channel safe to lose. |
| RDR-206 | Precedent | `renew` and reply-in-ack stay as session operations. The waiter stops using `renew`; nothing in RDR-206 changes. |
| RDR-208 | Adjacent, shipped | Session-id addressing and the directory lease are untouched; the instance-name subscription keeps writing the lease. |

## Context

### Background

RDR-211 shipped in engine-service-v0.1.127 and conexus 7.51.0 on 2026-09-17.
The same day, a test of the dialog-free launch form
(`--channels plugin:conexus@nexus-plugins` with the `allowedChannelPlugins`
managed setting) found the waiter proof-gated to `none`, its probe lost, and,
once proven by hand, dead after its first delivery because a mailbox-only
subscription set leaves the wait spec empty while a claim is outstanding.
Both were fixed and verified live (T2
`nexus/channel-waiter-fix-live-verification-2026-09-17`) and ship in 7.51.1.
The fixes are patches to two measured failures of a design whose safety rests
on knowing something the client does not tell us.

### Technical Environment

Claude Code 2.1.274; channels are a research preview, opt-in per launch, not
available on Amazon Bedrock, Google Cloud Agent Platform or Microsoft Foundry.
Engine v0.1.127: `wait` over up to 34 subspaces, `in` with a lease, the
mailbox template with `take.max_attempts: 3`. Client 7.51.1: `ChannelWaiter`
in `src/nexus/mcp/channel.py`, `SubscriptionSet` in
`src/nexus/mcp/subscriptions.py`, the drain hook in
`conexus/hooks/scripts/mailbox_drain.py`.

## Research Findings

### Investigation

Read at the 7.51.1 tree: `ChannelWaiter.run`, `tick`, `_maybe_claim_mail`,
`_renew_or_release`, `_adopt_persisted_outstanding`, `detect_channel_argv`;
the board path `_process_results` and `_deliver_board_post`; the drain hook's
claim loop; the doctor row `tuples.channel_delivery` in `src/nexus/health.py`.
Measurements: T2 `nexus/channel-plugin-form-allowlist-measurement-2026-09-17`
(registration race, lost probe, dead waiter) and
`nexus/channel-waiter-fix-live-verification-2026-09-17` (fix verified on a
throwaway engine with real sessions).

#### Dependency Source Verification

| Dependency | Source Searched? | Key Findings |
| --- | --- | --- |
| Claude Code channels (docs and 2.1.274 binary strings) | Yes | No handshake marker for the channel; `--channels` required every launch; channel delivery registered about 0.5 s after the MCP connection is up; a notification sent before that is dropped. |
| Engine `wait` (`TupleRepository.waitAny`) | Yes | A mailbox spec with `n=1` and no cursor returns the oldest available row on every wake while it stays available; an empty spec list is refused. |
| Engine `in` | Yes | Claims the oldest matching available row for the caller; the mailbox template pins `to`, so a session's `in` on its own mailbox claims its oldest message. |

### Key Discoveries

- **Verified**: a board post is delivered as a reference with no claim and a
  cursor, by the same waiter, and this path has no proof gate.
- **Verified**: without a proof, the only cost of a lost mailbox notification
  is that the message waits for the next wake or the next prompt; it is never
  claimed by anyone but the session or the drain hook.
- **Verified**: the drain hook claims at every prompt; a message the channel
  referenced and the session did not claim is rendered at the next prompt
  regardless.
- **Documented**: Claude Code drops a notification sent before it registers
  the channel; no acknowledgement of a notification exists in the protocol.
- **Assumed**: a session that receives a reference and is told to claim with
  `tuple_in` does so reliably. RDR-211's MVV showed sessions acting on
  references (`tuple_rd` then `tuple_ack`); the claim step is one more tool
  call on the same instruction.

### Critical Assumptions

- [ ] A session acts on a reference by calling `tuple_in` on its mailbox at
  least as reliably as it called `tuple_rd` and `tuple_ack` under RDR-211.
  **Status**: Unverified. **Method**: Spike (two real sessions, the Step 0
  harness of RDR-211).
- [ ] Re-announcing an unclaimed row at each wake, damped to the cadence in
  Technical Design, does not disturb an idle session more than RDR-211's
  re-send did. **Status**: Unverified. **Method**: Spike (count wakes over ten
  minutes with one unclaimed row).

## Proposed Solution

### Approach

1. **The waiter never claims mail.** It parks `wait` over every subscription
   including mailboxes (`n=1`, no cursor) and pushes a reference for the
   oldest available row. The session claims with `tuple_in` on its mailbox,
   then acks, nacks, releases or replies as today.
2. **One message in front of the session at a time, bounded.** The waiter
   announces only the oldest unclaimed row of each mailbox. It re-announces
   the same row at most once every 150 seconds and at most five times, then
   stops announcing it and leaves it to the drain hook. A restart announces
   it once more. This keeps RDR-211's back-pressure ruling in effect with no
   claim.
3. **No proof, no probe, no flag table.** The waiter starts parking at
   lifespan start and pushes whenever a row appears. A session that cannot
   hear the channel loses nothing: its rows stay available and the drain hook
   renders them at the next prompt. `tuple_channel_probe` and
   `detect_channel_argv` are deleted, not kept as fallbacks.
4. **The status record and doctor row report what is observable.** `alive`,
   `last_wake`, `announced` (rows announced, cumulative), `pending` (rows
   announced and not yet gone, 0 or 1 per mailbox), `oldest_pending_age_s`.
   The `proof`, `unacked` and `released` facts go.
5. **Skills and docs say claim, not read.** The mailbox skill's push rule
   becomes: on a reference, `tuple_in` your mailbox, act, then ack or nack.
   The launch flag remains the only opt-in and both forms stay documented.

### Technical Design

**Delivery.** `tick()` builds one spec per subscription: boards with their
cursor as today, mailboxes with `n=1` and no cursor. On wake, board results
are handled as today. For each mailbox result the waiter looks at the oldest
row it returned. If that row's id is not in the announce table, it sends the
reference and records `(id, first_announced, count=1)`. If it is, and
`now - last_announced >= 150 s` and `count < 5`, it re-sends and increments.
Otherwise it does nothing. A row that stops appearing (claimed, consumed,
expired) is dropped from the table at the next wake it is absent. The table is
in memory; a restart starts empty, so the oldest row is announced once more.

**Notification content**, unchanged in shape from RDR-211's reference:
subspace, tuple id, the instruction to claim with `tuple_in` on that subspace
and then act. No body, no `from`, no `kind`.

**Back pressure.** A mailbox with two available rows announces the oldest
only; `wait` with `n=1` returns exactly that row. Once the session claims and
acks it, the next wake returns the next row and the waiter announces it.

**Empty spec.** With every subscription in the spec on every tick, the spec
list is never empty; the 7.51.1 sleep branch becomes dead code and is removed
with a test that proves a mailbox-only session parks on its mailbox.

**Errors.** The 7.51.1 rule stays: a bare 404 from `wait` stops the waiter
(engine without the route); any other failure is logged and retried after a
backoff.

**Deleted.** `_CHANNEL_ARGV_FLAGS`, `detect_channel_argv`,
`_read_parent_command`, `_PROBE_CONTENT`, `_probe_until_live`,
`_send_probe`, `note_probe_ack`, the `tuple_channel_probe` tool, `claimant`,
`lease_s`, `renew_interval_s`, `max_resends`, `_Outstanding`,
`_maybe_claim_mail`, `_renew_or_release`, `_adopt_persisted_outstanding`,
`note_credit` and the credit hooks in `tuple_ack`, `tuple_nack` and
`tuple_release`. The deletion census test of RDR-211 gains these names.

```text
// Illustrative; the announce table and its cadence
announce: dict[tuple_id, (first_announced: float, last_announced: float, count: int)]
on wake, for the oldest available row r of mailbox m:
  if r.id not in announce: send(ref(m, r.id)); announce[r.id] = (now, now, 1)
  elif now - last >= 150 and count < 5: send(ref(m, r.id)); update
drop ids absent from this wake's results
```

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| Mailbox announce table | `_deliver_board_post` and the board cursor in `channel.py` | Extend: the board path is already notify-only; mailboxes use a per-row table where boards use a cursor |
| Session-side claim | `tuple_in` MCP tool, `mailbox_drain.py` claim loop | Reuse unchanged |
| Status record and doctor row | `write_channel_status`, `_check_tuple_channel_delivery` in `health.py` | Extend: replace the three claim facts with the two announce facts |
| Proof gate and probe | `detect_channel_argv`, `tuple_channel_probe` | Replace with nothing; delete |

### Decision Rationale

The design that needs proof cannot get it, so its safety is a heuristic and
its failure is silent. The design that needs no proof already exists in the
same file for boards and has no comparable failure mode: the worst a lost
notification does is defer to the next wake or the next prompt. The cost is a
possible duplicate reference (channel, then the drain hook's body at the
prompt), which is a repeat of a pointer, not of content.

## Alternatives Considered

### Alternative 1: Keep claim-at-delivery, probe on every tick until proven

**Description**: leave the gate and re-send the probe at each wake until the
session answers.

**Pros**: smallest diff; keeps the single-render guarantee.

**Cons**: a session that never answers (busy, or a model that does not act on
the probe text) is probed every 25 seconds forever; the argv table stays as a
second heuristic; a stranded claim remains possible on every drop.

**Reason for rejection**: it makes the heuristic louder, not sound.

### Alternative 2: Claim-at-delivery with a short lease

**Description**: keep claiming but with a 30-second lease and no renew, so a
stranded claim costs little.

**Pros**: bounds the strand.

**Cons**: a session that takes longer than the lease to act loses the claim to
the drain hook or a restart mid-work; every lapse counts an attempt against
`max_attempts: 3`, so three slow reads dead-letter a message.

**Reason for rejection**: it converts a delay into data loss.

### Briefly Rejected

- **Read the channel state from Claude Code**: no handshake marker exists
  (RDR-211 Step 0, measured); nothing to read.
- **Make the drain hook the only path**: it already is the floor; the point
  of the channel is delivery while idle.

## Trade-offs

### Consequences

- Positive: no proof, so no silent no-push state; the launch flag alone
  decides whether a session hears the channel, and a session that does not
  hear it is exactly as well served as today's floor.
- Positive: about a third of `channel.py` and one MCP tool are deleted; the
  waiter holds no lease and cannot strand a message.
- Positive: the mailbox path and the board path become one shape.
- Negative: a message can be referenced by the channel and then rendered in
  full by the drain hook at the next prompt if the session did not claim it
  in between. That is a duplicate pointer, never a duplicate body.
- Negative: a session must make one more tool call (`tuple_in`) before it can
  read; the skill text changes and every installed plugin re-learns it at the
  next release.

### Risks and Mitigations

- **Risk**: sessions do not act on the reference and rows pile up unclaimed.
  **Mitigation**: the drain hook claims at the next prompt as before; the
  doctor row reports `oldest_pending_age_s`.
- **Risk**: the re-announce cadence wakes an idle session five times for one
  ignored message.
  **Mitigation**: the same bound RDR-211 chose for re-sends; the second
  critical assumption measures it.
- **Risk**: the drain hook and a session's `tuple_in` race for the same row.
  **Mitigation**: `in` is atomic; the loser gets nothing and does nothing,
  as the drain hook already handles today.

### Failure Modes

- Channel not live (no flag, or Claude Code drops notifications): nothing is
  pushed, the doctor row shows `announced` not advancing while rows are
  pending, the drain hook renders at the next prompt. No claim is held.
- Waiter dead (bare 404, engine without `wait`): as 7.51.1.
- Session claims and then crashes before ack: the lease lapses and the row is
  available again; the drain hook or the next announce picks it up. One
  attempt is spent, as for any claimant.

## Implementation Plan

### Prerequisites

- [ ] Both Critical Assumptions verified by the spike named there.
- [ ] 7.51.1 shipped, so the MVV compares against a live waiter, not the dead
  one.

### Minimum Viable Validation

Two real Claude Code sessions on a throwaway engine, the RDR-211 harness
(`scratchpad rdr211-mvv`): (1) mail to an idle session with the channel:
reference arrives, the session claims with `tuple_in`, reads, acks; a second
message follows the same way on the same waiter. (2) Mail to a session
launched without the flag: no notification; the drain hook renders it at the
next prompt. (3) Kill the MCP server between announce and claim, restart it:
the row is announced once more and claimed. (4) Leave one message unclaimed
for ten minutes: at most five announcements, then silence, doctor row shows
`pending 1` and its age. Counts recorded in this section.

### Phase 1: Code Implementation

#### Step 1: Mailbox announce path

Replace `_maybe_claim_mail` and `_renew_or_release` with the announce table
and cadence; mailboxes enter the wait spec on every tick. Delete the proof
gate, the probe and the flag table. Tests before code: mailbox-only session
parks on its mailbox; oldest row announced once; re-announce cadence and cap;
absent row dropped; restart announces once; 404 stops, other errors retry.

#### Step 2: Tools, status record and doctor row

Delete `tuple_channel_probe` and the credit hooks; write the new status
facts; rewrite `tuples.channel_delivery`. Tool count and deletion census
tests updated.

#### Step 3: Skills and docs

Mailbox and peer-messaging skills: claim with `tuple_in` on a reference.
`docs/tuple-space.md`, `web/coordination.html`, `web/tuple-space.html`,
`docs/architecture.md` module map. RDR-211's Delivery section gains a note
pointing here.

### Phase 2: Operational Activation

#### Activation Step 1: Client release

A client release carries it; no engine tag, no wire-ledger entry. The
plugin pin advances with it so the skill text is live.

### Day 2 Operations

| Resource | List | Info | Delete | Verify | Backup |
| --- | --- | --- | --- | --- | --- |
| Channel status record (`<config>/tuple-watch/channel-status.d/<session>`) | N/A (one per session) | doctor row | swept with the session's tuple-watch files as today | doctor row | N/A |

### New Dependencies

None.

## Test Plan

- **Scenario**: mailbox-only session, one message arrives while idle.
  **Verify**: one reference pushed within one wait tick; the session's
  `tuple_in` claims it; no waiter claim ever recorded.
- **Scenario**: two messages arrive together. **Verify**: only the oldest is
  announced; the second is announced after the first is acked.
- **Scenario**: the session ignores a reference. **Verify**: re-announced at
  150 s intervals, five times, then silence; the drain hook renders it at the
  next prompt.
- **Scenario**: notification dropped by Claude Code (fake sender returns
  false). **Verify**: no claim exists; the row is announced again at the next
  cadence point.
- **Scenario**: session launched without the flag. **Verify**: nothing
  pushed; drain hook renders at the next prompt; doctor row informational.
- **Scenario**: waiter restart with one pending row. **Verify**: announced
  once more, count restarts at one.
- **Scenario**: engine without `wait`. **Verify**: waiter stops, doctor row
  says so.
- **Scenario**: board post and mailbox message in one wake. **Verify**: both
  references pushed, board cursor advances, mailbox row stays available.

## Validation

### Testing Strategy

1. **Scenario**: the Test Plan above as unit tests against the fake store.
   **Expected**: every scenario green; the deletion census names every
   removed symbol.
2. **Scenario**: the MVV above on real sessions.
   **Expected**: counts recorded; assumption 1 and 2 verified.

### Performance Expectations

No new load: one `wait` per tick as today; announcements are bounded per
row. Measured, not estimated, in the MVV.

## Finalization Gate

### Contradiction Check

To be written at gate time.

### Assumption Verification

To be written at gate time.

#### API Verification

| API Call | Library | Verification |
| --- | --- | --- |
| `POST /v1/tuples/wait` with mailbox specs `n=1` | engine | Source Search (TupleRepository.waitAny) |
| `POST /v1/tuples/in` from the session | engine | Source Search |
| `notifications/claude/channel` | Claude Code 2.1.274 | Spike (RDR-211 Step 0, nexus-tk2cz) |

### Scope Verification

The MVV is Phase 1's exit, not deferred.

### Cross-Cutting Concerns

- **Versioning**: client-only; the plugin pin advances with the release. N/A
  for the engine.
- **Build tool compatibility**: N/A
- **Licensing**: N/A
- **Deployment model**: no cloud change.
- **IDE compatibility**: N/A
- **Incremental adoption**: a session on the old client keeps claim-at-
  delivery until it upgrades; both coexist against the same engine.

## Open Questions

1. Should the reference name the tuple id at all, given the session claims
   the oldest row rather than a specific one? Naming it lets the session
   check it got what was announced.
2. Does the mailbox skill keep `tuple_rd` before `tuple_in` (read then
   claim) or claim first? Claim first is one call fewer and avoids reading a
   row another claimant takes.

## Revision History

- 2026-09-17: created (draft) from Sam's request after bead nexus-tk2cz;
  amends RDR-211's Delivery design.
- 2026-09-17: Sam confirmed the design as his original intent for channel
  delivery (T2 `nexus_rdr/213-decision-notify-then-claim-2026-09-17`); the
  Provenance paragraph records it.
