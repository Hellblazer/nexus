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
check_wiring "the PASSED line carries the unasserted-mode note" 'violations=0 \(ownerless-write mode NOT asserted'

echo "$NAME: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
