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


class TestAssignFromChashesPaging:
    """assign_from_chashes pages at the engine's MAX_ASSIGN_FROM_CHASHES cap
    (nexus-yu9w5 substantive-critic finding): the legacy per-file fallback
    passes a single oversize file's FULL chash list here, and an unpaged
    >1000-chash POST would 400 and lose taxonomy assignment for the whole
    file. Same _PAGE pattern as persist_assignments above."""

    def _paging_store(self, monkeypatch, posts: list[dict]):
        store = HttpTaxonomyStore.__new__(HttpTaxonomyStore)

        def post(path, body):
            assert path == "/assignments/assign_from_chashes"
            posts.append(body)
            return {
                "assigned": len(body["chashes"]),
                "cross_assigned": 1,
                "unmatched_chashes": [body["chashes"][0]],
            }

        monkeypatch.setattr(store, "_post", post, raising=False)
        return store

    def test_single_post_under_cap(self, monkeypatch) -> None:
        posts: list[dict] = []
        store = self._paging_store(monkeypatch, posts)
        out = store.assign_from_chashes("code__x", [f"c{i}" for i in range(5)])
        assert len(posts) == 1
        assert posts[0]["cross_collection"] is True
        assert out == {"assigned": 5, "cross_assigned": 1, "unmatched_chashes": ["c0"]}

    def test_pages_at_1000_and_aggregates(self, monkeypatch) -> None:
        posts: list[dict] = []
        store = self._paging_store(monkeypatch, posts)
        chashes = [f"c{i}" for i in range(2500)]
        out = store.assign_from_chashes("code__x", chashes, cross_collection=False)
        assert [len(p["chashes"]) for p in posts] == [1000, 1000, 500]
        # Pages must partition the input in order, no overlap, no drop.
        assert [c for p in posts for c in p["chashes"]] == chashes
        assert all(p["cross_collection"] is False for p in posts)
        assert out["assigned"] == 2500
        assert out["cross_assigned"] == 3
        assert out["unmatched_chashes"] == ["c0", "c1000", "c2000"]

    def test_empty_is_noop(self, monkeypatch) -> None:
        store = HttpTaxonomyStore.__new__(HttpTaxonomyStore)
        monkeypatch.setattr(store, "_post", lambda *a: pytest.fail("no post"), raising=False)
        assert store.assign_from_chashes("code__x", []) == {
            "assigned": 0, "cross_assigned": 0, "unmatched_chashes": [],
        }
