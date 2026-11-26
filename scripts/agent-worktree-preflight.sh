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
#   When omitted, the required base is the NEWER of local `develop` and
#   the repo-local `origin/develop` (nexus-aukeu). Both directions happen
#   routinely and for structural reasons: batched pushes run local AHEAD
#   of origin, while landings through worktrees leave the primary's local
#   develop BEHIND origin for long stretches. Preferring local
#   unconditionally, as this did until 2026-09-11, under-recovers in the
#   second window exactly as a pure-origin default would in the first —
#   measured, an agent told to build on a commit only origin had got
#   PREFLIGHT_OK against a tree 25 commits behind. So the two are
#   compared: whichever is a descendant of the other is the base. A
#   genuine divergence refuses and names both tips rather than guessing.
#   Neither branch is ever fetched; if neither resolves locally,
#   preflight refuses. Dispatchers should pass the sha explicitly anyway.
#
#   A release worktree is the exception (nexus-sby49). When REQUIRED_SHA is
#   omitted and the branch is release/* or release-prep/*, or HEAD is detached
#   at a commit any tag points to (a release or engine-service cut), there is no base to compare against: the tree is cut from
#   the last tag (the plugin-only fast lane) or from a develop that has since
#   moved on, and its contents ARE the release, so a fast-forward to develop
#   would change what ships. The script touches nothing, prints the usual
#   PREFLIGHT_OK line with recovered=no, then one PREFLIGHT_NOTE line saying it
#   did not compare against develop, and exits 0. A passed REQUIRED_SHA keeps
#   the behaviour above everywhere, release branches included.
#
# Exit codes (every failure path prints a named discriminator line first):
#   0  PREFLIGHT_OK head=<sha> recovered=<yes|no>; a defaulted base on a
#      release tree adds a second line, PREFLIGHT_NOTE release branch: ...
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

# RDR-253 Step 6: a worktree is work its creator's identity owns. Label it so nx identity may-stop keeps another
# session from removing it while this one runs. Best effort in every part: skipped without nx, silent on any failure,
# bounded in time where `timeout` exists, and never a reason to turn a PREFLIGHT_OK into a failure. Runs only after
# the verdict line is printed.
register_worktree() {
  command -v nx >/dev/null 2>&1 || return 0
  local wt
  wt="$(pwd -P)" || return 0
  if command -v timeout >/dev/null 2>&1; then
    timeout 5 nx identity work register --kind worktree --ref "$wt" >/dev/null 2>&1 || true
  else
    nx identity work register --kind worktree --ref "$wt" >/dev/null 2>&1 || true
  fi
  return 0
}

# True on a release/* or release-prep/* branch, or with HEAD detached at a commit any tag points to.
is_release_tree() {
  local branch
  if branch="$(git symbolic-ref -q --short HEAD)"; then
    case "$branch" in release/*|release-prep/*) return 0 ;; esac
    return 1
  fi
  [ -n "$(git tag --points-at HEAD)" ]
}

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
elif is_release_tree; then
  # Defaulted base on a release tree (nexus-sby49): leave it exactly as found.
  echo "PREFLIGHT_OK head=$(git rev-parse HEAD) recovered=no"
  echo "PREFLIGHT_NOTE release branch: base is its own tag/cut, not develop; pass a sha to check one"
  register_worktree
  exit 0
else
  # No sha given: pick the NEWER of local develop and origin/develop rather than
  # preferring local unconditionally (nexus-aukeu).
  #
  # Local-first was written for this project's batched-push workflow, where
  # local develop routinely runs AHEAD of origin. But the opposite also holds
  # routinely, and for a structural reason: landings go through worktrees, never
  # the primary, so the primary's local develop sits BEHIND origin for long
  # stretches. Measured 2026-09-11 — an agent told to build on a commit only
  # origin had got PREFLIGHT_OK against a tree 25 commits behind it, and
  # recovered by hand afterwards. Preferring local in that window is exactly the
  # under-recovery local-first was meant to prevent, in the other direction.
  #
  # So compare instead of assuming. When one is an ancestor of the other, the
  # descendant is the right base whichever ref it came from. When they have
  # genuinely diverged, neither is safe to guess at, so refuse and name both
  # tips rather than silently picking one.
  local_dev="$(git rev-parse -q --verify refs/heads/develop^{commit} || true)"
  origin_dev="$(git rev-parse -q --verify refs/remotes/origin/develop^{commit} || true)"
  if [ -n "$local_dev" ] && [ -n "$origin_dev" ]; then
    if [ "$local_dev" = "$origin_dev" ]; then
      required_sha="$local_dev"
    elif git merge-base --is-ancestor "$local_dev" "$origin_dev"; then
      required_sha="$origin_dev"      # local is behind: origin is the real base
    elif git merge-base --is-ancestor "$origin_dev" "$local_dev"; then
      required_sha="$local_dev"       # local is ahead: the batched-push window
    else
      echo "PREFLIGHT_FAIL_DIVERGED_DEFAULT local=${local_dev} origin=${origin_dev}"
      echo "  develop and origin/develop have diverged, so neither is a safe default."
      echo "  Pass the required sha explicitly (dispatchers should always do this)."
      exit 5
    fi
  elif [ -n "$local_dev" ]; then
    required_sha="$local_dev"
  elif [ -n "$origin_dev" ]; then
    required_sha="$origin_dev"
  else
    echo "PREFLIGHT_FAIL_BAD_SHA develop|origin/develop"
    exit 5
  fi
fi

if git merge-base --is-ancestor "$required_sha" HEAD; then
  head_sha="$(git rev-parse HEAD)"
  echo "PREFLIGHT_OK head=${head_sha} recovered=no"
  register_worktree
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
register_worktree
exit 0
