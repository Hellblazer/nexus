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

if ! printf '%s' "$TOOL_INPUT" | grep -qE '\bbd[[:space:]]+(close|done|create)\b'; then
    allow
fi

# ---------------------------------------------------------------------------
# bd create branch — commitment-metadata enforcement during active RDR close
# (RDR-065 Gap 3). Audit log: /tmp/nexus-rdr065-bd-create-audit.log (NOT .beads/).
# ---------------------------------------------------------------------------

if printf '%s' "$TOOL_INPUT" | grep -qE '\bbd[[:space:]]+create\b'; then
    AUDIT_LOG="/tmp/nexus-rdr065-bd-create-audit.log"

    # Look up active-close marker via T1 scratch. The two-pass preamble tags
    # entries with `rdr-close-active,rdr-NNN` so the rdr id rides along on the
    # tag line. We avoid `nx scratch search` here because that is semantic, not
    # exact-tag — list+grep is the only reliable form.
    ACTIVE_CLOSE_RDR=""
    if command -v nx &>/dev/null; then
        ACTIVE_CLOSE_RDR=$(nx scratch list 2>/dev/null \
            | grep -E '\brdr-close-active\b' \
            | grep -oE '\brdr-[0-9]+\b' \
            | head -1 \
            | sed -E 's/^rdr-//')
    fi
    # Stripped form for numeric matching (065 → 65). The HA-3 regex below uses
    # 0* so it accepts either padded or unpadded forms in the bead text.
    ACTIVE_CLOSE_INT=$(printf '%s' "$ACTIVE_CLOSE_RDR" | sed -E 's/^0+//')
    [[ -z "$ACTIVE_CLOSE_INT" && -n "$ACTIVE_CLOSE_RDR" ]] && ACTIVE_CLOSE_INT="$ACTIVE_CLOSE_RDR"

    # Pull agent_id / agent_type from the original hook stdin (subagent attribution).
    AGENT_ID=$(printf '%s' "$STDIN" | python3 -c "
import json, sys
try: print(json.load(sys.stdin).get('agent_id', ''))
except Exception: print('')
" 2>/dev/null || true)
    AGENT_TYPE=$(printf '%s' "$STDIN" | python3 -c "
import json, sys
try: print(json.load(sys.stdin).get('agent_type', ''))
except Exception: print('')
" 2>/dev/null || true)

    audit_line() {
        local decision="$1" missing_json="${2:-[]}"
        local ts cmd_excerpt
        ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
        cmd_excerpt=$(printf '%s' "$TOOL_INPUT" | head -c 200 | python3 -c "import json,sys; print(json.dumps(sys.stdin.read()))" 2>/dev/null || printf '"%s"' "$TOOL_INPUT")
        printf '{"ts":"%s","agent_id":"%s","agent_type":"%s","decision":"%s","rdr":"%s","missing":%s,"cmd":%s}\n' \
            "$ts" "$AGENT_ID" "$AGENT_TYPE" "$decision" "$ACTIVE_CLOSE_RDR" "$missing_json" "$cmd_excerpt" \
            >> "$AUDIT_LOG" 2>/dev/null || true
    }

    if [[ -z "$ACTIVE_CLOSE_RDR" ]]; then
        audit_line "allow-no-active-close"
        allow
    fi

    # Robust title/description extraction via shlex.
    PARSED=$(printf '%s' "$TOOL_INPUT" | python3 -c "
import sys, shlex
title, desc = '', ''
try:
    tokens = shlex.split(sys.stdin.read())
    for i, t in enumerate(tokens):
        if t == '--title' and i + 1 < len(tokens):
            title = tokens[i + 1]
        elif t.startswith('--title='):
            title = t.split('=', 1)[1]
        elif t == '--description' and i + 1 < len(tokens):
            desc = tokens[i + 1]
        elif t.startswith('--description='):
            desc = t.split('=', 1)[1]
except Exception:
    pass
print(title)
print('---NXSEP---')
print(desc)
" 2>/dev/null || true)
    TITLE_VAL=$(printf '%s' "$PARSED" | sed -n '1,/---NXSEP---/p' | sed '$d')
    DESC_VAL=$(printf '%s' "$PARSED" | awk '/---NXSEP---/{flag=1; next} flag')
    COMBINED="${TITLE_VAL} ${DESC_VAL}"

    # HA-3 scoped detection: does the bead reference the active RDR ID?
    RDR_MENTIONED=false
    if printf '%s' "$COMBINED" | grep -qiE "(^|[^0-9])0*${ACTIVE_CLOSE_INT}([^0-9]|\$)|RDR-0*${ACTIVE_CLOSE_INT}|rdr-0*${ACTIVE_CLOSE_INT}"; then
        RDR_MENTIONED=true
    fi

    if [[ "$RDR_MENTIONED" == "false" ]]; then
        audit_line "allow-advisory"
        allow "RDR close active for RDR-${ACTIVE_CLOSE_RDR} — if this bead is a follow-up, add reopens_rdr/sprint/drift_condition metadata to the description."
    fi

    # RDR is referenced — require commitment markers.
    MISSING=()
    printf '%s' "$COMBINED" | grep -qi 'reopens_rdr' || MISSING+=("reopens_rdr")
    printf '%s' "$COMBINED" | grep -qiE 'sprint|due' || MISSING+=("sprint or due")
    printf '%s' "$COMBINED" | grep -qi 'drift_condition' || MISSING+=("drift_condition")

    if [[ ${#MISSING[@]} -eq 0 ]]; then
        audit_line "allow-complete"
        allow
    fi

    # Build missing-list JSON for audit
    MISSING_JSON=$(printf '%s\n' "${MISSING[@]}" | python3 -c "import json,sys; print(json.dumps([l.strip() for l in sys.stdin if l.strip()]))" 2>/dev/null || printf '[]')
    audit_line "deny" "$MISSING_JSON"

    MISSING_DISPLAY=$(printf -- '- %s\n' "${MISSING[@]}")
    REASON=$(printf 'Follow-up bead for RDR-%s is missing required commitment metadata.\nMissing fields:\n%s\nAdd these to the --description, e.g.:\n  reopens_rdr: %s\n  sprint: implementation-2026-04\n  drift_condition: <what drift looks like>' \
        "$ACTIVE_CLOSE_RDR" "$MISSING_DISPLAY" "$ACTIVE_CLOSE_RDR")
    REASON_JSON=$(printf '%s' "$REASON" | python3 -c "import json,sys; print(json.dumps(sys.stdin.read()))" 2>/dev/null || printf '"%s"' "$REASON")
    printf '{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "reason": %s}}\n' "$REASON_JSON"
    exit 0
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
