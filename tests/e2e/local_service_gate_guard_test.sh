#!/usr/bin/env bash
# tests/e2e/local_service_gate_guard_test.sh — wiring test for
# local-service-gate.sh's OWN release.properties guard/stamp/lease code
# (bead nexus-iexvl, critic finding 3 on commit 40963aeb5).
#
# local-service-gate.sh cannot be run end-to-end here the way
# tests/e2e/migration-rehearsal/run_sh_guard_test.sh runs a stubbed copy of
# run.sh: reaching its guard for real requires `nx init --service` to have
# already provisioned a throwaway PG cluster and installed a service
# binary — self-provisioning infra genuinely out of scope for a shell
# test (and for this fix round's box rules: no native builds, no real
# service init). Instead this test EXTRACTS the exact guard/snapshot
# fragment out of the REAL file, byte-for-byte, via a fixed sed range
# anchored on two literal lines that must both still be present — an
# empty extraction (the anchors no longer matching, e.g. after a refactor)
# is asserted non-empty and FAILS LOUD rather than silently skipping, per
# this repo's vacuous-gate doctrine. The extracted fragment is executed as
# its own real `bash` process (its `exit 75` on refusal must not kill this
# test), with `release_props_guard_clean` / `build_lease_acquire_wait` /
# `build_lease_release` sourced from the REAL scripts/lib/*.sh (already
# covered end-to-end by scripts/lib/release-props-lease_test.sh) and
# RELEASE_PROPS/GATE_STAMP/GATE_BUILD_REF/RELEASE_PROPS_SNAPSHOT set the
# same way local-service-gate.sh itself sets them at this point.
#
# Test 1: a pre-dirtied release.properties refuses (rc 75, REFUSED) and
#   the file is left exactly as dirtied (no snapshot ever taken).
# Test 2: the nexus-iexvl reproduction at local-service-gate.sh's OWN
#   level. A concurrent "stamper B" dirties release.properties and holds
#   the lease for a couple of seconds before restoring; the extracted
#   fragment ("A") is started while B's dirty bytes are on disk, then (as
#   the real script does on a successful build) the two restore helper
#   functions the fragment defines are invoked. With the fix — the
#   snapshot is taken INSIDE this fragment, after the guard has passed —
#   A's restore lands on the true baseline. Reverting to commit
#   40963aeb5's shape (RELEASE_PROPS_SNAPSHOT taken at the top of the
#   whole script, unconditionally, before the lease is ever acquired —
#   see this test's own header for the exact before/after) makes A adopt
#   B's abandoned stamp instead; verified by hand against that shape
#   while writing this test (not re-verified on every run, since the
#   fragment this test extracts today already contains the fix — see the
#   code-review pass notes for the manual revert-and-rerun record).
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

# ── Non-vacuity: extract the guard fragment, fail loud if the anchors
# no longer match (a silent empty extraction would make every assertion
# below vacuously true against a no-op fragment).
fragment="$(sed -n '/^  build_lease_acquire_wait service/,/_restore_props_and_release_lease/p' "$REPO_ROOT/tests/e2e/local-service-gate.sh")"
if [[ -z "$fragment" ]]; then
  bad "extraction anchors matched nothing — local-service-gate.sh's guard block moved or was renamed; this test needs updating, not a silent pass"
  echo
  echo "local_service_gate_guard_test.sh: $PASS passed, $FAIL failed"
  exit 1
fi
line_count="$(printf '%s\n' "$fragment" | wc -l | tr -d ' ')"
[[ "$line_count" -ge 10 ]] && ok "extraction non-vacuity: fragment has $line_count lines" || bad "extraction suspiciously short ($line_count lines): $fragment"

repo="$WORKDIR/repo"
props_rel="service/src/main/resources/META-INF/nexus/release.properties"
mkdir -p "$repo/scripts/lib" "$repo/service/src/main/resources/META-INF/nexus"
cp "$REPO_ROOT/scripts/lib/build-lease.sh" "$repo/scripts/lib/build-lease.sh"
cp "$REPO_ROOT/scripts/lib/release-props-lease.sh" "$repo/scripts/lib/release-props-lease.sh"
printf 'release_version=\nbuild_ref=\nname=nexus\n' > "$repo/$props_rel"
git -C "$repo" init -q
git -C "$repo" -c user.email=test@test -c user.name=test add -A
git -C "$repo" -c user.email=test@test -c user.name=test commit -q -m init

make_harness() {
  # $1 = harness script path to write
  local out="$1"
  {
    echo '#!/usr/bin/env bash'
    echo 'set -euo pipefail'
    echo "source \"$repo/scripts/lib/release-props-lease.sh\""
    echo "RELEASE_PROPS=\"$repo/$props_rel\""
    echo 'GATE_STAMP=test-stamp'
    echo 'GATE_BUILD_REF=test-build-ref'
    echo 'RELEASE_PROPS_SNAPSHOT=""'
    printf '%s\n' "$fragment"
    echo '_restore_props_and_release_lease'
  } > "$out"
}

harness="$WORKDIR/harness.sh"
make_harness "$harness"

echo "Test 1: pre-dirtied release.properties refuses before any snapshot is taken"
printf 'release_version=9.9.8\nbuild_ref=abandoned-B\nname=nexus\n' > "$repo/$props_rel"
out1="$(NX_BUILD_LEASE_WAIT=5 bash "$harness" 2>&1)"; rc1=$?
[[ $rc1 -eq 75 ]] && ok "refused with rc 75" || bad "expected rc 75, got $rc1: $out1"
[[ "$out1" == *"REFUSED"* ]] && ok "refusal is disclosed (REFUSED)" || bad "no REFUSED text: $out1"
actual1="$(cat "$repo/$props_rel")"
[[ "$actual1" == "release_version=9.9.8"* ]] && ok "tree left exactly as dirtied" || bad "tree changed after refusal: $actual1"
lease_dir_common="$(git -C "$repo" rev-parse --path-format=absolute --git-common-dir 2>/dev/null)/nexus-build-lease/service"
[[ ! -d "$lease_dir_common" ]] && ok "lease not left held after a refused guard" || bad "lease still held after refusal: $lease_dir_common"

echo "Test 2: nexus-iexvl reproduction — concurrent stamper B, snapshot taken after the guard"
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
[[ "$b_ready" -eq 1 ]] && ok "stamper B reached its dirty-and-holding state" || bad "stamper B never signaled ready — test setup itself is broken"
out2="$(NX_BUILD_LEASE_WAIT=10 bash "$harness" 2>&1)"; rc2=$?
wait "$b_pid"
[[ $rc2 -eq 0 ]] && ok "fragment completed (rc 0) after B released" || bad "fragment did not complete cleanly (rc $rc2): $out2"
final="$(cat "$repo/$props_rel")"
if [[ "$final" == "$baseline" ]]; then
  ok "final tree equals the true baseline — B's abandoned stamp was never adopted"
else
  bad "final tree does not equal the true baseline (nexus-iexvl reproduced): $final"
fi
[[ ! -d "$lease_dir_common" ]] && ok "lease released after the round trip" || bad "lease still held: $lease_dir_common"

echo
echo "local_service_gate_guard_test.sh: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]
