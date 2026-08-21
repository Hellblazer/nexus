---
title: "Cost-Aware nx_answer: Per-Step Cost/Quality Telemetry, Per-Operator Model Routing, and Cost-Ranked Plan Choice — Closing the NOMA §5.1–5.3 Gap"
id: RDR-196
type: Architecture
status: accepted
priority: medium
author: Hal Hildebrand
reviewed-by: self
created: 2026-08-19
accepted_date: 2026-08-20
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

`nx_answer` already has the *sub-plan half* of the paper's §5.4 piece and is the paper's
baseline on everything else. The plan library (RDR-078/080/084/100) is a semantically indexed,
auto-grown cache of retrieval plans — sub-plan topologies keyed by intent, which is the artifact
the paper says existing semantic caches lack. It does **not** yet cache model or engine
assignments, because nexus makes none (Gaps 2–5); once Phases 1–3 record those decisions, the
same library is where they would live.
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

#### Gap 4: The tool-free operator dispatch loads the user's entire MCP server set — ~2× context and cost per call, for tools it cannot use

`claude_dispatch`'s default argv passes `--mcp-config` only when a caller opts into tools and
never passes `--strict-mcp-config` (`dispatch.py:643-694`), so every operator subprocess
inherits every MCP server configured for the user and loads all their tool schemas into
context. Measured (196-research-3): the identical trivial dispatch costs **$1.84 / 92,052
context tokens** with the servers loaded vs **$0.91 / 45,396** with a strict empty MCP
config, on the default model. In `-p` mode without `--allowedTools` those tools cannot be
called, so the overhead buys nothing. Every nx_answer step, `nx enrich aspects` extraction,
`nx_tidy` and the inline planner pays it; `commands/enrich.py::_PER_PAPER_COST_USD = 0.01`
is ~100× under on a default-model box. This is a Phase 0: one argv change, measurable on the
Phase 1 telemetry, no routing required.

#### Gap 5: The one engine-assignment axis nexus has is chosen by heuristic

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
- **RDR-177 (draft)** — tenant-scoped telemetry/usage metering on the engine. Its design is
  tenant-level aggregate snapshots; RDR-196's `nx_answer_steps` is a per-run, per-step event
  log — a different grain, not a competing table. The relationship is one-directional:
  RDR-177's per-tenant nx_answer cost aggregates, if built, roll up from `nx_answer_steps`
  (this RDR's table is the event source, RDR-177 is a consumer). No conditional sequencing.
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
- Code reads cited in the gaps above.
- Four research records in T2 (`nexus_rdr/196-research-1..4`), summarized:
  - **196-R1 (verified, spike)** — the stream-json `result` event carries `total_cost_usd`,
    `duration_ms`, `duration_api_ms`, `num_turns`, `usage.{input,output,cache_creation,cache_read}`
    and `modelUsage.{<model>: {…, costUSD, canonicalModel}}` (per-model, so bundled multi-turn
    dispatches are attributable); assistant events carry `message.model` + per-turn usage.
    `dispatch.py` keeps none of it.
  - **196-R2 (verified, spike)** — `--model haiku|sonnet` composes with `--json-schema` +
    `stream-json` + `--no-session-persistence`; structured output validates; `modelUsage`
    reports the canonical id. Cost of one trivial operator-shaped dispatch, same box, same day:

    | dispatch shape | cost | context tokens |
    | --- | --- | --- |
    | default model, user's MCP servers loaded (today's `claude_dispatch`) | $1.84 | 92,052 |
    | default model, `--strict-mcp-config` empty | $0.91 | 45,396 |
    | sonnet, strict empty MCP | $0.34 | 56,669 |
    | haiku, strict empty MCP | $0.07 | 36,170 |

    ~13× spread between tiers at identical output; harness context dominates the tokens
    regardless of operator input.
  - **196-R3 (verified, spike)** — Gap 4 above: no `--strict-mcp-config` in the tool-free
    default.
  - **196-R4 (documented, source search)** — no operator-level quality proxy exists; RDR-090's
    `scripts/bench/` harness scores *retrieval* (NDCG@3, 5 queries in `bench/queries/spike_5q.yaml`),
    which RDR-179 P2 plans to operationalize. Phase 2 therefore defines its own proxy:
    tier-agreement against the strong model on a fixed operator-input set (exact membership for
    filter/groupby, rank correlation for rank, field-level F1 for extract), with a named threshold
    before any tier flips to default; LLM-as-judge alone is not the proxy for this decision. The
    RDR-090/179 bench remains the retrieval-quality check Phase 3 must not regress.

