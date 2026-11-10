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
# Three starting points, each in its own work dir with its own step table and verdict line:
#   the newest published version below the candidate (8.0.0 while 8.1.0 is the candidate), the
#   last published 7.x release, and 7.76.0 (before per-collection indexes: its upgrade boot builds
#   every pci_ index, and the PCI keep check restarts the engine mid-build, during nx upgrade, and judges by pid).
#   `--from-version X.Y.Z` runs one of them alone. Around `nx upgrade` the driver also records the
#   WAL bytes and MB/s PG wrote (see the driver's docstring for the extrapolation).
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
# Last line: "UPGRADE REHEARSAL PASSED ..." (exit 0) or "UPGRADE REHEARSAL FAILED ..." (exit 1).
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
LAST_LINE="UPGRADE REHEARSAL FAILED: the harness did not finish"

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
            echo "$LAST_LINE"
            return 0
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
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

if [ -n "${NEXUS_TARGET_RELEASE:-}" ]; then
    TARGET_ARGS=(--target-release "$NEXUS_TARGET_RELEASE")
    echo "upgrade rehearsal: target is the PUBLISHED conexus $NEXUS_TARGET_RELEASE"
else
    echo "── build the wheel under test ──"
    if ! ( cd "$REPO_ROOT" && uv build --wheel -o "$WORK/dist" ) >"$WORK/build.log" 2>&1; then
        LAST_LINE="UPGRADE REHEARSAL FAILED: wheel build failed (see $WORK/build.log)"
        echo "$LAST_LINE" >&2
        exit 1
    fi
    WHEELS=("$WORK"/dist/conexus-*.whl)
    WHEEL="${WHEELS[0]}"
    [ -f "$WHEEL" ] || { LAST_LINE="UPGRADE REHEARSAL FAILED: the build produced no wheel"; exit 1; }
    TARGET_ARGS=(--target-wheel "$WHEEL")
fi

set +e
"$E2E_PYTHON" -u "$REPO_ROOT/scripts/upgrade_rehearsal.py" \
    --work "$WORK" "${TARGET_ARGS[@]}" "${DRIVER_ARGS[@]+"${DRIVER_ARGS[@]}"}" \
    2>&1 | tee "$WORK/driver.out"
rc=${PIPESTATUS[0]}
set -e

LAST_LINE="$(grep -E '^UPGRADE REHEARSAL (PASSED|FAILED|NOT RUN)' "$WORK/driver.out" | tail -n1 || true)"
[ -n "$LAST_LINE" ] || LAST_LINE="UPGRADE REHEARSAL FAILED: the driver ended (exit $rc) without a verdict line"
if [ "$rc" = 0 ] && [[ "$LAST_LINE" == "UPGRADE REHEARSAL PASSED"* ]]; then
    GATE_OK=1
    exit 0
fi
# A driver that exits 0 without a PASSED line, or any non-zero exit, is a failure.
[ "$rc" != 0 ] || rc=1
exit "$rc"
