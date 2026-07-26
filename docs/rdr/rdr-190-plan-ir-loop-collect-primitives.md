---
title: "Plan-IR Fan-Out and Fold: Add `loop` and `collect` Step Primitives So Plans Can Process More Than 100 Items Without Truncating or Round-Tripping Through the Agent"
id: RDR-190
type: Architecture
status: draft
priority: medium
author: Hal Hildebrand
reviewed-by: unreviewed
created: 2026-07-26
related_issues: []
related: [RDR-078, RDR-079, RDR-084, RDR-088, RDR-089, RDR-093, RDR-097, RDR-179, RDR-189]
---

# RDR-190: Plan-IR Fan-Out and Fold — `loop` and `collect`

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

**Status note**: `draft`. Scope is deliberately two primitives. `parallel`
is explicitly OUT (see Alternatives) and conditionals are excluded on
principle, following the source paper. Do not quote as design intent
until gated.

## Problem Statement

Nexus's plan IR is a straight-line step list. `plan_run`
(`src/nexus/plans/runner.py:1021`) loads `steps` once
(`:1070-1071`), walks them in a single pass, and returns a frozen
`PlanResult` (`:1348`, dataclass at `:212` = `{steps, final}`). Steps
reference prior outputs via `$stepN.field`. There is no fan-out over a
collection, no fold of many results into one, and no sub-plan.

#### Gap 1: The 100-item ceiling truncates real workloads

`_OPERATOR_MAX_INPUTS = 100` (`runner.py:698`). When a step's input
exceeds it, the runner keeps the first 100 and attaches
`{truncated, original_count, kept_count}` (`:763-791`). The truncation is
honestly reported — it is not silent — but a plan author has no way to
opt out and no construct to process the remainder. Any question whose
evidence set exceeds 100 chunks/documents is answered from a positional
prefix.

#### Gap 2: Batch review costs O(N) agent passes instead of O(1)

There is no way for a plan to run a sub-sequence per item and hand the
*aggregate* back. An agent wanting per-item treatment across N items must
either accept the 100-cap or issue N tool calls from its own loop, which
puts N raw payloads into its context — the exact pattern the
code-mode literature prices at ~126x (catalog `1.14.24`).

#### Gap 3: The two primitives that close this are already specified, and we skipped them

Parmar 2026 ("Separating Intelligence from Execution: A Workflow Engine
for the Model Context Protocol", arXiv:2605.00827v1; extraction note
indexed in `knowledge__knowledge`) specifies a five-primitive DSL:
`call`, `loop`, `parallel`, `pipe`, `collect`. Nexus has `call` (every
step) and an implicit `pipe` (`$stepN` references). We have neither
`loop` nor `collect`. The paper's `collect` exists precisely for
"HYBRID agent-engine patterns (agent reviews the batch, not individual
items) — O(1) reasoning passes instead of O(N)."

## Context

- **This is not a general control-flow request.** Parmar deliberately
  excludes conditionals ("branching needs agent reasoning"), variables
  ("data flows implicitly via `steps.<id>`, eliminating scoping bugs"),
  and string manipulation, with the stated rationale that *"JSON DSLs
  tend to evolve into accidental programming languages; a firm boundary
  keeps failure modes predictable."* Nexus is already aligned with those
  exclusions and this RDR keeps them. `loop` and `collect` are the
  deterministic half.
- **The blueprint form is already settled and correct.** Parmar's "Why
  not generate code" section argues for JSON blueprints over generated
  scripts on four grounds: sandboxing (a blueprint can only invoke
  registered tools, no syscalls), inspectability, design-time schema
  validation, and LLM reliability at emitting structured JSON. Nexus
  already chose this. This RDR does **not** propose a script sandbox.
- **Nexus's economics make this cheap to justify but pointless in
  isolation.** Parmar's break-even is K\* ≈ 0.04 executions. Measured
  reuse in the live library is ~0 (RDR-189 R8; `nexus-sbl4m`). Adding
  expressiveness to plans nobody re-runs buys nothing. **This RDR is
  worth doing only alongside raising K** — see Open Question 1.
- **RDR-079 is the cautionary predecessor.** "Operator Dispatch + Plan
  Execution End-to-End" was abandoned because a synchronous
  `subprocess.run` blocked the asyncio event loop — a concurrency defect,
  not a design failure. Any primitive that introduces concurrency must
  confront that directly. This RDR avoids it by deferring `parallel`.
- Operator dispatch already goes out-of-context via `claude_dispatch`,
  and RDR-093's bundled `groupby → aggregate` path already demonstrates
  fold-shaped work inside a single dispatch. `collect` generalizes that
  from one hardcoded pair to a declared step.

## Desired End State

1. A plan step can declare a `loop` over a resolved array from a prior
   step, running a bounded sub-sequence per item, with per-item context
   injected.
2. A plan step can `collect` the outputs of a loop into a single
   aggregate passed to one downstream operator call — one reasoning pass
   over N items, not N.
3. Workloads above 100 items are expressible without positional
   truncation, or the ceiling is a declared per-step budget rather than a
   global constant.
4. The exclusions hold: no conditionals, no variables, no string
   manipulation, no arbitrary code.
5. `PlanResult` still returns one distilled object to the caller —
   per-item intermediates never reach the calling agent's context.

## Research Findings

Marked **[verified]** where confirmed by opening the cited file during
drafting.

### R1 — The IR has no fan-out, fold, or sub-plan **[verified]**

`grep -rniE "sub_?workflow|nested_plan|subplan" src/nexus/plans/` returns
nothing. `schema.py` carries no loop/branch/parallel construct; its only
structural vocabulary is `_REQUIRED_DIMENSIONS` (`:94`) and
`_TRAVERSAL_TOOLS` (`:99`). `plan_run` is a single pass over a flat list.

