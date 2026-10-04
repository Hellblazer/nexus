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
# Machine-global lock root the harnesses themselves use (must match — this
# test exercises the REAL lockdir path each harness computes, not a
# lookalike). HARD-CODED /tmp, not ${TMPDIR:-/tmp} (code-review SIGNIFICANT
# fix, mirrors the same fix in all 4 harnesses): a per-context TMPDIR
# divergence would make this test compute a DIFFERENT lockdir than the
# harness itself, silently validating nothing.
# Per-user, matching the harnesses (nexus-c6lsu).
LOCKROOT="/tmp/nexus-e2e-locks-$(id -u)"

for name in migration-rehearsal gc-ab sandbox; do
    echo
    echo "=== $name ==="
    script="${HARNESS_SCRIPT[$name]}"
    args="${HARNESS_ARGS[$name]}"
    lock_name="$name"
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
    if [[ "$out" == *"FAILED to acquire"* ]]; then
        ok "$name: failure message names the lock"
    else
        bad "$name: no lock-failure message in output: $out"
    fi
    if ((elapsed_ms < 1000)); then
        ok "$name: blocked invocation resolved in ${elapsed_ms}ms (<1000ms — never ran the harness body)"
    else
        bad "$name: blocked invocation took ${elapsed_ms}ms (>=1000ms — looks like real work ran before failing)"
    fi
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
    out2="$(cd "$REPO_ROOT" && NX_E2E_LOCK_SELFTEST=1 bash "$script" $args 2>&1)"
    rc2=$?
    if [[ $rc2 -eq 0 ]]; then
        ok "$name: past-the-lock invocation exited 0 (self-test seam fired)"
    else
        bad "$name: past-the-lock invocation exited $rc2 (expected 0 — did the self-test seam get skipped?): $out2"
    fi
    if [[ "$out2" == *"lock acquired"* ]]; then
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
# paths to differ and to name their own uid. The loop above already pins that
# the REAL harness computes $LOCKROOT, so this proves the uid reaches it.
echo
echo "=== per-user lock root ==="
REAL_ID="$(command -v id)"
SHIM_DIR="$(mktemp -d "${TMPDIR:-/tmp}/harness_lock_uid_shim.XXXXXX")"
cat >"$SHIM_DIR/id" <<SHIM
#!/usr/bin/env bash
if [[ "\$*" == "-u" ]]; then echo "\${NX_FAKE_UID:?}"; else exec "$REAL_ID" "\$@"; fi
SHIM
chmod +x "$SHIM_DIR/id"
for name in migration-rehearsal gc-ab sandbox; do
    script="${HARNESS_SCRIPT[$name]}"
    args="${HARNESS_ARGS[$name]}"
    declare -A seen=()
    for fake in 31337 42424; do
        # shellcheck disable=SC2086
        outu="$(cd "$REPO_ROOT" && PATH="$SHIM_DIR:$PATH" NX_FAKE_UID="$fake" NX_E2E_LOCK_SELFTEST=1 bash "$script" $args 2>&1)"
        seen[$fake]="$(printf '%s\n' "$outu" | sed -n 's/.*lock acquired: \(.*\) (pid .*/\1/p')"
    done
    if [[ -n "${seen[31337]}" && -n "${seen[42424]}" && "${seen[31337]}" != "${seen[42424]}" \
          && "${seen[31337]}" == *"-31337/"* && "${seen[42424]}" == *"-42424/"* ]]; then
        ok "$name: two uids get different lock dirs (${seen[31337]} vs ${seen[42424]})"
    else
        bad "$name: lock dir is not per-user: uid 31337 -> '${seen[31337]}', uid 42424 -> '${seen[42424]}'"
    fi
    unset seen
done
rm -rf "${SHIM_DIR:?}"
# The shim runs above create /tmp/nexus-e2e-locks-<fake uid> roots (the harnesses mkdir them).
# They are empty once every lock is released; remove them so a test run leaves nothing behind.
rm -rf "/tmp/nexus-e2e-locks-31337" "/tmp/nexus-e2e-locks-42424"

# ── no harness may still name the shared (uid-less) lock root ─────────────
# Scan the sources: any lock path under the bare "/tmp/nexus-e2e-locks/" root (no uid) is the
# permission failure nexus-c6lsu fixed, and the review found one such line left. Non-vacuity:
# migration-rehearsal's own lock line must be present (a scan that matched nothing proves
# nothing).
echo
echo "=== no harness names the shared lock root ==="
shared_root_hits=""
for name in "${!HARNESS_SCRIPT[@]}"; do
    hits="$(grep -nE '/tmp/nexus-e2e-locks/' "$REPO_ROOT/${HARNESS_SCRIPT[$name]}" | grep -vE '^[0-9]+:[[:space:]]*#' || true)"
    [[ -n "$hits" ]] && shared_root_hits+="${HARNESS_SCRIPT[$name]}: $hits"$'\n'
done
if [[ -z "$shared_root_hits" ]]; then
    ok "no harness script assigns a lock path under the uid-less /tmp/nexus-e2e-locks/ root"
else
    bad "harness(es) still use the shared uid-less lock root: $shared_root_hits"
fi
if grep -qE 'LOCKDIR="/tmp/nexus-e2e-locks-\$\(id -u\)/migration-rehearsal\.lock"' "$REPO_ROOT/tests/e2e/migration-rehearsal/run.sh"; then
    ok "migration-rehearsal lock is per-user"
else
    bad "migration-rehearsal LOCKDIR is missing or not per-user (non-vacuity guard tripped)"
fi

echo
echo "harness_lock_test.sh: ${PASS} passed, ${FAIL} failed"
[[ "$FAIL" -eq 0 ]]
