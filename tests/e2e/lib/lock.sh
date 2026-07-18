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
# wedging every future acquirer forever. Liveness is a permission-
# independent existence probe (see PID-EXISTENCE PROBE below), never
# `kill -0` alone: `kill -0 <pid>` cannot distinguish ESRCH (no such
# process — genuinely dead) from EPERM (process exists and is alive, but
# the caller lacks permission to signal it, e.g. a different user or a
# hardened-runtime sandbox) — RDR-184 P0 review found this conflation
# lets a live-but-unsignalable holder be misclassified as dead
# (empirically reproduced; see nexus-ccs9v.5 critique).
#
# PID-EXISTENCE PROBE: process existence, independent of signal
# permission — no `ps`/procps dependency where /proc is available (the
# migration-rehearsal package-upgrade image is DELIBERATELY built without
# procps to represent a real minimal-container deployment, see
# tests/e2e/migration-rehearsal/Dockerfile.package-upgrade):
#   - Linux (or anywhere /proc is mounted): `[[ -d /proc/<pid> ]]` — a
#     directory-traversal check only, never gated by signal permission.
#   - Darwin (and any host with a `ps` binary, no /proc): `ps -p <pid>` —
#     a process-table lookup, likewise not signal-permission-gated. BSD ps
#     ships with the base OS on macOS (it is not part of the procps
#     package), so it is always present there.
#   - Neither available: falls back to `kill -0` as a last resort, with
#     the residual EPERM/ESRCH ambiguity documented as before (this branch
#     is only reached on a host with neither /proc nor any `ps` binary).
#
# PID-REUSE MITIGATION (what we chose and why): pid-existence alone cannot
# tell a still-running original holder from an unrelated process that
# happens to have recycled the same pid after the holder died — the
# dangerous direction is reclaiming a lock that a NEW, unrelated live
# process now holds under the recycled pid. To bound that, the lock also
# records a best-effort process START-TIME TOKEN alongside the pid,
# captured with whatever portable mechanism is available on the platform:
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
# RECLAIM ATOMICITY (RDR-184 P0 review C1/C2, hardened in a re-round after
# the first fix introduced a WORSE bug — see ACQUIRE-PATH ATOMICITY below
# for the root cause): the reclaim step CANNOT be a plain
# check-then-`rm -rf` — two contenders that both read the same stale
# lockdir and then act on that verdict independently can otherwise both
# believe they hold the lock (C1), and an `rm -rf` that fails for any
# reason (foreign owner, RO mount) previously spun the whole loop
# sleeplessly forever, ignoring `timeout_s` entirely (C2). The fix is an
# atomic `mv` claim, POST-VERIFIED: `mv` (rename(2)) is atomic, so of any
# number of contenders racing to rename the SAME still-present stale
# lockdir, only one can win. But rename doesn't check identity — it moves
# whatever is CURRENTLY at the source path — so a contender delayed (by
# ordinary OS scheduling) between its staleness read and its own `mv`
# statement could still, without a second check, capture a DIFFERENT,
# freshly-(re)claimed live lock that appeared at that path in the
# meantime (exactly the "B, scheduled back in, already decided stale,
# never re-validates" scenario the original finding described). The
# post-capture identity check partially closes that: after a successful
# `mv`, the pid now sitting in the private captured copy is compared
# against the pid this contender actually decided was stale (threaded
# through from `_lock_is_stale`'s own read — see THREADED-PID below, not
# re-derived via a second independent `cat`); a match means this
# contender's rename really did capture the stale lock (safe to discard);
# a mismatch means it accidentally captured someone else's fresh lock.
#
# HONEST RESIDUAL (this does NOT fully close the mismatch case): restoring
# the mismatched capture (`mv` it back) is itself another rename that can
# lose a race to a THIRD contender's fresh `mkdir` landing in the vacancy
# left by our own capture — and `mv` onto an EXISTING directory NESTS
# rather than fails or replaces (verified empirically; this is `mv`'s own
# move-into-existing-directory convention, not a rename(2) restriction).
# The restore path below re-checks vacancy immediately before attempting
# the restore and, on ANY sign the check-then-restore itself lost that
# narrower race (target occupied at the check, or the post-restore
# identity readback doesn't show what we just tried to put there),
# DISCARDS rather than risks nesting — losing visibility of a lock this
# contender was never entitled to reclaim in the first place is the
# lesser evil versus silently corrupting or double-granting someone
# else's. This narrows the window from "however long a contender can be
# scheduled out" down to a single check-then-act pair (comparable in
# class to the THREADED-PID note below), not zero — a true fix would need
# a generation-counter/CAS redesign; tracked as a follow-up, not blocking,
# since the residual is self-healing (the next contender's ordinary
# staleness check reclaims it) and never produces the permanent-wedge or
# silent-corruption failure modes this round closed.
#
# THREADED-PID (RDR-184 P0 re-round HIGH): `_lock_is_stale` echoes the
# exact pid value it inspected to reach its verdict; `lock_acquire`
# captures that via `stale_holder_pid="$(_lock_is_stale "$lockdir")"`
# rather than re-`cat`-ing the pid file a second time afterward. A second
# independent read would leave a window where an intervening contender's
# full reclaim-discard-mkdir-write cycle lands between the two reads,
# making them agree "by construction" on that OTHER contender's fresh pid
# and defeating the whole point of the identity check.
#
# ACQUIRE-PATH ATOMICITY (RDR-184 P0 re-round CRITICAL, root cause of a
# NEW bug the first C1/C2 fix introduced): the fresh-acquire success path
# used to be `mkdir` (atomic) followed by TWO SEPARATE, non-atomic writes
# (`pid`, then `start_token`) — a real window in which the lockdir exists
# but is not yet (fully) populated. A delayed reclaimer's capture-mv
# landing in exactly that window captures an EMPTY or half-written
# directory; reading an empty pid mismatches whatever stale identity the
# reclaimer expected, so it takes the "restore" branch and puts an EMPTY
# directory back at `$lockdir`. The true acquirer's own `return 0` still
# fires (bash does not abort on a failed/misdirected write without
# `set -e`), so it believes it holds the lock — but `$lockdir/pid` is
# empty. `_lock_is_stale` on an empty pid file correctly returns
# "not observable yet, retry" — meaning NO future contender ever
# reclaims it (never classified stale) and the true acquirer's own
# `lock_release` refuses (pid file doesn't match `$$`) — a PERMANENT,
# unreclaimable, unreleasable wedge needing manual `rm -rf`, strictly
# worse than the C1 bug it replaced (which at least self-resolved once
# either holder released). The fix: populate a PRIVATE, uniquely-named
# temp directory with BOTH `pid` and `start_token` FIRST, then claim the
# real `$lockdir` path via a single atomic `mv` of the whole,
# already-fully-populated directory — `$lockdir` is never observable
# with content present but incomplete. Guarded the same way as the
# reclaim-restore path above (vacancy check + post-claim identity
# readback; any sign of a lost race or nesting discards rather than
# trusts a bare `mv` exit code, since nesting itself exits 0).
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

