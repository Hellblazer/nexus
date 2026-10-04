#!/usr/bin/env bash
# Behavioural test of cloud-client-path-gate.sh's leg B3 compare logic
# (nexus-20onx, round 3). The gate itself needs a cloud-mode box; the compare
# function does not. This extracts _ownerless_mode_verdict from the REAL script
# (never a copy), feeds it canned /v1/status bodies and checks the return code
# and the line it prints. It also checks the final-sentinel wiring: an
# unasserted mode must reach the PASSED line, and a violation must not.
#
# Prints "cloud_client_path_gate_b3_test.sh: N passed, M failed";
# tests/scripts/test_shell_suite_wiring.py holds the floor.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# The gate fragments this suite extracts call "$E2E_PYTHON", which the real gate sets by sourcing
# lib/python.sh at start; the extraction has no such preamble (nexus-u67ow).
# shellcheck source=lib/python.sh disable=SC1091
source "$HERE/lib/python.sh"
e2e_python_resolve || exit 2
GATE="$HERE/cloud-client-path-gate.sh"
NAME="cloud_client_path_gate_b3_test.sh"
PASS=0
FAIL=0

TMP="$(mktemp -d "${TMPDIR:-/tmp}/ccpg-b3-test.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT

sed -n '/^_ownerless_mode_verdict() {/,/^}/p' "$GATE" > "$TMP/fn.sh"
if [ "$(wc -l < "$TMP/fn.sh")" -lt 15 ]; then
  echo "[FAIL] _ownerless_mode_verdict not found in $GATE (extracted $(wc -l < "$TMP/fn.sh") lines)"
  echo "$NAME: 0 passed, 1 failed"
  exit 1
fi

# case <label> <want-rc> <want-text> <status-body> <expected-mode>
run_case() {
  local label="$1" want_rc="$2" want_text="$3" body="$4" expected="$5" out rc
  # shellcheck disable=SC1091
  out="$(source "$TMP/fn.sh"; _ownerless_mode_verdict "$body" "$expected")"
  rc=$?
  if [ "$rc" = "$want_rc" ] && [[ "$out" == *"$want_text"* ]]; then
    PASS=$((PASS + 1)); echo "[ok]   $label"
  else
    FAIL=$((FAIL + 1)); echo "[FAIL] $label: want rc=$want_rc text='$want_text', got rc=$rc"
    printf '%s\n' "$out" | sed 's/^/         /'
  fi
}

# A served /v1/status always carries embedding_mode; an engine before RDR-223 P3.2 has no mode field.
LOG='{"embedding_mode":"voyage","ownerless_write_mode":"log-only"}'
ENF='{"embedding_mode":"voyage","ownerless_write_mode":"enforce"}'
OLD='{"embedding_mode":"voyage"}'

run_case "expected log-only, engine log-only -> ok (0)" 0 "ok [B3]" "$LOG" log-only
run_case "expected enforce, engine enforce -> ok (0)" 0 "ok [B3]" "$ENF" enforce
run_case "expected log-only, engine enforce -> violation (1): the dangerous mis-wire" \
  1 "reports ownerless_write_mode='enforce', expected 'log-only'" "$ENF" log-only
run_case "expected enforce, engine log-only -> violation (1)" \
  1 "reports ownerless_write_mode='log-only', expected 'enforce'" "$LOG" enforce
run_case "expected set, engine reports no mode -> violation (1)" \
  1 "(absent)" "$OLD" log-only
run_case "expected set, status body unreadable -> violation (1)" \
  1 "no readable status body" "" enforce
run_case "expected set, status body is not JSON -> violation (1)" \
  1 "no readable status body" "<html>502</html>" log-only
# The skip-pass hole (critic H1): a live mode with no assertion must not pass.
run_case "expected UNSET, engine reports log-only -> violation (1), names the knob" \
  1 "NX_EXPECTED_OWNERLESS_WRITE_MODE is unset" "$LOG" ""
run_case "expected UNSET, engine reports enforce -> violation (1)" \
  1 "NX_EXPECTED_OWNERLESS_WRITE_MODE is unset" "$ENF" ""
run_case "expected UNSET, engine reports no mode -> not run (3)" \
  3 "NOT RUN [B3]" "$OLD" ""
# The unreadable-body hole (critic S4): with the variable unset, an edge failure must not read as "an
# engine with no mode" and let the gate print PASSED.
run_case "expected UNSET, status body empty (curl failed) -> violation (1), never not-run" \
  1 "no readable status body" "" ""
