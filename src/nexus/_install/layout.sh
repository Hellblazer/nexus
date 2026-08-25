#!/usr/bin/env sh
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
#
# The generation layout, stated for shell. Twin of src/nexus/install_layout.py;
# tests/test_install_layout_twins_agree.py is what says the two agree.
#
# nexus-utpuw.1 (P0). Two implementations exist because the callers have
# incompatible import constraints. The generation builder (.2) and the shim
# writer (.4) run from scripts/reinstall-tool.sh, which may run with NOTHING
# installed and therefore cannot import nexus. health.py and upgrade_finish.py
# run after the install and can. Neither half may be edited alone: if this one
# drifts, an install lands in a directory the Python half cannot find, and the
# symptom is not a failure but a doctor reporting green about a tree nobody is
# running from.
#
# SOURCED, NEVER EXECUTED. No shebang, and deliberately no `set -e`: this file
# is dotted into its callers, and options set here would silently change how
# every one of them handles an unrelated failure. Functions signal by exit
# status; callers decide.
#
# Every function prints its result to stdout and nothing else, so that a
# caller can safely write `dir=$(nx_tools_dir) || exit 1`. Refusals print to
# stderr and print NOTHING to stdout -- a refusal that also emits a path is
# how a caller ends up installing into it.

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
NX_RECEIPT_SCHEMA=1
NX_INSTALLER_SCHEMA=1

# Pinned against the Python dataclass by the twins test, so that the half
# which WRITES a receipt cannot add or drop a field the half which READS it
# does not know about.
NX_RECEIPT_FIELDS="schema version spec source_kind source extras python base_interpreter created_at installer_schema"

# EX_UNAVAILABLE. A specific status, so an operator seeing it in a log can
# tell "no current generation" from a command that merely failed.
NX_SHIM_NO_CURRENT_EXIT=70

# EX_USAGE, for every refusal in this file.
NX_LAYOUT_USAGE_EXIT=64

# Where an install came from. Pinned against the Python half's SOURCE_KINDS.
NX_SOURCE_KINDS="directory registry"

# Make a value safe to place inside a JSON string. Backslashes first, then
# quotes -- the other order double-escapes. A value carrying a control
# character (a newline in a path, say) cannot be represented by this escaper,
# so it is REFUSED: emitting JSON the Python half will reject is strictly
# worse than saying no here, where the message can name the field.
#
# $1 value. Prints the escaped form; non-zero and silent on a refusal.
_nx_json_escape() {
    case $1 in
        *[[:cntrl:]]*)
            echo "nexus: receipt values must not contain control characters" >&2
            return "$NX_LAYOUT_USAGE_EXIT"
            ;;
    esac
    printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g'
}

