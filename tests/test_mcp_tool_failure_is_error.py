# SPDX-License-Identifier: AGPL-3.0-or-later
"""A failing MCP tool answers ``isError: true`` with its text unchanged (nexus-6b6hq).

Sam's ruling of 2026-10-24: every failure reaches an MCP client as
``isError``, the text byte-identical, and a list tool's ``[]`` means only
"no results". Failure sites mark their return with ``_tool_failure.fail``;
the registration-boundary patch in ``_sdk_patches`` converts the mark. The
module-level tool names keep returning today's plain-looking shapes, which
the in-process callers and the older tests rely on.
"""
from __future__ import annotations

import ast
import asyncio
import importlib
import inspect
import pathlib
import threading

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.tools.base import Tool
from mcp.types import CallToolResult

from nexus.mcp import _sdk_patches, catalog, core
from nexus.mcp._tool_failure import (
    FailureDict,
    FailureList,
    FailureText,
    fail,
    is_exempt,
    is_failure,
)
from tests.test_mcp_sync_tool_offload import RELEASE_TIMEOUT

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
_MCP_SOURCES = [
    _REPO_ROOT / "src" / "nexus" / "mcp" / "core.py",
    _REPO_ROOT / "src" / "nexus" / "mcp" / "catalog.py",
]


def _call(server, name: str, arguments: dict):
    return asyncio.run(server.call_tool(name, arguments))


#: One per failure shape the tools use, extra keys included.
_FAILURES: dict[str, object] = {
    "text_failure": "Error: the service is not answering",
    "dict_failure": {"error": "Not found: 1.1.9"},
    "multi_key_failure": {"error": "Would remove 12 links", "would_remove": 12},
    "list_failure": [{"error": "query or at least one filter required"}],
    "list_text_failure": ["Error: unknown tumbler"],
}


def _returning(value):
    def body() -> object:
        return value

    return body


def _server():
    """A fresh FastMCP whose tools register through the patched boundary."""
    status = _sdk_patches._patch_tool_failure_is_error()
    assert status in {"applied", "already applied"}, status
    server = FastMCP("failure-test")

    for name, value in _FAILURES.items():
        # Each failure is registered twice: marked, and as the plain value a
        # tool returned before nexus-6b6hq. The two must send the same content.
        server.add_tool(_returning(fail(value)), name=name, structured_output=False)
        server.add_tool(_returning(value), name=f"plain_{name}", structured_output=False)

    @server.tool(structured_output=False)
    def echoes_content() -> str:
        # scratch get returns a note verbatim; a note may begin "Error:".
        return "Error: this is a stored note, not a failure"

    @server.tool(structured_output=False)
    def empty_list() -> list:
        return []

    @server.tool(structured_output=False)
    async def async_failure() -> str:
        return fail("Error: async body")

    @server.tool(structured_output=False)
    def nx_answer() -> str:
        return fail("Error: scope unknown")

    @server.tool(structured_output=False)
    def hook_example() -> str:
        return fail("Error: kept as a success result")

    return server


@pytest.fixture
def _restore_from_function():
    saved = Tool.__dict__.get("from_function")
    yield
    if saved is not None:
        Tool.from_function = saved  # type: ignore[assignment]


@pytest.mark.parametrize("tool", sorted(_FAILURES))
def test_a_marked_failure_sends_the_old_content_flagged_as_an_error(
    _restore_from_function, tool: str,
) -> None:
    server = _server()
    result = _call(server, tool, {})
    plain = _call(server, f"plain_{tool}", {})
    assert isinstance(result, CallToolResult), result
    assert result.isError is True
    assert not isinstance(plain, CallToolResult)
    assert [c.model_dump() for c in result.content] == [c.model_dump() for c in plain]
    assert all(c.text for c in result.content)


def test_an_async_failure_is_an_error_result(_restore_from_function) -> None:
    result = _call(_server(), "async_failure", {})
    assert isinstance(result, CallToolResult) and result.isError is True
    assert [c.text for c in result.content] == ["Error: async body"]


