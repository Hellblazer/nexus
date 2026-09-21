# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
#
# The generation layout, for shell callers. THE LOGIC IS NOT HERE: it is in
# layout_core.py beside this file, and every function below dispatches to it.
#
# nexus-utpuw.1 (P0). This file used to be a second IMPLEMENTATION of the
# layout contract, kept in step with src/nexus/install_layout.py by
# tests/test_install_layout_twins_agree.py. Two implementations exist when the
# callers have incompatible import constraints: the generation builder and the
# shim writer run from scripts/reinstall-tool.sh, which may run with NOTHING
# installed and therefore cannot import nexus.
#
# The constraint was always importing NEXUS, never running Python -- the
# scripts here call python3 constantly. So layout_core.py imports nothing from
# nexus, runs as a plain script, and serves both callers. What was two
# statements of one rule is now one.
#
# WHAT IS STILL STATED TWICE, AND WHY THAT IS ALL RIGHT
#
# The NX_* constants below are still shell literals, pinned value-by-value
# against layout_core's by the twins test, whose coverage assert PARSES this
# file so a new constant cannot be added here without a twin. They stay because
# single-sourcing them would mean eval-ing generated shell at source time and
# would make SOURCING this file depend on python3 -- gc.sh and census.sh read
# constants and call no functions at all, so they would newly fail whole rather
# than at a call. A literal under a pin does not drift the way an escaping
# routine or a resolution rule drifts; that is the duplication worth removing,
# and it is gone.
#
# SOURCED, NEVER EXECUTED. No `set -e`: this file is dotted into its callers,
# and options set here would silently change how every one of them handles an
# unrelated failure. Functions signal by exit status; callers decide.
#
# THE CALLING CONTRACT, unchanged, and the reason the dispatch is not simply
# `python3 ... "$@"`: every function prints its result to stdout and NOTHING
# else does, so a caller can safely write `dir=$(nx_tools_dir) || exit 1`.
# Refusals print to stderr and print nothing to stdout -- a refusal that also
# emits a path is how a caller ends up installing into it. layout_core.py
# honours the same contract, including NX_LAYOUT_USAGE_EXIT on every refusal.

# gen-<stamp>: a prefix rather than a bare stamp, so a GC pass can tell a
# generation from anything else that lands in the root.
NX_GENERATION_PREFIX="gen-"

# The pointer every shim resolves. Always an ABSOLUTE symlink, so that plain
# `readlink` suffices; `readlink -f` is macOS >= 12.3 only.
NX_CURRENT_LINK_NAME="current"

# The rollback pointer, written by the flip (nexus-utpuw.3). GC's never-delete
# rule (b) protects "the previous current", and until .3 the layout gave that no
# on-disk representation at all -- GC would have had to approximate it from
# mtime, the heuristic this arc replaced with an exact readlink.
NX_PREVIOUS_LINK_NAME="previous"

# The ONE generation entry permitted to be a symlink, and the only route by
# which GC may ever delete something outside the tools root: .7 registers the
# legacy uv-tool tree as gen-legacy-uv-tool pointing at $(uv tool dir)/conexus.
# Defined here rather than in legacy.sh because gc.sh must recognise it WITHOUT
# sourcing legacy.sh — it deliberately does not, so that a reap can never fire
# during a migrating run.
NX_LEGACY_GENERATION_NAME="legacy-uv-tool"

# The nexus-owned receipt: the replacement for uv-receipt.toml and the only
# home extras have. Losing extras re-opens the 768->384 embedder downgrade.
NX_RECEIPT_NAME="nexus-install.json"
#: Written by install_generation.sh the instant a gen-* directory exists and
#: left in place; gc.sh reads its mtime as "a builder claimed this tree" and
#: keeps a receipt-less tree whose marker is younger than NX_GC_BUILD_CLAIM_MINUTES
#: (nexus-xn84f review: a slow resolve/download writes nothing into the tree).
NX_BUILDING_MARKER_NAME=".nx-building"
NX_RECEIPT_SCHEMA=1
NX_INSTALLER_SCHEMA=1

# Pinned against the Python dataclass by the twins test, so that the half
# which WRITES a receipt cannot add or drop a field the half which READS it
# does not know about.
NX_RECEIPT_FIELDS="schema version spec source_kind source extras python base_interpreter created_at installer_schema"

# EX_UNAVAILABLE. A specific status, so an operator seeing it in a log can
# tell "no current generation" from a command that merely failed.
NX_SHIM_NO_CURRENT_EXIT=70

# EX_USAGE, for every refusal reached through this file.
NX_LAYOUT_USAGE_EXIT=64

# Where an install came from. Pinned against the Python half's SOURCE_KINDS.
NX_SOURCE_KINDS="directory registry"

# ---------------------------------------------------------------------------
# The dispatcher
# ---------------------------------------------------------------------------

