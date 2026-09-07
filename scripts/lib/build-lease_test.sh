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

# ── Test 6: worktrees of one repo share the lease (nexus-g6xpa) ──────────
# Before: each worktree had its own service/.build-lease, so three worktree
# agents ran three engine suites at once. Now the lease lives in the git
# common dir; a holder in the primary blocks an acquire from a worktree.
echo "Test 6: a worktree and its primary share one lease"
repo6="$WORKDIR/repo6"
_fake_repo "$repo6"
git -C "$repo6" init -q && git -C "$repo6" config user.email t@t && git -C "$repo6" config user.name t
git -C "$repo6" add scripts && git -C "$repo6" commit -qm base
git -C "$repo6" worktree add -q "$WORKDIR/repo6-wt" -b wt
common6="$(cd "$repo6/.git" && pwd -P)"
root6="$(bash -c "source '$repo6/scripts/lib/build-lease.sh'; _build_lease_root")"
if [[ "$(cd "$(dirname "$root6")" && pwd -P)/$(basename "$root6")" == "$common6/nexus-build-lease" ]]; then ok "lease root is <git common dir>/nexus-build-lease"; else bad "lease root is $root6"; fi
root6wt="$(bash -c "source '$WORKDIR/repo6-wt/scripts/lib/build-lease.sh'; _build_lease_root")"
if [[ "$root6wt" == "$root6" ]]; then ok "worktree resolves the same lease root as the primary"; else bad "worktree root $root6wt != primary root $root6"; fi
bash -c "source '$repo6/scripts/lib/build-lease.sh'; NX_AGENT=primary-holder build_lease_acquire shared || exit 9; sleep 10" &
holder6=$!
for _ in $(seq 1 50); do
    [[ -f "$root6/shared/pid" ]] && break
    sleep 0.1
done
if [[ ! -f "$root6/shared/pid" ]]; then
    bad "primary holder never acquired (setup failure)"
else
    out6="$(bash -c "source '$WORKDIR/repo6-wt/scripts/lib/build-lease.sh'; build_lease_acquire shared" 2>&1)"
    rc6=$?
    if [[ $rc6 -eq 75 ]]; then ok "acquire from the worktree refuses (rc 75) while the primary holds"; else bad "worktree acquire rc $rc6 (expected 75): $out6"; fi
    if [[ "$out6" == *"primary-holder"* ]]; then ok "refusal names the primary's holder label"; else bad "refusal missing holder label: $out6"; fi
fi
kill -9 "$holder6" 2>/dev/null
wait "$holder6" 2>/dev/null
outo="$(bash -c "export NX_BUILD_LEASE_ROOT='$WORKDIR/override'; source '$repo6/scripts/lib/build-lease.sh'; _build_lease_root")"
if [[ "$outo" == "$WORKDIR/override" ]]; then ok "NX_BUILD_LEASE_ROOT overrides the resolution"; else bad "override ignored: $outo"; fi

# ── Test 7: build_lease_acquire_wait waits for a live holder ─────────────
echo "Test 7: acquire_wait waits out a live holder, then acquires; 0 is a single attempt"
repo7="$WORKDIR/repo7"
_fake_repo "$repo7"
bash -c "source '$repo7/scripts/lib/build-lease.sh'; NX_AGENT=short-holder build_lease_acquire w || exit 9; trap 'build_lease_release w' EXIT; sleep 7" &
holder7=$!
for _ in $(seq 1 50); do
    [[ -f "$repo7/service/.build-lease/w/pid" ]] && break
    sleep 0.1
done
out7="$(bash -c "source '$repo7/scripts/lib/build-lease.sh'; build_lease_acquire_wait w 0" 2>&1)"
rc7=$?
if [[ $rc7 -eq 75 ]]; then ok "max-seconds 0 refuses immediately (rc 75)"; else bad "wait 0 returned rc $rc7: $out7"; fi
start7=$SECONDS
out7b="$(bash -c "source '$repo7/scripts/lib/build-lease.sh'; build_lease_acquire_wait w 60 && echo HELD" 2>&1)"
rc7b=$?
took7=$((SECONDS - start7))
if [[ $rc7b -eq 0 && "$out7b" == *HELD* ]]; then ok "acquire_wait acquired after the holder released (${took7}s)"; else bad "acquire_wait rc $rc7b: $out7b"; fi
if [[ "$out7b" == *"waiting"* ]]; then ok "the wait was announced on stderr"; else bad "no waiting line: $out7b"; fi
if (( took7 >= 3 )); then ok "it actually waited (${took7}s), not a stale reclaim"; else bad "returned too fast (${took7}s) — reclaimed a live holder?"; fi
wait "$holder7" 2>/dev/null
bash -c "source '$repo7/scripts/lib/build-lease.sh'; NX_AGENT=long-holder build_lease_acquire w2 || exit 9; sleep 30" &
holder7b=$!
for _ in $(seq 1 50); do
    [[ -f "$repo7/service/.build-lease/w2/pid" ]] && break
    sleep 0.1
done
out7c="$(bash -c "source '$repo7/scripts/lib/build-lease.sh'; build_lease_acquire_wait w2 5" 2>&1)"
rc7c=$?
if [[ $rc7c -eq 75 && "$out7c" == *"gave up"* ]]; then ok "bounded wait gives up with rc 75 naming the bound"; else bad "bounded wait rc $rc7c: $out7c"; fi
kill -9 "$holder7b" 2>/dev/null
wait "$holder7b" 2>/dev/null

# ── Test 8: a pid-less lease dir is HELD while young, stale once old ─────
echo "Test 8: mid-populate window — a fresh pid-less lease is held, an old one is stale"
repo8="$WORKDIR/repo8"
_fake_repo "$repo8"
leasedir8="$repo8/service/.build-lease/pop"
mkdir -p "$leasedir8"     # mkdir done, populate not yet — the racing acquirer's view
out8="$(bash -c "source '$repo8/scripts/lib/build-lease.sh'; build_lease_acquire pop" 2>&1)"
rc8=$?
if [[ $rc8 -eq 75 ]]; then ok "a seconds-old pid-less lease dir is treated as held (rc 75)"; else bad "young pid-less dir rc $rc8 (expected 75): $out8"; fi
if [[ "$out8" == *"populated"* ]]; then ok "the refusal says it is being populated"; else bad "refusal text: $out8"; fi
touch -t 202001010000 "$leasedir8"
out8b="$(bash -c "source '$repo8/scripts/lib/build-lease.sh'; build_lease_acquire pop" 2>&1)"
rc8b=$?
if [[ $rc8b -eq 0 && "$out8b" == *WARNING* ]]; then ok "an old pid-less lease dir is reclaimed with a WARNING"; else bad "old pid-less dir rc $rc8b: $out8b"; fi

echo
echo "build-lease_test.sh: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]
