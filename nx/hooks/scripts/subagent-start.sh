#!/bin/bash

# SubagentStart Hook
# Injects context when agents spawn

# Show available T2 memory docs for active project (all namespaces via prefix scan)
if command -v git &> /dev/null; then
  PROJECT=$(basename "$(git rev-parse --show-toplevel 2>/dev/null)" 2>/dev/null)
  if [[ -n "$PROJECT" ]]; then
    SCAN_SCRIPT="$CLAUDE_PLUGIN_ROOT/hooks/scripts/t2_prefix_scan.py"
    T2_OUT=$(python3 "$SCAN_SCRIPT" "$PROJECT" 2>/dev/null)
    if [[ -n "$T2_OUT" ]]; then
      echo "## T2 Memory (Active Project)"
      echo "$T2_OUT"
      echo ""
    fi
  fi
fi

# Show active beads
if command -v bd &> /dev/null; then
  ACTIVE=$(bd list --status=in_progress 2>/dev/null | head -1)
  if [[ -n "$ACTIVE" ]]; then
    echo "Active Bead: $ACTIVE"
  fi
fi

# Inject relay template so skills don't need to duplicate it
RELAY_TEMPLATE="$CLAUDE_PLUGIN_ROOT/agents/_shared/RELAY_TEMPLATE.md"
if [[ -f "$RELAY_TEMPLATE" ]]; then
  echo ""
  echo "## Relay Format (injected by hook)"
  echo ""
  # Emit required-fields table and template (stop before Optional Fields)
  awk '/^## Optional Fields/{exit} {print}' "$RELAY_TEMPLATE"
fi

# Serena MCP guidance is injected by the sn plugin's own SubagentStart hook.
# Do NOT duplicate here — sn/hooks/scripts/mcp-inject.sh handles it.

# nx MCP Tools — inject usage guidance so ALL agents can use the three-tier storage
cat <<'NXTOOLS'

## nx MCP Tools — Three-Tier Storage (injected by nx plugin)

All agents in this session have access to nx storage tiers via MCP tools. Use these to share findings, read project context, and query knowledge.

### T1 Scratch (session-scoped, shared across all agents)

Inter-agent communication within this session. Siblings and parent see the same entries.

```
# Write a finding for other agents to see
mcp__plugin_nx_nexus__scratch(action="put", content="<your finding>", tags="hypothesis,auth")

# Search for what siblings/parent already found
mcp__plugin_nx_nexus__scratch(action="search", query="<topic>", n=5)

# List all entries
mcp__plugin_nx_nexus__scratch(action="list")

# Read a specific entry
mcp__plugin_nx_nexus__scratch(action="get", entry_id="<id>")
```

**Tags**: `impl`, `checkpoint`, `failed-approach`, `hypothesis`, `discovery`, `decision`

**Flag for persistence** (survives session end → promoted to T2):
```
mcp__plugin_nx_nexus__scratch_manage(action="flag", entry_id="<id>", project="<project>", title="<name>.md")
```

### T2 Memory (project-scoped, persistent across sessions)

Read project decisions, session state, active work context.

```
# Read a specific memory entry
mcp__plugin_nx_nexus__memory_get(project="<project>", title="<name>.md")

# List all entries for a project
mcp__plugin_nx_nexus__memory_get(project="<project>", title="")

# Search memory (paged — use offset for next page)
mcp__plugin_nx_nexus__memory_search(query="<topic>", project="<project>", limit=20, offset=0)

# Write to memory (30d default TTL)
mcp__plugin_nx_nexus__memory_put(content="<content>", project="<project>", title="<name>.md")
```

### T3 Knowledge Store (permanent, cross-session)

Search indexed code, docs, and knowledge. Store validated findings.

```
# Search (paged — use offset for next page)
mcp__plugin_nx_nexus__search(query="<topic>", corpus="knowledge", n=10, offset=0)

# List entries (paged)
mcp__plugin_nx_nexus__store_list(collection="knowledge", limit=20, offset=0)

# Store a finding
mcp__plugin_nx_nexus__store_put(content="<content>", collection="knowledge", title="<title>", tags="<tags>")
```

### Pagination

All list/search tools return paged results. The response footer shows the current page and `offset=N` for the next page. To get more results, re-call with `offset=N`:

```
# First page (default)
mcp__plugin_nx_nexus__search(query="auth", n=10, offset=0)
# → "--- Page 1: showing 1-10 of 25. Next page: offset=10"

# Second page
mcp__plugin_nx_nexus__search(query="auth", n=10, offset=10)
```

This applies to: `search`, `store_list`, `memory_search`.

### When to Use Which Tier

| Need | Tier | Tool |
|------|------|------|
| Share finding with sibling agents this session | T1 | `scratch(action="put")` |
| Check what other agents already found | T1 | `scratch(action="search")` |
| Read project context/decisions | T2 | `memory_get` |
| Persist finding across sessions | T2 | `memory_put` |
| Search indexed codebase or knowledge | T3 | `search` |
| Store validated architectural insight | T3 | `store_put` |
| Search saved query plans | T2 | `plan_search(query="...", project="{repo}")` |

### T2 Plan Library

Query execution plans can be saved and reused. The `/nx:query` skill manages this automatically, but agents can search for prior plans:

```
# Search for similar query plans
mcp__plugin_nx_nexus__memory_search(query="compare error handling", project="nexus")
```

### Analytical Operators

The `analytical-operator` agent provides 5 operations over retrieved content:
- **extract**: structured JSON extraction using a template
- **summarize**: short/detailed/evidence-backed summary
- **rank**: LLM-scored ordering by criterion
- **compare**: consistency/contradiction check
- **generate**: evidence-grounded text with citations

These are dispatched by the `/nx:query` skill. Step outputs persist in T1 scratch with tag `query-step,step-N`.
NXTOOLS

# Sequential Thinking MCP — inject usage guidance for hypothesis-driven work
cat <<'SEQTHINK'

## Sequential Thinking MCP (injected by nx plugin)

Use `mcp__plugin_nx_sequential-thinking__sequentialthinking` for any non-trivial decision:
debugging hypotheses, design choices, plan evaluation, risk assessment.

**Pattern**: State hypothesis → identify evidence → gather → evaluate → branch or proceed.
- Set `needsMoreThoughts: true` to continue reasoning
- Set `isRevision: true` + `revisesThought: N` to correct earlier thinking
- Set `branchFromThought: N` + `branchId: "alt"` to explore alternatives
- Adjust `totalThoughts` up/down as complexity becomes clearer

**When to use**: debugging, analysis, design, exploration, any multi-step reasoning.
**When NOT to use**: simple lookups, straightforward file edits, routine operations.
SEQTHINK

# T1 scratch: SHARED across all agents in this session via PPID chain (RDR-010).
# All agents spawned from the same root Claude Code process see the same entries.
# Inject current entries so this agent knows what siblings/parent already found.
if command -v nx &> /dev/null; then
  T1_ENTRIES=$(nx scratch list 2>/dev/null)
  if [[ -n "$T1_ENTRIES" && "$T1_ENTRIES" != "No scratch entries." ]]; then
    echo ""
    echo "## Session Scratch (T1 — shared across all agents this session)"
    echo "$T1_ENTRIES"
    echo ""
  fi
fi
