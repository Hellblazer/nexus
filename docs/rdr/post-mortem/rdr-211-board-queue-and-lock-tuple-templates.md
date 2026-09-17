# RDR-211 Post-Mortem: Broadcast Board, Work Queue, and Lock as Tuple-Space Templates

**Closed** 2026-09-17 · **Accepted** 2026-09-17 · **Epic** nexus-rplay (25 beads) · **Shipped** engine-service-v0.1.127 (2026-09-17), client 7.51.0 (2026-09-17)

## What the RDR set out to do

RDR-205 shipped the tuple space with two templates, the mailbox and the
dispatch ledger. Sessions still had no way to announce something to
everyone, no shared queue two workers could take from without taking the
same task, no lock one session could hold while others waited, no way to
hand a claim back without it counting as a failure, and no push: a
session learned about mail only when a watcher process pinged it or when
its next prompt drained the mailbox. This RDR added three templates
(`board/<topic>`, `queue/<name>`, `lock/<resource>`), a `release`
operation, a lock flag that refuses `ack` so a lock can only be released,
a multiplexed `wait` over several subspaces in one parked call, and
delivery of a tuple reference to the session over the Claude Code channel
from the session's own MCP server, with the watcher process deleted. The push
reaches only a session launched with the channel flag, a research preview;
every other session keeps the drain hook at its next prompt as the floor.

## Implementation status

Implemented and shipped in both halves. Engine in v0.1.127, deployed
before the client tag on the all-additive wire ledger. Client, MCP tools,
CLI verb, doctor rows, skills and docs in 7.51.0. Push delivery is live for
a session launched with the channel flag and inert, by design, for any
other; the drain hook is unchanged for both. The Phase 1 close gate
cross-walked all eight Approach items and all eighteen Test Plan scenarios
to landed beads and named tests; the Minimum Viable Validation ran against
a real engine with two real sessions.

## Implementation vs plan

### As planned

- Step 0: the channel assumption spiked on a real idle session before the
  waiter was written (four idle wakes, three mid-turn deliveries, a
  no-flag control).
- Step 1, engine: `release` with its claim-log transition and waiter
  signal; the lock flag read at `writeOut`, `claimOnce` and `renew` with
  the `consumeClaim` refusal; `waitAny` over up to 34 subspaces on one
  global slot; per-template `max_live_rows` and claim-log TTL; the
  park-slot report and its route.
- Step 2: the three templates as classpath resources, the registry
  refusing a lock template with `max_attempts`.
- Step 3, client: `HttpTupleStore.release`, `wait` and `park_stats`; the
  `tuple_release` tool and `nx tuple release`; the lifespan waiter with
  claim-at-delivery, back pressure of one outstanding message, five
  re-sends then release, restart adoption of a persisted claim; the three
  subscription tools with T1 persistence, the instance mailbox lease and
  the 32-topic cap; the doctor rows; the watcher, its arming and `nx tuple
  watch` deleted rather than kept as a fallback.
- Step 4: docs on every surface, the mailbox and peer-messaging skills.
- Phase 2: engine cut, deploy ahead of the client tag, paired client
  release.

### Diverged

- **The waiter's claim gate is parent-process evidence, not the
  handshake.** The design gated claiming on the handshake carrying the
  channel capability. The Step 0 spike measured that Claude Code's
  handshake and environment are byte-identical with and without the
  channel flag, so nothing in the handshake can prove the channel is
  there. Sam chose parent argv proof (the `claude` process's own command
  line) with a probe tool as the fallback, and the doctor row now reports
  which leg proved it.
- **A notification carries a reference, never the body.** The design had
  the notification carry the rendered message. The client review found
  the injection surface, and Sam ruled the notification carries subspace,
  tuple id, claim id and claimant only; the session reads the body on
  purpose with `tuple_rd`. The 4096-byte body cap no longer bounds a
  notification.
- **A subscription change takes effect at the next wait tick.** The design
  had the waiter cancel its parked call when the subscription set changed.
  The shipped waiter picks the change up when the current `wait` returns,
  at most 25 seconds later, which the MVV measured and the RDR records as
  a variance.
- **The per-launch development-channel dialog is setup.** The channel
  capability is a research preview behind a hidden flag that shows a
  confirmation dialog on every launch. The design did not anticipate the
  dialog; Sam accepted it and it is documented as one-time setup for a
  channel-capable session.
- **Doctor row semantics sharpened.** "Unacked" is a live 0-or-1 gauge
  under back pressure, "released" a cumulative count; the design named
  both without saying which was a gauge.
- **A pre-existing leak was fixed on the way.** The Step 1 review found
  that `rd` and `in` did not release their waiter registration on every
  exit path, a defect older than this RDR that the new `waitAny` made
  visible. Fixed in the same phase.
- **Lock `from` stays optional.** One sentence in the design said the lock
  template required a `from`; the template table wrote it optional. Sam
  kept it optional and the sentence was corrected.

### Added beyond the plan

- The waiter's persisted status record, adopted at restart, which the
  doctor row and the MVV read.
- The scenario-11 pin test (subscribe while parked, delivered at the next
  tick with one slot) added at the close gate when the cross-walk found
  the scenario proved only live.
- The `park_stats` doctor anchor's pin test, added in the release commit
  that pinned the engine it names.

### Planned but not implemented

Nothing. Every Approach item and Test Plan scenario has a landed bead or a
named test; the six residuals carried out of the close gate are recorded
in T2 `nexus_rdr/211-phase1-close-gate-2026-09-17` and are all
observability or coverage details, not scope.

## Drift classification

| Category | Count | Examples | Preventable? |
| --- | --- | --- | --- |
| **Unvalidated assumption** | 1 | handshake carries the channel capability | Yes, by the spike the RDR itself scheduled as Step 0, which is what caught it |
| **Framework API detail** | 0 | | |
| **Missing failure mode** | 1 | the rd/in waiter-registration leak | No: pre-existing, outside the design's text; found by review |
| **Missing Day 2 operation** | 1 | the per-launch development-channel dialog | No: research-preview behaviour, only visible on a real launch |
| **Deferred critical constraint** | 0 | | |
| **Over-specified code** | 1 | cancel the parked call on a subscription change | Yes, by leaving the mechanism to the implementation |
| **Under-specified architecture** | 1 | gauge versus counter in the doctor row | Yes, by naming the type of each fact |
| **Scope underestimation** | 0 | | |
| **Internal contradiction** | 1 | lock `from` required in one sentence, optional in the table | Yes, by a source search of the template table against the prose |
| **Missing cross-cutting concern** | 1 | notification content as an injection surface | Yes, by a security pass on anything that carries another session's text into a prompt |

### Pattern references

No category reached two instances. Across RDR-205, RDR-206 and this RDR,
the recurring shape is one design fact per RDR that only a real client
launch could settle (the edge stubbing `/version`, the handshake with no
channel marker, the launch dialog). The Step 0 spike pattern, a
measurement on the real client before the dependent bead is written, is
what turned this RDR's instance from a mid-implementation surprise into a
decision taken before the waiter existed.

## What to check first next time

- Anything a session receives from another session and renders into a
  prompt is an injection surface. Send a reference, read the body on
  purpose.
- A capability negotiated with a client is proven by observing the client,
  not by reading its handshake. Spike it on the real client before writing
  the code that depends on it.
- A public site page counts as a docs surface. `web/coordination.html`
  still described the deleted watcher after every docs bead closed; the
  docs audit covered `docs/` and `README.md` only.
- A gate run under tmux has a TTY and `nx init` asks its autostart
  question. Answer no; under Monitor there is no TTY and no question.
