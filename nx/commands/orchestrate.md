---
description: Route request to appropriate agent using orchestrator
---

# Orchestration Request

!{
  echo "## Context"
  echo ""
  echo "**Working directory:** $(pwd)"
  echo ""

  # Git context
  if git rev-parse --git-dir > /dev/null 2>&1; then
    echo "**Branch:** $(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo 'unknown')"
    echo ""
  fi

  # Available beads for context
  echo "### Active Work"
  echo '```'
  if command -v bd &> /dev/null; then
    bd list --status=in_progress --limit=5 2>/dev/null || echo "No in-progress beads"
  else
    echo "Beads not available"
  fi
  echo '```'
  echo ""

  echo "### Available Agents"
  echo ""
  echo "Development: java-developer, java-architect-planner, java-debugger"
  echo "Review: code-review-expert, plan-auditor, substantive-critic, test-validator"
  echo "Research: deep-research-synthesizer"
  echo "Analysis: deep-analyst, codebase-deep-analyzer, strategic-planner"
  echo "Utility: knowledge-tidier, orchestrator, pdf-chromadb-processor"

  # Project context
  echo "### Project Context"
  echo ""
  if command -v nx &> /dev/null; then
    PROJECT=$(basename "$(git rev-parse --show-toplevel 2>/dev/null)" 2>/dev/null)
    if [ -n "$PROJECT" ]; then
      echo "**T2 Memory ($PROJECT):**"
      echo '```'
      nx memory list --project "$PROJECT" 2>/dev/null | head -8 || echo "No T2 memory"
      echo '```'
      echo ""
      echo "**Session Scratch (T1):**"
      echo '```'
      nx scratch list 2>/dev/null | head -5 || echo "No T1 scratch"
      echo '```'
    fi
  fi
}

## Request to Route

$ARGUMENTS

## Action

Invoke the **orchestration** skill with the following relay. Fill in dynamic fields from the context above:

```markdown
## Relay: orchestrator

**Task**: Analyze "$ARGUMENTS" and recommend the appropriate agent(s) and workflow
**Bead**: [fill from active bead above or 'none']

### Input Artifacts
- Files: [fill from relevant files if applicable, or 'none']

### User Request
$ARGUMENTS

### Deliverable
Routing recommendation: identified agent(s), execution order (sequential/parallel), rationale for selection, and a ready-to-use relay message for the first agent in the workflow.

### Quality Criteria
- [ ] User goal clearly understood and restated
- [ ] Most appropriate agent(s) identified from available roster
- [ ] Workflow order justified (sequential vs parallel) with rationale
- [ ] Ready-to-use relay message included for the recommended agent
```

For full relay structure and optional fields, see [RELAY_TEMPLATE.md](../agents/_shared/RELAY_TEMPLATE.md).
