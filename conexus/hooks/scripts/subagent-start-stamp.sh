#!/bin/bash
# SubagentStart expectations-stamp — RDR-184 .16 wiring (bead
# nexus-ccs9v.16): record a START row (agent_id + agent_type) in the
# session's expectations file at dispatch time. This is the DISPATCH
# RECORD the declaration-completeness retro audit diffs against EXPECT
# rows (a START row with no matching EXPECT = an undeclared background
# teammate — the Gap-1 escalation tripwire).
#
# Non-load-bearing for the stop-hook consult (which never reads START
# rows — scenario 27: the start payload cannot classify background-ness).
#
# Contract:
#   - Mode-gated: writes when NX_ORCH_STOP_GUARD is observe|block —
#     which since the P1.G default-ON flip (2026-07-17) includes UNSET
#     (default block, matching subagent-stop.sh). Explicit off opts out.
#   - Idempotent per agent_id: plugin hooks.json AND the repo's project
#     settings.json may both register this script in one session; two
#     firings must compose to ONE row.
#   - STDOUT-SILENT: SubagentStart stdout injects context into the
#     spawned subagent. This script must never add context — everything
#     chatty goes to stderr or /dev/null, and the script ends with no
#     stdout emitted.
#   - Fail-open shape: any parse/validation failure exits 0 silently.

MODE="${NX_ORCH_STOP_GUARD:-block}"
if [[ "$MODE" != "observe" && "$MODE" != "block" ]]; then
    exit 0
fi

PAYLOAD="$(cat)"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./expectations.sh disable=SC1091
source "$HERE/expectations.sh" 2>/dev/null || exit 0

IFS=$'\t' read -r SESSION_ID AGENT_ID AGENT_TYPE <<<"$(
    printf '%s' "$PAYLOAD" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    d = {}
fields = [str(d.get(k) or "") for k in ("session_id", "agent_id", "agent_type")]
print("\t".join(f.replace("\t", " ").replace("\n", " ") for f in fields))
' 2>/dev/null
)"

[[ -n "$SESSION_ID" && -n "$AGENT_ID" && -n "$AGENT_TYPE" ]] || exit 0

FILE="$(expectations_file "$SESSION_ID" 2>/dev/null)" || exit 0

# Idempotence: one START row per agent_id, however many registrations fire.
if [[ -r "$FILE" ]] && awk -F'\t' -v id="$AGENT_ID" \
    '$2 == "START" && $3 == id { found = 1 } END { exit !found }' "$FILE" 2>/dev/null; then
    exit 0
fi

expectations_start "$SESSION_ID" "$AGENT_ID" "$AGENT_TYPE" >/dev/null 2>&1
exit 0
