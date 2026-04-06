---
name: deep-analysis
description: Use when surface-level analysis is insufficient and problems require hypothesis-driven investigation across multiple system components
effort: high
---

# Deep Analysis Skill

Delegates to the **deep-analyst** agent (model: opus).

## When This Skill Activates

- When investigating performance mysteries
- When debugging multi-component interactions
- When understanding complex system behavior
- When surface-level analysis is insufficient
- When root cause analysis requires deep investigation
- After debugger if issue is cross-cutting

## Agent Invocation

Use the Agent tool to invoke **deep-analyst**:

```markdown
## Relay: deep-analyst

**Task**: [what needs to be done]
**Bead**: [ID] or 'none'

### Input Artifacts
- Files: [relevant files]

### Deliverable
Analysis report with findings and recommendations

### Quality Criteria
- [ ] Multiple hypotheses explored
- [ ] Root cause(s) identified with confidence
- [ ] Recommendations are actionable
```

For full relay structure and optional fields, see [RELAY_TEMPLATE.md](../../agents/_shared/RELAY_TEMPLATE.md).

## Investigation Methodology

The deep-analyst uses `mcp__sequential-thinking__sequentialthinking`:
1. Form initial hypothesis about the problem
2. Identify evidence needed to validate/refute
3. Gather evidence systematically — use `query(question=..., subtree=..., follow_links="cites")` for citation-aware evidence gathering, or `query(question=..., content_type=...)` for type-scoped retrieval
4. Evaluate hypothesis against evidence
5. If refuted, branch to new hypothesis; iterate until root cause found
6. Synthesize findings and provide actionable recommendations

## Success Criteria

- [ ] Problem clearly understood and scoped
- [ ] Multiple hypotheses explored
- [ ] Root cause(s) identified with confidence
- [ ] Conclusions supported by evidence
- [ ] Recommendations are actionable
- [ ] Findings stored in nx store for future reference
- [ ] T2 memory updated with session findings (if multi-session work)

## Agent-Specific PRODUCE

- **Analysis Findings**: Store in nx T3 via store_put tool: content="# Analysis: {topic}\n{findings}", collection="knowledge", title="analysis-{topic}-{date}", tags="analysis"
- **Hypothesis Results**: Document with confidence levels in nx T3
- **Recommendations**: Include in output as "Recommended Next Step" for caller to dispatch strategic-planner
- **Analysis Chain**: Use T1 scratch to track hypothesis progression during investigation:
  - scratch tool: action="put", content="Analysis step {N}: {hypothesis}\nEvidence: {evidence}\nConfidence: {level}", tags="analysis,step-{N}"
  - scratch_manage tool: action="promote", entry_id="<id>", project="{project}", title="analysis-chain.md"
