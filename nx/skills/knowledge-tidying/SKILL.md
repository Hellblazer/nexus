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
- [ ] Knowledge stored in Nexus T3 via store_put tool
- [ ] No contradictions with existing knowledge
- [ ] Tags are meaningful for future retrieval
```

For full relay structure and optional fields, see [RELAY_TEMPLATE.md](../../agents/_shared/RELAY_TEMPLATE.md).

## Nexus Storage Standards

**Store to T3 knowledge** (store_put tool):
- store_put tool: content="# content", collection="knowledge", title="research-topic", tags="research"
- store_put tool: content="# content", collection="knowledge", title="decision-component-name", tags="decision,architecture"
- store_put tool: content="# content", collection="knowledge", title="pattern-name", tags="pattern"

**Title conventions**:
- `research-{topic}` - Research findings
- `debug-{component}-{issue-type}` - Debugging insights
- `architecture-{project}-{component}` - Architecture documentation
- `decision-{component}-{decision-name}` - Architectural decisions
- `pattern-{pattern-name}` - Reusable patterns

**Verify storage**:
- Use search tool: query="topic", corpus="knowledge", n=5 — confirm searchable
- Use store_list tool: collection="knowledge" — list all knowledge entries

## Contradiction Handling

If contradictions found with existing knowledge:
1. Search: Use search tool: query="topic", corpus="knowledge" to find related entries
2. Identify which is more current/accurate
3. Replace the stale entry by re-storing with the corrected content

## Agent-Specific PRODUCE

- **Consolidated Documents**: Store in nx T3 via store_put tool: content="# {topic}\n{consolidated-content}", collection="knowledge", title="consolidation-{date}-{scope}", tags="consolidation,tidier"
- **Archive Actions**: Moved documents go to `--collection knowledge__archive --title "{old-title}-archived-{date}"`, logged in nx T2 memory as `--project {project} --title archive-log.md`
- **Contradiction Resolutions**: Updated directly in source nx T3 documents
- **Review Artifacts**: Use T1 scratch to track review round findings:
  - scratch tool: action="put", content="# Review Round {N}: {N} issues found\n{issue-list}", tags="review,round-{N}"
  - scratch_manage tool: action="promote", id="<id>", project="{project}", title="review-round-{N}.md"

## Success Criteria

- [ ] Knowledge stored in Nexus T3 via store_put tool
- [ ] No contradictions with existing knowledge
- [ ] Knowledge is searchable (verify with search tool)
- [ ] Tags are meaningful for future retrieval
- [ ] Title follows naming convention
