# SPDX-License-Identifier: AGPL-3.0-or-later
"""TDD tests for ``Catalog.graph_many`` — RDR-078 P3 (nexus-05i.5).

Implementation strategy is fan-out per ``link_type`` (Option A in the
bead description). Per-seed × per-type BFS, results merged with:

  * Node dedup keyed on ``str(tumbler)`` (first-seen wins);
    ``seed_origin: list[str]`` accumulates every seed that reached
    the node across the merged frontier.
  * Edge dedup on ``(from, to, link_type)`` triples.
  * ``_MAX_GRAPH_NODES = 500`` cap on the **merged** frontier across
    all fan-out calls; partial results returned, warning logged.
"""
from __future__ import annotations

from pathlib import Path

import pytest


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture()
def catalog(tmp_path: Path):
    from nexus.catalog.catalog import Catalog

    cat_dir = tmp_path / "catalog"
    cat_dir.mkdir(parents=True, exist_ok=True)
    return Catalog(catalog_dir=cat_dir, db_path=tmp_path / "catalog.db")


def _seed_diamond(cat):
    """Build the diamond graph used by node/edge dedup tests::

           A ──implements──> B ──implements──> D
           │                                   ▲
           └──cites────────> C ────relates────┘

    Returns the four tumblers (a, b, c, d).
    """
    owner = cat.register_owner("p", "test")
    a = cat.register(owner, "A")
    b = cat.register(owner, "B")
    c = cat.register(owner, "C")
    d = cat.register(owner, "D")
    cat.link(a, b, "implements", created_by="t")
    cat.link(a, c, "cites", created_by="t")
    cat.link(b, d, "implements", created_by="t")
    cat.link(c, d, "relates", created_by="t")
    return a, b, c, d


# ── Node dedup + seed_origin (SC-4) ────────────────────────────────────────


def test_graph_many_node_dedup_first_seen_wins(catalog) -> None:
    """Two seeds reaching the same node return one node, not two."""
    a, b, c, d = _seed_diamond(catalog)

    result = catalog.graph_many(
        seeds=[a, c],
        depth=2,
        link_types=["implements", "relates"],
        direction="out",
    )
    titles = [n["title"] for n in result["nodes"]]
    # D appears at most once — node dedup by tumbler.
    assert titles.count("D") == 1


def test_graph_many_seed_origin_accumulates(catalog) -> None:
    """A node reached from multiple seeds carries every seed in its
    ``seed_origin`` list."""
    a, b, c, d = _seed_diamond(catalog)

    result = catalog.graph_many(
        seeds=[a, c],
        depth=2,
        link_types=["implements", "relates"],
        direction="out",
    )
    d_node = next(n for n in result["nodes"] if n["title"] == "D")
    assert "seed_origin" in d_node
    origins = set(d_node["seed_origin"])
    assert str(a) in origins
    assert str(c) in origins


def test_graph_many_seed_node_self_origin(catalog) -> None:
    """Each seed appears in its own ``seed_origin`` list."""
    a, *_ = _seed_diamond(catalog)
    result = catalog.graph_many(
        seeds=[a], depth=1, link_types=["implements"], direction="out",
    )
    a_node = next(n for n in result["nodes"] if n["tumbler"] == str(a))
    assert str(a) in a_node["seed_origin"]


# ── Edge dedup (SC-4) ──────────────────────────────────────────────────────


def test_graph_many_edge_dedup(catalog) -> None:
    """``(from, to, link_type)`` triples dedup across seed traversals."""
    a, b, c, d = _seed_diamond(catalog)
    result = catalog.graph_many(
        seeds=[a, b],  # b is already reachable from a
        depth=2,
        link_types=["implements"],
        direction="out",
    )
    # Each edge appears at most once.
    triples = [
        (str(e.from_tumbler), str(e.to_tumbler), e.link_type)
        for e in result["edges"]
    ]
    assert len(triples) == len(set(triples))


# ── Cap on merged frontier (SC-4) ──────────────────────────────────────────


def test_graph_many_max_nodes_cap_short_circuits(catalog, monkeypatch) -> None:
    """Cap applies to the **merged** node count, not per fan-out call."""
    from nexus.catalog import catalog as catalog_mod

    # Build a wide chain longer than the test cap.
    monkeypatch.setattr(catalog_mod.Catalog, "_MAX_GRAPH_NODES", 5)

    owner = catalog.register_owner("p", "test")
    nodes = [catalog.register(owner, f"N{i}") for i in range(10)]
    for i in range(9):
        catalog.link(nodes[i], nodes[i + 1], "implements", created_by="t")

    result = catalog.graph_many(
        seeds=[nodes[0]],
        depth=10,
        link_types=["implements"],
        direction="out",
    )
    # Cap = 5 → node count must not exceed 5 on the merged frontier.
    assert len(result["nodes"]) <= 5


# ── direction handling ────────────────────────────────────────────────────


def test_graph_many_direction_in(catalog) -> None:
    """``direction='in'`` walks inbound edges only."""
    a, b, c, d = _seed_diamond(catalog)
    # Walk inbound from D — should reach B and C (and their parents).
    result = catalog.graph_many(
        seeds=[d],
        depth=2,
        link_types=["implements", "relates"],
        direction="in",
    )
    titles = {n["title"] for n in result["nodes"]}
    assert "D" in titles
    assert "B" in titles  # via implements (D ← B)
    assert "C" in titles  # via relates (D ← C)


def test_graph_many_empty_link_types_returns_seeds_only(catalog) -> None:
    """No link types to fan out → just the seed entries themselves."""
    a, *_ = _seed_diamond(catalog)
    result = catalog.graph_many(
        seeds=[a], depth=2, link_types=[], direction="out",
    )
    titles = [n["title"] for n in result["nodes"]]
    assert titles == ["A"]
    assert result["edges"] == []
