---
name: plan-validation
description: Use when a plan needs validation before implementation — catches gaps and codebase misalignment.
effort: low
---

# Plan Validation

Calls the `nx_plan_audit` MCP tool. No agent spawn needed.

```
mcp__plugin_nx_nexus__nx_plan_audit(plan_json="<plan JSON string>", context="<codebase context if any>")
```