### R2 — Truncation is reported, not silent, and has no opt-out **[verified]**

`runner.py:763-791` logs a warning and attaches
`{truncated: True, original_count, kept_count}` to the operator envelope.
Good hygiene, but `_OPERATOR_MAX_INPUTS` is a module constant (`:698`)
with no per-step override, so the ceiling is not a policy a plan can set.

### R3 — Nexus has `call` + implicit `pipe`, and nothing else of the five **[verified]**

Every step is a `call`; `$stepN.field` references give `pipe`. `loop`,
`parallel`, `collect` are absent.

### R4 — Open: what actually breaks at >100 today

Not measured. Before committing, quantify how often real `nx_answer` runs
hit the truncation warning. `runner.py:765` already emits a structured
warning with `input_count` — the signal exists and can be counted. If the
answer is "almost never," this RDR is premature and should be closed in
favour of raising the constant.

## Decision (draft — to resolve in research)

**D1. Is `loop` per-item over a sub-sequence, or a single fan-out into a
batched operator call?** The latter is much smaller and may cover most
of the need given operators already accept arrays.

**D2. Does `collect` fold client-side or inside one `claude_dispatch`?**
RDR-093's bundle path is the precedent for the latter and keeps the
intermediates out of context, which is the whole point.

**D3. Per-step input budget, or raise the global constant?** A declared
budget is more honest; raising the constant is one line. R4's evidence
should decide.

**D4. Does the inline planner get to emit these?** `_ALLOWED_TOOLS`
(`core.py:5192`) gates what the plan-miss path may emit. Adding
expressive primitives to a planner that already drops unknown steps
(`core.py:5235-5238`) needs care.

## Approach (phased, draft)

### P0 — Evidence gate (no code)

Answer R4 by counting truncation warnings in real runs. Resolve D1-D3. If
truncation is rare, close this RDR and file a one-line constant bump
instead. **This phase can conclude "not worth building."**

### P1 — `collect` alone

The higher-value half and the smaller one. Fold N prior outputs into a
single operator dispatch, generalizing RDR-093's bundled
`groupby → aggregate`. Shippable and useful without `loop`.

### P2 — `loop`

Bounded fan-out over a resolved array with per-item context injection,
with an explicit iteration cap in the step declaration (no unbounded
iteration, ever).

### P3 — Input-budget policy

Replace or supplement `_OPERATOR_MAX_INPUTS` per D3, keeping the existing
truncation envelope as the reporting mechanism.

## Alternatives Considered

- **Add `parallel` too, completing Parmar's five.** Rejected for this
  RDR. It is a latency optimization, not a capability gain — everything
  expressible with `parallel` is expressible with `loop`, more slowly.
  It also introduces exactly the concurrency surface that killed RDR-079.
  Revisit once `loop` is real and the RDR-079 event-loop hazard has a
  named owner.
- **Add conditionals.** Rejected on the source paper's reasoning, which
  nexus's own architecture already follows: branching is agent territory.
- **Build a script sandbox (code mode).** Rejected — Parmar's four
  arguments against generated code apply, and it is a large new
  execution and security surface for a codebase under a no-knob-reflex
  directive.
- **Do nothing; raise `_OPERATOR_MAX_INPUTS`.** The honest baseline, and
  P0 may well select it. Cheap, and if truncation is rare it is correct.

## Trade-offs

- Every primitive added to a JSON DSL is one step toward the accidental
  programming language the source paper warns about. Two is defensible;
  the discipline is refusing the third without evidence.
- `loop` makes plan cost unbounded in a way flat plans are not. The
  iteration cap is not optional.
- Expressiveness on an unused library is waste. Sequencing matters more
  than the feature.

## Open Questions

1. **[BLOCKING for value, not for design] Is reuse being raised?** RDR-189
   R8 measured K ≈ 0 and `nexus-sbl4m` shows grown plans expiring before
   reuse. Parmar's amortization argument is the entire justification for
   a richer blueprint. If K stays ~0, build P1 for the truncation fix
   alone and drop the rest.
2. Should `collect` be reachable from the inline planner (D4), or
   library-plan-authored only at first, mirroring how `traverse` was
   introduced?
3. Does `PlanResult` need a shape change to carry loop provenance, and
   does that collide with RDR-189 P4's preservation trace on the same
   dataclass?

## References

- Parmar, A.S. (2026). *Separating Intelligence from Execution: A
  Workflow Engine for the Model Context Protocol.* arXiv:2605.00827v1.
  Extraction note in `knowledge__knowledge__voyage-context-3__v1`
  (DEVONthink `x-devonthink-item://079D1F3D-60D6-4901-AAAC-E18A01909BF2`).
- Code-mode measurement: catalog `1.14.24`,
  `knowledge__knowledge__voyage-context-3__v1`.
- `docs/exploration/workflow-engine-*.md` — May 2026 landscape work.
  **Caution**: its closing section ties the design to RDR-110/111/112,
  all scrapped 2026-05-19; that integration story is dead.
- VADAOrchestra borrowables note (`knowledge__knowledge`, 2026-06-25) —
  bounded replanning and recursive catalog rules, both still unfiled.
- Code: `src/nexus/plans/runner.py`, `src/nexus/plans/schema.py`,
  `src/nexus/mcp/core.py`.

## Revision History

- 2026-07-26 — created (draft). Scope set at `loop` + `collect`;
  `parallel` deferred with RDR-079's event-loop defect named as the
  reason; conditionals excluded per the source paper. P0 is an evidence
  gate empowered to close the RDR in favour of a constant bump.
