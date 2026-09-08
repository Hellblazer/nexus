# SPDX-License-Identifier: AGPL-3.0-or-later
"""persist_assignments batch path (nexus-71988): one POST per <=1000 rows
via /assignments/assign_many, with 404 fallback to the per-row loop for
engines predating v0.1.24."""

from __future__ import annotations
from unittest.mock import MagicMock

import httpx
import pytest

import nexus.corpus as corpus
from nexus.db.t2.http_taxonomy_store import HttpTaxonomyStore


def _rows(n: int, by: str = "centroid") -> list[dict]:
    return [
        {"doc_id": f"d{i}", "topic_id": 7, "assigned_by": by}
        for i in range(n)
    ]


def _rows_with_collection(n: int, collection: str, by: str = "centroid") -> list[dict]:
    return [
        {"doc_id": f"d{i}", "topic_id": 7, "assigned_by": by, "source_collection": collection}
        for i in range(n)
    ]


def _http_422(body_text: str) -> httpx.HTTPStatusError:
    response = MagicMock(status_code=422)
    response.text = body_text
    return httpx.HTTPStatusError(body_text, request=MagicMock(), response=response)


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


class TestPersistAssignmentsBatchSelfHeals:
    """RDR-204 Phase 1 follow-up (nexus-f5wwx, code review [24995]
    Significant finding 1): the batch write self-heals on the engine's
    not-registered 422 (the per-tenant boot-sweep race, bead .3) via
    ``write_with_registration_retry``, exactly like every sibling write
    path — not only on the older-engine 404 handled above."""

    @pytest.fixture(autouse=True)
    def _clear_registration_cache(self):
        corpus._REGISTERED_COLLECTIONS.clear()
        yield
        corpus._REGISTERED_COLLECTIONS.clear()

    @pytest.fixture(autouse=True)
    def _pin_write_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)
        monkeypatch.setattr(
            "nexus.db.http_vector_client.is_vector_service_mode", lambda: True,
        )

    def _fake_writer(self, monkeypatch: pytest.MonkeyPatch) -> MagicMock:
        writer = MagicMock()
        writer.register_collection.return_value = None
        monkeypatch.setattr(
            "nexus.catalog.factory.make_catalog_writer", MagicMock(return_value=writer),
        )
        return writer

    def test_batch_selfheals_on_stale_registration_422(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        store = HttpTaxonomyStore.__new__(HttpTaxonomyStore)
        writer = self._fake_writer(monkeypatch)
        posts: list[dict] = []
        err = _http_422(
            "collection 'code__stale-registration-test' is not registered "
            "for tenant 'nexus'",
        )
        calls = {"n": 0}

        def post(path, body):
            calls["n"] += 1
            if calls["n"] == 1:
                raise err
            posts.append(body)
            return {"persisted": len(body["assignments"])}

        monkeypatch.setattr(store, "_post", post, raising=False)

        n = store.persist_assignments(
            _rows_with_collection(2, "code__stale-registration-test"),
        )

        assert n == 2
        assert calls["n"] == 2  # first attempt 422s, retry succeeds
        assert len(posts) == 1
        assert len(posts[0]["assignments"]) == 2
        # Registered once up front, once more after the stale-cache eviction.
        assert writer.register_collection.call_count == 2

    def test_batch_different_422_is_not_retried(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        store = HttpTaxonomyStore.__new__(HttpTaxonomyStore)
        self._fake_writer(monkeypatch)
        err = _http_422(
            "embedding_model 'some-other-model' does not match the install "
            "profile's 'bge-base-en-v15-768'",
        )
        calls = {"n": 0}

        def post(path, body):
            calls["n"] += 1
            raise err

        monkeypatch.setattr(store, "_post", post, raising=False)

        with pytest.raises(httpx.HTTPStatusError):
            store.persist_assignments(
                _rows_with_collection(1, "code__profile-mismatch-test"),
            )

        assert calls["n"] == 1  # not retried — a different 422 propagates unretried


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
