#!/bin/bash

# sn SubagentStart hook — inject Serena + Context7 MCP tool guidance
# Selectively injects based on agent task text to save tokens.
# Default: inject both (safe fallback on parse failure).
# Timeout: 5s (hooks.json) — stdin read + python3 ~50ms, well within budget.
#
# DELIVERY CONTRACT (Claude Code SubagentStart): emit content via the JSON
# envelope of the form
#   {"hookSpecificOutput": {"hookEventName": "SubagentStart",
#                            "additionalContext": "<text>"}}
# Plain stdout was the prior shape and once worked, but the JSON envelope is
# the documented schema — it makes the emit intent unambiguous, so a Claude
# Code change that tightens parsing won't silently drop the content. This
# mirrors nx/hooks/scripts/subagent-start.sh, which migrated 2026-05-05
# (commit 68854ca). The sn plugin missed that migration; restoring it here
# is what gets Serena + Context7 setup back into spawned subagents.
#
# Implementation: capture all body stdout into a tempfile via FD redirection
# at the top of the script, then emit the JSON envelope at the end via an
# EXIT trap. Body code below stays unchanged and continues to use cat for
# content generation.

_SN_HOOK_OUTBUF=$(mktemp -t sn-subagent-start.XXXXXX) || _SN_HOOK_OUTBUF=""
if [[ -n "$_SN_HOOK_OUTBUF" ]]; then
    exec 3>&1 1>"$_SN_HOOK_OUTBUF"
fi
_sn_emit_json_envelope() {
    local rc=$?
    if [[ -n "$_SN_HOOK_OUTBUF" ]]; then
        exec 1>&3 3>&-
        python3 -c '
import json, sys
print(json.dumps({
    "hookSpecificOutput": {
        "hookEventName": "SubagentStart",
        "additionalContext": sys.stdin.read(),
    },
}))
' < "$_SN_HOOK_OUTBUF"
        rm -f "$_SN_HOOK_OUTBUF"
    fi
    return $rc
}
trap _sn_emit_json_envelope EXIT

# --- Agent-type detection via stdin JSON ---
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

SKIP_SERENA=0
SKIP_CONTEXT7=0

if echo "$TASK_TEXT" | grep -qiE "research|synthesize|audit|survey|deep.anal|investigate|knowledge.tid"; then
    # Pure research agents don't need code nav or library docs
    SKIP_SERENA=1
    SKIP_CONTEXT7=1
elif echo "$TASK_TEXT" | grep -qiE "library|framework|api.doc|context7|package|dependency|migrate"; then
    # Library-focused agents don't need code nav
    SKIP_SERENA=1
elif echo "$TASK_TEXT" | grep -qiE "refactor|rename.*symbol|find.*method|find.*class|type.hierarch|navigate.code"; then
    # Code-nav agents don't need library docs
    SKIP_CONTEXT7=1
fi

# Section bodies live in sibling .md files rather than heredocs.
# Bash here-docs hang in some non-interactive shell contexts (Claude Code
# harness, test subprocess fixtures) where the parent's stdin is wired to
# a pipe the here-doc machinery never closes — symptom is rc=124 timeout
# with empty output. Reading from a real file via cat has no such
# dependency, and the markdown stays editable as markdown.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ $SKIP_SERENA -eq 0 ]]; then
    cat "$SCRIPT_DIR/serena-section.md"
fi

if [[ $SKIP_CONTEXT7 -eq 0 ]]; then
    cat "$SCRIPT_DIR/context7-section.md"
fi
