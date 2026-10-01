#!/bin/bash
# usage: OUT_DIR=<dir> run-review.sh
#
# The review-loop scenarios (RDR-221 Steps 1.1 and 1.5): real headless runs of the prose-edit skill, each
# turn through run-scenario.sh, an answer turn through RESUME_FROM. The skill echoes the author's answer
# (apply --dry-run) and waits, so every answer turn is followed by a confirmation turn ("Yes, apply.",
# named NAMEc). They WRITE to T2, so every record goes
# to projects named with PROSE_EDIT_PROJECT_PREFIX (default zzprose024_), never to the live prose projects.
# The scenario document is a copy of fixtures/review-scenario.md at docs/zz-review-scenario.md, removed on
# exit. Afterwards: python3 review_verdicts.py $OUT_DIR, then delete the zzprose024_* T2 projects.
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
export TMPDIR="${TMPDIR:?set TMPDIR to a directory outside the repository}"
export PROSE_EDIT_PROJECT_PREFIX="${PROSE_EDIT_PROJECT_PREFIX:-zzprose024_}"
export PROSE_EDIT_TEST=1  # PROSE_EDIT_OPEN is ignored without it
export PROSE_EDIT_OPEN="${PROSE_EDIT_OPEN:-true}"
DOC=docs/zz-review-scenario.md
mkdir -p "$OUT"
cd "$WT" || exit 1
cp tests/prose_edit/fixtures/review-scenario.md "$DOC" || exit 1
trap 'rm -f "$WT/$DOC"' EXIT
git status --short > "$OUT/repo-status-before.txt"
python3 .claude/skills/prose-edit/scripts/memory.py viewer --set Typora > "$OUT/seed-viewer.json" || exit 1

run() { OUT_DIR="$OUT" "$HERE/run-scenario.sh" "$@"; }
# answer NAME FROM PROMPT: the author's answer, then the confirmation of the echo the skill shows.
answer() { RESUME_FROM="$2" run "$1" "$3"; RESUME_FROM="$1" run "${1}c" "Yes, apply."; }

# rerun NAME PROMPT: the editor picks the span of an edit afresh each run, so a rejected edit often comes back
# with a different old string. Repeat (at most 4 times) until the editor proposes the edit the check needs;
# each failed attempt is kept as NAME-tryN and its work directory closed by an answer turn.
rerun() {
  local name="$1" prompt="$2" i rc
  for i in 1 2 3 4; do
    run "$name" "$prompt"
    python3 "$HERE/review_verdicts.py" --probe "$name" "$OUT"
    rc=$?
    echo "$name attempt $i probe rc=$rc" >> "$OUT/attempts.txt"
    if [ "$rc" -ne 3 ] || [ "$i" -eq 4 ]; then return 0; fi
    for ext in jsonl err rc; do cp "$OUT/$name.$ext" "$OUT/$name-try$i.$ext"; done
    answer "$name-hold$i" "$name" "Hold every edit; accept none."
  done
}

(
  run c1 "/prose-edit - --genre commit-message

fix: make the queue drain in order basically

This commit really quite simply changes the drain loop so that it is in fact ordered. It should be noted that the retry path is unchanged."
  answer c2 c1 "Accept edit 1."
) &

run a1 "/prose-edit $DOC --budget 5"
answer a2 a1 "Accept edit 1 and reject all the other edits."
rerun a3 "/prose-edit $DOC --budget 5"
answer a4 a3 "Hold every edit; accept none."
run b1 "/prose-edit rejections $DOC"
run b2 "/prose-edit rejections $DOC --remove 1"
rerun b3 "/prose-edit $DOC --budget 5"
answer b4 b3 "Hold every edit; accept none."
wait
git status --short > "$OUT/repo-status-after.txt"
echo done > "$OUT/all.done"
