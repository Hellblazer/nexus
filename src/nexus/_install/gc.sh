#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
#
# Reap old generations. nexus-utpuw.6 (P2b). SOURCED, never executed; sets no
# shell options — they would land in the caller's shell.
#
# THIS IS THE ONLY CODE IN THIS ARC THAT DELETES ANYTHING. Everything else
# builds beside, points at, or reports on. Read the refusals before the logic.
#
# ── FOUR NEVER-DELETE RULES ──────────────────────────────────────────────────
#   (a) the generation `current` points at
#   (b) the PREVIOUS current — rollback for free, recorded by .3
#   (c) any generation with a live holder — .5's census
#   (d) the generation hosting the RUNNING INSTALLER. Under `nx self install`
#       (.14) the installer is exec'd from its own generation. keep-last-N
#       usually covers this; the plan is explicit that "usually" is not a rule,
#       so it is passed in and checked.
# They are ABSOLUTE, not tiebreaks: a held generation far outside keep-last-N
# is still retained.
#
# ── THE DATA-LOSS HAZARD IS THE PARENT DIRECTORY ─────────────────────────────
# ~/.local/share/nexus/ also holds chroma/ (stranded_install.py) and
# fastembed_cache/ (config.py) — user data that `nx uninstall` deliberately
# does not remove. This sweep is scoped to <tools>/gen-* and touches nothing
# else, not even the pointers that live beside them. A glob that walked the
# parent would delete someone's vector store, which is why the tests assert
# both siblings survive WITH a non-vacuity check that something was actually
# reaped in the same run.
#
# ── THE BASE INTERPRETER IS NEVER OURS ───────────────────────────────────────
# Old generations' pyvenv.cfg home= points at a uv-managed CPython outside the
# tools tree. Deleting or pruning it silently breaks every old generation (the
# pipx#146 / uv#8028 class). We never reach outside tools/, which is what makes
# that true; .11 adds the doctor check for when uv prunes it out from under us.
#
# ── WHAT COUNTS AS A GENERATION ──────────────────────────────────────────────
# A gen-* directory CONTAINING a receipt (.2's completion marker). A
# receipt-less gen-* is wreckage from a build that died before writing one: it
# is reaped, and it does NOT count toward keep-last-N — otherwise one crashed
# install shields a real generation from retention it is entitled to. Nothing
# ever pointed `current` at it, which is what makes reaping it safe.

_nx_gc_here="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
# shellcheck source=src/nexus/_install/census.sh
. "$_nx_gc_here/census.sh"

# ── THE DISPATCH ─────────────────────────────────────────────────────────────
# The rules above are implemented in gc_core.py beside this file, and nothing
# below restates them. tests/test_install_gc_twins_agree.py is what says so.
#
# The whole sweep is ONE core call. Not a decision per generation dispatched
# from here, for two reasons that both matter more for gc than anywhere else in
# this directory. The census must take one `ps` for the entire pass, exactly as
# .5 requires, and a call per generation would take one each. And the four
# never-delete rules are evaluated against a single view of the tree: deciding
# one generation at a time across separate processes means the tree can change
# underneath the sweep between decisions, which for the only code here that
# DELETES is the difference between a reap and a data-loss report.

_nx_gc_core() {
    if [ -z "${NX_LAYOUT_HOME-}" ]; then
        echo "nexus: NX_LAYOUT_HOME is unset; gc.sh dispatches to gc_core.py" \
             "beside it and a sourced file cannot find its own directory under" \
             "POSIX sh." >&2
        return "$NX_LAYOUT_USAGE_EXIT"
    fi
    if [ ! -f "$NX_LAYOUT_HOME/gc_core.py" ]; then
        echo "nexus: no gc_core.py in NX_LAYOUT_HOME=$NX_LAYOUT_HOME." \
             "It ships beside gc.sh; an install with one and not the other is" \
             "incomplete." >&2
        return "$NX_LAYOUT_USAGE_EXIT"
    fi
    python3 "$NX_LAYOUT_HOME/gc_core.py" "$@" </dev/null
}

#: Minutes a receipt-less gen-* tree is presumed to be a build in progress.
#: A generation build takes minutes; an hour is well past any of them. Exported
#: rather than passed, because the core reads them from the environment: they
#: are operator knobs, and threading them through argv would mean every caller
#: of nx_gc_generations had to know about them to leave them alone.
NX_GC_BUILD_GRACE_MINUTES="${NX_GC_BUILD_GRACE_MINUTES:-60}"
#: Minutes a receipt-less tree carrying the builder's claim marker
#: ($NX_BUILDING_MARKER_NAME, written the instant the directory exists) is
#: presumed to be a build in progress even if nothing under it was written
#: since: a slow resolve/download phase lands packages in uv's cache, not the
#: tree. Six hours is past any build; a crashed one is reaped after that.
NX_GC_BUILD_CLAIM_MINUTES="${NX_GC_BUILD_CLAIM_MINUTES:-360}"

# Reap generations outside the keep window that no rule protects.
#
#   --keep N        retain the newest N complete generations (default 3)
#   --self <dir>    the generation running this installer (rule d)
#   --dry-run       report exactly what would go, delete nothing
#   $1 optional trailing tools root
nx_gc_generations() {
    NX_GC_BUILD_GRACE_MINUTES="$NX_GC_BUILD_GRACE_MINUTES" \
    NX_GC_BUILD_CLAIM_MINUTES="$NX_GC_BUILD_CLAIM_MINUTES" \
        _nx_gc_core "$@"
}
