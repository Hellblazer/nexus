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

# ── Test 5: a DELAYED reclaimer must not clobber a lock recreated in ────
# ── the interim (RDR-184 P0 review C1: reclaim TOCTOU) ───────────────────
# The reviewer's own C1 narrative is specifically: "B (scheduled back in,
# already decided 'stale' ... never re-validates)" -- i.e. B is descheduled
# by ordinary OS scheduling AFTER deciding a lockdir is stale but BEFORE
# acting on that decision, and when B resumes it acts on stale information
# without re-checking. A purely-simultaneous release of two contenders
# does NOT reproduce this (verified empirically while designing this
# test: mkdir's own atomicity already protects the truly-simultaneous
# case -- only one of two concurrent mkdir calls can ever win). The real
# bug requires asymmetric delay, so this test constructs it directly and
# deterministically:
#   1. Contender B is launched with `rm`/`mv` shadowed via a PATH-
#      prepended wrapper that pauses (bounded) at the exact moment B
#      calls whichever primitive its reclaim path uses against the
#      ORIGINAL lockdir path -- modeling "B decided stale, got scheduled
#      out before acting".
#   2. Once B is confirmed paused there, contender A runs an entirely
#      normal, unshadowed acquire against the SAME originally-stale
#      lockdir and succeeds (a completely ordinary reclaim).
#   3. Only THEN is B released to actually execute its (now stale-in-
#      truth) reclaim action against whatever currently occupies that
#      path -- A's brand new, live lock.
# Pre-fix (rm -rf with no identity check), B's delayed rm silently
# destroys A's live lock and B goes on to also acquire -- both winners.
# Post-fix (atomic mv, post-verified against the pid this contender
# itself decided was stale), B's mv still succeeds at capturing A's live
# lock (rename doesn't know or care what it's renaming), but the
# post-capture identity check catches the mismatch, restores A's lock,
# and refuses to let B claim -- exactly one winner.
echo "Test 5: a delayed reclaimer must not clobber a lock recreated in the interim"
LOCKDIR="$WORKDIR/race.lock"
mkdir "$LOCKDIR"
bash -c 'exit 0' &
dead_pid4=$!
wait "$dead_pid4" 2>/dev/null
printf '%s\n' "$dead_pid4" >"$LOCKDIR/pid"
echo "unknown" >"$LOCKDIR/start_token"

RACE_BIN="$WORKDIR/race_bin"
mkdir -p "$RACE_BIN"
REACHED="$WORKDIR/race_reached"
GO="$WORKDIR/race_go"
rm -f "$REACHED" "$GO"

# Shadow BOTH rm and mv (pre-fix reclaims via `rm -rf`; the fix reclaims
# via `mv`) so this test exercises whichever primitive is actually in
# play without needing to know which. Only pauses when one of the
# wrapper's own arguments is literally $LOCKDIR (this contender's
# reclaim action against the ORIGINAL lockdir path) -- every other rm/mv
# call in this test (cleanup, the restore-on-mismatch's OWN internal
# bookkeeping once $GO already exists, etc.) passes straight through.
# Bounded (10s) wait, never hangs forever even if the test driver logic
# below has a bug.
for cmd in rm mv; do
    real="$(command -v "$cmd")"
    cat >"$RACE_BIN/$cmd" <<WRAPPER_EOF
#!/usr/bin/env bash
for a in "\$@"; do
    if [[ "\$a" == "$LOCKDIR" ]]; then
        : >"$REACHED"
        for _ in \$(seq 1 500); do
            [[ -f "$GO" ]] && break
            sleep 0.02
        done
        break
    fi
done
exec "$real" "\$@"
WRAPPER_EOF
    chmod +x "$RACE_BIN/$cmd"
done

# Contender B: shadowed PATH so its reclaim action against $LOCKDIR
# pauses at the marker above until released via $GO. B's own acquire
# timeout (1s) is deliberately SHORTER than A's hold time (3s, below) so
# the two outcomes are unambiguous: if B is correctly blocked (the fix
# works), B's lock_acquire must time out and return nonzero BEFORE A
# ever releases -- it cannot legitimately succeed within that window. A
# b_rc of 0 can therefore only mean B actually stole A's live lock, not
# a benign "B waited, then acquired after A released" sequence (which
# Test 5's first iteration incorrectly conflated with the bug -- see
# git history / review notes: A holding only 1s let B legitimately
# re-acquire the FREE lock after A's own release, which is correct
# behavior, not the C1 defect).
bash -c "export PATH='$RACE_BIN:$PATH'; source '$HERE/lock.sh'; lock_acquire '$LOCKDIR' 1 >'$WORKDIR/race_b_out' 2>&1; echo \$? >'$WORKDIR/race_b_rc'" &
bpid=$!