# _lock_pid_exists <pid> — permission-independent existence probe. See
# PID-EXISTENCE PROBE above: never relies on `kill -0` alone, since a
# nonzero `kill -0` conflates "no such process" (ESRCH, genuinely dead)
# with "process exists but I can't signal it" (EPERM, alive). Returns 0
# if the pid demonstrably exists, 1 if it demonstrably does not.
_lock_pid_exists() {
    local pid="$1"
    if [[ -d /proc ]]; then
        [[ -d "/proc/$pid" ]]
        return $?
    fi
    if command -v ps >/dev/null 2>&1; then
        ps -p "$pid" >/dev/null 2>&1
        return $?
    fi
    # Last resort only (no /proc, no ps binary at all): residual
    # EPERM/ESRCH ambiguity applies here, same as before this fix.
    kill -0 "$pid" 2>/dev/null
}

# _lock_is_stale <lockdir> — 0 (stale, reclaimable) or 1 (live holder, or
# indeterminate — treated as live to stay on the safe side). ALWAYS
# echoes the pid value it actually inspected (possibly empty) to stdout
# before returning — see THREADED-PID above. Callers that only want the
# boolean verdict must redirect stdout (`_lock_is_stale "$d" >/dev/null`);
# callers that need the threaded pid capture it via
# `pid="$(_lock_is_stale "$d")"`, which preserves the function's own exit
# code on the assignment.
_lock_is_stale() {
    local lockdir="$1" holder_pid holder_token live_token
    holder_pid="$(cat "$lockdir/pid" 2>/dev/null || true)"
    echo "$holder_pid"
    if [[ -z "$holder_pid" ]]; then
        # Lockdir exists but the pid file hasn't landed yet (a concurrent
        # acquirer between its mkdir and its pid write) — not stale, just
        # not observable yet. Caller will retry.
        return 1
    fi
    if ! _lock_pid_exists "$holder_pid"; then
        return 0 # holder pid demonstrably does not exist -> stale
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
    local stale_holder_pid claim captured_pid fresh

    if [[ -z "$lockdir" ]]; then
        echo "lock_acquire: ERROR — lockdir-path is required" >&2
        return 2
    fi

    start_ts=$(date +%s)
    while true; do
        # RDR-184 P0 re-round CRITICAL fix: see ACQUIRE-PATH ATOMICITY at
        # the top of this file. Populate a PRIVATE, uniquely-named temp
        # dir with BOTH pid and start_token FIRST, then claim the real
        # path via ONE atomic mv of the already-fully-populated
        # directory — $lockdir is never observable with content present
        # but incomplete. `[[ ! -e "$lockdir" ]]` right before the mv
        # guards the common case (mv onto an EXISTING directory nests
        # rather than fails); the post-claim identity readback below
        # catches the rarer case where that check-then-mv itself lost a
        # race in the single-statement gap, since a nesting mv still
        # exits 0 and must not be trusted blindly.
        fresh="$lockdir.fresh.$$"
        if mkdir "$fresh" 2>/dev/null; then
            printf '%s\n' "$$" >"$fresh/pid"
            _lock_start_token "$$" >"$fresh/start_token"
            if [[ ! -e "$lockdir" ]] && mv "$fresh" "$lockdir" 2>/dev/null; then
                # Test-only seam (RDR-184 P0 re-round Test 5b): widen the
                # gap between the claiming mv succeeding and this
                # process's own verification read below, so a test that
                # has forced a nesting outcome can observe the nested
                # state before this verification acts on it, instead of
                # relying on scheduler luck. No-op (empty var) in every
                # normal invocation.
                if [[ -n "${NX_LOCK_TEST_WIDEN_WINDOW:-}" ]]; then
                    sleep "${NX_LOCK_TEST_WIDEN_SECONDS:-1}"
                fi
                if [[ "$(cat "$lockdir/pid" 2>/dev/null)" == "$$" ]]; then
                    return 0
                fi
                # The mv "succeeded" (exit 0) but $lockdir/pid doesn't
                # show our own pid — we nested inside a directory that
                # raced into existence in the vacancy-check-to-mv gap.
                # Do not claim success. Clean up ONLY our own nested
                # artifact (never the other occupant's content) and
                # retry from the top.
                rm -rf "${lockdir:?}/$(basename "$fresh")" 2>/dev/null
            else
                # Lost the race outright (something occupied $lockdir
                # between our check and our mv, or the mv itself
                # failed) — our populated temp dir is private and
                # nobody else can see it; discard and retry.
                rm -rf "$fresh" 2>/dev/null
            fi
        fi

        if [[ -d "$lockdir" ]] && stale_holder_pid="$(_lock_is_stale "$lockdir")"; then
            # RDR-184 P0 review C1/C2, re-round hardened — see the
            # RECLAIM ATOMICITY comment at the top of this file.
            # `stale_holder_pid` is threaded from `_lock_is_stale`'s own
            # read (THREADED-PID), not re-derived via a second
            # independent `cat` of the same path.
            claim="$lockdir.reclaim.$$"
            if mv "$lockdir" "$claim" 2>/dev/null; then
                captured_pid="$(cat "$claim/pid" 2>/dev/null || true)"
                if [[ "$captured_pid" == "$stale_holder_pid" ]]; then
                    # Captured exactly the stale lock this contender
                    # observed — safe to discard and retry mkdir. Only
                    # the mv-winner ever reaches this rm; a losing
                    # contender's mv fails outright (see below) and never
                    # touches this path at all.
                    rm -rf "$claim" 2>/dev/null
                    continue # retry mkdir immediately, no timeout consumed
                fi
                # Captured a DIFFERENT (fresher, possibly live) holder's
                # lock by accident. Restore it rather than destroy it —
                # but ONLY if $lockdir is genuinely vacant right now: `mv`
                # onto an EXISTING directory nests rather than fails or
                # replaces (HONEST RESIDUAL above), so a lost race here
                # must discard, never attempt a restore that could nest
                # our mismatched capture inside a third contender's fresh
                # claim.
                if [[ ! -e "$lockdir" ]]; then
                    # Test-only seam (RDR-184 P0 re-round Test 9): widen
                    # the gap between THIS vacancy check and the restore
                    # mv immediately below, so a test can deterministically
                    # land a THIRD contender's fresh mkdir into it (the
                    # actual TOCTOU this check narrows but cannot close
                    # outright — see HONEST RESIDUAL above). No-op (empty
                    # var) in every normal invocation.
                    if [[ -n "${NX_LOCK_TEST_WIDEN_WINDOW:-}" ]]; then
                        sleep "${NX_LOCK_TEST_WIDEN_SECONDS:-1}"
                    fi
                    if mv "$claim" "$lockdir" 2>/dev/null; then
                        if [[ "$(cat "$lockdir/pid" 2>/dev/null)" != "$captured_pid" ]]; then
                            # Restore "succeeded" (exit 0) but a third
                            # contender's fresh claim landed in the
                            # widened gap and we nested inside IT instead
                            # of replacing vacancy — clean up ONLY our own
                            # nested artifact, never their content.
                            rm -rf "${lockdir:?}/$(basename "$claim")" 2>/dev/null
                        fi
                    else
                        rm -rf "$claim" 2>/dev/null
                    fi
                else
                    # Already re-occupied at the check — discard rather
                    # than risk nesting. Losing visibility of a lock this
                    # contender was never entitled to reclaim is the
                    # lesser evil.
                    rm -rf "$claim" 2>/dev/null
                fi
            fi
            # mv failed outright (another contender's mv already won this
            # exact stale dir — rename(2) only lets one such rename
            # succeed), or we resolved a mismatched capture just above —
            # fall through to the timeout/sleep path below. This
            # contender must not act any further on a stale verdict it
            # can no longer corroborate, and the loop must still
            # terminate even if reclaim never succeeds (e.g. mv
            # permanently failing under a foreign owner / RO mount).
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
