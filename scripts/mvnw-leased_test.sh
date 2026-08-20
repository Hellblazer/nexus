#!/usr/bin/env bash
# scripts/mvnw-leased_test.sh — shell-level test for mvnw-leased.sh (bead
# nexus-c00dw): proves the wrapper acquires the build lease, execs a
# `./mvnw` stub with args passed through untouched, and releases the lease
# afterward. Self-provisioning: builds a throwaway fake-repo tmpdir with a
# FAKE `service/mvnw` stub — never invokes the real ./mvnw. No pytest, no
# engine substrate. Run directly with bash:
#   bash scripts/mvnw-leased_test.sh
set -u -o pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/mvnw_leased_test.XXXXXX")"
trap 'rm -rf "$WORKDIR"' EXIT

PASS=0
FAIL=0
SKIP=0
ok() { echo "  [ok] $1"; PASS=$((PASS + 1)); }
bad() { echo "  [FAIL] $1"; FAIL=$((FAIL + 1)); }
skip() { echo "  [skip] $1"; SKIP=$((SKIP + 1)); }

# _sigint_delivery_works — runtime probe: can a `trap ... INT` installed in
# THIS shell ever fire from a self-`kill -INT`? Some sandboxed execution
# environments (confirmed empirically for the harness this suite was
# authored in, nexus-c00dw round 3) inherit SIGINT as SIG_IGN at the root
# of the process tree — a disposition bash's own `trap` builtin is
# DOCUMENTED to be unable to override ("signals ignored upon entry to the
# shell cannot be trapped or reset"), regardless of `set -m`, backgrounding,
# or nesting depth. Probing this ONCE, honestly, and skipping the
# OS-level SIGINT sub-test with a clear reason when it is unavailable, beats
# either a fabricated pass or a false failure that blames the code for an
# environment property. TERM is NOT subject to this rule and needs no probe
# — Test 4 already proves it works end to end.
_sigint_delivery_works() {
    local marker="$WORKDIR/.sigint-probe-$$"
    rm -f "$marker"
    ( trap 'touch "'"$marker"'"; exit 0' INT; kill -INT $$; sleep 0.3 ) >/dev/null 2>&1
    [[ -f "$marker" ]]
}

repo="$WORKDIR/repo"
mkdir -p "$repo/scripts/lib" "$repo/service/target" "$repo/service/.build-lease"
cp "$HERE/lib/build-lease.sh" "$repo/scripts/lib/build-lease.sh"
cp "$HERE/mvnw-leased.sh" "$repo/scripts/mvnw-leased.sh"
chmod +x "$repo/scripts/mvnw-leased.sh"

# Fake ./mvnw stub: records the args it was invoked with and the cwd it ran
# from, in a marker file the test can inspect. Never the real mvnw.
cat > "$repo/service/mvnw" <<'STUB'
#!/usr/bin/env bash
{
    echo "CWD=$(pwd)"
    echo "ARGS=$*"
} > "$MVNW_STUB_MARKER"
exit 0
STUB
chmod +x "$repo/service/mvnw"

# ── Test 1: args pass through untouched, cwd is service/ ────────────────
echo "Test 1: mvnw-leased.sh passes args through to the (fake) mvnw"
marker="$WORKDIR/stub-invocation.txt"
MVNW_STUB_MARKER="$marker" "$repo/scripts/mvnw-leased.sh" -q package -DskipTests --also-make
rc1=$?
if [[ $rc1 -eq 0 ]]; then ok "wrapper exited 0 (fake mvnw succeeded)"; else bad "wrapper exited $rc1"; fi
if [[ -f "$marker" ]]; then
    ok "fake mvnw was actually invoked"
    content="$(cat "$marker")"
    if [[ "$content" == *"ARGS=-q package -DskipTests --also-make"* ]]; then
        ok "args passed through untouched"
    else
        bad "args mismatch: $content"
    fi
    # Compare against the shell's OWN normalized view of the path (`cd &&
    # pwd`), not a raw string concat: $TMPDIR on macOS can carry a
    # trailing slash, which `pwd` collapses but a literal "$repo/service"
    # string does not — a spurious mismatch, not a real cwd bug.
    expected_service_dir="$(cd "$repo/service" && pwd)"
    if [[ "$content" == *"CWD=$expected_service_dir"* ]]; then
        ok "fake mvnw ran with cwd=service/"
    else
        bad "unexpected cwd: $content (expected CWD=$expected_service_dir)"
    fi
