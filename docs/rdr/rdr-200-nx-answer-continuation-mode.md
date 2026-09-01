---
title: "nx_answer Continuation Mode and the Composed-Retrieval Bridge Route"
id: RDR-200
type: Architecture
status: draft
priority: medium
author: Hal Hildebrand
reviewed-by: substantive-critic (T2 [23900], 2026-09-01)
created: 2026-09-01
accepted_date: # YYYY-MM-DD, set by /rdr-accept
related_issues: ["nexus-4e75w", "nexus-mt9p8"]
---

# RDR-200: nx_answer Continuation Mode and the Composed-Retrieval Bridge Route

> Status: draft — numbered, critic-reviewed (T2 [23900]), gate PASSED
> 2026-09-01 (critique + re-verification T2 [23912]); awaiting
> `/conexus:rdr-accept`.
> This RDR covers two beads as ONE surface: `nexus-4e75w` (continuation
> mode) and `nexus-mt9p8` (the RDR-152 bridge composed-retrieval route).
> They are one surface because the continuation envelope is what the
> bridge serves.

## Problem Statement

`nx_answer` composes multi-step retrieval into a synthesized answer. It
executes plan-match, then runs the matched plan's steps, then returns
prose. Measured over its whole 214-row history, the retrieval steps cost
**$0.00 and 2.5-10.5s**; the reduction steps — the `claude -p`
subprocess dispatches that run `extract` / `summarize` / `generate` /
`compare` — carry **100% of the dollars and roughly 70% of the wall
clock** ($0.74-0.90 and 60-95s per untiered step; executed-run p50
80.1s, p95 217.1s, mean ~$0.64-1.05).

Two consequences follow, and they are the whole problem.

**First, the tool loses to its own caller.** An in-session agent that
can run `search` in 2-8s for free will not wait 80s and spend a dollar
for a composed answer. Organic usage is ~5 calls/month and has been
since 2026-06-01 — measured through an 80-day window in which the
maximal pro-`nx_answer` routing mandate was still in force, which
falsifies "fix the guidance" in advance (T2 [23879] RC-3 — the RC-n /
O-n / C-n labels used throughout this RDR are that analysis's
root-cause, option, and correction numbering). The residual value is
real but narrow: cross-corpus synthesis and RDR research.

That figure is a snapshot at today's corpus size, not a steady state
(Sam, 2026-09-01). Flat top-N similarity search degrades as the corpus
grows: the top-N fills with adjacent-but-irrelevant matches — crowding,
a phenomenon this project already measures on its own corpus (the
`nexus-x9mly` recall-crowding work and its `ef_search` floor
discriminator). Composed retrieval holds precision at scale because it
navigates structure instead of ranking one flat similarity list:
metadata/graph/topic-scoped search, multi-step plans, winnowing
operators. So the caller-only arm's competitiveness — the premise of
"just use search" — is *trending down* as the corpus grows, while the
composed path's value trends up. The entropy argument does not remove
the measurement burden (the Phase 1 gate below is designed to test it
directly); it changes what the disuse figure means.

**Second, the bridge route cannot be built.** `nexus-mt9p8` has been
open since 2026-06-18 with a hard constraint from the conexus edge
(the edge-relay finding `njrcn-relay` r3): composed retrieval is
30s-5min, the edge proxy is a bounded 16-thread synchronous relay
behind an ALB (the cloud load balancer) with a ~60s idle timeout, and
a synchronous `/v1/answer` would starve the data path. The
prescribed remedy was async submit→poll on an isolated concurrency
budget. No such machinery exists in the engine (verified: zero
submit/poll routes, zero streaming responses, every `/v1/*` response is
one JSON body). So the route's cost was "build a job system first", and
the prospective consumer shipped around it.

Both consequences have the same cause: `nx_answer` forks a separately
billed frontier model to reduce evidence it has already retrieved —
while its caller is *already a frontier model with the evidence in
front of it*.

