#!/usr/bin/env bash
# scripts/lib/build-lease.sh — single-builder lease for the service/
# Maven build (bead nexus-c00dw). Sourced, never executed.
#
# WHY THIS EXISTS (incident 2026-08-20, T2 nexus/lessons-coordination-2026-08-20):
# the orchestrator ran scripts/build-gate-jar.sh while a developer agent was
# running ./mvnw in the same checkout; both wrote service/target concurrently
# and the build failed with "package dev.nexus.service.jooq.nexus does not
# exist" — Liquibase had applied cleanly, but the jOOQ generated sources were
# clobbered mid-build by the second writer. service/target is single-writer
# state, the same class of hazard as the shared git tree; this is that guard,
# scoped to the build output directory instead of the working tree.
#
# NO T1 DEPENDENCY, BY DESIGN. T1 is service-backed (RDR-105) and the engine
# it depends on is frequently the very thing a `service` build is producing
# or repairing — a T1 scratch entry would be unusable exactly when the lease
# matters most (a cold, mid-rebuild, or broken engine). A flat filesystem
# marker needs nothing but the filesystem, so it works before, during, and
# after any engine state, with no substrate to bootstrap.
#
# LEASE LOCATION IS service/.build-lease/<name>, DELIBERATELY *NOT* UNDER
# service/target (review finding, same day as first cut): `mvn clean`
# deletes the entire `target/` directory wholesale. A lease living inside
# it would be destroyed by any `./mvnw clean ...` invocation the CURRENT
# holder itself runs mid-build — silently releasing the lease out from
# under a process that is still running and about to keep writing to
# `target/` after the clean completes, and leaving a concurrent second
# acquirer free to "acquire" nothing was ever protecting. `service/.build-
# lease/` is a sibling of `target/`, untouched by `mvn clean`, so the lease
# survives exactly the operation it exists to serialize around. Gitignored
# (see repo-root `.gitignore`) — it is process-local runtime state, never
# committed.
#
# LEASE SHAPE. service/.build-lease/<name> is a DIRECTORY — mkdir is
# POSIX-atomic on every filesystem this repo targets, the same rationale
# tests/e2e/lib/lock.sh documents for choosing mkdir over flock (flock is
# absent on darwin, the primary dev platform). This deliberately does NOT
# source or depend on tests/e2e/lib/lock.sh: that lib lives under tests/ and
# a production script (scripts/build-gate-jar.sh, scripts/mvnw-leased.sh)
# must not reach into test-only machinery for something it needs at build
# time. The lease directory holds four flat files: `pid` (holder pid),
# `ts` (ISO-8601 UTC acquire time), `label` (holder identity — $NX_AGENT or
# $USER), `command` (the acquiring command line, best-effort).
#
# ACQUIRE-PATH ATOMICITY — AND A PITFALL THIS FILE HAD TO UN-LEARN THE HARD
# WAY. The first cut of this lib tried to gate BOTH the fresh-acquire and
# the stale-reclaim path with the same trick: populate a private staging
# directory fully, then claim the real lease path via one `mv staging dir`.
# That is atomic and race-free ONLY when `dir` does not already exist.
# `mv src dst` where `dst` already exists AS A DIRECTORY does not fail and
# does not replace it — POSIX `mv` NESTS `src` inside `dst` instead
# (`dst/$(basename src)`), and returns exit code 0. So the "already held"
# branch below was silently unreachable: every acquire attempt against a
# live OR stale existing lease "succeeded" by nesting a throwaway staging
# dir inside it, leaving the real `pid`/`ts`/`label`/`command` files
# untouched and reporting success to a caller that did not actually hold
# anything. (tests/e2e/lib/lock.sh's own header names this same class of
# hazard under "HONEST RESIDUAL" for its restore path — this is the same
# footgun, hit here on the PRIMARY acquire path instead of an edge case,
# and caught by this file's own `build-lease_test.sh` before it ever
# shipped.) The fix: `mkdir` is the ONLY primitive used to CLAIM a path in
# this file. `mkdir` on an existing path unambiguously fails (EEXIST) —
# no nesting semantics apply to it the way they do to `mv` — so there is
# no destination-already-exists ambiguity left to reason about. `mv` is
# still used, but only ever to rename an EXISTING lease dir OUT to a
# freshly-random, guaranteed-not-yet-existing capture path (see STALE
# RECLAIM below) — a rename onto a nonexistent destination is a true,
# unambiguous, atomic POSIX rename, not the nesting case.
#
# This does leave a narrow, HONEST residual: `mkdir` claims the path
# atomically, but the four files are then written into it in place,
# non-atomically, one `printf` at a time — a racing reader could observe
# the lease directory between mkdir and the last write. Accepted here:
# contention is low (the orchestrator plus at most one sibling build), the
# window is a handful of tiny `printf`s, and an empty/partial `pid` file is
# already treated as "not alive" by `_build_lease_pid_alive` (see below),
# so the worst case is a spurious-but-safe stale-reclaim retry, never a
# false "still held".
#
# STALE RECLAIM. A lease directory whose pid is no longer alive is taken
# over with a WARNING on stderr — never silently. Liveness probe order (no
# signal-permission dependency where a /proc or ps path exists — same
# rationale as tests/e2e/lib/lock.sh): /proc/<pid> where mounted (Linux),
# `ps -p <pid>` where available (Darwin and most hosts — BSD ps ships with
# the base OS, not the procps package), `kill -0` as a last resort. `kill -0`
# alone cannot distinguish ESRCH (dead) from EPERM (alive, unsignalable) —
# accepted here, same as lock.sh, because the failure direction is always
# "treat as still held", never a false takeover. Reclaim itself is a two-step
# rename-then-recreate: `mv "$dir" "$dir.reclaim.<pid>.<rand>"` (atomic;
# whoever's rename succeeds is the sole reclaimer — a losing concurrent
# reclaimer's `mv` fails cleanly with ENOENT since the source is already
# gone, no restore/mismatch dance needed, because the thing being discarded
# was independently already proven dead before any reclaimer touched it),
# discard the captured copy, then `mkdir "$dir"` fresh and populate it.
#
# API:
#   build_lease_acquire <name>
#       Acquire service/.build-lease/<name> for THIS process ($$).
#       Returns 0 once held. Refuses LOUDLY (rc 75 / EX_TEMPFAIL) naming the
#       live holder's pid/ts/label/command when the lease is held by a live
#       process. A stale lease (dead pid) is reclaimed with a WARNING line
#       on stderr, not silently.
#   build_lease_release <name>
#       Release service/.build-lease/<name>. Only the holding pid may
#       release; releasing a lease this process does not hold (never
#       acquired, already released, or held by someone else) is a silent
#       no-op — safe to call unconditionally from an EXIT trap.
#
# TRAP USAGE (callers MUST do this immediately after a successful acquire):
#   build_lease_acquire service || exit $?
#   trap 'build_lease_release service' EXIT

