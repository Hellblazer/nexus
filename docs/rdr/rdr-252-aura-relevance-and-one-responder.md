---
title: "Groups, Relevance and One Responder: Raid-Style Coordination for Sessions, with Auras That Load by Membership and Act by Assignment"
id: RDR-252
type: Architecture
status: draft
priority: medium
author: Hal Hildebrand
reviewed-by: pending
created: 2026-11-16
accepted_date:
related_issues: []
related: [RDR-249, RDR-211, RDR-220, RDR-213, RDR-245, RDR-242, JDR-002]
---

# RDR-252: Groups, Relevance and One Responder

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
sessions, conducts it. A **red** is a develop CI run that failed. A **group**
is this RDR's new term: a named set of sessions with a leader, a roster, typed
assignments and a lifetime, the counterpart of a raid in World of Warcraft.

Origin. Sam, 2026-11-16, on turning `ci_develop_red` on for every session:
"why would everyone try to resolve the red if only one or a subset are the
folk who should respond? ... let's think about what models we want,
strategically, rather than simply tactically responding to this one signal".
Then: "do we have the equivalent of a raid? ... while I usually set it up,
it's not fixed, we can do several sometimes. we can also be part of several
simultaneously? anyway, they're just group coord mechanics". The models below
were checked against the World of Warcraft add-on ecosystem that RDR-249
borrowed its aura model from (T3 `agent-orchestration`, "WeakAuras and raid
coordination models: who an alert is for, who responds", 2026-11-16; called
"the research" below).

## Problem Statement

### Enumerated gaps to close

#### Gap 1: There is no group, so coordination lives in prose

Sessions already work in groups that form and dissolve. The push train has a
leader (halcyon, set up by Sam on 2026-11-07) and rules in a T2 memo
(`nexus/push-train`). There are also push windows (three sessions pushing in
order), review pairs, a release crew (Sam and halcyon) and RDR co-authors. A
session is often in several at once. None of these exists in the system:
membership, leader and assignments live in messages and prose, so no tool and
no aura can ask "who is in this, who leads it, who owns this task". Some
raid mechanics already run without the name: the restart script's "ready
file written" exchange is a ready check, a CI post is a raid-wide warning,
and halcyon's queue line is an assignment note.

#### Gap 2: Every enabled session evaluates every event, relevant to it or not

RDR-249 took WeakAuras' trigger, conditions, actions and display, and left out
its **Load** conditions. In WeakAuras, Load decides whether an aura is
considered at all for this player (class, spec, role, zone, group). An
unloaded aura "does not exist to WeakAuras" and its triggers are never checked.
Here, an enabled aura runs its procedure in every session for every matching
tuple, and wakes each one with a channel notice.

#### Gap 3: Nothing says who responds, so everyone may, or no one does

RDR-249 answered "one block per session; no lock tuple" (Sam, 2026-11-13).
That settled the display and left action open. In a raid the boss warning goes
to everyone, and only the player the assignment names acts on it. Nothing here
names that player.

#### Gap 4: The record does not say which session pushed a commit

The CI post comes from GitHub's webhook adapter. It carries the commit, the
run and the conclusion, and nothing that names a session: every session pushes
as the same GitHub account. `scripts/git-push-develop.sh` knows the pushing
session (its push-lock claimant, `<session>@<host>#<pid>`), but the lock tuple
lasts only while the push is in flight. After the push, nothing durable maps a
commit to the session that pushed it.

#### Gap 5: The displayed state follows arrival order, not commit order

A red can arrive after a newer commit has gone green. On 2026-11-16 the red
for 3b580d898 posted at 13:58Z, after the newer 22f80cfc7 went green at
13:55Z, so the block went up while develop's tip was green (marginalia). The
aura's clear rule ("any green clears") has the mirror defect: a late green for
an old commit clears a block that a newer red put up.

## Relationship to Prior RDRs

