#!/usr/bin/env bash
# scripts/lib/release-props-lease.sh — hold a release.properties stamp only
# for as long as the shared build lease is held (bead nexus-iexvl). Sourced,
# never executed.
#
# WHY THIS EXISTS. Observed 2026-09-13 in the v0.1.118 prep worktree:
# tests/e2e/migration-rehearsal/run.sh stamps release.properties for a
# --guided/--shakeout-e2e/--candidate-migration leg and restores its own
# snapshot at the script's EXIT, but only acquired scripts/lib/build-lease.sh's
# "service" lease around the actual native-image build step — leaving a
# window, between the stamp write and that acquire, where the tracked file
# sat stamped in the tree with the lease completely free. scripts/build-
# gate-jar.sh (invoked by tests/e2e/published-client-write-gate.sh) acquired
# the free lease in that window, `cp`'d the ALREADY-STAMPED file as its own
# "pre-stamp" backup, stamped its own value, built, and restored that backup
# on exit — reapplying the run.sh leg's stale stamp to the tracked file
# after run.sh had already restored the true clean bytes. The next leg
# (--shakeout) then started on a dirty tree.
#
# A second, independent ordering bug compounded this: scripts/build-gate-
# jar.sh's own EXIT trap released the lease BEFORE restoring the file
# (`build_lease_release service; cp "$backup" "$props"`), so even a build-
# gate-jar.sh-only interleaving had a window — after the lease was freed,
# before the restore landed — where a second acquirer could snapshot a
# still-stamped file. tests/e2e/migration-rehearsal/build-artifacts.sh and
# tests/e2e/local-service-gate.sh (nexus-56qvf) already got this right:
# restore the bytes, THEN release the lease. This file makes that the ONE
# implementation every stamper shares, instead of five hand-rolled copies
# that can individually regress.
#
# THE RULE (nexus-iexvl root fix): a release.properties stamp lives only
# for the build window, and that window is bounded by the shared build
# lease, not by a script's own EXIT trap running eventually. Concretely:
#   1. guard: refuse to stamp if the file is already dirty relative to HEAD
#      (someone else's stamp is still in the tree — see
#      release_props_guard_clean below).
#   2. acquire the lease.
#   3. guard AGAIN, now that the lease is ours: nothing else can be
#      mid-stamp once we hold it, so a dirty file here means the guard in
#      step 1 raced a stamp that landed between the check and the acquire.
#   4. snapshot the (now known-clean) bytes, then stamp.
#   5. build.
#   6. restore the snapshot.
#   7. release the lease.
# Steps 6 and 7 are ALWAYS in that order — never the reverse — so no other
# stamping process can ever observe a released lease while the file is
# still stamped.
#
# API:
#   release_props_guard_clean <props-path> [lease-name]
#       Refuses (rc 75) if <props-path>'s bytes differ from the same path's
#       content at git HEAD. Prints the file, its current (stamped) value,
#       and how to recover: `git checkout -- <props-path>`, but only after
#       confirming no build is running — names <lease-name> (default
#       "service") and the lease directory to check. A path with no HEAD
#       blob (untracked, or run outside a git repo) is treated as
#       ungoverned and always passes — there is nothing to compare against.
#       Returns 0 (clean) or 75 (dirty / refused).
#
#   release_props_stamp_under_lease <props-path> <lease-name> <max-wait-s> <key=value> [<key=value> ...]
#       Acquires <lease-name> (waiting up to <max-wait-s> — see
#       build_lease_acquire_wait; a dirty file while someone else legitimately
#       holds the lease is the ordinary state of a build in progress and is
#       NOT itself refused — the wait handles it), then, now that the lease
#       is ours and nobody else can legitimately be mid-stamp, guards clean:
#       a dirty file at THAT point means a prior holder left the tree
#       stamped. Once clean, snapshots the pre-stamp bytes to a fresh temp
#       file, then upserts each `key=value` pair into <props-path>
#       (replacing any existing line for that key, appending otherwise; all
#       other lines are preserved verbatim). On success (rc 0) the lease is
#       HELD by this process and the snapshot path is the ONLY line printed
#       to stdout — capture it and pass it to release_props_restore_and_release
#       on every exit path. On failure nothing is stamped, no snapshot is
#       left behind, and the lease is NOT held (a guard failure after
#       acquiring releases it again before returning).
#
#   release_props_restore_and_release <props-path> <snapshot-path> <lease-name>
#       Restores <props-path> from <snapshot-path> bytes, removes the
#       snapshot, THEN releases <lease-name> — in that order, always. Safe
#       to call from an EXIT trap unconditionally: a missing snapshot file
#       is a no-op restore (nothing to copy back), and build_lease_release
#       is itself a no-op when this process does not hold the named lease.
#
# NOT RESPONSIBLE FOR: running the actual build. Callers snapshot/stamp,
# run their own build command, then restore/release — this file only
# closes the window around those two edges.

set -u -o pipefail

_release_props_lease_lib_dir() {
    cd "$(dirname "${BASH_SOURCE[0]}")" && pwd
}

# shellcheck source=./build-lease.sh disable=SC1091
source "$(_release_props_lease_lib_dir)/build-lease.sh"

