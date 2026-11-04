# SPDX-License-Identifier: AGPL-3.0-or-later
"""conexus/README.md's skill and tool counts, and its core tool table, are derived from what ships (nexus-pplwc).

7.95.0's README said 44 skills (39 ship) and, two paragraphs apart, 46 and 42 core tools, with a tool table that
left out tools the server registers. The counts here come from the skill directories and the live MCP registries:
an agent-facing tool is one the server registers that is not an internal ``hook_*`` tool (the plugin's own hooks
call those; the README says so).
"""
from __future__ import annotations

import importlib
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.lint

_REPO = Path(__file__).resolve().parents[1]
_README = _REPO / "conexus" / "README.md"


def _registered(module: str) -> list[str]:
    return sorted(importlib.import_module(module).mcp._tool_manager._tools)


def _core_agent_tools() -> list[str]:
    return [t for t in _registered("nexus.mcp.core") if not t.startswith("hook_")]


def _skill_count() -> int:
    return len(list((_REPO / "conexus" / "skills").glob("*/SKILL.md")))


def _claims(pattern: str) -> list[tuple[int, tuple[str, ...]]]:
    out = []
    for lineno, line in enumerate(_README.read_text(encoding="utf-8").splitlines(), 1):
        for match in re.finditer(pattern, line):
            out.append((lineno, match.groups()))
    return out


def test_skill_counts_match_the_skill_directories() -> None:
    claims = _claims(r"\b(\d+) skills\b") + _claims(r"\*\*(\d+) skills\*\*")
    assert len(claims) >= 2, claims
    wrong = [(ln, int(g[0])) for ln, g in claims if int(g[0]) != _skill_count()]
    assert not wrong, f"{_skill_count()} skills ship; README says {wrong}"


def test_core_tool_counts_match_the_registry() -> None:
    core = len(_core_agent_tools())
    claims = (_claims(r"`nexus` \((\d+) tools") + _claims(r"\| (\d+) tools — see below")
              + _claims(r"MCP tool catalog \((\d+) tools\)"))
    assert len(claims) >= 3, claims
    wrong = [(ln, int(g[0])) for ln, g in claims if int(g[0]) != core]
    assert not wrong, f"the core server has {core} agent-facing tools; README says {wrong}"


def test_the_server_totals_sentence_adds_up() -> None:
    core, catalog = len(_core_agent_tools()), len(_registered("nexus.mcp.catalog"))
    hooks = len([t for t in _registered("nexus.mcp.core") if t.startswith("hook_")])
    [(lineno, (c, k, total))] = _claims(
        r"core server exposes (\d+) MCP tools and the nexus-catalog server exposes (\d+) catalog tools, (\d+) tools")
    assert (int(c), int(k), int(total)) == (core, catalog, core + catalog), f"line {lineno}"
    [(lineno, (h,))] = _claims(r"also registers (\d+) internal `hook_\*` tools")
    assert int(h) == hooks, f"line {lineno}"


def test_the_core_tool_table_names_every_agent_facing_tool() -> None:
    text = _README.read_text(encoding="utf-8")
    table = text.split("MCP tool catalog (", 1)[1].split("\n\n", 2)[1]
    named = set(re.findall(r"`([a-z_]+)`", table))
    missing = sorted(set(_core_agent_tools()) - named)
    assert not missing, f"registered but not in the README's tool table: {missing}"
