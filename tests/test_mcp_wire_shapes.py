# SPDX-License-Identifier: AGPL-3.0-or-later
"""Wire-shape audit for every ``@mcp.tool()`` registration (nexus-r90ao).

Root cause (T2 ``nexus/s4-structuredcontent-design-2026-08-22`` [23351],
found incidentally by nexus-6jlki Phase 1): ``mcp==1.27.1`` FastMCP
auto-detects structured output from a tool function's return annotation.
Any ``@mcp.tool()`` registration with a ``str``/``dict``/union return
annotation and NO explicit ``structured_output=`` kwarg silently emits
``structuredContent`` on the wire -- for most of these annotation shapes
(bare ``str``, a ``str | dict`` union, ``list[str]``, ``list[dict]``) that
means an accidental, wrongly-shaped ``{"result": <value>}`` wrap that
nobody designed and no client is documented to rely on
(``mcp/server/fastmcp/utilities/func_metadata.py::_try_create_model_and_schema``).
A bare, unparameterized ``-> dict`` annotation is a partial exception --
FastMCP's own model-building falls through to ``None`` for it today (no
schema, no wrap) -- but that is an accident of the CURRENT SDK's type
matching, not a guarantee; leaving it undeclared still lets a future
signature edit (``dict`` -> ``dict[str, Any]``) silently reintroduce a
wrap with no test noticing. This file makes every tool's disposition an
explicit source-level fact instead of a return-annotation accident.

nexus-6jlki's ``search()`` is the one tool with a DELIBERATE, non-empty
structuredContent shape (a ``CallToolResult`` the tool body constructs by
hand, wire wrapper at ``structured_output=False`` to suppress
auto-detection so the union return annotation is never auto-wrapped
either). Every other tool in this file's census keeps
``structured_output=False`` and emits no structuredContent at all -- pure
text over the wire, byte-identical to before this bead.

Two layers:

1. Registration census (``pytest.mark.lint``, pure AST, no substrate) --
   every ``@mcp.tool(...)`` call site in ``src/nexus/mcp/{core,catalog}.py``
   must carry an explicit ``structured_output=`` keyword.
2. Call-through spot-checks (default loop) -- for a representative subset
   (search, query, memory_search, store_get_many, scratch), actually
   invoke the tool through FastMCP's own ``ToolManager``/``convert_result``
   machinery (``mcp.call_tool(name, arguments)``, the same path a real
   ``tools/call`` request drives) and assert the wire-level
   structuredContent shape: present + deliberate for ``search``, entirely
   absent for the rest.
"""
from __future__ import annotations

import ast
import asyncio
import os
import pathlib
from unittest.mock import MagicMock, patch

import pytest
from mcp.types import CallToolResult

from nexus.mcp.core import mcp as _mcp_instance
from nexus.mcp.core import memory_put
from nexus.types import SearchResult

# Reuse this suite's proven, substrate-free fixture/helper set rather than
# reinventing T1/T2/T3 injection -- these are the exact fixtures
# tests/test_mcp_server.py's own tool tests already run under
# NX_TEST_T2_SUBSTRATE=none. Importing a `@pytest.fixture` (including an
# autouse one) into this module's namespace makes pytest discover it for
# THIS module's collection too -- standard cross-file fixture reuse, no
# conftest.py change needed.
from tests.test_mcp_server import (  # noqa: F401 -- t1/t2_path/_patch_t2/_reset used as fixtures
    _HYBRID_DEFAULT_ON_CFG,
    _mock_t3,
    _patch_t2,
    _reset,
    t1,
    t2_path,
)

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
_MCP_SOURCE_FILES = [
    _REPO_ROOT / "src" / "nexus" / "mcp" / "core.py",
    _REPO_ROOT / "src" / "nexus" / "mcp" / "catalog.py",
]


# ── Layer 1: registration census (lint, AST-only, no substrate) ────────────

def _iter_tool_decorator_calls(path: pathlib.Path):
    """Yield (function_name, decorator_call_node) for every ``@mcp.tool(...)``
    registration in ``path``. Skips bare ``@mcp.tool`` (no-parens) forms --
    none exist in this codebase today (verified: every registration takes at
    least a ``title=``), and a bare form can never carry ``structured_output=``
    so it would trivially fail the census below by design, not by mistake.
    """
    tree = ast.parse(path.read_text(), filename=str(path))
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for deco in node.decorator_list:
            if (
                isinstance(deco, ast.Call)
                and isinstance(deco.func, ast.Attribute)
                and deco.func.attr == "tool"
                and isinstance(deco.func.value, ast.Name)
                and deco.func.value.id == "mcp"
            ):
                yield node.name, deco


