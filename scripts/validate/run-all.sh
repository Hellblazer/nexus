#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Live validation harness for the full nexus plugin surface.
# Runs each suite in sequence, streams per-case pass/fail with timestamps,
# and prints a roll-up at the end.
#
#   bash scripts/validate/run-all.sh                 # full sweep
#   bash scripts/validate/run-all.sh mcp-core        # one suite
#   NX_VALIDATE_WITH_LLM=1 bash scripts/validate/...  # include LLM-backed ops
#   KEEP_SANDBOX=1  bash scripts/validate/...         # preserve /tmp/nx-validate-*
#   NX_VALIDATE_VERBOSE=1 ...                         # full tracebacks on fail
#
# Exit codes:
#   0 — all suites green
#   1+ — at least one suite failed (count is number of failing suites)

# NOTE: we do NOT `set -e` — we want each suite to run even if an earlier
# one fails, so the full gap list is surfaced in one pass.
set -uo pipefail

export ORIG_HOME="$HOME"
export SANDBOX="${SANDBOX:-/tmp/nx-validate-$$}"
mkdir -p "$SANDBOX"
trap '[[ "${KEEP_SANDBOX:-0}" == "1" ]] || rm -rf "$SANDBOX"' EXIT

VALIDATE_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$VALIDATE_DIR/lib.sh"   # exports sandbox env + status helpers

# ── Banner ───────────────────────────────────────────────────────────────────
echo "════════════════════════════════════════════════════════════════════════"
echo "  nexus live validation harness"
echo "  branch:  $(git rev-parse --abbrev-ref HEAD)  ($(git rev-parse --short HEAD))"
echo "  sandbox: $SANDBOX"
echo "  env:     NX_LOCAL=$NX_LOCAL"
echo "           HOME=$HOME"
echo "           NEXUS_CATALOG_PATH=$NEXUS_CATALOG_PATH"
echo "           NX_LOCAL_CHROMA_PATH=$NX_LOCAL_CHROMA_PATH"
echo "  LLM ops: ${NX_VALIDATE_WITH_LLM:-off}  (set NX_VALIDATE_WITH_LLM=1 to include)"
echo "════════════════════════════════════════════════════════════════════════"

# ── Suite dispatch ───────────────────────────────────────────────────────────
declare -a ALL_SUITES=(
    "mcp-core"       "01-mcp-core.py"
    "mcp-catalog"    "02-mcp-catalog.py"
    "cli"            "03-cli.sh"
    "hooks"          "04-hooks.sh"
    "plugin-wiring"  "05-plugin-wiring.py"
)

# Optional suite filter: $1 is a suite name
REQUESTED_SUITE="${1:-}"

declare -a SUITE_RESULTS=()
FAIL_COUNT=0

run_suite() {
    local name="$1"
    local file="$2"
    local path="$VALIDATE_DIR/$file"
    local start=$(date +%s)

    printf "\n────────────────────────── suite: %s ──────────────────────────\n" "$name"
    if [[ "$file" == *.py ]]; then
        uv run python "$path"
    else
        bash "$path"
    fi
    local rc=$?
    local dur=$(( $(date +%s) - start ))

    if [[ $rc -eq 0 ]]; then
        SUITE_RESULTS+=("$name: PASS (${dur}s)")
    else
        SUITE_RESULTS+=("$name: FAIL rc=$rc (${dur}s)")
        FAIL_COUNT=$((FAIL_COUNT+1))
    fi
}

# Iterate pairs from ALL_SUITES
for (( i=0; i<${#ALL_SUITES[@]}; i+=2 )); do
    name="${ALL_SUITES[i]}"
    file="${ALL_SUITES[i+1]}"
    if [[ -z "$REQUESTED_SUITE" || "$REQUESTED_SUITE" == "$name" ]]; then
        run_suite "$name" "$file"
    fi
done

# ── Summary ──────────────────────────────────────────────────────────────────
echo
echo "════════════════════════════════════════════════════════════════════════"
echo "  Summary"
echo "════════════════════════════════════════════════════════════════════════"
for r in "${SUITE_RESULTS[@]}"; do
    echo "  $r"
done

echo
if [[ $FAIL_COUNT -eq 0 ]]; then
    echo "  ✓ All suites green."
else
    echo "  ✗ $FAIL_COUNT suite(s) failed. See per-case output above."
fi
echo "  Sandbox: $SANDBOX  (KEEP_SANDBOX=1 to preserve)"
echo "════════════════════════════════════════════════════════════════════════"

exit $FAIL_COUNT
