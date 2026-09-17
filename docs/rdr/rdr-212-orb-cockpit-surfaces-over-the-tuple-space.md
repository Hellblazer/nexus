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
related_external: [palinex-rdr-001, a2ui-v0.9-spec, textual-8.2]
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
post-substrate RDR." This is that RDR.

Terms used throughout:

- **Tuple space**: the shared store of small records that sessions, agents,
  hooks and scripts coordinate through (RDR-205), held in the engine, the Java
  service that owns the database. A **tuple** has keys, dimensions and a short
  body; a **subspace** is a named part of the space such as `mailbox/<address>`;
  a **template** is the engine's registered definition of one kind of subspace.
- **Claim**: what `in` gives a caller when it takes a tuple, for a limited
  **lease**. The caller ends it with `ack` (consumed) or `nack` (returned). A
  tuple that fails too often becomes a **dead letter**.
- **ORB**: the Observable Relay Bus of RDR-111, the idea that the tuple space is
  the switchboard every actor coordinates through and that hook events are
  projected into it as tuples, so a **cockpit** of panels can show what is
  running, what it holds, and what just happened.
- **a2ui**: a declarative UI specification (Google, Apache-2.0) in which an
  agent emits a JSON description of components and data and a host renders it.
  Version 0.9 has 18 Basic Catalog components.
- **palinex**: the downstream project (RDR-127) that owns a2ui surface
  emission: typed Python builders, a single-file browser renderer, three
  delivery shapes, and a mandatory markdown sidecar.
- **Textual**: a Python terminal-application framework built on Rich, with a
  CSS dialect for styling and keyed table widgets.
- **Panel**: one cockpit surface answering one question at a glance.

## Problem Statement

RDR-111 named three gaps in May 2026. Two of them have since closed by a
different road: the tuple space exists in the engine on Postgres (RDR-205,
RDR-206), the directory and mailbox templates are the switchboard, and the
ledger template plus the SubagentStart and SubagentStop hooks project agent
starts and reports into `ledger/<session>` through
`conexus/hooks/scripts/tuple_ledger_project.py`. The third gap is untouched:
nothing draws any of it. An operator running several sessions learns what is
claimed, what is stalled, and what just happened by reading `nx tuple watch`
ping lines and raw tool output. The cockpit still has no instruments.

### Enumerated gaps to close

#### Gap 1: No situational-awareness surface

There is no panel that answers "what is held right now, by whom, for how much
longer" or "what happened in the last few minutes" across the tuple space.
`tuple_stats` gives one subspace's counts at one instant; `tuple_rd` gives raw
rows; the watcher pings on mailbox arrivals only. RDR-111's three minimum
panels (active claims, recent events, active bindings) were designed and the
first two are feedable from what is built today; none exists.

#### Gap 2: No read paths for the two panels

Verified absent against the code on 2026-09-16 (T3 tuple-space,
`research-tuple-space-observables-for-surfaces-2026-09-16`): the engine keeps
a `tuple_claim_log` table of every claim, ack, nack, renew, expire and dead
transition, and no MCP tool or CLI verb reads it, so "claimed longest", "ever
contested" and any recent-events view over claims are unanswerable from a
client. `tuple_stats` is a point census with no rate. The watcher is
mailbox-typed (`src/nexus/tuple_watch.py` lines 5, 415-424, 498).

#### Gap 3: No terminal host for a2ui surfaces, and the browser host is a snapshot

palinex's RDR-001 named terminal hosts as future work. Its browser path through
MCP delivers a surface once and never updates it: `wrap_as_mcp_ui_resource`
(`src/palinex/__init__.py` lines 732-772) posts the initial payload after the
renderer's ready handshake and has no later post, because MCP tool calls are
request and response. The renderer itself supports in-place update
(`updateComponents` merges, `updateDataModel` sets by JSON pointer, `web/index.html`
lines 610-639). An ambient cockpit needs a host that keeps updating, and the
operator's terminal is where sessions already live.

#### Gap 4: No design grammar of record for coordination surfaces

RDR-111 cited SCADA and mission control as prior art and never said what a
panel should look like. A sourced grammar now exists as research (T3
interface-design, `research-coordination-display-grammar-2026-09-16`, 34 rules;
`research-tuple-surface-design-synthesis-2026-09-16`, the reconciliation). It is
not adopted by any design record, so nothing binds a panel to it.

## Relationship to Prior RDRs

