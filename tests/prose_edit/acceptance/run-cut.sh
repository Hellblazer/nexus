#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# usage: OUT_DIR=<dir> TMPDIR=<a directory outside the repository> [SCENARIOS="a web"] run-cut.sh
#
# The cut-only scenarios (RDR-221 Step 3.1, nexus-ger02.10): real headless runs of the prose-cut skill, each turn
# through run-scenario.sh, the decide turn through RESUME_FROM. They WRITE to T2 (a stored rejection), so every record goes to
# projects named with PROSE_EDIT_PROJECT_PREFIX (default zzprose0210_), never to the live prose projects. Afterwards:
#   python3 verdicts.py $OUT_DIR, then delete the zzprose0210_* T2 projects.
#
#   group a     a copy of fixtures/review-scenario.md at docs/zz-cut/scenario.md, in a nested directory no built-in
#               genre maps (so it is in no "Genre paths:" list and the editor's Grep never meets it), --genre how-to
#     cutonly-1    the cut run: at least one edit, every one only removes words, no app, no copy write, WORK printed
#     cutonly-1d   the decide turn (RESUME_FROM cutonly-1): the first cut rejected, the rest accepted; a dry run, then
#                  the real apply, no question, the rejection stored
#     cutonly-2    a fresh cut run on the same copy: the rejected change must not come back (VACUOUS when the editor
#                  never proposes it again)
#   group web   a cut run on the real page web/getting-started.html, never decided (the page is not changed): no edit
#               inside an excluded element. The word budget comes from `brief.py count`; when that fails the scenario is
#               NOT-RUN. The verdict is VACUOUS when the editor never tried an excluded element (the page has no nav)
#
# The decide turn names the work directory, the cut to reject and the cuts to accept, read from cutonly-1's own
# transcript by `verdicts.py --decide-args`; when cutonly-1 left no cut, cutonly-1d and cutonly-2 are NOT-RUN. Every
# scenario runs ONCE: there is no retry loop, because a retry that keeps the run in which the model happened to behave
# selects the pass (nexus-ger02.16). Do not run it while another runner holds the lock (one runner at a time; exit 75).
#
# Stop and report if a run shows a T2 failure; never repair it from here.
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WT="$(cd "$HERE/../../.." && pwd)"
OUT="${OUT_DIR:?set OUT_DIR}"
SCENARIOS="${SCENARIOS:-a web}"
for g in $SCENARIOS; do
  case "$g" in
    a|web) ;;
    *) echo "run-cut.sh: unknown scenario '$g' in SCENARIOS (a and web exist)" >&2; exit 2 ;;
  esac
done
. "$HERE/runner_guard.bash"
runner_lock run-cut.sh
export TMPDIR="${TMPDIR:?set TMPDIR to a directory outside the repository}"
export PROSE_EDIT_PROJECT_PREFIX="${PROSE_EDIT_PROJECT_PREFIX:-zzprose0210_}"
DIR=docs/zz-cut
DOC=$DIR/scenario.md
PAGE=web/getting-started.html
mkdir -p "$OUT" || exit 1
OUT="$(cd "$OUT" && pwd)"
echo "$SCENARIOS" > "$OUT/scenarios.txt"
cd "$WT" || exit 1
runner_sweep_stale
mkdir -p "$DIR" && runner_track "$DIR"
cp tests/prose_edit/fixtures/review-scenario.md "$DOC" || exit 1
git status --short > "$OUT/repo-status-before.txt"

# The caller's voice card, a text file outside the repository: the skill takes it by path.
CARD="$OUT/voice-card.txt"
cat > "$CARD" <<'EOF'
Plain technical English for a reader who runs the system. First person plural. Short declarative sentences.
No device to protect in this page: it is a list of notes, and a note that repeats the one before it can go.
EOF

run() { OUT_DIR="$OUT" "$HERE/run-scenario.sh" "$@"; }

# The word budget of the real page: nine tenths of its prose words, counted by `brief.py count` (a target, not a cap).
# It prints nothing and fails when the count does, so the web scenario is NOT-RUN and never gets an empty flag.
page_budget() {
  local counted
  counted="$(python3 prose-edit/skills/prose-edit/scripts/brief.py count "$PAGE")" || return 1
  python3 -c 'import json, sys; print(int(json.loads(sys.argv[1])["words"] * 0.9))' "$counted"
}

case " $SCENARIOS " in *" a "*)
run cutonly-1 "/conexus-prose-edit:prose-cut '$DOC' --mode cut-only --voice-card '$CARD' --word-budget 40 --genre how-to --budget 20"
if DECIDE="$(python3 "$HERE/verdicts.py" --decide-args "$OUT/cutonly-1.jsonl")" && [ -n "$DECIDE" ]; then
  IFS=$'\t' read -r WORK REJECT ACCEPT <<< "$DECIDE"
  RESUME_FROM=cutonly-1 run cutonly-1d "/conexus-prose-edit:prose-cut decide '$WORK' --accept $ACCEPT --reject $REJECT"
  run cutonly-2 "/conexus-prose-edit:prose-cut '$DOC' --mode cut-only --voice-card '$CARD' --word-budget 40 --genre how-to --budget 20"
else
  runner_not_run "$OUT" cutonly-1d "cutonly-1 left no WORK= line or no surviving cut to decide"
  runner_not_run "$OUT" cutonly-2 "cutonly-1d did not run, so no cut was rejected to look for again"
fi
;; esac

case " $SCENARIOS " in *" web "*)
if BUDGET="$(page_budget)" && [ -n "$BUDGET" ]; then
  run cutonly-web "/conexus-prose-edit:prose-cut '$PAGE' --mode cut-only --voice-card '$CARD' --word-budget $BUDGET --genre how-to --budget 20"
else
  runner_not_run "$OUT" cutonly-web "brief.py count failed on $PAGE, so the word budget is unknown"
fi
;; esac

git status --short > "$OUT/repo-status-after.txt"
echo done > "$OUT/all.done"
