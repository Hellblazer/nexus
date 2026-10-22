# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""One event per ``tools/call``: the tool name and the keys of ``_meta``.

nexus-2nxoo (shakeout F18). nx-mcp logged no tool calls, so nobody could say
whether a client (the Claude Desktop remote-devices bridge) sends per-request
``_meta`` such as a session id. This answers that from ``mcp.log``.

KEYS ONLY. ``_meta`` and the call arguments can carry tokens, so neither a
value nor an argument is ever read into the event.

Level: INFO when ``_meta`` carries any key besides ``progressToken``, DEBUG
otherwise. The ``mcp`` entry point logs at INFO by default
(``logging_setup._resolve_level``), and the server Claude Desktop starts has no
editable environment, so the case F18 asks about (a client forwarding a session
id or any other key) must reach ``mcp.log`` without ``NEXUS_LOG_LEVEL=DEBUG``.
Routine calls (no ``_meta``, or only the SDK's own ``progressToken``) stay at
DEBUG so ``mcp.log`` does not gain a line per call.

HOOK. FastMCP binds its ``call_tool`` handler into the low-level server's
``request_handlers[CallToolRequest]`` during ``FastMCP.__init__``, so the wrap
happens on the constructed server, the same private-internals coupling as
``_stale_host.install_stale_host_hook`` (verified against mcp 1.27.1). It
degrades to a status string, never an exception: an unverifiable SDK shape must
not break startup (nexus-vc5yb / nexus-3hc9z).
"""

from __future__ import annotations

from typing import Any

import structlog

#: ``_meta`` keys every MCP client may send on a call; their presence alone is
#: not news, so a call carrying only these logs at DEBUG.
_ROUTINE_META_KEYS: frozenset[str] = frozenset({"progressToken"})


def _meta_keys(req: Any) -> list[str]:
    """Sorted keys of the request's ``_meta``, ``[]`` when it has none.

    ``RequestParams.Meta`` declares ``progressToken`` and allows extras, so the
    keys are the declared fields that were set plus every extra.
    """
    meta = req.params.meta
    if meta is None:
        return []
    keys = set(meta.model_fields_set)
    keys.update(meta.model_extra or {})
    return sorted(keys)


def _emit(req: Any) -> None:
    # Fetched per call, not at import: this module loads before
    # configure_logging runs, and a logger bound earlier would miss the config.
    keys = _meta_keys(req)
    log = structlog.get_logger(__name__)
    emit = log.info if set(keys) - _ROUTINE_META_KEYS else log.debug
    emit("mcp_tool_call", tool=req.params.name, meta_keys=keys)


def install_tool_call_log(server: object) -> str:
    """Wrap the server's ``CallToolRequest`` handler with the per-call event.

    Returns a short status for the caller to log; never raises.
    """
    try:
        from mcp import types  # noqa: PLC0415 — SDK loaded only when patching

        registry = server._mcp_server.request_handlers  # type: ignore[attr-defined]
        original = registry.get(types.CallToolRequest)
        if original is None:
            return "unavailable: no CallToolRequest handler registered"
        if getattr(original, "_nx_tool_call_log", False):
            return "already applied"

        async def _logged(req: Any) -> Any:
            try:
                _emit(req)
            except Exception:  # noqa: BLE001 — observability must never break a tool call
                pass
            return await original(req)

        _logged._nx_tool_call_log = True  # type: ignore[attr-defined]
        registry[types.CallToolRequest] = _logged
        return "applied"
    except Exception as exc:  # noqa: BLE001 — boundary: an unverifiable SDK shape must not kill startup
        return f"unavailable: {type(exc).__name__}: {exc}"
