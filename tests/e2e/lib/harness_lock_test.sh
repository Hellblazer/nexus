#!/usr/bin/env bash
# tests/e2e/lib/harness_lock_test.sh — concurrent-invocation regression test
# for the 4 e2e harnesses guarded by lock.sh (RDR-184 P0.2, nexus-ccs9v.2).
#
# For each of the 4 harnesses this proves, WITHOUT ever running the harness's
# real body (no docker, no native build, no `uv tool install`, no
# `rm -rf $SANDBOX` for real):
#
#   1. Wiring (non-vacuity): the harness script still contains its
#      `lock_acquire "$LOCKDIR"` call. A harness that silently lost its
#      wiring (a bad merge, a copy-paste refactor) must FAIL this test, not
#      silently skip it — this is the max-skip/non-vacuity guard the repo's
#      gate convention requires.
#   2. Simulated holder: this test script acquires the harness's OWN lockdir
#      directly via lock.sh, simulating a currently-running instance.
#   3. Blocked invocation: with the lock held, invoking the REAL harness
#      script must exit nonzero in well under 1s, print the lock's loud
#      failure message (naming the lockdir), and never print the harness's
#      own "lock acquired" line (proof it never got past the lock, hence
#      never reached any docker/build/rm-rf work).
#   4. Past-the-lock invocation: after releasing the simulated holder, the
#      REAL harness script is invoked again with NX_E2E_LOCK_SELFTEST=1 set
#      — a test seam each harness checks immediately after its own
#      `lock_acquire` call, printing the "lock acquired" line and exiting 0
#      right there. This proves re-acquisition through the ACTUAL script
#      (arg parsing, validation guards, the lock_acquire call itself, the
#      EXIT trap) without ever letting the heavy body run — no killing, no
#      process-tree races, fully deterministic.
#
# Self-provisioning: builds its own throwaway lockdir under the same
# machine-global lock root the harnesses use (so a stale run does not wedge
# a real harness invocation), no ambient state, no dependency on docker/PG/
# any other harness. Run directly with bash:
#   bash tests/e2e/lib/harness_lock_test.sh
set -u -o pipefail

# RDR-184 P0 review M3: `declare -A` (below) is bash 4.0+ only and does
# not exist on stock macOS /bin/bash 3.2 (the OS-shipped default on this
# repo's own stated primary dev platform) — fail loud with a clear
# message rather than a bare parse error if invoked under an old bash.
if ((BASH_VERSINFO[0] < 4)); then
    echo "harness_lock_test.sh: requires bash >= 4 (found ${BASH_VERSION}); on macOS, run via Homebrew bash (e.g. /opt/homebrew/bin/bash), not the OS-shipped /bin/bash 3.2" >&2
    exit 1
fi

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../../.." && pwd)"
# shellcheck source=./lock.sh disable=SC1091
source "$HERE/lock.sh"

PASS=0
FAIL=0
ok()  { echo "  [ok] $1"; PASS=$((PASS + 1)); }
bad() { echo "  [FAIL] $1"; FAIL=$((FAIL + 1)); }

# name -> script path (repo-root relative). `sandbox` and
# `t2-migration-sqlite` added RDR-184 P0 review guard-surface gap fix
# (nexus-ccs9v.4/.5): both were unguarded fixed-shared-resource scripts
# the original Phase-0 audit missed.
declare -A HARNESS_SCRIPT=(
    [migration-rehearsal]="tests/e2e/migration-rehearsal/run.sh"
    [gc-ab]="tests/e2e/gc-ab/run-ab.sh"
    [release-sandbox]="tests/e2e/release-sandbox.sh"
    [upgrade-shakeout]="tests/e2e/upgrade-shakeout.sh"
    [sandbox]="tests/e2e/sandbox.sh"
    [t2-migration-sqlite]="tests/e2e/t2-migration-sqlite/run.sh"
)
# name -> cheap, side-effect-free positional args (empty for the
# no-argument scripts; a real, valid, non-mutating mode for the
# mode-dispatch scripts so they never hit an early "unknown mode"/"--help"
# path that would exit BEFORE reaching lock_acquire, which would make the
# test vacuous rather than exercising the lock).
declare -A HARNESS_ARGS=(
    [migration-rehearsal]=""
    [gc-ab]=""
    [release-sandbox]="reset"
    [upgrade-shakeout]="reset"
    [sandbox]=""
    [t2-migration-sqlite]=""
)
# Per-harness lockdir NAME override — defaults to "$LOCKROOT/$name.lock"
# when a name has no entry here. `sandbox` deliberately shares
# release-sandbox's lockdir name: sandbox.sh mutates the IDENTICAL fixed
# resource ($HOME/nexus-sandbox) as release-sandbox.sh, so it is the SAME
# lock, not a new one (RDR-184 P0 review guard-surface gap). Using this
# override in the loop below means the loop's "(2) simulate a running
# holder" step for name=sandbox acquires the literal
# release-sandbox.lock lockdir — i.e. this exercises the actual
# cross-script contention (sandbox.sh blocked by release-sandbox's own
# lock), not a lookalike.
declare -A HARNESS_LOCKDIR=(
    [sandbox]="release-sandbox"
)

