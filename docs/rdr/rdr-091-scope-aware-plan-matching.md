---
title: "RDR-091: Scope-Aware Plan Matching (nexus-zs1d Phase 2)"
id: RDR-091
type: Feature
status: draft
priority: medium
author: Hal Hildebrand
reviewed-by: self
created: 2026-04-22
accepted_date:
related_issues:
  - "nexus-zs1d: Wire nx_answer scope through plan_match + plan_run"
related_tests: [test_plan_match.py, test_plan_library.py, test_nx_answer.py]
related: [RDR-078, RDR-079, RDR-080, RDR-084]
implementation_notes: ""
---

# RDR-091: Scope-Aware Plan Matching (nexus-zs1d Phase 2)

## Problem Statement

`plan_match` today selects plans purely on question similarity. The
`scope_preference` parameter is accepted and documented but unused
(`src/nexus/plans/matcher.py:77-80`: *"accepted for forward
compatibility with Phase 2 scoping + specificity ranking (PQ-14 / PQ-20), unused at this version"*).

The nexus-zs1d Phase 1 runtime override (shipped in PR #229) propagates
caller scope *into* a matched plan's retrieval steps when the plan
doesn't pin a corpus. It does not help when the matcher picks the
*wrong plan entirely* because the matcher can't tell a specialized
plan from a generic template.

Concrete failure mode (observed while validating post 5's Arcaneum
worked-example): a generic 2-step decision-retrieval plan at score
0.82 outranks a specialized 5-step `arcaneum-tradeoffs-comparison`
plan at 0.79 for an Arcaneum question, even when the caller passes
`scope="rdr__arcaneum-*"`. Phase 1 puts the right corpus into the
wrong-shape plan and returns thin output. The specialized plan, which is exactly the kind of investment post 4's "library grows by capture" argument depends on, never runs.

Four downstream consequences:

1. **Plan library doesn't compound with use.** Saving specialized
   plans for specific corpora is mostly invisible to the matcher.
   Generic templates keep winning on surface similarity. `match_count`
   metrics become noisy: heavily used templates look productive
   even when the matched shape is consistently suboptimal.

2. **Corpus-incompatible plans still run.** A plan targeting
   `code__*` collections currently matches any semantically-similar
   question regardless of scope, and Phase 1 happily overrides its
   corpus. Code-shaped operator prompts get dispatched against RDR
   content. The wrong shape gets forced onto the wrong content type.

3. **Specificity has no signal.** When two plans both clear the
   confidence threshold, there's no way to prefer the narrower one
   even when it's a better match for the caller's scope.

4. **The plan authoring feedback loop is broken.** Without a reliable
   way for specialized plans to win matches, authoring one is an act
   of faith. Promotion discipline (RDR-084) depends on this working.

## Context

### Technical Environment

- **Plan library**: `src/nexus/db/t2/plan_library.py`, the SQLite table
  `plans` with columns including `plan_json`, `description`,
  `dimensions_json`, `match_count`, `match_conf_sum`, `tags`.
- **Matcher**: `src/nexus/plans/matcher.py`, the two-path match (T1
  cosine + FTS5 fallback), dimension post-filter, confidence
  threshold.
- **Plan save/execution**: `src/nexus/mcp/core.py::plan_save`
  stores plans; `plan_run` executes them; RDR-084 handles promotion
  discipline via `match_count` and success signals.
- **Retrieval step args**: plans reference corpora via
  `args["corpus"]` (comma-separated prefix) or `args["collection"]`
  (specific collection name). `_COLLECTION_ARG_KEYS` in
  `runner.py:260` enumerates the collection-carrying keys.

### Research Findings

**RF-1** (Verified, source search `src/nexus/plans/matcher.py:77-80`,
`src/nexus/plans/matcher.py:65` parameter signature):
The matcher accepts `scope_preference: str = ""` and docstring
acknowledges it's dropped. No caller today provides it except
Phase 1's plumbing, which uses it purely to flow into runtime.

**RF-2** (Verified, source search `src/nexus/db/t2/plan_library.py`):
The `plans` table has no corpus/scope metadata column. Plans are
schemaless with respect to what corpus they target; inferable from
the `plan_json` steps but not indexed for query.

**RF-3** (Verified, probe 2026-04-22 against
`rdr__arcaneum-2ad2825c`): the matcher returns `plan_id=38`
(generic 2-step) for an Arcaneum-specific question. The plan's
retrieval step has no corpus pinned, so Phase 1's runtime override
would fill it, but the plan's DAG shape is still wrong (no extract
or compare steps for the requested analytical query).

**RF-4** (Verified, source search `src/nexus/plans/match.py`):
`Match` objects already carry `plan_json` and `dimensions`. Adding
a corpus-scope field is a natural extension of the existing match
contract; downstream consumers (`plan_run`, nx_answer) don't need
to change.

**RF-5** (Documented, paper §D.2): AgenticScholar's
"predefined-plan selection" benchmark (the +47% NDCG@3 result cited
in post 4) depends on plans being selected by shape-relevance to
the query, not by question-surface similarity alone. Nexus has the
scoring infrastructure but not the scope-fit input.

**RF-6** (Documented, paper §3 and §5 via targeted probe
2026-04-22 against `knowledge__agentic-scholar`): AgenticScholar's
plan selection is richer than a single-signal threshold. The
mechanism is three-stage:

1. Semantic search retrieves candidate `(query, plan)` *demonstration
   pairs* from an extensible library and injects them into the LLM
   context. Retrieval is over paired examples, not over plan
   descriptions alone.
2. An LLM reasons about analytical intent alignment between the
   incoming query and each candidate and assigns a confidence
   score.
3. A plan is selected only when the confidence score exceeds a
   high threshold (the paper uses `> 90%` as its example);
   otherwise the query is treated as ad-hoc and passed to the
   planner.

Implications for Nexus's Phase 2:

- Nexus's matcher today is cosine-over-plan-description at
  `min_confidence=0.40` (calibrated in RDR-079 P5). That maps to
  stage 1 of the paper's flow, without stages 2–3.
- The paper's stage 2 uses an LLM call per match to assess
  intent fit. Cost: one subprocess per match. Nexus could add
  this as an optional stage 3 (after scope-tag filtering)
  behind a feature flag, but the per-match cost argues for
  doing it only when the cheap-signal stages are ambiguous.
- Phase 2's `scope_tags` mechanism is a cheap *structural*
  pre-filter that sits between retrieval and any eventual
  intent-fit LLM stage. Structural match is free; semantic
  match is calibrated; intent-fit is LLM-expensive. A three-
  tier gate with cheap stages short-circuiting to expensive
  ones is the natural architecture.
- Specificity ranking (tie-break by narrower `scope_tags`) in
  the current RDR proposal is the Nexus analogue of the
  paper's "demonstration-pair" retrieval: both prefer the
  example most closely aligned with the query's shape, not
  just its surface.
- The paper's 90% threshold is against a different signal
  (LLM-assessed intent fit). Our `scope_fit_weight`
  calibration target stays at the cosine-confidence layer and
  doesn't need to reach that bar.

### Critical Assumptions

**A-1** (Assumed, low stakes): scope metadata can be inferred from
a plan's retrieval steps (union of `corpus` and `collection` values
across all steps). Invalid plans or dynamically-bound corpora are
rare enough that a best-effort inference is acceptable. Explicit
plan-author override remains available for cases the inference
misses.

