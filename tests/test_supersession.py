# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-2k66t.6 (2A): a hit whose owning documents are all superseded is annotated and demoted."""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from nexus import supersession
from nexus.context_annotations import hit_annotation_line
from nexus.scoring import apply_hybrid_scoring
from nexus.supersession import DISTANCE_PENALTY, SUPERSEDED_KEY, mark_superseded
from nexus.types import SearchResult


@pytest.fixture(autouse=True)
def _fresh_cache():
    supersession._clear()
    yield
    supersession._clear()


@dataclass
class _Link:
    from_tumbler: str
    to_tumbler: str
    created_at: str
    link_type: str = "supersedes"


@dataclass
class _Entry:
    title: str


class _Catalog:
    def __init__(self, links, titles=None, fail: str = ""):
        self.links = links
        self.titles = titles or {}
        self.fail = fail
        self.queries = 0
        self.resolves: list[list[str]] = []

    def link_query(self, *, link_type, limit, offset):
        assert link_type == "supersedes"
        self.queries += 1
        if self.fail == "links":
            raise RuntimeError("catalog down")
        return self.links[offset:offset + limit]

    def resolve_many(self, doc_ids):
        self.resolves.append(list(doc_ids))
        if self.fail == "titles":
            raise RuntimeError("catalog down")
        return {d: _Entry(self.titles[d]) for d in doc_ids if d in self.titles}


def _hit(rid: str, owners: list[str], dist: float = 0.3) -> SearchResult:
    return SearchResult(id=rid, content="x", distance=dist, collection="rdr__1-1__m__v1",
                        metadata={"_owners": owners})


def _owners(r: SearchResult) -> list[str]:
    return r.metadata["_owners"]


LINKS = [
    _Link("1.1.20", "1.1.10", "2026-09-04T20:58:50+00:00"),  # 20 supersedes 10
    _Link("1.1.30", "1.1.10", "2026-10-01T08:00:00+00:00"),  # 30 supersedes 10 too, later
    _Link("1.1.40", "1.1.11", "2026-08-01T00:00:00+00:00"),
]
TITLES = {"1.1.20": "RDR-020 old successor", "1.1.30": "RDR-030 Current Design", "1.1.40": "RDR-040"}


def test_a_hit_whose_owner_is_superseded_is_marked_and_demoted() -> None:
    cat = _Catalog(LINKS, TITLES)
    old, cur = _hit("a", ["1.1.10"]), _hit("b", ["1.1.99"])
    mark_superseded([old, cur], cat, owners_of=_owners, clock=lambda: 0.0)
    # the newest link names the successor
    assert old.metadata[SUPERSEDED_KEY] == ("RDR-030 Current Design", "2026-10-01")
    assert old.topic_boost == -DISTANCE_PENALTY
    assert SUPERSEDED_KEY not in cur.metadata and cur.topic_boost == 0.0
    assert cat.resolves == [["1.1.20", "1.1.30"]]


def test_a_chunk_a_current_document_still_holds_is_not_superseded() -> None:
    cat = _Catalog(LINKS, TITLES)
    shared = _hit("a", ["1.1.10", "1.1.99"])
    mark_superseded([shared], cat, owners_of=_owners, clock=lambda: 0.0)
    assert SUPERSEDED_KEY not in shared.metadata and shared.topic_boost == 0.0
    assert cat.resolves == [], "no title lookup when nothing on the page is superseded"


@pytest.mark.parametrize("hybrid", [False, True])
def test_the_superseded_hit_ranks_below_an_equal_current_one(hybrid: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("nexus.mcp_infra.get_collection_row", lambda name: None)  # no code collections here
    cat = _Catalog(LINKS, TITLES)
    old, cur, far = _hit("old", ["1.1.10"], 0.30), _hit("cur", ["1.1.99"], 0.30), _hit("far", ["1.1.98"], 0.60)
    ranked = apply_hybrid_scoring(mark_superseded([old, cur, far], cat, owners_of=_owners, clock=lambda: 0.0),
                                  hybrid, model_of=lambda _c: None)
    assert [r.id for r in ranked] == ["cur", "old", "far"]
    assert old.distance == 0.30, "the reported distance is the measured one"


def test_the_annotation_line_leads_with_the_successor() -> None:
    cat = _Catalog(LINKS, TITLES)
    old = _hit("a", ["1.1.11"])
    mark_superseded([old], cat, owners_of=_owners, clock=lambda: 0.0)
    assert hit_annotation_line(old.metadata) == "superseded by RDR-040 (2026-08-01)"
    assert hit_annotation_line({}) == ""


def test_an_unresolved_successor_title_falls_back_to_its_tumbler() -> None:
    cat = _Catalog(LINKS, {})
    old = _hit("a", ["1.1.11"])
    mark_superseded([old], cat, owners_of=_owners, clock=lambda: 0.0)
    assert old.metadata[SUPERSEDED_KEY] == ("1.1.40", "2026-08-01")


def test_the_link_set_is_cached_per_catalog_for_the_ttl() -> None:
    cat = _Catalog(LINKS, TITLES)
    t = [0.0]
    for _ in range(3):
        mark_superseded([_hit("a", ["1.1.10"])], cat, owners_of=_owners, clock=lambda: t[0])
    assert cat.queries == 1
    t[0] = supersession.TTL_S + 1
    mark_superseded([_hit("a", ["1.1.10"])], cat, owners_of=_owners, clock=lambda: t[0])
    assert cat.queries == 2
    other = _Catalog(LINKS, TITLES)
    mark_superseded([_hit("a", ["1.1.10"])], other, owners_of=_owners, clock=lambda: t[0])
    assert other.queries == 1


@pytest.mark.parametrize("fail", ["links", "titles"])
def test_a_failed_lookup_leaves_the_results_as_they_were(fail: str) -> None:
    cat = _Catalog(LINKS, TITLES, fail=fail)
    old = _hit("a", ["1.1.10"])
    assert mark_superseded([old], cat, owners_of=_owners, clock=lambda: 0.0) == [old]
    assert SUPERSEDED_KEY not in old.metadata and old.topic_boost == 0.0


def test_a_link_set_past_the_cap_is_not_used(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(supersession, "MAX_LINKS", 4)
    monkeypatch.setattr(supersession, "_PAGE", 2)
    many = [_Link(f"1.2.{i}", "1.1.10", "2026-01-01") for i in range(6)]
    cat = _Catalog(many, {})
    old = _hit("a", ["1.1.10"])
    mark_superseded([old], cat, owners_of=_owners, clock=lambda: 0.0)
    assert SUPERSEDED_KEY not in old.metadata and old.topic_boost == 0.0


def test_paging_reads_every_link_under_the_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(supersession, "_PAGE", 2)
    cat = _Catalog(LINKS, TITLES)
    old = _hit("a", ["1.1.11"])  # its link is on the second page
    mark_superseded([old], cat, owners_of=_owners, clock=lambda: 0.0)
    assert cat.queries == 2 and old.metadata[SUPERSEDED_KEY][0] == "RDR-040"


def test_no_catalog_or_no_owners_is_a_no_op() -> None:
    r = _hit("a", [])
    assert mark_superseded([r], None, owners_of=_owners) == [r]
    cat = _Catalog(LINKS, TITLES)
    mark_superseded([r], cat, owners_of=_owners, clock=lambda: 0.0)
    assert SUPERSEDED_KEY not in r.metadata
