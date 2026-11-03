#!/usr/bin/env bash
# tests/e2e/lib/harness_lock_test.sh — concurrent-invocation regression test
# for the e2e harnesses guarded by lock.sh (RDR-184 P0.2, nexus-ccs9v.2).
#
# For each harness this proves, WITHOUT ever running the harness's
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
# migration-rehearsal/run.sh derives PREV_RELEASE by walking published v*
# tags (git show vX:src/nexus/engine_version.py) BEFORE it reaches the lock
# this suite exercises. CI's pytest jobs check out at depth 1 with no tags, so
# the derivation there aborts with "cannot derive PREV_RELEASE" and every
# migration-rehearsal case fails for a reason unrelated to locking (7.45.0
# release PR #1543, 2026-09-14). Pin the pair; the values only need to
# parse, nothing here converges an engine.
export NEXUS_PREV_RELEASE="${NEXUS_PREV_RELEASE:-1.0.0}"
export NEXUS_PREV_ENGINE_TAG="${NEXUS_PREV_ENGINE_TAG:-engine-service-v0.0.1}"
ok()  { echo "  [ok] $1"; PASS=$((PASS + 1)); }
bad() { echo "  [FAIL] $1"; FAIL=$((FAIL + 1)); }

# name -> script path (repo-root relative). `sandbox` added RDR-184 P0
# review guard-surface gap fix (nexus-ccs9v.4): an unguarded fixed-shared-
# resource script the original Phase-0 audit missed. `t2-migration-sqlite`
# was added alongside it at nexus-ccs9v.5 but the harness itself was
# deleted outright at e3c00252a (RDR-158 P3, the SQLite opt-out backend
# retirement) — there is no SQLite side to guard any more, so the entry is
# removed rather than repointed (nexus-fcjt7 round 2: this table's own
# non-vacuity check had been silently tripping on the stale reference).
declare -A HARNESS_SCRIPT=(
    [migration-rehearsal]="tests/e2e/migration-rehearsal/run.sh"
    [gc-ab]="tests/e2e/gc-ab/run-ab.sh"
    [sandbox]="tests/e2e/sandbox.sh"
)
# name -> cheap, side-effect-free positional args (empty for the
# no-argument scripts; a real, valid, non-mutating mode for the
# mode-dispatch scripts so they never hit an early "unknown mode"/"--help"
# path that would exit BEFORE reaching lock_acquire, which would make the
# test vacuous rather than exercising the lock).
declare -A HARNESS_ARGS=(
    [migration-rehearsal]="--package-upgrade"
    [gc-ab]=""
    [sandbox]=""
)
# Lock roots (nexus-xzwyo). The harnesses take their lockdir from lock.sh's e2e_lock_root: the
# machine-global per-user /tmp/nexus-e2e-locks-<uid> by default, NX_E2E_LOCK_ROOT when set. This test
# never touches that default root: every harness run below sets NX_E2E_LOCK_ROOT to a mktemp
# directory of this run's own, so an overlapping run (or a real harness holding sandbox.lock) neither
# collides with it nor loses its lock to it. The default is exercised only in the per-user section
# below, under random fake uids that no real user has. HARD-CODED /tmp stays in lock.sh, not
# ${TMPDIR:-/tmp} (a per-context TMPDIR would give two contexts different lockdirs).
unset NX_E2E_LOCK_ROOT
TEST_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/harness_lock_test.XXXXXX")"
# An empty root would make rm_own's "strictly under TEST_ROOT" test match every absolute path
# (review r1 of nexus-xzwyo): stop before anything is created or removed.
if [[ -z "$TEST_ROOT" || ! -d "$TEST_ROOT" ]]; then
    echo "  [FAIL] mktemp did not give this run a root (TMPDIR=${TMPDIR:-/tmp}); nothing was run" >&2
    exit 1
fi
trap 'rm -rf "${TEST_ROOT:?}"' EXIT

