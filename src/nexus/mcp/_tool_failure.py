# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Tool failures that reach an MCP client as ``isError`` (nexus-6b6hq).

Before this module a failing tool answered a SUCCESS result whose body said it
failed, in one of four shapes: an ``"Error: ..."`` string, an ``{"error": ...}``
dict, a ``[{"error": ...}]`` list, or a list holding an ``"Error: ..."`` string.
A client could not tell a failure from an empty answer without knowing which
shape each tool used. Sam's ruling of 2026-10-24: a failure reaches every MCP
client as ``isError: true``, with the text byte-identical; a list tool's ``[]``
means only "no results". The error result carries exactly the content MCPServer
would have built from the unmarked value, so only the flag changes.

A failure site marks what it returns with :func:`fail`. The marked value is a
subclass of the plain ``str`` / ``dict`` / ``list`` and compares equal to it,
so the in-process callers (``plans/runner.py``, ``commands/answer_runs.py``,
``commands/doc.py``) and the tests that assert today's shapes see no change.
Only the registration boundary looks at the mark:
``_sdk_patches._patch_tool_failure_is_error`` wraps every registered tool body
so a marked return becomes a ``CallToolResult`` with ``isError=True``.

Marked, never sniffed. A shape rule at the boundary ("starts with Error:")
would misfire on a tool that echoes stored content: ``scratch get`` returns a
note's text verbatim, and a note may begin with "Error:".
"""

from __future__ import annotations

import functools
import inspect
from typing import Any, overload

import structlog
from mcp.types import CallToolResult

_log = structlog.get_logger(__name__)

#: Tools never converted, whatever they return. The ``hook_*`` tools fail open
#: on purpose (``never_fail``). ``nx_answer``'s ``[non-answer: ...]`` text is a
#: success contract too, but it is never marked, so the tool itself needs no
#: exemption.
EXEMPT_TOOLS: frozenset[str] = frozenset()
EXEMPT_TOOL_PREFIXES: tuple[str, ...] = ("hook_",)


class FailureText(str):
    """An ``"Error: ..."`` string a tool returned because it failed."""

    __slots__ = ()


class FailureDict(dict):  # type: ignore[type-arg]
    """An ``{"error": ...}`` dict a tool returned because it failed."""


class FailureList(list):  # type: ignore[type-arg]
    """A ``[{"error": ...}]`` or ``["Error: ..."]`` list a tool returned because it failed."""



@overload
def fail(value: str) -> FailureText: ...
@overload
def fail(value: dict[str, Any]) -> FailureDict: ...
@overload
def fail(value: list[Any]) -> FailureList: ...


def fail(value: str | dict[str, Any] | list[Any]) -> FailureText | FailureDict | FailureList:
    """Mark *value* as a failure return. The value itself is unchanged."""
    if isinstance(value, str):
        return FailureText(value)
    if isinstance(value, dict):
        return FailureDict(value)
    if isinstance(value, list):
        return FailureList(value)
    raise TypeError(f"fail() takes a str, dict or list, not {type(value).__name__}")


def is_failure(result: object) -> bool:
    """Whether *result* is a value a failure site marked with :func:`fail`."""
    return isinstance(result, (FailureText, FailureDict, FailureList))


def _unmarked(result: FailureText | FailureDict | FailureList) -> str | dict[str, Any] | list[Any]:
    if isinstance(result, FailureText):
        return str(result)
    if isinstance(result, FailureDict):
        return dict(result)
    return list(result)


def _error_result(result: FailureText | FailureDict | FailureList) -> CallToolResult:
    """The content MCPServer would have sent for the plain value, flagged ``isError``.

    The SDK's own conversion, so the text is byte-identical to the success
    result this replaced: a string as itself, a dict as its JSON with every
    key, a list as one block per item.
    """
    from mcp.server.mcpserver.utilities.func_metadata import (  # noqa: PLC0415 — SDK internal, probed by the patch
        _convert_to_content,
    )

    return CallToolResult(content=list(_convert_to_content(_unmarked(result))), is_error=True)


def is_exempt(name: str) -> bool:
    """Whether the tool named *name* keeps its failure text as a success result."""
    return name in EXEMPT_TOOLS or name.startswith(EXEMPT_TOOL_PREFIXES)


def _keep_crash_text(exc: Exception, tool: str) -> None:
    """Log a tool-body crash with its traceback, then re-raise it as a ``ToolError``.

    mcp 1.x rendered every tool-body exception into the ``isError`` text as
    ``Error executing tool <name>: <exc>``. mcp 2.x does that only for a
    ``ToolError`` and answers any other exception with the bare
    ``Error executing tool <name>`` ("nothing from an unexpected exception
    reaches the client"). Two things here depend on the 1.x text: the
    stale-host hook (``_stale_host``) recognises an upgraded-under-a-live-process
    host by the ``No module named`` / ``cannot import name`` / ``has no
    attribute`` text of an ``ImportError`` / ``AttributeError``, and the tools
    that raise on purpose (``nx_tidy``, ``nx_enrich_beads``, ``nx_plan_audit``,
    ``setup``, ``daemon_uninstall``: an ``OperatorError`` is a plain
    ``Exception``) tell the caller what went wrong through that text
    (GH #1414). So every ``Exception`` keeps its text (nexus-sgmyl).

    The SDK logs a ``ToolError`` at INFO without a traceback, so a real bug
    would lose its stack in the server log; this logs it first. ``ToolError``
    and ``MCPError`` never reach here (the caller lets them pass), and a
    ``BaseException`` that is not an ``Exception`` (cancellation,
    ``KeyboardInterrupt``) is never caught.
    """
    from mcp.server.mcpserver.exceptions import ToolError  # noqa: PLC0415 — SDK exception, only on the failure path

    _log.error("mcp_tool_crashed", tool=tool, error_type=type(exc).__name__, exc_info=exc)
    raise ToolError(str(exc)) from exc


def _passes_through(exc: Exception) -> bool:
    """Whether the SDK already reports *exc* with its own text or as a protocol error."""
    from mcp.server.mcpserver.exceptions import ResourceError, ToolError  # noqa: PLC0415 — SDK exceptions
    from mcp.shared.exceptions import MCPError  # noqa: PLC0415

    return isinstance(exc, (ToolError, ResourceError, MCPError))


def failures_as_error_results(fn: Any) -> Any:
    """Wrap a tool body so a marked failure return becomes an ``is_error`` result.

    Keeps the body sync or async as it was, so the SDK still sees a sync body
    and moves it off the loop. ``functools.wraps`` keeps ``__wrapped__``, so
    MCPServer builds the same parameter schema from it. Any ``Exception`` out of
    the body other than ``ToolError`` / ``ResourceError`` / ``MCPError`` keeps its
    text and is logged with its traceback (see :func:`_keep_crash_text`).
    """
    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def _async_body(*args: Any, **kwargs: Any) -> Any:
            try:
                result = await fn(*args, **kwargs)
            except Exception as exc:
                if _passes_through(exc):
                    raise
                _keep_crash_text(exc, fn.__name__)
            return _error_result(result) if is_failure(result) else result

        _async_body._nx_failure_wrapped = True  # type: ignore[attr-defined]
        return _async_body

    @functools.wraps(fn)
    def _sync_body(*args: Any, **kwargs: Any) -> Any:
        try:
            result = fn(*args, **kwargs)
        except Exception as exc:
            if _passes_through(exc):
                raise
            _keep_crash_text(exc, fn.__name__)
        return _error_result(result) if is_failure(result) else result

    _sync_body._nx_failure_wrapped = True  # type: ignore[attr-defined]
    return _sync_body
