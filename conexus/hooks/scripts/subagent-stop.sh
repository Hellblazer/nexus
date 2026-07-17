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
#   NX_ORCH_STOP_GUARD unset/off/unknown  -> exit 0        (DEFAULT-OFF until P1.G, bead .15)
#   stop_hook_active true                 -> exit 0        (21c once-guard round-trip)
#   agent not listed / sync / unnamed     -> exit 0        (sync stays unblockable by construction)
#   BLOCKED row already present           -> exit 0        (once-guard belt)
#   transcript missing/not-a-file/junk    -> exit 0        (fail-open; scan crash too)
#   assistant SendMessage in transcript   -> REPORTED row, exit 0   (report sent)
#   otherwise, mode=observe               -> WOULDBLOCK row, exit 0   (.11 measurement)
#   otherwise, mode=block                 -> BLOCKED row + {"decision":"block"}
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
# P1.G / bead .15 (default-ON flip): change the MODE fallback below from
# ":-off" to ":-block" — that single token is the gate.

MODE="${NX_ORCH_STOP_GUARD:-off}"
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

[[ "$STOP_ACTIVE" == "true" ]] && exit 0
[[ -n "$SESSION_ID" && -n "$AGENT_ID" && -n "$AGENT_TYPE" ]] || exit 0

expectations_sweep

expectations_owes_report "$SESSION_ID" "$AGENT_ID" "$AGENT_TYPE" || exit 0
expectations_already_blocked "$SESSION_ID" "$AGENT_ID" && exit 0

# Report check: a SendMessage tool_use in an ASSISTANT message of the
# agent transcript (scoped to assistant tool_use blocks so
# SendMessage-shaped JSON the agent merely READ — nested in a
# tool_result — never counts as its report). VERDICT-TOKEN plumbing: the
# scan prints FOUND / NOTFOUND; only the literal NOTFOUND may block.
# Anything else — python3 missing, a crash (e.g. the path is a readable
# DIRECTORY), empty output — fails OPEN, never through to the block
# branch. Missing/non-regular/unreadable transcript -> fail open too.
[[ -n "$TRANSCRIPT" && -f "$TRANSCRIPT" && -r "$TRANSCRIPT" ]] || exit 0
VERDICT="$(python3 - "$TRANSCRIPT" <<'PYEOF' 2>/dev/null
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
)"
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
