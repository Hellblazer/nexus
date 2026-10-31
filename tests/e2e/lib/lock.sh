#!/usr/bin/env bash
# tests/e2e/lib/lock.sh — directory-lock primitive (mv-claim, not mkdir-claim).
# RDR-184 P0.1 (nexus-ccs9v.1). Shared across e2e harnesses that guard a
# fixed, singleton resource (docker tag, sandbox dir, tmux session name) —
# per the "daemon-lifecycle fixes land in the shared primitive, never one
# tier's copy" convention (AGENTS.md), this is the ONE lock helper, not a
# per-harness copy.
#
# WHY no flock: `flock` is absent on darwin (RDR-184 finding 3), the primary
# dev platform for this repo. The primitive needs no external binary beyond
# mkdir and mv. mkdir(1) is NOT the claim: it is atomic on darwin and GNU
# coreutils (verified live on darwin AND debian:trixie-slim, finding 3), but
# uutils coreutils 0.8.0 (Ubuntu 26.04, chas) reports the EEXIST of a lost race
# as success (nexus-bo01z, nexus-x8sk8). So mkdir only makes a pid-unique
# STAGING directory; the claim is the `mv` of that fully populated directory
# onto the lock path, plus the pid readback after it (ACQUIRE-PATH ATOMICITY).
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
# LIVENESS UNDER PID NAMESPACES (nexus-fuz9t, lab-run #88): a pid is only
# meaningful inside the PID namespace that issued it. Two jobs on one pool
# host (the same user, a shared /tmp, so the same fixed lock root) can sit in
# different namespaces, where the other job's holder pid reads as "does not
# exist" and a live lock is reclaimed. On a host with flock(1) and bash 4.1+
# the holder therefore also takes flock(LOCK_EX) on `holder.flock` inside the
# lockdir and keeps the descriptor open until lock_release (or its death, which
# the kernel turns into a release). The lock is private to the lockdir inode, so
# the mv-claim and the reclaim rename carry it along. _lock_is_stale asks the
# lock first: held (or unreadable) is live, free is dead whatever the pid says,
# and a lockdir with no holder.flock (made by an older tree, or on darwin, where
# flock is absent) keeps the pid and start-token rule. The descriptor is
# inherited by the holder's children (bash cannot mark it close-on-exec), which
# errs towards keeping the lock: a daemon that outlives a crashed holder keeps
# it live until the daemon exits. A timeout against such a lock names the pids
# that actually hold holder.flock (/proc, Linux), not only the recorded one.
# Reclaim re-probes the flock of the dir it renamed away before it removes it
# (a pid number alone does not identify a lock across namespaces).
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

# Holder-side flock (see LIVENESS UNDER PID NAMESPACES above).
_LOCK_FLOCK_FILE="holder.flock"
#: "lockdir|fd" for every flock this shell holds.
_LOCK_FD_REG=()
#: Set by _lock_flock_take: the descriptor it opened, or empty.
_LOCK_NEW_FD=""

# _lock_flock_ok — flock(1) is installed and this bash has the {var} redirection.
_lock_flock_ok() {
    (( BASH_VERSINFO[0] > 4 || (BASH_VERSINFO[0] == 4 && BASH_VERSINFO[1] >= 1) )) || return 1
    command -v flock >/dev/null 2>&1
}

# _lock_flock_take <staging-dir> — create <dir>/holder.flock, open it and take
# LOCK_EX on it BEFORE the dir is moved into place, so a lockdir is never
# visible with a lock file nobody holds yet. Leaves the descriptor in
# _LOCK_NEW_FD (empty where there is no flock). A failure to take it is not an
# error: the lockdir is then a pid-rule lockdir.
_lock_flock_take() {
    _LOCK_NEW_FD=""
    _lock_flock_ok || return 0
    local fd
    if ! { exec {fd}>"$1/$_LOCK_FLOCK_FILE"; } 2>/dev/null; then
        return 0
    fi
    if ! flock -n "$fd" 2>/dev/null; then
        exec {fd}>&-
        rm -f "$1/$_LOCK_FLOCK_FILE"
        return 0
    fi
    _LOCK_NEW_FD="$fd"
}

# _lock_flock_drop_new — close the descriptor _lock_flock_take opened (the claim lost).
_lock_flock_drop_new() {
    local fd="$_LOCK_NEW_FD"
    _LOCK_NEW_FD=""
    [[ -n "$fd" ]] || return 0
    exec {fd}>&-
}

# _lock_fd_register <lockdir> <fd> / _lock_fd_close <lockdir>
_lock_fd_register() { _LOCK_FD_REG+=("$1|$2"); }
_lock_fd_close() {
    local entry fd keep=()
    for entry in ${_LOCK_FD_REG[@]+"${_LOCK_FD_REG[@]}"}; do
        if [[ "${entry%|*}" == "$1" ]]; then
            fd="${entry##*|}"
            exec {fd}>&-
        else
            keep+=("$entry")
        fi
    done
    _LOCK_FD_REG=(${keep[@]+"${keep[@]}"})
}

