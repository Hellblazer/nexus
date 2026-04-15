---
name: plan-promote
description: Use when surveying the plan library's runtime metrics to propose plans for promotion to a higher scope — advisory-only; dispatches the plan-promote-propose meta-seed (no lifecycle ops — those ship in RDR-079)
effort: medium
---

# plan-promote

**Advisory meta-seed.** Scans the plan library's runtime metrics and
proposes promotion candidates — plans whose usage pattern suggests a
higher scope (personal → project → global, etc.).

**Out of scope**: actual promotion. The `nx plan promote` CLI,
`nx plan audit` CLI, and RDR-close hooks for plan seeding / archival
ship in **RDR-079**. This skill is a primitive form — surface
candidates via markdown shortlist; let the user decide.

## Flow

```
plan_match(
    intent="rank plan promotion candidates",
    dimensions={verb: "plan-promote", strategy: "propose"},
    min_confidence=0.85,
    n=1,
)
→ plan_run(match, bindings={threshold: 5, limit: 20})
```

## Required bindings

- `threshold` — minimum `match_count` for a plan to be considered
  (filters out one-off matches).

## Optional bindings

- `limit` — max candidates to return (`default_bindings` → 20).

## Output

A markdown shortlist with one entry per candidate:

```
1. [id=42] research/default — 18 matches, avg conf 0.89, 15 runs (0 failures)
2. [id=57] analyze/default — 12 matches, avg conf 0.84, 9 runs (1 failure)
...
```

Each entry names the canonical identity (`verb/strategy`), the raw
metrics, and any notable paraphrases from the match history (the
prior-intent set the plan has been responding to).

## Anti-patterns

- **Treating the shortlist as a mandate.** It's advisory. A plan's
  metrics might be high because it's the default for a broadly
  -worded skill, not because it deserves global scope.
- **Running plan-promote on a fresh library.** Nothing to propose;
  the skill will return "(empty)" without error.
- **Extending the skill to write promotion records.** Don't —
  RDR-079 owns lifecycle operations; writing here would bypass the
  authoring/review discipline the RDR-078 → RDR-079 split was
  designed to preserve.

See `docs/plan-authoring-guide.md` for the lifecycle-tier background.
