#!/usr/bin/env bash
# scripts/plugin_cut_back_merge.sh — back-merge main into develop after a
# plugin cut (RDR-197), resolving the ONE conflict a cut creates by
# construction.
#
# The cut commit imports develop's plugin surface and then EMPTIES the
# ledger entries it ships (cut_plugin_release._rewrite_ledger). develop still
# carries those entries. Merge base -> develop replaced the "Nothing pending"
# text with the bullets; merge base -> main deleted them: overlapping hunks,
# so `git merge origin/main` on develop conflicts on
# conexus/PENDING_RELEASE.md every time (the RDR's "expected conflict-free"
# was wrong; found by tests/e2e/plugin-cut-rehearsal, 2026-08-30).
#
# Resolution: main's ledger wins — it is develop's ledger at cut time minus
# exactly the entries that shipped. The one thing that can go wrong is an
# entry develop added AFTER the cut branch was taken, which "main wins" would
# drop; develop's own drift contract (test_every_drifted_file_is_declared_
# in_the_ledger) fails loud on that, so the verify step below is the safety
# net, not a formality. Any conflict outside the ledger is refused and the
# merge aborted: that is not the cut's conflict and needs a human.
#
# Usage: scripts/plugin_cut_back_merge.sh [<repo>] [--no-verify]
#   <repo>        checkout to operate on (default: .)
#   --no-verify   skip develop's drift contract (unit tests only)
# Env: PLUGIN_CUT_BACK_MERGE_VERIFY_CMD overrides the verify command (the
#   unit tests exercise the net's plumbing with it; the default is develop's
#   real drift contract). Pushes nothing. Prints the push command on success.
#
# Exit states: 0 merged (and verified); 1 refused with the merge ABORTED
# (conflict outside the ledger, or the resolution commit itself failed --
# never left mid-merge), or merged-but-verify-RED (the merge commit stands;
# the operator re-adds the dropped entry on top of it).
set -euo pipefail

REPO="."
VERIFY=1
for arg in "$@"; do
    case "$arg" in
        --no-verify) VERIFY=0 ;;
        -h|--help) sed -n '2,26p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) REPO="$arg" ;;
    esac
done
LEDGER="conexus/PENDING_RELEASE.md"
g() { git -C "$REPO" "$@"; }

g fetch -q origin
g checkout -q develop
if ! g merge --no-edit origin/main >/dev/null 2>&1; then
    unmerged="$(g diff --name-only --diff-filter=U)"
    if [ "$unmerged" != "$LEDGER" ]; then
        echo "back-merge: conflicts outside the ledger, not the cut's conflict — aborted, resolve by hand:" >&2
        printf '  %s\n' $unmerged >&2
        g merge --abort
        exit 1
    fi
    g checkout --theirs -- "$LEDGER"
    g add -- "$LEDGER"
    if ! g -c core.editor=true merge --continue >/dev/null; then
        # A hook rejection, a signing failure, a full disk: never leave the
        # checkout mid-merge with the resolution staged (R4 review).
        echo "back-merge: the resolution commit failed — merge ABORTED, develop unchanged" >&2
        g merge --abort || true
        exit 1
    fi
    echo "back-merge: $LEDGER resolved to main's version (the cut emptied the entries it shipped)"
fi

VERIFY_CMD="${PLUGIN_CUT_BACK_MERGE_VERIFY_CMD:-NX_REQUIRE_PLUGIN_DRIFT_CHECK=1 uv run pytest tests/test_plugin_release_drift_ledger.py -q}"
if [ "$VERIFY" = 1 ]; then
    if ! (cd "$REPO" && bash -c "$VERIFY_CMD"); then
        echo "back-merge: develop's drift contract is RED after the merge — a ledger entry develop added after the cut was dropped by the resolution; re-add it, commit, and re-run the contract" >&2
        exit 1
    fi
fi
echo "back-merge: develop = $(g rev-parse --short HEAD); push with: git -C $REPO push origin develop"
