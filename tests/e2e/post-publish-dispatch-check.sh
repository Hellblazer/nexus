#!/usr/bin/env bash
# nexus-0zsmg / shakeout-7.41.0-ledger-projector-dead-on-cloud-2026-09-11:
# POST-PUBLISH DISPATCH CHECK — the real-dispatch leg of the post-publish
# shakedown (T2 nexus/shakedown-playbook), added because every prior gate
# tested a consistent PAIR (client + engine) or a FIXTURE, never a real
# Explore/general-purpose dispatch inside a live Claude Code session on the
# installed plugin. That is exactly the gap that shipped conexus 7.41.0
# with the ledger-tuple projector (conexus/hooks/scripts/tuple_ledger_
# project.py) silently dead on every cloud-mode box: the fixture MVV run
# that gated Phase 4 never went through a real subagent dispatch, so the
# "SKIP ... no service endpoint resolvable" failure mode never fired.
#
# WHAT THIS CHECKS, for one already-completed session (a human or the
# session itself dispatches one trivial agent FIRST, then this script runs
# against that session's id):
#   (a) the TSV expectations ledger has >=1 START row and >=1 REPORTED row
#       (tests/e2e/lib/expectations.sh -- the always-on half of RDR-184).
#   (b) the tuple-space subspace ledger/<sid> (read-only, via the
#       INSTALLED `nx tuple stats` / `nx tuple rd`, cloud or local) holds
#       >=1 kind=start and >=1 kind=report tuple whose agent_id matches a
#       TSV START row -- proof the async projector actually reached the
#       engine for THIS session's dispatch, not merely that the TSV side
#       (which never talks to the engine) looks clean.
#   (c) <sid>.tuple-projection.log (the projector's own failure log --
#       every line in it, by construction, is a SKIP; see tuple_ledger_
#       project.py's _log_skip) carries no line newer than the newest TSV
#       START row. A SKIP after our dispatch's START is the projector
#       failing on OUR OWN traffic, not stale history from an earlier run.
#   (d) `expectations_census` (tests/e2e/lib/expectations.sh) ends its
#       space-backed report on SPACE_PRESENT for this session -- never
#       SPACE_FALLBACK (the space could not be consulted at all) or
#       SPACE_BLINDSPOT (consulted, saw nothing under ledger/ at all).
#
# NEVER A SKIP-PASS (nexus-moht0 vacuous-gate doctrine). A prerequisite
# this script cannot self-provision -- no `nx` on PATH, or no TSV ledger
# file for the named session -- is reported as exit 2 with a named reason,
# distinct from a genuine miss (exit 1). There is no third path that
# quietly reports success with nothing checked.
#
# THIS SCRIPT DOES NOT DISPATCH THE AGENT ITSELF. It is the ASSERTION half
# only -- the dispatch is a real Agent-tool call in a live Claude Code
# session (Explore or general-purpose is enough; it must be a REAL
# dispatch through the installed plugin, not a fixture/subprocess claude
# -p run, which is exactly the layer gap this check exists to close -- see
# T2 nexus/shakeout-7.41.0-ledger-projector-dead-on-cloud-2026-09-11).
# Run this script AFTER that dispatch's SubagentStop has fired, naming
# that session's id.
#
# USAGE: tests/e2e/post-publish-dispatch-check.sh <session_id>
#
# EXIT CODES:
#   0 = POST-PUBLISH DISPATCH CHECK PASSED -- all four checks hold.
#   1 = POST-PUBLISH DISPATCH CHECK FAILED -- at least one check missed
#       (the named MISS lines say which).
#   2 = a prerequisite is absent (no `nx` on PATH, no TSV ledger file for
#       this session, or no session_id given) -- nothing was checkable.
#
# BOX-CLASS-AGNOSTIC BY DESIGN: run this identically on a managed-cloud box
# and a local-supervisor box. Neither box class gets a "not applicable"
# exemption here -- the whole point is that the tuple space is reachable
# (or honestly reported unreachable) from both.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/expectations.sh
source "$SCRIPT_DIR/lib/expectations.sh"

_prereq_fail() {
    echo "POST-PUBLISH DISPATCH CHECK FAILED (prerequisite absent): $*" >&2
    exit 2
}

SID="${1:-}"
if [[ -z "$SID" ]]; then
    _prereq_fail "usage: $0 <session_id>"
fi

if ! command -v nx >/dev/null 2>&1; then
    _prereq_fail "PATH has no nx -- install/activate the plugin's nx CLI before running this check"
fi

TSV_FILE=""
if ! TSV_FILE="$(expectations_file "$SID" 2>&1)"; then
    _prereq_fail "invalid session_id '$SID': $TSV_FILE"
fi
if [[ ! -r "$TSV_FILE" ]]; then
    _prereq_fail "no ledger file for session '$SID' at $TSV_FILE -- dispatch at least one real agent in a live session with this session id, wait for its SubagentStop, then re-run this check"
fi

STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/nexus/orchestration"
LOG_FILE="$STATE_DIR/$SID.tuple-projection.log"

VIOLATIONS=0
_miss() {
    VIOLATIONS=$((VIOLATIONS + 1))
    echo "MISS: $*"
}

# _run_capture VAR_OUT VAR_RC -- cmd args...  -- run a command WITHOUT
# letting `set -e` abort the script on a non-zero exit; stdout+stderr are
# combined into VAR_OUT, the exit code into VAR_RC. Works for both real
# external commands and sourced bash functions (expectations_census).
_run_capture() {
    local __out_var="$1" __rc_var="$2"
    shift 2
    [[ "${1:-}" == "--" ]] && shift
    local __out __rc
    set +e
    __out="$("$@" 2>&1)"
    __rc=$?
    set -e
    printf -v "$__out_var" '%s' "$__out"
    printf -v "$__rc_var" '%s' "$__rc"
}

echo "=== POST-PUBLISH DISPATCH CHECK: session=$SID ==="
echo "ledger file:     $TSV_FILE"
echo "projection log:  $LOG_FILE"
echo

# ── (a) TSV ledger: >=1 START row, >=1 REPORTED row ─────────────────────
TSV_START_COUNT="$(grep -c $'\tSTART\t' "$TSV_FILE" 2>/dev/null || true)"
TSV_REPORTED_COUNT="$(grep -c $'\tREPORTED\t' "$TSV_FILE" 2>/dev/null || true)"
TSV_START_COUNT="${TSV_START_COUNT:-0}"
TSV_REPORTED_COUNT="${TSV_REPORTED_COUNT:-0}"
echo "COUNT tsv_start=$TSV_START_COUNT tsv_reported=$TSV_REPORTED_COUNT"
if [[ "$TSV_START_COUNT" -lt 1 ]]; then
    _miss "(a) TSV ledger $TSV_FILE has zero START rows"
fi
if [[ "$TSV_REPORTED_COUNT" -lt 1 ]]; then
    _miss "(a) TSV ledger $TSV_FILE has zero REPORTED rows"
fi

# TSV START agent_ids (for the (b) cross-check) and the newest START row's
# own timestamp (for the (c) cross-check) -- one awk pass covers both.
TSV_START_IDS="$(awk -F'\t' '$2 == "START" { print $3 }' "$TSV_FILE" | sort -u)"
TSV_NEWEST_START_TS="$(awk -F'\t' '$2 == "START" { ts = $1 } END { print ts }' "$TSV_FILE")"

# ── (b) tuple space ledger/<sid> holds matching kind=start/kind=report ──
_run_capture SPACE_STATS_OUT SPACE_STATS_RC -- nx tuple stats "ledger/$SID" --json
SPACE_TOTAL=0
if [[ "$SPACE_STATS_RC" != 0 ]]; then
    _miss "(b) nx tuple stats ledger/$SID failed (rc=$SPACE_STATS_RC): $SPACE_STATS_OUT"
else
    SPACE_TOTAL="$(python3 -c '
import json, sys
try:
    d = json.loads(sys.argv[1])
except Exception:
    print(0)
else:
    print(int(d.get("total", 0)) if isinstance(d, dict) else 0)
' "$SPACE_STATS_OUT" 2>/dev/null || echo 0)"
fi
echo "COUNT space_stats_total=$SPACE_TOTAL"
if [[ "$SPACE_TOTAL" -lt 1 ]]; then
    _miss "(b) tuple space ledger/$SID reports total=0 tuples"
fi

_run_capture SPACE_RD_OUT SPACE_RD_RC -- nx tuple rd "ledger/$SID" -n 300 --json

SPACE_KIND_START=0
SPACE_KIND_REPORT=0
SPACE_START_MATCH=0
SPACE_REPORT_MATCH=0
if [[ "$SPACE_RD_RC" != 0 ]]; then
    _miss "(b) nx tuple rd ledger/$SID failed (rc=$SPACE_RD_RC): $SPACE_RD_OUT"
else
    SPACE_SUMMARY="$(TSV_START_IDS="$TSV_START_IDS" python3 -c '
import json, os, sys

tsv_ids = {x for x in os.environ.get("TSV_START_IDS", "").splitlines() if x}

try:
    rows = json.loads(sys.argv[1])
except Exception as exc:
    print(f"PARSE_ERROR\t{exc}")
    sys.exit(0)
if not isinstance(rows, list):
    print("PARSE_ERROR\tnx tuple rd --json did not return an array")
    sys.exit(0)

start_ids, report_ids = set(), set()
for row in rows:
    if not isinstance(row, dict):
        continue
    keys = row.get("keys") or {}
    kind = keys.get("kind")
    agent_id = keys.get("agent_id")
    if not agent_id:
        continue
    if kind == "start":
        start_ids.add(agent_id)
    elif kind == "report":
        report_ids.add(agent_id)