def test_nx_answer_failures_convert_like_any_other(_restore_from_function) -> None:
    """Only its never-marked ``[non-answer]`` text is a success contract."""
    result = _call(_server(), "nx_answer", {})
    assert isinstance(result, CallToolResult) and result.isError is True
    assert [c.text for c in result.content] == ["Error: scope unknown"]


def test_an_unmarked_error_looking_text_stays_a_success(_restore_from_function) -> None:
    result = _call(_server(), "echoes_content", {})
    assert not isinstance(result, CallToolResult)
    assert [c.text for c in result] == ["Error: this is a stored note, not a failure"]


def test_an_empty_list_stays_a_success(_restore_from_function) -> None:
    assert not isinstance(_call(_server(), "empty_list", {}), CallToolResult)


def test_hook_tools_keep_their_failure_text_as_a_success(_restore_from_function) -> None:
    result = _call(_server(), "hook_example", {})
    assert not isinstance(result, CallToolResult)
    assert [c.text for c in result] == ["Error: kept as a success result"]


def test_a_wrapped_sync_failure_still_runs_off_the_loop(_restore_from_function) -> None:
    """The failure wrapper must keep a sync body sync, or the offload skips it.

    Same race as ``test_mcp_sync_tool_offload``: the slow body waits on an
    Event only the cheap body sets, so a body left on the loop times out.
    """
    assert _sdk_patches._patch_sync_tool_offload() in {"applied", "already applied"}
    assert _sdk_patches._patch_tool_failure_is_error() in {"applied", "already applied"}
    released = threading.Event()
    server = FastMCP("failure-offload-test")

    @server.tool(structured_output=False)
    def slow_failure() -> str:
        return fail("Error: released" if released.wait(RELEASE_TIMEOUT) else "Error: timed-out")

    @server.tool(structured_output=False)
    def fast() -> str:
        released.set()
        return "fast"

    async def race() -> CallToolResult:
        slow = asyncio.create_task(server.call_tool("slow_failure", {}))
        await asyncio.sleep(0)
        await server.call_tool("fast", {})
        return await slow

    result = asyncio.run(race())
    assert isinstance(result, CallToolResult) and result.isError is True
    assert [c.text for c in result.content] == ["Error: released"]


def test_the_patch_is_idempotent(_restore_from_function) -> None:
    _sdk_patches._patch_tool_failure_is_error()
    assert _sdk_patches._patch_tool_failure_is_error() == "already applied"


def test_apply_sdk_patches_reports_it() -> None:
    assert core._SDK_PATCH_RESULTS["tool_failure_is_error"] == "applied"


def test_a_marked_value_equals_the_plain_value() -> None:
    """In-process callers and the older tests compare against plain shapes."""
    text = fail("Error: x")
    assert text == "Error: x" and isinstance(text, str) and text.startswith("Error:")
    assert fail({"error": "x"}) == {"error": "x"}
    assert fail([{"error": "x"}]) == [{"error": "x"}]
    assert isinstance(text, FailureText)
    assert isinstance(fail({"error": "x"}), FailureDict)
    assert isinstance(fail([]), FailureList)


def test_plain_values_are_not_failures() -> None:
    assert not is_failure("Error: x")
    assert not is_failure({"error": "x"})
    assert not is_failure([{"error": "x"}])
    assert is_failure(fail("Error: x"))


def _failure_wrapped(fn) -> bool:
    while fn is not None:
        if getattr(fn, "_nx_failure_wrapped", False):
            return True
        fn = getattr(fn, "__wrapped__", None)
    return False


@pytest.mark.parametrize("module", ["nexus.mcp.core", "nexus.mcp.catalog"])
def test_every_registered_tool_converts_failures(module: str) -> None:
    """Production registration, both servers: nothing bypasses the boundary."""
    server = importlib.import_module(module).mcp
    tools = list(server._tool_manager.list_tools())
    assert tools, f"no tools registered on {module}.mcp -- test is checking nothing"
    missed = [t.name for t in tools if not is_exempt(t.name) and not _failure_wrapped(t.fn)]
    # With an output schema, convert_result validates a returned
    # CallToolResult's structuredContent, so a failure would surface as
    # "Error executing tool" instead of passing through.
    schema = [t.name for t in tools if t.fn_metadata.output_schema is not None]
    assert not schema, f"tools with an output schema break the isError passthrough: {schema}"
    assert not missed, f"tools on {module}.mcp that would answer a failure as a success: {missed}"
    exempt_wrapped = [t.name for t in tools if is_exempt(t.name) and _failure_wrapped(t.fn)]
    assert not exempt_wrapped, exempt_wrapped


