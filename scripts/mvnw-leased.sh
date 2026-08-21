#!/usr/bin/env bash
# scripts/mvnw-leased.sh — thin wrapper around service/mvnw that takes the
# nexus-c00dw single-builder lease first, so an orchestrator invocation and
# a developer-agent invocation of ./mvnw never write service/target at the
# same time (see scripts/lib/build-lease.sh for the mechanism and the
# incident that motivated it).
#
# usage: scripts/mvnw-leased.sh [mvnw args...]
#
# Exits 75 (EX_TEMPFAIL) without running mvnw at all if another live process
# already holds the lease — the refusal message names that holder.
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

build_lease_acquire service ./mvnw "$@"

# Initialized before any trap is installed, so a trap can always reference
# it safely under `set -u` even if a signal lands in the brief window
# before the real build is launched below.
child_pgid=""
child_pid=""

# _release_if_safe — the EXIT trap. Never releases while the tracked build
# is still alive (round-3 fix); an unconditional release here is exactly
# what let a SIGTERM'd wrapper free the lease out from under a
# still-running child.
_release_if_safe() {
    if [[ -n "$child_pgid" ]] && _build_lease_group_alive "$child_pgid" 2>/dev/null; then
        echo "mvnw-leased.sh: WARNING — exiting WITHOUT releasing the build lease 'service': the tracked build (process group $child_pgid) is still alive. A future acquire will reclaim it as stale once that process actually exits; it is not being silently held forever, but it is also not released early." >&2
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
    echo "mvnw-leased.sh: received $sig — forwarding to the build (process group $child_pgid)..." >&2
    kill -s "$sig" -- "-$child_pgid" 2>/dev/null || true
    local waited=0
    while _build_lease_group_alive "$child_pgid" 2>/dev/null && (( waited < 30 )); do
        sleep 1
        waited=$((waited + 1))
    done
    if _build_lease_group_alive "$child_pgid" 2>/dev/null; then
        echo "mvnw-leased.sh: build did not exit ${waited}s after $sig — escalating to KILL" >&2
        kill -9 -- "-$child_pgid" 2>/dev/null || true
    fi
    wait "$child_pid" 2>/dev/null || true
    exit 130
}
trap '_forward_signal INT' INT
trap '_forward_signal TERM' TERM

cd "$repo_root/service"
./mvnw "$@" &
child_pid=$!
child_pgid="$child_pid"
build_lease_track_pid service "$child_pgid"
# `wait` on a specific pid returns that process's own exit status; under
# `set -e` a nonzero result here still exits this script with the same
# code (same propagation as the direct foreground call this replaces). By
# the time `wait` returns NORMALLY (not interrupted by a trap), the child
# has already exited on its own, so the EXIT trap's liveness check above
# is safe to release immediately.
wait "$child_pid"
