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
sessions, conducts it. A **red** is a develop CI run that failed. An
**identity** is RDR-253's record for who a session is: a **seat** (such as
`marginalia`) that one process after another holds, with an id `i<n>` that is
never reused and an **alias**, its name; it survives `/clear`, restarts and
renames. RDR-253 also has **ephemeral** identities (a session started with
no seat name) and **delegated** ones (a `claude -p` child); both end with their
process, so only a seat holds a lasting membership. A **group** is this RDR's new term: a named set of identities with a
leader, a roster and a lifetime, the counterpart of a raid in World of
Warcraft.

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
- **RDR-253** (session identity, accepted): groups, owners and push records
  name identities. Memberships are its `member/<id>` records, fenced by its
  Step 2; liveness is its binding state; a push script's write carries the
  identity because a Bash child reads its process's token file. This RDR
  was parked until RDR-253 was accepted, and depends on its Phase 1 Steps 1
  to 3 (`nexus_rdr/252-research-4`).

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
  stamp (Technical Design, Trust). Today a shell `nx` carries no session
  token, so that stamp would be empty; under RDR-253 the push script, a Bash
  child, reads its process's identity token file and its write is stamped
  (`nexus_rdr/252-research-4` F3).
- [x] One writer can join a CI post with the push records and stamp the owner
  within seconds of the post. **Status**: met by design. The stamper runs in
  the engine on the write of the CI post (Sam, 2026-11-16), so the join is a
  read of the engine's own rows inside that write, not a second process.
- [x] Group membership can be read at MCP server start in a few tuple reads.
  **Status**: met by design. Memberships are RDR-253's `member/<id>` records
  under the member's own identity (Technical Design, Groups), so "my groups"
  is one read of one subspace, plus one read of `group/<name>` per group a
  loaded aura names (`nexus_rdr/252-research-1` F7, `252-research-4` F1).
- [x] The engine can tell whether a leased identity is alive. **Status**: met
  by RDR-253's design (`nexus_rdr/252-research-4` F5, `252-research-5` F3,
  F4): a seat is `lapsed` once its last renewal is more than 300 seconds old,
  and the engine applies that rule when it reads the binding. An `unleased`
  (Desktop) identity's liveness is unknown; Technical Design, Liveness, says
  how the stamper treats it.

## Proposed Solution

### Approach

1. **Groups.** A group is an engine record with a name, a leader, a roster and
   a lifetime. The leader forms and disbands one, at Sam's request; an
   identity can be in several. A member is an RDR-253 seat, so it stays a
   member across `/clear`, restarts and renames.
2. **A push record.** The push script writes one tuple per push, stamped by
   the engine with the pushing session, which the engine maps to its
   identity.
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
session token. For an identity-token write, accepted RDR-253 does not yet
say what the stamp holds; its Step 2 bead recommends the current session id
from the latest binding row (`nexus_rdr/252-research-5` F7). This RDR needs
a session id the engine's session index (`session/<session id>`) maps to an
identity and generation, and states that as a requirement on RDR-253 Step 2. A **wake** is a channel notice that interrupts an idle
session. A **lease** is a claim on a tuple that lapses unless renewed.

### Technical Design

**Groups.** One engine record per group at `group/<name>`:

```
dims:  kind=group  name=push-train  leader=<identity id>
body:  {"purpose": "develop push order", "requested_by": "<sam or identity id>",
        "formed_at": "<UTC>", "ends": "<UTC or open>", "ended_at": null,
        "ack_deadline": 20, "ack_deadline_by_topic": {}}
```

`ack_deadline` is the group's default in minutes; `ack_deadline_by_topic`
optionally overrides it for one board topic, keyed by the topic's base name,
the part before `--` (`nexus-develop` covers `--done`, `--push` and
`--owner`). An aura triggers on one topic family, so this is the per-aura
override in a form the engine stamper can read (Sam, 2026-11-16,
`nexus_rdr/252-research-4` F9). Each value is a whole number of minutes from
5 to 240; the template refuses others. The `push-train` record's deadline
governs develop reds.

