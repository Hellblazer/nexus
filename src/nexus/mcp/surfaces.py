# SPDX-License-Identifier: AGPL-3.0-or-later
"""MCP tool: render an a2ui v0.9 surface as a Claude-Code-renderable UI resource.

Registers ``render_surface`` on the ``nexus`` FastMCP instance defined in
``nexus.mcp.core``. Importing this module triggers the registration via the
``@mcp.tool()`` decorator side-effect. See RDR-127 for the integration shape.
"""
from __future__ import annotations

import json
from typing import Any

from nexus.mcp.core import mcp
from nexus.surfaces.mcp_ui import render_surface_resource as _render


@mcp.tool()
def render_surface(payload: str | dict[str, Any], collection: str = "knowledge") -> dict[str, Any]:
    """Render an a2ui v0.9 surface payload as an MCP UI resource.

    Wraps the payload via ``palinex.wrap_as_mcp_ui_resource`` with the nexus
    chash resolver — any chash references in the data model are substituted
    with the actual chunk text at wrap time, and any ``openChash`` Button
    actions are rewritten to ``copyToClipboard`` so the resolved text ends
    up on the clipboard when clicked. No live host bridge required for the
    surface to be useful in Claude Code.

    Args:
        payload: a2ui v0.9 surface payload. Accepts a dict (envelope shape
            ``{version, messages: [...]}`` or flat shape ``{components,
            dataModel}``) or a JSON string. JSON strings are convenient when
            the surface is constructed by an upstream tool that returns text.
        collection: T3 collection to resolve chashes against. Defaults to
            ``"knowledge"``.

    Returns:
        Embedded MCP UI resource dict: ``{type: "resource", resource:
        {uri: "ui://nexus/surface/<id>", mimeType: "text/html", text:
        <wrapper HTML>}}``. Claude Code's MCP client renders the HTML
        inline as a sandboxed iframe.

    Raises:
        ValueError: if ``payload`` is a string that can't be parsed as JSON.
    """
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError as e:
            raise ValueError(f"payload string is not valid JSON: {e}") from e
    if not isinstance(payload, dict):
        raise ValueError(f"payload must be a dict or JSON string; got {type(payload).__name__}")
    return _render(payload, collection=collection)
