---
name: plan-first
description: Use when starting any retrieval task — route it through `mcp__plugin_conexus_nexus__nx_answer`, which runs the plan-match-first gate internally (match against the plan library, execute on hit, inline-plan on miss). Optionally `plan_search` first to inspect candidate plans. Raw `search` for keyword lookup only. Skipping nx_answer for a verb-shaped question is a defect.
effort: low
---

# plan-first

**Hard gate.** Every retrieval-shaped task routes through
`mcp__plugin_conexus_nexus__nx_answer`. The plan-match-first logic
(library match → execute → inline planner on miss → auto-save) lives
INSIDE `nx_answer` — there are no separately exposed `plan_match` /
`plan_run` MCP tools. Do not attempt to call them.

## Rule

1. Optional pre-check: `mcp__plugin_conexus_nexus__plan_search(query=<intent>, limit=3)`
   to see whether a reusable plan exists (presentational only — `nx_answer`
   re-matches internally either way).
2. Dispatch `mcp__plugin_conexus_nexus__nx_answer(question=<intent>,
   dimensions={verb: "<verb>"} when known)`. On a library hit it executes the
   matched plan; on a miss it inline-plans and auto-saves the new plan, so the
   next identical intent is a cache hit.
3. Raw `search` is for keyword lookup only ("find X in collection Y") — never
   for analytical / verb-shaped questions.

## When to pin dimensions

- **From a verb skill** (e.g. `/conexus:research`): pass
  `dimensions={"verb": "research"}` — narrows the match pool to that verb's
  plans.
- **From the top-level agent**: omit `dimensions` and let the semantic match
  rank across verbs.

## Exit conditions

- `nx_answer` returned → present its answer; the run is recorded and any new
  plan is auto-saved (no manual `plan_save` needed for retrieval pipelines).
- `nx_answer` errored → surface the error verbatim; do not retry with a
  reworded question more than once.