**A-2** (Assumed, low stakes): scope tags can be stored as a
denormalized string column (comma-separated collection-name
prefixes) rather than a joined table. Plan library is small
(hundreds, not millions), query cost is negligible.

**A-3** (Assumed, medium stakes): the re-ranking weight between
question-similarity confidence and scope-fit score can be tuned to
preserve existing behavior when no scope is passed, while giving
meaningful boost when it is. Specific weights determined by test
against the existing match corpus (RDR-079 P5 precedent).

## Proposed Solution

Four coordinated changes:

### 1. Plan-library schema: add `scope_tags` column

Single denormalized string column on `plans` table, comma-separated
collection-name prefixes (e.g. `"rdr__arcaneum,knowledge__arcaneum"`).
Empty string means corpus-agnostic (generic template).

```sql
ALTER TABLE plans ADD COLUMN scope_tags TEXT NOT NULL DEFAULT '';
```

Backfill existing plans via migration: parse `plan_json`, union
`corpus` + `collection` values across retrieval steps, store the
result. Corpus-agnostic plans (no corpus hint anywhere) get empty
string.

Index on `scope_tags` is not needed: plan library is small enough
that a full scan per match is cheap (O(10s-100s) rows).

### 2. `plan_save`: capture scope at store time

Two paths:
- **Explicit**: caller passes `scope_tags` argument alongside
  `plan_json`. Authored plans with a specific target.
