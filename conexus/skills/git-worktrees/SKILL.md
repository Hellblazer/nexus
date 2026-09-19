---
name: git-worktrees
description: Use when starting feature work that needs isolation from current workspace, before executing implementation plans, or when MORE THAN ONE SESSION is working on one checkout - creates isolated git worktrees with smart directory selection and safety verification
effort: medium
---

# Git Worktrees

Git worktrees create isolated workspaces sharing the same repository.

## Directory Selection (priority order)

1. Check existing: `.worktrees/` (preferred) or `worktrees/`
2. Check CLAUDE.md for worktree directory preference
3. Ask user: `.worktrees/` (project-local, hidden) or `~/.config/nexus/worktrees/<project>/` (global)

## Safety: Verify Ignored

**Before creating project-local worktrees:**

```bash
git check-ignore -q .worktrees 2>/dev/null
```

If NOT ignored: add to `.gitignore` and commit, then proceed.

## Creation

```bash
project=$(basename "$(git rev-parse --show-toplevel)")
git worktree add "$path" -b "$BRANCH_NAME"
cd "$path"
```

### Setup (auto-detect)

```bash
[ -f pyproject.toml ]    && { command -v uv &>/dev/null && uv sync || pip install -e .; }
[ -f requirements.txt ] && ! [ -f pyproject.toml ] && pip install -r requirements.txt
[ -f package.json ]     && npm install
[ -f Cargo.toml ]       && cargo build
[ -f go.mod ]           && go mod download
```

### Verify baseline

Run project test suite. If tests fail: report failures, ask whether to proceed.

## Branch Naming

Follow project convention. For beads-tracked work: `feature/<bead-id>-<description>`.

## More Than One Session On One Checkout

**Each session takes its own worktree; the shared checkout becomes
reference-only.** This is the case the rest of this skill did not cover:
the sections above are about isolating FEATURE WORK, and Agent Isolation
below is about subagent dispatches. Neither says where the session itself
lives, so sessions default into the shared checkout — not by decision, by
omission.

What that default costs, measured on a repo with three concurrent
sessions: a commit that carries a peer's uncommitted file because
`git commit -- <path>` takes the WORKING TREE version; a push refused
because a peer's unpushed commit is an ancestor of yours; a deadlock where
neither session can push because each is behind the other's unready work;
and subagent briefs that have to name permitted files because the tree is
shared. None of these are avoidable by care. In separate worktrees they
cannot occur.

**Keep the shared checkout on the integration branch.** Git refuses to
check out a branch that is already checked out elsewhere, so the shared
checkout HOLDING that branch is what makes "everyone works on a feature
branch" self-enforcing rather than a thing to remember.

**"Reference-only" means no edits, no commits, no staging, no branch
switches.** Build and test output is expected — a test run writes there.
State it that way or the first person to run a suite thinks they broke the
rule and the second concludes the rule is advisory.

### Moving a session that is already in flight

Do the work in the new worktree FIRST and verify it there; only then
revert the source. Never the other way round.

- **Uncommitted work**: `git diff <upstream> -- <paths>` out, `git apply`
  in, then revert just those paths in the source. Check that the
  diff-vs-upstream and diff-vs-HEAD are identical first — that is what
  proves the paths rebase cleanly BEFORE anything moves.
- **Committed work**: cherry-pick it. A commit is recoverable; an
  applied-but-unverified diff is not.
- Do not move a tree a subagent is still writing to.

### Run verification from the worktree, not from where the shell sits

The shared checkout stays on the integration branch and is therefore
always a VALID tree — it just is not YOUR tree. A linter, a formatter or
a scoped test run invoked from there examines files without your edits
and comes back clean, and nothing about the output says which tree it
read. This is the easy mistake in this layout precisely because the
reference checkout is never broken.

Before trusting a verification run, confirm the working directory is the
worktree holding the change. A green belongs to a TREE, and here the tree
is chosen by the shell's cwd rather than by anything in the command.

### Serena differs by how you got there

A session STARTED in a worktree gets its own language server rooted there
(`--project-from-cwd`) and keeps symbol editing. A session that RELOCATES
mid-flight keeps the server rooted at the original checkout, so it must
use ordinary file edits with absolute worktree paths instead.

### Project-specific mechanics live with the project

A worktree may need setup the shared checkout already has — a virtualenv,
a built artifact, a cached toolchain — and which suites or gates must run
in the original checkout is a property of that project's build, not of
worktrees. Those rules belong in the project's own AGENTS.md or CLAUDE.md
next to the commands they concern, NOT here: a second copy of a
project-specific rule drifts from the first until the stale one wins.

## Agent Isolation

The Agent tool supports `isolation: "worktree"` natively — it creates a temporary git worktree automatically and cleans up if no changes are made. Prefer this for parallel agent work over manual worktree management.

## Quick Reference

| Situation | Action |
|---|---|
| `.worktrees/` exists | Use it (verify ignored) |
| `worktrees/` exists | Use it (verify ignored) |
| Both exist | Use `.worktrees/` |
| Neither exists | Check CLAUDE.md → ask user |
| Not ignored | Add to .gitignore + commit |
| Tests fail baseline | Report + ask |
| Parallel agents | Use `isolation: "worktree"` on Agent tool |
| Another session shares this checkout | Each session takes its own worktree; the shared one goes reference-only |
| Relocating a session mid-flight | Verify in the new worktree BEFORE reverting the source |
| Verifying a change | Run it from the worktree holding the edits, not from the reference checkout |

**Pairs with:** `/conexus:finishing-branch` for merge/PR/cleanup after work is done.
