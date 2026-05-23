---
description: Enrich beads with execution context using mcp__plugin_nx_nexus__nx_enrich_beads (RDR-080)
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

Invoke the **enrich-plan** skill (calls `mcp__plugin_nx_nexus__nx_enrich_beads` directly — RDR-080, no agent spawn):

```
mcp__plugin_nx_nexus__nx_enrich_beads(
    bead_description="<plan title + description from $ARGUMENTS>",
    context="<audit findings from T1 scratch if present>"
)
```

Fill `bead_description` from the epic bead title and description above. Fill `context` from any prior `nx_plan_audit` findings in T1 scratch.

Deliverable: all beads enriched with file paths, code patterns, test commands, constraints, and audit gap mitigations. Epic bead ID persisted to T2.
