#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
#
# Which live processes are still running from which generation.
# nexus-utpuw.5 (P2a). SOURCED, never executed. Sets no shell options — they
# would land in the caller's shell. Bash, not POSIX sh: it uses
# ${BASH_SOURCE[0]} to find its own directory, the only reliable way for a
# sourced script to locate itself.
#
# ── THE ROLE CHANGED, AND THAT IS THE POINT OF THE ARC ───────────────────────
# This replaces scripts/reinstall-tool.sh's live_venv_processes(), which
# answered one question — "is ANYTHING running from the tool venv" — and used
# the answer to REFUSE an install. Under side-by-side generations nothing is
# ever refused (nexus-utpuw comment 1: zero flags, zero steps). A holder is no
# longer an obstacle; it is a fact about ONE generation:
#   - an input to GC (.6), which must never reap a tree someone is running from
#   - one informational line, telling the operator which generations are still
#     spoken for. Those sessions converge on their next spawn.
# Nothing here exits non-zero because it found holders. An exit status that
# meant "occupied" would smuggle the refusal back in wearing a different hat.
#
# ── MARKERS ARE DERIVED, NOT HARDCODED ───────────────────────────────────────
# src/nexus/upgrade_finish.py:50 hardcodes _PROC_MARKERS = ('uv/tools/conexus',
# '.local/bin/nx'): a substring that silently stops matching the moment the
# layout moves, which is the failure class this arc keeps removing. Under
# generations the marker set is ENUMERABLE — the gen-* directories that
# actually exist — so there is nothing to keep in sync and nothing to rot. .10
# does the same for the Python side.
#
# ── ONE SNAPSHOT ─────────────────────────────────────────────────────────────
# `ps` runs ONCE per census and every generation is attributed from that single
# view. Calling it per generation would let a process exit between calls and
# appear to hold two trees or none, and GC would then reap against a state that
# never existed at any instant.

_nx_census_here="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
# shellcheck source=src/nexus/_install/layout.sh
. "$_nx_census_here/layout.sh"

# One process snapshot, cached for the life of the shell that sourced this.
# $1 optional: "refresh" to retake it.
_nx_ps_snapshot() {
    if [ "${1-}" = "refresh" ] || [ -z "${_NX_PS_SNAPSHOT+set}" ]; then
        _NX_PS_SNAPSHOT="$(ps ax -o pid=,command= 2>/dev/null)"
    fi
    printf '%s\n' "$_NX_PS_SNAPSHOT"
}

# PIDs of live processes running from $1, one per line, empty if none.
#
# Attribution is on the GENERATION path, never the shim path. A shim resolves
# `current` and execs the real binary, so a live holder's argv names its own
# generation; something naming only the shim has not resolved one and must not
# pin a tree it is not running from — GC would otherwise keep a generation
# alive on the strength of a wrapper.
#
# $1 generation dir (absolute).  $2 optional pre-taken snapshot.
nx_generation_holder_pids() {
    case ${1-} in
        /*) ;;
        *)
            echo "nexus: generation must be an absolute path, got '${1-}'" >&2
            return "$NX_LAYOUT_USAGE_EXIT"
            ;;
    esac

    # PROVIDED-BUT-EMPTY is not the same as NOT PROVIDED, and testing the value
    # conflates them: a snapshot with no matching processes is empty, so the
    # falsy check re-took one PER GENERATION -- ps ran N+1 times for one census
    # and the single-view guarantee above was quietly false. Test the ARGUMENT
    # COUNT, which is the thing actually being asked about.
    if [ $# -ge 2 ]; then
        _nx_hp_snapshot="$2"
    else
        _nx_hp_snapshot="$(_nx_ps_snapshot)"
    fi

    # The transient-grep exclusion is preserved from live_venv_processes(): a
    # grep whose own argv contains the search string counts as a holder
    # otherwise, and every generation looks permanently occupied.
    printf '%s\n' "$_nx_hp_snapshot" \
        | grep -F -- "$1" \
        | grep -v -e '[[:space:]]grep[[:space:]]' -e '[[:space:]]grep$' \
        | awk '{print $1}' \
        || true
}

# One line per generation: its path and how many live processes hold it.
# Informational; always exits 0 when it can read the tools directory.
# $1 optional tools root.
nx_census_report() {
    _nx_cr_root="$(_nx_root "${1-}")" || return "$NX_LAYOUT_USAGE_EXIT"
    [ -d "$_nx_cr_root" ] || return 0

    # Taken once, passed down. See the header.
    _nx_cr_snapshot="$(_nx_ps_snapshot refresh)"

    for _nx_cr_gen in "$_nx_cr_root"/"$NX_GENERATION_PREFIX"*; do
        # Only gen-* directories. ~/.local/share/nexus/ also holds chroma/ and
        # fastembed_cache/, which are documented user data; enumerating past
        # the prefix is how a sweep turns into a data-loss bug (.6 scopes the
        # same way).
        [ -d "$_nx_cr_gen" ] || continue

        _nx_cr_pids="$(nx_generation_holder_pids "$_nx_cr_gen" "$_nx_cr_snapshot")"
        if [ -n "$_nx_cr_pids" ]; then
            _nx_cr_count="$(printf '%s\n' "$_nx_cr_pids" | grep -c .)"
        else
            _nx_cr_count=0
        fi
        printf '%s holders=%s %s\n' \
            "$_nx_cr_gen" "$_nx_cr_count" "$(printf '%s' "$_nx_cr_pids" | tr '\n' ',' | sed 's/,$//')"
    done
    return 0
}