run_case "expected UNSET, edge 401 JSON error body -> violation (1)" \
  1 "no readable status body" '{"error":"unauthorized"}' ""
run_case "expected UNSET, edge 502 HTML page -> violation (1)" \
  1 "no readable status body" "<html>502 Bad Gateway</html>" ""
run_case "expected UNSET, JSON that is not an object -> violation (1)" \
  1 "no readable status body" '["embedding_mode"]' ""

# Leg J's compare logic (nexus-wbfpw.50): the reaper liveness verdict, extracted from the real script and
# fed canned /v1/status bodies with a fixed clock (13:00:00Z; limit = 3 x 3600 + 600 = 11400 s).
sed -n '/^_reaper_status_verdict() {/,/^}/p' "$GATE" > "$TMP/jfn.sh"
if [ "$(wc -l < "$TMP/jfn.sh")" -lt 30 ]; then
  echo "[FAIL] _reaper_status_verdict not found in $GATE (extracted $(wc -l < "$TMP/jfn.sh") lines)"
  echo "$NAME: $PASS passed, $((FAIL + 1)) failed"
  exit 1
fi
NOW="$("$E2E_PYTHON" -c "from datetime import datetime, timezone; print(int(datetime(2026, 10, 4, 13, 0, 0, tzinfo=timezone.utc).timestamp()))")"

# case <label> <want-rc> <want-text> <status-body>
run_j() {
  local label="$1" want_rc="$2" want_text="$3" body="$4" out rc
  # shellcheck disable=SC1091
  out="$(source "$TMP/jfn.sh"; _reaper_status_verdict "$body" "$NOW")"
  rc=$?
  if [ "$rc" = "$want_rc" ] && [[ "$out" == *"$want_text"* ]]; then
    PASS=$((PASS + 1)); echo "[ok]   $label"
  else
    FAIL=$((FAIL + 1)); echo "[FAIL] $label: want rc=$want_rc text='$want_text', got rc=$rc"
    printf '%s\n' "$out" | sed 's/^/         /'
  fi
}
# reaper_body <enabled> <interval> <budget> <last-json> <failed-json>
reaper_body() {
  printf '{"embedding_mode":"voyage","reaper":{"enabled":%s,"interval_seconds":%s,"wall_clock_budget_seconds":%s,"last_completed_pass_at":%s,"failed_passes_total":%s,"last_pass":{"tenants_visited":3}}}' \
    "$1" "$2" "$3" "$4" "$5"
}

run_j "J: enabled, fresh pass, no failures -> ok (0)" 0 "ok [J]" \
  "$(reaper_body true 3600 600 '"2026-10-04T12:30:00Z"' 0)"
run_j "J: a pass exactly at the limit (three intervals plus the budget) -> ok (0)" 0 "ok [J]" \
  "$(reaper_body true 3600 600 '"2026-10-04T09:50:00Z"' 0)"
run_j "J: a pass one second past the limit -> violation (1)" 1 "more than 3 intervals" \
  "$(reaper_body true 3600 600 '"2026-10-04T09:49:59Z"' 0)"
run_j "J: a stale pass (12 h) -> violation (1)" 1 "the reaper may be dead" \
  "$(reaper_body true 3600 600 '"2026-10-04T01:00:00Z"' 0)"
run_j "J: the limit reads interval and budget from the object (900 s + 0 budget, pass 4 intervals ago) -> violation (1)" \
  1 "more than 3 intervals of 900s" "$(reaper_body true 900 0 '"2026-10-04T12:00:00Z"' 0)"
run_j "J: no completed pass yet (null) -> violation (1)" 1 "has made no completed pass" \
  "$(reaper_body true 3600 600 null 0)"
run_j "J: an unparseable pass time -> violation (1)" 1 "not an ISO-8601 instant" \
  "$(reaper_body true 3600 600 '"yesterday"' 0)"
run_j "J: failed_passes_total 2 -> violation (1)" 1 "failed_passes_total is 2" \
  "$(reaper_body true 3600 600 '"2026-10-04T12:30:00Z"' 2)"
run_j "J: failed_passes_total absent -> violation (1)" 1 "failed_passes_total is None" \
  '{"embedding_mode":"voyage","reaper":{"enabled":true,"interval_seconds":3600,"wall_clock_budget_seconds":600,"last_completed_pass_at":"2026-10-04T12:30:00Z"}}'
run_j "J: reaper disabled -> violation (1), never not-applicable" 1 "reaper.enabled is False" \
  "$(reaper_body false 3600 600 '"2026-10-04T12:30:00Z"' 0)"
