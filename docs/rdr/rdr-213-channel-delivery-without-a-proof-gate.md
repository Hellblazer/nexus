---
title: "Channel Delivery Without a Proof Gate: Notify a Reference, Let the Session Claim"
id: RDR-213
type: Architecture
status: accepted
priority: high
author: Sam
reviewed-by: self
created: 2026-09-17
accepted_date: 2026-09-17
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
- **Verified**: a session without the plugin's hooks that receives a
  reference and is told to claim with `tuple_in` does so reliably: five of
  five messages claimed and acked by a real session in the spike (T2
  `nexus_rdr/213-spike-1-session-claims-2026-09-17`), including two that
  arrived together, announced oldest first.
- **Verified**: with the conexus plugin's hooks loaded and a live channel,
  the channel notification itself fires `UserPromptSubmit`, so the drain
  hook claims, acks and renders the body about 150 ms after the reference,
  before the session's own turn; the session's `tuple_in` then returns
  nothing (T2 `nexus_rdr/213-mvv-2026-09-17`, 2 of 2 messages).

### Critical Assumptions

- [x] A session acts on a reference by calling `tuple_in` on its mailbox at
  least as reliably as it called `tuple_rd` and `tuple_ack` under RDR-211.
  **Status**: Verified. **Method**: Spike, live (T2
  `nexus_rdr/213-spike-1-session-claims-2026-09-17`). Five messages sent to
  one real Claude Code session against a throwaway engine, including two
  sent together with no wait between: 5/5 claimed via `tuple_in` and acked,
  the concurrent pair correctly showing only the oldest announced until the
  first was gone, and the waiter itself never held a claim throughout (final
  mailbox stats: consumed=5, claimed=0). The spike ran without the
  plugin's hooks loaded, so it measured the hookless claim path only; the
  MVV (T2 `nexus_rdr/213-mvv-2026-09-17`) separately found that with the
  plugin's hooks loaded, the drain hook claims, acks and renders the body
  at the wake, before the session's own `tuple_in` would run. The claim
  path above is verified for a hookless session; it is not the primary
  hook-delivers path.