else
    bad "fake mvnw was never invoked (marker file missing) — lease acquire likely failed"
fi
leasedir="$repo/service/.build-lease/service"
if [[ ! -d "$leasedir" ]]; then
    ok "lease released after mvnw-leased.sh exits normally"
else
    bad "lease still held after mvnw-leased.sh exited"
fi

# ── Test 2: refuses (rc 75) and never invokes mvnw when the lease is held ─
echo "Test 2: mvnw-leased.sh refuses when the lease is already held"
bash -c "source '$repo/scripts/lib/build-lease.sh'; build_lease_acquire service || exit 9; sleep 10" &
holder_pid=$!
for _ in $(seq 1 50); do
    [[ -f "$leasedir/pid" ]] && break
    sleep 0.1
done
if [[ ! -f "$leasedir/pid" ]]; then
    bad "background holder never acquired the lease (setup failure)"
else
    marker2="$WORKDIR/stub-invocation-2.txt"
    rm -f "$marker2"
    out2="$(MVNW_STUB_MARKER="$marker2" "$repo/scripts/mvnw-leased.sh" -q package 2>&1)"
    rc2=$?
    if [[ $rc2 -eq 75 ]]; then ok "wrapper refused with rc 75"; else bad "wrapper returned rc $rc2 (expected 75): $out2"; fi
    if [[ ! -f "$marker2" ]]; then ok "fake mvnw was never invoked while lease was held"; else bad "fake mvnw ran despite the lease being held"; fi
fi
kill -9 "$holder_pid" 2>/dev/null
wait "$holder_pid" 2>/dev/null

# ── Test 3: SIGKILLing just the wrapper does NOT free the lease while its
# real build child (tracked via build_lease_track_pid / process group) is
# still alive — review finding, nexus-c00dw. ─────────────────────────────
echo "Test 3: killing the wrapper alone must not free a still-building lease"
started_marker="$WORKDIR/stub3-started.txt"
rm -f "$started_marker"
cat > "$repo/service/mvnw" <<STUB
#!/usr/bin/env bash
echo "\$\$" > "$started_marker"
sleep 30
STUB
chmod +x "$repo/service/mvnw"

"$repo/scripts/mvnw-leased.sh" build &
wrapper_pid=$!

for _ in $(seq 1 50); do
    [[ -f "$started_marker" ]] && break
    sleep 0.1
done
if [[ ! -f "$started_marker" ]]; then
    bad "fake mvnw (sleeping child) never started (setup failure)"
