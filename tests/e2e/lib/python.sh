#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Source this; it defines functions only and runs nothing (nexus-u67ow).
#
# One interpreter for the host side of the e2e harness. Bare `python3` is
# whatever the box ships first on PATH, and on a macOS host with no Homebrew
# python that is /usr/bin/python3, which is 3.9.6. tests/e2e/lib/
# artifact_manifest.py uses `match` (3.10+), so on that box the 2026-10-02 cut
# battery aborted with "artifacts manifest does not verify against this tree
# (SyntaxError ...)" and no engine leg ran: a harness defect that read as a
# manifest mismatch. The harness now resolves an interpreter ONCE, checks its
# version, and refuses by name when none qualifies.
#
#     source "$REPO_ROOT/tests/e2e/lib/python.sh"
#     e2e_python_resolve || exit 2        # sets and exports E2E_PYTHON
#     "$E2E_PYTHON" "$REPO_ROOT/tests/e2e/lib/artifact_manifest.py" verify ...
#
# e2e_python_resolve [min-minor]   (default 10: Python 3.<min-minor> or newer;
#                                   pass 11 for a caller that reads tomllib)
#
# Order of candidates, first one that qualifies wins:
#   1. NX_E2E_PYTHON        an operator's explicit choice. If it does not
#                           qualify the resolver refuses; it never falls past a
#                           named interpreter to some other one.
#   2. E2E_PYTHON           a parent script's resolution, accepted only when it
#                           still qualifies (a child may ask for a higher floor).
#   3. python3, then python3.14 ... python3.<min-minor> on PATH.
#   4. `uv python find '>=3.<min-minor>'`, when uv is on PATH.
#
# Failure prints one block on stderr naming every candidate and the version it
# reported (or why it was unusable), so a 3.9 box says "python3 -> /usr/bin/
# python3 is 3.9.6", and returns 2. A caller that cannot proceed exits there;
# nothing past this call runs on an interpreter that was never checked.
#
# The probe asks the interpreter itself (`sys.version_info`) rather than
# parsing `--version`, so a wrapper script that prints a banner is judged by
# what it actually runs.

# Print "<major>.<minor>.<micro>" for interpreter $1, or return 1 when it will
# not say. Stdin is closed so a stub that reads it cannot hang the resolver.
_e2e_py_version() {  # <interpreter>
    local out
    out="$("$1" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])' 2>/dev/null </dev/null)" || return 1
    case "$out" in
        [0-9]*.[0-9]*.[0-9]*) printf '%s' "$out" ;;
        *) return 1 ;;
    esac
}

# 0 when "<M>.<m>.<p>" is at least 3.<min-minor>.
_e2e_py_qualifies() {  # <version> <min-minor>
    local major minor
    major="${1%%.*}"
    minor="${1#*.}"; minor="${minor%%.*}"
    [ "$major" -gt 3 ] || { [ "$major" -eq 3 ] && [ "$minor" -ge "$2" ]; }
}

e2e_python_resolve() {  # [min-minor]
    local min="${1:-10}" cand path ver notes="" order=() i
    case "$min" in ''|*[!0-9]*) echo "e2e_python_resolve: min-minor must be an integer, got '$min'" >&2; return 2 ;; esac

    # 1. The operator's explicit choice binds: qualify or refuse.
    if [ -n "${NX_E2E_PYTHON:-}" ]; then
        path="$(command -v "$NX_E2E_PYTHON" 2>/dev/null)" || path=""
        if [ -z "$path" ]; then
            echo "e2e python: NX_E2E_PYTHON=$NX_E2E_PYTHON is not an executable; refusing (it is an explicit choice, so no other interpreter is tried)." >&2
            return 2
        fi
        if ver="$(_e2e_py_version "$path")" && _e2e_py_qualifies "$ver" "$min"; then
            E2E_PYTHON="$path"; export E2E_PYTHON
            return 0
        fi
        if [ -n "$ver" ]; then
            echo "e2e python: NX_E2E_PYTHON=$NX_E2E_PYTHON ($path) is $ver, older than 3.$min; refusing (it is an explicit choice, so no other interpreter is tried)." >&2
        else
            echo "e2e python: NX_E2E_PYTHON=$NX_E2E_PYTHON ($path) did not report a version (unusable); refusing (it is an explicit choice, so no other interpreter is tried)." >&2
        fi
        return 2
    fi

    # 2. A parent's resolution, if it still qualifies.
    if [ -n "${E2E_PYTHON:-}" ] && [ -x "$E2E_PYTHON" ]; then
        if ver="$(_e2e_py_version "$E2E_PYTHON")" && _e2e_py_qualifies "$ver" "$min"; then
            export E2E_PYTHON
            return 0
        fi
    fi

    # 3. PATH: python3 first (what the bare call used to mean), then the
    # versioned names, newest first.
    order=(python3)
    for ((i = 14; i >= min; i--)); do order+=("python3.$i"); done
    for cand in "${order[@]}"; do
        path="$(command -v "$cand" 2>/dev/null)" || { notes+="  $cand: not on PATH"$'\n'; continue; }
        if ! ver="$(_e2e_py_version "$path")"; then
            notes+="  $cand -> $path: did not report a version (unusable)"$'\n'
            continue
        fi
        if _e2e_py_qualifies "$ver" "$min"; then
            E2E_PYTHON="$path"; export E2E_PYTHON
            return 0
        fi
        notes+="  $cand -> $path: is $ver"$'\n'
    done

    # 4. uv knows about interpreters that are not on PATH.
    if command -v uv >/dev/null 2>&1; then
        path="$(uv python find ">=3.$min" 2>/dev/null </dev/null)" || path=""
        if [ -n "$path" ] && ver="$(_e2e_py_version "$path")" && _e2e_py_qualifies "$ver" "$min"; then
            E2E_PYTHON="$path"; export E2E_PYTHON
            return 0
        fi
        notes+="  uv python find >=3.$min: ${path:-nothing found}"$'\n'
    else
        notes+="  uv: not on PATH"$'\n'
    fi

    {
        echo "e2e python: no Python 3.$min or newer found; the e2e harness needs one (artifact_manifest.py uses match, 3.10+; a script that reads tomllib asks for 3.11)."
        printf '%s' "$notes"
        echo "  Install one (brew install python@3.12, or uv python install 3.12) or point NX_E2E_PYTHON at it."
    } >&2
    return 2
}