set -u -o pipefail

# _build_lease_repo_root — anchored on this file's own location, never on
# $PWD: a sourced lib must not depend on the caller's cwd.
_build_lease_repo_root() {
    (cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
}

_build_lease_dir() {
    local name="$1"
    printf '%s/service/.build-lease/%s\n' "$(_build_lease_repo_root)" "$name"
}

_build_lease_ts() {
    date -u +%Y-%m-%dT%H:%M:%SZ
}

# _build_lease_pid_alive <pid> — portable SINGLE-PROCESS existence probe,
# no signal permission required where a /proc or ps path exists (see STALE
# RECLAIM above). Empty/non-numeric input is treated as "not alive" (a
# corrupt or half-written lease is stale, not held). This is the fallback
# used when a lease has no tracked process GROUP (see
# _build_lease_group_alive below) — e.g. a caller that never runs an actual
# child build, or an old-format lease with no `pgid` file.
_build_lease_pid_alive() {
    local pid="$1"
    [[ "$pid" =~ ^[0-9]+$ ]] || return 1
    if [[ -d "/proc/$pid" ]]; then
        return 0
    elif command -v ps >/dev/null 2>&1; then
        ps -p "$pid" >/dev/null 2>&1
        return $?
    else
        kill -0 "$pid" 2>/dev/null
        return $?
    fi
}

# _build_lease_group_alive <pgid> — is ANY process in this process GROUP
# still alive? (review finding, nexus-c00dw: the wrapper shell that calls
# build_lease_acquire is not the same process as the actual build — e.g.
# mvnw-leased.sh's `./mvnw "$@"` child. If only the WRAPPER dies (killed
# directly, not via process-group signal delivery), a liveness check keyed
# on the wrapper's own pid alone goes stale immediately while the real
# build (a grandchild, now orphaned but still running and still writing to
# service/target) keeps going — a second acquirer would then "reclaim" a
# lease that is provably still protecting live, in-progress writes.)
#
# `kill -0 -- -PGID` (a NEGATIVE pid) targets the whole process group in
# standard POSIX kill(2) semantics — succeeds (0) if ANY member is alive,
# fails (ESRCH) only once every member has exited. Same permission caveat
# as the single-pid probe: EPERM (group exists, not ours to signal) is
# indistinguishable here from "definitely alive" — accepted for the same
# reason, the failure direction is always "treat as still held", disclosed
# in the caller's WARNING/REFUSED text rather than silently assumed.
_build_lease_group_alive() {
    local pgid="$1"
    [[ "$pgid" =~ ^[0-9]+$ ]] || return 1
    kill -0 -- "-$pgid" 2>/dev/null
    return $?
}

# _build_lease_populate <dir> <holder-label> <command-line> — fill an
# already-`mkdir`'d (freshly claimed) lease dir with the four flat files.
# No `pgid` file is written here — see build_lease_track_pid, called
# separately once (if ever) the real build child exists.
_build_lease_populate() {
    local dir="$1" holder_label="$2" command_line="$3"
    printf '%s\n' "$$" > "$dir/pid"
    _build_lease_ts > "$dir/ts"
    printf '%s\n' "$holder_label" > "$dir/label"
    printf '%s\n' "$command_line" > "$dir/command"
}

# build_lease_track_pid <name> <pgid> — record the process-group id of the
# REAL build process (e.g. a `./mvnw ... &` launched under `set -m` job
# control, whose pgid equals its own pid as the new group's leader) into an
# ALREADY-HELD lease, so subsequent liveness checks track the build itself
# rather than the acquiring wrapper shell (see _build_lease_group_alive
# above). Only the current holder ($$) may do this; a no-op if this
# process does not currently hold the named lease (never acquired,
# released already, or held by someone else) — same ownership guard as
# build_lease_release.
build_lease_track_pid() {
    local name="${1:?build_lease_track_pid: usage: build_lease_track_pid <name> <pgid>}"
    local pgid="${2:?build_lease_track_pid: usage: build_lease_track_pid <name> <pgid>}"
    local dir
    dir="$(_build_lease_dir "$name")"
    [[ -d "$dir" ]] || return 0
    local existing_pid
    existing_pid="$(cat "$dir/pid" 2>/dev/null || true)"
    [[ "$existing_pid" == "$$" ]] || return 0
    printf '%s\n' "$pgid" > "$dir/pgid"
    return 0
}

# build_lease_acquire <name> [command args, for the recorded 'command' field]
build_lease_acquire() {
    local name="${1:?build_lease_acquire: usage: build_lease_acquire <name>}"
    shift || true
    local dir
    dir="$(_build_lease_dir "$name")"
    mkdir -p "$(dirname "$dir")" 2>/dev/null

    local holder_label="${NX_AGENT:-${USER:-unknown}}"
    local command_line="$0 $*"

    # `mkdir` is the ONLY claim primitive here (see ACQUIRE-PATH ATOMICITY
    # above for why `mv` onto a possibly-existing path is unsafe). Its
    # stderr is captured so a genuine filesystem error (permission denied,
    # ENOSPC, missing parent) can be told apart from plain EEXIST below
    # (review finding: reporting a permission/ENOSPC failure as "held by
    # <pid>" would be actively misleading — there is no holder at all).
    local mkdir_err
    if mkdir_err="$(mkdir "$dir" 2>&1)"; then
        _build_lease_populate "$dir" "$holder_label" "$command_line"
        return 0
    fi
    if [[ ! -d "$dir" ]]; then
        echo "build_lease_acquire: ERROR — could not create build lease '$name' at $dir, and not because it already exists: $mkdir_err. This looks like a filesystem or permission problem, not lock contention — fix that before retrying." >&2
        return 74
    fi

    # Lease directory already exists (the ordinary EEXIST case) — decide
    # live vs stale. A tracked `pgid` (see build_lease_track_pid) takes
    # priority over the plain `pid`: it reflects the real build process,
    # not just the wrapper that called build_lease_acquire.
    local existing_pid existing_ts existing_label existing_cmd existing_pgid
    existing_pid="$(cat "$dir/pid" 2>/dev/null || true)"
    existing_ts="$(cat "$dir/ts" 2>/dev/null || echo unknown)"
    existing_label="$(cat "$dir/label" 2>/dev/null || echo unknown)"
    existing_cmd="$(cat "$dir/command" 2>/dev/null || echo unknown)"
    existing_pgid="$(cat "$dir/pgid" 2>/dev/null || true)"

    local is_live=1
    if [[ -n "$existing_pgid" ]]; then
        _build_lease_group_alive "$existing_pgid" && is_live=0
    elif [[ -n "$existing_pid" ]]; then
        _build_lease_pid_alive "$existing_pid" && is_live=0
    fi

    if [[ $is_live -eq 0 ]]; then
        local who="pid $existing_pid"
        [[ -n "$existing_pgid" ]] && who="pid $existing_pid or its process group $existing_pgid"
        echo "build_lease_acquire: REFUSED — service build lease '$name' is held by $who (label=$existing_label, acquired=$existing_ts, command: $existing_cmd). Wait for it to finish, or run \`build_lease_release $name\` if that pid is dead." >&2
        return 75
    fi

    # Stale (or corrupt — missing/empty pid file counts as stale, never as
    # held): reclaim with a WARNING, never silently. Capture the existing
    # dir OUT to a guaranteed-fresh path via `mv` (an unambiguous atomic
    # rename onto a nonexistent destination — never the nesting case), so
    # a losing concurrent reclaimer's own `mv` fails cleanly (ENOENT)
    # rather than racing a delete.
    echo "build_lease_acquire: WARNING — reclaiming stale build lease '$name' (previous holder pid=${existing_pid:-unknown}${existing_pgid:+, process group $existing_pgid}, label=${existing_label}, acquired=${existing_ts} is not alive); it never released cleanly." >&2
    local capture="${dir}.reclaim.$$.$RANDOM"
    if ! mv "$dir" "$capture" 2>/dev/null; then
        echo "build_lease_acquire: REFUSED — lost the race reclaiming stale build lease '$name'; another process is already reclaiming it. Retry." >&2
        return 75
    fi
    rm -rf "$capture" 2>/dev/null

    if mkdir_err="$(mkdir "$dir" 2>&1)"; then
        _build_lease_populate "$dir" "$holder_label" "$command_line"
        return 0
    fi
    if [[ ! -d "$dir" ]]; then
        echo "build_lease_acquire: ERROR — could not re-create build lease '$name' at $dir after reclaiming it, and not because someone else claimed it first: $mkdir_err. This looks like a filesystem or permission problem, not lock contention." >&2
        return 74
    fi
    echo "build_lease_acquire: REFUSED — lost the race re-creating build lease '$name' after reclaiming it; another process claimed it first. Retry." >&2
    return 75
}

# build_lease_release <name>
build_lease_release() {
    local name="${1:?build_lease_release: usage: build_lease_release <name>}"
    local dir
    dir="$(_build_lease_dir "$name")"
    [[ -d "$dir" ]] || return 0
    local existing_pid
    existing_pid="$(cat "$dir/pid" 2>/dev/null || true)"
    if [[ "$existing_pid" == "$$" ]]; then
        rm -rf "$dir" 2>/dev/null
    fi
    return 0
}