# rm_own <path>: remove a path ONLY if this run created it, i.e. it sits strictly under TEST_ROOT.
# Anything else (the real lock root, a harness's lock, an empty or relative value) is refused loudly.
rm_own() {
    local p="${1:-}"
    if [[ -n "$TEST_ROOT" && -d "$TEST_ROOT" && -n "$p" && "$p" == "$TEST_ROOT"/?* && "$p" != *..* ]]; then
        rm -rf "${p:?}"
    else
        echo "  [FAIL] rm_own refused to remove '$p': not under this run's $TEST_ROOT" >&2
        FAIL=$((FAIL + 1))
        return 1
    fi
}

# harness_cycle <name> <lockroot>: wiring, simulated holder, blocked and past-the-lock invocation for
# one harness, every lock under <lockroot>. Prints [ok]/[FAIL] lines; touches the counters only
# through ok/bad, so the concurrent section below runs it in subshells and counts the printed lines.
harness_cycle() {
    local name="$1" lockroot="$2" script args lockdir out rc out2 rc2
    script="${HARNESS_SCRIPT[$name]}"
    args="${HARNESS_ARGS[$name]}"
    lockdir="$lockroot/$name.lock"

    # ── non-vacuity: wiring assertion ────────────────────────────────────
    # shellcheck disable=SC2016 # intentional literal — grepping for the literal source line, not expanding it
    if grep -qF 'lock_acquire "$LOCKDIR" || exit 1' "$REPO_ROOT/$script"; then
        ok "wiring: $script calls lock_acquire"
    else
        bad "wiring: $script has NO lock_acquire call — silently unwired (non-vacuity guard tripped)"
        return 0 # nothing further to test for an unwired harness
    fi

    # Start from a clean slate inside this run's own root.
    rm_own "$lockdir" || return 0
    mkdir -p "$lockroot"

    # ── (2) simulate a running holder ────────────────────────────────────
    if lock_acquire "$lockdir" >/dev/null 2>&1; then
        ok "simulated holder acquired $lockdir"
    else
        bad "test setup: could not acquire $lockdir as simulated holder"
        return 0
    fi

    # ── (3) blocked invocation: real harness, lock held ──────────────────
    # shellcheck disable=SC2086 # $args is a single trusted literal per harness, intentionally unquoted for the (possibly-empty) split
    out="$(cd "$REPO_ROOT" && NX_E2E_LOCK_ROOT="$lockroot" bash "$script" $args 2>&1)"
    rc=$?

    if [[ $rc -ne 0 ]]; then
        ok "$name: blocked invocation exited nonzero ($rc)"
    else
        bad "$name: blocked invocation exited 0 — should have failed on the held lock"
    fi
    if [[ "$out" == *"FAILED to acquire"* ]]; then
        ok "$name: failure message names the lock"
    else
        bad "$name: no lock-failure message in output: $out"
    fi
    # The harness honoured the override: the lock it was refused is the one in THIS run's root, not
    # the real /tmp/nexus-e2e-locks-<uid> one (a harness that ignored NX_E2E_LOCK_ROOT would have
    # acquired the real lockdir and never been blocked by our simulated holder).
    if [[ "$out" == *"$lockdir"* ]]; then
        ok "$name: the blocked invocation names this run's own lockdir"
    else
        bad "$name: blocked invocation does not name $lockdir (override ignored?): $out"
    fi
    # No wall-clock bound here (nexus-boz84): "the harness body never ran" is proven by
    # the nonzero exit above and the output check below ('lock acquired' absent). A
    # timing bound only adds load sensitivity (1205 ms under 32-way load, well under
    # 1000 ms alone): any bound either flakes under a concurrent suite or is too loose
    # to tell anything.
    if [[ "$out" == *"lock acquired"* ]]; then
        bad "$name: 'lock acquired' appeared while the lock was HELD — got past a lock it should not have"
    else
        ok "$name: never got past the held lock (no 'lock acquired' in output)"
    fi

    # Release the simulated holder — the harness's own lock is now free.
    lock_release "$lockdir" 2>/dev/null || true

    # ── (4) past-the-lock invocation: real harness, lock free, self-test
    #     seam stops it immediately after lock_acquire succeeds ───────────
    # shellcheck disable=SC2086
    out2="$(cd "$REPO_ROOT" && NX_E2E_LOCK_ROOT="$lockroot" NX_E2E_LOCK_SELFTEST=1 bash "$script" $args 2>&1)"
    rc2=$?
    if [[ $rc2 -eq 0 ]]; then
        ok "$name: past-the-lock invocation exited 0 (self-test seam fired)"
    else
        bad "$name: past-the-lock invocation exited $rc2 (expected 0 — did the self-test seam get skipped?): $out2"
    fi
    if [[ "$out2" == *"lock acquired: $lockdir "* ]]; then
        ok "$name: re-invocation acquired the (now-free) lock and printed the acquire line"
    else
        bad "$name: re-invocation never got past the lock: $out2"
    fi
    # The lock must be released again afterward (the harness's own EXIT trap
    # firing on the self-test seam's `exit 0`) — not left held.
    if [[ -d "$lockdir" ]]; then
        bad "$name: lockdir still present after past-the-lock invocation exited — EXIT trap did not release it"
        rm_own "$lockdir"
    else
        ok "$name: lockdir released after past-the-lock invocation (EXIT trap fired)"
    fi

    rm_own "$lockdir"
}

