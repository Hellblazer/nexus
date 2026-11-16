---
title: "Declarative Auras: A Trigger, a Condition and a Display Around a Sandboxed Procedure"
id: RDR-249
type: Feature
status: accepted
priority: medium
author: Sam
reviewed-by: self
created: 2026-11-12
accepted_date: 2026-11-15
related_issues: ["nexus-mdbds"]
related_rdrs: [RDR-239, RDR-240, RDR-242, RDR-211, RDR-212, RDR-213, RDR-220, RDR-245, RDR-110, RDR-111]
---

# RDR-249: Declarative Auras: A Trigger, a Condition and a Display Around a Sandboxed Procedure

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

**Provenance.** Stage 2 of the scriptable tuple space (epic nexus-mdbds, Sam
2026-10-31: "emulate WeakAuras, script the tuple space like AppleScript").
Stage 1, the tuple-space dictionary (RDR-239), and stage 3, the sandboxed Lua
runtime (RDR-242) with its first consumer, procedures as Lua scripts
(RDR-240), have shipped. RDR-239 required that each stage be "a separate RDR,
gated on the one before it shipping and being used". Sam chose the shape on
2026-11-12 from three candidates: an aura is a thin declarative wrapper over
an RDR-240 procedure ("c"), declared in its own file beside the procedure, so
RDR-240's four-line header stays exactly as accepted ("1"). Design of record:
T2 `nexus/design-declarative-auras.md`.

