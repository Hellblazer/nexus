#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Agent-worktree preflight (nexus-5kwkf). Worktree-dispatched agents run
# this as their FIRST action, before any edit, and stop on any
# PREFLIGHT_FAIL line.
#
# Root cause this guards against: the dispatch harness cuts an agent's
# `isolation:worktree` worktree from the repo's DEFAULT branch tip, not
# from the session's current branch — so a fresh worktree can be silently
# N commits behind `develop` by construction. A second, worse failure mode
# observed the same day: a worktree dispatch produced no worktree at all,
# leaving the agent operating in the shared primary checkout while
# believing itself isolated. This script checks for both:
#
#   1. Isolation: the cwd must be a linked worktree, never the primary
#      checkout.
#   2. Base: the worktree's HEAD must carry REQUIRED_SHA. A stale-but-clean
#      worktree is recovered in place via `git merge --ff-only`; a dirty or
#      diverged worktree is refused rather than touched.
#
# Usage: agent-worktree-preflight.sh [REQUIRED_SHA]
#   REQUIRED_SHA is optional and may be any git revision (sha, branch,
#   tag, HEAD, etc.) — it is resolved and existence-checked as a single
#   guarded step (`git rev-parse -q --verify <rev>^{commit}`), so both an
#   unresolvable ref AND a syntactically valid but nonexistent full sha
#   are caught the same way, rather than the former crashing the script
#   via errexit or the latter silently misfolding into the diverged path.
#
#   When omitted, the required base defaults to the LOCAL `develop`
#   branch tip if one exists (refs/heads are shared across all worktrees
#   of a repo, so this is visible even though the worktree itself is
#   checked out to some other branch); only if there is no local
#   `develop` does it fall back to the repo-local `origin/develop` ref.
#   Local-first matters because this project's own workflow (routine
#   work commits direct to `develop`, batched pushes) routinely runs
#   local `develop` ahead of `origin/develop` for extended windows — a
#   pure-origin default would silently under-recover in exactly that
#   window. Neither branch is ever fetched; if neither resolves locally,
#   preflight refuses rather than guessing.
#
# Exit codes (every failure path prints a named discriminator line first):
#   0  PREFLIGHT_OK head=<sha> recovered=<yes|no>
#   2  PREFLIGHT_FAIL_PRIMARY_CHECKOUT — not a linked worktree. STOP: zero
#      edits, zero git writes.
#   3  PREFLIGHT_FAIL_DIVERGED — REQUIRED_SHA is not fast-forward reachable
#      from HEAD; worktree left untouched.
#   4  PREFLIGHT_FAIL_DIRTY_TREE — stale AND dirty; refused before any
#      recovery attempt, dirt left untouched.
#   5  PREFLIGHT_FAIL_BAD_SHA — REQUIRED_SHA (explicit or defaulted) does
#      not resolve to a real commit object; nothing touched.

set -euo pipefail

required_sha_input="${1:-}"

git_dir="$(git rev-parse --git-dir)"
case "$git_dir" in
  */.git/worktrees/*)
    ;;
  *)
    toplevel="$(git rev-parse --show-toplevel 2>/dev/null || echo unknown)"
    echo "PREFLIGHT_FAIL_PRIMARY_CHECKOUT toplevel=${toplevel} git_dir=${git_dir}"
    exit 2
    ;;
esac

if [ -n "$required_sha_input" ]; then
  if ! required_sha="$(git rev-parse -q --verify "${required_sha_input}^{commit}")"; then
    echo "PREFLIGHT_FAIL_BAD_SHA ${required_sha_input}"
    exit 5
  fi
elif required_sha="$(git rev-parse -q --verify refs/heads/develop^{commit})"; then
  :
elif required_sha="$(git rev-parse -q --verify refs/remotes/origin/develop^{commit})"; then
  :
else
  echo "PREFLIGHT_FAIL_BAD_SHA develop|origin/develop"
  exit 5
fi

if git merge-base --is-ancestor "$required_sha" HEAD; then
  head_sha="$(git rev-parse HEAD)"
  echo "PREFLIGHT_OK head=${head_sha} recovered=no"
  exit 0
fi

if [ -n "$(git status --porcelain)" ]; then
  echo "PREFLIGHT_FAIL_DIRTY_TREE required=${required_sha} head=$(git rev-parse HEAD)"
  exit 4
fi

if ! git merge --ff-only "$required_sha" >/dev/null 2>&1; then
  echo "PREFLIGHT_FAIL_DIVERGED required=${required_sha} head=$(git rev-parse HEAD)"
  exit 3
fi

if ! git merge-base --is-ancestor "$required_sha" HEAD; then
  echo "PREFLIGHT_FAIL_DIVERGED required=${required_sha} head=$(git rev-parse HEAD)"
  exit 3
fi

head_sha="$(git rev-parse HEAD)"
echo "PREFLIGHT_OK head=${head_sha} recovered=yes"
exit 0
