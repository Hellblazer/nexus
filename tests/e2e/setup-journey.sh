#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# RDR-228 P2.7 (nexus-sv2wx.20): the journey test for the plugin's setup path, with no network.
#
# Starts conexus/hooks/hooks.json's SessionStart entries and conexus/.mcp.json's servers, as Claude
# Code starts them, in a sandbox home that has uv and no Nexus. Three journeys (tests/e2e/lib/
# setup_journey.py has the steps): A, the local path from "no CLI" to a real server answering;
# B, a generation whose Python was pruned, repaired by setup mode cli-only; C, the same for a legacy
# `uv tool install conexus` tree.
#
# RUN IT ONLY ON A TEST HOST (chas, hellmini), NEVER ON A DEVELOPER'S OWN MACHINE: it runs the plugin's
# hooks and starts a PostgreSQL and an engine, all inside the sandbox home, but a hook entry is not a
# thing to run on the machine you work on.
#
#   tests/e2e/setup-journey.sh --seed      once, with a network: runs the journeys online, fills the seed
#   tests/e2e/setup-journey.sh             the journey, offline, from the seed
#   tests/e2e/setup-journey.sh --only A    one journey (B needs A: --only AB)
#
# THE SEED (NX_JOURNEY_SEED, default ${XDG_CACHE_HOME:-~/.cache}/nexus-setup-journey) holds what the
# journey cannot fetch: a uv cache with every package of the build, the engine, its PostgreSQL bundle and
# the two models `nx init` downloads. --seed is the ONLY run that touches the network, and it fetches:
# the dependencies of this tree's wheel (PyPI; the CPU torch index on Linux), hatchling and the MCP SDK,
# the engine and PostgreSQL bundle of the pinned engine tag (GitHub releases, signature checked), the
# bge-768 model (GitHub releases) and the cross-encoder (huggingface.co). A run without a complete seed
# FAILS; it does not skip.
#
# The offline run sets UV_OFFLINE, a uv that cannot build without the cache, UV_PYTHON_DOWNLOADS=never
# (it needs a Python 3.12 on the host), and points every proxy variable at a local listener that
# refuses and records; the journey fails if the listener saw a request.
#
# Exit 0 and the last line "SETUP JOURNEY PASSED" is the pass.
set -euo pipefail
export NX_NO_TELEMETRY=1

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck source=lib/python.sh disable=SC1091
source "$REPO_ROOT/tests/e2e/lib/python.sh"
e2e_python_resolve 11 || exit 2

ONLINE=0
ONLY="ABC"
KEEP=0
while [ $# -gt 0 ]; do
    case "$1" in
        --seed) ONLINE=1; shift ;;
        --only) ONLY="${2:?--only needs journey letters, e.g. A or AB}"; shift 2 ;;
        --keep) KEEP=1; shift ;;
        -h|--help) sed -n '2,/^set -euo/p' "${BASH_SOURCE[0]}" | sed '$d'; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

SEED="${NX_JOURNEY_SEED:-${XDG_CACHE_HOME:-$HOME/.cache}/nexus-setup-journey}"
UV="$(command -v uv)" || { echo "SETUP JOURNEY FAILED: uv is not on PATH" >&2; exit 1; }
WORK="$(mktemp -d "${TMPDIR:-/tmp}/setup-journey-XXXXXX")"
BUILD_HOME="$WORK/build-home"
mkdir -p "$BUILD_HOME" "$SEED/uvcache"

DONE=0
cleanup() {
    if [ "$DONE" = 1 ] && [ "$KEEP" = 0 ]; then
        rm -rf "$WORK"
    else
        echo "evidence kept: $WORK (seed: $SEED)" >&2
    fi
}
trap cleanup EXIT

_fail() { echo "SETUP JOURNEY FAILED: $*" >&2; exit 1; }

# Where uv keeps the Python it manages, read from the host's own uv before the sandbox hides the host.
UV_PY_DIR="$(env -i HOME="$HOME" PATH="$PATH" "$UV" python dir)"
env -i HOME="$HOME" PATH="$PATH" UV_PYTHON_DOWNLOADS=never "$UV" python find 3.12 >/dev/null 2>&1 \
    || _fail "no Python 3.12 on this host, and the journey never downloads one (UV_PYTHON_DOWNLOADS=never)"

# uv for the host side (the wheel build and the client venv): the same cache and the same offline rule.
_uv_host() {
    local offline=()
    [ "$ONLINE" = 1 ] || offline=(UV_OFFLINE=1)
    env -i HOME="$BUILD_HOME" PATH="$(dirname "$UV"):/usr/bin:/bin" TMPDIR="$WORK" NX_NO_TELEMETRY=1 \
        UV_CACHE_DIR="$SEED/uvcache" UV_PYTHON_INSTALL_DIR="$UV_PY_DIR" UV_PYTHON_DOWNLOADS=never UV_NO_PROGRESS=1 \
        ${offline[@]+"${offline[@]}"} "$UV" "$@"
}

echo "== this tree's wheel"
mkdir -p "$WORK/wheels"
_uv_host build --wheel --out-dir "$WORK/wheels" "$REPO_ROOT" >"$WORK/build.log" 2>&1 \
    || { tail -20 "$WORK/build.log" >&2; _fail "the wheel did not build (offline: hatchling must be in the seed's cache)"; }

echo "== the client: a venv with the MCP SDK"
_uv_host venv --python 3.12 "$WORK/client" >"$WORK/client.log" 2>&1 || { tail -20 "$WORK/client.log" >&2; _fail "the client venv did not build"; }
_uv_host pip install --python "$WORK/client/bin/python" "mcp>=1.0,<2" >>"$WORK/client.log" 2>&1 \
    || { tail -20 "$WORK/client.log" >&2; _fail "the MCP SDK did not install (offline: it must be in the seed's cache)"; }

ARGS=(--seed-dir "$SEED" --work "$WORK/boxes" --uv "$UV" --uv-python-dir "$UV_PY_DIR" --wheels "$WORK/wheels" --only "$ONLY")
[ "$ONLINE" = 1 ] && ARGS+=(--online)
[ "$KEEP" = 1 ] && ARGS+=(--keep)

if env -i HOME="$BUILD_HOME" PATH="/usr/bin:/bin" TMPDIR="$WORK" NX_NO_TELEMETRY=1 PYTHONUNBUFFERED=1 \
    "$WORK/client/bin/python" -I "$REPO_ROOT/tests/e2e/lib/setup_journey.py" "${ARGS[@]}"; then
    DONE=1
else
    exit 1
fi
