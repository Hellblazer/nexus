# SPDX-License-Identifier: AGPL-3.0-or-later
"""Gap-0 fallback suite: every DT layer degrades EXACTLY when DT is absent (RDR-139).

The contract (RDR §Optionality and Fallback Contract) is per-layer and EXACT,
not "no crash":

- **Layer B (linking)**: zero new edges — the catalog edge set after a run with
  ``available()=False`` equals the edge set before; no ``created_by=dt_*`` rows.
- **Layer F (write-back)**: no DT-side mutation; index/enrich result unchanged,
  exit 0.  *(Added when P1.7 Layer F lands — nexus-x70wg.)*

Integration over mocks: a real ``Catalog`` on tmp SQLite; only the DT client's
``available()`` is forced False (the genuine fallback trigger).
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


class _UnavailableDT:
    """DT client whose server is down. Any read/write helper would be a bug to call."""

    def available(self, *, refresh=False):
        return False

    def dt_find_similar(self, *a, **k):  # pragma: no cover - must not be reached
        raise AssertionError("dt_find_similar called despite available()=False")

    def dt_record_links(self, *a, **k):  # pragma: no cover
        raise AssertionError("dt_record_links called despite available()=False")

    def dt_call(self, *a, **k):  # pragma: no cover
        raise AssertionError("dt_call called despite available()=False")


@pytest.fixture
def indexed(cat):
    owner = cat.register_owner("nexus", "repo", repo_hash="571b8edd")
    this = cat.register(
        owner, "this", content_type="paper", file_path="",
        source_uri="x-devonthink-item://THIS",
    )
    other = cat.register(
        owner, "other", content_type="paper", file_path="",
        source_uri="x-devonthink-item://A",
    )
    # A pre-existing non-DT edge: the fallback must leave it untouched.
    cat.link_if_absent(this, other, "relates", created_by="auto_linker")
    return cat, this, other


def _edge_snapshot(cat: Catalog, tumbler):
    return sorted(
        (str(e.from_tumbler), str(e.to_tumbler), e.link_type, e.created_by)
        for e in cat.links_from(tumbler)
    )


class TestLayerBFallback:
    """Layer B: DT absent → zero new edges; pre-existing edge set unchanged."""

    def test_zero_new_edges_when_unavailable(self, indexed):
        cat, this, _other = indexed
        before = _edge_snapshot(cat, this)
        counts = generate_dt_links(cat, this, "THIS", dt_client=_UnavailableDT())
        after = _edge_snapshot(cat, this)
        assert counts == {"similar": 0, "link": 0}
        assert after == before  # EXACT: edge set unchanged, not merely "no crash"

    def test_no_dt_attributed_rows_added(self, indexed):
        cat, this, _other = indexed
        generate_dt_links(cat, this, "THIS", dt_client=_UnavailableDT())
        assert cat.link_query(created_by="dt_similar") == []
        assert cat.link_query(created_by="dt_link") == []

    def test_no_read_helper_invoked_when_unavailable(self, indexed):
        # _UnavailableDT raises if any read helper is called; reaching here proves
        # the available() gate short-circuits before any DT read.
        cat, this, _other = indexed
        generate_dt_links(cat, this, "THIS", dt_client=_UnavailableDT())


# Layer F fallback (no DT-side mutation, index/enrich unchanged, exit 0) is added
# with P1.7 write-back (nexus-x70wg). Placed here intentionally so the Gap-0
# contract lives in one suite — see module docstring.