- **RDR-249** (declarative auras, closed): this RDR adds a load stage keyed on
  group membership and an owner read from the group's assignments. Its answer
  "one block per session" stands for display.
- **RDR-211** (board, queue and lock templates, closed): groups are built from
  its board and claim shapes; its queue serves work no assignment names.
- **RDR-220** (CI status on the board, closed): the CI post is the trigger; its
  GitHub adapter is unchanged.
- **RDR-242** (sandboxed Lua runtime): aura procedures stay read-only.
- **RDR-245** (structured asks on the threads board): where an escalation to
  Sam lands.

## Context

### Background

A raid in World of Warcraft has the same problem: 20 players get the same
broadcast, and a mechanic needs one or two of them to act. What the research
found, with how sure it is:

- **Load conditions** gate evaluation before any trigger runs (high: the
  WeakAuras wiki). The authors' stated reason is cost; the effect is relevance.
- **Assignment notes name owners**, and add-ons match the player's own name in
  the note (high for Sniffa). Parsing free text is brittle: one stray space and
  the assignment silently matches no one.
- **Who may publish assignments** is controlled: Angry Assignments accepts them
  from the whole raid, a guild officer, or a listed set of players; edits are
  sent only at a sync point; a changed assignment re-appears for players who
  had hidden it (low: search summaries only).
- **KickSync** uses no claims. Every client runs the same calculation on the
  same shared state and agrees on whose turn it is, with three states: "KICK
  NOW" to the player whose turn it is, "NEXT UP" to the next, nothing to
  everyone else (high).
- **Private auras**: the game itself renders a debuff only for the affected
  player; add-ons place an anchor and never see the data (high for the API;
  the "never see" reading is inferred from its shape). Who an alert is for is
  decided once, by the platform, not by every client.
