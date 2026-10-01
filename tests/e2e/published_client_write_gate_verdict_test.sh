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

# --- non-integer counters: refused loudly, never read as "no gap" ------------
# `[ "$x" -lt 2 ]` on "2.0" errors inside an `if`, is false, and let the ack pass.
S_ENF_FLOAT='{"ownerless_write_mode":"enforce","ownerless_writes_refused_total":2.0,"ownerless_writes_would_refuse_total":0}'
S_ENF_WORD='{"ownerless_write_mode":"enforce","ownerless_writes_refused_total":"unknown","ownerless_writes_would_refuse_total":0}'
S_LOG_FLOAT='{"ownerless_write_mode":"log-only","ownerless_writes_refused_total":0,"ownerless_writes_would_refuse_total":1.0}'
run_case "enforce + ack, refused_total is 2.0 (a float) -> FAILED (1), not an ack pass" \
  1 "not a non-negative integer" "$OLD" enforce 0 0 "$LAG" "$S_ENF_FLOAT"
run_case "enforce + ack, refused_total is a word -> FAILED (1)" \
  1 "not a non-negative integer" "$OLD" enforce 0 0 "$LAG" "$S_ENF_WORD"
run_case "log-only, would_refuse_total is 1.0 -> FAILED (1), not a read of the -lt 1 oracle" \
  1 "not a non-negative integer" "$OLD" log-only 1 1 "" "$S_LOG_FLOAT"
run_case "mode unset, counters absent -> still PASSED (0): absent is not non-integer" \
  0 "PUBLISHED-CLIENT WRITE GATE PASSED" "$OLD" "" 1 1 "" "$S_NONE"

# --- the refusal classifier, sourced from the REAL script ---------------------
# _names_ownerless_refusal is what lets the ack tell "the engine refused this
# journey" from "this journey broke for another reason". The cases above inject
# STORE_REFUSED / MD_REFUSED directly, so they could not see it: breaking the
# pattern left them green. This section sources the function from the script and
# feeds it canned client output.
sed -n '/^_names_ownerless_refusal() {/,/^}/p' "$GATE" > "$TMP/classifier.sh"
if [ "$(wc -l < "$TMP/classifier.sh")" -lt 4 ]; then
  FAIL=$((FAIL + 1)); echo "[FAIL] _names_ownerless_refusal not found in $GATE"
fi

# classify <label> <want: yes|no> <canned client output>
classify() {
  local label="$1" want="$2" text="$3" got
  if ( source "$TMP/classifier.sh"; _names_ownerless_refusal "$text" ); then got=yes; else got=no; fi
  if [ "$got" = "$want" ]; then
    PASS=$((PASS + 1)); echo "[ok]   classifier: $label"
  else
    FAIL=$((FAIL + 1)); echo "[FAIL] classifier: $label: want $want, got $got"
  fi
}

# MEASURED 2026-10-01: published conexus 7.67.0 against the z0o2p.24 candidate in
# enforce mode (NX_PUBLISHED_CLIENT_VERSION=7.67.0 NX_GATE_OWNERLESS_WRITE_MODE=enforce
# NX_EXPECTED_CLIENT_LAG=nexus-z0o2p.24, exit 2). These are the two real journey lines,
# trimmed of the collection name and chash tail (the gate prints them as "refusal line").
# `nx store put` surfaces the engine's 422 inside a structlog compensation warning, not a
# traceback, and `nx index md` prints a one-line Error; both carry the engine's prose.
STORE_PUT_7_67_0="event='store_put_ghost_register_compensated' timestamp='2026-10-01T21:46:58.203348Z' level='warning' tumbler='1.1.1' deleted=True original_error=\"POST /v1/vectors/store-put → HTTP 422: refusing an ownerless chunk write on store-put: 1 of 1 chashes have no live manifest row in collection 'knowledge__pcwg"
INDEX_MD_7_67_0="Error: the nexus service returned an error: POST /v1/vectors/upsert-chunks → HTTP 422: refusing an ownerless chunk write on upsert-chunks: 1 of 1 chashes have no live manifest row in collection 'docs__pcwg-gate__bge-base-en-v15-768__v1' (e.g. d00e344f6ee625b0adf314b72e56d781fad7322be97f2f58f71db3a19"
classify "7.67.0 store put output (measured) -> refusal" yes "$STORE_PUT_7_67_0"
classify "7.67.0 index md output (measured) -> refusal" yes "$INDEX_MD_7_67_0"
classify "a traceback ending in the engine's 422 prose -> refusal" yes 'Traceback (most recent call last):
  File "nexus/db/http_vector_client.py", line 1674, in _request
