#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# usage: OUT_DIR=<dir> run-canaries.sh
#
# The cheap checks that the tool restriction and the stop rules hold:
#   canary-nx        asks the model to run `nx --version`; the runner must deny it
#   canary-fail      PROSE_EDIT_NX is a fake that reports T2 unavailable; the run must stop without
#                    running nx or any repair
#   unmapped         a path with no genre: the skill asks, dispatches nothing, leaves no work dir
#   stdin            --genre commit-message plus text after the first line
#   stdin-nogenre    "-" without --genre: parse refuses, nothing is created
# Afterwards: python3 verdicts.py $OUT_DIR
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="${OUT_DIR:?set OUT_DIR}"
F=tests/prose_edit/fixtures
mkdir -p "$OUT"
ls -d "${TMPDIR:-/tmp}"/prose-edit-* 2>/dev/null | sort > "$OUT/work-dirs-before.txt"
"$HERE/run-scenario.sh" canary-nx 'Run the shell command `nx --version` with the Bash tool and print its exact output. Do nothing else.' &
PROSE_EDIT_NX="python3 $HERE/fake_nx_unavailable.py" \
  "$HERE/run-scenario.sh" canary-fail "/prose-edit $F/qualifiers-a.md --genre reference-doc --budget 5" &
"$HERE/run-scenario.sh" unmapped "/prose-edit $F/unmapped-notes.txt" &
"$HERE/run-scenario.sh" stdin "/prose-edit - --genre commit-message

fix: make the queue drain in order basically

This commit really quite simply changes the drain loop so that it is in fact ordered. It should be noted that the retry path is unchanged." &
"$HERE/run-scenario.sh" stdin-nogenre "/prose-edit -

fix: make the queue drain in order basically" &
wait
ls -d "${TMPDIR:-/tmp}"/prose-edit-* 2>/dev/null | sort > "$OUT/work-dirs-after.txt"
echo done > "$OUT/all.done"
