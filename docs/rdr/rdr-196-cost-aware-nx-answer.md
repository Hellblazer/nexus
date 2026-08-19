---
title: "Cost-Aware nx_answer: Per-Step Cost/Quality Telemetry, Per-Operator Model Routing, and Cost-Ranked Plan Choice — Closing the NOMA §5.1–5.3 Gap"
id: RDR-196
type: Architecture
status: draft
priority: medium
author: Hal Hildebrand
reviewed-by: self
created: 2026-08-19
accepted_date:
related_issues: ["nexus-h33x8.6"]
---

# RDR-196: Cost-Aware nx_answer

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

Kaoudi & Giurgiu, *Rethinking Query Optimization for Multi-Agent Systems [Vision]*
(arXiv 2512.11001; catalog 1.14.51; read-against-nexus note 1.11.472) frame multi-agent
pipelines as a new query-optimization problem with four coupled pieces: (§5.1) a joint
topology × model × engine search under multiple objectives, (§5.2) unified *distributional*
cost models, (§5.3) continuous re-planning under stochastic execution, and (§5.4) a semantic
cache of sub-plans and optimization decisions. Their measured baseline — one familiar model
assigned to every agent, fixed topology — is the expensive corner: 153× cost / 5× latency /
25% quality spread across the plan space, with 96% of Pareto-optimal plans using *mixed*
model assignments.

`nx_answer` already has the paper's §5.4 piece and is the paper's baseline on everything
else. The plan library (RDR-078/080/084/100) is a semantically indexed, auto-grown cache of
optimization decisions — the exact artifact the paper says existing semantic caches lack.
But every operator runs on one model via `claude -p`, no step carries a cost/latency/quality
estimate, the run record stores `cost_usd = 0.0` unconditionally, `budget_usd` is "reserved
for future enforcement", and `plan_match` ranks candidates on match confidence alone. The
measured consequence is already on file: nexus-h33x8.6 found `nx_answer` used 4 times in its
lifetime against a mandate routing every analytical question through it, with 23% of runs
taking 2–5 minutes — a capability/latency defect, not a compliance one.

This RDR adopts the paper's own build order, bounded to what nexus can measure: telemetry
first (so a cost model has data), then per-operator model routing (the paper's single
largest measured lever), then cost-ranked plan choice and budget enforcement. The generative
planner (§5.1) and the continuous Bayesian refiner (§5.3) are research programs and are out
of scope; one bounded topology transformation the paper measured (cheap-first with
escalate-on-low-confidence) is an optional, gated last phase.

### Enumerated gaps to close

#### Gap 1: No per-step cost, latency, or model record — the run log cannot feed any cost model

`_record_nx_answer_run` (`src/nexus/mcp/core.py:5760-5786`) persists one row per invocation
with `question, plan_id, matched_confidence, step_count, final_text, cost_usd, duration_ms`;
every call site passes `cost_usd=0.0` (`core.py:6441, 6547, 6573, 6641`). `plan_run` emits
`nx_answer_step_start` / `nx_answer_step_complete` structlog events with `elapsed_ms`
(`src/nexus/plans/runner.py:1210-1345`) — to the log file only, never to the store, and
without model, tokens, or cost. `claude_dispatch` (`src/nexus/operators/dispatch.py:538`)
parses the subprocess's stream-json result envelope and discards its cost/usage fields
(no `cost_usd` / `usage` reference anywhere in the module). NOMA §5.2's observation that
monetary cost is "almost analytic" (tokens × published price) is true here too — and nexus
throws the tokens away.

#### Gap 2: One model for every operator — the paper's "habit" baseline by construction

`claude_dispatch` builds `["claude", "-p", "--output-format", "stream-json", ...]`
(`dispatch.py:643-659`) with no `--model`; every operator — `extract`, `filter`, `rank`,
`groupby`, `aggregate`, `compare`, `check`, `verify`, `summarize`, `generate`, the inline
planner — runs on the session's default model. 18 call sites across 4 modules go through
this one function. There is no per-operator model policy, no way to express one, and no
measurement that would justify one.

#### Gap 3: Plan choice ignores cost; `budget_usd` is unenforced