for name in migration-rehearsal gc-ab sandbox; do
    echo
    echo "=== $name ==="
    harness_cycle "$name" "$TEST_ROOT/solo"
done

# ── two overlapping runs do not interfere (nexus-xzwyo) ──────────────────
# Before the override, every run for one uid used /tmp/nexus-e2e-locks-<uid>/<name>.lock: run A's
# simulated holder made run B's setup fail, and B's `rm -rf` of the lockdir deleted A's live lock.
# (a) Deterministic overlap: A holds its lock while a harness runs against B's root and gets
#     straight through; the same harness against A's root stays blocked.
# (b) Concurrent: two full cycles at once, each in its own root.
echo
echo "=== overlapping runs do not interfere ==="
for name in migration-rehearsal gc-ab sandbox; do
    root_a="$TEST_ROOT/overlap-a"
    root_b="$TEST_ROOT/overlap-b"
    rm_own "$root_a/$name.lock" || continue
    rm_own "$root_b/$name.lock" || continue
    mkdir -p "$root_a" "$root_b"
    args="${HARNESS_ARGS[$name]}"
    if ! lock_acquire "$root_a/$name.lock" >/dev/null 2>&1; then
        bad "$name: overlap setup: could not hold $root_a/$name.lock"
        continue
    fi
    # shellcheck disable=SC2086
    out_b="$(cd "$REPO_ROOT" && NX_E2E_LOCK_ROOT="$root_b" NX_E2E_LOCK_SELFTEST=1 bash "${HARNESS_SCRIPT[$name]}" $args 2>&1)"
    rc_b=$?
    # shellcheck disable=SC2086
    out_a="$(cd "$REPO_ROOT" && NX_E2E_LOCK_ROOT="$root_a" NX_E2E_LOCK_SELFTEST=1 bash "${HARNESS_SCRIPT[$name]}" $args 2>&1)"
    rc_a=$?
    if [[ $rc_b -eq 0 && "$out_b" == *"lock acquired: $root_b/$name.lock "* ]]; then
        ok "$name: run B passed the lock while run A held its own"
    else
        bad "$name: run B was affected by run A's held lock (rc $rc_b): $out_b"
    fi
    if [[ $rc_a -ne 0 && "$out_a" == *"FAILED to acquire"* ]]; then
        ok "$name: run A is still blocked by its own held lock"
    else
        bad "$name: run A's held lock did not block A (rc $rc_a): $out_a"
    fi
    if [[ -d "$root_a/$name.lock" ]]; then
        ok "$name: run A's lock survived run B (not deleted)"
    else
        bad "$name: run A's lock was deleted while run B ran"
    fi
    lock_release "$root_a/$name.lock" 2>/dev/null || true
