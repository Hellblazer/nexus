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

# Catalog link context for files mentioned in the task (always, even for code-nav agents)
if command -v nx &> /dev/null; then
  # Extract file paths from the task text and show linked RDRs
  FILE_PATHS=$(python3 -c "
import re, sys
text = sys.argv[1] if len(sys.argv) > 1 else ''
# Match patterns like src/nexus/foo.py or docs/rdr/bar.md
paths = re.findall(r'(?:src|tests|docs|nx)/[\w/.-]+\.\w+', text)
for p in set(paths[:5]):  # cap at 5 to keep it fast
    print(p)
" "$TASK_TEXT" 2>/dev/null)

  if [[ -n "$FILE_PATHS" ]]; then
    LINK_OUT=""
    while IFS= read -r fp; do
      LINKS=$(nx catalog links-for-file "$fp" 2>/dev/null | grep -E '^\s+[←→]')
      if [[ -n "$LINKS" ]]; then
        LINK_OUT+="  $fp:"$'\n'"$LINKS"$'\n'
      fi
    done <<< "$FILE_PATHS"
    if [[ -n "$LINK_OUT" ]]; then
      echo ""
      echo "## Linked RDRs (files in task)"
      echo "$LINK_OUT"
    fi
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
    search(query="...", corpus="knowledge", limit=10, offset=0, where="bib_year>=2023", cluster_by="", topic="")
    → where: section_type!=references filters noise. cluster_by="semantic" groups by topic. topic="Label" pre-filters to a topic cluster
  Tool: mcp__plugin_nx_nexus__query
    query(question="...", corpus="knowledge", where="bib_year>=2020", limit=10,
          author="", content_type="", follow_links="cites", depth=1, subtree="1.1")
    → document-level results; catalog params scope search; taxonomy-boosted ranking
    → subtree: all descendants of tumbler prefix (e.g. "1.1" = all nexus docs)
    → follow_links: enrich results with linked docs (any link type)
  Tool: mcp__plugin_nx_nexus__store_list
    store_list(collection="knowledge", limit=20, offset=0)
    store_list(collection="knowledge__art", docs=true)   → document-level view
  Tool: mcp__plugin_nx_nexus__store_get
    store_get(doc_id="...", collection="knowledge")
  Tool: mcp__plugin_nx_nexus__store_put
    store_put(content="...", collection="knowledge", title="...", tags="...")
    AUTO-LINKING: store_put checks T1 scratch for link-context (tag: "link-context") and auto-creates catalog links.
    If no link-context in scratch, self-seed: catalog_search your task references → scratch put with targets.
    You MUST call store_put before returning — findings not stored are findings lost.
  Tool: mcp__plugin_nx_nexus__collection_list

Plan library (T2):
  Tool: mcp__plugin_nx_nexus__plan_search
    plan_search(query="...", project="...", limit=5)
  Tool: mcp__plugin_nx_nexus__plan_save
    plan_save(query="...", plan_json="...", project="...", tags="...", ttl=30)

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
if echo "$TASK_TEXT" | grep -qiE "author|cit(e|ation|es|ed)|who wrote|what did.*write|papers? (by|about)|provenance|corpus|collection|tumbler|what research|informed by|based on|relationship|links? (from|to)|referenc|follow.on|build.on|what (implements|supersedes)|link (audit|query|graph)|orphan|catalog|rdr.*(close|accept|show|gate|research)|close.*rdr|accept.*rdr|supersed|consolidat|tidy|knowledge|research|synthesiz|archive|store_put|store put|debug.*finding|root.cause|prevention.pattern|architecture.*map|pattern.*catalog|architect.*decision|risk.assess|insight.*developer|analysis.*deep|analyz.*codebas"; then
  CATALOG_PATH="${NEXUS_CATALOG_PATH:-$HOME/.config/nexus/catalog}"
  if [[ -d "$CATALOG_PATH/.git" && -f "$CATALOG_PATH/documents.jsonl" ]]; then
    cat <<'CATALOG'

## Catalog — Document Registry + Link Graph

Use catalog tools for metadata-first queries: author, corpus, title, citations, provenance, references.
The `/nx:query` skill handles full catalog-aware plan execution.

  mcp__plugin_nx_nexus-catalog__search(query="...", author="...", corpus="...", owner="...", file_path="...", content_type="...")
  mcp__plugin_nx_nexus-catalog__show(tumbler="1.2.5")  — full entry with links_from + links_to
  mcp__plugin_nx_nexus-catalog__links(tumbler="1.2.5", direction="in", link_type="cites", depth=2)
    Returns {"nodes": [CatalogEntry dicts], "edges": [link dicts]}.
    Only live documents — deleted nodes excluded. Use mcp__plugin_nx_nexus-catalog__link_query for all links.
  mcp__plugin_nx_nexus-catalog__link(from_tumbler="...", to_tumbler="...", link_type="cites", created_by="agent-name",
    from_span="chash:<sha256hex>", to_span="chash:<sha256hex>")
    Accepts titles or tumblers. Returns {"from", "to", "type", "created": true/false}.
    SPAN FORMAT (preferred): "chash:<64-char-hex>" — content-addressed, survives re-indexing.
    Sub-chunk precision: "chash:<64-char-hex>:<start>-<end>" — character range within a chunk.
    Get chunk hashes from search result metadata: each chunk has chunk_text_hash field.
    Fallback spans: "42-57" (line range) or "3:100-250" (chunk:char) — positional, may go stale.
  mcp__plugin_nx_nexus-catalog__link_query(link_type="cites", created_by="bib_enricher", created_at_before="...", limit=50)
    All links including orphans. Admin/audit — not a planner step.
  mcp__plugin_nx_nexus-catalog__resolve(owner="1.1", corpus="schema-evolution")  — → collection names
  Link types: cites, implements-heuristic, supersedes, quotes, relates, comments, implements
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
