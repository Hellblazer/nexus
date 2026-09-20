# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every handler hooks.json names must exist (RDR-215, nexus-q02nx.21).

Bead .21 re-declared 21 of 25 entries, and the majority of the file now
identifies its handler by a BARE STRING -- an `mcp_tool` name, or a verb
in `args` after `nx-hook`. A string that resolves to nothing is not an
error at runtime: Claude Code treats an unavailable tool as non-blocking,
so a typo'd or retired handler makes the hook a silent no-op.

The path-shaped version of this was already pinned (a declared script
must exist on disk, tests/test_plugin_structure.py). The tool-shaped and
verb-shaped versions had nothing, which is the same defect class one tier
up -- seven `hook_*` names appeared in no hooks.json-reading test at all,
including `hook_agent_dispatch_expect`, the writer of the RDR-184 EXPECT
row that AGENTS.md names as the thing whose absence masks an undeclared
start.

Mutation M1 in ``mutations_qc4p1.sh`` used to falsify the registration of
that entry and was retired during this bead as "analogue-free". It was
not analogue-free, only harder: the invariant survived as an `mcp_tool`
entry and is perfectly mutable. This file is what replaces it.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from nexus._hook_runtime.entry import VERB_TABLE
from nexus.mcp.hooks import HOOK_TOOLS

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOKS_JSON = REPO_ROOT / "conexus" / "hooks" / "hooks.json"

#: Measured on the tree that introduced this gate. A DROP means either
#: entries were removed or the extractor stopped recognising how they are
#: declared, and the second reading is the one to rule out first -- this
#: bead shipped four separate gates that went quiet exactly that way.
#:
#: 13 -> 9 at bead nexus-17i1n, which moved the three verdict-returning
#: hooks to the command tier in four entries (auto-approve is wired
#: twice, on PreToolUse and on PermissionRequest). An `mcp_tool` hook
#: cannot return a verdict, so all three shipped inert in conexus 7.55.0.
#: This floor moves DOWN for the first time, which is exactly the reading
#: the paragraph above says to rule out -- so, explicitly: a deliberate
#: migration off the tier, not an extractor going blind.
#: nexus.mcp.hooks.DECIDING_HOOKS names the three, and
#: tests/test_deciding_hooks_are_command_tier.py refuses a hooks.json
#: that puts any of them back.
_MIN_MCP_TOOL_ENTRIES = 9
#: 3 -> 6 at bead nexus-q02nx.22, which converted the last four shell-form
#: entries (`nx upgrade --auto ... || echo ...`, `nx self gc ... || true`,
#: `nx hook session-start`, `nx-session-end-launcher`) to exec form. Three of
#: those became nx-hook verbs; measured after: upgrade-auto, preflight,
#: self-gc, session-start, session-context, rdr.
#:
#: 6 -> 10 at bead nexus-17i1n: the four entries the line above moved off
#: the tool tier arrive here.
_MIN_NX_HOOK_ENTRIES = 10


def _declared() -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """``(mcp_tool names, nx-hook verbs)``, each with its event."""
    data = json.loads(HOOKS_JSON.read_text())
    events = data.get("hooks", data)
    tools: list[tuple[str, str]] = []
    verbs: list[tuple[str, str]] = []
    for event, entries in events.items():
        for entry in entries:
            for sub in (entry.get("hooks", [entry]) if isinstance(entry, dict) else []):
                if not isinstance(sub, dict):
                    continue
                if sub.get("type") == "mcp_tool":
                    name = sub.get("tool_name") or sub.get("tool") or sub.get("name")
                    if name:
                        tools.append((event, str(name)))
                elif sub.get("command") == "nx-hook":
                    args = [a for a in sub.get("args", []) if isinstance(a, str)]
                    if args:
                        verbs.append((event, args[0]))
    return tools, verbs


def test_the_extraction_is_not_vacuous() -> None:
    tools, verbs = _declared()
    assert len(tools) >= _MIN_MCP_TOOL_ENTRIES, (
        f"{len(tools)} mcp_tool entries found, expected >= "
        f"{_MIN_MCP_TOOL_ENTRIES}. Rule out the extractor going blind "
        f"before concluding entries were removed."
    )
    assert len(verbs) >= _MIN_NX_HOOK_ENTRIES, (
        f"{len(verbs)} nx-hook entries found, expected >= "
        f"{_MIN_NX_HOOK_ENTRIES}. Same reading order."
    )