# Machine-global lock root the harnesses themselves use (must match — this
# test exercises the REAL lockdir path each harness computes, not a
# lookalike). HARD-CODED /tmp, not ${TMPDIR:-/tmp} (code-review SIGNIFICANT
# fix, mirrors the same fix in all 4 harnesses): a per-context TMPDIR
# divergence would make this test compute a DIFFERENT lockdir than the
# harness itself, silently validating nothing.
LOCKROOT="/tmp/nexus-e2e-locks"

for name in migration-rehearsal gc-ab release-sandbox upgrade-shakeout sandbox t2-migration-sqlite; do
    echo
    echo "=== $name ==="
    script="${HARNESS_SCRIPT[$name]}"
    args="${HARNESS_ARGS[$name]}"
    lock_name="${HARNESS_LOCKDIR[$name]:-$name}"
    lockdir="$LOCKROOT/$lock_name.lock"

    # ── non-vacuity: wiring assertion ────────────────────────────────────
    # shellcheck disable=SC2016 # intentional literal — grepping for the literal source line, not expanding it
    if grep -qF 'lock_acquire "$LOCKDIR" || exit 1' "$REPO_ROOT/$script"; then
        ok "wiring: $script calls lock_acquire"
    else
        bad "wiring: $script has NO lock_acquire call — silently unwired (non-vacuity guard tripped)"
        continue # nothing further to test for an unwired harness
    fi

    # Start from a clean slate — a leftover lockdir from a previous aborted
    # test run must not be mistaken for a live holder.
    rm -rf "$lockdir"
    mkdir -p "$LOCKROOT"

    # ── (2) simulate a running holder ────────────────────────────────────
    if lock_acquire "$lockdir" >/dev/null 2>&1; then
        ok "simulated holder acquired $lockdir"
    else
        bad "test setup: could not acquire $lockdir as simulated holder"
        continue
    fi

    # ── (3) blocked invocation: real harness, lock held ──────────────────
    t0=$(date +%s%N)
    # shellcheck disable=SC2086 # $args is a single trusted literal per harness, intentionally unquoted for the (possibly-empty) split
    out="$(cd "$REPO_ROOT" && bash "$script" $args 2>&1)"
    rc=$?
    t1=$(date +%s%N)
    elapsed_ms=$(( (t1 - t0) / 1000000 ))

    if [[ $rc -ne 0 ]]; then
        ok "$name: blocked invocation exited nonzero ($rc)"
    else
        bad "$name: blocked invocation exited 0 — should have failed on the held lock"
    fi
    if echo "$out" | grep -q "FAILED to acquire"; then
        ok "$name: failure message names the lock"
    else
        bad "$name: no lock-failure message in output: $out"
    fi
    if ((elapsed_ms < 1000)); then
        ok "$name: blocked invocation resolved in ${elapsed_ms}ms (<1000ms — never ran the harness body)"
    else
        bad "$name: blocked invocation took ${elapsed_ms}ms (>=1000ms — looks like real work ran before failing)"
    fi
    if echo "$out" | grep -q "lock acquired"; then
        bad "$name: 'lock acquired' appeared while the lock was HELD — got past a lock it should not have"
    else
        ok "$name: never got past the held lock (no 'lock acquired' in output)"
    fi

    # Release the simulated holder — the harness's own lock is now free.
    lock_release "$lockdir" 2>/dev/null || true

    # ── (4) past-the-lock invocation: real harness, lock free, self-test
    #     seam stops it immediately after lock_acquire succeeds ───────────
    # shellcheck disable=SC2086
    out2="$(cd "$REPO_ROOT" && NX_E2E_LOCK_SELFTEST=1 bash "$script" $args 2>&1)"
    rc2=$?
    if [[ $rc2 -eq 0 ]]; then
        ok "$name: past-the-lock invocation exited 0 (self-test seam fired)"
    else
        bad "$name: past-the-lock invocation exited $rc2 (expected 0 — did the self-test seam get skipped?): $out2"
    fi
    if echo "$out2" | grep -q "lock acquired"; then
        ok "$name: re-invocation acquired the (now-free) lock and printed the acquire line"
    else
        bad "$name: re-invocation never got past the lock: $out2"
    fi
    # The lock must be released again afterward (the harness's own EXIT trap
    # firing on the self-test seam's `exit 0`) — not left held.
    if [[ -d "$lockdir" ]]; then
        bad "$name: lockdir still present after past-the-lock invocation exited — EXIT trap did not release it"
        rm -rf "$lockdir"
    else
        ok "$name: lockdir released after past-the-lock invocation (EXIT trap fired)"
    fi

    rm -rf "$lockdir"
