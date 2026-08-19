#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-184 Gap-4 mechanization (nexus-s88vq, widened by nexus-ays2l): deny
index-writing AND working-tree-destroying git verbs from SUBAGENTS in the
shared tree.

Standing rule (``feedback_orchestration_friction_2026_07_15``): agents in
a shared tree NEVER ``git add``/``git commit`` — hand-back is diffs+paths;
the orchestrator commits pathspec-limited. The rule was prompt-enforced
only, and planner-186 committed in the shared tree anyway (20cd906e).

Mechanism: the PreToolUse payload carries ``agent_id`` IFF the call
originates from a subagent (documented hook schema; absent for the main
conversation). A subagent's Bash ``git commit``/``git add`` in the
PRIMARY checkout is denied with the hand-back protocol. Allowed:

- Main-conversation git writes (no ``agent_id``).
- Read-only git (status/diff/log/...) from anyone.
- Subagent commits inside a LINKED WORKTREE (``git rev-parse --git-dir``
  differs from ``--git-common-dir``): worktree-isolated agents own their
  tree and their local commits are the documented harvest choreography.
- A valid ``# routing-allow:`` escape (deliberate orchestrator-sanctioned
  exception, auditable in the routing log).

Fail mode is SPLIT by what is at stake (nexus-ays2l item 3, Hal ruling
2026-07-25):

- ``commit`` / ``add`` — hygiene. An undeterminable worktree state fails
  OPEN, because a flaky ``git rev-parse`` must never wedge agent work over
  tidiness. ``add`` mutates only the index and destroys nothing.
- ``checkout`` / ``restore`` / ``stash`` / ``clean`` / ``reset`` / ``rm`` —
  destruction. An undeterminable worktree state fails CLOSED. The failure
  mode here is silent, unrecoverable loss of the orchestrator's uncommitted
  work, and "I could not tell whether this tree is shared" is not a good
  enough reason to permit that. The ``# routing-allow:`` escape remains.

``run_hook(fail_closed=False)`` is unchanged: a crash in the hook ITSELF
still allows, since a broken guard must not brick every agent's Bash.
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(__file__))
import _lib  # noqa: E402

RULE_NAME = "subagent_git_write_requires_orchestrator"

#: Index/history writers. These are a HYGIENE concern: ``git add`` mutates
#: only the index and destroys nothing, ``git commit`` makes history the
#: orchestrator should own. Undeterminable state fails OPEN for these — a
#: crash must never wedge agent work over tidiness.
_INDEX_WRITE_SUBCOMMANDS = {"commit", "add"}

#: WORKING-TREE DESTROYERS (nexus-ays2l). These delete an orchestrator's
#: uncommitted work outright. The original guard covered only the set above,
#: which is strictly narrower than the set that can cause damage: it blocked
#: the harmless-but-untidy verbs and permitted the destructive ones.
#:
#: Damage signature that produced this bead (2026-07-24): three silent
#: reversions of one file over ~10 minutes with two subagents live, siblings
#: edited in the same window untouched, and NO stash entry and NO reflog entry
#: — exactly the trace ``git checkout -- <path>`` leaves and ``git stash``
#: does not.
#:
#: ``checkout`` is denied outright rather than only for pathspec forms: a
#: subagent switching HEAD in a SHARED tree moves the ground under the
#: orchestrator too. Read-only comparison has other spellings
#: (``git show <ref>:<path>``), which this guard never touches.
_TREE_DESTRUCTIVE_SUBCOMMANDS = {"checkout", "restore", "stash", "clean", "reset", "rm"}

_WRITE_SUBCOMMANDS = _INDEX_WRITE_SUBCOMMANDS | _TREE_DESTRUCTIVE_SUBCOMMANDS

#: Read-only spellings of otherwise-destructive verbs, allowlisted per the
#: bead's preference for allowlisting reads over blanket-denying the verb, so
#: a reviewer's ``git stash list`` keeps working.
_READ_ONLY_FORMS: dict[str, set[str]] = {
    "stash": {"list", "show"},
}

#: git global flags that take a VALUE argument before the subcommand
#: (``git -C path commit``); the value must be skipped when locating the
#: subcommand token.
_VALUED_GLOBAL_FLAGS = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"}


def _git_subcommand(tokens: list[str]) -> str | None:
    """Return the git subcommand of ``tokens`` (a shell segment), or None."""
    if not tokens or tokens[0] != "git":
        return None
    i = 1
    while i < len(tokens):
        tok = tokens[i]
        if tok in _VALUED_GLOBAL_FLAGS:
            i += 2
            continue
        if tok.startswith("-"):
            # value-carrying --flag=value or boolean global flag
            i += 1
            continue
        return tok
    return None


def _matched_write_subcommands(command: str) -> set[str]:
    """Write subcommands present in *command*, minus read-only spellings.

    Returns the SET rather than a bool (nexus-ays2l) because the caller has to
    distinguish index-hygiene verbs from working-tree destroyers: they get
    different fail modes when the worktree state is undeterminable.
    """
    matched: set[str] = set()
    segments = re.split(r"(?:&&|\|\||;|\s\|\s|\bthen\b|\bdo\b)", command)
    for segment in segments:
        try:
            candidates = [shlex.split(segment, posix=True)]
        except ValueError:
            # nexus-2e874: never silently skip a segment shlex rejects for
            # unbalanced quoting — that direction fully bypassed the guard
            # (a stray quote in any argument made a subagent `git stash`
            # invisible). Degrade to rough token variants; a match in any
            # variant counts.
            candidates = _lib.degraded_token_variants(segment)
        for tokens in candidates:
            sub = _git_subcommand(tokens)
            if sub not in _WRITE_SUBCOMMANDS:
                continue
            if sub in _READ_ONLY_FORMS and _read_only_form(tokens, sub):
                continue
            matched.add(sub)
    return matched


