---
name: plan-inspect
description: Use when inspecting plan runtime metrics or enumerating dimension-registry usage — dispatches plan_match with dimensions={verb:plan-inspect}; strategy:default reports per-plan metrics, strategy:dimensions reports registry usage counts
effort: low
---

# plan-inspect

Two variants share this skill. The `strategy` dimension picks which:

- **`strategy:default`** — inspect a single plan's runtime metrics
  and match history.
- **`strategy:dimensions`** — enumerate registered dimensions from
  `nx/plans/dimensions.yml` and count plans pinning each.

## Flow — single-plan inspection

```
plan_match(
    intent="inspect plan $target",
    dimensions={verb: "plan-inspect", strategy: "default"},
    min_confidence=0.40,
    n=1,
)
→ plan_run(match, bindings={target: <plan_id or search string>})
```

## Flow — dimension-registry inspection

```
plan_match(
    intent="show dimension registry usage",
    dimensions={verb: "plan-inspect", strategy: "dimensions"},
    min_confidence=0.40,
    n=1,
)
→ plan_run(match, bindings={})
```

## Metrics surfaced (default variant)

- `use_count` — how many times `plan_run` was invoked
- `last_used` — ISO timestamp of most recent run
- `match_count` — how many `plan_match` calls surfaced the plan
- `match_conf_sum` / `match_count` — average cosine of scored hits
- `success_count` / `failure_count` — `plan_run` outcome breakdown

## Anti-patterns

- **Using plan-inspect as a promotion trigger.** Metrics inspection
  is diagnostic; promotion decisions go through `/nx:plan-promote`.
- **Querying a plan you just wrote.** Metrics are empty for a new
  plan. Wait for runtime data before judging.

See `docs/plan-authoring-guide.md` for dimension conventions.
