# SPDX-License-Identifier: AGPL-3.0-or-later
"""Nexus-side integration of palinex a2ui v0.9 surfaces (RDR-127).

palinex (PyPI, Apache-2.0) owns the IR and the rendering machinery. This
module is the thin integration layer that:

- Provides a nexus chash resolver — looks up T3 chunks by content hash so
  ``palinex.wrap_as_mcp_ui_resource`` can pre-resolve ``openChash`` actions
  into ``copyToClipboard`` with the actual chunk text inline. Static-snapshot
  surfaces become useful without a live host bridge.

- Exposes ``render_surface_resource(payload)`` — the helper that the
  ``render_surface`` MCP tool wraps to produce embedded UI resources Claude
  Code renders inline as iframes.

See ``docs/rdr/rdr-127-substrate-decoupled-surface-rendering.md`` for the
integration shape and the bolt points to the eventual cockpit substrate
(``InProcessBroker`` → ``TupleSpaceBroker`` when RDR-118 successor lands).
"""
from __future__ import annotations

from nexus.surfaces.mcp_ui import nexus_chash_resolver, render_surface_resource

__all__ = ["nexus_chash_resolver", "render_surface_resource"]
