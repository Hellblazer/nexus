#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/e2e/compact-outcome-gate.sh -- after a real compaction, does the model go back to its open work?
#
#   tests/e2e/compact-outcome-gate.sh [--runs 5] [--budget 1.5] [--arms real,control,bad,fix] [--threshold 4]
#                                     [--gate-arm real] [--min-score 0.8] [--jobs 5] [--out DIR] [--keep]
#
# RUN BY HAND ONLY. This starts real `claude` sessions and spends model calls: nothing in CI, the default pytest
# run or any other script runs it, and none should. The gate arm is the shipped conexus compaction hooks over a
# stub nx (see the docstring of lib/compact_outcome.py for the arms, the stub and the scratch plugin).
#
# COST AND WALL TIME. Each run is one haiku-class session that stops at --max-budget-usd (this script's
# --budget, default 1.5). arms x runs sessions: 4 x 5 = 20 by default. Measured 2026-11-04: about $0.03 and one
# minute per run. Arms go one after another, the runs of an arm in parallel (--jobs): expect 5 to 10 minutes of
# wall time and well under $1; the ceiling is 20 sessions x --budget x 2 attempts (a NOISE run is retried once).
#
# VERDICTS, per run. PASS: a real automatic compaction, then at least --min-score of the open items acted on
# with no user input. FAIL: it acted on fewer (did one item and ended, or asked). NOISE: no verdict (no
# automatic compaction seen, the turn did not finish or was refused, the hooks did not run, the arm's injection
# is not what the arm plants, or the stub nx was asked for a call it does not answer). UNVERIFIED: claude is
# missing.
# EXIT. 0: the gate arm reached --threshold passes and the control and planted-bad arms did not. 1: it did
# not, or a control arm reached the threshold (the gate does not discriminate). 2: UNVERIFIED, any run
# without a verdict after its retry; never a pass. 64: usage (the gate arm, bad and control must all run;
# --threshold must be 1..--runs).
# Measurement only: nothing here changes conexus/hooks. Unit tests (no claude, in the default suite):
# tests/hooks/test_compact_outcome_harness.py.
#
# SPIKE (Claude Code 2.1.318, 2026-11-04), how a compaction is forced:
#   * `claude -p --resume <id> "/compact"` works (trigger "manual", compact_boundary event, the checkpoint
#     hooks fire) but the process ends, so a background shell dies and there is no post-compaction turn.
#   * `--autocompact <tokens>` takes 100k..1M. With 100000 the AUTOMATIC compaction fires at about 57k tokens
#     of prompt (measured pre_tokens 57-87k), so ~190k chars of prose pasted in one user message is
#     enough. One long-lived `claude -p --input-format stream-json` process keeps the background shell
#     alive across turns. The automatic compaction runs at the start of the next turn's request, keeps the
#     last few messages verbatim (compact_metadata.preserved_messages) and the model then continues the
#     SAME turn by itself: the "threshold" case. An "idle" compaction (about 55 min after a turn ended)
#     starts no turn and is not reproduced here.
#   * A synthetic CI log as filler tripped the model's bio safety classifier (garbled post-compaction
#     output); real prose (docs/agents/*.md) does not.
#   * Auth: the real HOME / ~/.claude sign-in is used (a scratch CLAUDE_CONFIG_DIR has no credentials and
#     the automation token is not provisioned on this box). The run's own ~/.claude/projects directory is
#     removed by exact name afterwards; everything else (nexus config, tools, uv, plugin registry) is scratch.
set -u -o pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/python.sh disable=SC1091
source "$HERE/lib/python.sh"
e2e_python_resolve 10 || exit 2
command -v "${NX_GATE_CLAUDE:-claude}" >/dev/null 2>&1 || { echo "COMPACT-OUTCOME GATE UNVERIFIED -- claude is not on PATH"; exit 2; }
OUT=""
args=()
while [ $# -gt 0 ]; do
    case "$1" in
        --out) [ $# -ge 2 ] || { echo "--out needs a directory" >&2; exit 64; }; OUT="$2"; shift 2 ;;
        -h|--help) sed -n '3,27p' "$0" >&2; exit 64 ;;
        *) args+=("$1"); shift ;;
    esac
done
[ -n "$OUT" ] || OUT="$(mktemp -d "${TMPDIR:-/tmp}/compact-outcome-results.XXXXXX")"
mkdir -p "$OUT"
echo "results: $OUT"
exec "$E2E_PYTHON" -I "$HERE/lib/compact_outcome.py" matrix --out "$OUT" ${args[@]+"${args[@]}"}
