---
allowed-tools: Bash
description: Thorough analysis of complex problems using deep-analyst agent
---

# Deep Analysis Request

!`nx command-context deep-analysis`

### Project Context

Gather project context using MCP tools:
- Use **memory_get** tool: project="{project}", title="" to list T2 memory entries
- Use **scratch** tool: action="list" to list T1 scratch entries

## Problem to Analyze

$ARGUMENTS

## Action

Invoke the **deep-analysis** skill with the following relay. Fill in dynamic fields from the context above:

```markdown
## Relay: deep-analyst

**Task**: Investigate "$ARGUMENTS" using hypothesis-driven sequential analysis
**Bead**: [fill from active bead above or 'none']

### Input Artifacts
- Files: [fill from key files related to the problem]

### Problem Statement
$ARGUMENTS

### Deliverable
Root cause analysis with hypothesis chain, evidence inventory, confidence-rated conclusions, and prioritized actionable recommendations.

### Quality Criteria
- [ ] Multiple hypotheses explored and eliminated before concluding
- [ ] Evidence gathered from code, logs, and metrics
- [ ] Root cause identified with confidence rating (high/medium/low)
- [ ] Each conclusion supported by specific cited evidence
- [ ] Recommendations are actionable and prioritized by impact

**IMPORTANT**: After analysis completes, persist findings using `mcp__plugin_conexus_nexus__store_put` directly (RDR-080 — no agent spawn needed).
```

For full relay structure and optional fields, see [RELAY_TEMPLATE.md](../agents/_shared/RELAY_TEMPLATE.md).
