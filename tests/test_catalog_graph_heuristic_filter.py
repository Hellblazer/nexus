# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-6ppk: ``Catalog.graph()`` and ``graph_many()`` default-
exclude ``implements-heuristic`` edges so the auto-emitted heuristic
flood (66% of the 2026-05-08 prod link graph; 562-660 inbound on
high-traffic infrastructure RDRs) doesn't drown out hand-curated
edges. Callers wanting the heuristic edges opt back in via
``include_heuristic=True``.
"""
from __future__ import annotations

from pathlib import Path

from tests._catalog_fixture_ops import ActiveCatalog

# nexus-i711w terminal deletion: TestFilterLinkTypesHelper (4 tests) and
# TestHeuristicTokenSet retired WITH nexus.catalog.catalog_links — the
# ``_filter_link_types`` pure helper died with the local BFS; the
# default-exclude contract itself is server-side now (nexus-ybj1b) and
# stays pinned end-to-end by TestGraphDefaultExcludesHeuristic below.


def _make_catalog(tmp_path: Path) -> ActiveCatalog:
    """Facade over the live (service) catalog; needs no local init.

    nexus-aqbrk: link-graph filtering is substrate-independent behaviour, so
    this file seeds through the same factories the graph reader uses.
    """
    return ActiveCatalog()


# ── End-to-end: Catalog.graph default-excludes heuristic ─────────────────


def _assert_heuristic_excluded(node_tumblers: set[str], heuristic: object) -> None:
    """Assert the default-exclude — now unconditional on both substrates.

    nexus-ybj1b RESOLVED 2026-07-26. This used to branch: SQLite asserted the
    real expectation while the SERVICE arm asserted the INVERSE, because the
    flag never reached the query. http_catalog_client sent
    ``include_heuristic`` with the comment "forwarded to service for future
    support; currently informational", and CatalogHandler.handleTraverse read
    only ``link_types`` — so the default did NOT exclude and the opt-in was a
    no-op in both directions, putting the heuristic flood back on (66% of the
    2026-05-08 production link graph).

    The server honours it now, so the branch is gone. Asserting the inverse
    rather than xfail is what made the fix fail loudly here instead of going
    quietly green.
    """
    assert str(heuristic) not in node_tumblers, (
        "implements-heuristic neighbor leaked into the default graph "
        "traversal; reverting the nexus-6ppk default-exclude lets the "
        "heuristic flood dominate the result"
    )


class TestGraphDefaultExcludesHeuristic:
    def test_graph_default_skips_heuristic_neighbor(self, tmp_path: Path) -> None:
        """Build a catalog with one ``cites`` and one
        ``implements-heuristic`` edge from the seed; default
        ``cat.graph(seed)`` returns only the ``cites`` neighbor.
        Reverting the default-exclude makes the heuristic neighbor
        appear in the result.
        """
        cat = _make_catalog(tmp_path)
        owner = cat.register_owner("nexus", "repo", repo_hash="abab1234")
        seed = cat.register(
            owner, "Seed", content_type="rdr", file_path="docs/rdr/seed.md",
        )
        cited = cat.register(
            owner, "Cited", content_type="rdr",
            file_path="docs/rdr/cited.md",
        )
        heuristic_target = cat.register(
            owner, "HeuristicMatch", content_type="code",
            file_path="src/heuristic.py",
        )
        cat.link(seed, cited, "cites", created_by="test")
        cat.link(
            seed, heuristic_target, "implements-heuristic",
            created_by="index_hook",
        )

        result = cat.graph(seed, depth=1)

        node_tumblers = {
            str(n.tumbler) if hasattr(n, "tumbler") else str(n)
            for n in result["nodes"]
        }
        # Seed always present.
        assert str(seed) in node_tumblers
        # Cites neighbor present (default-allowed type).
        assert str(cited) in node_tumblers
        # Heuristic neighbor MUST be absent (default-excluded).
        _assert_heuristic_excluded(node_tumblers, heuristic_target)

    def test_graph_include_heuristic_returns_heuristic_neighbor(
        self, tmp_path: Path,
    ) -> None:
        """Opt back in: ``cat.graph(seed, include_heuristic=True)``
        returns the heuristic neighbor. Audit / debug consumers use
        this path.
        """
        cat = _make_catalog(tmp_path)
        owner = cat.register_owner("nexus", "repo", repo_hash="abab1234")
        seed = cat.register(
            owner, "Seed", content_type="rdr", file_path="docs/rdr/seed.md",
        )
        target = cat.register(
            owner, "Target", content_type="code",
            file_path="src/target.py",
        )
        cat.link(seed, target, "implements-heuristic", created_by="hook")

        result = cat.graph(seed, depth=1, include_heuristic=True)
        node_tumblers = {
            str(n.tumbler) if hasattr(n, "tumbler") else str(n)
            for n in result["nodes"]
        }
        assert str(target) in node_tumblers

    def test_graph_explicit_link_type_overrides_default(
        self, tmp_path: Path,
    ) -> None:
        """When the caller passes
        ``link_type="implements-heuristic"`` explicitly, the
        heuristic neighbor IS returned (the caller knows what they
        asked for).
        """
        cat = _make_catalog(tmp_path)
        owner = cat.register_owner("nexus", "repo", repo_hash="abab1234")
        seed = cat.register(
            owner, "Seed", content_type="rdr", file_path="docs/rdr/seed.md",
        )
        target = cat.register(
            owner, "Target", content_type="code",
            file_path="src/target.py",
        )
        cat.link(seed, target, "implements-heuristic", created_by="hook")

        result = cat.graph(
            seed, depth=1, link_type="implements-heuristic",
        )
        node_tumblers = {
            str(n.tumbler) if hasattr(n, "tumbler") else str(n)
            for n in result["nodes"]
        }
        assert str(target) in node_tumblers

    def test_graph_many_inherits_default_exclude(
        self, tmp_path: Path,
    ) -> None:
        """``graph_many`` propagates the same default; multi-seed
        traversal also skips heuristic neighbors unless opted in.
        """
        cat = _make_catalog(tmp_path)
        owner = cat.register_owner("nexus", "repo", repo_hash="abab1234")
        seed_a = cat.register(
            owner, "SeedA", content_type="rdr",
            file_path="docs/rdr/a.md",
        )
        seed_b = cat.register(
            owner, "SeedB", content_type="rdr",
            file_path="docs/rdr/b.md",
        )
        heuristic_a = cat.register(
            owner, "HeurA", content_type="code",
            file_path="src/a.py",
        )
        heuristic_b = cat.register(
            owner, "HeurB", content_type="code",
            file_path="src/b.py",
        )
        cat.link(seed_a, heuristic_a, "implements-heuristic", created_by="hook")
        cat.link(seed_b, heuristic_b, "implements-heuristic", created_by="hook")

        # Default: heuristic neighbors absent.
        result = cat.graph_many([seed_a, seed_b], depth=1)
        node_tumblers = {
            str(n.tumbler) if hasattr(n, "tumbler") else str(n)
            for n in result["nodes"]
        }
        _assert_heuristic_excluded(node_tumblers, heuristic_a)
        _assert_heuristic_excluded(node_tumblers, heuristic_b)

        # Opt-in: both heuristic neighbors present.
        result_opt = cat.graph_many(
            [seed_a, seed_b], depth=1, include_heuristic=True,
        )
        node_tumblers_opt = {
            str(n.tumbler) if hasattr(n, "tumbler") else str(n)
            for n in result_opt["nodes"]
        }
        assert str(heuristic_a) in node_tumblers_opt
        assert str(heuristic_b) in node_tumblers_opt
