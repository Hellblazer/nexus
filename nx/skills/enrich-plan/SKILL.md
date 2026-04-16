---
name: enrich-plan
description: Use when beads need enrichment with execution context — file paths, code patterns, constraints, test commands.
effort: low
---

# Enrich Plan

Calls the `nx_enrich_beads` MCP tool. No agent spawn needed.

```
mcp__plugin_nx_nexus__nx_enrich_beads(bead_description="<title + description>", context="<audit findings if any>")
```
