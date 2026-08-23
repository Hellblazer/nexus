---
name: plan-validation
description: Use when a plan needs validation before implementation — catches gaps and codebase misalignment.
effort: low
---

# Plan Validation

Calls the `nx_plan_audit` MCP tool. No agent spawn needed.

```
mcp__plugin_conexus_nexus__nx_plan_audit(
    plan_json="<plan JSON string>",
    context="<codebase context if any>",
    round_number=<1, then 2, then 3... for THIS plan>,
    budget_rounds=<declared budget, 0 if unstated>,
)
```

## The loop terminates (nexus-ll7zm)

- Count `round_number` yourself. The tool is one stateless subprocess
  call and cannot count.
- Findings come back as `BLOCKS-PLANNING` (fix before implementing) or
  `DISCOVER-AT-IMPLEMENTATION` (record, carry, do not re-plan).
- Only `BLOCKS-PLANNING` can return `NOT READY`.
- Round 3 onward returns `RESIDUALS-ONLY`: planning is over, record the
  residuals, start building.
- `budget_rounds` may tighten the cap, never widen it.

Two rounds is not a claim that two rounds suffice. It is the claim that
a third round's findings are cheaper to discover at the keyboard.
