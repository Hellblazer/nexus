#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/e2e/lib/python_test.sh: shell-level tests for lib/python.sh (nexus-u67ow).
# Self-provisioning: every interpreter here is a stub script in a throwaway
# directory that reports a version of our choosing, and each case runs in a
# clean `env -i` shell whose PATH is that directory alone, so the box's real
# python (and a real uv) can never answer for a stub. Run directly:
# `bash tests/e2e/lib/python_test.sh`.
set -u -o pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/python_test.XXXXXX")"
trap 'rm -rf "$WORKDIR"' EXIT

PASS=0
FAIL=0
ok() { echo "  [ok] $1"; PASS=$((PASS + 1)); }
bad() { echo "  [FAIL] $1"; FAIL=$((FAIL + 1)); }
expect_eq() {  # <label> <want> <got>
    if [ "$2" = "$3" ]; then ok "$1"; else bad "$1: want [$2] got [$3]"; fi
}
expect_has() {  # <label> <haystack> <needle>
    case "$2" in *"$3"*) ok "$1" ;; *) bad "$1: missing [$3] in: $2" ;; esac
}
expect_lacks() {  # <label> <haystack> <needle>
    case "$2" in *"$3"*) bad "$1: unexpected [$3] in: $2" ;; *) ok "$1" ;; esac
}

# A directory of stub interpreters: stubs <dir> <name>=<reported-version>...
# A stub ignores its arguments and prints the version, which is all the
# resolver's probe reads. Stdin is not consumed (the resolver closes it).
mkbin() {  # <dir> <name>=<version>...
    local d="$1" spec; shift
    mkdir -p "$d"
    for spec in "$@"; do
        printf '#!/bin/sh\necho %s\n' "${spec#*=}" >"$d/${spec%%=*}"
        chmod +x "$d/${spec%%=*}"
    done
}

# Run the resolver in a clean shell with PATH=<dir> only. Prints rc, the
# resolved interpreter and stderr on three labelled lines.
resolve_in() {  # <dir> <min-minor|-> [VAR=value...]
    local dir="$1" min="$2"; shift 2
    [ "$min" = "-" ] && min=""
    env -i PATH="$dir" HOME="$WORKDIR" "$@" "$BASH" -c '
source "$1/python.sh"
e2e_python_resolve '"$min"' 2>"$2"; rc=$?
printf "rc=%s\nresolved=%s\n" "$rc" "${E2E_PYTHON:-}"
' _ "$HERE" "$WORKDIR/err"
}
field() { printf '%s\n' "$1" | sed -n "s/^$2=//p"; }

# ── 1. the hellmini case: python3 is 3.9, nothing else exists ────────────────
echo "Test 1: a PATH whose python3 reports 3.9.6 and nothing else fails closed naming the version"
B="$WORKDIR/b1"; mkbin "$B" python3=3.9.6
out="$(resolve_in "$B" -)"; err="$(cat "$WORKDIR/err")"
expect_eq "refuses with rc 2" "2" "$(field "$out" rc)"
expect_eq "resolves nothing" "" "$(field "$out" resolved)"
expect_has "names the version it found" "$err" "is 3.9.6"
expect_has "names the candidate and its path" "$err" "python3 -> $B/python3"
expect_has "says what floor it wanted" "$err" "no Python 3.10 or newer found"
expect_has "says how to fix it" "$err" "NX_E2E_PYTHON"

# ── 2. a qualifying interpreter elsewhere on PATH is picked over the 3.9 ─────
echo "Test 2: python3 is 3.9 but python3.12 exists: the resolver picks python3.12"
B="$WORKDIR/b2"; mkbin "$B" python3=3.9.6 python3.12=3.12.4
out="$(resolve_in "$B" -)"
expect_eq "rc 0" "0" "$(field "$out" rc)"
expect_eq "picked python3.12, not python3" "$B/python3.12" "$(field "$out" resolved)"

echo "Test 3: python3 itself qualifying wins over a newer versioned name (python3 is what a bare call meant)"
B="$WORKDIR/b3"; mkbin "$B" python3=3.11.2 python3.13=3.13.0
out="$(resolve_in "$B" -)"
expect_eq "picked python3" "$B/python3" "$(field "$out" resolved)"

