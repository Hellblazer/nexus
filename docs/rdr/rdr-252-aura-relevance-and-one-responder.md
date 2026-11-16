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
is this RDR's new term: a named set of sessions with a leader, a roster and a
lifetime, the counterpart of a raid in World of Warcraft.

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
  group membership and an owner read from a stamped owner post. Its answer
  "one block per session" stands for display.
- **RDR-211** (board, queue and lock templates, closed): groups are built from
  its board and claim shapes; its queue serves work no owner post names.
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
  template's key, `--key topic=nexus-develop--push`. The spike proved the
  template takes the write, not who wrote it; trust comes from the engine's
  stamp (Technical Design, Trust).
- [x] One writer can join a CI post with the push records and stamp the owner
  within seconds of the post. **Status**: met by design. The stamper runs in
  the engine on the write of the CI post (Sam, 2026-11-16), so the join is a
  read of the engine's own rows inside that write, not a second process.
- [x] Group membership can be read at MCP server start in one tuple read.
  **Status**: met by design. Memberships live under the member's own subspace,
  `member/<session name>` (Technical Design, Groups), so "my groups" is one
  read of one subspace (`nexus_rdr/252-research-1` F7).
- [x] The engine can tell whether a session is alive. **Status**: Verified
  (`nexus_rdr/252-research-3`): every session's MCP server holds a
  `directory/<name>` lease mapping its session name to its current id,
  re-sent every 60 seconds and lapsing 300 seconds after the session dies.

## Proposed Solution

### Approach

1. **Groups.** A group is an engine record with a name, a leader, a roster and
   a lifetime. Sam or the leader forms and disbands one; a session can be in
   several. A member is named by its session name, so it stays a member
   across `/clear`.
2. **A push record.** The push script writes one tuple per push, stamped by
   the engine with the pushing session.
3. **The owner, stamped once.** The engine, on each finished develop run,
   works out develop's state in commit order and posts it: an owner post for
   a red (owner and next up included), a clear post for a green. Sessions
   only read it.
4. **Load by membership.** An aura file may say which group it is for; a
   session that is not in it never loads the aura.
5. **Local actions only.** Each state maps to what this session does to
   itself. No aura writes shared state.

Terms used below. A **tuple** is one record in the engine's tuple space (the
shared store every session reads and writes); a **subspace** is its address,
such as `board/ci/nexus-develop--done`; a **template** is the engine's schema
for a family of subspaces, naming its fields (**dims**, short typed fields
the engine can match on) and its free-text **body**. The **stamp**,
`sender_session`, is the session id the engine records on a write made with a
session token. A **wake** is a channel notice that interrupts an idle
session. A **lease** is a claim on a tuple that lapses unless renewed.

### Technical Design

**Groups.** One engine record per group at `group/<name>`:

```
dims:  kind=group  name=push-train  leader=<session name>
body:  {"purpose": "develop push order", "formed_by": "<session name or sam>",
        "formed_at": "<UTC>", "ends": "<UTC or open>", "ack_deadline": 20}
```

