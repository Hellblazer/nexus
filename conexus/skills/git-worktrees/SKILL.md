---
name: git-worktrees
description: Use when starting feature work that needs isolation from current workspace or before executing implementation plans - creates isolated git worktrees with smart directory selection and safety verification
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

**Pairs with:** `/nx:finishing-branch` for merge/PR/cleanup after work is done.
