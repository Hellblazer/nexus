#!/usr/bin/env bash
# scripts/mvnw-leased.sh — thin wrapper around service/mvnw that takes the
# nexus-c00dw single-builder lease first, so an orchestrator invocation and
# a developer-agent invocation of ./mvnw never write service/target at the
# same time (see scripts/lib/build-lease.sh for the mechanism and the
# incident that motivated it).
#
# usage: scripts/mvnw-leased.sh [--lab-run <class>] [mvnw args...]
#
# On a lab pool host (chas, qwent) pass --lab-run <class> rather than
# wrapping this script in lab-run: the lease is then taken before the pool
# grant, so a build waiting its turn holds no cores (nexus-iqmih).
#
# A live holder is WAITED FOR (nexus-g6xpa): the lease is shared by every
# worktree of this repo (see scripts/lib/build-lease.sh § LEASE LOCATION),
# so a second engine build or suite on this box queues behind the first
# instead of running beside it. NX_BUILD_LEASE_WAIT=<seconds> bounds the
# wait (default 3600); 0 restores the old refuse-immediately behaviour.
# Exits 75 (EX_TEMPFAIL) without running mvnw at all only once that bound
# is exhausted — the refusal message names the holder.
#
# SIGNAL HANDLING (review finding round 3, nexus-c00dw). A plain
# `trap '...release...' EXIT` is UNSAFE here. Under `set -m` the
# backgrounded ./mvnw child lives in its OWN process group, separate from
# this wrapper's — so SIGINT (Ctrl-C) or SIGTERM (a `timeout`, or an
# orchestrator killing just this wrapper's pid) reaches ONLY the wrapper,
# never the child. With no INT/TERM trap installed, bash's default
# disposition terminates the wrapper on that signal but still runs the
# EXIT trap first (the exact mechanism proven safe-in-isolation by
# scripts/lib/build-lease_test.sh's "SIGTERM'd holder releases via its own
# EXIT trap" case) — so an UNCONDITIONAL release there frees the lease
# while the real build (the child, still alive in its own group) keeps
# writing to service/target. Two changes close it:
#   1. The EXIT trap is now CONDITIONAL: it releases only when the tracked
#      build (child_pgid) is confirmed dead, or was never launched at all.
#      If it is still alive, the lease is left HELD with a WARNING naming
#      the live pgid — a future stale-reclaim picks it up once the child
#      actually exits; it is never silently double-held either.
#   2. INT and TERM traps FORWARD the signal to the child's process group,
#      wait (bounded, 30s) for it to actually exit, escalate to KILL if it
#      does not, and only then exit — so the now-safe EXIT trap releases
#      cleanly instead of abandoning a still-running build.

set -euo pipefail
set -m
# Job control (review finding round 2, nexus-c00dw): with `set -m`, a
# backgrounded job becomes its own process-GROUP leader — its pgid equals
# its own pid. Tracking the child's process group instead of just the
# wrapper's own pid is what makes round 3's signal handling above possible
# — see build_lease_track_pid / _build_lease_group_alive in
# scripts/lib/build-lease.sh.

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=./lib/build-lease.sh disable=SC1091
source "$repo_root/scripts/lib/build-lease.sh"

# --lab-run <class> (nexus-iqmih): run the build as `lab-run -c <class> --
# ./mvnw ...` AFTER taking the lease. The other order, `lab-run -- this
# script`, waits for the lease inside the pool grant: on chas two lab-m
# grants sat idle behind one lease holder while other requests queued
# behind them. Waiting here, outside any grant, holds no cores or memory.
lab_class=""
if [[ "${1:-}" == "--lab-run" ]]; then
    if [[ -z "${2:-}" || "${2:-}" == -* ]]; then
        echo "mvnw-leased.sh: --lab-run needs a class (lab-s, lab-m, lab-l, ...)" >&2
        exit 64
    fi
    lab_class="$2"
    shift 2
    if [[ -n "${LAB_CPUS:-}" ]]; then
        # A second grant requested from inside a grant can wait forever on a
        # full pool while holding the first.
        echo "mvnw-leased.sh: --lab-run given inside a lab-run grant (LAB_CPUS is set); run it outside lab-run, or drop --lab-run" >&2
        exit 64
    fi
    if ! command -v lab-run >/dev/null 2>&1; then
        echo "mvnw-leased.sh: --lab-run $lab_class given but lab-run is not on PATH" >&2
        exit 127
    fi
elif [[ -n "${LAB_CPUS:-}" ]]; then
    # Already inside a lab-run grant: a wait for the lease from here holds
    # the grant's cores. Say so once; the build still runs.
    holder_pid="$(cat "$(_build_lease_dir service)/pid" 2>/dev/null || true)"
    if [[ -n "$holder_pid" ]] && _build_lease_pid_alive "$holder_pid"; then
        echo "mvnw-leased.sh: WARNING: waiting for the build lease (held by pid $holder_pid) inside a lab-run grant holds its cores; run 'scripts/mvnw-leased.sh --lab-run <class> ...' outside lab-run instead" >&2
    fi
fi