done
conc_a="$TEST_ROOT/conc-a.out"
conc_b="$TEST_ROOT/conc-b.out"
for name in migration-rehearsal gc-ab sandbox; do
    harness_cycle "$name" "$TEST_ROOT/conc-a" >"$conc_a" 2>&1 &
    pid_a=$!
    harness_cycle "$name" "$TEST_ROOT/conc-b" >"$conc_b" 2>&1 &
    pid_b=$!
    wait "$pid_a"
    wait "$pid_b"
    n_ok_a="$(grep -c '^  \[ok\]' "$conc_a" || true)"
    n_ok_b="$(grep -c '^  \[ok\]' "$conc_b" || true)"
    n_bad="$(cat "$conc_a" "$conc_b" | grep -c '\[FAIL\]' || true)"
    # Non-vacuity: a cycle that ran prints 9 [ok] lines; both must have, and neither a [FAIL].
    if [[ "$n_ok_a" -ge 9 && "$n_ok_b" -ge 9 && "$n_bad" -eq 0 ]]; then
        ok "$name: two concurrent full cycles in separate roots both passed ($n_ok_a/$n_ok_b ok lines)"
    else
        fail_lines="$(grep -h 'FAIL' "$conc_a" "$conc_b" || true)"
        bad "$name: concurrent cycles interfered (ok $n_ok_a/$n_ok_b, FAIL lines $n_bad): ${fail_lines:0:600}"
    fi
done

# ── the real lock root is out of reach ───────────────────────────────────
# Each harness's lock line must be under the test's root, never the real per-user root; the test's only removal
# primitive refuses anything outside TEST_ROOT.
echo
echo "=== the real lock root is untouched ==="
FAIL_BEFORE=$FAIL
# The refusal is checked on a decoy outside the root, never on the real lock path: a regression in
# rm_own must not be able to delete a live harness's lock (review r1 of nexus-xzwyo).
DECOY_DIR="$(mktemp -d "${TMPDIR:-/tmp}/harness_lock_decoy.XXXXXX")"
mkdir -p "$DECOY_DIR/sandbox.lock"
if rm_own "$DECOY_DIR/sandbox.lock" 2>/dev/null || [[ ! -d "$DECOY_DIR/sandbox.lock" ]]; then
    bad "rm_own removed a path outside this run's root"
else
    ok "rm_own refuses a lock path outside this run's root"
fi
FAIL=$FAIL_BEFORE # rm_own counted its own refusal as a failure; it was the expected one
# With an empty root rm_own must still refuse (a failed mktemp left unchecked would otherwise
# turn "under TEST_ROOT" into "any absolute path").
if (TEST_ROOT=""; rm_own "$DECOY_DIR/sandbox.lock") 2>/dev/null || [[ ! -d "$DECOY_DIR/sandbox.lock" ]]; then
    bad "rm_own with an empty TEST_ROOT accepted a path outside the run"
else
    ok "rm_own with an empty TEST_ROOT refuses"
fi
FAIL=$FAIL_BEFORE
rm -rf "${DECOY_DIR:?}"
if rm_own "" 2>/dev/null || rm_own "$TEST_ROOT" 2>/dev/null || rm_own "$TEST_ROOT/../x" 2>/dev/null; then
    bad "rm_own accepted an empty path, the root itself or a .. path"
else
    ok "rm_own refuses an empty path, the root itself and a .. path"
fi
FAIL=$FAIL_BEFORE

