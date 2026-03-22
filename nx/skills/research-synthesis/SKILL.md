---
name: research-synthesis
description: Use when researching unfamiliar topics, comparing technology approaches, or building comprehensive understanding from multiple sources
effort: medium
---

# Research Synthesis Skill

Delegates to the **deep-research-synthesizer** agent. See [registry.yaml](../../registry.yaml) for details.

## When This Skill Activates

- Researching a new topic or technology
- Investigating best practices for an unfamiliar domain
- Comparing different approaches or frameworks
- Questions requiring synthesis from multiple sources
- When nx T3 search returns insufficient context for a decision

## Agent Invocation

Use the Task tool to invoke **deep-research-synthesizer**:

```markdown
## Relay: deep-research-synthesizer

**Task**: [what needs to be done]
**Bead**: [ID] or 'none'

### Input Artifacts
- Files: [relevant files]

### Deliverable
Research synthesis across multiple sources

### Quality Criteria
- [ ] All relevant sources consulted
- [ ] Key findings synthesized (not just listed)
- [ ] Recommendations provided with supporting evidence
```

For full relay structure and optional fields, see [RELAY_TEMPLATE.md](../../agents/_shared/RELAY_TEMPLATE.md).

## Research Methodology

The agent uses `mcp__sequential-thinking__sequentialthinking`:
1. Form hypothesis about what information is needed
2. Search nx store for existing knowledge: Use search tool: query="topic", corpus="knowledge"
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

- **Research Synthesis**: Store in nx T3 via store_put tool: content="# Research: {topic}\n{content}", collection="knowledge", title="research-{topic}-{date}", tags="research,{domain}"
- **Source Citations**: Include in document content (not separate)
- **Knowledge Gaps**: Create research beads for follow-up
- **Cross-Reference Maps**: Document relationships in nx T3 document content
- **Round Artifacts**: Use T1 scratch to track findings per research round:
  - scratch tool: action="put", content="# Round {N} findings\n{content}", tags="research,round-{N}"
  - scratch_manage tool: action="flag", entry_id="<id>", project="{project}", title="research-round-{N}.md"