Membership is one tuple per group the session belongs to, under
`member/<session name>`: `kind=member group=<name>`. The session's own MCP
server writes it when the session joins and renews it at every server start;
it expires after 24 hours without renewal and is removed when the session
leaves. Keying it by session name makes "which groups am I in" one read at
server start, and it re-points itself across `/clear`: the name stays, and
the session directory (`directory/<name>`, the lease every session's MCP
server holds, mapping its name to its current id) points the name at the new
id. No hook moves anything (Sam's answer on gate finding S3, "re-point and
liveness"). A session name fits the engine's address-segment pattern
(`[A-Za-z0-9][A-Za-z0-9._-]*`). Two new engine templates carry these:
`group/<name>` and `member/<name>`. A disbanded group's record stays as
history with `ended_at`.

There are no typed assignments in this RDR (Sam's answer on gate finding S5:
nothing read them; a later RDR can add them with a reader). The leader is the
group's one role.

First groups, made explicit from what runs today: `push-train` (leader
halcyon), and ad hoc push windows, review pairs and the release crew as
needed. The restart script's ready check becomes a group operation: "every
member of group X, report ready".

**Liveness.** A session is alive while its `directory/<name>` lease is live.
The stamper reads the directory to decide "the owner's session is gone"; no
other liveness source is added (`nexus_rdr/252-research-3`).

**Trust and attestation.** Only the group's leader may write the group record;
only a session may write its own membership; the push record names its
pusher by the engine's stamp. JDR-002 sets the starting point: the tenant
(from the bearer token) is the security boundary, `from`, claimant and
directory names are claims, and the engine's `sender_session` stamp
(engine-service-v0.1.163) is "a label, never a refusal". Any holder of the
tenant's data token can mint a session token for any session id, so the stamp
guards against a confused model, not a hostile one. JDR-002 says to revisit
this "in an RDR" when trust needs grow; this RDR is that revisit for group
writes.

| Level | What the engine checks | Guards against | Cost |
|---|---|---|---|
| 0 (today) | Nothing on these subspaces; readers see the stamp as a label | Nothing; a reader may notice a mismatch | None |
| 1 (this RDR) | On `group/<name>`, refuse a write whose stamp is not the id the directory gives for the record's `leader`; on `member/<name>`, refuse a write whose stamp is not the id the directory gives for `<name>`; an unstamped write is refused on both | A confused or stale session writing the wrong group state | A refusal path on two subspace families, new against JDR-002's "never a refusal" |
| 2 (later) | Leadership as a claim carrying `claim_id` | Any session that is not the current leader, including one that set its own stamp | Needs a heartbeat a session does not have; moved out with assignments |
| 3 (later) | Per-principal identity, host-attested Desktop sessions (JDR-002's own revisit list) | A second human principal or an agent class the tenant does not trust | Engine and auth work beyond this RDR |

Bootstrap: the first write of a `group/<name>` record names its leader and is
accepted when its stamp is that leader's directory id; Sam forms a group by
asking the session that will lead it to write it. A session whose writes carry
no stamp (a Desktop session today, JDR-002) cannot join or lead under level 1;
that is stated, not fixed here. Level 1 is a refusal, and JDR-002 says the
stamp is never one, so JDR-002 gains a revision entry naming `group/*` and
`member/*` as the first subspace families where the stamp is enforced; that
edit to a joint record is Sam's.

**The push record.** On `PUSH_OK`, on every path of `git-push-develop.sh`
including `--skip-lock`, the script posts to `board/ci/nexus-develop--push`
on the existing `board/ci/<topic>` template (5000 live rows, three days'
retention, the same lifetime as the CI posts it is joined with), with the
session token the script already resolves:

```
dims:  from=<session name>  kind=push  sha=<tip>
body:  {"base": "<develop sha before the push>", "n": <commits>}
```

The stamper trusts the record's stamp, never `from` (Sam's answer on gate
finding S4). A record with no stamp counts as no record. The template's
doc block gains the push script as a writer and the `from` doc says
`github` or a session name.

**Develop's state from commit order.** The push records' `base` and `tip`
chain gives the order of develop's tips; arrival time never does. A pushed
tip need not have a run of its own: develop CI runs in concurrency slots
where "a newer push replaces only that slot's pending run"
(`.github/workflows/ci.yml:20-40`; `nexus_rdr/252-research-3`), and a
replaced run posts `outcome=cancelled`. So the stamper treats only
`success` and `failure` as verdicts, and a verdict counts only if its tip is
the newest tip in the chain with a verdict. A late red for an older tip is
history, not a block; a late green for an older tip does not clear a block a
newer red raised. When a push's `base` matches no recorded tip (a hand push, a
release back-merge), the chain has a gap: the stamper orders by the CI post's
own times past the gap, and the owner of a red across a gap is the leader,
`reason=chain_gap`.

