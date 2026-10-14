#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/e2e/hook-cli-skew/run.sh -- nexus-rcoze: this checkout's plugin cannot
# block a session under ANY conexus CLI a user may still have.
#
# A plugin can update before its CLI, and a user can skip releases, so the
# plugin's hooks.json runs against every CLI still in the field. `nx-hook`
# from 7.55.0 (its first release) to 7.57.x exits 2 on a verb it does not
# register, and exit 2 blocks on UserPromptSubmit, PreToolUse and
# PermissionRequest (nexus-t9klx, caught by the 7.58.0 battery). This gate
# installs each real published CLI from PyPI, plus this checkout's own wheel
# and a box with no CLI at all, and fires every command-tier hooks.json entry
# against each (tests/e2e/hook-cli-skew/drive.py). Any exit 2, or stdout that
# is not JSON, fails the gate.
#
# Every entry starts with `uv tool run ... python launch/nx_launch.py ...`
# (RDR-228), so drive.py puts `uv` alone on the driven PATH and points
# NX_TOOLS_DIR at an empty directory, which makes the launcher find the CLI
# under test on PATH rather than this machine's install. An entry that did not
# start, or fewer than the floor of started entries, fails the gate: a driver
# whose PATH lacks `uv` used to count every entry "command not found" and pass.
#
# Positive control first: the raw `nx-hook` of 7.57.0 must still exit 2 on an
# unknown verb here, or this environment does not reproduce the failure and
# a clean pass would mean nothing.
#
# Network: PyPI installs, cached per version under $NX_SKEW_CACHE (default
# ${TMPDIR:-/tmp}/nx-hook-cli-skew-cache). Never touches the real HOME.
# Must end HOOK-CLI SKEW GATE PASSED. Exit 1 on any failure, 2 when a
# dependency (uv, network) is absent: "could not check" is never a pass.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../../.." && pwd)"
# One interpreter >= 3.10, resolved once; never a bare python3 (nexus-u67ow).
# shellcheck source=lib/python.sh disable=SC1091
source "$ROOT/tests/e2e/lib/python.sh"
e2e_python_resolve || exit 2
CACHE="${NX_SKEW_CACHE:-${TMPDIR:-/tmp}/nx-hook-cli-skew-cache}"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/nx-hook-cli-skew.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
_fail() { echo "HOOK-CLI SKEW GATE FAILED -- $*" >&2; exit 1; }
_unverified() { echo "HOOK-CLI SKEW GATE UNVERIFIED -- $*" >&2; exit 2; }
command -v uv >/dev/null || _unverified "uv not on PATH"

# Every release whose nx-hook fails closed, the first that fails open, and the
# newest published. Extend when a release ships; never drop one: those CLIs
# are still installed somewhere.
VERSIONS=(7.55.0 7.55.3 7.56.0 7.57.0 7.58.0 7.59.0)

_cli() {  # $1 = version -> prints its bin dir, installing on a cache miss
    local v="$1" d="$CACHE/cli-$1"
    if [ ! -x "$d/bin/nx-hook" ]; then
        rm -rf "$d"
        uv venv -q --python 3.12 "$d" >/dev/null 2>&1 || _unverified "uv venv for $v failed"
        uv pip install -q --python "$d/bin/python" "conexus==$v" >"$WORK/install-$v.log" 2>&1 \
            || _unverified "installing conexus==$v failed (network/PyPI): $(tail -2 "$WORK/install-$v.log" | tr '\n' ' ')"
    fi
    echo "$d/bin"
}

mkdir -p "$CACHE"
# uv's own caches are shared across the driven runs: drive.py gives each entry a
# scratch HOME, and a HOME-relative cache would be cold for every CLI.
export UV_CACHE_DIR="${UV_CACHE_DIR:-$CACHE/uv-cache}"
UV_PYTHON_INSTALL_DIR="${UV_PYTHON_INSTALL_DIR:-$(uv python dir)}"
export UV_PYTHON_INSTALL_DIR
echo "── positive control: raw nx-hook 7.57.0 on an unknown verb ──"
BIN57="$(_cli 7.57.0)"
set +e
echo '{}' | env -i HOME="$WORK/pc" PATH="$BIN57:/usr/bin:/bin" nx-hook no-such-verb-skew-gate >/dev/null 2>"$WORK/pc.err"
PRC=$?
set -e
[ "$PRC" = 2 ] || _fail "positive control: the 7.57.0 CLI exited $PRC on an unknown verb, not 2; this environment does not reproduce the failure"
grep -q "unknown verb" "$WORK/pc.err" || _fail "positive control: 7.57.0 stderr did not name the unknown verb: $(cat "$WORK/pc.err")"
echo "  the 7.57.0 CLI exits 2 on an unknown verb, as in the field"

