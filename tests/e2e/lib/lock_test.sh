#!/usr/bin/env bash
# tests/e2e/lib/lock_test.sh — unit-level shell tests for lock.sh (RDR-184
# P0.1, nexus-ccs9v.1). Self-provisioning: builds its own throwaway tmpdir,
# no ambient state, no dependency on any other harness. Run directly with
# bash: `bash tests/e2e/lib/lock_test.sh`.
set -u -o pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./lock.sh disable=SC1091
source "$HERE/lock.sh"

WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/lock_test.XXXXXX")"
trap 'rm -rf "$WORKDIR"' EXIT

PASS=0
FAIL=0

ok() {
    echo "  [ok] $1"
    PASS=$((PASS + 1))
}
bad() {
    echo "  [FAIL] $1"
    FAIL=$((FAIL + 1))
}

# ── Test 1: acquire/release roundtrip ────────────────────────────────────
echo "Test 1: acquire/release roundtrip"
LOCKDIR="$WORKDIR/roundtrip.lock"
if lock_acquire "$LOCKDIR" >/tmp/lock_test_out1 2>&1; then
    ok "lock_acquire succeeded"
else
    bad "lock_acquire failed: $(cat /tmp/lock_test_out1)"
fi
if [[ -d "$LOCKDIR" ]]; then ok "lockdir exists after acquire"; else bad "lockdir missing after acquire"; fi
if [[ -f "$LOCKDIR/pid" && "$(cat "$LOCKDIR/pid")" == "$$" ]]; then
    ok "pid file records this process"
else
    bad "pid file wrong/missing"
fi

if lock_release "$LOCKDIR" 2>/tmp/lock_test_out1; then
    ok "lock_release succeeded"
else
    bad "lock_release failed: $(cat /tmp/lock_test_out1)"
fi
if [[ ! -d "$LOCKDIR" ]]; then ok "lockdir gone after release"; else bad "lockdir still present after release"; fi

# ── Test 2: second concurrent acquire fails loud, <1s, does not queue ────
echo "Test 2: second concurrent acquire fails loud <1s"
LOCKDIR="$WORKDIR/concurrent.lock"
bash -c "source '$HERE/lock.sh'; lock_acquire '$LOCKDIR' 0 || exit 9; sleep 10" &
holder_pid=$!

# Wait for the holder to actually have the lock (bounded poll, not a sleep
# guess) before racing the second acquire against it.
for _ in $(seq 1 50); do
    [[ -f "$LOCKDIR/pid" ]] && break
    sleep 0.1
done
if [[ ! -f "$LOCKDIR/pid" ]]; then
    bad "background holder never acquired the lock (setup failure)"
else
    t0=$(date +%s%N)
    if lock_acquire "$LOCKDIR" 0 2>/tmp/lock_test_out2; then
        bad "second concurrent acquire unexpectedly SUCCEEDED (should have failed)"
        lock_release "$LOCKDIR" 2>/dev/null || true
    else
        ok "second concurrent acquire failed as expected"
        if grep -q "FAILED to acquire" /tmp/lock_test_out2; then
            ok "failure message is loud (stderr)"
        else
            bad "no loud failure message"
        fi
    fi
    t1=$(date +%s%N)
    elapsed_ms=$(((t1 - t0) / 1000000))
    if ((elapsed_ms < 1000)); then
        ok "second acquire resolved in ${elapsed_ms}ms (<1000ms — fails fast, does not queue)"
    else
        bad "second acquire took ${elapsed_ms}ms (>=1000ms — looks like it queued)"
    fi
fi

kill "$holder_pid" 2>/dev/null || true
wait "$holder_pid" 2>/dev/null || true
rm -rf "$LOCKDIR"

# ── Test 3: stale lock (dead pid) is reclaimed ───────────────────────────
echo "Test 3: stale lock (dead pid) reclaimed"
LOCKDIR="$WORKDIR/stale.lock"
mkdir "$LOCKDIR"
# Spawn and immediately reap a process so its pid is guaranteed dead, then
# plant it as the recorded "holder" — simulating a crashed acquirer that
# never got to clean up its own lockdir.
bash -c 'exit 0' &
dead_pid=$!
wait "$dead_pid" 2>/dev/null
printf '%s\n' "$dead_pid" >"$LOCKDIR/pid"
echo "unknown" >"$LOCKDIR/start_token"

if lock_acquire "$LOCKDIR" 0 2>/tmp/lock_test_out3; then
    ok "stale lock (dead pid $dead_pid) was reclaimed"
    if [[ "$(cat "$LOCKDIR/pid" 2>/dev/null)" == "$$" ]]; then
        ok "reclaimed lockdir now records this process"
    else
        bad "reclaimed lockdir has wrong pid"
    fi
    lock_release "$LOCKDIR"
else
    bad "stale lock was NOT reclaimed: $(cat /tmp/lock_test_out3)"
    rm -rf "$LOCKDIR"
fi

# ── Test 4: non-holder release is refused ────────────────────────────────
echo "Test 4: non-holder release refused"
LOCKDIR="$WORKDIR/foreign.lock"
bash -c "source '$HERE/lock.sh'; lock_acquire '$LOCKDIR' 0 || exit 9; sleep 10" &
holder_pid=$!
for _ in $(seq 1 50); do
    [[ -f "$LOCKDIR/pid" ]] && break
    sleep 0.1
done

if [[ ! -f "$LOCKDIR/pid" ]]; then
    bad "background holder never acquired the lock (setup failure)"
else
    if lock_release "$LOCKDIR" 2>/tmp/lock_test_out4; then
        bad "lock_release unexpectedly SUCCEEDED for a lock this process does not hold"
    else
        ok "lock_release correctly refused (not the holder)"
        if grep -q "refusing to release" /tmp/lock_test_out4; then
            ok "refusal message is loud (stderr)"
        else
            bad "no loud refusal message"
        fi
    fi
    if [[ -d "$LOCKDIR" ]]; then
        ok "lockdir untouched after refused release"
    else
        bad "lockdir was removed despite refused release"
    fi
fi

kill "$holder_pid" 2>/dev/null || true
wait "$holder_pid" 2>/dev/null || true
rm -rf "$LOCKDIR"

rm -f /tmp/lock_test_out1 /tmp/lock_test_out2 /tmp/lock_test_out3 /tmp/lock_test_out4

echo
echo "lock_test.sh: ${PASS} passed, ${FAIL} failed"
[[ "$FAIL" -eq 0 ]]