- **Typed warnings**: DBM has five warning types (generic, area, "do this or
  die", run away, and one that fires only for players named in a note), each
  with its own flash, sound and repeat settings (high).
- **Noise**: players asked how to silence DBM's `/say` countdowns (low).
- **Side channels fail when they matter**: add-on messages are blocked inside
  raid encounters (high, from Blizzard's secret-values API).
- **Platform and user split**: in Midnight (12.0) the game keeps the
  authoritative signal and its rendering and leaves selection and reaction to
  user scripts; scripts may display some values but not decide on them (high).
  WeakAuras also reported losing combat-state support under those changes
  (low): the platform can take a capability away.
- **Not found**: any backup-owner or escalation protocol, and any source for a
  resolver that maps a role to an owner. The raid leader calls it live.

### Technical Environment

- `src/nexus/auras/aura.py` parses aura files (`[trigger]`, `[args]`,
  `[when]`, `[clear]`, `[display]`); `src/nexus/procedures/ci_develop_red.aura.toml`
  is the one shipped aura. Auras and config are read once, when the session's
  MCP server starts.
- Aura procedures run in the read-only Lua runtime (RDR-242).
- `scripts/git-push-develop.sh` claims `lock/ci-develop-push` with a claimant
  naming the session, and prints `PUSH_OK n=<count> tip=<sha>`.
- The CI post on `board/ci/nexus-develop--done` carries `kind=sha`, the sha,
  `outcome` and the run (RDR-220). Posts are unattested: the engine records no
  session for the GitHub adapter's writes.

## Research Findings

### Investigation

One research pass on 2026-11-16 (the T3 entry named above), reading WeakAuras'
wiki, Warcraft Wiki's API pages (private auras, secret values), DBM's
special-warning guide, KickSync, Sniffa and Blizzard's Midnight post.

### Key Discoveries

1. Load is the missing layer, and cheap: decide relevance once, from facts
   about the session, and most evaluations never happen.
2. Where every reader shares the same record, an owner computed from it beats
   a claim. Better still, the platform computes it once and stamps it, as
   private auras do, so readers cannot disagree.
3. The owner is a typed field, never text a reader parses.
4. Who may write an assignment matters as much as the assignment.
5. Three states (owner, next up, everyone else) give a fallback without a
   hand-off protocol.
6. Escalation and role-to-owner resolution have no prior art; they are ours,
   unvalidated, and kept small.

### Critical Assumptions

- [x] The pushing session can be recorded durably at push time without
  slowing the push. **Status**: Verified. Source (`nexus_rdr/252-research-1`
  F4): the script already resolves the session id and writes tuples for its
  push lock. Spike (`nexus_rdr/252-research-2`): ten records written through
  `nx tuple out` with an ordinary tenant token were all accepted by the
  existing `board/ci/<topic>` template, at a median 466 ms a call, nearly all
  CLI start-up, against a push that takes seconds. The write passes the
  template's key, `--key topic=nexus-develop--push`.
- [x] One writer can join a CI post with the push records and stamp the owner
  within seconds of the post. **Status**: met by design. The stamper runs in
  the engine on the write of the CI post (Sam, 2026-11-16), so the join is a
  read of the engine's own rows inside that write, not a second process.
- [x] Group membership can be read at MCP server start in one tuple read.
  **Status**: met by design. Memberships live under the member's own subspace,
  `member/<session id>` (Technical Design, Groups), so "my groups" is one read
  of one subspace. Under `group/<name>` it would have been one read per group
  (`nexus_rdr/252-research-1` F7).

## Proposed Solution

### Approach

1. **Groups.** A group is a board entry with a name, a leader, a roster, typed
   assignments and a lifetime. Sam or a leader forms and disbands one; a
   session can be in several.
2. **A push record.** The push script writes one tuple per push naming the
   pushing session.
3. **The owner, stamped once.** One writer joins each red with the push
   records and the group's assignments and posts the result, owner and next up
   included. Sessions only read it.
4. **Load by membership.** An aura file may say which group and role it is
   for; a session that is not in it never loads the aura.
5. **Local actions only.** Each state maps to what this session does to
   itself. No aura writes shared state.

### Technical Design

**Groups.** One board entry per group at `group/<name>`:

```
kind=group  name=push-train  leader=<session id>  purpose="develop push order"
formed_by=<session id or "sam">  formed_at=<UTC>  ends=<UTC or "open">
```

Membership is one tuple per group the session belongs to, under the
session's own subspace `member/<session id>`: `kind=member group=<name>
roles=[...]`, written by the member when it joins and removed when it leaves
or its session ends. Keying it by session makes "which groups am I in" one
read at server start; a group's roster is a read of `member/*` filtered by
`group`, done only by the stamper and a group listing, never at load. A
session id fits the engine's address-segment pattern
(`[A-Za-z0-9][A-Za-z0-9._-]*`). Assignments are typed tuples under `group/<name>`, `kind=assign
task=<task> owner=<session id> next=<session id>`, written by the leader.
Two new engine templates carry these: `group/<name>` (the group entry and its
assignments) and `member/<session>` (memberships). A
session can hold membership in any number of groups. A disbanded group's
entry stays as history with `ended_at`.

First groups, made explicit from what runs today: `push-train` (leader
halcyon), and ad hoc push windows, review pairs and the release crew as
needed. The restart script's ready check becomes a group operation: "every
member of group X, report ready".

**Trust and attestation.** Only Sam or the group's leader may write the group
entry and its assignments; only a session may write its own membership. The
push record is written by the push script under the pushing session's
identity. How strongly the engine enforces that is a choice between levels.
JDR-002 sets the starting point: the tenant (from the bearer token) is the
security boundary, `from`, claimant and directory names are claims, and the
engine's `sender_session` stamp (engine-service-v0.1.163) is "a label, never a
refusal". Any holder of the tenant's data token can mint a session token for
any session id, so the stamp guards against a confused model, not a hostile
one. JDR-002 says to revisit this "in an RDR" when trust needs grow; this RDR
is that revisit for group writes.

| Level | What the engine checks | Guards against | Cost |
|---|---|---|---|
| 0 (today) | Nothing on these subspaces; readers see the stamp as a label | Nothing; a reader may notice a mismatch | None |
| 1 | On `group/*`, refuse a write whose `sender_session` is not the leader (group entry, assignments) or the member itself (membership); an unattested write is never an assignment | A confused or stale session writing the wrong group state | A refusal path on one subspace family, new against JDR-002's "never a refusal" |
| 2 | Leadership is a claim: the leader holds the claim on the group's own tuple, and an assignment write must carry that `claim_id`, the one capability JDR-002 names (returned only to the claim's winner, withheld from `rd`) | Any session that is not the current leader, including one that set its own stamp | The leader keeps the claim's lease alive; a lost lease is a leader change |
| 3 | Per-principal identity, host-attested Desktop sessions, binding directory writes to the writer's session (JDR-002's own revisit list) | A second human principal or an agent class the tenant does not trust | Engine and auth work beyond this RDR |

Sam's answer to open question 4 (2026-11-16): the engine's recorded session is
enough for now, and the attestation work is evaluated and included here. So
level 1 ships with Phase 1, level 2 is Step 6 below, evaluated before it is
built, and level 3 is named and not built: it waits for JDR-002's trigger (a
second human principal or an untrusted agent class). Level 1 is a refusal, and
JDR-002 says the stamp is never one, so JDR-002 gains a revision entry naming
`group/*` as the first subspace family where the stamp is enforced; that edit
to a joint record is Sam's.

**The push record.** On `PUSH_OK`, `git-push-develop.sh` posts to
`board/ci/nexus-develop--push`, on the existing `board/ci/<topic>` template
(5000 live rows, three days' retention, the same lifetime as the CI posts it
is joined with). No template matches the draft's `board/push/nexus-develop`
(`nexus_rdr/252-research-1` F3). The template's dimensions already fit:

```
dims:  from=<session id>  kind=push  sha=<tip>
body:  {"base": "<develop sha before the push>", "n": <commits>, "name": "<session name>"}
```

The record carries no list of pushed shas: develop CI runs once per pushed
tip, so every red is a push's `tip`, and the base-to-tip chain orders the
pushes. The writer is the pushing session with its ordinary tenant token, not
the adapter's `board-ci` credential, so the template's doc block gains it as a
writer.

**Develop's state from commit order.** Commit order comes from the push
records' `base` and `tip` (each push names the develop sha it built on), never
from when a CI post arrived. A red counts only if its sha is the newest develop
commit with a finished run. A late red for an older commit is history, not a
block; a late green for an older commit does not clear a block a newer red
raised.

**The owner, stamped once.** One writer, the stamper, watches
`board/ci/nexus-develop--done` and `board/ci/nexus-develop--push`. For a red that
counts, it posts once to `board/ci/nexus-develop--owner`:

```
kind=owner  sha=<red sha>  owner=<session id>  next=<session id>  reason=<why>
```

- **Owner**: the session of the push whose `tip` is the first red tip after
  the last green tip, in the order the push records' `base`-to-`tip` chain
  gives. A red that a later push only inherited belongs to
  the earlier pusher. Example: on 2026-11-16, 4554bd15f went red only because
  da1c285da had; the owner is da1c285da's pusher.
- **Next up**: the `push-train` group's leader. If the owner's session is gone,
  the leader is owner and `reason` says so.
- **Unknown pusher** (no push record): the leader is owner, `reason=no_record`.

The stamper runs in the engine (Sam, 2026-11-16, refining answer 5 once
`nexus_rdr/252-research-1` F5 showed the CI adapter is an AWS Lambda in the
conexus repository, not part of the engine): Java logic run when a
`board/ci/nexus-develop--done` row is written, reading the push records and
the group's assignments from the engine's own tables and writing the owner
row. The Lambda adapter is unchanged and keeps its write-only `board-ci`
scope. It ships in an engine release. Alternative 2 below keeps "every
session computes" as the fallback design.

**Load by membership.** New optional table in the aura file:

```toml
[load]
group = "push-train"      # loaded only in sessions that are members
roles = ["pusher", "leader"]
```

Load is evaluated when the session's MCP server starts, the only time auras
and config are read today (Sam's answer 3). A server start is not a new
session: the session id survives an MCP reconnect (`/mcp`) and a restart, as
the 8.3.0 restart round on 2026-11-16 showed. So to join or leave a group, a
session writes or removes its membership tuple and then reconnects its MCP
server; its identity, board entry and other memberships are unchanged. Only
`/clear` gives a session a new id. Memberships then move the way the threads
board's `session:` line does: the hook that rewrites that line also re-points
the session's membership tuples to the new id. Unknown keys are refused by
`nx aura check`.

**Typed displays and noise.** `ci_develop_red` triggers on the owner post, not
the raw CI post, and shows one of three states, each with its own urgency, in
the manner of DBM's typed warnings:

| State | Display | Channel wake | Local action |
|---|---|---|---|
| Owner | "Develop is red at <sha>, first red in your push. Diagnose it." | Yes | Diagnose; acknowledge |
| Next up | "Develop is red at <sha>; <owner> owns it. You are next up." | Yes | Stand by; take it if not acknowledged |
| Everyone else in the group | "Develop is red at <sha>; <owner> owns it. Hold your pushes." | No (block only) | Hold its own pushes until green |

**Two additions to the aura surface** (`nexus_rdr/252-research-1` F1, F2).
An aura cannot tell which state it is in without knowing its own session,
and today an argument can come only from the tuple or a literal. So:

- A new argument source, `self.session`, filled by the host with the
  session id the waiter resolved at load (`[args] me = "self.session"`). The
  procedure compares it with the owner post's `owner` and `next` fields. It
  is a source, not a reserved argument name, so RDR-242's header grammar is
  unchanged; `nx aura check` accepts it for any declared string argument.
- `display.channel` accepts, besides `true` and `false`, the name of a
  boolean field of the procedure's result (`channel = "wake"`). The host
  sends the fixed-template notice only when that field is `true`, so the
  owner and next up are woken and everyone else gets the block alone. The
  notice stays the fixed template of RDR-249; only whether it is sent varies.

**Re-surfacing.** When the owner changes (the leader takes over, or a new
owner post supersedes the old one), each member's block is rewritten, and the
new owner gets a wake even if it had already seen the red, as Angry
Assignments re-shows a changed assignment.

**Acknowledgement.** The owner writes `kind=ack sha=<sha> session=<id>` to
`board/ci/nexus-develop--owner` with an ordinary tool. If none arrives by a
deadline counted from the owner post, the stamper posts a new owner post naming
the next-up session. The deadline defaults to 20 minutes (Sam, 2026-11-16) and
is set per group in the group entry (`ack_deadline=<minutes>`), with an aura
able to override it for its own task. The research gives no basis for any
number, so the default is calibrated: the push records and acknowledgements
give each red's time to acknowledge, and the default is reviewed against that
distribution after the first 20 reds (or 30 days), and set to cover most
acknowledgements without leaving a dead owner unnoticed for long.

**Escalation.** Owner, then next up, then an RDR-245 ask to Sam only if next up
is also gone. Unvalidated: the research found no protocol to copy.

**Claims.** Only for work no assignment names, such as rerunning a flaky shard;
RDR-211's queue as is.

**If the platform narrows.** If the read-only procedure surface shrinks (as
WeakAuras lost combat-state support under Midnight), the owner post still
carries the decision, and a plain display of it needs no procedure.

### Existing Infrastructure Audit

| Piece | Exists? | Use |
|---|---|---|
| Aura parse, trigger, display | Yes (RDR-249) | Adds `[load]` and typed displays |
| Read-only procedure runtime | Yes (RDR-242) | Formats the display |
| Board posts and claims | Yes (RDR-211, 220) | Groups, push record, owner post, acknowledgement |
| Push script with session identity | Yes | Writes the push record |
| `sender_session` stamp | Yes, as a label (JDR-002; engine-service-v0.1.163) | Level 1 enforces it on `group/*` |
| `claim_id` capability | Yes (JDR-002) | Level 2: leadership as a claim |
| Groups, members, assignments | No | New board templates |
| Owner stamper | No | New; engine or one session |

### Decision Rationale

A stamped owner over every session computing it, because the platform deciding
once is what private auras do and leaves nothing for sessions to disagree on.
A computed owner over claims, because the record names the owner and a claim
only adds a race. Groups as the base, because the push train, windows, review
pairs and the release crew are already groups, and Load, assignments, leader
calls and ready checks are all group mechanics. Local actions only, because
procedures are read-only by design.

## Alternatives Considered

### Alternative 1: Claim queue for every red

A red becomes a task on RDR-211's queue, and the first session to claim it owns
it.

- **Pro:** a shipped template; no push record.
- **Con:** the fastest session wins, not the one with context; a lease can
  expire under a slow diagnosis; it races where a computation would not.
- **Rejected:** the record can name the owner.

### Alternative 2: Every session computes the owner (no stamper)

Each session's aura procedure reads the push records and works out owner and
next up itself, as KickSync's clients do.

- **Pro:** no new writer; nothing to run in the engine.
- **Con:** a dozen computations of one answer; a stale read in one session
  disagrees with the rest; the logic lives in Lua.
- **Kept** as the fallback if the stamper cannot be placed.

### Alternative 3: Convention only

A written rule: the pusher or halcyon diagnoses, everyone else holds pushes.

- **Pro:** no code.
- **Con:** every session still wakes on every red; the rule drifts; late reds
  still mislead.
- **Rejected** as the end state; the interim if Sam turns the aura on first.

### Alternative 4: Route every red to halcyon

- **Pro:** one fixed owner.
- **Con:** least context about a commit it did not write; a bottleneck.
- **Rejected** as the default; kept as next up.

### Briefly Rejected

- Parsing the pusher out of commit messages or bead ids: the assignment-note
  failure mode.
- Mail between sessions to hand off ownership: a side channel.

## Trade-offs

### Consequences

- Most group members see a block that says "hold your pushes" and are not
  woken.
- Sessions outside the group never load the aura.
- The push script gains one board write per push; there is one new writer, the
  stamper.

### Risks and Mitigations

- **Stamper down**: no owner post, so no member's aura fires. Mitigation: the
  aura also triggers on the raw red after a short wait and shows the plain
  RDR-249 block, naming no owner.
- **Wrong or forged assignment**: at level 1 the engine refuses a group write
  whose recorded session is not the leader's or the member's own, and
  unattested writes are never assignments. A hostile token holder can still
  mint a matching session token (JDR-002); level 2 closes that for
  assignments, and level 3 is out of scope.
- **Stale membership** (a session's server started before it joined): it loads
  the aura at its next start, unless the re-load hook is added.

### Failure Modes

- The procedure fails or times out: the session shows the owner post as text.

## Implementation Plan

### Prerequisites

- [ ] Sam accepts the model (groups, stamped owner, three states, local
  actions) and answers the open questions.
- [ ] The `push-train` group's shape agreed with halcyon.

### Minimum Viable Validation

With `ci_develop_red` loaded in three members of a test group (A pushed commit
X, B leads, C pushed nothing) and one non-member D: a red on X wakes A as owner
and B as next up, shows C "hold your pushes" with no wake, and does nothing in
D. A later green on an older commit does not clear it, and a late red on an
older commit raises nothing. With A's session gone, the owner post names B and
B is woken. A group write from C is refused.

### Phase 1: Code Implementation

Steps 1 and 3 are engine work and ship in one engine release, which Sam
cuts; Steps 2, 4 and 5 are client work and ship in a conexus release that
pins that engine.

#### Step 1: Group, member and assignment templates, with the writer check

Engine: templates `group/<name>` and `member/<session>`, and the level-1
writer check on them (`nexus_rdr/252-research-1` F6).

#### Step 2: Push record in `git-push-develop.sh`

Client: one `nx tuple out` to `board/ci/nexus-develop--push` (with
`--key topic=nexus-develop--push`) after `PUSH_OK`, never failing the push; the `board/ci/<topic>` doc block names the
new writer.

#### Step 3: The owner stamper and the owner post

Engine: on a `--done` row's write, compute the owner from the push records
and the assignments, and write the owner row.

#### Step 4: `[load]` and typed displays in auras; `ci_develop_red` moved onto the owner post

Client: `[load]` read from `member/<session>` at server start; the
`self.session` argument source; `display.channel` naming a result field.

#### Step 5: Acknowledgement, deadline and re-surfacing

The per-group `ack_deadline` with a 20-minute default, the per-aura override,
and the calibration read (time to acknowledge per red) in Day 2.

#### Step 6: Evaluate and build attestation level 2

Measure what leadership-as-a-claim costs (lease renewal by the leader, leader
change on a lost lease, the `claim_id` carried on assignment writes), against
the level-1 refusal alone. Build it if the evaluation holds; record the result
either way. JDR-002's revision entry for level 1 (Sam's edit) lands with
Step 1.

### Phase 2: Operational Activation

#### Activation Step 1: Form `push-train` and turn the aura on

Sam's go: form the group with halcyon as leader, then `nx config set
auras.ci_develop_red=on` and a restart round.

### Day 2 Operations

- `nx aura list` shows, per aura, loaded or not and why (which group).
- A group listing shows leader, members and open assignments.
- Push records and acknowledgement times give after-the-fact grading: time
  from red to acknowledgement and to fix, as WoW tools grade cooldown use.

### New Dependencies

None.

## Test Plan

- Owner, next up and everyone-else states from fixed push records.
- Inherited red: the owner is the first red's pusher.
- Late red and late green, in both orders.
- Owner session gone; push record missing: the leader owns, with the reason.
- Writer check: a non-leader's assignment is refused; an unattested one is
  ignored.
- `[load]`: a non-member never subscribes; an unknown key is refused.
- No channel wake for "everyone else"; a wake on re-surfacing.
- Stamper down: the plain block after the wait.

## Validation

### Testing Strategy

Unit tests on the stamper with recorded tuples; one live check in a test group
(the MVV).

### Performance Expectations

Load removes the aura from non-members. A fire costs about 50 ms today
(marginalia, 2026-11-16); members read one owner post instead of computing.

## Finalization Gate

### Contradiction Check

The subspace names agree everywhere: memberships under `member/<session>`,
group entries and assignments under `group/<name>`, push records on
`board/ci/nexus-develop--push`, owner posts on `board/ci/nexus-develop--owner`.
The stamper's placement (the engine) is stated once in Technical Design and
matches open question 5 as refined. Owner, next up and everyone else are the
same three states in Technical Design, the MVV and the Test Plan.

### Assumption Verification

All three Critical Assumptions are settled: 1 by source and a spike
(`nexus_rdr/252-research-1` F4, `252-research-2`); 2 and 3 by design choices
the audit forced (the stamper in the engine; memberships keyed by session).
Unvalidated by design and said so: escalation and role-to-owner resolution
(Key Discovery 6) and the 20-minute deadline (calibrated after use).

### Scope Verification

In scope: groups, push records, the stamper, `[load]`, two aura-surface
additions, acknowledgement, attestation levels 1 and 2. Out: level 3, any
aura that writes shared state, a re-load hook (named for the gate, not
built). The MVV exercises every in-scope piece once.

### Cross-Cutting Concerns

Trust: level 1 enforces `sender_session` on `group/*` and `member/*`, a
first refusal against JDR-002's "never a refusal" that needs Sam's revision
entry there. Release: Steps 1 and 3 need an engine release, Steps 2, 4 and 5
a conexus release that pins it. Noise: no wake for everyone else. Data
lifetime: push and owner records share the CI posts' three-day retention.

### Proportionality

The two new engine templates and one stamper serve every red; the alternative
of convention only (Alternative 3) was tried today and two late posts misled
the block in one afternoon. Attestation level 2 is evaluated before it is
built.

## Open Questions for Sam

Answered 2026-11-16 by Sam ("your recom look good"): 1 from the record, 3 load
at server start, 5 the stamper in the engine beside the CI adapter (refined the same day:
in the engine, since the adapter is a Lambda outside it; T2
`nexus_rdr/252-decision-stamper-2026-11-16`), 6 through
the leader, 7 the aura stays off until this lands. Then: 2, 20 minutes as the
default, set per group with a per-aura override, and calibrated from measured
acknowledgement times; 4, the engine's recorded session is enough for now, and
the attestation work is evaluated and included (Trust and attestation, levels
1 to 3; Step 6). All seven are answered.

1. **Owner source.** From the record (the pusher, as designed), or always the
   group's leader?
2. **Deadline.** How long before next up takes over? The research gives no
   basis; 20 minutes is a placeholder.
3. **Late joiners.** Load only at server start (a restart to join), or one
   re-load hook when a session joins a group?
4. **Trust.** Is the engine's recorded session enough for the writer check, or
   does group trust wait for the attestation work?
5. **Stamper placement.** In the engine beside the CI adapter, or as one
   session's job (halcyon's)?
6. **Escalation.** Should a red ever reach Sam without going through the
   leader?
7. **Interim.** Turn `ci_develop_red` on now with a written convention, or keep
   it off until this lands?

## References

- RDR-249 § Background (what was taken from WeakAuras).
- T3 `agent-orchestration`: "WeakAuras and raid coordination models: who an
  alert is for, who responds" (2026-11-16), with its sources: the WeakAuras
  wiki (Load), Warcraft Wiki (private auras, secret values), DBM's
  special-warning guide, KickSync, Sniffa, Angry Assignments, Blizzard's
  Midnight post.
- T2 `nexus/push-train` (the push train's rules, Sam 2026-11-07).
- marginalia's live check of `ci_develop_red` and notes on this draft,
  2026-11-16.

## Revision History

- 2026-11-16: created (draft), on Sam's request to model aura response
  strategically and validate it against the WeakAuras ecosystem.
- 2026-11-16: reworked around groups (Sam: "do we have the equivalent of a
  raid? ... they're just group coord mechanics"); owner stamped once rather
  than computed in every session (private auras); trust for group writes
  (Angry Assignments); role routing and the deadline marked unvalidated; no
  wake for "everyone else" (noise); re-surfacing on owner change; a fallback if
  the procedure surface narrows; marginalia's notes on load timing and commit
  order kept.
- 2026-11-16: Sam's answers to questions 2 and 4 recorded: a per-group acknowledgement deadline (20-minute default, per-aura override, calibrated from measured times); attestation evaluated in four levels against JDR-002, with level 1 in Phase 1, level 2 as Step 6 and level 3 named, not built.
- 2026-11-16: ownership moved from wren to marginalia. Gaps from the source audit (`nexus_rdr/252-research-1`) closed in the design: memberships under `member/<session>` (one read at load); push records on `board/ci/nexus-develop--push` with the existing template, ordered by base and tip; the stamper in the engine (Sam's refined answer 5); the `self.session` argument source and `display.channel` naming a result field; Phase 1 split into engine and client steps. Assumptions 2 and 3 met by design; assumption 1 verified by source and a spike (`nexus_rdr/252-research-2`).