**The owner, stamped once.** On each `board/ci/nexus-develop--done` write, the
stamper (Java, in the engine, run on that write; Sam, 2026-11-16, refining
answer 5 once `nexus_rdr/252-research-1` F5 showed the CI adapter is an AWS
Lambda in the conexus repository, not part of the engine) works out
develop's state and, when it changed, posts one row to
`board/ci/nexus-develop--owner`:

```
red:    dims kind=owner  sha=<red tip>
        body {"owner": "<session name>", "next": "<session name>", "reason": "<why>", "suspects": [...]}
green:  dims kind=clear  sha=<green tip>
```

- **Suspects**: every push after the last green tip up to and including the
  red tip, since a replaced run leaves a pushed tip untested.
- **Owner**: the pusher of the earliest suspect (Sam's answer on gate finding
  C1). A red that a later push only inherited belongs to the earlier pusher.
  Example: on 2026-11-16, 4554bd15f went red only because da1c285da had; the
  owner is da1c285da's pusher.
- **Next up**: the next suspect's pusher, or the `push-train` leader when
  there is none.
- **Leader takes it** when the owner's session is gone (no live directory
  lease), when the owner is not a member of `push-train` (its aura would not
  load), when no push record has a stamp (`reason=no_record`), and across a
  chain gap (`reason=chain_gap`).
- **A stamper error** posts the leader as owner with `reason=stamper_error`,
  so a red is never left without an owner post. If the engine is down, no CI
  post lands either, so there is no separate "stamper down" path.

The Lambda adapter is unchanged and keeps its write-only `board-ci` scope.
The stamper ships in an engine release. Alternative 2 below keeps "every
session computes" as the fallback design.

**Load by membership.** New optional table in the aura file:

```toml
[load]
group = "push-train"      # loaded only in sessions that are members
```

