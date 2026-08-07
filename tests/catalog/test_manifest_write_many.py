# SPDX-License-Identifier: AGPL-3.0-or-later
"""Batched manifest replace (nexus-u2kwq): write_manifest_many pages at
1000 docs via /manifest/write_many; _manifest_write_loop uses it for
multi-doc flush-grain batches with 404 fallback to the per-doc path.

GH #1371: _manifest_write_loop retries transient connection errors and,
on persistent failure, records the doc_id in the module-level failure
collector instead of only logging a WARNING (see TestManifestWriteFailureSurfacing).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import httpx
import pytest

from nexus.catalog.http_catalog_client import HttpCatalogClient
from nexus.mcp_infra import (
    _manifest_write_loop,
    get_manifest_write_failures,
    reset_manifest_write_failures,
)


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
        result = c.write_manifest_many([
            ("1.15.1", [{"chash": "a" * 64, "position": 0}]),
            ("1.15.2", [{"chash": "b" * 64, "position": 0}, {"chash": "c" * 64, "position": 1}]),
        ])
        assert result["failed_doc_ids"] == []
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
        result = c.write_manifest_many([("1.9.7", [{"chash": "a" * 64, "position": 0}])])
        assert result["failed_doc_ids"] == ["1.9.7"]

    def test_failed_reasons_logged(self, monkeypatch) -> None:
        # nexus-fhhwf: engine v0.1.33+ returns {failed:[{doc_id, reason,
        # sqlstate}]} alongside the bare id list — the client must surface
        # the reason in its log so a failed doc is diagnosable without
        # server access (the v0.1.24 three-iteration lesson).
        import structlog.testing

        c = _client()
        monkeypatch.setattr(
            c, "_post",
            lambda path, body: {
                "docs": 0, "rows": 0,
                "failed_doc_ids": ["1.9.8"],
                "failed": [{
                    "doc_id": "1.9.8",
                    "reason": "check constraint violation [catalog_document_chunks_chash_len_check]",
                    "sqlstate": "23514",
                }],
            },
            raising=False,
        )
        with structlog.testing.capture_logs() as logs:
            result = c.write_manifest_many(
                [("1.9.8", [{"chash": "a" * 64, "position": 0}])]
            )
        assert result["failed_doc_ids"] == ["1.9.8"]
        events = [l for l in logs if l["event"] == "manifest_write_many_doc_failed"]
        assert len(events) == 1
        assert events[0]["doc_id"] == "1.9.8"
        assert "chash_len_check" in events[0]["reason"]
        assert events[0]["sqlstate"] == "23514"

    def test_pre_reason_engine_response_still_works(self, monkeypatch) -> None:
        # Back-compat: an older engine without the "failed" field must not
        # break the client (no KeyError, ids still returned).
        c = _client()
        monkeypatch.setattr(
            c, "_post",
            lambda path, body: {"docs": 0, "rows": 0, "failed_doc_ids": ["1.9.9"]},
            raising=False,
        )
        assert c.write_manifest_many(
            [("1.9.9", [{"chash": "a" * 64, "position": 0}])]
        )["failed_doc_ids"] == ["1.9.9"]


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

    # nexus-kgos1: the sweep's two READS. Present and empty so the per-doc
    # path's sweep is exercised and provably no-ops on an empty `before` —
    # rather than dying on AttributeError and no-opping by accident, which is
    # what this file did before `reader` became required.
    def get_chunk_chashes(self, doc_id):
        return []

    def docs_for_chashes(self, chashes):
        return {}

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
        _manifest_write_loop(cat, _by_doc(3), reader=cat)
        assert len(cat.many_calls) == 1
        assert len(cat.many_calls[0]) == 3
        assert cat.replace_calls == []

    def test_404_falls_back_to_per_doc_with_chunk_count_resync(self) -> None:
        cat = _FakeCat(many_404=True)
        _manifest_write_loop(cat, _by_doc(2), reader=cat)
        assert sorted(cat.replace_calls) == ["1.9.0", "1.9.1"]
        # chunk_count parity (critique Critical): HTTP replace does not
        # sync documents.chunk_count — the fallback must resync per doc.
        assert sorted(cat.resync_calls) == ["1.9.0", "1.9.1"]

    def test_single_doc_batch_takes_write_many(self) -> None:
        # critique Critical: len(by_doc)==1 must STILL use write_many —
        # it is the only HTTP path that folds chunk_count in.
        cat = _FakeCat()
        _manifest_write_loop(cat, _by_doc(1), reader=cat)
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
        _manifest_write_loop(cat, by_doc, reader=cat)
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
        _manifest_write_loop(cat, by_doc, reader=cat)
        assert len(cat.many_calls) == 1
        assert [d for d, _ in cat.many_calls[0]] == ["1.9.0"]
        assert cat.replace_calls == []  # continuation NOT replaced
        assert cat.append_calls == ["1.9.1"]  # appended instead

    def test_writer_without_capability_uses_per_doc(self) -> None:
        cat = _FakeCat(many=False)
        _manifest_write_loop(cat, _by_doc(2), reader=cat)
        assert sorted(cat.replace_calls) == ["1.9.0", "1.9.1"]


class _FlakyThenOkCat:
    """Fails the first N calls to a given write op with a transient
    connection error, then succeeds. Used to prove _manifest_write_loop
    retries transient connection failures instead of losing the write."""

    def __init__(self, fail_times: int) -> None:
        self.fail_times = fail_times
        self.calls = 0
        self.replace_calls: list[str] = []
        self.resync_calls: list[str] = []
        self.write_manifest_many = None  # type: ignore[assignment]

    def atomic_manifest_replace(self, doc_id, chunks, **kw):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise httpx.ConnectError("connection refused")
        self.replace_calls.append(doc_id)

    def resync_chunk_count_cache(self, doc_id):
        self.resync_calls.append(doc_id)


class _AlwaysDownCat:
    """Every write raises a persistent (non-connection) error — proves the
    hook still swallows the failure (contract: never propagate) and
    records it for the CLI summary."""

    def __init__(self) -> None:
        self.write_manifest_many = None  # type: ignore[assignment]

    def atomic_manifest_replace(self, doc_id, chunks, **kw):
        raise ValueError("FK violation: doc_id not found")

    def resync_chunk_count_cache(self, doc_id):
        pass


class _AlwaysDownManyCat:
    """write_manifest_many always raises a persistent connection error —
    proves the write_many path does not crash the caller and records
    every doc in the failed batch."""

    def __init__(self) -> None:
        self.replace_calls: list[str] = []

    def write_manifest_many(self, docs):
        raise httpx.ConnectError("connection refused")

    def atomic_manifest_replace(self, doc_id, chunks, **kw):
        self.replace_calls.append(doc_id)

    def resync_chunk_count_cache(self, doc_id):
        pass


class TestManifestWriteFailureSurfacing:
    """GH #1371: retry transient connection errors; surface persistent
    failures via the module-level collector instead of only a log line."""

    def setup_method(self) -> None:
        reset_manifest_write_failures()

    def teardown_method(self) -> None:
        reset_manifest_write_failures()

    def test_transient_connect_error_recovers_without_recording_failure(self) -> None:
        cat = _FlakyThenOkCat(fail_times=2)
        with patch("nexus.retry.time.sleep"):
            _manifest_write_loop(cat, _by_doc(1), reader=cat)
        assert cat.replace_calls == ["1.9.0"]
        assert get_manifest_write_failures() == []

    def test_persistent_per_doc_failure_is_swallowed_and_recorded(self) -> None:
        cat = _AlwaysDownCat()
        with patch("nexus.retry.time.sleep"):
            # Contract: must never raise out of the hook.
            _manifest_write_loop(cat, _by_doc(2), reader=cat)
        assert sorted(get_manifest_write_failures()) == ["1.9.0", "1.9.1"]

    def test_persistent_write_many_failure_is_swallowed_and_recorded(self) -> None:
        cat = _AlwaysDownManyCat()
        with patch("nexus.retry.time.sleep"):
            _manifest_write_loop(cat, _by_doc(2), reader=cat)
        assert sorted(get_manifest_write_failures()) == ["1.9.0", "1.9.1"]
        # Not re-attempted per-doc after the batch path exhausted retries.
        assert cat.replace_calls == []

    def test_reset_clears_prior_run_failures(self) -> None:
        cat = _AlwaysDownCat()
        with patch("nexus.retry.time.sleep"):
            _manifest_write_loop(cat, _by_doc(1), reader=cat)
        assert get_manifest_write_failures() == ["1.9.0"]
        reset_manifest_write_failures()
        assert get_manifest_write_failures() == []


class TestManifestNeverOutrunsConfirmedChunks:
    """nexus-tp8yk D1 consequence, pinned at this file's own layer (memo
    §5 TDD plan): ``_manifest_write_loop`` — and therefore the whole
    ``manifest_write_batch_hook`` chain this file otherwise drives
    directly — must be STRUCTURALLY UNREACHABLE for a batch
    ``doc_indexer._upsert_skip_reembed`` could not confirm landed. The
    gate itself lives one layer up (``_upsert_skip_reembed`` raises
    ``ChunkLandingUnverifiedError`` BEFORE ``hooks.fire_batch`` runs);
    this test proves the consequence at THIS file's call site by driving
    the real production entry point (``nexus.doc_indexer._index_document``)
    with a fake T3 that reproduces the "stale-positive probe, cannot-tell
    update response" fault, and asserting `_manifest_write_loop` sees
    zero calls.
    """

    def test_unconfirmed_batch_never_reaches_manifest_write_loop(
        self, tmp_path, monkeypatch,
    ) -> None:
        from nexus.doc_indexer import _index_document
        from nexus.errors import ChunkLandingUnverifiedError

        calls: list = []
        monkeypatch.setattr(
            "nexus.mcp_infra._manifest_write_loop",
            lambda *a, **kw: calls.append((a, kw)),
        )

        class _FakeCol:
            def get(self, where=None, include=None, limit=None, offset=None, **kw):
                return {"ids": [], "metadatas": []}

        class _FakeT3:
            def get_or_create_collection(self, name):
                return _FakeCol()

            def existing_ids(self, collection, ids):
                # Stale-positive probe: reports EVERY id present.
                return list(ids)

            def update_chunks(self, collection, ids, metadatas):
                # "Cannot tell" — the exact D1 trigger.
                return None

            def upsert_chunks_with_embeddings(self, *a, **k):
                raise AssertionError(
                    "must not be called — every id took the stale-positive "
                    "(existing_ids) branch, never the fresh-upsert branch"
                )

        def _chunks(file_path, content_hash, target_model, now_iso, corpus):
            return [(
                "chunk-0", "chunk text",
                {"content_hash": content_hash, "embedding_model": target_model,
                 "chunk_text_hash": "a" * 64, "content_type": "prose"},
            )]

        def _embed(texts, model):
            return [[0.1, 0.2]] * len(texts), model

        f = tmp_path / "doc.md"
        f.write_text("hello tp8yk")

        with patch("nexus.doc_indexer._register_or_lookup_doc_id", return_value="1.9.99"), \
                patch("nexus.doc_indexer._fence_begin"), \
                patch("nexus.doc_indexer._fence_fail"):
            with pytest.raises(ChunkLandingUnverifiedError):
                _index_document(
                    f, "testowner", _chunks, _FakeT3(),
                    collection_name="docs__testowner__bge-base-en-v15-768__v1",
                    embed_fn=_embed,
                )

        assert calls == [], (
            "_manifest_write_loop must be UNREACHABLE for a batch "
            f"_upsert_skip_reembed could not confirm landed — got "
            f"{len(calls)} call(s)"
        )