- **Inferred**: when `scope_tags` is omitted, infer from
  `plan_json` by visiting retrieval steps and collecting every
  `corpus` / `collection` value that isn't a `$var` placeholder.

Authoring guide update (`docs/plan-authoring-guide.md`) documents
the expectation.

### 3. Matcher: scope-fit score + filter

After the T1 cosine + dimension post-filter steps, apply scope
matching:

- When `scope_preference` is passed:
  - Plans with `scope_tags == ""` (agnostic) stay in the pool
    with a small neutral weight (no penalty, no boost).
  - Plans whose `scope_tags` includes the caller's scope prefix
    get a boost.
  - Plans whose `scope_tags` is non-empty AND excludes the
    caller's scope prefix get filtered out. A plan declared for
    `code__*` does not match an `rdr__*` question.
- When `scope_preference` is empty, scope_tags is ignored (current
  behavior preserved).

Score formula (to be calibrated):

```
final_score = base_confidence * (1 + scope_fit_weight * scope_fit)
where scope_fit ∈ {0.0 agnostic, 1.0 matching, -∞ conflicting}
```

Specific `scope_fit_weight` value determined by calibration against
existing match corpus (target: don't regress on no-scope queries,
boost specialized plans by ~10-20% for matching-scope queries).

### 4. Specificity ranking tie-breaker

When two plans have the same final score after scope boost, prefer
the one with the narrower `scope_tags` (fewer collection prefixes).
`arcaneum-tradeoffs-comparison` with `scope_tags="rdr__arcaneum"`
beats a plan tagged with `scope_tags="rdr__arcaneum,rdr__delos,rdr__nexus"`
for an Arcaneum query, all else equal.

## Alternatives Considered

### A: Inline scope tagging via `dimensions` field

Use the existing `dimensions_json` field to carry scope by
convention (e.g. `dimensions["corpus_scope"] = "rdr__arcaneum"`).
No schema change.

**Rejected.** `dimensions` is set-membership semantics (exact
equality post-filter, per `matcher._superset`). Scope matching
needs prefix-match and filtering, not equality. Layering a string
convention on a wrong-shape field would produce obscure bugs at
scale.

### B: Runtime inference only, no stored metadata

Re-parse `plan_json` on every match to derive scope at query time.
No schema change, no migration.

**Rejected.** Match path runs frequently; parsing every plan's JSON
on every match is unnecessary work. A stored denormalized column
amortizes this at plan-save time. Also prevents the inference logic
from drifting between author-time and match-time.

### C: Defer until the library is bigger

Wait until enough plans exist that specificity actually matters.
Ship Phase 1 and revisit.

**Rejected.** The feedback loop for plan authoring is already
broken (RF-4 identifies the post-4 argument depending on this).
Without specificity winning, authors can't observe their
specialized plans being preferred. Phase 2 unblocks the feedback
loop itself, not just a larger library.

## Trade-offs

**Cost**:
- Schema migration (one `ALTER TABLE ADD COLUMN` + backfill).
- `plan_save` gains an inference step (~20 lines).
- Matcher gains a filter + score adjustment (~30 lines).
- Calibration run to pick `scope_fit_weight`.
- ~10 new tests.

**Risk**:
- Mis-inference at backfill: plan's retrieval steps use a `$var`
  for corpus → inferred as empty. Handled: empty `scope_tags`
  means "agnostic" which preserves current behavior.
- Over-aggressive filtering: a plan tagged `code__*` correctly
  filtered out for an `rdr__*` question even if the question
  happens to be about code. Acceptable: a question that's about
  code should pass `scope=code__*`, not rely on accidental
  matches.
- Weight calibration: wrong `scope_fit_weight` could regress
  no-scope behavior. Mitigated: fall-back case (scope_preference
  empty) is a hard no-op; regression only possible when scope is
  passed.