run_j "J: {\"enabled\":false} alone -> violation (1)" 1 "reaper.enabled is False" \
  '{"embedding_mode":"voyage","reaper":{"enabled":false}}'
run_j "J: reaper key absent (an engine before it, or an edge that strips it) -> violation (1)" \
  1 "no \`reaper\` object" "$OLD"
run_j "J: reaper is not an object -> violation (1)" 1 "no \`reaper\` object" \
  '{"embedding_mode":"voyage","reaper":"enabled"}'
run_j "J: a boolean interval is not an integer -> violation (1)" 1 "interval_seconds is True" \
  "$(reaper_body true true 600 '"2026-10-04T12:30:00Z"' 0)"
run_j "J: status body unreadable (curl failed) -> violation (1)" 1 "no readable status body" ""
run_j "J: status body is an edge HTML page -> violation (1)" 1 "no readable status body" "<html>502 Bad Gateway</html>"
run_j "J: an edge 401 JSON error body -> violation (1)" 1 "no readable status body" '{"error":"unauthorized"}'

# Wiring: the not-run state reaches the final sentinel, a violation is a leg
# failure, and the function is called with the bearer-resolved status body.
check_wiring() {
  local label="$1" pattern="$2"
  if grep -Eq -- "$pattern" "$GATE"; then
    PASS=$((PASS + 1)); echo "[ok]   $label"
  else
    FAIL=$((FAIL + 1)); echo "[FAIL] $label: /$pattern/ not found in $GATE"
  fi
}
check_wiring "B3 return 3 sets B3_NOT_RUN" '3\) echo "  \$B3_LINE"; B3_NOT_RUN=1'
check_wiring "B3 violation is a leg failure" '\*\) _leg_fail "\$B3_LINE"'
check_wiring "J is judged on the status body leg B fetched" 'J_LINE="\$\(_reaper_status_verdict "\$STATUS_BODY"\)"'
check_wiring "J violation is a leg failure" '_leg_fail "\$J_LINE"'
check_wiring "the leg battery expects 9 legs (A B C+D E F H I J K)" '^EXPECTED_LEGS=9$'

# Leg K runs against the LIVE engine, so its read-only property is pinned here rather than left to a comment:
# every _edge_post must be answered by an _edge_expect, a route that can move data is only ever posted to
# in a form the engine refuses (400/404/422) or, for the two read routes, against the unregistered
# collection name, and quarantine-restore always carries dry_run. A new probe that breaks this fails here.
K_AUDIT="$("$E2E_PYTHON" - "$GATE" <<'PY'
import re
import sys

text = open(sys.argv[1]).read()
calls = re.findall(r'_edge_post\s+"([^"]+)"\s*(?:\\\n\s*)?(\'[^\']*\'|"(?:[^"\\]|\\.)*")\n\s*_edge_expect\s+(K\d+)\s+(\d+)\s+(\w+)', text)
posted = len(re.findall(r'^\s*_edge_post\s', text, re.M))
errs = []
if len(calls) != posted or posted < 12:
    errs.append("expected every one of >= 12 _edge_post calls to pair with an _edge_expect, got %d of %d" % (len(calls), posted))
for path, body, label, code, kind in calls:
    if code in ("400", "404", "422"):
        if path.endswith("/gc/quarantine-restore") and '"dry_run":true' not in body.replace('\\"', '"'):
            errs.append("%s: a quarantine-restore probe without dry_run" % label)
    elif code == "200":
        if not (path in ("/v1/vectors/reapable", "/v1/vectors/manifest-less-census") and "$K_COLLECTION" in body):
            errs.append("%s: a 200 probe that is not a read of the unregistered collection name" % label)
    else:
        errs.append("%s: unexpected status %s" % (label, code))
if "unregistered" not in text or not re.search(r'K_COLLECTION="knowledge__ccpg-unregistered-', text):
    errs.append("K_COLLECTION is no longer an unregistered name")
print("; ".join(errs) if errs else "OK %d" % len(calls))
PY
)"
if [[ "$K_AUDIT" == OK\ * ]]; then
  PASS=$((PASS + 1)); echo "[ok]   K probes are read-only by construction (${K_AUDIT#OK } probes audited)"
else
  FAIL=$((FAIL + 1)); echo "[FAIL] K probes are read-only by construction: $K_AUDIT"
fi
check_wiring "the PASSED line carries the unasserted-mode note" 'violations=0 \(ownerless-write mode NOT asserted'

echo "$NAME: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
