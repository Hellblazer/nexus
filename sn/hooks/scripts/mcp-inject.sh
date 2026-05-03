#!/bin/bash

# sn SubagentStart hook — inject Serena + Context7 MCP tool guidance
# Selectively injects based on agent task text to save tokens.
# Default: inject both (safe fallback on parse failure).
# Timeout: 5s (hooks.json) — stdin read + python3 ~50ms, well within budget.

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
