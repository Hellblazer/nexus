---
title: "Declarative Auras: A Trigger, a Condition and a Display Around a Sandboxed Procedure"
id: RDR-249
type: Feature
status: draft
priority: medium
author: Sam
reviewed-by: self
created: 2026-11-12
accepted_date:
related_issues: ["nexus-mdbds"]
related_rdrs: [RDR-239, RDR-240, RDR-242, RDR-212, RDR-213, RDR-220, RDR-245, RDR-110, RDR-111]
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
excluded "bindings and any reaction loop" from the cockpit on purpose. A
stage 2 design has to give reactions a form without rebuilding what was
scrapped.

## Relationship to Prior RDRs

| RDR | Relationship | What it settles here |
|---|---|---|
| RDR-240 Procedures as Sandboxed Lua Scripts (accepted 2026-11-07) | Builds on it | The procedure library (`src/nexus/procedures/` shipped, `.nexus/procedures/` per repo), the four-line header, the argument types and the reserved arguments `now` and `repo`. An aura names a procedure and maps tuple fields onto its declared arguments. The header is not changed. |
| RDR-242 Sandboxed Lua Runtime (accepted 2026-11-07) | Builds on it | `run_script(name, args, limits)`, the worker process, the read-only verb table and the default limits (50M instructions, 64 MiB, 60 s wall, 1 MiB result). Its version 1 has no write verb; a write waits for its approval document (its Technical Design 1 and 10). So an aura's procedure cannot write, and this RDR adds no write path. |
| RDR-239 Tuple-Space Dictionary (closed) | Uses it | `GET /v1/tuples/registry?doc=1` describes each subspace's fields. `nx aura check` reads it to refuse a trigger filter or an argument mapping that names a field the template does not have. |
| RDR-213 channel push (amends RDR-211) | Hosts it | The channel waiter is the MCP server's one background task. It parks a wait over the session's subscriptions, and the engine announces each board post to a subscriber once (`nexus.tuple_deliveries`). Auras are evaluated on what the waiter receives. |
| RDR-220 CI board | First consumer's source | `board/ci/<topic>` and its `--done` wake, with `kind`, `sha` and `outcome` dimensions. |
| RDR-245 Structured asks on the threads board | Display target | The threads board entry (T2 project `threads`, one per session) is where a durable display lands. |
| RDR-212 Cockpit surfaces | Constraint honoured | Its exclusion of bindings and any engine reaction loop stands. Auras are evaluated in the session's own MCP server, never in the engine. |
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
- `src/nexus/hooks/mailbox_drain.py` runs on `UserPromptSubmit`, claims and
  shows mail. It is the floor for mail when no channel is live.
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
  as it is evaluated on the waiter's path.
- **Verified (source):** RDR-220's `--done` wake carries `kind=sha`, `sha` and
  `outcome` as dimensions (when the adapter's `FOLD_RESULT_DIMS` switch is
  on), so a red develop sha can be matched on dimensions alone, without
  reading the body.
- **Verified (source):** RDR-240's header parser refuses anything past four
  header lines (`nexus.lua_runtime.header.parse`). A trigger in the header
  would amend an accepted format; Sam chose a separate file.
- **Documented (RDR-242 TD1, TD10):** no write verb exists, and adding one
  waits for an approval document that only Sam signs.
- **Documented (nexus-mdbds comment, 2026-11-08):** `memory.get` counts as an
  access and raises a memory's heat and TTL. A polling aura would distort
  both. Event triggers avoid polling.

### Critical Assumptions

- **Assumed:** running a procedure per matching arrival costs a worker start
  (a `python -I` process), on the order of a few hundred milliseconds, which
  is acceptable for posts that arrive minutes apart. To verify in Phase 2 by
  measuring a fire on the CI consumer.