The obvious protocol fix is unavailable. MCP `sampling`/`createMessage`
— server asks the client's model to complete something — is not
implemented by Claude Code, Claude Desktop, or the API MCP connector
(anthropics/claude-code#1785, open; verified 2026-08-31). There is no
other server→client LLM delegation in the MCP spec. Borrowing the
caller's model *by protocol* is off the table.

What remains is to borrow it **by convention**: return the reduction
instead of performing it.

### Enumerated gaps to close

#### Gap 1: The reduction is performed in the wrong process

The evidence is retrieved server-side and then shipped to a cold
subprocess that pays an ~11s session bootstrap before it reads a
character, while the process that asked the question holds a warm
context it has already paid for.

#### Gap 2: There is no shape in which a non-Claude-Code consumer can use composed retrieval

`nx_answer` is an MCP tool and nothing else — there is no `nx answer`
CLI command (verified: `src/nexus/commands/` has `answer_runs.py`
only). An editor or plugin consumer has no supported entry point, and
the one the bead asks for is blocked on latency it does not control.

#### Gap 3: Any caller-side reduction is invisible to telemetry unless designed for

`nx_answer_runs` is an append-only event log with no amend route. A
handoff that is recorded only when the caller comes back records
nothing when the caller does not — which reproduces the
`nexus-wzw9p` census-disagreement class: a counter that disagrees with
the runs table because the write that would reconcile them was never
designed in.

## Relationship to Prior RDRs

Scanned the full RDR index for overlap with `nx_answer`, operator
dispatch, the bridge, and reduction placement.

**Origins** (they created what this RDR changes):

- **RDR-080** (closed) built `nx_answer` and chose subprocess
  (`claude -p`) reduction. That choice's rationale — one composed tool,
  self-contained execution — predates any consideration of the caller
  as a reduction host, and MCP sampling did not (and still does not)
  exist. The composed-retrieval half of its rationale holds and is
  preserved; the reduction-placement half is what this RDR revisits.
- **RDR-152** (closed) built the engine and the thin HTTP bridge Phase 3
  extends. Its route model (synchronous, one JSON body, `AuthFilter`)
  is the constraint F4 works within, not against.

**Precedents:**

- **RDR-196** (closed) built the cost/telemetry instrumentation this
  RDR's measurement plan rides. Its post-mortem names plan generality
  as the binding constraint and never considered reduction placement —
  this RDR is the first to treat placement as the lever.
- **RDR-079** (abandoned) is the negative precedent for server-side
  lifecycle machinery: warm `claude -p` pools, abandoned over a
  blocking synchronous auth check plus lifecycle complexity and
  superseded by the pool-less dispatch (PR #168). Cited in
  Alternative 2 as the shape not to rebuild. The telemetry design's
  precedent is `nexus-wzw9p`, not RDR-079.

**Adjacent drafts** (unmerged, overlapping surface — scope boundaries):

| Concern | Belongs to |
|---|---|
| What is retrieved (taxonomy-aware recall) | RDR-134 |
| What plans promise to produce (target contracts, witness obligations) | RDR-189 |
| Plan step primitives beyond today's grammar (`loop`/`collect`) | RDR-190 |
| Who performs the terminal reduction, and the envelope that carries it | **RDR-200** |

No sequencing dependency in either direction: RDR-134/189/190 change
what plans retrieve or promise; this RDR relocates execution of the
terminal reduction. If RDR-190 lands, `loop`/`collect` steps are
multi-dispatch by construction and fall outside the single-bundle
suffix — the existing headless fallback covers them with no change
here. RDR-123/124 (superseded) touched `nx_answer`'s *rendering*, not
reduction placement; no overlap.

## Context

### Background

- **RDR-080** built `nx_answer` and the operator substrate. **RDR-196**
  (closed 2026-08-21) made it cost-aware: per-step `StepRecord`
  telemetry, budget enforcement, cost-aware plan choice, and the model
  tier flip. RDR-196 never considered the caller-side option; its
  post-mortem names plan generality, not reduction placement, as the
  binding constraint.
- **The tiering lever is nearly spent.** `operator_extract`, `filter`,
  `groupby`, `rank`, `check`, `verify` already default to the cheap
  alias (`FLIPPED_OPERATORS`, `src/nexus/operators/model_tiers.py`).
  `aggregate` / `summarize` / `compare` / `generate` stay on the strong
  tier per the `nexus-rv9xp` synthesis study's actual verdicts:
  summarize/generate/compare **refuted for both cheap arms**; aggregate
  **refuted for haiku**, and sonnet-on-aggregate **NOT_REFUTED at 3/6**
  — a no-signal result at n=6 the study itself called "not a flip
  license". Fable (the strongest Claude tier) on synthesis remains the
  measured pin for the rest. The one door left ajar is bounded:
  extending the sonnet-aggregate cell past n=6 could at best move ONE
  of the four operators one tier down, while the three refuted
  operators still carry the terminal-suffix cost. Continuation mode is
  therefore the only remaining lever on the *bulk* of synthesis cost;
  the sonnet-aggregate extension is a cheap, parallel, non-exclusive
  follow-up, not an alternative to this design.
- **The recall thread just shipped** (033f8d71b, `nexus-93cc6`;
  T2 [23885]): the grow-time generalizer, the `match_description`
  round-trip, and the browsing-only any-lexeme FTS fallback — the fix
  to the plan-generality constraint RDR-196's post-mortem named as
  binding. All of it runs server-side and is unaffected by who
  performs the reduction.
- **RDR-079** built warm `claude -p` worker pools and abandoned them
  (blocking auth check, lifecycle complexity). Named here as the
  anti-pattern this design must not re-create.

### Technical Environment

Established by direct reading of the tree at `033f8d71b`:

- **`nx_answer` is entirely client-side Python.** Plan-match, the plan
  runner, argument resolution, auto-hydration, bundling and operator
  dispatch all live in `src/nexus/mcp/core.py` and
  `src/nexus/plans/`. The engine is called only for storage and
  telemetry.
- **The dispatch boundary is already a clean seam.**
  `nexus/plans/bundle.py::compose_bundle_prompt(bundle)` returns
  `(prompt, json_schema)`, and `dispatch_bundle` does nothing but hand
  that pair to `claude_dispatch`. Every standalone `operator_*` tool in
  `mcp/core.py` has the same shape: build `prompt`, build `schema`,
  call `claude_dispatch(prompt, schema, ...)`.
- **Hydration is already done before that seam.**
  `runner.py::_hydrate_operator_args` calls `store_get_many` and
  substitutes document contents into the operator's args, capped at
  `_OPERATOR_MAX_INPUTS = 100`, with `source_collections` captured
  pre-hydration so the composer can attribute content to its corpus.
  The bundle prompt therefore already *contains* hydrated evidence.
- **The structured envelope already carries chunk provenance.**
  `_result()` in `mcp/core.py` returns `chunks: [{id, chash,
  collection, distance}]` alongside `final_text`, `plan_id`,
  `step_count`, `steps[]`, `cost_usd`, `truncated_chars`,
  `budget_warnings[]`, `plan_choice`, `budget_exhausted_at_step`.
  Every optional key is **always present** with a null/`[]` value —
  a documented convention ("a caller relies on the key, never
  membership-checks it") that any new field must follow.
- **The bridge is the Java engine-service.** Routes are registered in
  `service/src/main/java/dev/nexus/service/NexusService.java` via
  `server.createContext(...)`, each behind `AuthFilter` (Bearer token
  hashed and resolved server-side; tenant authoritative from the token,
  never from the client header). No async submit→poll. No streaming.
  Adding a route means: new `HttpHandler`, new `createContext`
  registration, a `/version` capability flag, an engine tag, a deploy,
  and a paired Python thin client.
- **`nx_answer_runs` is append-only.** Schema at
  `service/src/main/resources/db/changelog/telemetry-001-baseline.xml`
  (changeset `telemetry-001-7`), with `nx_answer_steps` as a
  CASCADE child (`telemetry-007-nx-answer-steps.xml`). Routes are
  `POST /v1/telemetry/nx_answer_runs/record` and `GET
  .../query` — **no PATCH, no amend, no UPDATE at runtime anywhere.**
- **The run-state split is derived at read time, client-side.**
  `src/nexus/commands/answer_runs.py::_split_three_way` branches on
  `step_count > 0`, then `_row_is_failed` keys on `final_text`
  **prefixes** — including `NX_ANSWER_BUDGET_EXHAUSTED_MARKER_PREFIX`
  (`"[budget exhausted"`, defined at `mcp/core.py:7350`). A run state
  expressed as a `final_text` marker prefix is therefore an
  **established convention in this codebase**, not a new mechanism.

## Research Findings

### Key Discoveries

**F1 — Continuation mode is an early return at an existing boundary,
not a second pipeline.** `dispatch_bundle` already computes exactly
what a caller would need: `compose_bundle_prompt(bundle)` →
`(prompt, schema)`, over hydrated args. Continuation mode returns that
pair instead of sending it. This is the difference between a design
whose fidelity is a *promise* and one whose fidelity is a *code-sharing
invariant*.

**F2 — The cut point must be defined structurally, because plans
interleave.** A plan may be `search → extract → search($step.ids) →
generate`; handing "the operators" to the caller would strand a
retrieval step that depends on an operator's output. The well-defined
cut is the **maximal terminal suffix of operator steps**. Everything
before it runs server-side exactly as today.

**F3 — The residual cost is concentrated in four operators.** The
cheap-tier flip already covers the mechanical operators;
`aggregate`/`summarize`/`compare`/`generate` are pinned strong with the
cheap arms measured and refuted. Those four are overwhelmingly the
*terminal* steps of composed plans, so F2's suffix and F3's cost centre
coincide.

**F4 — Continuation mode dissolves mt9p8's blocking constraint rather
than satisfying it.** The reason a composed route could not be
synchronous is that composition takes 30s-5min. A continuation-mode
composed route runs plan-match plus retrieval only: 2.5-10.5s, $0.00 —
comfortably inside the ALB's ~60s idle. The async submit→poll
machinery the relay demanded, which does not exist and would be a large
engine build, is **not needed**, because the thing that made it
necessary is exactly the thing that is handed off. This dissolution is
complete for the MCP path. For the bridge path it is **qualified** by
the fidelity gap named in the bridge section below: the Java engine
cannot share the Python prompt builder, so a bridge envelope cannot
carry a byte-identical `reduction_spec.prompt` without porting prompt
composition — F1's invariant does not extend to Phase 3 as drafted.

**F5 — It also matches what the asking consumer said it wanted.**
Conductus (an external editor/vault client that consumes the bridge)
gave its own design input on `nexus-mt9p8`: *"KEEP retrieval
primitives and the composed route SEPARATE — editor clients often want
retrieval-only (related-notes, no generation) and should not be forced
to take server-side generation."* A continuation envelope is composed
retrieval **without** forced server-side generation. Conductus already
built a `LocalComposedProvider` (bridge retrieval + BYO/local LLM
generation); the envelope is precisely that provider's missing input.

**F6 — A fourth run state costs zero engine change.** Because
`_split_three_way` derives state client-side from `step_count` and
`final_text` prefixes, a `NX_ANSWER_CONTINUATION_MARKER_PREFIX`
following the budget-exhausted precedent yields a fourth state with no
schema migration, no new route, no engine tag.

### Critical Assumptions

- **A1.** A frontier calling model executing the *same prompt and
  schema* the headless path would have used produces an answer of
  comparable quality. **Unvalidated — this is Phase 1's gate.** If it
  fails, the design fails.
- **A2.** The marginal token cost to the calling session is materially
  below the $0.74-0.90 per untiered dispatch it replaces. Plausible
  (no subprocess bootstrap, no separate session, warm context) but
  **not zero and not measured**. Order of magnitude: a cap-sized
  envelope is ~15k tokens of caller input context — real marginal
  dollars on a metered caller and, more binding, context headroom;
  repeated continuation calls in one session compound both. See Risk
  R4.
- **A3.** A hydrated terminal suffix fits a caller's context budget for
  the majority of real questions. **Unmeasured**; the fallback rate is
  Phase 1's instrument.
- **A4.** An MCP caller is, by construction, a model in a session — so
  capability is implied by the call itself and no negotiation handshake
  is needed. True for every caller that exists today.

## Proposed Solution

### Approach

`nx_answer` gains a **continuation mode**. In it the server does
everything it does today except the final reduction: plan-match (with
the generalizer and browsing fallback untouched), plan choice, budget
accounting, retrieval, hydration, and any mid-plan operator steps whose
outputs later retrieval depends on. It then stops at the **continuation
cut**, records a handoff run row, and returns a **continuation
envelope** carrying the exact prompt and output schema the reduction
would have used, over the evidence already hydrated for it. The calling
session performs the reduction in-context.

The headless `claude -p` path is retained unchanged as the
**reference implementation**: the reproducible path used by the
measurement harness, by any caller that sets `continuation=False`, and
by any future non-model caller.

### Technical Design

#### The continuation cut

> **CONTINUATION SUFFIX** — the maximal terminal suffix of
> `plan_json.steps` consisting solely of operator steps
> (`bundle.is_operator_tool`).

- `search → search → extract → generate` — suffix is
  `[extract, generate]`; **zero** `claude -p` dispatches; the whole
  reduction is handed off.
- `search → extract → search($step2.ids) → generate` — suffix is
  `[generate]`; the mid-plan `extract` runs server-side because the
  second retrieval depends on it; only the terminal synthesis is handed
  off. This still removes the strong-tier step, which is the cost
  centre (F3).
- A plan with no terminal operator suffix has nothing to continue. It
  returns exactly what it returns today. This includes the
  `single_query` fast path, unchanged.

**The suffix must compose into a single bundle.** `segment_steps` may
split an operator sequence into multiple segments (unbundleable
operators, oversize), and the envelope carries exactly one
`(prompt, schema)` pair. Phase 1 hands off only a suffix that composes
into **one** bundle; a multi-segment suffix falls back to headless for
that call, telemetered exactly like the size-cap fallback. The
measured multi-segment rate decides whether `reduction_spec`
generalizes to a list — a Phase-2 refinement, considered alongside
Alternative 5.

#### The envelope

One new key on the structured envelope, following the file's
always-present convention (`null` when the call was not a continuation):

```jsonc
"continuation": {
  "spec_version": 1,              // caller MUST refuse loudly on unknown
  "continuation_id": "…",         // server-generated; correlates the report
  "run_id": 12345,                // nx_answer_runs.id of the handoff row
  "cut_at_step": 2,               // 0-based plan index of the suffix start
  "plan_id": 357,
  "reduction_spec": {
    "prompt": "…",                // VERBATIM compose_bundle_prompt output
    "response_schema": { … },     // VERBATIM _terminal_schema output
    "operators": ["extract", "generate"],
    "prompt_chars": 41234
  },
  "hydrated_bundles": [ … ],      // provenance, see below
  "reduction_contract": {
    "return": "one JSON object conforming to response_schema",
    "report_tool": "nx_answer_report"
  }
}
```

**`reduction_spec.prompt` is not newly authored prose.** It is the
byte-identical string `dispatch_bundle` would have handed
`claude_dispatch` for that suffix. One prompt builder, two carriers.
This is the mechanism by which "a faithful caller converges with the
headless path" stops being an aspiration.

**`hydrated_bundles` carries provenance, not a second copy of the
evidence.** The hydrated content is already inside the prompt (that is
how the headless path works). What the prompt does not carry in
machine-readable form is where each piece came from, and citations must
survive the handoff. Each entry is the existing chunk shape plus the
plan step it fed:

```jsonc
{ "step_index": 1, "collection": "knowledge__…",
  "items": [ { "id": "…", "chash": "…", "tumbler": "…",
               "collection": "…", "distance": 0.31 } ] }
```

`id`/`chash`/`collection`/`distance` are exactly what `_result()`
already harvests for `chunks`; `tumbler` is already present in
structured retrieval step outputs. `title` / `source_path` — which
conductus asked for, to render `[[wikilinks]]` — require a catalog
manifest join that no current path performs. **Deferred, and named as
an open question** rather than smuggled in.

#### Size discipline

`MAX_BUNDLE_PROMPT_CHARS` is 200,000 (~50k tokens) — a budget sized for
a dedicated subprocess, not for someone else's working context. A
separate, smaller `MAX_CONTINUATION_PROMPT_CHARS` governs continuation
mode. Proposed starting value **60,000 chars (~15k tokens)**, stated as
a starting value with no measurement behind it; Phase 1's fallback rate
is what tunes it.

On exceeding the cap, the segment **falls back to headless dispatch**
for that call, reusing the exact pattern the bundle path already uses
for its own oversize case (`bundle_oversized_fallback_to_per_step`),
with a structlog warning. The caller gets a normal answer; the
telemetry records that continuation did not apply and why. No silent
truncation of evidence.

#### Mode selection

There are exactly two caller classes, and they are distinguishable
without a negotiation protocol:

| Caller | Default | Rationale |
|---|---|---|
| MCP tool | Phase 1 opt-in → Phase 2 default-on | An MCP caller is a model in a session (A4) |
| Bridge (Phase 3) | Per-request field | An editor client declares whether it has a reducer |

The tool signature gains one parameter:

```python
continuation: bool | None = None   # None = policy default; False = force headless
```

An opt-in flag as the *end state* would repeat RC-3's lesson: for 80
days a maximal routing mandate produced ~5 calls/month, so a flag
agents must remember to set will be set approximately never. The
opt-in exists only to measure. Flipping the default is a separate
decision bead on the `.p2d` precedent (the RDR-196 sub-bead that
separated tier-flip *eligibility* from the flip *decision*) —
flip-eligible is not flip-decided. That flip bead carries a migration
note: A4 holds for
every caller that exists *today*, not every caller that could exist —
a programmatic MCP client that is not a model in a session (a harness,
a scripted eval) would receive an instruction blob as `final_text`,
and pins `continuation=False`.

#### What the caller actually does

In **text mode** (`structured=False`, what every skill and agent uses
today) the return value *is* the reduction instruction: a short
imperative preamble, then the verbatim prompt, then the schema, then
the report line. The calling model executes it because it is an
instruction in its context, not because it implements a protocol.
There is no new client-side machinery to build for Claude Code. The
preamble **delimits data from instruction**: the hydrated evidence
inside the prompt is named as data to be reduced — never instructions
to follow — and the verbatim prompt is fenced as a quoted block (see
Risk R7).

In **structured mode** the caller reads `continuation.reduction_spec`
programmatically. This is what the bridge serves.

`/conexus:query` and the `using-nx-skills` routing table need one
paragraph each describing the two possible return shapes. No routing
change — the routing-table narrowing decided at `nexus-h33x8.6` (which
question shapes get steered to `nx_answer` at all) stands, and this
RDR does not reopen it.

#### Telemetry

**The handoff row is written at handoff, before the envelope
returns — never when the caller reports.** That single ordering rule is
what keeps telemetry from going dark, and it is what prevents the
`nexus-wzw9p` shape (a counter that disagrees with the runs table).

- `final_text` begins with `NX_ANSWER_CONTINUATION_MARKER_PREFIX`
  (proposed `"[continuation handed off"`), followed by the
  `continuation_id`. This follows the budget-exhausted marker
  convention exactly.
- `step_count` counts the steps that **actually executed
  server-side** — never the planned total. A handoff after two
  retrieval steps records 2. Inflating it to include un-executed
  operators would fabricate work, which is the defect class RDR-196
  exists to close.
- `cost_usd` is the honest server-side sum: `0.0` for an all-SQL
  prefix, which `runner.py::_record_step` already distinguishes from
  `None` ("unknown"). A caller-side reduction's cost is **`None`,
  never `0.0`** — we cannot observe it, and recording zero would
  manufacture a "cost went to zero" headline that is an accounting
  artifact.
- **The split becomes four-way**: `executed-ok` / `executed-failed` /
  **`handed-off`** / `degenerate`, derived in `_split_three_way` from
  the new prefix. Client-only.
- **Completion reporting is a second append, not a mutation.** The log
  is append-only by doctrine; inventing an amend route to satisfy a
  reporting convenience would be the wrong move and an engine change
  besides. A new MCP tool `nx_answer_report(continuation_id, ok,
  final_text_excerpt)` writes a second row carrying the same
  `continuation_id` in its marker line. `answer-runs` pairs them at
  read time. The report row carries its **own** marker prefix
  (proposed `"[continuation completed"`) and is classified as a
  *report event, not a run* — excluded from the four-way run split
  entirely, so it can never double-count a run or misclassify as
  `degenerate` on its `step_count = 0`.
- **A handoff with no paired completion is counted — as *unreported*,
  not "abandoned".** The metric cannot distinguish a reduction that
  never happened from one that happened and was never reported: the
  report line is a convention the calling model must remember to
  follow, and the mode-selection section's own argument — a flag
  agents must remember to set will be set approximately never —
  applies to it verbatim. The published metric is therefore the
  **unreported rate**, labelled by what it actually measures. Phase 1
  pre-registers a decomposition audit: sample unreported handoffs
  (session transcripts where available) and classify non-reduction vs
  non-reporting **before** the Phase 2 threshold is set (OQ-2). This
  keeps the mechanism's failure mode counted rather than invisible —
  the `nexus-wzw9p` lesson — with the count named honestly.
- **Continuation runs are never folded into the headless cost
  average.** `answer-runs` labels the population, exactly as it already
  labels the pre-`COST_INSTRUMENTED_SINCE` dilution. Repeating the C3
  dilution defect inside the RDR that cites it would be indefensible.

#### The bridge route (nexus-mt9p8)

**`POST /v1/answer`** on the engine, behind the existing `AuthFilter`,
returning the continuation envelope. Synchronous, single JSON body, no
new job system, no streaming — because plan-match plus retrieval fits
inside the ALB idle (F4).

Scope is deliberately restricted. A full plan-runner port to Java is
**not** proposed. The engine executes a matched plan's **retrieval
prefix** using machinery it already has — plan-match (`PlanHandler`,
`/v1/plans/*`) and the combined-query SQL functions
(`search_metadata_scoped_<dim>`, `search_graph_hop_<dim>`,
`search_topic_scoped_<dim>`, `search_aspect_scoped_<dim>`) — and
returns the operator suffix as spec, **never executing it**. The one
piece that cannot be ported (spawning `claude -p`) is exactly the piece
continuation mode never executes. That is what makes the route
tractable.

Plans outside the supported grammar, and plan misses (the inline
planner is a `claude -p` dispatch, impossible engine-side), return a
**typed refusal** naming the reason — never a degraded answer.

**The bridge path breaks F1's byte-identity invariant, and this RDR
says so rather than hiding it.** `reduction_spec.prompt` is
byte-identical *because* the MCP path shares
`compose_bundle_prompt` — Python code the engine cannot call. The
bridge therefore faces a fork: (a) port prompt composition to Java and
keep two implementations byte-identical across languages — which
regresses fidelity to exactly the "promise" F1 exists to eliminate —
or (b) return the raw operator suffix plus hydrated provenance and let
the *client* compose via a published spec, accepting that the bridge
envelope's `reduction_spec.prompt` is absent and the composing burden
moves to the consumer. Nor is "NOT a plan-runner port" the whole
truth about the retrieval prefix: `$stepN` argument resolution,
hydration semantics (`_hydrate_operator_args`, `_OPERATOR_MAX_INPUTS`,
`source_collections` attribution) are Python runner machinery the
engine would have to replicate, and the combined-query functions carry
a documented tumbler-vs-chash hydration caveat (bead `nexus-zekpl` —
**closed** 2026-08-29 as disclosed-in-docstring for the client path;
closure does not discharge the caveat for new engine-side hydration,
which faces the same identity mismatch). These are
additional reasons Phase 3's ship gate is a consumer asking: the
fork's resolution — (b) looks better, since the asking consumer
already builds its own reduction — is deferred to that consumer's
actual requirements, not decided speculatively here.

Two further consumer asks from the same design input, disposed
explicitly rather than silently. **Scope parameters** (collections /
subtree / link-follow depth): the Phase 3 request shape accepts them
and passes them through to the retrieval prefix — natural, because the
combined-query SQL functions already take exactly those parameters;
the detail belongs to the consumer-driven Phase 3 spec. **Streaming**:
conductus asked for a token/chunk stream for server-side generation
output; under continuation mode there is no server-side generation to
stream, and the retrieval-only response is one small JSON body well
inside the ALB idle — so streaming is deliberately not built. If
progressive delivery matters to a real consumer, it is a Phase 3
requirement to surface then.

**This is the only part of this RDR that requires an engine change**:
new `HttpHandler`, `createContext` registration, `/version` capability
flag, engine tag, deploy, paired Python client. It is additive (old
client + new engine safe) and would ride a normal wire-ledger entry.

### What does NOT change

Stated explicitly because the value of this design depends on it:

- **Plan-match** — the gate, the confidence floor, the FTS
  verbatim-repeat sentinel, RDR-091 scope-fit re-ranking, and the
  RDR-196 cost-aware plan choice within the confidence band.
- **Plan growing and the generalizer** — the just-shipped
  `match_description` round-trip and the grow-time haiku generalizer
  (T2 [23885]) run server-side on every novel question, regardless of
  who reduces.
- **The browsing-only any-lexeme fallback** — `plan_match`
  structurally cannot reach it; unchanged.
- **Retrieval execution, auto-hydration, the `_OPERATOR_MAX_INPUTS`
  cap, bundling, segmentation** — all server-side and intact.
- **The headless `claude -p` path** — retained as the reference
  implementation, not deprecated.
- **The operator substrate** — `nx_tidy`, `nx_plan_audit`, and the
  standalone `operator_*` tools are untouched.
- **Routing guidance** — the `nexus-h33x8.6` narrowing stands.

## Alternatives Considered

### Alternative 1: MCP sampling / `createMessage`

The protocol-native way for a server to borrow the client's model.
**Rejected: unavailable.** Not implemented by Claude Code, Claude
Desktop, or the API MCP connector (anthropics/claude-code#1785, open;
verified 2026-08-31). No other server→client delegation exists in the
spec. Revisit if it ships — it would be strictly better than a
convention.

### Alternative 2: A persistent session pool inside the server

Keep server-side execution; amortize the ~11s bootstrap with a warm
`ClaudeSDKClient` pool. Estimated 80s → ~55-65s, minor dollar cut.
**Rejected on precedent and arithmetic.** RDR-079 built warm `claude -p`
worker pools and abandoned them (blocking auth check, lifecycle
complexity); the caller's own request names this the shape not to
rebuild. Operator prompts are dominated by unique bundle content that
never cache-hits, so the advertised prefix-cache win does not apply.
It re-imports a known lifecycle burden to buy ~20% of a latency problem
whose other 100% is available for free.

### Alternative 3: Tier the synthesis operators to a cheap model

The 10x lever that already worked for six operators.
**Rejected for three of the four operators: measured and refuted.** The
`nexus-rv9xp` synthesis study refuted the cheap arms for
summarize/generate/compare outright and refuted haiku for aggregate.
Sonnet-on-aggregate came back **NOT_REFUTED at 3/6** — no-signal at
n=6, "not a flip license" in the study's own words. That residual is
real but bounded: extending that one cell is a cheap follow-up that
could at best re-tier one operator to sonnet, and it *composes* with
continuation mode (which removes the dispatch entirely on the common
path) rather than competing with it. For the three refuted operators —
which dominate the terminal suffix — this alternative is closed on
evidence.

### Alternative 4: Status quo — accept the disposition and stop

The disuse analysis's own recommendation (O1 + O6a): accept that ~5
calls/month is the coherent consequence of two deliberate decisions,
reframe the metric, park `nx_answer` as an agent-facing tool.
**This is the honest alternative and it stays live until Phase 1's gate
passes.** It is cheaper than every other option and its supporting
evidence is already in hand. The case for building instead rests
entirely on A1 — that the envelope beats the caller's own
search-and-reason. If Phase 1 refutes A1, Alternative 4 is the answer.
(OQ-1 decided 2026-09-01: build direction — this alternative is now the
gate's fallback, not the default disposition; the entropy argument in
the Problem Statement is the reason, and the stratified gate is what
tests it.)

### Alternative 5: Cut at the first strong-tier operator instead of the terminal suffix

Let the server run the cheap-tier structural operators (which winnow),
then hand off only from the first strong-tier step. Smaller payload for
the caller's context.
**Rejected for Phase 1, retained as a Phase-2 refinement.** It costs an
extra dispatch (~11s, ~$0.08) and breaks a bundle that exists precisely
to avoid a second round trip, in exchange for a payload reduction that
the size cap already handles by falling back. Reconsider if the
measured fallback rate is high.

### Alternative 6: A Python sidecar HTTP service for the bridge route

Serve the composed route from Python, where the plan runner already
lives, and have the conexus edge proxy to it.
**Rejected: a new deployment surface with no consumer asking.** The
engine is the one service in this architecture; adding a second
long-lived network service to avoid a bounded Java handler is a poor
trade. Revisit only if the bridge genuinely requires the plan-miss path
(see Open Question OQ-3).

## Trade-offs

### Consequences

- Retrieval-only calls become 2.5-10.5s and $0.00 server-side; the
  ~11s subprocess bootstrap disappears from the common shape.
- Reduction quality becomes a property of the caller. For an in-session
  frontier model this is neutral-to-better (more context, more
  instruction-following headroom); for a weak caller it is worse, and
  the design makes that visible rather than preventing it.
- `nx_answer` becomes usable by a consumer that is not Claude Code,
  which it has never been.
- The measured cost of `nx_answer` will drop sharply in the telemetry.
  **Part of that drop is real and part of it is cost we stopped
  metering.** The telemetry design above exists to keep those two
  distinguishable; a reader who ignores the population labels will draw
  a false conclusion.

### Risks

**R1 — Reduction fidelity becomes caller-dependent.**
*Mitigation:* the reduction spec is the byte-identical prompt and schema
the headless path would have used (F1), enforced by Phase 0's shared
builder plus a guard test; the headless path is retained as the
reference implementation and is what the measurement harness runs;
`continuation=False` forces it. *Residual:* a caller may paraphrase
rather than execute, or answer from memory instead of the bundle. Only
Phase 1's comparison detects this, which is why it is the gate.

**R2 — Telemetry goes dark.**
*Mitigation:* the handoff row is written before the envelope returns,
never on report; the fourth state is derived from an established marker
convention; the unreported rate is a counted metric, not a blind spot.
*Residual:* the completion row's reduction cost is genuinely
unobservable and records `None`. We will know *that* a reduction
happened and not *what it cost*.

**R3 — Bundle size overruns the caller's context.**
*Mitigation:* a dedicated, smaller cap with headless fallback on
breach, reusing the existing oversize-fallback pattern.
*Residual:* the starting cap value is a guess. Phase 1 measures it.

**R4 — "Free" is an accounting artifact.**
*Mitigation:* stated in the design, enforced in the CLI's population
labelling, and `None`-not-`0.0` for unobservable cost.
*Residual:* the headline number will still be quoted without its label
by somebody. This already happened once (the C3 dilution, ~180 rows of
hardcoded `0.0` understating cost 6.5x).

**R5 — The design solves a problem nobody has.**
Organic demand is ~5 calls/month and the mt9p8 consumer shipped around
the route once already. *Mitigation:* Phase 3 does not start on our
initiative — it starts when a consumer asks. Phases 0-2 are small and
Phase 0 is worth landing on its own merits.

**R6 — The caller could just do this itself.**
The sharpest objection: continuation mode reduces `nx_answer` to
"`search` plus a prompt", and the caller already runs `search` and
reasons over results — which is *why* the tool went unused. What
survives is (a) the matched plan, a curated multi-step retrieval
strategy the caller does not have to invent, plus the generalizer and
browsing fallback behind it; (b) cross-corpus composition with
provenance the caller would otherwise assemble by hand; (c) a
reduction contract that makes the answer reproducible instead of
improvised; and (d) the scale trend — flat search swamps in adjacent
facts as the corpus grows (see Problem Statement), so the margin over
caller-only widens with corpus size rather than eroding. The honest
form of (d): the caller *can* invoke the scoped search tools by hand,
so the durable margin is curated strategy plus provenance at zero
caller effort, not exclusive capability. *If these do not beat a
caller's own search-and-reason on a measured question set, this RDR
should not ship and Alternative 4 is the answer.* That comparison is
Phase 1's pre-registered gate, and it is stratified to test (d)
directly.

**R7 — The reduction moves from a tool-free subprocess into a
tool-armed session.** Hydrated evidence is untrusted corpus text. The
headless path reduces it inside a `claude -p` dispatch that is
tool-free by default (the T1 dispatch contract's own census: ~15/17
call sites); continuation mode injects that same untrusted text —
wrapped in an imperative instruction the model is told to execute —
into a session holding tools, write permissions, and the user's
authority. An instruction-shaped chunk becomes an instruction inside
an instruction. *Mitigation:* the text-mode return delimits data from
instruction explicitly (the preamble names the fenced evidence as data
to reduce, never directives to follow), and the structured-mode
consumer receives the prompt as a field, not as conversation.
*Residual:* delimiting is advice to a model, not a sandbox; a
sufficiently instruction-shaped chunk may still steer a weak caller.
Named rather than solved — the headless path remains the conservative
choice for an untrusted corpus, and `continuation=False` is the
per-call escape.

## Implementation Plan

Phases 0-2 are **client/MCP-shape only — no engine change, no engine
tag.** Phase 3 is the only engine work.

### Phase 0 — Extract the prompt builders (no behaviour change)

Every `operator_*` tool in `mcp/core.py` builds `prompt` and `schema`
inline immediately before `claude_dispatch`. Hoist each into a pure
`build_<op>_request(args) -> (prompt, schema)` called by both the tool
and the continuation path.

*Ship gate:* byte-identity tests (each hoisted builder returns exactly
what its tool used to build) plus a lint-bucket test asserting no
operator constructs a prompt inline at its dispatch site. Full suite
green.

*Worth landing on its own merits even if the rest is parked* — it is a
pure refactor with a guard, and it is what converts R1's mitigation
from a promise into an invariant.

### Phase 1 — Continuation envelope, opt-in, MCP only

`continuation: bool | None`; the continuation cut; the envelope; the
size cap with headless fallback; the handoff row and marker; the
four-way split in `answer_runs.py`; the `nx_answer_report` tool.

*Ship gate (pre-registered, with a refutation criterion):* on a fixed
question set drawn from the shapes the 214-row history shows genuinely
benefit (the May paper-corpus questions and RDR research), compare three
arms — continuation, headless, and caller-only search-and-reason —
judged blind on answer quality and citation correctness. **Continuation
must be at least as good as headless and strictly better than
caller-only.** If it is not better than caller-only, Alternative 4 is
the answer and Phases 2-3 do not proceed. Additionally: zero
telemetry-dark runs, and a measured envelope-size distribution with the
fallback rate reported.

*Gate mechanics, fixed before Phase 1 lands:* a frozen question set of
~24 (the recall thread's harness-size precedent), committed to the
repo; blind judging by a judge model that is **not** the caller-arm's
model, on a written rubric covering answer quality and citation
correctness — the caller-only arm is judged on content, since it has
no schema to conform to; "at least as good" and "strictly better" are
win-rates with **ties awarded against continuation**, so the null
hypothesis is Alternative 4. Judge identity and any margin beyond
simple majority are OQ-6.

*Stratification (pre-registered prediction):* the set is split into a
**crowded** stratum and a **clean** stratum. The entropy argument
(Problem Statement) predicts continuation's margin over caller-only is
largest in the crowded stratum. If the margin does not appear even
there, the entropy rationale is refuted on today's corpus and the
build/park decision returns to Sam with that evidence — the gate must
not pass on an average dominated by easy questions while the claimed
value lives in the hard stratum.

The stratum assignment is **operationally defined and frozen with the
question set, before any arm runs**: for each question, run the plain
flat `search` top-10; the judge model (OQ-6) labels each result
relevant / irrelevant to the question; the question's **crowding
score** is the irrelevant fraction, and crowded means score ≥ 0.5.
Assignment is fixed at freeze time by the set-assembly step, not by
whoever later interprets results. Each stratum must hold at least 8
questions; if either falls short, the set is extended before Phase 1
runs. Numbers proposed, pinned at OQ-7.

*Arm identity:* the **same model plays both the continuation-arm
reducer and the caller-only-arm reasoner** — otherwise the comparison
confounds retrieval strategy with raw model capability, which is
exactly the variable the gate isolates. That model is the session
model the default flip would serve (Fable-class at time of writing),
named in the pre-registration.

*No-signal protocol (pre-registered):* a stratum below its minimum
size, or a win-rate statistically indistinguishable from a tie, yields
**INCONCLUSIVE — which is not a pass**. The `nexus-rv9xp` precedent
governs: no-signal at small n is "not a flip license". An inconclusive
gate returns to Sam with the data; it never silently defaults to
proceed.

### Phase 2 — Default flip for MCP callers

A separate decision bead on the `.p2d` precedent, gated on Phase 1's
**unreported rate** — decomposed per the telemetry section's audit into
non-reduction vs non-reporting — being below a threshold pre-registered
*before* Phase 1 runs, and on the fidelity comparison holding on a
larger set. The bead carries the programmatic-caller migration note
from the mode-selection section.

### Phase 3 — Bridge route (`nexus-mt9p8`)

`POST /v1/answer` with the restricted grammar and typed refusals; the
`/version` capability flag; the paired Python client; the wire-ledger
`[additive]` entry.

*Ship gate:* **a consumer asking for it.** Per the disuse analysis
RC-5/O6c, the prospective consumer routed around this route once and
argued the composed route should stay optional. Phase 3 does not start
on our initiative.

## Test Plan

- **Phase 0:** byte-identity per operator; the inline-prompt lint guard.
- **Cut selection:** table-driven over plan shapes — trailing operators,
  interleaved retrieval-after-operator, no operator suffix, single
  operator, empty plan, and a **multi-segment suffix** (must fall back
  to headless, never emit a partial envelope).
- **Fidelity:** a golden test asserting the envelope's
  `reduction_spec.prompt` is byte-identical to what `dispatch_bundle`
  would send for the same suffix. This is the test that keeps R1's
  mitigation real as the code changes.
- **Size cap:** a bundle over the cap falls back to headless and emits
  the warning; the caller still receives a normal answer.
- **Telemetry:** a continuation call writes exactly one handoff row
  before returning; the marker prefix classifies as `handed-off`; a
  report writes exactly one paired row that classifies as a **report
  event, never a run** in the split; an unpaired handoff is counted as
  unreported; a continuation run never appears in the headless cost
  population.
- **Versioning:** an unknown `spec_version` is a loud refusal, never a
  best-effort parse.
- **Phase 3:** engine handler contract tests; a plan outside the
  grammar returns a typed refusal, not a partial answer.

## Validation

The measurement plan uses the now-honest telemetry: `nx answer-runs
--since` works as of `nexus-spbay` (e24a91c4a) — the engine's `--since`
filter previously reused a write-stamp parser whose fallback on an
unparseable bare date was `now()`, so every date-bounded read returned a
manufactured zero. Any pre-fix measurement quoting a `--since` window is
void.

Baselines to capture **before** Phase 1 lands, so the comparison is not
retrospective: executed-ok p50/p95 duration and cost over the current
population, per-operator cost from `--steps`, and the current organic
call rate.

Post-Phase-1: continuation p50/p95 server-side duration and cost;
handoff-to-completion (abandonment) rate; envelope-size distribution and
fallback rate; and the blind quality comparison.

## Finalization Gate

Run 2026-09-01, twice. First run **BLOCKED** on three Criticals (the
rv9xp verdict misstatement, the RDR-079 telemetry framing, the
undefined crowding stratification) plus four Significants — all
provenance-confirmed against their sources before folding. Fixed same
day; the same critic re-verified every fix individually and re-issued
**PASSED** (0 critical, 0 significant). Full critique and
re-verification: T2 `nexus/critique-rdr-200-gate-2026-09-01` [23912];
gate result: T2 `nexus_rdr/200-gate-latest`. Not a design of record
until Sam accepts.

## Open Questions

**OQ-1 — Is Alternative 4 the answer? DECIDED (Sam, 2026-09-01):
build.** Rationale: composed retrieval is the better alternative to
search-plus-prompt, and the leverage grows as the corpus grows — flat
search swamps in irrelevant adjacent facts; composed retrieval is
required to avoid entropy. Phase 0 and Phase 1 proceed. Alternative 4
narrows from "the default disposition" to **the gate's fallback**: it
returns only if Phase 1's stratified gate refutes the margin even in
the crowded stratum.

**OQ-2 — What is the unreported-rate threshold for Phase 2?** It must
be pre-registered before Phase 1 runs, or it will be rationalized
afterward. Note the metric conflates non-reduction with non-reporting;
the pre-registered decomposition audit (telemetry section) informs the
threshold, but a number is still a judgment call about how much silent
non-reduction is tolerable. **Proposed: 25%** — if more than one in
four handoffs goes unreported, telemetry is too dark to flip the
default; the decomposition audit may revise the number *before* Phase 1
runs, never after.

**OQ-3 — Does the bridge need a plan-miss path?** Engine-side plan-miss
is impossible (the inline planner is a `claude -p` dispatch). Is
"plan-hit only, typed refusal on miss" acceptable for bridge v1? If not,
the composed route cannot live in the engine and Alternative 6 (Python
sidecar) comes back into scope with a much larger cost.

**OQ-4 — Do editor citations need `title` / `source_path` in the
envelope?** Conductus asked for per-claim refs with
`chunk_text_hash` + `source_path` + `title`. Only the first is available
without a new catalog manifest join. Add the join, or ship
chash-and-collection and let the client resolve?

**OQ-5 — Should Phase 3 be filed at all before a consumer asks?** R5
and the ship gate say it should not start; but `nexus-mt9p8` has been
open since 2026-06-18. Keep it open as the design of record for when a
consumer arrives, or close it as "designed, not built, reopen on
demand"?

**OQ-6 — Who judges the Phase 1 gate, and by what margin?** The gate
mechanics fix the structure (frozen ~24-question set stratified by
crowding, blind rubric judging, ties against continuation), but the
judge model's identity — it must not be the caller-arm's model — and
whether "strictly better" demands more than a simple majority are
pre-registration decisions that need a call before Phase 1 runs.
**Proposed:** an Opus-tier judge (distinct from the Fable caller arm),
blinded arm labels, simple majority with ties against continuation.

**OQ-7 — Pin the stratification values.** Crowding is operationally
defined in the gate mechanics: the judge-labelled irrelevant fraction
of each question's flat-search top-10, computed and frozen with the
set before any arm runs. **Proposed:** crowded = score ≥ 0.5; minimum
stratum size 8, extending the set if either stratum falls short.
Confirm or replace the numbers — like OQ-2 and OQ-6, they must be
pinned before Phase 1 runs, never after.

## References

- Beads: `nexus-4e75w` (continuation mode), `nexus-mt9p8` (bridge
  route), `conexus-e5lq` (inbound request), `nexus-wzw9p`
  (census-disagreement class).
- T2: [23894] `conexus/conexus-to-nexus-REQUEST-nx-answer-continuation-mode-cost-redesign-2026-08-31`;
  [23879] `nexus/analysis-nx-answer-disuse-2026-08-31`;
  [23885] `nexus/design-93cc6-vi8fp-plan-recall-thread`;
  [22886] `nexus/nx-answer-capability-analysis-2026-08-19`;
  [23900] `nexus/critique-continuation-mode-rdr-draft-2026-09-01`.
- RDRs: RDR-080 (`nx_answer` + operators), RDR-079 (abandoned worker
  pool), RDR-152 (`rdr-152-postgres-java-storage-service.md`, closed —
  the bridge), RDR-196 (`rdr-196-cost-aware-nx-answer.md`, closed) and
  its post-mortem `docs/rdr/post-mortem/196-cost-aware-nx-answer.md`.
- Code: `src/nexus/mcp/core.py` (`nx_answer`, `_result`,
  `_nx_answer_record_run`, `_step_record_to_wire`, the `operator_*`
  tools); `src/nexus/plans/bundle.py` (`compose_bundle_prompt`,
  `dispatch_bundle`, `MAX_BUNDLE_PROMPT_CHARS`, `segment_steps`);
  `src/nexus/plans/runner.py` (`StepRecord`, `_hydrate_operator_args`,
  `_record_step`, `_OPERATOR_MAX_INPUTS`);
  `src/nexus/operators/model_tiers.py` (`FLIPPED_OPERATORS`);
  `src/nexus/commands/answer_runs.py` (`_split_three_way`,
  `_row_is_failed`);
  `service/src/main/java/dev/nexus/service/NexusService.java` (route
  table); `.../http/AuthFilter.java`; `.../http/TelemetryHandler.java`;
  `service/src/main/resources/db/changelog/telemetry-001-baseline.xml`,
  `telemetry-007-nx-answer-steps.xml`.
- External: anthropics/claude-code#1785 (MCP sampling unsupported,
  open).

## Revision History

- 2026-09-01 — Initial draft. Combines `nexus-4e75w` and `nexus-mt9p8`
  as one surface per Sam's queue directive.
- 2026-09-01 — Folded the substantive-critic review (T2 [23900]):
  bridge-path fidelity gap named and F4 qualified (finding 1);
  abandonment relabelled *unreported* with a pre-registered
  decomposition audit (2); injection risk R7 with data/instruction
  delimiting (3); single-segment restriction on the cut with headless
  fallback (4); report-row classification excluded from the run split
  (5); Phase 1 gate mechanics specified, judge/margin split out as
  OQ-6 (6); programmatic-caller migration note on the flip bead (7);
  A2 marginal-cost sketch (8).
- 2026-09-01 — OQ-1 DECIDED by Sam: build. The entropy/scale argument
  added to the Problem Statement and R6 (flat search swamps in
  adjacent facts as the corpus grows; composed retrieval holds
  precision); Phase 1 gate stratified by crowding with the
  pre-registered prediction that continuation's margin over
  caller-only is largest in the crowded stratum. Proposed values
  placed on OQ-2 (25%) and OQ-6 (Opus-tier blinded judge, simple
  majority) for Sam to pin.
- 2026-09-01 — Numbered RDR-200; Relationship to Prior RDRs added
  (origins RDR-080/152, precedents RDR-196/079, adjacent drafts
  RDR-134/189/190 with scope table).
- 2026-09-01 — Gate-critique fixes (T2 [23912], first gate run
  BLOCKED): rv9xp verdict corrected (sonnet-on-aggregate NOT_REFUTED
  at n=6, "not a flip license" — not "no quality proxy"; Alternative 3
  narrowed accordingly); RDR-079's fabricated telemetry-invisibility
  framing removed everywhere (real failure: blocking auth check +
  lifecycle; `nexus-wzw9p` is the telemetry precedent); crowding
  stratification operationally defined (judge-labelled irrelevant
  fraction of flat-search top-10, frozen at set assembly) with OQ-7
  pinning its numbers; arm-identity and no-signal protocols
  pre-registered; conductus scope-params and streaming asks disposed
  explicitly; `nexus-zekpl` cited as closed with the caveat's residual
  scope stated; jargon glosses (ALB, conductus, Fable, `.p2d`,
  `njrcn-relay`, RC-n labels, h33x8.6).
