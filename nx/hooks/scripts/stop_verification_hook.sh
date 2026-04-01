#!/bin/bash
# Stop verification hook — checks for uncommitted changes, open beads, and test failures.
# Exit 0 always. Communicate via JSON stdout.
# SPDX-License-Identifier: AGPL-3.0-or-later

set -euo pipefail

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

approve() {
    if [[ -n "${1:-}" ]]; then
        local escaped
        escaped=$(printf '%s' "$1" | python3 -c "import json,sys; print(json.dumps(sys.stdin.read()))" 2>/dev/null || printf '"%s"' "$1")
        printf '{"decision": "approve", "reason": %s}\n' "$escaped"
    else
        printf '{"decision": "approve"}\n'
    fi
    exit 0
}

block() {
    local escaped
    escaped=$(printf '%s' "$1" | python3 -c "import json,sys; print(json.dumps(sys.stdin.read()))" 2>/dev/null || printf '"%s"' "$1")
    printf '{"decision": "block", "reason": %s}\n' "$escaped"
    exit 0
}

# ---------------------------------------------------------------------------
# Read stdin
# ---------------------------------------------------------------------------

STDIN=$(cat 2>/dev/null || true)

# ---------------------------------------------------------------------------
# Read config
# ---------------------------------------------------------------------------

PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "$0")/../../.." 2>/dev/null && pwd)}"
CONFIG=$(python3 "$PLUGIN_ROOT/hooks/scripts/read_verification_config.py" 2>/dev/null || echo '{}')

ON_STOP=$(printf '%s' "$CONFIG" | python3 -c "import json,sys; print(json.load(sys.stdin).get('on_stop', False))" 2>/dev/null || echo "False")
if [[ "$ON_STOP" != "True" ]]; then
    approve
fi

TEST_CMD=$(printf '%s' "$CONFIG" | python3 -c "import json,sys; print(json.load(sys.stdin).get('test_command', ''))" 2>/dev/null || echo "")
TEST_TIMEOUT=$(printf '%s' "$CONFIG" | python3 -c "import json,sys; print(json.load(sys.stdin).get('test_timeout', 120))" 2>/dev/null || echo "120")

STOP_HOOK_ACTIVE=$(printf '%s' "$STDIN" | python3 -c "import json,sys; d=json.load(sys.stdin); print(str(d.get('stop_hook_active', False)).lower())" 2>/dev/null || echo "false")

# ---------------------------------------------------------------------------
# Run checks
# ---------------------------------------------------------------------------

FAILURES=""
TEST_FAILED=""
ADVISORY_ONLY="true"

# Check 1: Uncommitted changes
if command -v git &>/dev/null; then
    GIT_STATUS=$(git status --porcelain 2>/dev/null || echo "")
    if [[ -n "$GIT_STATUS" ]]; then
        FAILURES="${FAILURES}Uncommitted changes detected:\n$(printf '%s' "$GIT_STATUS" | head -10)\n\n"
        ADVISORY_ONLY="false"
    fi
fi

# Check 2: Open beads
if command -v bd &>/dev/null; then
    BEADS_OUTPUT=$(bd list --status=in_progress 2>/dev/null || echo "")
    if [[ -n "$BEADS_OUTPUT" ]] && printf '%s' "$BEADS_OUTPUT" | grep -q "in_progress"; then
        FAILURES="${FAILURES}Beads still in progress:\n$(printf '%s' "$BEADS_OUTPUT" | head -5)\n\n"
        ADVISORY_ONLY="false"
    fi
fi

# Check 3: Test suite
if [[ -n "$TEST_CMD" ]]; then
    TEST_EXIT=0
    if command -v timeout &>/dev/null; then
        timeout "$TEST_TIMEOUT" bash -c "$TEST_CMD" >/dev/null 2>&1 || TEST_EXIT=$?
    else
        bash -c "$TEST_CMD" >/dev/null 2>&1 || TEST_EXIT=$?
    fi

    if [[ $TEST_EXIT -eq 124 ]]; then
        FAILURES="${FAILURES}ADVISORY: Test command timed out after ${TEST_TIMEOUT}s — skipping test check\n\n"
    elif [[ $TEST_EXIT -eq 126 || $TEST_EXIT -eq 127 ]]; then
        FAILURES="${FAILURES}ADVISORY: Test command not found or not executable (exit $TEST_EXIT) — skipping test check\n\n"
    elif [[ $TEST_EXIT -ne 0 ]]; then
        TEST_FAILED="true"
        FAILURES="${FAILURES}Tests failing (exit code $TEST_EXIT)\n\n"
        ADVISORY_ONLY="false"
    fi
else
    FAILURES="${FAILURES}ADVISORY: No test command configured or detected — skipping test check\n\n"
fi

# No failures → approve
if [[ -z "$FAILURES" ]]; then
    approve
fi

# Advisory-only failures → approve with advisory text
if [[ "$ADVISORY_ONLY" == "true" ]]; then
    approve "$(printf '%b' "$FAILURES")"
fi

# On retry pass, let test-only failures through
if [[ "$STOP_HOOK_ACTIVE" == "true" ]]; then
    # Re-check mechanical failures (git + beads)
    MECHANICAL_FAILURES=""
    if command -v git &>/dev/null; then
        GIT_STATUS=$(git status --porcelain 2>/dev/null || echo "")
        if [[ -n "$GIT_STATUS" ]]; then
            MECHANICAL_FAILURES="${MECHANICAL_FAILURES}Uncommitted changes still present\n"
        fi
    fi
    if command -v bd &>/dev/null; then
        BEADS_OUTPUT=$(bd list --status=in_progress 2>/dev/null || echo "")
        if [[ -n "$BEADS_OUTPUT" ]] && printf '%s' "$BEADS_OUTPUT" | grep -q "in_progress"; then
            MECHANICAL_FAILURES="${MECHANICAL_FAILURES}Beads still in progress\n"
        fi
    fi

    if [[ -n "$MECHANICAL_FAILURES" ]]; then
        block "$(printf '%b' "$MECHANICAL_FAILURES")"
    fi

    # Test failures on retry → let through with warning
    if [[ -n "$TEST_FAILED" ]]; then
        approve "WARNING: TESTS FAILING — agent could not resolve. Manual intervention needed."
    fi

    # Any remaining (advisory) failures → approve
    approve "$(printf '%b' "$FAILURES")"
fi

# First pass with non-advisory failures → block
block "$(printf '%b' "$FAILURES")"
