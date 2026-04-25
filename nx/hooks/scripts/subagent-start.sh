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

# Relay format — inline (was: awk-truncated RELAY_TEMPLATE.md). Keep compact.
cat <<'RELAY'

## Relay Format (Required Fields)

| Field | Description |
|-------|-------------|
| Task | 1-2 sentence summary |
| Bead | Bead ID with status, or 'none' |
| Input Artifacts | nx store, nx memory, nx scratch, files |
| Deliverable | What the agent should produce |
| Quality Criteria | Checkbox list |

Relays are constructed by the caller. Subagents output "Recommended Next Step" blocks for the caller to use.
RELAY

# Serena + Context7 guidance injected by sn plugin (sn/hooks/scripts/mcp-inject.sh).

if [[ $SKIP_STORAGE_DOCS -eq 0 ]]; then
cat <<'NXTOOLS'

## nx Storage Tools

Results from search/list are paged — footer shows `offset=N` for next page.

T1 scratch (session-scoped, shared across siblings):
  mcp__plugin_nx_nexus__scratch(action="put|search|list|get|delete", content, tags, query, limit, entry_id)
  mcp__plugin_nx_nexus__scratch_manage(action="flag", entry_id, project, title)

T2 memory (project-scoped, persistent):
  mcp__plugin_nx_nexus__memory_get(project, title="" → list titles only)
  mcp__plugin_nx_nexus__memory_search(query, project, limit=20, offset=0)
  mcp__plugin_nx_nexus__memory_put(content, project, title, ttl=30)
  mcp__plugin_nx_nexus__memory_delete(project, title)

T3 knowledge (permanent, semantic search):
  mcp__plugin_nx_nexus__search(query, corpus="knowledge", limit=10, offset=0, where, cluster_by, topic)
    where: "section_type!=references" filters noise; cluster_by="semantic" groups; topic="Label" pre-filters
  mcp__plugin_nx_nexus__query(question, corpus, where, limit, author, content_type, follow_links="cites", depth=1, subtree)
    document-level, catalog-aware, taxonomy-boosted; subtree="1.1" = all descendants of tumbler prefix
  mcp__plugin_nx_nexus__store_list(collection, limit=20, offset=0, docs=false)
  mcp__plugin_nx_nexus__store_get(doc_id, collection)
  mcp__plugin_nx_nexus__store_put(content, collection, title, tags)
    AUTO-LINKS via T1 scratch tag "link-context"; self-seed via catalog_search → scratch put if absent.
    Findings not stored are findings lost — call before returning.
  mcp__plugin_nx_nexus__collection_list

Plan library (T2):
  mcp__plugin_nx_nexus__plan_search(query, project, limit=5)
  mcp__plugin_nx_nexus__plan_save(query, plan_json, project, tags, ttl=30)

Routing: T1 (sibling sharing) → T2 (project persistence) → T3 (semantic knowledge).
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

## Analytical Operators (RDR-080)

Five MCP tools wrap the five operations — each spawns `claude -p` (default timeout 120s). Call directly; no Agent dispatch.

  mcp__plugin_nx_nexus__operator_summarize(content, cited=True)
  mcp__plugin_nx_nexus__operator_extract(inputs=[...], fields="title,year,author")
  mcp__plugin_nx_nexus__operator_rank(items=[...], criterion)
  mcp__plugin_nx_nexus__operator_compare(items=[...], focus)
  mcp__plugin_nx_nexus__operator_generate(template, context)

For plan-matched multi-step retrieval use mcp__plugin_nx_nexus__nx_answer — it composes search/query/operators under a plan-match-first gate.
OPERATORS
fi

# Catalog awareness — inject only for catalog-relevant tasks
if echo "$TASK_TEXT" | grep -qiE "author|cit(e|ation|es|ed)|who wrote|what did.*write|papers? (by|about)|provenance|corpus|collection|tumbler|what research|informed by|based on|relationship|links? (from|to)|referenc|follow.on|build.on|what (implements|supersedes)|link (audit|query|graph)|orphan|catalog|rdr.*(close|accept|show|gate|research)|close.*rdr|accept.*rdr|supersed|consolidat|tidy|knowledge|research|synthesiz|archive|store_put|store put|debug.*finding|root.cause|prevention.pattern|architecture.*map|pattern.*catalog|architect.*decision|risk.assess|insight.*developer|analysis.*deep|analyz.*codebas"; then
  CATALOG_PATH="${NEXUS_CATALOG_PATH:-$HOME/.config/nexus/catalog}"
  if [[ -d "$CATALOG_PATH/.git" && -f "$CATALOG_PATH/documents.jsonl" ]]; then
    cat <<'CATALOG'

## Catalog — Document Registry + Link Graph

Catalog tools for metadata-first queries (author, corpus, title, citations, provenance). /nx:query handles full catalog-aware plan execution.

  mcp__plugin_nx_nexus-catalog__search(query, author, corpus, owner, file_path, content_type)
  mcp__plugin_nx_nexus-catalog__show(tumbler) — full entry with links_from + links_to
  mcp__plugin_nx_nexus-catalog__links(tumbler, direction="in", link_type="cites", depth=2)
    → {"nodes":[CatalogEntry], "edges":[link]}; live documents only (use link_query for orphans)
  mcp__plugin_nx_nexus-catalog__link(from_tumbler, to_tumbler, link_type, created_by, from_span, to_span)
    spans: "chash:<sha256hex>" preferred (content-addressed); "chash:<sha256hex>:<start>-<end>" sub-chunk;
    fallback "L-L" line range or "C:S-E" chunk:char (positional, may go stale).
    chash from search result chunk_text_hash metadata.
  mcp__plugin_nx_nexus-catalog__link_query(link_type, created_by, created_at_before, limit=50) — admin/audit, all links
  mcp__plugin_nx_nexus-catalog__resolve(owner, corpus) — → collection names

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