`plan_match` picks by `confidence = 1 - distance` with a floor
(`src/nexus/plans/matcher.py:462-480`); when several plans clear the floor the cheapest or
fastest is never preferred because nothing records which that is. `nx_answer(budget_usd=0.25)`
documents itself as "reserved for future enforcement" (`core.py:6283`): a plan that will cost
$0.60 runs to completion against a $0.25 cap.

#### Gap 4: The one engine-assignment axis nexus has is chosen by heuristic

`operator_filter` / `operator_groupby` / `operator_aggregate` choose a SQL fast path over
`document_aspects` or a `claude -p` dispatch via `source="auto"` keyword cues
(`src/nexus/mcp/core.py` operator docstrings). That is NOMA's deterministic-vs-stochastic
engine choice in miniature, made without a cost or quality estimate and never recorded as a
decision.

## Context

### Background

Surfaced 2026-08-19 by indexing the paper into `knowledge__semantic-operators` and reading it
against `nx_answer` (note 1.11.472 records the full mapping). The paper's related-work table
positions prior systems as optimizing individual calls (LLM-as-UDF, model routing, serving
orchestration) while leaving pipeline structure, model set and engine fixed; nexus sits in
that row too, with the notable exception of its plan library.

Adjacent nexus work this RDR must not duplicate:

- **RDR-179 (draft)** — self-correction machinery: relight plan-reuse, retrieval benchmarking,
  plan-library hygiene. Its benchmarking leg is the natural home for the *quality* signal this
  RDR needs; this RDR consumes it, does not redefine it.
- **RDR-090 (closed, implementation parked)** — realistic AgenticScholar benchmark: the candidate
  fixed eval set for any quality measurement.
- **RDR-100 (closed)** — plan-cache improvements (diversity, floor, dispatcher, hierarchy): the
  §5.4 piece. RDR-196 adds a cost dimension to what RDR-100 matches on; it does not change
  matching.
- **RDR-177 (draft)** — tenant-scoped telemetry/usage metering on the engine. Gap 1's per-step
  rows are a client-side record of one pipeline's steps; if RDR-177 lands first, Gap 1 should
  write through its surface rather than add a parallel table.
- **RDR-190 (draft)** — plan-IR `loop`/`collect` primitives; topology vocabulary that Phase 3's
  escalate loop would reuse rather than invent.
- **nexus-h33x8.6** — the nx_answer latency/capability measurement that motivates Phase 1.

### Technical Environment

- `nx_answer` composition: `mcp/core.py` (plan-match gate → `plan_run`), `plans/runner.py`
  (step execution, bundling via `plans/bundle.py`, session cache), `operators/dispatch.py`
  (`claude -p` subprocess, stream-json parsing), `plans/matcher.py` (confidence ranking).
- Telemetry: `nx_answer_runs` table owned by the engine (`telemetry-001-baseline.xml`),
  written via `POST /v1/telemetry/nx_answer_runs/record`. All schema change goes through
  Liquibase on the engine (hot rule: ALL DDL through Liquibase).
- Operator bundling (`plans/bundle.py`) already performs NOMA's *fusion* transformation —
  adjacent operators collapsed into one dispatch — heuristically and unrecorded.

## Research Findings

### Investigation

- Paper read in full (sections 1–6; 76 chunks, catalog 1.14.51). Build order stated by the
  authors: (1) unified cost model over fixed topology, (2) generation, (3) refiner, (4) cache.
- Code reads cited in the gaps above; `claude -p` result-envelope fields (`total_cost_usd`,
  `duration_ms`, `usage.input_tokens/output_tokens`, `model`) are what the subprocess emits
  and what Gap 1 would capture — **Assumed** until verified against the installed CLI's
  stream-json `result` event (fixture: `tests/fixtures/claude_dispatch_stream_json_sample.ndjson`
  landed this week by the h33x8.6 a3 work and is the place to check).

#### Dependency Source Verification

| Dependency | Source Searched? | Key Findings |
| --- | --- | --- |
| `claude -p` stream-json result envelope | No (fixture available) | Whether `total_cost_usd`/`usage`/`model` are present per run, and per-turn for bundled steps — to verify before Phase 1 |
| `claude -p --model` | No | Accepts model aliases (`sonnet`, `haiku`, `opus`) and full ids; confirm it composes with `--json-schema` and `--output-format stream-json` |
| engine `nx_answer_runs` DDL | Yes | Columns as listed in Gap 1; per-step table does not exist |

