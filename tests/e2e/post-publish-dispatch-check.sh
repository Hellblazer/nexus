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
#       (nx-hook expectations_* -- the always-on half of RDR-184).
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
#   (d) `nx-hook expectations_census` ends its
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
# USAGE: tests/e2e/post-publish-dispatch-check.sh [session_id]
#
# nexus-7m6uc: the runner has no reliable way to know which of several live
# session ids on a shared box is the right one to pass (the harness session
# id, the MCP-leased id the hooks actually write under, the machine-wide
# `current_session` file, the newest T1 lease -- see JDR-001, four distinct
# scopes, three of them routinely wrong for this purpose). Two remedies:
#
#   (a) SELF-SOLVING FAILURE. When no ledger exists for a GIVEN session_id,
#       this script lists every ledger file that DOES exist under the
#       orchestration state dir, newest first, with mtime and START/REPORTED
#       counts, before exiting 2 -- the runner sees the real candidate
#       immediately instead of guessing blind.
#   (b) AUTO-DISCOVERY. Called with NO argument, the script looks for
#       ledgers with recent agent-dispatch activity (a ledger file write, or
#       an EXPECT credit-slot symlink -- nx.hooks.expectations._claim_credit
#       -- within the last $POST_PUBLISH_DISPATCH_RECENT_SECONDS seconds,
#       default 1800) and uses the session id automatically IF EXACTLY ONE
#       such ledger exists. Zero or more than one candidate is refused (exit
#       2), naming every candidate by session id and mtime -- the same
#       discipline `mailbox_send` uses for a name with more than one live
#       holder: never guess.
#
# Passing session_id explicitly still works exactly as before and skips
# auto-discovery entirely.
#
# EXIT CODES:
#   0 = POST-PUBLISH DISPATCH CHECK PASSED -- all four checks hold.
#   1 = POST-PUBLISH DISPATCH CHECK FAILED -- at least one check missed
#       (the named MISS lines say which).
#   2 = a prerequisite is absent (no `nx` on PATH, no TSV ledger file for
#       the given/discovered session, no session_id resolvable at all, or
#       auto-discovery was ambiguous) -- nothing was checkable. The
#       candidate listing printed alongside this exit code (see (a)/(b)
#       above) is the remedy, not a separate failure.
#
# BOX-CLASS-AGNOSTIC BY DESIGN: run this identically on a managed-cloud box
# and a local-supervisor box. Neither box class gets a "not applicable"
# exemption here -- the whole point is that the tuple space is reachable
# (or honestly reported unreachable) from both.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# NOTHING IS SOURCED ANY MORE (RDR-215 bead nexus-q02nx.21, which deleted
# the plugin copy of expectations.sh this used to source). Bead .14 left
# the decision here, naming the two uses: `expectations_census`, which
# has an `nx-hook` verb, and `expectations_file`, which does not.
#
#   census       -> `nx-hook expectations_census` (the ported verb).
#   expectations_file -> computed inline below.
#
# Computing the path inline rather than adding a verb for it is the right
# trade for THIS script specifically: it is a post-publish check that
# runs against an INSTALLED box with no checkout, so every dependency it
# takes has to be reachable from `nx`/`nx-hook` alone. The path is a pure
# function of XDG_STATE_HOME and the session id, this script already
# derives STATE_DIR that exact way for LOG_FILE two lines down, and the
# charset guard below is the same regex nexus.hooks.expectations
# (_SESSION_ID_RE) and the deleted bash both used -- it is the guard that
# matters, not the concatenation, and it is reproduced verbatim.

_prereq_fail() {
    echo "POST-PUBLISH DISPATCH CHECK FAILED (prerequisite absent): $*" >&2
    exit 2
}

# ── nexus-7m6uc helpers: which ledger, and how to say so ────────────────────

#: How far back "recent agent-dispatch activity" reaches for auto-discovery
#: (b). Overridable for a slow-dispatch environment; the default is
#: generous on purpose -- a false NEGATIVE here (a genuinely recent ledger
#: excluded) silently degrades to the zero-candidate case (2, self-solving);
#: a false POSITIVE (an old ledger wrongly included) can only ever make
#: auto-discovery MORE conservative, since it can only turn a would-be
#: unique candidate into an ambiguous one that refuses rather than guesses.
POST_PUBLISH_DISPATCH_RECENT_SECONDS="${POST_PUBLISH_DISPATCH_RECENT_SECONDS:-1800}"

