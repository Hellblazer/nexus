# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regression test for nexus-20iee.

pydantic-settings 2.15.0 added ``IncompleteFieldDefinitionWarning``, which
fires from the upstream ``mcp`` SDK's ``FastMCP`` ``Settings`` model: its
``lifespan`` field carries a forward reference to ``FastMCP`` that is
unresolved at class-definition time, and the SDK never calls
``model_rebuild()`` on it. Every ``FastMCP(...)`` construction -- including
``nexus.mcp.core``'s own module-level ``mcp = FastMCP(...)`` -- triggered the
warning on any path importing the MCP server settings (e.g. ``nx doctor
--check-t1``).

``nexus.mcp.core`` fixes this by calling ``Settings.model_rebuild()`` once
``FastMCP`` is fully defined, before constructing its own instance. This
test scopes ``IncompleteFieldDefinitionWarning`` to an error so a regression
(e.g. the model_rebuild call being removed, or a future SDK version
reintroducing an unresolved forward reference elsewhere) fails loudly.
"""
from __future__ import annotations

import warnings

import pytest


def test_fastmcp_construction_does_not_warn_incomplete_field_definition():
    """Constructing a FastMCP instance must not emit
    IncompleteFieldDefinitionWarning once nexus.mcp.core has been imported.

    Skipped on pydantic-settings versions that predate the warning class
    (< 2.15) -- the fix (model_rebuild()) is a no-op there and there is
    nothing to regress against.
    """
    try:
        # noqa: PLC0415 -- version-conditional; only importable on
        # pydantic-settings >= 2.15, so must be probed inside the test.
        from pydantic_settings.exceptions import IncompleteFieldDefinitionWarning  # noqa: PLC0415
    except ImportError:
        pytest.skip(
            "installed pydantic-settings predates IncompleteFieldDefinitionWarning "
            "(added in 2.15.0); nothing to regress against"
        )

    # Import nexus.mcp.core first -- this is what runs the model_rebuild()
    # fix (nexus-20iee) and is also the reproduction path from the bead
    # (any path importing the MCP server settings, e.g. `nx doctor
    # --check-t1`). Deliberately local: importing nexus.mcp.core at module
    # scope would trigger the fix (or the bug) as an import-time side
    # effect for every test in this file/session, defeating the point of
    # scoping the warnings-as-error probe to this one test.
    import nexus.mcp.core  # noqa: F401,PLC0415
    from mcp.server.fastmcp import FastMCP  # noqa: PLC0415

    with warnings.catch_warnings():
        warnings.simplefilter("error", IncompleteFieldDefinitionWarning)
        # Constructs a fresh Settings() internally; this is exactly the
        # call that warned before the fix.
        FastMCP("nexus-20iee-regression-probe")
