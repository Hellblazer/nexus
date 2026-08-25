#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
#
# Build ONE generation at $NX_TOOLS_DIR/gen-<stamp> and write its receipt.
# nexus-utpuw.2 (P1a). This is the half of nexus-utpuw that makes an install
# safe under live holders: it never writes into an existing generation, so the
# tree any running process resolved at spawn stays byte-identical underneath it.
#
# EXECUTED, not sourced (unlike layout.sh, its sibling in this directory) —
# scripts/reinstall-tool.sh execs it, and `nx self install` (.14) will exec the
# packaged copy from its own untouched generation. Being its own process is what
# lets it set -euo pipefail without changing a caller's shell.
#
# Usage:
#   install_generation.sh --source <path-or-name> [--version X.Y.Z]
#                         [--extras a,b] [--python 3.12]
#
# Prints the generation directory it built on stdout. The caller (.3's flip)
# consumes that rather than re-deriving the stamp, because re-deriving would
# race with the collision resolution below.
#
# ── WHY THE VENV IS BUILT AT ITS FINAL PATH ──────────────────────────────────
# Console-script shebangs bake ABSOLUTE paths at install time. Building
# elsewhere and renaming into place would leave every entry point in <gen>/bin
# pointing at a directory that no longer exists — complete-looking and entirely
# broken. That is also why `uv venv --relocatable` is banned here: it rewrites
# shebangs to an exec trick, and absolute baked paths are precisely what the
# spawn-time shim design requires.
#
# ── HOW A GENERATION BECOMES VISIBLE, GIVEN IT CANNOT BE RENAMED INTO PLACE ──
# By a completion marker, not an atomic rename. A GENERATION IS A gen-*
# DIRECTORY CONTAINING A VALID nexus-install.json, and the receipt is written
# LAST, after uv reports success, via tmp+mv inside the directory. So:
#   crash before the receipt -> a gen-* dir with no receipt: not a generation,
#                               skipped by every enumerator (.9) and reapable (.6)
#   crash mid-receipt        -> only the tmp name is ever partial
#   success                  -> receipt present, generation real
# The cleanup trap below is TIDINESS, not the guarantee: a SIGKILL skips it, and
# the marker is what keeps a half-built tree from being mistaken for a working
# one. Do not let a later reader promote the trap to the mechanism.
set -euo pipefail

_here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=src/nexus/_install/layout.sh
. "$_here/layout.sh"

_die() { echo "install_generation: $*" >&2; exit 64; }

SOURCE=""
VERSION=""
EXTRAS=""
PYTHON_VERSION="3.12"

while [ $# -gt 0 ]; do
    case "$1" in
        --source)  SOURCE="${2-}";          shift 2 ;;
        --version) VERSION="${2-}";         shift 2 ;;
        --extras)  EXTRAS="${2-}";          shift 2 ;;
        --python)  PYTHON_VERSION="${2-}";  shift 2 ;;
        *) _die "unknown argument: $1" ;;
    esac
done

[ -n "$SOURCE" ] || _die "--source is required (a checkout path, or a distribution name)"

# Resolves NX_TOOLS_DIR through the shared contract, so a relative override is
# refused here exactly as it is everywhere else rather than being resolved
# against this script's CWD.
TOOLS_DIR="$(nx_tools_dir)" || exit $?

# Classified by SHAPE, never by probing the filesystem. Probing (`[ -e "$SOURCE" ]`)
# makes the answer depend on the caller's working directory: run from this
# repository root, `--source conexus` finds the plugin directory `conexus/` and a
# registry install is silently recorded as a directory install. Caught by
# test_spec_reaches_uv_with_extras_before_the_pin, which runs from the repo root
# for exactly that reason. A bare distribution name is a registry source
# wherever you happen to be standing.
case "$SOURCE" in
    .|..|/*|./*|../*|"~"/*|*/*) SOURCE_KIND="directory" ;;
    *)                          SOURCE_KIND="registry"  ;;
