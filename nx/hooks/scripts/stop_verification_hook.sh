#!/bin/bash
# Stop verification hook — advisory warnings for uncommitted changes and open beads.
# Never blocks — warns only. The PreToolUse close gate handles hard enforcement.
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

# ---------------------------------------------------------------------------
# Read config
# ---------------------------------------------------------------------------

PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "$0")/../../.." 2>/dev/null && pwd)}"
CONFIG=$(python3 "$PLUGIN_ROOT/hooks/scripts/read_verification_config.py" 2>/dev/null || echo '{}')

ON_STOP=$(printf '%s' "$CONFIG" | python3 -c "import json,sys; print(json.load(sys.stdin).get('on_stop', False))" 2>/dev/null || echo "False")
if [[ "$ON_STOP" != "True" ]]; then
    approve
fi

# ---------------------------------------------------------------------------
# Run checks (advisory only — never blocks)
# ---------------------------------------------------------------------------

WARNINGS=""

# Check 1: Uncommitted changes
if command -v git &>/dev/null; then
    GIT_STATUS=$(git status --porcelain 2>/dev/null || echo "")
    if [[ -n "$GIT_STATUS" ]]; then
        WARNINGS="${WARNINGS}WARNING: Uncommitted changes detected — consider committing before ending session\n"
    fi
fi

# Check 2: Open beads
if command -v bd &>/dev/null; then
    BEADS_OUTPUT=$(bd list --status=in_progress 2>/dev/null || echo "")
    if [[ -n "$BEADS_OUTPUT" ]] && printf '%s' "$BEADS_OUTPUT" | grep -q "in_progress"; then
        WARNINGS="${WARNINGS}WARNING: Beads still in progress — consider closing or deferring before ending session\n"
    fi
fi

if [[ -n "$WARNINGS" ]]; then
    approve "$(printf '%b' "$WARNINGS")"
else
    approve
fi
