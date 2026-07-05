# SPDX-License-Identifier: AGPL-3.0-or-later
"""persist_assignments batch path (nexus-71988): one POST per <=1000 rows
via /assignments/assign_many, with 404 fallback to the per-row loop for
engines predating v0.1.24."""

from __future__ import annotations

import pytest

from nexus.db.t2.http_taxonomy_store import HttpTaxonomyStore


def _rows(n: int, by: str = "centroid") -> list[dict]:
    return [
        {"doc_id": f"d{i}", "topic_id": 7, "assigned_by": by}
        for i in range(n)
    ]


class TestPersistAssignmentsBatch:
    def test_single_post_for_small_batch(self, monkeypatch) -> None:
        store = HttpTaxonomyStore.__new__(HttpTaxonomyStore)
        posts: list[tuple[str, dict]] = []
        monkeypatch.setattr(
            store, "_post",
            lambda path, body: posts.append((path, body)) or {"persisted": len(body["assignments"])},
            raising=False,
        )
        n = store.persist_assignments(_rows(5))
        assert n == 5
        assert len(posts) == 1
        assert posts[0][0] == "/assignments/assign_many"
        assert len(posts[0][1]["assignments"]) == 5

    def test_pages_at_1000(self, monkeypatch) -> None:
        store = HttpTaxonomyStore.__new__(HttpTaxonomyStore)
        posts: list[dict] = []
        monkeypatch.setattr(
            store, "_post",
            lambda path, body: posts.append(body) or {"persisted": len(body["assignments"])},
            raising=False,
        )
        n = store.persist_assignments(_rows(2300))
        assert n == 2300
        assert [len(p["assignments"]) for p in posts] == [1000, 1000, 300]

    def test_404_falls_back_to_per_row(self, monkeypatch) -> None:
        # engine predates v0.1.24: assign_many 404s -> legacy assign_topic loop
        store = HttpTaxonomyStore.__new__(HttpTaxonomyStore)
        single: list[str] = []

        def post_404(path, body):
            err = RuntimeError("HTTP 404: not found")
            err.code = 404
            raise err

        monkeypatch.setattr(store, "_post", post_404, raising=False)
        monkeypatch.setattr(
            store, "assign_topic",
            lambda doc_id, topic_id, assigned_by, similarity=None,
                   source_collection=None, assigned_at=None: single.append(doc_id),
            raising=False,
        )
        n = store.persist_assignments(_rows(3))
        assert n == 3
        assert single == ["d0", "d1", "d2"]

    def test_empty_is_noop(self, monkeypatch) -> None:
        store = HttpTaxonomyStore.__new__(HttpTaxonomyStore)
        monkeypatch.setattr(store, "_post", lambda *a: pytest.fail("no post"), raising=False)
        assert store.persist_assignments([]) == 0