build_lease_acquire_wait service "${NX_BUILD_LEASE_WAIT:-3600}" ./mvnw "$@"

# Test Postgres containers are labelled dev.nexus.test.run=$NX_TEST_RUN_ID
# (nexus-xynce) so a host can count one build's containers; name this build
# unless the caller already did.
export NX_TEST_RUN_ID="${NX_TEST_RUN_ID:-mvnw-$(id -un 2>/dev/null || echo u)-$$-$(date +%s)}"

# Initialized before any trap is installed, so a trap can always reference
# it safely under `set -u` even if a signal lands in the brief window
# before the real build is launched below.
child_pgid=""
child_pid=""
# Under --lab-run the child is lab-run, and the real build runs further down
# (sudo, then a systemd scope) in a process group of its own that a signal to
# child_pgid need not reach: killing sudo can leave mvnw building. The inner
# command makes itself a group leader and writes its pid here; that group is
# tracked as well, and is what the lease's pgid names once it exists.
build_pgid=""
build_pidfile="$(_build_lease_dir service)/build_pid"

_build_alive() {
    { [[ -n "$child_pgid" ]] && _build_lease_group_alive "$child_pgid" 2>/dev/null; } ||
        { [[ -n "$build_pgid" ]] && _build_lease_group_alive "$build_pgid" 2>/dev/null; }
}

_note_build_pgid() {
    if [[ -z "$build_pgid" && -s "$build_pidfile" ]]; then
        build_pgid="$(cat "$build_pidfile" 2>/dev/null || true)"
        [[ "$build_pgid" =~ ^[0-9]+$ ]] || { build_pgid=""; return 0; }
        build_lease_track_pid service "$build_pgid"
    fi
}

# _release_if_safe — the EXIT trap. Never releases while the tracked build
# is still alive (round-3 fix); an unconditional release here is exactly
# what let a SIGTERM'd wrapper free the lease out from under a
# still-running child.
_release_if_safe() {
    _note_build_pgid
    if _build_alive; then
        echo "mvnw-leased.sh: WARNING — exiting WITHOUT releasing the build lease 'service': the tracked build (process group ${build_pgid:-$child_pgid}) is still alive. A future acquire will reclaim it as stale once that process actually exits; it is not being silently held forever, but it is also not released early." >&2
        return 0
    fi
    build_lease_release service
}
trap _release_if_safe EXIT

# _forward_signal <sig> — INT/TERM handler: forward to the child's own
# process group (the wrapper's default signal delivery never reaches it —
# see header), wait up to 30s for a graceful exit, escalate to KILL, reap
# it, then exit so the EXIT trap above (now safe — the group is actually
# dead) releases the lease instead of abandoning it.
_forward_signal() {
    local sig="$1"
    if [[ -z "$child_pgid" ]]; then
        # No child launched yet — nothing to forward to.
        exit 130
    fi
    _note_build_pgid
    echo "mvnw-leased.sh: received $sig — forwarding to the build (process group $child_pgid${build_pgid:+ and $build_pgid})..." >&2
    kill -s "$sig" -- "-$child_pgid" 2>/dev/null || true
    [[ -z "$build_pgid" ]] || kill -s "$sig" -- "-$build_pgid" 2>/dev/null || true
    local waited=0
    while _build_alive && (( waited < 30 )); do
        sleep 1
        waited=$((waited + 1))
    done
    if _build_alive; then
        echo "mvnw-leased.sh: build did not exit ${waited}s after $sig — escalating to KILL" >&2
        kill -9 -- "-$child_pgid" 2>/dev/null || true
        [[ -z "$build_pgid" ]] || kill -9 -- "-$build_pgid" 2>/dev/null || true
    fi
    wait "$child_pid" 2>/dev/null || true
    exit 130
}
trap '_forward_signal INT' INT
trap '_forward_signal TERM' TERM

cd "$repo_root/service"
if [[ -n "$lab_class" ]]; then
    rm -f "$build_pidfile"
    # The inner build: become a process-group leader, publish the pid, exec
    # mvnw (which execs java), so the pid and group are the build's own.
    lab-run -c "$lab_class" -- python3 -c '
import os, sys
os.setpgid(0, 0)
with open(sys.argv[1], "w") as f:
    f.write(f"{os.getpid()}\n")
os.execvp(sys.argv[2], sys.argv[2:])
' "$build_pidfile" ./mvnw "$@" &
else
    ./mvnw "$@" &
fi
child_pid=$!
child_pgid="$child_pid"
build_lease_track_pid service "$child_pgid"
if [[ -n "$lab_class" ]]; then
    # lab-run may queue for its grant first; pick up the build's group as soon
    # as it starts. A trap set during `sleep` runs when the sleep returns.
    while kill -0 "$child_pid" 2>/dev/null; do
        _note_build_pgid
        sleep 1
    done
fi
# `wait` on a specific pid returns that process's own exit status; under
# `set -e` a nonzero result here still exits this script with the same
# code (same propagation as the direct foreground call this replaces). By
# the time `wait` returns NORMALLY (not interrupted by a trap), the child
# has already exited on its own, so the EXIT trap's liveness check above
# is safe to release immediately.
wait "$child_pid"
