# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the ``traverse`` MCP tool (RDR-078 P3, SC-4/SC-5/SC-16).

Focuses on the contracts that protect callers from silent data loss:

  * Malformed seed strings are dropped — but the return value signals
    the loss; an all-invalid input returns the empty-result shape, not
    an exception.
  * Mixed valid/invalid seeds: valid ones traverse, invalid ones are
    silently dropped (partial result, no exception).
  * SC-16 mutual-exclusion: link_types + purpose together returns an
    ``{"error": ...}`` dict, not a raise.
  * Unknown purpose: returns empty result with a ``"warning"`` key.
  * Depth cap: depth=99 is silently clamped to _TRAVERSE_MAX_DEPTH.
  * Empty seeds input returns empty-result shape.
  * String seeds normalised: single string accepted alongside list.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


# ── helpers ──────────────────────────────────────────────────────────────────


def _empty_catalog():
    """Return a minimal fake Catalog with graph_many returning no nodes."""
    cat = MagicMock()
    cat.graph.return_value = {"nodes": [], "edges": []}
    cat.graph_many.return_value = {"nodes": [], "edges": []}
    return cat


# ── Malformed seed handling ───────────────────────────────────────────────────


def test_all_malformed_seeds_returns_empty_shape():
    """All-invalid seeds → empty result, no exception.

    The traverse tool silently drops unparseable seeds.  When all seeds
    are malformed the caller gets the empty-result dict, not a raise.
    This is the drop-silent contract the critique flagged as untested.
    """
    from nexus.mcp.core import traverse

    with patch("nexus.mcp.core._get_catalog", return_value=_empty_catalog()):
        result = traverse(
            seeds=["not-a-tumbler", "also::bad", "???"],
            link_types=["implements"],
        )

    assert isinstance(result, dict)
    assert result.get("tumblers") == []
    assert result.get("ids") == []
    assert result.get("collections") == []
    assert "error" not in result, (
        "All-malformed seeds must return empty shape, not an error key"
    )


def test_mixed_valid_and_malformed_seeds_drops_bad_silently():
    """Valid seed traverses; malformed ones are dropped without error key.

    The result is a partial traversal — we verify the catalog was called
    (meaning the valid seed reached it) and no exception surfaced.
    """
    from nexus.mcp.core import traverse

    cat = _empty_catalog()
    with patch("nexus.mcp.core._get_catalog", return_value=cat):
        result = traverse(
            seeds=["1.1", "NOT-A-TUMBLER", "1.2"],
            link_types=["cites"],
        )

    assert isinstance(result, dict)
    assert "tumblers" in result
    # Catalog must have been called — the valid seeds reached graph_many.
    assert cat.graph_many.called or cat.graph.called


def test_single_malformed_seed_returns_empty_not_exception():
    """Single malformed seed → empty result, catalog not invoked."""
    from nexus.mcp.core import traverse

    cat = _empty_catalog()
    with patch("nexus.mcp.core._get_catalog", return_value=cat):
        result = traverse(seeds="bad-seed-string", link_types=["implements"])

    assert isinstance(result, dict)
    assert result["tumblers"] == []
    # No catalog call — all seeds were dropped before graph dispatch.
    assert not cat.graph.called
    assert not cat.graph_many.called


# ── SC-16 mutual exclusion ────────────────────────────────────────────────────


def test_link_types_and_purpose_returns_error_key():
    """SC-16: link_types + purpose together → {"error": ...}, no raise."""
    from nexus.mcp.core import traverse

    result = traverse(
        seeds=["1.1"],
        link_types=["implements"],
        purpose="find-implementations",
    )
    assert "error" in result
    assert "both" in result["error"].lower()


# ── Unknown purpose ───────────────────────────────────────────────────────────


def test_unknown_purpose_returns_warning_key():
    """Unknown purpose alias → empty result with 'warning' key."""
    from nexus.mcp.core import traverse

    with patch("nexus.mcp.core._get_catalog", return_value=_empty_catalog()):
        result = traverse(seeds=["1.1"], purpose="not-a-registered-purpose")

    assert isinstance(result, dict)
    assert "warning" in result
    assert result.get("tumblers") == []


# ── Depth cap ─────────────────────────────────────────────────────────────────


def test_depth_clamped_to_max():
    """depth=99 is clamped to _TRAVERSE_MAX_DEPTH, not forwarded raw."""
    from nexus.mcp.core import traverse, _TRAVERSE_MAX_DEPTH

    cat = _empty_catalog()
    with patch("nexus.mcp.core._get_catalog", return_value=cat):
        traverse(seeds=["1.1"], link_types=["implements"], depth=99)

    # Inspect the call: depth must be ≤ _TRAVERSE_MAX_DEPTH.
    if cat.graph.called:
        _, kwargs = cat.graph.call_args
        assert kwargs.get("depth", 99) <= _TRAVERSE_MAX_DEPTH
    elif cat.graph_many.called:
        _, kwargs = cat.graph_many.call_args
        assert kwargs.get("depth", 99) <= _TRAVERSE_MAX_DEPTH


# ── Empty input ───────────────────────────────────────────────────────────────


def test_empty_seeds_list_returns_empty_shape():
    """traverse([]) → empty result without hitting the catalog."""
    from nexus.mcp.core import traverse

    cat = _empty_catalog()
    with patch("nexus.mcp.core._get_catalog", return_value=cat):
        result = traverse(seeds=[], link_types=["implements"])

    assert result == {"tumblers": [], "ids": [], "collections": []}
    assert not cat.graph.called
    assert not cat.graph_many.called


def test_empty_string_seed_returns_empty_shape():
    """traverse('') normalises to empty list → empty result."""
    from nexus.mcp.core import traverse

    cat = _empty_catalog()
    with patch("nexus.mcp.core._get_catalog", return_value=cat):
        result = traverse(seeds="", link_types=["implements"])

    assert result["tumblers"] == []
    assert not cat.graph.called


# ── Catalog unavailable ───────────────────────────────────────────────────────


def test_catalog_unavailable_returns_error_key():
    """When the catalog singleton is None, traverse returns {"error": ...}."""
    from nexus.mcp.core import traverse

    with patch("nexus.mcp.core._get_catalog", return_value=None):
        result = traverse(seeds=["1.1"], link_types=["implements"])

    assert "error" in result
