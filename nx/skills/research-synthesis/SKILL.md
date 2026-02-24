---
name: research-synthesis
description: Use when researching unfamiliar topics, comparing technology approaches, or building comprehensive understanding from multiple sources
---

# Research Synthesis Skill

Delegates to the **deep-research-synthesizer** agent. See [registry.yaml](../../registry.yaml) for details.

## When This Skill Activates

- Researching a new topic or technology
- Investigating best practices
- Comparing different approaches
- Learning about unfamiliar concepts
- Questions requiring information synthesis
- User says "research", "find out", "best practices"

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

## Research Methodology

The agent uses sequential thinking:
1. Form hypothesis about what information is needed
2. Search nx store for existing knowledge: `nx search "topic" --corpus knowledge`
3. Search web resources for current information
4. Analyze relevant code if applicable
5. Synthesize findings from all sources
6. Resolve contradictions between sources
7. Formulate actionable recommendations

## Success Criteria

- [ ] All relevant sources consulted (nx store, web, code)
- [ ] Key findings synthesized (not just listed)
- [ ] Contradictions identified and resolved
- [ ] Recommendations provided with supporting evidence
- [ ] Findings persisted to nx T3 store via knowledge-tidier
- [ ] Round artifacts persisted to T2 or T3

## Agent-Specific PRODUCE

- **Research Synthesis**: Store in nx T3 as `printf "# Research: {topic}\n{content}\n" | nx store put - --collection knowledge --title "research-{topic}-{date}" --tags "research,{domain}"`
- **Source Citations**: Include in document content (not separate)
- **Knowledge Gaps**: Create research beads for follow-up
- **Cross-Reference Maps**: Document relationships in nx T3 document content
- **Round Artifacts**: Use T1 scratch to track findings per research round:
  ```bash
  nx scratch put $'# Round {N} findings\n{content}' --tags "research,round-{N}"
  nx scratch flag <id> --project {project} --title research-round-{N}.md
  ```
