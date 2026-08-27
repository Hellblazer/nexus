#!/usr/bin/env bash
# nexus-utpuw.17: GEN-FLIP LIVE-HOLDER — the side-by-side property on the REAL
# artifact.
#
# Why this exists. nexus-utpuw.16 guards the PROPERTY cheaply on every run,
# against a three-line fixture distribution. It cannot guard the ARTIFACT: real
# console scripts, a real dependency graph, the real certifi/cacert path whose
# failure was the concrete nexus-q3xrx symptom (95 cacert tracebacks in a live
# session). This gate runs the same ladder against two REAL conexus generations
# built from this checkout, with an actual nx-mcp process as the holder and a
# real MCP tools/call as the thing that must keep working across a flip.
#
# COVERAGE GAP THIS CLOSES (audit item #10). Nothing in the fast gates
# exercises shim/current/GC machinery: fresh-install-mvv.sh uses plain
# `uv pip install` into a scrubbed venv and never touches shims, and GitHub
# Actions contains ZERO uv-tool references — every workflow runs `uv sync`
# against the dev venv. CI will not catch this class on its own.
#
# HERMETIC BY CONSTRUCTION, and this is not boilerplate caution — it was
# measured while building this gate. An nx-mcp started from the dev venv with
# an ambient environment answered a `search` tools/call out of the OPERATOR'S
# REAL collections. So the ladder runs under `env -i` with a virgin HOME, its
# own NEXUS_CONFIG_DIR, and a curated PATH: nothing this gate observes can come
# from the developer's machine, and nothing it does can reach the developer's
# data.
#
# WHAT A GREEN MEANS HERE. In this sandbox there is no backend, so the tool
# call is EXPECTED to answer "nexus-service endpoint is not resolvable". That
# is the point: the call must fail for want of a BACKEND and never for want of
# a MODULE. An import-shaped failure after the flip is nexus-q3xrx itself, and
# the ladder refuses it by name using the same marker vocabulary
# src/nexus/mcp/_stale_host.py uses.
#
# Self-provisioning (feedback_gates_scripted_not_ambient): everything it needs
# it builds. There is no skip-pass path — a missing prerequisite is a loud
# failure, because a skip here would be vacuous exactly where the defect lives.
#
# Cost: two real conexus generation builds. This is E2E cadence, not the fast
# loop; .16 is the fast-loop half of the pair.
#
# Usage:  bash tests/e2e/gen-flip-live-holder.sh

set -euo pipefail

GATE="GEN-FLIP LIVE-HOLDER"
_fail() { echo "$GATE FAILED: $*" >&2; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
LADDER="$SCRIPT_DIR/lib/gen_flip_holder.py"

[ -f "$LADDER" ] || _fail "ladder driver is missing: $LADDER"

command -v uv >/dev/null 2>&1 || _fail "uv is required to build a generation and is not on PATH"
UV_BIN="$(cd "$(dirname "$(command -v uv)")" && pwd)"

# A python3 for the driver itself. It is stdlib-only and drives the artifact
# from OUTSIDE, so it deliberately is not the generation's interpreter.
DRIVER_PY="$(command -v python3)" || _fail "python3 is required and is not on PATH"

WORK="$(mktemp -d "${TMPDIR:-/tmp}/nexus-gen-flip.XXXXXX")"
GATE_OK=0
cleanup() {
    if [ "$GATE_OK" = 1 ]; then
        rm -rf "$WORK"
    else
        echo "FAILURE EVIDENCE PRESERVED: $WORK" >&2
    fi
}
trap cleanup EXIT

mkdir -p "$WORK/home" "$WORK/config" "$WORK/tools" "$WORK/bin"

echo "$GATE: sandbox $WORK"
echo "  repo under test: $REPO_ROOT"

# `env -i` rather than unsetting a denylist: a denylist is a list someone has
# to remember to extend, and the variable that reaches the journey is always
# the one nobody thought of (the r5f3c lesson, restated in
# fresh-install-mvv.sh). Everything the ladder needs is named here explicitly.
env -i \
    PATH="$UV_BIN:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin" \
    HOME="$WORK/home" \
    TMPDIR="${TMPDIR:-/tmp}" \
    NEXUS_CONFIG_DIR="$WORK/config" \
    NX_LOCAL=1 \
    NX_TOOLS_DIR="$WORK/tools" \
    NX_BIN_DIR="$WORK/bin" \
    NX_GATE_WORK="$WORK" \
    NX_GATE_REPO="$REPO_ROOT" \
    "$DRIVER_PY" "$LADDER" \
    || _fail "the live-holder ladder did not complete (evidence above)"

GATE_OK=1
echo "GEN-FLIP LIVE-HOLDER PASSED — two real conexus generations, a live nx-mcp holder across a flip"
