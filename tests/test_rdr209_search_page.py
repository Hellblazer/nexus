# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-209 Phase 0b, client half (nexus-1gdv0.1.5): ``search_page`` sends the page to the engine.

The engine's ``POST /v1/memory/search`` takes ``limit`` and ``offset`` and answers
``{rows, matching_total}``, stamping only the rows it returns (a7adca21b). ``search()`` keeps
sending no limit and gets the bare list back, unchanged.

These tests mint no tenant: the request the client builds and the answers it reads are checked
against a stand-in ``_post``. The engine's own answer, and that only the page is stamped, are
checked against the substrate in ``tests/test_mcp_server.py::test_memory_search_pagination``, a
tenant test that already existed, so the page costs no new tenant (tests/storage_touch_ceiling.toml).
"""
from __future__ import annotations

import pytest

from nexus.db.t2 import T2Database
from nexus.db.t2.http_memory_store import HttpMemoryStore


def _stub(answer) -> tuple[HttpMemoryStore, list[tuple[str, dict]]]:
    store = HttpMemoryStore.__new__(HttpMemoryStore)
    sent: list[tuple[str, dict]] = []

    def post(path, payload, **kw):
        sent.append((path, dict(payload)))
        return answer

    store._post = post  # type: ignore[method-assign]
    return store, sent


def test_the_page_goes_to_the_engine_and_its_answer_is_read():
    store, sent = _stub({"rows": [{"id": 3, "project": "p", "title": "c", "content": "x"}], "matching_total": 5})

    rows, total = store.search_page("quillwort", project="p", limit=2, offset=4)

    assert sent == [("/v1/memory/search",
                     {"query": "quillwort", "access": "track", "limit": 2, "offset": 4, "project": "p"})]
    assert ([r["title"] for r in rows], total) == (["c"], 5)


def test_search_without_a_page_still_sends_no_limit():
    store, sent = _stub([{"id": 1, "project": "p", "title": "a", "content": "x"}])

    assert [r["title"] for r in store.search("quillwort", project="p")] == ["a"]
    assert "limit" not in sent[-1][1] and "offset" not in sent[-1][1]


@pytest.mark.parametrize(("limit", "offset"), [(0, 0), (-1, 0), (5, -1)])
def test_a_bad_page_is_refused_before_any_request(limit, offset):
    store, sent = _stub({"rows": [], "matching_total": 0})
    with pytest.raises(ValueError):
        store.search_page("quillwort", project="p", limit=limit, offset=offset)
    assert sent == []


def test_the_t2_database_passes_the_page_through():
    asked: list[tuple] = []

    class _Memory:
        def search_page(self, query, project=None, limit=20, offset=0, access="track"):
            asked.append((query, project, limit, offset, access))
            return [], 0

    db = T2Database.__new__(T2Database)
    db.memory = _Memory()  # type: ignore[assignment]

    assert db.search_page("bladderwrack", project="p", limit=2, offset=1, access="silent") == ([], 0)
    assert asked == [("bladderwrack", "p", 2, 1, "silent")]
