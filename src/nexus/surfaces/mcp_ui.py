# SPDX-License-Identifier: AGPL-3.0-or-later
"""Nexus chash resolver + MCP UI resource emitter for palinex surfaces.

RDR-127 Items 5 + 6: when a nexus tool wants to render an a2ui v0.9 surface
inline in Claude Code, it hands the payload to ``render_surface_resource``,
which delegates to ``palinex.wrap_as_mcp_ui_resource`` with the nexus-aware
chash resolver. The resulting HTML is returned as an embedded UI resource.
"""
from __future__ import annotations

import logging
import secrets
from typing import Any

import palinex

logger = logging.getLogger(__name__)


def nexus_chash_resolver(chash: str, *, collection: str = "knowledge") -> str | None:
    """Resolve a chash to chunk text by looking up T3 entry by ID.

    Per RDR-108, T3 document IDs are ``chunk_text_hash[:32]`` — a 32-char
    lowercase hex prefix of the content hash. This resolver treats the
    input string as a candidate T3 doc_id and returns its content if found.

    Non-chash strings (anything not matching the 32-hex-char shape) return
    None without an exception, so ``palinex.wrap_as_mcp_ui_resource`` can
    pass arbitrary data-model strings through this resolver harmlessly.

    Args:
        chash: Candidate content-hash string (typically 32 lowercase hex).
        collection: T3 collection to search. Defaults to ``"knowledge"``;
            callers can pass a different collection via a wrapper.

    Returns:
        Chunk content as a string if found, else None.
    """
    if not isinstance(chash, str) or len(chash) != 32:
        return None
    if not all(c in "0123456789abcdef" for c in chash):
        return None

    # Imports kept lazy so that:
    #   - palinex tests can stub `palinex.wrap_as_mcp_ui_resource` and
    #     pass their own resolver without dragging in T3 setup
    #   - import-time cycles are avoided (nexus.mcp_infra imports a lot)
    try:
        from nexus.corpus import t3_collection_name
        from nexus.mcp_infra import get_t3
    except ImportError as e:
        logger.warning("nexus_chash_resolver: cannot import T3 deps (%s)", e)
        return None

    try:
        t3 = get_t3()
        col_name = t3_collection_name(collection, t3=t3)
        entry = t3.get_by_id(col_name, chash)
        if entry is None:
            return None
        content = entry.get("content")
        return content if isinstance(content, str) else None
    except Exception as e:
        logger.warning("nexus_chash_resolver: lookup failed for %s: %s", chash[:12], e)
        return None


def render_surface_resource(
    payload: dict[str, Any],
    *,
    collection: str = "knowledge",
    renderer_url: str = "https://hellblazer.github.io/palinex/index.html",
    title: str = "nexus surface",
) -> dict[str, Any]:
    """Render an a2ui v0.9 surface payload as an MCP UI resource.

    Wraps ``palinex.wrap_as_mcp_ui_resource`` with the nexus T3 chash
    resolver so any chash references in the payload's data model are
    substituted with the actual chunk text at wrap time, and any
    ``openChash`` Button actions are rewritten to ``copyToClipboard``
    with the resolved text. The resulting HTML is a static snapshot:
    no live host bridge required for these chash-bound interactions.

    Args:
        payload: a2ui v0.9 envelope (``{version, messages: [...]}``) or
            flat shape (``{components: [...], dataModel: {...}}``).
        collection: T3 collection to look up chashes in. Defaults to
            ``"knowledge"``.
        renderer_url: URL of the palinex renderer to embed. Defaults to
            the hosted GitHub Pages renderer.
        title: HTML ``<title>`` for the wrapper page.

    Returns:
        MCP UI resource dict: ``{type: "resource", resource: {uri, mimeType,
        text}}``. Pass this through directly as a tool return value; Claude
        Code's MCP client renders the HTML inline as an iframe.
    """
    resolver = (
        nexus_chash_resolver
        if collection == "knowledge"
        else lambda c: nexus_chash_resolver(c, collection=collection)
    )
    html = palinex.wrap_as_mcp_ui_resource(
        payload,
        chash_resolver=resolver,
        renderer_url=renderer_url,
        title=title,
    )
    return {
        "type": "resource",
        "resource": {
            "uri": f"ui://nexus/surface/{secrets.token_hex(8)}",
            "mimeType": "text/html",
            "text": html,
        },
    }
