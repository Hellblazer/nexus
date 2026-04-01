---
name: finishing-branch
description: Use when implementation is complete, all tests pass, and you need to decide how to integrate the work - guides completion of development work by presenting structured options for merge, PR, or cleanup
effort: medium
---

# Finishing a Development Branch

Verify tests → present options → execute choice → clean up.

## Step 1: Verify Tests

```bash
uv run pytest   # or project-appropriate command
```

If tests fail: **stop.** Fix before proceeding.

## Step 2: Determine Base Branch

```bash
git merge-base HEAD main 2>/dev/null || git merge-base HEAD master 2>/dev/null
```

## Step 3: Present Options

```
Implementation complete. Options:

1. Merge back to <base-branch> locally
2. Push and create a Pull Request
3. Keep the branch as-is
4. Discard this work
```

## Step 4: Execute

### Option 1 — Merge locally
```bash
git checkout <base-branch>
git pull
git merge <feature-branch>
# verify tests on merged result
git branch -d <feature-branch>
```

### Option 2 — Push + PR
```bash
git push -u origin <feature-branch>
gh pr create --title "<title>" --body "$(cat <<'EOF'
## Summary
<bullets>

## Test plan
- [ ] <steps>
EOF
)"
```

### Option 3 — Keep as-is
Report location. Don't clean up worktree.

### Option 4 — Discard
**Require user to type `discard` to confirm.** Do not accept "yes" or "ok".
```bash
git checkout <base-branch>
git branch -D <feature-branch>
```

## Step 5: Cleanup

Detect whether running in a worktree:
```bash
git worktree list | grep "$(pwd)"
```

| Option | Clean worktree? | Clean branch? |
|---|---|---|
| 1. Merge | Yes | Yes |
| 2. PR | Yes | No |
| 3. Keep | No | No |
| 4. Discard | Yes | Yes (force) |

If in a worktree (options 1, 2, 4):
```bash
cd <base-worktree>
git worktree remove <path>
```

## Beads Integration

If beads are active, close related beads before or after merge:
```bash
bd close <bead-id> --reason="Merged to <base-branch>"
```

## Sync Beads

If beads are active, push state to remote after closing:
```bash
bd dolt push
```

**Pairs with:** `/nx:git-worktrees` for initial workspace setup.