# _lock_flock_state <lockdir> — what <lockdir>/holder.flock says about the holder:
# "none" (no lock file: a pid-rule lockdir), "live" (the lock is held),
# "dead" (it was free), "unknown" (a lock file is there but cannot be tried;
# the caller treats that as live). The probe holds the lock for the length of
# `true`, so two reclaimers probing at once may each read "live" and retry.
_lock_flock_state() {
    local f="$1/$_LOCK_FLOCK_FILE" rc=0
    [[ -e "$f" ]] || { echo none; return 0; }
    _lock_flock_ok || { echo unknown; return 0; }
    flock -n -E 200 "$f" true 2>/dev/null || rc=$?
    case "$rc" in
        0) echo dead ;;
        200) echo live ;;
        *) echo unknown ;;
    esac
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
    # The holder's flock decides when there is one: a pid from another PID
    # namespace reads as nonexistent here (nexus-fuz9t).
    case "$(_lock_flock_state "$lockdir")" in
        live | unknown) return 1 ;;
        dead) return 0 ;;
    esac
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

# _lock_flock_holders <lockdir> — the pids (one per line) that hold a descriptor on
# <lockdir>/holder.flock, read from /proc/*/fd. Linux only; prints nothing elsewhere or
# for processes this user cannot inspect.
_lock_flock_holders() {
    local f="$1/$_LOCK_FLOCK_FILE" d pid
    [[ -d /proc/self/fd && -e "$f" ]] || return 0
    for d in /proc/[0-9]*/fd/*; do
        [[ "$d" -ef "$f" ]] || continue
        pid="${d#/proc/}"
        echo "${pid%%/*}"
    done 2>/dev/null | sort -un
}

# _lock_explain_held <lockdir> <recorded-pid> — on a timeout, say who really holds a lock
# whose recorded pid names nothing visible here. The pid in the message is the one the
# holder wrote; under PID namespaces, or when a daemon the holder started kept its inherited
# descriptor after the holder died, it points at nothing, and the operator needs the pids
# that hold the lock (nexus-fuz9t).
_lock_explain_held() {
    local lockdir="$1" recorded="$2" pids pid
    [[ "$recorded" != '?' ]] && _lock_pid_exists "$recorded" && return 0
    [[ "$(_lock_flock_state "$lockdir")" == live ]] || return 0
    echo "lock_acquire: pid ${recorded} is not a process here, but '$lockdir/$_LOCK_FLOCK_FILE' is held;" \
        "the lock is live until every process holding that file exits." >&2
    pids="$(_lock_flock_holders "$lockdir")"
    if [[ -n "$pids" ]]; then
        for pid in $pids; do
            echo "lock_acquire:   held via pid ${pid}: $(tr '\0' ' ' <"/proc/$pid/cmdline" 2>/dev/null)" >&2
        done
        echo "lock_acquire: if none of these is a run you want, stop them (an orphaned child of a crashed" \
            "holder keeps an inherited descriptor); the lock frees when the last one exits." >&2
    else
        echo "lock_acquire: no holder is visible from here (another job's PID namespace, or another user);" \
            "wait for that job to end." >&2
    fi
}

# lock_acquire <lockdir-path> [timeout_s]
lock_acquire() {
    local lockdir="$1" timeout="${2:-0}" start_ts now holder_pid
    local stale_holder_pid claim captured_pid fresh claim_flock

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
        # This mkdir picks a pid-unique staging name; it is NOT the claim, so
        # mkdir(1) being non-exclusive under a race (uutils coreutils 0.8.0,
        # nexus-x8sk8) cannot admit a second holder. The claim is the
        # `mv "$fresh" "$lockdir"` below plus the pid readback after it.
        if mkdir "$fresh" 2>/dev/null; then
            printf '%s\n' "$$" >"$fresh/pid"
            _lock_start_token "$$" >"$fresh/start_token"
            _lock_flock_take "$fresh"
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
                    [[ -z "$_LOCK_NEW_FD" ]] || _lock_fd_register "$lockdir" "$_LOCK_NEW_FD"
                    _LOCK_NEW_FD=""
                    return 0
                fi
                # The mv "succeeded" (exit 0) but $lockdir/pid doesn't
                # show our own pid — we nested inside a directory that
                # raced into existence in the vacancy-check-to-mv gap.
                # Do not claim success. Clean up ONLY our own nested
                # artifact (never the other occupant's content) and
                # retry from the top.
                _lock_flock_drop_new
                rm -rf "${lockdir:?}/$(basename "$fresh")" 2>/dev/null
            else
                # Lost the race outright (something occupied $lockdir
                # between our check and our mv, or the mv itself
                # failed) — our populated temp dir is private and
                # nobody else can see it; discard and retry.
                _lock_flock_drop_new
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
                # Identity by pid text is not enough: pids repeat across PID namespaces, so a
                # live holder that landed at $lockdir after our verdict can carry the dead
                # holder's pid number. The dir we hold now is private, so its flock cannot be
                # taken by anyone new; if it is held (or cannot be tried), it is not the stale
                # lock we judged and goes back (nexus-fuz9t).
                claim_flock="$(_lock_flock_state "$claim")"
                if [[ "$captured_pid" == "$stale_holder_pid" && "$claim_flock" != live && "$claim_flock" != unknown ]]; then
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
            _lock_explain_held "$lockdir" "$holder_pid" >&2
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

    # Remove first, close after: the flock keeps a reclaimer out until the dir is gone. The
    # status that comes back is the removal's, not the close's: a lock that is still on disk
    # must not read as released (it frees itself for the next contender, its flock now being
    # dropped, but the caller has to know).
    local rc=0
    rm -rf "$lockdir" || rc=$?
    _lock_fd_close "$lockdir"
    if ((rc != 0)); then
        echo "lock_release: ERROR — could not remove '$lockdir' (rm exited $rc); its flock is released, so the next contender will reclaim it" >&2
    fi
    return "$rc"
}
