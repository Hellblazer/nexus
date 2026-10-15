# SPDX-License-Identifier: AGPL-3.0-or-later
"""The nexus MCP server loads a small core of tools eagerly (nexus-fbtyl).

Claude Code reads ``_meta["anthropic/alwaysLoad"]`` on each tool of a
``tools/list`` response. A tool carrying ``True`` has its schema in the
first request; every other tool is found through tool search. Measured on
Claude Code 2.1.300 (T2 ``nexus/nexus-tools-eager-vs-deferred-ab-2026-10-14``):
that arrangement matched an all-eager server on tool choice and success with
a 41% smaller first request, and an all-deferred server lost the memory
tasks.

The server-level ``alwaysLoad`` key in ``conexus/.mcp.json`` must therefore
be ABSENT for ``nexus``. ``"alwaysLoad": false`` is not the neutral choice:
it overrides every per-tool flag and defers all 44 tools, which is the
losing arm of the test. ``true`` loads all 44 and defeats the point.
"""
from __future__ import annotations

import asyncio
import json
import pathlib

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.types import ListToolsResult

from nexus.mcp import catalog as _catalog
from nexus.mcp import core as _core

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
_META_KEY = "anthropic/alwaysLoad"

#: The eight tools sessions reach for first. Spelled out here a second time
#: on purpose: the pin must fail when the production constant drifts.
_EXPECTED_CORE = frozenset(
    {
        "search",
        "query",
        "memory_get",
        "memory_put",
        "memory_search",
        "scratch",
        "store_get",
        "store_put",
    }
)


def _wire_tools(server: FastMCP) -> list[dict]:
    """The tools exactly as a ``tools/list`` response serializes them."""
    result = ListToolsResult(tools=asyncio.run(server.list_tools()))
    return json.loads(result.model_dump_json(by_alias=True, exclude_none=True))["tools"]


def test_exactly_the_core_tools_carry_always_load_on_the_wire() -> None:
    tools = _wire_tools(_core.mcp)
    flagged = {t["name"] for t in tools if t.get("_meta", {}).get(_META_KEY) is True}
    assert flagged == _EXPECTED_CORE
    # Non-vacuity: the census saw the whole server, not just the core.
    assert len(tools) > len(_EXPECTED_CORE) * 2


def test_no_tool_carries_always_load_with_any_other_value() -> None:
    odd = {
        t["name"]: t["_meta"][_META_KEY]
        for t in _wire_tools(_core.mcp)
        if _META_KEY in t.get("_meta", {}) and t["_meta"][_META_KEY] is not True
    }
    assert odd == {}


def test_the_core_set_constant_matches_the_expected_set() -> None:
    assert frozenset(_core.ALWAYS_LOAD_TOOLS) == _EXPECTED_CORE


def test_the_catalog_server_flags_nothing() -> None:
    """nexus-catalog stays server-level ``alwaysLoad: false``: its tools load on use."""
    flagged = [
        t["name"] for t in _wire_tools(_catalog.mcp) if _META_KEY in t.get("_meta", {})
    ]
    assert flagged == []


def test_the_nexus_server_entry_has_no_always_load_key() -> None:
    """Absent, not false.

    A server-level ``alwaysLoad: false`` overrides every per-tool flag, so
    all tools defer and the eight above stop loading eagerly. ``true`` loads
    every tool and gives the saving back.
    """
    entry = json.loads((_REPO_ROOT / "conexus" / ".mcp.json").read_text())["nexus"]
    assert "alwaysLoad" not in entry


def test_flagging_a_tool_that_is_not_registered_fails_loud() -> None:
    with pytest.raises(RuntimeError, match="nope"):
        _core._apply_always_load(FastMCP("t"), frozenset({"nope"}))