#### Dependency Source Verification

| Dependency | Source Searched? | Key Findings |
| --- | --- | --- |
| `claude -p` stream-json result envelope | Yes (fixture + live spike, 196-R1) | `total_cost_usd`, `usage.*`, `modelUsage` per model, `duration_ms` present per run; per-turn usage on assistant events |
| `claude -p --model` | Yes (live spike, 196-R2) | `haiku`/`sonnet` aliases compose with `--json-schema` + stream-json; canonical id reported in `modelUsage` |
| `claude -p --strict-mcp-config` | Yes (live spike, 196-R3) | Empty strict config halves context/cost for the tool-free default |
| engine `nx_answer_runs` DDL | Yes | Columns as listed in Gap 1; per-step table does not exist |
| RDR-090 bench harness | Yes (196-R4) | Retrieval-only proxy (NDCG@3, 5 queries); no operator-output proxy exists |

### Key Discoveries

- **Documented** — `cost_usd` is a constant 0.0 at every run-record call site.
- **Documented** — no `--model` in the dispatch argv; no model policy anywhere in the operator layer.
- **Documented** — `plan_match` ranking is confidence-only; `budget_usd` unenforced.
- **Documented** — bundling already changes topology (fusion) without recording the decision.
- **Documented (paper)** — 96% of Pareto-optimal plans in the paper's 10-agent pipeline mix
  models; cheap-first + escalate dominated the best fixed-topology plan at equal quality.
- **Assumed → resolved per operator by Phase 2 (2026-08-21, see the Phase 2 OUTCOME block)** —
  the paper's result transfers to nexus operators: VERIFIED on the fixture for
  extract/filter/groupby/rank (both frozen criteria passed, n=3, ceiling effect noted);
  VERIFIED for check/verify on the three-arm study (fable/sonnet/haiku, 9 cross pairs each;
  check 1.000/1.000, verify 0.980 min / 0.941 mean, both against a 0.70 threshold — flipped
  by nexus-3mea3 as a recorded Sam decision, check's proxy saturated and verify's .p2a
  margin undecidable, both caveats on record); **REFUTED** for
  aggregate/summarize/compare/generate — the synthesis study (nexus-rv9xp) built the
  missing proxy as blind pairwise judging and the cheap arms lost on summarize/generate/
  compare, with sonnet-on-aggregate the sole non-refuted cell at n=6 (no-signal, not a flip
  licence); UNMEASURED for the inline planner (nexus-i8to5 is its prerequisite). Originally
  a premise; now a measurement in both directions — the transfer is real for mechanical
  operators and does not hold for synthesis.

### Critical Assumptions

- [x] The `claude -p` result envelope exposes cost/usage/model per dispatch (and per bundled
      turn) — **Status**: Verified (196-R1) — **Method**: Spike (fixture + live CLI).
- [x] `--model` composes with `--json-schema` + `stream-json` — **Status**: Verified (196-R2)
      for `haiku`/`sonnet` on the operator-shaped trivial prompt — **Method**: Spike. Per-operator
      prompt shapes are exercised by Phase 2's own runs, not assumed.
- [x] A quality proxy for Phase 2 — **Status**: Verified-as-ABSENT (196-R4): no operator-level
      proxy exists; Phase 2 defines tier-agreement-vs-strong on a fixed input set (see Research
      Findings) — **Method**: Source Search. The proxy's construction is Phase 2 Step 2, in scope.
- [x] A strict empty MCP config is accepted by the CLI and removes the inherited tool-schema
      context — **Status**: Verified (196-R3) — **Method**: Spike.
- [x] Per-step rows at nx_answer's real volume are negligible store load (h33x8.6: single-digit
      lifetime runs; even at 100/day × 6 steps this is trivial) — **Status**: Verified by
      arithmetic — **Method**: Docs Only (acceptable: not load-bearing at this volume).

## Proposed Solution

### Approach

