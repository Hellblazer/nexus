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

# $1 tools root. Prints protected absolute generation paths, one per line.
_nx_gc_protected() {
    _nx_gp_root="$1"
    for _nx_gp_link in "$NX_CURRENT_LINK_NAME" "$NX_PREVIOUS_LINK_NAME"; do
        if [ -L "$_nx_gp_root/$_nx_gp_link" ]; then
            _nx_gp_target="$(readlink "$_nx_gp_root/$_nx_gp_link")"
            [ -n "$_nx_gp_target" ] && printf '%s\n' "$_nx_gp_target"
        fi
    done
}

# Reap generations outside the keep window that no rule protects.
#
#   --keep N        retain the newest N complete generations (default 3)
#   --self <dir>    the generation running this installer (rule d)
#   --dry-run       report exactly what would go, delete nothing
#   $1 optional trailing tools root
nx_gc_generations() {
    _nx_gc_keep=3
    _nx_gc_self=""
    _nx_gc_dry=0
    _nx_gc_root_arg=""

    while [ $# -gt 0 ]; do
        case "$1" in
            --keep)    _nx_gc_keep="${2-}"; shift 2 ;;
            --self)    _nx_gc_self="${2-}"; shift 2 ;;
            --dry-run) _nx_gc_dry=1;        shift ;;
            *)         _nx_gc_root_arg="$1"; shift ;;
        esac
    done

    case "$_nx_gc_keep" in
        ''|*[!0-9]*)
            echo "nexus: --keep must be a non-negative integer, got '$_nx_gc_keep'" >&2
            return "$NX_LAYOUT_USAGE_EXIT"
            ;;
    esac
    if [ "$_nx_gc_keep" -lt 1 ]; then
        # --keep 0 means "retain nothing", leaving only the four rules between
        # the operator and an install with no fallback at all. Almost certainly
        # a mistake, and refusing costs one message.
        echo "nexus: --keep 0 would retain no generations; refusing" >&2
        return "$NX_LAYOUT_USAGE_EXIT"
    fi

    _nx_gc_root="$(_nx_root "$_nx_gc_root_arg")" || return "$NX_LAYOUT_USAGE_EXIT"
    [ -d "$_nx_gc_root" ] || return 0

    _nx_gc_protected_list="$(_nx_gc_protected "$_nx_gc_root")"
    [ -n "$_nx_gc_self" ] && _nx_gc_protected_list="$_nx_gc_protected_list
$_nx_gc_self"

    # One census for the whole pass, for the same reason .5 takes one snapshot:
    # a per-generation re-read could reap against a state that never existed.
    _nx_gc_snapshot="$(_nx_ps_snapshot refresh)"

    # Complete generations, newest last. Stamps sort chronologically by .2's
    # construction, so lexical order IS creation order — the property .2 pins.
    _nx_gc_complete=""
    for _nx_gc_dir in "$_nx_gc_root"/"$NX_GENERATION_PREFIX"*; do
        [ -d "$_nx_gc_dir" ] || continue
        if [ -f "$_nx_gc_dir/$NX_RECEIPT_NAME" ]; then
            _nx_gc_complete="$_nx_gc_complete$_nx_gc_dir