The template is keyed on the group name, with a retention of 30 days. The
leader's MCP server refires the record at each binding renewal, which only
moves its expiry (`nexus_rdr/252-research-5` F1), so a led group stays while
its leader is alive. An update (a new leader, `ended_at`, a deadline) is a take
of the current row and an `out` of the new one, by the current leader; the
stamper and the load stage read the one current row. A group whose leader
identity has ended stops being refreshed and expires 30 days later; Sam
re-forms it by asking another seat to write a new first record after that.

Membership is RDR-253's `member/<id>` record, one per group the identity
belongs to: `group=<name> role=<leader|member>` (`nexus_rdr/252-research-4`
F1). `role` is informational; the group record's `leader` is the only
authority on who leads, and nothing reads `role`. It is written when the
identity joins and removed when it leaves. `member` is a tuple template, so it
has a finite retention (`nexus_rdr/252-research-5` F1, F2); the identity's
MCP server refires its memberships at each binding renewal, so a live seat
never decays out of its group, and a lapsed seat keeps them for the
template's retention. This is a requirement on RDR-253 Step 1's `member`
template (a keyed template whose refire moves only the expiry, retention 7
days), passed to that step's implementer on 2026-11-16. `nx group join`
refuses an ephemeral or delegated identity, since its membership would end
with its process. It needs no re-pointing across `/clear`: the identity stays and only its session
id changes (Sam's answer on gate finding S3, "re-point and liveness", met by
the identity). Keying it by identity makes "which groups am I in" one read at
server start. One new engine template carries groups, `group/<name>`; the
`member` template is RDR-253's. A group is disbanded by setting `ended_at`;
the record stays until its retention ends. The stamper and the load stage
treat a group as disbanded once `ended_at` is set or `ends` has passed, and
treat a missing record the same way: with no live `push-train` record the
stamper posts no owner rows, and no session loads the aura.

There are no typed assignments in this RDR (Sam's answer on gate finding S5:
nothing read them; a later RDR can add them with a reader). The leader is the
group's one role.

First groups, made explicit from what runs today: `push-train` (leader
halcyon), and ad hoc push windows, review pairs and the release crew as
needed. The restart script's ready check becomes a group operation: "every
member of group X, report ready".

**Liveness.** The stamper asks RDR-253's liveness read, which applies the
300-second rule to the last renewal rather than trusting a stored `live`
(`nexus_rdr/252-research-5` F3). An identity is **live** when that read says
`live`. An `unleased` (Desktop) identity's liveness is unknown and its writes
are unstamped, so it could neither be checked nor acknowledge; the stamper
treats it as not live (`reason=owner_unleased`; `252-research-5` F4). A
delegated identity is mapped to its `parent` before any of this, so a push by
a `claude -p` child is owned by the seat that dispatched it
(`252-research-5` F6). No other liveness source is added; the session
directory (`directory/<name>`) is not read.

