#!/usr/bin/env bash
# scripts/mvnw-leased_test.sh — shell-level test for mvnw-leased.sh (bead
# nexus-c00dw): proves the wrapper acquires the build lease, execs a
# `./mvnw` stub with args passed through untouched, and releases the lease
# afterward. Self-provisioning: builds a throwaway fake-repo tmpdir with a
# FAKE `service/mvnw` stub — never invokes the real ./mvnw. No pytest, no
# engine substrate. Run directly with bash:
#   bash scripts/mvnw-leased_test.sh
set -u -o pipefail

# The suite builds its own fake repo and assumes the lease root is that repo's
# own. An inherited NX_BUILD_LEASE_ROOT (CI's lease step exports it, and so
# does the documented hand-run setup) would point the lease at the shared
# root: the lease dir is then missing from the fake repo, and the fixture
# leases this suite writes would land in the shared root.
unset NX_BUILD_LEASE_ROOT NX_SUITE_LEASE_WAIT NX_SUITE_LEASE_HELD_BY
# A lab-run grant exports LAB_CPUS, which changes the wrapper's behaviour
# (nexus-iqmih); the cases that want it set it themselves.
unset LAB_CPUS GITHUB_ACTIONS

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/mvnw_leased_test.XXXXXX")"
trap 'rm -rf "$WORKDIR"' EXIT
# Never the real per-user lease-events log (nexus-jqk5c).
export NX_LEASE_EVENTS_LOG="$WORKDIR/lease-events.log"

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
# environment property. TERM is subject to the same rule: build-lease_test.sh's
# Test 4 failed in CI (5588beead) when its suite started with TERM ignored or
# blocked, so that test now resets TERM for its holder. Test 4 here sends TERM
# too: the pytest wrapper (test_shell_suite_wiring.py, via tests/_sigterm_guard.py)
# starts this suite with TERM at its default and unblocked, and tests/conftest.py
# names any test that leaves TERM ignored or blocked (nexus-yqzpw); a direct run
# from a shell that ignores TERM has neither and would fail Test 4.
#
# The subshell must signal ITSELF. `$$` inside `( ... )` is the PARENT
# script's pid, not the subshell's, and until nexus-fd3zf this probe used
# it: the trap in the subshell never fired (so every run took the skip path
# below and the real SIGINT case never ran), and the SIGINT landed on this
# test script instead. The script survived only while bash was already
# waiting on the subshell, which defers a foreground child's SIGINT; under
# `pytest -n auto` load the signal could arrive first and kill the script
# with rc=-2 before its summary line (2026-09-24, full suite). $BASHPID is
# the subshell's own pid; the fallback covers bash 3.2, which lacks it.
_sigint_delivery_works() {
    local marker="$WORKDIR/.sigint-probe-$$"
    rm -f "$marker"
    ( trap 'touch "'"$marker"'"; exit 0' INT
      kill -INT "${BASHPID:-$(exec sh -c 'echo "$PPID"')}"; sleep 0.3 ) >/dev/null 2>&1
    [[ -f "$marker" ]]
}

repo="$WORKDIR/repo"
mkdir -p "$repo/scripts/lib" "$repo/service/target" "$repo/service/.build-lease"
cp "$HERE/lib/build-lease.sh" "$repo/scripts/lib/build-lease.sh"
cp "$HERE/lib/gate-jar-cache.sh" "$repo/scripts/lib/gate-jar-cache.sh"
cp "$HERE/lib/leased-build.sh" "$repo/scripts/lib/leased-build.sh"
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

