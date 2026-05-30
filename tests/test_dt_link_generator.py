# SPDX-License-Identifier: AGPL-3.0-or-later
"""P1.5 contracts for DEVONthink Layer B link generation (RDR-139).

Pins (real Catalog on tmp SQLite, fake DT client injected):
- 2 of 3 similarity neighbours catalog-known → exactly 2 ``relates`` edges;
  the unindexed neighbour is skipped with no error.
- re-run is idempotent (no new edges the second time).
- an explicit DT link to a pair already joined by similarity is deduped
  (not double-counted); a fresh DT-link pair creates one ``dt_link`` edge.
- DT unavailable → zero new edges, no exception (Gap 0 fallback).
"""

from __future__ import annotations

import pytest

from nexus.catalog.catalog import Catalog
from nexus.catalog.dt_link_generator import generate_dt_links


@pytest.fixture
def cat(tmp_path):
    d = tmp_path / "catalog"
    d.mkdir()
    return Catalog(d, d / ".catalog.db")


def _uri(uuid: str) -> str:
    return f"x-devonthink-item://{uuid}"


class _FakeDT:
    """Injectable stand-in for nexus.mcp_client.devonthink."""

    def __init__(self, *, available=True, similar=None, links=None):
        self._available = available
        self._similar = similar or []
        self._links = links or []
        self.classify_calls = 0

    def available(self, *, refresh=False):
        return self._available

    def dt_find_similar(self, uuid, *, limit=25, floor=0.0):
        return [n for n in self._similar if n["score"] >= floor]

    def dt_record_links(self, uuid):
        return self._links

    def dt_call(self, tool, args=None):
        self.classify_calls += 1
        return {}


@pytest.fixture
def indexed(cat):
    """Register a 'this' record plus two indexed DT neighbours; return ids."""
    owner = cat.register_owner("nexus", "repo", repo_hash="571b8edd")
    this = cat.register(owner, "this", content_type="paper", file_path="", source_uri=_uri("THIS"))
    a = cat.register(owner, "alpha", content_type="paper", file_path="", source_uri=_uri("A"))
    b = cat.register(owner, "beta", content_type="paper", file_path="", source_uri=_uri("B"))
    return cat, owner, this, a, b


def test_two_of_three_neighbours_known_make_two_edges(indexed):
    cat, _owner, this, a, b = indexed
    dt = _FakeDT(similar=[
        {"uuid": "A", "score": 0.9, "name": "alpha"},
        {"uuid": "B", "score": 0.8, "name": "beta"},
        {"uuid": "UNINDEXED", "score": 0.95, "name": "ghost"},
    ])
    counts = generate_dt_links(cat, this, "THIS", dt_client=dt)
    assert counts == {"similar": 2, "link": 0}
    # Verify two relates edges exist from `this`.
    out = cat.links_from(this, "relates")
    assert {str(e.to_tumbler) for e in out} == {str(a), str(b)}


def test_idempotent_on_rerun(indexed):
    cat, _owner, this, a, b = indexed
    dt = _FakeDT(similar=[{"uuid": "A", "score": 0.9, "name": "alpha"}])
    first = generate_dt_links(cat, this, "THIS", dt_client=dt)
    second = generate_dt_links(cat, this, "THIS", dt_client=dt)
    assert first == {"similar": 1, "link": 0}
    assert second == {"similar": 0, "link": 0}


def test_explicit_link_deduped_against_similarity(indexed):
    cat, _owner, this, a, b = indexed
    # A appears in BOTH similarity and explicit links → only the similarity edge counts.
    # B appears only as an explicit link → one dt_link edge.
    dt = _FakeDT(
        similar=[{"uuid": "A", "score": 0.9, "name": "alpha"}],
        links=[{"uuid": "A", "score": 1.0, "name": "alpha"}, {"uuid": "B", "score": 1.0, "name": "beta"}],
    )
    counts = generate_dt_links(cat, this, "THIS", dt_client=dt)
    assert counts == {"similar": 1, "link": 1}


def test_unavailable_makes_zero_edges(indexed):
    cat, _owner, this, _a, _b = indexed
    dt = _FakeDT(available=False, similar=[{"uuid": "A", "score": 0.9, "name": "alpha"}])
    counts = generate_dt_links(cat, this, "THIS", dt_client=dt)
    assert counts == {"similar": 0, "link": 0}
    assert cat.links_from(this) == []


def test_floor_filters_low_similarity(indexed):
    cat, _owner, this, a, b = indexed
    dt = _FakeDT(similar=[
        {"uuid": "A", "score": 0.9, "name": "alpha"},
        {"uuid": "B", "score": 0.3, "name": "beta"},
    ])
    counts = generate_dt_links(cat, this, "THIS", floor=0.5, dt_client=dt)
    assert counts == {"similar": 1, "link": 0}
