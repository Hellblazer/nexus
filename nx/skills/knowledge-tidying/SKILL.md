---
name: knowledge-tidying
description: Use when validated findings, decisions, or patterns need to be persisted to nx T3 knowledge store for cross-session reuse
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

Use the Task tool to invoke **knowledge-tidier**:

```markdown
## Relay: knowledge-tidier

**Task**: [what needs to be done]
**Bead**: [ID] or 'none'

### Input Artifacts
- Files: [relevant files]

### Deliverable
Organized knowledge entries in T3

### Quality Criteria
- [ ] Knowledge stored in Nexus T3 via nx store put
- [ ] No contradictions with existing knowledge
- [ ] Tags are meaningful for future retrieval
```

For full relay structure and optional fields, see [RELAY_TEMPLATE.md](../../agents/_shared/RELAY_TEMPLATE.md).

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
nx store list --collection knowledge  # list all knowledge entries
```

## Contradiction Handling

If contradictions found with existing knowledge:
1. Search: `nx search "topic" --corpus knowledge` to find related entries
2. Identify which is more current/accurate
3. Replace the stale entry by re-storing with the corrected content

## Agent-Specific PRODUCE

- **Consolidated Documents**: Store in nx T3 as `printf "# {topic}\n{consolidated-content}\n" | nx store put - --collection knowledge --title "consolidation-{date}-{scope}" --tags "consolidation,tidier"`
- **Archive Actions**: Moved documents go to `--collection knowledge__archive --title "{old-title}-archived-{date}"`, logged in nx T2 memory as `--project {project} --title archive-log.md`
- **Contradiction Resolutions**: Updated directly in source nx T3 documents
- **Review Artifacts**: Use T1 scratch to track review round findings:
  ```bash
  nx scratch put $'# Review Round {N}: {N} issues found\n{issue-list}' --tags "review,round-{N}"
  nx scratch promote <id> --project {project} --title review-round-{N}.md
  ```

## Success Criteria

- [ ] Knowledge stored in Nexus T3 via `nx store put`
- [ ] No contradictions with existing knowledge
- [ ] Knowledge is searchable (verify with `nx search`)
- [ ] Tags are meaningful for future retrieval
- [ ] Title follows naming convention