**Failure modes**:
- Plan authored with `scope_tags="rdr__arcaneum"` but deployed
  against a database where only `rdr__delos` exists → plan never
  matches. Surface via `nx doctor` check or an authoring-time
  warning.
- Migration aborts mid-backfill. Idempotent: re-running picks up
  where it left off. `scope_tags DEFAULT ''` means unrun plans
  stay functional.

## Implementation Plan

### Prerequisites

- nexus-zs1d Phase 1 merged (done: PR #229).
- RDR-091 accepted.

### Phase 2a: schema + save

1. Migration: `ALTER TABLE plans ADD COLUMN scope_tags TEXT NOT NULL DEFAULT ''`
   via `src/nexus/db/migrations.py`. Version bump recorded in T2
   schema tracker.
2. `PlanLibrary.save_plan` accepts optional `scope_tags` arg.
3. Inference helper: `_infer_scope_tags(plan_json) -> str`. Walks
   retrieval steps, unions `corpus` + `collection` values,
   strips `$var` placeholders.
4. Backfill migration: iterate existing rows, call inference,
   update. Idempotent.
5. Tests: `test_plan_library.py` covers explicit tagging, inferred
   tagging, empty-plan edge case, migration idempotence.

### Phase 2b: matcher re-ranking

6. `plan_match` computes `scope_fit` per candidate when
   `scope_preference` is non-empty.
7. Scope-conflicting candidates filter out (`scope_tags` non-empty
   and prefix doesn't match).
8. Score adjustment applied to matching candidates.
9. Specificity tie-breaker.
10. Tests: `test_plan_match.py` covers scope-boost, scope-filter,
    agnostic pass-through, no-scope no-op, specificity tie-break.

### Phase 2c: calibration

11. Run calibration against existing match corpus (same pattern as
    RDR-079 P5).
12. Pick `scope_fit_weight` that preserves no-scope precision/recall
    and lifts specialized-plan recall by ≥10% for scope-passed
    queries.
13. Update RDR with calibrated value.

### Phase 2d: docs

14. Update `docs/plan-authoring-guide.md` with scope_tags field
    description and inference behavior.
15. Update `docs/plan-centric-retrieval.md` to reflect the
    scope-aware matcher.

### Day-2 operations

- Re-backfill script for plan_json edits that change scope.
- `nx plan inspect <id>` shows inferred vs explicit scope_tags.

## Test Plan

**Unit tests**:
- `_infer_scope_tags`: corpus string, collection string, mixed,
  `$var` placeholder skipped, no retrieval steps (agnostic), plan
  with multiple retrieval steps (union).
- Migration: empty table, populated table, idempotent re-run.
- Matcher: scope boost applied, scope filter applied, no-scope
  no-op, specificity tie-break.
- `plan_save` with explicit vs inferred scope_tags.
- End-to-end via `test_nx_answer.py`: nx_answer with scope returns
  scope-matching plan, not scope-conflicting one.

**Integration tests**:
- nx_answer against `rdr__arcaneum-2ad2825c` (the original
  nexus-zs1d probe): confirm a saved `arcaneum-tradeoffs`
  specialized plan wins over a generic decision-retrieval plan
  when scope is passed.

## Validation

- Regression: all 190 existing plan-subsystem tests still pass
  (same gate as Phase 1).
- New: 10-15 new tests covering the four behaviors above.
- Calibration target met (weight picks don't regress no-scope
  queries, boost specialized recall ≥10% on scope-passed queries).
- Probe: the Arcaneum worked-example from post 5 runs end-to-end
  with a matched specialized plan (not inline-planner miss path).

## Finalization Gate

- [ ] Structural: all sections filled.
- [ ] Assumption audit: A-3 weight-calibration assumption flagged
      for Phase 2c calibration run.
- [ ] AI critique pass via `/nx:rdr-gate`.

## References

- nexus-zs1d (the parent bead, both phases).
- PR #229 (Phase 1 implementation).
- `src/nexus/plans/matcher.py:77-80` (original TODO comment).
- RDR-079 P5 (calibration precedent).
- RDR-084 (plan-library growth discipline that Phase 2 enables).
- AgenticScholar paper §D.2 (predefined-plan selection as the
  source of the +47% NDCG@3 lift).

## Revision History

- 2026-04-22: drafted.
