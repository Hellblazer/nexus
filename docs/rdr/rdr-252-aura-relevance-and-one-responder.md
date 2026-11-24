---
title: "Groups, Relevance and One Responder: Raid-Style Coordination for Sessions, with Auras That Load by Membership and Act by Assignment"
id: RDR-252
type: Architecture
status: closed
closed_date: 2026-11-21
priority: medium
author: Hal Hildebrand
reviewed-by: self
created: 2026-11-16
accepted_date: 2026-11-17
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
renames. RDR-253 also has **ephemeral** identities (a session given no seat,
or whose named seat is held elsewhere) and **delegated** ones (a `claude -p`
child); neither lasts beyond its process unless promoted, so only a seat
joins a group. A **group** is this RDR's new term: a named set of identities with a
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

## Post-close amendment (2026-11-22)

**Status.** This RDR was closed early, on 2026-11-21. A post-close audit the next day (T2 `nexus_rdr/252-post-close-audit-2026-11-22`) found three Significant gaps against the Problem Statement, and Sam ruled that the work finishes under this RDR, not a new one (T2 `nexus/sam-ruling-finish-252-no-257-2026-11-22`). Completion is tracked as comments on its epic, nexus-igea9. Under the rule Sam set on 2026-11-22, drafted for CLAUDE.md and not yet in it (T2 `nexus/sam-ruling-nail-252-253-2026-11-22`, item 3: nothing is fixed or done until the check that proves it has run once), each piece below is finished only when its own test or live run has run once; for the every-repository piece that run is a real red on a second repository.

**What was missing at the close.**
1. The acknowledgement said "seen", not "fixing". An owner who pushed the fix without acking was handed off and nearly escalated, a "not mine" ack counted as an ack, and an acked red had no second timer (audit S1).
2. Next up was woken on every red, so halcyon was woken on 9 of 14 (S2).
3. A failed push-record write went to standard error only, and `from` was always `unknown` (S3).
4. "Hold your pushes" was display only: 24 pushes landed inside red windows.
5. Seven owner paths (`no_record`, `chain_gap`, `owner_not_live`, `owner_unleased`, `owner_not_member`, `stamper_error`, `escalated`) had never occurred on the real board.
6. Only nexus could use any of it, though it was never meant to be nexus-only (T2 `nexus/sam-ruling-generalize-252-2026-11-22`).