reached=0
for _ in $(seq 1 100); do
    if [[ -f "$REACHED" ]]; then
        reached=1
        break
    fi
    sleep 0.05
done

if [[ "$reached" -ne 1 ]]; then
    bad "test setup: contender B never reached its reclaim action (setup failure, not a lock.sh verdict)"
    kill "$bpid" 2>/dev/null || true
    wait "$bpid" 2>/dev/null || true
else
    # Contender A: entirely normal PATH/timing, races the SAME
    # originally-observed stale lockdir while B sits paused mid-reclaim.
    # Holds for 3s -- longer than B's 1s acquire timeout above -- so B
    # can only succeed by actually stealing the live lock, never by
    # legitimately outlasting it.
    rm -f "$WORKDIR/race_a_won"
    (
        # shellcheck source=./lock.sh disable=SC1091
        source "$HERE/lock.sh"
        if lock_acquire "$LOCKDIR" 5; then
            : >"$WORKDIR/race_a_won"
            sleep 3
            lock_release "$LOCKDIR" 2>/dev/null || true
        fi
    ) &
    apid=$!

    # Wait for A to fully complete its acquire (lock_acquire returns only
    # after pid+token are written) before releasing the delayed B.
    a_settled=0
    for _ in $(seq 1 100); do
        if [[ -f "$WORKDIR/race_a_won" ]]; then
            a_settled=1
            break
        fi
        sleep 0.05
    done

    if [[ "$a_settled" -ne 1 ]]; then
        bad "test setup: contender A never completed its acquire while B was paused (setup failure, not a lock.sh verdict)"
    fi

    # Release the delayed B now that A's fresh lock is in place.
    : >"$GO"
    wait "$bpid" 2>/dev/null
    wait "$apid" 2>/dev/null

    b_rc="$(cat "$WORKDIR/race_b_rc" 2>/dev/null || echo '?')"
    a_won=0
    [[ -f "$WORKDIR/race_a_won" ]] && a_won=1
    b_won=0
    [[ "$b_rc" == "0" ]] && b_won=1

    if [[ "$a_won" -eq 1 && "$b_won" -eq 1 ]]; then
        bad "BOTH A and B believe they hold the lock (C1: delayed reclaimer B clobbered A's freshly-(re)claimed lock)"
    elif [[ "$a_won" -eq 1 && "$b_won" -eq 0 ]]; then
        ok "only A holds the lock; B's delayed reclaim correctly failed (C1 mutual exclusion holds under a genuinely delayed reclaimer)"
    else
        bad "unexpected outcome: a_won=$a_won b_won=$b_won rc(B)=$b_rc (neither/wrong contender acquired -- see $WORKDIR/race_b_out)"
    fi
fi

lock_release "$LOCKDIR" 2>/dev/null || rm -rf "$LOCKDIR"
rm -rf "$RACE_BIN"
rm -f "$REACHED" "$GO" "$WORKDIR/race_a_won" "$WORKDIR/race_b_out" "$WORKDIR/race_b_rc"

# ── Test 5b: acquire-path readback — a fresh claim whose atomic mv ───────
# ── NESTS into a concurrently-landed live claim must be detected, ────────
# ── never trusted, and its own artifact cleaned up (RDR-184 P0 ───────────
# ── re-round CRITICAL 1's guard code: lock.sh fresh-acquire readback) ────
# The fresh-acquire path is populate-private-dir -> `[[ ! -e $lockdir ]]`
# vacancy check -> atomic mv -> post-claim readback. The check-then-mv
# pair has a single-statement TOCTOU gap: if another contender's claim
# lands inside it, the mv NESTS the private dir inside the other claim
# (mv onto an existing directory nests rather than fails, and still
# exits 0). The post-claim readback (pid != $$) is the ONLY thing
# standing between that outcome and a false `return 0` on top of
# somebody else's live lock.
#
# The previous revision of this test (two contenders, delayed reclaimer)
# was mutation-tested VACUOUS 3/3 (r3 critique, T2
# critique-nexus-ccs9v.5-phase0-fix-r3): the reclaimer always
# round-tripped its capture back intact before the claimant ever looked,
# so the readback's mismatch branch never fired regardless of the code
# under test. This revision constructs the nesting outcome directly,
# Test-9-style third-actor choreography applied to the acquire branch:
#
#   1. Contender A (mv PATH-shadowed, same technique as Test 5/9) starts
#      a fresh acquire on a VACANT lockdir and is paused INSIDE its
#      claiming mv — after its vacancy check passed, before rename(2)
#      fires. This is literally the check-to-mv gap, held open.
#   2. Contender E (entirely normal PATH) legitimately claims the still-
#      vacant lockdir and holds it as a live lock.
#   3. A is released: its mv now fires against E's live directory and
#      NESTS. A's widen-seam (NX_LOCK_TEST_WIDEN_WINDOW, between the mv
#      succeeding and the readback) holds the nested state observable
#      for 2s so the test can assert the nesting GENUINELY happened
#      (setup non-vacuity) before A's readback acts on it.
#   4. Asserted, in order: the nested artifact IS present inside E's
#      live lockdir mid-window; E's pid file is undisturbed; after A's
#      readback runs, the artifact is GONE (the cleanup branch fired)
#      while E still holds; A never returned 0 while E held the lock
#      (no double-holder); A converges to a real acquire after E
#      releases, and releases clean.
#
# Mutation-verified non-vacuous (each mutant run red, restore green):
#   - readback stripped (blind `return 0` after mv exit 0): overlap +
#     persistent nested artifact + failed release -> red.
#   - cleanup rm stripped (readback kept, artifact removal removed):
#     nested artifact persists inside E's live lock -> red.
#   - acquire path reverted to pre-fix `mkdir $lockdir` + two separate
#     writes: A never reaches a claiming mv -> setup assert -> red.
echo "Test 5b: acquire-path readback detects+cleans a fresh claim nested into a live third-party claim"
LOCKDIR="$WORKDIR/acqnest.lock"
rm -rf "$LOCKDIR"

