#!/bin/bash
# PreToolUse(Agent) expectations-declaration — RDR-184 Gap-1 mechanization
# (bead nexus-qc4p1). Writes the EXPECT row from the dispatch's own tool
# input, BEFORE the dispatch lands, so the declaration is no longer a
# human step performed out of band.
#
# WHY MECHANIZED. The convention was "call expectations_expect before every
# background Agent dispatch". It did not survive contact: five consecutive
# sessions, twenty-five dispatches, zero pairable declarations. The step
# sits at exactly the moment attention is on composing the prompt, and the
# write path is a SOURCED SHELL LIB (not an MCP tool, not an `nx` verb —
# nexus-3ra9h), which several sessions concluded was missing entirely. A
# PreToolUse hook removes the step: it runs in the harness, from the same
# payload the dispatch is made of.
#
# WHY THE KEY IS subagent_type, MEASURED (2026-07-31, live payloads):
#   PreToolUse   tool_name="Agent"
#                tool_input = {description, prompt, subagent_type,
#                              run_in_background}          <- no `name`
#                top level  = {session_id, tool_use_id, prompt_id, cwd,
#                              hook_event_name, permission_mode, effort,
#                              transcript_path}            <- no `agent_id`
#   SubagentStart agent_id="a<hash>", agent_type=<subagent_type>,
#                same session_id, same prompt_id
# subagent_type is the ONLY value both sides carry, and it arrives at
# SubagentStart verbatim as agent_type. An orchestrator-invented name
# could only live in the prompt TEXT, which the start hook never sees —
# which is why hand-written EXPECT rows were unpairable BY CONSTRUCTION.
#
# NO ORDINAL, deliberately. Two dispatches of one type in a turn are
# indistinguishable at pairing time: tool_use_id is unique per dispatch
# but ABSENT from the SubagentStart payload, and prompt_id (in both) is
# the TURN id, identical across every dispatch in the message. No
# per-instance key exists in either direction, so an ordinal would be as
# unpairable as the name. The audit matches N-of-type instead
# (expectations_undeclared / expectations_census).
#
# Contract:
#   - Mode-gated: writes when NX_ORCH_STOP_GUARD is observe|block, which
#     since the P1.G default-ON flip (2026-07-17) includes UNSET. Explicit
#     off opts out. Same gate as subagent-start-stamp.sh.
#   - FAIL-OPEN ON THE WRITE PATH, absolutely: the ledger is an audit aid,
#     not a gate. Every failure path exits 0 emitting NOTHING, which the
#     harness reads as "no decision, proceed". This hook must never emit a
#     deny envelope and must never be the reason a dispatch does not land.
#   - Idempotent per tool_use_id: plugin hooks.json AND a project
#     settings.json may both register it; two firings must compose to ONE
#     row, or the N-of-type deficit count is inflated into nonsense.
#   - STDOUT-SILENT.

MODE="${NX_ORCH_STOP_GUARD:-block}"
if [[ "$MODE" != "observe" && "$MODE" != "block" ]]; then
    exit 0
fi

PAYLOAD="$(cat)"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./expectations.sh disable=SC1091
source "$HERE/expectations.sh" 2>/dev/null || exit 0

# run_in_background ABSENT => background. That is this harness's documented
# Agent-tool default ("Subagents run in the background by default; pass
# run_in_background: false for a synchronous run"), and it is also the safe
# direction for a ledger of who owes a report: recording an obligation that
# turns out to be sync is noise, silently dropping a real one is the exact
# Gap-1 failure this file exists to catch. Every dispatch observed in the
# wild passed the field explicitly, so this default is unexercised there.
# Delimiter is US (0x1f), NOT tab. `IFS=$'\t' read` COLLAPSES empty fields,
# because tab is IFS *whitespace*: a payload with an empty session_id (or an
# absent subagent_type) shifts every later field one position left, and the
# hook then writes a row whose "name" is whatever landed there — e.g. the
# literal string "background". A non-whitespace IFS preserves empty fields
# positionally. Found by mutating the fail-open guard and watching the test
# stay green for the wrong reason; the same `IFS=$'\t' read` shape is used by
# subagent-start-stamp.sh, where it is currently benign only by luck.
IFS=$'\x1f' read -r SESSION_ID TOOL_NAME SUBAGENT_TYPE DISPATCH_MODE DISPATCH_ID <<<"$(
    printf '%s' "$PAYLOAD" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    d = {}
if not isinstance(d, dict):
    d = {}
ti = d.get("tool_input")
if not isinstance(ti, dict):
    ti = {}
bg = ti.get("run_in_background", True)
if isinstance(bg, str):
    bg = bg.strip().lower() not in ("false", "0", "no", "")
fields = [
    str(d.get("session_id") or ""),
    str(d.get("tool_name") or ""),
    str(ti.get("subagent_type") or ""),
    "background" if bg else "sync",
    str(d.get("tool_use_id") or ""),
]
scrub = str.maketrans({"\x1f": " ", "\t": " ", "\n": " ", "\r": " "})
print("\x1f".join(f.translate(scrub) for f in fields))
' 2>/dev/null
)"

# "Task" is the pre-rename spelling of the same tool; accept both so a
# harness that still emits it is covered rather than silently unrecorded.
[[ "$TOOL_NAME" == "Agent" || "$TOOL_NAME" == "Task" ]] || exit 0
[[ -n "$SESSION_ID" && -n "$SUBAGENT_TYPE" ]] || exit 0

FILE="$(expectations_file "$SESSION_ID" 2>/dev/null)" || exit 0

# Idempotence: one EXPECT row per tool_use_id. Mirrors the START-row guard
# in subagent-start-stamp.sh, including its TOCTOU lesson (nexus-3h0u6: a
# bare check-then-append doubled every row under concurrent registration
# while the sequential unit test stayed green). Fail-open throughout: if
# the lock cannot be taken we write anyway, because a duplicate row is a
# census nuisance and a MISSING row is the defect this hook exists to fix.
_expect_if_absent() {
    if [[ -n "$DISPATCH_ID" && -r "$FILE" ]] && awk -F'\t' -v id="$DISPATCH_ID" \
        '$2 == "EXPECT" && $5 == id { found = 1 } END { exit !found }' "$FILE" 2>/dev/null; then
        return 0
    fi
    expectations_expect "$SESSION_ID" "$SUBAGENT_TYPE" "$DISPATCH_MODE" "$DISPATCH_ID" >/dev/null 2>&1
}

LOCKDIR="${FILE}.expect.lock"
_held=""
for _ in 1 2 3 4 5 6 7 8 9 10; do
    if mkdir "$LOCKDIR" 2>/dev/null; then
        _held=1
        break
    fi
    sleep 0.1
done

_expect_if_absent

[[ -n "$_held" ]] && rmdir "$LOCKDIR" 2>/dev/null
exit 0
