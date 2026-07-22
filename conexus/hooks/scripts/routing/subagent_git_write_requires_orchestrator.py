#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-184 Gap-4 mechanization (bead nexus-s88vq): deny ``git commit`` /
``git add`` from SUBAGENTS in the shared tree.

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

Fail-open (``fail_closed=False``): this is a hygiene guard for
cooperative subagents, not a security boundary — a crash or an
undeterminable worktree state must never wedge agent work.
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

#: git subcommands that WRITE the index/history and are orchestrator-only
#: in a shared tree. Deliberately narrow: status/diff/log/show/stash-list
#: etc. stay agent-usable everywhere.
_WRITE_SUBCOMMANDS = {"commit", "add"}

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


def _has_git_write(command: str) -> bool:
    segments = re.split(r"(?:&&|\|\||;|\s\|\s|\bthen\b|\bdo\b)", command)
    for segment in segments:
        try:
            tokens = shlex.split(segment, posix=True)
        except ValueError:
            continue
        if _git_subcommand(tokens) in _WRITE_SUBCOMMANDS:
            return True
    return False


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


def _deny_message(agent_type: str) -> str:
    return (
        f"Subagents never `git add`/`git commit` in the shared tree (you are "
        f"subagent `{agent_type or 'unknown'}`). Hand back your changes as "
        f"diffs + file paths via SendMessage; the ORCHESTRATOR commits, "
        f"pathspec-limited (RDR-184 Gap-4, feedback_orchestration_friction).\n"
        f"Worktree-isolated agents are exempt automatically (commits inside a "
        f"linked worktree are allowed).\n"
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
    if not _has_git_write(command):
        _lib.allow()

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
    if worktree is None or worktree is True:
        # Linked worktree (agent owns it) or undeterminable (fail open).
        _lib.allow()

    agent_type = str(payload.get("agent_type") or "")
    _lib.log_routing_event(
        rule=RULE_NAME, outcome="deny", tool_name="Bash",
        command_fragment=command,
    )
    _lib.deny(
        _deny_message(agent_type),
        summary="subagent git commit/add in the shared tree blocked: hand back diffs; orchestrator commits.",
    )


if __name__ == "__main__":
    _lib.run_hook(body, fail_closed=False, rule_name=RULE_NAME)
