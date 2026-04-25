---
title: "RDR-093: GroupBy and Aggregate Operators"
id: RDR-093
type: Feature
status: closed
priority: medium
author: Hal Hildebrand
reviewed-by: self
created: 2026-04-24
accepted_date: 2026-04-24
closed_date: 2026-04-25
close_reason: implemented
related_issues: []
related_tests: [test_operator_dispatch.py, test_plan_run.py, test_plan_bundle.py]
related: [RDR-042, RDR-078, RDR-079, RDR-088, RDR-089]
---

# RDR-093: GroupBy and Aggregate Operators

Closes the last two AgenticScholar paper §D.4 operators that were
explicitly deferred from RDR-088 ("no concrete compositional query
class has surfaced needing this"). Reopened now because the target
use case — general scholarly research over `knowledge__*` corpora
— is load-bearing for the direction of the system, and waiting for
a single blocking query to crystallize is the wrong posture when
the foundation is inexpensive and the downstream consumers are
known (MatrixConstruct, research synthesis, cross-paper analytics).

Pairs with RDR-089 (ingest-time aspect extraction) to form the
minimum viable §D.4 analytics quartet: `Filter → GroupBy →
Aggregate` over structured aspects. Filter alone narrows; GroupBy
partitions; Aggregate reduces. Together they let a plan ask "for
each experimental dataset across these papers, what baseline method
won on the reported metric" without a separate ad-hoc pipeline per
question.

Paper operator coverage after this RDR: **13/13 = 100%** of the
§D.2 and §D.4 operators (Check, Verify, Filter from RDR-088;
GroupBy and Aggregate from this RDR; Extract / Rank / Compare /
Summarize / Generate already shipped).

## Problem Statement

### Enumerated gaps to close

#### Gap 1: No partition-by operator exists

Paper §D.4 defines `GroupBy(items, key) → groups` where `items` is
a prior step's output and `key` is a natural-language or field-name
partition expression. Nexus has no operator that turns a flat list
of items into a keyed grouping. `operator_rank` orders items but
does not partition; `operator_extract` pulls fields but does not
group by them; `operator_filter` narrows a single pool but does
not split one.

**Impact**: any plan that needs per-subgroup analysis (per-dataset,
per-method-family, per-year, per-venue) has to synthesize the
grouping inside a free-text `operator_summarize` pass, losing the
ability to feed each group into a downstream operator
independently. Plans like "for each experimental dataset, which
method won on the reported metric?" cannot be expressed as a DAG
because there's no primitive that emits N distinct downstream
branches keyed by a partition.

#### Gap 2: No reducer operator exists

Paper §D.4 defines `Aggregate(groups, reducer) → summary` — one
summary per group, with `reducer` being a natural-language
reduction instruction. Nexus has `operator_summarize` which reduces
a single blob of content but not a keyed collection of
sub-collections.

**Impact**: even if GroupBy existed, there's no structured way to
reduce each group to a per-group record. Callers would fall back
to N `operator_summarize` calls, losing the group-key association
that makes the output composable with downstream operators.

#### Gap 3: MatrixConstruct (paper §5) remains unbuildable

The paper's flagship discovery pipeline
`FindNode → Traverse → MatrixConstruct → Generate` synthesizes a
(problem × method) matrix and identifies empty cells (unexplored
research directions). MatrixConstruct is
`GroupBy(problem) → Aggregate(methods attempted) → cross-tabulate`
— it cannot exist without GroupBy and Aggregate as primitives.
RDR-089 provides the data layer; this RDR provides the operators
that consume it.

## Context

### Background

RDR-088 (accepted 2026-04-24, closed 2026-04-24) shipped
`operator_filter`, `operator_check`, `operator_verify` as the three
missing §D.2 and §D.4 operators. GroupBy and Aggregate were
included in the paper-coverage analysis but explicitly deferred in
the scope subsection with the rationale that no live query in the
plan library or shakeout demanded them. The deferral note committed
to a follow-up RDR if demand surfaced rather than reopening
RDR-088.

Demand surfaced in the 2026-04-24 review of RDR-089 when the
decision was made to target general scholarly research and **build
the foundation forward** rather than gate on concrete query
demand. With RDR-089 landing (or even in parallel), the aspect
store becomes a pre-extracted corpus of structured per-paper
metadata. GroupBy / Aggregate become the operators that make that
store composable — without them, the aspect store is a query-time
lookup table, not an analytical substrate.

#### Bar for reversing a scope deferral

RDR-088's deferral note committed to a follow-up RDR *if demand
surfaces*. RDR-093 reopens that decision on a stronger posture
than "demand surfaced" — it reopens because a **known architectural
consumer lands in the same release cycle** (MatrixConstruct per
paper §5, enabled by RDR-089's aspect store). The distinction
matters because a looser standard — "reopen any deferral when
someone imagines a use" — would hollow out the deferral mechanism
that lets RDRs bound scope tightly.

The bar this RDR sets for future deferral reversals: the reopening
RDR must identify an architectural consumer (a paper-described
pipeline, a concrete user workflow, or a sibling RDR whose
acceptance is already decided) that is itself load-bearing in the
current planning horizon — not anticipated, hypothetical, or
"worth considering". When the bar is not met, the deferral holds
until it is.

### Technical Environment

- `src/nexus/mcp/core.py:1399–1745` — current operator_* implementations
  (extract, rank, compare, summarize, generate, filter, check,
  verify). This RDR adds `operator_groupby` and `operator_aggregate`
  following the same `claude_dispatch`-backed pattern.
- `src/nexus/plans/runner.py::_OPERATOR_TOOL_MAP`,
  `_INPUTS_TARGET`, ids-branch auto-hydration, list coercion — the
  hot spots for the nexus-yis0 class of wiring bug. RDR-088 audit
  carry-over surfaced these for each new operator; same rigor
  applies here.
- `src/nexus/plans/bundle.py::BUNDLEABLE_OPERATORS`,
  `_describe_step`, `_terminal_schema` — bundle path extension,
  also from the RDR-088 pattern.
- `src/nexus/operators/dispatch.py` — `claude_dispatch` substrate
  (no new infrastructure).

## Research Findings

### Investigation

Primary source: `knowledge__agentic-scholar` §D.4 defining the
operator algebra. The paper treats GroupBy and Aggregate as pure
LLM operators that take items + a key / reducer expression and
return structured output. No inline retrieval, no corpus access;
pure reshaping of prior-step output.

RDR-088 Spike A evidence: `operator_check` produced 95% fully-
stable verdicts, 99% micro-stable run agreement, 0% schema-
validation errors across 100 dispatches on `knowledge__delos`. The
same `claude_dispatch` substrate backs GroupBy / Aggregate; the
reliability budget for schema-conformant structured output is
inherited.

### Key Discoveries

- **Verified** (`src/nexus/operators/dispatch.py`): JSON-schema
  enforced output is reliable at scale (RDR-088 Spike A + 4.10.0
  operator-bundling rollout both exercise the substrate).
- **Verified** (RDR-088 Phase 2 bundle integration): the
  `BUNDLEABLE_OPERATORS` set + per-verb `_describe_step` and
  `_terminal_schema` branches successfully compose multi-operator
  plans into a single `claude -p` dispatch. Same path extends to
  groupby / aggregate.
- **Cardinality scope**: `claude -p` handles O(100) items in a
  prompt cleanly. Beyond that, prompt size degrades output
  quality. Documented as a known limit; `operator_rank` in RDR-078
  paired with an implicit pre-winnow (`_OPERATOR_MAX_INPUTS = 100`)
  uses the same cap. This RDR inherits the cap — callers with
  larger inputs pre-rank or pre-filter.

### Critical Assumptions

- [ ] `claude_dispatch` produces stable group assignments across
      reruns (the partition does not shuffle items between groups
      on identical input) — **Status**: Partially verified via
      RDR-088 Spike A boolean-verdict stability; group-assignment
      stability is a stronger contract but same substrate. **Method**:
      re-run Spike A pattern with a 20-item partition fixture, measure
      fraction of runs where the group assignment set (as a set-of-sets)
      is identical to the modal run.
- [ ] Aggregate's per-group reduction does not leak cross-group
      content (summaries of group A reference only items in group A)
      — **Status**: Unverified — **Method**: spike with adversarial
      inputs where content overlap exists.

## Proposed Solution

### Approach

Two new `@mcp.tool()` functions in `src/nexus/mcp/core.py`
following the exact `operator_filter` / `operator_check` pattern
from RDR-088. Both route through `claude_dispatch`. Plan-runner
hydration and bundle integration extended in the same shape as
the RDR-088 audit carry-over prescribed for filter / check /
verify (four edits in `runner.py`, four branches in `bundle.py`).

### Technical Design

**`operator_groupby`** — signature:

`groupby(items: str, key: str, timeout: float = 300.0) → {groups: list[{key_value, items: list[dict]}]}`

`items` is a JSON array string of prior-step outputs (each item
expected to carry an `id` field for round-trip composability).
`key` is a natural-language partition expression — it can name a
structured field ("experimental_dataset"), an inferred property
("method family"), or a derived attribute ("publication year
bucket"). Output schema binds `key_value` (the group's label) to
the actual items that belong to the group. **Items are carried
inline, not as ID references.** Each emitted item preserves the
input's `id` field for round-trip composability, so callers that
only need the ID structure can derive it via `[i["id"] for g in
groups for i in g["items"]]`. Carrying items inline is load-
bearing for the bundled `groupby → aggregate` path — the bundle
dispatch is a single `claude -p` call with no host-side retrieval,
so aggregate must receive resolvable content inside the bundle
prompt (see Gate finding C-1, resolved in this design). No item
is assigned to more than one group; items the LLM cannot
confidently assign land in a `key_value="unassigned"` group.

```python
schema: dict = {
    "type": "object",
    "required": ["groups"],
    "properties": {
        "groups": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["key_value", "items"],
                "properties": {
                    "key_value": {"type": "string"},
                    "items": {
                        "type": "array",
                        "items": {"type": "object"},
                    },
                },
            },
        },
    },
}
```

**`operator_aggregate`** — signature:

`aggregate(groups: str, reducer: str, timeout: float = 300.0) → {aggregates: list[{key_value, summary}]}`

`groups` is a JSON-serialized `list[{key_value, items: list[dict]}]`
from a prior groupby step. Items arrive pre-hydrated in the groups
payload (carried inline by `operator_groupby`'s output contract),
so both the bundled and isolated dispatch paths see the same shape
and no runner-side nested-id hydration is required. `reducer` is a
natural-language reduction instruction ("winning baseline by
reported metric", "most cited method", "earliest publication").
Output preserves the `key_value` label so downstream operators can
round-trip the grouping.

```python
schema: dict = {
    "type": "object",
    "required": ["aggregates"],
    "properties": {
        "aggregates": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["key_value", "summary"],
                "properties": {
                    "key_value": {"type": "string"},
                    "summary": {"type": "string"},
                },
            },
        },
    },
}
```

**Plan-runner hydration** (`src/nexus/plans/runner.py`):

- `_OPERATOR_TOOL_MAP`: add `"groupby": "operator_groupby"` and
  `"aggregate": "operator_aggregate"`.
- `_INPUTS_TARGET`: add `"operator_groupby": "items"`. **Deliberately
  omit** `operator_aggregate` — its positional arg is `groups`, not
  `items`, and a stray `inputs:` on an aggregate step should surface
  as an authoring bug rather than be silently renamed. Same pattern
  RDR-088 used for `operator_verify`.
- ids-branch auto-hydration: `operator_groupby` joins the
  `args.setdefault("items", json.dumps(non_empty))` pool.
  `operator_aggregate` does not — its ids-branch shape is different
  (ids-per-group, not a flat list) and the bundle path handles the
  group hydration via `_describe_step`. Isolated dispatch of
  aggregate expects `groups` already materialised.
- List coercion: extend the trailing `json.dumps` block to include
  `operator_groupby`.

**Bundle integration** (`src/nexus/plans/bundle.py`):

- `BUNDLEABLE_OPERATORS`: add `"groupby"`, `"aggregate"`,
  `"operator_groupby"`, `"operator_aggregate"` (4 new entries, same
  bare-and-resolved pattern).
- `_describe_step`: two new `elif verb == ...` branches rendering
  the key / reducer expression + prior-step chaining + terminal
  output-shape guidance.
- `_terminal_schema`: two new branches matching the standalone
  schemas above.

**Composition semantics**:

| Plan step chain | Interpretation |
|---|---|
| `search → filter → groupby → aggregate` | Retrieve, narrow, partition, reduce each partition — canonical paper §D.4 pipeline |
| `traverse → groupby → aggregate` | Walk link graph, partition hits, reduce |
| `... → extract → groupby → aggregate` | Extract fields on-the-fly, partition by extracted field, reduce |
| `... → groupby → rank` | Partition then rank the groups themselves (groupwise comparative ranking) |

The RDR-089 aspect store does not require new operator wiring —
any step that reads from T2 aspects emits items in the standard
shape, and groupby accepts them directly.

### Existing Infrastructure Audit

| Proposed component | Existing module | Decision |
| --- | --- | --- |
| `operator_groupby`, `operator_aggregate` | `operator_extract` et al. | **Same pattern**: `@mcp.tool()` + `claude_dispatch` + fixed schema. No new substrate. |
| Plan-runner hydration | `_hydrate_operator_args` (runner.py) | **Extend**: four edits per operator per the RDR-088 audit carry-over template. |
| Bundle composition | `BUNDLEABLE_OPERATORS`, `_describe_step`, `_terminal_schema` | **Extend**: four entries + two verb branches + two schema branches. |

### Decision Rationale

The operators ship together because they are a pair — Aggregate
without GroupBy has no input shape; GroupBy without Aggregate
produces a structure that only `operator_summarize` can consume
(and badly, because it discards the group keys). Separating them
into two RDRs would create a partial-landing trap where one of
the two never gets demanded on its own.

Both operators are pure LLM reshaping of prior-step output — no
inline retrieval, no corpus access, no new persistence. They
slot into the operator family without a single new module;
everything happens in the existing `core.py` + `runner.py` +
`bundle.py` axis.

## Alternatives Considered

### Alternative 1: Ship GroupBy only, defer Aggregate

**Description**: Land partition semantics; leave per-group reduction
to ad-hoc `operator_summarize` calls.

**Pros**: Smaller surface; one operator instead of two.

**Cons**: Aggregate is the step that preserves the group key
association. Without it, downstream plan steps lose the per-group
label and can't round-trip through another operator. Users fall
back to N ad-hoc summarize calls with a separate shell to re-associate
output with keys — the exact ergonomic loss RDR-088 cited as
justification for each of the paper's structured operators.

**Reason for rejection**: half of a paired primitive delivers less
than half the value.

### Alternative 2: SQL-backed GroupBy over the RDR-089 aspect store

**Description**: Implement GroupBy as a SQL query against
`document_aspects` instead of an LLM call.

**Pros**: Cheap; deterministic; scales to N > 100.

**Cons**: Constrains the partition key to fields the aspect store
has already extracted. Natural-language keys like "method family"
or "year bucket" require secondary extraction. Ties the operator
to one specific data layer (RDR-089) and bifurcates the user model
(SQL groupby on aspects; LLM groupby on arbitrary content).

**Reason for rejection**: the paper's GroupBy is specifically the
natural-language-key variant that makes ad-hoc partitioning cheap
to express. A SQL-aspect-store fast path can be added later as an
optional optimisation under the same operator name when the aspect
field happens to match the key verbatim; the LLM path stays the
default contract.

### Briefly rejected

- **Fuse GroupBy + Aggregate into one `operator_partition_reduce` macro**: obscures the pair's composability; prevents GroupBy-then-Rank patterns.
- **Emit groups as a `list[list[dict]]` (nested list) instead of a `list[{key_value, item_ids}]` record**: loses the explicit key label and breaks downstream `$stepN.key_value` reference composition.

## Trade-offs

### Consequences

- Operator family grows 8 → 10. Composable plan-step tool count
  12 → 14. Paper coverage 11/13 → 13/13 (100%).
- Plan authors gain per-group analytical composition; MatrixConstruct
  becomes expressible (paired with `operator_extract` or RDR-089
  aspects for field-typed dimensions).
- Latency: GroupBy adds one `claude -p` call per step (~3-10s);
  Aggregate adds one per step. In a bundled `filter → groupby →
  aggregate` chain, all three collapse into one dispatch per the
  4.10.0 bundling path.
- No persistent storage, no migrations, no new dependencies.

### Risks and Mitigations

- **Risk**: Group assignment instability across re-runs (same input,
  different partitioning on different claude -p invocations) makes
  plans non-deterministic in a user-visible way.
  **Mitigation**: Spike with a 20-item partition fixture, measure
  modal-run agreement. RDR-088 Spike A gives us 95% fully-stable on
  boolean verdicts; expect partitioning to be at or above that
  because the partition task is more constrained (assign each item
  to exactly one key). Document the observed stability rate in the
  operator docstring so callers know what to expect.

- **Risk**: `operator_aggregate` leaks cross-group content into a
  group's summary when the items share vocabulary (summary of group A
  references items in group B).
  **Mitigation**: Prompt framing isolates each group explicitly
  ("For group X, summarising ONLY these items: ..."); test adversarial
  inputs with near-identical content across groups.

- **Risk**: Cardinality limit (O(100) items) surprises users who
  expect arbitrary-sized groupby.
  **Mitigation**: Same `_OPERATOR_MAX_INPUTS = 100` auto-hydration
  cap already applied by the runner to other operators. Document
  in operator docstrings; recommend `operator_rank` pre-winnow for
  larger inputs.

### Failure modes

- **Visible**: schema-validation failure → `OperatorOutputError`
  raised with snippet (delegated to `claude_dispatch` substrate).
- **Silent**: partition hallucination (LLM invents a plausible key
  value that's not grounded in the items). Mitigated by the
  `key_value="unassigned"` convention; low-confidence callers can
  inspect `unassigned` group size as a quality signal.
- **Silent (resolved in design)**: bundled `groupby → aggregate`
  content-resolution failure. The first-pass design carried only
  `item_ids` in groupby's output, which would have left aggregate
  with id references it cannot resolve inside a single bundled
  `claude -p` dispatch (no host-side retrieval in a bundle). Gate
  finding C-1 flagged this; the output contract now carries items
  inline so both bundled and isolated paths see the same resolvable
  payload. The bundle content-resolution test scenario is a
  regression guard against a future change that reverts to
  id-references.

## Implementation Plan

### Prerequisites

- [ ] Spike: 20-item partition fixture on `knowledge__delos`, 5
      runs each, measure modal-set-agreement stability. Target
      ≥95% fully-stable (modal set identical across all 5 runs).
- [ ] Spike: adversarial cross-group content test, 10 runs with
      near-identical items spread across 3 groups, inspect each
      group's summary for cross-group leakage.

### Minimum Viable Validation

A plan chaining `search → operator_filter → operator_groupby →
operator_aggregate` runs end-to-end via `plan_run`, returns a
structured aggregate list keyed by partition label, and each
operator's contract is exercised.

### Phase 1: `operator_groupby`

#### Step 1: Implement operator_groupby in core.py

Follow the `operator_filter` pattern at `core.py:1618`. Define
JSON schema, dispatch, surface typed errors.

#### Step 2: Integrate with plan_run

Extend `_OPERATOR_TOOL_MAP`, `_INPUTS_TARGET`, ids-branch,
list-coercion per the RDR-088 audit carry-over template. Bundle
integration: add to `BUNDLEABLE_OPERATORS`, implement
`_describe_step` and `_terminal_schema` branches.

### Phase 2: `operator_aggregate`

#### Step 1: Implement operator_aggregate in core.py

Schema + prompt + dispatch. Deliberately omit from `_INPUTS_TARGET`
(takes `groups`, not `items`).

#### Step 2: Integration test

End-to-end plan: `search → filter → groupby → aggregate` on
`knowledge__delos`. Shape assertions only (LLM content varies).

### Phase 3: MVV integration

Full four-step paper §D.4 pipeline end-to-end. Exercises every
new operator's contract via `plan_run`. Verifies bundling fuses
the operator run into one dispatch.

### Day 2 Operations

| Resource | List | Info | Delete | Verify | Backup |
| --- | --- | --- | --- | --- | --- |
| Operator registrations | `nx catalog search --type=operator` | N/A | N/A | integration test | code |

### New Dependencies

None. Same `claude_dispatch` substrate as the existing operator family.

## Test Plan

- **Scenario**: `operator_groupby` called with 10 items and the key "publication year" — **Verify**: output partitions items into year buckets, every input item's `id` lands in exactly one group's `items` array, no item id invented, items carry their original content (not just id).
- **Scenario**: `operator_groupby` with an ambiguous key (items lacking the field) — **Verify**: ambiguous items land in `key_value="unassigned"`, confidence signal surfaced via group size.
- **Scenario**: `operator_aggregate` called with 3 groups totalling 9 items and the reducer "most-cited method" — **Verify**: output has exactly 3 aggregates, each `key_value` preserved, each summary references only items in its group (adversarial cross-group test).
- **Scenario**: `plan_run` chain `search → filter → groupby → aggregate` bundled into one dispatch — **Verify**: terminal step output is the aggregate list; intermediate steps are `BUNDLED_INTERMEDIATE` sentinels; no per-step claude -p subprocess spawned for the three operators.
- **Scenario** (bundle content-resolution, Gate finding C-1 regression guard): `plan_run` chain `search → groupby → aggregate` bundled end-to-end, with a fixture whose aggregate summary must reference content that only exists in each item's body (not in the id). **Verify**: aggregate summaries include content-specific terms from each group's items — proves groupby's inline-items contract is plumbed correctly through the bundle prompt, not a reference-only shape that would leave aggregate guessing.
- **Scenario**: Cardinality cap (150 items passed) — **Verify**: `_OPERATOR_MAX_INPUTS` auto-hydration cap kicks in, groupby receives 100 items, runner logs the truncation, **and the tool's return envelope carries a `truncated: true, original_count: 150, kept_count: 100` metadata block** so plan authors see the limit hit rather than silently losing 50 items. (The metadata-surface enhancement is scoped to `operator_groupby` in this RDR; a follow-up should generalise the pattern across the operator family.)

## Validation

### Testing Strategy

Unit tests per operator with mocked `claude_dispatch`
(`test_operator_dispatch.py`) covering schema shape, prompt
content, and output contract. Hydration tests
(`test_plan_run.py::TestHydrateInputsTranslation`) cover the
inputs-rename + ids-branch + list-coercion paths for each new
operator. Bundle-composition tests (`test_plan_bundle.py`) cover
`_describe_step` and `_terminal_schema` for both verbs. Live-I/O
integration test (`tests/integration/test_rdr_088_operator_pipelines.py`
extension or new file) runs the four-step paper §D.4 pipeline
against `knowledge__delos` with shape-based assertions.

## Finalization Gate

Gated 2026-04-24 via `/nx:rdr-gate 093`. First pass surfaced one
critical issue plus two significant issues — all resolvable in
design, no code changes required pre-accept. Fixed in-place
before the re-gate:

1. **Critical (C-1) — bundle content-resolution gap.** Original
   `operator_groupby` output was `{groups: [{key_value, item_ids}]}`.
   Inside a bundled `groupby → aggregate` dispatch, aggregate
   receives groups from groupby's output contract; with
   id-references only, aggregate cannot resolve ids to content
   because a single `claude -p` dispatch has no host-side retrieval
   (store_get_many is not callable inside the LLM's reasoning).
   Fix: change groupby's output to
   `{groups: [{key_value, items: list[dict]}]}` with items carried
   inline. Bundle path now works (items visible in the LLM's
   context alongside groupby's output). Isolated path now works
   (aggregate receives pre-hydrated groups directly). Added a
   regression-guard test scenario and a Failure Modes entry
   documenting the historical shape so future reverts trip the
   test.
2. **Significant — silent cardinality truncation.** The
   `_OPERATOR_MAX_INPUTS = 100` cap was mentioned in Consequences
   but the cap's firing was invisible to callers (only a structlog
   warning). Fix: `operator_groupby`'s return envelope carries a
   `truncated: true, original_count: N, kept_count: 100` metadata
   block when the cap fires. Scoped to this operator in this RDR;
   a follow-up RDR should generalise the pattern across the full
   operator family.
3. **Significant — test gap for bundle wiring.** The first-pass
   Test Plan verified `BUNDLED_INTERMEDIATE` sentinels and process
   counts but did not exercise the content-reference path that the
   C-1 fix protects. Fix: added an end-to-end bundle content-
   resolution scenario whose fixture's aggregate summaries must
   reference item-body content (not just ids) to pass.

Observations addressed (recorded, not code-change):

- **Deferral-reversal precedent.** The RDR's reopening of
  GroupBy/Aggregate could set a loose bar for future reversals.
  Context section already documents the bar ("known architectural
  consumer in the same release cycle — MatrixConstruct, RDR-089")
  so the reversal is principled rather than ad-hoc.
- **Paper parity deviations.** All five deviations (short param
  names, string summary, explicit schema, `key_value` naming,
  `unassigned` convention) verified intentional and defensible in
  `nexus_rdr/093-research-1: paper-signature-parity-check`.

Re-gate result: PASSED. No critical issues outstanding; all
significant issues resolved; observations converted to either RDR
content (deferral-reversal bar, paper-parity reference) or
follow-up scope (cross-operator truncation metadata).

## References

- Paper: `knowledge__agentic-scholar` §D.4 (GroupBy, Aggregate)
- RDR-042 — original operator-model adoption
- RDR-078 — dimensional plan identity + `_OPERATOR_MAX_INPUTS` cap
- RDR-079 — operator dispatch substrate
- RDR-088 — Filter / Check / Verify (sibling operators; defer note is the trigger for this RDR)
- RDR-089 — ingest-time aspect extraction (natural data-layer partner; not a hard dependency)
- `src/nexus/mcp/core.py:1399–1745` — existing `operator_*` implementations
- `src/nexus/plans/runner.py::_hydrate_operator_args` — hydration hot spot
- `src/nexus/plans/bundle.py` — operator-bundle composition
- `src/nexus/operators/dispatch.py` — `claude_dispatch` substrate