ACQ_BIN="$WORKDIR/acqnest_bin"
mkdir -p "$ACQ_BIN"
ACQ_REACHED="$WORKDIR/acqnest_reached"
ACQ_GO="$WORKDIR/acqnest_go"
rm -f "$ACQ_REACHED" "$ACQ_GO"

# Contender A's mv shadow — pauses only when an argument is literally
# $LOCKDIR (the claiming mv's destination), releases on $ACQ_GO. ONLY mv
# is shadowed: A's readback, cleanup, and retry paths must run entirely
# unmodified, since they are exactly the code under test.
real_mv="$(command -v mv)"
cat >"$ACQ_BIN/mv" <<WRAPPER_EOF
#!/usr/bin/env bash
for a in "\$@"; do
    if [[ "\$a" == "$LOCKDIR" ]]; then
        : >"$ACQ_REACHED"
        for _ in \$(seq 1 500); do
            [[ -f "$ACQ_GO" ]] && break
            sleep 0.02
        done
        break
    fi
done
exec "$real_mv" "\$@"
WRAPPER_EOF
chmod +x "$ACQ_BIN/mv"

# Contender A: paused mid-claim, widened readback window, generous
# timeout (it must survive E's whole 5s hold and still converge to a
# genuine acquire afterwards).
cat >"$WORKDIR/acqnest_a_driver.sh" <<DRIVER_EOF
#!/usr/bin/env bash
source "$HERE/lock.sh"
if lock_acquire "$LOCKDIR" 20; then
    echo 0 >"$WORKDIR/acqnest_a_rc"
    if [[ -f "$WORKDIR/acqnest_e_holding" ]]; then
        : >"$WORKDIR/acqnest_overlap"
    fi
    if lock_release "$LOCKDIR" 2>"$WORKDIR/acqnest_release_out"; then
        echo 0 >"$WORKDIR/acqnest_release_rc"
    else
        echo 1 >"$WORKDIR/acqnest_release_rc"
    fi
else
    echo 1 >"$WORKDIR/acqnest_a_rc"
fi
DRIVER_EOF
chmod +x "$WORKDIR/acqnest_a_driver.sh"
rm -f "$WORKDIR/acqnest_a_rc" "$WORKDIR/acqnest_release_rc" "$WORKDIR/acqnest_overlap" \
      "$WORKDIR/acqnest_e_pid" "$WORKDIR/acqnest_e_holding" "$WORKDIR/acqnest_e_won" \
      "$WORKDIR/acqnest_e_rc"
PATH="$ACQ_BIN:$PATH" NX_LOCK_TEST_WIDEN_WINDOW=1 NX_LOCK_TEST_WIDEN_SECONDS=2 \
    bash "$WORKDIR/acqnest_a_driver.sh" &
apid=$!

reached=0
for _ in $(seq 1 100); do
    if [[ -f "$ACQ_REACHED" ]]; then
        reached=1
        break
    fi
    sleep 0.05
done

if [[ "$reached" -ne 1 ]]; then
    bad "test setup: contender A never reached its claiming mv (setup failure, not a lock.sh verdict — but ALSO what a pre-fix mkdir-style acquire path looks like)"
    kill "$apid" 2>/dev/null || true
    wait "$apid" 2>/dev/null || true
