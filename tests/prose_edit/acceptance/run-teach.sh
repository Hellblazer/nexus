#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# usage: OUT_DIR=<dir> TMPDIR=<a directory outside the repository> run-teach.sh
#
# Scenario 6 and step 13 of the prose-edit skill (RDR-221, nexus-ger02.6 critique E), as real headless runs, each
# turn through run-scenario.sh and an answer through RESUME_FROM. They WRITE to T2, so every record goes to
# projects named with PROSE_EDIT_PROJECT_PREFIX (default zzprose0206_), never to the live prose projects. Afterwards:
#   python3 teach_verdicts.py $OUT_DIR, then delete the zzprose0206_* T2 projects.
#
#   s6-1            scenario 6: a user-level entry is stored first, then a document of another genre is edited
#   t1 t2 t2c       step 13, voice card: edit a copy of fixtures/review-scenario.md at docs/zz-teach-scenario.md,
#                   the author's answer, and the confirmation of the dry-run echo the skill shows
#   t3              the author's yes to keeping the voice card; the script then reads the card back itself
#   t4 t4c          step 13, promote: the skill dry-runs and asks; the author confirms and it promotes for real
#
# Between t3 and t4 the script runs a real promote with no dry run, by hand: the refusal is a property of the
# script, and no model turn can be trusted to produce it. Every flow runs ONCE: there is no retry loop, because a
# retry that keeps the run in which the model happened to behave selects the pass (nexus-ger02.16).
#
# Stop and report if a run shows a T2 failure; never repair it from here.
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WT="$(cd "$HERE/../../.." && pwd)"
OUT="${OUT_DIR:?set OUT_DIR}"
export TMPDIR="${TMPDIR:?set TMPDIR to a directory outside the repository}"
export PROSE_EDIT_PROJECT_PREFIX="${PROSE_EDIT_PROJECT_PREFIX:-zzprose0206_}"
export PROSE_EDIT_TEST=1  # PROSE_EDIT_OPEN is ignored without it
export PROSE_EDIT_OPEN="${PROSE_EDIT_OPEN:-true}"
DOC=docs/zz-teach-scenario.md
S6DOC=tests/prose_edit/fixtures/user-entry.md
MEM=(python3 .claude/skills/prose-edit/scripts/memory.py)
mkdir -p "$OUT"
cd "$WT" || exit 1
cp tests/prose_edit/fixtures/review-scenario.md "$DOC" || exit 1
trap 'rm -f "$WT/$DOC"' EXIT
git status --short > "$OUT/repo-status-before.txt"
"${MEM[@]}" viewer --set Typora > "$OUT/seed-viewer.json" || exit 1

run() { OUT_DIR="$OUT" "$HERE/run-scenario.sh" "$@"; }

# Scenario 6: the entry is stored at user level (the prefix's own user project), used once, and removed, so the
# step 13 runs below do not see a rule about the word worker.
python3 "$HERE"/teach_verdicts.py seed-json | "${MEM[@]}" add-entry --level user --from-stdin > "$OUT/seed-entry.json" || exit 1
run s6-1 "/prose-edit $S6DOC --genre changelog --budget 5"
"${MEM[@]}" entries --level user --remove diagnostics > "$OUT/seed-entry-removed.json" || exit 1

run t1 "/prose-edit $DOC --genre reference-doc --budget 5"
RESUME_FROM=t1 run t2 "Accept edit 1 and reject all the other edits."
RESUME_FROM=t2 run t2c "Yes, apply."
RESUME_FROM=t2c run t3 "Yes, keep that voice card as shown."
"${MEM[@]}" voice-card "$DOC" > "$OUT/voice-card-after.json"
python3 .claude/skills/prose-edit/scripts/memory.py promote "$DOC" 1 --level repo > "$OUT/promote-nodry.out" 2> "$OUT/promote-nodry.err"
echo "rc=$?" > "$OUT/promote-nodry.rc"
RESUME_FROM=t3 run t4 "The first stored rejection is never a defect: promote it at repo level."
RESUME_FROM=t4 run t4c "Yes, store it."
git status --short > "$OUT/repo-status-after.txt"
echo done > "$OUT/all.done"
