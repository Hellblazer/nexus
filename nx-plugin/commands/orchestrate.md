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
  echo "Review: code-review-expert, plan-auditor, deep-critic, test-validator"
  echo "Research: deep-research-synthesizer"
  echo "Analysis: deep-analyst, codebase-deep-analyzer, strategic-planner"
  echo "Utility: knowledge-tidier, orchestrator, pdf-chromadb-processor, project-management-setup"
}

## Request to Route

$ARGUMENTS

## Relay Instructions

Use the **Task tool** to delegate to orchestrator:

```markdown
## Relay: orchestrator

**Task**: Analyze "$ARGUMENTS" and recommend the appropriate agent(s) and workflow
**Bead**: [Create bead if this initiates trackable work or 'none']

### Input Artifacts
- nx store: [Search for prior work on related topic]
- nx memory: [project/title path or 'none']
- Files: [Relevant files if applicable]

### User Request
$ARGUMENTS

### Deliverable
Clear recommendation of which agent(s) to use, in what order, with rationale and a ready-to-use relay message

### Quality Criteria
- [ ] User goal clearly understood
- [ ] Most appropriate agent(s) identified
- [ ] Workflow order justified (sequential vs parallel)
- [ ] Clear rationale provided for routing decision
- [ ] Ready-to-use relay message included for next agent
```
