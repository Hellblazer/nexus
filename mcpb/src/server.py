#!/usr/bin/env python3
"""Nexus MCP server entry point for the Claude Desktop .mcpb spike.

RDR-126 P1. Imports and runs ``nexus.mcp.core:main``, the same entry
point the Claude Code plugin invokes via the ``nx-mcp`` console script.
"""
from __future__ import annotations


def main() -> None:
    from nexus.mcp.core import main as _nx_mcp_main
    _nx_mcp_main()


if __name__ == "__main__":
    main()
