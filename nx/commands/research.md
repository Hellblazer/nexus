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

## Research Topic

$ARGUMENTS

## Action

Invoke the **research-synthesis** skill with the following relay. Fill in dynamic fields from the context above:

```markdown
## Relay: deep-research-synthesizer

**Task**: Research "$ARGUMENTS" across all available sources
**Bead**: [fill from active bead above or 'none']

### Input Artifacts
- Files: [fill from relevant code if applicable, or 'none']

### Research Question
$ARGUMENTS

### Deliverable
Comprehensive research synthesis that integrates findings from nx store, web, and codebase. Includes executive summary, detailed findings with source citations, contradiction resolution, and prioritized actionable recommendations.

### Quality Criteria
- [ ] All available sources consulted (nx store, web, codebase)
- [ ] Findings synthesized into coherent narrative (not just listed)
- [ ] Contradictions between sources identified and resolved
- [ ] Actionable recommendations provided with confidence levels
- [ ] All claims cite their source

**IMPORTANT**: After research completes, MUST delegate to knowledge-tidier to persist findings to nx store.
```

For full relay structure and optional fields, see [RELAY_TEMPLATE.md](../agents/_shared/RELAY_TEMPLATE.md).
