# SPDX-License-Identifier: AGPL-3.0-or-later
"""P1.1 scaffold pins for the RDR-139 MCP client substrate.

These are deliberately minimal: they assert the `mcp` SDK dependency is
importable in the venv and that the new `nexus.mcp_client` package exists.
Behavioural contracts (fail-soft call_tool, availability gate) land in later
Phase 1 beads with their own test modules.
"""

import importlib


def test_mcp_streamable_http_importable() -> None:
    """The `mcp` SDK (pyproject.toml: mcp>=1.0) exposes the streamable-HTTP client."""
    mod = importlib.import_module("mcp.client.streamable_http")
    assert mod is not None


def test_mcp_client_package_importable() -> None:
    """The new nexus.mcp_client package is present and distinct from nexus.mcp."""
    client_pkg = importlib.import_module("nexus.mcp_client")
    server_pkg = importlib.import_module("nexus.mcp")
    assert client_pkg.__name__ == "nexus.mcp_client"
    assert client_pkg.__name__ != server_pkg.__name__
