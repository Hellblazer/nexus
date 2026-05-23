#!/bin/bash

# SubagentStart Hook — injects storage context + active state for spawned agents.
# Selectively skips sections based on agent task to save tokens.
#
# DELIVERY CONTRACT (Claude Code SubagentStart): Both plain stdout and a JSON
# envelope of the form
#   {"hookSpecificOutput": {"hookEventName": "SubagentStart",
#                            "additionalContext": "<text>"}}
# inject content into the spawned subagent's initial context (verified by
# tests/cc-validation/scenarios/13_disambiguate_subagent_inject.sh: 13a/13b/
# 13c all pass). The JSON envelope is the explicit contract — it makes the
# emit intent unambiguous and tracks the documented schema, so a future
# Claude Code change that tightens parsing won't silently drop the content.
#
# Implementation: capture all body stdout into a tempfile via FD redirection
# at the top of the script, then emit the JSON envelope at the end via an
# EXIT trap. Body code below stays unchanged and continues to use echo /
# cat heredocs for content generation.

_NX_HOOK_OUTBUF=$(mktemp -t nx-subagent-start.XXXXXX) || _NX_HOOK_OUTBUF=""
if [[ -n "$_NX_HOOK_OUTBUF" ]]; then
    exec 3>&1 1>"$_NX_HOOK_OUTBUF"
fi
_nx_emit_json_envelope() {
    local rc=$?
    if [[ -n "$_NX_HOOK_OUTBUF" ]]; then
        exec 1>&3 3>&-
        python3 -c '
import json, sys
print(json.dumps({
    "hookSpecificOutput": {
        "hookEventName": "SubagentStart",
        "additionalContext": sys.stdin.read(),
    },
}))
' < "$_NX_HOOK_OUTBUF"
        rm -f "$_NX_HOOK_OUTBUF"
    fi
    return $rc
}
trap _nx_emit_json_envelope EXIT

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
# Heredoc bodies kept under 500 bytes each — bash 5.3.x deadlocks on
# larger heredocs when the child writes the body to a pipe before
# exec'ing cat (write blocks waiting for a reader that hasn't started
# yet). /bin/bash 3.2 uses temp files so it's unaffected, but launchers
# that pick homebrew bash via PATH lookup hang the whole subagent
# dispatch. Splitting also trims redundant param signatures the agent
# already sees in the live MCP tool list.
cat <<'NX_TIERS'

## nx storage (call as mcp__plugin_conexus_nexus__<tool>; paged: footer shows offset=N)

Read widest -> narrowest BEFORE any work; check before duplicating effort:
  T3 search/nx_answer  all sessions/projects   <- check before researching
  T2 memory            project-scoped          <- check before project work
  T1 scratch           shared with siblings    <- check before duplicating sibling work
NX_TIERS

cat <<'NX_TOOLS'

T1: scratch, scratch_manage
T2: memory_get, memory_search, memory_put, memory_delete
NX_TOOLS

cat <<'NX_T3'
T3: search (where, cluster_by, topic), query (catalog-aware; follow_links, depth, subtree),
    store_list, store_get, store_put, collection_list
Plans (T2): plan_search, plan_save
Hint: where="section_type!=references" filters noise.

Verb-shape question (how / why / tradeoffs / compare)? -> nx_answer (composed > raw).
Raw search is for keyword lookup only ("find X in collection Y").

WRITE-BACK: findings not stored = findings lost. store_put (T3) or memory_put (T2) before returning.
NX_T3

cat <<'NX_AUTOLINK'

AUTO-LINK RECIPE (drives store_put -> catalog links):
  1. catalog_search your task references -> get target tumblers
  2. scratch put (tag: "link-context") with target tumblers
  3. store_put -- auto-creates catalog links from scratch context

If link-context already in scratch (sibling agent did 1+2), skip to step 3.

AGENT TAG: pass agent="<your-role>" to memory_put so nx tier-status slices writes by agent (nexus-9clx).
NX_AUTOLINK

# Phase boundary enforcement: inject for tasks that close, review, or
# audit phase work. Hard-enforcement preamble at gate invocation; this
# block tells the subagent the gate exists and when to invoke it.
if echo "$TASK_TEXT" | grep -qiE "close.*phase|phase.*clos|phase.*review|review.*gate|approach.*cross.walk|cross.walk.*approach|phase.*close|closeout|rdr.*phase|phase.*rdr|silent.scope|scope.reduc"; then
cat <<'PHASE_GATE'