# ── Test 1a (nexus-k6zxp): a gate-jar cache hit leaves service/target's jar
# hard-linked to the cache entry, and the jar plugin rewrites an existing jar in
# place. A goal that reaches `package` must unlink a shared jar first; one that
# stops short of it must not touch the link.
echo "Test 1a: a package goal unlinks a cache-shared jar; a test goal leaves the link"
entry1a="$WORKDIR/cache-entry-jar"; tjar1a="$repo/service/target/nexus-service-1.0-SNAPSHOT.jar"
echo "cached jar bytes" > "$entry1a"; chmod a-w "$entry1a"
ln "$entry1a" "$tjar1a"
MVNW_STUB_MARKER="$WORKDIR/stub-1a.txt" "$repo/scripts/mvnw-leased.sh" -q test >/dev/null 2>&1
[[ "$tjar1a" -ef "$entry1a" ]] && ok "a test goal leaves the shared jar linked" || bad "a test goal unlinked the jar"
MVNW_STUB_MARKER="$WORKDIR/stub-1a.txt" "$repo/scripts/mvnw-leased.sh" -q package -DskipTests >/dev/null 2>&1
[[ ! -e "$tjar1a" ]] && ok "a package goal unlinked the shared jar before mvnw ran" || bad "shared jar still in service/target after a package goal"
[[ "$(cat "$entry1a")" == "cached jar bytes" ]] && ok "the cache entry is intact" || bad "cache entry changed"
echo "private jar" > "$tjar1a"
MVNW_STUB_MARKER="$WORKDIR/stub-1a.txt" "$repo/scripts/mvnw-leased.sh" -q package >/dev/null 2>&1
[[ -f "$tjar1a" ]] && ok "an unshared writable jar is left for the build to replace" || bad "private jar removed"
# Every goal that reaches the jar: the phases after package, and the plugin
# goals that write it directly, bare or prefixed.
for goal1a in verify install deploy integration-test jar:jar shade:shade \
        org.apache.maven.plugins:maven-jar-plugin:3.4.1:jar \
        org.apache.maven.plugins:maven-shade-plugin:3.6.0:shade; do
    rm -f "$tjar1a"; ln "$entry1a" "$tjar1a"
    MVNW_STUB_MARKER="$WORKDIR/stub-1a.txt" "$repo/scripts/mvnw-leased.sh" -q "$goal1a" >/dev/null 2>&1
    [[ ! -e "$tjar1a" ]] && ok "goal '$goal1a' unlinked the shared jar" || bad "goal '$goal1a' left the shared jar linked"
done
# nexus-8qdpe: one quoted argument naming several goals is split before matching.
for goal1a in "clean package" "  install -DskipTests"; do
    rm -f "$tjar1a"; ln "$entry1a" "$tjar1a"
    MVNW_STUB_MARKER="$WORKDIR/stub-1a.txt" "$repo/scripts/mvnw-leased.sh" -q "$goal1a" >/dev/null 2>&1
    [[ ! -e "$tjar1a" ]] && ok "quoted '$goal1a' unlinked the shared jar" || bad "quoted '$goal1a' left the shared jar linked"
done
for goal1a in compile test test-compile clean "clean test" ""; do
    rm -f "$tjar1a"; ln "$entry1a" "$tjar1a"
    MVNW_STUB_MARKER="$WORKDIR/stub-1a.txt" "$repo/scripts/mvnw-leased.sh" -q "$goal1a" >/dev/null 2>&1
    [[ "$tjar1a" -ef "$entry1a" ]] && ok "goal '$goal1a' leaves the shared jar linked" || bad "goal '$goal1a' unlinked the jar"
done
rm -f "$tjar1a" "$entry1a"

# ── Test 1b: --lab-run <class> takes the lease FIRST, then runs mvnw under
# lab-run (nexus-iqmih). Waiting for the lease inside a lab grant held pool
# cores and memory while a single holder built; outside it, the wait costs
# nothing. The stub lab-run records its args, whether the lease was already
# held when it started, and runs the command it was given.
echo "Test 1b: --lab-run <class> acquires the lease, then runs mvnw under lab-run"
stubbin="$WORKDIR/stubbin"
mkdir -p "$stubbin"
cat > "$stubbin/lab-run" <<STUB
#!/usr/bin/env bash
{
    echo "LABRUN_ARGS=\$*"
    if [[ -f "$leasedir/pid" ]]; then echo "LEASE_HELD_AT_START=yes"; else echo "LEASE_HELD_AT_START=no"; fi
} > "\$LABRUN_STUB_MARKER"
[[ "\$1" == "-c" && "\$3" == "--" ]] || exit 64
shift 3
exec "\$@"
STUB
chmod +x "$stubbin/lab-run"
marker1b="$WORKDIR/stub-invocation-1b.txt"
labmarker="$WORKDIR/labrun-invocation.txt"
rm -f "$marker1b" "$labmarker"
PATH="$stubbin:$PATH" LABRUN_STUB_MARKER="$labmarker" MVNW_STUB_MARKER="$marker1b" \
    "$repo/scripts/mvnw-leased.sh" --lab-run lab-m -q test -Dtest=Foo
