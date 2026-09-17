---
title: "ORB Cockpit Surfaces over the Tuple Space"
id: RDR-212
type: Architecture
status: draft
priority: medium
author: Sam
reviewed-by: self
created: 2026-09-16
accepted_date:
related_issues: []
related_rdrs: [RDR-111, RDR-118, RDR-119, RDR-127, RDR-064, RDR-205, RDR-206, RDR-211]
related_external: [palinex-rdr-001, a2ui-v0.9-spec, textual-8.2, claude-code-hooks-reference]
related_tests: []
---

# RDR-212: ORB Cockpit Surfaces over the Tuple Space

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.
> Prose: see REGISTER.md beside this template. Write for a smart reader who
> may not know the jargon; define terms on first use; simplified, never
> simplistic.

**Provenance.** Sam, 2026-09-16, after a research synthesis on interface
grammar, the tuple-space substrate and the palinex renderer: "I was thinking
of the original idea of palinex and the orb surface. Can you bring that back
into frame?" then "create that post substrate RDR." RDR-111's scrap reason
(2026-05-19) says: "cockpit/ORB work, if revived, files a separate
post-substrate RDR." This is that RDR. Revised the same day after Sam's
correction that the cockpit runs at the agent event layer of the tuple space
("I believe I mixed this up some time ago when this was all written"); the
first draft had the panels polling rows, which was the same mistake RDR-111
made with SQL.

Terms used throughout:

- **Tuple space**: the shared store of small records that sessions, agents,
  hooks and scripts coordinate through (RDR-205), held in the engine, the Java
  service that owns the database. A **tuple** has keys, dimensions and a short
  body; a **subspace** is a named part of the space such as `mailbox/<address>`;
  a **template** is the engine's registered definition of one kind of subspace.
- **out, rd, in**: the Linda operations. `out` appends a tuple. `rd` reads
  matching tuples without taking them, in `(created_at, id)` order from an
  optional **cursor**. `in` takes one tuple, giving the caller a **claim** for a
  limited **lease**, ended by `ack` (consumed) or `nack` (returned). A tuple
  that fails too often becomes a **dead letter**.
- **Event layer**: the reading of the tuple space as an append-only log. Every
  `out` is an event; a subspace whose template forbids taking is a stream that
  many readers walk with a cursor. The current tuples are a fold over that log
  (docs/exploration/agentic-cockpit.md, "The substrate is log-structured").
- **ORB**: the Observable Relay Bus of RDR-111: the tuple space as the
  switchboard every actor coordinates through, and the harness's hook events
  projected into it as tuples, so a **cockpit** of panels can show what is
  running, what it holds, and what just happened.
- **Hook**: a script the Claude Code harness runs at a lifecycle point
  (SessionStart, PreToolUse, SubagentStop, and so on), reading the event as
  JSON on stdin.
- **Projection**: a consumer's state folded from events, rebuildable by replay.
- **a2ui**: a declarative UI specification (Google, Apache-2.0) in which an
  agent emits a JSON description of components and data and a host renders it.
- **palinex**: the downstream project (RDR-127) that owns a2ui surface
  emission: typed Python builders, a single-file browser renderer, delivery
  shapes, and a mandatory markdown sidecar.
- **Textual**: a Python terminal-application framework built on Rich, with a
  CSS dialect for styling and keyed table widgets.

## Problem Statement

RDR-111 named two architectural moves and three gaps. Move 1, the tuple space
as switchboard, exists: RDR-205 and RDR-206 put a Linda tuple space in the
engine, and the directory and mailbox templates are how sessions find and
message each other. Move 2, hook events projected into the space, exists for
two of twelve hook kinds: the SubagentStart and SubagentStop hooks write
`start` and `report` rows into `ledger/<session_id>` through
`conexus/hooks/scripts/tuple_ledger_project.py`. RDR-111's third gap, a
situational-awareness surface, is untouched. Nothing draws any of it; the
operator reads `nx tuple watch` ping lines and raw tool output.

RDR-111 also crossed its own layers. It designed the hook-to-tuple bridge as an
event layer and then had its panels read the storage layer by SQL over the
`tuples` table, which is why RDR-112 had to forbid the cockpit from opening
the database. RDR-118 pushed from the other side by storing surfaces as
tuples. Both were storage framings of what was meant to be an event consumer.
This RDR keeps the cockpit on the event layer end to end: hooks and the
engine emit events as tuples, the cockpit walks them with a cursor and folds
them into panels, and nothing reads a table.

### Enumerated gaps to close

#### Gap 1: No situational-awareness surface

There is no panel that answers "what is held right now, by whom, for how much
longer" or "what happened in the last few minutes" across the tuple space.
`tuple_stats` gives one subspace's counts at one instant; `tuple_rd` gives raw
rows; the watcher pings on mailbox arrivals only.

#### Gap 2: Claim transitions are logged but not emitted

The engine writes every claim, ack, nack, renew, expire and dead transition to
`tuple_claim_log` (five call sites in `TupleRepository.java`: 824, 836, 863,
934, and 1135 to 1192), and nothing can read it: no MCP tool or CLI verb, no
index for a subspace-scoped scan (only `tuple_id` and `(tenant_id, expires_at)`
indexes exist, tuples-001 and tuples-005), and none of the five sites wakes a
waiting reader, because `signalAll` is called only from `out` (line 440) and
from the reply written by an ack (line 1009). A claim changing state is
invisible to a consumer until it re-reads the row.

#### Gap 3: Ten of twelve hook kinds are not projected

`conexus/hooks/hooks.json` wires twelve hook events. Only SubagentStart and
SubagentStop reach the tuple space. Session start and end, stop, stop failure,
compaction, user prompt, notification, tool call intent and completion, and
permission requests fire and vanish. The measured rates (Research Findings)
show why the milestone discipline matters: tool calls run at 37 to 66 per hour
per session, agent starts at about 1.5 per hour.

