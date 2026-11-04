#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/e2e/compact-outcome-gate.sh -- after a real compaction, does the model go back to its open work?
#
#   tests/e2e/compact-outcome-gate.sh [--runs N] [--arms real,control,bad,fix] [--threshold K]
#                                     [--gate-arm real] [--min-score 0.8] [--jobs J] [--out DIR] [--keep]
#
# Runs lib/compact_outcome.py matrix (live `claude`, haiku-class, about $0.03 and 1 minute per run).
# Exit: 0 gate arm reached --threshold and the control and planted-bad arms did not; 1 it did not, or
# a non-vacuity check failed; 2 UNVERIFIED (claude missing, or a run produced no verdict); 64 usage.
# Measurement only: nothing here changes conexus/hooks. Mechanics, arms and judging: the docstring of
# lib/compact_outcome.py. Unit tests (no claude): tests/hooks/test_compact_outcome_harness.py.
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
        -h|--help) sed -n '4,14p' "$0" >&2; exit 64 ;;
        *) args+=("$1"); shift ;;
    esac
done
[ -n "$OUT" ] || OUT="$(mktemp -d "${TMPDIR:-/tmp}/compact-outcome-results.XXXXXX")"
mkdir -p "$OUT"
echo "results: $OUT"
exec "$E2E_PYTHON" -I "$HERE/lib/compact_outcome.py" matrix --out "$OUT" ${args[@]+"${args[@]}"}