echo "── negative control: the driver must catch a direct unknown verb ──"
# Pinned, not a one-off (critic finding, nexus-rcoze): a hooks.json wiring
# `nx-hook mcp-connect-check` directly, as the t9klx tree did, run under the
# 7.57.0 CLI. drive.py must report it as blocking; if it passes, the driver
# has gone blind and every PASSED below means nothing.
mkdir -p "$WORK/negctl"
cat > "$WORK/negctl/hooks.json" <<'JSON'
{"hooks": {"UserPromptSubmit": [{"matcher": "", "hooks": [
  {"type": "command", "command": "nx-hook", "args": ["mcp-connect-check"], "timeout": 5}]}]}}
JSON
set +e
"$E2E_PYTHON" "$HERE/drive.py" "$WORK/negctl/hooks.json" "$ROOT/conexus" "$BIN57" negctl "$WORK/negctl" 1 > "$WORK/negctl.out" 2>&1
NRC=$?
set -e
[ "$NRC" = 1 ] && grep -q 'FAIL  \[UserPromptSubmit\] nx-hook mcp-connect-check: exit 2' "$WORK/negctl.out" \
    || _fail "negative control: drive.py did not flag a direct unknown verb under 7.57.0 (rc=$NRC): $(cat "$WORK/negctl.out")"
echo "  a hooks.json entry naming a verb 7.57.0 lacks, wired directly, is flagged as blocking"

echo "── launcher control: the plugin's launcher must reach the CLI under test ──"
# The same unknown verb, through `uv` and launch/nx_launch.py with an EMPTY
# generation root and a PATH of 7.57.0's bin, uv alone and the system dirs. Exit
# 2 here means the launcher found 7.57.0 on PATH and ran it. Exit 0 would mean it
# found some other install (this machine's, or none), and every PASSED below
# would be a statement about that instead.
mkdir -p "$WORK/lc/uvbin" "$WORK/lc/tools" "$WORK/lc/home"
ln -sf "$(command -v uv)" "$WORK/lc/uvbin/uv"
set +e
echo '{}' | env -i HOME="$WORK/lc/home" NX_TOOLS_DIR="$WORK/lc/tools" PATH="$BIN57:$WORK/lc/uvbin:/usr/bin:/bin" \
    uv tool run --directory "$ROOT/conexus" --no-config --quiet --python '>=3.12' python \
    "$ROOT/conexus/launch/nx_launch.py" nx-hook no-such-verb-skew-gate >/dev/null 2>"$WORK/lc.err"
LRC=$?
set -e
if [ "$LRC" != 2 ] || ! grep -q "unknown verb" "$WORK/lc.err"; then
    _fail "launcher control: exit $LRC through the launcher, not 2 from the 7.57.0 CLI: $(cat "$WORK/lc.err")"
fi
echo "  the launcher reached the 7.57.0 CLI on PATH, not this machine's install"

echo "── this checkout's wheel ──"
HEADV="$WORK/cli-head"
uv venv -q --python 3.12 "$HEADV" >/dev/null 2>&1 || _unverified "uv venv for HEAD failed"
uv pip install -q --python "$HEADV/bin/python" "$ROOT" >"$WORK/install-head.log" 2>&1 \
    || _fail "installing this checkout failed: $(tail -3 "$WORK/install-head.log" | tr '\n' ' ')"

FAILED=0
_drive() {  # $1 = label, $2 = bin dir or "none"
    local s="$WORK/scratch-$1"
    mkdir -p "$s"
    echo "── plugin from this checkout x CLI $1 ──"
    "$E2E_PYTHON" "$HERE/drive.py" "$ROOT/conexus/hooks/hooks.json" "$ROOT/conexus" "$2" "$1" "$s" || FAILED=$((FAILED+1))
}
for v in "${VERSIONS[@]}"; do _drive "$v" "$(_cli "$v")"; done
_drive head "$HEADV/bin"
_drive none none

[ "$FAILED" = 0 ] || _fail "$FAILED CLI(s) had an entry that blocks or corrupts a session (see FAIL lines above)"
echo "HOOK-CLI SKEW GATE PASSED -- every command-tier hooks.json entry of this checkout, against CLIs ${VERSIONS[*]}, this checkout's wheel, and no CLI: none exits 2, and every decision-event stdout is JSON"
