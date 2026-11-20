# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The guarded seam between the MCP server's core and its identity lease (RDR-253 Step 3, nexus-hczdo.1.6).

``nexus.mcp.core`` calls these four functions and imports nothing of the lease itself. Identity is additive: a failure to
import :mod:`nexus.mcp.identity_lease` (it pulls in the whole ``nexus.identity`` chain) is logged here and never reaches the
server's start, its T1 handoff tick or its exit path. This module imports nothing from ``nexus.identity`` at load, so it is
as cheap as the deferred imports it replaces."""
from __future__ import annotations

from typing import Any

import structlog

_log = structlog.get_logger(__name__)


def _lease() -> Any | None:
    """The lease module, or None (logged) when it cannot be imported."""
    try:
        from nexus.mcp import identity_lease  # noqa: PLC0415 -- deferred: the identity import chain is not paid at server load
    except Exception as exc:  # noqa: BLE001 -- identity is additive; the server runs without it
        _log.warning("identity_lease_unavailable", error=f"{type(exc).__name__}: {exc}")
        return None
    return identity_lease


def start() -> None:
    """The server's start path: start the lease (:func:`nexus.mcp.identity_lease.start_safely`)."""
    lease = _lease()
    if lease is not None:
        lease.start_safely()


async def follow_marker(marker: Any, log: Any) -> None:
    """The T1 handoff tick found a marker: let the lease follow the identity it names (never raises)."""
    lease = _lease()
    if lease is not None:
        await lease.follow_marker(marker, log)


def start_shutdown() -> None:
    """The exit path: start the lease's own shutdown thread (never raises, never blocks)."""
    lease = _lease()
    if lease is not None:
        lease.start_shutdown()


def await_shutdown() -> None:
    """The exit path: wait for the lease's shutdown until its own deadline."""
    lease = _lease()
    if lease is not None:
        lease.await_shutdown()
