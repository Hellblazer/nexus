---
allowed-tools: Bash
description: Audit a plan using mcp__plugin_conexus_nexus__nx_plan_audit (RDR-080)
---

# Plan Audit Request

!`nx command-context plan-audit`

## Plan to Audit

$ARGUMENTS

## Action

Invoke the **plan-validation** skill (calls `mcp__plugin_conexus_nexus__nx_plan_audit` directly — RDR-080, no agent spawn):

```
mcp__plugin_conexus_nexus__nx_plan_audit(
    plan_json="<serialized plan or plan description>",
    context="<codebase context relevant to the plan, if any>",
    round_number=<1 for a first audit; increment for each re-audit of the same plan>,
    budget_rounds=<the plan's declared effort budget, 0 if unstated>
)
```

Fill `plan_json` from the plan to validate (`$ARGUMENTS` or strategic-planner output). Fill `context` from key files referenced in the plan. Track `round_number` yourself — the tool is stateless and cannot.

Deliverable: validation report with go/no-go decision — assumption verification, dependency confirmation, build/test command validation, risk assessment.

Findings arrive classified (nexus-ll7zm). `BLOCKS-PLANNING` holds the plan; `DISCOVER-AT-IMPLEMENTATION` is recorded and carried into implementation, never re-planned. A `RESIDUALS-ONLY` verdict means the blocking-round cap is reached: record the residuals and start building.