# _epoch_mtime PATH -- epoch mtime of PATH, symlink or regular file, without
# following a symlink (the EXPECT credit-slot files are DANGLING by design
# -- nexus.hooks.expectations._claim_credit's target is an agent_id
# identity string, not a real path -- so `stat -L` would fail on every one
# of them). BSD stat (macOS) first, GNU stat (Linux) second; the same
# fallback shape tests/e2e/plugin-lockstep-gate.sh already uses.
_epoch_mtime() {
    stat -f %m "$1" 2>/dev/null || stat -c %Y "$1" 2>/dev/null
}

# _human_mtime EPOCH -- best-effort local-time rendering for a listing line;
# falls back to the bare epoch rather than failing the check over a display
# nicety.
_human_mtime() {
    date -r "$1" 2>/dev/null || date -d "@$1" 2>/dev/null || echo "epoch:$1"
}

# _ledger_recency_epoch FILE -- the newest of FILE's own mtime and any of
# its EXPECT credit-slot symlinks' mtimes (FILE.credit.<type_enc>.<n>). The
# slot is created at DISPATCH time and can be newer than the ledger file's
# last START/REPORTED append -- e.g. a background agent whose EXPECT row
# landed but whose START has not (or, for a still-running dispatch, never
# will before this check runs).
_ledger_recency_epoch() {
    local file="$1" best="" slot cand
    best="$(_epoch_mtime "$file")" || best=""
    for slot in "$file".credit.*; do
        [[ -L "$slot" ]] || continue
        cand="$(_epoch_mtime "$slot")" || continue
        if [[ -z "$best" || "$cand" -gt "$best" ]]; then
            best="$cand"
        fi
    done
    [[ -n "$best" ]] && echo "$best"
}