| Prior RDR | Relationship | What it means for this one |
| --- | --- | --- |
| RDR-111 (ORB, abandoned) | Origin | Its two architectural moves are now real by other means; its Gap 3 is this RDR's Gap 1. Its scrap reason directs this document. Its bindings primitive, connection manifest and auto-layout engine are out of scope here, by that same lesson. |
| RDR-118 (surfaces as tuples, superseded) | Origin | Adopted a2ui as the descriptor. The `surface_cell` subspace and recursive surfaces are not revived. |
| RDR-119 (cockpit UI fabric, abandoned) | Origin | Its `nexus.notcurses.v1` catalog table is replaced by an a2ui-to-Textual table. Its Bakke demotion cascade is reduced to two fixed tiers here. |
| RDR-127 (palinex is downstream, closed) | Precedent | Nexus ships no rendering code. Producers, the terminal catalog and the cockpit entry point live in palinex; nexus ships the read paths. |
| RDR-064 (nx console, closed) | Precedent | Rejected a terminal UI because the operator wants a separate window for observability. This RDR keeps that option through textual-serve and adds the pane. |
| RDR-205, RDR-206 (tuple space, closed) | Origin | The substrate this RDR reads. The state machine the panels render is theirs. |
| RDR-211 (board, queue, lock; draft) | Adjacent draft | Scope boundary below. |

Scope boundary with RDR-211: RDR-211 owns templates, the `release` and `wait`
operations, the lock flag, per-template caps, doctor rows, and the
subspace-scoped index. This RDR owns the two panels, the claim-log read path,
the terminal catalog, and the grammar adoption. This RDR polls; when RDR-211's
multiplexed `wait` ships, the panels subscribe instead (Approach item 5).

## Context

### Background

RDR-111 was accepted on 2026-05-13 and scrapped six days later, with eight
sibling RDRs, because the storage-substrate split was bundled with every new
abstraction at once: nine RDRs, 67 stranded beads
(`docs/postmortem/2026-05-16-rdr110-113-remediation-chain.md`). The substrate
work went on alone under RDR-120 and, later, RDR-205. The cockpit idea was
never found wrong; it was found entangled.

On 2026-09-16 three parallel research rounds established what a coordination
panel should look like, what the substrate exposes, and what the renderer
allows. The findings that shape this RDR:

- The grammar has three axes owned by three traditions. Structure from the
  LCARS analyses (one frame per scope, the frame's edge broken into the
  operations, rows keep their slot, three text tiers). State from industrial
  human-machine-interface practice (ISA-101, EEMUA 191: neutral base, colour
  only for abnormal). Behaviour from Nielsen 1994, Mackay 1999, Pousman and
  Stasko 2006 and TupleScope 1990 (every action acknowledged, nothing silently
  removed, density by watcher, incremental update).
- In the substrate, a row carries two independent axes, `claim_state` and
  `consumed_at`, plus `expires_at`; `ack` does not reset `claim_state`. The
  abnormal states a panel colours are dead, lease near lapse, stale heartbeat
  and contested lock. Claimed-by-me is the one highlight tier. Consumed rows are
  absent.
- Every field is bounded to one line: body 4096 bytes, keys and dimensions 256,
  claimant and claim id 128 (`TupleLimits.java`).
- A Textual prototype of a queue frame rendered headless on 2026-09-16
  (textual 8.2.8) with the grammar's tokens as stylesheet variables.

### Technical Environment

- Engine: `service/`, Java, Postgres 17. Tuple code in
  `service/src/main/java/dev/nexus/service/db/TupleRepository.java`,
  `TupleLimits.java`, `tuples/TupleWaitRegistry.java`; templates under
  `service/src/main/resources/tuples/templates/`; claim log table
  `tuple_claim_log` (`tuples-001-baseline.xml`).
- Client: `src/nexus/db/t2/http_tuple_store.py`, MCP tools in
  `src/nexus/mcp/core.py` (`tuple_out`, `tuple_rd`, `tuple_in`, `tuple_ack`,
  `tuple_nack`, `tuple_renew`, `tuple_list`, `tuple_stats`,
  `tuple_registry`), CLI `src/nexus/commands/tuple_cmd.py`, watcher
  `src/nexus/tuple_watch.py`.
- Hooks: `conexus/hooks/scripts/subagent-start-tuple-async.sh`,
  `subagent-stop-tuple-async.sh`, `tuple_ledger_project.py`, `mailbox_drain.py`.
- palinex: `/Users/hal.hildebrand/git/palinex` at 5711143 (2026-05-23),
  `src/palinex/__init__.py` (Surface builder, validate, to_markdown,
  wrap_as_mcp_ui_resource), `src/palinex/nexus_bridge.py`, `web/index.html`
  (863 lines, all 18 Basic Catalog components), `web/host-bridge.html`.
