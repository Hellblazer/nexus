# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-209 Phase 0b, client half (nexus-1gdv0.1.5): ``search_page`` sends the page to the engine.

The engine's ``POST /v1/memory/search`` takes ``limit`` and ``offset`` and answers
``{rows, matching_total}``, stamping only the rows it returns (a7adca21b). ``search()`` keeps
sending no limit and gets the bare list back, unchanged.
"""
from __future__ import annotations

import pytest

from nexus.db.t2 import T2Database

pytestmark = pytest.mark.t2_tenant

_PROJECT = "rdr209-search-page"


@pytest.fixture
def store(tmp_path):
    db = T2Database(tmp_path / "t2.db")
    for i in range(5):
        db.memory.put(project=_PROJECT, title=f"page-{i}", content=f"quillwort finding number {i}")
    yield db.memory
    db.close()


def _spy_posts(store, monkeypatch) -> list[dict]:
    sent: list[dict] = []
    real = store._post

    def spy(path, payload=None, **kw):
        if path == "/v1/memory/search":
            sent.append(dict(payload or {}))
        return real(path, payload, **kw)

    monkeypatch.setattr(store, "_post", spy)
    return sent


def test_a_page_and_the_total_come_from_the_engine(store, monkeypatch):
    sent = _spy_posts(store, monkeypatch)

    rows, total = store.search_page("quillwort", project=_PROJECT, limit=2, offset=0)

    assert total == 5
    assert len(rows) == 2
    assert sent[-1]["limit"] == 2 and sent[-1]["offset"] == 0, "the page must reach the engine"
    rest, total_again = store.search_page("quillwort", project=_PROJECT, limit=2, offset=4)
    assert total_again == 5 and len(rest) == 1
    seen = {r["title"] for r in rows} | {r["title"] for r in rest}
    assert len(seen) == 3, "pages do not overlap"


def test_a_page_past_the_end_is_empty_and_still_carries_the_total(store):
    assert store.search_page("quillwort", project=_PROJECT, limit=3, offset=10) == ([], 5)


def test_search_without_a_page_still_sends_no_limit_and_returns_every_match(store, monkeypatch):
    sent = _spy_posts(store, monkeypatch)

    rows = store.search("quillwort", project=_PROJECT)

    assert len(rows) == 5
    assert "limit" not in sent[-1] and "offset" not in sent[-1]


def test_a_stopword_only_query_still_raises_value_error(store):
    with pytest.raises(ValueError):
        store.search_page("and", project=_PROJECT, limit=5, offset=0)


@pytest.mark.parametrize(("limit", "offset"), [(0, 0), (-1, 0), (5, -1)])
def test_a_bad_page_is_refused_before_any_request(store, monkeypatch, limit, offset):
    sent = _spy_posts(store, monkeypatch)
    with pytest.raises(ValueError):
        store.search_page("quillwort", project=_PROJECT, limit=limit, offset=offset)
    assert sent == []


def test_the_t2_database_passes_the_page_through(tmp_path):
    db = T2Database(tmp_path / "t2.db")
    try:
        for i in range(3):
            db.memory.put(project=_PROJECT, title=f"pass-{i}", content="bladderwrack note")
        rows, total = db.search_page("bladderwrack", project=_PROJECT, limit=2, offset=0)
        assert total == 3 and len(rows) == 2
    finally:
        db.close()


def test_only_the_rows_on_the_page_are_stamped(store):
    before = {r["title"]: r["access_count"] for r in store.search("quillwort", project=_PROJECT, access="silent")}

    page, _ = store.search_page("quillwort", project=_PROJECT, limit=2, offset=2)

    after = {r["title"]: r["access_count"] for r in store.search("quillwort", project=_PROJECT, access="silent")}
    paged = {r["title"] for r in page}
    assert len(paged) == 2
    assert {t for t in after if after[t] == before[t] + 1} == paged
    assert all(after[t] == before[t] for t in after if t not in paged)
