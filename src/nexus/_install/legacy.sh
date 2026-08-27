#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
#
# The legacy `uv tool install conexus` bridge. nexus-utpuw.7 (P3). SOURCED,
# never executed — same contract as flip.sh/shims.sh/census.sh, which this
# sources. No shell options are set here; they would land in the caller's
# shell.
#
# `migrate_legacy.sh` composes three things from here:
#   nx_legacy_venv_dir        -- where the legacy install lives
#   nx_legacy_extras          -- the ONLY bridge for the [local] extra
#   nx_register_legacy_generation -- the GC-ledger entry that lets a LATER,
#                                     separate pass reap it once nothing
#                                     holds it
#
# ── WHY A SYMLINK IS THE LEDGER ENTRY ─────────────────────────────────────
# gc.sh enumerates `<tools>/gen-*` directories carrying a receipt; the legacy
# tree lives at `$(uv tool dir)/conexus`, entirely outside `<tools>/`. A
# symlink named `gen-legacy-uv-tool` pointing OUTSIDE the tools root makes
# gc.sh's existing `[ -d "$_nx_gc_dir" ]` enumeration see it for free (`-d`
# follows symlinks) — no change to gc.sh's enumeration or protection rules.
# It is PERMANENTLY receipt-less: nothing here ever writes
# `nexus-install.json` into a tree this project does not own, so gc.sh's
# existing "receipt-less -> reapable, never counted toward keep-last-N" rule
# already gives it exactly the semantics this bead needs — never protected
# by retention, eligible for reap the instant nothing holds it. census.sh's
# `nx_generation_holder_pids` and gc.sh's reap step each resolve one level of
# symlink so that rule (c) and the actual deletion operate on the REAL
# legacy tree, never the pointer (see their own comments for why).

_nx_legacy_here="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
# shellcheck source=src/nexus/_install/layout.sh
. "$_nx_legacy_here/layout.sh"

# The one-time pseudo-generation name. A fixed literal, not a stamp: there is
# ever at most one legacy tree to bridge, and a fixed name is what makes
# re-registration (the accepted-risk "a stray `uv tool upgrade` repopulated
# it" mitigation) idempotent rather than minting a new ledger entry per run.
# NX_LEGACY_GENERATION_NAME is defined by layout.sh (sourced above): gc.sh
# needs it too and does not source this file, so one definition serves both.

# Where the legacy `uv tool install conexus` layout lives. $1 optional
# override — every test in this arc supplies one so nothing here ever shells
# out to a real `uv`. Without it, resolved via `uv tool dir`, the SAME call
# scripts/reinstall-tool.sh has always made.
nx_legacy_venv_dir() {
    if [ -n "${1-}" ]; then
        printf '%s\n' "$1"
        return 0
    fi
    _nx_lv_uv_dir="$(uv tool dir)" || {
        echo "nexus: could not resolve 'uv tool dir'" >&2
        return 1
    }
    printf '%s\n' "$_nx_lv_uv_dir/conexus"
}

# Extras recorded in a legacy uv-receipt.toml, comma-joined and sorted --
# the SAME parsing scripts/reinstall-tool.sh has always used (a bracketed
# `extras = [...]` block, quoted names), so the two never disagree about
# what a legacy receipt says. mineru is filtered out: it is a default
# dependency now, not an extra (nexus-2fyb), and a stale receipt that still
# lists it must not re-request it. Prints an empty line -- not an error --
# when there is no receipt, or the receipt names no extras: an absent
# [local] is a normal fresh-registry install, not a bridge failure.
# $1 legacy venv dir (absolute).
nx_legacy_extras() {
    _nx_le_receipt="$1/uv-receipt.toml"
    if [ ! -f "$_nx_le_receipt" ]; then
        printf '\n'
        return 0
    fi
    NEXUS_RECEIPT_PATH="$_nx_le_receipt" python3 -c '
import os, re
text = open(os.environ["NEXUS_RECEIPT_PATH"]).read()
m = re.search(r"extras\s*=\s*\[([^\]]*)\]", text, re.DOTALL)
extras = []
if m:
    extras = re.findall(r"\"([^\"]+)\"", m.group(1))
    extras = sorted({e for e in extras if e != "mineru"})
print(",".join(extras))
' 2>/dev/null || printf '\n'
}

# Register $1 (the legacy tree, absolute) as a reapable pseudo-generation
# inside $2's GC ledger: a symlink named gen-legacy-uv-tool pointing at it.
# Idempotent -- a no-op when the pointer is already correct, and a refresh
# (never a duplicate entry) when it names something else, which is what
# lets a repeat migration attempt reconcile a legacy tree a stray `uv tool
# upgrade` repopulated during the accepted-risk window.
#
# $1 legacy venv dir (absolute).  $2 optional tools root.
nx_register_legacy_generation() {
    case ${1-} in
        /*) ;;
        *)
            echo "nexus: legacy venv dir must be an absolute path, got '${1-}'" >&2
            return "$NX_LAYOUT_USAGE_EXIT"
            ;;
    esac
    _nx_rlg_root="$(_nx_root "${2-}")" || return "$NX_LAYOUT_USAGE_EXIT"
    mkdir -p "$_nx_rlg_root" || return 1
    _nx_rlg_link="$(nx_generation_dir "$NX_LEGACY_GENERATION_NAME" "$_nx_rlg_root")" \
        || return "$NX_LAYOUT_USAGE_EXIT"

    if [ -L "$_nx_rlg_link" ] && [ "$(readlink "$_nx_rlg_link")" = "$1" ]; then
        return 0
    fi

    # Same tmp-then-rename swap flip.sh uses for `current`/`previous`: a
    # plain `ln -sfn` unlinks then relinks as two syscalls, leaving a window
    # where the pointer does not exist. Nothing reads this specific pointer
    # at spawn time the way a shim reads `current`, but there is no reason
    # for the ledger entry to be any less atomic than the pointers it sits
    # beside in the same directory.
    _nx_rlg_tmp="$_nx_rlg_root/.$NX_GENERATION_PREFIX$NX_LEGACY_GENERATION_NAME.tmp.$$"
    rm -f "$_nx_rlg_tmp"
    ln -s "$1" "$_nx_rlg_tmp" || return 1
    if mv -h "$_nx_rlg_tmp" "$_nx_rlg_link" 2>/dev/null; then
        return 0
    fi
    if mv -T "$_nx_rlg_tmp" "$_nx_rlg_link" 2>/dev/null; then
        return 0
    fi
    rm -f "$_nx_rlg_tmp"
    echo "nexus: could not register the legacy generation at $_nx_rlg_link" >&2
    return 1
}
