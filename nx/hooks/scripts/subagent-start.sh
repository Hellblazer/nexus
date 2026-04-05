#!/bin/bash

# SubagentStart Hook — injects storage context + active state for spawned agents.
# Selectively skips sections based on agent task to save tokens.

# --- Agent-type detection via stdin ---
STDIN=$(cat)
TASK_TEXT=$(python3 -c "
import json, sys
try:
    data = json.loads(sys.argv[1])
    text = ' '.join([
        str(data.get('task', '')),
        str(data.get('prompt', '')),
    ]).lower()
    print(text)
except: print('')
" "$STDIN" 2>/dev/null)

# Classify agent purpose
SKIP_STORAGE_DOCS=0
SKIP_T2_SCAN=0
SKIP_OPERATORS=0

if echo "$TASK_TEXT" | grep -qiE "refactor|rename.*symbol|find.*method|type.hierarch|navigate.code"; then
    # Code-nav agents don't need storage docs or operators
    SKIP_STORAGE_DOCS=1
    SKIP_OPERATORS=1
elif echo "$TASK_TEXT" | grep -qiE "code.review|review.code|lint|style.check"; then
    # Code review agents don't need storage docs or operators
    SKIP_STORAGE_DOCS=1
    SKIP_OPERATORS=1
fi

# T2 memory for active project
if [[ $SKIP_T2_SCAN -eq 0 ]] && command -v git &> /dev/null; then
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

if [[ $SKIP_STORAGE_DOCS -eq 0 ]]; then
cat <<'NXTOOLS'

## nx Storage Tools

All results from search/list tools are **paged**. Response footer shows `Next page: offset=N`. Re-call with that offset to get more.

T1 scratch — session-scoped, shared across all sibling agents:
  Tool: mcp__plugin_nx_nexus__scratch
    scratch(action="put", content="...", tags="hypothesis,decision")
    scratch(action="search", query="...", limit=5)
    scratch(action="list")
    scratch(action="get", entry_id="...")
    scratch(action="delete", entry_id="...")
  Tool: mcp__plugin_nx_nexus__scratch_manage
    scratch_manage(action="flag", entry_id="...", project="...", title="...")

T2 memory — project-scoped, persistent:
  Tool: mcp__plugin_nx_nexus__memory_get
    memory_get(project="...", title="...")       read one entry
    memory_get(project="...", title="")          list titles only
  Tool: mcp__plugin_nx_nexus__memory_search
    memory_search(query="...", project="...", limit=20, offset=0)
  Tool: mcp__plugin_nx_nexus__memory_put
    memory_put(content="...", project="...", title="...", ttl=30)
  Tool: mcp__plugin_nx_nexus__memory_delete
    memory_delete(project="...", title="...")

T3 knowledge — permanent, semantic search:
  Tool: mcp__plugin_nx_nexus__search
    search(query="...", corpus="knowledge", limit=10, offset=0, where="bib_year>=2023")
  Tool: mcp__plugin_nx_nexus__query
    query(question="...", corpus="knowledge", where="bib_year>=2020", limit=10)  → document-level results
  Tool: mcp__plugin_nx_nexus__store_list
    store_list(collection="knowledge", limit=20, offset=0)
    store_list(collection="knowledge__art", docs=true)   → document-level view
  Tool: mcp__plugin_nx_nexus__store_get
    store_get(doc_id="...", collection="knowledge")
  Tool: mcp__plugin_nx_nexus__store_put
    store_put(content="...", collection="knowledge", title="...", tags="...")
  Tool: mcp__plugin_nx_nexus__store_delete
    store_delete(doc_id="...", collection="knowledge")
  Tool: mcp__plugin_nx_nexus__collection_list
  Tool: mcp__plugin_nx_nexus__collection_info
    collection_info(name="knowledge__art")

Plan library (T2):
  Tool: mcp__plugin_nx_nexus__plan_search
    plan_search(query="...", project="...", limit=5)
  Tool: mcp__plugin_nx_nexus__plan_save
    plan_save(query="...", plan_json="...", project="...", tags="...")

Routing: T1 for sibling sharing → T2 for project persistence → T3 for semantic knowledge.
NXTOOLS
fi

cat <<'SEQTHINK'

## Sequential Thinking

Tool: mcp__plugin_nx_sequential-thinking__sequentialthinking
Use for: debugging hypotheses, design choices, plan evaluation, risk assessment.
Params: needsMoreThoughts=true (continue), isRevision=true+revisesThought=N (correct), branchFromThought=N+branchId="alt" (explore).
SEQTHINK

if [[ $SKIP_OPERATORS -eq 0 ]]; then
cat <<'OPERATORS'

## Analytical Operators

Agent `analytical-operator` — dispatch via Agent tool with relay containing operation + inputs + params.
Operations: extract (JSON template), summarize (short|detailed|evidence), rank (criterion), compare (criterion), generate (cited text).
Step outputs: T1 scratch tag `query-step,step-N`.
OPERATORS
fi

# Catalog awareness — inject only for catalog-relevant tasks
if echo "$TASK_TEXT" | grep -qiE "author|cit(e|ation|es|ed)|who wrote|what did.*write|papers (by|about)|provenance|corpus|collection|tumbler|what research|informed by|based on|relationship|links (from|to)"; then
  CATALOG_PATH="${NEXUS_CATALOG_PATH:-$HOME/.config/nexus/catalog}"
  if [[ -d "$CATALOG_PATH/.git" && -f "$CATALOG_PATH/documents.jsonl" ]]; then
    cat <<'CATALOG'

## Catalog (RDR-049)

Use catalog tools for metadata-first queries (author, corpus, title, citations, provenance).
The `/nx:query` skill handles full catalog-aware plan execution.

  Tool: mcp__plugin_nx_nexus__catalog_search
    catalog_search(query="...", author="...", corpus="...", owner="...", file_path="...", content_type="...")
  Tool: mcp__plugin_nx_nexus__catalog_show
    catalog_show(tumbler="1.2.5")
  Tool: mcp__plugin_nx_nexus__catalog_links
    catalog_links(tumbler="1.2.5", direction="in", link_type="cites", depth=2)
  Tool: mcp__plugin_nx_nexus__catalog_resolve
    catalog_resolve(owner="1.1", corpus="schema-evolution")
  Tool: mcp__plugin_nx_nexus__catalog_stats
CATALOG
  fi
fi

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