"
        fi
    done
    _nx_gc_complete="$(printf '%s' "$_nx_gc_complete" | grep -c . 2>/dev/null || true)"

    _nx_gc_index=0
    _nx_gc_total="$_nx_gc_complete"

    for _nx_gc_dir in "$_nx_gc_root"/"$NX_GENERATION_PREFIX"*; do
        [ -d "$_nx_gc_dir" ] || continue

        if [ -f "$_nx_gc_dir/$NX_RECEIPT_NAME" ]; then
            _nx_gc_index=$((_nx_gc_index + 1))
            # Inside the keep window: the newest N complete generations.
            if [ $((_nx_gc_total - _nx_gc_index)) -lt "$_nx_gc_keep" ]; then
                continue
            fi
        fi
        # A receipt-less directory falls through here deliberately: reapable,
        # and never counted toward the keep window.

        # Rules (a), (b), (d).
        _nx_gc_is_protected=0
        for _nx_gc_p in $_nx_gc_protected_list; do
            [ -n "$_nx_gc_p" ] || continue
            if [ "$_nx_gc_p" = "$_nx_gc_dir" ]; then
                _nx_gc_is_protected=1
                break
            fi
        done
        [ "$_nx_gc_is_protected" -eq 1 ] && continue

        # Rule (c).
        if [ -n "$(nx_generation_holder_pids "$_nx_gc_dir" "$_nx_gc_snapshot")" ]; then
            continue
        fi

        if [ "$_nx_gc_dry" -eq 1 ]; then
            printf 'would reap %s\n' "$_nx_gc_dir"
        else
            if [ -L "$_nx_gc_dir" ]; then
                # SCOPING GUARD. Following a gen-* symlink is the ONLY way this
                # sweep can delete anything outside the tools root, so it is
                # fenced twice rather than trusted.
                #
                # Measured before this guard existed: a `gen-rogue` symlink
                # pointing at an unrelated directory caused `rm -rf` of that
                # directory. The only check was that the target was not
                # literally "/" — one value out of infinitely many dangerous
                # ones, which is the shape of a guard that reads as protection
                # without being any.
                #
                # (1) Only the reserved ledger name may be a symlink at all.
                # (2) Its target must look like the uv-managed venv it claims
                #     to be — a directory carrying pyvenv.cfg. A wrong target
                #     (a home directory, a checkout) has none, so the pointer
                #     is unlinked and the tree is left alone. Failing that way
                #     leaves litter; failing the other way deletes data.
                if [ "${_nx_gc_dir##*/}" != "$NX_GENERATION_PREFIX$NX_LEGACY_GENERATION_NAME" ]; then
                    echo "nexus: refusing to reap through an unrecognised generation symlink: $_nx_gc_dir" >&2
                    continue
                fi
                # A pseudo-generation (.7's legacy uv-tool bridge): this
                # entry is only our LEDGER pointer, never the tree itself.
                # `rm -rf` on a symlink unlinks the pointer and leaves its
                # target untouched -- exactly backwards for a reap, whose
                # entire job here is deleting the legacy tree. Resolve one
                # level (registration only ever writes a direct absolute
                # symlink, never a chain) and remove both: the real tree,
                # then the now-dangling pointer.
                _nx_gc_real="$(readlink "$_nx_gc_dir")"
                case "$_nx_gc_real" in
                    /*) ;;
                    *)
                        echo "nexus: ledger target is not an absolute path, refusing: '$_nx_gc_real'" >&2
                        continue
                        ;;
                esac
                if [ ! -d "$_nx_gc_real" ] || [ ! -f "$_nx_gc_real/pyvenv.cfg" ]; then
                    echo "nexus: ledger target is not a venv, unlinking the pointer only: $_nx_gc_real" >&2
                    rm -f -- "$_nx_gc_dir"
                    printf 'reaped %s\n' "$_nx_gc_dir"
                    continue
                fi
                rm -rf -- "$_nx_gc_real"
                rm -f -- "$_nx_gc_dir"
                # uv's own registry still names this tool, and a dangling
                # entry is what lets a stray `uv tool upgrade conexus`
                # rebuild the tree and retake the shims (.7's accepted-risk
                # window, made permanent). Not run from here: uv's uninstall
                # also removes the executables its receipt names, which are
                # now nexus-owned shims at those very paths. Say it instead.
                echo "nexus: reaped the legacy uv tree $_nx_gc_real; run 'uv tool uninstall ${_nx_gc_real##*/}' to clear uv's now-dangling registry entry (nothing runs from it any more)" >&2
            else
                # -rf on the directory itself, never through a pointer: the
                # pointers live in this same directory, and following one
                # would empty the generation it names rather than removing a
                # link.
                rm -rf -- "$_nx_gc_dir"
            fi
            printf 'reaped %s\n' "$_nx_gc_dir"
        fi
    done
    return 0
}
