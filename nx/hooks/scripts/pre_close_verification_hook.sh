#!/bin/bash
# PreToolUse close verification hook — advisory checks on bd close/done.
# Checks for review scratch marker. Never blocks, never runs tests.
# Exit 0 always with hookSpecificOutput JSON.
# SPDX-License-Identifier: AGPL-3.0-or-later

# No set -e/-u/-o pipefail — this hook must NEVER fail.
# Every code path must produce valid JSON on stdout and exit 0.

# ---------------------------------------------------------------------------
# Helpers — PreToolUse uses hookSpecificOutput, NOT decision/reason
# ---------------------------------------------------------------------------

allow() {
    if [[ -n "${1:-}" ]]; then
        local escaped
        escaped=$(printf '%s' "$1" | python3 -c "import json,sys; print(json.dumps(sys.stdin.read()))" 2>/dev/null || printf '"%s"' "$1")
        printf '{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow", "additionalContext": %s}}\n' "$escaped"
    else
        printf '{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"}}\n'
    fi
    exit 0
}

# ---------------------------------------------------------------------------
# Read stdin
# ---------------------------------------------------------------------------

STDIN=$(cat 2>/dev/null || true)

if [[ -z "$STDIN" ]]; then
    allow
fi

# ---------------------------------------------------------------------------
# Fast no-op: check tool_name
# ---------------------------------------------------------------------------

TOOL_NAME=$(printf '%s' "$STDIN" | python3 -c "
import json, sys
try:
    print(json.load(sys.stdin).get('tool_name', ''))
except Exception:
    print('')
" 2>/dev/null || true)

if [[ "$TOOL_NAME" != "Bash" ]]; then
    allow
fi

# ---------------------------------------------------------------------------
# Fast no-op: check if command matches bd close/done
# ---------------------------------------------------------------------------

TOOL_INPUT=$(printf '%s' "$STDIN" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    print(d.get('tool_input', {}).get('command', ''))
except Exception:
    print('')
" 2>/dev/null || true)

if ! printf '%s' "$TOOL_INPUT" | grep -qE '\bbd[[:space:]]+(close|done)\b'; then
    allow
fi

# ---------------------------------------------------------------------------
# Extract bead ID (first token after close/done)
# ---------------------------------------------------------------------------

BEAD_ID=$(printf '%s' "$TOOL_INPUT" | sed -E -n 's/.*bd[[:space:]]+(close|done)[[:space:]]+([^[:space:]]*).*/\2/p' 2>/dev/null || true)
BEAD_ID="${BEAD_ID:-}"

# ---------------------------------------------------------------------------
# Read verification config
# ---------------------------------------------------------------------------

PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "$0")/../../.." 2>/dev/null && pwd)}"
CONFIG=$(python3 "$PLUGIN_ROOT/hooks/scripts/read_verification_config.py" 2>/dev/null || true)

if [[ -z "$CONFIG" ]]; then
    allow
fi

ON_CLOSE=$(printf '%s' "$CONFIG" | python3 -c "
import json, sys
try:
    print(json.load(sys.stdin).get('on_close', False))
except Exception:
    print('False')
" 2>/dev/null || echo "False")

if [[ "$ON_CLOSE" != "True" ]]; then
    allow
fi

# ---------------------------------------------------------------------------
# Advisory: review scratch marker check (never blocks)
# ---------------------------------------------------------------------------

REVIEW_MSG=""
if command -v nx &>/dev/null; then
    SCRATCH_RESULTS=$(nx scratch list 2>/dev/null || true)
    if [[ -n "$BEAD_ID" ]]; then
        if [[ -n "$SCRATCH_RESULTS" ]] && \
           printf '%s' "$SCRATCH_RESULTS" | grep -q "review-completed" && \
           printf '%s' "$SCRATCH_RESULTS" | grep -q "$BEAD_ID"; then
            REVIEW_MSG="Review completed for $BEAD_ID."
        else
            REVIEW_MSG="ADVISORY: No review marker found for $BEAD_ID — consider running /nx:review-code before closing."
        fi
    fi
fi

# ---------------------------------------------------------------------------
# Audit trail
# ---------------------------------------------------------------------------

if [[ -n "$BEAD_ID" ]] && command -v bd &>/dev/null; then
    bd set-state "$BEAD_ID" verification=passed 2>/dev/null || true
fi

allow "$REVIEW_MSG"