**What this RDR now covers.**
- *Client and push script* (lands first): the shared commands `nx ci record-push` and `nx ci check-hold`, with `scripts/git-push-develop.sh` as their first caller. A record failure is reported in the push summary, a failed stamped write is retried as a minimal unstamped record, and `from` is the identity id. The hold refuses a push onto an open red unless the pusher owns the red, pushed a suspect, or gives `--red-fix '<reason>'`; the reason is kept in the push record (Sam's ruling, 2026-11-22). `nx ci ack --not-mine` posts a `disclaim` row, which the engine records only until the engine cut below acts on it. Each of the seven owner paths has a row in the identity journeys, which run a local engine with hand-posted push and CI rows; that proves the stamper's logic end to end, and the first real occurrence on the board is still the live check.
- *Engine* (shipped in engine-service-v0.1.201): the current owner's stamped push, made after the red was reported and chained from a red tip, counts as an ack and moves the timer to the stall deadline; the same push by an earlier owner of the red or by any other live member of its group is no ack, but holds the current owner's hand-off and escalation until that pushed commit's own CI verdict arrives, then allows one more deadline from that verdict; the owner's `disclaim` hands the red on at once; an acked red still open `stall_factor` times the deadline after the last ack or push escalates (a group setting, default 3; the row carries `stalled: true`, `red_since` and `open_minutes`, plus `stalled_owner`, `stalled_since` and `stalled_sign` (`ack` or `push`) when an ack or push came before it), and any red still open at twice that from its start escalates whatever the pushes; one `nudge` row at half the deadline names next up when nobody has acked; and the stamper counts tips that have no push record and tips whose record names no pusher (`unrecorded_tips` and `unattributed_tips` on the group read). The notice change that wakes next up only on a nudge ships with this cut, because without the nudge an absent owner's red would wake nobody else for the full deadline.
- *Every repository.* The engine needs no change: the stamper takes any `board/ci/<base>`, a group's `serves` lists its bases, and the board package names no repository. No shared command defaults its base to nexus. Each takes the repository from the origin remote's name, lowercased as the adapter does: `nx ci record-push` and `nx ci check-hold` add the branch being pushed; `nx ci ack` uses that repository's one open red; `nx ci wait` uses `develop` if the remote has it, else the remote's default branch. Each refuses, naming its flag (`--base`, `--repo` or `--branch`), when it cannot derive one. The rest:
  1. *Aura per base.* The aura grammar takes one concrete subspace. Both shipped auras, `ci_develop_red` and `ci_develop_nudge`, get a placeholder trigger, `board/ci/{serves}--owner`, which the loader expands into one aura per base in `serves` of each live group the session belongs to. Their notice texts name the branch, so they no longer say "Develop".
  2. *Group.* One group per repository by default. A group may serve several bases. In a group of one the leader takes every red; with two or more, each pusher owns the reds its push caused and the leader takes only the fallback cases.
  3. *CI results.* No workflow step is needed. The CI board adapter (a GitHub webhook in the conexus repository) already names the topic from the repository and branch. Adding a repository means adding it to the adapter's `repos`, widening the adapter's read-only token, creating that repository's webhook, and, for a branch other than develop or main, widening the adapter's one branch list, which applies to every repository. All four are cloud or credential steps and Sam's. Every webhook shares one secret, so rotating it means updating every repository's hook. The adapter counts only push-event runs on an allowed branch.
  4. *Push records and the hold without a push script.* A managed `pre-push` hook calls `nx ci check-hold`. Git has no post-push hook, so the same hook starts a detached job that waits, for at most ten minutes, until the remote branch shows the pushed tip and only then calls `nx ci record-push`; a refused push gets no record. `nx upgrade`'s hook refresh today rewrites only post-commit, post-merge and post-rewrite hooks, and never installs a missing hook, so it gains this hook type and an install step. A push from a machine without nx records nothing; the stamper counts that gap. For a repository that merges pull requests into its integration branch there is no local push to the branch, so until the adapter names the merger, such a red goes to the leader as `no_record`, as the owner rules above say.
  5. *First repositories.* recording-rig first: it is Sam's own, its one cheap workflow runs on push to main, it already has managed hooks, and it tests a `main` branch and PR merges. ART second: its Java CI runs on every push and it has no managed hooks, so it tests the hook install. conexus and qwen-coprocessor-stack wait: their CI runs only on pull requests, so they would post no results until a push trigger is added, which costs CI minutes and is Sam's call.

**Amendment (2026-11-24, nexus-igea9), the template as built.** The wording above is read with these clauses; each matches the reviewed code.
- A `{serves}` template takes no `[load]` table (a template that has one is refused). It expands over every live group in the session's memberships that serves at least one base, one aura per base, named `<aura>@<base>`, each with its own group's verdict and its own threads block (`{branch} CI red ({base})`). A concrete aura keeps `[load]`. A base two groups both name is expanded once, for the group first by name.
- The branch is the part of the base after its last hyphen. A branch that itself holds a hyphen reads as its last word; the adapter's branch list can be widened (item 3), and no row or group field carries the branch. A base whose branch the procedure would refuse (not a letter or digit followed by letters, digits, dot, dash and underscore, or over 32 characters), or that holds a colon, is left out at load and recorded as `bad_base`.
- A member group whose record cannot be read is skipped while the readable ones load. It is recorded as `<aura>@group:<name>` with the reason `group_unreadable`, and `nx aura list` shows it as `not read: group <name> (reconnect)` beside the loaded line.
- `nx aura check` validates a template against the groups the checking identity belongs to. A session in no serving group is a warning, not a refusal; a base whose expansion is rejected is a refusal.

**Amendment (2026-11-24, nexus-igea9), the pre-push hook as built.** Item 4 and item 5 above are read with these clauses; each matches the reviewed code.
- The hook acts on pushes to `develop` and `main` only: the stanza is built from `ci_wait.BOARD_BRANCHES`, a client mirror of the adapter's branch list (item 3), which lives in the conexus repository. Widening the adapter's list does not widen the hook; the client list changes in the same release, and nothing checks that the two agree. A push to any other branch is neither checked nor recorded.
- `nx upgrade` installs the hook only in a catalog-registered repository whose base (`<repo>-develop` or `<repo>-main`, from the origin URL, named as the adapter names a topic) is in the `serves` of a live group the upgrading identity belongs to, and only when the repository's hooks directory (`core.hooksPath` included) lies inside that repository, so a shared hooks path is never armed by one repository's group. A group's `serves` therefore turns the hook on at each member's next `nx upgrade`, and removing the base from `serves` does not remove the hook. A process that holds no identity belongs to no group and installs nothing; one whose groups cannot be read installs nothing and says so. A stanza already in a repository is rewritten whoever serves it. `nx hooks install --pre-push` installs only this hook, in any repository, and `nx hooks uninstall --pre-push` removes only it.
- The detached job waits for the `git push` that ran the hook to exit, then looks at the remote branch for up to 30 seconds (`NX_PRE_PUSH_GRACE_S`), and calls `nx ci record-push` when it shows the pushed tip. The whole job is bounded at ten minutes (`NX_PRE_PUSH_WAIT_S`) and every remote call has its own watchdog, so a push still running after ten minutes is not recorded. When the tip never shows, the job logs one line to `pre-push.log` in the nexus config directory and ends without error.
- ART has two install paths: `nx upgrade` once ART is registered in the catalog and a group Sam belongs to serves its base, and `nx hooks install --pre-push` run in the ART checkout. The first run on ART tests whichever path is taken, so "tests the hook install" in item 5 names both.
- recording-rig's hooks directory is its tracked `.beads/hooks` (`core.hooksPath`), whose `pre-push` is a committed beads hook. The stanza goes right after that file's shebang, ahead of the beads body, so installing it shows as a change to a committed file; the install and the upgrade both say so.

## Problem Statement

### Enumerated gaps to close

#### Gap 1: There is no group, so coordination lives in prose

Sessions already work in groups that form and dissolve. The push train has a
leader (halcyon, set up by Sam on 2026-11-07) and rules in a T2 memo
(`nexus/push-train`). There are also push windows (three sessions pushing in
order), review pairs, a release crew (Sam and halcyon) and RDR co-authors. A
session is often in several at once. None of these exists in the system:
membership, leader and assignments live in messages and prose, so no tool and
no aura can ask "who is in this, who leads it, who owns this task" (built here:
membership and the leader; for a develop red, the owner post; typed assignments
cut at gate round 1). Some
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
  name identities. Memberships are rows in its membership table, with
  routes built in its Step 1 and checked by its Step 2; liveness is its binding state; a push script's write carries the
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
  `outcome` (the tip's own), `branch_outcome` once the adapter's switch is on
  (the tip's runs plus carried-forward results, read first by the stamper) and
  the run (RDR-220). Posts are unattested: the engine records no session for
  the GitHub adapter's writes.

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
- [x] Group membership can be read at MCP server start in a few reads.
  **Status**: met by design. Memberships are rows in RDR-253's membership
  table, read by identity (`GET /v1/identity/<id>/memberships`, built in its
  Step 1; `nexus_rdr/252-research-7` F1), plus one read of a group record
  per group a loaded aura names (`nexus_rdr/252-research-1` F7, `252-research-4` F1).
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
session token. For an identity-token write, RDR-253 now says what the stamp
holds ("With an identity token, a tuple's `sender_session` is the session id of
the identity's newest binding row under the token's generation, so a send after
`/clear` carries the new session id", `rdr-253-session-identity.md:743`), which
is the session id this RDR needs, one the engine's session index
(`session/<session id>`) maps to an identity and generation. This RDR stated
that as a requirement on RDR-253 Step 2 (`nexus_rdr/252-research-5` F7). A **wake** is a channel notice that interrupts an idle
session. A **lease** is a claim on a tuple that lapses unless renewed.

### Technical Design

**Groups.** One engine record per group, `group/<name>`, in a permanent
Postgres table beside RDR-253's identity tables, not in the tuple space:

```
fields: name=push-train  leader=<identity id>
        {"purpose": "develop push order", "requested_by": "<sam or identity id>",
        "formed_at": "<UTC>", "ends": "<UTC or open>", "ended_at": null,
        "ack_deadline": 20, "ack_deadline_by_topic": {}, "serves": ["nexus-develop"]}
```

`ack_deadline` is the group's default in minutes; `ack_deadline_by_topic`
optionally overrides it for one board topic, keyed by the topic's base name,
the part before `--` (`nexus-develop` covers `--done`, `--push` and
`--owner`). An aura triggers on one topic family, so this is the per-aura
override in a form the engine stamper can read (Sam, 2026-11-16,
`nexus_rdr/252-research-4` F9). `ack_deadline` and every
`ack_deadline_by_topic` value is a whole number of minutes from 5 to 240; the
group route refuses others (a tuple template could not check a body field;
`252-research-6` F4). The deadline that governs a red is that of the group
that serves its topic, overridden for the topic when `ack_deadline_by_topic`
names it; its keys must be topics in `serves`, checked on every write, and
removing a topic from `serves` removes its key.

The group record is permanent: a re-send of a tuple row whose template is
not lock-flagged is clamped to its creation time plus the retention
(`nexus_rdr/252-research-6` F1), so a group lives in a table, as RDR-253's
identities do (`252-research-6` F3, `252-research-7` F1). A group name follows the
engine's address-segment pattern (`[A-Za-z0-9][A-Za-z0-9._-]*`, at most 64
characters, the bound RDR-253 Step 1's membership routes already apply;
`nexus_rdr/252-research-8` F3). The first write is an insert that fails if a live record has
the name, so two concurrent formations leave one; it also writes the
leader's membership, in the same transaction. A disband removes every
membership of the group, the leader's included, inside the group route's
transaction; this cascade is the engine's own and is exempt from the
membership check and the leader-remove rule, as RDR-253's pruning is. A join
to a disbanded group is refused. `ended_at` is set once and cleared only by
a re-form. A disbanded name is re-formed by the former leader while it is
live, or by any live, leased seat when it is not; the engine locks the
record row, treats the re-form as a first write (the writer names itself
leader and becomes the only member, and every field is the form call's), and
writes the disband and the re-form to `gc_audit` with the old record's
fields, so the history survives. The leader
of a first write, and any new leader, must be a live, leased seat and, for a
new leader, a member of the group. An update (a new leader, `ended_at`, a
deadline, `serves`) changes the one row in place, by the current leader,
under the level-1 check. When the leader identity is `ended`, `unheld`,
`lapsed` with a recorded `gone`, or in state `live` with lease `unleased`
(a Desktop holder, liveness unknown, which cannot write a group record), Sam's tenant credential may set a new leader
or `ended_at`; the engine refuses it otherwise, and writes it to `gc_audit`
with operation `group_override`, in the same transaction, as RDR-253's purge
does.

The group record's `serves` names the board topic families whose reds it
owns (`["nexus-develop"]` for `push-train`). The stamper finds the group for
a `--done` write by the topic's base name, so no repository name is coded in
the engine; a topic no live group serves gets no owner rows. A topic is
served by at most one live group: each `serves` entry is a base topic name
(the address-segment pattern, no `--`), and every write that can make a group
serve a topic (form, re-form, `set --serves`, `set --ends`; nothing else can
clear `ended_at`) takes the stamper's per-topic advisory lock for each topic,
in sorted order, and refuses a second live server.

Membership is a row in RDR-253's membership table (the RDR's `member/<id>`),
one per group the identity belongs to: `group=<name> role=<leader|member>` (`nexus_rdr/252-research-4`
F1). `role` is informational; the group record's `leader` is the only
authority on who leads, and nothing reads `role`. It is written when the
identity joins and removed when it leaves, and it is permanent: like the
group record, it is a row in a permanent table owned by RDR-253's identity
repository, with no retention, so a member never decays out of its group
(`nexus_rdr/252-research-6` F1, F3). RDR-253's pruning of an ephemeral or
delegated identity removes its rows. RDR-253's fence table puts `member/<id>`
in the tuple space; its Step 1 as built moved it to a table
(`nexus_rdr/252-research-7` F1), and RDR-253's wording is amended to match
once that code is reviewed. The repository lists
memberships by identity and by group, in the order they were created.
RDR-253 Step 1 builds the add, remove and list routes for these rows
(`nexus_rdr/252-research-7` F1), and RDR-253 Step 2 puts its ownership and
generation check on them. This RDR's Step 1 adds three group rules to the add
and remove routes: adding a membership in a group that has a record requires
a seat, adding one to a disbanded group is refused, and removing the current
leader's membership is refused; the leader hands the group on first. `nx group join` and `leave` call those routes. It needs no re-pointing across `/clear`: the identity stays and only its session
id changes (Sam's answer on gate finding S3, "re-point and liveness", met by
the identity). Keying it by identity makes "which groups am I in" one read at
server start. A group is disbanded by setting `ended_at`; the record stays
until the name is re-formed, and `gc_audit` keeps the history. The stamper and the load stage treat a group as disbanded once
`ended_at` is set or `ends` has passed, and treat a missing record the same
way: with no live record serving a topic the stamper posts no owner rows for
it, including hand-off and `escalated` rows for a red already open, and no
session loads an aura gated on that group. Clear rows are posted on a green
whatever the group's state for a topic with an open owner row, so a block
raised before a disband still clears; a topic with no open owner row gets
none.

There are no typed assignments in this RDR (Sam's answer on gate finding S5:
nothing read them; a later RDR can add them with a reader). The leader is the
group's one role.

First groups, made explicit from what runs today: `push-train` (leader
halcyon), and ad hoc push windows, review pairs and the release crew as
needed. The restart script's ready check becomes a group operation:
`claude-restart-sessions --ask-ready --group X` asks, and `--status --group X`
reports, only the live sessions whose names are the aliases of X's live
members, read from the engine roster (`nx group list X --json`). It prints how
many members it covers and names each one it does not, with its state and why
(no alias, no live session here, a Desktop binding or session, left out by
`--skip` or `--only`, or not on this tmux server), so a ready exit cannot hide
who was never asked. The restart itself never takes a group, and a group round
replaces the round file, so a restart after it still refuses for every session
the group round never asked (shipped in v8.5.3).

**Liveness.** The stamper asks RDR-253's liveness read, which applies the
300-second rule to the last renewal rather than trusting a stored `live`
(`nexus_rdr/252-research-5` F3). An identity is **live** when that read says
`live`. An `unleased` (Desktop) identity's liveness is unknown and its writes
are unstamped, so it could neither be checked nor acknowledge; the stamper
treats it as not live (`reason=owner_unleased`; `252-research-5` F4). A
Desktop server knows its identity only when it made the bind itself from the
pending file (`252-research-6` F5); RDR-253 otherwise gives it no token and
no identity. It loads group-gated auras only in that case, and otherwise
loads none and says so in `nx aura list`; when the identity lease later
adopts its first identity, the server gates its group-gated auras once more
(see Load by membership). A
delegated identity is mapped to its `parent` before any of this, so a push by
a `claude -p` child is owned by the seat that dispatched it
(`252-research-5` F6). No other liveness source is added; the session
directory (`directory/<name>`) is not read.

**Trust and attestation.** Only the group's leader may write the group record,
apart from the audited operator override and a re-form of a disbanded name,
both above;
only an identity may write its own membership; the push record names its
pusher by the engine's stamp. JDR-002 sets the starting point: the tenant
(from the bearer token) is the security boundary, `from`, claimant and
directory names are claims, and the engine's `sender_session` stamp
(engine-service-v0.1.163) is "a label, never a refusal". Any holder of the
tenant's data token can mint a session token for any session id, so the stamp
guards against a confused model, not a hostile one. JDR-002 says to revisit
this "in an RDR" when trust needs grow; this RDR is that revisit for group
writes.

One carve-out, this RDR's own resolution of the close critique's S3: the
engine's owner and clear rows on `board/ci/<topic>--owner` carry `from=engine`
and no sender session, and the tuple routes (`out`, including `mode=replace`,
and an `ack` reply) refuse a client write whose `from` dim is `engine` on a
`board/ci/<topic>` subspace, with 403 `reserved_dim`, for every credential. The
stamper writes through the repository, never the route, so `from=engine` is
engine-only by construction. The owner decision also counts a prior owner or
clear row only when its `sender_session` is null (a blank stamp is a stamp), the
test the aura applies; that check alone separates only rows written by a
session-bearing client before the refusal, because a raw-HTTP client with no
minted session also has a null stamp.

| Level | What the engine checks | Guards against | Cost |
|---|---|---|---|
| 0 (today) | Nothing on these subspaces; readers see the stamp as a label | Nothing; a reader may notice a mismatch | None |
| 1 (this RDR, with RDR-253) | On `member/<id>`, RDR-253 Step 2's fence: the writer's identity token names `<id>` and its current generation. On `group/<name>`, the same two checks against the leader: for an existing record, the writer's identity is the stored record's `leader` and its generation is that identity's current one (so a write that changes the leader comes from the current leader); for the first write, and for a re-form of a disbanded name (a first write too), the writer is the `leader` it names, and the write adds its membership. A tokenless write is refused on both, except the audited operator override on a group; RDR-253's `unleased` exception applies to memberships only; on `board/ci/<topic>`, no client write sets `from=engine` | A confused session, and a superseded process of the right identity (after a restart, a resume elsewhere or a take-over), writing group state; a client forging the stamper's owner or clear rows | One check on `group/*` beside RDR-253's four fenced families |
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
on the existing `board/ci/<topic>` template (three days' retention, the
lifetime of the CI posts it is joined with; only a topic ending `--owner`
keeps 35 days). Its 5000-row cap counts each
subspace on its own (`nexus_rdr/252-research-8` F1), so the CI posts on
`board/ci/nexus-develop` never share a cap with `--push` or `--owner`. The
script writes with the
identity token the script reads as a Bash child of its session
(`nexus_rdr/252-research-4` F3):

```
key:   topic=nexus-develop--push   nonce: push-<tip>
dims:  from=<identity id, or unknown>  kind=push  sha=<tip>
body:  {"base": "<develop sha before the push>", "n": <commits>}
```

`from` is the pushing process's identity id, else `unknown` (a plain shell with no Claude ancestor, or
an unreadable token record). It is a label: the stamper never reads
it. A push that overrode the hold below with `--red-fix '<reason>'` adds `"red_fix": "<reason>"` to the
body, so the board shows every escape.

The nonce is required by the template's `keys+nonce` identity, and a retry of
one push lands on the same tuple. The script releases the push lock before it
writes the record and stops the write after 20 seconds
(`NX_PUSH_RECORD_TIMEOUT_S`), so a slow tuple space never holds a push. A record that is skipped (no
installed `nx`), or that fails, is one line in the push summary on standard output after `PUSH_OK`
(`PUSH_RECORD_SKIPPED`, `PUSH_RECORD_FAILED`, or `PUSH_RECORD_UNSTAMPED` for a record written with no session stamp) as well as on standard error. An installed `nx` that predates `nx ci record-push` gets the older direct `nx tuple out` write (`PUSH_RECORD_LEGACY`). A failed stamped write is
retried once as a minimal unstamped record (`base`, tip, `from`; the session header dropped, the same
nonce), reported as `PUSH_RECORD_FALLBACK`, so the chain keeps its link; only when that fails too is
the record missing. The push never fails because of the record.

The stamper trusts the record's stamp, never `from` (Sam's answer on gate
finding S4): it maps the stamped session id to an identity through the
session index (`nexus_rdr/252-research-4` F4). A record with no stamp, or
whose stamp the index does not map, counts as no record. A push from a plain
shell with no Claude ancestor writes an unstamped record with `from=unknown`. The fallback
above is unstamped too, so it counts as no record for ownership; its `from` is the identity id as usual. On the `--skip-lock` path the script resolves the installed `nx`
for this write, as the lock path does (`nexus_rdr/252-research-5` F8); if
none resolves, it writes no record and the push still succeeds. It writes a
record only when the pushed branch is `develop`. An unstamped record still
links its `base` to its tip in the chain; only its pusher is unknown. The `board/ci/<topic>`
template's doc block gains the push script and the stamper as writers, and the `from` doc says
`github`, `engine`, `unknown` or an identity id.

**Develop's state from commit order.** The push records' `base` and `tip`
chain gives the order of develop's tips; arrival time never orders two tips
the chain links. A pushed
tip need not have a run of its own: develop CI runs in concurrency slots
where "a newer push replaces only that slot's pending run"
(`.github/workflows/ci.yml:20-40`; `nexus_rdr/252-research-3`), and a
replaced run posts `outcome=cancelled`. So the stamper treats only
`success` and `failure` as verdicts, and a tip's verdict is develop's state at
that tip, not only the tip's own runs: the `--done` row's `branch_outcome` dim
(the tip's outcome folded with the latest earlier result of each workflow that
did not run on the tip, such as the path-filtered Service CI; the adapter's
FOLD_BRANCH_OUTCOME switch) is read ahead of `outcome`, so a clear needs every
workflow's latest result green. A per-sha failure stays a failure whatever
`branch_outcome` says, and a `branch_outcome` outside success, failure and
cancelled is no verdict. A row without `branch_outcome` whose body carries a
null `carried` (the carry read failed), a `carried` list with an entry whose
conclusion is not `success` or cannot be read, or a `carried_n` above the
list's length (entries trimmed; an absent list counts as empty) has no verdict
when its per-sha result is success; a body with neither `carried` nor
`carried_n` reads as before. A carried entry passes only on `success`: a
skipped or neutral latest run of a workflow must not clear an earlier red,
while the sha's own runs keep `success`, `neutral` and `skipped`. A workflow
whose develop push runs start concluding skipped or neutral would therefore
hold every red open until escalation, so any future job-level `if:` that skips
a whole workflow on push has to be weighed against it; none of the last 100
completed develop push runs concluded skipped or neutral in any workflow
(measured 2026-11-20). Nothing in this repository watches that assumption. The engine logs
`board_done_no_verdict ... reason=carried_not_green` at info for a sha row that
leaves no verdict (`OwnerStamper.afterOut`), and no lint, test or alert in this
repository reads it; `scripts/check_release_ci_evidence.py` and `tests/test_ci_wait.py`
handle skipped and neutral conclusions for other jobs and do not touch the
carried list. The adapter that writes `carried` is not in this repository (a
`git grep` for `FOLD_BRANCH_OUTCOME` finds only this RDR,
`docs/agents/worktrees.md`, `board-ci.yaml` and `DoneVerdict.java` comments), so
its metrics were not read here; the close critique r3 reads its `FoldCarryFailed`
metric as counting read failures, not carried non-success (the critique's
reading, not read here). The decision is taken at the 2026-12-20 review, which
the threads-board thread "RDR-252 calibration and attribution read" carries
(due 2026-12-20, with the suspect-dropping question below): a lint or an adapter metric on carried non-success conclusions, or the
one-time measurement stands. The engine does not tell workflows apart: it reads
each carried entry's conclusion (`DoneVerdict.carryProblem`), and
`board-ci.yaml` says the fold covers "each workflow that has a push run on the
branch but none on this sha", Service CI as its example. The plugin drift ledger
and `windows-pg-bundle-rehearsal` both run on a develop push under a `paths:`
filter and neither sets `continue-on-error`, so by that wording they count in
develop's state, and a red from either would hold an open red or raise one
(`aBranchOutcomeOfFailureWinsOverAPerShaSuccess`); whether the adapter includes
them is inferred, not read. This RDR does not decide whether they should hold
pushes; that is the same review's call. `outcome` stays the tip's own result. A verdict counts only if its tip is
the newest tip in the chain with a verdict. A late red for an older tip is
history, not a block; a late green for an older tip does not clear a block a
newer red raised. When a push's `base` matches no recorded tip (a hand push, a
release back-merge), the chain has a gap: the stamper orders by the CI post's
own times past the gap, and the owner of a red across a gap is the leader,
`reason=chain_gap`.

