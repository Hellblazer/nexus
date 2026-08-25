#!/usr/bin/env sh
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
#
# Write the nexus-owned shims that bind a spawn to the current generation.
# nexus-utpuw.4 (P1c). SOURCED, never executed — same contract as layout.sh,
# which this sources. No shell options are set here; they would land in the
# caller's shell.
#
# ── THE SET IS DERIVED, NOT LISTED ───────────────────────────────────────────
# Audit finding F1: a hardcoded five-name allowlist omitted
# nx-session-end-launcher, this project's FOURTH console script, which
# conexus/hooks/hooks.json invokes by BARE PATH NAME with no fallback. Every
# SessionEnd flush would have died silently after migration — and it works today
# only because uv links all of a project's own entry points, so nothing would
# have surfaced until the layout moved.
#
#   shim set = the installed distribution's own console scripts
#            ∪ an explicit dependency-script list
#
# The first clause is uv's own rule, asked of the generation itself so a fifth
# script needs no edit here. The second is our documented extension: uv does NOT
# link a DEPENDENCY's entry points, which is the entire reason the old
# reinstall-tool.sh symlinked mineru by hand, and mineru has been a default
# dependency since nexus-2fyb rather than an extra.
#
# It stays an EXCLUSION discipline and never a glob over <gen>/bin, because that
# directory also holds python, pip and activate.
#
# ── ENTRY POINTS ARE THIRD-PARTY DATA ────────────────────────────────────────
# They come from whatever wheels the distribution depends on, so a name arriving
# here is not ours. It is interpolated into a shell script that gets written onto
# the operator's PATH, so it goes through the layout contract's allowlist first.
# nexus-xk7g2 is why that is an allowlist and not a denylist: a denylist that
# rejected separators, traversals and whitespace still admitted
# nx$(touch${IFS}PWNED), which executes on the next invocation. A name that fails
# is SKIPPED WITH A WARNING and never written — silence would leave an operator
# whose tool vanished with nothing to read.

_nx_shims_here="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
# shellcheck source=src/nexus/_install/layout.sh
. "$_nx_shims_here/layout.sh"

# Entry points of DEPENDENCIES, which uv does not link. Explicit by necessity:
# nothing in the distribution's own metadata declares them.
NX_DEPENDENCY_SCRIPTS="mineru mineru-api"

# Never shimmed, whatever else is in <gen>/bin.
NX_NEVER_SHIM="python python3 pip pip3 activate activate.csh activate.fish uv uvx"

# Ask the generation what console scripts its distribution declares.
# $1 generation dir.  $2 distribution name.
_nx_declared_scripts() {
    "$1/bin/python" -c '
import sys
try:
    import importlib.metadata as md
except ImportError:
    sys.exit(0)
try:
    eps = md.distribution(sys.argv[1]).entry_points
except Exception:
    sys.exit(0)
for ep in eps:
    if getattr(ep, "group", None) == "console_scripts":
        print(ep.name)
' "$2" 2>/dev/null
}

# Write <bin>/<command> for every shim this generation should own.
# $1 generation dir (absolute).  $2 optional bin dir.  $3 optional dist name.
nx_write_shims() {
    case ${1-} in
        /*) ;;
        *)
            echo "nexus: generation must be an absolute path, got '${1-}'" >&2
            return "$NX_LAYOUT_USAGE_EXIT"
            ;;
    esac
    if [ ! -d "$1" ]; then
        echo "nexus: generation is not a directory: $1" >&2
        return "$NX_LAYOUT_USAGE_EXIT"
    fi

    _nx_ws_gen="$1"
    if [ -n "${2-}" ]; then
        _nx_ws_bin="$2"
    else
        _nx_ws_bin="$(nx_bin_dir)" || return "$NX_LAYOUT_USAGE_EXIT"
    fi
    _nx_ws_dist="${3-conexus}"

    mkdir -p "$_nx_ws_bin" || return 1

    # ONE NAME PER LINE, and iterated with read rather than word-splitting.
    # Word-splitting would take an entry point called "bad name" and quietly
    # process "bad" and "name" -- two individually VALID names -- so the
    # allowlist never sees the hostile one and nothing is refused or reported.
    # Caught by test_a_refused_name_is_warned_about_not_silently_dropped.
    _nx_ws_names="$(
        _nx_declared_scripts "$_nx_ws_gen" "$_nx_ws_dist"
        for _nx_ws_dep in $NX_DEPENDENCY_SCRIPTS; do printf '%s\n' "$_nx_ws_dep"; done
    )"

    printf '%s\n' "$_nx_ws_names" | while IFS= read -r _nx_ws_name; do
        [ -n "$_nx_ws_name" ] || continue

        # Explicitly excluded even if something declares them.
        for _nx_ws_never in $NX_NEVER_SHIM; do
            if [ "$_nx_ws_name" = "$_nx_ws_never" ]; then
                _nx_ws_name=""
                break
            fi
        done
        [ -n "$_nx_ws_name" ] || continue

        # Declared but not built (a partial install, an optional extra). A shim
        # pointing at nothing would fail at exec complaining about the target
        # rather than about the install, so write none and say nothing.
        [ -e "$_nx_ws_gen/bin/$_nx_ws_name" ] || continue

        # VALIDATION LIVES IN THE CONTRACT, deliberately, and there is no second
        # copy of it here. nx_render_shim runs the layout's allowlist over the
        # name before it reaches the shim body, refuses a hostile one, and prints
        # the name it refused -- so this `|| continue` skips the entry point and
        # the operator still gets told which one.
        #
        # An earlier version of this file ALSO checked the allowlist itself. That
        # branch was unreachable: deleting it left every test green, because the
        # renderer already refused and already reported. A duplicated guard that
        # no test can distinguish is not defence in depth, it is a second thing
        # to keep in sync that reads as protection. The falsification that
        # matters is in the contract's own suite, where removing the allowlist
        # turns 14 tests red.
        _nx_ws_body="$(nx_render_shim "$_nx_ws_name" "$(dirname "$_nx_ws_gen")")" || continue

        # Per-file atomic: a reader on PATH sees the old shim or the new one,
        # never a half-written script. mv -f also REPLACES a uv-owned symlink at
        # this name rather than following it into the old target, which is what
        # migration (.7) depends on.
        _nx_ws_tmp="$_nx_ws_bin/.$_nx_ws_name.tmp.$$"
        printf '%s\n' "$_nx_ws_body" > "$_nx_ws_tmp" || exit 1
        chmod 755 "$_nx_ws_tmp" || exit 1
        mv -f "$_nx_ws_tmp" "$_nx_ws_bin/$_nx_ws_name" || exit 1
    done || return 1
    return 0
}