# release_props_guard_clean <props-path> [lease-name]
release_props_guard_clean() {
    local props="${1:?release_props_guard_clean: usage: release_props_guard_clean <props-path> [lease-name]}"
    local lease_name="${2:-service}"

    [[ -f "$props" ]] || return 0  # nothing to guard: no file, nothing stamped

    local repo tracked actual
    repo="$(cd "$(dirname "$props")" && git rev-parse --show-toplevel 2>/dev/null || true)"
    [[ -n "$repo" ]] || return 0  # not a git checkout: ungoverned, nothing to compare

    local relpath
    relpath="$(cd "$repo" && git ls-files --full-name --error-unmatch -- "$(cd "$(dirname "$props")" && pwd)/$(basename "$props")" 2>/dev/null || true)"
    [[ -n "$relpath" ]] || return 0  # untracked: ungoverned

    tracked="$(cd "$repo" && git show "HEAD:$relpath" 2>/dev/null || true)"
    [[ -n "$tracked" ]] || return 0  # no HEAD blob (new/renamed): nothing to compare

    actual="$(cat "$props" 2>/dev/null || true)"
    if [[ "$actual" == "$tracked" ]]; then
        return 0
    fi

    local lease_dir
    lease_dir="$(_build_lease_dir "$lease_name" 2>/dev/null || true)"
    if [[ -z "$lease_dir" ]]; then
        lease_dir="<repo>/service/.build-lease/$lease_name (or the git common dirs nexus-build-lease/$lease_name)"
    fi
    echo "release_props_guard_clean: REFUSED — $props differs from its git HEAD content; a concurrent stamp is likely still in the tree." >&2
    echo "  current bytes:" >&2
    sed 's/^/    /' "$props" >&2
    echo "  Restore it with: git checkout -- $relpath (run from $repo)" >&2
    echo "  This process holds the $lease_name build lease right now (that is how it got here) and is about to release it without touching the file -- it does not restore anything for you." >&2
    echo "  If you restore by hand later, re-check the $lease_name build lease at $lease_dir AT THAT TIME: a live pid/label/command there means a build is genuinely in progress -- wait for it to finish rather than racing its restore." >&2
    return 75
}

# release_props_stamp_under_lease <props-path> <lease-name> <max-wait-s> <key=value> [...]
release_props_stamp_under_lease() {
    local props="${1:?release_props_stamp_under_lease: usage: release_props_stamp_under_lease <props-path> <lease-name> <max-wait-s> <key=value> [...]}"
    local lease_name="${2:?release_props_stamp_under_lease: usage: release_props_stamp_under_lease <props-path> <lease-name> <max-wait-s> <key=value> [...]}"
    local max_wait="${3:?release_props_stamp_under_lease: usage: release_props_stamp_under_lease <props-path> <lease-name> <max-wait-s> <key=value> [...]}"
    shift 3
    (( $# > 0 )) || { echo "release_props_stamp_under_lease: at least one key=value pair is required" >&2; return 64; }

    test -f "$props" || { echo "release_props_stamp_under_lease: $props does not exist" >&2; return 66; }

    # No pre-acquire guard here, DELIBERATELY: a dirty file at this instant
    # is the ordinary, expected state while a legitimate holder is
    # mid-stamp -- refusing on that alone would misfire on every caller
    # that simply arrives while someone else's build is in progress, which
    # build_lease_acquire_wait already handles correctly (wait, don't
    # refuse). Only ONE guard check matters: the one AFTER we hold the
    # lease ourselves, below -- at that point nobody else can legitimately
    # be mid-stamp, so a dirty file there means a PRIOR holder left the
    # tree stamped (never went through this lease at all, or released
    # before restoring), which is the actual nexus-iexvl hazard.
    build_lease_acquire_wait "$lease_name" "$max_wait" release-props-stamp "$props" "$*" || return $?

    if ! release_props_guard_clean "$props" "$lease_name"; then
        build_lease_release "$lease_name"
        return 75
    fi

    local snapshot
    snapshot="$(mktemp "${TMPDIR:-/tmp}/release.properties.snapshot.XXXXXX")"
    cp "$props" "$snapshot"

    local tmp="${props}.stamp.tmp.$$"
    cp "$props" "$tmp"
    local kv key
    for kv in "$@"; do
        key="${kv%%=*}"
        grep -v "^${key}=" "$tmp" > "${tmp}.next" 2>/dev/null || true
        mv "${tmp}.next" "$tmp"
        printf '%s\n' "$kv" >> "$tmp"
    done
    mv "$tmp" "$props"

    printf '%s\n' "$snapshot"
    return 0
}

# release_props_restore_and_release <props-path> <snapshot-path> <lease-name>
release_props_restore_and_release() {
    local props="${1:?release_props_restore_and_release: usage: release_props_restore_and_release <props-path> <snapshot-path> <lease-name>}"
    local snapshot="${2:?release_props_restore_and_release: usage: release_props_restore_and_release <props-path> <snapshot-path> <lease-name>}"
    local lease_name="${3:?release_props_restore_and_release: usage: release_props_restore_and_release <props-path> <snapshot-path> <lease-name>}"

    if [[ -f "$snapshot" ]]; then
        cp "$snapshot" "$props" 2>/dev/null || true
        rm -f "$snapshot" 2>/dev/null || true
    fi
    # ALWAYS after the restore above, never before — see THE RULE at the
    # top of this file. This ordering is the entire fix.
    build_lease_release "$lease_name" 2>/dev/null || true
}
