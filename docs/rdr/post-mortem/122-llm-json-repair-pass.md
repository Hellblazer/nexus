# RDR-122 Post-Mortem: LLM-JSON Repair Pass

**Closed** 2026-09-16 (from draft, Sam's override) · **Created** 2026-05-19 · **Beads** none

## What the RDR set out to do

LLM output that failed JSON validation was re-prompted from scratch: a
trailing comma in a 4k-token plan cost 4k tokens and a round trip. The RDR
proposed porting a2ui's `PayloadFixer`, a mechanical repair pass (fence
strip, trailing commas, delimiter close, smart quotes, key misspellings)
run before validation, and wiring it into four parsers: plan save and
match, RDR frontmatter, operator outputs, catalog payloads.

## Implementation status

Not implemented as designed. The problem it priced went away by another
route.

## Implementation vs plan

### Solved by other means

- The operator leg, the RDR's main cost driver, now runs under
  schema-constrained output: `src/nexus/operators/dispatch.py` passes
  `--json-schema` to the dispatch. The RDR's Alternative 3 rejected exactly
  this as "not available for the Claude API surface nexus uses". It became
  available and was adopted without anyone returning to this document.
- Fence stripping exists as a one-parser helper, `_strip_code_fence` in
  `src/nexus/aspect_extractor.py`, called before `json.loads`.

### Not implemented

- No `repair_json` module anywhere. `plan_save` still refuses malformed
  JSON with a hard error instead of repairing it. Nobody has measured that
  refusal as a cost, so it stays a note, not a bead.
- Two of the four cited call sites no longer exist:
  `nexus/db/plan_library.py` became `src/nexus/db/t2/http_plan_library.py`,
  and `tools/build_catalog` was never in the tree.

## Drift classification

| Category | Count | Examples | Preventable? |
| --- | --- | --- | --- |
| Unvalidated assumption | 1 | "constrained decoding is not available" | Yes, spike |
| Framework API detail | 1 | two cited paths did not survive the service cutover | No |

## What to check first next time

When an RDR rejects an alternative because a platform lacks a feature, the
rejection carries a date. Re-check it before gating; the cheapest fix to a
parsing problem is usually the platform growing the feature you wanted.
