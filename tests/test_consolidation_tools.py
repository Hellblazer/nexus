# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for P3 consolidation MCP tools — RDR-080.

Tests cover nx_tidy, nx_enrich_beads, nx_plan_audit:
  - Tool existence and registration
  - Worker-mode restriction
  - Argument validation
"""
from __future__ import annotations

import pytest


class TestToolRegistration:
    """All 3 P3 tools are registered in the MCP server."""

    def test_nx_tidy_registered(self):
        from nexus.mcp.core import mcp

        tool_names = {t.name for t in mcp._tool_manager.list_tools()}
        assert "nx_tidy" in tool_names

    def test_nx_enrich_beads_registered(self):
        from nexus.mcp.core import mcp

        tool_names = {t.name for t in mcp._tool_manager.list_tools()}
        assert "nx_enrich_beads" in tool_names

    def test_nx_plan_audit_registered(self):
        from nexus.mcp.core import mcp

        tool_names = {t.name for t in mcp._tool_manager.list_tools()}
        assert "nx_plan_audit" in tool_names


class TestWorkerModeRestriction:
    """P3 tools are in _WORKER_FORBIDDEN_TOOLS."""

    def test_all_in_forbidden(self):
        from nexus.mcp.core import _WORKER_FORBIDDEN_TOOLS

        for name in ("nx_tidy", "nx_enrich_beads", "nx_plan_audit"):
            assert name in _WORKER_FORBIDDEN_TOOLS, f"{name} not in _WORKER_FORBIDDEN_TOOLS"


class TestNxTidy:
    """nx_tidy consolidates knowledge entries."""

    def test_callable(self):
        from nexus.mcp.core import nx_tidy

        assert callable(nx_tidy)

    def test_signature(self):
        import inspect
        from nexus.mcp.core import nx_tidy

        sig = inspect.signature(nx_tidy)
        params = set(sig.parameters.keys())
        assert "topic" in params
        assert "collection" in params


class TestNxEnrichBeads:
    """nx_enrich_beads enriches beads with execution context."""

    def test_callable(self):
        from nexus.mcp.core import nx_enrich_beads

        assert callable(nx_enrich_beads)

    def test_signature(self):
        import inspect
        from nexus.mcp.core import nx_enrich_beads

        sig = inspect.signature(nx_enrich_beads)
        params = set(sig.parameters.keys())
        assert "bead_description" in params


class TestNxPlanAudit:
    """nx_plan_audit audits a plan for correctness."""

    def test_callable(self):
        from nexus.mcp.core import nx_plan_audit

        assert callable(nx_plan_audit)

    def test_signature(self):
        import inspect
        from nexus.mcp.core import nx_plan_audit

        sig = inspect.signature(nx_plan_audit)
        params = set(sig.parameters.keys())
        assert "plan_json" in params
