# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for Catalog.graph_many node-cap invariant (RDR-078 SC-4).

Verifies that the _MAX_GRAPH_NODES = 500 cap is enforced correctly
across multi-seed traversal — no single seed or combined result exceeds
the cap, and the cap fires before processing remaining seeds.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from nexus.catalog.catalog import Catalog
from nexus.catalog.tumbler import Tumbler


def _make_node(tumbler_str: str) -> MagicMock:
    """Return a fake catalog node with a .tumbler attribute."""
    node = MagicMock()
    node.tumbler = Tumbler.parse(tumbler_str)
    return node


def _make_edge(from_str: str, to_str: str, link_type: str = "relates") -> MagicMock:
    edge = MagicMock()
    edge.from_tumbler = Tumbler.parse(from_str)
    edge.to_tumbler = Tumbler.parse(to_str)
    edge.link_type = link_type
    return edge


def _make_graph_result(node_count: int, seed_prefix: str) -> dict:
    """Return a fake graph() result with node_count distinct nodes."""
    nodes = [_make_node(f"1.{seed_prefix}.{i}") for i in range(node_count)]
    edges = [_make_edge(f"1.{seed_prefix}.0", f"1.{seed_prefix}.{i}") for i in range(1, min(node_count, 10))]
    return {"nodes": nodes, "edges": edges}


class TestGraphManyNodeCap:
    """graph_many must stop at _MAX_GRAPH_NODES across merged results."""

    def test_node_cap_fires_at_boundary(self, tmp_path: Path) -> None:
        """Two seeds each returning 300 nodes → merged result capped at 500.

        Both seeds are processed (300+300=600 without cap), but the node merge
        loop stops inserting once the cap is reached.  call_count == 2 because
        the cap is only hit *during* seed-2 merge, not before it starts.
        """
        Catalog.init(tmp_path)
        cat = Catalog(tmp_path, tmp_path / ".catalog.db")

        call_count = [0]

        def fake_graph(seed, **kwargs):
            prefix = call_count[0]
            call_count[0] += 1
            return _make_graph_result(300, str(prefix))

        seeds = [Tumbler.parse("1.1"), Tumbler.parse("1.2")]

        with patch.object(cat, "graph", side_effect=fake_graph):
            result = cat.graph_many(seeds, depth=1)

        assert len(result["nodes"]) <= Catalog._MAX_GRAPH_NODES, (
            f"graph_many returned {len(result['nodes'])} nodes, "
            f"expected ≤ {Catalog._MAX_GRAPH_NODES}"
        )
        assert call_count[0] == 2, "Both seeds must be dispatched (cap fires mid-merge)"

    def test_node_cap_short_circuits_seed(self, tmp_path: Path) -> None:
        """First seed alone fills the cap → second seed never dispatched."""
        Catalog.init(tmp_path)
        cat = Catalog(tmp_path, tmp_path / ".catalog.db")

        call_count = [0]

        def fake_graph(seed, **kwargs):
            prefix = call_count[0]
            call_count[0] += 1
            return _make_graph_result(500, str(prefix))  # fills cap exactly

        seeds = [Tumbler.parse("1.1"), Tumbler.parse("1.2"), Tumbler.parse("1.3")]

        with patch.object(cat, "graph", side_effect=fake_graph):
            result = cat.graph_many(seeds, depth=1)

        assert len(result["nodes"]) <= Catalog._MAX_GRAPH_NODES
        assert call_count[0] == 1, (
            "Once cap is full before the next seed, that seed must be skipped"
        )

    def test_node_cap_exact_boundary(self, tmp_path: Path) -> None:
        """Exactly 500 nodes → result == 500 (cap fires at >=, not >)."""
        Catalog.init(tmp_path)
        cat = Catalog(tmp_path, tmp_path / ".catalog.db")

        def fake_graph(seed, **kwargs):
            return _make_graph_result(500, "0")

        seeds = [Tumbler.parse("1.1")]
        with patch.object(cat, "graph", side_effect=fake_graph):
            result = cat.graph_many(seeds, depth=1)

        assert len(result["nodes"]) == 500

    def test_below_cap_returns_all_nodes(self, tmp_path: Path) -> None:
        """When total < 500, all nodes are returned untruncated."""
        Catalog.init(tmp_path)
        cat = Catalog(tmp_path, tmp_path / ".catalog.db")

        call_count = [0]

        def fake_graph(seed, **kwargs):
            prefix = call_count[0]
            call_count[0] += 1
            return _make_graph_result(100, str(prefix))

        seeds = [Tumbler.parse("1.1"), Tumbler.parse("1.2")]
        with patch.object(cat, "graph", side_effect=fake_graph):
            result = cat.graph_many(seeds, depth=1)

        assert len(result["nodes"]) == 200  # 2 × 100 disjoint nodes
        assert call_count[0] == 2           # both seeds processed

    def test_empty_seeds_returns_empty(self, tmp_path: Path) -> None:
        Catalog.init(tmp_path)
        cat = Catalog(tmp_path, tmp_path / ".catalog.db")

        result = cat.graph_many([], depth=1)
        assert result == {"nodes": [], "edges": []}

    def test_deduplication_across_seeds(self, tmp_path: Path) -> None:
        """Nodes shared by two seeds appear only once in merged result."""
        Catalog.init(tmp_path)
        cat = Catalog(tmp_path, tmp_path / ".catalog.db")

        shared_node = _make_node("1.1.1")

        def fake_graph(seed, **kwargs):
            return {"nodes": [shared_node], "edges": []}

        seeds = [Tumbler.parse("1.1"), Tumbler.parse("1.2")]
        with patch.object(cat, "graph", side_effect=fake_graph):
            result = cat.graph_many(seeds, depth=1)

        assert len(result["nodes"]) == 1, (
            "Shared node discovered from two seeds must appear only once"
        )
