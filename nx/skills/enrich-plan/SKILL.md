---
name: enrich-plan
description: Use when beads need enrichment with audit findings, execution context, and codebase alignment after plan-audit validates
effort: medium
---

# Enrich Plan Skill

Delegates to the **plan-enricher** agent (sonnet). See [registry.yaml](../../registry.yaml).

## When This Skill Activates

- After plan-auditor completes in RDR planning chain (automatic relay)
- User says "enrich beads", "groom plan", "enrich plan"
- User invokes `/nx:enrich-plan`
- After `/nx:plan-audit` when beads need audit findings folded in

## Agent Invocation

Use the Agent tool to invoke **plan-enricher**:

```markdown
## Relay: plan-enricher

**Task**: Enrich all beads with audit findings, execution context, and codebase alignment
**Bead**: [epic bead ID] or 'none'

### Input Artifacts
- nx scratch: audit findings, plan structure, bead IDs (from same-session /nx:plan-audit)
- Files: [RDR file path if known]

### Deliverable
All beads enriched with audit-identified gaps, test strategies, dependency refinements, and full execution context. Epic bead ID persisted to T2.

### Quality Criteria
- [ ] Every bead enriched with audit findings (or context-only if T1 miss)
- [ ] Epic bead ID written to T2 for close-time advisory
- [ ] Enrichment summary reported to user
```

For full relay structure and optional fields, see [RELAY_TEMPLATE.md](../../agents/_shared/RELAY_TEMPLATE.md).

## Session Scope Note

T1 scratch is session-scoped. Standalone invocation only works within the same session where `/nx:plan-audit` ran. Cross-session use requires re-running `/nx:plan-audit` first to populate T1 with audit findings.

## Agent-Specific PRODUCE

- **Enriched Beads**: Updated via Write tool → `bd update <id> --body-file /tmp/bead-<id>.md` with execution-ready context
- **T2 memory**: Epic bead ID written via memory_put tool: project="{repo}_rdr", title="NNN"
- **T1 scratch**: Enrichment summary via scratch tool: action="put", tags="enrichment-complete"
- **Console output**: Enriched plan summary table

## Success Criteria

- [ ] Plan-enricher agent dispatched with relay template
- [ ] All beads enriched with available context
- [ ] Epic bead ID persisted to T2
- [ ] Enrichment summary displayed
