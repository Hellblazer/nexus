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
