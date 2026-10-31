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

# The suite builds its own fake repo and assumes the lease root is that repo's
# own. An inherited NX_BUILD_LEASE_ROOT (CI's lease step exports it, and so
# does the documented hand-run setup) would point the lease at the shared
# root: the lease dir is then missing from the fake repo, and the fixture
# leases this suite writes would land in the shared root.
unset NX_BUILD_LEASE_ROOT NX_SUITE_LEASE_WAIT NX_SUITE_LEASE_HELD_BY

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/build_lease_test.XXXXXX")"
trap 'rm -rf "$WORKDIR"' EXIT
# Never the real per-user lease-events log (nexus-jqk5c): Test 13 points it at its own files.
export NX_LEASE_EVENTS_LOG="$WORKDIR/events.log"

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

# A pid no process can hold: above Linux's maximum pid_max (4194304, 2^22) and macOS's
# pids (which stay below 100000), so _build_lease_pid_alive and _build_lease_group_alive always read it dead.
# A reaped child's pid is NOT dead for long: the kernel reuses it (nexus-5aotg).
NEVER_ALIVE_PID=99999999

# ── Test 1: acquire/release roundtrip ────────────────────────────────────
echo "Test 1: acquire/release roundtrip"
repo1="$WORKDIR/repo1"
_fake_repo "$repo1"
out1="$(bash -c "source '$repo1/scripts/lib/build-lease.sh'; build_lease_acquire smoke" 2>&1)"
rc1=$?
if [[ $rc1 -eq 0 ]]; then ok "acquire succeeded (rc 0)"; else bad "acquire failed (rc $rc1): $out1"; fi
leasedir1="$repo1/service/.build-lease/smoke"
if [[ -d "$leasedir1" ]]; then ok "lease dir exists after acquire"; else bad "lease dir missing after acquire"; fi
for f in pid ts label command worktree; do
    if [[ -f "$leasedir1/$f" ]]; then ok "lease has '$f' file"; else bad "lease missing '$f' file"; fi
done
if [[ "$(cat "$leasedir1/worktree" 2>/dev/null)" == "$(cd "$repo1" && pwd -P)" ]]; then ok "worktree names the taking checkout's physical path (nexus-qt6q7)"; else bad "worktree file: $(cat "$leasedir1/worktree" 2>&1)"; fi
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
# Manufacture a lease left behind by a holder that is definitely dead. The pid is
# NEVER_ALIVE_PID, above every kernel's pid ceiling: a reaped child's pid can be
# handed to a new process within a second on a busy runner, which turned this
# "dead" holder live (CI shard 5/6, 2026-10-29).
dead_pid="$NEVER_ALIVE_PID"
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
# The ready marker is written AFTER the trap is installed: the lease's pid
# file appears inside build_lease_acquire, before the trap, and a SIGTERM
# in that gap kills the holder with nothing to release the lease
# (nexus-n70xz, seen under a 32-worker suite).
ready4="$WORKDIR/repo4.ready"
# The holder starts with SIGTERM at its default and unblocked. A non-interactive bash
# cannot trap a signal that was ignored when it started, and a blocked one stays
# pending, and both pass across exec: a suite launched with TERM ignored or blocked
# made this case fail with the lease still held at 16 s and the holder alive
# (5588beead CI shard 3; reproduced in a container both ways, never under CPU load).
# The disposition the suite inherits is the launcher's business; the property under
# test is that a holder that CAN receive TERM releases its lease (the HUP twin:
# nexus-0a1hw).
holder4_cmd=(bash -c "source '$repo4/scripts/lib/build-lease.sh'; build_lease_acquire trapped || exit 9; trap 'build_lease_release trapped' EXIT; : > '$ready4'; sleep 30")
if command -v perl >/dev/null 2>&1; then
    perl -MPOSIX -e 'sigprocmask(SIG_UNBLOCK, POSIX::SigSet->new(SIGTERM)); $SIG{TERM} = "DEFAULT"; exec { $ARGV[0] } @ARGV or exit 127' "${holder4_cmd[@]}" &
else
    "${holder4_cmd[@]}" &
fi
holder4=$!
# State, SigBlk and SigIgn of a pid, for the failure message (Linux /proc only).
_sigs4() {
    [[ -r "/proc/$1/status" ]] || { printf 'pid %s: no /proc status' "$1"; return; }
    printf 'pid %s: %s' "$1" "$(awk '/^(State|SigBlk|SigIgn):/ { printf "%s %s ", $1, $2 }' "/proc/$1/status")"
}
# Polled up to 15 s: a holder that has to start, source and acquire under a loaded host
# (nexus-ys8a4) can take well over the 5 s this used to allow.
for _ in $(seq 1 150); do
    [[ -f "$ready4" ]] && break
    sleep 0.1
done
if [[ ! -f "$ready4" || ! -f "$leasedir4/pid" ]]; then
    bad "background holder never acquired the lease (setup failure)"
