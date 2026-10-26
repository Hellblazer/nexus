#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/e2e/plugin-lockstep-tests.sh -- nexus-rbmja.3: the plugin-only battery's lockstep leg.
#
# Runs the plugin/CLI lockstep tests (tests/test_plugin_lockstep.py and
# tests/hooks/test_lockstep_survives_cli_skew.py) as one battery leg and prints a verdict line the
# battery can read:
#
#   PLUGIN LOCKSTEP TESTS PASSED (N passed, S skipped)
#   PLUGIN LOCKSTEP TESTS FAILED: <why>
#
# Non-vacuity (the nexus-moht0 doctrine): a run that passed nothing is a failure, and so is one that
# skipped more than the stated budget, because a skip-passing lockstep test proves nothing about the
# lockstep. The budget is NX_LOCKSTEP_SKIP_BUDGET (default 0: today's run skips none).
# NX_TEST_T2_SUBSTRATE=none: the files need no engine. Exit 0 only on PASSED. Exit 1 on a red, a
# vacuous run, a skip over budget, or a runner that failed to start (a missing uv is pytest exit 127,
# reported as a FAILED line). Exit 2 only for a malformed NX_LOCKSTEP_SKIP_BUDGET or when the repo
# root or the log file cannot be had. The tests run through `uv run pytest`, as the preflight's pytest
# checks do: the battery's resolved $E2E_PYTHON is a bare interpreter without the project's
# environment, so it cannot import pytest here.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
cd "$REPO_ROOT" || exit 2

SKIP_BUDGET="${NX_LOCKSTEP_SKIP_BUDGET:-0}"
[[ "$SKIP_BUDGET" =~ ^[0-9]+$ ]] || { echo "NX_LOCKSTEP_SKIP_BUDGET must be a non-negative integer" >&2; exit 2; }
FILES=(tests/test_plugin_lockstep.py tests/hooks/test_lockstep_survives_cli_skew.py)

LOG="$(mktemp "${TMPDIR:-/tmp}/plugin-lockstep.XXXXXX")" || exit 2
trap 'rm -f "$LOG"' EXIT

NX_TEST_T2_SUBSTRATE=none uv run pytest "${FILES[@]}" -q -m "" -p no:cacheprovider >"$LOG" 2>&1
RC=$?
cat "$LOG"

# pytest -q ends with "41 passed, 1 skipped, 2 warnings in 4.9s"; read the counts from that line.
SUMMARY="$(grep -E '^(=+ )?[0-9]+ (passed|failed|errors?|skipped|deselected|xfailed|xpassed)' "$LOG" | tail -1 || true)"
PASSED="$(printf '%s\n' "$SUMMARY" | grep -oE '[0-9]+ passed' | grep -oE '[0-9]+' || true)"
SKIPPED="$(printf '%s\n' "$SUMMARY" | grep -oE '[0-9]+ skipped' | grep -oE '[0-9]+' || true)"
PASSED="${PASSED:-0}"; SKIPPED="${SKIPPED:-0}"

if [ "$RC" -ne 0 ]; then
  echo "PLUGIN LOCKSTEP TESTS FAILED: pytest exit $RC (${SUMMARY:-no summary line})"; exit 1
fi
if [ -z "$SUMMARY" ] || [ "$PASSED" -eq 0 ]; then
  echo "PLUGIN LOCKSTEP TESTS FAILED: 0 tests passed (${SUMMARY:-no summary line}); a lockstep gate that ran nothing proves nothing"; exit 1
fi
if [ "$SKIPPED" -gt "$SKIP_BUDGET" ]; then
  echo "PLUGIN LOCKSTEP TESTS FAILED: $SKIPPED skipped, over the budget of $SKIP_BUDGET ($PASSED passed)"; exit 1
fi
echo "PLUGIN LOCKSTEP TESTS PASSED ($PASSED passed, $SKIPPED skipped)"
