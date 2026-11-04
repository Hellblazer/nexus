# SPDX-License-Identifier: AGPL-3.0-or-later
"""conexus/README.md's counts, and its core tool table, are derived from what ships (nexus-pplwc).

7.95.0's README said 44 skills (39 ship) and, two paragraphs apart, 46 and 42 core tools, with a tool table that
left out tools the server registers. Every number the README states is checked here against its source:

* skills: the ``conexus/skills/*/SKILL.md`` directories; agents: ``conexus/agents/*.md``;
* pipelines: ``conexus/registry.yaml`` ``pipelines``;
* tools: the live MCP servers' ``list_tools()``. An agent-facing tool is one the server lists that is not an internal
  ``hook_*`` tool (the plugin's own hooks call those; the README says so);
* operators: the ``operator_*`` functions ``nx_answer`` plans run, demoted from tools (nexus-ivi4s).

The core tool table must name every agent-facing tool and only registered ones. ``docs/mcp-servers.md`` states the
same registered count.
"""
from __future__ import annotations

import asyncio
import importlib
import inspect
import re
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.lint

_REPO = Path(__file__).resolve().parents[1]
_README = _REPO / "conexus" / "README.md"


def _registered(module: str) -> list[str]:
    return sorted(t.name for t in asyncio.run(importlib.import_module(module).mcp.list_tools()))


def _core_agent_tools() -> list[str]:
    return [t for t in _registered("nexus.mcp.core") if not t.startswith("hook_")]


def _claims(pattern: str) -> list[tuple[int, tuple[str, ...]]]:
    return [(n, m.groups()) for n, line in enumerate(_README.read_text(encoding="utf-8").splitlines(), 1)
            for m in re.finditer(pattern, line)]


def _check(claims: list[tuple[int, tuple[str, ...]]], truth: int, what: str, at_least: int = 1) -> None:
    assert len(claims) >= at_least, f"the README's {what} claims were not found: {claims}"
    wrong = [(n, int(g[0])) for n, g in claims if int(g[0]) != truth]
    assert not wrong, f"{truth} {what}; README says {wrong}"


def test_skill_counts_match_the_skill_directories() -> None:
    _check(_claims(r"\b(\d+) skills\b") + _claims(r"\*\*(\d+) skills\*\*"),
           len(list((_REPO / "conexus" / "skills").glob("*/SKILL.md"))), "skills", at_least=2)


def test_agent_counts_match_the_agent_files() -> None:
    _check(_claims(r"(?:^|\*\*)(\d+) agents\b"), len(list((_REPO / "conexus" / "agents").glob("*.md"))), "agents",
           at_least=2)


def test_pipeline_count_matches_the_registry() -> None:
    pipelines = yaml.safe_load((_REPO / "conexus" / "registry.yaml").read_text(encoding="utf-8"))["pipelines"]
    _check(_claims(r"\*\*(\d+) standard pipelines\*\*"), len(pipelines), "pipelines")


def _operators() -> list[str]:
    core = importlib.import_module("nexus.mcp.core")
    return [n.removeprefix("operator_") for n, _ in inspect.getmembers(core, inspect.iscoroutinefunction)
            if n.startswith("operator_")]


def test_operator_count_matches_the_operator_functions() -> None:
    _check(_claims(r"\b(\d+) (?:LLM-backed )?operators\b"), len(_operators()), "operators", at_least=2)


def test_core_tool_counts_match_the_registry() -> None:
    claims = (_claims(r"`nexus` \((\d+) tools") + _claims(r"\| (\d+) tools — see below")
              + _claims(r"MCP tool catalog \((\d+) tools\)"))
    _check(claims, len(_core_agent_tools()), "agent-facing core tools", at_least=3)


def test_the_server_totals_sentence_adds_up() -> None:
    core, catalog = len(_core_agent_tools()), len(_registered("nexus.mcp.catalog"))
    hooks = len([t for t in _registered("nexus.mcp.core") if t.startswith("hook_")])
    [(lineno, (c, k, total))] = _claims(
        r"core server exposes (\d+) MCP tools and the nexus-catalog server exposes (\d+) catalog tools, (\d+) tools")
    assert (int(c), int(k), int(total)) == (core, catalog, core + catalog), f"line {lineno}"
    _check(_claims(r"also registers (\d+) internal `hook_\*` tools"), hooks, "hook tools")
    _check(_claims(r"`nexus-catalog` \((\d+) catalog tools\)"), catalog, "catalog tools")


def _tool_table() -> str:
    return _README.read_text(encoding="utf-8").split("MCP tool catalog (", 1)[1].split("\n\n", 2)[1]


def test_the_core_tool_table_names_every_agent_facing_tool_and_only_those() -> None:
    named = set(re.findall(r"`([a-z_]+)`", _tool_table()))
    agent_facing = set(_core_agent_tools())
    assert len(named) > 30, named
    missing = sorted(agent_facing - named)
    assert not missing, f"registered but not in the README's tool table: {missing}"
    stale = sorted(named - agent_facing - set(_operators()))  # the operators row says they are not tools
    assert not stale, f"in the README's tool table but not an agent-facing tool: {stale}"


def test_docs_mcp_servers_states_the_registered_count() -> None:
    text = (_REPO / "docs" / "mcp-servers.md").read_text(encoding="utf-8")
    [(registered, agents, hooks)] = re.findall(
        r"## `nexus` — retrieval \+ storage \((\d+) registered tools: (\d+) for agents, (\d+) internal", text)
    every = _registered("nexus.mcp.core")
    assert (int(registered), int(agents), int(hooks)) == (len(every), len(_core_agent_tools()),
                                                         len(every) - len(_core_agent_tools()))
