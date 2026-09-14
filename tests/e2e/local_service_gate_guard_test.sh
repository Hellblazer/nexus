#!/usr/bin/env bash
# tests/e2e/local_service_gate_guard_test.sh — wiring test for
# local-service-gate.sh's OWN release.properties guard/stamp/lease code
# (bead nexus-iexvl).
#
# ROUND 3 (ec4b27761 -> this fix): local-service-gate.sh's stamp block now
# routes through scripts/lib/release-props-lease.sh's
# release_props_stamp_under_lease / release_props_restore_and_release,
# instead of hand-rolled _restore_props / _restore_props_and_release_lease
# functions that never cleared RELEASE_PROPS_SNAPSHOT or removed the
# snapshot temp file after restoring. This test extracts the REAL current
# lines out of the real file -- fail loud (no silent vacuous pass) if any
# anchor stops matching:
#   - the stamp call (one line: acquire the lease, guard clean, snapshot,
#     stamp -- all inside release_props_stamp_under_lease)
#   - the success-path restore call (release_props_restore_and_release,
#     plus the RELEASE_PROPS_SNAPSHOT="" clear right after it)
#   - cleanup()'s own trailing backstop restore (cp/rm/build_lease_release)
#
# Test 1: a pre-dirtied release.properties refuses (rc 75, REFUSED) and
#   the file is left exactly as dirtied (no snapshot ever taken).
# Test 2: nexus-iexvl (round 1/2) reproduction at local-service-gate.sh's
#   OWN level -- a concurrent "stamper B" dirties release.properties and
#   holds the lease before restoring; the extracted stamp call only
#   snapshots AFTER the guard has passed, so B's abandoned stamp is never
#   adopted as the true baseline.
# Test 3 (round 3, THIS fix): the trailing-snapshot clobber. Runs the
#   extracted stamp call + success-path restore (exactly what
#   local-service-gate.sh does when its jar build succeeds), THEN fires
#   the extracted cleanup() backstop a second time -- as it does at the
#   real script's own EXIT, potentially minutes later -- while a
#   concurrent, legitimate stamper is mid-build holding the freed lease.
#   On ec4b27761 (RELEASE_PROPS_SNAPSHOT and the snapshot file were never
#   cleared after the success-path restore) the trailing backstop
#   re-applies the stale snapshot over the concurrent stamper's in-flight
#   bytes -- CLOBBERED. With this fix (RELEASE_PROPS_SNAPSHOT cleared to ""
#   right after the restore) the backstop is a no-op.
#
# Run with: bash tests/e2e/local_service_gate_guard_test.sh
set -u -o pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# HERE is tests/e2e; REPO_ROOT (the repo checkout root) is two levels up.
REPO_ROOT="$(cd "$HERE/../.." && pwd)"
WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/local_service_gate_guard_test.XXXXXX")"
trap 'rm -rf "$WORKDIR"' EXIT
PASS=0; FAIL=0
ok()  { echo "  [ok] $1"; PASS=$((PASS + 1)); }
bad() { echo "  [FAIL] $1"; FAIL=$((FAIL + 1)); }

fail_loud_and_exit() {
  bad "$1"
  echo
  echo "local_service_gate_guard_test.sh: $PASS passed, $FAIL failed"
  exit 1
}

GATE_FILE="$REPO_ROOT/tests/e2e/local-service-gate.sh"

