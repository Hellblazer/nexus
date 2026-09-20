#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
#
# Write the nexus-owned shims that bind a spawn to the current generation.
# nexus-utpuw.4 (P1c). SOURCED, never executed — same contract as layout.sh,
# which this sources. No shell options are set here; they would land in the
# caller's shell.
#
# ── THE DISPATCH ─────────────────────────────────────────────────────────────
# The logic is in shims_core.py beside this file. What follows dispatches to it
# and states nothing itself; tests/test_install_shims_twins_agree.py is what
# says so, and tests/scripts/test_write_shims.py is what pins the behaviour
# through this entry point.
#
# WHAT THE COLLAPSE FIXED, rather than merely moved. This file used to derive
# the shim set, write it, and then recompute the OWNED set inline in its prune
# loop — while layout_core.owned_shim_names computed the same set for nx doctor
# and for self_cmd's reclaim repair. The comment sitting over that loop said
# "the two must agree or the twins drift". Measured against a generation
# declaring a hostile entry point that also existed in bin/:
#
#     kept by this file's prune : ['nx', 'nx$(touch${IFS}PWNED)']
#     owned_shim_names          : ['nx']
#
# The inline rule never consulted the name allowlist, so a hostile name was
# OWNED by the pruner and kept forever, while the writer refused to write it
# and nx doctor — which walks the owned set — never looked at it. The one
# component that would have removed such a file was the one component that
# believed it belonged there. Reachable from history, not hypothetical: the
# pre-nexus-xk7g2 writer used a DENYLIST that admitted exactly that name.
#
# There is now ONE rule (layout_core.owned_from_declared), asked once, used by
# the writer and the pruner and by nx doctor alike.

_nx_shims_here="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
# layout.sh dispatches to layout_core.py beside it, and a sourced file
# cannot find its own directory under POSIX sh. We already know it.
NX_LAYOUT_HOME="$_nx_shims_here"
# shellcheck source=src/nexus/_install/layout.sh
. "$_nx_shims_here/layout.sh"

_nx_shims_core() {
    if [ -z "${NX_LAYOUT_HOME-}" ]; then
        echo "nexus: NX_LAYOUT_HOME is unset; shims.sh dispatches to" \
             "shims_core.py beside it and a sourced file cannot find its own" \
             "directory under POSIX sh." >&2
        return "$NX_LAYOUT_USAGE_EXIT"
    fi
    if [ ! -f "$NX_LAYOUT_HOME/shims_core.py" ]; then
        echo "nexus: no shims_core.py in NX_LAYOUT_HOME=$NX_LAYOUT_HOME." \
             "It ships beside shims.sh; an install with one and not the other" \
             "is incomplete." >&2
        return "$NX_LAYOUT_USAGE_EXIT"
    fi
    python3 "$NX_LAYOUT_HOME/shims_core.py" "$@"
}

# Write <bin>/<command> for every shim this generation should own, then remove
# the ones it no longer owns.
# $1 generation dir (absolute).  $2 optional bin dir.  $3 optional dist name.
#
# Produces FILES, not a line on stdout: callers write `nx_write_shims "$gen"
# "$bin"` and read the exit status. Diagnostics go to stderr, so a skipped
# entry point is something the operator can read rather than a tool that
# silently vanished.
nx_write_shims() {
    _nx_shims_core write "${1-}" "${2-}" "${3-}"
}
