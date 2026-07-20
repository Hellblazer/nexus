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

# Idempotence: one START row per agent_id.
#
# This was a bare check-then-append, which is a TOCTOU: two registrations
# firing concurrently for the same SubagentStart both read the file, neither
# saw a row, and both appended. That is exactly what happened in production
# (nexus-3h0u6) — every START row doubled, with IDENTICAL timestamps, while
# the sequential unit test stayed green because sequential invocation cannot
# reproduce a race. The duplicate registration that caused it is gone, but
# the guard now actually holds under concurrency rather than only appearing
# to, so re-introducing a second surface degrades the census instead of
# silently corrupting it.
#
# Fail-open throughout: if the lock cannot be taken we stamp anyway. A
# duplicate row is a census nuisance; a missing START row breaks the
# declaration-completeness audit, which is the worse failure.
_stamp_if_absent() {
    if [[ -r "$FILE" ]] && awk -F'\t' -v id="$AGENT_ID" \
        '$2 == "START" && $3 == id { found = 1 } END { exit !found }' "$FILE" 2>/dev/null; then
        return 0
    fi
    expectations_start "$SESSION_ID" "$AGENT_ID" "$AGENT_TYPE" >/dev/null 2>&1
}

# mkdir is the atomic test-and-set: it fails if the directory exists, so
# exactly one concurrent invocation enters the critical section. The RDR-184
# P0 lock primitive (tests/e2e/lib/lock.sh) is the heavier, stale-detecting
# tool for long-held harness locks; this section is a file scan plus one
# append, so a bounded wait and a fail-open fallback are the proportionate
# shape and keep the plugin free of a 400-line vendored dependency.
LOCKDIR="${FILE}.stamp.lock"
_held=""
for _ in 1 2 3 4 5 6 7 8 9 10; do
    if mkdir "$LOCKDIR" 2>/dev/null; then
        _held=1
        break
    fi
    sleep 0.1
done

_stamp_if_absent

# A stale lockdir (holder killed mid-section) degrades to the pre-existing
# behaviour — a possible duplicate row — never to a missing one.
[[ -n "$_held" ]] && rmdir "$LOCKDIR" 2>/dev/null
exit 0
