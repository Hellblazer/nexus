#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
#
# Migrate a legacy `uv tool install conexus` layout onto the generation
# layout, WITHOUT breaking a single live holder. nexus-utpuw.7 (P3).
#
# EXECUTED, not sourced (unlike layout.sh/flip.sh/shims.sh/census.sh/gc.sh/
# legacy.sh, its siblings in this directory) — mirrors install_generation.sh:
# being its own process is what lets it set -euo pipefail without changing a
# caller's shell.
#
# ── THE ORDER, AND WHY THE NAIVE ONE IS WRONG ────────────────────────────
# `uv tool uninstall conexus` deletes $(uv tool dir)/conexus — the exact
# tree every live holder is running from (nexus-q3xrx verbatim). A migration
# that uninstalls is a migration that breaks live sessions, so this NEVER
# uninstalls. Required order:
#   1. read the legacy uv-receipt.toml ONE LAST TIME -> extract extras ->
#      seed the new generation's receipt (the only bridge for [local]);
#   2. build the new generation side-by-side (legacy tree untouched);
#   3. flip current;
#   4. replace the uv-owned ~/.local/bin SYMLINKS with nexus-owned shim
#      FILES (nx_write_shims's mv -f REPLACES a symlink at that name rather
#      than following it into the old target);
#   5. register the legacy tree as a pseudo-generation, reaped on a LATER,
#      SEPARATE `nx_gc_generations` pass once nothing holds it.
#
# This script never sources gc.sh and never calls nx_gc_generations — reap
# cannot fire in this process, by construction, regardless of how many
# holders exist at migration time. That is deliberate, not merely cautious:
# the accepted-risk window (a stray `uv tool upgrade conexus` racing the
# hook rewiring) means "zero holders right now" is not the same guarantee as
# "safe to delete right now", and the two-pass split is what keeps this
# script's job to *build and point*, never *decide it is safe to delete*.
#
# Usage:
#   migrate_legacy.sh --source <path-or-name> [--version X.Y.Z]
#                      [--python 3.12] [--legacy-venv <dir>] [--dist <name>]
#
# `--legacy-venv` overrides where the legacy tree is looked for; omitted, it
# resolves via `uv tool dir` (nx_legacy_venv_dir). When no legacy tree is
# found, this is a clean no-op: exit 0, nothing built, nothing printed on
# stdout — a caller that only migrates when there is something to migrate
# can tell the two apart by whether stdout produced a path.
#
# Prints the new generation's directory on stdout when it migrated
# something, mirroring install_generation.sh's stdout-purity contract.
set -euo pipefail

_here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=src/nexus/_install/legacy.sh
. "$_here/legacy.sh"
# shellcheck source=src/nexus/_install/flip.sh
. "$_here/flip.sh"
# shellcheck source=src/nexus/_install/shims.sh
. "$_here/shims.sh"

_die() { echo "migrate_legacy: $*" >&2; exit 64; }

SOURCE=""
VERSION=""
PYTHON_VERSION=""
LEGACY_VENV_OVERRIDE=""
DIST="conexus"

while [ $# -gt 0 ]; do
    case "$1" in
        --source)      SOURCE="${2-}";               shift 2 ;;
        --version)     VERSION="${2-}";               shift 2 ;;
        --python)      PYTHON_VERSION="${2-}";        shift 2 ;;
        --legacy-venv) LEGACY_VENV_OVERRIDE="${2-}";  shift 2 ;;
        --dist)        DIST="${2-}";                  shift 2 ;;
        *) _die "unknown argument: $1" ;;
    esac
done

LEGACY="$(nx_legacy_venv_dir "$LEGACY_VENV_OVERRIDE")" || exit $?

# No legacy tree -> nothing to bridge. Not an error: a fresh install, or a
# box already migrated, both look like this.
if [ ! -d "$LEGACY" ]; then
    exit 0
fi

[ -n "$SOURCE" ] || _die "--source is required (a checkout path, or a distribution name)"

TOOLS_DIR="$(nx_tools_dir)" || exit $?
BIN_DIR="$(nx_bin_dir)" || exit $?

# install_generation.sh does not create its own root -- .2's contract
# assumes a caller has one. On a box migrating for the very first time
# nothing has ever created $NX_TOOLS_DIR, so this caller does.
mkdir -p "$TOOLS_DIR" || exit 1

# ── 1. extras bridge, one last time ──────────────────────────────────────
EXTRAS="$(nx_legacy_extras "$LEGACY")" || exit $?

_build_args=(--source "$SOURCE")
[ -n "$VERSION" ] && _build_args+=(--version "$VERSION")
[ -n "$EXTRAS" ] && _build_args+=(--extras "$EXTRAS")
[ -n "$PYTHON_VERSION" ] && _build_args+=(--python "$PYTHON_VERSION")

# ── 2. build side-by-side; the legacy tree is never touched ─────────────
GEN="$("$_here/install_generation.sh" "${_build_args[@]}")" || exit $?

# ── 3. flip ───────────────────────────────────────────────────────────
nx_flip_current "$GEN" "$TOOLS_DIR" || exit $?

# ── 4. shim takeover: uv-owned symlinks become nexus-owned files ────────
nx_write_shims "$GEN" "$BIN_DIR" "$DIST" || exit $?

# ── 5. register the legacy tree; reap is a LATER, separate pass ─────────
nx_register_legacy_generation "$LEGACY" "$TOOLS_DIR" || exit $?

printf '%s\n' "$GEN"
