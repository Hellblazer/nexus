# SPDX-License-Identifier: AGPL-3.0-or-later
"""Minimal shared-state module for the top-level MCP's T1 chroma address.

RDR-105 RF-7: extracted into its own module so the dispatcher
(``operators/dispatch.py``) can read it without importing ``mcp/core``,
which transitively pulls FastMCP, chromadb, corpus, T3 — heavy and
circular-prone.

Stdlib-only by contract. Do not add imports beyond ``typing``.

Writers
    ``mcp.core`` lifespan when this MCP server owns the chroma and
    ``NX_T1_NEW_DISCOVERY=1`` (P1 spike feature flag, nexus-4fek).

Readers
    ``operators.dispatch`` for the ``share_t1=True`` case.
"""
from __future__ import annotations

#: Top-level MCP's chroma HTTP address, or None when no chroma is
#: published. ``(host, port)`` when set.
T1_ADDR: tuple[str, int] | None = None
