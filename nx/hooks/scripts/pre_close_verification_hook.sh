#!/bin/bash
# PreToolUse close verification hook — gates bd close/done on test pass.
# Exit 0 with hookSpecificOutput JSON for structured decisions.
# Exit 2 is the alternative blocking path (stderr fed to Claude as error).
# SPDX-License-Identifier: AGPL-3.0-or-later

set -euo pipefail

# ---------------------------------------------------------------------------
# Helpers — PreToolUse uses hookSpecificOutput, NOT decision/reason
# ---------------------------------------------------------------------------

allow() {
    # permissionDecision: "allow" — tool call proceeds
    if [[ -n "${1:-}" ]]; then
        local escaped
        escaped=$(printf '%s' "$1" | python3 -c "import json,sys; print(json.dumps(sys.stdin.read()))" 2>/dev/null || printf '"%s"' "$1")
        printf '{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow", "additionalContext": %s}}\n' "$escaped"
    else
        printf '{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"}}\n'
    fi
    exit 0
}

deny() {
    # permissionDecision: "deny" — tool call blocked, reason fed to Claude
    local escaped
    escaped=$(printf '%s' "$1" | python3 -c "import json,sys; print(json.dumps(sys.stdin.read()))" 2>/dev/null || printf '"%s"' "$1")
    printf '{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": %s}}\n' "$escaped"
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
CONFIG_SCRIPT="$PLUGIN_ROOT/hooks/scripts/read_verification_config.py"

CONFIG=$(python3 "$CONFIG_SCRIPT" 2>/dev/null || true)

if [[ -z "$CONFIG" ]]; then
    # Config reader failed — fail open
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

TEST_CMD=$(printf '%s' "$CONFIG" | python3 -c "
import json, sys
try:
    print(json.load(sys.stdin).get('test_command', ''))
except Exception:
    print('')
" 2>/dev/null || true)

TEST_TIMEOUT=$(printf '%s' "$CONFIG" | python3 -c "
import json, sys
try:
    print(json.load(sys.stdin).get('test_timeout', 120))
except Exception:
    print('120')
" 2>/dev/null || echo "120")

# ---------------------------------------------------------------------------
# Layer 2a: Mechanical gate (blocking)
# ---------------------------------------------------------------------------

ADVISORY_MSGS=""

if [[ -z "$TEST_CMD" ]]; then
    ADVISORY_MSGS="ADVISORY: No test command configured or detected — skipping test check"
else
    TEST_EXIT=0
    if command -v timeout &>/dev/null; then
        timeout "$TEST_TIMEOUT" bash -c "$TEST_CMD" >/dev/null 2>&1 || TEST_EXIT=$?
    else
        bash -c "$TEST_CMD" >/dev/null 2>&1 || TEST_EXIT=$?
    fi

    if [[ $TEST_EXIT -eq 124 ]]; then
        ADVISORY_MSGS="ADVISORY: Test command timed out after ${TEST_TIMEOUT}s — skipping test check"
    elif [[ $TEST_EXIT -eq 126 || $TEST_EXIT -eq 127 ]]; then
        ADVISORY_MSGS="ADVISORY: Test command not found or not executable (exit $TEST_EXIT) — skipping test check"
    elif [[ $TEST_EXIT -ne 0 ]]; then
        deny "Tests failing (exit code $TEST_EXIT) — fix before closing bead ${BEAD_ID:-unknown}"
    fi
fi

# ---------------------------------------------------------------------------
# Layer 2b: Advisory check (non-blocking) — review scratch marker
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

# ---------------------------------------------------------------------------
# Compose allow message
# ---------------------------------------------------------------------------

CONTEXT=""
if [[ -n "$ADVISORY_MSGS" ]]; then
    CONTEXT="$ADVISORY_MSGS"
fi
if [[ -n "$REVIEW_MSG" ]]; then
    if [[ -n "$CONTEXT" ]]; then
        CONTEXT="$CONTEXT | $REVIEW_MSG"
    else
        CONTEXT="$REVIEW_MSG"
    fi
fi

allow "$CONTEXT"
