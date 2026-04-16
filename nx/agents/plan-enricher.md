---
name: plan-enricher
version: "3.0"
description: Enriches beads with execution context — file paths, code patterns, constraints, test commands, and (when available) audit findings. Use after plan-audit in RDR planning chain, or standalone for bead enrichment within the same session.
model: sonnet
color: emerald
effort: medium
---

Use `mcp__plugin_nx_nexus__nx_enrich_beads(bead_description=..., context=...)` directly. The MCP tool dispatches to the operator pool for bead enrichment. This agent file is retained as a doc stub for registry compatibility.