- Textual 8.2.8 (pure Python, Apache-2.0-compatible MIT licence), Rich 14.3.3
  already installed transitively in this checkout.

## Research Findings

### Investigation

Three research rounds ran 2026-09-16 with disjoint reading, then one
synthesis. Round 1 read the three LCARS documents indexed that day and fetched
Nielsen, Pousman and Stasko, Mankoff and Dey, Mackay, Anderson, ISA-101,
EEMUA 191, Tufte and Bercovitz and Carriero. Round 2 read RDR-205, 206, 211,
skimmed 118 and 119, read the three template YAML files, `TupleRepository.java`,
`TupleLimits.java`, `TupleWaitRegistry.java`, and called `tuple_registry`,
`tuple_list`, `tuple_stats` and `tuple_rd` against the running engine. Round 3
read palinex's renderer, wrapper, RDRs and skills, and fetched a2ui.org's
specification and theming guide. The synthesis added a Textual prototype.

#### Dependency Source Verification

| Dependency | Source Searched? | Key Findings |
| --- | --- | --- |
| Engine claim state machine (`TupleRepository.java`) | Yes | Two axes on one row; `ack` sets `consumed_at` and nulls the body without resetting `claim_state`; `nack` and lease lapse count an attempt; a separate sweep purges by `expires_at`; `expired_unpurged` in the census is the lag. |
| Engine claim log (`tuple_claim_log`) | Yes | Written on claim, ack, nack, renew, expire and dead (two rows on dead-lettering). No read path in `core.py` or `tuple_cmd.py`. |
| Engine wait registry (`TupleWaitRegistry.java`) | Yes | 16 parked calls engine-wide, in memory, no report. A `tuple_rd` with `timeout_s=0` never parks. |
| Client tuple tools (`core.py`, `tuple_cmd.py`) | Yes | Subspace-agnostic reads; `tuple_list` pages by cursor; `tuple_stats` returns total, available, claimed, dead, consumed, expired_unpurged, oldest and newest timestamps. |
| palinex renderer (`web/index.html`) | Yes | 18 of 18 components (`renderers` from line 181); `applyMessage` merges `updateComponents` (610) and sets `updateDataModel` by pointer (631-632); `createSurface.theme` never read (615-621); eight CSS tokens, `prefers-color-scheme` only, no `data-theme`. |
| palinex MCP wrapper (`__init__.py` 732-772) | Yes | One delivery after `a2ui.ready`; iframe sandbox `allow-scripts allow-same-origin`, so the `openUrl` action's `window.open` is blocked inside it. |
| palinex builder (`Surface`) | Yes | Template item paths must be `/@item/<field>`; `validate()` is structural; `to_markdown()` is lossless for List templates when paths are correct. |
| Textual 8.2.8 | Yes (docs, Context7) | `Theme` and `get_css_variables()` supply `$variables`; DataTable rows are keyed and `update_cell` changes one cell in place; `run_test` plus `save_screenshot` render headless SVG. |
| a2ui v0.9 (a2ui.org, 2026-09-16) | Yes | "Agents describe what, renderers decide how"; theming is renderer-defined; `theme` on createSurface is unwired in the reference catalogs (issue #1118). |

### Key Discoveries

- **Documented**: RDR-111's Move 2 exists in production. The ledger template is
  take-disabled, one writer, many readers; the SubagentStart and SubagentStop
  hooks write `start` and `report` rows keyed by agent id
  (`tuple_ledger_project.py`). A recent-events panel over the ledger needs no
  new engine work.
- **Documented**: an active-claims panel across every take-enabled subspace can
  be built today from `tuple_list` (subspaces by prefix with census) and
  `tuple_rd` (rows with `claim_state`, `claimant`, `lease_until`), polled with
  `timeout_s=0` so it never occupies a park slot.
- **Documented**: "claimed longest without renew", "ever contested", and the
  claim half of recent events need the claim log, which has no read path
  (Gap 2).
- **Verified** (2026-09-16, T3 `example-queue-frame-a2ui-payload-2026-09-16`): a
  queue frame in a2ui Basic Catalog validates with palinex's builder (33
  components, 3.3 KB) and its markdown sidecar is lossless.
- **Verified** (2026-09-16, T3 `terminal-surfaces-narrowed-to-textual-2026-09-16`):
  the same frame renders in Textual 8.2.8 headless at 96 by 22 with the
  grammar's tokens as `$variables`, a three-column rail, a keyed DataTable and
  an edge of four Buttons. Two corrections were needed and are recorded: the
  DataTable's default row colour is muted, and the row cursor must be hidden or
  moved so it is not read as the highlight tier.
