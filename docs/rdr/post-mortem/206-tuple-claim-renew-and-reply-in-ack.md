# RDR-206 Post-Mortem: Tuple Claim Renewal and Reply-in-Ack

**Closed** 2026-09-16 · **Accepted** 2026-09-11 · **Epic** nexus-h61dl (22 beads) · **Shipped** engine-service-v0.1.117 (2026-09-13), client 7.44.0 (2026-09-13)

## What the RDR set out to do

RDR-205 shipped the tuple space with two limits it named and accepted for
v1. A claim could not be extended, so long work lost its message when the
lease lapsed and the sweep handed it to someone else. A reply and its ack
were two calls, so a crash between them either duplicated the reply or
redid the work. This RDR added `renew` and an optional reply object on
`ack`, written in the same transaction that consumes the request, and
gave `ack`, `nack` and `renew` a compare-and-swap on the claim row so a
stale release fails `ClaimNotFound` instead of writing over a row someone
else now holds.

## Implementation status

Implemented and shipped in both halves. Engine in v0.1.117, deployed
before the client tag on the all-additive ledger. Client, MCP tools, CLI
flags, mailbox skill rules and docs in 7.44.0.

## Implementation vs plan

### As planned

- Phase 1, engine: compare-and-swap at all three sites, `out` and `ack`
  factored onto a caller context and composed as `ackWithReply`, `renew`
  reusing the live-claim predicate and the shared lease clamp, sweep and
  census proven unaffected, engine-direct MVV (beads .2 to .5, .14).
- Phase 2, client: `HttpTupleStore.renew` and `ack(reply=)`, `tuple_renew`
  and the `tuple_ack` reply argument, `nx tuple renew` and the `--reply-*`
  flags, the two mailbox-skill rules, docs across every surface, and the
  scenario journey with one reply under a forced crash (.8 to .15).
- Phase 3: engine cut and paired client release (.18, .19).

### Diverged

- **Phase 1 was re-ordered before it started.** The accepted order was
  cyclic; the first post-accept amendment re-derived it in dependency
  order, compare-and-swap first. Nothing was dropped in the reorder; the
  Phase 1 critic diffed the reordering commit scenario by scenario.
- **The reply validation moved outside the transaction.** The Technical
  Design pseudocode validated the reply after `consumeClaim` inside the
  transaction. The shipped code validates before the transaction opens, so
  a refused reply never starts the ack. The difference is observable on
  one input, a stale claim with an invalid reply, and the document moved to
  match the code, with a test pinning the observable case.
- **The lease ceiling moved into SQL.** `renew` first computed its ceiling
  in Java from an unlocked read, which an `out` refire could race so that a
  lease outlived its tuple and the purge could delete the row under a live
  holder. Found by the whole-phase critic, not by the per-step reviews. The
  ceiling is now applied in the UPDATE against the live row.
- **The client release waited eleven hours on another epic.** Advancing the
  plugin pin would have made the mailbox drain hook of nexus-6konb live
  before that epic's arming phase existed. The engine deployed on its own
  cadence; the client tag waited for nexus-6konb's Phase 3.

### The question the close bead asked

Gate round 2's Significant 1 said a reply target must be a nonce-keyed
template or the no-collision guarantee does not hold, and left open whether
that needed an engine guard or only a documented limitation. It needed the
guard, and it got one: `ackWithReply` refuses a reply whose target
template's id derives from keys alone, before the transaction opens, with a
test against the real ledger template that is the concrete collision case.
The documented limitation alone would have left a second reply silently
overwriting the first. Carry this forward: when two tuple operations are
composed, the second one's identity rule is a runtime check, not a note.

## Drift classification

| Category | Count | Examples | Preventable? |
| --- | --- | --- | --- |
| Internal contradiction | 1 | cyclic Phase 1 order | Yes, planner reads step dependencies |
| Over-specified code | 1 | pseudocode pinned an order the code did not need | No |
| Missing failure mode | 1 | renew ceiling raced by an `out` refire | Yes, spike |
| Missing cross-cutting concern | 1 | plugin pin coupling to another epic's hook | No |

## What to check first next time

Per-step review reads each bead's commit against the code. Two of this
RDR's real findings, the refire race and the missing revision-history
entry, were reachable only from the whole-phase view. Keep the whole-phase
critic pass at every phase close; it is not a duplicate of the per-step
reviews.
