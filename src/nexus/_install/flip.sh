#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
#
# Move <tools>/current to a new generation, atomically, and record where it
# came from. nexus-utpuw.3 (P1b).
#
# SOURCED, never executed — same contract as layout.sh, which this sources.
# No shell options are set here: they would land in the caller's shell.
#
# ── WHY NOT `ln -sfn` ────────────────────────────────────────────────────────
# Because it is two syscalls. `ln -sfn` unlinks the existing symlink and then
# creates a new one, and BETWEEN THOSE the pointer does not exist. Every shim
# resolves <tools>/current at spawn (readlink, then exec the result), so a
# process starting inside that window gets exit 70 instead of running. The
# window is small and the failure is intermittent, which makes it worse rather
# than better: it would surface as a rare unexplained "no current generation"
# during exactly the moment an install was running.
#
# The correct form creates the replacement under a temporary name and then
# RENAMES it over the pointer. rename(2) is atomic: an observer sees the old
# target or the new one, never neither.
#
# ── THE mv PORTABILITY DETAIL, AND WHY IT IS NOT COSMETIC ────────────────────
# A plain `mv tmp current`, where `current` is an existing symlink to a
# DIRECTORY, follows the symlink and moves tmp INSIDE that directory. The
# pointer is left untouched and a stray link appears in the generation. The
# flag that says "operate on the symlink itself" differs by platform:
#   BSD / macOS : mv -h
#   GNU coreutils: mv -T
# GNU mv has no -h and rejects it during argument parsing, before touching
# anything, so trying -h first and falling back to -T is safe in both
# directions. Both failing is an error, never a silent plain mv.
#
# ── ORDERING: `previous` IS RECORDED BEFORE `current` MOVES ──────────────────
# A crash between the two then leaves `previous` pointing at what is still
# `current`, which makes a rollback a harmless no-op. The other order leaves
# `previous` stale — pointing at a generation two steps back — and a rollback
# would go somewhere the operator did not ask for.

_nx_flip_here="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
# shellcheck source=src/nexus/_install/layout.sh
. "$_nx_flip_here/layout.sh"

# Replace the symlink at $2 with one pointing at $1, atomically.
_nx_symlink_swap() {
    _nx_swap_target="$1"
    _nx_swap_link="$2"
    _nx_swap_tmp="$(dirname "$_nx_swap_link")/.$(basename "$_nx_swap_link").tmp.$$"

    rm -f "$_nx_swap_tmp"
    ln -s "$_nx_swap_target" "$_nx_swap_tmp" || return 1

    if mv -h "$_nx_swap_tmp" "$_nx_swap_link" 2>/dev/null; then
        return 0
    fi
    if mv -T "$_nx_swap_tmp" "$_nx_swap_link" 2>/dev/null; then
        return 0
    fi

    # Never fall back to a plain `mv`: it would follow the existing symlink and
    # deposit the temporary pointer inside the current generation, leaving the
    # real pointer untouched while reporting success.
    rm -f "$_nx_swap_tmp"
    echo "nexus: could not atomically replace $_nx_swap_link (mv supports neither -h nor -T)" >&2
    return 1
}

# Point current at $1, recording the outgoing generation as previous.
# $1 generation directory (absolute).  $2 optional tools root.
nx_flip_current() {
    case ${1-} in
        /*) ;;
        *)
            echo "nexus: flip target must be an absolute path, got '${1-}'" >&2
            return "$NX_LAYOUT_USAGE_EXIT"
            ;;
    esac
    if [ ! -d "$1" ]; then
        echo "nexus: flip target is not a directory: $1" >&2
        return "$NX_LAYOUT_USAGE_EXIT"
    fi

    _nx_flip_current_link="$(nx_current_link "${2-}")" || return "$NX_LAYOUT_USAGE_EXIT"
    _nx_flip_previous_link="$(nx_previous_link "${2-}")" || return "$NX_LAYOUT_USAGE_EXIT"

    # Record where we are leaving BEFORE moving — see the ordering note above.
    # Nothing to record on a virgin install, and inventing one would make GC
    # protect a generation that never existed.
    if [ -L "$_nx_flip_current_link" ]; then
        _nx_flip_outgoing="$(readlink "$_nx_flip_current_link")"
        if [ -n "$_nx_flip_outgoing" ] && [ "$_nx_flip_outgoing" != "$1" ]; then
            _nx_symlink_swap "$_nx_flip_outgoing" "$_nx_flip_previous_link" || return 1
        fi
    fi

    _nx_symlink_swap "$1" "$_nx_flip_current_link" || return 1
    return 0
}

# Return current to whatever previous names, and make that itself reversible.
# $1 optional tools root.
nx_rollback_current() {
    _nx_rb_current_link="$(nx_current_link "${1-}")" || return "$NX_LAYOUT_USAGE_EXIT"
    _nx_rb_previous_link="$(nx_previous_link "${1-}")" || return "$NX_LAYOUT_USAGE_EXIT"

    if [ ! -L "$_nx_rb_previous_link" ]; then
        echo "nexus: no previous generation recorded; nothing to roll back to" >&2
        return "$NX_LAYOUT_USAGE_EXIT"
    fi

    _nx_rb_target="$(readlink "$_nx_rb_previous_link")"
    if [ -z "$_nx_rb_target" ] || [ ! -d "$_nx_rb_target" ]; then
        # GC is supposed never to reap the previous generation. If it has, say
        # so rather than pointing current at a hole — the shim's error would
        # name a missing directory and not the rollback that caused it.
        echo "nexus: previous generation is gone, refusing to roll back to: $_nx_rb_target" >&2
        return "$NX_LAYOUT_USAGE_EXIT"
    fi

    # nx_flip_current records the outgoing generation as the new previous, so a
    # rollback is itself reversible — a mistaken rollback must not be a
    # one-way door.
    nx_flip_current "$_nx_rb_target" "${1-}"
}