### Key Discoveries

- **Documented** — `cost_usd` is a constant 0.0 at every run-record call site.
- **Documented** — no `--model` in the dispatch argv; no model policy anywhere in the operator layer.
- **Documented** — `plan_match` ranking is confidence-only; `budget_usd` unenforced.
- **Documented** — bundling already changes topology (fusion) without recording the decision.
- **Documented (paper)** — 96% of Pareto-optimal plans in the paper's 10-agent pipeline mix
  models; cheap-first + escalate dominated the best fixed-topology plan at equal quality.
- **Assumed** — the paper's result transfers to nexus operators: extract/filter/groupby/rank are
  cheap-model-tolerant, generate/verify/check are not. This is the Phase 2 measurement, not a
  premise.

### Critical Assumptions

- [ ] The `claude -p` result envelope exposes cost/usage/model per dispatch (and per bundled
      turn) — **Status**: Unverified — **Method**: Spike against the installed CLI + fixture.
- [ ] `--model` composes with `--json-schema` + `stream-json` for every operator prompt shape —
      **Status**: Unverified — **Method**: Spike.
- [ ] A fixed, cheap-to-run quality proxy exists for at least the extract/filter/rank operators
      (RDR-090 set, or a small hand-labelled set under `tests/fixtures`) so Phase 2 can say
      "equal quality" with a number — **Status**: Unverified — **Method**: Source Search
      (RDR-090 / RDR-179 artifacts) then Spike.
- [ ] Per-step rows at nx_answer's real volume are negligible store load (h33x8.6: single-digit
      lifetime runs; even at 100/day × 6 steps this is trivial) — **Status**: Verified by
      arithmetic — **Method**: Docs Only (acceptable: not load-bearing at this volume).

## Proposed Solution

### Approach

Adopt NOMA's build order, each phase gated by the measurement the previous one produces:

1. **Phase 1 — measure (Gap 1, Gap 4).** Capture per-step `{plan_id, run_id, step_index,
   operator, source (sql|llm|bundle), model, input_tokens, output_tokens, cost_usd,
   elapsed_ms, ok}` from the dispatch result envelope and the operator fast-path branch; write
   them alongside the run row; stop writing `cost_usd=0.0` (sum of steps). Surface via
   `nx telemetry`-style read (or `nx doctor` line) and the existing `nx_answer` `structured`
   output. This alone answers "what does nx_answer cost and where does the time go", which
   nobody can answer today.
2. **Phase 2 — per-operator model routing (Gap 2).** A small, explicit policy table
   `operator → model tier` (default: cheap tier for extract/filter/groupby/aggregate/rank/
   summarize; strong tier for generate/check/verify/compare and the inline planner), threaded
   to `claude_dispatch(model=...)`; measured on the Phase-1 telemetry plus the quality proxy
   before it becomes the default. The paper's number to beat is the all-strong baseline at
   equal proxy quality.
3. **Phase 3 — cost-ranked plan choice + budget (Gap 3).** When ≥2 plans clear the confidence
   floor, prefer the one with the lower recorded median cost (latency as tiebreak); enforce
   `budget_usd` as a pre-flight refusal (estimated > cap → refuse with the estimate) and a
   mid-run stop at the step boundary where the running sum crosses the cap. Estimates come
   from the per-plan history Phase 1 accumulates; a plan with no history runs with a warning.
4. **Phase 4 (optional, gated on Phase 2 evidence) — one topology transformation.** For
   `verify`/`check`, cheap-model first and escalate to the strong model only when the cheap
   verdict is low-confidence, as a bounded loop — the single transformation the paper measured.
   Only if Phase 2 shows the cheap tier is *not* good enough for verify/check on its own.

Out of scope, stated so it is not silently re-entered: generative pipeline synthesis (§5.1),
continuous Bayesian refinement (§5.3), time/price arbitrage (§6), any change to plan matching
semantics (RDR-100 owns that), any change to operator semantics.

### Technical Design

- `claude_dispatch(..., model: str | None = None)` appends `--model <tier-or-id>` when set;
  returns, in addition to the parsed JSON, a `DispatchUsage` record
  `{model, input_tokens, output_tokens, cost_usd, elapsed_ms}` parsed from the result event
  (field names **Assumed** — verify against the fixture). Existing callers are unaffected
  (keyword-only, default None).
