# SPDX-License-Identifier: AGPL-3.0-or-later
"""MCP client substrate for outbound calls to external MCP servers (RDR-139).

Distinct from :mod:`nexus.mcp`, which hosts the nexus MCP *server*. This
package is the *client* side: nexus reaching out to other MCP servers (the
DEVONthink built-in server first; RDR-126 daemon-held connections later).

Layer A of the RDR-139 design lives here. ``core`` holds the shared transport
and a fail-soft ``call_tool`` seam; per-server wrappers (e.g. ``devonthink``)
build on it.
"""