**Trust and attestation.** Only the group's leader may write the group record;
only an identity may write its own membership; the push record names its
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
| 1 (this RDR, with RDR-253) | On `member/<id>`, RDR-253 Step 2's fence: the writer's identity token names `<id>` and its current generation. On `group/<name>`, the same two checks against the leader: for an existing record, the writer's identity is the stored record's `leader` and its generation is that identity's current one (so a write that changes the leader comes from the current leader); for the first write, the writer is the `leader` it names and holds a membership in the group. A tokenless write is refused on both, except RDR-253's `unleased` case below | A confused session, and a superseded process of the right identity (after a restart, a resume elsewhere or a take-over), writing group state | One check on `group/*` beside RDR-253's four fenced families |
| 2 (met by level 1) | Only the current process of the leader identity may write | A session that is not the current leader, including a stale process of the leader; not a tenant-token holder who binds the leader's seat | Was "leadership as a claim with `claim_id`, needing a heartbeat"; RDR-253's binding renewal and generation check deliver it, so this RDR adds no separate mechanism for it (`nexus_rdr/252-research-4` F10) |
| 3 (later) | Per-principal identity, host-attested Desktop sessions (JDR-002's own revisit list) | A second human principal or an agent class the tenant does not trust | Engine and auth work beyond this RDR |

Bootstrap: Sam forms a group by asking the identity that will lead it to write
the record. Any holder of the tenant's data token can still bind a seat and
write as it (RDR-253 keeps the tenant as the boundary), so level 1 guards
against confused and stale writers, not a hostile one. A Desktop session's
seat is `unleased` under RDR-253 and writes without a token: it may write its
own membership on RDR-253's `unleased` exception, labelled unattested. This
RDR does not extend the exception to `group/*`: a group's leader must be a
leased seat, since anyone could rewrite a record whose leader writes without a
token. JDR-002 gains a revision entry naming `group/*` as a fenced
family beside RDR-253's; that edit to a joint record is Sam's, drafted with
RDR-253's own JDR-002 entry.

**The push record.** On `PUSH_OK`, on every path of `git-push-develop.sh`
including `--skip-lock`, the script posts to `board/ci/nexus-develop--push`
on the existing `board/ci/<topic>` template (5000 live rows, three days'
retention, the same lifetime as the CI posts it is joined with), with the
identity token the script reads as a Bash child of its session
(`nexus_rdr/252-research-4` F3):

```
dims:  from=<identity id>  kind=push  sha=<tip>
body:  {"base": "<develop sha before the push>", "n": <commits>}
```

The stamper trusts the record's stamp, never `from` (Sam's answer on gate
finding S4): it maps the stamped session id to an identity through the
session index (`nexus_rdr/252-research-4` F4). A record with no stamp, or
whose stamp the index does not map, counts as no record. A push from a plain
shell with no Claude ancestor writes an unstamped record, which counts as no
record. On the `--skip-lock` path the script resolves the installed `nx`
for this write, as the lock path does (`nexus_rdr/252-research-5` F8); if
none resolves, it writes no record and the push still succeeds. It writes a
record only when the pushed branch is `develop`. An unstamped record still
links its `base` to its tip in the chain; only its pusher is unknown. The template's doc block
gains the push script and the stamper as writers, and the `from` doc says
`github`, `engine` or an identity id.

**Develop's state from commit order.** The push records' `base` and `tip`
chain gives the order of develop's tips; arrival time never orders two tips
the chain links. A pushed
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
red:    dims from=engine  kind=owner  sha=<red tip>
        body {"owner": "<identity id>", "next": "<identity id>", "reason": "<why>",
              "suspects": ["<sha>", ...], "suspects_total": <n>}
