#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# nexus-f3yqd.5: UPGRADE REHEARSAL FROM THE USER'S SEAT -- a release gate.
#
# Why: two upgrades in a row did a ~20 minute silent rewrite at the first engine boot after
# `nx upgrade`, and no gate looked at what a user sees. This one takes a real install of a
# previous conexus with a sizeable local store, upgrades the package the way a user does, runs
# `nx upgrade`, `nx doctor` and `nx search`, and times each step: a step fails when it prints
# nothing for more than the silence limit (default 10 s), runs past its ceiling, or exits
# non-zero. The mechanics live in scripts/upgrade_rehearsal.py (offline self-test:
# tests/test_upgrade_rehearsal_watchdog.py).
#
# Three starting points, each in its own work dir with its own step table and its own
# "UPGRADE REHEARSAL START (from X)" line:
#   the newest published version below the candidate (8.0.0 while 8.1.0 is the candidate), the
#   last published 7.x release, and 7.76.0 (before per-collection indexes: its upgrade boot builds
#   every pci_ index, and the PCI keep check restarts the engine mid-build, during nx upgrade, and judges by pid).
#   `--from-version X.Y.Z` runs one of them alone (and is then not the release gate). Around
#   `nx upgrade` the driver also records the WAL bytes and MB/s PG wrote (see the driver's docstring
#   for the extrapolation). The 7.76.0 start's nx upgrade ceiling is PENDING (nexus-f3yqd.5): until it
#   is set that start cannot pass, and the gate ends NOT RUN (exit 2).
#
# Self-provisioning: builds the wheel from THIS checkout (or, with NEXUS_TARGET_RELEASE=X.Y.Z,
# installs that published version), uses a throwaway HOME and NEXUS_CONFIG_DIR under a work dir,
# NX_LOCAL=1, no cloud, no Voyage key, NX_NO_TELEMETRY=1. Ambient state is not trusted: every
# step runs under an allowlisted environment built by the driver.
#
# Usage: tests/e2e/upgrade-rehearsal.sh [driver flags]
#   --from-version X.Y.Z|both   (default both)
#   --standin                   small stand-in prepare instead of the real seeder (development)
#   --rows-floor N, --silence-limit S, --upgrade-ceiling S, ...   see scripts/upgrade_rehearsal.py --help
#   --engine-jar PATH           run the new client's engine from a built jar (also NEXUS_SERVICE_JAR),
#                               e.g. one built from the PCI restart fix
#   --keep-work                 keep the work tree on success too (failure always keeps the logs)
#
# Heavy: run it on a lab host, e.g.
#   lab-remote -c lab-m -- tests/e2e/upgrade-rehearsal.sh
# The last line is the gate's own, "UPGRADE REHEARSAL GATE: <word> ...", and the exit says the same:
#   exit 0  PASSED                every start passed, no non-default setting
#   exit 1  FAILED                a breach, a failed step, a non-vacuity finding, a surviving postmaster
#   exit 2  NOT RUN               the harness could not start, or a start's ceiling is pending
#           NOT THE RELEASE GATE  every start passed under a non-default flag (--standin, --rows-floor,
#                                 --silence-limit, any --*-ceiling, --prepare-cmd, --no-pci-restart-check,
#                                 a single --from-version, --engine-jar ...): a development run
# Only the "GATE:" line counts; the per-start lines never start with it. A run that ends without one
# is FAILED. Run it in the background: it takes about an hour (see the release skill, Step 6e).
set -euo pipefail

# shellcheck source=lib/python.sh disable=SC1091
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/python.sh"
e2e_python_resolve || exit 2

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
KEEP_WORK=0
DRIVER_ARGS=()
for arg in "$@"; do
    case "$arg" in
        --keep-work) KEEP_WORK=1 ;;
        -h|--help)
            sed -n '2,/^set -euo/p' "${BASH_SOURCE[0]}" | sed '$d' | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *) DRIVER_ARGS+=("$arg") ;;
    esac
done

MVV_TMP="${TMPDIR:-/tmp}"
MVV_TMP="${MVV_TMP%/}"
WORK="$(mktemp -d "$MVV_TMP/upgrade-rehearsal-XXXXXX")"
GATE_OK=0
GATE_MATCH='^UPGRADE REHEARSAL GATE: (PASSED|FAILED|NOT RUN|NOT THE RELEASE GATE)'
LAST_LINE="UPGRADE REHEARSAL GATE: FAILED: the harness did not finish"

