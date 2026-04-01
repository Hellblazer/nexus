---
name: enrich-plan
description: Use when beads need enrichment with execution context, file paths, code patterns, constraints, and test commands. Audit findings incorporated when available.
effort: medium
---

# Enrich Plan Skill

Delegates to the **plan-enricher** agent (sonnet). See [registry.yaml](../../registry.yaml).

## When This Skill Activates

- After plan-auditor completes in RDR planning chain (automatic relay)
- User says "enrich beads", "groom plan", "enrich plan"
- User invokes `/nx:enrich-plan`
- Beads need execution context before implementation begins

## Agent Invocation

Use the Agent tool to invoke **plan-enricher**:

```markdown
## Relay: plan-enricher

**Task**: Enrich all beads with execution context and codebase alignment
**Bead**: [epic bead ID] or 'none'

### Input Artifacts
- nx scratch: plan structure, bead IDs, audit findings (if present from same-session /nx:plan-audit)
- Files: [RDR file path if known]

### Deliverable
All beads enriched with file paths, code patterns, test commands, dependency constraints, and (when available) audit gap mitigations. Epic bead ID persisted to T2.

### Quality Criteria
- [ ] Every bead enriched with execution context
- [ ] Epic bead ID written to T2 for close-time advisory
- [ ] Enrichment summary reported to user
```

For full relay structure and optional fields, see [RELAY_TEMPLATE.md](../../agents/_shared/RELAY_TEMPLATE.md).

## Session Scope Note

T1 scratch is session-scoped. When invoked after `/nx:plan-audit` in the same session, audit findings from T1 are automatically incorporated. Standalone invocation works without audit findings — codebase-derived context is the primary enrichment value.

## Agent-Specific PRODUCE

- **Enriched Beads**: Updated via Write tool → `/beads:update <id> --body-file /tmp/bead-<id>.md` with execution-ready context
- **T2 memory**: Epic bead ID written via memory_put tool: project="{repo}_rdr", title="NNN"
- **T1 scratch**: Enrichment summary via scratch tool: action="put", tags="enrichment-complete"
- **Console output**: Enriched plan summary table

## Success Criteria

- [ ] Plan-enricher agent dispatched with relay template
- [ ] All beads enriched with available context
- [ ] Epic bead ID persisted to T2
- [ ] Enrichment summary displayed
