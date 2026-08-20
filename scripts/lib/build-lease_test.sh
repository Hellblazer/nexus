#!/usr/bin/env bash
# scripts/lib/build-lease_test.sh — unit-level shell tests for
# build-lease.sh (bead nexus-c00dw). Self-provisioning: builds its own
# throwaway fake-repo tmpdir (never the real checkout's service/ —
# service/ is off-limits to this test, another agent owns it), no
# dependency on pytest or any engine substrate. Run directly with bash:
#   bash scripts/lib/build-lease_test.sh
#
# Mirrors tests/e2e/lib/lock_test.sh's ok/bad/PASS/FAIL convention.
set -u -o pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/build_lease_test.XXXXXX")"
trap 'rm -rf "$WORKDIR"' EXIT

PASS=0
FAIL=0
ok() { echo "  [ok] $1"; PASS=$((PASS + 1)); }
bad() { echo "  [FAIL] $1"; FAIL=$((FAIL + 1)); }

# Fake repo tree: build-lease.sh anchors its repo-root computation on its
# OWN BASH_SOURCE location, so sourcing a COPY placed under
# <fake_repo>/scripts/lib/build-lease.sh makes it operate entirely inside
# <fake_repo>/service/.build-lease — never the real checkout's service/.
_fake_repo() {
    local repo="$1"
    mkdir -p "$repo/scripts/lib" "$repo/service/target" "$repo/service/.build-lease"
    cp "$HERE/build-lease.sh" "$repo/scripts/lib/build-lease.sh"
}

# ── Test 1: acquire/release roundtrip ────────────────────────────────────
echo "Test 1: acquire/release roundtrip"
repo1="$WORKDIR/repo1"
_fake_repo "$repo1"
out1="$(bash -c "source '$repo1/scripts/lib/build-lease.sh'; build_lease_acquire smoke" 2>&1)"
rc1=$?
if [[ $rc1 -eq 0 ]]; then ok "acquire succeeded (rc 0)"; else bad "acquire failed (rc $rc1): $out1"; fi
leasedir1="$repo1/service/.build-lease/smoke"
if [[ -d "$leasedir1" ]]; then ok "lease dir exists after acquire"; else bad "lease dir missing after acquire"; fi
for f in pid ts label command; do
    if [[ -f "$leasedir1/$f" ]]; then ok "lease has '$f' file"; else bad "lease missing '$f' file"; fi
done
bash -c "source '$repo1/scripts/lib/build-lease.sh'; build_lease_acquire smoke >/dev/null 2>&1; build_lease_release smoke"
if [[ ! -d "$leasedir1" ]]; then ok "lease dir gone after release"; else bad "lease dir still present after release"; fi

# ── Test 2: concurrent second acquire refuses loud, rc 75 ────────────────
echo "Test 2: concurrent second acquire refuses (rc 75)"
repo2="$WORKDIR/repo2"
_fake_repo "$repo2"
bash -c "source '$repo2/scripts/lib/build-lease.sh'; NX_AGENT=holder-agent build_lease_acquire svc || exit 9; sleep 10" &
holder_pid=$!
leasedir2="$repo2/service/.build-lease/svc"
for _ in $(seq 1 50); do
    [[ -f "$leasedir2/pid" ]] && break
    sleep 0.1
done
if [[ ! -f "$leasedir2/pid" ]]; then
    bad "background holder never acquired the lease (setup failure)"
else
    out2="$(bash -c "source '$repo2/scripts/lib/build-lease.sh'; build_lease_acquire svc" 2>&1)"
    rc2=$?
    if [[ $rc2 -eq 75 ]]; then
        ok "second concurrent acquire refused with rc 75"
    else
        bad "second concurrent acquire returned rc $rc2 (expected 75); output: $out2"
    fi
    if [[ "$out2" == *"REFUSED"* ]]; then ok "refusal message present"; else bad "no REFUSED message: $out2"; fi
    if [[ "$out2" == *"$holder_pid"* ]]; then ok "refusal names the holder pid"; else bad "refusal missing holder pid: $out2"; fi
    if [[ "$out2" == *"holder-agent"* ]]; then ok "refusal names the holder label"; else bad "refusal missing holder label: $out2"; fi
    if [[ "$out2" == *"build_lease_release"* ]]; then ok "refusal names the remedy (build_lease_release)"; else bad "refusal missing remedy hint: $out2"; fi
fi
kill -9 "$holder_pid" 2>/dev/null
wait "$holder_pid" 2>/dev/null

