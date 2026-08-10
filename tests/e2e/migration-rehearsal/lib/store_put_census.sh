#!/usr/bin/env bash
# tests/e2e/migration-rehearsal/lib/store_put_census.sh — nexus-xm0cp
#
# Extracted from rehearse_shakeout.sh Phase D so the concurrent-store-put
# census logic (client-rc failures AND absorbed-gateway-retry detection) can
# be exercised by a durable pytest self-test
# (tests/test_shakeout_store_put_census.py) without provisioning the full
# container/service stack. Both the real gate and the self-test source and
# call this SAME function — not a reimplementation that could silently
# drift out of sync with what actually ships.
#
# census_concurrent_store_puts <n> <log-prefix> [doc-dir=/tmp]
#   Runs `nx store put` n times against synthesized "<doc-dir>/load-$i.md"
#   documents, writing each call's combined stdout+stderr to
#   "<log-prefix>-$i.log". doc-dir defaults to /tmp, matching the ORIGINAL
#   inline Phase D loop's hardcoded path exactly (so the real gate's
#   behavior/log locations are unchanged); the pytest self-test passes its
#   own tmp_path here so parallel test runs never race on a shared /tmp
#   filename.
#
#   Sets (bash has no return-by-value for this shape) in the CALLER's
#   scope:
#     STORE_FAILS      — count of calls that exited non-zero
#     STORE_FAIL_IDX    — space-separated indices of those calls
#     STORE_RETRIES     — count of calls whose combined log contains a
#                         vector_gateway_retry line — HttpVectorClient
#                         absorbed a 502/503/504 within its bounded retry
#                         budget (http_vector_client.py's
#                         _GATEWAY_RETRY_CODES / _GATEWAY_RETRY_SLEEPS)
#                         WITHOUT the call ever reaching a non-zero exit.
#                         nexus-xm0cp Finding 2: this is exactly the shape
#                         a lock convoy is most likely to take, and a bare
#                         client-rc census is structurally blind to it.
#     STORE_RETRY_IDX    — space-separated indices of those calls
census_concurrent_store_puts() {
  local n="$1" log_prefix="$2" doc_dir="${3:-/tmp}"
  STORE_FAILS=0
  STORE_FAIL_IDX=""
  STORE_RETRIES=0
  STORE_RETRY_IDX=""
  local i logf docf
  for i in $(seq 1 "$n"); do
    docf="$doc_dir/load-$i.md"
    printf '# load doc %d\ncontent %d\n' "$i" "$i" > "$docf"
    logf="${log_prefix}-$i.log"
    if ! nx store put "$docf" --collection knowledge__shakeout --title "load-$i" >"$logf" 2>&1; then
      STORE_FAILS=$((STORE_FAILS + 1))
      STORE_FAIL_IDX="$STORE_FAIL_IDX $i"
    fi
    if grep -q "vector_gateway_retry" "$logf" 2>/dev/null; then
      STORE_RETRIES=$((STORE_RETRIES + 1))
      STORE_RETRY_IDX="$STORE_RETRY_IDX $i"
    fi
  done
}
