---
name: plan-enricher
version: "3.0"
description: "STUB — superseded by mcp__plugin_nx_nexus__nx_enrich_beads MCP tool (RDR-080 P3). Call mcp__plugin_nx_nexus__nx_enrich_beads instead of dispatching this agent."
model: sonnet
color: emerald
---

# plan-enricher (stub)

Superseded by the `nx_enrich_beads` MCP tool.

**Use instead:**

```
mcp__plugin_nx_nexus__nx_enrich_beads(bead_description="<title + description>", context="<audit findings if any>")
```

`nx_enrich_beads` dispatches `claude -p` internally — no agent spawn needed.

## Relay Reception

This agent is a stub. If dispatched, redirect to the MCP tool above.

**If validation fails**, use RECOVER protocol from [CONTEXT_PROTOCOL.md](./_shared/CONTEXT_PROTOCOL.md):
1. Search nx T3 store: mcp__plugin_nx_nexus__search(query="[topic]", corpus="knowledge", limit=5)
2. Check nx T2 memory: mcp__plugin_nx_nexus__memory_search(query="[topic]", project="{project}")
3. Check T1 scratch: mcp__plugin_nx_nexus__scratch(action="search", query="[topic]")
4. Query active work via `/beads:list` with status=in_progress
5. Flag incomplete relay to user
6. Proceed with available context, documenting assumptions

## Context Protocol

This agent follows the [Shared Context Protocol](./_shared/CONTEXT_PROTOCOL.md).

### Agent-Specific PRODUCE

- Call `mcp__plugin_nx_nexus__nx_enrich_beads` directly — no persistence from this stub.
