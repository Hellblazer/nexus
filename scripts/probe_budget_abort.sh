#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Probe: what does `claude -p --max-budget-usd <tiny>` actually emit on
# stdout/stderr/exit-code when the budget aborts? (nexus-2g8y7)
#
# WHY THIS EXISTS. Claude Code 2.1.217+ added --max-budget-usd as a HARD
# abort at cap (per the CLI docs: spawning denied, running background
# subagents halted, exit code 1, no graceful completion) -- but the docs
# do not define a distinct wire signal for "aborted because of budget"
# beyond that bare exit code. `nexus.operators.dispatch.claude_dispatch`
# needs to tell a budget abort apart from every OTHER nonzero-exit failure
# (auth error, bad --model, rate limit, ...) so it can raise the typed
# `OperatorBudgetExceededError` instead of the generic `OperatorError`.
#
# At the time this script was written, the AMBIENT monthly spend limit on
# the authoring environment already blocks every `claude -p` invocation,
# budget flag or not -- so the real abort shape could not be captured
# in-line with the bead. `claude_dispatch`'s detection
# (`_looks_like_budget_exceeded_signal`) therefore ships as a conservative,
# generous keyword match on "budget" in the captured text rather than a
# precise signature. Run THIS script once `claude auth status` / a trial
# `claude -p 'say ok'` succeeds again (spend-limit window reset, or a
# different account), read the captured output, and tighten the match in
# src/nexus/operators/dispatch.py::_looks_like_budget_exceeded_signal
# (and its docstring / this bead's follow-up) against what actually came
# back.
#
# What this does:
#   1. TEST run: `claude -p 'say ok' --max-budget-usd 0.000001
#      --output-format stream-json --strict-mcp-config --verbose` -- a
#      ceiling small enough that essentially any real turn exceeds it.
#   2. CONTROL run: the same prompt with no --max-budget-usd flag at all,
#      so the two captures can be diffed for what changed.
#   3. Captures exit code, full stdout (the stream-json NDJSON), and full
#      stderr for both runs to timestamped files under bench/out/, then
#      prints a diff-oriented summary: exit codes, any stderr lines
#      differing between the two, and any stdout line containing a
#      case-insensitive "budget"/"abort"/"cost"/"limit" token.
#
# Usage:
#   scripts/probe_budget_abort.sh
#
# Requirements: `claude` CLI on PATH with valid auth. Each run is a REAL
# `claude -p` invocation -- the control run has ordinary API cost; the
# test run's cost is bounded by the ~$0.000001 ceiling itself (it should
# abort before accruing much, that being exactly the behavior under test).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="${REPO_ROOT}/bench/out"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
TEST_STDOUT="${OUT_DIR}/probe_budget_abort_${TS}_test.stdout.log"
TEST_STDERR="${OUT_DIR}/probe_budget_abort_${TS}_test.stderr.log"
CONTROL_STDOUT="${OUT_DIR}/probe_budget_abort_${TS}_control.stdout.log"
CONTROL_STDERR="${OUT_DIR}/probe_budget_abort_${TS}_control.stderr.log"

mkdir -p "${OUT_DIR}"

if ! command -v claude >/dev/null 2>&1; then
    echo "PROBE_FAIL: claude CLI not on PATH" >&2
    exit 1
fi

echo "== TEST run: --max-budget-usd 0.000001 =="
set +e
echo 'say ok' | claude -p --max-budget-usd 0.000001 \
    --output-format stream-json --strict-mcp-config --verbose \
    >"${TEST_STDOUT}" 2>"${TEST_STDERR}"
TEST_EXIT=$?
set -e
echo "test exit code: ${TEST_EXIT}"
echo "test stdout -> ${TEST_STDOUT} ($(wc -c <"${TEST_STDOUT}") bytes)"
echo "test stderr -> ${TEST_STDERR} ($(wc -c <"${TEST_STDERR}") bytes)"

echo
echo "== CONTROL run: no --max-budget-usd =="
set +e
echo 'say ok' | claude -p \
    --output-format stream-json --strict-mcp-config --verbose \
    >"${CONTROL_STDOUT}" 2>"${CONTROL_STDERR}"
CONTROL_EXIT=$?
set -e
echo "control exit code: ${CONTROL_EXIT}"
echo "control stdout -> ${CONTROL_STDOUT} ($(wc -c <"${CONTROL_STDOUT}") bytes)"
echo "control stderr -> ${CONTROL_STDERR} ($(wc -c <"${CONTROL_STDERR}") bytes)"

echo
echo "== What distinguishes the budget abort =="
echo "exit codes: test=${TEST_EXIT} control=${CONTROL_EXIT}"

echo
echo "-- stderr lines present in TEST but not CONTROL --"
comm -23 <(sort -u "${TEST_STDERR}") <(sort -u "${CONTROL_STDERR}") || true

echo
echo "-- stdout lines in TEST mentioning budget/abort/cost/limit (case-insensitive) --"
grep -inE 'budget|abort|cost|limit' "${TEST_STDOUT}" || echo "(no match)"

echo
echo "-- stderr lines in TEST mentioning budget/abort/cost/limit (case-insensitive) --"
grep -inE 'budget|abort|cost|limit' "${TEST_STDERR}" || echo "(no match)"

echo
echo "Raw captures retained under ${OUT_DIR} for manual inspection."
echo "Next step: compare against"
echo "  src/nexus/operators/dispatch.py::_looks_like_budget_exceeded_signal"
echo "and tighten the match if the real signal is more specific than a bare"
echo "'budget' substring."
