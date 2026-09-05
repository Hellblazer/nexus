# SPDX-License-Identifier: AGPL-3.0-or-later
"""Linked-worktree detection for the sn hooks.

Serena's MCP server resolves every path against the project root it found
at startup (``--project-from-cwd``, read once in ``serena.cli``). Claude Code
subagents share the parent's MCP connection, so a subagent dispatched with
``isolation: "worktree"`` sends its edits to that shared server, and the
server writes them into the PRIMARY checkout while the subagent believes it
is working in its worktree (T3 "Serena MCP write tools escape git-worktree
isolation", 2026-08-09; T2 nexus/incident-serena-replace-in-files-wrong-
checkout-worktree-agent, 2026-09-05). Three incidents; the tool reported
success each time.

Both hooks receive the caller's ``cwd`` on stdin. A LINKED worktree (one
made by ``git worktree add``) has a ``.git`` FILE holding ``gitdir: <path>``
that points under some other repository's ``.git/worktrees/``; the primary
checkout has a ``.git`` DIRECTORY. That distinction is the whole test.

Stdlib only: hooks run under system python with no conexus installed.
"""
from __future__ import annotations

import json
import pathlib

SERENA_PREFIX = "mcp__plugin_sn_serena__"

# Every Serena tool that writes to the project through the server's root.
# Memory tools are included: ``.serena/memories`` is also under that root.
# Read/navigation tools stay allowed; they return locations in the wrong
# tree, but that is visible and harmless. Kept in sync with serena-tools.txt
# by tests/test_sn_plugin.py.
SERENA_WRITE_TOOLS = frozenset({
    "delete_lines",
    "delete_memory",
    "edit_memory",
    "insert_after_symbol",
    "insert_at_line",
    "insert_before_symbol",
    "jet_brains_inline_symbol",
    "jet_brains_move",
    "jet_brains_rename",
    "jet_brains_safe_delete",
    "rename_memory",
    "rename_symbol",
    "replace_content",
    "replace_in_files",
    "replace_lines",
    "replace_symbol_body",
    "safe_delete_symbol",
    "write_memory",
})


def cwd_from_payload(payload: str) -> str:
    """The ``cwd`` field of a hook stdin payload, or '' when absent/unparseable."""
    try:
        data = json.loads(payload)
    except (ValueError, TypeError):
        return ""
    if not isinstance(data, dict):
        return ""
    cwd = data.get("cwd", "")
    return cwd if isinstance(cwd, str) else ""


def is_linked_worktree(cwd: str | pathlib.Path) -> bool:
    """True when *cwd* (or an ancestor, up to the nearest ``.git``) is a linked git worktree.

    Walks up from *cwd* to the first entry named ``.git``; a directory means
    the primary checkout (or a bare-attached primary), a file whose content
    starts with ``gitdir:`` and names a ``.git/worktrees/`` path means a
    linked worktree. Submodules also carry a ``.git`` file, but their gitdir
    points under ``.git/modules/``, so they are NOT reported as worktrees.
    """
    if not cwd:
        return False
    here = pathlib.Path(cwd)
    for candidate in (here, *here.parents):
        dot_git = candidate / ".git"
        if dot_git.is_dir():
            return False
        if dot_git.is_file():
            try:
                head = dot_git.read_text(errors="replace").strip()
            except OSError:
                return False
            if not head.startswith("gitdir:"):
                return False
            target = head[len("gitdir:"):].strip().replace("\\", "/")
            return "/.git/worktrees/" in target or target.endswith("/.git/worktrees")
    return False


def is_serena_write_tool(tool_name: str) -> bool:
    return tool_name.startswith(SERENA_PREFIX) and tool_name[len(SERENA_PREFIX):] in SERENA_WRITE_TOOLS


def deny_reason(tool_name: str, cwd: str) -> str:
    short = tool_name[len(SERENA_PREFIX):] if tool_name.startswith(SERENA_PREFIX) else tool_name
    return (
        f"sn worktree guard: {short} refused. This agent's cwd ({cwd}) is a linked git worktree, "
        "but the Serena MCP server writes to the project root it resolved at startup, which is the "
        "shared primary checkout, not this worktree. Use Edit/Write/Bash with absolute paths under "
        "the worktree, and the built-in LSP tool for navigation. Serena read tools remain available."
    )