cleanup() {
    local rc=$?
    # A postmaster still running over a work dir we are about to delete holds a SysV shm segment
    # for good. The driver stops its own stack; this only verifies, by the pid the cluster wrote
    # in OUR work dir, and keeps the tree rather than orphan one.
    local f pid
    while IFS= read -r f; do
        pid="$(head -n1 "$f" 2>/dev/null || true)"
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            echo "WORK KEPT: $WORK (postmaster $pid from $f is still running; removing the tree would orphan it)" >&2
            # A run that left a postmaster behind is not a pass, whatever the driver printed.
            echo "UPGRADE REHEARSAL GATE: FAILED: postmaster $pid from $f survived the run (was: $LAST_LINE)"
            exit 1
        fi
    done < <(find "$WORK" -name postmaster.pid 2>/dev/null)
    if [ "$GATE_OK" != 1 ] && [ "$KEEP_WORK" != 1 ]; then
        # Keep only small evidence: step logs, verdicts, the prepare JSON and the service-side
        # root causes under the sandbox config dir. config.yml, credentials and tokens are never
        # copied. The multi-GB tree is removed.
        local evidence="$MVV_TMP/${WORK##*/}-logs" run
        mkdir -p "$evidence"
        for run in "$WORK"/from-*; do
            [ -d "$run" ] || continue
            local dest="$evidence/${run##*/}"
            mkdir -p "$dest"
            cp -R "$run/logs" "$dest/logs" 2>/dev/null || true
            cp "$run/verdict.json" "$run/prepare.json" "$dest/" 2>/dev/null || true
            [ -d "$run/config/logs" ] && cp -R "$run/config/logs" "$dest/engine-logs" 2>/dev/null || true
            [ -f "$run/config/postgres/pg.log" ] && cp "$run/config/postgres/pg.log" "$dest/pg.log" 2>/dev/null || true
        done
        cp "$WORK/verdict.json" "$WORK/build.log" "$evidence/" 2>/dev/null || true
        echo "FAILURE EVIDENCE PRESERVED: $evidence (work tree removed; --keep-work keeps it)" >&2
    fi
    if [ "$GATE_OK" = 1 ] && [ "$KEEP_WORK" = 1 ]; then
        echo "WORK KEPT: $WORK (--keep-work)" >&2
    elif [ "$KEEP_WORK" != 1 ]; then
        rm -rf "$WORK" || true
    fi
    echo "$LAST_LINE"
    return "$rc"
}
DRIVER_PID=""
TEE_PID=""
# A signal must reach the driver, which runs its own cleanup (stack stop, postmaster reap): bash would
# otherwise hold the trap until a foreground driver ended. The driver runs in the background so that
# `wait` is interruptible; then this waits for the driver's cleanup before the work dir is judged.
forward_signal() {
    local code=$1
    if [ -n "$DRIVER_PID" ] && kill -0 "$DRIVER_PID" 2>/dev/null; then
        kill -TERM "$DRIVER_PID" 2>/dev/null || true
        wait "$DRIVER_PID" 2>/dev/null || true
    fi
    if [ -n "$TEE_PID" ]; then wait "$TEE_PID" 2>/dev/null || true; fi
    exit "$code"
}
trap cleanup EXIT
trap 'forward_signal 130' INT
trap 'forward_signal 143' TERM
trap 'forward_signal 129' HUP

if [ -n "${NEXUS_TARGET_RELEASE:-}" ]; then
    TARGET_ARGS=(--target-release "$NEXUS_TARGET_RELEASE")
    echo "upgrade rehearsal: target is the PUBLISHED conexus $NEXUS_TARGET_RELEASE"
else
    echo "── build the wheel under test ──"
    if ! ( cd "$REPO_ROOT" && uv build --wheel -o "$WORK/dist" ) >"$WORK/build.log" 2>&1; then
        LAST_LINE="UPGRADE REHEARSAL GATE: FAILED: wheel build failed (see $WORK/build.log)"
        echo "$LAST_LINE" >&2
        exit 1
    fi
    WHEELS=("$WORK"/dist/conexus-*.whl)
    WHEEL="${WHEELS[0]}"
    [ -f "$WHEEL" ] || { LAST_LINE="UPGRADE REHEARSAL GATE: FAILED: the build produced no wheel"; exit 1; }
    TARGET_ARGS=(--target-wheel "$WHEEL")
fi

mkfifo "$WORK/driver.fifo"
tee "$WORK/driver.out" <"$WORK/driver.fifo" &
TEE_PID=$!
set +e
"$E2E_PYTHON" -u "$REPO_ROOT/scripts/upgrade_rehearsal.py" \
    --work "$WORK" "${TARGET_ARGS[@]}" "${DRIVER_ARGS[@]+"${DRIVER_ARGS[@]}"}" \
    >"$WORK/driver.fifo" 2>&1 &
DRIVER_PID=$!
wait "$DRIVER_PID"
rc=$?
wait "$TEE_PID"
set -e
DRIVER_PID=""
TEE_PID=""

LAST_LINE="$(grep -E "$GATE_MATCH" "$WORK/driver.out" | tail -n1 || true)"
[ -n "$LAST_LINE" ] || LAST_LINE="UPGRADE REHEARSAL GATE: FAILED: the driver ended (exit $rc) without a verdict line"
if [ "$rc" = 0 ] && [[ "$LAST_LINE" == "UPGRADE REHEARSAL GATE: PASSED"* ]]; then
    GATE_OK=1
    exit 0
fi
# A driver that exits 0 without the PASSED gate line, or any non-zero exit, is not a pass.
[ "$rc" != 0 ] || rc=1
exit "$rc"
