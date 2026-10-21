#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# scripts/lib/leased-build.sh — the build-under-a-held-lease machinery that
# scripts/mvnw-leased.sh and scripts/build-gate-jar.sh share (nexus-t6t9f): the
# `--lab-run <class>` argument checks, the inner build run as its own process
# group recorded as the lease's pgid, and the signal handling that keeps the
# lease held while that build is alive. Source it AFTER scripts/lib/build-lease.sh
# and AFTER the lease is held; the caller acquires.
#
# WHY A PROCESS GROUP. Under `set -m` a backgrounded job is its own process-group
# leader, so SIGINT or SIGTERM sent to the wrapper alone never reaches the build.
# A plain `trap release EXIT` would then free the lease while the build still
# writes service/target (nexus-c00dw round 3). So:
#   1. The EXIT trap releases only when the tracked build is confirmed dead, or
#      was never launched. A live build leaves the lease HELD with a WARNING
#      naming the group; a later acquire reclaims it once that group exits.
#   2. INT, TERM, HUP and QUIT are forwarded to the build's group, waited for (30s),
#      escalated to KILL, re-polled for up to 3s, and only then does the wrapper
#      exit, so the EXIT trap above runs with the group dead. HUP matters: a closed
#      terminal or dropped ssh session sends it, and untrapped it would kill the
#      wrapper with the build alive and the finish steps skipped.
#   3. Once the build's group is gone the lease stops naming it (a dead pgid reads
#      as a stale lease, and a waiting peer would reclaim it while the wrapper is
#      still storing a cache entry or restoring a file); liveness is the wrapper's
#      own pid again until the finish command releases. A group member that outlived
#      the leader keeps the lease tracked, and rule 1 applies.
# Under --lab-run the build runs further down (sudo, a systemd scope) in a group
# that a signal to lab-run need not reach, so the inner command makes itself a
# group leader and publishes its pid; that group is tracked too and is what the
# lease's pgid names once it exists (nexus-iqmih).
#
# API (state is global, one leased build per process):
#   leased_build_check_lab_run <script> <class>
#       Validate `--lab-run <class>`: exit 64 for a missing class or one that
#       starts with `-`, exit 64 inside a lab-run grant (LAB_CPUS set: a nested
#       grant can wait forever on a full pool while holding the first), exit 127
#       when lab-run is not on PATH. Call it BEFORE taking the lease.
#   leased_build_init <script> <lease-name>
#       After a successful acquire: initialise the state and install the EXIT,
#       INT, TERM, HUP and QUIT traps. Replaces any EXIT trap the caller set.
#   LEASED_BUILD_FINISH=(cmd args...)
#       What the EXIT trap runs once the build is gone. Default: release the
#       lease. A caller with more to undo (build-gate-jar.sh restores
#       release.properties) sets it to a command that does that AND releases.
#   leased_build_run <lab-class-or-empty> <cmd> [args...]
#       Run the build in the background as its own group, track it as the lease's
#       pgid, wait, stop tracking the group once it is gone, and return its exit status. With a class the command runs as
#       `lab-run -c <class> -- <cmd>`.

# shellcheck shell=bash

# Fail fast when sourced out of order: every function below needs these.
if ! declare -F build_lease_track_pid >/dev/null || ! declare -F build_lease_untrack_pid >/dev/null; then
    echo "leased-build.sh: source scripts/lib/build-lease.sh first (build_lease_track_pid / build_lease_untrack_pid are undefined)" >&2
    exit 70
fi

_LEASED_BUILD_SCRIPT=""
_LEASED_BUILD_LEASE=""
_leased_build_child_pid=""
_leased_build_child_pgid=""
_leased_build_pgid=""
_leased_build_pidfile=""
LEASED_BUILD_FINISH=()

leased_build_check_lab_run() {
    local script="$1" class="${2:-}"
    if [[ -z "$class" || "$class" == -* ]]; then
        echo "$script: --lab-run needs a class (lab-s, lab-m, lab-l, ...)" >&2
        exit 64
    fi
    if [[ -n "${LAB_CPUS:-}" ]]; then
        echo "$script: --lab-run given inside a lab-run grant (LAB_CPUS is set); run it outside lab-run, or drop --lab-run" >&2
        exit 64
    fi
    if ! command -v lab-run >/dev/null 2>&1; then
        echo "$script: --lab-run $class given but lab-run is not on PATH" >&2
        exit 127
    fi
}

_leased_build_alive() {
    { [[ -n "$_leased_build_child_pgid" ]] && _build_lease_group_alive "$_leased_build_child_pgid" 2>/dev/null; } ||
        { [[ -n "$_leased_build_pgid" ]] && _build_lease_group_alive "$_leased_build_pgid" 2>/dev/null; }
}

_leased_build_note_pgid() {
    if [[ -z "$_leased_build_pgid" && -s "$_leased_build_pidfile" ]]; then
        _leased_build_pgid="$(cat "$_leased_build_pidfile" 2>/dev/null || true)"
        [[ "$_leased_build_pgid" =~ ^[0-9]+$ ]] || { _leased_build_pgid=""; return 0; }
        build_lease_track_pid "$_LEASED_BUILD_LEASE" "$_leased_build_pgid"
    fi
}

