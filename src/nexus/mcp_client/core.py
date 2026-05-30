# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Shared MCP-client core: transport connect + fail-soft ``call_tool`` (RDR-139 Layer A).

This is the substrate seam shared with RDR-126. It provides two things and
deliberately decides nothing about lifecycle:

- :func:`open_session` — an async context manager that connects to a
  streamable-HTTP MCP endpoint and yields an initialized
  :class:`mcp.ClientSession`. The *caller* chooses the lifecycle: RDR-139's
  per-call wrapper opens and closes one per tool call; RDR-126's daemon holds
  one open. Do not bake either policy in here.
- :func:`call_tool` — the result-or-``None`` call contract. Every failure
  (transport error, ``isError`` result, unparseable payload) is swallowed into
  a structured-log warning and a ``None`` return. No MCP failure may propagate
  to a caller; that is what makes every layer's fallback provable (Gap 0).

Secrets in tool arguments are redacted before any value is logged.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

import structlog

log = structlog.get_logger(__name__)

#: Placeholder substituted for any argument value whose key looks secret.
REDACTED = "***redacted***"

#: Argument keys (lowercased) whose values are redacted from log events.
_SECRET_KEYS = frozenset(
    {"authorization", "auth", "token", "api_key", "apikey", "password", "secret"}
)


@runtime_checkable
class ToolCaller(Protocol):
    """Structural type for the slice of ``ClientSession`` that :func:`call_tool` uses."""

    async def call_tool(
        self, name: str, arguments: dict[str, Any] | None = None
    ) -> Any: ...


@dataclass(frozen=True)
class MCPEndpoint:
    """Connection coordinates for a streamable-HTTP MCP server.

    ``url`` is the full endpoint (e.g. ``http://localhost:8420/mcp``). ``headers``
    carries optional transport headers; ``timeout_s`` bounds the connect/read.
    """

    url: str
    headers: Mapping[str, str] = field(default_factory=dict)
    timeout_s: float = 30.0


def _redact(arguments: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy of ``arguments`` with secret-looking values masked."""
    return {
        key: (REDACTED if key.lower() in _SECRET_KEYS else value)
        for key, value in arguments.items()
    }


def _parse_result(result: Any) -> dict[str, Any] | None:
    """Coerce a ``CallToolResult`` into a plain dict, or ``None`` if not parseable.

    Preference order: ``structuredContent`` (already a dict) → the first text
    content block parsed as JSON → a ``{"text": ...}`` wrapper of joined text.
    """
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        return structured

    content = getattr(result, "content", None) or []
    texts = [getattr(block, "text", None) for block in content]
    texts = [t for t in texts if isinstance(t, str)]
    if not texts:
        return None

    joined = "\n".join(texts)
    try:
        parsed = json.loads(joined)
    except (ValueError, TypeError):
        return {"text": joined}
    return parsed if isinstance(parsed, dict) else {"result": parsed}


async def call_tool(
    session: ToolCaller,
    name: str,
    arguments: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Call an MCP tool fail-soft: parsed dict on success, ``None`` on any failure.

    A raised transport error, an ``isError`` result, or an unparseable payload
    all collapse to ``None`` plus a single structured warning. The exception is
    never re-raised: callers rely on ``None`` to take their tested fallback path.
    """
    args = dict(arguments or {})
    try:
        result = await session.call_tool(name, args)
    except Exception as exc:  # fail-soft: no MCP failure may propagate
        log.warning(
            "mcp_call_tool_failed",
            tool=name,
            args=_redact(args),
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return None

    if getattr(result, "isError", False):
        log.warning("mcp_call_tool_is_error", tool=name, args=_redact(args))
        return None

    parsed = _parse_result(result)
    if parsed is None:
        log.warning("mcp_call_tool_unparseable", tool=name, args=_redact(args))
    return parsed


@asynccontextmanager
async def open_session(endpoint: MCPEndpoint) -> AsyncIterator[Any]:
    """Connect to ``endpoint`` over streamable HTTP and yield an initialized session.

    The transport and session are torn down on exit. Lifecycle policy lives with
    the caller: open one per call (RDR-139) or hold one open (RDR-126). Imports
    are local so importing this module never costs the ``mcp`` SDK transport
    machinery until a connection is actually opened.
    """
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared._httpx_utils import create_mcp_http_client

    http_client = create_mcp_http_client(
        headers=dict(endpoint.headers) or None,
        timeout=httpx.Timeout(endpoint.timeout_s),
    )
    # ``streamable_http_client`` does NOT own a caller-provided client's
    # lifecycle (it skips its own ``aclose`` when ``http_client`` is passed in).
    # Close it ourselves so the long-running Layer A' server does not leak one
    # AsyncClient + connection pool per call (RDR-139 Layer A' code review).
    async with http_client:
        async with streamable_http_client(endpoint.url, http_client=http_client) as (
            read_stream,
            write_stream,
            _get_session_id,
        ):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                yield session