else
    # Contender E: entirely normal PATH — legitimately claims the still-
    # vacant lockdir while A sits paused inside its own claiming mv,
    # i.e. E's claim lands squarely in A's check-to-mv gap.
    cat >"$WORKDIR/acqnest_e_driver.sh" <<DRIVER_EOF
#!/usr/bin/env bash
source "$HERE/lock.sh"
if lock_acquire "$LOCKDIR" 5; then
    printf '%s\n' "\$\$" >"$WORKDIR/acqnest_e_pid"
    : >"$WORKDIR/acqnest_e_holding"
    : >"$WORKDIR/acqnest_e_won"
    sleep 5
    rm -f "$WORKDIR/acqnest_e_holding"
    if lock_release "$LOCKDIR" 2>/dev/null; then
        echo 0 >"$WORKDIR/acqnest_e_rc"
    else
        echo 1 >"$WORKDIR/acqnest_e_rc"
    fi
else
    echo 1 >"$WORKDIR/acqnest_e_rc"
fi
DRIVER_EOF
    chmod +x "$WORKDIR/acqnest_e_driver.sh"
    bash "$WORKDIR/acqnest_e_driver.sh" &
    epid=$!

    e_settled=0
    for _ in $(seq 1 100); do
        if [[ -f "$WORKDIR/acqnest_e_won" ]]; then
            e_settled=1
            break
        fi
        sleep 0.05
    done

    if [[ "$e_settled" -ne 1 ]]; then
        bad "test setup: contender E never acquired the vacant lockdir while A was paused (setup failure, not a lock.sh verdict)"
        kill "$epid" 2>/dev/null || true
        : >"$ACQ_GO" # unstick A so its wait below terminates
        wait "$apid" 2>/dev/null
        wait "$epid" 2>/dev/null
    else
        # t0: release A. Its real mv fires now, against E's live claim.
        : >"$ACQ_GO"
        sleep 0.8

        # Mid-window: A's mv has landed (~ms after GO) and A is asleep
        # in its 2s widen-seam — its readback has NOT yet run. This is
        # the setup-non-vacuity assert the previous revision lacked: the
        # nesting outcome the readback exists to catch must be OBSERVED
        # to have actually happened, or the rest of the test proves
        # nothing about the readback.
        e_pid="$(cat "$WORKDIR/acqnest_e_pid" 2>/dev/null || true)"
        nested_mid="$(find "$LOCKDIR" -mindepth 1 -maxdepth 1 -type d -name "*.fresh.*" 2>/dev/null | wc -l | tr -d ' ')"
        if [[ "$nested_mid" -ge 1 ]]; then
            ok "setup non-vacuity: A's claiming mv genuinely NESTED into E's live lockdir ($nested_mid artifact(s) observed mid-window)"
        else
            bad "test setup: A's mv did not nest into E's live claim (choreography rot — the readback branch was never exercised, test proves nothing)"
        fi
        if [[ -n "$e_pid" && "$(cat "$LOCKDIR/pid" 2>/dev/null)" == "$e_pid" ]]; then
            ok "E's pid file undisturbed by the nesting mv (nesting never clobbers the live holder's content)"
        else
            bad "E's pid file disturbed mid-window (expected pid=$e_pid, got '$(cat "$LOCKDIR/pid" 2>/dev/null)')"
        fi

        # t0+3.2s: A's readback+cleanup ran at ~t0+2s; E holds to ~t0+5s.
        sleep 2.4
        nested_after="$(find "$LOCKDIR" -mindepth 1 -maxdepth 1 -type d -name "*.fresh.*" 2>/dev/null | wc -l | tr -d ' ')"
        if [[ "$nested_after" -eq 0 ]]; then
            ok "A's readback detected the nesting and cleaned up its own artifact while E still held (mismatch/cleanup branch fired)"
        else
            bad "nested artifact STILL inside E's live lockdir after A's readback window ($nested_after found — cleanup branch did not fire)"
        fi
        if [[ -n "$e_pid" && "$(cat "$LOCKDIR/pid" 2>/dev/null)" == "$e_pid" ]]; then
            ok "E's pid file still intact after A's cleanup (cleanup touched ONLY A's own artifact)"
        else
            bad "E's pid file damaged after A's cleanup (expected pid=$e_pid, got '$(cat "$LOCKDIR/pid" 2>/dev/null)')"
        fi

        wait "$apid" 2>/dev/null
        wait "$epid" 2>/dev/null

        if [[ -f "$WORKDIR/acqnest_overlap" ]]; then
            bad "DOUBLE-HOLDER: A's lock_acquire returned 0 while E still held the lock (readback failed to reject the nesting outcome)"
        else
            ok "A never claimed success while E held the lock (readback rejected the nesting outcome)"
        fi
        a_rc="$(cat "$WORKDIR/acqnest_a_rc" 2>/dev/null || echo '?')"
        if [[ "$a_rc" == "0" ]]; then
            ok "A converged to a genuine acquire after E released (retry loop recovered)"
        else
            bad "A never converged after E released (rc=$a_rc)"
        fi
        release_rc="$(cat "$WORKDIR/acqnest_release_rc" 2>/dev/null || echo '?')"
        if [[ "$release_rc" == "0" ]]; then
            ok "A's lock_release succeeded afterward (the converged lock was genuinely A's own)"
        else
            bad "A's lock_release FAILED afterward (rc=$release_rc): $(cat "$WORKDIR/acqnest_release_out" 2>/dev/null)"
        fi
        e_rc="$(cat "$WORKDIR/acqnest_e_rc" 2>/dev/null || echo '?')"
        if [[ "$e_rc" == "0" ]]; then
            ok "E's own hold/release cycle completed clean throughout"
        else
            bad "E's hold/release cycle broke (rc=$e_rc)"
        fi
        if [[ ! -e "$LOCKDIR" ]]; then
            ok "lockdir fully vacated at the end (no residue from either contender)"
        else
            bad "lockdir still present at the end: $(ls -la "$LOCKDIR" 2>/dev/null)"
        fi
    fi