rc1b=$?
if [[ $rc1b -eq 0 ]]; then ok "wrapper exited 0 under --lab-run"; else bad "wrapper exited $rc1b under --lab-run"; fi
labcontent="$(cat "$labmarker" 2>/dev/null)"
if [[ "$labcontent" == "LABRUN_ARGS=-c lab-m -- python3 -c "*" ./mvnw -q test -Dtest=Foo"* ]]; then
    ok "lab-run got the class and ./mvnw with the args untouched"
else
    bad "lab-run args mismatch: ${labcontent:-<lab-run never invoked>}"
fi
if [[ "$labcontent" == *"LEASE_HELD_AT_START=yes"* ]]; then
    ok "the lease was already held when lab-run started (lease before grant)"
else
    bad "lab-run started without the lease held: $labcontent"
fi
if [[ "$(cat "$marker1b" 2>/dev/null)" == *"ARGS=-q test -Dtest=Foo"* ]]; then
    ok "mvnw ran inside lab-run with the args untouched"
else
    bad "mvnw was not run inside lab-run"
fi
if [[ ! -d "$leasedir" ]]; then ok "lease released after the --lab-run build"; else bad "lease still held after the --lab-run build"; fi

echo "Test 1c: --lab-run with no lab-run on PATH fails loud and takes no lease"
marker1c="$WORKDIR/stub-invocation-1c.txt"
rm -f "$marker1c"
out1c="$(PATH="/usr/bin:/bin" MVNW_STUB_MARKER="$marker1c" "$repo/scripts/mvnw-leased.sh" --lab-run lab-m -q test 2>&1)"
rc1c=$?
if [[ $rc1c -eq 127 ]]; then ok "rc 127 when lab-run is missing"; else bad "rc $rc1c (expected 127): $out1c"; fi
if [[ ! -f "$marker1c" && ! -d "$leasedir" ]]; then ok "no mvnw run, no lease left"; else bad "mvnw ran or the lease was left held"; fi

echo "Test 1d: --lab-run without a class is a usage error"
out1d="$(MVNW_STUB_MARKER="$WORKDIR/unused" "$repo/scripts/mvnw-leased.sh" --lab-run 2>&1)"
rc1d=$?
if [[ $rc1d -eq 64 ]]; then ok "rc 64 for --lab-run with no class"; else bad "rc $rc1d (expected 64): $out1d"; fi

echo "Test 1e: --lab-run inside a lab-run grant is refused (no nested grant)"
marker1e="$WORKDIR/stub-invocation-1e.txt"
rm -f "$marker1e"
out1e="$(LAB_CPUS=8 PATH="$stubbin:$PATH" LABRUN_STUB_MARKER="$WORKDIR/unused-lab" MVNW_STUB_MARKER="$marker1e" "$repo/scripts/mvnw-leased.sh" --lab-run lab-m -q test 2>&1)"
rc1e=$?
if [[ $rc1e -eq 64 && "$out1e" == *"inside a lab-run grant"* ]]; then ok "rc 64 naming the nested grant"; else bad "rc $rc1e (expected 64): $out1e"; fi
if [[ ! -f "$marker1e" && ! -d "$leasedir" ]]; then ok "no mvnw run, no lease left"; else bad "mvnw ran or the lease was left held"; fi

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
    # NX_BUILD_LEASE_WAIT=0: the wrapper WAITS for a live holder by default
    # (nexus-g6xpa); 0 is the single-attempt, refuse-immediately contract
    # this test pins.
    out2="$(NX_BUILD_LEASE_WAIT=0 MVNW_STUB_MARKER="$marker2" "$repo/scripts/mvnw-leased.sh" -q package 2>&1)"
    rc2=$?
    if [[ $rc2 -eq 75 ]]; then ok "wrapper refused with rc 75"; else bad "wrapper returned rc $rc2 (expected 75): $out2"; fi
    if [[ ! -f "$marker2" ]]; then ok "fake mvnw was never invoked while lease was held"; else bad "fake mvnw ran despite the lease being held"; fi
    # nexus-dfg0q (Sam 2026-10-19): a hand run inside a lab-run grant (LAB_CPUS
    # set, not GitHub Actions) REFUSES at once instead of waiting, whatever
    # NX_BUILD_LEASE_WAIT says: waiting there holds the grant's cores.
    t0=$SECONDS
    out2w="$(LAB_CPUS=8 NX_BUILD_LEASE_WAIT=60 MVNW_STUB_MARKER="$marker2" "$repo/scripts/mvnw-leased.sh" -q package 2>&1)"
    rc2w=$?
    if [[ $rc2w -eq 75 && $((SECONDS - t0)) -lt 15 ]]; then ok "inside a grant, a held lease refuses at once with rc 75"; else bad "inside a grant: rc $rc2w after $((SECONDS - t0)) s (expected 75 at once): $out2w"; fi
    if [[ "$out2w" == *"--lab-run <class>"* ]]; then ok "the refusal names the --lab-run form"; else bad "refusal does not name --lab-run: $out2w"; fi
    if [[ ! -f "$marker2" ]]; then ok "no mvnw run inside the grant"; else bad "mvnw ran inside the grant"; fi
    if [[ "$out2" != *"inside a lab-run grant"* ]]; then ok "outside a grant, no grant wording"; else bad "grant wording outside a grant: $out2"; fi
    # A CI job on the pool also has LAB_CPUS, and waiting its turn there is the
    # scheduler's own serialisation: GitHub Actions keeps the configured wait.
    out2c="$(GITHUB_ACTIONS=true LAB_CPUS=8 NX_BUILD_LEASE_WAIT=0 MVNW_STUB_MARKER="$marker2" "$repo/scripts/mvnw-leased.sh" -q package 2>&1)"
    if [[ "$out2c" != *"inside a lab-run grant"* ]]; then ok "a CI job inside a grant keeps the plain wait semantics"; else bad "CI job got the hand-run refusal: $out2c"; fi