@pytest.mark.lint
def test_every_mcp_tool_registration_has_explicit_structured_output():
    """nexus-r90ao registration census: no ``@mcp.tool()`` may rely on
    FastMCP's implicit auto-detect. A future tool that omits
    ``structured_output=`` fails THIS test, not silently ships an
    accidental wire shape.
    """
    implicit: list[str] = []
    total = 0
    for path in _MCP_SOURCE_FILES:
        assert path.is_file(), f"expected MCP tool source file at {path}"
        for func_name, deco in _iter_tool_decorator_calls(path):
            total += 1
            has_explicit = any(kw.arg == "structured_output" for kw in deco.keywords)
            if not has_explicit:
                implicit.append(f"{path.name}:{func_name}")

    # Non-vacuity (nexus-moht0 doctrine): a census that silently found
    # nothing to check is a failure, not a pass. 46 tools existed at the
    # time this test was written (36 core.py + 10 catalog.py); floor set
    # comfortably below that so an unrelated future deletion doesn't
    # spuriously red this test, while a near-total collection failure
    # (e.g. a decorator-shape change this AST walk stops recognizing)
    # still gets caught.
    assert total >= 40, (
        f"expected >=40 registered @mcp.tool() functions across "
        f"{[p.name for p in _MCP_SOURCE_FILES]}, found {total} -- "
        "census may be broken (decorator shape changed?) rather than "
        "the tool count actually having dropped"
    )
    assert implicit == [], (
        "@mcp.tool() registration(s) with no explicit structured_output= "
        f"(silently inheriting FastMCP's auto-detect): {implicit}"
    )


# ── Layer 2: call-through spot-checks (default loop, mocked substrate) ─────

async def _call_tool(name: str, **kwargs):
    """Invoke a registered tool through FastMCP's real ToolManager/
    convert_result path -- the same conversion a live ``tools/call``
    request drives -- without a live transport or session.
    """
    return await _mcp_instance.call_tool(name, kwargs)


@pytest.mark.skipif(
    os.environ.get("NX_TEST_T2_SUBSTRATE") == "none",
    reason=(
        "search()'s wire wrapper exercises a T2/service-endpoint-resolving "
        "path unrelated to this bead's scope -- confirmed (2026-08-22) that "
        "nexus-6jlki's OWN direct-call equivalent, "
        "test_mcp_server.py::test_search_default_mode_returns_calltoolresult_"
        "with_structured_content, fails identically under "
        "NX_TEST_T2_SUBSTRATE=none with the same ServiceEndpointUnresolvableError "
        "(not something introduced or fixable by nexus-r90ao). Run this test "
        "with a built service jar / real engine substrate (drop "
        "NX_TEST_T2_SUBSTRATE=none) to exercise it."
    ),
)
def test_search_wire_call_has_deliberate_structured_content():
    """search(): the one tool with an intentional, non-empty
    structuredContent shape (nexus-6jlki)."""
    _mock_t3([{"name": "code__test", "count": 1}])
    with patch(
        "nexus.search_engine.search_cross_corpus",
        lambda *a, **kw: [
            SearchResult(
                id="r1", content="vector database internals", distance=0.1234,
                collection="code__test",
                metadata={"tumbler": "1.1", "chunk_text_hash": "ab" * 32},
            )
        ],
    ), patch("nexus.config.load_config", return_value=_HYBRID_DEFAULT_ON_CFG):
        result = asyncio.run(_call_tool("search", query="vector database", corpus="code__test"))

    assert isinstance(result, CallToolResult)
    assert result.structuredContent is not None
    data = result.structuredContent
    assert data["ids"] == ["r1"]
    assert data["tumblers"] == ["1.1"]
    assert data["collections"] == ["code__test"]


def test_query_wire_call_has_no_structured_content():
    """query(): str | dict return, structured_output=False -- no
    structuredContent at all over the wire (out of scope for the
    nexus-6jlki dual-shape treatment; this bead only kills the accident)."""
    _mock_t3([{"name": "code__test", "count": 1}])
    with patch(
        "nexus.search_engine.search_cross_corpus",
        lambda *a, **kw: [
            SearchResult(id="r1", content="small", distance=0.1,
                         collection="code__test", metadata={"title": "solo"})
        ],
    ), patch("nexus.config.load_config", return_value=_HYBRID_DEFAULT_ON_CFG):
        result = asyncio.run(_call_tool("query", question="small", corpus="code__test"))

    assert not isinstance(result, CallToolResult)
    assert not isinstance(result, tuple), (
        "convert_result returns (content, structuredContent) only when "
        "output_schema is set -- a tuple here means the accidental wrap "
        "is back"
    )


def test_memory_search_wire_call_has_no_structured_content(t2_path):
    memory_put(content="chromadb vector embeddings", project="testproj", title="vectors.md")
    result = asyncio.run(_call_tool("memory_search", query="chromadb"))

    assert not isinstance(result, CallToolResult)
    assert not isinstance(result, tuple)


def test_store_get_many_wire_call_has_no_structured_content():
    class _FakeCollectionStub:
        def get(self, ids=None, **kwargs):
            ids = list(ids or [])
            return {"ids": ids, "documents": ["hello"] * len(ids), "metadatas": [{}] * len(ids)}

    mock_t3 = MagicMock()
    mock_t3.get_or_create_collection = lambda name: _FakeCollectionStub()

    with patch("nexus.mcp.core._get_t3", return_value=mock_t3):
        result = asyncio.run(_call_tool("store_get_many", ids="id1", collections="knowledge"))

    assert not isinstance(result, CallToolResult)
    assert not isinstance(result, tuple)


def test_scratch_wire_call_has_no_structured_content(t1):
    result = asyncio.run(_call_tool("scratch", action="put", content="scratch note"))

    assert not isinstance(result, CallToolResult)
    assert not isinstance(result, tuple)