# The EXIT trap. Never releases while the tracked build is alive.
_leased_build_release_if_safe() {
    _leased_build_note_pgid
    if _leased_build_alive; then
        echo "$_LEASED_BUILD_SCRIPT: WARNING — exiting WITHOUT releasing the build lease '$_LEASED_BUILD_LEASE': the tracked build (process group ${_leased_build_pgid:-$_leased_build_child_pgid}) is still alive. The finish steps were skipped too, so anything this script stamped (release.properties) stays as it is until a later run or a hand restore. A future acquire will reclaim the lease as stale once that process actually exits; it is not being silently held forever, but it is also not released early." >&2
        return 0
    fi
    "${LEASED_BUILD_FINISH[@]}"
}

# INT/TERM/HUP/QUIT handler: forward to the build's groups, wait up to 30s, escalate to
# KILL, reap, exit so the EXIT trap (now safe: the group is dead) releases.
_leased_build_forward_signal() {
    local sig="$1"
    if [[ -z "$_leased_build_child_pgid" ]]; then
        # No child launched yet — nothing to forward to.
        exit 130
    fi
    _leased_build_note_pgid
    echo "$_LEASED_BUILD_SCRIPT: received $sig — forwarding to the build (process group $_leased_build_child_pgid${_leased_build_pgid:+ and $_leased_build_pgid})..." >&2
    kill -s "$sig" -- "-$_leased_build_child_pgid" 2>/dev/null || true
    [[ -z "$_leased_build_pgid" ]] || kill -s "$sig" -- "-$_leased_build_pgid" 2>/dev/null || true
    local waited=0
    while _leased_build_alive && (( waited < 30 )); do
        sleep 1
        waited=$((waited + 1))
    done
    if _leased_build_alive; then
        echo "$_LEASED_BUILD_SCRIPT: build did not exit ${waited}s after $sig — escalating to KILL" >&2
        kill -9 -- "-$_leased_build_child_pgid" 2>/dev/null || true
        [[ -z "$_leased_build_pgid" ]] || kill -9 -- "-$_leased_build_pgid" 2>/dev/null || true
        # KILL is asynchronous: give the group a moment to be reaped, so the EXIT
        # trap finds it dead and runs the finish steps rather than skipping them.
        local killed=0
        while _leased_build_alive && (( killed < 3 )); do
            sleep 1
            killed=$((killed + 1))
        done
    fi
    wait "$_leased_build_child_pid" 2>/dev/null || true
    exit 130
}

leased_build_init() {
    _LEASED_BUILD_SCRIPT="${1:?leased_build_init: usage: leased_build_init <script> <lease-name>}"
    _LEASED_BUILD_LEASE="${2:?leased_build_init: usage: leased_build_init <script> <lease-name>}"
    _leased_build_child_pid=""
    _leased_build_child_pgid=""
    _leased_build_pgid=""
    _leased_build_pidfile="$(_build_lease_dir "$_LEASED_BUILD_LEASE")/build_pid"
    LEASED_BUILD_FINISH=(build_lease_release "$_LEASED_BUILD_LEASE")
    trap _leased_build_release_if_safe EXIT
    trap '_leased_build_forward_signal INT' INT
    trap '_leased_build_forward_signal TERM' TERM
    trap '_leased_build_forward_signal HUP' HUP
    trap '_leased_build_forward_signal QUIT' QUIT
}

leased_build_run() {
    local lab_class="$1"
    shift
    set -m
    if [[ -n "$lab_class" ]]; then
        rm -f "$_leased_build_pidfile"
        # The inner build: become a process-group leader, publish the pid, exec
        # the command (mvnw execs java), so the pid and group are the build's own.
        lab-run -c "$lab_class" -- python3 -c '
import os, sys
os.setpgid(0, 0)
with open(sys.argv[1], "w") as f:
    f.write(f"{os.getpid()}\n")
os.execvp(sys.argv[2], sys.argv[2:])
' "$_leased_build_pidfile" "$@" &
    else
        "$@" &
    fi
    _leased_build_child_pid=$!
    _leased_build_child_pgid="$_leased_build_child_pid"
    build_lease_track_pid "$_LEASED_BUILD_LEASE" "$_leased_build_child_pgid"
    if [[ -n "$lab_class" ]]; then
        # lab-run may queue for its grant first; pick up the build's group as soon
        # as it starts. A trap set during `sleep` runs when the sleep returns.
        while kill -0 "$_leased_build_child_pid" 2>/dev/null; do
            _leased_build_note_pgid
            sleep 1
        done
    fi
    # `wait` on a specific pid returns that process's own exit status. By the
    # time it returns normally (not interrupted by a trap) the child has exited.
    # Capture the status (a bare failing `wait` would exit under set -e before the
    # steps below), then stop tracking the dead group: the caller still has work
    # under the lease, and a lease whose pgid names a dead group reads as stale.
    local rc=0
    wait "$_leased_build_child_pid" || rc=$?
    set +m
    # Only a group that is really gone is forgotten. A member that outlived the
    # leader (a daemon the build started) keeps the lease tracked: the EXIT trap
    # then leaves it held with a WARNING instead of releasing under a live writer.
    if ! _leased_build_alive; then
        build_lease_untrack_pid "$_LEASED_BUILD_LEASE"
        rm -f "$_leased_build_pidfile"
        _leased_build_child_pgid=""
        _leased_build_pgid=""
    fi
    return "$rc"
}