#### Gap 4: No subspace-generic consumer

The only long-running consumer, `src/nexus/tuple_watch.py`, is mailbox-typed
end to end (lines 5, 415-424, 498) and RDR-211 deletes it. Its cursor
discipline (a safety lag against out-of-order commits, a seen set, a persisted
per-address state file) is right and is not reusable as written.

#### Gap 5: No terminal host for a2ui surfaces, and the browser host is a snapshot

palinex's RDR-001 named terminal hosts as future work. Its browser path
through MCP delivers a surface once (`wrap_as_mcp_ui_resource`,
`src/palinex/__init__.py` 732-772) although the renderer supports in-place
update (`web/index.html` 610-639). An ambient cockpit needs a host that keeps
folding events into the view.

#### Gap 6: No design grammar of record for coordination surfaces

A sourced grammar exists as research (T3 interface-design,
`research-coordination-display-grammar-2026-09-16`, 34 rules;
`research-tuple-surface-design-synthesis-2026-09-16`). No design record adopts
it.

## Relationship to Prior RDRs

| Prior RDR | Relationship | What it means for this one |
| --- | --- | --- |
| RDR-111 (ORB, abandoned) | Origin | Its two moves are real by other means; its Gap 3 is this Gap 1; its seven event subspaces become one `events/<session_id>` template here; its SQL panels are the error this RDR corrects. Bindings, the connection manifest and auto-layout stay out, by its own scrap lesson. |
| RDR-118 (surfaces as tuples, superseded) | Origin | Adopted a2ui. `surface_cell` and recursive surfaces are not revived. |
| RDR-119 (cockpit UI fabric, abandoned) | Origin | Its notcurses catalog table is replaced by an a2ui-to-Textual table. Its demotion cascade is reduced to two fixed tiers. |
| RDR-127 (palinex is downstream, closed) | Precedent | Nexus ships no rendering code. The cockpit process, the producers and the terminal catalog live in palinex; nexus and the engine ship the event layer. |
| RDR-064 (nx console, closed) | Precedent | Rejected a terminal UI because the operator wants a separate window; textual-serve keeps that option. |
| RDR-205, RDR-206 (tuple space, closed) | Origin | The substrate. The claim state machine the panels fold is theirs. |
| RDR-211 (board, queue, lock; draft) | Adjacent draft | Boundary below. |

Scope boundary with RDR-211: RDR-211 owns the board, queue and lock templates,
`release`, the multiplexed `wait`, the lock flag, per-template caps, doctor
rows and the watcher's deletion. This RDR owns the two event templates, the
write-through projection of claim transitions, the hook bridge for the ten
unprojected kinds, the cockpit consumer, the two panels, the terminal catalog
and the grammar. The cockpit polls with non-parking reads until `wait` ships,
then subscribes; both RDRs' engine work can ride one cut (Open Question 3).

## Context

### Background

RDR-111 was accepted on 2026-05-13 and scrapped six days later with eight
sibling RDRs: nine RDRs, 67 stranded beads
(`docs/postmortem/2026-05-16-rdr110-113-remediation-chain.md`). The substrate
went on alone and shipped as RDR-205. The cockpit was never found wrong; it
was found entangled, and its panels were designed at the wrong layer.

The exploration behind RDR-111 (docs/exploration/agentic-cockpit.md) states
the layer precisely: the tuple space is an append-only event log per tier with
the current tuples as a fold over it; subscriptions are offset-tracking
consumers in the shape of Kafka or Redis Streams, never callbacks; and
semantic events are milestones, one tuple per tool call, never one per output
line. The ledger template is that design in production for two event kinds.

Six research rounds on 2026-09-16 established the rest: what a coordination
panel should look like (grammar), what the substrate exposes (state machine,
limits), what the renderer allows (envelope), whether the claim log can be an
event stream (engine), which hook events to project and at what rate (hooks),
and what a cursor consumer may assume (consumers). Their records are listed in
References.

### Technical Environment

- Engine: `service/`, Java, Postgres 17. `TupleRepository.java` (`rd` at
  583-619, `queryOnce` at 621-656, `writeOut` at 402-430 with `signalAll` at
  440, `computeId` at 525-557, claim-log inserts at 824-1192),
  `TupleLimits.java`, `tuples/TupleWaitRegistry.java`, templates under
  `service/src/main/resources/tuples/templates/`, changelogs tuples-001,
  tuples-002, tuples-005.
- Client: `src/nexus/db/t2/http_tuple_store.py` (`rd` at 517-543), MCP tools
  in `src/nexus/mcp/core.py`, CLI `src/nexus/commands/tuple_cmd.py`, watcher
  `src/nexus/tuple_watch.py`.
- Hooks: `conexus/hooks/hooks.json` (twelve kinds), the async sibling scripts
  `subagent-start-tuple-async.sh` and `subagent-stop-tuple-async.sh`,
  `tuple_ledger_project.py` (642 lines), `_endpoint_resolve.py`,
  `_tuple_size_limits.py` (parity-pinned to `TupleLimits.java`).
- Claude Code hooks reference, code.claude.com/docs/en/hooks, fetched
  2026-09-16.
- palinex at 5711143: `src/palinex/__init__.py`, `src/palinex/nexus_bridge.py`,
  `web/index.html` (863 lines, 18 of 18 Basic Catalog components).
- Textual 8.2.8 (MIT); Rich 14.3.3 already installed.

## Research Findings

### Investigation