fi

lock_release "$LOCKDIR" 2>/dev/null || rm -rf "$LOCKDIR"
rm -rf "$ACQ_BIN"
rm -f "$ACQ_REACHED" "$ACQ_GO" "$WORKDIR/acqnest_a_driver.sh" "$WORKDIR/acqnest_e_driver.sh" \
      "$WORKDIR/acqnest_a_rc" "$WORKDIR/acqnest_release_rc" "$WORKDIR/acqnest_release_out" \
      "$WORKDIR/acqnest_overlap" "$WORKDIR/acqnest_e_pid" "$WORKDIR/acqnest_e_holding" \
      "$WORKDIR/acqnest_e_won" "$WORKDIR/acqnest_e_rc"

# ── Test 9: a THIRD contender's fresh claim must never be silently ──────
# ── nested-into during a reclaimer's restore (RDR-184 P0 re-round ───────
# ── CRITICAL 2) ───────────────────────────────────────────────────────────
# `mv <dir> <target>` where <target> already exists as a directory NESTS
# the source inside it (verified empirically; a POSIX/`mv` UX convention,
# not a rename(2) restriction) rather than failing or replacing. If a
# mismatched-capture restore's `mv "$claim" "$lockdir"` fires exactly
# when a fresh, unrelated contender E has already re-populated $lockdir
# in the vacancy the capture left, the restore would silently bury the
# captured (mismatched) data inside E's live directory -- E's own lock
# stays intact (no double-acquisition), but the buried data is permanent,
# invisible clutter, a silent violation of the restore's own
# "restore it rather than destroy it" contract.
#
# Construction (needs a GENUINELY delayed reclaimer B, per the same
# reasoning as Test 5 -- a fresh, unpaused lock_acquire always
# re-validates staleness against CURRENT state, so it can never be
# tricked into treating a live lock as a stale one it already decided on
# earlier):
#   1. B (PATH-shadowed rm/mv, like Test 5) decides dead_pid6's lock is
#      stale and is paused right at its own capture-mv.
#   2. WHILE B is paused, C legitimately reclaims dead_pid6 (unshadowed,
#      ordinary) and holds a fresh, live lock.
#   3. B is released: its capture-mv now fires against whatever is
#      CURRENTLY at $lockdir -- C's live lock, not dead_pid6 -- a genuine
#      mismatch. B enters its restore branch, where
#      NX_LOCK_TEST_WIDEN_WINDOW widens the vacancy-check-to-restore-mv
#      gap.
#   4. E races a fresh mkdir into that widened vacancy.
#   5. B's restore mv then nests into E's now-populated directory --
#      asserted to be DETECTED (post-restore identity mismatch) and
#      CLEANED UP (only B's own nested artifact removed, E's content
#      untouched).
echo "Test 9: a third contender's fresh claim is never nested-into during a reclaimer's restore"
LOCKDIR="$WORKDIR/nest.lock"
mkdir "$LOCKDIR"
bash -c 'exit 0' &
dead_pid6=$!
wait "$dead_pid6" 2>/dev/null
printf '%s\n' "$dead_pid6" >"$LOCKDIR/pid"
echo "unknown" >"$LOCKDIR/start_token"