- **Documented**: the browser path through MCP is a static snapshot by design
  (palinex-overview skill, "the static-snapshot guarantee"), while the renderer
  can update in place. A live browser cockpit is host-bridge work in palinex,
  not nexus work.
- **Documented**: the grammar's colour rule resolves a real conflict. The LCARS
  sources colour-block every frame; ISA-101 and EEMUA 191, from process-safety
  incident review, reserve colour for deviation. The synthesis keeps LCARS
  geometry for structure and takes the industrial rule for state. A round-3
  proposal to colour the claimed state was withdrawn on that rule.
- **Documented**: RDR-064 rejected a Rich or Textual console because the
  operator "needs a separate window for observability, not another pane sharing
  terminal real estate with agent work." textual-serve runs the same app in a
  browser tab.

### Critical Assumptions

- [ ] A claim-log read endpoint scoped by tenant and subspace can be added to
  the engine as a read-only route over `tuple_claim_log` under the existing
  row-level security, with no changeset. **Status**: Unverified. **Method**:
  Source Search (`TupleRepository`, the tuples security policy, the
  `tuple_claim_log` DDL) before Phase 1.
- [ ] Polling `tuple_list` and `tuple_rd` at one-second intervals across the
  live subspaces of one tenant costs the engine nothing a panel would notice
  and never parks. **Status**: Documented (`timeout_s=0` never blocks;
  `TupleRepository.java` 584-637). **Method**: Spike, measured request count
  and latency for one hour against a local engine, Phase 1 Step 3.
- [ ] Textual's DataTable keeps row order and the cursor position across
  `update_cell` and across `add_row` of later rows, so a one-second refresh
  never reshuffles the operator's view. **Status**: Documented (Textual
  DataTable guide). **Method**: Spike, a snapshot test that updates a middle
  row and adds a row, Phase 1 Step 2.
- [ ] One a2ui payload per panel renders equivalently in palinex's browser
  renderer and in the Textual catalog, and its markdown sidecar is lossless
  for both. **Status**: Verified for the builder and sidecar, Assumed for the
  browser render (the payload was validated structurally, not opened in the
  renderer). **Method**: Spike, open the payload in `web/inspector.html`,
  Phase 1 Step 1.
- [ ] textual-serve runs the cockpit app in a browser tab with the same
  screenshot as the pane. **Status**: Documented. **Method**: Spike, Phase 1
  Step 4.
- [ ] palinex may depend on `textual` as an optional extra without breaking its
  no-daemon, pip-install-only constraint. **Status**: Documented (Textual is
  pure Python; RDR-001 constraints). **Method**: Docs Only; confirmed at the
  first `pip install palinex[tui]`.

## Proposed Solution

### Approach

Item 1. **Two panels, each one a2ui v0.9 payload built with palinex.** The
active-claims panel: every claimed tuple across the tenant's take-enabled
subspaces, one row each, ordered by claim time then id, with subspace,
claimant, kind or key summary, held-for, lease-left, attempts. The
recent-events panel: ledger starts and reports, mailbox arrivals and
dead-letters, and (once Gap 2 closes) claim-log transitions, newest last,
capped at the last N minutes, never reordered. Each panel's edge carries only
the operations the source template permits, and only for the operator's own
claims (renew, hand back, ack). Producers live in palinex's nexus bridge; nexus
provides the data.

Item 2. **A claim-log read path in the engine and client.** One read-only
route, filtered by subspace and since-timestamp, paged by the same cursor
shape `tuple_rd` uses; one MCP tool `tuple_log`; one CLI verb `nx tuple log`.
This is the only engine work in this RDR.

Item 3. **A Textual catalog for the a2ui Basic Catalog in palinex.** One
widget per component, keyed DataTable for List templates, `ModalScreen` for
Modal, Buttons posting the a2ui event name and context. The grammar's tokens
are the theme's variables. Image, Video and AudioPlayer render as their
sidecar text.

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

Item 4. **The grammar adopted as the panels' stylesheet.** Neutral base, one
frame hue, colour only on dead, urgent (lease near lapse), stale (heartbeat
late, purge lag) and contested; claimed-by-me as the highlight text tier;
consumed rows absent; rows keep their slot; three text tiers; sentence case.
Pinned by headless snapshot tests, so a rule is an assertion.

