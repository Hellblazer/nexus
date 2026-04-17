# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shape-coverage contract for ``tests/fixtures/name_canaries.py``.

RDR-087 Phase 3.1 (nexus-yi4b.3.1). The canary fixture is the foundation
of Probe 3a (``nx doctor --check=search``): miss a shape class here and
the canary misses the bug class. These tests pin the shape enumeration
so a future refactor can't silently drop a category.
"""
from __future__ import annotations

import pytest


# ── Surface literals ──────────────────────────────────────────────────────────

_VALID_SURFACES = {"resolve_corpus", "rdr_resolve", "resolve_span"}


@pytest.fixture(scope="module")
def canaries():
    from nexus.name_canaries import NAME_CANARIES

    return NAME_CANARIES


# ── Shape-and-load contract ───────────────────────────────────────────────────


class TestFixtureLoads:
    def test_import_resolves(self) -> None:
        """Module imports without error."""
        from nexus import name_canaries

        assert hasattr(name_canaries, "NAME_CANARIES")

    def test_list_non_empty(self, canaries) -> None:
        assert len(canaries) >= 6

    def test_entries_parse(self, canaries) -> None:
        """Every entry is a ``NameCanary`` with the right field shape."""
        from nexus.name_canaries import NameCanary

        for c in canaries:
            assert isinstance(c, NameCanary)
            assert isinstance(c.name, str) and c.name
            assert isinstance(c.expected_surface, frozenset) and c.expected_surface
            assert isinstance(c.shape_note, str) and c.shape_note

    def test_surfaces_are_valid(self, canaries) -> None:
        """Every entry's expected_surface is a subset of the known surface set."""
        for c in canaries:
            assert c.expected_surface <= _VALID_SURFACES, (
                f"canary {c.name!r} routes to unknown surface(s): "
                f"{c.expected_surface - _VALID_SURFACES}"
            )

    def test_names_unique(self, canaries) -> None:
        """Duplicate canary names would mask shape-coverage gaps."""
        names = [c.name for c in canaries]
        assert len(names) == len(set(names))


# ── Category coverage (one canary per shape class) ────────────────────────────


class TestShapeCoverage:
    """Each historical incident class must have at least one canary."""

    def test_multi_hyphen_corpus(self, canaries) -> None:
        """nexus-rc45 shape: ``art-grossberg-papers`` — multi-hyphen corpus."""
        matches = [
            c for c in canaries
            if "resolve_corpus" in c.expected_surface and c.name.count("-") >= 2
        ]
        assert matches, "need ≥1 multi-hyphen resolve_corpus canary (nexus-rc45)"

    def test_mixed_case_rdr(self, canaries) -> None:
        """nexus-51j shape: ``73`` / ``RDR-073`` / ``rdr-73``."""
        matches = [
            c for c in canaries
            if "rdr_resolve" in c.expected_surface
        ]
        assert len(matches) >= 2, (
            "need ≥2 rdr_resolve canaries covering bare-number + mixed-case (nexus-51j)"
        )

    def test_hash_suffixed(self, canaries) -> None:
        """Hash-suffixed name shape: ``ART-8c2e74c0``."""
        import re

        matches = [
            c for c in canaries
            if re.search(r"-[0-9a-f]{8}\b", c.name)
        ]
        assert matches, "need ≥1 hash-suffixed canary"

    def test_dot_bearing(self, canaries) -> None:
        """Dot-bearing shape: ``nexus-qo0.1`` (bead-id-like)."""
        matches = [c for c in canaries if "." in c.name]
        assert matches, "need ≥1 dot-bearing canary"

    def test_long_name(self, canaries) -> None:
        """Long-name edge: >64 chars — resolver truncation bugs."""
        matches = [c for c in canaries if len(c.name) > 64]
        assert matches, "need ≥1 >64-char canary"

    def test_prefix_broadcast(self, canaries) -> None:
        """nexus-7ay class: bare meta-name (e.g., ``docs``) broadcasts across prefix."""
        matches = [
            c for c in canaries
            if c.name in {"code", "docs", "knowledge", "rdr"}
            and "resolve_corpus" in c.expected_surface
        ]
        assert matches, "need ≥1 prefix-broadcast canary (nexus-7ay class)"