NEST_BIN="$WORKDIR/nest_bin"
mkdir -p "$NEST_BIN"
NEST_REACHED="$WORKDIR/nest_reached"
NEST_GO="$WORKDIR/nest_go"
rm -f "$NEST_REACHED" "$NEST_GO"

# B's capture-mv shadow -- identical technique to Test 5: pauses only
# when the target is literally $LOCKDIR, releases on $NEST_GO.
for cmd in rm mv; do
    real="$(command -v "$cmd")"
    cat >"$NEST_BIN/$cmd" <<WRAPPER_EOF
#!/usr/bin/env bash
for a in "\$@"; do
    if [[ "\$a" == "$LOCKDIR" ]]; then
        : >"$NEST_REACHED"
        for _ in \$(seq 1 500); do
            [[ -f "$NEST_GO" ]] && break
            sleep 0.02
        done
        break
    fi
done
exec "$real" "\$@"
WRAPPER_EOF
    chmod +x "$NEST_BIN/$cmd"
done

# Contender B: shadowed PATH (delays its capture) AND the widen-seam
# (delays its subsequent restore, once released) -- both active for the
# whole of B's process.
bash -c "export PATH='$NEST_BIN:$PATH'; export NX_LOCK_TEST_WIDEN_WINDOW=1 NX_LOCK_TEST_WIDEN_SECONDS=2; source '$HERE/lock.sh'; lock_acquire '$LOCKDIR' 3 >'$WORKDIR/nest_b_out' 2>&1; echo \$? >'$WORKDIR/nest_b_rc'" &
bpid=$!

reached=0
for _ in $(seq 1 100); do
    if [[ -f "$NEST_REACHED" ]]; then
        reached=1
        break
    fi
    sleep 0.05
done

if [[ "$reached" -ne 1 ]]; then
    bad "test setup: delayed reclaimer B never reached its capture action (setup failure, not a lock.sh verdict)"
    kill "$bpid" 2>/dev/null || true
    wait "$bpid" 2>/dev/null || true
else
    # Contender C: entirely normal (unshadowed) PATH. B is paused BEFORE
    # its own mv fires, so $LOCKDIR still shows dead_pid6 untouched -- C
    # independently decides it's stale too, reclaims it for real
    # (C's own mv/rm are real, never shadowed), and ends up holding a
    # fresh, live lock under C's own pid.
    cat >"$WORKDIR/nest_c_driver.sh" <<DRIVER_EOF
#!/usr/bin/env bash
source "$HERE/lock.sh"
if lock_acquire "$LOCKDIR" 5; then
    echo 0 >"$WORKDIR/nest_c_rc"
    cat "$LOCKDIR/pid" >"$WORKDIR/nest_c_pid_snapshot" 2>/dev/null
    sleep 3
    lock_release "$LOCKDIR" 2>/dev/null
else
    echo 1 >"$WORKDIR/nest_c_rc"
fi
DRIVER_EOF
    chmod +x "$WORKDIR/nest_c_driver.sh"
    rm -f "$WORKDIR/nest_c_rc" "$WORKDIR/nest_c_pid_snapshot"
    bash "$WORKDIR/nest_c_driver.sh" &
    cpid=$!

    c_settled=0
    for _ in $(seq 1 100); do
        if [[ -f "$WORKDIR/nest_c_rc" ]]; then
            c_settled=1
            break
        fi
        sleep 0.05
    done

    if [[ "$c_settled" -ne 1 ]]; then
        bad "test setup: contender C never completed its claim while B was paused (setup failure, not a lock.sh verdict)"
        kill "$bpid" "$cpid" 2>/dev/null || true
        wait "$bpid" "$cpid" 2>/dev/null || true
    else
        c_rc="$(cat "$WORKDIR/nest_c_rc" 2>/dev/null || echo '?')"
        if [[ "$c_rc" != "0" ]]; then
            bad "test setup: contender C's lock_acquire did not return 0 (rc=$c_rc) -- cannot evaluate the nesting invariant"
            kill "$bpid" 2>/dev/null || true
            wait "$bpid" 2>/dev/null || true
        else
            # Release B: its paused capture-mv now fires against C's
            # LIVE lock (a genuine mismatch against B's ORIGINAL
            # dead_pid6 decision), sending B into its widened restore
            # window.
            : >"$NEST_GO"

            # Give B enough time to exec its real capture-mv, detect the
            # mismatch, pass its vacancy check, and enter the 2s widened
            # sleep before its restore-mv -- all fast (a handful of
            # forks), well under the settle delay below.
            sleep 0.3

            # Contender E: a fresh, unrelated contender racing a real
            # mkdir into the vacancy B's capture leaves, DURING B's
            # widened check-to-restore gap.
            cat >"$WORKDIR/nest_e_driver.sh" <<DRIVER_EOF