**The owner, stamped once.** On each `--done` write, the
stamper (Java, in the engine, run on that write; Sam, 2026-11-16, refining
answer 5 once `nexus_rdr/252-research-1` F5 showed the CI adapter is an AWS
Lambda in the conexus repository, not part of the engine) works out
develop's state and, when it changed, posts one row (an owner row only when a
live group serves the topic; a clear row for any topic with an open owner
row) to
`board/ci/nexus-develop--owner`:

```
red:    dims from=engine  kind=owner  sha=<red tip>
        body {"owner": "<identity id or null>", "owner_alias": "<alias>",
              "next": "<identity id or null>", "next_alias": "<alias>", "reason": "<why>",
              "suspects": ["<sha>", ...], "suspects_total": <n>,
              "episode_at": "<UTC>", "refresh": <bool>, "handoffs": <n>}
green:  dims from=engine  kind=clear  sha=<green tip>
```

Rows name identities by id, and from engine 0.1.198 also by alias: the
stamper reads the owner's and next up's live alias at stamp time, in the
evaluation's own transaction, and writes it as `owner_alias` and `next_alias`.
An identity with no live alias gets no field, and a failed alias read posts the
row with ids alone. Who owns a red is always decided on the ids; a reader
shows the alias when the row carries one, else the id. The template requires
`from` and caps a body at 1024 bytes (`nexus_rdr/252-research-4` F8), so the
stamper's rows carry `from=engine` and no sender session (the route refuses
`from=engine` from a client) and list at most the first ten suspects,
with `suspects_total` giving the full count. The aliases count inside the
1024 bytes: when they would push the body past it, suspects are dropped from
the end, down to one, and `suspects_total` keeps the full count.

