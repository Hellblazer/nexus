# RDR-213 Post-Mortem: Channel Delivery Without a Proof Gate

**Closed** 2026-09-18 · **Accepted** 2026-09-17 · **Epic** nexus-gomuo (8 beads) · **Shipped** engine-service-v0.1.128 (2026-09-18), client 7.52.0 (2026-09-18); the boards half landed on develop 2026-09-18 for the next engine cut

## What the RDR set out to do

RDR-211 delivered a mailbox message to a session by claiming it in the
session's own MCP server and pushing a notification, and it gated that
claim on proof that the session could hear the channel, because a claim
held for a deaf session strands the message for its lease. The proof
could not be given: the handshake carries no channel marker, so the gate
was a heuristic and its failure was silent. This RDR removed the claim
and the gate together. The waiter announces a reference and never claims.
With the plugin's hooks loaded, the announcement wakes the same prompt's
drain hook, which claims, acks and renders the body before the session's
turn. Without them the session claims with `tuple_in`. A lost
announcement costs a wait until the next wake or prompt, never a lease.

## Implementation status

Implemented and shipped in both halves. The waiter, the deletion of the
proof gate, the probe tool and the flag table, the skills and docs, and
the doctor row shipped in conexus 7.52.0. The engine half, an announce
stamp on the mailbox row, shipped in engine-service-v0.1.128 and was
deployed before the client tag. The boards half, a per-subscriber stamp
in `nexus.tuple_deliveries`, landed on develop the same day for the next
engine cut. The Minimum Viable Validation ran three times on real Claude
Code sessions against a real engine.

## Implementation vs plan

### As planned

- Step 1 and 2: the waiter parks one `wait` over every subscription and
  announces a reference that names the subspace and tuple id only; the
  claim, the lease and renew loop, the outstanding-claim adoption, the
  parent-argv proof, the probe tool and the launch-flag table are deleted
  outright, and a census test bans their names.
- Step 3: the mailbox and peer-messaging skills say claim with
  `tuple_in`, act, then ack, nack, or release when only deferring.
- The re-announce cadence of 150 seconds and the cap of five.
- The doctor row reports the waiter's own status from a per-session
  record, with a stopped reason when the waiter knows why.

### Diverged

- **The design was not client-only.** The RDR said no engine change and
  no wire-ledger entry. The delivery position the waiter needed could not
  live in the client: a `(created_at, id)` cursor skips a row whose
  transaction started earlier but committed later, because `created_at`
  is stamped at transaction start. The position moved into the engine as
  an announce stamp written in the same statement that selects the row
  (bead nexus-vsipz), which needed a changeset, a wire field, an engine
  tag and a floor bump.
- **The hook delivers at the channel wake.** The RDR had the session claim
  its own mail after the notification. The first MVV run showed the
  notification fires the same `UserPromptSubmit` the drain hook runs on,
  so the hook claims, acks and renders the body before the session's
  turn, and the session claims nothing. The RDR's Approach item 1 and
  Critical Assumption 1 were rewritten from the measurement.
- **Back pressure went through three shapes.** The accepted design
  announced one message at a time and the next only after an ack. The MVV
  measured a busy loop of over 160 wakes a second from that rule, and
  three fix rounds of row tracking followed before a deep analysis showed
  the ack gate carried no weight: the drain hook renders ten rows at a
  prompt regardless. Sam ruled announcements rate-limited, not ack-gated,
  which became the cursor, which became the engine stamp.
- **Boards moved too.** The RDR kept the board cursor as the precedent it
  was borrowing from. The same race applied to boards with no drain-hook
  floor to recover a missed post, so boards got a per-subscriber stamp
  (bead nexus-q82tk), and the client keeps no cursor for any shape.

### Added beyond the plan

- The engine's `announce` field on a `wait` spec, its `subscriber` form
  for boards, and the result's echo of the subscriber it honoured, which
  is what lets a client stop loud on an engine that predates the stamp.
- A waiter stop reason per missing engine feature, named in the doctor
  row with the engine fix.
- A held-transaction test in `TupleAnnounceTest` that reproduces the
  late-commit race deterministically, on a mailbox and on a board, and a
  two-writer stop-rule test through the real delivery path.

### Planned but not implemented

Nothing. Every Approach item has a landed bead. The Finalization Gate was
not re-run after the engine stamp landed; the RDR's Assumption
Verification now names the tests that verify the one claim that
postdates acceptance.

## Drift classification

| Category | Count | Examples | Preventable? |
| --- | --- | --- | --- |
| **Unvalidated assumption** | 1 | client-only: a client position is enough to gate re-delivery | Yes, by a source search of `writeOut`, which stamps `created_at` at transaction start |
| **Framework API detail** | 0 | | |
| **Missing failure mode** | 1 | the busy loop under one-at-a-time back pressure | No: only a real session with real hooks produced it; the MVV is what caught it |
| **Missing Day 2 operation** | 1 | a local install converging to the engine floor after a client upgrade, the window an ignored `subscriber` opens | Yes, by reading `engine_version.py`'s own docstring before claiming a refusal at spawn |
| **Deferred critical constraint** | 0 | | |
| **Over-specified code** | 1 | the one-outstanding-message rule and the row tracking that served it | Yes, by asking what the rule bought once the hook renders ten at a prompt |
| **Under-specified architecture** | 1 | where the delivery position lives | Yes, by the same source search as the first row |
| **Scope underestimation** | 1 | one client module became two engine changesets, a wire field, an engine tag and a floor bump | Yes, once the position moved into the engine |
| **Internal contradiction** | 1 | the board precedent cited as having no comparable failure mode while carrying the cursor race | Yes, by a source search of the board arm before citing it |
| **Missing cross-cutting concern** | 0 | | |

### Pattern references

No category reached two instances. Across RDR-205, RDR-206, RDR-211 and
this RDR the recurring shape is a design fact that only a real client
launch settles; this RDR adds a second shape, a design fact that only the
engine's own SQL settles. The `created_at` race was in `writeOut` the
whole time and three fix rounds did not read it. A source search of the
store's write path before choosing a client-side position would have
found it on day one.

## What to check first next time

- Before a client keeps a position over engine rows, read how the engine
  stamps the ordering column. A transaction-start timestamp is not a
  commit order, and a cursor over it can skip a live row forever.
- Before a delivery rule is defended, ask what it buys once the floor
  runs. The one-at-a-time rule cost three fix rounds and bought nothing
  the drain hook did not already provide.
- A precedent cited for safety is a precedent to read. The board arm was
  named as having no failure mode while carrying the race this RDR would
  spend its engine half on.
- A local install converges to the engine floor; it does not refuse the
  engine at spawn. A client feature that depends on a new engine field
  needs a runtime proof of that field, not the floor.
- The site pages are a docs surface, again. Both channel pages describe
  the released client and change at the client release, and nothing
  mechanical tracks that.
