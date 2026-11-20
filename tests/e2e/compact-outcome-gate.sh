#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/e2e/compact-outcome-gate.sh -- after a real compaction, does the model go back to the work it owes?
#
#   tests/e2e/compact-outcome-gate.sh [--runs 15] [--threshold 11] [--model sonnet] [--budget 1.5]
#                                     [--scenarios meanwhile,bounded,waiting] [--arms ...] [--gate-arm real]
#                                     [--min-score 0.8] [--jobs 5] [--pre-target 30000] [--out DIR] [--keep]
#
# RUN BY HAND ONLY. This starts real `claude` sessions and spends model calls: nothing in CI, the default pytest
# run or any other script runs it, and none should. The docstring of lib/compact_outcome.py has the scenarios, the arms,
# the stub nx and the scratch plugin. In short:
#   scenarios  meanwhile  every board thread blocked; one independent job lives only in the summary (the census's real misses)
#              bounded    the user asked for one step and said wait: PASS = it does that step and stops
#              waiting    the only open item is a running background task: PASS = it says what it waits on
#              open       the 2026-11-19 five-item regression scenario (not in the default set)
#   arms       real (shipped hooks), control (no plugin), bad (BOARD WITHHELD: the board is withheld, the hook's own process-table
#              section and the injection framing remain), directive (real hooks + ONLY the narrow directive line), broad (+ only
#              the broad directive), fix (the overlay's list + broad directive), nocompact (same turns, no compaction: a baseline)
#
# COST AND WALL TIME. Each run is one `claude` session that stops at --max-budget-usd (this script's --budget, default 1.5).
# The default matrix is 10 arm-scenario cells (meanwhile: real, control, directive, nocompact; bounded: control, real,
# directive; waiting: control, real, directive) x 15 runs = 150 sessions. Measured 2026-11-19 (the old one-paste shape, haiku):
# about $0.03 and one minute a run. The new shape runs about 8 turns to ~80k tokens, so the ESTIMATE (unmeasured) is $0.08 a run
# on haiku and $0.25 on sonnet, the default model (real sessions run sonnet or opus): about $12 and $38 for the 150 sessions.
# Adding the broad arm to every scenario is +45 sessions. Measure first: one `run-one` costs a run's price. The ceiling is
# 150 sessions x --budget x 2 attempts (a NOISE run is retried once) = $450, which no run approaches. Arms go one after another,
# the runs of an arm in parallel (--jobs): expect 3 to 4 hours of wall time for the default matrix.
#
# VERDICTS, per run. PASS: the scenario's condition, judged by effect (repo state and the tool calls after the compaction).
# FAIL: a real compaction happened and the session did not meet it. NOISE: no verdict (no automatic compaction, one before the
# final turn or more than one, the turn did not finish or was refused, every open item was finished before the window, the hooks
# did not run, the stub logged no call, the summary dropped the meanwhile item, or the stub nx was asked for a call it does not
# answer). UNVERIFIED: claude is missing.
# EXIT. 0: in every scenario the gate arm reached --threshold passes (11 of 15) AND beat control (and bad) at a one-sided Fisher
# p < 0.05, and they did not reach it themselves (bounded and waiting: every hook arm reached it and none did worse than control).
# 1: a scenario failed. 2: UNVERIFIED, any run without a verdict after its retry or a harness crash; never a pass. 64: usage.
# Each arm prints its Wilson 95% interval and its cost. The real arm's resume rate is compared with the census (32 of 35 boundaries
# did not stall); far below it the output says "scenario harsher than observed use". To calibrate, run
#   tests/e2e/compact-outcome-gate.sh --scenarios open --arms real,control,bad,fix
# first. Measurement only: nothing here changes conexus/hooks. Unit tests (no claude, in the default suite):
# tests/hooks/test_compact_outcome_harness.py.
#
# SPIKE (Claude Code 2.1.318, 2026-11-04), how a compaction is forced:
#   * `claude -p --resume <id> "/compact"` works (trigger "manual", compact_boundary event, the checkpoint
#     hooks fire) but the process ends, so a background shell dies and there is no post-compaction turn.
#   * `--autocompact <tokens>` takes 100k..1M. With 100000 the AUTOMATIC compaction fires at about 57k tokens
#     of prompt (measured pre_tokens 57-87k). One long-lived `claude -p --input-format stream-json` process keeps the
#     background shell alive across turns. The automatic compaction runs between model calls, keeps the last few messages
#     verbatim (compact_metadata.preserved_messages) and the model then continues the SAME turn by itself: the "threshold"
#     case. An "idle" compaction (about 55 min after a turn ended) starts no turn and is not reproduced here.
#   * The first design pasted ~190k chars in one message: every run then compacted twice (R R C R C R, the second a summary of
#     a summary). The current design reads notes turn by turn and aims for one compaction in the final turn; the constants
#     (--pre-target) were not calibrated live, so a run that compacts early or never is NOISE and says which way to move.
#   * A synthetic CI log as filler tripped the model's bio safety classifier (garbled post-compaction
#     output); real prose (docs/agents/*.md) does not.
#   * Auth: the real HOME / ~/.claude sign-in is used (a scratch CLAUDE_CONFIG_DIR has no credentials and
#     the automation token is not provisioned on this box); CLAUDE_CODE_OAUTH_TOKEN, if set, is passed on. The run's own
#     ~/.claude/projects directory is removed by exact name afterwards; everything else (nexus config, tools, uv, plugin
#     registry) is scratch.
set -u -o pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Help first: it needs no interpreter and no claude (the header is the leading comment block, up to the first code line).
for a in "$@"; do
    case "$a" in
        -h|--help) awk 'NR>1 { if ($0 !~ /^#/) exit; print }' "$0" >&2; exit 64 ;;
    esac
done
# shellcheck source=lib/python.sh disable=SC1091
source "$HERE/lib/python.sh"
e2e_python_resolve 10 || exit 2
command -v "${NX_GATE_CLAUDE:-claude}" >/dev/null 2>&1 || { echo "COMPACT-OUTCOME GATE UNVERIFIED -- claude is not on PATH"; exit 2; }
OUT=""
args=()
while [ $# -gt 0 ]; do
    case "$1" in
        --out) [ $# -ge 2 ] || { echo "--out needs a directory" >&2; exit 64; }; OUT="$2"; shift 2 ;;
        *) args+=("$1"); shift ;;
    esac
done
[ -n "$OUT" ] || OUT="$(mktemp -d "${TMPDIR:-/tmp}/compact-outcome-results.XXXXXX")"
mkdir -p "$OUT"
echo "results: $OUT"
exec "$E2E_PYTHON" -I "$HERE/lib/compact_outcome.py" matrix --out "$OUT" ${args[@]+"${args[@]}"}