print(f"KIND_COUNTS\t{len(start_ids)}\t{len(report_ids)}")
print(f"START_MATCH\t{len(start_ids & tsv_ids)}")
print(f"REPORT_MATCH\t{len(report_ids & tsv_ids)}")
' "$SPACE_RD_OUT" 2>&1)"
    PARSE_ERROR="$(awk -F'\t' '$1 == "PARSE_ERROR" { print $2 }' <<<"$SPACE_SUMMARY")"
    if [[ -n "$PARSE_ERROR" ]]; then
        _miss "(b) could not parse nx tuple rd ledger/$SID --json output: $PARSE_ERROR"
    else
        SPACE_KIND_START="$(awk -F'\t' '$1 == "KIND_COUNTS" { print $2 }' <<<"$SPACE_SUMMARY")"
        SPACE_KIND_REPORT="$(awk -F'\t' '$1 == "KIND_COUNTS" { print $3 }' <<<"$SPACE_SUMMARY")"
        SPACE_START_MATCH="$(awk -F'\t' '$1 == "START_MATCH" { print $2 }' <<<"$SPACE_SUMMARY")"
        SPACE_REPORT_MATCH="$(awk -F'\t' '$1 == "REPORT_MATCH" { print $2 }' <<<"$SPACE_SUMMARY")"
    fi
fi
echo "COUNT space_kind_start=$SPACE_KIND_START space_kind_report=$SPACE_KIND_REPORT space_start_match=$SPACE_START_MATCH space_report_match=$SPACE_REPORT_MATCH"
if [[ "$SPACE_RD_RC" == 0 ]]; then
    if [[ "${SPACE_START_MATCH:-0}" -lt 1 ]]; then
        _miss "(b) tuple space ledger/$SID holds no kind=start tuple whose agent_id matches a TSV START row"
    fi
    if [[ "${SPACE_REPORT_MATCH:-0}" -lt 1 ]]; then
        _miss "(b) tuple space ledger/$SID holds no kind=report tuple whose agent_id matches a TSV START row"
    fi
fi

# ── (c) tuple-projection.log carries no SKIP newer than the last START ──
LOG_TOTAL_LINES=0
LOG_NEWER_COUNT=0
LOG_NEWER_SAMPLE=""
if [[ -f "$LOG_FILE" ]]; then
    LOG_TOTAL_LINES="$(wc -l < "$LOG_FILE" | tr -d ' ')"
    if [[ -n "$TSV_NEWEST_START_TS" ]]; then
        LOG_NEWER_LINES="$(awk -F'\t' -v cutoff="$TSV_NEWEST_START_TS" '$1 > cutoff' "$LOG_FILE")"
        if [[ -n "$LOG_NEWER_LINES" ]]; then
            LOG_NEWER_COUNT="$(wc -l <<<"$LOG_NEWER_LINES" | tr -d ' ')"
            LOG_NEWER_SAMPLE="${LOG_NEWER_LINES%%$'\n'*}"
        fi
    fi
fi
echo "COUNT log_total_lines=$LOG_TOTAL_LINES log_skip_after_last_start=$LOG_NEWER_COUNT"
if [[ "$LOG_NEWER_COUNT" -gt 0 ]]; then
    _miss "(c) $LOG_FILE has $LOG_NEWER_COUNT SKIP line(s) newer than the last TSV START ($TSV_NEWEST_START_TS): e.g. $LOG_NEWER_SAMPLE"
fi

# ── (d) expectations_census ends on SPACE_PRESENT, never FALLBACK/BLINDSPOT
_run_capture CENSUS_OUT CENSUS_RC -- expectations_census "$SID"
HAS_SPACE_PRESENT=0
HAS_SPACE_BAD=0
LAST_SPACE_LINE=""
while IFS= read -r line; do
    case "$line" in
        SPACE_*) LAST_SPACE_LINE="$line" ;;
    esac
    case "$line" in
        SPACE_PRESENT*) HAS_SPACE_PRESENT=1 ;;
        SPACE_FALLBACK*|SPACE_BLINDSPOT*) HAS_SPACE_BAD=1 ;;
    esac
done <<<"$CENSUS_OUT"
CENSUS_SPACE_STATUS="${LAST_SPACE_LINE:-NONE}"
echo "COUNT census_rc=$CENSUS_RC census_space_status=$CENSUS_SPACE_STATUS"
echo "--- expectations_census output ---"
echo "$CENSUS_OUT"
echo "--- end expectations_census output ---"
if [[ "$HAS_SPACE_PRESENT" != 1 || "$HAS_SPACE_BAD" == 1 ]]; then
    _miss "(d) expectations_census for session '$SID' did not end on SPACE_PRESENT: ${LAST_SPACE_LINE:-no SPACE_ line found in census output at all}"
fi

echo
if [[ "$VIOLATIONS" -eq 0 ]]; then
    echo "POST-PUBLISH DISPATCH CHECK PASSED -- session=$SID violations=0"
    exit 0
fi
echo "POST-PUBLISH DISPATCH CHECK FAILED -- session=$SID violations=$VIOLATIONS"
exit 1