# _ledger_listing -- one TAB-separated `epoch<TAB>sid<TAB>start<TAB>reported`
# line per `*.expectations` ledger under STATE_DIR, newest epoch first.
# Empty output (no lines at all) when STATE_DIR does not exist or holds no
# ledgers -- callers check for that, never assume a line exists.
_ledger_listing() {
    local f sid epoch start_c rep_c
    [[ -d "$STATE_DIR" ]] || return 0
    for f in "$STATE_DIR"/*.expectations; do
        [[ -f "$f" ]] || continue
        sid="$(basename "$f" .expectations)"
        # nexus-7m6uc round 2 (review finding, IMPORTANT): UNGUARDED under
        # `set -euo pipefail` (line 89), this command substitution's
        # non-zero exit -- which `_ledger_recency_epoch` returns whenever
        # BOTH the file's own stat and every credit-slot stat fail, e.g. a
        # TOCTOU race where a peer session's ledger is reaped between the
        # glob above and this call -- used to abort the WHOLE SCRIPT
        # silently: no message, exit 1, indistinguishable from a genuine
        # MISS to a caller reading this script's own contract. Confirmed by
        # repro: an unguarded `var=$(failing_fn)` under this exact `set`
        # line prints nothing past that point. `|| epoch=""` makes the
        # race a graceful skip instead, exactly like `_epoch_mtime`'s own
        # callers three lines above this function.
        epoch="$(_ledger_recency_epoch "$f")" || epoch=""
        if [[ -z "$epoch" ]]; then
            echo "  (skipping $f: vanished or became unreadable mid-scan)" >&2
            continue
        fi
        start_c="$(grep -c $'\tSTART\t' "$f" 2>/dev/null || true)"
        rep_c="$(grep -c $'\tREPORTED\t' "$f" 2>/dev/null || true)"
        printf '%s\t%s\t%s\t%s\n' "$epoch" "$sid" "${start_c:-0}" "${rep_c:-0}"
    done | sort -t $'\t' -k1,1 -rn
}

# _print_ledger_listing LISTING -- render each `_ledger_listing` line as a
# human line on stderr. Shared by (a)'s not-found diagnostic and (b)'s
# zero/ambiguous-candidate refusal so the two remedies read identically.
_print_ledger_listing() {
    local epoch sid start_c rep_c
    while IFS=$'\t' read -r epoch sid start_c rep_c; do
        [[ -n "$sid" ]] || continue
        echo "  $sid  mtime=$(_human_mtime "$epoch")  start=$start_c reported=$rep_c" >&2
    done <<<"$1"
}

# _auto_discover_session_id -- remedy (b). Echoes the sole session id with
# recent agent-dispatch activity, or exits 2 (via _prereq_fail) naming every
# candidate -- zero or more than one is refused, never guessed.
_auto_discover_session_id() {
    local now cutoff listing candidates n_candidates window_min
    now="$(date +%s)"
    cutoff=$((now - POST_PUBLISH_DISPATCH_RECENT_SECONDS))
    window_min=$((POST_PUBLISH_DISPATCH_RECENT_SECONDS / 60))
    listing="$(_ledger_listing)"
    if [[ -z "$listing" ]]; then
        _prereq_fail "no session_id given (usage: $0 [session_id]) and no ledger files exist at all under $STATE_DIR -- dispatch at least one real agent in a live session first, or pass its session id explicitly"
    fi
    candidates="$(awk -F'\t' -v cutoff="$cutoff" '$1 >= cutoff' <<<"$listing")"
    n_candidates="$(grep -c . <<<"$candidates" 2>/dev/null || true)"
    n_candidates="${n_candidates:-0}"
    if [[ -z "$candidates" || "$n_candidates" -eq 0 ]]; then
        echo "No ledger has agent-dispatch activity in the last ${window_min} minute(s). Ledgers present under $STATE_DIR, newest first:" >&2
        _print_ledger_listing "$listing"
        _prereq_fail "no session_id given (usage: $0 [session_id]) and auto-discovery found no recent candidate -- pass one explicitly, e.g. the freshest one listed above"
    fi
    if [[ "$n_candidates" -gt 1 ]]; then
        echo "AMBIGUOUS: $n_candidates ledgers have agent-dispatch activity in the last ${window_min} minute(s) -- refusing to guess. Candidates, newest first:" >&2
        _print_ledger_listing "$candidates"
        _prereq_fail "no session_id given and auto-discovery is ambiguous ($n_candidates recent candidates) -- pass one explicitly (see candidates above)"
    fi
    awk -F'\t' '{print $2}' <<<"$candidates"
}

if ! command -v nx >/dev/null 2>&1; then
    _prereq_fail "PATH has no nx -- install/activate the plugin's nx CLI before running this check"
fi
# `nx-hook` is a separate console script from the same wheel, and an older
# installed generation simply does not have it (AGENTS.md § hot rules).
# Check (d) runs `nx-hook expectations_census`, so a missing shim is a
# prerequisite absence, not a dispatch finding.
if ! command -v nx-hook >/dev/null 2>&1; then
    _prereq_fail "nx-hook is not on PATH, though nx is -- this generation predates the nx-hook console script; reinstall/activate a current conexus generation before running this check"
fi

STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/nexus/orchestration"

SID="${1:-}"
if [[ -z "$SID" ]]; then
    SID="$(_auto_discover_session_id)"
    echo "AUTO-DISCOVERED session_id=$SID (sole ledger with agent-dispatch activity in the last $((POST_PUBLISH_DISPATCH_RECENT_SECONDS / 60)) minute(s))" >&2
fi

# The per-session ledger path, formerly expectations_file(). Same charset
# guard as nexus.hooks.expectations._SESSION_ID_RE: it is what keeps a
# session id like '../../evil' from writing outside the state dir.
if [[ ! "$SID" =~ ^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$ ]]; then
    _prereq_fail "invalid session_id '$SID' (path-safe charset only)"
fi

TSV_FILE="$STATE_DIR/$SID.expectations"
LOG_FILE="$STATE_DIR/$SID.tuple-projection.log"

if [[ ! -r "$TSV_FILE" ]]; then
    LISTING="$(_ledger_listing)"
    if [[ -n "$LISTING" ]]; then
        echo "No ledger for '$SID'. Ledgers present under $STATE_DIR, newest first:" >&2
        _print_ledger_listing "$LISTING"
    fi
    _prereq_fail "no ledger file for session '$SID' at $TSV_FILE -- dispatch at least one real agent in a live session with this session id, wait for its SubagentStop, then re-run this check"
fi

VIOLATIONS=0
_miss() {
    VIOLATIONS=$((VIOLATIONS + 1))
    echo "MISS: $*"
}

# _run_capture VAR_OUT VAR_RC -- cmd args...  -- run a command WITHOUT
# letting `set -e` abort the script on a non-zero exit; stdout+stderr are
# combined into VAR_OUT, the exit code into VAR_RC. Works for both real
# external commands and (formerly) sourced bash functions; since RDR-215
# bead nexus-q02nx.21 every caller here is an external command.
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
_run_capture CENSUS_OUT CENSUS_RC -- nx-hook expectations_census "$SID"
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
