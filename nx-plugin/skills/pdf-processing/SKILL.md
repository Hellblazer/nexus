---
name: pdf-processing
description: >
  Process PDF files into nx store for semantic search. Triggers: user says "index PDF",
  importing technical documentation, PDFs need semantic indexing.
allowed-tools: Task, Read, Glob, Grep, Bash
# See ~/.claude/registry.yaml for full agent metadata
---

# PDF Processing Skill

Delegates to the **pdf-chromadb-processor** agent (haiku). See [registry.yaml](../../registry.yaml).

## When This Skill Activates

- When PDFs need to be indexed for semantic search
- When importing technical documentation
- When adding documents to the knowledge base
- When user wants to query PDF content semantically
- When processing research papers or manuals

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

## Processing Methodology

The pdf-chromadb-processor agent:
1. Reads PDF files using appropriate tools
2. Extracts text with layout preservation
3. Chunks content for optimal semantic search
4. Extracts metadata (title, author, date)
5. Creates embeddings via nx store (T3)
6. Verifies indexing success

## Success Criteria

- [ ] All PDFs processed without errors
- [ ] Text properly extracted
- [ ] Content chunked appropriately
- [ ] Metadata preserved
- [ ] Documents searchable in nx store
- [ ] Sample queries return relevant results
