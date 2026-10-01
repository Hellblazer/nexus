#!/usr/bin/env bash
# Behavioural test of published-client-write-gate.sh's verdict section
# (nexus-9a6io, RDR-223 P3.2). The gate itself needs a candidate engine and a
# PyPI install; its verdict logic does not. This extracts step 4 of the real
# script (from the "# ── 4. Verdict" marker to the end), runs it under a
# prelude that stubs `curl` with a canned /v1/status body, and checks the exit
# code and verdict line for each combination of mode, client version, journey
# outcome, counters and acknowledgement. FIXED_IN_VERSION and
# EXPECTED_LAG_BEAD are read from the real script, so a hand-update of either
# is exercised by the same cases.
#
# Prints "published_client_write_gate_verdict_test.sh: N passed, M failed";
# tests/scripts/test_shell_suite_wiring.py holds the floor.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GATE="$HERE/published-client-write-gate.sh"
NAME="published_client_write_gate_verdict_test.sh"
PASS=0
FAIL=0

TMP="$(mktemp -d "${TMPDIR:-/tmp}/pcwg-verdict-test.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT

FIXED="$(sed -n 's/^FIXED_IN_VERSION="\(.*\)"$/\1/p' "$GATE")"
LAG="$(sed -n 's/^EXPECTED_LAG_BEAD="\(.*\)"$/\1/p' "$GATE")"
if [ -z "$FIXED" ] || [ -z "$LAG" ]; then
  echo "[FAIL] could not read FIXED_IN_VERSION / EXPECTED_LAG_BEAD from $GATE"
  echo "$NAME: 0 passed, 1 failed"
  exit 1
fi

# Verdict section of the real script.
sed -n '/^# ── 4\. Verdict/,$p' "$GATE" > "$TMP/verdict.sh"
if [ "$(wc -l < "$TMP/verdict.sh")" -lt 40 ]; then
  echo "[FAIL] the verdict marker moved: extracted $(wc -l < "$TMP/verdict.sh") lines"
  echo "$NAME: 0 passed, 1 failed"
  exit 1
fi

# A client version safely below, and one at, FIXED_IN_VERSION.
OLD="7.0.1"
NEW="$FIXED"

# run_case <label> <want-rc> <want-text|-> <client> <mode> <store_ok> <md_ok> <ack> <status-json> [store_refused=1] [md_refused=1]
# store_refused / md_refused: 1 when that journey's own client output named the ownerless refusal.
run_case() {
  local label="$1" want_rc="$2" want_text="$3" client="$4" mode="$5" store_ok="$6" md_ok="$7" ack="$8" status="$9"
  local store_refused="${10:-1}" md_refused="${11:-1}"
  {
    echo 'set -euo pipefail'
    echo "FIXED_IN_VERSION=\"$FIXED\""
    echo "EXPECTED_LAG_BEAD=\"$LAG\""
    echo "CLIENT_VERSION=\"$client\""
    echo "NX_GATE_OWNERLESS_WRITE_MODE=\"$mode\""
    echo "NX_EXPECTED_CLIENT_LAG=\"$ack\""
    echo "STORE_OK=$store_ok"
    echo "MD_OK=$md_ok"
    echo "STORE_REFUSED=$store_refused"
    echo "MD_REFUSED=$md_refused"
    echo 'FAIL_REASONS=("store put: probe failure" "index md: probe failure")'
    echo 'GATE_OK=0'
    echo 'SERVICE_PORT=1'
    echo 'SERVICE_TOKEN=t'
    echo "FAKE_STATUS='$status'"
    echo 'curl() { printf "%s" "$FAKE_STATUS"; }'
    cat "$TMP/verdict.sh"
  } > "$TMP/case.sh"
  local out rc
  out="$(bash "$TMP/case.sh" 2>&1)"
  rc=$?
  if [ "$rc" = "$want_rc" ] && { [ "$want_text" = "-" ] || [[ "$out" == *"$want_text"* ]]; }; then
    PASS=$((PASS + 1))
    echo "[ok]   $label"
  else
    FAIL=$((FAIL + 1))
    echo "[FAIL] $label: want rc=$want_rc text='$want_text', got rc=$rc"
    printf '%s\n' "$out" | sed 's/^/         /'
  fi
}

S_LOG_SEEN='{"ownerless_write_mode":"log-only","ownerless_writes_refused_total":0,"ownerless_writes_would_refuse_total":4}'
S_LOG_QUIET='{"ownerless_write_mode":"log-only","ownerless_writes_refused_total":0,"ownerless_writes_would_refuse_total":0}'
S_LOG_REFUSED='{"ownerless_write_mode":"log-only","ownerless_writes_refused_total":2,"ownerless_writes_would_refuse_total":1}'
S_ENF_SEEN='{"ownerless_write_mode":"enforce","ownerless_writes_refused_total":3,"ownerless_writes_would_refuse_total":0}'
S_ENF_ONE='{"ownerless_write_mode":"enforce","ownerless_writes_refused_total":1,"ownerless_writes_would_refuse_total":0}'
S_ENF_QUIET='{"ownerless_write_mode":"enforce","ownerless_writes_refused_total":0,"ownerless_writes_would_refuse_total":0}'
S_NONE='{"status":"ok"}'