def test_a_real_core_tool_fails_as_an_error_result() -> None:
    """store_get rejects an empty doc_id before touching any substrate."""
    result = _call(core.mcp, "store_get", {"doc_id": ""})
    assert isinstance(result, CallToolResult) and result.isError is True
    assert [c.text for c in result.content] == ["Error: doc_id is required"]
    # The module-level name keeps the old shape for in-process callers.
    assert core.store_get(doc_id="") == "Error: doc_id is required"


def test_a_real_catalog_list_tool_fails_as_an_error_result() -> None:
    """A catalog list tool's failure is isError, so [] means only no results.

    With no catalog the refusal is the missing catalog, otherwise the limit;
    both are marked failures of this list tool.
    """
    result = _call(catalog.mcp, "search", {"query": "x", "limit": 0})
    assert isinstance(result, CallToolResult) and result.isError is True
    assert len(result.content) == 1 and result.content[0].text


# ── lint: no failure return escapes the mark ───────────────────────────────


def _load_codemod_rules():
    """The same rules the 6b6hq codemod used, kept here as the census."""

    def starts_error(node: ast.AST) -> bool:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value.startswith("Error")
        if isinstance(node, ast.JoinedStr) and node.values:
            first = node.values[0]
            return (isinstance(first, ast.Constant) and isinstance(first.value, str)
                    and first.value.startswith("Error"))
        return False

    def error_dict(node: ast.AST) -> bool:
        return (isinstance(node, ast.Dict) and any(
            isinstance(k, ast.Constant) and k.value == "error" for k in node.keys))

    def is_failure(node: ast.AST) -> bool:
        if starts_error(node) or error_dict(node):
            return True
        return (isinstance(node, ast.List) and len(node.elts) == 1
                and (starts_error(node.elts[0]) or error_dict(node.elts[0])))

    return is_failure


#: (file, function) pairs whose ``error`` key is a reported state, not a
#: failure. ``_resolve_mode_diagnostics`` is a startup diagnostic, not a tool.
_NOT_FAILURES: frozenset[tuple[str, str]] = frozenset({
    ("core.py", "_resolve_mode_diagnostics"),
})


def _returns(path: pathlib.Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if (path.name, func.name) in _NOT_FAILURES:
            continue
        for node in ast.walk(func):
            if isinstance(node, ast.Return) and node.value is not None:
                value = node.value
                yield from (value.body, value.orelse) if isinstance(value, ast.IfExp) else (value,)


def _is_fail_call(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_fail"


@pytest.mark.lint
def test_no_failure_return_is_left_unmarked() -> None:
    """A literal ``return "Error: ..."`` or ``return {"error": ...}`` must use ``_fail``.

    Without the mark it reaches the client as a success, which is the defect
    nexus-6b6hq removed. What this sees: return statements in core.py and
    catalog.py whose value is written in place. What it cannot see: a failure
    built in a variable first, one returned from a helper in another module,
    text not written as a literal or f-string starting "Error" (``+``, ``%``,
    ``.format``), ``dict(error=...)``, a list of two or more error dicts, or
    failure text that does not start with "Error" (nexus-4jwr0).
    """
    is_failure = _load_codemod_rules()
    unmarked: list[str] = []
    marked: set[tuple[str, int, int]] = set()
    for path in _MCP_SOURCES:
        for value in _returns(path):
            if _is_fail_call(value):
                marked.add((path.name, value.lineno, value.col_offset))
            elif is_failure(value):
                unmarked.append(f"{path.name}:{value.lineno}")
    marked_count = len(marked)
    assert marked_count >= 90, f"only {marked_count} marked failure returns found -- census is checking too little"
    assert not unmarked, f"failure returns that would reach the client as a success: {unmarked}"

