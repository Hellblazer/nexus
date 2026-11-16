---
title: "Aura Relevance and One Responder: Load Conditions, an Owner Computed from the Record, and Local Actions for Everyone Else"
id: RDR-252
type: Architecture
status: draft
priority: medium
author: Hal Hildebrand
reviewed-by: pending
created: 2026-11-16
accepted_date:
related_issues: []
related: [RDR-249, RDR-211, RDR-220, RDR-213, RDR-245, RDR-242]
---

# RDR-252: Aura Relevance and One Responder

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.
> Prose: see REGISTER.md beside this template. Write for a smart reader who
> may not know the jargon; define terms on first use; simplified, never
> simplistic.

Terms. A **session** is one running Claude Code instance working in this
repository; about a dozen run at once. An **aura** (RDR-249) is a file that
says "when this tuple arrives on this board subspace, run this read-only
procedure and show its result in this session". The **board** is a set of
tuple-space subspaces any session can read; `board/ci/nexus-develop--done`
carries one post per finished develop CI run (RDR-220). The **push train** is
the agreed order in which sessions push to `develop`; halcyon, one of the
sessions, conducts it. A **red** is a develop CI run that failed.

Origin. Sam, 2026-11-16, on turning `ci_develop_red` on for every session:
"why would everyone try to resolve the red if only one or a subset are the
folk who should respond? ... let's think about what models we want,
strategically, rather than simply tactically responding to this one signal".
The models below were checked against the World of Warcraft add-on ecosystem
that RDR-249 borrowed its aura model from (T3 `agent-orchestration`, "WeakAuras
and raid coordination models: who an alert is for, who responds", 2026-11-16).

## Problem Statement

### Enumerated gaps to close

#### Gap 1: Every enabled session evaluates every event, relevant to it or not

RDR-249 took WeakAuras' trigger, conditions, actions and display, and left out
its **Load** conditions. In WeakAuras, Load decides whether an aura is
considered at all for this player (class, spec, role, zone, group). An
unloaded aura "does not exist to WeakAuras" and its triggers are never checked.
Here, an enabled aura runs its procedure in every session for every matching
tuple. A fact like "this session never pushes" or "this session pushed nothing
in the failing range" cannot keep a session out.

#### Gap 2: Nothing says who responds, so everyone may, or no one does

RDR-249 answered "one block per session; no lock tuple" (Sam, 2026-11-13).
That settled the display and left action open. When `ci_develop_red` fires in
every session, each one sees the same red, and whether one, several or none of
them diagnoses it depends on what each happens to be doing. In a raid, the
boss warning goes to everyone, and only the player the assignment note names
acts on it. Nothing here names that player.

#### Gap 3: The record does not say which session pushed a commit

The CI post comes from GitHub's webhook adapter. It carries the commit, the
run and the conclusion, and nothing that names a session: every session pushes
as the same GitHub account. `scripts/git-push-develop.sh` knows the pushing
session (its push-lock claimant, `<session>@<host>#<pid>`), but the lock tuple
lasts only while the push is in flight and is then released. After the push,
nothing durable maps a commit to the session that pushed it, so no reader can
work out an owner.

#### Gap 4: The displayed state follows arrival order, not commit order

A red can arrive after a newer commit has gone green. On 2026-11-16 the red
for 3b580d898 posted at 13:58Z, after the newer 22f80cfc7 went green at
13:55Z, so the block went up while develop's tip was green. The aura's clear
rule ("any green clears") has the mirror defect: a late green for an old commit
clears a block that a newer red put up. The block tracks the last post, not
develop's state.

## Relationship to Prior RDRs

- **RDR-249** (declarative auras, closed): this RDR adds a load stage and an
  owner computation to its trigger, condition, action and display model. Its
  answer "one block per session" stands for display; this RDR adds who acts.
- **RDR-211** (board, queue and lock templates, closed): supplies the claim
  queue, used here only for work no record assigns (decision 4).
- **RDR-220** (CI status on the board, closed): the CI post is the trigger
  tuple; this RDR does not change the GitHub adapter.
- **RDR-242** (sandboxed Lua runtime): aura procedures are read-only, which
  this design depends on. Every session computes the owner by reading; no
  aura writes.
- **RDR-245** (structured asks on the threads board): where an escalation to
  Sam lands.

## Context

### Background

A raid in World of Warcraft has the same problem: 20 players get the same
broadcast, and a mechanic needs one or two of them to act. The research
(T3 entry above) found these working models, with sources:

- **Load conditions** gate evaluation before any trigger runs. The authors'
  stated reason is cost; the effect is relevance.
- **Assignment notes name owners.** Add-ons match the player's own name in a
  raid note. Parsing free text is brittle: one stray space and the assignment
  silently matches no one.
- **KickSync** (interrupt rotation) uses no claims. Every client runs the same
  calculation on the same shared state and agrees on whose turn it is. It
  shows three states: "KICK NOW" to the player whose turn it is, "NEXT UP" to
  the player after, and nothing to everyone else.
- **Display for all, action for one.** Blizzard's boss warnings show the
  mechanic and "leave the response to players"; DBM has a warning type that
  fires only for players named in the note.
- **Side channels fail when they matter.** Add-on messages are blocked inside
  raid encounters, so coordination that depends on them breaks in combat.
- **Platform and user split.** In Midnight (12.0) Blizzard keeps the
  authoritative signal and its rendering in the game and leaves selection and
  reaction to user scripts.
- **Not found:** any backup-owner or escalation protocol. The raid leader
  calls it live.

### Technical Environment

- `src/nexus/auras/aura.py` parses aura files (`[trigger]`, `[args]`,
  `[when]`, `[clear]`, `[display]`); `src/nexus/procedures/ci_develop_red.aura.toml`
  is the one shipped aura.
- Aura procedures run in the read-only Lua runtime (RDR-242); they can read
  tuples and T2, and cannot write.
- `scripts/git-push-develop.sh` claims `lock/ci-develop-push` with a claimant
  string naming the session, and prints `PUSH_OK n=<count> tip=<sha>`.
- The CI post on `board/ci/nexus-develop--done` carries `kind=sha`, the sha,
  `outcome` and the run (RDR-220).

## Research Findings

### Investigation

One research pass on 2026-11-16 (T3 entry named above), reading WeakAuras'
wiki, Warcraft Wiki's API pages (private auras, secret values), DBM's
special-warning guide, KickSync, Sniffa and Blizzard's Midnight post. Confidence
is high for Load, the private-aura API, KickSync and DBM; low for the
assignment tools read only through search summaries.

### Key Discoveries

1. Load is the missing layer, and it is cheap: deciding relevance once, from
   facts about the session, removes most evaluations.
2. Where every session can read the same record, a computed owner beats a
   claim. A claim adds a race and a lease to lose; a computation gives the
   same answer everywhere with no messages.
3. The owner must be a typed field in the record, not text a reader parses.
4. Three computed states (owner, next up, everyone else) give a fallback
   without a hand-off protocol.
5. Escalation has no prior art to copy; it is ours to keep small.

### Critical Assumptions

- [ ] The pushing session can be recorded durably at push time without
  slowing the push. **Method**: Source Search of `git-push-develop.sh`, then a
  spike.
- [ ] A read-only procedure can find the push record for a sha in one or two
  tuple reads. **Method**: Spike.
- [ ] Commit order for develop can be read cheaply by a procedure (for
  example, from the push records' parent links, without a git call).
  **Method**: Source Search.

## Proposed Solution

### Approach

Four parts, in the order a red is handled:

1. **Load.** An aura file may carry a `[load]` table of facts about the
   session, checked once when the session's auras load (and again when a
   named fact changes). An aura whose load fails is not subscribed.
2. **A push record.** The push script writes one durable tuple per push.
3. **A computed owner and three states.** The aura's procedure reads the push
   records and computes, the same way in every session, who owns the event
   and who is next. The display differs by state.
4. **Local actions only.** Each state maps to what this session does to
   itself: act, stand by, or hold its own pushes. No aura writes shared state.

### Technical Design

**Load.** New optional table in the aura file:

```toml
[load]
pushes_develop = true      # this session has pushed to develop at least once
                           # (or is in the push train); a reader-only session
                           # never loads ci_develop_red
```

The fact names are a closed vocabulary listed in code, each with how it is
read. A first set: `pushes_develop`, `repo` (the session's repository), and
`role` (a role this session holds, such as `push-conductor`, from a role
tuple). An unknown fact refuses the file at `nx aura check`.

**The push record.** On `PUSH_OK`, `git-push-develop.sh` writes one tuple to
`board/push/nexus-develop`:

```
kind=push  tip=<sha>  base=<sha before the push>  shas=<the pushed shas>
session=<session id>  name=<session name>  at=<UTC>
```

It is a board post (read by anyone, never claimed). The session id is a typed
field, so no reader parses a claimant string.

**Develop's state from commit order.** The procedure judges a CI post against
the push records, not against arrival order. A red counts only if its sha is
the newest develop commit with a finished run. A late red for an older commit
shows as history, not as a block. A late green for an older commit does not
clear a block a newer red raised. This closes Gap 4.

**Owner, next up, everyone else.** For a red that counts:

- **Owner:** the session of the push record whose `shas` contain the first
  red commit after the last green one. When a red is inherited (a later push
  went red only because an earlier one did), the owner is the earlier pusher.
  Example: on 2026-11-16, 4554bd15f went red only because da1c285da had; the
  owner is da1c285da's pusher.
- **Next up:** the push conductor (the session holding the `push-conductor`
  role tuple). If the owner's session is gone (no live session id), next up
  becomes owner.
- **Everyone else:** every other loaded session.

**Displays and local actions.**

| State | Display | Local action |
|---|---|---|
| Owner | "Develop is red at <sha>, first red in your push. Diagnose it." | Diagnose; acknowledge on the board |
| Next up | "Develop is red at <sha>; <owner> owns it. You are next up." | Stand by; take it if the owner has not acknowledged by the deadline |
| Everyone else | "Develop is red at <sha>; <owner> owns it. Hold your pushes." | Hold its own pushes until green |

**Acknowledgement.** The owner acknowledges by writing one tuple
(`board/push/nexus-develop` `kind=ack sha=<sha> session=<id>`) with an ordinary
tool, not from the aura. Every session's next evaluation sees it. The
deadline (a default of 20 minutes) is computed from the CI post's time, so no
timer message is needed; the next event on the subspace, or the channel
waiter's periodic re-check, re-evaluates it.

**Escalation.** Owner, then next up, then an RDR-245 ask to Sam only if next up
is also gone. No other chain.

**Claims.** Only for work no record assigns, such as rerunning a flaky shard
that any session may do. That uses RDR-211's queue as is.

### Existing Infrastructure Audit

| Piece | Exists? | Use |
|---|---|---|
| Aura parse, trigger, display | Yes (RDR-249) | Adds `[load]` and state-dependent display text |
| Read-only procedure runtime | Yes (RDR-242) | Computes owner and state |
| Board posts | Yes (RDR-211, 220) | Push record and acknowledgement |
| Push script with session identity | Yes | Writes the push record |
| Role tuples | No | A minimal `role/<name>` tuple, claimed by its holder |

### Decision Rationale

Computation over claims, because every session can read the same record (the
KickSync evidence), and claims add a race. Local actions only, because RDR-242
keeps procedures read-only and the platform and user split in Midnight keeps
the authoritative record in the platform. Load first, because it is cheap and
removes most of the noise before any other part runs.

## Alternatives Considered

### Alternative 1: Claim queue for every red

A red becomes a task on RDR-211's queue, and the first session to claim it
owns it.

- **Pro:** uses a shipped template; no push record needed.
- **Con:** the fastest session wins, not the one with context; a lease can
  expire under a slow diagnosis; it races where a computation would not.
- **Rejected:** the record can name the owner, so a race buys nothing.

### Alternative 2: Convention only

A written rule: the pusher or halcyon diagnoses, everyone else holds pushes.

- **Pro:** no code.
- **Con:** every session still wakes on every red; the rule lives in prose
  and drifts; late reds still mislead (Gap 4).
- **Rejected** as the end state; acceptable as the interim if Sam turns the
  aura on before this lands.

### Alternative 3: Route every red to halcyon

- **Pro:** one fixed owner.
- **Con:** halcyon has the least context about a commit it did not write and
  becomes a bottleneck.
- **Rejected** as the default; kept as next up.

### Briefly Rejected

- Parsing the pusher out of commit messages or bead ids: brittle, the
  assignment-note failure mode.
- Mail between sessions to hand off ownership: a side channel, the failure
  mode WoW hits in combat.

## Trade-offs

### Consequences

- Most sessions see a one-line "hold your pushes" and do nothing else.
- The push script gains one board write per push.
- Aura files gain an optional `[load]` table; existing files are unchanged.

### Risks and Mitigations

- **Push record missing** (the write failed or an old push): the owner is
  unknown, so next up owns it. The display says why.
- **Two sessions think they own it** (a stale read): both act; the
  acknowledgement tuple shows the second one that the first already has it.
- **Load facts go stale** (a session starts pushing after load): `pushes_develop`
  is re-read when the session takes the push lock.

### Failure Modes

- The procedure fails or times out: the session falls back to RDR-249's
  plain display, which says the red exists and names no owner.

## Implementation Plan

### Prerequisites

- [ ] Sam accepts the model (computed owner, three states, local actions).
- [ ] The role-tuple shape agreed with halcyon (only `push-conductor` at
  first).

### Minimum Viable Validation

With `ci_develop_red` on in three test sessions (A pushed commit X, B is
`push-conductor`, C pushed nothing): a red on X shows "owner" in A, "next
up" in B and "hold your pushes" in C. A later green on an older commit does
not clear it. A late red on an older commit does not raise a block. With A's
session gone, B shows "owner".

### Phase 1: Code Implementation

#### Step 1: Push record

`git-push-develop.sh` writes the `kind=push` post on `PUSH_OK`, with a test
that the post carries the session id and the pushed shas.

#### Step 2: `[load]` in the aura file

Parse, check against the closed fact vocabulary, evaluate at load; `nx aura
check` refuses unknown facts; `nx aura list` shows each aura's load result for
this session.

#### Step 3: Owner and state in `ci_red_notice`

The procedure reads push records and acknowledgements, computes develop's
state by commit order, the owner, next up and this session's state, and
returns state-dependent display text.

#### Step 4: Role tuple and acknowledgement

A minimal `role/push-conductor` tuple; the acknowledgement post; the deadline
check on re-evaluation.

### Phase 2: Operational Activation

#### Activation Step 1: Turn it on

`nx config set auras.ci_develop_red=on` and a restart round, after the MVV
passes. Sam's go.

### Day 2 Operations

- `nx aura list` shows, per aura, loaded or not and why.
- The push records double as an audit of who pushed what, and acknowledgement
  times give the after-the-fact grading the WoW tools use (time from red to
  acknowledgement, to fix).

### New Dependencies

None.

## Test Plan

- Owner, next up and everyone-else states from a fixed set of push records.
- Inherited red: the owner is the first red's pusher.
- Late red and late green, in both orders.
- Owner session gone: next up owns.
- Push record missing: next up owns, display says so.
- `[load]` unknown fact refused; a session with `pushes_develop` false never
  subscribes.

## Validation

### Testing Strategy

Unit tests on the owner computation with recorded tuples; one live check in
three sessions (the MVV).

### Performance Expectations

Load removes the aura from sessions that never push. A fire costs about 50 ms
today (marginalia, 2026-11-16); the owner computation adds one or two tuple
reads.

## Finalization Gate

Not yet run.

## Open Questions for Sam

1. **Owner source.** From the record (the pusher, as designed), or from roles
   only (always the push conductor)?
2. **Escalation.** Should a red ever reach Sam without going through halcyon?
3. **Interim.** Turn `ci_develop_red` on now with a written convention, or
   keep it off until this lands?

## References

- RDR-249 § Background (what was taken from WeakAuras).
- T3 `agent-orchestration`: "WeakAuras and raid coordination models: who an
  alert is for, who responds" (2026-11-16), with its sources: the WeakAuras
  wiki (Load), Warcraft Wiki (private auras, secret values), DBM's
  special-warning guide, KickSync, Sniffa, Blizzard's Midnight post.
- marginalia's live check of `ci_develop_red`, 2026-11-16 (fire, clear, late
  red).

## Revision History

- 2026-11-16: created (draft), on Sam's request to model aura response
  strategically and validate it against the WeakAuras ecosystem.
