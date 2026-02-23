---
description: Thorough analysis of complex problems using deep-analyst agent
---

# Deep Analysis Request

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

  # Active beads context
  echo "### Active Beads"
  echo '```'
  if command -v bd &> /dev/null; then
    bd list --status=in_progress --limit=5 2>/dev/null || echo "No in-progress beads"
  else
    echo "Beads not available"
  fi
  echo '```'
  echo ""

  echo "### Tip"
  echo ""
  echo "The deep-analyst uses sequential thinking: hypothesis → evidence → evaluation → conclusion."
  echo "For cross-cutting issues, this agent explores multiple components before converging on root cause."

  # Project management context
  echo "### Project Management Context"
  echo ""
  if command -v nx &> /dev/null; then
    echo "**PM Status:**"
    echo '```'
    nx pm status 2>/dev/null || echo "No PM initialized"
    echo '```'
    echo ""
    PROJECT=$(basename "$(git rev-parse --show-toplevel 2>/dev/null)" 2>/dev/null)
    if [ -n "$PROJECT" ]; then
      echo "**T2 Memory (${PROJECT}_active):**"
      echo '```'
      nx memory list --project "${PROJECT}_active" 2>/dev/null | head -8 || echo "No T2 memory"
      echo '```'
      echo ""
      echo "**Session Scratch (T1):**"
      echo '```'
      nx scratch list 2>/dev/null | head -5 || echo "No T1 scratch"
      echo '```'
    fi
  fi
}

## Problem to Analyze

$ARGUMENTS

## Relay Instructions

Use the **Task tool** to delegate to deep-analyst:

```markdown
## Relay: deep-analyst

**Task**: Investigate "$ARGUMENTS" using hypothesis-driven sequential analysis
**Bead**: [Create investigation bead if significant or 'none']

### Input Artifacts
- nx store: [Search for prior analysis on this component/topic]
- nx memory: [project/title path or 'none']
- Files: [Key files related to the problem]

### Problem Statement
$ARGUMENTS

### Deliverable
Root cause analysis with evidence-backed conclusions and actionable recommendations

### Quality Criteria
- [ ] Multiple hypotheses explored before concluding
- [ ] Evidence gathered from code, logs, and metrics
- [ ] Root cause identified with confidence rating
- [ ] Conclusions supported by specific evidence
- [ ] Recommendations are actionable and prioritized

**IMPORTANT**: After analysis completes, persist findings to nx store using knowledge-tidier.
```