echo "Test 4: newest qualifying versioned name wins when python3 does not qualify"
B="$WORKDIR/b4"; mkbin "$B" python3=3.9.6 python3.10=3.10.1 python3.13=3.13.0 python3.11=3.11.9
out="$(resolve_in "$B" -)"
expect_eq "picked python3.13" "$B/python3.13" "$(field "$out" resolved)"

# ── 5. the floor ─────────────────────────────────────────────────────────────
echo "Test 5: 3.10 qualifies at the default floor and is refused at 11 (tomllib), naming 3.10.8"
B="$WORKDIR/b5"; mkbin "$B" python3=3.10.8
out="$(resolve_in "$B" -)"
expect_eq "default floor accepts 3.10.8" "$B/python3" "$(field "$out" resolved)"
out="$(resolve_in "$B" 11)"; err="$(cat "$WORKDIR/err")"
expect_eq "floor 11 refuses 3.10.8" "2" "$(field "$out" rc)"
expect_has "floor 11 names 3.10.8" "$err" "is 3.10.8"
expect_has "floor 11 says 3.11" "$err" "no Python 3.11 or newer found"

echo "Test 6: a 4.x interpreter qualifies (major compared before minor)"
B="$WORKDIR/b6"; mkbin "$B" python3=4.0.0
out="$(resolve_in "$B" 12)"
expect_eq "4.0.0 satisfies >= 3.12" "$B/python3" "$(field "$out" resolved)"

# ── 7. unusable interpreters are named, not skipped silently ─────────────────
echo "Test 7: a python3 that prints no version is reported as unusable"
B="$WORKDIR/b7"; mkdir -p "$B"
printf '#!/bin/sh\nexit 0\n' >"$B/python3"; chmod +x "$B/python3"
out="$(resolve_in "$B" -)"; err="$(cat "$WORKDIR/err")"
expect_eq "refuses" "2" "$(field "$out" rc)"
expect_has "says it did not report a version" "$err" "did not report a version"

echo "Test 8: a python3 that exits non-zero is reported as unusable"
B="$WORKDIR/b8"; mkdir -p "$B"
printf '#!/bin/sh\necho "broken shim" >&2\nexit 3\n' >"$B/python3"; chmod +x "$B/python3"
out="$(resolve_in "$B" -)"; err="$(cat "$WORKDIR/err")"
expect_eq "refuses" "2" "$(field "$out" rc)"
expect_has "names python3 as unusable" "$err" "python3 -> $B/python3: did not report a version"

# ── 9. NX_E2E_PYTHON: an explicit choice binds ───────────────────────────────
echo "Test 9: NX_E2E_PYTHON naming a 3.9 stub refuses even though python3.12 is on PATH"
B="$WORKDIR/b9"; mkbin "$B" python3=3.9.6 python3.12=3.12.4 mine=3.9.1
out="$(resolve_in "$B" - NX_E2E_PYTHON=mine)"; err="$(cat "$WORKDIR/err")"
expect_eq "refuses" "2" "$(field "$out" rc)"
expect_has "names the explicit choice and its version" "$err" "NX_E2E_PYTHON=mine ($B/mine) is 3.9.1"
expect_has "says no other interpreter is tried" "$err" "no other interpreter is tried"

echo "Test 10: NX_E2E_PYTHON naming a qualifying stub wins over python3"
B="$WORKDIR/b10"; mkbin "$B" python3=3.12.0 mine=3.13.2
out="$(resolve_in "$B" - NX_E2E_PYTHON=mine)"
expect_eq "picked the named interpreter" "$B/mine" "$(field "$out" resolved)"

echo "Test 11: NX_E2E_PYTHON naming something that does not exist refuses"
B="$WORKDIR/b11"; mkbin "$B" python3=3.12.0
out="$(resolve_in "$B" - NX_E2E_PYTHON=/nonexistent/python)"; err="$(cat "$WORKDIR/err")"
expect_eq "refuses" "2" "$(field "$out" rc)"
expect_has "says it is not an executable" "$err" "is not an executable"

