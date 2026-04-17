---
title: "RDR-084: Plan Library Growth — Auto-Save Successful Ad-Hoc Plans"
id: RDR-084
status: draft
type: Feature
priority: medium
author: Hal Hildebrand
reviewed-by: self
created: 2026-04-16
related_issues: []
related: [RDR-078, RDR-080]
---

# RDR-084: Plan Library Growth

The plan library seeded by `nx catalog setup` ships 14 plans (5 legacy
RDR-063 templates + 9 RDR-078 scenario templates). When `nx_answer`
falls through plan-match on miss, an inline `claude -p` planner
decomposes the question into a DAG and `plan_run` executes it. The DAG
ran, often produced a useful answer, and — **nothing saves it back**.
Every future paraphrase of that same question re-decomposes the same
DAG. The library stays at 14 plans forever unless a human hand-authors
a YAML template.

This RDR closes that gap. A successful ad-hoc plan (plan-miss path →
planner → `plan_run` without error) is automatically persisted via
`plan_save` with `scope:personal`, allowing the T1 cosine cache to
match it on the next paraphrase. The library compounds, and the
plan-match hit rate should climb without any human curation step.

## Problem Statement

### Enumerated gaps to close

#### Gap 1: Successful inline plans vanish at the end of `nx_answer`

In `src/nexus/mcp/core.py:_nx_answer_plan_miss`, the planner returns a
DAG, `plan_run` executes it, and the result is recorded in
`nx_answer_runs` (plan_id=0). The `plan_json` itself — the thing that
proved it could answer the question — is discarded after the function
returns. The FTS5 index never sees it; the T1 cosine cache never sees
it; the next paraphrase re-triggers the planner for identical work.

#### Gap 2: No metric distinguishes "same question re-decomposed" from "genuinely new question"

`nx_answer_runs` logs each run but the library has no growth signal.
Operators and the Nexus team cannot tell whether the inline planner
is serving a long tail of novel questions or re-solving the same three
questions 50 times. Without this signal there's no visibility into
whether the library is compounding or plateauing.

#### Gap 3: Hand-authoring is the only growth path, and it's expensive

Writing a `nx/plans/*.yml` template is a high-friction act: authors
must pick dimensions, required/optional bindings, tags. Operators who
hit plan-miss paths never see the diff between "this worked; do more
of it" and "this failed; do something else" — they have to reverse-engineer
the planner's output by reading logs.

## Context

### Background

RDR-078 shipped the `plans` table with dimensional identity and a
`personal`/`project`/`global` scope hierarchy. RDR-080 shipped
`nx_answer` with the plan-match → plan-run → record trunk. The
trunk's "record" step writes runtime metrics to `nx_answer_runs`
(duration, step_count, cost, matched_confidence, final_text), but
not the plan JSON itself.

The missing step is a `plan_save` call on the success path of the
plan-miss branch. Every other piece of infrastructure is in place:
- `PlanLibrary.save_plan()` accepts a canonical dimensions JSON.
- `plan_match` already falls through T1 cosine → FTS5, and both code
  paths match against `scope:personal` entries when the caller doesn't
  narrow by scope.
- Rate-limiting by canonical dimension is already enforced by the
  `UNIQUE (project, dimensions)` partial index — duplicate saves
  no-op.

### Technical Environment

- Python 3.12+, SQLite + FTS5 (T2), ChromaDB (T1 cosine via
  `plans__session` collection).
- `nx_answer` entry in `src/nexus/mcp/core.py`.
- Plan-miss branch at `_nx_answer_plan_miss()`.
- T1 plan cache singleton in `src/nexus/mcp_infra.py#get_t1_plan_cache`.

## Research Findings

### Investigation (to be completed during drafting)

- **Verify** — does `T1 plan cache.populate(lib)` re-load when new
  plans arrive, or is it one-shot? If one-shot, the new plan won't
  hit T1 cosine until a SessionStart event re-populates it.
- **Verify** — is `plan_save` thread-safe / re-entrant from inside
  `_nx_answer_plan_miss`? The planner lives on the same event loop
  as the MCP server; a blocking SQLite write should be fine but worth
  confirming under the existing SQLite WAL setup.