- `plans/runner.py` collects one `StepRecord` per executed step (including SQL fast-path steps
  with `model=None, cost_usd=0`) and bundles one record per bundled dispatch with the bundled
  step indices listed; passes the list to the run recorder.
- Engine: `nx_answer_steps` table (Liquibase changeset under `telemetry-*`), FK to
  `nx_answer_runs`, tenant-keyed like its parent; `POST /v1/telemetry/nx_answer_runs/record`
  accepts an optional `steps: [...]` array and writes both in one transaction. `nx_answer_runs.cost_usd`
  becomes the sum of steps.
- Model policy: a module-level typed table in `operators/` (`OPERATOR_MODEL_TIER: dict[str, Tier]`)
  plus a per-call override; tiers resolve to concrete model ids in one place so a model rename
  is one edit. Configurable per repo via `.nexus.yml` `[tuning]` only if Phase 2 shows a need —
  not pre-emptively.
- Plan choice: `plan_match` returns candidates above the floor with their recorded
  `median_cost_usd`/`p50_ms` (joined from `nx_answer_steps` history, cached per process);
  `nx_answer` picks min cost among candidates within a small confidence band of the best
  (band width is a named constant, not a knob exposed to users).
- Budget: estimate = median cost of the matched plan's history; pre-flight refusal carries the
  estimate and the cap in the error text; mid-run stop returns the partial result with an
  explicit `budget_exhausted_at_step` marker in structured output (fail loud, no silent
  truncation).

```text
// Illustrative — verify field names against the stream-json fixture
StepRecord = {run_id, step_index, operator, source: "llm"|"sql"|"bundle",
              model: str|None, input_tokens: int, output_tokens: int,
              cost_usd: float, elapsed_ms: int, ok: bool, bundled_steps: [int]}
```

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| Per-step usage capture | `operators/dispatch.py` stream-json parser (h33x8.6 a3) | Extend: parse the result envelope's cost/usage instead of discarding |
| Step records → store | `mcp/core.py::_record_nx_answer_run`, engine `nx_answer_runs` | Extend: add `steps` array + child table; same endpoint |
| Per-step structlog events | `plans/runner.py` `nx_answer_step_*` | Reuse: same instrumentation points, add fields, also persist |
| Model policy | none | New, minimal (one dict + one resolver) |
| Cost-ranked choice | `plans/matcher.py` | Extend: return candidate set with history; selection in `nx_answer` |
| Budget enforcement | `nx_answer(budget_usd)` parameter | Extend: enforce what is already declared |
| Escalate loop (Phase 4) | `plans/bundle.py` fusion, RDR-190 `loop` primitive (draft) | Reuse RDR-190's primitive if landed; otherwise a runner-local bounded retry |
| Quality proxy | RDR-090 benchmark, RDR-179 benchmarking leg | Reuse; if absent, a small fixture set — never an LLM-judge-only proxy for the cheap-tier decision |

### Decision Rationale

The paper's own ordering is the rationale: cost models need data, routing needs a cost model
to be judged against, plan ranking needs routed-step costs to be meaningful. Each phase is
independently useful (Phase 1 alone answers the h33x8.6 question), which keeps the RDR
abandonable at any boundary without stranded work. Model routing is chosen as the first
optimization because it is the paper's largest measured lever, it is a one-argument change at
a single chokepoint, and its risk (quality loss on cheap-tier operators) is exactly what
Phase 1's telemetry plus the quality proxy can measure before it ships as default.

## Alternatives Considered

### Alternative 1: Build the cost model first (learned, distributional, per NOMA §5.2)

**Description**: Train per-objective estimators over a pipeline-graph representation before
changing any behavior.

**Pros**: matches the paper's vision; enables Pareto reasoning.

**Cons**: no training data exists (Gap 1); nexus plans are small DAGs with ~6 operator types,
for which a per-plan median from history is a sufficient estimator; learned quality needs labels
nexus does not have.

**Reason for rejection**: premature; Phase 1's history-based medians are the cost model this
scale needs, and are what a learned model would be bootstrapped from anyway.

### Alternative 2: Route the whole `nx_answer` call to a cheaper model

**Description**: one global model switch instead of per-operator tiers.

**Pros**: trivial.