# -- Non-vacuity: extract the three fragments this test drives, fail loud
# if any anchor no longer matches (a silent empty extraction would make
# every assertion below vacuously true against a no-op fragment).
stamp_line="$(grep -n 'RELEASE_PROPS_SNAPSHOT="\$(release_props_stamp_under_lease' "$GATE_FILE" | tail -1 | cut -d: -f1)"
[[ -n "$stamp_line" ]] || fail_loud_and_exit "stamp-call anchor matched nothing -- local-service-gate.sh's stamp line moved or was renamed; this test needs updating, not a silent pass"
stamp_fragment="$(sed -n "${stamp_line}p" "$GATE_FILE")"
[[ "$stamp_fragment" == *"GATE_STAMP"* && "$stamp_fragment" == *"GATE_BUILD_REF"* ]] || fail_loud_and_exit "stamp-call fragment does not reference GATE_STAMP/GATE_BUILD_REF -- extraction landed on the wrong line: $stamp_fragment"
ok "stamp-call extraction non-vacuous"

restore_line="$(grep -n 'release_props_restore_and_release "\$RELEASE_PROPS" "\$RELEASE_PROPS_SNAPSHOT" service' "$GATE_FILE" | tail -1 | cut -d: -f1)"
[[ -n "$restore_line" ]] || fail_loud_and_exit "success-path restore-call anchor matched nothing -- this test needs updating"
restore_fragment="$(sed -n "${restore_line},$((restore_line + 1))p" "$GATE_FILE")"
[[ "$restore_fragment" == *'RELEASE_PROPS_SNAPSHOT=""'* ]] || fail_loud_and_exit "round-3 regression: the success-path restore no longer clears RELEASE_PROPS_SNAPSHOT right after restoring: $restore_fragment"
ok "success-path restore extraction non-vacuous and clears the snapshot var"

cleanup_fragment="$(sed -n '/# Restore the pre-invocation BYTES, never HEAD (nexus-iws18)\./,/build_lease_release service/p' "$GATE_FILE")"
[[ -n "$cleanup_fragment" ]] || fail_loud_and_exit "cleanup() backstop-restore anchor matched nothing -- this test needs updating"
cleanup_line_count="$(printf '%s\n' "$cleanup_fragment" | wc -l | tr -d ' ')"
[[ "$cleanup_line_count" -ge 3 ]] && ok "cleanup() backstop extraction non-vacuous: $cleanup_line_count lines" || bad "cleanup() backstop extraction suspiciously short ($cleanup_line_count lines): $cleanup_fragment"

repo="$WORKDIR/repo"
props_rel="service/src/main/resources/META-INF/nexus/release.properties"
mkdir -p "$repo/scripts/lib" "$repo/service/src/main/resources/META-INF/nexus"
cp "$REPO_ROOT/scripts/lib/build-lease.sh" "$repo/scripts/lib/build-lease.sh"
cp "$REPO_ROOT/scripts/lib/release-props-lease.sh" "$repo/scripts/lib/release-props-lease.sh"
printf 'release_version=\nbuild_ref=\nname=nexus\n' > "$repo/$props_rel"
git -C "$repo" init -q
git -C "$repo" -c user.email=test@test -c user.name=test add -A
git -C "$repo" -c user.email=test@test -c user.name=test commit -q -m init
git_common_dir="$(git -C "$repo" rev-parse --path-format=absolute --git-common-dir 2>/dev/null)"
lease_dir_common="$git_common_dir/nexus-build-lease/service"

# make_harness <out> <GATE_STAMP> <GATE_BUILD_REF> [<trailer-fragment>]
# Emits: source the real lib -> stamp call -> simulated build -> success-
# path restore -> optional trailer (Test 3's cleanup() backstop re-fire).
make_harness() {
  local out="$1" stamp="$2" build_ref="$3" trailer="${4:-}"
  {
    echo '#!/usr/bin/env bash'
    echo 'set -euo pipefail'
    echo "source \"$repo/scripts/lib/release-props-lease.sh\""
    echo "RELEASE_PROPS=\"$repo/$props_rel\""
    echo "GATE_STAMP=$stamp"
    echo "GATE_BUILD_REF=$build_ref"
    echo 'RELEASE_PROPS_SNAPSHOT=""'
    printf '%s\n' "$stamp_fragment"
    echo 'sleep 0.3   # simulate the mvnw build'
    printf '%s\n' "$restore_fragment"
    if [[ -n "$trailer" ]]; then
      echo 'sleep 1.5   # simulate the rest of local-service-gate.sh (smoke/e2e legs)'
      printf '%s\n' "$trailer"
    fi
  } > "$out"
}

harness="$WORKDIR/harness.sh"
make_harness "$harness" test-stamp test-build-ref

echo "Test 1: pre-dirtied release.properties refuses before any snapshot is taken"
printf 'release_version=9.9.8\nbuild_ref=abandoned-B\nname=nexus\n' > "$repo/$props_rel"
out1="$(NX_BUILD_LEASE_WAIT=5 bash "$harness" 2>&1)"; rc1=$?
[[ $rc1 -eq 75 ]] && ok "refused with rc 75" || bad "expected rc 75, got $rc1: $out1"
[[ "$out1" == *"REFUSED"* ]] && ok "refusal is disclosed (REFUSED)" || bad "no REFUSED text: $out1"
actual1="$(cat "$repo/$props_rel")"
[[ "$actual1" == "release_version=9.9.8"* ]] && ok "tree left exactly as dirtied" || bad "tree changed after refusal: $actual1"
[[ ! -d "$lease_dir_common" ]] && ok "lease not left held after a refused guard" || bad "lease still held after refusal: $lease_dir_common"

echo "Test 2: nexus-iexvl reproduction -- concurrent stamper B, snapshot taken after the guard"
git -C "$repo" checkout -q -- "$props_rel"
baseline="$(cat "$repo/$props_rel")"
ready="$WORKDIR/b-dirty-ready"
rm -f "$ready"
(
  # shellcheck source=/dev/null
  source "$repo/scripts/lib/release-props-lease.sh"
  build_lease_acquire_wait service 10 fake-B-holder >/dev/null 2>&1
  printf 'release_version=B-DIRTY\nbuild_ref=B-DIRTY\nname=nexus\n' > "$repo/$props_rel"
  touch "$ready"
  sleep 2
  printf '%s' "$baseline" > "$repo/$props_rel"
  build_lease_release service
) &
b_pid=$!
b_ready=0
for _ in $(seq 1 50); do
  if [[ -f "$ready" ]]; then b_ready=1; break; fi
  sleep 0.1
done
[[ "$b_ready" -eq 1 ]] && ok "stamper B reached its dirty-and-holding state" || bad "stamper B never signaled ready -- test setup itself is broken"
out2="$(NX_BUILD_LEASE_WAIT=10 bash "$harness" 2>&1)"; rc2=$?
wait "$b_pid"
[[ $rc2 -eq 0 ]] && ok "fragment completed (rc 0) after B released" || bad "fragment did not complete cleanly (rc $rc2): $out2"
final="$(cat "$repo/$props_rel")"
if [[ "$final" == "$baseline" ]]; then
  ok "final tree equals the true baseline -- B's abandoned stamp was never adopted"
else
  bad "final tree does not equal the true baseline (nexus-iexvl reproduced): $final"
fi
[[ ! -d "$lease_dir_common" ]] && ok "lease released after the round trip" || bad "lease still held: $lease_dir_common"

echo "Test 3 (round 3, nexus-iexvl): trailing-snapshot clobber after the success-path restore"
git -C "$repo" checkout -q -- "$props_rel"
baseline3="$(cat "$repo/$props_rel")"
harness_p="$WORKDIR/harness_p.sh"
make_harness "$harness_p" P-STAMP P-BUILD "$cleanup_fragment"

(
  NX_BUILD_LEASE_WAIT=10 bash "$harness_p" >"$WORKDIR/p.out" 2>&1
) &
p_pid=$!

sleep 0.6   # let P finish its own stamp+build+restore, then start Q mid-P's "smoke leg"

(
  # shellcheck source=/dev/null
  source "$repo/scripts/lib/release-props-lease.sh"
  q_snap="$(release_props_stamp_under_lease "$repo/$props_rel" service 10 "release_version=Q-STAMP" "build_ref=Q-BUILD")" || exit $?
  sleep 2.0   # Q's own build window -- overlaps P's cleanup() at ~1.8s mark
  cat "$repo/$props_rel" > "$WORKDIR/q-mid-content"
  release_props_restore_and_release "$repo/$props_rel" "$q_snap" service
) &
q_pid=$!

wait "$p_pid"; p_rc=$?
wait "$q_pid"; q_rc=$?
[[ $p_rc -eq 0 ]] && ok "P's harness completed cleanly (rc 0)" || bad "P's harness failed (rc $p_rc): $(cat "$WORKDIR/p.out")"
[[ $q_rc -eq 0 ]] && ok "Q's harness completed cleanly (rc 0)" || bad "Q's harness failed (rc $q_rc)"

q_mid="$(cat "$WORKDIR/q-mid-content" 2>/dev/null || echo "")"
if [[ "$q_mid" == *"Q-STAMP"* ]]; then
  ok "Q's own stamp survived P's trailing cleanup() restore (round-3 fix holds)"
else
  bad "round-3 regression reproduced: P's trailing cleanup() clobbered Q's in-flight stamp: $q_mid"
fi
final3="$(cat "$repo/$props_rel")"
[[ "$final3" == "$baseline3" ]] && ok "final tree equals the true baseline after both P and Q finish" || bad "final tree does not equal the true baseline: $final3"
[[ ! -d "$lease_dir_common" ]] && ok "lease released after both P and Q finish" || bad "lease still held: $lease_dir_common"

echo
echo "local_service_gate_guard_test.sh: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]