# ── Test 3: stale-pid takeover ────────────────────────────────────────────
echo "Test 3: stale-pid takeover (dead holder, no WARNING-free silent skip)"
repo3="$WORKDIR/repo3"
_fake_repo "$repo3"
leasedir3="$repo3/service/.build-lease/stale"
# Manufacture a lease left behind by a holder that is definitely dead: spawn
# a subprocess, let it exit, and record its (now-reusable but currently
# unassigned) pid -- this is the same "run it, capture its pid, let it
# finish" technique used for guaranteed-dead pids elsewhere in this repo's
# shell test suite.
( exit 0 ) &
dead_pid=$!
wait "$dead_pid" 2>/dev/null
mkdir -p "$leasedir3"
printf '%s\n' "$dead_pid" > "$leasedir3/pid"
printf '%s\n' "2020-01-01T00:00:00Z" > "$leasedir3/ts"
printf '%s\n' "long-gone-agent" > "$leasedir3/label"
printf '%s\n' "some old command" > "$leasedir3/command"
out3="$(bash -c "source '$repo3/scripts/lib/build-lease.sh'; build_lease_acquire stale" 2>&1)"
rc3=$?
if [[ $rc3 -eq 0 ]]; then ok "stale lease reclaimed successfully (rc 0)"; else bad "stale reclaim failed (rc $rc3): $out3"; fi
if [[ "$out3" == *"WARNING"* ]]; then ok "reclaim is disclosed via a WARNING, not silent"; else bad "no WARNING on stale reclaim: $out3"; fi
if [[ -f "$leasedir3/pid" ]]; then
    new_holder="$(cat "$leasedir3/pid")"
    if [[ "$new_holder" != "$dead_pid" ]]; then ok "lease now records a live (new) pid"; else bad "lease still records the dead pid"; fi
else
    bad "lease pid file missing after reclaim"
fi

# ── Test 4: release-on-trap after a (gracefully) killed holder ──────────
echo "Test 4: SIGTERM'd holder releases via its own EXIT trap"
repo4="$WORKDIR/repo4"
_fake_repo "$repo4"
leasedir4="$repo4/service/.build-lease/trapped"
bash -c "source '$repo4/scripts/lib/build-lease.sh'; build_lease_acquire trapped || exit 9; trap 'build_lease_release trapped' EXIT; sleep 30" &
holder4=$!
for _ in $(seq 1 50); do
    [[ -f "$leasedir4/pid" ]] && break
    sleep 0.1
done
if [[ ! -f "$leasedir4/pid" ]]; then
    bad "background holder never acquired the lease (setup failure)"
else
    # SIGTERM (not -9): catchable, so bash runs the EXIT trap before the
    # process actually terminates -- this is what should release the lease
    # WITHOUT needing stale-pid reclaim.
    kill -TERM "$holder4" 2>/dev/null
    for _ in $(seq 1 50); do
        [[ ! -d "$leasedir4" ]] && break
        sleep 0.1
    done
    if [[ ! -d "$leasedir4" ]]; then
        ok "lease released by the holder's own EXIT trap after SIGTERM"
    else
        bad "lease dir still present after SIGTERM'd holder should have released it"
    fi
    # A fresh acquire must now succeed cleanly, with NO stale-reclaim
    # WARNING (the lease was released properly, not left stale).
    out4="$(bash -c "source '$repo4/scripts/lib/build-lease.sh'; build_lease_acquire trapped" 2>&1)"
    rc4=$?
    if [[ $rc4 -eq 0 ]]; then ok "fresh acquire after clean release succeeds"; else bad "fresh acquire failed (rc $rc4): $out4"; fi
    if [[ "$out4" != *"WARNING"* ]]; then ok "fresh acquire is clean (no stale-reclaim WARNING)"; else bad "unexpected WARNING on a cleanly-released lease: $out4"; fi
fi
wait "$holder4" 2>/dev/null

# ── Test 5: mid-hold eviction — the lease must survive `mvn clean` ───────
# Review finding (nexus-c00dw): the lease used to live INSIDE service/target,
# which `mvn clean` deletes wholesale. Simulate that: a holder acquires the
# lease (now at service/.build-lease/, a sibling of target/), then
# service/target/ itself gets `rm -rf`'d out from under it (standing in for
# a `./mvnw clean ...` mid-build) — the lease directory must be UNAFFECTED,
# and a concurrent second acquire must still see it as held and refuse.
echo "Test 5: lease survives service/target/ being wiped (mvn clean simulation)"
repo5="$WORKDIR/repo5"
_fake_repo "$repo5"
leasedir5="$repo5/service/.build-lease/svc"
bash -c "source '$repo5/scripts/lib/build-lease.sh'; NX_AGENT=clean-holder build_lease_acquire svc || exit 9; sleep 10" &
holder5=$!
for _ in $(seq 1 50); do
    [[ -f "$leasedir5/pid" ]] && break
    sleep 0.1
done
if [[ ! -f "$leasedir5/pid" ]]; then
    bad "background holder never acquired the lease (setup failure)"
else
    rm -rf "$repo5/service/target"
    if [[ -d "$leasedir5" ]]; then
        ok "lease directory survives service/target/ being wiped"
    else
        bad "lease directory was destroyed by wiping service/target/ — it is not actually outside the Maven-owned tree"
    fi
    out5="$(bash -c "source '$repo5/scripts/lib/build-lease.sh'; build_lease_acquire svc" 2>&1)"
    rc5=$?
    if [[ $rc5 -eq 75 ]]; then
        ok "a concurrent acquire still refuses (rc 75) after the target/ wipe"
    else
        bad "a concurrent acquire returned rc $rc5 (expected 75) after the target/ wipe — the lease was silently defeated: $out5"
    fi
fi
kill -9 "$holder5" 2>/dev/null
wait "$holder5" 2>/dev/null

echo
echo "build-lease_test.sh: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]
