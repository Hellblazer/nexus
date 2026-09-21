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
# layout.sh dispatches to layout_core.py beside it, and a sourced file
# cannot find its own directory under POSIX sh. We already know it.
NX_LAYOUT_HOME="$_nx_census_here"
# shellcheck source=src/nexus/_install/layout.sh
. "$_nx_census_here/layout.sh"

# ── THE DISPATCH ─────────────────────────────────────────────────────────────
# The logic is in census_core.py beside this file. What follows dispatches to
# it and states nothing itself; tests/test_install_census_twins_agree.py is
# what says so.
#
# THE SNAPSHOT IS WHY THE BOUNDARY SITS WHERE IT DOES. nx_census_report is ONE
# core call that runs the whole per-generation loop inside one process, so `ps`
# still runs exactly once per census. Dispatching per generation instead would
# start a fresh process each time and take a fresh snapshot with it, which is
# the single-view guarantee quietly becoming false -- the same bug this file
# already fixed once by testing argument COUNT rather than the snapshot's
# value, since a census with no holders has an empty and therefore falsy one.
#
# A caller that holds its own snapshot sends it on STDIN. Not argv and not the
# environment: `ps axww -o pid=,command=` on a busy box runs to hundreds of
# kilobytes and both have a hard size limit. The `-` argument, not the
# emptiness of what arrives, is what says a snapshot was supplied.

_nx_census_core() {
    if [ -z "${NX_LAYOUT_HOME-}" ]; then
        echo "nexus: NX_LAYOUT_HOME is unset; census.sh dispatches to" \
             "census_core.py beside it and a sourced file cannot find its own" \
             "directory under POSIX sh." >&2
        return "$NX_LAYOUT_USAGE_EXIT"
    fi
    if [ ! -f "$NX_LAYOUT_HOME/census_core.py" ]; then
        echo "nexus: no census_core.py in NX_LAYOUT_HOME=$NX_LAYOUT_HOME." \
             "It ships beside census.sh; an install with one and not the other" \
             "is incomplete." >&2
        return "$NX_LAYOUT_USAGE_EXIT"
    fi
    python3 "$NX_LAYOUT_HOME/census_core.py" "$@"
}

# One process snapshot. Kept for callers that attribute several generations
# from one view; see the header on why that matters.
# $1 optional: "refresh", accepted and ignored — the core holds no cache, so
# every call is already a fresh snapshot and there is nothing to invalidate.
_nx_ps_snapshot() {
    _nx_census_core ps_snapshot
}

# PIDs of live processes running from $1, one per line, empty if none.
# $1 generation dir (absolute).  $2 optional pre-taken snapshot.
nx_generation_holder_pids() {
    if [ $# -ge 2 ]; then
        printf '%s\n' "$2" | _nx_census_core holder_pids "${1-}" -
    else
        _nx_census_core holder_pids "${1-}" </dev/null
    fi
}

# One line per generation: its path and how many live processes hold it.
# Informational; always exits 0 when it can read the tools directory.
# $1 optional tools root.
nx_census_report() {
    _nx_census_core report "${1-}" </dev/null
}