else
    child_pid="$(cat "$started_marker")"
    for _ in $(seq 1 50); do
        [[ -f "$leasedir/pgid" ]] && break
        sleep 0.1
    done
    if [[ -f "$leasedir/pgid" ]] && [[ "$(cat "$leasedir/pgid")" == "$child_pid" ]]; then
        ok "lease tracks the real build child's pgid, not the wrapper's pid"
    else
        bad "lease pgid file missing or does not match the child ($child_pid): $(cat "$leasedir/pgid" 2>/dev/null)"
    fi

    kill -9 "$wrapper_pid" 2>/dev/null
    wait "$wrapper_pid" 2>/dev/null
    sleep 0.3
    if kill -0 "$wrapper_pid" 2>/dev/null; then
        bad "wrapper process is still alive after kill -9 (setup failure)"
    else
        ok "wrapper process is confirmed dead"
    fi
    if kill -0 "$child_pid" 2>/dev/null; then
        ok "fake mvnw (build) child is STILL alive — the wrapper's death alone did not stop it"
    else
        bad "fake mvnw child died along with the wrapper (setup failure — cannot test the group-liveness gap)"
    fi

    out3="$(bash -c "source '$repo/scripts/lib/build-lease.sh'; build_lease_acquire service" 2>&1)"
    rc3=$?
    if [[ $rc3 -eq 75 ]]; then
        ok "a second acquire still REFUSES while the tracked build child is alive (rc 75)"
    else
        bad "a second acquire returned rc $rc3 (expected 75 — the still-alive child should have blocked it): $out3"
    fi

    # The stub's own `sleep 30` is a grandchild in the SAME process group
    # (group membership is inherited at fork, unaffected by the group
    # leader's own death) — kill the whole GROUP (negative pid), not just
    # the stub's bare pid, or `sleep 30` lingers for up to 30s and the
    # group still reads as alive, which is exactly what
    # _build_lease_group_alive is designed to catch.
    kill -9 -- "-$child_pid" 2>/dev/null
    for _ in $(seq 1 50); do
        kill -0 -- "-$child_pid" 2>/dev/null || break
        sleep 0.1
    done
    if kill -0 -- "-$child_pid" 2>/dev/null; then
        bad "fake mvnw child's process group never died after kill -9 (setup failure)"
    else
        out4="$(bash -c "source '$repo/scripts/lib/build-lease.sh'; build_lease_acquire service" 2>&1)"
        rc4=$?
        if [[ $rc4 -eq 0 ]]; then
            ok "once the tracked child actually exits, a fresh acquire succeeds"
        else
            bad "acquire after the child exited returned rc $rc4 (expected 0): $out4"
        fi
        if [[ "$out4" == *"WARNING"* ]]; then
            ok "that acquire is disclosed as a stale-reclaim, not silent"
        else
            bad "no WARNING on the post-child-exit reclaim: $out4"
        fi
    fi
fi

# ── Tests 4/5: SIGTERM / SIGINT to the WRAPPER — the round-3 review
# finding. Round 2's fix (Test 3 above) only proved a KILLED wrapper
# leaves the lease correctly HELD. But SIGTERM/SIGINT are catchable: with
# no INT/TERM trap, bash still runs the EXIT trap before terminating —
# which, pre-round-3, released the lease UNCONDITIONALLY even though the
# real build (a background child in its own process group under `set -m`)
# was never touched by that signal and kept running. The fix forwards the
# signal to the child's process group and waits for it to actually die
# before releasing. Verify BOTH halves: the child's group actually dies
# (forwarding worked), AND the lease is actually released afterward (not
# left stale). ─────────────────────────────────────────────────────────
_test_signal_forwarding() {
    local sig="$1" test_num="$2"
    echo "Test $test_num: SIG$sig to the wrapper forwards to the child, then releases cleanly"
    local started="$WORKDIR/stub${test_num}-started.txt"
    rm -f "$started"
    cat > "$repo/service/mvnw" <<STUB
#!/usr/bin/env bash
echo "\$\$" > "$started"
sleep 30
STUB
    chmod +x "$repo/service/mvnw"

    "$repo/scripts/mvnw-leased.sh" build &
    local wrapper_pid=$!

    for _ in $(seq 1 50); do
        [[ -f "$started" ]] && break
        sleep 0.1
    done
    if [[ ! -f "$started" ]]; then
        bad "fake mvnw (sleeping child) never started (setup failure)"
        kill -9 "$wrapper_pid" 2>/dev/null
        wait "$wrapper_pid" 2>/dev/null
        return
    fi
    local child_pid
    child_pid="$(cat "$started")"

    kill -s "$sig" "$wrapper_pid" 2>/dev/null

    for _ in $(seq 1 100); do
        kill -0 "$wrapper_pid" 2>/dev/null || break
        sleep 0.1
    done
    if kill -0 "$wrapper_pid" 2>/dev/null; then
        bad "wrapper never exited after SIG$sig (setup failure)"
        kill -9 "$wrapper_pid" 2>/dev/null
        kill -9 -- "-$child_pid" 2>/dev/null
        wait "$wrapper_pid" 2>/dev/null
        return
    fi
    ok "wrapper exited after SIG$sig"

    if kill -0 -- "-$child_pid" 2>/dev/null; then
        bad "the child's process group is STILL ALIVE after the wrapper exited — the signal was never forwarded (this is exactly the round-3 bug)"
        kill -9 -- "-$child_pid" 2>/dev/null
    else
        ok "SIG$sig was forwarded — the child's process group is dead, not orphaned"
    fi

    if [[ -d "$leasedir" ]]; then
        bad "lease is still held after a clean SIG$sig shutdown — it should have been released once the child actually died"
        # cleanup so later tests aren't polluted
        bash -c "source '$repo/scripts/lib/build-lease.sh'; build_lease_release service" 2>/dev/null
    else
        ok "lease was released once the forwarded shutdown actually completed"
    fi
    wait "$wrapper_pid" 2>/dev/null
}