**Cons**: the paper's central measurement is that uniform assignment is the wrong shape; the
inline planner and `generate`/`verify` are where quality concentrates.

**Reason for rejection**: it is the baseline, moved, not optimized.

### Briefly Rejected

- **Generative planner / Bayesian refiner**: research programs; no eval harness to judge them.
- **Time-of-day price arbitrage (§6)**: nexus runs interactively; deferral is not an option users want.
- **Parallel telemetry table on the client**: violates the PG-everywhere / Liquibase-only rules.

## Trade-offs

### Consequences

- Positive: `nx_answer` cost and latency become observable per step; the h33x8.6 question gets a number.
- Positive: cheap-tier routing should cut cost and latency on the 40%/23% long-tail runs if the paper's result transfers.
- Negative: a second model tier means two models' failure modes in one pipeline; schema-conformance errors on the cheap tier surface as operator failures.
- Negative: a new telemetry table and an endpoint contract change on the engine (a paired engine cut).

### Risks and Mitigations

- **Risk**: cheap-tier operators silently degrade answer quality.
  **Mitigation**: routing ships behind the Phase-1 measurement and a quality proxy; default flips only on evidence; per-operator tiers are individually revertible.
- **Risk**: cost fields in the result envelope differ by CLI version.
  **Mitigation**: parse defensively, record `None` and a warning when absent; the non-vacuity test asserts the fixture carries them.
- **Risk**: budget enforcement refuses plans whose first run has no history.
  **Mitigation**: no-history plans run with a warning, not a refusal; refusal only on an actual estimate.
- **Risk**: scope creep toward the planner/refiner.
  **Mitigation**: out-of-scope list above; phase-review-gate at each boundary.

### Failure Modes

- Telemetry write fails → run still answers; `_warn_telemetry_drop` path already exists; visible in `nx doctor`.
- `--model` rejected by the CLI → dispatch error names the model and operator; policy entry revertible to default tier.
- Budget stop mid-run → partial result with explicit marker; never a silently truncated answer.

## Implementation Plan

### Prerequisites

- [ ] All Critical Assumptions verified (two spikes: envelope fields, `--model` composition; one source search: quality proxy)
- [ ] RDR-177 status checked: write through its surface if it has landed

### Minimum Viable Validation

One `nx_answer` call end-to-end on a multi-step plan produces a run row whose `cost_usd` equals
the sum of its step rows, each step row carrying the model and token counts the subprocess
reported, and `nx_answer(..., structured=True)` returns the per-step breakdown. Asserted by a
test against the engine substrate, not a mock.

### Phase 1: Measure

#### Step 1: Usage capture in `claude_dispatch` (+ fixture-backed test of the result envelope)
#### Step 2: `StepRecord` collection in `plans/runner.py` incl. SQL fast-path and bundled steps
#### Step 3: Engine changeset `nx_answer_steps` + `steps` on the record endpoint; client write-through; `cost_usd` = Σ steps
#### Step 4: Read surface (`structured` output + a `nx` read) and the MVV test

### Phase 2: Per-Operator Model Routing

#### Step 1: `model` parameter on `claude_dispatch`; tier table + resolver
#### Step 2: Quality proxy wired (RDR-090 / fixture set); baseline run all-strong, candidate run tiered; record both
#### Step 3: Flip default only if cost/latency improve at ≥ proxy-equal quality; otherwise record the negative result in the RDR and stop

### Phase 3: Cost-Ranked Choice + Budget

#### Step 1: candidate set with history from `plan_match`; min-cost-within-band selection
#### Step 2: `budget_usd` pre-flight estimate + mid-run step-boundary stop with explicit marker

### Phase 4 (optional, gated): Escalate-on-Low-Confidence for verify/check

Only on Phase-2 evidence that the cheap tier is insufficient for these operators alone.

### Day 2 Operations

| Resource | List | Info | Delete | Verify | Backup |
| --- | --- | --- | --- | --- | --- |
| `nx_answer_steps` rows | In scope (read surface) | In scope | Deferred (event log; TTL/retention follows `nx_answer_runs`) | In scope (MVV) | N/A (engine backup) |
| Model tier table | In scope (one module) | In scope | N/A | Tests | N/A |

### New Dependencies

None. (Cheaper model tiers are reached through the same `claude` CLI.)