Adopt NOMA's build order, each phase gated by the measurement the previous one produces:

0. **Phase 0 — stop paying for tools the operator cannot call (Gap 4).** Pass
   `--strict-mcp-config --mcp-config '{"mcpServers":{}}'` in the tool-free default argv (the
   opt-in `mcp_servers` path is unchanged). Measured on the default model: ~2× fewer context
   tokens and ~2× lower cost per dispatch before any routing. Lands with Phase 1 so the
   telemetry records the post-fix baseline; correct `_PER_PAPER_COST_USD` from the measured
   per-dispatch cost rather than the 0.01 literal.

1. **Phase 1 — measure (Gap 1, Gap 5).** Capture per-step `{plan_id, run_id, step_index,
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

   > **What actually shipped (2026-08-21) differs from the table proposed above, in both
   > directions — the measurement moved two operators the opposite way from the guess.**
   > Shipped: cheap tier (`haiku`) for filter/groupby/extract/rank **plus check/verify**;
   > strong tier for aggregate/summarize/compare/generate, bundles, and the inline planner,
   > every one of them now pinned EXPLICITLY to `STRONG_DEFAULT_ALIAS` rather than inheriting
   > the box CLI default (nexus-ek8tr), and that alias re-pointed from `fable` to `opus` on
   > the synthesis study's opus arm. So `aggregate` and `summarize` — proposed as cheap here —
   > measured as strong-only, and `check`/`verify` — proposed as strong — measured as
   > cheap-tolerant. The proposal is left standing as written because the delta between it
   > and the measurement is the phase's actual finding.
3. **Phase 3 — cost-ranked plan choice + budget (Gap 3).** When ≥2 plans clear the confidence
   floor, prefer the one with the lower recorded median cost (latency as tiebreak); enforce
   `budget_usd` as a pre-flight refusal (estimated > cap → refuse with the estimate) and a
   mid-run stop at the step boundary where the running sum crosses the cap. Estimates come
   from the per-plan history Phase 1 accumulates; a plan with no history runs with a warning.

   > **What actually shipped (.p3c, 2026-08-21, nexus-nyry9.21 round 2) differs from the
   > pre-flight half of this bullet — Sam's decision on a critic CRITICAL, T2
   > p3c-critique-2026-08-21.** As specified above, an over-cap estimate refuses the call.
   > Shipped instead: an over-cap estimate WARNS and RUNS, through the same single warning
   > emitter the no-history case already uses (never a second mechanism) — the pre-flight
   > refusal is demoted to a pre-flight warning. Forcing measurement: `.p3b`'s step-shape
   > estimator (§ Technical Design below) has NO per-plan discriminating power in the live
   > population — a same-day 20-run measurement found all 4 live plans predicting the
   > IDENTICAL $0.90433 (bundle-dominant pricing always resolves to the same generate@opus
   > ceiling, regardless of which plan), median actual/predicted ratio 0.805 (a systematic
   > ~24% overestimate on the typical run), worst overestimate 11.2x, only 3/20 runs
   > under-predicted. A hard refusal at that number is a step function, not a per-plan
   > judgement: under the derived default cap (1.0530 > 0.90433) it is dormant; under any
   > caller-supplied `budget_usd` near real median spend (~$0.70–0.80) it would refuse real
   > runs deterministically and wrongly, including runs that would truly land under the cap.
   > The MID-RUN stop described in this same bullet is UNCHANGED and remains the real
   > enforcement — it sums MEASURED `StepRecord.cost_usd`, not a step-shape guess, which is
   > exactly why it survives this finding while the predicted-cost half does not.
   > Re-enabling a hard pre-flight refusal is gated on the estimator gaining real per-plan
   > discriminating power (a recorded-per-plan-median population, RDR-100/nexus-93cc6, or an
   > equivalent); `BUDGET_ENFORCEMENT_ENABLED` remains the pre-built kill switch for the whole
   > feature if the warning itself needs to come out. Round 2 additionally surfaces a mid-run
   > coverage gap (critic Significant 1): a step whose `cost_usd` comes back unknown cannot
   > contribute to the running sum (correct — never fabricate a 0), so the mid-run stop can be
   > silently blind for part of a run; this is now reported once per run through the same
   > warning emitter rather than left unstated.
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

> **CORRECTION (2026-08-20, nexus-nyry9.7):** The `DispatchUsage` field names above were
> illustrative and unverified. Verified against
> `tests/fixtures/claude_dispatch_stream_json_sample.ndjson`'s terminal result event: the
> per-dispatch duration field is `duration_ms` (plus a separate `duration_api_ms`), not
> `elapsed_ms` as used above and in this section's `StepRecord` illustration below and in
> the Approach section's telemetry-fields list. Top-level `usage.*` is snake_case
> (`input_tokens`, `output_tokens`, `cache_creation_input_tokens`,
> `cache_read_input_tokens`); per-model `modelUsage.<key>.*` is camelCase (`inputTokens`,
> `outputTokens`, `costUSD`, `canonicalModel`), and the map key can differ from
> `canonicalModel` (a requested alias vs. the resolved id) — the recorded model id must be
> `canonicalModel`, never the key (196-R3). Separately: "existing callers are unaffected"
> does not hold for a tuple-return shape — all ~17 non-`aspect_extractor.py` call sites do
> bare `return await claude_dispatch(...)` with no unpacking, so `.p1a`'s implementation
> instead added a keyword-only `usage_sink: list[DispatchUsage] | None = None` out-param
> (default `None` is a true no-op) rather than changing the return type. Implemented in
> `src/nexus/operators/dispatch.py` (`DispatchUsage`, `ModelUsage`, `_parse_dispatch_usage`).

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
  explicit `budget_exhausted_at_step` marker **in both output shapes** — a top-level field in
  `structured=True` output and a leading `[budget exhausted after step N of M — partial answer]`
  line in the default text output (`nx_answer`'s default is `structured=False`, so a
  structured-only marker would be a silent-truncation shape for most callers). The same
  two-shape rule applies to the "no cost history — ran unestimated" warning. Fail loud, no
  silent truncation, on every path a caller can take.

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
- **Risk**: the current default `budget_usd=0.25` predates any measurement; 196-R2 measured a
  single default-model dispatch at $0.34–$1.84 on this box, so turning the cap into a hard
  pre-flight refusal at today's default would refuse most multi-step plans outright.
  **Mitigation**: Phase 3 Step 0 re-derives the default from Phase 1 history (a named
  percentile of observed per-plan cost, post-Phase-0/2) before enforcement is enabled; the
  old literal is never enforced as-is. Enforcement ships off by default until the derived
  value exists.
- **Risk**: scope creep toward the planner/refiner.
  **Mitigation**: out-of-scope list above; phase-review-gate at each boundary.

### Failure Modes

- Telemetry write fails → run still answers; `_warn_telemetry_drop` path already exists; visible in `nx doctor`.
- `--model` rejected by the CLI → dispatch error names the model and operator; policy entry revertible to default tier.
- Budget stop mid-run → partial result with explicit marker; never a silently truncated answer.

## Implementation Plan

### Prerequisites

- [x] All Critical Assumptions verified (196-research-1..4: envelope fields + `--model` composition + strict-MCP by spike; quality proxy by source search — absent, Phase 2 builds it)
- [ ] RDR-177 status checked: write through its surface if it has landed

### Minimum Viable Validation

One `nx_answer` call end-to-end on a multi-step plan produces a run row whose `cost_usd` equals
the sum of its step rows, each step row carrying the model and token counts the subprocess
reported, and `nx_answer(..., structured=True)` returns the per-step breakdown. Asserted by a
test against the engine substrate, not a mock.

### Phase 0: Strict Empty MCP Config for the Tool-Free Default

#### Step 1: `--strict-mcp-config` + empty `--mcp-config` in `claude_dispatch`'s base argv; test asserts the opt-in `mcp_servers` path still passes its own config

**Status: LANDED by nexus-h33x8.6, commit f1ae257d0 (2026-08-19, on develop; Sam's call to fold it
into the in-flight release). Argv now always carries `--strict-mcp-config`; tool-free default =>
zero servers, opt-in `mcp_servers` => only those.** RDR-196 does not re-implement it; Phase 1's
telemetry records the post-fix baseline.

> **CORRECTION (2026-08-20, plan audit fold — nexus-nyry9.6 / epic comment):** "LANDED" holds
> for `claude_dispatch` only. `src/nexus/aspect_extractor.py:1559` runs its OWN
> `["claude", "-p", "--output-format", "json"]` subprocess that never goes through
> `claude_dispatch`, so Gap 4 is NOT closed for `nx enrich aspects` (one of the four consumers
> this gap names): every aspect extraction still loads the ambient MCP set (196-R2's ~2x,
> $1.84 vs $0.91) and still uses the buffered `json` output mode whose partial-output capture
> h33x8.6 proved structurally vacuous (dispatch.py moved to stream-json at dca12e1e3; this site
> did not). Phase 1's `.p1a` envelope capture therefore cannot reach it either. Bead
> nexus-nyry9.6 owns the scope decision (Option A, recommended: re-route through
> `claude_dispatch`, fixing all three at once; blast radius `nx enrich aspects` +
> `daemon/aspect_worker_daemon.py`; Option B leaves two consequences open and requires
> follow-up beads, not prose).
>
> **OUTCOME (2026-08-20, nexus-nyry9.6):** Option B taken, not Option A. Reason: PR_SET_PDEATHSIG
> parity — `aspect_extractor.py`'s `Popen` call (~1572-1580) arms parent-death protection for the
> aspect-worker daemon (RDR-173 RF-8 / nexus-4r9ja); `claude_dispatch`'s
> `asyncio.create_subprocess_exec` call (dispatch.py:943-948) has no equivalent, and rerouting
> would silently regress that incident-driven safety guarantee (stacked-review-corrected
> reasoning — an earlier draft cited `HookRegistry.fire_document` as a sync-contract blocker;
> that was wrong, it only enqueues; the real sync callers are `aspect_worker.py:571` and
> `commands/enrich.py:1363`). `--strict-mcp-config` landed directly at `aspect_extractor.py:1559`
> (consequence 2, fixed in this bead). The stream-json/partial-capture consequence (3) is tracked
> as follow-up bead nexus-rhwbx. `_PER_PAPER_COST_USD` = 1.18, a single measured sample (n=1,
> default model `claude-fable-5`, 2026-08-20) — not Option A's live-metered figure.
#### Step 2: replace `_PER_PAPER_COST_USD` literal with a measured per-dispatch figure (or derive from Phase 1 history once available)

### Phase 1: Measure

#### Step 1: Usage capture in `claude_dispatch` (+ fixture-backed test of the result envelope)
#### Step 2: `StepRecord` collection in `plans/runner.py` incl. SQL fast-path and bundled steps
#### Step 3: Engine changeset `nx_answer_steps` + `steps` on the record endpoint; client write-through; `cost_usd` = Σ steps

Engine half: Liquibase changeset under `telemetry-*`, tenant-keyed + RLS like `nx_answer_runs`,
FK to the run row. Client half: sends `steps` only when the engine advertises the capability
(version probe), so a client ahead of its engine degrades to the run-row-only record with a
logged warning, never a 400. Ships as a PAIRED release per AGENTS.md § Engine-service release:
engine tag cut first, `REQUIRED_ENGINE_VERSION` bumped in the same client release, deploy at
client-tag push; `scripts/check_engine_release_floor.py` is the mechanical gate.
#### Step 4: Read surface (`structured` output + a `nx` read) and the MVV test

### Phase 2: Per-Operator Model Routing

#### Step 1: `model` parameter on `claude_dispatch`; tier table + resolver
#### Step 2: Quality proxy wired (RDR-090 / fixture set); baseline run all-strong, candidate run tiered; record both
#### Step 3: Flip default only if cost/latency improve at ≥ proxy-equal quality; otherwise record the negative result in the RDR and stop

##### OUTCOME (nexus-nyry9.17, 2026-08-21)

Per-operator decision, against the criteria pre-registered in T2
`nexus_rdr/196-phase2-ab-measurement-REGISTERED-2026-08-21` (frozen before
any .p2c measurement ran) and measured in T2 `nexus_rdr/
196-phase2-ab-measurement` (n=3/arm, same box/day):

| operator | verdict | n | cost ratio (cand/base) | agreement min/mean | threshold | caveats |
|---|---|---|---|---|---|---|
| `operator_filter` | **FLIP** | 3 | 0.068 (14.7× cheaper) | 1.000 / 1.000 | 0.80 | ceiling effect — scored 1.0 on every pair; external validity beyond the 8-item fixture untested |
| `operator_groupby` | **FLIP** | 3 | 0.061 (16.3× cheaper) | 1.000 / 1.000 | 0.80 | same ceiling-effect caveat as filter |
| `operator_extract` | **FLIP** | 3 | 0.060 (16.6× cheaper) | 1.000 / 1.000 | 0.85 | same ceiling-effect caveat |
| `operator_rank` | **FLIP** | 3 | 0.049 (20.4× cheaper) | 0.952 / 0.960 | 0.60 | min score sits BELOW .p2a's own strong-vs-strong noise floor (0.9762) — cite the large threshold margin (0.9524 vs 0.60), not a noise-band comparison (round-2 correction) |
| `operator_check` | **HOLD** → **FLIP 2026-08-21** | 9 cross pairs | ~0.08x (three-arm) | 1.000 / 1.000 | 0.70 | HOLD at this bead ("no tiering delta — both strong-tier in every arm"); flipped by nexus-3mea3 (Sam decision) on the pre-registered three-arm study (T2 `nexus_rdr/196-model-tier-study`): every within- and cross-arm pair 1.000 across fable/sonnet/haiku. Caveat on record: that proxy saturates (ceiling effect — it cannot RANK the models, only fail to refute), and check's `{ok: bool}` gates plan branching; `.p4`'s escalate-on-low-confidence is the designed safety half and is now the natural follow-up rather than an alternative (see the Phase 4 note below) |
| `operator_verify` | **HOLD** → **FLIP 2026-08-21** | 9 cross pairs | ~0.07x (three-arm) | 0.980 / 0.941 | 0.70 | HOLD at this bead; flipped by nexus-3mea3 on the three-arm study (fable-vs-haiku min 0.941, haiku noise min 1.000 — the cleanest verify result in the arc). Caveat ON RECORD: verify remains UNDECIDABLE on the .p2a strong-vs-strong proxy (margin +0.033) and the study registration pre-declared verify verdicts non-binding — the flip is a recorded Sam decision on the three-arm data, individually revertible |
| `operator_aggregate` | **HOLD (by construction)** | — | — | — | — | no .p2a quality proxy exists (free-text output, LLM-as-judge rejected); pinned `"strong"` in `model_tiers.OPERATOR_MODEL_TIER` and mechanically guarded by `test_every_cheap_entry_has_a_registered_proxy_metric` — a future re-flip cannot silently skip adding proxy coverage first |
| `operator_summarize` | **HOLD (by construction)** | — | — | — | — | same as aggregate |
| `operator_compare` | **HOLD (by construction)** | — | — | — | — | same as aggregate |
| `operator_generate` | **HOLD (by construction)** | — | — | — | — | same as aggregate |
| inline planner | **UNMEASURED** | 0 (candidate) | — | — | — | .p2c's candidate-planner arm never ran — budget exhausted (~$19 of ~$20 cap) after the operator core + 3 baseline-planner runs; baseline-only mean cost $2.86/call (corrected). Left HOLD (safe side) pending a dedicated top-up (~3 cheap-tier planner dispatches, small marginal spend once separated from the operator core) — Sam's call whether/when to fund it, not spent here. |

**Zero negative results AT THIS BEAD** — every operator measured here either
cleared both pre-registered refutation criteria (filter/groupby/extract/rank)
or had no tiering delta to measure in the first place (check/verify, later
measured and flipped on the three-arm study, nexus-3mea3 2026-08-21). No
operator's default flip was attempted and rejected *here*.

> **Amended 2026-08-21 (nexus-rv9xp):** the arc as a whole DID produce negative
> results, and this paragraph must not be read as covering them. The synthesis
> study built the quality proxy the four HOLD-by-construction operators lacked
> (blind pairwise judging) and REFUTED the cheap arms for summarize, generate,
> and compare; haiku was refuted everywhere; the sole non-refuted cell was
> sonnet-on-aggregate at 3/6, which is no-signal at n=6 rather than a flip
> licence. Those four stay strong — now by measurement rather than by absence
> of a proxy.

**What flipped, mechanically**: `nexus.operators.model_tiers.FLIPPED_OPERATORS`
(filter/groupby/extract/rank, + check/verify since nexus-3mea3
2026-08-21) is now consulted UNCONDITIONALLY on the
plan-run dispatch path (`plans/runner.py::_default_dispatcher`, which is
what `nx_answer` / `plan_run` use to invoke operator steps) — no env var
required. The only other consult site is `mcp/core.py::_nx_answer_plan_miss`
(the inline planner), and that one stays strong unless
`NX_OPERATOR_MODEL_TIERING=1`. The operator MCP tools themselves
(`operator_filter` etc.) never consult the tier table: an agent calling
them DIRECTLY, outside a plan, still gets the strong tier unless it passes
`model=` — so the 14-20x savings measured here apply to plan-mediated
dispatch only, not to direct tool use. Every other operator gets no
`model` override by default, unchanged from pre-.p2d behaviour.

Evidence caveat carried from the .p2c round-2 critique (T2
`nexus/review-nexus-nyry9.16-round2`): of the four flipped operators only
extract's costs are cross-referenced to a persisted `nx_answer_runs` row by
run id; filter/groupby/rank costs come from the dispatch's own structured
envelope (real, same telemetry path) and are not independently traceable to
a persisted row. `scripts/bench/operator_proxy_ab.stitch_run_id` closes that
for future runs; the already-spent measurement was not re-run. `NX_OPERATOR_MODEL_TIERING=1` keeps its .p2c meaning: a
measurement override that consults the WHOLE tier table (including
"strong" entries) for future A/B re-verification.
`NX_OPERATOR_MODEL_TIERING=0` is a new kill switch — forces every
operator back to strong (pre-.p2d behaviour) without a code change, for
rollback. See `docs/cli-reference.md`'s "Per-operator model tiering"
section for the full env-var contract.

**Bundles stay strong, unconditionally.** `nexus.plans.bundle.dispatch_bundle`
/ `compose_bundle_prompt` never import `model_tiers` and never pass a
`model` kwarg to `claude_dispatch` — a bundle containing a flipped
operator (e.g. a fused filter→groupby chain) dispatches at the default
(strong) model, not cheap. This is unchanged from .p1b's note that
bundling bypasses the SQL fast path too: the .p2c/.p2d measurement was
scoped to ISOLATED single-step plans specifically to hold bundling
constant/absent, so there is no A/B evidence for a flipped operator's
quality *inside* a bundle — leaving bundles on strong is the safe,
evidence-consistent default until a future bead measures the bundled
case separately.

**verify/check verdict for Phase 4 (`nexus-nyry9.23`)**: the cheap tier
was never tested against either operator (no tiering delta existed to
measure — see the table above), so this bead cannot say the cheap tier
is "not good enough" for them; it can only say the question was never
asked at the model-tier level. Phase 4's own framing (cheap-first
escalate-on-low-confidence) is a DIFFERENT mechanism than static tier
routing, so `.p4` is **not closed as not-needed** by this bead — it
remains open, gated on its own precondition being independently
evaluated, not on this bead's HOLD verdict for check/verify.

> **Amended 2026-08-21 (nexus-3mea3):** the sentence above's premise
> ("the cheap tier was never tested against either operator") no longer
> holds — the three-arm study (T2 `nexus_rdr/196-model-tier-study`)
> tested both, and nexus-3mea3 flipped check/verify to cheap by default
> on that evidence (Sam decision; verify's .p2a-undecidable caveat on
> record). This INVERTS `.p4`'s role: cheap-first is now the default for
> check/verify, so escalate-on-low-confidence is the missing SAFETY half
> of the design rather than an alternative routing mechanism.
> nexus-nyry9.23 now depends on nexus-3mea3 and its evaluation should
> start from this state.

**RDR-196 h33x8/hmu02 bench caveat, restated**: the retrieval-bench
non-regression check (`tests/integration/test_rdr_196_p2c_retrieval_bench.py`)
proved `off == on` for search/query outputs against a real freshly-indexed
corpus (5/5 non-error queries, byte-identical both arms) — tiering does
not touch retrieval, which is structurally guaranteed and now empirically
confirmed. It did NOT prove retrieval QUALITY is good (NDCG@3=0.0 in
both arms, traced to a pre-existing `scripts/bench/paths.py` metadata-
extraction bug, unrelated to .p2c/.p2d). Read this OUTCOME's flip
decisions as validated against the OPERATOR quality proxy only, not as
a broader nx_answer end-to-end quality claim.

### Phase 3: Cost-Ranked Choice + Budget

> **Sequencing note (2026-08-21, .rg2 finding):** Phase 3 Step 1 (`.p3b`,
> nexus-nyry9.20, commit 078c15976) shipped BEFORE the Phase 2 boundary gate
> ran — 2 min before `.p2c`'s commit and 35 min before `.p2d`'s flip. That was
> deliberate: Sam's option (a) re-scope (predicted cost from step shape) decoupled
> `.p3b` from the recorded-history premise Phases 1/2 were to produce, and its
> price table keys on `(operator, canonical model)`, so the flip cannot pool
> tiers into its medians. Recorded here so the out-of-order landing is visible to
> `.rg3`, not laundered by the later gate pass.

#### Step 0: derive the default `budget_usd` from Phase 1 history (named percentile of observed per-plan cost); enforcement stays off until the derived value exists
#### Step 1: candidate set with history from `plan_match`; min-cost-within-band selection
#### Step 2: `budget_usd` pre-flight estimate + mid-run step-boundary stop with explicit marker

### Phase 4 (optional, gated): Escalate-on-Low-Confidence for verify/check

Only on Phase-2 evidence that the cheap tier is insufficient for these operators alone.

##### OUTCOME (nexus-nyry9.23, 2026-08-21): NOT NEEDED — closed without code

The gate above never opened, and two further conditions independently block
building the mechanism anyway. Recorded here rather than only on the bead,
because a reader arriving at this section otherwise sees an optional phase with
no disposition.

1. **The condition is unmet.** Phase 2, as extended by the three-arm study and
   the check/verify flip, shows the cheap tier IS good enough for both:
   `operator_check` scored 1.000/1.000 agreement across every within- and
   cross-arm pair against a 0.70 threshold, `operator_verify` 0.980 min /
   0.941 mean against the same threshold (see the Phase 2 OUTCOME table).
2. **The escalate trigger has no input.** `operator_check` returns
   `{ok, evidence[]}` and `operator_verify` returns
   `{verified, reason, citations[]}`. Neither carries a confidence signal, so
   "escalate when the cheap verdict is low-confidence" has nothing to read.
   Adding one to an operator's own output is an operator-semantics change —
   named in this RDR's own out-of-scope fence — and therefore needs its own
   design of record, not an implementation step inside this arc.
3. **The deciding measurement cannot discriminate.** Shipping required
   escalate-on-demand to beat all-strong on cost at ≥ equal agreement, with a
   non-improvement reverted rather than shipped. `check`'s proxy is saturated
   at 1.000: it can fail to refute, never rank, so it can show neither an
   improvement nor a regression. Shipping an unmeasurable mechanism is exactly
   what Phase 2 Step 3 refused to do for aggregate/summarize/compare/generate
   (HOLD by construction, mechanically guarded); the opposite call at the arc's
   last step would leave the arc internally inconsistent.

**The residual risk is carried, not dismissed.** `check`'s `ok` bool gates plan
branching, so a wrong verdict changes control flow rather than only output text
— a higher-consequence failure mode than the other five flipped operators.
Tracked as `nexus-8uk28`, which names the two real prerequisites in order: a
non-saturated check/verify quality proxy FIRST, then an ungrounded-verdict
predicate (escalate on a verdict arriving with zero evidence/citations) that
reads existing fields without crossing the operator-semantics fence. RDR-190's
`loop`/`collect` primitives were not consulted here because there is nothing to
build yet; that check moves to the follow-up.

### Day 2 Operations

| Resource | List | Info | Delete | Verify | Backup |
| --- | --- | --- | --- | --- | --- |
| `nx_answer_steps` rows | In scope (read surface) | In scope | Deferred (event log; TTL/retention follows `nx_answer_runs`) | In scope (MVV) | N/A (engine backup) |
| Model tier table | In scope (one module) | In scope | N/A | Tests | N/A |

### New Dependencies

None. (Cheaper model tiers are reached through the same `claude` CLI.)
