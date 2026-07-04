# SPDX-License-Identifier: AGPL-3.0-or-later
"""Batched manifest replace (nexus-u2kwq): write_manifest_many pages at
1000 docs via /manifest/write_many; _manifest_write_loop uses it for
multi-doc flush-grain batches with 404 fallback to the per-doc path."""

from __future__ import annotations

from typing import Any

import pytest

from nexus.catalog.http_catalog_client import HttpCatalogClient
from nexus.mcp_infra import _manifest_write_loop


def _client() -> HttpCatalogClient:
    return HttpCatalogClient.__new__(HttpCatalogClient)


class TestWriteManifestMany:
    def test_single_post_and_row_shape(self, monkeypatch) -> None:
        c = _client()
        posts: list[tuple[str, dict]] = []
        monkeypatch.setattr(
            c, "_post",
            lambda path, body: posts.append((path, body)) or {"docs": 2, "rows": 3, "failed_doc_ids": []},
            raising=False,
        )
        failed = c.write_manifest_many([
            ("1.15.1", [{"chash": "a" * 64, "position": 0}]),
            ("1.15.2", [{"chash": "b" * 64, "position": 0}, {"chash": "c" * 64, "position": 1}]),
        ])
        assert failed == []
        assert len(posts) == 1
        path, body = posts[0]
        assert path == "/manifest/write_many"
        assert [d["doc_id"] for d in body["docs"]] == ["1.15.1", "1.15.2"]
        assert len(body["docs"][1]["rows"]) == 2

    def test_pages_at_1000_docs(self, monkeypatch) -> None:
        c = _client()
        posts: list[dict] = []
        monkeypatch.setattr(
            c, "_post",
            lambda path, body: posts.append(body) or {"docs": len(body["docs"]), "rows": 0, "failed_doc_ids": []},
            raising=False,
        )
        docs = [(f"1.9.{i}", [{"chash": "a" * 64, "position": 0}]) for i in range(1500)]
        c.write_manifest_many(docs)
        assert [len(p["docs"]) for p in posts] == [1000, 500]

    def test_failed_doc_ids_surface(self, monkeypatch) -> None:
        c = _client()
        monkeypatch.setattr(
            c, "_post",
            lambda path, body: {"docs": 1, "rows": 1, "failed_doc_ids": ["1.9.7"]},
            raising=False,
        )
        failed = c.write_manifest_many([("1.9.7", [{"chash": "a" * 64, "position": 0}])])
        assert failed == ["1.9.7"]


class _FakeCat:
    """Catalog test double capturing which write path the loop takes."""

    def __init__(self, many: bool = True, many_404: bool = False) -> None:
        self.many_calls: list[list] = []
        self.replace_calls: list[str] = []
        self.resync_calls: list[str] = []
        self.many_404 = many_404
        if not many:
            # simulate a writer without the capability (legacy proxy)
            self.write_manifest_many = None  # type: ignore[assignment]

    def write_manifest_many(self, docs):  # type: ignore[no-redef]
        if self.many_404:
            err: Any = RuntimeError("HTTP 404")
            err.code = 404
            raise err
        self.many_calls.append(list(docs))
        return []

    def atomic_manifest_replace(self, doc_id, chunks, **kw):
        self.replace_calls.append(doc_id)

    def resync_chunk_count_cache(self, doc_id):
        self.resync_calls.append(doc_id)


def _by_doc(n_docs: int) -> dict:
    return {
        f"1.9.{d}": [(i, {"chunk_text_hash": f"{d:02d}{i:02d}" + "e" * 60,
                          "chunk_index": i}) for i in range(2)]
        for d in range(n_docs)
    }


class TestManifestWriteLoopBatching:
    def test_multi_doc_uses_write_many_once(self) -> None:
        cat = _FakeCat()
        _manifest_write_loop(cat, _by_doc(3))
        assert len(cat.many_calls) == 1
        assert len(cat.many_calls[0]) == 3
        assert cat.replace_calls == []

    def test_404_falls_back_to_per_doc_with_chunk_count_resync(self) -> None:
        cat = _FakeCat(many_404=True)
        _manifest_write_loop(cat, _by_doc(2))
        assert sorted(cat.replace_calls) == ["1.9.0", "1.9.1"]
        # chunk_count parity (critique Critical): HTTP replace does not
        # sync documents.chunk_count — the fallback must resync per doc.
        assert sorted(cat.resync_calls) == ["1.9.0", "1.9.1"]

    def test_single_doc_batch_takes_write_many(self) -> None:
        # critique Critical: len(by_doc)==1 must STILL use write_many —
        # it is the only HTTP path that folds chunk_count in.
        cat = _FakeCat()
        _manifest_write_loop(cat, _by_doc(1))
        assert len(cat.many_calls) == 1
        assert [d for d, _ in cat.many_calls[0]] == ["1.9.0"]
        assert cat.replace_calls == []

    def test_all_continuation_batch_still_appends(self) -> None:
        # critique Significant: every doc lacking position 0 must fall
        # through to the append path, not vanish in an early return.
        cat = _FakeCat()
        cat.append_calls: list[str] = []
        cat.append_manifest_chunks = lambda doc_id, chunks: cat.append_calls.append(doc_id)
        by_doc = {
            f"1.9.{d}": [(i, {"chunk_text_hash": "a" * 64, "chunk_index": 3 + i})
                         for i in range(2)]
            for d in range(2)
        }
        _manifest_write_loop(cat, by_doc)
        assert cat.many_calls == []
        assert sorted(cat.append_calls) == ["1.9.0", "1.9.1"]
        assert cat.replace_calls == []

    def test_continuation_doc_without_position0_never_replaced(self) -> None:
        # Review Important #1: a doc whose slice lacks position 0 is a
        # continuation — REPLACE would delete its earlier rows. It must
        # route to the per-doc append path, never write_many.
        cat = _FakeCat()
        cat.append_calls: list[str] = []
        cat.append_manifest_chunks = lambda doc_id, chunks: cat.append_calls.append(doc_id)
        cat.resync_chunk_count_cache = lambda doc_id: None
        by_doc = _by_doc(2)  # both have position 0 via chunk_index 0
        # make one doc a continuation slice (positions 5,6)
        by_doc["1.9.1"] = [
            (i, {"chunk_text_hash": "f" * 64, "chunk_index": 5 + i})
            for i in range(2)
        ]
        _manifest_write_loop(cat, by_doc)
        assert len(cat.many_calls) == 1
        assert [d for d, _ in cat.many_calls[0]] == ["1.9.0"]
        assert cat.replace_calls == []  # continuation NOT replaced
        assert cat.append_calls == ["1.9.1"]  # appended instead

    def test_writer_without_capability_uses_per_doc(self) -> None:
        cat = _FakeCat(many=False)
        _manifest_write_loop(cat, _by_doc(2))
        assert sorted(cat.replace_calls) == ["1.9.0", "1.9.1"]