else
    # SIGTERM (not -9): catchable, so bash runs the EXIT trap before the
    # process actually terminates -- this is what should release the lease
    # WITHOUT needing stale-pid reclaim.
    kill -TERM "$holder4" 2>/dev/null
    term4_at=$SECONDS
    for _ in $(seq 1 150); do
        [[ ! -d "$leasedir4" ]] && break
        sleep 0.1
    done
    if [[ ! -d "$leasedir4" ]]; then
        ok "lease released by the holder's own EXIT trap after SIGTERM"
    else
        # Elapsed seconds tell the two causes apart: well past the 15 s the poll nominally
        # allows means the host starved the loop itself; about 15 s means the trap really
        # did not release.
        # The masks say which: a holder with SIGTERM (bit 15, 0x4000) in SigIgn or SigBlk
        # never acted on it, a Z state died without its trap, and the suite's own masks
        # say whether the disposition came from whatever launched the suite.
        bad "lease dir still present $((SECONDS - term4_at)) s after SIGTERM (polled up to 15 s); the holder should have released it [holder $(_sigs4 "$holder4")] [suite $(_sigs4 $$)]"
    fi
    # A fresh acquire must now succeed cleanly, with NO stale-reclaim
    # WARNING (the lease was released properly, not left stale).
    out4="$(bash -c "source '$repo4/scripts/lib/build-lease.sh'; build_lease_acquire trapped" 2>&1)"
    rc4=$?
    if [[ $rc4 -eq 0 ]]; then ok "fresh acquire after clean release succeeds"; else bad "fresh acquire failed (rc $rc4): $out4"; fi
    if [[ "$out4" != *"WARNING"* ]]; then ok "fresh acquire is clean (no stale-reclaim WARNING)"; else bad "unexpected WARNING on a cleanly-released lease: $out4"; fi
fi
# A holder that survived TERM would hold the suite here for the rest of its sleep.
kill -KILL "$holder4" 2>/dev/null
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
    if [[ "$(cat "$root6/shared/worktree" 2>/dev/null)" == "$(cd "$repo6" && pwd -P)" ]]; then ok "the shared lease names the primary as the building checkout, not the worktree"; else bad "worktree file: $(cat "$root6/shared/worktree" 2>&1)"; fi
fi
kill -9 "$holder6" 2>/dev/null
wait "$holder6" 2>/dev/null
bash -c "source '$WORKDIR/repo6-wt/scripts/lib/build-lease.sh'; build_lease_acquire fromwt >/dev/null 2>&1 || exit 9; cat '$root6/fromwt/worktree'; build_lease_release fromwt" > "$WORKDIR/wt6.out" 2>&1
if [[ "$(cat "$WORKDIR/wt6.out")" == "$(cd "$WORKDIR/repo6-wt" && pwd -P)" ]]; then ok "a lease taken from the worktree names the worktree, not the primary (nexus-qt6q7)"; else bad "worktree-taken lease names: $(cat "$WORKDIR/wt6.out")"; fi
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

# ── Test 9: the lease held across a WHOLE compound stamp->build->restore ──
# step (nexus-56qvf) actually serializes two gate-shaped wrappers against a
# SHARED fake "release.properties", never just the build call inside it.
# Models tests/e2e/local-service-gate.sh's fixed shape directly: acquire
# the SAME named lease before stamping, hold it through a (fake, sleeping)
# build, restore, THEN release -- a concurrent second wrapper must WAIT for
# the whole span, not just for a narrower "build" sub-step, so it can never
# observe the first wrapper's stamp mid-flight or race its own stamp/
# restore against it.
echo "Test 9: lease held across a full stamp->build->restore span serializes two gate-shaped wrappers"
repo9="$WORKDIR/repo9"
_fake_repo "$repo9"
props9="$repo9/service/target/fake-release.properties"
mkdir -p "$(dirname "$props9")"
printf 'release_version=\nbuild_ref=\n' > "$props9"
baseline9="$(cat "$props9")"

# A gate-shaped wrapper: acquire -> stamp -> sleep (stand-in for `./mvnw
# package`) -> restore -> release. Mirrors the ORDER local-service-gate.sh's
# fixed sequence now uses (bare build under an already-held lease, never a
# nested build_lease_acquire_wait call -- see that script's own comment on
# why nesting would deadlock instead of serializing).
_gate_wrapper9() {
    local repo="$1" stamp="$2" sleep_s="$3" out_file="$4"
    bash -c "
        source '$repo/scripts/lib/build-lease.sh'
        build_lease_acquire_wait service 60 fake-gate-$stamp || exit \$?
        printf 'release_version=%s\nbuild_ref=%s\n' '$stamp' '$stamp' > '$props9'
        sleep $sleep_s
        # the OBSERVED content while this wrapper holds the lease, for the
        # OTHER wrapper's out_file -- proves what was visible during the hold
        cat '$props9' > '$out_file.mid'
        printf 'release_version=\nbuild_ref=\n' > '$props9'
        build_lease_release service
    "
}

obs_a="$WORKDIR/obs_a"
obs_b="$WORKDIR/obs_b"
_gate_wrapper9 "$repo9" "STAMP-A" 2 "$obs_a" &
wrapper_a=$!
# Give A a head start on the acquire so B is the one forced to wait.
for _ in $(seq 1 50); do
    [[ -f "$repo9/service/.build-lease/service/pid" ]] && break
    sleep 0.05
