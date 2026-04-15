# SPDX-License-Identifier: AGPL-3.0-or-later
"""TDD tests for the purpose registry resolver — RDR-078 P3 (nexus-05i.5)."""
from __future__ import annotations

import logging
from pathlib import Path

import pytest


def test_purpose_resolves_to_link_types() -> None:
    """``find-implementations`` resolves to ``[implements, implements-heuristic]``."""
    from nexus.plans.purposes import resolve_purpose

    out = resolve_purpose("find-implementations")
    assert out == ["implements", "implements-heuristic"]


def test_purpose_unknown_purpose_returns_empty() -> None:
    """An unknown purpose name returns an empty list (callers decide
    whether that's an error)."""
    from nexus.plans.purposes import resolve_purpose

    assert resolve_purpose("not-a-real-purpose") == []


def test_purpose_unknown_link_type_warn_and_drop(caplog) -> None:
    """A purpose whose YAML lists a not-yet-implemented link type
    drops the unknown entries and logs ``purpose_unknown_link_type``;
    known entries pass through."""
    from nexus.plans.purposes import resolve_purpose

    fake_yaml = {
        "speculative": {
            "description": "x",
            "link_types": [
                "implements",            # known
                "semantic-implements",   # not yet — drop with warning
                "supersedes",            # known
            ],
        },
    }
    with caplog.at_level(logging.WARNING):
        out = resolve_purpose(
            "speculative", _registry_override=fake_yaml,
            _known_link_types_override={
                "implements", "implements-heuristic", "cites",
                "supersedes", "quotes", "relates", "comments",
            },
        )
    assert out == ["implements", "supersedes"]
    assert any("purpose_unknown_link_type" in r.message for r in caplog.records)
    assert any("semantic-implements" in r.message for r in caplog.records)


def test_purpose_registry_ships_required_keys() -> None:
    """The shipped ``purposes.yml`` contains the six purposes named
    by the bead description."""
    import yaml

    path = (
        Path(__file__).resolve().parents[1]
        / "nx" / "plans" / "purposes.yml"
    )
    assert path.exists(), f"missing {path}"
    data = yaml.safe_load(path.read_text())
    for required in (
        "find-implementations", "decision-evolution", "reference-chain",
        "documentation-for", "soft-relations", "all-implementations",
    ):
        assert required in data, f"missing purpose: {required}"
        assert "link_types" in data[required]
        assert data[required]["link_types"]


def test_purpose_resolve_hits_known_catalog_link_types() -> None:
    """All shipped purposes resolve to non-empty subsets of the
    catalog's known link type set when no overrides are passed."""
    from nexus.plans.purposes import resolve_purpose

    for name in (
        "find-implementations", "decision-evolution", "reference-chain",
        "documentation-for", "soft-relations", "all-implementations",
    ):
        assert resolve_purpose(name), (
            f"purpose {name} resolved to empty; check known link types"
        )
