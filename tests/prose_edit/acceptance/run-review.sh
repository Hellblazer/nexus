#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# usage: OUT_DIR=<dir> [SCENARIOS="a b c"] run-review.sh
#
# The review-loop scenarios (RDR-221 Steps 1.1 and 1.5): real headless runs of the prose-edit skill, each
# turn through run-scenario.sh, an answer turn through RESUME_FROM. The skill echoes the author's answer
# (apply --dry-run) and waits, so every answer turn is followed by a confirmation turn ("Yes, apply.",
# named NAMEc). They WRITE to T2, so every record goes
# to projects named with PROSE_EDIT_PROJECT_PREFIX (default zzprose024_), never to the live prose projects.
# The scenario document is a copy of fixtures/review-scenario.md at docs/zz-review/scenario.md, in a nested
# directory no built-in genre maps: it is not in any "Genre paths:" list, so the editor's Grep never meets it
# (--genre reference-doc is passed instead). It is removed on exit. runner_guard.bash takes an exclusive lock
# for the whole run (one runner at a time; exit 75 when another holds it) and sweeps the leftovers of an older
# runner. SCENARIOS names the groups to run (default all three; b needs a, because b1 lists the rejections a2 stored, and the runner refuses b without a); it is recorded in $OUT/scenarios.txt, and
# review_verdicts.py scores a group that was not asked for and left no transcript as NOT-RUN, not FAIL.
# Every scenario runs ONCE: there is no retry loop, because a retry that keeps the run in which the editor
# behaved selects the pass (nexus-ger02.16). Afterwards: python3 review_verdicts.py $OUT_DIR, then delete the
# zzprose024_* T2 projects.
#
#   a1 a2 a3 a4   reject, then run again
#   b1 b2 b3 b4   list the stored rejections, remove one, run again
#   c1 c2         a stdin run with --genre commit-message
#
# Stop and report if a run shows a T2 failure; never repair it from here.
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WT="$(cd "$HERE/../../.." && pwd)"
OUT="${OUT_DIR:?set OUT_DIR}"
SCENARIOS="${SCENARIOS:-a b c}"
for g in $SCENARIOS; do
  case "$g" in
    a|b|c) ;;
    *) echo "run-review.sh: unknown scenario '$g' in SCENARIOS (a, b and c exist)" >&2; exit 2 ;;
  esac
done
case " $SCENARIOS " in
  *" b "*)
    case " $SCENARIOS " in
      *" a "*) ;;
      *) echo "run-review.sh: scenario b needs scenario a: b1 lists the rejections a2 stored, so b alone has none to list and scores FAIL. Name a with b (SCENARIOS=\"a b\")." >&2; exit 2 ;;
    esac ;;
esac
. "$HERE/runner_guard.bash"
runner_lock run-review.sh
export TMPDIR="${TMPDIR:?set TMPDIR to a directory outside the repository}"
export PROSE_EDIT_PROJECT_PREFIX="${PROSE_EDIT_PROJECT_PREFIX:-zzprose024_}"
export PROSE_EDIT_TEST=1  # PROSE_EDIT_OPEN is ignored without it
export PROSE_EDIT_OPEN="${PROSE_EDIT_OPEN:-true}"
DIR=docs/zz-review
DOC=$DIR/scenario.md
mkdir -p "$OUT"
echo "$SCENARIOS" > "$OUT/scenarios.txt"
cd "$WT" || exit 1
runner_sweep_stale
mkdir -p "$DIR" && runner_track "$DIR"
cp tests/prose_edit/fixtures/review-scenario.md "$DOC" || exit 1
git status --short > "$OUT/repo-status-before.txt"
python3 .claude/skills/prose-edit/scripts/memory.py viewer --set Typora > "$OUT/seed-viewer.json" || exit 1

run() { OUT_DIR="$OUT" "$HERE/run-scenario.sh" "$@"; }
# answer NAME FROM PROMPT: the author's answer, then the confirmation of the echo the skill shows.
answer() { RESUME_FROM="$2" run "$1" "$3"; RESUME_FROM="$1" run "${1}c" "Yes, apply."; }

case " $SCENARIOS " in *" c "*)
(
  run c1 "/prose-edit - --genre commit-message

fix: make the queue drain in order basically

This commit really quite simply changes the drain loop so that it is in fact ordered. It should be noted that the retry path is unchanged."
  answer c2 c1 "Accept edit 1."
) &
;; esac

case " $SCENARIOS " in *" a "*)
run a1 "/prose-edit $DOC --genre reference-doc --budget 5"
answer a2 a1 "Accept edit 1 and reject all the other edits."
run a3 "/prose-edit $DOC --genre reference-doc --budget 5"
answer a4 a3 "Hold every edit; accept none."
;; esac

case " $SCENARIOS " in *" b "*)
run b1 "/prose-edit rejections $DOC"
run b2 "/prose-edit rejections $DOC --remove 1"
run b3 "/prose-edit $DOC --genre reference-doc --budget 5"
answer b4 b3 "Hold every edit; accept none."
;; esac
wait
git status --short > "$OUT/repo-status-after.txt"
echo done > "$OUT/all.done"
