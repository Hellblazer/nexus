# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The behaviour leg's decision tables: the rows each hook is driven with and what each must answer (nexus-sby49.4).

Split out of ``plugin_behaviour.py`` (over its size cap) as the one self-contained area of data with no code in it: the
guard's rows of plan section 2.1 in the seatbelt and strict modes, the dirty-tree rows, the credential and advice rows, the
tools auto-approved for every user and the Stop guard's CI wording. ``plugin_behaviour`` re-exports every name, so the leg
and its tests read them as before.
"""
from __future__ import annotations

# --- section 2.1: the git guard per verb, in the default mode and in strict mode ---------------------------
#: (command, seatbelt, strict): "deny" or "pass". The seatbelt's dirty-tree rows are in ``DIRTY_ROWS``.
GUARD_ROWS: list[tuple[str, str, str]] = [
    ("git commit -am x", "pass", "deny"),
    ("git add f", "pass", "deny"),
    ("git push origin main", "pass", "deny"),
    ("git push -f origin main", "deny", "deny"),
    ("git push --force-with-lease origin main", "deny", "deny"),
    ("git push --force-if-includes origin main", "deny", "deny"),
    ("git push origin +main", "deny", "deny"),
    ("git push origin --delete foo", "deny", "deny"),
    ("git push origin :foo", "deny", "deny"),
    ("git push --mirror origin", "deny", "deny"),
    ("git push --prune origin", "deny", "deny"),
    ("git rebase main", "pass", "deny"),
    ("git merge other", "pass", "deny"),
    ("git pull", "pass", "deny"),
    ("git cherry-pick abc1234", "pass", "deny"),
    ("git revert abc1234", "pass", "deny"),
    ("git branch newb", "pass", "pass"),
    ("git branch -d foo", "pass", "deny"),
    ("git branch -D foo", "deny", "deny"),
    ("git branch --delete --force foo", "deny", "deny"),
    ("git branch -M a b", "deny", "deny"),
    ("git branch -f x HEAD", "pass", "deny"),
    ("git tag v1", "pass", "deny"),
    ("git tag -d v1", "pass", "deny"),
    ("git config core.hooksPath /tmp/x", "deny", "deny"),
    ("git config " + "--glob" + "al user.name x", "deny", "deny"),
    ("git config user.email a@b", "pass", "deny"),
    ("git config --get user.name", "pass", "pass"),
    ("git reflog expire --all", "deny", "deny"),
    ("git reflog delete HEAD@{1}", "deny", "deny"),
    ("git gc --prune=now", "deny", "deny"),
    ("git prune", "deny", "deny"),
    ("git update-ref -d refs/heads/x", "deny", "deny"),
    ("git update-ref refs/heads/x HEAD", "pass", "deny"),
    ("git filter-branch --all", "deny", "deny"),
    ("git fetch origin", "pass", "pass"),
    ("git worktree add ../wt", "pass", "deny"),
    ("git worktree remove --force ../wt", "deny", "deny"),
    ("git worktree prune", "pass", "deny"),
    ("git mv a b", "pass", "deny"),
    ("git rm f", "pass", "deny"),
    ("git rm --cached f", "pass", "deny"),
    ("git checkout main", "pass", "deny"),
    ("git switch main", "pass", "deny"),
    ("git restore --staged f", "pass", "deny"),
    ("git reset --soft HEAD~1", "pass", "deny"),
    ("git reset --mixed HEAD", "pass", "deny"),
    ("git stash pop", "pass", "deny"),
    ("git stash drop", "deny", "deny"),
    ("git stash clear", "deny", "deny"),
    # Text that only names a write verb is admitted (nexus-5y47c); the same text beside a real write still denies in strict.
    ('echo "git reset --hard"', "pass", "pass"),
    ("git log --grep=reset", "pass", "pass"),
    ('echo "git reset --hard" && git add f', "pass", "deny"),
    ("git status", "pass", "pass"),
    ("git log", "pass", "pass"),
    ("git diff", "pass", "pass"),
    ("git show HEAD", "pass", "pass"),
    ("gh pr merge 5", "pass", "pass"),
    ("gh pr checkout 5", "pass", "deny"),
]
#: Rows the seatbelt denies only when the tree has something to lose; the plain fixture is clean, plain-dirty is not.
DIRTY_ROWS: list[str] = [
    "git checkout -- f", "git checkout -f main", "git restore f", "git reset --hard", "git reset --hard HEAD~1",
    "git stash", "git stash -u", "git rm -f f", "git clean -fd",
]
CREDENTIAL_ROWS = [("cat /proc/1234/environ", "deny"), ("ls -la", "pass")]
#: (command, what the output is in a fixture with no ``advice_block``): advice is additionalContext with no decision, none is
#: silence. A row blocks only where the project's or the user's ``advice_block`` lists its id.
ADVICE_ROWS = [("pkill -f pytest", "advice"), ("find / -name x.py", "advice"), ("ls -la", "none")]
#: The row ``PROJECT_SETTINGS`` and ``USER_SETTINGS`` switch to block, and the command that matches it.
BLOCKED_ROW_COMMAND = "find / -name x.py"
#: U2b: the destructive or other-party tools, auto-approved for every user (the hook says allow in every fixture).
AUTO_APPROVED_FOR_EVERYONE = (
    "memory_delete", "answer_plan_delete", "plan_delete", "memory_consolidate", "nx_tidy", "tuple_in", "mailbox_send",
)
#: U6b: the stop guard's CI wording, which only a project's ``ci_wait`` brings.
CI_TEXT = ("nx ci wait", "CI board", "develop and main")