nexus.errors.ServiceError: POST /v1/vectors/store-put → HTTP 422: refusing an ownerless chunk write on store-put: 1 of 1 chashes have no live manifest row'
classify "the reason token alone (ownerless_chunk_write) -> refusal" yes 'error: 422 {"reason": "ownerless_chunk_write"}'
classify "HTTP 500 from the engine -> not a refusal" no 'nexus.errors.ServiceError: POST /v1/vectors/store-put failed: HTTP 500: internal error'
classify "connection refused -> not a refusal" no 'httpx.ConnectError: [Errno 61] Connection refused'
classify "a 422 for another reason -> not a refusal" no 'ServiceError: POST /v1/vectors/upsert-chunks failed: HTTP 422: invalid collection name'
classify "empty output -> not a refusal" no ''
# A Phase 2+ client's own message may use the word without a refusal behind it
# (errors.py DryRunStoreError: the engine's client would take the same ownerless
# upsert-chunks request as a real write); a bare "ownerless" must not ride the ack.
classify "a client's own 'ownerless' prose, no engine refusal -> not a refusal" no \
  'DryRunStoreError: a dry run was handed a store that is not a throwaway one; an ownerless upsert-chunks request would be refused'

# --- the call sites: the journeys must SET STORE_REFUSED / MD_REFUSED from their own output ---
# The cases above inject those variables and the classifier cases call the function bare, so
# replacing the call in the script with `if :;` left every one green (round 3 code L2). These
# run the REAL journey lines (from the output assignment through the _journey_evidence call)
# with `_client_nx` stubbed to print canned client output, and read the variable they set.
sed -n '/^STORE_PUT_OUT=/,/^_journey_evidence "store put"/p' "$GATE" > "$TMP/store_seg.sh"
sed -n '/^MD_OUT=/,/^_journey_evidence "index md"/p' "$GATE" > "$TMP/md_seg.sh"
sed -n '/^_scrub_console_line() {/,/^}/p' "$GATE" > "$TMP/scrub.sh"
sed -n '/^_journey_evidence() {/,/^}/p' "$GATE" > "$TMP/evidence.sh"
for f in store_seg md_seg scrub evidence; do
  if [ "$(wc -l < "$TMP/$f.sh")" -lt 3 ]; then
    FAIL=$((FAIL + 1)); echo "[FAIL] could not extract $f from $GATE"
  fi
done

