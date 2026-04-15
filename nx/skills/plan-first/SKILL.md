---
name: plan-first
description: Use when starting any retrieval task — the gate that tries plan_match first, executes via plan_run if a match clears the threshold, and falls through to /nx:query only on a miss
effort: low
---

# plan-first

**Gate skill.** Every retrieval-shaped task starts here. The plan
library exists so agents don't re-derive pipelines that have already
been authored; this skill is the discipline that enforces it.

## Rule

Before decomposing any retrieval task, call `plan_match` first. If a
match clears `min_confidence`, execute the returned plan via
`plan_run`. Only dispatch `/nx:query` when no plan matches.

## Flow

1. **`plan_match(intent, dimensions={...}, min_confidence=0.85, n=1)`**
   — match the caller's intent against the T1 `plans__session`
   cache. Pin `verb` when known (via the caller's own verb skill),
   leave `dimensions={}` when unknown.
2. **If match returned with `confidence >= 0.85`** (or if the output
   line shows `confidence=fts5` — the FTS5 fallback sentinel that
   `plan_match` renders when the in-session cosine cache is
   unavailable; internally this is `confidence=None` on the `Match`
   object):
   - Present the plan's `description` and `dimensions` to the caller
     as a one-line summary.
   - Invoke `plan_run(plan_id=<match.id>, bindings='{...}')`.
   - Return the final step's result to the caller.
3. **If no match clears the threshold**:
   - Dispatch `/nx:query` with the original intent. The
     query-planner agent will decompose; save the resulting plan
     via `plan_save` so the next identical intent is a cache hit.

## When to pin dimensions

- **From a verb skill** (e.g. `/nx:research`): pin `dimensions={verb:
  "research"}`. Narrows the cosine pool to research plans; specificity
  tiebreaks pick the best strategy variant.
- **From the top-level agent**: leave dimensions empty. Let the
  semantic match rank across verbs.
- **From a specialisation context** (e.g. security review of a
  change set): pin `dimensions={verb: "review", domain: "security"}`.
  The curried `strategy:security` variant wins when available; falls
  back to `strategy:default` otherwise.

## Caller bindings

Every verb scenario template declares `required_bindings`. Pass them
via `plan_run(match, bindings={...})`:

- `research` — `concept` (required), `limit` (optional)
- `review` — `changed_paths` (required), `depth` (optional)
- `analyze` — `area`, `criterion` (required), `limit` (optional)
- `debug` — `failing_path`, `symptom` (required)
- `document` — `area` (required), `limit` (optional)

If `plan_run` raises `PlanRunBindingError(missing=[...])`, surface
the missing bindings to the user rather than guessing defaults.

## Exit conditions

- **Plan returned** → present the final step's result, log the
  plan_id for the session trace.
- **No plan matched** → fall through to `/nx:query` and save the
  resulting plan via `plan_save` for future reuse.
- **`plan_run` raised** → surface the error verbatim; do not retry
  with heuristic bindings.

## Anti-patterns

- **Skipping plan_match for "simple" queries.** Description: "Just
  grep for the config key." No — even simple queries benefit from
  the plan library's retrieval discipline when a matching plan
  exists.
- **Passing an unnamed plan result to a downstream tool.** The
  `plan_run` result is a `PlanResult` with `steps` and `final`; read
  `final` into the user-facing summary, not the raw step list.
- **Ignoring `confidence=fts5`.** The FTS5 fallback sentinel (rendered
  as `confidence=fts5` in `plan_match` output; `confidence=None` on
  the Python `Match` object) means "no cosine but keyword match
  survived" — treat it as a pass, not
  a miss. Only `confidence < min_confidence` (numeric) is a miss.

## Companion docs

- `docs/plan-authoring-guide.md` — how to write a new plan when
  nothing matches.
- `docs/catalog-link-types.md` — the link-type set the `traverse`
  operator walks.
- `docs/catalog-purposes.md` — purpose aliases for common traversal
  shapes.