# --- log-only: the first production deploy ---------------------------------
run_case "log-only, old client, both journeys ok, would-refuse seen -> PASSED (0)" \
  0 "PUBLISHED-CLIENT WRITE GATE PASSED" "$OLD" log-only 1 1 "" "$S_LOG_SEEN"
run_case "log-only, old client, nothing counted -> oracle fails (FIXED_IN_VERSION stale or counter dead)" \
  1 "or the counter is dead" "$OLD" log-only 1 1 "" "$S_LOG_QUIET"
run_case "log-only engine that refused -> oracle fails" \
  1 "log-only must never refuse" "$OLD" log-only 1 1 "" "$S_LOG_REFUSED"
run_case "log-only, journeys fail, ack set, nothing refused -> ack refused (no evidence), not EXPECTED-INCOMPATIBLE" \
  1 "no evidence" "$OLD" log-only 0 0 "$LAG" "$S_LOG_SEEN"

# --- enforce: the final posture --------------------------------------------
run_case "enforce, old client, both journeys fail, refused counted, ack -> EXPECTED-INCOMPATIBLE (2)" \
  2 "EXPECTED-INCOMPATIBLE" "$OLD" enforce 0 0 "$LAG" "$S_ENF_SEEN"
# nexus-9a6io fix round: the ack must not hide a failure that is not the refusal.
run_case "enforce, store refused but index md failed for an unrelated reason (refused_total 3) -> ack refused (1)" \
  1 "ack gap: the index md journey's output does not name the ownerless refusal" "$OLD" enforce 0 0 "$LAG" "$S_ENF_SEEN" 1 0
run_case "enforce, index md refused but store put failed for an unrelated reason -> ack refused (1)" \
  1 "ack gap: the store put journey's output does not name the ownerless refusal" "$OLD" enforce 0 0 "$LAG" "$S_ENF_SEEN" 0 1
run_case "enforce, only ONE journey failed (the other succeeded), refused counted -> ack refused (1)" \
  1 "ack gap: a journey succeeded" "$OLD" enforce 0 1 "$LAG" "$S_ENF_SEEN" 1 0
run_case "enforce, both journeys name the refusal but the engine counted only one -> ack refused (1)" \
  1 "fewer than the 2 refusals" "$OLD" enforce 0 0 "$LAG" "$S_ENF_ONE"
run_case "enforce, old client, journeys fail, no ack -> FAILED (1) with the ack hint" \
  1 "re-run with NX_EXPECTED_CLIENT_LAG=$LAG" "$OLD" enforce 0 0 "" "$S_ENF_SEEN"
run_case "enforce, old client, journeys fail, ack, but the engine refused nothing -> oracle fails" \
  1 "enforce engine refused no ownerless write" "$OLD" enforce 0 0 "$LAG" "$S_ENF_QUIET"
run_case "enforce, old client, journeys PASS and nothing refused -> oracle fails (nothing legacy was exercised)" \
  1 "enforcement is dead" "$OLD" enforce 1 1 "" "$S_ENF_QUIET"

# --- a Phase 2 client (>= FIXED_IN_VERSION) --------------------------------
run_case "enforce, fixed client, both ok, counters zero -> PASSED (0)" \
  0 "PUBLISHED-CLIENT WRITE GATE PASSED" "$NEW" enforce 1 1 "" "$S_ENF_QUIET"
run_case "enforce, fixed client but the engine refused it -> oracle fails" \
  1 "wrote an ownerless chunk" "$NEW" enforce 1 1 "" "$S_ENF_SEEN"
run_case "log-only, fixed client but would-refuse counted -> oracle fails" \
  1 "wrote an ownerless chunk" "$NEW" log-only 1 1 "" "$S_LOG_SEEN"
run_case "fixed client fails and the ack is set -> stale ack refused (1)" \
  1 "ACKNOWLEDGMENT REFUSED (stale)" "$NEW" enforce 0 0 "$LAG" "$S_ENF_QUIET"

# --- the explicit mode against an engine that cannot report it -------------
run_case "explicit mode, engine reports no ownerless_write_mode -> fails (candidate lacks P3.2)" \
  1 "lacks the RDR-223 P3.2 refusal" "$OLD" enforce 1 1 "" "$S_NONE"
run_case "explicit mode, status unreadable -> fails" \
  1 "lacks the RDR-223 P3.2 refusal" "$OLD" log-only 1 1 "" ""

# --- mode unset: the pre-P3.2 behaviour is unchanged -----------------------
run_case "mode unset, old engine (no fields), both ok -> PASSED (0)" \
  0 "PUBLISHED-CLIENT WRITE GATE PASSED" "$OLD" "" 1 1 "" "$S_NONE"
run_case "mode unset, journeys fail, no ack -> FAILED (1)" \
  1 "PUBLISHED-CLIENT WRITE GATE FAILED" "$OLD" "" 0 1 "" "$S_NONE"
run_case "mode unset, journeys fail, ack, engine reports a refusal -> EXPECTED-INCOMPATIBLE (2)" \
  2 "EXPECTED-INCOMPATIBLE" "$OLD" "" 0 0 "$LAG" "$S_ENF_SEEN"
run_case "mode unset, journeys fail, ack, engine reports no refusal -> ack refused (1)" \
  1 "no evidence" "$OLD" "" 0 0 "$LAG" "$S_NONE"

echo "$NAME: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