Load is evaluated when the session's MCP server starts, the only time auras
and config are read today (Sam's answer 3). A server start is not a new
session: the session survives an MCP reconnect (`/mcp`) and a restart, as the
8.3.0 restart round on 2026-11-16 showed. To join or leave a group, a session
writes or removes its membership and reconnects its MCP server. Unknown keys
are refused by `nx aura check`.

**Typed displays and noise.** `ci_develop_red` triggers on
`board/ci/nexus-develop--owner` and clears on its `kind=clear` rows, so the
commit-order logic lives only in the stamper. It shows one of three states,
each with its own urgency, in the manner of DBM's typed warnings:

| State | Display | Channel wake | Local action |
|---|---|---|---|
| Owner | "Develop is red at <sha>, first red in your push. Diagnose it." | Yes | Diagnose; acknowledge |
| Next up | "Develop is red at <sha>; <owner> owns it. You are next up." | Yes | Stand by; take it if not acknowledged |
| Everyone else in the group | "Develop is red at <sha>; <owner> owns it. Hold your pushes." | No (block only) | Hold its own pushes until green |

**Two additions to the aura surface** (`nexus_rdr/252-research-1` F1, F2).
An aura cannot tell which state it is in without knowing its own session,
and today an argument can come only from the tuple or a literal. So:

- A new argument source, `self.session`, filled by the host with the
  session's name at load (`[args] me = "self.session"`). The procedure
  compares it with the owner post's `body.owner` and `body.next`. It is a
  source, not a reserved argument name, so RDR-242's header grammar is
  unchanged; `nx aura check` accepts it for any declared string argument.
- `display.channel` accepts, besides `true` and `false`, the name of a
  boolean field of the procedure's result (`channel = "wake"`). The host
  sends the fixed-template notice only when that field is `true`, so the
  owner and next up are woken and everyone else gets the block alone. The
  notice stays the fixed template of RDR-249; only whether it is sent varies.

**Re-surfacing.** When the owner changes (the deadline passes, or a newer red
supersedes the old one), the stamper posts a new owner row; each member's
block is rewritten, and the new owner gets a wake even if it had already seen
the red, as Angry Assignments re-shows a changed assignment.

**Acknowledgement.** The owner writes `kind=ack sha=<sha>` (stamped) to
`board/ci/nexus-develop--owner` with an ordinary tool. The deadline is a timer
in the stamper, in the engine: if no stamped ack from the owner arrives by
the deadline counted from the owner row, the stamper posts a new owner row
naming the next-up session. After an engine restart the timer is rebuilt from
`--owner` rows with no ack, so a restart loses no deadline. The deadline
defaults to 20 minutes (Sam, 2026-11-16), set per group in the group record's
`ack_deadline`. The research gives no basis for any number, so the default is
calibrated: the push records and acknowledgements give each red's time to
acknowledge, and the default is reviewed against that distribution after the
first 20 reds (or 30 days).

**Escalation.** Owner, then next up, then an RDR-245 ask to Sam only if next up
is also gone. Unvalidated: the research found no protocol to copy.

**Claims.** Only for work no owner post names, such as rerunning a flaky shard;
RDR-211's queue as is.

**If the platform narrows.** If the read-only procedure surface shrinks (as
WeakAuras lost combat-state support under Midnight), the owner post still
carries the decision, and a plain display of it needs no procedure.

### Existing Infrastructure Audit

| Piece | Exists? | Use |
|---|---|---|
| Aura parse, trigger, clear, display | Yes (RDR-249) | Adds `[load]`, `self.session`, a result-driven wake |
| Read-only procedure runtime | Yes (RDR-242) | Formats the display |
| `board/ci/<topic>` template | Yes (RDR-220) | Push records, owner and clear rows, acks (body fields; doc block updated) |
| Session directory leases | Yes (RDR-208) | Liveness and the name-to-id map for level 1 |
| Push script with session identity | Yes | Writes the push record |
| `sender_session` stamp | Yes, as a label (JDR-002; engine-service-v0.1.163) | Level 1 enforces it on `group/*` and `member/*` |
| Groups and members | No | Two new engine templates |
| Owner stamper and its deadline timer | No | New, in the engine |

### Decision Rationale

A stamped owner over every session computing it, because the platform deciding
once is what private auras do and leaves nothing for sessions to disagree on.
A computed owner over claims, because the record names the owner and a claim
only adds a race. Groups as the base, because the push train, windows, review
pairs and the release crew are already groups, and Load, leader calls and
ready checks are all group mechanics. Members by name, because the directory
already maps names to live sessions. Local actions only, because procedures
are read-only by design.

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

- **Stamper error**: the stamper posts the leader as owner
  (`reason=stamper_error`), so a red always gets an owner post.
- **Wrong group write**: at level 1 the engine refuses a group or member write
  whose stamp is not the directory's id for the leader or the member's own
  name, and refuses an unstamped one. A hostile token holder can still mint a
  matching session token (JDR-002); levels 2 and 3 are later work.
- **Wrong pusher**: the stamper uses the push record's stamp, never `from`; an
  unstamped record counts as no record and the leader owns the red.
- **Stale membership** (a session's server started before it joined): it loads
  the aura at its next start; meanwhile the stamper sees it as a non-member
  and the leader owns its reds.

### Failure Modes

- The procedure fails or times out: the session shows the owner post as text.
- The push chain has a gap: the leader owns a red across it, `reason=chain_gap`.
- The owner's session dies after the owner post: the deadline passes and the
  next-up session is named.

## Implementation Plan

### Prerequisites

- [x] Sam accepts the model (groups, stamped owner, three states, local
  actions) and answers the open questions (2026-11-16; gate round 1 answers
  the same day).
- [ ] The `push-train` group's shape agreed with halcyon.

### Minimum Viable Validation

With `ci_develop_red` loaded in three members of a test group (A pushed commit
X, B leads, C pushed nothing) and one non-member D: a red on X wakes A as owner
and B as next up, shows C "hold your pushes" with no wake, and does nothing in
D. The green that follows clears every member's block, whichever of the red
and the green arrives first. A late green on an older tip does not clear it,
and a late red on an older tip raises nothing. With two pushes X then Y and
X's run replaced, a red on Y names X's pusher as owner and Y's as next up. With
A's session gone, the owner post names B and B is woken; with no ack by the
deadline, the next up is named. A group write from C is refused.

### Phase 1: Code Implementation

Steps 1 and 3 are engine work and ship in one engine release, which Sam
cuts; Steps 2 and 4 are client work and ship in a conexus release that pins
that engine.

#### Step 1: Group and member templates, with the level-1 writer check

Engine: templates `group/<name>` and `member/<name>`, retention long enough for
a standing group (a group record lives until disbanded; a membership expires
24 hours after its last renewal), and the level-1 check against the directory
(`nexus_rdr/252-research-1` F6, `252-research-3`). JDR-002's revision entry
(Sam's edit) lands with it.

#### Step 2: Push record in `git-push-develop.sh`

Client: one stamped `nx tuple out` to `board/ci/nexus-develop--push` (with
`--key topic=nexus-develop--push`) after `PUSH_OK` on every path, never
failing the push; the `board/ci/<topic>` doc block names the new writers and
the body fields of push, owner, clear and ack rows.

#### Step 3: The owner stamper, its deadline timer and the owner and clear rows

Engine: on a `--done` write, develop's state from the push chain, the suspect
set, owner and next up, the leader's cases, owner and clear rows; the
acknowledgement deadline as a timer rebuilt from `--owner` rows after a
restart; a stamper error posting the leader.

#### Step 4: `[load]`, `self.session` and the result-driven wake; `ci_develop_red` moved onto the owner post

Client: `[load]` read from `member/<name>` at server start, and the MCP
server writing and renewing the session's memberships; the `self.session`
argument source; `display.channel` naming a result field; `ci_develop_red`
triggering on `kind=owner` and clearing on `kind=clear`; re-surfacing on a new
owner row; the calibration read (time to acknowledge per red) for Day 2.

### Phase 2: Operational Activation

#### Activation Step 1: Form `push-train` and turn the aura on

Sam's go: halcyon writes the `push-train` record as its leader, members join,
then `nx config set auras.ci_develop_red=on` and a restart round.

### Day 2 Operations

- `nx aura list` shows, per aura, loaded or not and why (which group).
- A group listing shows the leader and members, each with its directory lease.
- Push records and acknowledgement times give after-the-fact grading: time
  from red to acknowledgement and to fix, as WoW tools grade cooldown use.

### New Dependencies

None.

## Test Plan

- Owner, next up and everyone-else states from fixed push records.
- Inherited red: the owner is the earliest suspect's pusher.
- A replaced (cancelled) run: the untested tip is a suspect.
- Late red and late green, in both orders; a green clears in both arrival
  orders.
- Owner session gone (lapsed lease), owner not a member, no stamped push
  record, a chain gap, a stamper error: the leader owns, with the reason.
- The deadline: no ack names next up; an engine restart keeps the timer.
- Writer check: a member write by another session, a group write by a
  non-leader and any unstamped write are refused.
- `[load]`: a non-member never subscribes; an unknown key is refused.
- No channel wake for "everyone else"; a wake on re-surfacing.

## Validation

### Testing Strategy

Unit tests on the stamper with recorded tuples; one live check in a test group
(the MVV).

### Performance Expectations

Load removes the aura from non-members. A fire costs about 50 ms today
(marginalia, 2026-11-16); members read one owner post instead of computing.

## Finalization Gate

### Contradiction Check

The subspaces agree everywhere: `group/<name>` and `member/<name>` (new
templates), push records on `board/ci/nexus-develop--push`, owner, clear and
ack rows on `board/ci/nexus-develop--owner`, all on the existing
`board/ci/<topic>` template with new fields in the body. The owner rule
(earliest suspect), the leader's cases and the three display states read the
same in Technical Design, the MVV and the Test Plan. The engine-client split
is stated once in Phase 1 and matches the steps.

### Assumption Verification

All four Critical Assumptions are settled: 1 by source and a spike
(`nexus_rdr/252-research-1` F4, `252-research-2`); 2 and 3 by design; 4 by
source (`252-research-3`). Corrected after gate round 1: CI does not run once
per pushed tip (`252-research-3`), so the owner rule works on a suspect set.
Inferred, not read: that a session keeps its name across `/clear`.
Unvalidated by design and said so: escalation and the 20-minute deadline
(calibrated after use).

### Scope Verification

In scope: groups and members, push records, the stamper with its deadline
timer, owner and clear rows, `[load]`, two aura-surface additions,
acknowledgement, attestation level 1. Out: typed assignments, attestation
levels 2 and 3, any aura that writes shared state, a re-load hook. The MVV
exercises every in-scope piece once.

### Cross-Cutting Concerns

Trust: level 1 enforces the stamp on `group/*` and `member/*` against the
session directory, a first refusal against JDR-002's "never a refusal" that
needs Sam's revision entry there; unstamped (Desktop) sessions cannot join.
Release: Steps 1 and 3 need an engine release, Steps 2 and 4 a conexus
release that pins it. Noise: no wake for everyone else. Data lifetime: push,
owner and clear rows share the CI posts' three-day retention; a group record
lives until disbanded; a membership expires 24 hours after its last renewal.

### Proportionality

Two new engine templates, one stamper with a timer, and two aura-surface
additions serve every red. The alternative of convention only (Alternative 3)
leaves every reader to catch a late post, and `ci_develop_red`'s live run on
2026-11-16 saw two in one afternoon (a red for 3b580d898 after a newer green,
and a red for a708b957e after a newer green). Typed assignments and
attestation level 2, which nothing here would read, were cut at gate round 1.

## Open Questions for Sam

All answered. On the draft, 2026-11-16 (Sam: "your recom look good", then
two more): 1 the owner from the record; 2 a 20-minute default deadline, set
per group and calibrated from measured acknowledgement times; 3 load at server
start; 4 the engine's recorded session is enough, with attestation evaluated
in levels; 5 the stamper in the engine (refined the same day once the CI
adapter proved to be a Lambda outside it; T2
`nexus_rdr/252-decision-stamper-2026-11-16`); 6 escalation through the
leader; 7 `ci_develop_red` stays off until this lands.

On gate round 1's findings, 2026-11-16, by picker in marginalia's session:
C1, the earliest suspect owns a red; S5, cut typed assignments; S3, re-point
and liveness (met by naming members by session name over the session
directory); S4, trust the engine's stamp only.

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
- 2026-11-16: Gate round 1 — BLOCKED (3 Critical, 7 Significant, 3 ship-blocker(s)); blob `f2d3d214950a`; critique `nexus_rdr/252-gate-critique-2026-11-16-r1`.
- 2026-11-16: gate round 1 findings addressed (`nexus_rdr/252-research-3`; Sam's answers by picker): owner from the earliest suspect, since a replaced CI run leaves a pushed tip untested; the stamper posts clear rows as well as owner rows, and the aura triggers and clears on them; the acknowledgement deadline is a timer in the engine stamper, rebuilt after a restart; new fields live in the body of the existing `board/ci/<topic>` template; members named by session name, with liveness and re-pointing from the session directory; typed assignments and attestation level 2 cut; push records trusted by the engine's stamp only, written on every path of the push script; the leader owns a red across a chain gap, on a stamper error, or when the owner is gone or not a member.