- **Suspects**: every push after the last green tip up to and including the
  red tip, since a replaced run leaves a pushed tip untested.
- **Owner**: the pusher of the earliest suspect (Sam's answer on gate finding
  C1). A red that a later push only inherited belongs to the earlier pusher.
  Example: on 2026-11-16, 4554bd15f went red only because da1c285da had; the
  owner is da1c285da's pusher.
- **Next up**, the first that applies: the pusher of the next suspect who is
  not the owner, is live and is a member of the serving group; the leader, if live
  and not the owner; the first other live member by the time its membership
  row was created. With none, `next` is null and the next row for that red
  is the `escalated` one (see Escalation).
- **Leader takes it**, with the reason named: the owner is `unleased`
  (`owner_unleased`), is not live, that is `lapsed`, `unheld` or `ended`
  (`owner_not_live`), or is not a member of
  the serving group (`owner_not_member`; its aura would not load); the earliest
  suspect's record is unstamped or missing, or the red tip is in no push
  record at all, as for a hand push or a PR merged on GitHub
  (`no_record`); and across a chain gap (`chain_gap`). Ordering past a chain
  gap uses the CI posts' times, so rows past a gap are best effort. When
  several reasons hold, the first in this list is posted (`no_record` before
  `chain_gap`).
- **A stamper error** posts the leader as owner with `reason=stamper_error`
  and `next` null. The row runs the acknowledgement deadline like every owner
  row, so a red whose evaluation failed is escalated when the leader does not
  acknowledge; with no next up shown, nobody receives a hand-off first.
  If the engine is down, no CI post lands either, so there is no separate
  "stamper down" path. A CI post that never lands (the adapter's write is
  best effort) leaves that run with no owner post; the next run's post
  covers its tip as a suspect.

The Lambda adapter is unchanged and keeps its write-only `board-ci` scope.
That scope is per template, so it reaches every `board/ci/<topic>` subspace
(`nexus_rdr/252-research-9` F2), and the token lives in a public Lambda. So
Step 1 adds an engine refusal: a `board-ci`-scoped write to a subspace whose
topic ends `--owner` or `--push` is refused with 403, leaving the adapter
only its own posts; owner, clear, ack and push rows come from sessions and
the stamper. A second refusal, for every credential: a write to a
`board/ci/<topic>` subspace whose `from` dim is `engine` is refused with 403
`reserved_dim` on `out` (including `mode=replace`) and on an `ack` reply.
The stamper ships in an engine release. Alternative 2 below keeps "every
session computes" as the fallback design.

**Load by membership.** New optional table in the aura file:

```toml
[load]
group = "push-train"      # loaded only in sessions that are members; must be the group that serves the trigger's topic
```

A concrete aura keeps this table. A `{serves}` template takes none: it loads for every live group the session belongs to, for the bases that group serves.