_test_signal_forwarding TERM 4

echo "Test 5: SIGINT to the wrapper forwards to the child, then releases cleanly"
if _sigint_delivery_works; then
    ok "SIGINT delivery is live in this environment — running the real end-to-end case"
    _test_signal_forwarding INT 5b
else
    skip "OS-level SIGINT delivery is unavailable in this execution environment (a trap installed in THIS shell, self-signaled via 'kill -INT \$\$' in the same command, never fired — reproduced deterministically; bash documents 'signals ignored upon entry to the shell cannot be trapped or reset', and that disposition is inherited by every descendant regardless of set -m or nesting). This is an environment property, not evidence about mvnw-leased.sh's code, so it is not reported as a failure. Falling back to two logic-level checks that do not depend on OS SIGINT delivery reaching the wrapper's own trap:"
    if grep -q "trap '_forward_signal INT' INT" "$repo/scripts/mvnw-leased.sh"; then
        ok "mvnw-leased.sh registers an INT trap wired to the SAME _forward_signal function TERM uses (verified end-to-end by Test 4) — not just TERM"
    else
        bad "mvnw-leased.sh no longer registers 'trap ... INT' at all — SIGINT forwarding is unwired"
    fi
    # Prove the underlying OS primitive _forward_signal relies on for INT
    # specifically (`kill -s INT -- -PGID`) actually terminates a live
    # process group, independent of whether bash's trap dispatch can
    # reach it in this sandbox. `set -m` is required here so THIS
    # background job becomes its own process-group leader (pgid == pid) —
    # without it, a backgrounded job stays in the launching shell's own
    # group and "-$probe_pid" targets a process group that does not exist
    # at all (a setup-failure-shaped false negative, not a real finding
    # about INT vs TERM — this is the last test in the file, so leaving
    # job control on for the remainder is harmless).
    set -m
    # One retry (fork/job-control-table settle time observed to be
    # occasionally slower right after Test 4's own set -m + backgrounding
    # + wait sequence in this SAME shell process — a benign OS-timing
    # race, not a correctness question; 5/5 standalone runs of this exact
    # probe were reliable, one flake was observed only immediately after
    # Test 4 in the same process). A SECOND consecutive failure is treated
    # as real, not retried away.
    attempt=""; died=""
    for attempt in 1 2; do
        sleep 30 &
        probe_pid=$!
        sleep 0.2   # let the new job's process group settle before signaling it
        died=0
        if kill -s INT -- "-$probe_pid" 2>/dev/null; then
            for _ in $(seq 1 20); do
                kill -0 -- "-$probe_pid" 2>/dev/null || { died=1; break; }
                sleep 0.1
            done
        fi
        if [[ $died -eq 1 ]]; then
            ok "the underlying 'kill -s INT -- -PGID' primitive _forward_signal uses correctly terminates a live process group$( [[ $attempt -eq 2 ]] && echo " (attempt 2)" )"
            break
        fi
        kill -9 -- "-$probe_pid" 2>/dev/null
        wait "$probe_pid" 2>/dev/null
        if [[ $attempt -eq 2 ]]; then
            bad "kill -s INT -- -PGID did not terminate a live probe process group on EITHER attempt — the primitive _forward_signal relies on for INT is not sound here"
        fi
    done
    wait "$probe_pid" 2>/dev/null
fi

echo
echo "mvnw-leased_test.sh: $PASS passed, $FAIL failed, $SKIP skipped"
[[ $FAIL -eq 0 ]]
