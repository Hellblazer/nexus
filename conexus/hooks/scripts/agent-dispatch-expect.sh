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
#     not a gate. Every failure path exits 0 emitting NOTHING ON STDOUT,
#     which the harness reads as "no decision, proceed". This hook must
#     never emit a deny envelope and must never be the reason a dispatch
#     does not land.
#   - Idempotent per tool_use_id: plugin hooks.json AND a project
#     settings.json may both register it; two firings must compose to ONE
#     row, or the N-of-type deficit count is inflated into nonsense.
#   - STDOUT-SILENT, but NOT STDERR-SILENT (nexus-mqnkt). A real incident
#     (session 49d1c3ab, 2026-09-01) left a START row with no matching
#     EXPECT row and zero forensic trace anywhere: every skip path here
#     used to exit 0 with nothing on stdout OR stderr, so a genuinely
#     dropped write was indistinguishable, after the fact, from the hook
#     never having been invoked at all. Every skip below that represents a
#     write this hook COULD have made, but did not, now writes one line to
#     stderr first — stdout stays empty (the harness contract is
#     unchanged; PreToolUse stdout is parsed, stderr is not) so this adds
#     zero risk of ever blocking or altering a dispatch. Visible only under
#     ``claude --debug`` or a captured hook log, same as
#     expectations_owes_report's lock-exhaustion warning.

MODE="${NX_ORCH_STOP_GUARD:-block}"
if [[ "$MODE" != "observe" && "$MODE" != "block" ]]; then
    # Deliberate, session-scoped opt-out (NX_ORCH_STOP_GUARD=off) — not a
    # failure, so no diagnostic: a user who turned this off does not need
    # to be told the ledger is not being written.
    exit 0
fi

PAYLOAD="$(cat)"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./expectations.sh disable=SC1091
if ! source "$HERE/expectations.sh" 2>/dev/null; then
    echo "agent-dispatch-expect: could not source $HERE/expectations.sh — EXPECT row NOT written for this dispatch" >&2
    exit 0
fi

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
# subagent_type ABSENT/EMPTY => general-purpose (nexus-a795d). Mirrors the
# run_in_background default just above, in this same file: the harness
# still starts a general-purpose agent for an omitted type, so recording
# nothing is a silent ledger blindspot (or a FALSE undeclared accusation
# against a later same-type dispatch that was declared), never a safe
# no-op. Reproduced live 2026-08-03 (probe START ac93416a2d9d417d9).
fields = [
    str(d.get("session_id") or ""),
    str(d.get("tool_name") or ""),
    str(ti.get("subagent_type") or "general-purpose"),
    "background" if bg else "sync",
    str(d.get("tool_use_id") or ""),
]
scrub = str.maketrans({"\x1f": " ", "\t": " ", "\n": " ", "\r": " "})
print("\x1f".join(f.translate(scrub) for f in fields))
' 2>/dev/null
)"

# "Task" is the pre-rename spelling of the same tool; accept both so a
# harness that still emits it is covered rather than silently unrecorded.
# NOT diagnosed on stderr: hooks.json's matcher is "Agent|Task" (verified
# by TestPluginWiring::test_registered_on_agent_pretooluse), so the
# harness should never invoke this script for any other tool_name in the
# first place — a mismatch here means either a matcher regression (a real
# bug, but one the matcher test already catches) or a fully malformed
# payload (already covered by the diagnostics below). Logging every
# incidental non-Agent firing would be the noisy branch, not the silent
# one this fix targets.
if [[ "$TOOL_NAME" != "Agent" && "$TOOL_NAME" != "Task" ]]; then
    # The matcher should make this unreachable; if it fires, the payload
    # shape has drifted and an EXPECT row is being dropped (nexus-mqnkt
    # critique: the session's first dispatch is exactly where this would
    # hide). Loud on stderr, never on stdout.
    echo "agent-dispatch-expect: tool_name '${TOOL_NAME}' is not Agent/Task — EXPECT row NOT written for this dispatch (tool_use_id=${DISPATCH_ID:-<none>})" >&2
    exit 0
fi
if [[ -z "$SESSION_ID" ]]; then
    echo "agent-dispatch-expect: empty/unparseable session_id — EXPECT row NOT written for this dispatch (tool_use_id=${DISPATCH_ID:-<none>})" >&2
    exit 0
fi
# SUBAGENT_TYPE cannot be empty here: the python parse above defaults an
# absent/empty subagent_type to "general-purpose" (nexus-a795d), so this
# branch is dead in practice; kept only as the historical shape of the
# original guard.
[[ -n "$SUBAGENT_TYPE" ]] || exit 0

FILE="$(expectations_file "$SESSION_ID" 2>/dev/null)"
if [[ -z "$FILE" ]]; then
    # nexus-mqnkt: THE candidate this incident points at. expectations_file
    # fails only on an empty sid (already excluded above) or one outside
    # its path-safe charset (session ids are framework-assigned UUIDs in
    # every observed payload, so this should not fire in practice either —
    # but "should not" is exactly what silently ate a real EXPECT row
    # once, with nothing to show for it afterward).
    echo "agent-dispatch-expect: expectations_file rejected session_id '${SESSION_ID}' — EXPECT row NOT written for this dispatch (tool_use_id=${DISPATCH_ID:-<none>}, subagent_type=${SUBAGENT_TYPE})" >&2
    exit 0
fi

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
    # STDOUT only is suppressed here (expectations_expect never prints on
    # success) — STDERR passes through, so a validation failure inside it
    # (bad name charset, bad mode, tab/newline in dispatch_id) is no longer
    # silently discarded. It was previously ``>/dev/null 2>&1``, which
    # swallowed both.
    expectations_expect "$SESSION_ID" "$SUBAGENT_TYPE" "$DISPATCH_MODE" "$DISPATCH_ID" >/dev/null
}

# A lockdir left behind by a killed hook would otherwise cost EVERY later
# dispatch in the session the full 1s budget, forever. This hook runs before
# every Agent dispatch, so that tax is worth one stat: reap a lockdir older
# than a minute (orders of magnitude beyond the ~ms critical section) and
# retry. Failure to reap is ignored — the loop below still proceeds.
LOCKDIR="${FILE}.expect.lock"
if [[ -d "$LOCKDIR" ]] && [[ -z "$(find "$LOCKDIR" -maxdepth 0 -mmin -1 2>/dev/null)" ]]; then
    rmdir "$LOCKDIR" 2>/dev/null
fi
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
