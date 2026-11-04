# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The client-side subspace-to-template resolver (RDR-239 Phase 3, bead
nexus-mdbds.2.17), shared by ``nexus.mcp.subscriptions`` (take-enabled
check), ``nexus.health`` (doctor rows) and ``nx tuple dictionary``.

Standard library only, on purpose: an MCP tool call must never pull in
``nexus.health`` (a large module with its own import cost) for a fifteen-line
pure function, so the function lives here, where any of them can import it
cheaply.
"""
from __future__ import annotations

from typing import Any


def resolve_tuple_template(templates: list[dict[str, Any]], subspace: str) -> dict[str, Any] | None:
    """Resolve *subspace* against the ``registry()`` wire's ``templates`` list
    and return the matching template dict, or ``None`` when nothing resolves.

    Mirrors ``TemplateRegistry.resolve()``'s literal-before-pattern rule
    (``service/src/main/java/dev/nexus/service/tuples/TemplateRegistry.java``):
    a literal template name is checked before any parameterised one, and a
    ``<param>`` segment matches exactly one segment in the same position. A
    template name resolves to itself (a literal one by equality, a patterned
    one because ``<topic>`` matches the text ``<topic>``).
    """
    segments = subspace.split("/")

    def _matches(name: str) -> bool:
        t_segments = name.split("/")
        if len(t_segments) != len(segments):
            return False
        return all(
            (ts.startswith("<") and ts.endswith(">")) or ts == ss
            for ts, ss in zip(t_segments, segments)
        )

    literal = [t for t in templates if "<" not in t.get("name", "")]
    patterned = [t for t in templates if "<" in t.get("name", "")]
    for t in literal:
        if t.get("name", "") == subspace:
            return t
    for t in patterned:
        if _matches(t.get("name", "")):
            return t
    return None
