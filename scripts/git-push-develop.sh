#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Push the local integration branch only when every commit in the outbound
# range is one the caller vouches for (nexus-9wxu6).
#
# Why: several sessions share one primary checkout and commit as the same
# git user, so the author field cannot tell a peer's unpushed commit from
# the caller's own. On 2026-09-07 four pushes carried a peer's commits
# because the origin/develop..develop read was printed beside the push or
# skipped on a retry. This script makes the read and the push one step,
# with the vouch list as the only thing the caller supplies: the outbound
# range must equal the vouched set exactly, or nothing is pushed.
#
# Usage: git-push-develop.sh <sha> [<sha> ...]
#   Each argument is a commit the caller made (any revision that resolves
#   to a commit; short SHAs are fine). With no arguments the script only
#   reports; a non-empty range is refused, never pushed.
#
#   A vouched MERGE commit also vouches for everything it merges in (the
#   commits reachable from its second and later parents that are not yet on
#   the remote branch). That is the release back-merge and the plugin-cut
#   back-merge: the caller made the merge, so main's release-only commits
#   ride it. Commits under the merge on its FIRST-parent line (a peer's
#   unpushed work on the local branch) are never covered by it.
#
# Environment:
#   NX_PUSH_REMOTE   remote name (default origin)
#   NX_PUSH_BRANCH   branch name (default develop)
#   NX_PUSH_SOURCE   the local revision to push (default refs/heads/$branch).
#                    Set it to HEAD from a detached worktree based on
#                    $remote/$branch when the local branch is a peer's
#                    in-flight tree: the range is then $remote/$branch..HEAD
#                    and the local branch is left alone.
#
# Exit codes (each failure prints a PUSH_REFUSED_* line first):
#   0  PUSH_OK n=<count> tip=<sha>, or PUSH_NOOP when nothing is outbound
#      and nothing was vouched
#   2  PUSH_REFUSED_FOREIGN     the range holds commits nobody vouched for
#   3  PUSH_REFUSED_STALE_VOUCH a vouched commit is not in the range (already
#                               pushed, rebased away, or on another branch)
#   4  PUSH_REFUSED_DIVERGED    the remote branch is not an ancestor of the
#                               local one; rebase or merge first
#   5  PUSH_REFUSED_BAD_SHA     an argument does not resolve to a commit
#   6  PUSH_REFUSED_NO_BRANCH   the local or remote-tracking branch is missing

set -euo pipefail

remote="${NX_PUSH_REMOTE:-origin}"
branch="${NX_PUSH_BRANCH:-develop}"
source="${NX_PUSH_SOURCE:-refs/heads/$branch}"

git rev-parse --show-toplevel >/dev/null

git fetch -q "$remote"

# Resolve the tip ONCE. Everything below (ancestry, range, push) uses this
# sha, never the moving ref, so a peer commit landing on the branch between
# the range read and the push cannot ride it.
if ! tip="$(git rev-parse -q --verify "$source^{commit}")"; then
  echo "PUSH_REFUSED_NO_BRANCH source $source does not resolve to a commit"
  exit 6
fi
if ! git rev-parse -q --verify "refs/remotes/$remote/$branch^{commit}" >/dev/null; then
  echo "PUSH_REFUSED_NO_BRANCH $remote/$branch does not exist after fetch"
  exit 6
fi

if ! git merge-base --is-ancestor "refs/remotes/$remote/$branch" "$tip"; then
  echo "PUSH_REFUSED_DIVERGED $remote/$branch is not an ancestor of $source; rebase or merge first"
  exit 4
fi

range=()
while IFS= read -r sha; do
  [[ -n "$sha" ]] && range+=("$sha")
done < <(git rev-list "refs/remotes/$remote/$branch..$tip")

vouched=()
for arg in "$@"; do
  if ! full="$(git rev-parse -q --verify "$arg^{commit}")"; then
    echo "PUSH_REFUSED_BAD_SHA $arg does not resolve to a commit"
    exit 5
  fi
  vouched+=("$full")
  # A merge's non-first parents: what it merges in is covered by vouching it.
  while IFS= read -r parent; do
    [[ -z "$parent" ]] && continue
    while IFS= read -r merged; do
      [[ -n "$merged" ]] && vouched+=("$merged")
    done < <(git rev-list "$parent" "^refs/remotes/$remote/$branch")
  done < <(git rev-list --parents -n 1 "$full" | cut -d' ' -f3-  | tr ' ' '\n')
done

if [[ ${#range[@]} -eq 0 && ${#vouched[@]} -eq 0 ]]; then
  echo "PUSH_NOOP $source is already at $remote/$branch"
  exit 0
fi

contains() {
  local needle="$1"; shift
  local x
  for x in "$@"; do [[ "$x" == "$needle" ]] && return 0; done
  return 1
}

foreign=()
for sha in "${range[@]+"${range[@]}"}"; do
  contains "$sha" "${vouched[@]+"${vouched[@]}"}" || foreign+=("$sha")
done
stale=()
for sha in "${vouched[@]+"${vouched[@]}"}"; do
  contains "$sha" "${range[@]+"${range[@]}"}" || stale+=("$sha")
done

if [[ ${#foreign[@]} -gt 0 ]]; then
  echo "PUSH_REFUSED_FOREIGN ${#foreign[@]} of ${#range[@]} outbound commit(s) on $source are not vouched:"
  for sha in "${foreign[@]}"; do
    git log -1 --format='  %h %an %s' "$sha"
  done
  echo "Vouch only for commits this session made. A peer's commit is theirs to push; do not add its sha to unblock yourself."
  exit 2
fi

if [[ ${#stale[@]} -gt 0 ]]; then
  echo "PUSH_REFUSED_STALE_VOUCH ${#stale[@]} vouched commit(s) are not in $remote/$branch..$source:"
  for sha in "${stale[@]}"; do
    git log -1 --format='  %h %s' "$sha"
  done
  echo "Re-read the range: the commit was already pushed, rebased away, or is on another branch."
  exit 3
fi

git push -q "$remote" "$tip:refs/heads/$branch"
echo "PUSH_OK n=${#range[@]} tip=$tip"
