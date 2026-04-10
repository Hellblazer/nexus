---
name: knowledge-tidying
description: Use when validated findings, decisions, or patterns need to be persisted to nx T3 knowledge store for cross-session reuse
effort: medium
---

# Knowledge Tidying Skill

Delegates to the **knowledge-tidier** agent (haiku). See [registry.yaml](../../registry.yaml).

## When This Skill Activates

- **Always** after research-synthesis completes (required step)
- After major investigation concludes with valuable findings
- When user says "save this", "persist findings", "remember this"
- When valuable insights should be preserved for future sessions
- Consolidating or organizing existing Nexus knowledge (T3)

## Pre-Dispatch: Seed Link Context

Before dispatching the knowledge-tidier agent, seed T1 scratch with link targets so the auto-linker creates catalog links when the agent calls `store_put`. See `/nx:catalog` skill for full reference.

1. If the task references an RDR or source document, resolve it: `mcp__plugin_nx_nexus-catalog__catalog_search(query="<reference>")`
2. Check T1 scratch for existing `link-context` (may already be seeded by a predecessor skill like research-synthesis)
3. If no link-context exists, seed: `mcp__plugin_nx_nexus__scratch(action="put", content='{"targets": [{"tumbler": "<tumbler>", "link_type": "relates"}], "source_agent": "knowledge-tidier"}', tags="link-context")`
4. If no document reference found, skip seeding (auto-linker handles empty context gracefully)

## Agent Invocation

Use the Agent tool to invoke **knowledge-tidier**:

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
- mcp__plugin_nx_nexus__query(question="topic", content_type="knowledge") — confirm searchable via catalog-scoped query
- mcp__plugin_nx_nexus__store_list(collection="knowledge" — list all knowledge entries

## Contradiction Handling

If contradictions found with existing knowledge:
1. Search: mcp__plugin_nx_nexus__search(query="topic", corpus="knowledge") to find related entries
2. Identify which is more current/accurate
3. Re-store with corrected content, then create a `supersedes` link from the new entry to the old:
   ```
   mcp__plugin_nx_nexus-catalog__catalog_link(from_tumbler="<new-entry-tumbler>", to_tumbler="<old-entry-tumbler>", link_type="supersedes", created_by="knowledge-tidier")
   ```
   This preserves the history chain and prevents the old entry from appearing in link-boosted results.

## Agent-Specific PRODUCE

- **Consolidated Documents**: Store in nx T3 via store_put tool: content="# {topic}\n{consolidated-content}", collection="knowledge", title="consolidation-{date}-{scope}", tags="consolidation,tidier"
- **Archive Actions**: Moved documents go to `--collection knowledge__archive --title "{old-title}-archived-{date}"`, logged in nx T2 memory as `--project {project} --title archive-log.md`
- **Contradiction Resolutions**: Updated directly in source nx T3 documents
- **Review Artifacts**: Use T1 scratch to track review round findings:
  - scratch tool: action="put", content="# Review Round {N}: {N} issues found\n{issue-list}", tags="review,round-{N}"
  - scratch_manage tool: action="promote", entry_id="<id>", project="{project}", title="review-round-{N}.md"

## Success Criteria

- [ ] Knowledge stored in Nexus T3 via store_put tool
- [ ] No contradictions with existing knowledge
- [ ] Knowledge is searchable (verify with search tool)
- [ ] Tags are meaningful for future retrieval
- [ ] Title follows naming convention