green:  dims from=engine  kind=clear  sha=<green tip>
```

Rows name identities by id; a reader shows the alias. The template requires
`from` and caps a body at 1024 bytes (`nexus_rdr/252-research-4` F8), so the
stamper's rows carry `from=engine` and list at most the first ten suspects,
with `suspects_total` giving the full count.

- **Suspects**: every push after the last green tip up to and including the
  red tip, since a replaced run leaves a pushed tip untested.
- **Owner**: the pusher of the earliest suspect (Sam's answer on gate finding
  C1). A red that a later push only inherited belongs to the earlier pusher.
  Example: on 2026-11-16, 4554bd15f went red only because da1c285da had; the
  owner is da1c285da's pusher.
- **Next up**, the first that applies: the pusher of the next suspect who is
  not the owner, is live and is a member of `push-train`; the leader, if live
  and not the owner; the first other live member by the time its membership
  row was created. With none, `next` is null and the owner row is the last
  for that red (see Escalation).
- **Leader takes it**, with the reason named: the owner is not live
  (`owner_not_live`), is `unleased` (`owner_unleased`) or is not a member of
  `push-train` (`owner_not_member`; its aura would not load); the earliest
  suspect's record is unstamped or missing, or the red tip is in no push
  record at all, as for a hand push or a PR merged on GitHub
  (`no_record`); and across a chain gap (`chain_gap`). Ordering past a chain
  gap uses the CI posts' times, so rows past a gap are best effort.
- **A stamper error** posts the leader as owner with `reason=stamper_error`.
  If the engine is down, no CI post lands either, so there is no separate
  "stamper down" path. A CI post that never lands (the adapter's write is
  best effort) leaves that run with no owner post; the next run's post
  covers its tip as a suspect.

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

- A new argument source, `self.identity`, filled by the host with the
  session's identity id at load (`[args] me = "self.identity"`). The
  procedure compares it with the owner post's `body.owner` and `body.next`.
  An id never changes across `/clear`, a restart or a rename. It does change
  when the process switches identity (a cross-seat `/resume`, a take-over;
  `nexus_rdr/252-research-5` F5): when the MCP heartbeat adopts a different
  identity, the host re-reads `[load]` and `self.identity`. Load waits for
  the server's bind; a server that starts unbound loads no group-gated aura,
  says so in `nx aura list`, and loads them when it binds. A Desktop server,
  which cannot resolve its identity, loads none. It is a
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

**Acknowledgement.** The owner writes `from=<identity id> kind=ack sha=<sha>`
(stamped) to `board/ci/nexus-develop--owner` with an ordinary tool, MCP or a
shell `nx`; either carries the identity. An ack counts when the session index
maps its stamp to the owner identity. The deadline is a timer in the
stamper, in the engine: if no such ack arrives by the deadline counted from
the owner row, the stamper posts a new owner row naming the next-up identity.
After an engine restart the timer is rebuilt from the latest owner row of
each red tip that has no counting ack and no newer owner or clear row after
it, so a restart loses no deadline and revives none that a later row
superseded. The deadline defaults to 20 minutes (Sam, 2026-11-16), set per
group in the group record's `ack_deadline` and overridden per board topic in
`ack_deadline_by_topic`. The research gives no basis for any number, so the default is
calibrated: the push records and acknowledgements give each red's time to
acknowledge, and the default is reviewed against that distribution after the
first 20 reds (or 30 days).

**Escalation.** Owner, then next up, then Sam. The stamper hands a red on at
most twice. When the second named identity also misses its deadline, or when
`next` is null and the owner misses its deadline, the stamper posts a final
owner row naming the leader with `reason=escalated`. The leader's display
says to raise an RDR-245 ask to Sam, and the stamper stops re-issuing rows
for that red. Unvalidated: the research found no protocol to copy.

**Claims.** Only for work no owner post names, such as rerunning a flaky shard;
RDR-211's queue as is.

**If the platform narrows.** If the read-only procedure surface shrinks (as
WeakAuras lost combat-state support under Midnight), the owner post still
carries the decision, and a plain display of it needs no procedure.

### Existing Infrastructure Audit

| Piece | Exists? | Use |
|---|---|---|
| Aura parse, trigger, clear, display | Yes (RDR-249) | Adds `[load]`, `self.identity`, a result-driven wake |
| Read-only procedure runtime | Yes (RDR-242) | Formats the display |
| `board/ci/<topic>` template | Yes (RDR-220) | Push records, owner and clear rows, acks (body fields; doc block updated) |
| Identity records, session index, `member/<id>`, the `member` fence | No, RDR-253 Phase 1 Steps 1 and 2 | Liveness, the stamp-to-identity map, memberships and their writer check |
| Identity token for Bash children | No, RDR-253 Phase 1 Step 3 | Stamps the push script's record and a shell ack |
| Push script | Yes | Writes the push record |
| `sender_session` stamp | Yes, as a label (JDR-002; engine-service-v0.1.163) | Mapped to an identity; level 1 checks it on `group/*` |
| Groups | No | One new engine template, `group/<name>` |
| Owner stamper and its deadline timer | No | New, in the engine |

### Decision Rationale

A stamped owner over every session computing it, because the platform deciding
once is what private auras do and leaves nothing for sessions to disagree on.
A computed owner over claims, because the record names the owner and a claim
only adds a race. Groups as the base, because the push train, windows, review
pairs and the release crew are already groups, and Load, leader calls and
ready checks are all group mechanics. Members by identity, because RDR-253's
seat outlives every session and its binding says whether it is live.
Local actions only, because procedures
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
  (`reason=stamper_error`). A red still gets no owner post when its CI post
  never lands or no live `push-train` record exists.
- **Wrong group write**: at level 1 the engine refuses a member write not
  from the member's own identity at its current generation, a group write not
  from the leader's, and a tokenless write to either (bar RDR-253's
  `unleased` exception). A tenant-token holder can still bind a seat and write
  as it (JDR-002, RDR-253); level 3 is later work.
- **Wrong pusher**: the stamper uses the push record's stamp, never `from`; an
  unstamped or unmapped record counts as no record and the leader owns the red.
- **Stale load** (a session's server started before its identity joined): it
  loads the aura at its next start. The stamper already sees it as a member
  and may name it owner, so until then it shows no block for its own red, and
  next up is named when the deadline passes. `nx group join` says to
  reconnect the MCP server.
- **RDR-253 slips**: every step here depends on RDR-253 Phase 1 Steps 1 to
  3 (Step 1 on its Steps 1 and 2, Steps 2 and 4 on its Step 3, Step 3 on its
  Steps 1 and 2); none starts before those land.

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
- [ ] RDR-253 Phase 1 Steps 1 to 3 landed: identity records and the session
  index, the `member` fence and identity tokens, and the client resolver
  with Bash-child token files. Two requirements on them, passed to those
  steps: the `member` template refires on binding renewal (Step 1), and an
  identity-token write's stamp is a session id the session index maps
  (Step 2).

### Minimum Viable Validation

With `ci_develop_red` loaded in three members of a test group (A pushed commit
X, B leads, C pushed nothing) and one non-member D: a red on X wakes A as owner
and B as next up, shows C "hold your pushes" with no wake, and does nothing in
D. The green that follows clears every member's block, whichever of the red
and the green arrives first. A late green on an older tip does not clear it,
and a late red on an older tip raises nothing. With two pushes X then Y and
X's run replaced, a red on Y names X's pusher as owner and Y's as next up. With
A's identity lapsed, the owner post names B and B is woken; with no ack by the
deadline, the next up is named. After A runs `/clear`, a red on X still names
A and wakes it. A group write from C is refused.

### Phase 1: Code Implementation

Steps 1 and 3 are engine work and ship in one engine release, which Sam
cuts; Steps 2 and 4 are client work and ship in a conexus release that pins
that engine.

#### Step 1: Group template, with the level-1 writer check

Engine, after RDR-253 Steps 1 and 2: the `group/<name>` template (keyed on
the name, 30-day retention, refired by the leader, updated by take and
replace, deadline values bounded to 5 to 240 minutes), and the level-1 check on it (the writer's identity is the stored
record's leader at its current generation; the first write's writer is the
leader it names), built on RDR-253's identity token and fence code.
Memberships use RDR-253's `member/<id>` and its fence unchanged. The
`board/ci/<topic>` template's doc block (an engine resource) names the new
writers (the push script, the stamper) and the body fields of push, owner,
clear and ack rows. JDR-002's revision entry (Sam's edit) lands with it.

#### Step 2: Push record in `git-push-develop.sh`

Client, after RDR-253 Step 3: one `nx tuple out` to
`board/ci/nexus-develop--push` (with `--key topic=nexus-develop--push`) after
`PUSH_OK` on every path, never failing the push, stamped through the token
file the script's `nx` reads; on `--skip-lock` the installed `nx` is
resolved for this write; only for a push to `develop`.

#### Step 3: The owner stamper, its deadline timer and the owner and clear rows

Engine: on a `--done` write, develop's state from the push chain, stamps
mapped to identities through the session index (a delegated identity mapped
to its parent), liveness through RDR-253's lapse-applying read, the suspect set (at most ten
listed), owner and next up, the leader's cases, owner and clear rows with
`from=engine`; the acknowledgement deadline (group default, per-topic
override) as a timer rebuilt after a restart from unsuperseded owner rows; a
stamper error posting the leader; the named reasons; at most two hand-offs,
then the `escalated` row; no rows while `push-train` is missing or
disbanded.

#### Step 4: `[load]`, `self.identity` and the result-driven wake; `ci_develop_red` moved onto the owner post

Client, after RDR-253 Step 3: `[load]` read from `member/<id>` at server
start, with one `group/<name>` read per named group, waiting for the bind and
re-read when the heartbeat adopts a new identity; `nx group join <name>`
(refused for an ephemeral or delegated identity) and `nx group leave
<name>`; the MCP server refiring its memberships, and the group it leads, at
each binding renewal; `nx group list <name>` showing the leader and members
by alias (an ephemeral by its display name) with their RDR-253 state; the
`self.identity` argument source; `display.channel` naming a result field; `ci_develop_red`
triggering on `kind=owner` and clearing on `kind=clear`; re-surfacing on a new
owner row; the calibration read (time to acknowledge per red) for Day 2.

### Phase 2: Operational Activation

#### Activation Step 1: Form `push-train` and turn the aura on

Sam's go: halcyon writes the `push-train` record as its leader, members join,
then `nx config set auras.ci_develop_red=on` and a restart round.

### Day 2 Operations

- `nx aura list` shows, per aura, loaded or not and why (which group).
- `nx group list` shows the leader and members by alias, each with its RDR-253 state (`unheld`, `live`, `lapsed`, `ended`, or `unleased`).
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
- Owner identity lapsed, owner not a member, no stamped or no mapped push
  record, a chain gap, a stamper error: the leader owns, with the reason.
- The deadline: no ack names next up; an engine restart keeps the timer.
- Next up is never the owner, and skips a lapsed pusher or a non-member.
- Identity across `/clear`: a push before and an ack after `/clear` both map
  to the same identity; the owner is still woken.
- Body cap: a red with more than ten suspects lists ten and the true
  `suspects_total`, within 1024 bytes.
- Per-topic deadline: `ack_deadline_by_topic` overrides the group default.
- Writer check: a membership written by another identity, a group write by a
  non-leader, a group write by a superseded process of the leader, and any
  tokenless write to a leased target are refused; a write for an `unleased`
  target is accepted and labelled unattested.
- Disbanded or missing group: with `ended_at` set, `ends` passed or no
  record, the stamper posts no owner row and `[load]` does not load.
- Retention: a seat's membership and its led group still read after a time
  longer than the templates' retention, with renewals in between; without
  renewals they expire.
- Liveness: a seat whose last renewal is over 300 seconds old reads as not
  live even though no lapse row was written; an `unleased` owner hands the
  red to the leader (`owner_unleased`).
- Delegated pusher: a push by a `claude -p` child is owned by its parent
  seat.
- Unrecorded tip: a red on a tip in no push record goes to the leader
  (`no_record`).
- Escalation: two missed deadlines give one `escalated` row and no more.
- Identity switch: after a cross-seat `/resume`, `self.identity` and the
  loaded auras follow the new identity; an unbound start loads none until
  it binds.
- `nx group join` refuses an ephemeral identity; `join`, `leave` and
  `list` round-trip.
- Stamp mapping: the stamper maps a stamp to an identity through the
  session index, and an unmapped stamp counts as no record.
- Push script: a `--skip-lock` push writes the record; a push to another
  branch writes none.
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

The subspaces agree everywhere: `group/<name>` (new template) and RDR-253's
`member/<id>`, push records on `board/ci/nexus-develop--push`, owner, clear and
ack rows on `board/ci/nexus-develop--owner`, all on the existing
`board/ci/<topic>` template with new fields in the body. The owner rule
(earliest suspect), the leader's cases and the three display states read the
same in Technical Design, the MVV and the Test Plan. The engine-client split
is stated once in Phase 1 and matches the steps.

### Assumption Verification

All four Critical Assumptions are settled: 1 by source and a spike
(`nexus_rdr/252-research-1` F4, `252-research-2`); 2 and 3 by design; 4 by
design (`252-research-4` F5, `252-research-5` F3, F4). Corrected after gate round 1: CI does not run once
per pushed tip (`252-research-3`), so the owner rule works on a suspect set.
Re-keyed after the round 1 fix check: memberships, owners, liveness and the
stamp's meaning come from RDR-253 as accepted (`252-research-4`), so a
session's identity across `/clear` is RDR-253's design, not an inference
here.
Unvalidated by design and said so: escalation and the 20-minute deadline
(calibrated after use).

### Scope Verification

In scope: groups and members, push records, the stamper with its deadline
timer, owner and clear rows, `[load]`, two aura-surface additions,
acknowledgement with a per-topic deadline override, attestation level 1
(which with RDR-253's generation check also covers level 2's target). Out:
typed assignments, attestation level 3, the identity records themselves
(RDR-253), any aura that writes shared state, a re-load hook. The MVV
exercises the core path once; the Test Plan covers the rest.

### Cross-Cutting Concerns

Trust: level 1 checks ownership and generation on `group/*`, beside
RDR-253's fence on `member/*`, against JDR-002's "never a refusal"; Sam's
revision entry there names it. A Desktop seat is `unleased` and writes on
RDR-253's exception, labelled unattested.
Release: Steps 1 and 3 need an engine release, Steps 2 and 4 a conexus
release that pins it. Noise: no wake for everyone else. Data lifetime: push,
owner and clear rows share the CI posts' three-day retention; a group record
lives until disbanded; a membership lives until its identity leaves the
group, or with an ephemeral identity until RDR-253's retention removes it.
Dependency: every step waits on RDR-253 Phase 1 Steps 1 to 3.

### Proportionality

One new engine template, one stamper with a timer, and two aura-surface
additions serve every red. The alternative of convention only (Alternative 3)
leaves every reader to catch a late post, and `ci_develop_red`'s live run on
2026-11-16 saw two in one afternoon (a red for 3b580d898 after a newer green,
and a red for a708b957e after a newer green). Typed assignments,
which nothing here would read, were cut at gate round 1 (Sam's answer S5);
attestation level 2 is met by RDR-253's generation check rather than built
here.

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
and liveness (met first by naming members by session name over the session
directory, now by RDR-253's identity); S4, trust the engine's stamp only.

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
- 2026-11-16: re-keyed onto RDR-253 identities and the round 1 fix check's defects fixed (`nexus_rdr/252-research-4`; fix check `252-fix-check-cee51c460fc1`): members are RDR-253's `member/<id>`, permanent with the identity, so nothing decays; liveness from the identity state; push records and acks stamped through a Bash child's token file and mapped through the session index; owner, next up and leader as identity ids; `self.identity`; level 1 as an ownership and generation check on `group/*`, which also meets level 2's target; the per-topic deadline override (Sam, by picker); chain-gap wording; at most ten suspects in a 1024-byte body and `from=engine` on stamper rows; next up distinct and live; the timer rebuilt only from unsuperseded owner rows; best-effort CI posts stated; the group check reads the stored leader; disbanded groups enforced; the template doc edit moved to engine Step 1; Steps 1, 3 and 4 wait on RDR-253 Phase 1 Steps 1 to 3.
- 2026-11-16: fix check `252-fix-check-96cc049e8c53` passed 2-1; its counted items fixed (`nexus_rdr/252-research-5`): memberships and the group record refired at binding renewal under finite template retention, a requirement passed to RDR-253 Step 1; liveness through RDR-253's lapse-applying read, `unleased` owners handed to the leader; `self.identity` and `[load]` follow an identity switch and wait for the bind; the group record's `leader` the one authority, its update by take and replace, a dead leader's group expiring; the stamp's meaning stated as a requirement on RDR-253 Step 2; the per-topic key as the base topic, values bounded; delegated pushers mapped to their parent; named reasons, next-up precedence, at most two hand-offs then `escalated`; unrecorded and unstamped tips; the skip-lock and non-develop push paths; `nx group join`, `leave` and `list` with tests.