- **Assumption** — the inline planner's JSON, when stored and later
  re-matched, produces the same answer quality as the fresh planner
  run. Verification: pick 3 plan-miss questions, save their plans,
  re-query with paraphrased intents, compare output to the original.

### Critical Assumptions

- [ ] Saved ad-hoc plans match future paraphrases at a useful rate
  (T1 cosine threshold 0.40 or FTS5). **Verification**: MVV spike
  with 3 seeds + 3 paraphrases each.
- [ ] The inline planner's generated plans are stable enough to be
  reused. **Verification**: run the same plan twice; confirm identical
  step sequence.
- [ ] Scoping grown plans to `scope:personal` prevents pollution of
  the global library. **Verification**: confirm `plan_match` with
  `dimensions={verb: …, scope: personal}` can match them without the
  plan_match default filter dropping them.

## Proposed Solution

### Approach

One write on the success path. After `plan_run` returns without error
in `_nx_answer_plan_miss`, insert a synchronous `plan_save` call
with:

- `query = question` (the user's original intent).
- `plan_json = <the DAG the planner generated>`.
- `scope = "personal"` (caller's home; no pollution of global library).
- `outcome = "success"` (per RDR-078 plan-match filter — FTS5 matches
  `outcome=success` entries only).
- `project = <caller's repo basename>` (inherits from working-directory
  resolution).
- `ttl = 30` days default; configurable via `.nexus.yml#plans.ad_hoc_ttl`.
- `dimensions` derived from the planner prompt if possible (verb
  inference from question tokens) OR omitted, which is fine — the
  `(project, dimensions)` UNIQUE constraint only fires when dimensions
  is non-null.

### Technical Design

As-built (`src/nexus/mcp/core.py:2046-2088`). The tuple-return
pattern the draft originally proposed was discarded during
implementation: `_nx_answer_plan_miss` continues to return a single
`Match` and the caller reads `best.plan_json` inline. Simpler and
equivalent.

```text
# nx_answer main body, after plan_run success:
if best.plan_id == 0:
    ttl_days = _load_ad_hoc_ttl()   # .nexus.yml#plans.ad_hoc_ttl
    if ttl_days > 0:
        try:
            from pathlib import Path as _Path
            project_name = _Path.cwd().name
            with _t2_ctx() as _save_db:
                grown_id = _save_db.plans.save_plan(
                    query=question,
                    plan_json=best.plan_json,
                    outcome="success",
                    tags="ad-hoc,grown",
                    project=project_name,
                    ttl=ttl_days,
                    scope="personal",
                )
                try:
                    cache = get_t1_plan_cache()
                    if cache is not None:
                        row = _save_db.plans.get_plan(grown_id)
                        if row:
                            cache.upsert(row)
                except Exception:
                    _log.debug("plan_grow_cache_upsert_failed", exc_info=True)
            _log.info("plan_grow_saved", plan_id=grown_id, ttl_days=ttl_days,
                      project=project_name)
        except Exception as exc:
            _log.warning("plan_grow_save_failed", error=str(exc))
```

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
|---|---|---|
| Plan persistence | `T2Database.save_plan()` (`db/t2/__init__.py`) | **Reuse** — already accepts dimensional kwargs |
| Canonical dimensions | `nexus.plans.schema.canonical_dimensions_json` | **Reuse** |
| Scope enforcement | UNIQUE `(project, dimensions)` partial index | **Reuse** |
| Success-path hook point | `_nx_answer_plan_miss` return site | **Extend** — return the plan_json so the caller can save on success |
| T1 cosine re-population | `get_t1_plan_cache(populate_from=db.plans)` | **Verify** — check if adds propagate or only initial populate |

### Decision Rationale

- **Personal scope only** — grown plans are caller-specific by default.
  Promotion to `project` or `global` is a separate decision (the
  existing `/nx:plan-promote` skill + `plan-promote-propose` YAML
  handle this; RDR-083 / RDR-084 can layer on top).
- **TTL 30 days default** — ad-hoc plans are experimental; a stale
  plan is worse than no plan. 30 days is long enough to cover "used
  it a few times this month" but not so long that old bad plans clog
  the library indefinitely.
- **Save on success only** — failed `plan_run` returns aren't useful
  references. No "failure library" is needed at this scope.
- **Best-effort** — if `save_plan` fails (disk full, SQLite lock,
  whatever), the user still gets their answer. Growing the library is
  an optimization; the answer is the product.

## Alternatives Considered

### Alternative 1: Save every run, not just successes

**Description**: Persist both success and failure ad-hoc plans.

**Pros**: Full observability; future work could A/B on failure analysis.

**Cons**: FTS5 grows with noise; failed plans match paraphrases and
re-fail; library trust erodes.

**Reason for rejection**: The `outcome=success` filter in `plan_match`
already drops failures from FTS5 results. Saving them anyway would
just bloat the table.

### Alternative 2: Save to project scope (not personal)

**Description**: `scope="project"` so other agents in the same repo
share the grown plans.

**Pros**: Faster library compounding in a shared repo.

**Cons**: One agent's bad plan pollutes others' matches. No review
step before promotion.

**Reason for rejection**: Personal is the right default; promotion
(personal → project → global) is a deliberate act via
`plan-promote-propose`.

### Alternative 3: Prompt the user before saving

**Description**: Ask "save this plan for re-use?" on the success path.

**Pros**: No silent DB growth.

**Cons**: Breaks the one-shot `nx_answer` ergonomic; users will
decline to avoid the interrupt and the library will grow slower than
organic usage.

**Reason for rejection**: Silent opt-in with TTL 30 is the right
balance; explicit save-plan is already available for users who want it.

## Trade-offs

### Consequences

- Plan library compounds with real usage. First-week feel should be
  similar to today; by week 4, matched-plan hit rate should rise as
  common questions find their saved plans.
- `plans` table grows — bounded by TTL + UNIQUE constraint on
  dimensions. At 10-20 ad-hoc plans per user per week × 30-day TTL ≈
  40-80 plans at steady state per user.
- New config key `plans.ad_hoc_ttl` (default 30 days).
- New metric surface in `nx plan_search` output: "grown" tag.

### Risks and Mitigations

- **Risk**: Planner generates non-deterministic DAGs; the saved plan
  doesn't reproduce.
  **Mitigation**: Accept best-effort; the plan_match threshold is
  soft (0.40); if a saved plan returns garbage, the TTL clears it.
  Future RDR could add a "success-rate decay" signal.
- **Risk**: Personal scope bleeds into project scope via plan
  promotion.
  **Mitigation**: Promotion is a separate `plan-promote-propose`
  workflow; no automatic upgrade.
- **Risk**: T1 cosine cache doesn't see new saves until restart.
  **Mitigation**: RF verifies; if one-shot, add `cache.add(plan_id)`
  call on save.
- **Risk**: FTS5 noise from low-quality saved plans degrades match
  precision.
  **Mitigation**: TTL + `outcome=success` filter + manual review
  surface (`plan-inspect` skill).

### Failure Modes

- `save_plan` fails → logged at warning, user still gets answer.
- Saved plan fails on re-execution → standard `plan_run` error path;
  user sees error and can retry with different phrasing.
- Duplicate dimensions → UNIQUE constraint no-ops; logged once.

## Implementation Plan

### Prerequisites

- [x] RDR-078 + RDR-080 shipped.
- [x] `T2Database.save_plan()` accepts dimensional kwargs (fixed
  in audit commit `fc4f30e`).
- [ ] RF-1 through RF-3 verified (see Research Findings).

### Minimum Viable Validation

1. Run `nx_answer(question="some novel question")` against a sandbox
   plan library. Verify the new run appears in `plans` table with
   `tags=ad-hoc,grown`, `scope=personal`.
2. Run a paraphrase of the same question. Verify `plan_match` hits
   the saved plan (confidence or FTS5) rather than calling the
   inline planner again.
3. Kill and restart the sandbox. Verify the plan persists and matches
   after restart (durable T2 write).

**As-built (2026-04-16 live run)**:

All three steps passed end-to-end against a fresh isolated T2 via
`nx_answer`:

- **Step 1** — q1 = "What is the plan library growth architecture in
  Nexus?". Inline planner fired (no seed match), `plan_run` executed,
  grown plan persisted. Wall-clock 48.0s. T2 after: 1 active plan,
  `tags="ad-hoc,grown"`, `scope="personal"`.
- **Step 2** — q2 = "How does the plan library get new entries over
  time in Nexus?" (paraphrase of q1). Wall-clock **25.7s — ~2× faster
  than Step 1**. T2 after: still 1 active plan. The paraphrase matched
  the grown plan via `plan_match`; the inline planner did NOT re-fire
  (if it had, a second grown plan would be present).
- **Step 3** — T2 re-opened after the session; 1 plan still present,
  durability confirmed.

This empirically verifies both Critical Assumption 1 (saved plans
match paraphrases at a useful rate — 1-of-1 on this seed) and
Critical Assumption 2 (plans stable enough to be reused — the q2
execution completed without error on the q1-derived DAG). Step 1 + 3
are also automated in
`test_save_plan_get_plan_cache_upsert_round_trips`.

### Phase 1: Code Implementation

#### Step 1: Plumb `plan_json` through the plan-miss success path

- Modify `_nx_answer_plan_miss` to return `(Match, plan_json)`.
- Modify `nx_answer` to capture `plan_json` and call `save_plan` on
  success.
- Unit test: mock planner, assert `save_plan` called with expected
  kwargs.

#### Step 2: Config key

- Add `plans.ad_hoc_ttl` (default 30) to `.nexus.yml` schema.
- Load via `nexus.config.load_config`.

#### Step 3: T1 cache propagation

- RF-verify whether `get_t1_plan_cache(populate_from=lib)` picks up
  new plans organically.
- If not, add `cache.add(plan_row)` call inside the save path.

#### Step 4: Observability

- New `nx plans grown` subcommand: list ad-hoc grown plans with
  timestamps + match count.
- Optional: JSON `--format json` for scripting.

#### Step 5: Documentation + release notes

- Update `docs/plan-centric-retrieval.md` — growth section.
- Update `docs/configuration.md` — `plans.ad_hoc_ttl` key.
- Entry in `CHANGELOG.md`.

### Phase 2: Dogfood

- Index Nexus's own corpus, run `nx_answer` on 10 real research
  questions over two weeks, measure hit rate rise.

## Test Plan

- Unit: save-on-success call path, skip-on-failure call path.
- Unit: TTL expiry.
- Integration: end-to-end round-trip (ask → save → paraphrase → hit).
- Regression: `plan_match` still prefers higher-scope plans
  (`scope:global` > `scope:project` > `scope:personal`) when both
  match.

## Validation

### Testing Strategy

1. **Unit** — mock `plan_run`, assert `save_plan` invocation shape.
   Shipped in `tests/test_rdr_084_plan_grow.py::TestAdHocSaveOnSuccess` (4 tests),
   `::TestT1CachePropagation` (3 tests), `::TestAdHocTtlConfig` (5 tests).
2. **Integration (live SQLite)** — `tests/test_rdr_084_plan_grow.py::TestLiveSqliteRoundTrip`
   opens a real on-disk `T2Database` at `tmp_path/memory.db`, runs
   `nx_answer` end-to-end, and asserts both (a) the saved plan is
   independently re-queryable via a fresh `T2Database` after the
   command returns, and (b) the row passed to `cache.upsert` carries
   the same `id`/`query`/`plan_json` as the persisted row. Closes
   the mock-only coverage gap raised at gate. Also covers the
   `ad_hoc_ttl=0` opt-out path.
3. **Manual** — dogfood on real Nexus questions for a week, measure
   observed hit rate improvement.

### Performance Expectations

One additional SQLite write per successful `nx_answer` (micros).
Negligible vs the seconds-to-minutes of the underlying LLM work.

## References

- RDR-078 (plan library + dimensional identity)
- RDR-080 (nx_answer trunk + `claude_dispatch` substrate)
- `src/nexus/plans/runner.py` — `plan_run` entry point
- `src/nexus/mcp/core.py#_nx_answer_plan_miss` — the save insertion point

## Follow-up (out of scope)

- **Success-rate decay** — weight plan_match by recent success rate,
  retire under-performing grown plans faster than TTL.
- **Cross-session growth aggregation** — merge multiple users'
  personal plans into a project-scope seed.
- **Human-review gate on promotion** — personal → project requires
  review; already covered by `plan-promote-propose`.