# ── migration-rehearsal arg-conflict region regression ───────────────────
# Code-review CRITICAL finding: the FIRST trap (originally installed before
# LOCKDIR is assigned and lib/lock.sh is sourced) referenced $LOCKDIR. Under
# `set -u`, any of the argument-conflict guards between those two points (e.g.
# --package-upgrade + --acquire together) fires that trap on `exit 2`, and the
# trap's OWN evaluation aborts on the unbound $LOCKDIR before the documented
# exit 2 / conflict message ever reaches the caller — silently downgrading a
# clean usage error into a confusing "unbound variable" crash (exit 1). This
# is deliberately OUTSIDE the per-harness loop above (that loop only exercises
# the post-lock-acquire region) — it targets the pre-lock region specifically,
# no lock held, no SELFTEST var, real invocation.
echo
echo "=== migration-rehearsal: arg-conflict region (pre-lock-acquire guards) ==="
out3="$(cd "$REPO_ROOT" && bash tests/e2e/migration-rehearsal/run.sh --package-upgrade --acquire 2>&1)"
rc3=$?
if [[ $rc3 -eq 2 ]]; then
    ok "migration-rehearsal --package-upgrade --acquire: exits exactly 2"
else
    bad "migration-rehearsal --package-upgrade --acquire: exited $rc3 (expected 2): $out3"
fi
if [[ "$out3" == *"standalone journeys"* ]]; then
    ok "migration-rehearsal --package-upgrade --acquire: conflict message present"
else
    bad "migration-rehearsal --package-upgrade --acquire: conflict message missing: $out3"
fi
if [[ "${out3,,}" == *"unbound variable"* ]]; then
    bad "migration-rehearsal --package-upgrade --acquire: 'unbound variable' leaked on stderr (a pre-lock trap referenced \$LOCKDIR before assignment): $out3"
else
    ok "migration-rehearsal --package-upgrade --acquire: no unbound-variable leak"
fi

# ── gc-ab: no early-exit/validation-guard region exists ──────────────────
# gc-ab/run-ab.sh takes no CLI arguments at all — a single linear path from
# top to bottom, no arg-parsing while loop, no mode dispatch, nothing that
# could exit before reaching lock_acquire. The wiring assertion in the main
# per-harness loop above (the non-vacuity grep for its lock_acquire call)
# is the only coverage this harness's shape admits; no separate early-exit
# region test applies here.

# ── per-user lock root (nexus-c6lsu) ─────────────────────────────────────
# The lock root carries the uid so two Unix users on one box never share (and
# never fail to create or write into) each other's lockdirs. Simulate two uids
# with a PATH shim that answers `id -u` and defers everything else to the real
# `id`, run each harness's self-test seam under both, and require the two lock
# paths to differ and to be exactly the documented default,
# /tmp/nexus-e2e-locks-<uid>/<name>.lock (NX_E2E_LOCK_ROOT is unset above, so this is the only place
# the default root is exercised, and only under fake uids no real user has).
echo
echo "=== per-user lock root ==="
REAL_ID="$(command -v id)"
SHIM_DIR="$(mktemp -d "${TMPDIR:-/tmp}/harness_lock_uid_shim.XXXXXX")"
cat >"$SHIM_DIR/id" <<SHIM
#!/usr/bin/env bash
if [[ "\$*" == "-u" ]]; then echo "\${NX_FAKE_UID:?}"; else exec "$REAL_ID" "\$@"; fi
SHIM
chmod +x "$SHIM_DIR/id"
# Two fake uids private to this run: the lock root is a hard-coded /tmp that concurrent CI
# jobs on one host share, so fixed uids let another run's live lock refuse this one.
FAKE_A="$(( 1000000 + $(od -An -N3 -tu4 /dev/urandom | tr -d ' ') ))"
FAKE_B="$(( FAKE_A + 1 ))"
for name in migration-rehearsal gc-ab sandbox; do
    script="${HARNESS_SCRIPT[$name]}"
    args="${HARNESS_ARGS[$name]}"
    declare -A seen=()
    for fake in "$FAKE_A" "$FAKE_B"; do
        # shellcheck disable=SC2086
        outu="$(cd "$REPO_ROOT" && PATH="$SHIM_DIR:$PATH" NX_FAKE_UID="$fake" NX_E2E_LOCK_SELFTEST=1 bash "$script" $args 2>&1)"
        seen[$fake]="$(printf '%s\n' "$outu" | sed -n 's/.*lock acquired: \(.*\) (pid .*/\1/p')"
    done
    if [[ -n "${seen[$FAKE_A]}" && -n "${seen[$FAKE_B]}" && "${seen[$FAKE_A]}" != "${seen[$FAKE_B]}" \
          && "${seen[$FAKE_A]}" == "/tmp/nexus-e2e-locks-$FAKE_A/$name.lock" \
          && "${seen[$FAKE_B]}" == "/tmp/nexus-e2e-locks-$FAKE_B/$name.lock" ]]; then
        ok "$name: two uids get different lock dirs (${seen[$FAKE_A]} vs ${seen[$FAKE_B]})"
    else
        bad "$name: lock dir is not per-user: uid $FAKE_A -> '${seen[$FAKE_A]}', uid $FAKE_B -> '${seen[$FAKE_B]}'"
    fi
    unset seen
