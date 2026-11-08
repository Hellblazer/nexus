# SPDX-License-Identifier: AGPL-3.0-or-later
"""The nested ``nx-mcp`` server enters the sensitivity scope for every tool call (RDR-243 Phase 2 Step 2.5).

``_build_dispatch_env`` exports ``NX_SENSITIVITY_POSTURE=llm_bound`` to every ``claude -p`` child it launches, and
Claude Code hands that environment to the stdio MCP servers the child starts (assumption A5, T2 nexus_rdr/243-research-11).
``nexus.sensitivity.current_posture`` already falls back to the variable when no scope is entered, so every read the
server makes under it is ``exclude`` without this module. What this hook adds is the scope itself: each ``tools/call``
runs inside ``llm_bound("nx_mcp_child")``, so a tool that dispatches again passes ``assert_llm_bound`` and
``current_surface()`` names the server, whatever happens to the process environment afterwards.

A server started without the variable (a user's own session) installs nothing and behaves as before.

COUPLING: appends a ``(ctx, call_next)`` entry to ``server.middleware`` exactly as
``nexus.mcp._stale_host.install_stale_host_hook`` does (mcp 2.x provisional API); the idempotency tag is
``_nx_sensitivity_posture``.
"""
from __future__ import annotations

import os
from typing import Any

import structlog

from nexus.sensitivity import CHILD_SURFACE, POSTURE_ENV, child_scope

__all__ = ["CHILD_SURFACE", "install_sensitivity_posture_hook"]

_log = structlog.get_logger(__name__)


def install_sensitivity_posture_hook(server: object) -> bool:
    """Wrap every ``tools/call`` of *server* in ``llm_bound`` when the environment says this process is a scoped
    parent's child. Returns True when the hook is installed (or already was), False when the variable is not
    ``llm_bound`` or the SDK's middleware list moved. Never raises: MCP boot is not blocked."""
    if os.environ.get(POSTURE_ENV) != "llm_bound":
        return False
    try:
        middleware = server.middleware  # type: ignore[attr-defined]
        if any(getattr(m, "_nx_sensitivity_posture", False) for m in middleware):
            return True

        async def _scoped(ctx: Any, call_next: Any) -> Any:
            if ctx.method != "tools/call":
                return await call_next(ctx)
            with child_scope():
                return await call_next(ctx)

        _scoped._nx_sensitivity_posture = True  # type: ignore[attr-defined]
        middleware.append(_scoped)
    except Exception as exc:  # noqa: BLE001 — never block boot on the posture hook
        _log.warning("mcp_sensitivity_posture_install_failed", error=f"{type(exc).__name__}: {exc}")
        return False
    _log.info("mcp_sensitivity_posture", posture="llm_bound", surface=CHILD_SURFACE)
    return True
