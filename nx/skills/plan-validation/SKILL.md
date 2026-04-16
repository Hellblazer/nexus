---
name: plan-validation
description: Use when a plan has been created and needs validation before implementation begins, or when reviewing an existing plan for gaps
effort: high
---

# Plan Validation Skill

Use `mcp__plugin_nx_nexus__nx_plan_audit(plan_json=..., context=...)` to validate plans against the codebase. The MCP tool dispatches to the operator pool for file-path verification, dependency checking, and gap analysis.
