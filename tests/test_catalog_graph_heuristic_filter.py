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

import pytest

from nexus.catalog.catalog import Catalog
from nexus.catalog.catalog_links import (
    _HEURISTIC_LINK_TYPES,
    _filter_link_types,
)
from nexus.catalog.tumbler import Tumbler


@pytest.fixture(autouse=True)
def _git_identity(monkeypatch):
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@test.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@test.invalid")


def _make_catalog(tmp_path: Path) -> Catalog:
    cat_dir = tmp_path / "catalog"
    Catalog.init(cat_dir)
    return Catalog(cat_dir, cat_dir / ".catalog.db")


# ── _filter_link_types pure helper ────────────────────────────────────────


class TestFilterLinkTypesHelper:
    def test_explicit_link_types_pass_through(self) -> None:
        """A caller's explicit list wins; the helper trusts the caller
        knows whether they want heuristic edges in the result.
        """
        result = _filter_link_types(
            ["cites", "implements-heuristic"], "",
            include_heuristic=False,
        )
        assert result == ["cites", "implements-heuristic"]

    def test_explicit_single_link_type_passes_through(self) -> None:
        result = _filter_link_types(
            None, "implements-heuristic",
            include_heuristic=False,
        )
        assert result == ["implements-heuristic"]

    def test_no_filter_default_excludes_heuristic(self) -> None:
        """nexus-6ppk primary contract: when no explicit filter is
        given AND ``include_heuristic`` is False, the result is the
        full known set MINUS heuristic types.
        """
        result = _filter_link_types(
            None, "", include_heuristic=False,
        )
        assert result is not None
        assert "implements-heuristic" not in result
        # Sanity: meaningful types are present.
        for must_have in (
            "cites", "implements", "relates", "supersedes",
        ):
            assert must_have in result, f"{must_have!r} missing"

    def test_include_heuristic_true_returns_no_filter(self) -> None:
        """``include_heuristic=True`` with no explicit types means
        the caller wants EVERY type including heuristic. Returning
        None signals the BFS to skip the link-type filter entirely.
        """
        result = _filter_link_types(
            None, "", include_heuristic=True,
        )
        assert result is None


# ── End-to-end: Catalog.graph default-excludes heuristic ─────────────────


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
        assert str(heuristic_target) not in node_tumblers, (
            f"implements-heuristic neighbor leaked into the default "
            f"graph traversal; reverting the nexus-6ppk default-"
            f"exclude lets the heuristic flood dominate the result"
        )

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
        assert str(heuristic_a) not in node_tumblers
        assert str(heuristic_b) not in node_tumblers

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


class TestHeuristicTokenSet:
    def test_heuristic_set_pinned(self) -> None:
        """Lock the heuristic-link-type set so adding a new
        heuristic link type to the catalog forces a deliberate
        decision about whether it should be default-excluded.
        """
        assert _HEURISTIC_LINK_TYPES == frozenset({"implements-heuristic"})
