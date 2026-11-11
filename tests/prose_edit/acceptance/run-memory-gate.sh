#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# usage: OUT_DIR=<dir> run-memory-gate.sh LABEL SOURCE [N]
#
# The rejection-memory gate (RDR-221, nexus-ger02.16). N fresh runs (default 10) on copies of SOURCE, each
# four headless sessions through run-scenario.sh (see README.md, "The rejection-memory gate", for the
# threshold, which is fixed before any run, and for what counts):
#
#   LABEL-<k>-1  /prose-edit <copy> --genre reference-doc --budget 5
#   LABEL-<k>-2  $ANSWER (default "Accept none. Reject all the edits."; the skill echoes: apply --dry-run). Under
#                PROSE_EDIT_APP_RUNNER=1 it is just "reviewed": the author's decisions are the stand-in's statuses
#                (markup_standin_decisions.json: every edit rejected for a -2 turn), and an App-decided run applies in
#                this turn
#   LABEL-<k>-3  "Yes, apply."                          (stores the rejections); only when turn 2 did not already apply
#   LABEL-<k>-4  the same /prose-edit again, a fresh session
#
# No run is repeated and none is replaced: a run that errors stays in OUT_DIR for memory_gate_verdicts.py
# to list. Every run has its own copy of SOURCE (docs/zz-memgate/LABEL-<k>.md, removed on exit), so its own
# T2 document record; SOURCE itself is never edited. T2 writes go to projects named with
# PROSE_EDIT_PROJECT_PREFIX (default zzprose216_); delete them afterwards. GATE_JOBS runs at once (default 1: the preregistered runs were one at a time, and N identical copies at once sit as siblings in docs/zz-memgate/, within reach of the editor's Glob and Grep).
# A copy of SOURCE is kept as $OUT/LABEL-source.txt: memory_gate_verdicts.py reads it for the same-spot column.
# ANSWER is the author's answer in turn 2; it must name edits by their content, because the numbers differ
# from run to run (e.g. "Reject the edit that cuts basically and the edit that cuts It should be noted that.
# Hold every other edit; accept none."). The verdicts count what the skill stored, not what the answer said.
# runner_guard.bash takes an exclusive lock for the whole run (one runner at a time, so no other runner's copy
# is a sibling document of these; exit 75 when another holds it), sweeps the leftovers of an older runner and
# removes $DIR on exit. The copies sit in a nested directory no built-in genre maps, so they are in no "Genre
# paths:" list and the editor's Grep never meets them.
# Under PROSE_EDIT_APP_RUNNER=1 the shipped decision table rejects every edit of a -2 turn, so a custom ANSWER would
# disagree with the app: the run exits 1 when ANSWER is set without PROSE_EDIT_STANDIN_DECISIONS (a table that says the
# same). GATE_JOBS above 1 exits 1 too: the sessions of one run share one stand-in state file. A stand-in gate batch is
# a harness measurement, not a run of the preregistered gate (README.md).
# T2: unless PROSE_EDIT_LOCAL_ENGINE=0 or PROSE_EDIT_NX is set, the run starts a throwaway local engine
# (local_engine.bash); every T2 write goes to it, never to the operator's install.
# Stop and report if a run shows a T2 failure; never repair it from here.
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WT="$(cd "$HERE/../../.." && pwd)"
LABEL="${1:?label}"
SOURCE="${2:?source document}"
N="${3:-10}"
JOBS="${GATE_JOBS:-1}"
ANSWER_SET="${ANSWER:-}"
ANSWER="${ANSWER:-Accept none. Reject all the edits.}"
OUT="${OUT_DIR:?set OUT_DIR}"
. "$HERE/runner_guard.bash"
. "$HERE/local_engine.bash"
runner_lock run-memory-gate.sh
export TMPDIR="${TMPDIR:?set TMPDIR to a directory outside the repository}"
export PROSE_EDIT_PROJECT_PREFIX="${PROSE_EDIT_PROJECT_PREFIX:-zzprose216_}"
# Every session is a path run, reviewed in the MarkupEditor app, which a headless session cannot drive
# (nexus-scmcs.2.13): NOT-RUN, recorded in $OUT/not-run.txt, exit 3. PROSE_EDIT_APP_RUNNER=1, set only by a
# harness that answers the MarkupEditor tools, runs the gate.
if [ "${PROSE_EDIT_APP_RUNNER:-0}" != 1 ]; then
  runner_not_run "$OUT" "memory-gate $LABEL" "$RUNNER_NO_APP_REASON"
  exit 3
fi
DIR=docs/zz-memgate  # nested, so no built-in genre maps it: --genre is passed
if [ "${PROSE_EDIT_APP_RUNNER:-0}" = 1 ]; then
  if [ "$JOBS" != 1 ]; then
    echo "run-memory-gate.sh: GATE_JOBS=$JOBS is refused under PROSE_EDIT_APP_RUNNER=1: the sessions of one run share one stand-in state file (read and written without a lock), so the jobs would mix their suggestions and ids. Run one at a time." >&2
    exit 1
  fi
  if [ -n "${ANSWER_SET:-}" ] && [ -z "${PROSE_EDIT_STANDIN_DECISIONS:-}" ]; then
    echo "run-memory-gate.sh: ANSWER is set under PROSE_EDIT_APP_RUNNER=1 without PROSE_EDIT_STANDIN_DECISIONS. The shipped table rejects every edit of a -2 turn and the turn says only \"reviewed\", so a custom answer would disagree with what the app decides. Name a decision table that says what ANSWER says, or unset ANSWER." >&2
    exit 1
  fi
fi
mkdir -p "$OUT"
runner_standin_reset "$OUT"
cd "$WT" || exit 1
[ -f "$SOURCE" ] || { echo "no such source: $SOURCE" >&2; exit 1; }
local_engine_start run-memory-gate.sh
runner_sweep_stale
mkdir -p "$DIR" && runner_track "$DIR"
git status --short > "$OUT/$LABEL-repo-status-before.txt"
wc -l < "$SOURCE" > "$OUT/$LABEL-source-lines.txt"
cp "$SOURCE" "$OUT/$LABEL-source.txt"

run() { OUT_DIR="$OUT" "$HERE/run-scenario.sh" "$@"; }

one() {
  local k="$1" doc="$DIR/$LABEL-$1.md"
  runner_standin_reset "$OUT"   # one run, one state file: GATE_JOBS is 1 under the stand-in, so nothing else uses it now
  cp "$SOURCE" "$WT/$doc" || return 1
  run "$LABEL-$k-1" "/prose-edit $doc --genre reference-doc --budget 5"
  RESUME_FROM="$LABEL-$k-1" run "$LABEL-$k-2" "$(runner_answer "$ANSWER")"
  runner_turn_applied "$OUT" "$LABEL-$k-2" || RESUME_FROM="$LABEL-$k-2" run "$LABEL-$k-3" "Yes, apply."
  run "$LABEL-$k-4" "/prose-edit $doc --genre reference-doc --budget 5"
}

k=1
while [ "$k" -le "$N" ]; do
  batch=0
  while [ "$batch" -lt "$JOBS" ] && [ "$k" -le "$N" ]; do
    one "$k" &
    k=$((k + 1))
    batch=$((batch + 1))
  done
  wait
done
git status --short > "$OUT/$LABEL-repo-status-after.txt"
echo done > "$OUT/$LABEL.done"
