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

Note on pre-existing wiring: `nx_answer` (`src/nexus/mcp/core.py:2033`)
*already* passes `scope_preference=scope` into `plan_match`. It has
since RDR-080 consolidated the retrieval layer. The matcher just drops
the argument silently today. Phase 2 does not add new wiring between
`nx_answer` and `plan_match`; it makes the matcher actually use the
value that's already arriving.

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
- Specificity ranking (tie-break by fewer `scope_tags`
  entries) is a much weaker signal than the paper's
  demonstration-pair retrieval. The paper reasons about
  query-plan intent alignment semantically; Nexus's
  specificity is a structural preference for narrower scope
  declarations. The tie-break exists mostly to prevent
  surprising reshuffles at identical scores, not as an
  intent-fit mechanism.
- The paper's 90% threshold is against a different signal
  (LLM-assessed intent fit). Our `scope_fit_weight` picks
  stay at the cosine-confidence layer and don't need to reach
  that bar.

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

Inferred `scope_tags` are stored in normalized form: strip any
trailing `-<hexhash>` suffix from collection names at save time
(`rdr__arcaneum-2ad2825c` → `rdr__arcaneum`). This keeps tags
stable across repo relocations that would otherwise produce new
hash suffixes for the same logical corpus.

Traverse-only plans: `traverse` operates on tumblers, not a
corpus argument, and contributes nothing to the union. A plan
whose only retrieval step is `traverse` is inferred as agnostic
(empty `scope_tags`), which is the right default.

### 2. `plan_save`: capture scope at store time

Two paths:
- **Explicit**: caller passes `scope_tags` argument alongside
  `plan_json`. Authored plans with a specific target.
- **Inferred**: when `scope_tags` is omitted, infer from
  `plan_json` by visiting retrieval steps and collecting every
  `corpus` / `collection` value that isn't a `$var` placeholder,
  then normalizing (strip trailing `-<hexhash>` suffix).

The `plan_save` MCP tool signature (`src/nexus/mcp/core.py`) also
gains an optional `scope_tags: str = ""` parameter so callers can
override inference explicitly. Default-empty preserves the
inference path for existing callers. Round-trip test verifies an
explicit value flows through `plan_save` → `PlanLibrary.save_plan`
→ `plans.scope_tags` column.

**Interaction with RDR-084 grown plans:** the ad-hoc-save path in
`nx_answer` (grown plans under `scope="personal"`, TTL=30) infers
`scope_tags` from the actual corpus used in the run that produced
the plan. If `_nx_scope` was passed and filled in the corpus, that
becomes the plan's scope tag. If the same question is later asked
against a different corpus, the matcher will filter out the first
grown plan and the inline planner fires again, possibly growing a
second plan with different tags. Two grown plans with different
scopes is correct behaviour: they solve different shapes. TTL=30
handles churn for plans that don't earn repeated matches.

Authoring guide update (`docs/plan-authoring-guide.md`) documents
the expectation.

### 3. Matcher: scope-fit score + filter

After the T1 cosine + dimension post-filter steps, apply scope
matching:

- When `scope_preference` is passed (non-empty after normalization,
  see below):
  - Plans with `scope_tags == ""` (agnostic) stay in the pool with
    no boost and no penalty. Neutral baseline.
  - Plans whose `scope_tags` contains at least one tag that
    *prefix-matches* the normalized `scope_preference` stay in the
    pool and receive a boost. Multi-corpus plans pass through: any overlap with the caller's scope is enough to match.
    Rationale: bridging plans (e.g. a Luciferase→Delos
    cross-corpus plan) should match a narrow Luciferase query,
    because the plan covers that scope and more.
  - Plans whose `scope_tags` is non-empty AND no tag
    prefix-matches the caller's scope are *filtered out*. A plan
    declared exclusively for `code__*` does not run against an
    `rdr__*` question regardless of what else scores.