done
start9=$SECONDS
_gate_wrapper9 "$repo9" "STAMP-B" 0 "$obs_b" &
wrapper_b=$!
wait "$wrapper_a" "$wrapper_b"
took9=$((SECONDS - start9))

if (( took9 >= 1 )); then
    ok "B's acquire genuinely waited for A's whole span (${took9}s), not just A's build sub-step"
else
    bad "B returned in ${took9}s -- it did not wait for A's stamp+build+restore at all"
fi
if [[ -f "$obs_a.mid" && "$(cat "$obs_a.mid")" == *"STAMP-A"* ]]; then
    ok "A observed its OWN stamp while holding the lease"
else
    bad "A did not observe its own stamp: $(cat "$obs_a.mid" 2>/dev/null)"
fi
if [[ -f "$obs_b.mid" && "$(cat "$obs_b.mid")" == *"STAMP-B"* ]]; then
    ok "B observed its OWN stamp while holding the lease (never A's, and never a blank clobber)"
else
    bad "B did not observe its own stamp cleanly: $(cat "$obs_b.mid" 2>/dev/null)"
fi
final9="$(cat "$props9")"
if [[ "$final9" == "$baseline9" ]]; then
    ok "props file is back to the pre-invocation baseline after both wrappers finished"
else
    bad "props file left stamped/corrupted after both wrappers finished: $final9"
fi
if [[ ! -d "$repo9/service/.build-lease/service" ]]; then
    ok "lease released after both wrappers finished"
else
    bad "lease dir still present after both wrappers finished"
fi

# ── Test 10: a process group owned by ANOTHER USER is alive, not stale ───
# Two users sharing one lease root (NX_BUILD_LEASE_ROOT) is the shape: the
# holder's `kill -0 -- -PGID` gets EPERM from the second user, which must
# read as "still held" (build-lease.sh header), not as a dead group.
echo "Test 10: a group the caller cannot signal (another user's) counts as alive"
repo10="$WORKDIR/repo10"
_fake_repo "$repo10"
if [[ "$(id -u)" -eq 0 ]]; then
    ok "skipped: root can signal every group (no EPERM to observe)"
else
    # Not group 1: `kill -0 -- -1` is "every process" (pid -1), which always succeeds.
    foreign_pgid="$(ps -axo pgid=,user= 2>/dev/null | awk '$2 == "root" && $1 > 1 { print $1; exit }')"
    if [[ -z "$foreign_pgid" ]]; then
        bad "found no root-owned process group to probe with: the test has no evidence (it must not pass vacuously)"
    else
        if bash -c "source '$repo10/scripts/lib/build-lease.sh'; _build_lease_group_alive $foreign_pgid"; then
            ok "root's group $foreign_pgid reads as alive from an unprivileged user (EPERM is not ESRCH)"
        else
            bad "root's group $foreign_pgid read as DEAD: a second user would reclaim a live build lease"
        fi
    fi
    if bash -c "source '$repo10/scripts/lib/build-lease.sh'; _build_lease_group_alive $NEVER_ALIVE_PID"; then
        bad "a group that does not exist read as alive"
    else
        ok "a group that does not exist still reads as dead"
    fi
fi