Rounds 1 to 3 (grammar, substrate observables, rendering envelope) are recorded
in the synthesis note. Rounds 4 to 6 ran with disjoint reading: round 4 read
the engine's tuple code, templates, changelogs and RDR-211's design sections;
round 5 read the exploration, RDR-111's findings, every hook script, the
ledger template, and the current hooks reference, and measured rates from
transcripts and the RDR-184 expectations ledger; round 6 read the client's
read paths, the watcher, the drain hook, the census library, and fetched
Fowler on event sourcing.

#### Dependency Source Verification

| Dependency | Source Searched? | Key Findings |
| --- | --- | --- |
| `rd` and its cursor (`queryOnce`) | Yes | Filter `tenant_id, subspace, consumed_at IS NULL, expires_at > now()`, optional key equality, cursor `(created_at, id) > since` as a row-value comparison (exclusive); order `created_at, id`; `n` clamped to 300; dead rows stay visible until purge; a blocking read registers its waiter before its first query; 1 s park ticks under a 25 s cap; `timeout_s=0` never parks. |
| `out` and `signalAll` | Yes | `created_at` is stamped per transaction at write time (420); `signalAll` after commit (440). No serialisation of commit order against `created_at` order. |
| Claim-log inserts | Yes | Five sites, all inside the tenant-scoped transaction; none signals; `tuple_claim_log` has no keys, dims, body or template columns (DDL tuples-001 121-135); RLS present (143-147). |
| Indexes | Yes | `idx_tuples_subspace_scan (tenant_id, subspace, created_at, id)` (tuples-005) serves `rd`'s predicate and order exactly; the claim log has no per-subspace index. |
| `TemplateSchema` | Yes | No `max_live_rows` field today (RDR-211 proposes it). `keys.kind` may carry a `values` allow-list (ledger precedent). |
| `TupleLimits` | Yes | Request 8192 bytes, field value 256, body 4096 (template-lowerable), subspace 256, nonce, claimant and claim id 128. |
| Hook bridge (`tuple_ledger_project.py`) | Yes | Sibling entries in `hooks.json`, never wrappers; detached subshell measured at about 18 ms; 5 s whole-call bound on its own thread; one schema-evolution retry; every other failure a logged skip; never mints a token. |
| Hooks reference (2026-09-16) | Yes | Fields added since RDR-111's inventory: `prompt_id`, `scratchpad_dir`, `permission_mode`, `effort.level`, `agent_id` and `agent_type` on PreToolUse and UserPromptSubmit, `tool_use_id`. New kinds: PostToolUseFailure, PreModelSwitch, PostModelSwitch, MessageDisplay. `async: true` confirmed (background, timeouts unenforced). SessionEnd shares a 1.5 s budget, the tightest wired. Stop's output contract is documented differently from RDR-111's table; irrelevant to a sibling that emits nothing. |
| Watcher (`tuple_watch.py`) | Yes | Cursor safety lag 10 s (151-170), probe at 923-1015, reset on absence at 1003-1008, mailbox-typed throughout. |
| Census (`expectations.sh`) | Yes | Pairs START with EXPECT by identity, dedups by id (944-970); re-reads the whole file per call. |
| palinex renderer and builder | Yes | Merge and pointer-set semantics at 610-639; template item paths `/@item/<field>`; `validate()` structural; `to_markdown()` lossless. |
| Textual 8.2.8 | Yes (docs) | `Theme` and `get_css_variables()`; keyed DataTable with `update_cell`; `run_test` plus `save_screenshot`. |

### Key Discoveries

- **Verified** (engine source): the claim log cannot be served to `rd` by a
  read adapter without a parallel query implementation, a synthetic mapping,
  a new index, and a new signal at every transition site. A write-through
  projection, one extra `out` inside the same transaction at each of the five
  sites into a take-disabled template, inherits the cursor, the index, RLS and
  the wake-up for free, because `out` already signals. RDR-211's board
  template proves the shape: take-disabled, `id_from: keys+nonce`, 350
  steady-state rows at 50 posts per day and seven days' retention, zero
  claim-log rows of its own.