## Phase Boundary Gate (mandatory)

If your task closes a phase-review bead, run `/conexus:phase-review-gate <rdr-id> --phase N` BEFORE close. Pass 1 enumerates §Approach items; Pass 2 validates each has a closing-bead pointer (`ItemN=nexus-xxxx`) or explicit `none`. BLOCKED on any unaccounted item; phase close is gated on PASSED. Skipping the gate is the silent-scope-reduction failure mode that cost RDR-112 Phase 1 (nexus-52lb, 2026-05-15) 2-3 days of replanning when T3 daemon was silently dropped and discovered three phases later.
PHASE_GATE
fi

# L1 Knowledge Map (per-repo, RDR-072) — outside the heredoc so $(…) expands
CONTEXT_DIR="$HOME/.config/nexus/context"
REPO_HASH=$(echo -n "$(pwd -P)" | shasum -a 1 | cut -c1-8)
REPO_NAME=$(basename "$(pwd -P)")
CONTEXT_FILE="$CONTEXT_DIR/${REPO_NAME}-${REPO_HASH}.txt"
if [ ! -f "$CONTEXT_FILE" ]; then
  CONTEXT_FILE="$HOME/.config/nexus/context_l1.txt"
fi
if [ -f "$CONTEXT_FILE" ]; then
  echo ""
  cat "$CONTEXT_FILE"
fi
fi

cat <<'SEQTHINK'

## Sequential Thinking

Tool: mcp__plugin_conexus_sequential-thinking__sequentialthinking
Use for: debugging hypotheses, design choices, plan evaluation, risk assessment.
Params: needsMoreThoughts=true (continue), isRevision=true+revisesThought=N (correct), branchFromThought=N+branchId="alt" (explore).
SEQTHINK

if [[ $SKIP_OPERATORS -eq 0 ]]; then
cat <<'OPERATORS'

## Analytical operators (RDR-080)

5 ops wrap claude -p (120s default). Call directly, no Agent dispatch:
  operator_summarize, operator_extract, operator_rank, operator_compare, operator_generate

Multi-step retrieval (plan-match gate): nx_answer.
OPERATORS
fi

# Catalog awareness — inject only for catalog-relevant tasks
if echo "$TASK_TEXT" | grep -qiE "author|cit(e|ation|es|ed)|who wrote|what did.*write|papers? (by|about)|provenance|corpus|collection|tumbler|what research|informed by|based on|relationship|links? (from|to)|referenc|follow.on|build.on|what (implements|supersedes)|link (audit|query|graph)|orphan|catalog|rdr.*(close|accept|show|gate|research)|close.*rdr|accept.*rdr|supersed|consolidat|tidy|knowledge|research|synthesiz|archive|store_put|store put|debug.*finding|root.cause|prevention.pattern|architecture.*map|pattern.*catalog|architect.*decision|risk.assess|insight.*developer|analysis.*deep|analyz.*codebas"; then
  CATALOG_PATH="${NEXUS_CATALOG_PATH:-$HOME/.config/nexus/catalog}"
  if [[ -d "$CATALOG_PATH/.git" && -f "$CATALOG_PATH/documents.jsonl" ]]; then
    cat <<'CATALOG_TOOLS'

## Catalog - metadata-first queries (author, corpus, citations, provenance)
(call as mcp__plugin_conexus_nexus-catalog__<tool>)

  search, show, resolve
  links (direction="in"|"out", link_type, depth=2 -> nodes+edges; live docs only)
  link (from_tumbler, to_tumbler, link_type, created_by, spans)
  link_query (admin/audit; includes orphans)
CATALOG_TOOLS

    cat <<'CATALOG_LINKING'
Link spans: "chash:<sha256hex>" preferred (content-addressed); ":<start>-<end>" sub-chunk;
fallback "L-L" or "C:S-E" (positional, may go stale). chash via search chunk_text_hash.

Link types: cites, implements, implements-heuristic, supersedes, quotes, relates, comments.

For full plan execution use /conexus:query.
CATALOG_LINKING
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