- **Assumed:** one session per repo is enough to carry a given aura's
  display. Several sessions subscribed to the same topic each run the aura
  and each write their own threads block; that duplication is accepted for
  version 1 (see Open Questions).

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

   [display]
   channel = true
   threads = "develop CI red"
   ```

   - `trigger.subspace` is one concrete board or mailbox subspace
     (`board/<topic>`, `board/ci/<topic>`, or `mailbox/self`, which stands
     for the session's own mailbox). No wildcards in version 1.
   - `trigger.match` is equality on dimensions only; every pair must hold.
     Absent means every post on the subspace.
   - `args` maps each of the procedure's declared arguments to a source:
     `dims.<field>`, `body.<field>` (top-level key of a JSON body), `id`
     (the tuple id), `subspace`, or a literal written as `= "value"`. The
     reserved `now` and `repo` are filled by the host as RDR-240 says and may
     not be mapped. Every required argument must be mapped.
   - `when.field` names a boolean field of the procedure's result table; the
     aura shows only when it is `true`. Absent means always show.
   - `display.channel` sends the procedure's `text` result field as a
     channel notification. `display.threads` writes the same text as a block
     titled `aura: <value>` in this session's threads entry, replacing the
     previous block of that title (the latest fire wins). At least one
     display is required.
   - Any other key, a missing key, or a type mismatch refuses the file.

2. **Checking.** `nx aura check [NAME]` parses every aura (or one), resolves
   its procedure through RDR-240's library, checks the argument mapping
   against the procedure's header, and checks the trigger's fields against
   the dictionary (`tuple_registry` with docs). `nx aura list` shows name,
   summary, trigger and whether it is enabled. Both run without the engine
   only for the parts that need none, and say which checks were skipped.

3. **Enabling.** No aura runs unless enabled. An aura is enabled by name in
   the repo's `.nexus.yml` (`auras: [ci_develop_red]`) or in the user config,
   read when the MCP server starts. A repo-root aura follows whatever Sam
   decides for repo-root procedures (RDR-242 residual S5). Enabling
   subscribes the trigger subspace, within `MAX_BOARD_TOPICS`; an aura past
   the limit is refused at load with its name.

4. **Evaluation.** In `ChannelWaiter`, after a row is received and before
   it is rendered: for each enabled aura whose subspace and match fit the
   row, build the arguments, run the procedure through `run_script` in a
   thread (the waiter's loop is never blocked), test `when`, and display.
   The normal announcement of the row is unchanged. Mail matched by a
   `mailbox/self` aura is evaluated on the waiter when the channel is live,
   and by the drain hook at the next prompt otherwise. The aura records the
   tuple ids it handled in the session's state directory, so the two paths
   never both fire on one row.

5. **Failure.** A `ScriptError`, a missing result field, or a display error
   is logged with the aura name and the tuple id, and shown once in the
   threads block as an error line. A failing aura never blocks the waiter or
   the announcement. Three consecutive failures disable the aura for the
   rest of the session, and the threads block says so.

6. **Limits.** Each fire uses RDR-242's default limits, or lower ones the
   aura file declares. At most one fire per aura at a time; an arrival while
   one runs waits behind it, and more than 20 waiting drops the oldest with a
   logged count.

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
| Mailbox drain hook | Yes | The no-channel floor for mailbox auras. |
| Dictionary with field docs | Yes (RDR-239) | `nx aura check` reads it. |
| Threads board entry | Yes (RDR-245) | The durable display. |
| Similarity on tuples | No | Out of scope. |
| Write verbs | No (RDR-242 v1) | Out of scope. |

### Decision Rationale

An aura file keeps the declaration (when and where) apart from the logic
(what), so one procedure can serve several auras and an accepted header
format stays untouched. Evaluating in the session's own server keeps the
scrapped engine reaction loop scrapped: an aura runs only while a session
that enabled it is open, and only for that session. Display-only actions
follow from RDR-242's read-only version 1 rather than a separate decision.

## Alternatives Considered

### Alternative 1: Trigger and display lines in the procedure header

One file per aura, but RDR-240's header refuses a fifth line, so it amends
an accepted format, and a procedure could not be shared by two auras.
Rejected by Sam (2026-11-12, "1").

### Alternative 2: Auras declared in the dictionary's template doc blocks

Ties a reaction to the subspace's published meaning, but the template parser
has a closed key set, so every new aura is an engine release. That restores
Gap 2's cost. Not chosen (Sam, "c").

### Alternative 3: Engine-side evaluation

Fires without an open session, but it is the `_BindingWatcher` pattern that
was scrapped, and RDR-212 excludes it. Would need Sam to override RDR-212.

### Briefly Rejected

- Free-form YAML auras evaluated by the agent (the client-side candidate A):
  logic outside the sandbox, which is what RDR-242 exists to prevent.
- Polling triggers: distort memory heat and TTL (nexus-mdbds, 2026-11-08).

## Trade-offs

### Consequences

- An aura fires only while a session that enabled it is open. A red at
  night is shown to the first session opened after it only if the post is
  still within its retention and that session's waiter receives it.
- Several sessions enabling one aura each write their own threads block.

### Risks and Mitigations

- A noisy subspace runs many workers: the match filter is on dimensions,
  checked before any worker starts, and the per-aura queue is capped.
- An aura hides an announcement the agent should have read: it never
  suppresses the announcement; it adds to it.

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
green one.

### Phase 1: The aura file, its check and its list

#### Step 1: Parser and loader

`nexus.auras` package: parse `<name>.aura.toml` with `tomllib`, the closed
key set of Technical Design 1, library lookup and shadow refusal shared with
RDR-240. Tests: every refusal kind, a valid file, shadowing.

#### Step 2: `nx aura check` and `nx aura list`

Argument mapping against the procedure header; trigger fields against the
dictionary. Tests against a real engine's registry (self-provisioned).

### Phase 2: Evaluation

#### Step 1: Waiter path

Enable from config, subscribe, match, run in a thread, condition, channel and
threads display, the handled-id record, the failure and queue rules. Tests
with a real engine and a real worker: one fire, no fire on a non-match, a
failing procedure disabled after three.

#### Step 2: Drain path for `mailbox/self`

The same evaluation from the drain hook when no channel is live; the
handled-id record prevents a second fire.

### Phase 3: The first consumer

`ci_red_notice.lua` and `ci_develop_red.aura.toml` in the package root;
measure one fire's cost (the Critical Assumption).

### New Dependencies

None (`tomllib` is in the standard library).

## Test Plan

Unit tests for the parser's refusals and the argument mapping. Integration
tests against a self-provisioned engine for subscription, announce-once
evaluation, the drain fallback and the threads block. A gate test that fails
if no aura was evaluated (non-vacuity).

## Open Questions for Sam

1. Duplicate displays: accept one threads block per enabled session, or
   limit a given aura to one session per repo (a lock tuple)?
2. Default enablement of shipped auras: off everywhere (this draft), or
   `ci_develop_red` on in the nexus repo's `.nexus.yml`?
3. Repo-root auras follow RDR-242 residual S5; confirm.

## References

- T2 `nexus/design-declarative-auras.md` (design of record).
- RDR-110, 111, 212, 213, 220, 239, 240, 242, 245; postmortem
  `docs/postmortem/2026-05-16-rdr110-113-remediation-chain.md`.
- WeakAuras2 wiki (trigger types, conditions, actions, regions), via mirror,
  read 2026-11-11.

## Revision History

- 2026-11-12: drafted (marginalia) from Sam's choices "c" and "1".