- **Verified** (engine source): `rd`'s cursor is exclusive on `(created_at,
  id)` and ordered the same way, but nothing serialises commit order against
  timestamp order. A reader that has advanced past a later pair can
  permanently miss a row from a concurrent transaction that stamped an earlier
  time and committed later. The watcher's 10 s safety lag exists for this. It
  is a consumer obligation, not an engine guarantee.
- **Verified** (engine source): only `out` and the reply on ack wake waiters.
  A claim changing state wakes no one today; under the projection every
  transition is an `out` and does.
- **Verified** (measured 2026-09-16, three largest transcripts in
  `~/.claude/projects/-Users-hal-hildebrand-git-nexus/`, span last minus first
  timestamp including idle): tool_use blocks per hour 37.4, 65.9 and 62.9 over
  26.5 h, 25.6 h and 36.8 h. The expectations ledger for the 36.8 h session
  holds 57 START and 56 REPORTED rows, about 1.55 starts per hour. Projecting
  every tool call would add roughly 900 to 1,700 rows per session-day.
- **Verified** (engine source): `id_from: keys` collapses same-key events
  into one row through `writeOut`'s refire path, which touches only
  `expires_at`; an append-only event template needs `keys+nonce`.
- **Documented**: at 2,000 events per session-day and one to three days'
  retention, five concurrent sessions hold about 10,000 live event rows,
  roughly doubling today's 10,736 total tuple rows, and do not accumulate
  beyond retention. Zero claim-log rows, since the templates are take-disabled.
- **Documented**: the ledger projector's three constraints (sibling entry,
  async by construction, skip and never block or mint) retire RDR-111's
  ordering risk outright: a sibling that emits no stdout and no exit code
  cannot enter the allow, deny or block chain.
- **Documented**: ordering is per subspace only. A merged panel sorts by
  timestamp for display and never infers causality across subspaces.
- **Documented** (Fowler, Event Sourcing, 2005, fetched): a projection is a
  fold over a replayable stream; consumers apply idempotently and tolerate
  out-of-order and duplicate delivery. The cockpit's reducers are designed to
  that rule.
- **Verified** (2026-09-16): a queue frame in a2ui Basic Catalog validates
  with palinex's builder and renders in Textual 8.2.8 headless with the
  grammar's tokens as stylesheet variables; the markdown sidecar is lossless.
- **Documented**: the grammar's colour rule resolves the LCARS-versus-ISA
  conflict by keeping LCARS geometry for structure and the industrial rule
  (neutral base, colour only for abnormal) for state; a proposal to colour the
  claimed state was withdrawn on it.

### Critical Assumptions

- [ ] A second `out` inside each of the five claim-log transactions, into a
  take-disabled `claim-events/<subspace>` template, commits atomically with
  the transition, signals after commit, and adds no claim-log rows of its own.
  **Status**: Documented (all five sites run inside the tenant-scoped
  transaction; `out` signals after commit; take-disabled writes generate no
  claim-log rows). **Method**: Spike, an engine test that claims, renews,
  nacks to dead and acks one tuple and reads the projected stream by cursor
  in that order, Phase 1 Step 1.
- [ ] A cursor consumer with a safety lag of L seconds behind the newest row
  it has seen misses no row under concurrent writers to one subspace.
  **Status**: Documented (the watcher's 10 s lag; the engine gives no
  guarantee). **Method**: Spike, ten concurrent writers for one minute against
  a local engine, a reader with L = 10 s, zero misses across ten runs; the
  measured worst commit skew is recorded, Phase 1 Step 3.
- [ ] A hook sibling that writes `events/<session_id>` inside SessionEnd's
  1.5 s shared budget never delays the harness. **Status**: Documented (18 ms
  detach measured for the ledger sibling). **Method**: Spike, the same
  measurement for the new sibling on every wired kind, Phase 1 Step 2.
- [ ] Textual's DataTable keeps row order and cursor position across
  `update_cell` and later `add_row`. **Status**: Documented. **Method**: Spike,
  snapshot test, Phase 1 Step 5.
- [ ] One a2ui payload per panel renders equivalently in palinex's browser
  renderer and the Textual catalog, with a lossless sidecar. **Status**:
  Verified for the builder and sidecar; Assumed for the browser render.
  **Method**: Spike, open in `web/inspector.html`, Phase 1 Step 4.
- [ ] textual-serve runs the cockpit in a browser tab. **Status**: Documented.
  **Method**: Spike, Phase 1 Step 6.
- [ ] A dimension may carry a `values` allow-list the way a key does.
  **Status**: Unverified (the ledger precedent shows it on a key only).
  **Method**: Source Search of `TemplateSchema`, Phase 1 Step 2; if not,
  `tool` and `error_class` stay free text validated client-side before the
  write.

## Proposed Solution

### Approach

Item 1. **Three event streams, one shape.** Three take-disabled, append-only
subspaces that any reader walks with a cursor: `ledger/<session_id>` (exists;
agent start and report), `events/<session_id>` (new; the other ten hook
kinds), `claim-events/<subspace>` (new; every claim transition, projected by
the engine). All three are body-less, `id_from: keys+nonce`, and wake waiters
through `out`. No route, no table, no second read primitive.

Item 2. **Claim transitions emitted by write-through projection.** At each of
the five claim-log sites the engine also performs one `out` into
`claim-events/<subspace>` in the same transaction, carrying the tuple id, the
transition (claim, renew, ack, nack, expire, dead, and release once RDR-211
ships it), the claimant, attempts and lease_until as dimensions. The claim log
table stays as the audit record; the stream is the event.

Item 3. **The hook bridge completed.** One new sibling entry per unprojected
hook kind in `hooks.json`, each the same detached-subshell shape as the ledger
siblings, calling one stdlib projector that writes `events/<session_id>`.
Tool-call events are matcher-scoped to the matchers already wired (`Bash`,
`Agent|Task`, the conexus MCP tools for PreToolUse; `Write|Edit` for
PostToolUse), never every tool. Nothing projected carries prompt text, tool
arguments, file contents, results or secrets: kind, actor, a bounded enum, a
tool name.

| Hook kind | Projected | Never projected |
| --- | --- | --- |
| SessionStart | reason | model, transcript |
| SessionEnd | reason | |
| Stop | kind only | last assistant message |
| StopFailure | error class | error details, message |
| PreCompact, PostCompact | trigger | summary, token counts |
| UserPromptSubmit | kind only | prompt text |
| Notification | notification type | notification data |
| PreToolUse (scoped) | tool name | tool input |
| PostToolUse (scoped) | tool name, exit status | result |
| PermissionRequest | tool name, decision | reason text |
| SubagentStart, SubagentStop | already on the ledger | |

Item 4. **A subspace-generic cursor consumer.** The cockpit opens each panel's
subspace set with a persisted cursor in its own state directory, bootstraps
with a non-parking `rd` from the start, advances the cursor only after a
successful apply and never past now minus the safety lag, dedups by tuple id,
stays live by non-parking polls at one second until RDR-211's multiplexed
`wait` ships and by one parked `wait` after, re-bootstraps after a gap longer
than retention, and merges subspaces by timestamp for display only.

Item 5. **Two panels as projections.** Active claims is a fold over
`claim-events/*`: claim upserts a row, renew changes lease_until in place,
nack or expire or release removes it with one status-line announcement, dead
marks it until purge, ack removes it. Its bootstrap is a census of currently
claimed rows read by `rd`, shown as "since bootstrap" until a transition is
observed. Recent events is the ordered merge of `ledger/*`, `events/*`,
`claim-events/*` and mailbox arrivals, append-only, deduplicated by id,
evicted past a window, never resorted; "agent running since" is the unmatched
start, exactly the census's pairing rule. Derived fields (held-for,
lease-left) are computed at render time.

Item 6. **One a2ui payload per panel, two renderers, one patch per event.**
Producers in palinex's nexus bridge build each panel as an a2ui v0.9 surface.
One reduced event becomes one `updateDataModel` pointer patch for the browser
renderer and one `update_cell` or `add_row` for the Textual catalog; the diff
lives in the reducer, never in a renderer. The Textual catalog maps the Basic
Catalog as in the table below. The grammar (neutral base, one frame hue,
colour only for dead, urgent, stale and contested, claimed-by-me as the
highlight tier, rows keep their slot, three text tiers, sentence case) is the
stylesheet and is pinned by headless snapshot tests.

| a2ui component | Textual widget |
| --- | --- |
| Column / Row | Vertical / Horizontal |
| Text (variants) | Static with a class per variant |
| List with template | DataTable, one keyed row per item |
| Card | Container with border |
| Button (event) | Button; the app posts the event name and context |
| Modal | ModalScreen |
| Tabs | TabbedContent |
| Divider | Rule |
| TextField, CheckBox, ChoicePicker, Slider, DateTimeInput | Input, Checkbox, Select or RadioSet, ProgressBar (display), Input |
| Image, Video, AudioPlayer | sidecar text |

Item 7. **Two delivery tiers.** `palinex cockpit` in a tmux pane;
`palinex cockpit --serve` in a browser tab through textual-serve, which
answers RDR-064; chat stays on `render_surface` plus the markdown sidecar.
`nx tuple watch` remains the notification tier until RDR-211 retires it.

Excluded, deliberately: bindings and any reaction loop, the connection
manifest, the auto-layout engine and its demotion cascade, `surface_cell` and
recursive surfaces, an LCARS look, a live MCP host bridge, and projection of
every tool call.

### Technical Design

Streams. Three templates, sketched; the implementation derives the exact
dimension set from the hooks reference and `TupleRepository` and pins it by
test.

```yaml
# events.yaml (sketch)
name: events/<session_id>
keys:
  agent_id:            # "" for session-level kinds
  kind:
    values: [session_start, session_end, stop, stop_failure, pre_compact,
             post_compact, user_prompt_submit, notification,
             tool_call_intent, tool_call_completed, permission_request]
dimensions:
  actor: {type: string}
  tool: {type: string}
  error_class: {type: string}
  exit_status: {type: string}
  reason: {type: string}
id_from: keys+nonce
take: {enabled: false}
retention_seconds: 259200     # 3 days, Open Question 2
max_body_bytes: 0

# claim-events.yaml (sketch)
name: claim-events/<subspace>
keys:
  tuple_id:
  transition:
    values: [claim, renew, ack, nack, expire, dead, release]
dimensions:
  claimant: {type: string}
  attempts: {type: string}
  lease_until: {type: string}
id_from: keys+nonce
take: {enabled: false}
retention_seconds: 259200
max_body_bytes: 0
```

Engine. `writeOut` is called at the five claim-log sites for the companion
template, inside the existing transaction; `signalAll` fires after commit as
for any `out`. No changeset: the rows live in `nexus.tuples` under the existing
RLS policy and `idx_tuples_subspace_scan`. Size per event stays under 200
bytes against the 8192-byte request cap.

Bridge. One stdlib script, `tuple_events_project.py`, beside
`tuple_ledger_project.py`, sharing `_endpoint_resolve.py` and
`_tuple_size_limits.py`; one detached sibling shell entry per hook kind;
never a wrapper of a decision-bearing hook; 5 s whole-call bound on its own
thread; one schema-evolution retry; skip and log otherwise; never mint.

Consumer, illustrative signatures:

```text
// Illustrative — verify signatures during implementation
open(panel) -> Cursor            // persisted file per panel; None means bootstrap
catch_up(cursor, subspaces) -> events     // rd since=cursor, n<=300, timeout_s=0, repeat until empty
advance(cursor, newest_seen, lag_s=10)    // never past now - lag_s
reduce_claims(state: dict[tuple_id, ClaimRow], e: ClaimEvent) -> state
reduce_events(state: deque[EventRow], e: LifecycleEvent) -> state
diff(before, after) -> patches            // one per changed row
```

Rendering. Each patch becomes `{"updateDataModel": {"path": "/items/<key>/<field>", "value": v}}`
for the browser renderer and `table.update_cell(key, field, v)` or
`table.add_row(..., key=key)` for the Textual catalog. Removal announces once in
the status line and then deletes the row.

Identity. The operator's claimant identity is what the nexus MCP server and
CLI already resolve; the highlight tier and the edge's operations (renew,
hand back, ack on the operator's own claims) key on it. Edge operations follow
the drain hook's rule: render what an action confirmed, never what a read
merely saw.

Tokens. `frame`, `frame-deep`, `mine`, `state-dead`, `state-urgent`,
`state-stale` on top of ground, ink, muted and border; light and dark each
defined once.

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| `claim-events` projection | `TupleRepository.java` claim-log sites | Extend: one `writeOut` beside each `insertClaimLog`, same transaction. |
| `events` template and bridge | `ledger.yaml`, `tuple_ledger_project.py`, the async siblings | Extend: same shape, `keys+nonce`, one new projector script and one sibling entry per kind. |
| Cursor consumer | `tuple_watch.py` | Reuse the discipline (safety lag, seen set, per-panel state file, reset on absence), not the code: it is mailbox-typed and RDR-211 deletes it. |
| Pairing logic | `expectations.sh` 944-970 | Reuse the rule (unmatched start means running) in Python; not the shell. |
| Edge operations | `mailbox_drain.py` 54-61 | Reuse the rule: act, then render the confirmed result. |
| Producers, catalog, cockpit process | `palinex/src/palinex/nexus_bridge.py`, `web/index.html` | Extend palinex; nexus ships nothing rendering-shaped (RDR-127). |
| Grammar tests | `pytest-textual-snapshot` | New: one snapshot per panel per theme. |

### Decision Rationale

The cockpit reads the space the way the space is built: as a log. Emitting
claim transitions as tuples rather than reading the claim log costs five
call-site edits and one template, the same size as a route, and keeps every
panel on `rd` and `wait`, which is the whole point of the redirect. Completing
the hook bridge is the ledger's pattern ten more times; the matcher scoping
and the byte discipline are what keep it a milestone stream rather than a
byte stream. The consumer is designed to Fowler's rules because the engine
gives per-subspace order and at-least-once delivery and nothing more. The
panels are folds so they are rebuildable and testable without a running
engine. Excluding bindings and auto-layout is the postmortem's lesson applied.

## Alternatives Considered

### Alternative 1: A claim-log read route (this RDR's first draft)

**Description**: `GET /v1/tuples/claim-log`, a `tuple_log` tool and verb.

**Pros**:

- Reads history without touching the transition sites.

**Cons**:

- A second read primitive: no `out`, no signal, so it cannot compose with
  `wait` and the cockpit would poll it separately; needs a new index.

**Reason for rejection**: it is the storage-layer mistake in a smaller form.

### Alternative 2: A read adapter serving `rd` over the claim log

**Description**: a reserved subspace name mapped onto `tuple_claim_log` rows.

**Pros**:

- One primitive from the consumer's view.

**Cons**:

- A parallel query implementation (the log has no keys, dims, body or
  template columns), a new index, and a new signal at all five sites anyway.

**Reason for rejection**: as invasive as the projection with none of its reuse.

### Alternative 3: Revive RDR-111 whole

**Description**: bindings, connection manifest, auto-layout and the panels.

**Reason for rejection**: the shape that stranded 67 beads; the panels are
independently shippable and are what bindings would display.

### Alternative 4: Project every tool call

**Description**: one event per PreToolUse and PostToolUse for every tool.

**Reason for rejection**: 37 to 66 events per hour per session against 1.5
milestones; the exploration's own byte-stream caution, now with numbers.

### Alternative 5: Poll rows on an interval (this RDR's first draft)

**Reason for rejection**: table polling reads the fold, not the log; it cannot
show a transition, only a state, and it is RDR-111's SQL error with HTTP.

### Briefly Rejected

- **notcurses**: no styling layer, abandoned with the cockpit.
- **Ink** (under Claude Code's own interface): TypeScript and Node.
- **The RDR-064 HTMX console as host**: a second renderer with no shared IR.

## Trade-offs

### Consequences

- Positive: every claim transition and every wired lifecycle milestone is an
  event any consumer can walk, not only the cockpit: doctor rows, audits and
  RDR-211's subscriptions read the same streams.
- Positive: one payload per panel serves pane, window and chat; one patch per
  event keeps every view stable.
- Negative: live tuple rows roughly double at five concurrent sessions and
  three days' retention; the footprint is bounded by retention and does not
  accumulate.
- Negative: two engine touches (five call sites, two templates) wait on an
  engine cut; the ledger and mailbox halves of the events panel work on any
  engine.
- Negative: palinex gains an optional dependency and a second renderer; the
  mapping table is the contract that keeps the two honest.

### Risks and Mitigations

- **Risk**: the bridge leaks content. **Mitigation**: the projection table is
  a test fixture; a projector test asserts every emitted field is in the
  allow-list and under 256 bytes.
- **Risk**: a consumer misses rows under concurrent writers. **Mitigation**:
  the safety-lag spike measures worst skew; the lag is a config with the
  measured floor; the census bootstrap on reopen catches what a lag missed.
- **Risk**: the panels become the place features go and the scope lesson
  repeats. **Mitigation**: the exclusion list is gate-checked; additions are
  new RDRs.
- **Risk**: hook budgets. **Mitigation**: siblings are detached by
  construction; the detach measurement is repeated per kind.
- **Risk**: browser and terminal renderings drift. **Mitigation**: one
  payload, two snapshots per panel, the mapping table reviewed.

### Failure Modes

- Engine unreachable: the pane keeps its last state with a stale marker on
  every row and the census line names the error; nothing is cleared.
- Older engine without the templates: the bridge's writes 400 and are skipped
  and logged; the cockpit shows ledger and mailbox streams and a one-line
  capability note; no traceback.
- Cursor file corrupt or older than retention: re-bootstrap; the panel says
  history before the bootstrap is unavailable.
- Identity unresolved: read-only panels, said in the status line.
- Diagnosis: `palinex cockpit --once --json` prints one fold and its sidecar;
  the projector's skip log names every dropped event and why.

## Implementation Plan

### Prerequisites

- [ ] Critical Assumptions verified (five spikes, one source search).
- [ ] Sam decides the Open Questions.
- [ ] palinex accepts its half under its own RDR (RDR-127).

### Minimum Viable Validation

One real engine, two sessions, one tmux pane, one browser tab:

1. Session A opens `palinex cockpit`. Session B sends A a mailbox message and
   A takes it. Within one second the active-claims panel in A's pane shows the
   row in the highlight tier, and the claim event appears in recent events; in
   B's pane the same row is neutral with A as claimant.
2. A renews from the panel's edge: lease resets in place, the row does not
   move, the renew event appears. A hands back: the row leaves with one
   status-line announcement; the nack event appears.
3. A dispatches a background agent and runs a Bash tool: the events panel
   appends the ledger start, the tool intent, the tool completion and the
   report, in order, without reordering earlier rows.
4. B nacks a queue task to dead: the events panel shows three nacks and the
   dead transition; the claims panel shows the dead marker until purge.
5. The cockpit is closed for two minutes during activity and reopened: the
   cursor catches up with no duplicate rows and no missing transition.
6. `--serve` shows the same panels in a browser tab; the headless snapshot and
   the tab differ only in size; `render_surface` of the claims payload renders
   in palinex's browser renderer with a lossless sidecar.

### Phase 1: Code Implementation

#### Step 1: `claim-events` projection (engine)

Template file, one line in the registry, one `writeOut` at each of the five
sites. Test: the transition-order spike; the existing tuple suites stay green;
a take-disabled write adds no claim-log rows.

#### Step 2: `events` template and the bridge (engine and hooks)

Template file and registry line; the `values`-on-dimension source search;
`tuple_events_project.py` and one sibling entry per kind; the allow-list
fixture test; the detach measurement per kind.

#### Step 3: The cursor consumer (palinex)

`open`, `catch_up`, `advance`, persisted cursor per panel, safety lag, seen
set, re-bootstrap; the concurrent-writer spike with the measured skew recorded
in Research Findings.

#### Step 4: The two reducers and their payloads (palinex)

Pure functions with fixture streams; the fold's determinism and idempotency
pinned by replaying each fixture twice; the a2ui payloads validated and
opened in the inspector; sidecars pinned.

#### Step 5: The Textual catalog and the grammar theme (palinex)

Widgets per the table; tokens; the DataTable stability spike; snapshots per
panel per theme.

#### Step 6: The cockpit process (palinex)

`palinex cockpit` with poll now and `wait` later, `--serve`, `--once --json`.

#### Step 7: Documentation

`docs/tuple-space.md` gains "The event layer" and "Seeing it"; palinex's
README gains the cockpit; `conexus/hooks/README` (or its equivalent) lists the
projected kinds and the never-projected fields.

### Phase 2: Operational Activation

#### Activation Step 1: Engine cut

Both templates and the five-site projection ride one engine cut, paired per
the release choreography, with RDR-211's engine work if Open Question 3 says
so. Additive: an older client is unaffected by streams it never reads.

### Day 2 Operations

| Resource | List | Info | Delete | Verify | Backup |
| --- | --- | --- | --- | --- | --- |
| `events/*`, `claim-events/*` rows | `tuple_list --prefix` | `tuple_stats` | retention purge (3 days) | `tuple_rd` by cursor | N/A |
| Cursor files | cockpit state dir | `--once --json` | delete to re-bootstrap | the reopen MVV step | N/A |
| Projector skip logs | per-session log file | read | rotate with the ledger's | N/A | N/A |

### New Dependencies

- palinex: `textual`, `textual-serve`, `pytest-textual-snapshot` (all MIT), as
  the `[tui]` extra.
- nexus and engine: none.

## Test Plan

- **Scenario**: claim, renew, nack to dead, ack on one tuple — **Verify**: the
  `claim-events` stream read by cursor holds exactly those transitions in
  order; the claim log holds the same count; a waiter parked on the stream
  wakes on each.
- **Scenario**: ten concurrent writers, one cursor reader with a 10 s lag —
  **Verify**: zero missed rows across ten runs; the worst skew recorded.
- **Scenario**: the projector on every wired hook kind with a fixture stdin —
  **Verify**: every emitted field is in the allow-list and under 256 bytes;
  prompt text, tool input and results never appear; detach under 50 ms.
- **Scenario**: a fixture stream replayed twice through each reducer —
  **Verify**: identical state; rows never resorted; removals announced once.
- **Scenario**: a fixture panel in light and dark — **Verify**: colour only on
  urgent, dead, stale and contested rows; the mine row in the highlight tier.
- **Scenario**: cursor older than retention — **Verify**: re-bootstrap, the
  "history unavailable" note, no traceback.
- **Scenario**: tenant isolation — **Verify**: a second tenant's events never
  appear in either stream.

## Validation

### Testing Strategy

1. **Scenario**: the Minimum Viable Validation steps 1 to 6 on a local engine
   with the gate jar. **Expected**: every observation recorded with timestamps
   in Revision History.
2. **Scenario**: the palinex snapshot suite. **Expected**: green in both
   themes for both panels.

### Performance Expectations

Measured, not estimated: the concurrent-writer spike records worst commit
skew; the projector spike records detach time per kind; the Textual spike
records refresh time at 1,000 rows. All land in Research Findings before the
gate. The row-footprint arithmetic above is re-derived from a live census at
the MVV.

## Open Questions (for Sam)

1. Tool-call events: the wired matchers only (recommended), or none until a
   consumer asks?
2. Retention for `events` and `claim-events`: three days as sketched, or one?
3. Do both templates and the five-site projection ride RDR-211's engine cut,
   or the next?
4. Is roughly doubling live tuple rows at five sessions acceptable for the
   cloud tenant, or should the cap wait for RDR-211's `max_live_rows`?
5. The cockpit process lives in palinex (RDR-127). May `nx tuple cockpit`
   shell out to it when installed?

## Finalization Gate

> Complete each item with a written response before marking this RDR as
> **Accepted**.

### Contradiction Check

Two recorded and resolved at draft time. Round 6 stated that every write
signals waiting readers; round 4 showed only `out` and the reply on ack do.
Under the projection every claim transition is an `out`, so the stream wakes
readers and the panels are live; the statement is true of the design, not of
the engine as built. The first draft's claim-log route and one-second row
polling are withdrawn in favour of the streams; Alternatives 1 and 5 record
why.

### Assumption Verification

Seven assumptions: five spikes and one source search scheduled in Phase 1,
one docs-only item. None is verified at draft time beyond the builder,
prototype and measurement facts in Key Discoveries.

#### API Verification

| API Call | Library | Verification |
| --- | --- | --- |
| `writeOut`, `signalAll`, the five claim-log sites | engine | Source Search |
| `rd` cursor (`queryOnce`) | engine | Source Search |
| `http_tuple_store.rd(since_created_at, since_id, n, timeout)` | nexus client | Source Search |
| hook stdin fields per kind | Claude Code hooks reference | Docs, fetched 2026-09-16 |
| `Surface.list(template=, template_path=)`, `validate()`, `to_markdown()` | palinex | Verified 2026-09-16 |
| `DataTable.add_row(key=)`, `update_cell` | Textual 8.2.8 | Docs; Spike Step 5 |
| `App.run_test`, `save_screenshot` | Textual 8.2.8 | Verified 2026-09-16 |
| textual-serve | textual-serve | Docs; Spike Step 6 |

### Scope Verification

The Minimum Viable Validation is in scope and runs before the RDR closes.

### Cross-Cutting Concerns

- **Versioning**: two new templates and a projection, additive; the wire
  ledger carries the entry; the cockpit reads capability from the registry.
- **Build tool compatibility**: N/A.
- **Licensing**: Textual family MIT; a2ui Apache-2.0.
- **Deployment model**: templates ship in the engine release; local and cloud
  both load them; the cockpit runs on the operator's box.
- **IDE compatibility**: N/A.
- **Incremental adoption**: the streams exist whether or not anyone opens a
  cockpit; the cockpit is opt-in.
- **Secret/credential lifecycle**: the bridge presents existing leases only
  and never mints; nothing new is stored.
- **Memory management**: reducers hold one state per panel; the events deque
  is windowed; cursor files are a few hundred bytes.

### Proportionality

Two templates, five call-site edits, one projector script with a dozen
sibling entries, one consumer, two reducers, one terminal catalog, one theme,
two entry flags, seven snapshot and fixture tests. The document is sized to
that and to the exclusions it defends.

## References

- docs/exploration/agentic-cockpit.md ("The two moves", "The substrate is
  log-structured, per tier", "What semantic event means concretely",
  "Surfaces")
- docs/rdr/rdr-111-orb-agentic-cockpit-substrate.md (RF-1 to RF-5 at 264-372,
  the seven event subspaces at 389-418, the bridge at 419-450, the three
  surfaces at 528-595, scrap reason)
- docs/rdr/rdr-118-surfaces-as-tuples.md; docs/rdr/rdr-119-cockpit-ui-fabric.md
- docs/rdr/rdr-127-substrate-decoupled-surface-rendering.md
- docs/rdr/rdr-064-nx-console-embedded-web-ui.md (Alternatives, item 7)
- docs/rdr/rdr-205-linda-tuple-space-over-postgres.md;
  docs/rdr/rdr-206-tuple-claim-renew-and-reply-in-ack.md;
  docs/rdr/rdr-211-board-queue-and-lock-tuple-templates.md (Technical Design
  357-542, Scale and Limits)
- docs/postmortem/2026-05-16-rdr110-113-remediation-chain.md
- service/src/main/java/dev/nexus/service/db/TupleRepository.java (402-430,
  440, 525-557, 583-656, 824-1192), TupleLimits.java (32-51),
  tuples/TupleWaitRegistry.java (23-45); changelogs tuples-001 (121-147),
  tuples-005; templates ledger.yaml, directory.yaml, mailbox.yaml
- conexus/hooks/hooks.json; conexus/hooks/scripts/tuple_ledger_project.py,
  subagent-start-tuple-async.sh, subagent-stop-tuple-async.sh,
  _endpoint_resolve.py, _tuple_size_limits.py, expectations.sh (944-970),
  mailbox_drain.py (54-61)
- src/nexus/db/t2/http_tuple_store.py (517-543); src/nexus/tuple_watch.py
  (151-170, 923-1015)
- Claude Code hooks reference, code.claude.com/docs/en/hooks (2026-09-16)
- T3 tuple-space: `research-rdr212-engine-claim-log-and-event-subspaces-2026-09-16`,
  `research-rdr212-lifecycle-events-as-tuples-2026-09-16`,
  `research-rdr212-event-consumer-contract-and-projections-2026-09-16`,
  `research-tuple-space-observables-for-surfaces-2026-09-16`
- T3 interface-design: `research-tuple-surface-design-synthesis-2026-09-16`,
  `research-coordination-display-grammar-2026-09-16` (1/2, 2/2),
  `research-palinex-a2ui-rendering-envelope-2026-09-16`,
  `example-queue-frame-a2ui-payload-2026-09-16`,
  `terminal-surfaces-narrowed-to-textual-2026-09-16`
- T2 nexus: `orb-cockpit-reframe-2026-09-16`
- palinex: docs/rdr/rdr-001-architecture.md, src/palinex/__init__.py,
  src/palinex/nexus_bridge.py, web/index.html
- Fowler, "Event Sourcing", martinfowler.com/eaaDev/EventSourcing.html (2005);
  Bercovitz and Carriero, TupleScope, Yale RR-782 (1990); ISA-101 (2015);
  EEMUA 191 (2013); Nielsen (1994); Mackay, ACM TOCHI (1999); Pousman and
  Stasko, AVI (2006)
- Artifact: https://claude.ai/artifact/DBfEkXXTwGJzY2iXV7qesn

## Revision History

- 2026-09-16: created (draft) after the surface research synthesis and the
  ORB reframe; panels polled rows and read a claim-log route.
- 2026-09-16: rewritten at the event layer after Sam's correction. Three
  event streams replace the route and the poll; the hook bridge is completed
  for ten kinds with measured rates; the consumer contract and the two folds
  are specified; the claim-log route and row polling move to Alternatives.
  Research rounds 4 to 6 (T3 tuple-space, listed in References).