- [x] Re-announcing an unclaimed row at each wake, damped to the cadence in
  Technical Design, does not disturb an idle session more than RDR-211's
  re-send did (150 s and 5 are RDR-211's own `DEFAULT_RENEW_INTERVAL_S` and
  `DEFAULT_MAX_RESENDS`). **Status**: Verified, cadence and cap confirmed exactly as
  designed. **Method**: Spike, live, with the re-announce interval
  temporarily shortened to 60s for the spike (T2
  `nexus_rdr/213-spike-2-reannounce-cadence-2026-09-17`). A row left
  genuinely unclaimed was announced 5 times total (1 initial + 4 re-sends)
  at the 60s cadence, then fell silent while remaining pending; a killed
  and reconnected MCP server re-announced the same still-available row once
  more with its count restarting at one, exactly as Technical Design
  states. Two caveats recorded in the T2 entry: a session told not to touch
  a reference still claims it once, since the channel push carries no body
  and reading the instruction requires a claim (the design already treats
  the session's own `tuple_release` as ordinary credit-freeing); and the
  MVV harness carries no conexus plugin, so "the drain hook renders it at
  the next prompt" could not be exercised live in this harness (unchanged
  RDR-205/211 territory, covered by those RDRs' own tests).

## Proposed Solution

### Approach

1. **The waiter never claims mail.** It parks `wait` over every subscription
   including mailboxes (`n=1`, no cursor) and pushes a reference for the
   oldest available row. That reference is also the wake for the same
   `UserPromptSubmit` hook that runs on every prompt: with the plugin's
   hooks loaded, the drain hook claims, acks and renders the body in that
   same prompt, before the session's turn, and the session acts on it,
   claiming nothing. Only a session without those hooks claims with
   `tuple_in` on its mailbox, then acks, nacks, releases or replies as
   today.
2. **Announcements are rate-limited by a cursor, not gated on the previous
   ack.** The waiter tracks a per-mailbox cursor past the last row it
   referenced, kept in the wait spec on every tick, the same shape a board
   topic already uses. Each new row is announced once as it passes the
   cursor, at most one reference per mailbox per wake; the waiter never
   reads a row back to check whether it is still current. It re-announces
   the last reference at most once every 150 seconds and at most five
   times, then stops. A restart starts the cursor empty, so the oldest row
   is announced once more.
3. **No proof, no probe, no flag table.** The waiter starts parking at
   lifespan start and pushes whenever a row appears. A session that cannot
   hear the channel loses nothing: its rows stay available and the drain hook
   renders them at the next prompt. `tuple_channel_probe` and
   `detect_channel_argv` are deleted, not kept as fallbacks.
4. **The status record and doctor row report what is observable.** `alive`,
   `last_wake`, `announced` (references sent for distinct rows, cumulative),
   `pending` (mailboxes whose last reference is under budget and not yet
   superseded, 0 or 1 per mailbox), `oldest_pending_age_s` (age of the
   oldest such reference). The `proof`, `unacked` and `released` facts go.
5. **Skills and docs say claim, not read.** The mailbox skill's push rule
   becomes: if the notification's body is rendered with that same prompt,
   act on it, claiming nothing, since the drain hook already claimed and
   acked it; otherwise `tuple_in` your mailbox, act, then ack or nack. A
   row you are not going to act on now is given back with `tuple_release`,
   never `tuple_nack`, because each announce is a fresh claim decision and
   the mailbox template dead-letters after three nacks. The launch flag
   remains the only opt-in and both forms stay documented.

### Technical Design

**Delivery.** `tick()` builds one spec per subscription: boards with their
cursor as today, and mailboxes the same shape, `n=1` since a per-mailbox
cursor past the last row referenced. Every subscription is in the spec on
every tick; nothing removes a mailbox from it. On wake, for each mailbox
result the waiter sends a reference for the returned row, advances the
cursor to it, and records it as the last reference (tuple id, `sent_at`,
`count=1`) -- unless the row is dead-lettered, in which case the cursor
still advances past it but no reference is sent, since the drain hook
already surfaces a dead row once on its own. On a tick with no new row for
a mailbox whose last reference is still within budget (`count < 5` and
`now - sent_at >= 150 s`), the waiter re-sends that same reference and
increments the count. It never reads a row back to check whether it is
still the head, whether it was claimed, or whether it dead-lettered after
the fact: the cursor's advance and the last-reference budget are the only
state kept, in memory; a restart starts the cursor empty, so the oldest row
is announced once more.

**Notification content**, unchanged in shape from RDR-211's reference:
subspace, tuple id, and one line telling the session how to get the body:
if it is rendered with this same prompt, the mailbox hook already claimed
and acked it and the session acts on it, claiming nothing; otherwise the
session claims it with `tuple_in` on that subspace and then acts. No body,
no `from`, no `kind`. The exact template (verified live, T2
`nexus_rdr/213-mvv-2026-09-17`):

```text
nexus mailbox message: subspace {subspace}, tuple {tuple_id}. If its body is rendered with this message, the mailbox hook already claimed and acked it: act on it, claim nothing. If not, claim it yourself with tuple_in("{subspace}", {"to": "{to_address}"}), then act: tuple_ack (with a reply for a request), tuple_nack, or tuple_release with the claim id. The waiter holds no claim.
```

**Back pressure.** A mailbox with two available rows announces the oldest
first; the cursor moves past it, so the next wake (immediate, since the
spec now matches the second row) returns and announces the second. Nothing
waits for the first row's ack. The bound is the rate limit itself: at most
one new-row reference per mailbox per wake, plus the 150 s / five-time
budget for re-sending the last one.

**Empty spec.** With every subscription, mailbox or board, in the spec on
every tick, the spec list is never empty; the 7.51.1 sleep branch is dead
code and is removed, with a test that proves a mailbox-only session parks
on `wait` every tick.

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
// Illustrative; the cursor and its re-send budget
_cursor: dict[subspace, (created_at, id)]          // position past the last reference sent
_last_ref: dict[subspace, (tuple_id, sent_at, count)]

build_specs: every mailbox, every tick, n=1, since=_cursor.get(subspace)
on a wake returning row R for mailbox m:
  if R is dead-lettered: _cursor[m] = (R.created_at, R.id); continue  // skip, no reference
  send(ref(m, R.id)); _cursor[m] = (R.created_at, R.id); _last_ref[m] = (R.id, now, 1)
on a tick with no wake for m, _last_ref[m] set, count < 5, now - sent_at >= 150:
  re-send the same reference; count += 1
```

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| Mailbox announce cursor | `_deliver_board_post` and the board cursor in `channel.py` | Reuse: the board path is already notify-only with a cursor; mailboxes use the identical cursor shape, not a separate per-row table |
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
  full by the drain hook at the next prompt if nothing claimed it in
  between. With the plugin's hooks loaded this is rare: the same
  notification usually wakes the drain hook and it wins the race, so the
  case is the normal one only for a session without those hooks. That is a
  duplicate pointer, never a duplicate body.
- Negative: a hookless session's two calls change shape: `tuple_in` (which
  returns the body with the claim) replaces `tuple_rd`, then ack, nack or
  release as today; a session with the plugin's hooks calls neither, since
  the drain hook claims, acks and renders the body for it. The skill text
  changes and every installed plugin re-learns it at the next release.

### Risks and Mitigations

- **Risk**: a session ignores a reference; only the mailbox's latest
  reference gets re-announced, so an earlier row that already got its one
  reference is not pushed again.
  **Mitigation**: the drain hook claims at the next prompt as before, for
  every available row regardless of announce history; the doctor row
  reports `oldest_pending_age_s` for the mailbox's current reference.
- **Risk**: the re-announce cadence wakes an idle session five times for one
  ignored message.
  **Mitigation**: the same bound RDR-211 chose for re-sends; the second
  critical assumption measures it.
- **Risk**: a session nacks a re-announced row it is only deferring; three
  nacks across the five announces dead-letter the message, a path RDR-211's
  single held claim never exposed the session to.
  **Mitigation**: Approach item 5's rule (release, never nack, when deferring)
  in the skill text; the Test Plan scenario below.
- **Risk**: the drain hook and a session's `tuple_in` race for the same row.
  **Mitigation**: the hook fires from the same notification and normally
  wins the race, at the wake, before the session's own turn starts; `in` is
  atomic regardless, so the rare loser gets nothing and does nothing, as
  the drain hook already handles today.

### Failure Modes

- Channel not live (no flag, or Claude Code drops notifications): nothing is
  pushed, the doctor row shows `announced` not advancing while rows are
  pending, the drain hook renders at the next prompt. No claim is held.
- Waiter dead (bare 404, engine without `wait`): as 7.51.1.
- Session claims and then crashes before ack: the lease lapses and the row is
  available again; the drain hook or the next announce picks it up. One
  attempt is spent, as for any claimant.
- Upgrade from 7.51.1 with a waiter claim outstanding: nothing adopts it; the
  lease lapses within 300 s and the row is available again.

## Implementation Plan

### Prerequisites

- [x] Both Critical Assumptions verified by the spike named there (T2
  `nexus_rdr/213-spike-1-session-claims-2026-09-17`,
  `213-spike-2-reannounce-cadence-2026-09-17`).
- [x] 7.51.1 shipped 2026-09-17 (tag `v7.51.1`), so the MVV compares against a
  live waiter, not the dead one.

### Minimum Viable Validation

Two real Claude Code sessions on a throwaway engine, the RDR-211 harness
(`scratchpad rdr211-mvv`): (1) mail to an idle session with the plugin's
hooks and the channel: reference arrives, the drain hook claims, acks and
renders the body at the wake, before the session's turn; the session acts
on it, claiming nothing. Control, same mail to a session with the channel
but without the plugin's hooks: reference arrives, the session claims with
`tuple_in`, reads, acks. A second message sent right after the first
produces its own reference one wake apart, not gated on the first's ack.
(2) Mail to a session
launched without the flag: no notification; the drain hook renders it at the
next prompt. (3) Kill the MCP server between announce and claim, restart it:
the row is announced once more and claimed. (4) Leave one message unclaimed
for ten minutes: at most five announcements, then silence, doctor row shows
`pending 1` and its age. Counts recorded in this section.

### Phase 1: Code Implementation

#### Step 1: Mailbox announce path

Replace `_maybe_claim_mail` and `_renew_or_release` with the announce table
and cadence; mailboxes enter the wait spec on every tick. Delete the proof
gate, the probe and the flag table, including their call sites in
`src/nexus/mcp/core.py` (the lifespan constructs the waiter with the argv
result and defines the probe tool). Tests before code: mailbox-only session
parks on its mailbox; oldest row announced once; re-announce cadence and cap;
absent row dropped; restart announces once; 404 stops, other errors retry.

#### Step 2: Tools, status record and doctor row

Delete `tuple_channel_probe` (`src/nexus/mcp/core.py`) and the credit hooks;
write the new status facts; rewrite `tuples.channel_delivery`. Tool count
(`docs/mcp-servers.md`, the core.py docstring, `tests/test_mcp_package.py`)
and deletion census tests updated.

#### Step 3: Skills and docs

Mailbox and peer-messaging skills: claim with `tuple_in` on a reference;
release, never nack, a row you are deferring.
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
  **Verify**: one reference pushed within one wait tick; with the plugin's
  hooks loaded, the drain hook claims, acks and renders the body at the
  wake and the session acts on it, claiming nothing; without those hooks,
  the session's `tuple_in` claims it; no waiter claim ever recorded either
  way.
- **Scenario**: two messages arrive together. **Verify**: two references,
  one wake apart, in arrival order; neither waits for the other's ack.
- **Scenario**: the session ignores a reference. **Verify**: re-announced at
  150 s intervals, five times, then silence; the drain hook renders it at the
  next prompt.
- **Scenario**: the session claims a re-announced row and gives it back with
  `tuple_release` three times. **Verify**: attempts unchanged, the row still
  available, never dead-lettered; the same three hand-backs with `tuple_nack`
  would dead-letter it, which is why the skill says release.
- **Scenario**: notification dropped by Claude Code (fake sender returns
  false). **Verify**: no claim exists; the row is announced again at the next
  cadence point.
- **Scenario**: session launched without the flag. **Verify** (fake-store
  unit test): nothing pushed; doctor row informational. **Verify** (MVV,
  host-only, Scenario 2 of the MVV above): the drain hook renders it at the
  next prompt -- the fake-store harness has no plugin hooks, so only the MVV
  can exercise that half.
- **Scenario**: waiter restart with one pending row. **Verify**: announced
  once more, count restarts at one.
- **Scenario**: engine without `wait`. **Verify**: waiter stops, doctor row
  says so.
- **Scenario**: board post and mailbox message in one wake. **Verify**: both
  references pushed, board cursor advances, mailbox row stays available.

## Validation

### Testing Strategy

1. **Scenario**: the Test Plan above as unit tests against the fake store,
   except the no-flag scenario's drain-hook-renders-at-next-prompt half,
   which the fake-store harness carries no plugin hooks to exercise and
   which Testing Strategy 2's MVV covers instead.
   **Expected**: every scenario green; the deletion census names every
   removed symbol.
2. **Scenario**: the MVV above on real sessions.
   **Expected**: counts recorded; assumption 1 and 2 verified.

### Performance Expectations

No new load: one `wait` per tick, every mailbox in the spec every time; no
row is ever read back to check its state. Announcements are bounded by the
per-mailbox, per-wake rate limit for new rows and the 150 s / five-time
budget for a re-send. Measured, not estimated, in the MVV.

## Finalization Gate

### Contradiction Check

No contradictions found between research findings, design principles, and
proposed solution. The one tension the research surfaced is stated in Trade-offs
rather than hidden: RDR-211's single-render property (a message the channel
delivered is never rendered again by the drain hook) is given up for a possible
duplicate reference, and the back-pressure ruling is kept by the announce
cadence rather than by a claim. The spikes measured the design as written: five
messages claimed by the session with no waiter claim, and an ignored row
announced five times then left to the floor.

### Assumption Verification

Both Critical Assumptions are verified by live spikes against a throwaway
engine with real Claude Code sessions (T2
`nexus_rdr/213-spike-1-session-claims-2026-09-17` and
`nexus_rdr/213-spike-2-reannounce-cadence-2026-09-17`). Nothing remains
unverified. Two caveats from the spikes are recorded there, not here: a session
told to ignore a reference still claims once, because the reference carries no
body to read the instruction from, and releases it; and the spike harness has
no plugin hooks, so the drain hook's rendering at the next prompt was not
exercised there (it is unchanged by this RDR and covered by RDR-205 and
RDR-211's own tests).

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

## Revision History

- 2026-09-17: created (draft) from Sam's request after bead nexus-tk2cz;
  amends RDR-211's Delivery design.
- 2026-09-17: Sam confirmed the design as his original intent for channel
  delivery (T2 `nexus_rdr/213-decision-notify-then-claim-2026-09-17`); the
  Provenance paragraph records it.
- 2026-09-17: prototype implemented on branch `rdr-213-spikes` (the middle
  of three commits on that branch, not the first -- unpushed, lifted by
  the Phase 1 beads) and both
  Critical Assumptions verified live against a
  throwaway engine (T2 `nexus_rdr/213-spike-1-session-claims-2026-09-17`,
  `nexus_rdr/213-spike-2-reannounce-cadence-2026-09-17`); checkboxes above
  updated accordingly.
- 2026-09-17: Gate round 1 — PASSED (0 Critical, 4 Significant, 0 ship-blocker(s)); commit `bb9e7193b`; critique `nexus_rdr/213-gate-critique-2026-09-17`.
- 2026-09-17: Gate round 1 fix (research `nexus_rdr/213-research-2`):
  Prerequisites ticked; the session's two calls stated once (claim returns the
  body) and Open Question 2 removed; core.py, docs/mcp-servers.md and
  tests/test_mcp_package.py named in Phase 1; release-never-nack on a deferred
  re-announce in Approach 5, Risks, the Test Plan and Step 3; the cadence
  constants and the upgrade lapse stated.
- 2026-09-17: Fix check `nexus_rdr/213-fix-check-6945efe4a` (two of three
  raised one clause): the Risks mitigation no longer says the rule lives in
  the reference, which carries no body; the skill text is its only carrier.
- 2026-09-17: Gate round 2 — PASSED (0 Critical, 0 Significant, 0 ship-blocker(s)); commit `7d3d14fe9`; critique `nexus_rdr/213-gate-critique-2026-09-17b`.
- 2026-09-17: Accepted by Sam (gate round 2 PASSED on 7d3d14fe9, fix check nexus_rdr/213-fix-check-7d3d14fe9, no residuals).
- 2026-09-17: nexus-gomuo.2 (Phase 1 Step 3, skills and docs): the Test
  Plan's no-flag scenario and Testing Strategy 1 reconciled to state once
  that the drain-hook-render half is the MVV's territory, not a fake-store
  unit test; the prototype-commit clause above corrected to say the middle
  of three commits, not the branch's first; RDR-211's Technical Design
  (Delivery, Subscriptions) and Approach item 7 each gained a pointer note
  to this RDR.
- 2026-09-17: Amended from the live MVV (T2 `nexus_rdr/213-mvv-2026-09-17`)
  and Sam's decision (T2
  `nexus_rdr/213-decision-hook-delivers-on-channel-wake-2026-09-17`). Two
  facts changed throughout: with the plugin's hooks loaded, the channel
  notification wakes the same prompt's drain hook, which usually claims,
  acks and renders the body before the session's turn, and the session
  claims with `tuple_in` only when no plugin hooks ran (Approach 1 and 5;
  Technical Design Notification content; Trade-offs; Risks; Critical
  Assumption 1; Key Discoveries; the MVV and Test Plan scenario 1); and
  the waiter does not park on a mailbox that already holds an announced,
  pending row, checking it once per tick and at each re-announce point
  instead, which closes a busy-loop the MVV measured at over 160 wakes a
  second and ephemeral-port exhaustion (Approach 2; Technical Design
  Delivery, Back pressure, Empty spec, the pseudocode; Performance
  Expectations).
- 2026-09-17: Amended per Sam's decision (T2
  `nexus_rdr/213-decision-announcements-rate-limited-not-ack-gated-2026-09-17`)
  and the deep analysis it answers (T2 `nexus_rdr/213-waiter-deep-analysis-2026-09-17`
  (1/2), (2/2)). The waiter's back pressure changes from "announce only the
  oldest unclaimed row; the next row after the first is acked" to a
  per-mailbox cursor, the same shape a board topic already uses: each new
  row is announced once as it passes the cursor, at most one reference per
  mailbox per wake, with the last reference re-sent at most every 150 s and
  at most five times; the waiter never reads a row back. This also
  replaces the round-3 text (a mailbox leaves the wait while a row is
  pending, checked once per tick) with the cursor shape, which never
  leaves the wait at all. Approach 2 and 4; Technical Design Delivery,
  Back pressure, Empty spec, the pseudocode; the Existing Infrastructure
  Audit row; Risks; Performance Expectations; the MVV part 1 second-message
  expectation; Test Plan scenario 2.