fi
kill -9 "$holder_pid" 2>/dev/null
wait "$holder_pid" 2>/dev/null

# ── Test 3: SIGKILLing just the wrapper does NOT free the lease while its
# real build child (tracked via build_lease_track_pid / process group) is

# ── Test 2b: the wrapper WAITS for a live holder (nexus-g6xpa) ────────────
echo "Test 2b: mvnw-leased.sh waits out a live holder, then runs mvnw"
rm -rf "$leasedir"   # Test 2's holder was killed -9 and left a stale lease behind
bash -c "source '$repo/scripts/lib/build-lease.sh'; build_lease_acquire service || exit 9; trap 'build_lease_release service' EXIT; sleep 6" &
holder2b=$!
for _ in $(seq 1 50); do
    [[ "$(cat "$leasedir/pid" 2>/dev/null)" == "$holder2b" ]] && break
    sleep 0.1
done
marker2b="$WORKDIR/marker2b"
start2b=$SECONDS
out2b="$(NX_BUILD_LEASE_WAIT=60 MVNW_STUB_MARKER="$marker2b" "$repo/scripts/mvnw-leased.sh" -q package 2>&1)"
rc2b=$?
took2b=$((SECONDS - start2b))
if [[ $rc2b -eq 0 ]]; then ok "wrapper ran after the holder released (rc 0, ${took2b}s)"; else bad "wrapper rc $rc2b: $out2b"; fi
if [[ -f "$marker2b" ]]; then ok "mvnw stub ran after the wait"; else bad "mvnw stub never ran"; fi
if [[ "$out2b" == *"waiting"* ]]; then ok "the wait was announced"; else bad "no waiting line: $out2b"; fi
if (( took2b >= 3 )); then ok "it waited for the holder (${took2b}s)"; else bad "returned in ${took2b}s — did not wait"; fi
wait "$holder2b" 2>/dev/null

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

    # Job control on for the launch: without it bash starts an async command
    # with SIGINT and SIGQUIT IGNORED, and a signal ignored on entry cannot be
    # trapped, so the wrapper's INT trap would never be installed and Test 5b
    # would prove nothing about it. An interactive Ctrl-C reaches a foreground
    # wrapper with the default disposition, which is what this reproduces.
    set -m
    "$repo/scripts/mvnw-leased.sh" build &
    local wrapper_pid=$!
    set +m

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
    # The traps live in scripts/lib/leased-build.sh since nexus-t6t9f; mvnw-leased.sh installs them
    # by calling leased_build_init.
    if grep -q "trap '_leased_build_forward_signal INT' INT" "$repo/scripts/lib/leased-build.sh" &&
        grep -q "trap '_leased_build_forward_signal TERM' TERM" "$repo/scripts/lib/leased-build.sh" &&
        grep -q '^leased_build_init mvnw-leased.sh service' "$repo/scripts/mvnw-leased.sh"; then
        ok "mvnw-leased.sh installs (via leased_build_init) an INT trap wired to the SAME forward function TERM uses (verified end-to-end by Test 4) — not just TERM"
    else
        bad "the INT trap is no longer installed by leased_build_init, or mvnw-leased.sh no longer calls it — SIGINT forwarding is unwired"
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
    # nexus-poqc6: the probe's child must not inherit an IGNORED SIGINT. A job launched in the
    # background of a non-interactive shell (`( cmd & wait )`, how full suites run on hellmini)
    # starts with SIGINT ignored, and bash cannot reset a signal that was ignored at shell entry
    # (so `trap - INT` in a bash subshell does nothing). Ignored dispositions survive exec, so a
    # plain `sleep 30 &` here could never be terminated by `kill -s INT` and the check failed on
    # a property of how the suite was launched. exec the sleep from an interpreter that sets the
    # disposition to the default first: the probe then tests the primitive, not the launch shape.
    _sleep_with_default_sigint() {
        # perl first: its startup is a few ms against python3's tens, and the window between process
        # start and the disposition reset must stay well inside the 0.2 s settle below.
        if command -v perl >/dev/null 2>&1; then
            perl -e '$SIG{INT} = "DEFAULT"; exec "sleep", "30"' &
        elif command -v python3 >/dev/null 2>&1; then
            python3 -c 'import os, signal; signal.signal(signal.SIGINT, signal.SIG_DFL); os.execvp("sleep", ["sleep", "30"])' &
        else
            sleep 30 &
        fi
    }
    for attempt in 1 2; do
        _sleep_with_default_sigint
        probe_pid=$!
        # Poll until the job has exec'd its sleep: until then SIGINT is still ignored (the
        # interpreter has not reset it), so a signal sent during a slow interpreter start on a
        # loaded host is dropped and the probe reads as "did not terminate" (nexus-ys8a4). Bounded
        # at 10 s; on timeout we signal anyway and the existing retry/verdict logic decides.
        for _ in $(seq 1 100); do
            probe_comm="$(ps -o comm= -p "$probe_pid" 2>/dev/null)"
            [[ "${probe_comm##*/}" == "sleep" ]] && break
            sleep 0.1
        done
        sleep 0.2   # let the new job's process group settle before signaling it
        died=0
        if kill -s INT -- "-$probe_pid" 2>/dev/null; then
            for _ in $(seq 1 100); do
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