#!/usr/bin/env bash
source "$HERE/lock.sh"
if lock_acquire "$LOCKDIR" 3; then
    echo 0 >"$WORKDIR/nest_e_rc"
    cat "$LOCKDIR/pid" >"$WORKDIR/nest_e_pid_snapshot" 2>/dev/null
else
    echo 1 >"$WORKDIR/nest_e_rc"
fi
DRIVER_EOF
            chmod +x "$WORKDIR/nest_e_driver.sh"
            rm -f "$WORKDIR/nest_e_rc" "$WORKDIR/nest_e_pid_snapshot"
            bash "$WORKDIR/nest_e_driver.sh" &
            epid=$!

            wait "$epid" 2>/dev/null
            wait "$bpid" 2>/dev/null
            wait "$cpid" 2>/dev/null

            e_rc="$(cat "$WORKDIR/nest_e_rc" 2>/dev/null || echo '?')"
            if [[ "$e_rc" != "0" ]]; then
                bad "test setup: contender E's lock_acquire did not return 0 (rc=$e_rc) -- cannot evaluate the nesting invariant"
            else
                ok "contender E successfully claimed the vacancy B's capture left (setup reached the intended window)"
            fi

            # THE INVARIANT: $lockdir must never contain a nested
            # reclaim artifact (no silent burial), regardless of the
            # exact interleaving, and must remain a normal, flat,
            # directly-readable lockdir (E's, in this scenario -- never
            # double-acquired with C).
            nested_count=$(find "$LOCKDIR" -mindepth 1 -maxdepth 1 -type d -name '*.reclaim.*' 2>/dev/null | wc -l | tr -d ' ')
            if [[ "$nested_count" -eq 0 ]]; then
                ok "no nested reclaim-artifact subdirectory left inside \$LOCKDIR after the widened restore race"
            else
                bad "FOUND $nested_count nested reclaim-artifact subdirector(y/ies) inside \$LOCKDIR -- restore-mv silently buried captured data (C2 nesting regression)"
            fi
            if [[ -f "$LOCKDIR/pid" ]]; then
                final_owner="$(cat "$LOCKDIR/pid" 2>/dev/null)"
                ok "\$LOCKDIR/pid is present and directly readable (pid=$final_owner, not itself nested away)"
            else
                bad "\$LOCKDIR/pid is missing at top level -- \$LOCKDIR may itself be nested inside something else"
            fi
        fi
    fi
fi

rm -rf "$LOCKDIR" "$NEST_BIN" 2>/dev/null
rm -f "$NEST_REACHED" "$NEST_GO" "$WORKDIR"/nest_*.sh "$WORKDIR"/nest_*_rc "$WORKDIR"/nest_*_out \
      "$WORKDIR"/nest_*_pid_snapshot

# ── Test 8: reclaim-action failure must not spin forever (RDR-184 P0 ────
# ── review C2: busy-loop ignores timeout on rm -rf failure) ──────────────
# Forces the reclaim's removal step (rm -rf pre-fix / mv post-fix -- both
# are ultimately a rename/unlink on the lockdir path) to fail
# deterministically via a portable immutable-flag trick, then asserts
# lock_acquire still returns within a bounded outer `timeout`, never
# spinning sleeplessly forever. `chflags uchg` (darwin) / a read-only
# parent dir (fallback, e.g. Linux CI without chflags) block unlink/
# rename on the flagged/protected path regardless of which primitive the
# implementation uses, so this test is agnostic to the pre-fix vs.
# post-fix mechanism -- it exercises the same failure class either way.
echo "Test 8: reclaim-action failure must not spin forever (C2)"
LOCKDIR="$WORKDIR/rmfail.lock"
mkdir "$LOCKDIR"
bash -c 'exit 0' &
dead_pid3=$!
wait "$dead_pid3" 2>/dev/null
printf '%s\n' "$dead_pid3" >"$LOCKDIR/pid"
echo "unknown" >"$LOCKDIR/start_token"

if command -v chflags >/dev/null 2>&1; then
    chflags uchg "$LOCKDIR"
    restore_perm() { chflags nouchg "$LOCKDIR" 2>/dev/null || true; }
else
    # Fallback for platforms without chflags (e.g. Linux): a read-only
    # parent directory blocks unlinking/renaming the lockdir's own entry.
    chmod 555 "$WORKDIR"
    restore_perm() { chmod 755 "$WORKDIR" 2>/dev/null || true; }
fi

t0=$(date +%s)
timeout 5 bash -c "source '$HERE/lock.sh'; lock_acquire '$LOCKDIR' 1" >/tmp/lock_test_out8 2>&1
rc8=$?
t1=$(date +%s)
elapsed8=$((t1 - t0))

