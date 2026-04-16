---
description: Enrich beads with execution context via nx_enrich_beads MCP tool
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

Call `mcp__plugin_nx_nexus__nx_enrich_beads(epic_bead=<epic-bead-id>, context=<context>)` to enrich all beads with execution context.

Populate `epic_bead` with the epic bead ID from the context above (or 'none'). Populate `context` with relevant file paths and audit findings from T1 scratch if present.

### Quality Criteria
- [ ] Every bead enriched with execution context
- [ ] Epic bead ID written to T2 for close-time advisory
- [ ] Enrichment summary reported to user
