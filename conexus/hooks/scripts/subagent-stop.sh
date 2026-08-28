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
#   ...and writes failed, mode=block      -> UNLANDEDWRITE + BLOCKED + block  (piqm5 L1)
#   otherwise, mode=observe               -> WOULDBLOCK row, exit 0   (.11 measurement)
#   otherwise, mode=block                 -> BLOCKED row + {"decision":"block"}
#
# UNLANDED-WRITE ARM (nexus-piqm5 Layer 1, 2026-08-27). An UNLANDEDWRITE row
# is stamped in BOTH modes whenever the transcript shows a storage write that
# returned an error; it is independent of the report verdict. The only NEW
# block is the reported-but-writes-failed case, and it fires only for agents
# already inside the owes-report allowlist -- no dispatch that is unblockable
# today becomes blockable. Same fail-open bias: only the literal UNLANDED
# verdict is actionable; a scan crash reads as CLEAN. See _writes_verdict.
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
# The scan body lives in a SIBLING FILE, not a heredoc: bash 5.3 pipes
# heredoc bodies and a >512B body deadlocks when macOS degrades pipe
# buffers (nexus-2gcqk; tests/hooks/test_heredoc_pipe_budget.py). A
# missing sibling file yields empty output -> fail-open, same as any
# other scan crash.
_scan_verdict() {
    if [[ -z "$TRANSCRIPT" || ! -f "$TRANSCRIPT" || ! -r "$TRANSCRIPT" ]]; then
        echo "SKIP"
        return 0
    fi
    python3 "$HERE/subagent-stop-scan.py" "$TRANSCRIPT" 2>/dev/null
}

# UNLANDED-WRITE SCAN (nexus-piqm5 Layer 1). Separate script, separate
# contract: echoes CLEAN / "UNLANDED <n> <tools>" / SCANERROR. A subagent's
# T1/T2/T3 write can fail for its entire session with the only signal being
# whether the agent narrated it in prose -- on 2026-08-25 two reviewers'
# findings survived only because both happened to mention the outage. Every
# write failure returns a str starting "Error: " (mcp/core.py:72-124), the
# same TYPE as a success string, so nothing downstream tells them apart.
#
# READS THE TRANSCRIPT, NOT THE tier_writes LEDGER, deliberately: the ledger
# read goes through the service, so when persistence is broken -- the case
# this exists to catch -- that read fails too and "no rows" is
# indistinguishable from "wrote nothing". The transcript is a local file,
# readable exactly when the store is not.
#
# POSITIVE EVIDENCE ONLY, so the fail-open contract above is preserved: only
# the literal UNLANDED verdict is actionable. Missing/unreadable transcript,
# SCANERROR, empty output, or python3 absent all fall through as CLEAN and
# change no decision. This scan can add evidence, never invent it from
# absence.
#
# SCOPE (documented narrowing, same posture as the report check): it runs
# only for agents that already reached the owes-report gate below, i.e. the
# population this hook already handles. A non-allowlisted or sync dispatch
# is never scanned and stays unblockable BY CONSTRUCTION -- this change adds
# no newly-blockable agent.
#
# CANNOT SEE a silent no-op (store returns success, lands nothing); the
# transcript records the success string. That needs a live read-back and is
# Layer 2, tracked separately. CLEAN means "no write reported failure", NOT
# "the writes landed".
_writes_verdict() {
    if [[ -z "$TRANSCRIPT" || ! -f "$TRANSCRIPT" || ! -r "$TRANSCRIPT" ]]; then
        echo "CLEAN"
        return 0
    fi
    local v
    v="$(python3 "$HERE/subagent-stop-writes-scan.py" "$TRANSCRIPT" 2>/dev/null)"
    case "$v" in
        UNLANDED\ *) printf '%s\n' "$v" ;;
        *)           echo "CLEAN" ;;
    esac
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

# nexus-4bqre.1: archive BEFORE the sweep, never after. expectations_sweep
# reaps ledgers older than 7 days; archiving afterwards would only preserve
# what survived the reap, which defeats the purpose. Ordering here is by
# construction, which is why this rides the existing sweep site rather than
# taking its own hook registration.
expectations_archive
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
WRITES_VERDICT="$(_writes_verdict)"

# nexus-piqm5 Layer 1: record the unlanded-write fact BEFORE any branch, so
# it lands in the ledger under every mode and both report outcomes -- the
# harm in the bead is that the failure is invisible unless the agent
# narrated it, and a row that only appears on the blocking path would keep
# it invisible in observe mode and for agents that did report. 4th TSV field
# carries "<n> <tools>"; readers matching exact verbs ignore the row.
if [[ "$WRITES_VERDICT" == UNLANDED\ * ]]; then
    _expectations_append "$(expectations_file "$SESSION_ID")" \
        "$(_expectations_ts)"$'\tUNLANDEDWRITE\t'"$AGENT_ID"$'\t'"${WRITES_VERDICT#UNLANDED }"
fi

case "$VERDICT" in
    FOUND)
        # .11 census raw material: EXPECT (dispatched) x REPORTED (scan
        # says reported) x WOULDBLOCK (scan says not). A missed block —
        # an agent whose SendMessage was a status ping, not the real
        # completion report — shows up as a REPORTED row the
        # orchestrator can cross-check against what it actually received.
        _expectations_append "$(expectations_file "$SESSION_ID")" \
            "$(_expectations_ts)"$'\tREPORTED\t'"$AGENT_ID"
        # nexus-piqm5: reported-but-writes-failed is the EXACT 2026-08-25
        # shape -- "review complete" delivered while every persistence call
        # 401'd, findings surviving only because the agent volunteered it.
        # Today this branch exits 0 and the loss is silent. Block ONCE so
        # the agent retries or states the failure. The once-guard bounds a
        # genuine outage to a single nudge (a retry cannot succeed while the
        # store is down, and must not loop).
        if [[ "$WRITES_VERDICT" == UNLANDED\ * && "$MODE" == "block" ]] \
           && ! expectations_already_blocked "$SESSION_ID" "$AGENT_ID"; then
            expectations_mark_blocked "$SESSION_ID" "$AGENT_ID" "unlanded-write"
            printf '{"decision": "block", "reason": "%s"}\n' \
                "You sent your completion report, but ${WRITES_VERDICT#UNLANDED } of your storage writes came back as errors, so those findings are NOT persisted -- a caller reading T1/T2/T3 will not see them. Retry the failed writes now. If they still fail, say so explicitly in a SendMessage and restate the findings inline so they are not lost. Then stop."
            exit 0
        fi
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

expectations_mark_blocked "$SESSION_ID" "$AGENT_ID" "$EXPECTATIONS_OWES_CAUSE"
# nexus-plycy: an exhaustion-forced block (EXPECTATIONS_OWES_CAUSE set by
# expectations_owes_report, see THE CONTRACT in expectations.sh) was
# never verified against the credit ledger -- name that in the reason so
# an over-blocked agent's operator can tell it apart from a genuine,
# credit-backed owes verdict (the ledger's BLOCKED row carries the same
# cause in its 4th field for later audit).
REASON="You are the named background teammate ${AGENT_TYPE} and your orchestrator expects a completion report you have not sent. Use SendMessage now to report: outcome, artifacts (paths/commits/IDs), and anything blocking. Then stop."
if [[ "$EXPECTATIONS_OWES_CAUSE" == "lock-exhausted" ]]; then
    REASON="${REASON} (NOTE: this block could not verify remaining report credit under lock contention -- treat it as a precaution, not a confirmed miss; see nexus-plycy.)"
fi
printf '{"decision": "block", "reason": "%s"}\n' "$REASON"
exit 0
