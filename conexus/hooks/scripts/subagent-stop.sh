#!/bin/bash
# SubagentStop Hook — RDR-184 Gap 1 (bead nexus-ccs9v.9): block a NAMED
# BACKGROUND teammate's idle exactly once when it never sent its
# orchestrator a completion report.
#
# ONE SubagentStop hook covers BOTH sync and background dispatches
# (finding 5 / scenario 21b: background teammates fire SubagentStop in the
# SPAWNER's session — the docs caveat is refuted). Ground truth for
# "owes a report" is the P1.1 expectations file, written by the
# orchestrator BEFORE dispatch (scenario 27: no hook payload can classify
# background-ness — see conexus/hooks/scripts/expectations.sh header).
#
# DECISION TABLE (every uncertain path fails OPEN — never block a stop on
# missing evidence; the file is an enabling allowlist, not a gate):
#   NX_ORCH_STOP_GUARD off/unknown        -> exit 0        (explicit opt-out)
#   NX_ORCH_STOP_GUARD unset              -> block         (DEFAULT-ON since P1.G, bead .15, 2026-07-17)
#   stop_hook_active true                 -> resolution stamp*, exit 0  (21c once-guard round-trip)
#   agent not listed / sync / unnamed     -> exit 0        (sync stays unblockable by construction)
#   BLOCKED row already present           -> resolution stamp*, exit 0  (once-guard belt)
#   transcript missing/not-a-file/junk    -> exit 0        (fail-open; scan crash too)
#   assistant SendMessage in transcript   -> REPORTED row, exit 0   (report sent)
#   otherwise, mode=observe               -> WOULDBLOCK row, exit 0   (.11 measurement)
#   otherwise, mode=block                 -> BLOCKED row + {"decision":"block"}
#
# *POST-BLOCK RESOLUTION STAMP (nexus-hybv1): before this fix, a BLOCKED
# row was terminal FOREVER — the once-guard exits recorded nothing, so an
# agent that heeded the block and delivered its report was ledger-
# indistinguishable from one that died silent. Forensics across bfbfa2fe +
# b819e8f3 showed ALL 7 recorded blocks resolved with a real SendMessage
# 17-26s after the nudge, yet the census read them as failures ("census
# OVER-reports BLOCKED"). Both once-guard exits now re-scan the transcript
# for an agent that owes and was already blocked, and append a REPORTED
# row when the report has since appeared — so BLOCKED followed by REPORTED
# reads as "guard worked" and a bare BLOCKED means genuinely unresolved.
# Same fail-open posture: a failed re-scan stamps nothing and never blocks.
#
# REPORT CHECK SCOPE (documented narrowing): the RDR's ideal is "final
# turn lacks a SendMessage-to-main". v1 checks for any SendMessage
# tool_use in an ASSISTANT message of the agent transcript, to any
# recipient — turn boundaries and recipient identity are
# transcript-format-fragile, and the marathon failure class this guards
# (idle-without-report x10) was zero-SendMessage teammates. Fail-open
# bias: a teammate that reported mid-run but finished silently is NOT
# blocked. The .11 measurement covers BOTH directions: WOULDBLOCK rows
# are the false-block candidates; REPORTED rows are the missed-block
# candidates (cross-check them against reports the orchestrator actually
# received). Tighten only if that measurement says so.
#
# NOT A SECURITY BOUNDARY: the check is satisfiable by any assistant
# SendMessage tool_use regardless of recipient, success, or content — a
# decoy call evades it. This is a hygiene guard for cooperative Claude
# subagents, not an enforcement surface against adversarial ones.
#
# P1.G / bead .15: FLIPPED default-ON 2026-07-17 (Hal accept; gates
# discharged: 3x same-day scenario-21 green incl. an independent
# validator run; 97MB worst-case transcript scans in 0.14s vs the 10s
# hook timeout; .13-S1 accept-in-writing — the flip precedes the .11
# census, safe because undeclared dispatches are fail-open by
# construction). Opt out per-session with NX_ORCH_STOP_GUARD=off.

MODE="${NX_ORCH_STOP_GUARD:-block}"
if [[ "$MODE" != "observe" && "$MODE" != "block" ]]; then
    exit 0
fi

PAYLOAD="$(cat)"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./expectations.sh disable=SC1091
source "$HERE/expectations.sh" 2>/dev/null || exit 0

# One parse call for all payload fields; junk payload -> empty fields ->
# fail-open below.
IFS=$'\t' read -r SESSION_ID AGENT_ID AGENT_TYPE TRANSCRIPT STOP_ACTIVE <<<"$(
    printf '%s' "$PAYLOAD" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    d = {}
fields = [str(d.get(k) or "") for k in
          ("session_id", "agent_id", "agent_type", "agent_transcript_path")]
fields.append("true" if d.get("stop_hook_active") else "false")
print("\t".join(f.replace("\t", " ").replace("\n", " ") for f in fields))
' 2>/dev/null
)"