# ── Test 11 (nexus-t6t9f): losing the stale-reclaim race is retried, not refused, even at wait 0.
# A hand run inside a lab-run grant waits 0 s (build_lease_caller_wait). Two contenders racing to
# reclaim one stale lease: the loser used to read "lost the race ... Retry." as a held lease and
# refuse with 75, though the lease was free a moment later. `mv` is shadowed by a shell function
# that fails its first N calls (counted in a file: acquire runs in a $(...) subshell), which is
# exactly what the loser of the rename sees.
echo "Test 11: a lost stale-reclaim race is retried at wait 0; a race lost forever still ends in rc 75"
repo11="$WORKDIR/repo11"
_fake_repo "$repo11"
_stale11() {
    rm -rf "$repo11/service/.build-lease/race"
    bash -c "source '$repo11/scripts/lib/build-lease.sh'; build_lease_acquire race" >/dev/null 2>&1   # exits without releasing: stale
}
_race11() {  # _race11 <failing-mv-calls> <wait>
    local cnt="$WORKDIR/mv11.count"; : > "$cnt"
    MV_COUNT="$cnt" MV_FAILS="$1" bash -c "
        source '$repo11/scripts/lib/build-lease.sh'
        mv() { echo x >> \"\$MV_COUNT\"; if [[ \$(wc -l < \"\$MV_COUNT\" | tr -d ' ') -le \$MV_FAILS ]]; then return 1; fi; command mv \"\$@\"; }
        build_lease_acquire_wait race $2 && echo HELD" 2>&1
}
_stale11
start11=$SECONDS
out11="$(_race11 1 0)"; rc11=$?
if [[ $rc11 -eq 0 && "$out11" == *HELD* ]]; then ok "one lost reclaim race at wait 0 is retried and the lease is acquired"; else bad "wait 0, one lost race: rc $rc11: $out11"; fi
if [[ "$(wc -l < "$WORKDIR/mv11.count" | tr -d ' ')" == 2 ]]; then ok "the reclaim ran twice (the lost race, then the retry)"; else bad "reclaim attempts: $(wc -l < "$WORKDIR/mv11.count")"; fi
if (( SECONDS - start11 < 10 )); then ok "the retry did not wait out a held-lease poll"; else bad "took $((SECONDS - start11))s"; fi
_stale11
out11b="$(_race11 1000 0)"; rc11b=$?
attempts11="$(wc -l < "$WORKDIR/mv11.count" | tr -d ' ')"
if [[ $rc11b -eq 75 && "$out11b" == *"lost the race"* ]]; then ok "a race lost on every attempt still ends in rc 75 with the reason"; else bad "perpetual race: rc $rc11b: $out11b"; fi
if (( attempts11 > 1 && attempts11 <= 10 )); then ok "the retries are bounded ($attempts11 attempts)"; else bad "attempts: $attempts11"; fi

# ── Test 12 (nexus-t6t9f): build_lease_untrack_pid points liveness back at the holder's own pid.
# A holder whose tracked build group has exited but which is itself alive must not be reclaimable.
echo "Test 12: after build_lease_untrack_pid a live holder is not reclaimed; a non-holder's call is a no-op"
repo12="$WORKDIR/repo12"
_fake_repo "$repo12"
rm -rf "$repo12/service/.build-lease/untrack"
out12="$WORKDIR/untrack12.out"; rm -f "$out12"
bash -c "
    source '$repo12/scripts/lib/build-lease.sh'
    build_lease_acquire untrack || exit 9
    dead=$NEVER_ALIVE_PID
    build_lease_track_pid untrack \$dead
    echo TRACKED > '$out12'
    sleep 2
    build_lease_untrack_pid untrack
    echo UNTRACKED >> '$out12'
    sleep 6" >/dev/null 2>&1 &
holder12=$!
for _ in $(seq 1 50); do grep -q TRACKED "$out12" 2>/dev/null && break; sleep 0.1; done
peer12="$(bash -c "source '$repo12/scripts/lib/build-lease.sh'; build_lease_acquire untrack" 2>&1)"; peerrc12=$?
if [[ $peerrc12 -eq 0 && "$peer12" == *"reclaiming stale"* ]]; then ok "control: while the dead group is tracked the lease reads stale and is reclaimed"; else bad "control: rc $peerrc12: $peer12"; fi
kill -9 "$holder12" 2>/dev/null; wait "$holder12" 2>/dev/null
rm -rf "$repo12/service/.build-lease/untrack"
rm -f "$out12"
bash -c "
    source '$repo12/scripts/lib/build-lease.sh'
    build_lease_acquire untrack || exit 9
    dead=$NEVER_ALIVE_PID
    build_lease_track_pid untrack \$dead
    build_lease_untrack_pid untrack
    echo UNTRACKED > '$out12'
    sleep 6" >/dev/null 2>&1 &
holder12=$!
for _ in $(seq 1 50); do grep -q UNTRACKED "$out12" 2>/dev/null && break; sleep 0.1; done
peer12="$(bash -c "source '$repo12/scripts/lib/build-lease.sh'; build_lease_acquire untrack" 2>&1)"; peerrc12=$?
if [[ $peerrc12 -eq 75 && "$peer12" == *REFUSED* && "$peer12" != *"reclaiming"* ]]; then ok "an untracked live holder is refused, not reclaimed"; else bad "rc $peerrc12: $peer12"; fi
if [[ ! -e "$repo12/service/.build-lease/untrack/pgid" ]]; then ok "the pgid file is gone"; else bad "pgid file remains"; fi
# a non-holder (this shell) must not touch the pgid
printf '%s\n' 99999 > "$repo12/service/.build-lease/untrack/pgid"
bash -c "source '$repo12/scripts/lib/build-lease.sh'; build_lease_untrack_pid untrack"
if [[ "$(cat "$repo12/service/.build-lease/untrack/pgid" 2>/dev/null)" == 99999 ]]; then ok "a non-holder's untrack is a no-op"; else bad "a non-holder removed the pgid"; fi
kill -9 "$holder12" 2>/dev/null; wait "$holder12" 2>/dev/null

# ── Test 13: lease telemetry (nexus-jqk5c) ──────────────────────────────
# One key=value line per CONTENDED event, appended to NX_LEASE_EVENTS_LOG; an
# immediate acquire writes nothing; a logging failure changes nothing.
echo "Test 13: lease events log (one line per contention event, none for an immediate acquire)"
repo13="$WORKDIR/repo13"
_fake_repo "$repo13"
ev13="$WORKDIR/ev13.log"
lib13="$repo13/scripts/lib/build-lease.sh"
leasedir13="$repo13/service/.build-lease"
_ev_lines() { if [[ -f "$1" ]]; then wc -l < "$1" | tr -d ' '; else echo 0; fi; }
_ev_shape() {  # the fields every event carries, in order
    [[ "$1" =~ ^ts=[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\ lease=service\ name=[^\ ]+\ host=[^\ ]+\ pid=[0-9]+\ waited_s=[0-9]+\ outcome=[a-z_]+ ]]
}

# 13a: an immediate, uncontended acquire writes nothing (and the log is not even created)
rm -f "$ev13"
NX_LEASE_EVENTS_LOG="$ev13" bash -c "source '$lib13'; build_lease_acquire quiet && build_lease_release quiet" >/dev/null 2>&1
if [[ ! -e "$ev13" ]]; then ok "an immediate acquire writes no event"; else bad "an immediate acquire wrote: $(cat "$ev13")"; fi
NX_LEASE_EVENTS_LOG="$ev13" bash -c "source '$lib13'; build_lease_acquire_wait quiet 30 && build_lease_release quiet" >/dev/null 2>&1
if [[ ! -e "$ev13" ]]; then ok "an immediate acquire_wait writes no event"; else bad "an immediate acquire_wait wrote: $(cat "$ev13")"; fi

# 13b: refused (live holder, no wait) -> one refused line naming the holder, label quoted
rm -f "$ev13"
NX_LEASE_EVENTS_LOG="$WORKDIR/ev13-holder.log" NX_AGENT="holder agent" bash -c "source '$lib13'; build_lease_acquire busy || exit 9; sleep 25" >/dev/null 2>&1 &
holder13=$!
for _ in $(seq 1 50); do [[ -f "$leasedir13/busy/pid" ]] && break; sleep 0.1; done
out13="$(NX_LEASE_EVENTS_LOG="$ev13" bash -c "source '$lib13'; build_lease_acquire busy" 2>&1)"; rc13=$?
if [[ $rc13 -eq 75 ]]; then ok "refused: rc 75"; else bad "refused: rc $rc13: $out13"; fi
if [[ "$(_ev_lines "$ev13")" == 1 ]]; then ok "refused: exactly one line"; else bad "refused: lines=$(_ev_lines "$ev13"): $(cat "$ev13" 2>&1)"; fi
line13="$(head -1 "$ev13" 2>/dev/null)"
if _ev_shape "$line13" && [[ "$line13" == *" name=busy "* && "$line13" == *" waited_s=0 outcome=refused "* && "$line13" == *" holder_pid=$holder13 "* && "$line13" == *' holder_label="holder agent"'* ]]; then ok "refused: line shape ($line13)"; else bad "refused: line: $line13"; fi

# 13c: acquire_wait with max 0 on a live holder -> one refused line (a refusal, not a give-up)
rm -f "$ev13"
NX_LEASE_EVENTS_LOG="$ev13" bash -c "source '$lib13'; build_lease_acquire_wait busy 0" >/dev/null 2>&1; rc13=$?
line13="$(head -1 "$ev13" 2>/dev/null)"
if [[ $rc13 -eq 75 && "$(_ev_lines "$ev13")" == 1 && "$line13" == *" waited_s=0 outcome=refused "* && "$line13" == *" holder_pid=$holder13 "* ]]; then ok "wait 0: one refused line ($line13)"; else bad "wait 0: rc $rc13 lines=$(_ev_lines "$ev13"): $line13"; fi

# 13d: acquire_wait that times out -> ONE gave_up line (the 5s polls log nothing)
rm -f "$ev13"
NX_LEASE_EVENTS_LOG="$ev13" bash -c "source '$lib13'; build_lease_acquire_wait busy 5" >/dev/null 2>&1; rc13=$?
line13="$(head -1 "$ev13" 2>/dev/null)"
if [[ $rc13 -eq 75 && "$(_ev_lines "$ev13")" == 1 && "$line13" == *" waited_s=5 outcome=gave_up "* && "$line13" == *" holder_pid=$holder13 "* ]]; then ok "timeout: one gave_up line ($line13)"; else bad "timeout: rc $rc13 lines=$(_ev_lines "$ev13"): $(cat "$ev13" 2>&1)"; fi
kill -9 "$holder13" 2>/dev/null; wait "$holder13" 2>/dev/null
rm -rf "$leasedir13/busy"

# 13e: acquired after a wait -> one acquired_after_wait line carrying the holder that was waited for
rm -f "$ev13"
NX_AGENT=releaser bash -c "source '$lib13'; build_lease_acquire hand || exit 9; sleep 2; build_lease_release hand" >/dev/null 2>&1 &
holder13=$!
for _ in $(seq 1 50); do [[ -f "$leasedir13/hand/pid" ]] && break; sleep 0.1; done
NX_LEASE_EVENTS_LOG="$ev13" bash -c "source '$lib13'; build_lease_acquire_wait hand 60 && build_lease_release hand" >/dev/null 2>&1; rc13=$?
wait "$holder13" 2>/dev/null
line13="$(head -1 "$ev13" 2>/dev/null)"
if [[ $rc13 -eq 0 && "$(_ev_lines "$ev13")" == 1 && "$line13" == *" waited_s=5 outcome=acquired_after_wait "* && "$line13" == *" holder_pid=$holder13 holder_label=releaser"* ]]; then ok "waited: one acquired_after_wait line ($line13)"; else bad "waited: rc $rc13 lines=$(_ev_lines "$ev13"): $(cat "$ev13" 2>&1)"; fi

# 13f: stale reclaim -> one stale_reclaimed line naming the dead holder
rm -f "$ev13"
mkdir -p "$leasedir13/corpse"
dead13="$NEVER_ALIVE_PID"
printf '%s\n' "$dead13" > "$leasedir13/corpse/pid"; echo "2026-01-01T00:00:00Z" > "$leasedir13/corpse/ts"; echo dead-agent > "$leasedir13/corpse/label"
out13="$(NX_LEASE_EVENTS_LOG="$ev13" bash -c "source '$lib13'; build_lease_acquire corpse && build_lease_release corpse" 2>&1)"; rc13=$?
line13="$(head -1 "$ev13" 2>/dev/null)"
if [[ $rc13 -eq 0 && "$(_ev_lines "$ev13")" == 1 && "$line13" == *" name=corpse "* && "$line13" == *" waited_s=0 outcome=stale_reclaimed "* && "$line13" == *" holder_pid=$dead13 holder_label=dead-agent"* ]]; then ok "stale: one stale_reclaimed line ($line13)"; else bad "stale: rc $rc13 lines=$(_ev_lines "$ev13"): $(cat "$ev13" 2>&1)"; fi

# 13f2: a wait that ends in a stale reclaim logs ONE line (stale_reclaimed, carrying the wait)
rm -f "$ev13"
NX_AGENT=crasher bash -c "source '$lib13'; build_lease_acquire crash || exit 9; sleep 2; kill -9 \$\$" >/dev/null 2>&1 &
holder13=$!
for _ in $(seq 1 50); do [[ -f "$leasedir13/crash/pid" ]] && break; sleep 0.1; done
NX_LEASE_EVENTS_LOG="$ev13" bash -c "source '$lib13'; build_lease_acquire_wait crash 60 && build_lease_release crash" >/dev/null 2>&1; rc13=$?
wait "$holder13" 2>/dev/null
line13="$(head -1 "$ev13" 2>/dev/null)"
if [[ $rc13 -eq 0 && "$(_ev_lines "$ev13")" == 1 && "$line13" == *" waited_s=5 outcome=stale_reclaimed "* && "$line13" == *" holder_pid=$holder13 holder_label=crasher"* ]]; then ok "wait ending in a stale reclaim: one stale_reclaimed line, no second acquired_after_wait ($line13)"; else bad "wait+stale: rc $rc13 lines=$(_ev_lines "$ev13"): $(cat "$ev13" 2>&1)"; fi

# 13g: an unwritable log leaves the return code and stderr unchanged
bad_log13="$WORKDIR/a-file/events.log"; : > "$WORKDIR/a-file"   # a path UNDER a regular file: mkdir and append both fail
NX_AGENT="holder agent" bash -c "source '$lib13'; build_lease_acquire busy2 || exit 9; sleep 8" >/dev/null 2>&1 &
holder13=$!
for _ in $(seq 1 50); do [[ -f "$leasedir13/busy2/pid" ]] && break; sleep 0.1; done
good13="$(NX_LEASE_EVENTS_LOG="$WORKDIR/ev13-good.log" bash -c "source '$lib13'; build_lease_acquire busy2" 2>&1)"; goodrc13=$?
badout13="$(NX_LEASE_EVENTS_LOG="$bad_log13" bash -c "source '$lib13'; build_lease_acquire busy2" 2>&1)"; badrc13=$?
if [[ $badrc13 -eq $goodrc13 && "$badout13" == "$good13" ]]; then ok "an unwritable log changes neither rc nor stderr"; else bad "rc $badrc13 vs $goodrc13; stderr: $badout13 vs $good13"; fi
# A SUCCESS path under set -e: a stale reclaim whose log write fails. errexit is ignored in
# `||` and `if` context, so the call is bare, in its own set -e subshell; only a log writer that
# leaks a non-zero status would end the subshell before SURVIVED.
# Both ways a log can be unwritable: the mkdir fails (parent is a file) and the append fails
# (the log is a directory; the mkdir succeeds, so only the guard stops errexit there).
mkdir -p "$WORKDIR/a-dir-log"
for how13 in "$bad_log13" "$WORKDIR/a-dir-log"; do
    mkdir -p "$leasedir13/corpse2"
    dead13="$NEVER_ALIVE_PID"
    printf '%s\n' "$dead13" > "$leasedir13/corpse2/pid"; echo dead-agent > "$leasedir13/corpse2/label"
    badout13="$(NX_LEASE_EVENTS_LOG="$how13" bash -c "set -e; source '$lib13'; build_lease_acquire corpse2 >/dev/null 2>&1; build_lease_release corpse2; echo SURVIVED" 2>&1)"
    if [[ "$badout13" == SURVIVED ]]; then ok "an unwritable log (${how13##*/}) under set -e does not abort a successful stale reclaim"; else bad "set -e caller (${how13##*/}): [$badout13]"; fi
done
kill -9 "$holder13" 2>/dev/null; wait "$holder13" 2>/dev/null
rm -rf "$leasedir13/busy2"

# 13i: the lease named suite logs as lease=suite (tests/e2e/macos-guest/host.sh takes it through build_lease_acquire)
rm -f "$ev13"
sleep 25 & peer13=$!
mkdir -p "$leasedir13/suite"; printf '%s\n' "$peer13" > "$leasedir13/suite/pid"; echo peer-suite > "$leasedir13/suite/label"
NX_LEASE_EVENTS_LOG="$ev13" bash -c "source '$lib13'; build_lease_acquire suite" >/dev/null 2>&1
line13="$(head -1 "$ev13" 2>/dev/null)"
if [[ "$line13" == *" lease=suite name=suite "* && "$line13" == *" outcome=refused "* ]]; then ok "the suite lease logs lease=suite ($line13)"; else bad "suite lease line: $line13"; fi
kill -9 "$peer13" 2>/dev/null; wait "$peer13" 2>/dev/null
rm -rf "$leasedir13/suite"

# 13j: a stale reclaim that loses the mv race names the CURRENT holder, never the dead one it read
rm -f "$ev13"
sleep 25 & peer13=$!
mkdir -p "$leasedir13/raced" "$WORKDIR/fakebin13"
dead13="$NEVER_ALIVE_PID"
printf '%s\n' "$dead13" > "$leasedir13/raced/pid"; echo dead-agent > "$leasedir13/raced/label"
# a peer wins the reclaim between our read and our mv: the lease is the peer's by the time ours fails
cat > "$WORKDIR/fakebin13/mv" <<MV
#!/bin/sh
rm -rf "\$1"; mkdir "\$1"; echo $peer13 > "\$1/pid"; echo racing-peer > "\$1/label"; exit 1
MV
chmod +x "$WORKDIR/fakebin13/mv"
NX_LEASE_EVENTS_LOG="$ev13" PATH="$WORKDIR/fakebin13:$PATH" bash -c "source '$lib13'; build_lease_acquire raced" >/dev/null 2>&1; rc13=$?
line13="$(head -1 "$ev13" 2>/dev/null)"
if [[ $rc13 -eq 75 && "$(_ev_lines "$ev13")" == 1 && "$line13" == *" outcome=refused "* && "$line13" == *" holder_pid=$peer13 holder_label=racing-peer"* && "$line13" != *"$dead13"* ]]; then ok "a lost reclaim race logs refused naming the current holder ($line13)"; else bad "lost race: rc $rc13 lines=$(_ev_lines "$ev13"): $line13 (dead=$dead13)"; fi
kill -9 "$peer13" 2>/dev/null; wait "$peer13" 2>/dev/null
rm -rf "$leasedir13/raced"

# 13k: a new log is group-writable whatever the caller's umask, an existing one keeps its mode
rm -f "$ev13"
NX_LEASE_EVENTS_LOG="$ev13" bash -c "umask 077; source '$lib13'; _build_lease_log_event modes modes 0" >/dev/null 2>&1
mode13="$(stat -c %a "$ev13" 2>/dev/null || stat -f %Lp "$ev13" 2>/dev/null)"
if [[ "$mode13" == 664 ]]; then ok "a new events log is mode 664 under umask 077"; else bad "new log mode: $mode13"; fi
chmod 600 "$ev13"
NX_LEASE_EVENTS_LOG="$ev13" bash -c "umask 077; source '$lib13'; _build_lease_log_event modes modes 0" >/dev/null 2>&1
mode13="$(stat -c %a "$ev13" 2>/dev/null || stat -f %Lp "$ev13" 2>/dev/null)"
if [[ "$mode13" == 600 && "$(_ev_lines "$ev13")" == 2 ]]; then ok "an existing events log keeps its mode"; else bad "existing log mode: $mode13 lines=$(_ev_lines "$ev13")"; fi

# 13h: concurrent writers never interleave a line
rm -f "$ev13"
for i in $(seq 1 20); do
    NX_LEASE_EVENTS_LOG="$ev13" bash -c "source '$lib13'; _build_lease_log_event conc conc 0 \$\$ 'label $i'" >/dev/null 2>&1 &
done
wait
if [[ "$(_ev_lines "$ev13")" == 20 && "$(grep -c -E '^ts=[0-9T:Z-]+ lease=service name=conc host=[^ ]+ pid=[0-9]+ waited_s=0 outcome=conc holder_pid=[0-9]+ holder_label="label [0-9]+"$' "$ev13")" == 20 ]]; then ok "20 concurrent writers: 20 whole lines"; else bad "concurrent: lines=$(_ev_lines "$ev13") whole=$(grep -c -E 'holder_label="label [0-9]+"$' "$ev13")"; fi

# ── Test 14: a holder caught between its pid create and its populate ─────
# Develop CI 40568573750 (ed5d8475c) shard 6: Test 2's contender read the holder
# after its exclusive pid create and before its label write, and the refusal said
# "label=unknown, acquired=unknown, command: unknown". The claim is the pid; the
# label, ts and command follow, so a contender that finds a LIVE pid without the
# populate's last file (command) waits for it, a bounded 2 s, before it reads.
echo "Test 14: a refusal names a holder that is still populating its lease"
repo14="$WORKDIR/repo14"
_fake_repo "$repo14"
lib14="$repo14/scripts/lib/build-lease.sh"
root14="$repo14/service/.build-lease"
sleep 60 & live14=$!
# Stand up the window by hand: pid (the claim) now, the rest later, in populate's order.
_stage14() {   # <name> <delay-s|never> — the claim now, the populate after <delay-s>
    local d="$root14/$1"
    mkdir -p "$d"
    printf '%s\n' "$live14" > "$d/pid"
    if [[ "$2" != never ]]; then
        (
            sleep "$2"
            printf '%s\n' "2026-01-02T03:04:05Z" > "$d/ts"
            printf '%s\n' "late-label" > "$d/label"
            printf '%s\n' "late-command arg" > "$d/command"
        ) &
    fi
}
_stage14 popwin 0.6
out14="$(bash -c "source '$lib14'; build_lease_acquire popwin" 2>&1)"
rc14=$?
if [[ $rc14 -eq 75 ]]; then ok "14a: the contender is refused (rc 75)"; else bad "14a: rc $rc14: $out14"; fi
if [[ "$out14" == *"label=late-label"* ]]; then ok "14a: the refusal names the label written after the pid"; else bad "14a: label not named: $out14"; fi
if [[ "$out14" == *"acquired=2026-01-02T03:04:05Z"* && "$out14" == *"command: late-command arg"* ]]; then ok "14a: acquired and command are named too"; else bad "14a: ts or command missing: $out14"; fi
sleep 1     # the staged writer has finished before the next case reuses the directory
# 14b: the events log reads the same populated lease (the held-event reader)
ev14="$WORKDIR/ev14.log"
rm -rf "${root14:?}/popwin"
_stage14 popwin 0.6
NX_LEASE_EVENTS_LOG="$ev14" bash -c "source '$lib14'; build_lease_acquire_wait popwin 0" >/dev/null 2>&1
if grep -q ' holder_label=late-label' "$ev14" 2>/dev/null; then ok "14b: the events log names the label of a holder that was populating"; else bad "14b: events log: $(cat "$ev14" 2>&1)"; fi
sleep 1
# 14c: a live holder that never populates costs the contender a bounded wait, then the old text
rm -rf "${root14:?}/popwin"
_stage14 popwin never
t14=$(date +%s)
out14c="$(_BUILD_LEASE_POPULATE_POLLS=6 bash -c "source '$lib14'; build_lease_acquire popwin" 2>&1)"
rc14c=$?
d14=$(( $(date +%s) - t14 ))
if [[ $rc14c -eq 75 && "$out14c" == *"label=unknown"* && $d14 -le 2 ]]; then ok "14c: a holder that never populates is still refused, after a bounded wait (${d14}s)"; else bad "14c: rc $rc14c after ${d14}s: $out14c"; fi
# 14d: a DEAD holder with an unpopulated lease is reclaimed without the wait
rm -rf "${root14:?}/popwin"
dead14="$NEVER_ALIVE_PID"   # a reaped child's pid could be reused and polled as live for the full 2 s
mkdir -p "$root14/popwin"
printf '%s\n' "$dead14" > "$root14/popwin/pid"
t14=$(date +%s)
# "Without the wait" is proven against a wait that would be visible: 100 polls of 1 s, so a
# contender that waited would cost 100 s, and a bound of 10 s is safe under CI load (develop
# 18f805d8a, shard 5: the old 1 s bound read 2 s on a whole-second clock with no wait taken).
out14d="$(_BUILD_LEASE_POPULATE_POLLS=100 _BUILD_LEASE_POPULATE_POLL_S=1 bash -c "source '$lib14'; build_lease_acquire popwin" 2>&1)"
rc14d=$?
d14=$(( $(date +%s) - t14 ))
if [[ $rc14d -eq 0 && "$out14d" == *WARNING* && $d14 -le 10 ]]; then ok "14d: a dead holder's unpopulated lease is reclaimed at once (${d14}s, a wait would cost 100 s)"; else bad "14d: rc $rc14d after ${d14}s: $out14d"; fi
# 14e: an uncontended acquire never reaches the wait (no pid file to wait on)
rm -rf "${root14:?}/popwin"
t14=$(date +%s)
_BUILD_LEASE_POPULATE_POLLS=100 _BUILD_LEASE_POPULATE_POLL_S=1 bash -c "source '$lib14'; build_lease_acquire popwin" >/dev/null 2>&1
rc14e=$?
d14=$(( $(date +%s) - t14 ))
if [[ $rc14e -eq 0 && $d14 -le 10 ]]; then ok "14e: an uncontended acquire adds no wait (${d14}s, a wait would cost 100 s)"; else bad "14e: rc $rc14e after ${d14}s"; fi
kill -9 "$live14" 2>/dev/null
wait "$live14" 2>/dev/null

echo
echo "build-lease_test.sh: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]
