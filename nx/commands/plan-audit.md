---
description: Audit a plan via nx_plan_audit MCP tool
---

# Plan Audit Request

!{
  echo "## Context"
  echo ""
  echo "**Working directory:** $(pwd)"
  echo ""

  echo "Provide the plan to audit in the arguments or reference existing documentation."
  echo ""

  # Bead context
  echo ""
  echo "### Related Beads"
  echo '```'
  if command -v bd &> /dev/null; then
    bd list --type=epic --status=open --limit=3 2>/dev/null || echo "No open epics"
  else
    echo "Beads not available"
  fi
  echo '```'
}

## Plan to Audit

$ARGUMENTS

## Action

Call `mcp__plugin_nx_nexus__nx_plan_audit(plan_json=<plan>, context=<context>)` to validate the plan.

Populate `plan_json` with the plan content from `$ARGUMENTS` (or from the strategic-planner output). Populate `context` with relevant bead and file information from the context above.

### Quality Criteria
- [ ] All assumptions verified against actual codebase state
- [ ] Dependencies confirmed to exist (classes, APIs, libraries)
- [ ] Build/test commands validated (runnable as specified)
- [ ] Risks identified with severity and mitigation status
- [ ] Clear go/no-go recommendation with rationale