done

# ── migration-rehearsal arg-conflict region regression ───────────────────
# Code-review CRITICAL finding: the FIRST trap (originally installed at
# ~line 128, well before LOCKDIR is assigned at ~line 183 and lib/lock.sh
# is sourced) referenced $LOCKDIR. Under `set -u`, any of the 12
# argument-conflict guards between those two points (e.g. --cold + --guided
# together) fires that trap on `exit 2`, and the trap's OWN evaluation
# aborts on the unbound $LOCKDIR before the documented exit 2 / conflict
# message ever reaches the caller — silently downgrading a clean usage
# error into a confusing "unbound variable" crash (exit 1). This is
# deliberately OUTSIDE the per-harness loop above (that loop only exercises
# the post-lock-acquire region) — it targets the pre-lock region
# specifically, no lock held, no SELFTEST var, real invocation.
echo
echo "=== migration-rehearsal: arg-conflict region (pre-lock-acquire guards) ==="
out3="$(cd "$REPO_ROOT" && bash tests/e2e/migration-rehearsal/run.sh --cold --guided 2>&1)"
rc3=$?
if [[ $rc3 -eq 2 ]]; then
    ok "migration-rehearsal --cold --guided: exits exactly 2"
else
    bad "migration-rehearsal --cold --guided: exited $rc3 (expected 2): $out3"
fi
if echo "$out3" | grep -q "different flows; pick one"; then
    ok "migration-rehearsal --cold --guided: conflict message present"
else
    bad "migration-rehearsal --cold --guided: conflict message missing: $out3"
fi
if echo "$out3" | grep -qi "unbound variable"; then
    bad "migration-rehearsal --cold --guided: 'unbound variable' leaked on stderr (a pre-lock trap referenced \$LOCKDIR before assignment): $out3"
else
    ok "migration-rehearsal --cold --guided: no unbound-variable leak"
fi

# ── upgrade-shakeout no-args regression (help/usage region, pre-lock) ────
# Code-review CRITICAL-2 finding: this harness's lock_acquire originally ran
# BEFORE its `"$0" --help` self-reinvocation (reached whenever MODE != run,
# including the DEFAULT bare no-arg invocation, since MODE defaults to
# "help"). The child re-entered the script and contended against its own
# parent's still-held lock; under `set -e` that failure aborted the parent
# before it reached `exit 0`, so `./upgrade-shakeout.sh` with no args at all
# exited 1 with a lock-contention error instead of printing help. Wrapped in
# `timeout` as a permanent safety net: the underlying `"$0" --help` pattern
# was ALSO capable of unbounded recursion once lock contention was removed
# from the picture (verified live during this fix — a bare invocation of the
# unfixed intermediate state spawned 100+ processes before being force-
# killed) — a future regression reintroducing either bug must not be able
# to fork-bomb the machine running this test.
echo
echo "=== upgrade-shakeout: no-args region (pre-lock-acquire help dispatch) ==="
out4="$(cd "$REPO_ROOT" && timeout 10 bash tests/e2e/upgrade-shakeout.sh 2>&1)"
rc4=$?
if [[ $rc4 -eq 0 ]]; then
    ok "upgrade-shakeout (no args): exits 0"