Load is evaluated when the session's MCP server starts, the only time auras
and config are read today (Sam's answer 3). A server start is not a new
session: the session survives an MCP reconnect (`/mcp`) and a restart, as the
8.3.0 restart round on 2026-11-16 showed. To join or leave a group, a session
writes or removes its membership and reconnects its MCP server. One case is
not left to a reconnect: a server that loaded with no identity gates its
group-gated auras once more when the identity lease adopts its first identity
(from the pending file, or from a token file a hook wrote), and loads the ones
that pass. Every other change waits for a reconnect. Unknown keys are refused
by `nx aura check`.

**Typed displays and noise.** `ci_develop_red` triggers on
`board/ci/<base>--owner` for each base the session's groups serve
(`board/ci/nexus-develop--owner` for nexus) and clears on its `kind=clear` rows, so the
commit-order logic lives only in the stamper. It shows one of three states, plus a fourth for an escalated red,
each with its own urgency, in the manner of DBM's typed warnings:

| State | Display | Channel wake | Local action |
|---|---|---|---|
| Owner | "Develop is red at <sha>, first red in your push. Diagnose it." (for a red the leader takes, the text names the `reason` instead) | Yes | Diagnose; acknowledge |
| Escalated (the identity an `escalated` row names, or every member when `owner` is null) | "Develop is red at <sha>; no one acknowledged. Raise an ask to Sam." | Yes | Raise an RDR-245 ask (with `owner` null, the first member to act raises it; the ask is visible on the board, so the others see it) |
| Next up | "Develop is red at <sha>; <owner> owns it. You are next up." (`<owner>` is the owner's alias, else its id) | Yes | Stand by; take it if not acknowledged |
| Everyone else in the group | "Develop is red at <sha>; <owner> owns it. Hold your pushes." | No (block only) | Hold its own pushes until green; the push script enforces it (below) |

**The hold is enforced** (2026-11-22, nexus-igea9; Sam's ruling on the post-close audit). Before it pushes to
develop, `git-push-develop.sh` runs `nx ci check-hold --base nexus-develop` (a shared command: any repo's push
script can call it, and its `--base` is given or derived from the remote URL and branch, never defaulted), which reads the open red on `board/ci/nexus-develop--owner`
(the split `nx ci ack` uses) and the push records of the red's suspects. The push is refused with
`PUSH_REFUSED_DEVELOP_RED` (exit 13, naming the owner and the red's sha) unless the pusher is the red's owner or
the pusher of one of its suspects (the identity the suspect's push record's sender-session stamp maps to; `from`
is never read), or the push carries `--red-fix '<reason>'`, which is recorded in the push summary and in the push
record body (`red_fix`). A caller with no identity is held like any other. A failed read of the red, of the
records or of the caller's identity is one `PUSH_GATE_UNCHECKED` line on standard error and does not refuse. A refusal names any suspect whose record names no pusher. The script recognises an override only by the `PUSH_RED_FIX_APPLIED` line the command prints, and the reason is cleaned of control characters and cut to fit the record body's 1024-byte cap. The record itself is written
by `nx ci record-push` (the script passes the `--red-fix` reason to it), which owns the stamped write, the
minimal fallback and `from`.

**Two additions to the aura surface** (`nexus_rdr/252-research-1` F1, F2).
An aura cannot tell which state it is in without knowing its own session,
and today an argument can come only from the tuple or a literal. So:

- A new argument source, `self.identity`, filled by the host with the
  session's identity id at load (`[args] me = "self.identity"`). The
  procedure compares it with the owner post's `body.owner` and `body.next`.
  An id never changes across `/clear`, a restart or a rename. It does change
  when the process switches identity (a cross-seat `/resume`, a take-over;
  `nexus_rdr/252-research-5` F5). Load stays at server start (Sam's answer
  3; `252-research-6` F6), so after a switch the loaded auras and
  `self.identity` are the old identity's until the MCP server reconnects;
  `nx aura list` shows the mismatch and says to reconnect. A server that
  starts unbound loads no group-gated aura and says so the same way, and
  gates them once more when the lease adopts its first identity; every
  other change waits for a reconnect. The
  MCP server writes its bound identity and loaded aura set to a state file
  keyed like RDR-253's token files by its Claude process's pid and start
  time (`~/.config/nexus/auras/loaded-<claude pid>.<startedAt>.json`),
  rewritten at load and on each identity change the heartbeat sees, and
  removed when the server exits. `nx aura list` reads the file of its
  nearest Claude ancestor process, as a Bash child's `nx` finds its token
  file, and removes a file whose holder RDR-253's holder check finds gone
  (no registry file with that pid and `startedAt`, or a pid whose OS start
  time differs, which covers an in-place `/restart`). With no Claude
  ancestor, `nx aura list` lists the files without claiming one. It is a
  source, not a reserved argument name, so RDR-242's header grammar is
  unchanged; `nx aura check` accepts it for any declared string argument.
- `display.channel` accepts, besides `true` and `false`, the name of a
  boolean field of the procedure's result (`channel = "wake"`). The host
  sends the fixed-template notice only when that field is `true`, so the
  owner and next up are woken and everyone else gets the block alone. The
  notice stays the fixed template of RDR-249; only whether it is sent varies.

**One evaluator at a time.** Develop CI runs two slots, so two `--done`
writes can land together. The stamper evaluates one topic at a time: it takes
a Postgres advisory lock keyed on the topic, re-reads the push, verdict and
owner rows under it, and posts at most one row before releasing it. The
deadline timer's hand-off takes the same lock, so a hand-off and a `--done`
cannot both post from stale state.

**Re-surfacing.** When the owner changes (the deadline passes, or a newer red
has a different earliest suspect), the stamper posts a new owner row with
`refresh: false` and a new `episode_at`; each member's block is rewritten, and
the new owner gets a wake even if it had already seen the red, as Angry
Assignments re-shows a changed assignment. A newer red whose earliest suspect
is unchanged posts a refreshed row: the new sha, the current row's owner,
`next` and `handoffs` carried over (so a hand-off is kept), the same
`episode_at`, and `refresh: true`. The procedure wakes only on a row with
`refresh: false`. A refresh carries the `reason` of the row it refreshes, so a
refresh after an `escalated` row carries `reason=escalated`. A hand-off row
carries `reason=handoff`.

**Acknowledgement.** The owner posts `kind=ack sha=<sha>` to
`board/ci/nexus-develop--owner` with `nx ci ack <sha7>`, the MCP `tuple_out`
tool or a shell `nx tuple out`; the client sets `from` to the caller's own
identity id (a typed value is replaced; a caller with no identity is refused).
The engine also stamps `from` on every `kind=ack` write to a `--owner` topic,
whatever the client typed, with the identity its sender-session stamp maps to,
a delegated identity as its parent (`AckIdentity`, `Identities.countedIdentity`:
"its parent when it is a delegated identity with one, else itself"); it logs
`board_ack_from_overwritten` when it replaces a non-blank value, refuses
nothing, and leaves `from` as typed when the stamp maps to no identity
(`BoardCiGuard.stampAckFrom`: "With no identity (the stamp maps to none) the
dims pass unchanged, so a `from` that survives is one no counted ack carries").
The stamp is a label for readers and `nx group ack-times`; the engine never
reads `from` to decide: an ack counts when the session index maps its
sender-session stamp to the current owner identity and its `sha` is a red tip
since the last green, so an ack carries across a refreshed row. An ack from
an identity the red was handed away from does not count, and neither does one
whose stamp is null (a tenant credential holds no session) or maps to no
identity, whatever its `from` says; `nx group ack-times` maps an ack's stamp to its poster the same way (a client read of the identity service, not the engine's count). The deadline is a timer in the
stamper, in the engine: if no such ack arrives by the deadline counted from
the row's `episode_at`, the stamper posts a new owner row naming the next-up identity.
After an engine restart the timer is rebuilt from the latest owner row of
each red episode that has no counting ack and no newer owner or clear row
after it, counted from its `episode_at`, so a restart loses no deadline and revives none that a later row
superseded. The rebuild runs five seconds after engine start. If its census does
not complete (the tenant list, which is every tenant holding a service token or
tuple rows, a tenant's subspaces, or a topic's re-evaluation failing), it retries
only what failed after a delay that doubles from ten seconds to a cap of five
minutes, until one completes; the first failure logs its stack and the twelfth
attempt raises the error-level event `board_ack_deadline_rebuild_stuck`. A topic
whose re-evaluation keeps throwing has its deadline armed from its latest owner
row alone, `episode_at` plus the deadline. The deadline defaults to 20 minutes (Sam, 2026-11-16), set per
group in the group record's `ack_deadline` and overridden per board topic in
`ack_deadline_by_topic`. The research gives no basis for any number, so the default is
calibrated: the push records and acknowledgements give each red's time to
acknowledge, and the default is reviewed against that distribution after the
first 20 reds (or 30 days).

**Escalation.** Owner, then next up, then Sam. The stamper hands a red on at
most twice. When the second named identity also misses its deadline, or when
`next` is null and the owner misses its deadline, the stamper posts a final
owner row with `reason=escalated`, naming the leader if live and otherwise the
first live member, with `next` null; with nobody live it names no one
(`owner` null). That row's display, a fourth state, says to raise an RDR-245
ask to Sam: to the identity it names, or, with `owner` null, to every member,
each woken. Board subscriptions begin when a server starts, so a server that
starts after the row does not see it (`nexus_rdr/252-research-9` F1); it shows
the next owner, refresh or clear row. The stamper posts no hand-off and no
second `escalated` row after an
`escalated` one, and the timer rebuild skips a red whose latest row is
`escalated`. If the evaluation at a deadline throws, the stamper posts the
`escalated` row from the current owner row alone: the leader if live, else the
first live member, else no one, with `handoffs` one higher, the row's suspects
and no deadline; an ack from the row's owner on the row's own tip ends it.
Matching that ack and finding who is live both read the identity service. If
that read fails and the owner's stamped ack, on the row's own tip and posted at
or after the row, exists (an ack row with a sender session whose `from` is the
row's owner; the engine stamps `from` with the identity of the sender session
when it writes the ack), the red may be acknowledged and cannot be matched
without the read, so the stamper posts nothing and the deadline timer retries after 30
seconds, without end; the twelfth failure of a topic's hand-off since its last success, cancel or eviction, and
every twelfth after it, raises the error-level event `board_ack_deadline_stuck`
(a log event; it posts no row). If that read fails and no such ack row exists,
the owner has not acknowledged, so the `escalated` row is posted at once with
`owner` null, as for nobody live. A non-owner's ack, or an ack with no sender
session, does not hold the escalation off. The remaining residual is a minted
session with no identity that hand-types the owner's id. Known transition: a
pre-0.1.200 ack row has a typed `from` (perhaps an alias) and does not match,
so under the compound fault (the evaluation and the liveness read both down) an
open red acknowledged before the deploy escalates with `owner` null, a bounded
false wake. This check is narrower than the normal deadline count: an owner's
ack posted before its owner row (the bound at or after the row is kept), or on
an older tip, makes the fail-safe escalate where the normal path would count
it, the safe direction, a false wake and never a suppression.
Unvalidated: the research found no protocol to copy.

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
| `board/ci/<topic>` template | Yes (RDR-220) | The `--done` CI posts the stamper reads; push records, owner, clear and ack rows on their own subspaces (body fields; doc block updated) |
| Identity records, session index, the membership table and routes, and their check | No, RDR-253 Phase 1 Steps 1 and 2 | Liveness, the stamp-to-identity map, memberships and their writer check |
| Identity token for Bash children | No, RDR-253 Phase 1 Step 3 | Stamps the push script's record and a shell ack |
| Push script | Yes | Writes the push record |
| `sender_session` stamp | Yes, as a label (JDR-002; engine-service-v0.1.163) | Mapped to an identity; level 1 checks it on `group/*` |
| Groups | No | A new group table, its routes and the level-1 check |
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
  never lands or no live group serves the topic.
- **Wrong group write**: at level 1 the engine refuses a member write not
  from the member's own identity at its current generation, a group write not
  from the leader's (bar the audited override and a re-form of a disbanded
  name), and a tokenless write to either (bar RDR-253's
  `unleased` exception, for memberships only). A tenant-token holder can still bind a seat and write
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

- [x] Sam accepts the model (groups, stamped owner, three states and the escalated one, local
  actions) and answers the open questions (2026-11-16; gate round 1 answers
  the same day).
- [x] The `push-train` group's shape agreed with halcyon. Evidence: halcyon
  formed it on 2026-11-20 (`nx group list push-train --json` read 2026-11-20:
  `"formed_at": "2026-11-20T05:19:53.537938Z"`, `"leader": "i12"` (halcyon),
  `"serves": ["nexus-develop"]`, `"ack_deadline": 20`, `"live": true`; the
  members joined by 05:20:31Z; plan in T2 `nexus/rdr252-activation-plan-852-2026-11-20`).
- [x] RDR-253 Phase 1 Steps 1 to 3 landed (evidence: RDR-253's Revision History
  of 2026-11-17, "wording amended to match the reviewed Step 1 code" (nexus-hczdo.1.1),
  "Step 2 code" (nexus-hczdo.1.2) and "Step 3 code" (nexus-hczdo.1.4), each a
  grant 3 amendment): identity records and the session
  index, the membership table with its routes and check, identity tokens, and the client resolver
  with Bash-child token files. Requirements on them, passed to those steps:
  memberships in a permanent table listed by identity and by group (Step 1);
  an identity-token write's stamp is a session id the session index maps,
  including a delegated child's session id, mapped to the delegated identity
  (Steps 1 and 2; `252-research-6` F7).

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

#### Step 1: Group table and routes, with the level-1 writer check

Engine, after RDR-253 Steps 1 and 2: a Liquibase table for groups and routes
to form, update, disband and read a group (members in join order come from
RDR-253's roster route); the name pattern; `serves`, one live group per
topic; a disbanded name re-formed in place; the seat rule on membership adds
and the leader rule on removes; the first write an insert that fails on an existing name and adds the
leader's membership; updates in place; deadline values bounded to 5 to 240
minutes; a first or new leader a live, leased seat (a new leader also a
member); the operator override with the tenant credential, allowed only in
the states named above and written to `gc_audit`; the `serves` checks
under a per-topic advisory lock; re-forming and disband as above; the refusal of a `board-ci`-scoped write
to a `--owner` or `--push` subspace, and of any client write that sets
`from=engine` on a `board/ci` topic; and the level-1 check (the writer's identity is
the stored record's leader at its current generation; the first write's
writer is the leader it names), built on RDR-253's identity token and fence
code. Memberships use RDR-253's permanent membership rows and their fence. The
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

Engine: on a `--done` write, under a per-topic advisory lock shared with the
deadline timer, the serving group found by `serves`, develop's state from the push chain, stamps
mapped to identities through the session index (a delegated identity mapped
to its parent), liveness through RDR-253's lapse-applying read, the suspect set (at most ten
listed), owner and next up, the leader's cases, owner and clear rows with
`from=engine`; the acknowledgement deadline (group default, per-topic
override) as a timer rebuilt after a restart from unsuperseded owner rows; a
stamper error posting the leader with its deadline, and the fail-safe escalation
when a deadline's evaluation throws; the named reasons; at most two hand-offs,
then the `escalated` row; refreshed rows for a newer red with the same owner
and acks carried across them; no owner rows while no live group serves the
topic (clear rows still posted for a topic with an open owner row);
`episode_at`, `refresh` and `handoffs` on every owner row.

#### Step 4: `[load]`, `self.identity` and the result-driven wake; `ci_develop_red` moved onto the owner post

Client, after RDR-253 Step 3: `[load]` read from the identity's memberships at server
start, with one group read per named group; `nx aura check` warning when an aura's `[load]` group is not the group
serving its trigger topic; `nx aura list` naming a
switched or unbound identity, read from the server's state file; `nx group
form <name> --serves <topic>`, `nx group set <name>` (leader, deadlines,
`ends`, `serves`), `nx group disband <name>`, `nx group override <name>`
(tenant credential: leader or disband); `nx group join <name>`
(refused for an ephemeral or delegated identity) and `nx group leave
<name>`; `nx group list <name>` showing the leader and members by alias with
their RDR-253 state; the
`self.identity` argument source; `display.channel` naming a result field; `ci_develop_red`
triggering on `kind=owner` and clearing on `kind=clear`; re-surfacing on a new
owner row; the calibration read (time to acknowledge per red) for Day 2.

### Phase 2: Operational Activation

#### Activation Step 1: Form `push-train` and turn the aura on

Sam's go: halcyon runs `nx group form push-train --serves nexus-develop` as
its leader, members join,
then `nx config set auras.ci_develop_red=on` and a restart round.

### Day 2 Operations

- `nx aura list` shows, per aura, loaded or not and why (which group).
- `nx group list` shows the leader and members by alias, each with its RDR-253 state (`unheld`, `live`, `lapsed`, `ended`, or `unleased`).
- Push records and acknowledgement times give after-the-fact grading: time
  from red to acknowledgement and to fix, as WoW tools grade cooldown use.
- The calibration follow-up is due 2026-12-20 (the close critique r2's S2,
  `nexus/critique-rdr252-close-2026-11-20-r2`: "the follow-up (calibration by
  2026-12-20) must name this fix as its precondition"; Sam's close-out ruling,
  `nexus/sam-ruling-rdr-closeout-batch-2026-11-19`, lets a measurement trail a
  close as a dated follow-up). Its precondition is the ack fixes: the client's
  `nx group ack-times` counting an ack by its sender-session stamp and reporting
  `ignored_acks` (9f14aa59a, f14d51a0d, first released in client v8.5.4) and the
  engine's stamp of an ack's `from` with `--owner` rows kept 35 days (b308e6dc9,
  b545859c0, first carried by engine-service-v0.1.200, serving in the cloud since
  2026-11-21 15:29Z, T2 `conexus/deploy-engine-v0.1.200-2026-11-21`). A read
  that counts acks from before that deploy miscounts acks the stamp did not map
  and reports an ack tail that does not exist.
  The follow-up reads attribution quality as well as ack time (close critique
  r3, S2, `nexus/critique-rdr252-close-2026-11-21-r3`): for each red, whether
  the first owner row named the pusher whose commit caused it or a suspect that
  was green on rerun or fixed by someone else (the misattribution case under
  Known limits), and how often a non-owner fixer had to have the owner ack for
  them. Owner rows written before the 0.1.200 deploy expired after three days,
  so reds from before 2026-11-21 15:29Z exist only as T2 snapshots (the
  critique's observation).

### New Dependencies

None.

## Test Plan

- Post-close fixes (2026-11-22; `tests/test_ci_push_gate.py`, `tests/test_ci_record_push.py`, `tests/test_ci_base.py`, `tests/scripts/test_git_push_develop_sh_hold.py`, `tests/test_ci_ack_not_mine.py`, `tests/identity/test_owner_path_journeys.py`, `OwnerDecisionAuditTest`, `OwnerStamperAuditTest`): the hold refuses a stranger and lets the owner, a suspect's pusher and `--red-fix` through, and never refuses on a failed read; the record's fallback, legacy write and unstamped report; base derivation and its refusals; `--not-mine` and its display; one journey row for each of the seven owner paths and one that runs the real `check-hold` and `record-push` against the engine; and, for the engine cut, the implicit ack only after the red verdict, the 2026-11-21 late-push replay (red 7e7840fae9), the disclaim hand-off, the stall and its hard cap, the nudge, and the unrecorded and unattributed counts.
- Owner, next up and everyone-else states from fixed push records.
- Inherited red: the owner is the earliest suspect's pusher.
- A replaced (cancelled) run: the untested tip is a suspect.
- Late red and late green, in both orders; a green clears in both arrival
  orders.
- Owner identity lapsed, owner not a member, no stamped or no mapped push
  record, a chain gap, a stamper error: the leader owns, with the reason.
- The deadline: no ack names next up; an engine restart keeps the timer.
- A stamper error arms the deadline and escalates at it, also when the decision
  throws on every trigger; a restart over such a row re-arms from the row; the
  restart census retries only failed topics and visits tenants without a token
  row.
- A failing liveness read in the fail-safe: a red with the owner's stamped ack
  on its tip (a sender session, `from` the row's owner) is retried every 30
  seconds and never escalated, and is escalated once the read recovers if the
  ack does not match; a red with no such ack, a non-owner's stamped ack or an
  ack with no sender session included, is escalated at once with `owner` null; a deadline that is not yet due, or an escalated or absent row,
  makes no liveness read.
- A client `out` or `ack` reply with `from=engine` is refused with 403
  `reserved_dim`, the claim kept; the stamper's owner and clear rows have a null
  sender session.
- Next up is never the owner, and skips a lapsed pusher or a non-member.
- Identity across `/clear`: a push before and an ack after `/clear` both map
  to the same identity; the owner is still woken.
- Body cap: a red with more than ten suspects lists ten and the true
  `suspects_total`, within 1024 bytes; with both aliases present, suspects are
  cut first to stay within it.
- Per-topic deadline: `ack_deadline_by_topic` overrides the group default.
- Writer check: a membership written by another identity, a group write by a
  non-leader, a group write by a superseded process of the leader, and any
  tokenless write to a leased target are refused; a membership write for an
  `unleased` identity is accepted and labelled unattested; a tokenless group
  write is refused even when the leader is `unleased`.
- Concurrency: two `--done` writes evaluated at once, and a hand-off racing a
  `--done`, post rows consistent with commit order.
- Same owner: a fix that goes red again posts a refreshed row, keeps the
  deadline, and an ack on the first red tip still counts.
- Serving: a red on a topic no live group serves gets no owner row; a second
  group serving the same topic is refused.
- Refresh: a refreshed row does not wake, keeps `episode_at`, and an
  engine restart after a refresh keeps the original deadline; after a
  hand-off a refresh keeps the new owner.
- Serves: `ack_deadline_by_topic` keys outside `serves` are refused;
  removing a topic from `serves` removes its key; two groups of different
  names forming on one topic at once leave one serving it.
- Re-form: the former leader re-forms a disbanded name; another seat may
  only when the former leader is not live; re-forming starts with only the
  leader as member.
- Disband during an open red: no hand-off or escalated row follows; the
  green still clears.
- Escalated with nobody live: every member is woken with the ask text.
- State file: `nx aura list` in a Bash child reads its own server's file and
  ignores a dead process's.
- Verbs: `nx group form`, `set`, `disband` and `override` round-trip; the
  override is refused while the leader is live; a disbanded name is formed
  again; a bad name is refused.
- Membership rules: an ephemeral or delegated identity cannot join a group
  that has a record; the leader cannot leave.
- Group writes: two concurrent re-forms of one disbanded name leave one; a
  join to a disbanded group is refused; the override is accepted for a leader
  in state `live` with lease `unleased`; two concurrent first writes leave one record; the first
  write adds the leader's membership; a new leader that is not a live,
  leased member is refused; a deadline outside 5 to 240 is refused; the
  operator override with the tenant credential is accepted and audited.
- Disbanded or missing group: with `ended_at` set, `ends` passed or no
  record, the stamper posts no owner row and `[load]` does not load; for a
  topic with an open owner row, a green
  still posts a clear row.
- Permanence: a membership and a group record still read 60 days after they
  were written, with no renewals.
- Liveness: a seat whose last renewal is over 300 seconds old reads as not
  live even though no lapse row was written; an `unleased` owner hands the
  red to the leader (`owner_unleased`).
- Delegated pusher: a push by a `claude -p` child is owned by its parent
  seat (an engine-level test with a delegated identity from RDR-253 Step 1's
  route; the live case needs RDR-253 Step 4's dispatch).
- Unrecorded tip: a red on a tip in no push record goes to the leader
  (`no_record`).
- Escalation: two missed deadlines give one `escalated` row and no hand-off
  or second `escalated` row after it, also across an engine restart; a
  refresh after it carries `reason=escalated`.
- Identity switch: after a cross-seat `/resume`, `nx aura list` reports the
  mismatch until a reconnect; an unbound start loads no group-gated aura.
- Late bind: a server that loaded with no identity gates its group-gated
  auras once when the lease adopts its first identity (also when that lands
  while the load is still running), and the runtime fires with
  `self.identity` filled; a later identity change gates nothing again.
- `nx group join` refuses an ephemeral identity; `join`, `leave` and
  `list` round-trip.
- Stamp mapping: the stamper maps a stamp to an identity through the
  session index, and an unmapped stamp counts as no record.
- Push script: a `--skip-lock` push writes the record; a push to another
  branch writes none.
- `[load]`: a non-member never subscribes; an unknown key is refused.
- No channel wake for "everyone else"; a wake on a new owner row, none on a
  refresh.
- `nx aura check` warns when an aura's `[load]` group is not the group that
  serves its trigger topic.
- The engine refuses a `board-ci`-scoped write to a `--owner` or `--push`
  subspace.
- The engine refuses a client write with `from=engine` to a `board/ci` topic, on
  `out`, `mode=replace` and an `ack` reply.
- Carry-forward and `branch_outcome`: a failed `branch_outcome` wins over a
  per-sha success and a per-sha failure is kept over any value; a carried
  entry that is not `success` (a skipped or neutral one included), an
  unreadable entry or a trimmed list leaves a per-sha success no verdict with
  its reason; a carried red keeps the episode open and a green `branch_outcome`
  clears it (`DoneVerdictBranchOutcomeTest`, for example
  `aBranchOutcomeOfFailureWinsOverAPerShaSuccess` and
  `aCarriedSkippedOrNeutralEntryIsNotPassingButThePerShaRunsKeepTheirSet`;
  `OwnerStamperBranchOutcomeTest`, for example
  `aCarriedRedKeepsTheEpisodeOpenAndALaterGreenBranchOutcomeClearsIt` and
  `aShaRowWithNoCountingVerdictLogsOneLineNamingTheReason`).
- The engine's ack-from stamp: an alias, another seat's id, an absent or blank
  `from` on an ack to a `--owner` subspace is overwritten with the sender
  session's identity, a delegated child's ack is stamped with its parent, a
  `mode=replace` write and an ack reply are stamped alike, and a subspace under
  another template or another kind is untouched
  (`service/src/test/java/dev/nexus/service/OwnerAckFromTest.java`, for example
  `anAliasInFromIsOverwrittenWithTheTokensIdentityAndTheWriteIsAccepted` and
  `aDelegatedChildsAckIsStampedWithItsParentBecauseThatIsWhoIsCounted`).
- `nx ci ack`: writes the ack row from the caller's identity id, resolves a
  sha prefix against the open red's tips and refuses a sha that is no tip; a
  hand-typed alias in an ack is restamped and a caller with no identity is
  refused (`tests/test_ci_ack_identity.py`, for example
  `test_nx_ci_ack_writes_the_ack_row_from_the_callers_identity_id` and
  `test_a_caller_with_no_identity_is_refused_and_nothing_is_written`).
- `nx group ack-times` `ignored_acks`: an ack with a null or unmapped stamp is
  counted per red, noted once on stderr and never counted in minutes
  (`tests/test_group_ack_times.py::test_ignored_acks_are_counted_per_red_and_noted_on_stderr_and_never_count_in_minutes`).
- The group ready check: `--ask-ready --group` asks only a group's live
  members and `--status --group` reports only them; each member it did not
  cover is named with its state and why; a blank, unknown, disbanded or
  member-less group refuses in one line; the restart takes no group and a
  restart after a group round still refuses
  (`tests/scripts/test_claude_restart_sessions_group.py`, for example
  `test_ask_ready_with_a_group_names_the_members_it_did_not_cover` and
  `test_a_restart_after_a_group_round_still_wants_every_session_asked`).

## Validation

### Testing Strategy

Unit tests on the stamper with recorded tuples; one live check in a test group
(the MVV).

### Performance Expectations

Load removes the aura from non-members. A fire costs about 50 ms today
(marginalia, 2026-11-16); members read one owner post instead of computing.

## Finalization Gate

### Contradiction Check

The stores agree: groups in a new table and memberships in RDR-253's
membership table; push records on `board/ci/nexus-develop--push` and owner,
clear and ack rows on `board/ci/nexus-develop--owner`, on the existing
`board/ci/<topic>` template. The owner rule
(earliest suspect) and the leader's cases read the same in Technical Design,
the MVV and the Test Plan; the MVV exercises three display states and the
Test Plan the fourth. The engine-client split
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

Known limits, seen live and not designed away here. Sam's owner rule stands
(the earliest suspect owns the red); this records where it misses.

- Misattribution. The develop red at 9f14aa59a, 2026-11-20 20:48Z: the owner
  row posted at 20:48:09 named i3 (linda) with reason `earliest_suspect` and suspects
  11da32740 and 9f14aa59a. 11da32740 (linda's P1.R) "was green after a rerun";
  the cause was 9f14aa59a (marginalia's ack-identity change). Linda's ack
  arrived at 1.75 minutes and said the red was not hers; it "counted" and "no
  handoff" followed the 21:08:09 deadline. The engine re-seated ownership onto i5
  (marginalia) at 21:27:58, about 40 minutes later, as a new episode with "no
  clear row in between" (T2 `nexus/rdr252-owner-rows-snapshot-2026-11-20b`,
  notes 1 and 2; `bd show nexus-igea9.4`, the A6 calibration note).
- A next-up who is the real fixer cannot stop the deadline: "a next-up who is
  the actual fixer cannot stop the deadline" (the same bead note; snapshot note
  2). The engine counts an ack by its sender-session stamp "mapped through the
  session index to the owner's identity" (`tests/test_ci_ack_identity.py`
  docstring), so the fixer waits for the hand-off or has the owner ack for
  them, as happened here; the timer path itself was not re-read for this line,
  inferred.

The open design question, whether the stamper should drop a suspect as soon as
that suspect's own run is green instead of waiting for the next red tip to
re-seat, is not decided here; it sits in the same bead note. Nothing in this
section changes Sam's rule.

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
RDR-253's exception for its own membership, labelled unattested.
Release: Steps 1 and 3 need an engine release, Steps 2 and 4 a conexus
release that pins it. Noise: no wake for everyone else. Data lifetime: the
base topic and `--push` keep the template's three days (`retention_seconds:
259200`); a topic ending `--owner`, which holds the owner, clear and ack rows,
keeps 35 days (`retention_by_suffix: --owner: 3024000`, "the calibration of the
acknowledgement deadline reads the first 20 reds or 30 days of --owner rows, so
those keep 35 days", `board-ci.yaml`); group records
and memberships are permanent rows, a membership removed when its seat
leaves the group, when the group is disbanded, or when RDR-253's retention
prunes an ephemeral or delegated identity.
Dependency: every step waits on RDR-253 Phase 1 Steps 1 to 3.

### Proportionality

A group table with its routes and CLI verbs, one small template, one
stamper with a timer, and two aura-surface
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
- 2026-11-16: fix check `252-fix-check-2c8be6194989` failed (3 of 3: a re-sent tuple row is clamped to its creation time plus retention, so the refire keep-alive could not work; `nexus_rdr/252-research-6`); fixed once: groups and memberships are permanent table rows beside RDR-253's identity tables (the membership requirement on RDR-253 Step 1 corrected the same day); first write an insert that adds the leader's membership; updates in place; deadline bounds in the group route; a new leader a live, leased member; an audited operator override for a dead leader; the `unleased` exception for memberships only; clear rows posted whatever the group state; load stays at server start (Sam's answer 3), a switch reported by `nx aura list`; Desktop servers load; `from=unknown` for an unstamped push; the escalated display state; reason precedence.
- 2026-11-17: Gate round 2 — PASSED (0 Critical, 6 Significant, 0 ship-blocker(s)); blob `7fea8aebbd8e`; critique `nexus_rdr/252-gate-critique-2026-11-17-r2`.
- 2026-11-17: post-gate fixes, all fourteen at Sam's direction ("Fix all, then accept"; `nexus_rdr/252-research-7`): the stamper serialized per topic by an advisory lock shared with the timer; a newer red with the same owner refreshes the row and keeps the ack and deadline; `nx group form`, `set`, `disband` and `override`; groups in a table and memberships on RDR-253 Step 1's routes; the serving group found by `serves`, not coded in the engine; push, owner, clear and ack rows on a new `board/coord/<topic>` template with its own cap; seat-only leaders and members; the override in RDR-253's purge states, audited in `gc_audit`; a Desktop server loads only when it knows its identity; `owner_unleased` first; the server's state file for `nx aura list`; group names, re-forming a disbanded name; acks across a refresh; the escalated row with nobody live.
- 2026-11-17: fix check `252-fix-check-b01405db2a50` passed 3-0; its counted items fixed (`nexus_rdr/252-research-8`): back to `board/ci/<topic>` (its cap counts each subspace, so nothing was crowded out, and the aura grammar takes it); refreshed rows carry `episode_at`, `refresh` and `handoffs`, keep the current owner and do not wake; `serves` format, keys within `serves`, one live group per topic under an advisory lock; re-forming a disbanded name by the former leader or, when it is not live, another seat, starting with no other members; disband removes memberships; the override also for an `unleased` or purged leader; the escalated display with nobody live; clear rows only for a topic with an open owner row; the aura state file keyed and cleaned like RDR-253's token files; the serving group in place of `push-train` in the stamper rules; Activation forms with `--serves`.
- 2026-11-17: fix check `252-fix-check-b03c30cf28aa` failed (2 counted BLOCKS-PLANNING: a start-time replay with no mechanism, and the `board-ci` token reaching owner rows); fixed once (`nexus_rdr/252-research-9`): the replay promise dropped (a late server shows the next row); the engine refuses a `board-ci`-scoped write to `--owner` and `--push`; the stamper runs on every `--done` write, owner rows only for a served topic and clear rows for any open red; refreshes carry their row's reason and hand-offs `reason=handoff`; the null-owner escalated display; re-form named in the leader-only rules, locked, every field the form call's, history kept in `gc_audit`; the disband cascade exempt from the membership checks, no join to a disbanded group, `ended_at` cleared only by re-form; one lock order shared with the stamper; the override for an `unleased` leader stated in RDR-253's terms; the state-file cleanup by RDR-253's holder check; the aura's group tied to the serving group.
- 2026-11-17: accepted by Sam ("Accept and plan"); last fix check `252-fix-check-b460ee2181a5` PASS 3-0; its seven counted DISCOVER-AT-IMPLEMENTATION items go to the plan as bead notes.
- 2026-11-18: wording matched to the reviewed Step 2 code (nexus-igea9.2, 5cf9ab46d): the push record's key and `push-<tip>` nonce, the lock released before the write, and the write's time bound.
- 2026-11-19: wording matched to the reviewed Step 4 code (nexus-igea9.7): a server that loaded with no identity gates its group-gated auras once more when the identity lease adopts its first identity; every other change waits for a reconnect; Test Plan line added.
- 2026-11-19: wording matched to the reviewed engine 0.1.198 code (nexus-igea9, b300a8550, 76bbd34d4; grant 3): the owner row carries `owner_alias` and `next_alias`, read at stamp time, absent for an identity with none or on a failed read; the notice shows the alias, else the id; the aliases count inside the 1024-byte cap, suspects cut first.
- 2026-11-20: engine fixes for the close critique's S2 and S3 (nexus-igea9; grant 3): the deadline rebuild retries failed pairs until a full census, a stamper error arms its deadline and escalates at it, a failing evaluation falls back to the current owner row, and `from=engine` is engine-only (client writes refused with `reserved_dim`; a prior row counts as state only with a null sender session). `board-ci.yaml` `from.engine` documents the refusal.
- 2026-11-20: `nx ci ack` and the client stamp of `from` on an ack (nexus-igea9; grant 3): `nx group ack-times` resolves an ack's poster by the sender-session stamp through the session index, a delegated child to its parent (read from the child's identity record, and noted as unresolved when the credential cannot read it), and never reads `from` (see the 2026-11-20 line below for the null stamp); it counts red tips and the engine's own owner and clear rows as the engine does, but it is a client read of the identity service, not the engine's count. The engine counts the sender stamp; a hand ack with a session alias stopped the hand-off on red ae619ef56 and only ack-times missed it.
- 2026-11-20: `nx group ack-times` counts an ack only when its sender-session stamp maps (nexus-igea9; grant 3, wording matched to the engine's confirmed ack half): an ack with a null stamp, or one that maps to no identity (404 `unknown_session`), counts for nobody whatever its `from` says, and is counted as ignored (`ignored_acks` per red in `--json`, one note on standard error, never in `minutes`) when it is on a red tip inside the red; a stamp that is not a session id, a host with no session route to ask, and a failed or refused lookup are noted as unresolved (counts may be low) and never remembered; the ack counts for nobody in each case. The `from` fallback (an identity id as written, a name through `owner_alias`/`next_alias`, then the identity service's name route) is removed, so a hand-typed `from=<owner id>` no longer counts in ack-times where the engine counts nothing.
- 2026-11-20: wording matched to the reviewed carry-forward engine code (conexus-lru2; grant 3): a tip's verdict reads the `--done` row's `branch_outcome` (the tip's outcome folded with the latest earlier result of a path-filtered workflow such as Service CI) ahead of `outcome`, so a Service CI red a later sha never re-ran keeps the red episode open; a per-sha failure is never overridden, an unrecognised `branch_outcome` is no verdict, and a row without it whose body carries a null, trimmed or not-all-`success` `carried` list has no verdict for a per-sha success (a carried skipped or neutral does not pass; a body with neither `carried` nor `carried_n` reads as before); the stamper logs `board_done_no_verdict` with the reason `carry_failed`, `carried_not_green`, `cancelled` or `unknown`.
- 2026-11-20: wording matched to the reviewed code (nexus-igea9, close critique r2 S1, S3 and observations; grant 3): the restart script's ready check is `claude-restart-sessions --ask-ready --group X` and `--status --group X` (live members here, matched by alias, uncovered members named with a state, the restart takes no group, a group round replaces the round file; v8.5.3), with a Test Plan line; Gap 1 says what was built (membership, the leader, the develop red's owner post; typed assignments cut at gate round 1), the title unchanged; the two Prerequisites ticked with their evidence (push-train formed 2026-11-20, RDR-253 Steps 1 to 3 landed); and the stamp's meaning cites RDR-253 now that it says it.
- 2026-11-21: wording matched to the reviewed fail-safe fix (nexus-igea9; grant 3): when the fail-safe's identity read fails, a red with an ack row on its tip is retried every 30 seconds without end (the twelfth failure of a topic's hand-off since its last success, cancel or eviction, and every twelfth after, raises the log event `board_ack_deadline_stuck`), and a red with no ack row is escalated at once with `owner` null; Test Plan line added.
- 2026-11-21: the fail-safe's hold-off narrowed to the owner's stamped ack (nexus-igea9; grant 3): when the liveness read fails, only an ack row with a sender session whose `from` is the row's owner, on the row's tip and at or after the row, holds the escalation off; a non-owner's ack and an ack with no sender session escalate at once with `owner` null. The known transition (pre-0.1.200 ack rows with a typed `from` escalate with `owner` null under the compound fault) and the check's narrowness against the normal deadline count are recorded in the fail-safe text; the Test Plan line is matched.
- 2026-11-21: close wording matched to the shipped engine ack half and the close critique r2's follow-ups (nexus-igea9; grant 3): the engine stamps an ack's `from` from the sender session's identity (a delegated one as its parent, left as typed when nothing maps) and `--owner` rows keep 35 days while the base topic and `--push` keep three (cited from `board-ci.yaml`, `BoardCiGuard.stampAckFrom` and `Identities.countedIdentity`); the Day 2 calibration follow-up due 2026-12-20 names the ack fixes as its precondition.
- 2026-11-21: known limits and Test Plan matched to the close critique r3 (nexus-igea9, nexus-igea9.4; grant 3): a Known limits clause beside "Unvalidated by design" records the live earliest-suspect misattribution at 9f14aa59a and the next-up fixer who cannot stop the deadline, leaves the suspect-dropping question open and Sam's rule standing; the Day 2 calibration read covers attribution quality as well as ack time; the skipped or neutral workflow assumption says what watches it (nothing) and who decides (the board's maintainers, 2026-12-20), with the path-filtered workflows named; four Test Plan lines (carry-forward and `branch_outcome`, the engine's ack-from stamp, `nx ci ack`, `ignored_acks`).
- 2026-11-21: release carriers named (close critique r3 and r4, S1, `nexus/critique-rdr252-close-2026-11-21-r4`; nexus-igea9; grant 3): the engine half (the ack `from` stamp and 35-day `--owner` retention, b308e6dc9 and b545859c0; the fail-safe's at-once escalation and owner-stamped-ack hold-off, ed7b34fde and a3b2c1220; carry-forward and `branch_outcome`, 6c1c73de9 and d13cba1b1) is first in engine-service-v0.1.200, serving in the cloud since 2026-11-21 15:29Z (T2 `conexus/deploy-engine-v0.1.200-2026-11-21`); the CI board adapter's `FOLD_BRANCH_OUTCOME` was switched on at 16:23Z (T2 `conexus/deploy-ci-board-adapter-fold-branch-outcome-on-2026-11-21`); `nx ci ack` and the sender-stamp count in `nx group ack-times` (9f14aa59a, f14d51a0d) are first in client v8.5.4 (v8.5.3 has no `nx ci ack`). Earlier reds and acks predate all of it.
- 2026-11-22: wording matched to the push script's post-close audit fixes (nexus-igea9; grant 3): the push record is written by the shared `nx ci record-push` and the hold checked by the shared `nx ci check-hold` (any repo's push script can call them; `git-push-develop.sh` is the first caller and passes `--base nexus-develop`; the base is an argument or derived as `<repo>-<branch>` from the remote URL and branch, and a base that cannot be derived is refused naming `--base`, never defaulted); the record's `from` is the pusher's identity id, else `unknown`; a skipped or failed record is reported on the script's standard output as well as standard error; a failed stamped write is retried once as a minimal unstamped record (base, tip, from) so the chain keeps its link; the "Hold your pushes" line is enforced (`PUSH_REFUSED_DEVELOP_RED`, exit 13, unless the pusher owns the red or pushed a suspect, or passes `--red-fix '<reason>'`, which is written into the push record body as `red_fix`); a caller with no identity is held like any other; a failed read of the red warns and does not refuse.
- 2026-11-22: post-close amendment added (Sam authorised this edit to the closed record, T2 `nexus/sam-ruling-finish-252-no-257-2026-11-22`; nexus-igea9): what was missing at the close, what the RDR now covers (the audit fixes, the hold, the seven owner paths, every repository), and the status note.
- 2026-11-22: engine wording matched to the reviewed engine fix rounds (nexus-igea9; grant 3): a live member's or earlier owner's push holds the deadline until its own verdict; stall rows carry `open_minutes` and `stalled_sign`.