restore_perm

if [[ $rc8 -eq 124 ]]; then
    bad "lock_acquire never returned within 5s when the reclaim action failed (C2 busy-loop: never honors timeout, elapsed=${elapsed8}s)"
else
    ok "lock_acquire returned (rc=$rc8) after ${elapsed8}s when the reclaim action failed -- did not spin forever"
fi

rm -rf "$LOCKDIR" 2>/dev/null || true
rm -f /tmp/lock_test_out8

# ── Test 6: EPERM-shape kill failure must NOT misclassify a live holder ──
# ── as stale (RDR-184 P0 review: kill -0 EPERM/ESRCH conflation) ─────────
# Reproduces the critic's own repro pattern: shadow the `kill` builtin so
# `kill -0 <pid>` fails exactly the way a real EPERM (process alive, but
# unsignalable -- e.g. a different user, or a hardened-runtime sandbox)
# would, against a lockdir whose recorded holder is a DEMONSTRABLY live
# background process. The module's own documented invariant (lines 36-40)
# says this must never be misclassified as stale.
echo "Test 6: EPERM-shape kill failure must not misclassify a live holder as stale"
LOCKDIR="$WORKDIR/eperm.lock"
mkdir "$LOCKDIR"
sleep 30 &
live_pid=$!
printf '%s\n' "$live_pid" >"$LOCKDIR/pid"
_lock_start_token "$live_pid" >"$LOCKDIR/start_token"

# shellcheck disable=SC2317,SC2329 # invoked indirectly via _lock_is_stale below
kill() {
    if [[ "$1" == "-0" ]]; then
        return 1 # simulate EPERM: nonzero, no signal delivered, but ALIVE
    fi
    command kill "$@"
}

if _lock_is_stale "$LOCKDIR" >/dev/null; then
    bad "_lock_is_stale treated an EPERM-shaped kill-0 failure as STALE (a live holder would be reclaimed -- EPERM/ESRCH conflation regression)"
else
    ok "_lock_is_stale correctly treats a live-but-unsignalable holder as HELD (not stale)"
fi

unset -f kill
kill -9 "$live_pid" 2>/dev/null || true
wait "$live_pid" 2>/dev/null
rm -rf "$LOCKDIR"

# ── Test 7: pid-recycle token-mismatch branch (previously zero coverage) ─
# The start-time-token comparison is, per the file's own docstring, the
# load-bearing defense against reclaiming a lock a NEW unrelated live
# process now holds under a recycled pid. Prior to this test neither
# branch of that comparison (mismatch -> stale; match -> held) was ever
# exercised against a genuinely live pid.
echo "Test 7: pid-recycle token-mismatch branch"
LOCKDIR="$WORKDIR/tokenmismatch.lock"
mkdir "$LOCKDIR"
sleep 30 &
live_pid2=$!
printf '%s\n' "$live_pid2" >"$LOCKDIR/pid"
# Fabricate a start_token that cannot match this pid's real one --
# simulating "the recorded holder pid has since been recycled by an
# unrelated live process".
echo "fabricated-mismatched-token-$$" >"$LOCKDIR/start_token"

if _lock_is_stale "$LOCKDIR" >/dev/null; then
    ok "_lock_is_stale detects a pid-recycle token mismatch as stale (correct reclaim)"
else
    bad "_lock_is_stale did NOT detect a start-token mismatch for a live pid -- pid-recycle mitigation is broken (the design's most load-bearing mechanism, was previously untested)"
fi

kill -9 "$live_pid2" 2>/dev/null || true
wait "$live_pid2" 2>/dev/null
rm -rf "$LOCKDIR"

echo "Test 7b: matching start token for a genuinely live holder is never stale"
LOCKDIR="$WORKDIR/tokenmatch.lock"
mkdir "$LOCKDIR"
sleep 30 &
live_pid3=$!
printf '%s\n' "$live_pid3" >"$LOCKDIR/pid"
_lock_start_token "$live_pid3" >"$LOCKDIR/start_token"

if _lock_is_stale "$LOCKDIR" >/dev/null; then
    bad "_lock_is_stale treated a live holder with a MATCHING start token as stale (false reclaim of a genuinely live, non-recycled holder)"
else
    ok "_lock_is_stale correctly treats a live holder with a matching start token as held (not stale)"
fi

kill -9 "$live_pid3" 2>/dev/null || true
wait "$live_pid3" 2>/dev/null
rm -rf "$LOCKDIR"

echo
echo "lock_test.sh: ${PASS} passed, ${FAIL} failed"
[[ "$FAIL" -eq 0 ]]
