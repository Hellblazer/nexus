# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Per-call DEVONthink MCP client over the shared core seam (RDR-139 Layer A).

The DEVONthink 4 built-in MCP server is an always-on localhost HTTP endpoint
(``http://localhost:8420/mcp``, no spawn/teardown). This module is the
**CLI-path-only** face: each :func:`dt_call` opens a session, runs one tool,
and closes it. Async callers (the aspect worker, any future daemon path) must
NOT use this module — they use the Layer A′ server face. :func:`dt_call`
enforces that contract with a running-loop guard.

Every helper is fail-soft: a missing/unreachable DT, an excluded record, or a
malformed result yields ``[]`` / ``None`` / ``False`` and a structured log
line, never an exception. The :func:`available` gate lets each layer fall back
to its tested pre-RDR-139 behaviour (Gap 0).
"""

from __future__ import annotations

import asyncio
from typing import Any, TypedDict

import structlog

from nexus.config import load_config
from nexus.mcp_client.core import MCPEndpoint, call_tool, open_session

log = structlog.get_logger(__name__)

#: Default DEVONthink built-in MCP endpoint (spike-verified, RDR-139).
DEFAULT_DT_MCP_URL = "http://localhost:8420/mcp"

#: Module-level availability cache; ``None`` = not yet probed.
_AVAIL_CACHE: bool | None = None


class Neighbour(TypedDict):
    """A DEVONthink record adjacent to a query record (similarity or link)."""

    uuid: str
    score: float
    name: str


def dt_mcp_url() -> str:
    """Resolve the DT MCP endpoint URL (config ``devonthink.mcp.url``, else default)."""
    cfg = load_config()
    url = (
        cfg.get("devonthink", {}).get("mcp", {}).get("url")
        if isinstance(cfg.get("devonthink"), dict)
        else None
    )
    return url or DEFAULT_DT_MCP_URL


def reset_availability_cache() -> None:
    """Clear the cached :func:`available` result (tests; long-lived processes)."""
    global _AVAIL_CACHE
    _AVAIL_CACHE = None


def dt_call(tool: str, args: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Run one DT MCP tool synchronously, fail-soft (``None`` on any failure).

    Bridges the async ``mcp`` SDK into the synchronous CLI via ``asyncio.run``.
    A running event loop is a contract violation (Layer A is CLI-path-only):
    calling ``asyncio.run`` from one raises an opaque ``RuntimeError`` that the
    fail-soft contract would otherwise mask as a benign ``None``. The guard
    logs a DISTINCT ``dt_asyncio_context_error`` so the misuse is visible.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass  # expected: no running loop, asyncio.run is safe
    else:
        log.error(
            "dt_asyncio_context_error",
            tool=tool,
            hint="nexus.mcp_client.devonthink is CLI-path-only; use the Layer A' server face from async contexts",
        )
        return None

    endpoint = MCPEndpoint(url=dt_mcp_url())

    async def _run() -> dict[str, Any] | None:
        async with open_session(endpoint) as session:
            return await call_tool(session, tool, args or {})

    try:
        return asyncio.run(_run())
    except Exception as exc:  # connect/transport failure → fail-soft
        log.warning("dt_call_failed", tool=tool, error=str(exc), error_type=type(exc).__name__)
        return None


def available(*, refresh: bool = False) -> bool:
    """Whether DEVONthink is reachable and running (cached).

    Probes the ``is_running`` tool (``{running: bool, ...}``). Unreachable
    server or ``running=False`` → ``False``. The result is cached; pass
    ``refresh=True`` to re-probe.
    """
    global _AVAIL_CACHE
    if not refresh and _AVAIL_CACHE is not None:
        return _AVAIL_CACHE
    result = dt_call("is_running")
    _AVAIL_CACHE = bool(result) and bool(result.get("running"))
    return _AVAIL_CACHE


def dt_find_similar(uuid: str, *, limit: int = 25, floor: float = 0.0) -> list[Neighbour]:
    """Similarity neighbours of ``uuid`` (DT 'See Also'), filtered by ``floor``.

    ``floor`` is also passed to the server as ``min_score`` for an early prune;
    the client-side filter is a defensive backstop. Entries without a UUID are
    dropped. Empty list when DT is unavailable or returns nothing.
    """
    result = dt_call(
        "find_similar_records",
        {"mode": "record", "uuid": uuid, "limit": limit, "min_score": floor},
    )
    if not result:
        return []
    out: list[Neighbour] = []
    for r in result.get("results", []) or []:
        ruuid = r.get("uuid")
        score = float(r.get("score", 0.0) or 0.0)
        if not ruuid or score < floor:
            continue
        out.append(Neighbour(uuid=ruuid, score=score, name=r.get("name", "")))
    return out


def dt_record_links(uuid: str) -> list[Neighbour]:
    """DEVONthink's own deliberate link neighbours (item links, both directions).

    Higher precision than similarity: these are author-curated references. Score
    is fixed at ``1.0`` to mark them as deliberate. Empty list when unavailable.
    """
    result = dt_call("get_record_links", {"uuid": uuid, "direction": "both", "kind": "item"})
    if not result:
        return []
    entries: list[dict[str, Any]] = []
    for key in ("incoming", "outgoing"):
        value = result.get(key)
        if isinstance(value, list):
            entries.extend(value)
    seen: set[str] = set()
    out: list[Neighbour] = []
    for r in entries:
        ruuid = r.get("uuid")
        if not ruuid or ruuid in seen:
            continue
        seen.add(ruuid)
        out.append(Neighbour(uuid=ruuid, score=1.0, name=r.get("name", "")))
    return out


def dt_resolve_doi(doi: str) -> dict[str, Any] | None:
    """Resolve a DOI to CrossRef bibliographic fields, or ``None`` (Layer C source)."""
    if not doi:
        return None
    return dt_call("resolve_doi_metadata", {"doi": doi})


def dt_extract_content(uuid: str) -> str | None:
    """AI-optimised text body of a record, or ``None`` (Layer D, non-file-backed records).

    Joins a sectioned/paged result into one string; returns ``None`` when no
    text is available (or the record is excluded from AI access).
    """
    result = dt_call("extract_record_content", {"uuid": uuid})
    if not result:
        return None
    text = result.get("text")
    if isinstance(text, str) and text:
        return text
    sections = result.get("sections") or result.get("pages")
    if isinstance(sections, list):
        parts = [s.get("text", "") for s in sections if isinstance(s, dict)]
        joined = "\n".join(p for p in parts if p)
        return joined or None
    return None


def dt_set_tags(uuid: str, tags: list[str], *, mode: str = "add") -> bool:
    """Write tags onto a record (default additive). ``True`` on success (Layer F)."""
    if not tags:
        return False
    result = dt_call("set_record_tags", {"uuid": uuid, "tags": tags, "mode": mode})
    return result is not None


def dt_set_annotation(uuid: str, text: str, *, mode: str = "append") -> bool:
    """Write an annotation note onto a record. ``True`` on success (Layer F backlink).

    The RDR Layer F design uses this to stamp a backlink to the nexus tumbler.
    Defaults to ``mode="append"`` so a nexus backlink never clobbers an existing
    annotation (no-clobber; DEVONthink also auto-checkpoints prior content when
    the host DB has versioning enabled). Empty text short-circuits to ``False``.
    """
    if not text:
        return False
    result = dt_call("set_record_annotation", {"uuid": uuid, "text": text, "mode": mode})
    return result is not None


def dt_set_custom_metadata(uuid: str, fields: dict[str, Any], *, mode: str = "merge") -> bool:
    """Write custom-metadata fields onto a record (default merge). ``True`` on success (Layer F)."""
    if not fields:
        return False
    result = dt_call(
        "set_record_custom_metadata", {"uuid": uuid, "metadata": fields, "mode": mode}
    )
    return result is not None