# ── 12. a parent's resolution is reused only while it still qualifies ────────
echo "Test 12: an inherited E2E_PYTHON that qualifies is kept without a PATH search"
B="$WORKDIR/b12"; mkbin "$B" python3=3.9.6 inherited=3.12.4
out="$(resolve_in "$B" - E2E_PYTHON="$B/inherited")"
expect_eq "kept the parent's interpreter" "$B/inherited" "$(field "$out" resolved)"

echo "Test 13: an inherited E2E_PYTHON below a child's higher floor is re-resolved, not trusted"
B="$WORKDIR/b13"; mkbin "$B" python3=3.9.6 python3.12=3.12.4 inherited=3.10.2
out="$(resolve_in "$B" 11 E2E_PYTHON="$B/inherited")"
expect_eq "moved to python3.12" "$B/python3.12" "$(field "$out" resolved)"

echo "Test 14: an inherited E2E_PYTHON that is stale (3.9) does not mask a real failure"
B="$WORKDIR/b14"; mkbin "$B" python3=3.9.6 inherited=3.9.6
out="$(resolve_in "$B" - E2E_PYTHON="$B/inherited")"; err="$(cat "$WORKDIR/err")"
expect_eq "refuses" "2" "$(field "$out" rc)"
expect_has "still names the 3.9.6 it found" "$err" "is 3.9.6"

# ── 15. uv knows an interpreter PATH does not ────────────────────────────────
echo "Test 15: uv python find supplies an interpreter when PATH has only a 3.9 python3"
B="$WORKDIR/b15"; mkbin "$B" python3=3.9.6 uvpython=3.12.7
printf '#!/bin/sh\n[ "$1 $2" = "python find" ] && [ "$3" = ">=3.10" ] && echo "%s/uvpython" && exit 0\nexit 1\n' "$B" >"$B/uv"
chmod +x "$B/uv"
out="$(resolve_in "$B" -)"
expect_eq "rc 0" "0" "$(field "$out" rc)"
expect_eq "picked the interpreter uv named" "$B/uvpython" "$(field "$out" resolved)"

echo "Test 16: uv that finds nothing is named in the refusal"
B="$WORKDIR/b16"; mkbin "$B" python3=3.9.6
printf '#!/bin/sh\nexit 1\n' >"$B/uv"; chmod +x "$B/uv"
out="$(resolve_in "$B" -)"; err="$(cat "$WORKDIR/err")"
expect_eq "refuses" "2" "$(field "$out" rc)"
expect_has "says uv found nothing" "$err" "uv python find >=3.10: nothing found"

# ── 17. input hygiene and export ─────────────────────────────────────────────
echo "Test 17: a non-numeric floor is refused rather than compared as text"
B="$WORKDIR/b17"; mkbin "$B" python3=3.12.0
out="$(resolve_in "$B" abc)"; err="$(cat "$WORKDIR/err")"
expect_eq "refuses" "2" "$(field "$out" rc)"
expect_has "says why" "$err" "min-minor must be an integer"

echo "Test 18: the resolved interpreter is EXPORTED, so a child process sees it"
B="$WORKDIR/b18"; mkbin "$B" python3=3.12.0
seen="$(env -i PATH="$B" HOME="$WORKDIR" "$BASH" -c 'source "$1/python.sh"; e2e_python_resolve || exit 9; "$2" -c '"'"'printf "%s" "${E2E_PYTHON:-}"'"'"'' _ "$HERE" "$BASH" 2>/dev/null)"
expect_eq "E2E_PYTHON is in a child's environment" "$B/python3" "$seen"

echo "Test 19: sourcing runs nothing and defines only functions"
out="$(env -i PATH="$WORKDIR/none" HOME="$WORKDIR" "$BASH" -c 'source "$1/python.sh" && echo "sourced rc=$? E2E_PYTHON=[${E2E_PYTHON:-}]"' _ "$HERE")"
expect_eq "no side effect at source time" "sourced rc=0 E2E_PYTHON=[]" "$out"

echo
echo "python_test.sh: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]