- When `scope_preference` is empty, scope_tags is ignored. Current
  behavior fully preserved on the no-scope path.

**Normalization rules for comparison** (deterministic contract so
implementation is unambiguous):

- **Stored `scope_tags`**: normalized at save time by stripping
  trailing `-<hexhash>` suffix (e.g. 8-char hex hash used for
  repo-derived collection names). `rdr__arcaneum-2ad2825c` stored
  as `rdr__arcaneum`.
- **Caller `scope_preference`**: normalized at match time by
  stripping trailing `*` or `-*` glob, then stripping trailing
  `-<hexhash>` the same way.
  - `rdr__arcaneum-*` → `rdr__arcaneum`
  - `rdr__arcaneum-2ad2825c` → `rdr__arcaneum`
  - `rdr__` → `rdr__` (bare family prefix, matches any
    `rdr__*` tag)
- **Match**: string `startswith` on normalized forms. A tag
  `rdr__arcaneum` matches a caller scope `rdr__` (bare family)
  *and* a caller scope `rdr__arcaneum-*` (specific project).
- **Tumbler inputs** (e.g. `1.16`): deferred. Phase 2 accepts
  string scope only; tumbler-to-collection resolution is
  follow-up work if the need materialises.

**Zero-candidate fallback**: when the scope filter removes every
candidate, the matcher returns no hits. `nx_answer` already
handles this path: it falls through to the inline planner
(`_nx_answer_plan_miss`), which consults `scope` via its existing
prompt hint. No new logic required in `plan_run`. This is the
same behaviour as "no library plan matched at all", which is
already tested and known-good.

Score formula (the `scope_fit_weight` is picked by inspection,
not statistics; see §Phase 2c):

```
final_score = base_confidence * (1 + scope_fit_weight * scope_fit)
where scope_fit ∈ {0.0 agnostic, 1.0 prefix-matching}
```

Scope-conflicting plans don't appear in this formula because the
filter removes them before scoring. Agnostic plans receive
neutral weight so they still compete with scope-matching plans on
base cosine confidence.

### 4. Specificity ranking tie-breaker

When two plans have the same final score after scope boost, prefer
the one with fewer entries in `scope_tags`. A plan tagged
`scope_tags="rdr__arcaneum"` beats one tagged
`scope_tags="rdr__arcaneum,rdr__delos,rdr__nexus"` for an Arcaneum
query on score ties.

**Honest note about signal strength:** ties are uncommon in
practice (cosine scores are near-continuous). The tie-break is
mostly defensive: it prevents surprising reshuffles when two
plans happen to land at identical scores after the boost. The
motivating failure case (generic plan at 0.82 vs specialized at
0.79) is actually resolved by the boost itself, not by the
tie-break.

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
- `plan_save` MCP tool + `PlanLibrary.save_plan` gain optional
  `scope_tags` param (~10 lines).
- Inference helper for the omit-scope path (~20 lines).
- Matcher gains a normalized-prefix filter + score adjustment
  (~40 lines).
- Qualitative spot-check fixtures (~5 pairs, in tests).
- ~12 new tests (library, matcher, end-to-end through nx_answer).

**Risk**:
- Mis-inference at backfill: plan's retrieval steps use a `$var`
  for corpus → inferred as empty. Handled: empty `scope_tags`
  means "agnostic" which preserves current behavior.
- Over-aggressive filtering: a plan tagged `code__*` correctly
  filtered out for an `rdr__*` question even if the question
  happens to be about code. Acceptable: a question that's about
  code should pass `scope=code__*`, not rely on accidental
  matches. Zero-candidate fallback keeps the plan-miss path
  available.
- Weight calibration: wrong `scope_fit_weight` could regress
  no-scope behavior. Mitigated: scope_preference-empty path is a
  hard no-op; regression only possible when scope is passed,
  and the no-regression test suite gates that.