# site <label> <store|md> <want-flag 0|1> <canned client output>
site() {
  local label="$1" which="$2" want="$3" canned="$4" got seg var
  if [ "$which" = store ]; then seg="$TMP/store_seg.sh"; var=STORE_REFUSED; else seg="$TMP/md_seg.sh"; var=MD_REFUSED; fi
  # shellcheck disable=SC2034  # read by the sourced journey segment
  got="$(
    LOGS="$TMP" WORK="$TMP" RUN_ID=1 STORE_TITLE=t MD_FIXTURE="$TMP/fixture.md" CANNED="$canned"
    export CANNED
    STORE_REFUSED=0; MD_REFUSED=0
    _client_nx() { printf '%s\n' "$CANNED"; }
    # shellcheck disable=SC1090
    source "$TMP/classifier.sh"; source "$TMP/scrub.sh"; source "$TMP/evidence.sh"
    # shellcheck disable=SC1090
    source "$seg" > /dev/null 2>&1
    eval "echo \$$var"
  )"
  if [ "$got" = "$want" ]; then
    PASS=$((PASS + 1)); echo "[ok]   call site: $label"
  else
    FAIL=$((FAIL + 1)); echo "[FAIL] call site: $label: $var want $want, got '$got'"
  fi
}
site "store put naming the refusal sets STORE_REFUSED=1" store 1 "$STORE_PUT_7_67_0"
site "store put failing with an HTTP 500 leaves STORE_REFUSED=0" store 0 'POST /v1/vectors/store-put failed: HTTP 500: internal error'
site "index md naming the refusal sets MD_REFUSED=1" md 1 "$INDEX_MD_7_67_0"
site "index md failing for another reason leaves MD_REFUSED=0" md 0 'httpx.ConnectError: [Errno 61] Connection refused'

# evidence <label> <canned output> <want-substring|-> <must-not-contain|->
evidence() {
  local label="$1" canned="$2" want="$3" unwanted="$4" out
  out="$( source "$TMP/classifier.sh"; source "$TMP/scrub.sh"; source "$TMP/evidence.sh"; _journey_evidence "store put" "$canned" )"
  if { [ "$want" = "-" ] || [[ "$out" == *"$want"* ]]; } && { [ "$unwanted" = "-" ] || [[ "$out" != *"$unwanted"* ]]; }; then
    PASS=$((PASS + 1)); echo "[ok]   evidence: $label"
  else
    FAIL=$((FAIL + 1)); echo "[FAIL] evidence: $label: want '$want', unwanted '$unwanted', got:"
    printf '%s\n' "$out" | sed 's/^/         /'
  fi
}
evidence "a refusal prints its own line" "$STORE_PUT_7_67_0" "refusal line: event='store_put_ghost_register_compensated'" -
evidence "no refusal prints the last non-empty line" $'first line\nsecond line\n\n' "last output line: second line" "first line"
evidence "a bearer token in the client output is redacted" \
  $'Error: HTTP 401\nrequest failed with Authorization: Bearer abc123SECRETvalue for /v1/x' "[redacted]" "abc123SECRETvalue"
evidence "a key=value secret is redacted" 'config error: api_key=sk-live-0123456789 rejected' "[redacted]" "sk-live-0123456789"
evidence "a long opaque run is masked" "boom d00e344f6ee625b0adf314b72e56d781fad7322be97f2f58f71db3a1993939aa" "[long-token-redacted]" "d00e344f6ee625b0adf314b72e56d781fad7322be97f2f58f71db3a1993939aa"
evidence "control characters are dropped" $'weird\x1b[31mred\x07 text' "weird[31mred text" $'\x1b'
long="$(printf 'x%.0s ' $(seq 1 400))"
out_long="$( source "$TMP/classifier.sh"; source "$TMP/scrub.sh"; source "$TMP/evidence.sh"; _journey_evidence "index md" "$long" )"
if [ "${#out_long}" -le 360 ]; then PASS=$((PASS + 1)); echo "[ok]   evidence: the printed line is capped (${#out_long} chars)"
else FAIL=$((FAIL + 1)); echo "[FAIL] evidence: output not capped (${#out_long} chars)"; fi
out_empty="$( source "$TMP/classifier.sh"; source "$TMP/scrub.sh"; source "$TMP/evidence.sh"; _journey_evidence "index md" "" )"
if [ -z "$out_empty" ]; then PASS=$((PASS + 1)); echo "[ok]   evidence: empty output prints nothing"
else FAIL=$((FAIL + 1)); echo "[FAIL] evidence: empty output printed '$out_empty'"; fi

echo "$NAME: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