def _read_only_form(tokens: list[str], sub: str) -> bool:
    """True iff this invocation is a read-only spelling (``git stash list``).

    Only the token immediately following the subcommand is considered, and
    only an exact match against the allowlist counts — so ``git stash`` bare
    (which STASHES, destroying the tree) is never mistaken for a read.
    """
    try:
        idx = tokens.index(sub)
    except ValueError:
        return False
    for tok in tokens[idx + 1:]:
        if tok.startswith("-"):
            continue
        return tok in _READ_ONLY_FORMS[sub]
    return False  # bare `git stash` — destructive


def _in_linked_worktree(cwd: str) -> bool | None:
    """True iff ``cwd`` is inside a linked git worktree (not the primary
    checkout). ``None`` when undeterminable (not a repo, git missing,
    timeout) — the caller treats None as fail-open."""
    try:
        git_dir = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=cwd, capture_output=True, text=True, timeout=5,
        )
        common = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=cwd, capture_output=True, text=True, timeout=5,
        )
    except Exception:  # noqa: BLE001 — undeterminable: fail open
        return None
    if git_dir.returncode != 0 or common.returncode != 0:
        return None
    gd = os.path.realpath(os.path.join(cwd, git_dir.stdout.strip()))
    cd = os.path.realpath(os.path.join(cwd, common.stdout.strip()))
    return gd != cd


def _deny_message(agent_type: str, *, destructive: bool = False,
                  undeterminable: bool = False) -> str:
    who = f"you are subagent `{agent_type or 'unknown'}`"
    if destructive:
        head = (
            f"Subagents never run WORKING-TREE-DESTROYING git verbs in the shared "
            f"tree ({who}). checkout / restore / stash / clean / reset / rm delete "
            f"uncommitted work outright — including edits the orchestrator has in "
            f"flight and has not committed yet (nexus-ays2l)."
        )
        if undeterminable:
            head += (
                "\nThis tree's worktree state could not be determined, and these "
                "verbs FAIL CLOSED: an undeterminable tree is not a licence to "
                "destroy one."
            )
    else:
        head = (
            f"Subagents never `git add`/`git commit` in the shared tree ({who})."
        )
    return (
        f"{head}\n"
        f"Hand back your changes as diffs + file paths via SendMessage; the "
        f"ORCHESTRATOR commits, pathspec-limited (RDR-184 Gap-4, "
        f"feedback_orchestration_friction).\n"
        f"Read-only inspection is untouched: `git status`, `git diff`, `git log`, "
        f"`git show <ref>:<path>`, `git stash list`.\n"
        f"Worktree-isolated agents are exempt automatically (a linked worktree is "
        f"yours to destroy).\n"
        f"To override deliberately, append `# routing-allow: <reason>` "
        f"(>=8 chars)."
    )


def body(payload: dict[str, Any]) -> None:
    agent_id = str(payload.get("agent_id") or "")

    if not agent_id:
        _lib.allow()  # main conversation — the rule targets subagents only

    command = _lib.get_bash_command(payload)
    if not command:
        _lib.allow()
    matched = _matched_write_subcommands(command)
    if not matched:
        _lib.allow()
    destructive = matched & _TREE_DESTRUCTIVE_SUBCOMMANDS

    # Match FIRST, escape SECOND (the nexus-mzvwa.8 telemetry rule).
    if _lib.should_skip_for_reason(command):
        _lib.log_routing_event(
            rule=RULE_NAME, outcome="escape", tool_name="Bash",
            command_fragment=command,
            escape_reason=_lib.extract_escape_reason(command),
        )
        _lib.allow()

    cwd = str(payload.get("cwd") or "") or os.getcwd()
    worktree = _in_linked_worktree(cwd)
    if worktree is True:
        # Linked worktree: the agent owns its tree, including destroying it.
        _lib.allow()
    if worktree is None and not destructive:
        # Undeterminable worktree state. FAIL OPEN for the index-hygiene verbs:
        # a flaky `git rev-parse` must not wedge agent work over tidiness.
        _lib.allow()
    # worktree is False (primary checkout), OR undeterminable with a
    # working-tree destroyer in play — FAIL CLOSED (Hal ruling 2026-07-25,
    # nexus-ays2l item 3). The failure mode being guarded is silent
    # destruction of the orchestrator's uncommitted work; "I could not tell
    # whether this tree is shared" is not a good enough reason to permit that.
    # The `# routing-allow:` escape above remains for deliberate exceptions.

    agent_type = str(payload.get("agent_type") or "")
    _lib.log_routing_event(
        rule=RULE_NAME, outcome="deny", tool_name="Bash",
        command_fragment=command,
    )
    _lib.deny(
        _deny_message(agent_type, destructive=bool(destructive),
                      undeterminable=worktree is None),
        summary=(
            "subagent working-tree-destroying git verb blocked: it would delete the "
            "orchestrator's uncommitted work."
            if destructive else
            "subagent git commit/add in the shared tree blocked: hand back diffs; orchestrator commits."
        ),
    )


if __name__ == "__main__":
    _lib.run_hook(body, fail_closed=False, rule_name=RULE_NAME)