Item 5. **Two delivery tiers, not five.** The pane: `palinex cockpit` opens the
Textual app in the operator's tmux pane, polling at one second until RDR-211's
`wait` ships, then subscribing. The window: `palinex cockpit --serve` runs the
same app in a browser tab through textual-serve, which answers RDR-064's
objection. The on-demand snapshot for chat stays what palinex already does:
`render_surface` plus the markdown sidecar. RDR-111's five-level demotion
cascade is not built; `nx tuple watch` remains the notification tier by
existing behaviour.

Excluded from this RDR, deliberately: bindings and any reaction loop, the
connection manifest, the auto-layout engine, `surface_cell` and recursive
surfaces, an LCARS look, a live browser host bridge for MCP UI.

### Technical Design

Data flow: engine → nexus tuple client (`tuple_list`, `tuple_rd`, `tuple_stats`,
new `tuple_log`) → palinex nexus bridge (producers build one `Surface` per
panel and set its data model) → one of two renderers (palinex `web/index.html`
via `render_surface`, or the Textual catalog in-process) → operator.

Refresh: the Textual app runs one worker per panel on a one-second
`set_interval`; each tick fetches, diffs against the last data model by tuple
id, and applies `update_cell` for changed rows and `add_row` for new ones. It
never rebuilds the table. Rows disappear only when their tuple is gone from the
read (consumed or purged), and the app announces the removal in the status
line for one refresh (Nielsen: nothing silently removed).

Identity: the operator's claimant identity is what the nexus MCP server and CLI
already resolve (session id or instance name); the highlight tier and the
edge's operations key on it.

Claim-log route, illustrative shape only:

```text
GET /v1/tuples/claim-log?subspace=<name>&since=<iso>&cursor=<created_at,id>&limit=<n>
→ [{tuple_id, subspace, transition, claimant, attempts, lease_until, at}]
```

Tenant scoping and the transition vocabulary come from the engine as built; the
implementation derives both from `TupleRepository` and the DDL and pins them by
test, never from this document.

