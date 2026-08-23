#!/bin/bash
# Stop verification hook — advisory warnings for uncommitted changes and open beads.
# Never blocks — warns only. The PreToolUse close gate handles hard enforcement.
# Exit 0 always. Communicate via JSON stdout.
# SPDX-License-Identifier: AGPL-3.0-or-later

# No set -e/-u/-o pipefail — this hook must NEVER fail.
# Every code path must produce valid JSON on stdout and exit 0.

PAYLOAD="$(cat 2>/dev/null)"

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

# ---------------------------------------------------------------------------
# RDR-184 expectations-ledger reconciliation (bead nexus-2v0v7, epic
# nexus-qkbo7). WARN-ONLY, unconditionally: this can never turn the hook's
# decision into "block" and runs independent of the on_stop verification
# toggle just below — it is a distinct RDR-184 concern, not part of that
# feature. Gated on NX_ORCH_STOP_GUARD (same gate as the rest of the
# expectations ledger machinery: subagent-stop.sh, agent-dispatch-expect.sh)
# so a session that opted the whole guard off does not pay for this either.
# Every failure path here (missing lib, no session_id, expectations_reconcile
# absent/erroring, rc other than 4) leaves RECONCILE_WARNING empty and never
# touches the hook's exit status.
# shellcheck source=./expectations.sh disable=SC1091
source "$PLUGIN_ROOT/hooks/scripts/expectations.sh" 2>/dev/null

RECONCILE_WARNING=""
GUARD_MODE="${NX_ORCH_STOP_GUARD:-block}"
if [[ "$GUARD_MODE" == "observe" || "$GUARD_MODE" == "block" ]] && command -v expectations_reconcile >/dev/null 2>&1; then
    STOP_SESSION_ID="$(printf '%s' "$PAYLOAD" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    d = {}
print(str(d.get("session_id") or "") if isinstance(d, dict) else "")
' 2>/dev/null)"
    if [[ -n "$STOP_SESSION_ID" ]]; then
        RECONCILE_OUT="$(expectations_reconcile "$STOP_SESSION_ID" "$PAYLOAD" 2>/dev/null)"
        if [[ $? -eq 4 ]]; then
            RECONCILE_WARNING="WARNING: expectations ledger reconciliation found background agent(s) the ledger still lists as outstanding but the harness no longer tracks (nexus-2v0v7) -- possible silent death, verify: ${RECONCILE_OUT//$'\n'/ | }\n"
        fi
    fi
fi

ON_STOP=$(python3 -c "import json,sys; print(json.loads(sys.argv[1]).get('on_stop', False))" "$CONFIG" 2>/dev/null || echo "False")
if [[ "$ON_STOP" != "True" ]]; then
    approve "$(printf '%b' "$RECONCILE_WARNING")"
fi

# ---------------------------------------------------------------------------
# Run checks (advisory only — never blocks)
# ---------------------------------------------------------------------------

WARNINGS="$RECONCILE_WARNING"

# Check 1: Uncommitted changes
if command -v git &>/dev/null; then
    GIT_STATUS=$(git status --porcelain 2>/dev/null || echo "")
    if [[ -n "$GIT_STATUS" ]]; then
        WARNINGS="${WARNINGS}WARNING: Uncommitted changes detected — consider committing before ending session\n"
    fi
fi

# Check 2: Catalog sync (auto-commit + push if remote configured)
if command -v nx &>/dev/null; then
    CATALOG_PATH="${NEXUS_CATALOG_PATH:-$HOME/.config/nexus/catalog}"
    if [[ -d "$CATALOG_PATH/.git" && -f "$CATALOG_PATH/documents.jsonl" ]]; then
        # Check for uncommitted JSONL changes
        # grep -c exits 1 on zero matches; || echo "0" catches both that and pipe failures
        CATALOG_DIRTY=$(git -C "$CATALOG_PATH" status --porcelain 2>/dev/null | grep -c "\.jsonl" || echo "0")
        if [[ "$CATALOG_DIRTY" -gt 0 ]]; then
            nx catalog sync -m "auto-sync at session close" >/dev/null 2>&1 || true
        fi
    fi
fi

# Check 3: Open beads
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
