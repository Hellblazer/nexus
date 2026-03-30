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

# Serena MCP — inject symbol navigation guidance so agents use LSP instead of grep
cat <<'SERENA'

## Serena Code Navigation (injected by nx plugin)

Use Serena MCP tools for **symbol-level** tasks. Use Grep only for **text search** (strings, comments, config values).

| Task | Tool |
|------|------|
| Symbol definition | `mcp__plugin_sn_serena__jet_brains_find_symbol(name_path_pattern="ClassName", include_body=false)` |
| All callers/references | `mcp__plugin_sn_serena__jet_brains_find_referencing_symbols(name_path="ClassName", relative_path="path/to/File.py")` |
| File structure overview | `mcp__plugin_sn_serena__jet_brains_get_symbols_overview(relative_path="path/to/File.py")` |
| Class/type hierarchy | `mcp__plugin_sn_serena__jet_brains_type_hierarchy(name_path="ClassName", relative_path="path/to/File.py")` |
| Replace function body | `mcp__plugin_sn_serena__replace_symbol_body(name_path="Class/method", relative_path="path/to/File.py", new_body="...")` |
| Rename symbol safely | `mcp__plugin_sn_serena__rename_symbol(name_path="oldName", relative_path="path/to/File.py", new_name="newName")` |
| Text/pattern search | `mcp__plugin_sn_serena__search_for_pattern(substring_pattern="text", relative_path="optional/dir")` |

**Critical parameter signatures** (common source of errors):
- `find_symbol`: uses `name_path_pattern` (NOT `name_path`)
- `find_referencing_symbols`: uses `name_path` + `relative_path` (NO `include_body`, NO `name_path_pattern`)
- `get_symbols_overview`: `relative_path` must be a FILE, not a directory
- `search_for_pattern`: uses `substring_pattern` (NOT `pattern`)

**Rules**:
- `get_symbols_overview` BEFORE reading whole files
- `find_referencing_symbols` BEFORE any signature change (impact analysis)
- `find_symbol(include_body=false)` first, `include_body=true` only when needed
SERENA

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

# Search memory
mcp__plugin_nx_nexus__memory_search(query="<topic>", project="<project>")

# Write to memory (30d default TTL)
mcp__plugin_nx_nexus__memory_put(content="<content>", project="<project>", title="<name>.md")
```

### T3 Knowledge Store (permanent, cross-session)

Search indexed code, docs, and knowledge. Store validated findings.

```
# Search (default: knowledge,code,docs corpora)
mcp__plugin_nx_nexus__search(query="<topic>", corpus="knowledge", n=5)

# Store a finding
mcp__plugin_nx_nexus__store_put(content="<content>", collection="knowledge", title="<title>", tags="<tags>")
```

### When to Use Which Tier

| Need | Tier | Tool |
|------|------|------|
| Share finding with sibling agents this session | T1 | `scratch(action="put")` |
| Check what other agents already found | T1 | `scratch(action="search")` |
| Read project context/decisions | T2 | `memory_get` |
| Persist finding across sessions | T2 | `memory_put` |
| Search indexed codebase or knowledge | T3 | `search` |
| Store validated architectural insight | T3 | `store_put` |
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