Theme tokens, the only new names: `frame`, `frame-deep`, `mine`, `state-dead`,
`state-urgent`, `state-stale`, on top of ground, ink, muted and border. Light
and dark values each defined once.

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| Active-claims and recent-events producers | `palinex/src/palinex/nexus_bridge.py` | Extend: it already imports nexus's stores for chash resolution; add the two producers beside it. |
| Claim-log read path | `TupleRepository.java` (claim-log writes), `http_tuple_store.py`, `core.py`, `tuple_cmd.py` | Extend: one route, one client method, one tool, one verb. |
| Textual catalog | `palinex/web/index.html` (browser catalog) | New sibling in palinex; same dispatch-per-component shape. |
| Polling refresh | `src/nexus/tuple_watch.py` | Reuse nothing: the watcher is mailbox-typed and RDR-211 deletes it. The cockpit polls with its own worker. |
| Theme tokens | `palinex/web/index.html` eight CSS tokens | Extend: the same names in the browser renderer, added under a `data-theme` attribute (palinex work, outside this RDR's gate). |
| Grammar snapshot tests | `pytest-textual-snapshot` | New: one snapshot per panel per theme. |

### Decision Rationale

The panels are the smallest thing that closes RDR-111's remaining gap, and the
substrate feeds them today with one added read path. Building them as a2ui
payloads keeps one IR for the browser, the terminal and chat, which is
palinex's founding decision. Building the terminal renderer in Textual rather
than notcurses keeps the whole stack in Python, gives a styling layer the
grammar maps onto directly, and makes the grammar testable. Excluding bindings
and auto-layout is the postmortem's lesson applied: ship the instruments, then
decide whether the cockpit wants a reaction loop.

## Alternatives Considered

### Alternative 1: Revive RDR-111 whole

**Description**: bindings, connection manifest, auto-layout with the five-level
demotion cascade, and the three panels together.

**Pros**:

- The complete cockpit as designed; user-authored reactions from day one.

**Cons**:

- The exact shape that stranded 67 beads in May; the panels would wait on the
  binding engine.

**Reason for rejection**: the panels are independently shippable and prove the
value; bindings can follow as their own RDR with the panels as their display.

### Alternative 2: The browser only, through palinex's MCP UI resource

**Description**: no terminal work; panels are `render_surface` snapshots the
operator requests.

**Pros**:

- Zero new renderer code.

**Cons**:

- A snapshot is not a cockpit. The MCP path cannot push updates, and a re-request
  remounts the iframe and loses position, which violates the position-stability
  rule.

**Reason for rejection**: it stays as the chat tier; it cannot be the ambient
tier.

### Alternative 3: Extend `nx tuple watch` into a status line

**Description**: keep the watcher and add claim counts to its ping lines.

**Pros**:

- Smallest change.

**Cons**:

- The watcher is mailbox-typed, RDR-211 deletes it, and a status line answers
  neither panel's question.

**Reason for rejection**: notification tier only.

### Alternative 4: Rich tables printed on demand, no Textual

**Description**: `nx tuple claims` and `nx tuple events` print the two panels as
Rich tables.

**Pros**:

- No new dependency; works in any terminal and in chat.

**Cons**:

- No live update, no highlight of the operator's own claims as the lease runs,
  no edge operations.

**Reason for rejection**: kept as the snapshot form of the same renderables,
insufficient alone.

### Briefly Rejected

- **notcurses** (RDR-119's terminal catalog): C library with thin Python
  bindings, no styling layer, abandoned with the cockpit.
- **Ink** (the framework under Claude Code's own interface, a React reconciler
  over Yoga flexbox): TypeScript and Node, not this project's runtime.
- **The RDR-064 HTMX console as the cockpit host**: a second renderer with no
  shared IR; textual-serve gives the browser window from the same app.

## Trade-offs

### Consequences

- Positive: the operator sees claims and events across sessions in one pane
  within a second of change, in a form pinned by tests.
- Positive: one payload per panel serves pane, window and chat.
- Positive: the claim-log read path also serves RDR-211's doctor rows and any
  later audit.
- Negative: palinex gains an optional dependency (`textual`) and a second
  renderer to keep in step with the browser one; the a2ui-to-Textual table is
  the contract that keeps them honest.
- Negative: polling at one second is a standing load per open cockpit until
  RDR-211's `wait` replaces it.
- Negative: the engine gains one route, so the panels' full function waits on
  an engine cut and deploy; the ledger and claims panels work without it.

### Risks and Mitigations

- **Risk**: the panels become a second place to build features (filters,
  actions, bindings) and the scope lesson repeats.
  **Mitigation**: the exclusion list in Approach item 5 is gate-checked; any
  addition is a new RDR.
- **Risk**: Textual's compositor is Python and a tenant with thousands of live
  claims makes the refresh visibly slow.
  **Mitigation**: the spike in Critical Assumptions measures refresh cost at
  1,000 rows; the panel caps rows and pages beyond the cap, which the grammar
  already prescribes.
- **Risk**: the browser and terminal renderings drift.
  **Mitigation**: one payload, two snapshot tests per panel, the mapping table
  as the reviewed contract.
- **Risk**: a claim-log route exposes cross-tenant history.
  **Mitigation**: the route reuses the tuples row-level security policy; the
  test suite's tenant-isolation contract test covers it before the route ships.

### Failure Modes

- Engine unreachable: the pane shows the last data model with a stale marker on
  every row and the census line names the failure; nothing is cleared.
- Read path missing (older engine): the recent-events panel shows ledger and
  mailbox rows only and a one-line note that claim transitions need engine
  version X; the claims panel is unaffected.
- Identity unresolved: no highlight tier and no edge operations; the panel is
  read-only and says so in the status line.
- Diagnosis: `palinex cockpit --once --json` prints one refresh's payload and
  sidecar for inspection; `nx doctor` gains no row from this RDR.

## Implementation Plan

### Prerequisites

- [ ] Critical Assumptions verified (four spikes, one source search).
- [ ] Sam decides the Open Questions.
- [ ] palinex accepts the work under its own RDR (RDR-127: nexus ships no
  rendering code).

### Minimum Viable Validation

One real engine, two sessions, one tmux pane, one browser tab:

1. Session A opens `palinex cockpit`. Session B sends A a mailbox message; A
   takes it. Within one second the active-claims panel in A's pane shows the
   row in the highlight tier with the lease counting down; B's pane shows the
   same row neutral with A as claimant.
2. A lets the lease run to its last minute: the row gains the urgent marker.
   A renews from the panel's edge: the marker clears, the lease resets, and
   the row has not moved.
3. A dispatches a background agent. The recent-events panel appends the ledger
   start row, then the report row, without reordering earlier rows.
4. With the claim-log route deployed, B nacks a task three times; the events
   panel shows the three nacks and the dead transition, and the claims panel
   shows the dead marker until purge.
5. `palinex cockpit --serve` shows the same two panels in a browser tab; the
   headless snapshot of the pane and the SVG of the tab differ only in size.
6. `render_surface` of the claims payload renders in palinex's browser renderer
   and its markdown sidecar lists every row.

### Phase 1: Code Implementation

#### Step 1: The two producers and their payloads (palinex)

Build `claims_surface()` and `events_surface()` in the nexus bridge from the
existing read tools. Validate structurally, open in `web/inspector.html`, pin
the markdown sidecar. Test: a fixture data model renders to a fixed component
count and a fixed sidecar.

#### Step 2: The Textual catalog and the grammar theme (palinex)

One widget per component per the table; the theme tokens; snapshot tests for
each panel in light and dark; the DataTable stability spike (update a middle
row, add a row, cursor and order unchanged).

#### Step 3: The refresh worker and the pane entry point (palinex)

`palinex cockpit`: one worker per panel, one-second interval, diff by tuple id,
`update_cell` and `add_row` only. The polling-cost spike runs here for one
hour against a local engine and records request count and p95 latency in this
RDR's Research Findings.

#### Step 4: The window (palinex)

`--serve` through textual-serve; the tab screenshot spike.

#### Step 5: The claim-log read path (nexus, engine and client)

Route, client method, `tuple_log` MCP tool, `nx tuple log`; the wire-ledger
entry marked additive; the tenant-isolation contract test extended; the
recent-events panel consumes it when the engine reports the capability.

#### Step 6: Documentation

`docs/tuple-space.md` gains a "Seeing it" section; palinex's README gains the
cockpit; this RDR's mapping table is referenced, not copied.

### Phase 2: Operational Activation

#### Activation Step 1: Engine cut

The claim-log route ships in the next engine cut after RDR-211's engine work,
paired per the release choreography; the panels degrade per Failure Modes on an
older engine.

### Day 2 Operations

| Resource | List | Info | Delete | Verify | Backup |
| --- | --- | --- | --- | --- | --- |
| Claim-log route | N/A (read-only) | `nx tuple log --help` | N/A | contract test | N/A |
| Cockpit process | tmux pane or browser tab | `--once --json` | close the pane | snapshot tests | N/A |

### New Dependencies

- palinex: `textual` (MIT), `textual-serve` (MIT), `pytest-textual-snapshot`
  (MIT, test only), as the `[tui]` extra. No legal review needed.
- nexus: none.

## Test Plan

- **Scenario**: the claims payload from a fixture with one mine, one urgent,
  one dead and one neutral row — **Verify**: component count fixed; sidecar
  lists four rows in created order; both snapshots show colour only on the
  urgent and dead rows and the mine row in the highlight tier.
- **Scenario**: refresh with one row changed and one added — **Verify**: the
  DataTable's row keys keep their order; the cursor row is unchanged; only one
  cell and one row were touched (counted through a test double).
- **Scenario**: engine unreachable for two refreshes — **Verify**: no row
  removed; every row carries the stale marker; the census line names the error.
- **Scenario**: claim-log route on an engine without it — **Verify**: the
  events panel shows ledger and mailbox rows and the capability note; no
  traceback.
- **Scenario**: tenant isolation on the claim-log route — **Verify**: a second
  tenant's transitions never appear.
- **Scenario**: the same payload in the browser renderer — **Verify**: every
  row present; the openUrl action absent from the payload (it is blocked in the
  MCP sandbox).

## Validation

### Testing Strategy

1. **Scenario**: the Minimum Viable Validation steps 1 to 6 on a local engine
   with the gate jar. **Expected**: every step's observation recorded in this
   RDR's Revision History with timestamps.
2. **Scenario**: snapshot suite in palinex CI. **Expected**: green in light and
   dark for both panels.

### Performance Expectations

Measured, not estimated: the polling spike (Step 3) records request count and
p95 latency for one hour with 50 live claims, and the DataTable spike records
refresh time at 1,000 rows. Both numbers land in Research Findings before the
gate.

## Open Questions (for Sam)

1. Dependency direction. RDR-127 puts every surface component in palinex,
   which makes the cockpit's entry point `palinex cockpit`. Is a convenience
   `nx tuple cockpit` that shells out to palinex when installed acceptable, or
   does the entry stay in palinex only?
2. Does the claim-log route ride RDR-211's engine cut or the one after it?
3. Refresh at one second (ISA-101's target) or two? The spike will price both.
4. Does the recent-events panel include mailbox arrivals for every address in
   the tenant, or only the operator's own instance and session addresses?

## Finalization Gate

> Complete each item with a written response before marking this RDR as
> **Accepted**.

### Contradiction Check

To be completed at the gate. At draft time: none found between the research
findings and the design. One withdrawn proposal is recorded in Key Discoveries
(colouring the claimed state) so a reviewer sees the resolution rather than
the omission.

### Assumption Verification

Six assumptions: one source search and four spikes scheduled in Phase 1, one
docs-only item confirmed at first install. None is verified at draft time
beyond the two builder and prototype facts in Key Discoveries.

#### API Verification

| API Call | Library | Verification |
| --- | --- | --- |
| `tuple_list`, `tuple_rd`, `tuple_stats` | nexus client | Source Search |
| `tuple_log` | nexus client and engine | To be built; Source Search first |
| `Surface.list(template=, template_path=)`, `validate()`, `to_markdown()` | palinex | Verified 2026-09-16 |
| `DataTable.add_row(key=)`, `update_cell` | Textual 8.2.8 | Docs Only; Spike in Step 2 |
| `App.run_test`, `save_screenshot` | Textual 8.2.8 | Verified 2026-09-16 |
| textual-serve | textual-serve | Docs Only; Spike in Step 4 |

### Scope Verification

The Minimum Viable Validation is in scope and runs before the RDR closes.

### Cross-Cutting Concerns

- **Versioning**: the claim-log route is additive and enters the wire-contract
  ledger; the panel reads the engine's version to decide whether to call it.
- **Build tool compatibility**: N/A.
- **Licensing**: Textual, textual-serve and pytest-textual-snapshot are MIT;
  a2ui is Apache-2.0.
- **Deployment model**: local and cloud engines both serve the route once cut;
  the cockpit runs on the operator's box.
- **IDE compatibility**: N/A.
- **Incremental adoption**: the cockpit is opt-in; nothing changes for a
  session that never opens it.
- **Secret/credential lifecycle**: the cockpit uses the same endpoint and
  credential resolution as the nexus CLI; nothing new is stored.
- **Memory management**: the panels cap rows and page; the diff keeps one
  previous data model per panel.

### Proportionality

Two producers, one terminal catalog, one theme, one refresh worker, two entry
flags, one read route with its tool and verb, and six snapshot tests. The
document is sized to that and to the exclusions it must defend.

## References

- docs/rdr/rdr-111-orb-agentic-cockpit-substrate.md (Problem Statement; the
  three surfaces, lines 528-595; the auto-layout engine, 595-626; scrap reason)
- docs/rdr/rdr-118-surfaces-as-tuples.md; docs/rdr/rdr-119-cockpit-ui-fabric.md
  (the `nexus.notcurses.v1` table)
- docs/rdr/rdr-127-substrate-decoupled-surface-rendering.md
- docs/rdr/rdr-064-nx-console-embedded-web-ui.md (Alternatives, item 7)
- docs/rdr/rdr-205-linda-tuple-space-over-postgres.md;
  docs/rdr/rdr-206-tuple-claim-renew-and-reply-in-ack.md;
  docs/rdr/rdr-211-board-queue-and-lock-tuple-templates.md
- docs/postmortem/2026-05-16-rdr110-113-remediation-chain.md
- T3 interface-design: `research-tuple-surface-design-synthesis-2026-09-16`
  (1.11.569), `research-coordination-display-grammar-2026-09-16` (1.11.564,
  1.11.565), `research-palinex-a2ui-rendering-envelope-2026-09-16` (1.11.566),
  `example-queue-frame-a2ui-payload-2026-09-16` (1.11.568),
  `terminal-surfaces-narrowed-to-textual-2026-09-16`
- T3 tuple-space: `research-tuple-space-observables-for-surfaces-2026-09-16`
  (1.11.567)
- T2 nexus: `orb-cockpit-reframe-2026-09-16`, `tuple-surface-design-synthesis-2026-09-16`,
  `terminal-surfaces-narrowed-to-textual-2026-09-16`
- palinex: docs/rdr/rdr-001-architecture.md, src/palinex/__init__.py,
  src/palinex/nexus_bridge.py, web/index.html, web/host-bridge.html
- a2ui v0.9 specification and theming guide, a2ui.org (fetched 2026-09-16)
- Textual 8.2.8 documentation: design guide (themes), DataTable, App.run_test
- Heil, Moradi, Weis, "LCARS: The Next Generation Programming Context", AVI 2006
- ISA-101 (2015); EEMUA 191 (1999, 3rd ed. 2013); Nielsen 1994; Mackay, ACM TOCHI
  1999; Pousman and Stasko, AVI 2006; Mankoff, Dey et al., CHI 2003; Bercovitz
  and Carriero, TupleScope, Yale RR-782, 1990
- Artifact: https://claude.ai/artifact/DBfEkXXTwGJzY2iXV7qesn (inventory and the
  rendered prototype)

## Revision History

- 2026-09-16: created (draft), from Sam's request after the surface research
  synthesis and the ORB reframe (T2 nexus/orb-cockpit-reframe-2026-09-16).