# Report check: a SendMessage tool_use in an ASSISTANT message of the
# agent transcript (scoped to assistant tool_use blocks so
# SendMessage-shaped JSON the agent merely READ — nested in a
# tool_result — never counts as its report). VERDICT-TOKEN plumbing: the
# scan echoes FOUND / NOTFOUND; only the literal NOTFOUND may block.
# Anything else — python3 missing, a crash (e.g. the path is a readable
# DIRECTORY), empty output, missing/non-regular/unreadable transcript
# (echoed as SKIP) — fails OPEN, never through to the block branch.
_scan_verdict() {
    if [[ -z "$TRANSCRIPT" || ! -f "$TRANSCRIPT" || ! -r "$TRANSCRIPT" ]]; then
        echo "SKIP"
        return 0
    fi
    python3 - "$TRANSCRIPT" <<'PYEOF' 2>/dev/null
import json, sys

def scan(path) -> bool:
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line or '"SendMessage"' not in line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("type") != "assistant":
                continue
            msg = entry.get("message") or {}
            content = msg.get("content") if isinstance(msg, dict) else None
            if not isinstance(content, list):
                continue
            for block in content:
                if (
                    isinstance(block, dict)
                    and block.get("type") == "tool_use"
                    and block.get("name") == "SendMessage"
                ):
                    return True
    return False

try:
    print("FOUND" if scan(sys.argv[1]) else "NOTFOUND")
except Exception:
    print("SCANERROR")
PYEOF
}

# _stamp_resolution_if_reported <strength> — nexus-hybv1: called from the
# two once-guard exits. If THIS agent owes a report, has a BLOCKED row,
# and its transcript NOW shows a SendMessage, append a REPORTED row so
# the ledger records the block as resolved. <strength> (4th TSV field;
# inert to every exact-field reader, same tolerance as WOULDBLOCK) keeps
# the causal evidence honest (critique 2026-07-22): "immediate" = the
# block round-trip itself produced the report (strong: the guard
# demonstrably worked); "later" = a subsequent stop found a report that
# may have arrived for unrelated reasons (weak). Every failure path
# stamps nothing (fail-open); never blocks, never exits non-zero.
_stamp_resolution_if_reported() {
    local strength="${1:-immediate}"
    [[ -n "$SESSION_ID" && -n "$AGENT_ID" && -n "$AGENT_TYPE" ]] || return 0
    expectations_owes_report "$SESSION_ID" "$AGENT_ID" "$AGENT_TYPE" || return 0
    expectations_already_blocked "$SESSION_ID" "$AGENT_ID" || return 0
    # Consecutive-duplicate guard (review 21032 finding 3): the scan is
    # whole-transcript, so every re-stop of a resolved agent would re-find
    # the same SendMessage and append another REPORTED forever. Stamp only
    # when the agent's LAST terminal row is not already REPORTED — real
    # interleavings (BLOCKED -> REPORTED) still record; idle re-stops of a
    # resolved agent add nothing.
    [[ "$(expectations_last_terminal "$SESSION_ID" "$AGENT_ID")" == "REPORTED" ]] && return 0
    if [[ "$(_scan_verdict)" == "FOUND" ]]; then
        _expectations_append "$(expectations_file "$SESSION_ID")" \
            "$(_expectations_ts)"$'\tREPORTED\t'"$AGENT_ID"$'\t'"$strength"
    fi
    return 0
}

if [[ "$STOP_ACTIVE" == "true" ]]; then
    # The immediate re-stop after a block round-trip: the agent was told
    # to report and stop again. Record whether it did (nexus-hybv1).
    _stamp_resolution_if_reported immediate
    exit 0
fi
[[ -n "$SESSION_ID" && -n "$AGENT_ID" && -n "$AGENT_TYPE" ]] || exit 0

expectations_sweep

expectations_owes_report "$SESSION_ID" "$AGENT_ID" "$AGENT_TYPE" || exit 0
if expectations_already_blocked "$SESSION_ID" "$AGENT_ID"; then
    # A later stop of a previously-blocked agent (e.g. a multi-round
    # teammate's round 2+): the once-guard still never re-blocks, but a
    # report sent since the block is stamped so the ledger reflects the
    # delivery outcome (nexus-hybv1 — before this, gh1414-critic's
    # round-2/3 reports left no trace while the never-blocked reviewer
    # accrued one REPORTED row per round).
    _stamp_resolution_if_reported later
    exit 0
fi

VERDICT="$(_scan_verdict)"
case "$VERDICT" in
    FOUND)
        # .11 census raw material: EXPECT (dispatched) x REPORTED (scan
        # says reported) x WOULDBLOCK (scan says not). A missed block —
        # an agent whose SendMessage was a status ping, not the real
        # completion report — shows up as a REPORTED row the
        # orchestrator can cross-check against what it actually received.
        _expectations_append "$(expectations_file "$SESSION_ID")" \
            "$(_expectations_ts)"$'\tREPORTED\t'"$AGENT_ID"
        exit 0
        ;;
    NOTFOUND)
        : # owes and unreported — fall through to observe/block below
        ;;
    *)
        exit 0 # scan crashed or python unavailable — fail open
        ;;
esac

# Owes a report, none sent.
if [[ "$MODE" == "observe" ]]; then
    # .11 measurement row — same TSV shape, foreign verb (readers of
    # EXPECT/BLOCKED ignore it). Never consumes the real once-guard.
    _expectations_append "$(expectations_file "$SESSION_ID")" \
        "$(_expectations_ts)"$'\tWOULDBLOCK\t'"$AGENT_ID"
    exit 0
fi

expectations_mark_blocked "$SESSION_ID" "$AGENT_ID"
printf '{"decision": "block", "reason": "You are the named background teammate %s and your orchestrator expects a completion report you have not sent. Use SendMessage now to report: outcome, artifacts (paths/commits/IDs), and anything blocking. Then stop."}\n' "$AGENT_TYPE"
exit 0