elif [[ $rc4 -eq 124 ]]; then
    bad "upgrade-shakeout (no args): TIMED OUT (10s) — looks like unbounded recursion regressed"
else
    bad "upgrade-shakeout (no args): exited $rc4 (expected 0): $out4"
fi
if echo "$out4" | grep -q "^Usage:"; then
    ok "upgrade-shakeout (no args): usage text present"
else
    bad "upgrade-shakeout (no args): usage text missing: $out4"
fi
if echo "$out4" | grep -qiE "unbound variable|FAILED to acquire"; then
    bad "upgrade-shakeout (no args): lock-contention/unbound-variable noise leaked: $out4"
else
    ok "upgrade-shakeout (no args): no lock-contention/unbound-variable noise"
fi
# Belt-and-suspenders: if the timeout ever fires, make sure nothing was left
# running (timeout only signals the direct child, not a whole recursive
# process chain under it).
pgrep -f "upgrade-shakeout.sh" >/dev/null 2>&1 && pkill -9 -f "upgrade-shakeout.sh" 2>/dev/null || true

# ── release-sandbox --help / unknown-mode region (pre-lock, already-correct
#    ordering — coverage addition, not a fix) ────────────────────────────
# release-sandbox.sh's help dispatch (--help option, and the MODE==help
# check) and its unknown-mode _die both print/report IN-PROCESS (no
# subprocess re-invocation like upgrade-shakeout's original `"$0" --help`),
# so this harness never had CRITICAL-2's failure mode. This block is
# coverage, confirming that stays true, not a red-then-green fix.
echo
echo "=== release-sandbox: --help / unknown-mode region ==="
out5="$(cd "$REPO_ROOT" && bash tests/e2e/release-sandbox.sh --help 2>&1)"
rc5=$?
if [[ $rc5 -eq 0 ]]; then
    ok "release-sandbox --help: exits 0"
else
    bad "release-sandbox --help: exited $rc5 (expected 0): $out5"
fi
if echo "$out5" | grep -q "^Usage:"; then
    ok "release-sandbox --help: usage text present"
else
    bad "release-sandbox --help: usage text missing: $out5"
fi
out6="$(cd "$REPO_ROOT" && bash tests/e2e/release-sandbox.sh bogus-mode-lock-test 2>&1)"
rc6=$?
if [[ $rc6 -ne 0 ]]; then
    ok "release-sandbox bogus-mode: exits nonzero ($rc6)"
else
    bad "release-sandbox bogus-mode: exited 0 (expected nonzero)"
fi
if echo "$out6" | grep -q "unknown mode"; then
    ok "release-sandbox bogus-mode: unknown-mode message present"
else
    bad "release-sandbox bogus-mode: unknown-mode message missing: $out6"
fi
if echo "$out6" | grep -qi "unbound variable"; then
    bad "release-sandbox bogus-mode: 'unbound variable' leaked: $out6"
else
    ok "release-sandbox bogus-mode: no unbound-variable leak"
fi
rm -rf "/tmp/nexus-e2e-locks/release-sandbox.lock"

# ── gc-ab: no early-exit/validation-guard region exists ──────────────────
# gc-ab/run-ab.sh takes no CLI arguments at all — a single linear path from
# top to bottom, no arg-parsing while loop, no mode dispatch, nothing that
# could exit before reaching lock_acquire. The wiring assertion in the main
# per-harness loop above (the non-vacuity grep for its lock_acquire call)
# is the only coverage this harness's shape admits; no separate early-exit
# region test applies here.

echo
echo "harness_lock_test.sh: ${PASS} passed, ${FAIL} failed"
[[ "$FAIL" -eq 0 ]]
