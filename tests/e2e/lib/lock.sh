#!/usr/bin/env bash
# tests/e2e/lib/lock.sh — POSIX-mkdir-based atomic lock primitive.
# RDR-184 P0.1 (nexus-ccs9v.1). Shared across e2e harnesses that guard a
# fixed, singleton resource (docker tag, sandbox dir, tmux session name) —
# per the "daemon-lifecycle fixes land in the shared primitive, never one
# tier's copy" convention (AGENTS.md), this is the ONE lock helper, not a
# per-harness copy.
#
# WHY mkdir, not flock: `flock` is absent on darwin (RDR-184 finding 3),
# the primary dev platform for this repo. `mkdir <path>` is POSIX-atomic on
# every filesystem this repo targets — verified live on darwin AND
# debian:trixie-slim (finding 3) — and needs no external binary at all.
#
# STALE-LOCK RECLAIM: mkdir gives no auto-release on crash, so a lockdir
# left behind by a dead holder must be detected and reclaimed rather than
# wedging every future acquirer forever. Liveness is `kill -0 <pid>` (a
# shell builtin — no `ps`/procps dependency: the migration-rehearsal
# package-upgrade image is DELIBERATELY built without procps to represent a
# real minimal-container deployment, see
# tests/e2e/migration-rehearsal/Dockerfile.package-upgrade).
#
# PID-REUSE MITIGATION (what we chose and why): `kill -0` alone cannot tell
# a still-running original holder from an unrelated process that happens to
# have recycled the same pid after the holder died — the dangerous
# direction is reclaiming a lock that a NEW, unrelated live process now
# holds under the recycled pid. To bound that, the lock also records a
# best-effort process START-TIME TOKEN alongside the pid, captured with
# whatever portable mechanism is available on the platform:
#   - Linux: /proc/<pid>/stat field 22 (starttime, in clock ticks since
#     boot) — read directly, no `ps` needed; /proc is a kernel mount
#     independent of the procps *package* (present on debian:trixie-slim
#     even without procps installed).
#   - Darwin (and any host with a `ps` binary): `ps -o lstart= -p <pid>` —
#     BSD ps ships with the base OS on macOS (it is not part of the procps
#     package), so it is always present there.
#   - Neither available: falls back to pid-only liveness. Documented
#     residual risk: an extremely narrow pid-reuse window could delay
#     reclaim of a truly-dead lock for one more retry/timeout cycle. This
#     can never produce two live holders — it only ever errs toward
#     treating a lock as still-held, the safe direction.
#
# API:
#   lock_acquire <lockdir-path> [timeout_s]
#       Returns 0 once the lock is held by this process ($$). Default
#       timeout_s=0 means a SINGLE attempt: if the lock is held by a live
#       process, this fails LOUD (nonzero, message on stderr) in well under
#       1s — it never silently queues. Pass a positive timeout_s to poll
#       (0.2s interval) for up to that many seconds before giving up.
#   lock_release <lockdir-path>
#       Returns 0 once released. Only the process that acquired the lock
#       (matching pid) may release it — releasing a lock this process does
#       not hold is a loud error (nonzero, message on stderr), never a silent
#       no-op.
#
# This file only defines functions — it does not set shell options, so
# sourcing it never changes the caller's `set -e`/`set -u`/etc.

# _lock_start_token <pid> — best-effort process start-time token; "unknown"
# if no portable mechanism is available. See PID-REUSE MITIGATION above.
_lock_start_token() {
    local pid="$1"
    if [[ -r "/proc/$pid/stat" ]]; then
        local stat rest
        stat="$(cat "/proc/$pid/stat" 2>/dev/null)" || stat=""
        if [[ -n "$stat" ]]; then
            # Fields: pid (comm) state ppid ... — comm can itself contain
            # ") ", so split at the LAST ") " to reach the numeric fields.
            rest="${stat##*) }"
            # shellcheck disable=SC2086 # intentional word-splitting into $N
            set -- $rest
            # rest field 1 == overall proc/stat field 3 (state); starttime
            # is overall field 22, i.e. rest field 20.
            if [[ -n "${20:-}" ]]; then
                echo "${20}"
                return 0
            fi
        fi
    fi
    if command -v ps >/dev/null 2>&1; then
        local lstart
        lstart="$(ps -o lstart= -p "$pid" 2>/dev/null | tr -s ' ' ' ')"
        if [[ -n "$lstart" ]]; then
            echo "$lstart"
            return 0
        fi
    fi
    echo "unknown"
}

# _lock_is_stale <lockdir> — 0 (stale, reclaimable) or 1 (live holder, or
# indeterminate — treated as live to stay on the safe side).
_lock_is_stale() {
    local lockdir="$1" holder_pid holder_token live_token
    holder_pid="$(cat "$lockdir/pid" 2>/dev/null || true)"
    if [[ -z "$holder_pid" ]]; then
        # Lockdir exists but the pid file hasn't landed yet (a concurrent
        # acquirer between its mkdir and its pid write) — not stale, just
        # not observable yet. Caller will retry.
        return 1
    fi
    if ! kill -0 "$holder_pid" 2>/dev/null; then
        return 0 # holder pid is dead -> stale
    fi
    holder_token="$(cat "$lockdir/start_token" 2>/dev/null || echo unknown)"
    live_token="$(_lock_start_token "$holder_pid")"
    if [[ "$holder_token" != "unknown" && "$live_token" != "unknown" && "$holder_token" != "$live_token" ]]; then
        return 0 # pid recycled by an unrelated process -> stale
    fi
    return 1 # genuinely live holder
}

# lock_acquire <lockdir-path> [timeout_s]
lock_acquire() {
    local lockdir="$1" timeout="${2:-0}" start_ts now holder_pid

    if [[ -z "$lockdir" ]]; then
        echo "lock_acquire: ERROR — lockdir-path is required" >&2
        return 2
    fi

    start_ts=$(date +%s)
    while true; do
        if mkdir "$lockdir" 2>/dev/null; then
            printf '%s\n' "$$" >"$lockdir/pid"
            _lock_start_token "$$" >"$lockdir/start_token"
            return 0
        fi

        if [[ -d "$lockdir" ]] && _lock_is_stale "$lockdir"; then
            rm -rf "$lockdir" 2>/dev/null
            continue # retry mkdir immediately, no timeout consumed
        fi

        now=$(date +%s)
        if ((now - start_ts >= timeout)); then
            holder_pid="$(cat "$lockdir/pid" 2>/dev/null || echo '?')"
            echo "lock_acquire: FAILED to acquire '$lockdir' (held by pid ${holder_pid})" >&2
            return 1
        fi
        sleep 0.2
    done
}

# lock_release <lockdir-path>
lock_release() {
    local lockdir="$1" holder_pid

    if [[ -z "$lockdir" ]]; then
        echo "lock_release: ERROR — lockdir-path is required" >&2
        return 2
    fi
    if [[ ! -d "$lockdir" ]]; then
        echo "lock_release: ERROR — '$lockdir' is not locked" >&2
        return 1
    fi

    holder_pid="$(cat "$lockdir/pid" 2>/dev/null || true)"
    if [[ "$holder_pid" != "$$" ]]; then
        echo "lock_release: ERROR — refusing to release '$lockdir': held by pid ${holder_pid:-?}, not this process ($$)" >&2
        return 1
    fi

    rm -rf "$lockdir"
}