# The one override rule, shared by both directory variables. Five states:
# unset and empty both fall back to the $HOME-derived default (`Path("")` is
# `Path(".")` on the Python side, so honouring an exported-but-empty variable
# would root the install at the caller's CWD); an absolute path is used
# verbatim; a leading ~ is expanded, because a config file or a launchd plist
# does not expand it the way a shell would; and a relative path is REFUSED
# rather than anchored to a guess.
#
# $1 variable name, for the message.  $2 raw value.  $3 default.
_nx_resolve_dir() {
    _nx_raw=$(printf '%s' "$2" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')

    if [ -z "$_nx_raw" ]; then
        printf '%s\n' "$3"
        return 0
    fi

    case $_nx_raw in
        '~') _nx_raw=$HOME ;;
        '~/'*) _nx_raw=$HOME/${_nx_raw#'~/'} ;;
    esac

    case $_nx_raw in
        /*) printf '%s\n' "$_nx_raw" ;;
        *)
            echo "nexus: $1=$2 is not an absolute path. The generation layout is" \
                 "resolved from processes whose working directory is not stable," \
                 "so a relative override is refused rather than anchored to a guess." >&2
            return "$NX_LAYOUT_USAGE_EXIT"
            ;;
    esac
}

# The generation root. Recomputed on every call, never cached: release-sandbox.sh
# and tests/e2e/run.sh isolate themselves ONLY by redirecting $HOME, so a value
# captured once would make those harnesses write into the live install.
nx_tools_dir() {
    _nx_resolve_dir NX_TOOLS_DIR "${NX_TOOLS_DIR-}" "$HOME/.local/share/nexus/tools"
}

# The directory the shims are written into. Recomputed on every call.
nx_bin_dir() {
    _nx_resolve_dir NX_BIN_DIR "${NX_BIN_DIR-}" "$HOME/.local/bin"
}

# Refuse anything that is not a plain, single path component. An ALLOWLIST,
# matching _COMPONENT_RE in the Python half, and deliberately so: an earlier
# denylist here rejected separators, traversals and whitespace -- every PATH
# hazard -- and still admitted `nx$(touch${IFS}PWNED)`, which lands inside a
# double-quoted string in the rendered shim and executes on the next
# invocation. The sink's hazard alphabet is not the path's. Audit finding F1
# has the shim set DERIVED from installed-distribution entry_points, so these
# names arrive from third-party wheels.
#
# $1 label.  $2 value.
_nx_require_component() {
    case $2 in
        ''|[!A-Za-z0-9]*) _nx_bad_component "$1" "$2"; return "$NX_LAYOUT_USAGE_EXIT" ;;
    esac
    case $2 in
        *[!A-Za-z0-9._-]*) _nx_bad_component "$1" "$2"; return "$NX_LAYOUT_USAGE_EXIT" ;;
    esac
    return 0
}

_nx_bad_component() {
    echo "nexus: $1 must be letters, digits, '.', '-' and '_', not leading" \
         "with '.' or '-', got '$2'" >&2
}

# $1 explicit tools root, or empty to resolve one.
_nx_root() {
    if [ -n "${1-}" ]; then
        printf '%s\n' "$1"
        return 0
    fi
    nx_tools_dir
}

# <tools>/gen-<stamp>: the directory one install builds and owns.
# $1 stamp.  $2 optional tools root.
nx_generation_dir() {
    _nx_require_component "generation stamp" "$1" || return "$NX_LAYOUT_USAGE_EXIT"
    _nx_gd_root=$(_nx_root "${2-}") || return "$NX_LAYOUT_USAGE_EXIT"
    printf '%s\n' "$_nx_gd_root/$NX_GENERATION_PREFIX$1"
}

# <tools>/current: the pointer a flip moves and every shim reads.
# $1 optional tools root.
nx_current_link() {
    _nx_cl_root=$(_nx_root "${1-}") || return "$NX_LAYOUT_USAGE_EXIT"
    printf '%s\n' "$_nx_cl_root/$NX_CURRENT_LINK_NAME"
}

# <tools>/previous: the generation a rollback returns to.
# $1 optional tools root.
nx_previous_link() {
    _nx_pl_root=$(_nx_root "${1-}") || return "$NX_LAYOUT_USAGE_EXIT"
    printf '%s\n' "$_nx_pl_root/$NX_PREVIOUS_LINK_NAME"
}

# The receipt inside a generation, which must already be an absolute path.
# $1 generation directory.
nx_receipt_path() {
    case ${1-} in
        /*) printf '%s\n' "$1/$NX_RECEIPT_NAME" ;;
        *)
            echo "nexus: a generation path must be absolute, got '${1-}'" >&2
            return "$NX_LAYOUT_USAGE_EXIT"
            ;;
    esac
}

# The body of <bin>/<command>.
#
# The absolute tools path is baked in, which makes a written shim
# $HOME-independent -- and therefore means shims must be REWRITTEN when
# NX_TOOLS_DIR changes and cannot be shared between sandboxes.
#
# $1 command.  $2 optional tools root.
nx_render_shim() {
    _nx_require_component "shim command" "$1" || return "$NX_LAYOUT_USAGE_EXIT"
    _nx_shim_root=$(_nx_root "${2-}") || return "$NX_LAYOUT_USAGE_EXIT"
    _nx_shim_ptr="$_nx_shim_root/$NX_CURRENT_LINK_NAME"

    printf '%s\n' \
        '#!/bin/sh' \
        '# Generated by nexus. Rewritten on every install; edits are lost.' \
        '#' \
        '# The pointer is resolved BEFORE the exec, and that ordering is' \
        '# load-bearing rather than stylistic. CPython looks for pyvenv.cfg next' \
        '# to the executable as it was INVOKED, before it resolves symlinks, so' \
        '# an exec through the pointer itself would leak that component into' \
        '# sys.prefix and sys.path -- and the next flip would retarget every' \
        '# not-yet-imported module in a process that was already running' \
        '# (nexus-q3xrx).' \
        "NX_GEN=\"\$(readlink \"$_nx_shim_ptr\")\" || {" \
        "    echo \"nexus: $1: no current generation at $_nx_shim_ptr\" >&2" \
        "    exit $NX_SHIM_NO_CURRENT_EXIT" \
        '}' \
        "exec \"\$NX_GEN/bin/$1\" \"\$@\""
}

# The one place a PEP 508 install spec is assembled. Extras PRECEDE the
# version pin -- `conexus[local]==7.18.0` is valid, `conexus==7.18.0[local]`
# is not. The fixup lived in scripts/reinstall-tool.sh:157-158 and the
# generation builder would otherwise restate it; a spec and its extras must
# not be able to disagree.
#
# $1 base (distribution name, or a path for a directory install)
# $2 extras, comma-separated, may be empty
# $3 version, may be empty (a directory install pins nothing)
nx_build_spec() {
    _nx_spec=$1
    if [ -n "${2-}" ]; then
        _nx_spec_extras=$(printf '%s' "$2" | tr ',' '\n' | grep -v '^$' \
                          | LC_ALL=C sort -u | tr '\n' ',' | sed 's/,$//')
        [ -n "$_nx_spec_extras" ] && _nx_spec="$_nx_spec[$_nx_spec_extras]"
    fi
    [ -n "${3-}" ] && _nx_spec="$_nx_spec==$3"
    printf '%s\n' "$_nx_spec"
}

# The receipt, rendered. The generation builder (.2) supplies the values; the
# SHAPE is fixed here so that the half which writes a receipt and the half
# which reads it cannot disagree about what a receipt is.
#
# $1 version  $2 spec  $3 source_kind  $4 source  $5 extras (comma-separated,
# may be empty)  $6 python  $7 base_interpreter  $8 created_at
nx_render_receipt() {
    _nx_kind_ok=0
    for _nx_kind in $NX_SOURCE_KINDS; do
        [ "$3" = "$_nx_kind" ] && _nx_kind_ok=1
    done
    if [ "$_nx_kind_ok" -ne 1 ]; then
        echo "nexus: source_kind must be one of $NX_SOURCE_KINDS, got '$3'" >&2
        return "$NX_LAYOUT_USAGE_EXIT"
    fi

    for _nx_value in "$1" "$2" "$4" "${5-}" "$6" "$7" "$8"; do
        _nx_json_escape "$_nx_value" >/dev/null || return "$NX_LAYOUT_USAGE_EXIT"
    done

    # Sorted and de-duplicated, so a receipt is stable across installs and the
    # spec the builder derives from it is deterministic. Matches the Python
    # half's __post_init__.
    _nx_extras_json=""
    if [ -n "${5-}" ]; then
        # LC_ALL=C so the order matches Python's sorted() byte-ordering. Under
        # a UTF-8 locale `sort` collates differently and the two halves would
        # write different receipts for the same extras.
        _nx_extras_sorted=$(printf '%s' "$5" | tr ',' '\n' | grep -v '^$' | LC_ALL=C sort -u)
        for _nx_extra in $_nx_extras_sorted; do
            if [ -n "$_nx_extras_json" ]; then
                _nx_extras_json="$_nx_extras_json, \"$(_nx_json_escape "$_nx_extra")\""
            else
                _nx_extras_json="\"$(_nx_json_escape "$_nx_extra")\""
            fi
        done
    fi

    printf '%s\n' \
        '{' \
        "  \"base_interpreter\": \"$(_nx_json_escape "$7")\"," \
        "  \"created_at\": \"$(_nx_json_escape "$8")\"," \
        "  \"extras\": [$_nx_extras_json]," \
        "  \"installer_schema\": $NX_INSTALLER_SCHEMA," \
        "  \"python\": \"$(_nx_json_escape "$6")\"," \
        "  \"schema\": $NX_RECEIPT_SCHEMA," \
        "  \"source\": \"$(_nx_json_escape "$4")\"," \
        "  \"source_kind\": \"$(_nx_json_escape "$3")\"," \
        "  \"spec\": \"$(_nx_json_escape "$2")\"," \
        "  \"version\": \"$(_nx_json_escape "$1")\"" \
        '}'
}