# ── Test 4b: SIGTERM under --lab-run when lab-run does NOT forward it
# (nexus-iqmih review). On chas lab-run ends in sudo and a systemd scope; the
# build runs in its own process group, and a TERM that kills sudo need not
# reach it. The stub lab-run below dies on TERM and leaves its command
# running, the worst case. The wrapper must signal the build's own group and
# hold the lease until that group is gone.
echo "Test 4b: SIGTERM under --lab-run reaches the build even when lab-run does not forward it"
cat > "$stubbin/lab-run" <<'STUB'
#!/usr/bin/env bash
[[ "$1" == "-c" && "$3" == "--" ]] || exit 64
shift 3
trap 'exit 143' TERM
# Its own process group for the command, as sudo with a pty gives it.
set -m
"$@" &
set +m
wait $!
STUB
chmod +x "$stubbin/lab-run"
started4b="$WORKDIR/stub4b-started.txt"
rm -f "$started4b"
cat > "$repo/service/mvnw" <<STUB
#!/usr/bin/env bash
echo "\$\$" > "$started4b"
sleep 30
STUB
chmod +x "$repo/service/mvnw"
set -m
PATH="$stubbin:$PATH" "$repo/scripts/mvnw-leased.sh" --lab-run lab-s build &
wrapper4b=$!
set +m
for _ in $(seq 1 100); do [[ -f "$started4b" ]] && break; sleep 0.1; done
if [[ ! -f "$started4b" ]]; then
    bad "fake mvnw never started under the stub lab-run (setup failure)"
    kill -9 "$wrapper4b" 2>/dev/null
