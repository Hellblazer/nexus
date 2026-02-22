---
description: Research topic using deep-research-synthesizer agent
---

# Research Request

!{
  echo "## Context"
  echo ""
  echo "**Working directory:** $(pwd)"
  echo ""

  echo "### Available Knowledge Sources"
  echo ""
  echo "- **nx store**: Semantic search across stored knowledge"
  echo "- **Web**: Current information from web search"
  echo "- **Codebase**: Relevant code examples and patterns"
  echo "- **nx memory**: Session context and prior work"
  echo ""

  # Check for existing research
  echo "### Tip"
  echo ""
  echo "The agent will first search nx store for existing research on this topic."
  echo "Prior findings will be incorporated into the synthesis."

  # Project management context
  echo "### Project Management Context"
  echo ""
  if command -v nx &> /dev/null; then
    echo "**PM Status:**"
    echo '```'
    nx pm status 2>/dev/null || echo "No PM initialized"
    echo '```'
    echo ""
    PROJECT=$(basename $(git rev-parse --show-toplevel 2>/dev/null) 2>/dev/null)
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

## Research Topic

$ARGUMENTS

## Relay Instructions

Use the **Task tool** to delegate to deep-research-synthesizer:

```markdown
## Relay: deep-research-synthesizer

**Task**: Research "$ARGUMENTS" across all available sources
**Bead**: [Create research bead if significant topic or 'none']

### Input Artifacts
- nx store: [Search for existing knowledge on topic]
- nx memory: [project/title path or 'none']
- Files: [Relevant code if applicable]

### Research Question
$ARGUMENTS

### Deliverable
Comprehensive research synthesis with actionable recommendations

### Quality Criteria
- [ ] All sources consulted (nx store, web, code)
- [ ] Findings synthesized (not just listed)
- [ ] Contradictions resolved
- [ ] Actionable recommendations provided
- [ ] Sources cited

**IMPORTANT**: After research completes, MUST delegate to knowledge-tidier to persist findings to nx store.
```
