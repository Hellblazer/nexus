---
name: deep-analysis
description: Use when surface-level analysis is insufficient and problems require hypothesis-driven investigation across multiple system components
---

# Deep Analysis Skill

Delegates to the **deep-analyst** agent (model: opus).

## When This Skill Activates

- When investigating performance mysteries
- When debugging multi-component interactions
- When understanding complex system behavior
- When surface-level analysis is insufficient
- When root cause analysis requires deep investigation
- After java-debugger if issue is cross-cutting

## Agent Invocation

## Relay Template (Use This Format)

When invoking this agent via Task tool, use this exact structure:

```markdown
## Relay: {agent-name}

**Task**: [1-2 sentence summary of what needs to be done]
**Bead**: [ID] (status: [status]) or 'none'

### Input Artifacts
- nx store: [document titles or "none"]
- nx memory: [project/title path or "none"]
- nx scratch: [scratch IDs or "none"]           # optional: ephemeral T1 items
- nx pm context: [Phase N, active blockers or "none"]  # optional: from nx pm status
- Files: [key files or "none"]

### Deliverable
[What the receiving agent should produce]

### Quality Criteria
- [ ] [Criterion 1]
- [ ] [Criterion 2]
- [ ] [Criterion 3]
```

**Required**: All fields must be present. Agent will validate relay before starting.

For additional optional fields, see [RELAY_TEMPLATE.md](../../agents/_shared/RELAY_TEMPLATE.md).

## Investigation Methodology

The deep-analyst uses sequential thinking:
1. Form initial hypothesis about the problem
2. Identify evidence needed to validate/refute
3. Gather evidence systematically (code, logs, metrics)
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

- **Analysis Findings**: Store in nx T3 as `printf "# Analysis: {topic}\n{findings}\n" | nx store put - --collection knowledge --title "analysis-{topic}-{date}" --tags "analysis"`
- **Hypothesis Results**: Document with confidence levels in nx T3
- **Recommendations**: Include in relay to downstream agent (strategic-planner)
- **Analysis Chain**: Use T1 scratch to track hypothesis progression during investigation:
  ```bash
  nx scratch put $'Analysis step {N}: {hypothesis}\nEvidence: {evidence}\nConfidence: {level}' --tags "analysis,step-{N}"
  nx scratch promote <id> --project {project} --title analysis-chain.md
  ```