@pytest.mark.parametrize(
    "event,tool_name",
    _declared()[0],
    ids=[f"{e}:{n}" for e, n in _declared()[0]],
)
def test_every_declared_mcp_tool_is_registered(event: str, tool_name: str) -> None:
    # register_hook_tools() publishes each spec as ``hook_<name>``; the
    # spec itself carries the unprefixed name.
    registered = {f"hook_{spec.name}" for spec in HOOK_TOOLS}
    assert tool_name in registered, (
        f"hooks.json [{event}] dispatches the mcp_tool {tool_name!r}, which "
        f"nexus.mcp.hooks.HOOK_TOOLS does not register. Claude Code treats "
        f"an unavailable tool as a non-blocking error, so this hook is a "
        f"silent no-op. Registered: {sorted(registered)}"
    )


@pytest.mark.parametrize(
    "event,verb",
    _declared()[1],
    ids=[f"{e}:{v}" for e, v in _declared()[1]],
)
def test_every_declared_nx_hook_verb_resolves(event: str, verb: str) -> None:
    assert verb in VERB_TABLE, (
        f"hooks.json [{event}] runs `nx-hook {verb}`, which is not in "
        f"nexus._hook_runtime.entry.VERB_TABLE. Known verbs: "
        f"{sorted(VERB_TABLE)}"
    )


README = REPO_ROOT / "conexus" / "README.md"


def _readme_handler_cells() -> list[str]:
    """The Handler column of every row in the README's hook table."""
    cells = []
    for line in README.read_text().splitlines():
        if not line.startswith("| `"):
            continue
        # Split on unescaped pipes only. An Event cell can legitimately
        # contain one -- `PreToolUse` (`Agent\|Task`) -- and splitting
        # naively shifts every later column, which made this check report
        # a missing row for hook_agent_dispatch_expect that was there all
        # along. The checker's own parsing, again.
        parts = [
            c.strip().replace("\\|", "|")
            for c in re.split(r"(?<!\\)\|", line.strip().strip("|"))
        ]
        if len(parts) >= 2:
            cells.append(parts[1])
    return cells


def _declared_handlers() -> list[tuple[str, str]]:
    """``(event, handler)`` for every hooks.json entry, in its own spelling."""
    data = json.loads(HOOKS_JSON.read_text())
    out = []
    for event, entries in data.get("hooks", data).items():
        for entry in entries:
            for sub in (entry.get("hooks", [entry]) if isinstance(entry, dict) else []):
                if not isinstance(sub, dict):
                    continue
                if sub.get("type") == "mcp_tool":
                    name = sub.get("tool_name") or sub.get("tool") or sub.get("name")
                    out.append((event, str(name)))
                else:
                    cmd = sub.get("command", "")
                    args = [a for a in sub.get("args", []) if isinstance(a, str)]
                    out.append((event, " ".join([cmd, *args]).strip()))
    return out


def _mentions(handler: str, cells: list[str]) -> bool:
    """Is *handler* named in some Handler cell, allowing for shortening?"""
    if handler.startswith("hook_"):
        return any(handler in c for c in cells)
    # A command line: the table gives the readable core, not the full
    # shell with its redirections and fallbacks.
    core = handler.split(" 2>")[0].split(" >")[0].strip()
    if core.startswith("python3 "):
        core = core.split("/")[-1]
    return any(core in c for c in cells)


@pytest.mark.parametrize(
    "event,handler",
    _declared_handlers(),
    ids=[f"{e}:{h[:40]}" for e, h in _declared_handlers()],
)
def test_the_readme_hook_table_names_every_declared_handler(
    event: str, handler: str
) -> None:
    """The table is documentation people act on, and it drifted silently.

    Bead .21 corrected three rows by hand and the commit said the table
    now matched hooks.json. It did not: two rows still named a ported
    script, and four entries had no row at all. Hand-checking a table
    against a JSON file is the kind of claim that should not be made by
    hand twice.
    """
    cells = _readme_handler_cells()
    assert cells, "no hook table rows found in conexus/README.md"
    assert _mentions(handler, cells), (
        f"hooks.json declares [{event}] {handler!r}, which no row of the "
        f"README's hook table names. Handlers listed: {cells}"
    )