**Terms.** A *tuple* is one row in the engine's tuple space, a shared board
that sessions and tools post to and read from. A *subspace* is a named part of
it, such as `board/ci/nexus-develop` (CI status posts) or `mailbox/<session>`
(one session's mail). A *procedure* is a Lua script from RDR-240's library,
run by RDR-242's runtime in a separate worker process that can only call the
read-only *verbs* its header grants. An *aura* (the name comes from
WeakAuras, a World of Warcraft add-on in which a player declares "when this
happens, if this holds, show this") is the thing this RDR adds: a declaration
that runs a procedure when a matching tuple arrives and shows the result if a
condition holds.

## Problem Statement

A session is told when a tuple arrives on a subspace it subscribes to: the
nexus MCP server's channel waiter announces it mid-turn (RDR-213), and the
mailbox drain hook shows mail at the next prompt. What happens next is left
to the agent reading the announcement. It decides, from its own reading,
whether the post matters, what to look up, and where to record it. That is
the failure RDR-240 removed for procedures: a fixed reaction with one right
answer is recalled and improvised each time instead of run.

### Enumerated gaps to close

#### Gap 1: An arrival is announced, but the reaction to it is improvised every time

A red develop run arrives on `board/ci/nexus-develop--done` as a `kind=sha`
post with `outcome=failure` (RDR-220's board template). Every session that
subscribes sees the same one-line announcement. Whether it is noticed, which
sha it names, and whether it reaches the threads board depends on what each
agent is doing at that moment. The worktrees guide (rule 7) asks for a
develop red to be diagnosed before the next push; nothing makes the red land
somewhere durable without an agent choosing to put it there.

#### Gap 2: A new watch costs code and a release

The only way today to give an arrival a fixed reaction is to write it into
nexus: the channel waiter's rendering, a hook, or a new MCP tool. Each one is
Python in the package, reviewed and released, for what is often one line of
logic ("if the outcome is failure, say which sha"). RDR-240 made a procedure
a file instead of a release. A reaction to a tuple has no such form.

#### Gap 3: Stage 2 has no design, and the last one was scrapped

The scriptable tuple space has stages 1 and 3 but nothing between them. The
earlier attempt, RDR-110's bindings (a tuple encoding a trigger, an action
and a surface), was scrapped with the RDR-110 to 119 arc, together with the
engine-side reaction loop it shipped (`_BindingWatcher`, PR #787; postmortem
`docs/postmortem/2026-05-16-rdr110-113-remediation-chain.md`). RDR-212 then
wrote "Excluded, deliberately: bindings and any reaction loop" (RDR-212,
line 409). An aura is a reaction loop, so a stage 2 design narrows that
exclusion; it has to give reactions a form without rebuilding what was
scrapped.

## Relationship to Prior RDRs

| RDR | Relationship | What it settles here |
|---|---|---|
| RDR-240 Procedures as Sandboxed Lua Scripts (accepted 2026-11-07) | Builds on it | The procedure library (`src/nexus/procedures/` shipped, `.nexus/procedures/` per repo), the four-line header, the argument types and the reserved arguments `now` and `repo`. An aura names a procedure and maps tuple fields onto its declared arguments. The header is not changed. |
| RDR-242 Sandboxed Lua Runtime (accepted 2026-11-07) | Builds on it | `run_script(name, args, limits)`, the worker process, the read-only verb table and the default limits (50M instructions, 64 MiB, 60 s wall, 1 MiB result). Its version 1 has no write verb; a write waits for its approval document (its Technical Design 1 and 10). So an aura's procedure cannot write, and this RDR adds no write path. |
| RDR-239 Tuple-Space Dictionary (closed) | Uses it | `GET /v1/tuples/registry?doc=1` describes each subspace's fields. `nx aura check` reads it to refuse a trigger filter or an argument mapping that names a field the template does not have. |
| RDR-211 channel delivery (Sam's reference-only decision, 2026-09-17, T2 `nexus_rdr/211-decision-push-reference-2026-09-17`) | Constraint honoured | "The channel notification carries a REFERENCE, never the body"; its content is "a fixed template built only from identifiers the server controls or verifies". An aura's channel display is such a template (Technical Design 1): aura name, subspace, tuple id and a pointer to the threads block, never procedure text (Sam, 2026-11-15: "Fixed template only"). RDR-211 is unchanged. |
| RDR-213 channel push (amends RDR-211) | Hosts it | The channel waiter is the MCP server's one background task. It parks a wait over the session's subscriptions, and the engine announces each board post to a subscriber once (`nexus.tuple_deliveries`). Auras are evaluated on what the waiter receives. |
| RDR-220 CI board | First consumer's source | `board/ci/<topic>` and its `--done` wake, with `kind`, `sha` and `outcome` dimensions. |
| RDR-245 Structured asks on the threads board | Display target | The threads board entry (T2 project `threads`, one per session) is where a durable display lands. |
| RDR-212 Cockpit surfaces | Exclusion narrowed on purpose (Sam, 2026-11-15: "Narrow it on purpose") | RDR-212 line 409: "Excluded, deliberately: bindings and any reaction loop". RDR-249 brings a reaction loop back in one form only: client-side, in the session's own MCP server, running a read-only sandboxed procedure whose only action is a display, off by default, never in the engine, with no new engine table, route or template field, and one consumer. Bindings stay excluded. |
| RDR-110, RDR-111 (scrapped) | Lesson applied | No new engine table, route or template field; one consumer first. |

## Context

### Background

Sam's 2026-10-31 direction staged the scriptable tuple space one consumer at
a time, because the RDR-110 arc failed by shipping substrate and consumers
together ("scope discipline failed across nine RDRs and 67 stranded beads").
Stage 1 published what each subspace means. Stage 3 made a sandbox in which a
script can read nexus but not change it. Stage 2 joins them: something
arrives, a script looks at it, and the session is shown the result.

WeakAuras splits an aura into a trigger (an event or status), conditions
(checked in order against the trigger's state), actions (on show, on hide)
and a display region (icon, bar, text). Its custom triggers and actions are
Lua. This RDR takes the split and drops what has no counterpart here: there
are no regions, the action is a display only, and the custom logic is an
RDR-240 procedure rather than inline Lua.

### Technical Environment

- The MCP server (`nexus.mcp.core`) runs per session. Its lifespan task is
  `ChannelWaiter` (`src/nexus/mcp/channel.py`), which waits on the session's
  `SubscriptionSet` (`src/nexus/mcp/subscriptions.py`: the session's mailbox
  plus up to `MAX_BOARD_TOPICS` board topics) and pushes a notification
  through `claude/channel`. The waiter runs whether or not Claude Code was
  launched with the channel enabled; without it, the notification is dropped
  by the client.
- `nexus.lua_runtime.run_script` runs a library procedure in a `python -I`
  worker and returns its result or raises `ScriptError` with a kind.

## Research Findings

### Investigation

Prior art brief (T1, 2026-11-11, from RDR-110, 111, 212, 239, 240, 242, the
postmortem, the code named above, and the WeakAuras wiki mirror). Source
checks for this draft, 2026-11-12, in the worktree at origin/develop.

### Key Discoveries

- **Verified (source search):** there is no similarity matching anywhere in
  the tuple space. The engine's tuple classes (`TupleRepository`,
  `TupleWaitRegistry`, `TupleShapeValidator`, `TupleHandler`) have no vector
  or embedding code, and the client subscription set filters by subspace
  only. A semantic trigger ("a post that means roughly this") is new work in
  the engine and is out of scope here.
- **Verified (source):** the engine already announces a board post once per
  subscriber (`announce` with `max=1`, stamped in `nexus.tuple_deliveries`,
  RDR-213 as amended by nexus-vsipz and nexus-q82tk). An aura on a board
  trigger therefore sees each post once, without its own bookkeeping, as long
  as it is evaluated on the waiter's path and the post was created after the
  topic was subscribed: the waiter drops "rows created before the topic's
  subscribe time" less a 30 s margin (`_deliver_board_rows` in
  `src/nexus/mcp/channel.py`; `DEFAULT_BOARD_START_SKEW_S`).
- **Verified (source):** RDR-220's `--done` wake carries `kind=sha`, `sha` and
  `outcome` as dimensions (when the adapter's `FOLD_RESULT_DIMS` switch is
  on), so a red develop sha can be matched on dimensions alone, without
  reading the body.
- **Verified (source):** RDR-240's header parser reads exactly four header
  lines and ignores a fifth (`nexus.lua_runtime.header.parse` loops over its
  four keys and reads nothing past them). A trigger line there would be an
  unchecked comment, and making it count would amend an accepted format;
  Sam chose a separate file.
- **Documented (RDR-242 TD1, TD10):** no write verb exists, and adding one
  waits for an approval document that only Sam signs.
- **Documented (nexus-mdbds comment, 2026-11-08):** `memory.get` counts as an
  access and raises a memory's heat and TTL. A polling aura would distort
  both. Event triggers avoid polling.

### Critical Assumptions

- **Assumed:** running a procedure per matching arrival costs a worker start
  (a `python -I` process), on the order of a few hundred milliseconds, which
  is acceptable for posts that arrive minutes apart. Worker start measured
  at gate round 1: 318 ms first, 47-57 ms warm (T2
  `nexus_rdr/249-gate-critique-2026-11-15-r1`). A whole fire is measured in
  Phase 3 on the CI consumer.
- **Decided (Sam, 2026-11-13):** each enabled session shows its own block.
  Several sessions subscribed to the same topic each run the aura and each
  write their own threads block; there is no lock tuple limiting an aura to
  one session per repo.

## Proposed Solution

### Approach

An aura is a small TOML file in the procedure library, beside the procedure
it wraps. The session's MCP server loads the enabled auras at start,
subscribes their trigger subspaces, and evaluates each arrival the waiter
receives: match the trigger, map fields to arguments, run the procedure in
the sandbox, test the condition on its result, and show it. Nothing changes
in the engine, and the procedure can still only read.

### Technical Design

1. **The aura file.** `<name>.aura.toml` in either library root
   (`src/nexus/procedures/`, `.nexus/procedures/`). One aura a file; the name
   follows RDR-242's name rule and may not shadow a shipped aura, as for
   procedures. Example, the first consumer:

   ```toml
   aura = "ci_develop_red"
   summary = "A red develop sha, posted to this session's threads entry."
   procedure = "ci_red_notice"

   [trigger]
   subspace = "board/ci/nexus-develop--done"
   match = { kind = "sha", outcome = "failure" }

   [args]
   sha = "dims.sha"
   outcome = "dims.outcome"

   [when]
   field = "show"

   [clear]
   match = { kind = "sha", outcome = "success" }

   [display]
   channel = true
   threads = "develop CI red"
   ```

   - `trigger.subspace` is one concrete board subspace (`board/<topic>` or
     `board/ci/<topic>`). No wildcards in version 1. Mailbox triggers
     (`mailbox/self`) and the drain-hook path they needed are out of
     version 1 (Sam, 2026-11-15: "Cut one"): no consumer uses one, and the
     drain hook is a stdlib-only plugin script that cannot run a procedure.
   - `trigger.match` is equality on dimensions only; every pair must hold.
     Absent means every post on the subspace.
   - `args` maps each of the procedure's declared arguments to a source:
     `dims.<field>`, `body.<field>` (top-level key of a JSON body), `id`
     (the tuple id), `subspace`, or a literal written as `= "value"`. The
     reserved `now` and `repo` are filled by the host as RDR-240 says and may
     not be mapped. Every required argument must be mapped.
   - `when.field` names a boolean field of the procedure's result table; the
     aura shows only when it is `true`. Absent means always show.
   - `clear.match` (optional) is equality on dimensions, like
     `trigger.match`, against posts on the same subspace. A post that
     matches it removes this session's `aura: <threads value>` block without
     running the procedure; for `ci_develop_red`, the next green develop sha
     clears the red (Sam, 2026-11-15: "amend two"). A `[clear]` that a post
     could meet together with `trigger.match` refuses the file at `nx aura
     check`: that is the case when `trigger.match` is absent (every post is
     a trigger, so none could clear), or when no key is in both tables with
     different values. `ci_develop_red`'s tables differ on `outcome`, so no
     post meets both.
   - `display.threads` is required. It writes the procedure's `text` result
     field as a block titled `aura: <value>` in this session's threads
     entry, replacing the previous block of that title (the latest fire
     wins). The threads block is the only place procedure text appears. The
     write follows four rules (Sam, 2026-11-15: "amend two"):
     - The entry is the one in T2 project `threads` that the board's own
       reader (`nexus.console.threads.parse_session`) gives this session's
       id from its `session: <id>` line, found by that line, never by title.
     - The host never creates an entry. When none carries the line, the
       fire is logged with the aura name and nothing is written. When more
       than one carries it (the board's duplicate-session finding), nothing
       is written and `aura_threads_ambiguous` is logged with their titles.
     - The block is the heading `## aura: <value>` and one line, `state:
       <text>`. `<text>` keeps only printable ASCII, turns each newline or
       tab into a space, and is cut to 200 characters, so it cannot start a
       heading, an `ask:` line or another field. The error and the disabled
       notice of Technical Design 5 are that line's text, never more lines.
       `display.threads` itself is one line of printable ASCII of at most 80
       characters, or the parser refuses the file.
     - The host reads the entry, replaces only its own block (the heading
       and its `state:` line), and writes the whole entry back. One lock in
       the server process covers every such read-modify-write, for all
       auras, fires and clears, and a clear on an entry with no such block
       writes nothing. T2 has no compare-and-set, so an agent's
       `memory_put` between that read and write can be lost, and the
       write refreshes the entry's updated time (inferred, not read).
   - Loading. The MCP server loads the auras turned on for a session the
     first time Claude Code's session registry names it: at server start, or
     at a later 60 s re-resolve after a startup miss. A later miss does not
     unload them. A host with no registry loads none and logs one INFO
     `auras_not_loaded` line. A file the library or the parser refuses, an
     aura whose `[clear]` a post could meet together with its trigger (the
     `nx aura check` rule, applied again at load), and a second aura writing
     a threads block another loaded aura writes are each refused by name. A
     load that fails, on a bad `auras:` value for one, is logged, releases
     any topic it already held and is not retried in that server.
   - Auras do not load in a `claude -p` child. `resolve_session_name` names
     only an interactive entry, and a child that inherited its parent's
     session id resolves to the parent's entry, so the server also walks up
     its own process tree to the nearest process that has a registry entry
     of any kind and loads only when that process is the interactive
     entry's. A child's own claude writes a registry file under its own pid
     and session id (observed 2026-11-15, with kind `interactive`) and is
     nearer. When the walk cannot reach a registered process, it loads none
     and says why.
   - `display.channel = true` also sends a channel notification, which is a
     fixed template and never carries procedure text (RDR-211; Sam,
     2026-11-15: "Fixed template only"). It holds only the aura name, the
     subspace, the tuple id and a pointer to the threads block: `aura
     <name> fired on <subspace> tuple <id>; see the "aura: <threads value>"
     block in your threads entry`. The aura name and the threads value come
     from the aura file, and the subspace and tuple id from the engine row,
     so nothing a tuple writer controls reaches the channel. It is sent
     whether or not the threads block could be written. A notice the
     channel does not take at once is logged; one held for a shut gate goes
     out when the gate opens.
   - `[limits]` (optional) lowers RDR-242's limits for this aura's fires
     (Technical Design 6); its keys are the limit names, and a value above
     the default or an unknown name refuses the file.
   - Any other key, a missing required key, or a type mismatch refuses the
     file.

2. **Checking.** `nx aura check [NAME]` parses every aura (or one), resolves
   its procedure through RDR-240's library, checks the argument mapping
   against the procedure's header (every required argument mapped, none
   the header lacks), and checks the `trigger.match`, `clear.match` and
   `dims.<field>` fields against the subspace template's dimensions and
   `body.<field>` against its `doc.body` (`tuple_registry` with docs). It
   also refuses a dead `[clear]` (Technical Design 1) and two auras that
   share a `display.threads` value, since their fires would overwrite one
   block; that check is static, spans both roots, and ignores on and off.
   A shadowed or badly named file is reported, not dropped. `nx aura list`
   shows name, summary, trigger, on or off, and the level that decided it.
   Both run without the engine only for the parts that need none, and say
   which checks were skipped, an unscanned repo root (switch off) and a
   configured name that matches no aura included.

3. **Enabling.** Every aura ships off, `ci_develop_red` included, and runs
   only when turned on by name (Sam, 2026-11-15: "Off by default"). Nexus
   ships nothing that turns one on: each user opts in (Sam, 2026-11-15:
   "Each user opts in"). The switches use nexus's config hierarchy, read
   when the MCP server starts, in which "Environment variables (`NX_*`)"
   have the "highest priority", then `.nexus.yml`, then
   `~/.config/nexus/config.yml` (`docs/configuration.md` § Config
   Hierarchy):
   - In config, `auras: {<name>: on}` turns an aura on and `auras: {<name>:
     off}` turns it off. A nexus developer turns `ci_develop_red` on in
     `~/.config/nexus/config.yml`.
   - `NX_AURAS_OFF` overrides the config: `all` turns every aura off, and a
     comma-separated list of names turns those off. It covers a CI job or
     any other process that should run none.
   - `nx aura list` shows each aura as on or off and the level that decided
     it (default, global config, repo config, env).
   - An aura's trigger subscription is aura-owned and is kept out of the
     `board_topics` list in the channel status record. The Stop-hook wake
     guard (`conexus/hooks/scripts/stop_wake_guard.py`) reads that list and
     counts any `board/ci/[^/]+--done` topic in it as a covering CI wake
     (the status record's comment: "a live waiter, the channel flag and a
     board/ci/*--done topic together are a CI wake"). A global opt-in would
     otherwise put `board/ci/nexus-develop--done` in every repo's sessions
     and let a turn stop on a CI wait nothing will wake. The session's own
     subscription to the same topic still counts.
   - A row on a topic only an aura holds is evaluated and not announced to
     the agent; `display.channel` is its only channel notice. A topic the
     session also subscribes keeps its announcement unchanged. The
     session's `tuple_unsubscribe` never removes an aura's hold, and an aura
     topic counts toward the session's `MAX_BOARD_TOPICS`.
   A repo-root aura follows RDR-242
   residual S5 for repo-root procedures (confirmed by Sam, 2026-11-13). Enabling
   subscribes the trigger subspace, within `MAX_BOARD_TOPICS`; an aura past
   the limit is refused at load with its name.

4. **Evaluation.** In `ChannelWaiter`, after a row is received and before
   it is rendered: for each enabled aura whose subspace and match fit the
   row, build the arguments, run the procedure through `run_script` in a
   thread (the waiter's loop is never blocked), test `when`, and display.
   On a topic the session subscribes the row's announcement is unchanged;
   on a topic only an aura holds it is not announced (Technical Design 3). A
   row that matches an aura's `clear.match` removes its block instead and
   runs nothing. No post meets both tables: such an aura is refused at
   `nx aura check` and at load.

5. **Failure.** A failure is a `ScriptRunError` other than
   `ScriptBusyError` (a script error, a limit, a worker that died), an
   argument source the tuple lacks, a missing or mistyped result field, a
   display error, or any other exception in the fire. It is logged with the
   aura name and the tuple id, and the block's line becomes `aura error:
   <kind> on tuple <id>`. A failing aura never blocks the waiter or the
   announcement. Three consecutive failures disable the aura for the rest of
   the session, and the line becomes `aura disabled after 3 failures:
   <kind>`; a disabled aura neither fires nor clears. A success, a `when`
   of false included, resets the count. `ScriptBusyError` is not a failure:
   the run path's four workers are shared with `procedure_run` and `nx run`,
   so the item goes back to the head of its queue and is retried every 5 s,
   up to 12 times, and is then dropped and logged (`aura_busy_dropped`). A
   clear removes the whole block, error line included.

6. **Limits.** Each fire uses RDR-242's default limits, or lower ones the
   aura file declares. Each aura has one queue that holds its fires and
   clears together, in arrival order, and runs one item at a time, so a
   clear that arrives during a fire lands after it. With more than 20 items
   waiting the oldest fire is dropped with a logged count; a clear is never
   dropped, and a clear queued straight after another clear is not queued,
   since it removes nothing more.

7. **The first consumer.** `ci_develop_red.aura.toml` and a procedure
   `ci_red_notice.lua` (header: `args: sha:string, outcome:string`,
   `grants: none`) that returns `{show = true, text = "develop red at
   <short sha>: diagnose before the next push (worktrees rule 7)"}`. Both ship
   in the package root.

### Existing Infrastructure Audit

| Piece | Exists | Used as |
|---|---|---|
| Procedure library, header, `run_script` | Yes (RDR-240, 242) | Unchanged; an aura calls it. |
| Channel waiter, board announce-once | Yes (RDR-213) | The evaluation site. |
| Dictionary with field docs | Yes (RDR-239) | `nx aura check` reads it. |
| Threads board entry | Yes (RDR-245) | The durable display. |
| Config hierarchy (env, `.nexus.yml`, `~/.config/nexus/config.yml`) | Yes (`docs/configuration.md`) | The on and off switch. |
| Similarity on tuples | No | Out of scope. |
| Write verbs | No (RDR-242 v1) | Out of scope. |

### Decision Rationale

An aura file keeps the declaration (when and where) apart from the logic
(what), so one procedure can serve several auras and an accepted header
format stays untouched. RDR-249 narrows RDR-212's exclusion of "any
reaction loop" on purpose (Sam, 2026-11-15), and the narrowing is what
keeps clear of RDR-110's failure. RDR-110 shipped an engine-side loop
(`_BindingWatcher`) together with its substrate and its consumers, across
nine RDRs. An aura's loop runs in the session's own server, only while a
session that turned it on is open, and only for that session; it reads
through the sandbox and can only display; it adds nothing to the engine;
and it ships with one consumer, off by default. Display-only actions
follow from RDR-242's read-only version 1 rather than a separate decision.

## Alternatives Considered

### Alternative 1: Trigger and display lines in the procedure header

One file per aura, but RDR-240's header parser ignores a fifth line, so
making one count amends an accepted format, and a procedure could not be shared by two auras.
Rejected by Sam (2026-11-12, "1").

### Alternative 2: Auras declared in the dictionary's template doc blocks

Ties a reaction to the subspace's published meaning, but the template parser
has a closed key set, so every new aura is an engine release. That restores
Gap 2's cost. Not chosen (Sam, "c").

### Alternative 3: Engine-side evaluation

Fires without an open session, but it is the `_BindingWatcher` pattern that
was scrapped, and it is outside the narrowing of RDR-212 that this RDR makes
(client-side only). Would need Sam to override RDR-212.

### Briefly Rejected

- Free-form YAML auras evaluated by the agent (the client-side candidate A):
  logic outside the sandbox, which is what RDR-242 exists to prevent.
- Polling triggers: distort memory heat and TTL (nexus-mdbds, 2026-11-08).

## Trade-offs

### Consequences

- An aura fires only while a session that enabled it is open, and only on
  posts created after that session subscribed the topic (less 30 s). A red
  posted at night, with no session open, is not evaluated by the first
  session opened after it; the post stays readable with `tuple_rd`.
- Several sessions enabling one aura each write their own threads block, by
  decision: there is no lock tuple.
- `ci_develop_red` ships off, so it reaches no install until its user turns
  it on. A user who turns it on in `~/.config/nexus/config.yml` gets it in
  every repo's sessions: it holds one of the `MAX_BOARD_TOPICS` (32) board
  subscriptions there and fires only when nexus's develop CI posts. Because
  the subscription is aura-owned, it never arms the Stop-hook CI wake guard
  (Technical Design 3).
- `clear.match` is equality on dimensions, so `ci_develop_red`'s clear
  matches kind and outcome only: a late green for an older sha clears the
  block of a newer red. Whether a clear should also match the sha is Sam's
  choice (asked on the threads board, `aura-clear-sha`).

### Risks and Mitigations

- A noisy subspace runs many workers: the match filter is on dimensions,
  checked before any worker starts, and the per-aura queue is capped.
- An aura hides an announcement the agent should have read: it never
  suppresses the announcement; it adds to it.
- A tuple writer controls the procedure's arguments, and so its text: that
  text never reaches the channel, which carries only the fixed template
  (RDR-211), and lands only in the threads block.

### Failure Modes

- Malformed aura file: refused at load, named in the server log and in
  `nx aura check`.
- Procedure error or timeout: logged, shown once, disabled after three.
- Engine down: no arrivals, no fires; the waiter's existing reconnect
  applies.

## Implementation Plan

### Prerequisites

RDR-240 Phase 1 (runner, library roots) shipped; RDR-213 waiter on
develop. The adapter's `FOLD_RESULT_DIMS` switch on for the CI consumer's
dimensions.

### Minimum Viable Validation

The first consumer, enabled in one session, writes an `aura: develop CI
red` block within a minute of a real red develop sha, and nothing for a
green one; its channel notification is the fixed template, with no
procedure text; the next green develop sha removes the block; and
`NX_AURAS_OFF=ci_develop_red` stops it.

### Phase 1 (CLOSED, 2026-11-15): The aura file, its check and its list

#### Step 1: Parser and loader

`nexus.auras` package: parse `<name>.aura.toml` with `tomllib`, the closed
key set of Technical Design 1, library lookup and shadow refusal shared with
RDR-240. Tests: every refusal kind, a valid file, shadowing.

#### Step 2: `nx aura check` and `nx aura list`

Argument mapping against the procedure header; trigger, clear and `dims`
fields against the dictionary, and `body` fields against `doc.body`; the
dead-`[clear]` and shared-threads refusals; the on and off resolver of
Technical Design 3, which `nx aura list` shows. Tests against a real
engine's registry (self-provisioned).

### Phase 2: Evaluation

#### Step 1: Waiter path

Resolve on and off from config and `NX_AURAS_OFF`, subscribe (aura-owned,
outside the status record's `board_topics`), match, run in a thread, condition, channel and
threads display under its four write rules, the clear match, the failure and queue rules. Tests
with a real engine and a real worker: one fire, no fire on a non-match, a
failing procedure disabled after three, a green post that removes the red
block, no write when the session has no entry, no aura in a `claude -p`
child; with an aura subscribed to
`board/ci/nexus-develop--done` and no session subscription to it, the Stop
hook still blocks a turn that ends on a develop-CI wait.

### Phase 3: The first consumer

`ci_red_notice.lua` and `ci_develop_red.aura.toml` in the package root,
shipped off; measure one fire's cost (the Critical
Assumption).

### New Dependencies

None (`tomllib` is in the standard library).

## Test Plan

Unit tests for the parser's refusals and the argument mapping. Integration
tests against a self-provisioned engine for subscription, announce-once
evaluation, the clear match and the threads block, with a tuple whose
procedure text carries a newline, a `## ` heading and an `ask:` line, which
must land as one capped `state:` line. A test that posts a
tuple whose dimensions and body carry instruction-like text and asserts the
channel notification equals the fixed template, with none of that text in
it (RDR-211). Tests of the switch at each level: the default (off), an
`on` in global config, an `off` in repo config over a global `on`, and
`NX_AURAS_OFF` over a config `on`. A Stop-hook test: an aura-owned
`board/ci/*--done` subscription does not count as a CI wake. A gate test that fails if no aura was evaluated (non-vacuity).

## Resolved Questions

Sam answered the draft's three open questions on 2026-11-13, choosing from
a picker in halcyon's session:

1. Duplicate displays: "One block per session". Each enabled session shows
   its own block; no lock tuple.
2. Default enablement: "ci_develop_red on in nexus". `ci_develop_red` is on
   in the nexus repo's `.nexus.yml`; every other aura is off everywhere.
   Superseded 2026-11-15 by answers 5 and 6 (6 is in force): `.nexus.yml` is gitignored, so it
   could not ship (gate round 1, S1).
3. Repo-root auras: "Confirm S5". They follow RDR-242 residual S5.

Gate round 1 raised two more, answered 2026-11-15:

4. Channel content (Critical 1): "Fixed template only", by dialog in
   halcyon's session. The channel notification carries the aura name, the
   subspace, the tuple id and a pointer to the threads block, with no
   procedure text; RDR-211 stands unchanged.
5. Enablement (S1): "Ship it on by default", by dialog in halcyon's
   session; confirmed in marginalia's session with "On; user config and env
   var". Superseded the same day by answer 6.
6. Default (gate round 2 Critical): "Off by default", by dialog in
   halcyon's session; confirmed in marginalia's session, which also asked
   whether nexus turns `ci_develop_red` on: "Each user opts in". Auras ship
   off; a user turns one on in config, and `NX_AURAS_OFF` still turns them
   off (Technical Design 3).
7. Accept dispositions (round 1 findings S2, S3, S4, carried as gate
   residuals): "Cut one,
   amend two, then accept", by dialog in halcyon's session. Mailbox
   triggers and the drain path are cut from version 1, and the threads
   write rules and the clear match are added (Technical Design 1).

## References

- T2 `nexus/design-declarative-auras.md` (design of record).
- RDR-110, 111, 211, 212, 213, 220, 239, 240, 242, 245; postmortem
  `docs/postmortem/2026-05-16-rdr110-113-remediation-chain.md`.
- WeakAuras2 wiki (trigger types, conditions, actions, regions), via mirror,
  read 2026-11-11.

## Revision History

- 2026-11-12: drafted (marginalia) from Sam's choices "c" and "1".
- 2026-11-13: Sam's answers to the three open questions folded in (one
  block per session; `ci_develop_red` on in nexus; S5 confirmed).
- 2026-11-15: Gate round 1 — BLOCKED (1 Critical, 8 Significant, 1 ship-blocker(s)); commit `d0c72fbce`; critique `nexus_rdr/249-gate-critique-2026-11-15-r1`.
- 2026-11-15: Gate round 2 — BLOCKED (1 Critical, 1 Significant, 1 ship-blocker(s)); commit `0b769887e`; critique `nexus_rdr/249-gate-critique-2026-11-15-r2`.
- 2026-11-15: Gate round 3 — PASSED (0 Critical, 1 Significant, 0 ship-blocker(s)); commit `8dec1a48b`; critique `nexus_rdr/249-gate-critique-2026-11-15-r3`.
- 2026-11-15: Accepted (Sam). Residuals: S2 a122b329b (cut), S3 a122b329b, S4 a122b329b (fix check 249-fix-check-a122b329b); S6 to nexus-mdbds.10.1 (Phase 1 parser and loader); S-r3-1, S7, S8 to nexus-mdbds.11.1 (Phase 2 waiter evaluation); the fix checks' rows to .10.1, .10.2, .11.1 and .12.1 (plan: T2 nexus/plan-rdr-249-declarative-auras.md).
- 2026-11-15: S6 (nexus-mdbds.10.1): RDR-212's exclusion of "any reaction loop" is narrowed on purpose, client-side only (Sam, by dialog in halcyon's session: "Narrow it on purpose"); counted fix-check wording observations folded in (drain-hook bullet, `[limits]` in Technical Design 1, Resolved Question 2's pointer, the measured worker start).
- 2026-11-15: Technical Design 1 and 2 and Phase 1 Step 2 amended to match the reviewed `nx aura check` (nexus-mdbds.10.2, its feat and fix commits; standing grant 3). The `[clear]` rule was too narrow as "equal tables": a `[clear]` is refused when a post could meet both tables, that is, `trigger.match` is absent or no key is in both with different values. The check also covers `clear.match`, `dims` and `body` fields and refuses two auras sharing a threads block.
- 2026-11-15: Technical Design 1, 3, 4, 5 and 6 and Consequences amended to match the reviewed waiter path (nexus-mdbds.11.1, review round 1 and its fixes; standing grant 3): loading and the `claude -p` rule as built, the not-announced rule for an aura-only topic, the threads write rules, the failure kinds and the busy retry, and the per-aura queue.
