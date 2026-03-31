#!/bin/bash

# SubagentStart Hook — injects storage context + active state for spawned agents.

# T2 memory for active project
if command -v git &> /dev/null; then
  PROJECT=$(basename "$(git rev-parse --show-toplevel 2>/dev/null)" 2>/dev/null)
  if [[ -n "$PROJECT" ]]; then
    SCAN_SCRIPT="$CLAUDE_PLUGIN_ROOT/hooks/scripts/t2_prefix_scan.py"
    T2_OUT=$(python3 "$SCAN_SCRIPT" "$PROJECT" 2>/dev/null)
    if [[ -n "$T2_OUT" ]]; then
      echo "## T2 Memory"
      echo "$T2_OUT"
      echo ""
    fi
  fi
fi

# Active beads
if command -v bd &> /dev/null; then
  ACTIVE=$(bd list --status=in_progress 2>/dev/null | head -1)
  if [[ -n "$ACTIVE" ]]; then
    echo "Active Bead: $ACTIVE"
  fi
fi

# Relay template (required fields only)
RELAY_TEMPLATE="$CLAUDE_PLUGIN_ROOT/agents/_shared/RELAY_TEMPLATE.md"
if [[ -f "$RELAY_TEMPLATE" ]]; then
  echo ""
  echo "## Relay Format"
  echo ""
  awk '/^## Optional Fields/{exit} {print}' "$RELAY_TEMPLATE"
fi

# Serena + Context7 guidance injected by sn plugin (sn/hooks/scripts/mcp-inject.sh).

cat <<'NXTOOLS'

## nx Storage Tools

All results from search/list tools are **paged**. Response footer shows `Next page: offset=N`. Re-call with that offset to get more.

T1 scratch — session-scoped, shared across all sibling agents:
  scratch(action="put", content="...", tags="hypothesis,failed-approach,decision")
  scratch(action="search", query="...", n=5)
  scratch(action="list")
  scratch(action="get", entry_id="...")
  scratch_manage(action="flag", entry_id="...", project="...", title="...")  → promotes to T2

T2 memory — project-scoped, persistent:
  memory_get(project="...", title="...")       read one entry
  memory_get(project="...", title="")          list all entries
  memory_search(query="...", project="...", limit=20, offset=0)
  memory_put(content="...", project="...", title="...", ttl=30)

T3 knowledge — permanent, semantic search:
  search(query="...", corpus="knowledge", n=10, offset=0)
  store_list(collection="knowledge", limit=20, offset=0)
  store_put(content="...", collection="knowledge", title="...", tags="...")

Plan library — saved query execution plans (T2):
  plan_search(query="...", project="...", limit=5)
  plan_save(query="...", plan_json="...", project="...", tags="...")

Tool prefix: mcp__plugin_nx_nexus__

Routing: T1 for sibling sharing → T2 for project persistence → T3 for semantic knowledge.
NXTOOLS

cat <<'SEQTHINK'

## Sequential Thinking

Tool: mcp__plugin_nx_sequential-thinking__sequentialthinking
Use for: debugging hypotheses, design choices, plan evaluation, risk assessment.
Params: needsMoreThoughts=true (continue), isRevision=true+revisesThought=N (correct), branchFromThought=N+branchId="alt" (explore).
SEQTHINK

cat <<'OPERATORS'

## Analytical Operators

Agent `analytical-operator` — dispatch via Agent tool with relay containing operation + inputs + params.
Operations: extract (JSON template), summarize (short|detailed|evidence), rank (criterion), compare (criterion), generate (cited text).
Step outputs: T1 scratch tag `query-step,step-N`.
OPERATORS

# Inject current T1 scratch entries
if command -v nx &> /dev/null; then
  T1_ENTRIES=$(nx scratch list 2>/dev/null)
  if [[ -n "$T1_ENTRIES" && "$T1_ENTRIES" != "No scratch entries." ]]; then
    echo ""
    echo "## T1 Scratch (shared session state)"
    echo "$T1_ENTRIES"
    echo ""
  fi
fi
