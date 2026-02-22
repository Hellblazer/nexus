---
name: knowledge-tidying
description: >
  Persist and organize knowledge in Nexus T3. Triggers: research-synthesis completes,
  major investigation concludes, user says "save this knowledge", valuable insights discovered.
# See ../../registry.yaml for full agent metadata
allowed-tools: Task, Read, Write, Edit, Glob, Grep, Bash
---

# Knowledge Tidying Skill

Delegates to the **knowledge-tidier** agent (haiku). See [registry.yaml](../../registry.yaml).

## When This Skill Activates

- **Always** after research-synthesis completes (required step)
- After major investigation concludes with valuable findings
- When user says "save this", "persist findings", "remember this"
- When valuable insights should be preserved for future sessions
- Consolidating or organizing existing Nexus knowledge (T3)

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

## Nexus Storage Standards

**Store to T3 knowledge** (`nx store put`):
```bash
echo "# content" | nx store put - --collection knowledge --title "research-topic" --tags "research"
echo "# content" | nx store put - --collection knowledge --title "decision-component-name" --tags "decision,architecture"
echo "# content" | nx store put - --collection knowledge --title "pattern-name" --tags "pattern"
```

**Title conventions**:
- `research-{topic}` - Research findings
- `debug-{component}-{issue-type}` - Debugging insights
- `architecture-{project}-{component}` - Architecture documentation
- `decision-{component}-{decision-name}` - Architectural decisions
- `pattern-{pattern-name}` - Reusable patterns

**Verify storage**:
```bash
nx search "topic" --corpus knowledge --n 5   # confirm searchable
nx store list --collection knowledge__topic  # list entries
```

## Contradiction Handling

If contradictions found with existing knowledge:
1. Search: `nx search "topic" --corpus knowledge` to find related entries
2. Identify which is more current/accurate
3. Replace the stale entry by re-storing with the corrected content

## Success Criteria

- [ ] Knowledge stored in Nexus T3 via `nx store put`
- [ ] No contradictions with existing knowledge
- [ ] Knowledge is searchable (verify with `nx search`)
- [ ] Tags are meaningful for future retrieval
- [ ] Title follows naming convention
