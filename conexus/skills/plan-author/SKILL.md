---
name: plan-author
description: Use when authoring a new plan template from scratch — dispatches the plan-author meta-seed which surveys prior art, the plan-authoring guide, and the dimension registry before drafting a candidate plan_json
effort: medium
---

# plan-author

Self-referential bootstrap. The plan-author meta-seed teaches how to
write plans; invoking this skill executes that seed.

## Flow

```
plan_match(
    intent="author a plan for $target_verb: $concept",
    dimensions={verb: "plan-author", strategy: "default"},
    min_confidence=0.40,
    n=1,
)
→ plan_run(match, bindings={concept: <target>, target_verb: <verb>})
```

## Required bindings

- `concept` — one-phrase description of what the new plan should do
  (e.g. "audit projection-quality hub detection").

## Optional bindings

- `target_verb` — which verb the new plan belongs to
  (`default_bindings` → `research`).

## What the seed surfaces

1. The plan-authoring guide (`docs/plan-authoring-guide.md`) —
   vocabulary, schema, dimension conventions.
2. The dimension registry (`nx/plans/dimensions.yml`) — valid
   dimension keys.
3. Prior art via `plan_search` — existing plans at the target verb
   so the draft doesn't duplicate or diverge needlessly.
4. A draft `plan_json` which the caller reviews + saves via
   `plan_save`.

The seed stops short of saving — authoring is collaborative; the
caller reviews the draft, edits as needed, and invokes `plan_save`
when satisfied.

## Anti-patterns

- **Skipping review.** Never auto-save the seed's draft. Good
  descriptions take a few iterations to settle.
- **Ignoring the dimension registry.** New plans whose dimensions
  are unregistered produce warnings (lenient mode) or raise (strict
  mode). Check `nx/plans/dimensions.yml` before pinning exotic axes.

See `docs/plan-authoring-guide.md` for full authoring reference.