esac

# Shape decides the KIND; existence is still checked so a mistyped path fails
# here with a clear message rather than inside uv.
if [ "$SOURCE_KIND" = "directory" ] && [ ! -e "$SOURCE" ]; then
    _die "directory source does not exist: $SOURCE"
fi

SPEC="$(nx_build_spec "$SOURCE" "$EXTRAS" "$VERSION")" || exit $?

# ── The stamp ────────────────────────────────────────────────────────────────
# UTC, second resolution, chronologically sortable as a plain string — .6's
# keep-last-N depends on lexical order being creation order, so that property is
# deliberate rather than incidental.
#
# Second resolution does NOT prevent two installs colliding, and no finer
# resolution would close it either — it would only narrow it. What closes it is
# mkdir's atomicity: a bare `mkdir` (never `mkdir -p`) fails if the directory
# exists, and that failure IS the collision detector. Building into an existing
# generation would mutate a tree a live process may be running from, which is
# the one thing this script exists to never do.
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
GEN=""
for _suffix in "" a b c d e f g h; do
    _candidate="$(nx_generation_dir "${STAMP}${_suffix}" "$TOOLS_DIR")" || exit $?
    if mkdir "$_candidate" 2>/dev/null; then
        GEN="$_candidate"
        break
    fi
done
[ -n "$GEN" ] || _die "could not claim a generation directory for stamp ${STAMP} (9 collisions)"

# Tidiness only — see the header. Correctness rests on the receipt marker.
_cleanup_incomplete() {
    if [ -n "$GEN" ] && [ ! -f "$GEN/$NX_RECEIPT_NAME" ]; then
        rm -rf "$GEN"
    fi
}
trap _cleanup_incomplete EXIT

# ── Build ────────────────────────────────────────────────────────────────────
# Deliberately NOT a per-version UV_TOOL_DIR: uv would put its own receipt
# inside the env and would need UV_TOOL_BIN_DIR redirected too, or it clobbers
# the shared bin entries — and those must be nexus-owned regular files (.4),
# never uv-owned symlinks.
uv venv --python "$PYTHON_VERSION" "$GEN" >&2
uv pip install --python "$GEN/bin/python" "$SPEC" >&2

# ── Receipt ──────────────────────────────────────────────────────────────────
# base_interpreter holds pyvenv.cfg's `home` value verbatim. That field is what
# CPython itself consults and what uv pruning removes, so it is the thing .11's
# doctor check must test for existence — a re-derived path could differ in shape
# from the one that actually goes missing (the pipx#146 / uv#8028 class).
# No pipe into head: under `set -o pipefail` a producer still writing when an
# early-exit consumer quits gets SIGPIPE, and that status is promoted over the
# consumer's success — which `set -e` then turns into a dead script. pyvenv.cfg
# is two lines so it would rarely bite, but the pipe-free form is also simpler.
# Command substitution drains to EOF, then take the first line in the shell.
BASE_INTERPRETER="$(sed -n 's/^home *= *//p' "$GEN/pyvenv.cfg")"
BASE_INTERPRETER="${BASE_INTERPRETER%%$'\n'*}"
PYTHON_FULL="$(sed -n 's/^version *= *//p' "$GEN/pyvenv.cfg")"
PYTHON_FULL="${PYTHON_FULL%%$'\n'*}"
[ -n "$PYTHON_FULL" ] || PYTHON_FULL="$PYTHON_VERSION"

RECEIPT="$(nx_receipt_path "$GEN")" || exit $?
_tmp_receipt="$GEN/.nexus-install.json.tmp"
nx_render_receipt \
    "$VERSION" "$SPEC" "$SOURCE_KIND" "$SOURCE" "$EXTRAS" \
    "$PYTHON_FULL" "$BASE_INTERPRETER" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    > "$_tmp_receipt"
mv -f "$_tmp_receipt" "$RECEIPT"

# The generation is real from this line onward.
printf '%s\n' "$GEN"