else
    build4b="$(cat "$started4b")"
    # Let the wrapper's poll pick up the build's group before signalling.
    for _ in $(seq 1 30); do [[ "$(cat "$leasedir/pgid" 2>/dev/null)" == "$build4b" ]] && break; sleep 0.1; done
    if [[ "$(cat "$leasedir/pgid" 2>/dev/null)" == "$build4b" ]]; then
        ok "the lease's pgid names the build's own group, not lab-run's"
    else
        bad "the lease's pgid is $(cat "$leasedir/pgid" 2>/dev/null), not the build's group $build4b"
    fi
    kill -s TERM "$wrapper4b" 2>/dev/null
    released_while_alive=0
    for _ in $(seq 1 400); do
        if [[ ! -d "$leasedir" ]] && kill -0 -- "-$build4b" 2>/dev/null; then released_while_alive=1; fi
        kill -0 "$wrapper4b" 2>/dev/null || break
        sleep 0.1
    done
    if kill -0 -- "-$build4b" 2>/dev/null; then
        bad "the build's group is still alive after the wrapper exited: TERM never reached it"
        kill -9 -- "-$build4b" 2>/dev/null
    else
        ok "TERM reached the build's own group"
    fi
    if [[ $released_while_alive -eq 0 && ! -d "$leasedir" ]]; then
        ok "the lease was released only after the build's group was gone"
    else
        bad "lease released while the build ran ($released_while_alive) or still held after"
        bash -c "source '$repo/scripts/lib/build-lease.sh'; build_lease_release service" 2>/dev/null
    fi
fi
wait "$wrapper4b" 2>/dev/null

# ── Tests 4c/4d (nexus-dfg0q, from the iqmih confirmation): under --lab-run a
# non-zero build exit propagates, and a TERM while lab-run is still queued for
# its grant releases the lease without ever starting the build.
echo "Test 4c: a non-zero mvnw exit propagates under --lab-run"
cat > "$stubbin/lab-run" <<'STUB'
#!/usr/bin/env bash
[[ "$1" == "-c" && "$3" == "--" ]] || exit 64
shift 3
exec "$@"
STUB
chmod +x "$stubbin/lab-run"
cat > "$repo/service/mvnw" <<'STUB'
#!/usr/bin/env bash
exit 7
STUB
chmod +x "$repo/service/mvnw"
PATH="$stubbin:$PATH" "$repo/scripts/mvnw-leased.sh" --lab-run lab-s build >/dev/null 2>&1
rc4c=$?
if [[ $rc4c -eq 7 ]]; then ok "exit 7 from mvnw comes back as 7"; else bad "rc $rc4c (expected 7)"; fi
if [[ ! -d "$leasedir" ]]; then ok "lease released after the failed build"; else bad "lease left held after a failed build"; fi

echo "Test 4d: TERM while lab-run is queued releases the lease and never starts the build"
queued="$WORKDIR/stub4d-queued.txt"; started4d="$WORKDIR/stub4d-started.txt"
rm -f "$queued" "$started4d"
cat > "$stubbin/lab-run" <<STUB
#!/usr/bin/env bash
[[ "\$1" == "-c" && "\$3" == "--" ]] || exit 64
shift 3
touch "$queued"
sleep 30    # queued for a grant
exec "\$@"
STUB
chmod +x "$stubbin/lab-run"
cat > "$repo/service/mvnw" <<STUB
#!/usr/bin/env bash
touch "$started4d"
STUB
chmod +x "$repo/service/mvnw"
set -m
PATH="$stubbin:$PATH" "$repo/scripts/mvnw-leased.sh" --lab-run lab-s build >/dev/null 2>&1 &
wrapper4d=$!
set +m
for _ in $(seq 1 100); do [[ -f "$queued" ]] && break; sleep 0.1; done
kill -s TERM "$wrapper4d" 2>/dev/null
for _ in $(seq 1 200); do kill -0 "$wrapper4d" 2>/dev/null || break; sleep 0.1; done
if kill -0 "$wrapper4d" 2>/dev/null; then bad "wrapper still alive 20 s after TERM while queued"; kill -9 "$wrapper4d" 2>/dev/null; else ok "wrapper exited on TERM while queued"; fi
sleep 0.5
if [[ ! -f "$started4d" ]]; then ok "the build never started"; else bad "the build started after TERM"; fi
if [[ ! -d "$leasedir" ]]; then ok "lease released"; else bad "lease left held after TERM while queued"; bash -c "source '$repo/scripts/lib/build-lease.sh'; build_lease_release service" 2>/dev/null; fi
wait "$wrapper4d" 2>/dev/null

echo
echo "mvnw-leased_test.sh: $PASS passed, $FAIL failed, $SKIP skipped"
[[ $FAIL -eq 0 ]]