done
rm -rf "${SHIM_DIR:?}"
# The shim runs above create /tmp/nexus-e2e-locks-<fake uid> roots (the harnesses mkdir them).
# They are empty once every lock is released; remove them so a test run leaves nothing behind.
rm -rf "/tmp/nexus-e2e-locks-${FAKE_A:?}" "/tmp/nexus-e2e-locks-${FAKE_B:?}"

# ── no harness may still name the shared (uid-less) lock root ─────────────
# Scan the sources: any lock path under the bare "/tmp/nexus-e2e-locks/" root (no uid) is the
# permission failure nexus-c6lsu fixed, and the review found one such line left. Non-vacuity:
# migration-rehearsal's own lock line must be present (a scan that matched nothing proves
# nothing).
echo
echo "=== no harness names the shared lock root ==="
shared_root_hits=""
for name in "${!HARNESS_SCRIPT[@]}"; do
    # Any non-comment mention of the root, with or without the uid: the root comes from
    # lock.sh's e2e_lock_root only, so a hard-coded path would bypass NX_E2E_LOCK_ROOT (nexus-xzwyo).
    hits="$(grep -nE 'nexus-e2e-locks' "$REPO_ROOT/${HARNESS_SCRIPT[$name]}" | grep -vE '^[0-9]+:[[:space:]]*#' || true)"
    [[ -n "$hits" ]] && shared_root_hits+="${HARNESS_SCRIPT[$name]}: $hits"$'\n'
done
if [[ -z "$shared_root_hits" ]]; then
    ok "no harness script hard-codes a /tmp/nexus-e2e-locks path (uid-less or not)"
else
    bad "harness(es) hard-code a lock root instead of e2e_lock_root: $shared_root_hits"
fi
# Non-vacuity: every harness must take its LOCKDIR from e2e_lock_root (a scan that matched nothing
# proves nothing); the default it resolves to is pinned per uid by the section above.
for name in "${!HARNESS_SCRIPT[@]}"; do
    if grep -qE 'LOCKDIR="\$\(e2e_lock_root\)/' "$REPO_ROOT/${HARNESS_SCRIPT[$name]}"; then
        ok "$name: LOCKDIR comes from e2e_lock_root"
    else
        bad "$name: LOCKDIR is not assigned from e2e_lock_root (non-vacuity guard tripped)"
    fi
done

echo
echo "harness_lock_test.sh: ${PASS} passed, ${FAIL} failed"
[[ "$FAIL" -eq 0 ]]
