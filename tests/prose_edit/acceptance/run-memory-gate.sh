#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# usage: OUT_DIR=<dir> run-memory-gate.sh LABEL SOURCE [N]
#
# The rejection-memory gate (RDR-221, nexus-ger02.16). N fresh runs (default 10) on copies of SOURCE, each
# four headless sessions through run-scenario.sh (see README.md, "The rejection-memory gate", for the
# threshold, which is fixed before any run, and for what counts):
#
#   LABEL-<k>-1  /prose-edit <copy> --genre reference-doc --budget 5
#   LABEL-<k>-2  $ANSWER (default "Accept none. Reject all the edits."; the skill echoes: apply --dry-run)
#   LABEL-<k>-3  "Yes, apply."                          (stores the rejections)
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
# Stop and report if a run shows a T2 failure; never repair it from here.
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WT="$(cd "$HERE/../../.." && pwd)"
LABEL="${1:?label}"
SOURCE="${2:?source document}"
N="${3:-10}"
JOBS="${GATE_JOBS:-1}"
ANSWER="${ANSWER:-Accept none. Reject all the edits.}"
OUT="${OUT_DIR:?set OUT_DIR}"
. "$HERE/runner_guard.bash"
runner_lock run-memory-gate.sh
export TMPDIR="${TMPDIR:?set TMPDIR to a directory outside the repository}"
export PROSE_EDIT_PROJECT_PREFIX="${PROSE_EDIT_PROJECT_PREFIX:-zzprose216_}"
export PROSE_EDIT_TEST=1  # PROSE_EDIT_OPEN is ignored without it
export PROSE_EDIT_OPEN="${PROSE_EDIT_OPEN:-true}"
DIR=docs/zz-memgate  # nested, so no built-in genre maps it: --genre is passed
mkdir -p "$OUT"
cd "$WT" || exit 1
[ -f "$SOURCE" ] || { echo "no such source: $SOURCE" >&2; exit 1; }
runner_sweep_stale
mkdir -p "$DIR" && runner_track "$DIR"
git status --short > "$OUT/$LABEL-repo-status-before.txt"
python3 .claude/skills/prose-edit/scripts/memory.py viewer --set Typora > "$OUT/$LABEL-seed-viewer.json" || exit 1
wc -l < "$SOURCE" > "$OUT/$LABEL-source-lines.txt"
cp "$SOURCE" "$OUT/$LABEL-source.txt"

run() { OUT_DIR="$OUT" "$HERE/run-scenario.sh" "$@"; }

one() {
  local k="$1" doc="$DIR/$LABEL-$1.md"
  cp "$SOURCE" "$WT/$doc" || return 1
  run "$LABEL-$k-1" "/prose-edit $doc --genre reference-doc --budget 5"
  RESUME_FROM="$LABEL-$k-1" run "$LABEL-$k-2" "$ANSWER"
  RESUME_FROM="$LABEL-$k-2" run "$LABEL-$k-3" "Yes, apply."
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