- Normalization edge cases: unexpected collection-name formats
  that don't match the `-<hexhash>` suffix pattern (e.g.
  manually-authored collection names without the suffix) stay
  as-is through normalization. Acceptable: identical strings on
  both sides still prefix-match correctly.

**Failure modes**:
- Plan authored with `scope_tags="rdr__arcaneum"` but deployed
  against a database where only `rdr__delos` exists → plan never
  matches. Surface via `nx doctor` check or an authoring-time
  warning. Not a regression; plan simply doesn't fire.
- Migration aborts mid-backfill. Idempotent: re-running picks up
  where it left off. `scope_tags DEFAULT ''` means unrun plans
  stay functional.
- All library plans filter out on a narrow scope → inline planner
  fires. Acceptable; same path as "no plan matched at all." User
  sees inline-planned response, may optionally `plan_save` the
  result to grow the library (RDR-084 discipline).
- Tumbler-form `scope_preference` (e.g. `1.16`) passed: today
  stripped of trailing glob but no tumbler-to-collection
  resolution applied. It won't prefix-match any normalized
  collection tag, so the filter eliminates all candidates →
  inline planner fires. Degrades to plan-miss behaviour, not
  broken. Proper tumbler support is follow-up work.

## Implementation Plan

### Prerequisites

- nexus-zs1d Phase 1 merged (done: PR #229).
- RDR-091 accepted.

### Phase 2a: schema + save

1. Migration: `ALTER TABLE plans ADD COLUMN scope_tags TEXT NOT NULL DEFAULT ''`
   via `src/nexus/db/migrations.py`. Version bump recorded in T2
   schema tracker.
2. `PlanLibrary.save_plan` accepts optional `scope_tags` arg.
3. `plan_save` MCP tool (`src/nexus/mcp/core.py`) gains
   `scope_tags: str = ""` parameter, passes through to the
   library. Default-empty preserves inference path for existing
   callers.
4. Inference helper: `_infer_scope_tags(plan_json) -> str`. Walks
   retrieval steps, unions `corpus` + `collection` values,
   strips `$var` placeholders, strips trailing `-<hexhash>`
   suffix from each tag.
5. Normalization helper: `_normalize_scope_string(scope) -> str`.
   Strips trailing glob (`*`, `-*`), strips trailing `-<hexhash>`
   suffix. Shared between plan_save inference and plan_match
   normalization.
6. Backfill migration: iterate existing rows, call inference,
   update. Idempotent.
7. Tests (`test_plan_library.py`): explicit tagging round-trips
   through `plan_save`; inferred tagging on various plan shapes
   including multi-corpus, traverse-only, `$var` corpora; empty
   plan edge case; normalization (hash-suffix strip,
   glob-trailing-strip, bare-family pass-through); migration
   idempotence.

### Phase 2b: matcher re-ranking

8. `plan_match` computes `scope_fit` per candidate when
   `scope_preference` is non-empty (after normalization).
9. Scope-conflicting candidates filter out (`scope_tags` non-empty
   and no tag prefix-matches normalized scope). Zero-candidate
   outcome: return no hits; `nx_answer` already falls through to
   the inline planner.
10. Score adjustment applied to matching candidates:
    `base_confidence * (1 + scope_fit_weight * scope_fit)`.
11. Specificity tie-breaker: when two plans score equal, prefer
    the one with fewer comma-separated entries in `scope_tags`.
12. Remove or rewrite the "unused at this version" docstring
    comment in `src/nexus/plans/matcher.py:77-80`. Update the
    matcher docstring to describe the live behaviour.
13. Tests (`test_plan_match.py`): scope-boost applies,
    scope-filter removes conflicting, agnostic plans pass through
    with neutral weight, no-scope no-op, specificity tie-break,
    zero-candidate fallback.

### Phase 2c: qualitative validation (not statistical calibration)

14. Author ~5 (scope, expected-plan) fixture pairs as part of
    Phase 2b test authoring. Each pair: a realistic question with
    a scope hint and the specific plan that should match.
15. Pick `scope_fit_weight` by inspection: a value that makes
    the 5 fixture cases return the expected plan without
    regressing the no-scope test suite. The existing `min_confidence=0.40`
    (RDR-079 P5) is not touched; only the new `scope_fit_weight`
    is tuned.
16. Hard requirement: all 190 existing plan-subsystem tests still
    pass with scope_preference empty (regression gate).
17. Record the picked weight and rationale in an
    `implementation_notes:` RDR update on close.

**Note:** a quantitative lift measurement (e.g. NDCG@k vs a
labelled scope-query corpus) is deferred to a future benchmark
RDR. We don't have a labelled corpus today; authoring one is its
own scope of work, likely a descendant of RDR-090.

### Phase 2d: docs

18. Update `docs/plan-authoring-guide.md` with `scope_tags` field
    description, inference rules, normalization contract.
19. Update `docs/plan-centric-retrieval.md` to reflect the
    scope-aware matcher and the zero-candidate fallback.

### Day-2 operations

- Re-backfill script for plan_json edits that change scope.
- `nx plan inspect <id>` shows inferred vs explicit scope_tags.

## Test Plan

**Unit tests**:
- `_infer_scope_tags`: single-corpus string, specific collection,
  mixed corpus+collection, `$var` placeholder skipped, no
  retrieval steps (agnostic), multi-step plan (union), `traverse`-
  only plan (agnostic), hash-suffix normalized at save time.
- `_normalize_scope_string`: trailing glob stripped, hash suffix
  stripped, bare family prefix preserved, empty input preserved,
  tumbler-form input degrades to plain-string no-match.
- Migration: empty table, populated table, idempotent re-run.
- Matcher: scope boost applied when prefix matches, scope filter
  removes conflicting plans (non-empty tags, no prefix match),
  agnostic plans pass through with neutral weight, no-scope
  no-op, specificity tie-break, zero-candidate fallback returns
  no hits.
- `plan_save` (library API and MCP tool): explicit `scope_tags`
  round-trips through to the library; inferred path fires when
  omitted.
- Multi-corpus plan: passes when caller's scope prefix-matches
  any one tag (intersect semantics).

**Integration tests**:
- End-to-end via `test_nx_answer.py`: `nx_answer` with scope
  returns scope-matching plan, not scope-conflicting one.
- `nx_answer` against `rdr__arcaneum-2ad2825c` (the original
  nexus-zs1d probe): confirm a saved `arcaneum-tradeoffs`
  specialized plan wins over a generic decision-retrieval plan
  when scope is passed.

## Validation

- Regression: all 190 existing plan-subsystem tests still pass
  (same gate as Phase 1). Hard requirement.
- New tests: ~12 new tests covering the behaviours above.
- Qualitative spot-check (Phase 2c): 5 (scope, expected-plan)
  fixture pairs return the expected plan with the picked
  `scope_fit_weight`.
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
- 2026-04-22: gate feedback addressed. Critical: score formula's
  `-∞` conflicting-plan case removed (filtering handles it before
  scoring; zero-candidate outcome falls through to the inline
  planner). Significant: multi-corpus filter semantics specified
  as prefix-overlap intersect (plan matches if any `scope_tags`
  prefix-matches caller scope, so bridging plans aren't filtered
  out); Phase 2c reframed as qualitative spot-check + no-regression
  (quantitative lift deferred to future benchmark RDR); `plan_save`
  MCP tool signature update added to Phase 2a; normalization
  contract specified (hash-suffix strip, glob strip, bare-family
  prefix, tumbler deferred). Observations: matcher comment
  cleanup added to Phase 2b; RDR-084 grown-plan scope_tags
  interaction documented; RDR-080 pre-existing scope_preference
  wiring noted in Problem Statement; RF-6 analogy softened.
