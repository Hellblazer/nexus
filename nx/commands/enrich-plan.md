---
description: Enrich beads with execution context using plan-enricher agent
---

# Enrich Plan

!{
  echo "## Context"
  echo ""
  echo "**Working directory:** $(pwd)"
  echo ""

  # Bead context
  echo "### Related Beads"
  echo '```'
  if command -v bd &> /dev/null; then
    bd list --type=epic --status=open --limit=5 2>/dev/null || echo "No open epics"
  else
    echo "Beads not available"
  fi
  echo '```'
}

## Plan to Enrich

$ARGUMENTS

## Action

Invoke the **enrich-plan** skill with the following relay. Fill in dynamic fields from the context above:

```markdown
## Relay: plan-enricher

**Task**: Enrich all beads with execution context and codebase alignment
**Bead**: [fill from epic bead above or 'none']

### Input Artifacts
- nx scratch: plan structure, bead IDs, audit findings (if present from same-session /nx:plan-audit)
- Files: [fill from key files referenced in plan]

### Deliverable
All beads enriched with file paths, code patterns, test commands, constraints, and (when available) audit gap mitigations. Epic bead ID persisted to T2.

### Quality Criteria
- [ ] Every bead enriched with execution context
- [ ] Epic bead ID written to T2 for close-time advisory
- [ ] Enrichment summary reported to user
```

For full relay structure, see [RELAY_TEMPLATE.md](../agents/_shared/RELAY_TEMPLATE.md).
