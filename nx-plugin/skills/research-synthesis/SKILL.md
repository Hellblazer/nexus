---
name: research-synthesis
description: >
  Research topics across multiple sources. Triggers: researching new topic/technology,
  investigating best practices, comparing approaches, learning unfamiliar concepts,
  user says "research", "find out about", "what are best practices for".
# See ../../registry.yaml for full agent metadata
allowed-tools: Task, Read, Glob, Grep, WebSearch, WebFetch
memory: local
context: fork
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