# Run one layout verb in layout_core.py, which sits beside this file.
#
# WHY THE CALLER HAS TO TELL US WHERE WE ARE. A sourced file cannot learn its
# own path under POSIX sh: there is no BASH_SOURCE in dash, and $0 is the
# SOURCING script. The obvious trick -- reading ${BASH_SOURCE[0]:-$0} here --
# works under bash and silently resolves to "sh" under `sh -c '. layout.sh'`,
# which is precisely how the twins test exercises this file. A mechanism that
# breaks in the harness that tests it is not a mechanism. So every caller sets
# NX_LAYOUT_HOME to the directory it already computed in order to source us.
#
# Refused loudly rather than guessed: a wrong guess here resolves paths against
# somebody else's tree, and this file exists to stop exactly that.
_nx_core() {
    if [ -z "${NX_LAYOUT_HOME-}" ]; then
        echo "nexus: NX_LAYOUT_HOME is unset. layout.sh is sourced, and a sourced" \
             "file cannot find its own directory under POSIX sh, so the sourcing" \
             "script must name it: NX_LAYOUT_HOME=\"\$_here\" before '. \$_here/layout.sh'." >&2
        return "$NX_LAYOUT_USAGE_EXIT"
    fi
    if [ ! -f "$NX_LAYOUT_HOME/layout_core.py" ]; then
        echo "nexus: no layout_core.py in NX_LAYOUT_HOME=$NX_LAYOUT_HOME." \
             "It ships beside layout.sh; an install that has one without the" \
             "other is incomplete." >&2
        return "$NX_LAYOUT_USAGE_EXIT"
    fi
    python3 "$NX_LAYOUT_HOME/layout_core.py" "$@"
}

# Which KIND of source a spec names, decided by SHAPE alone. See layout_core's
# source_kind for why shape and not existence: classifying by whether
# "$SOURCE/pyproject.toml" exists skipped the refusal that stops a PyPI install
# from wiping a dev checkout's unreleased modules (nexus-pk9yt).
# $1 source spec.
nx_source_kind() { _nx_core source_kind "${1-}"; }

# The generation root. Recomputed on every call, never cached: release-sandbox.sh
# and tests/e2e/run.sh isolate themselves ONLY by redirecting $HOME, so a value
# captured once would make those harnesses write into the live install.
nx_tools_dir() { _nx_core tools_dir; }

# The directory the shims are written into. Recomputed on every call.
nx_bin_dir() { _nx_core bin_dir; }

# <tools>/gen-<stamp>: the directory one install builds and owns.
# $1 stamp.  $2 optional tools root.
nx_generation_dir() { _nx_core generation_dir "${1-}" "${2-}"; }

# <tools>/current: the pointer a flip moves and every shim reads.
# $1 optional tools root.
nx_current_link() { _nx_core current_link "${1-}"; }

# <tools>/previous: the generation a rollback returns to.
# $1 optional tools root.
nx_previous_link() { _nx_core previous_link "${1-}"; }

# An explicit tools root, or a resolved one when the caller gave none.
#
# PUBLIC, and it was not before. This was `_nx_root`, a private helper, and
# gc.sh, census.sh and legacy.sh all called it across the file boundary anyway
# -- which made it part of the interface in fact while being named as though it
# were not, and is why deleting it during the collapse broke three scripts that
# no search for `nx_*` would have found. Named for what it is now.
#
# $1 optional tools root.
nx_root() { _nx_core root "${1-}"; }

# The receipt inside a generation, which must already be an absolute path.
# $1 generation directory.
nx_receipt_path() { _nx_core receipt_path "${1-}"; }

# The body of <bin>/<command>.
#
# The absolute tools path is baked in, which makes a written shim
# $HOME-independent -- and therefore means shims must be REWRITTEN when
# NX_TOOLS_DIR changes and cannot be shared between sandboxes.
#
# $1 command.  $2 optional tools root.
nx_render_shim() { _nx_core render_shim "${1-}" "${2-}"; }

# The one place a PEP 508 install spec is assembled. Extras PRECEDE the version
# pin -- `conexus[local]==7.18.0` is valid, `conexus==7.18.0[local]` is not.
#
# $1 base (distribution name, or a path for a directory install)
# $2 extras, comma-separated, may be empty
# $3 version, may be empty (a directory install pins nothing)
nx_build_spec() { _nx_core build_spec "${1-}" "${2-}" "${3-}"; }

# The receipt, rendered by the same code that reads it.
#
# $1 version  $2 spec  $3 source_kind  $4 source  $5 extras (comma-separated,
# may be empty)  $6 python  $7 base_interpreter  $8 created_at
nx_render_receipt() {
    _nx_core render_receipt "${1-}" "${2-}" "${3-}" "${4-}" "${5-}" "${6-}" "${7-}" "${8-}"
}
