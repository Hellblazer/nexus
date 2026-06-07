# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Unit tests for HttpDocumentAspectsStore, HttpDocumentHighlightsStore, HttpAspectQueue.

Tests use httpx.MockTransport to avoid requiring a live Java service.
Also includes the T2Database seam assertion: NX_STORAGE_BACKEND_DOCUMENT_ASPECTS=service
returns an HttpDocumentAspectsStore, etc.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import httpx
import pytest

from nexus.db.t2.aspect_extraction_queue import QueueRow
from nexus.db.t2.document_aspects import AspectRecord
from nexus.db.t2.document_highlights import HighlightRecord
from nexus.db.t2.http_document_aspects_store import HttpDocumentAspectsStore
from nexus.db.t2.http_document_highlights_store import HttpDocumentHighlightsStore
from nexus.db.t2.http_aspect_queue import HttpAspectQueue


# ── Mock transport helpers ─────────────────────────────────────────────────────


def _make_transport(handlers: dict):
    """Build an httpx.MockTransport from a path->response_body dict.

    Each handler value can be:
    - dict: serialized to JSON, status 200
    - (int, dict): status + JSON body
    - Exception: raised when path is matched
    """
    def handle_request(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        handler = handlers.get(path)
        if handler is None:
            return httpx.Response(404, json={"error": f"not found: {path}"})
        if isinstance(handler, Exception):
            raise handler
        if isinstance(handler, tuple):
            status, body = handler
            return httpx.Response(status, json=body)
        return httpx.Response(200, json=handler)
    return httpx.MockTransport(handle_request)


# ── AspectRecord factory ───────────────────────────────────────────────────────


def _make_aspect(**kwargs) -> AspectRecord:
    defaults = {
        "collection": "test-coll",
        "source_path": "doc.pdf",
        "problem_formulation": "pf",
        "proposed_method": "pm",
        "experimental_datasets": ["ds1"],
        "experimental_baselines": ["bl1"],
        "experimental_results": "good",
        "extras": {"k": "v"},
        "confidence": 0.9,
        "extracted_at": "2025-01-01T00:00:00.000000Z",
        "model_version": "claude-haiku-v1",
        "extractor_name": "scholarly-paper-v1",
        "source_uri": "chroma://test-coll/doc.pdf",
        "doc_id": "1.2.3",
        "salient_sentences": ["sentence one"],
    }
    defaults.update(kwargs)
    return AspectRecord(**defaults)


# ── HttpDocumentAspectsStore unit tests ────────────────────────────────────────


class TestHttpDocumentAspectsStore:
    def _store(self, handlers: dict) -> HttpDocumentAspectsStore:
        transport = _make_transport(handlers)
        store = HttpDocumentAspectsStore(base_url="http://test", _token="tok")
        store._client = httpx.Client(
            base_url="http://test",
            headers=store._headers,
            transport=transport,
        )
        return store

    def test_upsert_written(self):
        store = self._store({"/v1/aspects/upsert": {"written": True}})
        record = _make_aspect()
        assert store.upsert(record) is True

    def test_upsert_rejected_low_confidence(self):
        # Service returns written=False (confidence gate)
        store = self._store({"/v1/aspects/upsert": {"written": False}})
        record = _make_aspect(confidence=0.1)
        assert store.upsert(record) is False

    def test_upsert_validates_extracted_at(self):
        store = self._store({})
        with pytest.raises(ValueError, match="extracted_at"):
            store.upsert(_make_aspect(extracted_at=""))

    def test_upsert_validates_model_version(self):
        store = self._store({})
        with pytest.raises(ValueError, match="model_version"):
            store.upsert(_make_aspect(model_version=""))

    def test_upsert_validates_extractor_name(self):
        store = self._store({})
        with pytest.raises(ValueError, match="extractor_name"):
            store.upsert(_make_aspect(extractor_name=""))

    def test_get_returns_record(self):
        body = {
            "collection": "test-coll",
            "source_path": "doc.pdf",
            "problem_formulation": "pf",
            "proposed_method": "pm",
            "experimental_datasets": ["ds1"],
            "experimental_baselines": [],
            "experimental_results": None,
            "extras": {},
            "confidence": 0.9,
            "extracted_at": "2025-01-01T00:00:00.000000Z",
            "model_version": "mv",
            "extractor_name": "en",
            "source_uri": None,
            "doc_id": "",
            "salient_sentences": [],
        }
        store = self._store({"/v1/aspects/get": body})
        result = store.get("test-coll", "doc.pdf")
        assert result is not None
        assert result.collection == "test-coll"
        assert result.confidence == 0.9

    def test_get_returns_none_on_404(self):
        store = self._store({"/v1/aspects/get": (404, {"error": "not found"})})
        assert store.get("coll", "path.pdf") is None

    def test_get_by_doc_id_returns_record(self):
        body = {
            "collection": "c",
            "source_path": "s",
            "problem_formulation": None,
            "proposed_method": None,
            "experimental_datasets": [],
            "experimental_baselines": [],
            "experimental_results": None,
            "extras": {},
            "confidence": 0.8,
            "extracted_at": "2025-01-01T00:00:00.000000Z",
            "model_version": "mv",
            "extractor_name": "en",
            "source_uri": None,
            "doc_id": "1.2.3",
            "salient_sentences": [],
        }
        store = self._store({"/v1/aspects/get_by_doc_id": body})
        result = store.get_by_doc_id("1.2.3")
        assert result is not None
        assert result.doc_id == "1.2.3"

    def test_get_by_doc_id_returns_none_on_404(self):
        store = self._store({"/v1/aspects/get_by_doc_id": (404, {})})
        assert store.get_by_doc_id("missing") is None

    def test_list_by_collection_returns_records(self):
        body_list = [
            {"collection": "c", "source_path": f"doc{i}.pdf", "problem_formulation": None,
             "proposed_method": None, "experimental_datasets": [], "experimental_baselines": [],
             "experimental_results": None, "extras": {}, "confidence": 0.7,
             "extracted_at": "2025-01-01T00:00:00.000000Z", "model_version": "mv",
             "extractor_name": "en", "source_uri": None, "doc_id": "", "salient_sentences": []}
            for i in range(3)
        ]
        store = self._store({"/v1/aspects/list_by_collection": body_list})
        result = store.list_by_collection("c")
        assert len(result) == 3

    def test_delete_returns_count(self):
        store = self._store({"/v1/aspects/delete": {"deleted": 1}})
        assert store.delete("coll", "doc.pdf") == 1

    def test_delete_orphans_returns_zero_zero(self):
        store = self._store({})
        result = store.delete_orphans(Path("/some/path.db"))
        assert result == (0, 0)

    def test_rename_collection(self):
        store = self._store({"/v1/aspects/rename_collection": {"updated": 5}})
        assert store.rename_collection(old="old", new="new") == 5

    def test_list_by_extractor_version(self):
        body_list = [
            {"collection": "c", "source_path": "a.pdf", "problem_formulation": None,
             "proposed_method": None, "experimental_datasets": [], "experimental_baselines": [],
             "experimental_results": None, "extras": {}, "confidence": 0.7,
             "extracted_at": "2025-01-01T00:00:00.000000Z", "model_version": "old",
             "extractor_name": "en", "source_uri": None, "doc_id": "", "salient_sentences": []}
        ]
        store = self._store({"/v1/aspects/list_by_extractor_version": body_list})
        result = store.list_by_extractor_version("en", "new-version")
        assert len(result) == 1

    def test_set_salient_sentences(self):
        store = self._store({"/v1/aspects/salient_sentences/set": {"updated": True}})
        assert store.set_salient_sentences("1.2.3", ["sent1"]) is True

    def test_set_salient_sentences_by_key(self):
        store = self._store({"/v1/aspects/salient_sentences/set_by_key": {"updated": True}})
        assert store.set_salient_sentences_by_key("c", "p", ["s"]) is True

    def test_get_salient_sentences(self):
        store = self._store({
            "/v1/aspects/salient_sentences/get": {"sentences": ["one", "two"]}
        })
        result = store.get_salient_sentences("1.2.3")
        assert result == ["one", "two"]

    def test_get_salient_sentences_404_returns_empty(self):
        store = self._store({
            "/v1/aspects/salient_sentences/get": (404, {})
        })
        assert store.get_salient_sentences("missing") == []

    def test_import_aspect(self):
        store = self._store({"/v1/aspects/import": {"imported": 1}})
        assert store.import_aspect({"collection": "c", "source_path": "s"}) == 1

    def test_close_is_idempotent(self):
        store = self._store({})
        store.close()
        store.close()  # second close should not raise


# ── HttpDocumentHighlightsStore unit tests ────────────────────────────────────


class TestHttpDocumentHighlightsStore:
    def _store(self, handlers: dict) -> HttpDocumentHighlightsStore:
        transport = _make_transport(handlers)
        store = HttpDocumentHighlightsStore(base_url="http://test", _token="tok")
        store._client = httpx.Client(
            base_url="http://test",
            headers=store._headers,
            transport=transport,
        )
        return store

    def _record(self, **kwargs) -> HighlightRecord:
        defaults = {
            "doc_id": "1.2.3",
            "source_uri": "x-devonthink://abc",
            "collection": "c",
            "highlights_md": "# Highlights",
            "mentions_md": "- mention",
            "ingested_at": "2025-01-01T00:00:00Z",
        }
        defaults.update(kwargs)
        return HighlightRecord(**defaults)

    def test_upsert_written(self):
        store = self._store({"/v1/aspects/highlights/upsert": {"written": True}})
        assert store.upsert(self._record()) is True

    def test_upsert_empty_content_returns_false(self):
        store = self._store({})
        assert store.upsert(self._record(highlights_md="", mentions_md="")) is False

    def test_upsert_validates_doc_id(self):
        store = self._store({})
        with pytest.raises(ValueError, match="doc_id"):
            store.upsert(self._record(doc_id=""))

    def test_upsert_validates_ingested_at(self):
        store = self._store({})
        with pytest.raises(ValueError, match="ingested_at"):
            store.upsert(self._record(ingested_at=""))

    def test_get_returns_record(self):
        body = {
            "doc_id": "1.2.3",
            "source_uri": "uri",
            "collection": "c",
            "highlights_md": "hl",
            "mentions_md": "",
            "ingested_at": "2025-01-01T00:00:00Z",
        }
        store = self._store({"/v1/aspects/highlights/get": body})
        result = store.get("1.2.3")
        assert result is not None
        assert result.doc_id == "1.2.3"
        assert result.highlights_md == "hl"

    def test_get_returns_none_on_404(self):
        store = self._store({"/v1/aspects/highlights/get": (404, {})})
        assert store.get("missing") is None

    def test_get_by_source_uri_returns_record(self):
        body = {
            "doc_id": "1.2.3",
            "source_uri": "x-devonthink://abc",
            "collection": "c",
            "highlights_md": "hl",
            "mentions_md": "",
            "ingested_at": "2025-01-01T00:00:00Z",
        }
        store = self._store({"/v1/aspects/highlights/get_by_source_uri": body})
        result = store.get_by_source_uri("x-devonthink://abc")
        assert result is not None

    def test_get_by_source_uri_404(self):
        store = self._store({"/v1/aspects/highlights/get_by_source_uri": (404, {})})
        assert store.get_by_source_uri("missing") is None

    def test_list_returns_records(self):
        body_list = [
            {"doc_id": f"id{i}", "source_uri": "", "collection": "c",
             "highlights_md": "hl", "mentions_md": "", "ingested_at": "2025-01-01T00:00:00Z"}
            for i in range(2)
        ]
        store = self._store({"/v1/aspects/highlights/list": body_list})
        result = store.list()
        assert len(result) == 2

    def test_delete_true_on_found(self):
        store = self._store({"/v1/aspects/highlights/delete": {"deleted": True}})
        assert store.delete("1.2.3") is True

    def test_delete_false_on_missing(self):
        store = self._store({"/v1/aspects/highlights/delete": {"deleted": False}})
        assert store.delete("missing") is False

    def test_import_highlight(self):
        store = self._store({"/v1/aspects/highlights/import": {"imported": 1}})
        assert store.import_highlight({"doc_id": "1.2.3"}) == 1


# ── HttpAspectQueue unit tests ─────────────────────────────────────────────────


class TestHttpAspectQueue:
    def _queue(self, handlers: dict) -> HttpAspectQueue:
        transport = _make_transport(handlers)
        store = HttpAspectQueue(base_url="http://test", _token="tok")
        store._client = httpx.Client(
            base_url="http://test",
            headers=store._headers,
            transport=transport,
        )
        return store

    def test_enqueue_calls_endpoint(self):
        store = self._queue({"/v1/aspects/queue/enqueue": {"ok": True}})
        store.enqueue("coll", "doc.pdf", content_hash="abc", content="text", doc_id="1.2.3")

    def test_enqueue_validates_collection(self):
        store = self._queue({})
        with pytest.raises(ValueError, match="collection"):
            store.enqueue("", "doc.pdf")

    def test_enqueue_validates_source_path(self):
        store = self._queue({})
        with pytest.raises(ValueError, match="source_path"):
            store.enqueue("coll", "")

    def test_claim_next_returns_row(self):
        store = self._queue({
            "/v1/aspects/queue/claim_next": {
                "claimed": True,
                "row": {
                    "collection": "c",
                    "source_path": "doc.pdf",
                    "content_hash": "h",
                    "content": "t",
                    "retry_count": 0,
                    "doc_id": "1.2.3",
                },
            }
        })
        result = store.claim_next()
        assert result is not None
        assert isinstance(result, QueueRow)
        assert result.collection == "c"
        assert result.source_path == "doc.pdf"
        assert result.doc_id == "1.2.3"

    def test_claim_next_returns_none_when_empty(self):
        store = self._queue({
            "/v1/aspects/queue/claim_next": {"claimed": False}
        })
        assert store.claim_next() is None

    def test_claim_batch_returns_list(self):
        rows = [
            {"collection": "c", "source_path": f"doc{i}.pdf",
             "content_hash": "", "content": "", "retry_count": 0, "doc_id": ""}
            for i in range(3)
        ]
        store = self._queue({
            "/v1/aspects/queue/claim_batch": {"rows": rows}
        })
        result = store.claim_batch(5)
        assert len(result) == 3
        assert all(isinstance(r, QueueRow) for r in result)

    def test_claim_batch_zero_limit_returns_empty(self):
        store = self._queue({})
        assert store.claim_batch(0) == []

    def test_mark_done_returns_count(self):
        store = self._queue({"/v1/aspects/queue/mark_done": {"deleted": 1}})
        assert store.mark_done("c", "doc.pdf") == 1

    def test_mark_done_by_doc_id(self):
        store = self._queue({"/v1/aspects/queue/mark_done": {"deleted": 1}})
        assert store.mark_done(doc_id="1.2.3") == 1

    def test_mark_failed(self):
        store = self._queue({"/v1/aspects/queue/mark_failed": {"ok": True}})
        store.mark_failed("c", "doc.pdf", "some error")

    def test_mark_retry(self):
        store = self._queue({"/v1/aspects/queue/mark_retry": {"ok": True}})
        store.mark_retry("c", "doc.pdf")

    def test_reclaim_stale_returns_count(self):
        store = self._queue({"/v1/aspects/queue/reclaim_stale": {"reclaimed": 3}})
        assert store.reclaim_stale(300) == 3

    def test_pending_count(self):
        store = self._queue({"/v1/aspects/queue/pending_count": {"count": 7}})
        assert store.pending_count() == 7

    def test_is_drained_true(self):
        store = self._queue({"/v1/aspects/queue/is_drained": {"drained": True}})
        assert store.is_drained() is True

    def test_is_drained_false(self):
        store = self._queue({"/v1/aspects/queue/is_drained": {"drained": False}})
        assert store.is_drained() is False

    def test_list_pending_returns_rows(self):
        rows = [
            {"collection": "c", "source_path": "doc.pdf",
             "content_hash": "", "content": "", "retry_count": 0, "doc_id": ""}
        ]
        store = self._queue({"/v1/aspects/queue/list_pending": rows})
        result = store.list_pending()
        assert len(result) == 1
        assert isinstance(result[0], QueueRow)

    def test_list_pending_with_limit(self):
        rows = [
            {"collection": "c", "source_path": f"doc{i}.pdf",
             "content_hash": "", "content": "", "retry_count": 0, "doc_id": ""}
            for i in range(5)
        ]
        store = self._queue({"/v1/aspects/queue/list_pending": rows})
        result = store.list_pending(limit=5)
        assert len(result) == 5

    def test_rename_collection(self):
        store = self._queue({"/v1/aspects/queue/rename_collection": {"updated": 4}})
        assert store.rename_collection(old="old", new="new") == 4

    def test_import_queue_row(self):
        store = self._queue({"/v1/aspects/queue/import": {"imported": 1}})
        assert store.import_queue_row({"collection": "c", "source_path": "s"}) == 1

    def test_rename_lock_accepted_but_ignored(self):
        """rename_lock parameter accepted for constructor parity but not used."""
        lock = threading.RLock()
        store = HttpAspectQueue(base_url="http://test", _token="tok", rename_lock=lock)
        # Store has a rename_lock attribute for interface parity
        assert store.rename_lock is lock

    def test_close_is_idempotent(self):
        store = self._queue({})
        store.close()
        store.close()


# ── T2Database seam assertion tests ───────────────────────────────────────────


class TestT2DatabaseAspectsSeam:
    """Verify that T2Database routes to Http* stores when env vars are set."""

    def test_document_aspects_service_seam(self, tmp_path, monkeypatch):
        """NX_STORAGE_BACKEND_DOCUMENT_ASPECTS=service returns HttpDocumentAspectsStore."""
        monkeypatch.setenv("NX_STORAGE_BACKEND_DOCUMENT_ASPECTS", "service")
        monkeypatch.setenv("NX_SERVICE_HOST", "127.0.0.1")
        monkeypatch.setenv("NX_SERVICE_PORT", "9999")
        monkeypatch.setenv("NX_SERVICE_TOKEN", "test-token")

        from nexus.db.t2 import T2Database
        db = T2Database(tmp_path / "memory.db", run_migrations=False)
        try:
            assert isinstance(db.document_aspects, HttpDocumentAspectsStore), (
                f"Expected HttpDocumentAspectsStore, got {type(db.document_aspects)}"
            )
        finally:
            db.document_aspects.close()

    def test_document_highlights_service_seam(self, tmp_path, monkeypatch):
        """NX_STORAGE_BACKEND_DOCUMENT_HIGHLIGHTS=service returns HttpDocumentHighlightsStore."""
        monkeypatch.setenv("NX_STORAGE_BACKEND_DOCUMENT_HIGHLIGHTS", "service")
        monkeypatch.setenv("NX_SERVICE_HOST", "127.0.0.1")
        monkeypatch.setenv("NX_SERVICE_PORT", "9999")
        monkeypatch.setenv("NX_SERVICE_TOKEN", "test-token")

        from nexus.db.t2 import T2Database
        db = T2Database(tmp_path / "memory.db", run_migrations=False)
        try:
            assert isinstance(db.document_highlights, HttpDocumentHighlightsStore), (
                f"Expected HttpDocumentHighlightsStore, got {type(db.document_highlights)}"
            )
        finally:
            db.document_highlights.close()

    def test_aspect_queue_service_seam(self, tmp_path, monkeypatch):
        """NX_STORAGE_BACKEND_ASPECT_QUEUE=service returns HttpAspectQueue."""
        monkeypatch.setenv("NX_STORAGE_BACKEND_ASPECT_QUEUE", "service")
        monkeypatch.setenv("NX_SERVICE_HOST", "127.0.0.1")
        monkeypatch.setenv("NX_SERVICE_PORT", "9999")
        monkeypatch.setenv("NX_SERVICE_TOKEN", "test-token")

        from nexus.db.t2 import T2Database
        db = T2Database(tmp_path / "memory.db", run_migrations=False)
        try:
            assert isinstance(db.aspect_queue, HttpAspectQueue), (
                f"Expected HttpAspectQueue, got {type(db.aspect_queue)}"
            )
        finally:
            db.aspect_queue.close()

    def test_sqlite_seam_when_env_unset(self, tmp_path, monkeypatch):
        """When env vars are absent, T2Database uses the SQLite stores."""
        monkeypatch.delenv("NX_STORAGE_BACKEND_DOCUMENT_ASPECTS", raising=False)
        monkeypatch.delenv("NX_STORAGE_BACKEND_DOCUMENT_HIGHLIGHTS", raising=False)
        monkeypatch.delenv("NX_STORAGE_BACKEND_ASPECT_QUEUE", raising=False)
        monkeypatch.delenv("NX_STORAGE_BACKEND", raising=False)

        from nexus.db.t2 import T2Database
        from nexus.db.t2.document_aspects import DocumentAspects
        from nexus.db.t2.document_highlights import DocumentHighlights
        from nexus.db.t2.aspect_extraction_queue import AspectExtractionQueue

        db = T2Database(tmp_path / "memory.db", run_migrations=False)
        assert isinstance(db.document_aspects, DocumentAspects)
        assert isinstance(db.document_highlights, DocumentHighlights)
        assert isinstance(db.aspect_queue, AspectExtractionQueue)

    def test_missing_port_raises(self, tmp_path, monkeypatch):
        """RuntimeError when NX_SERVICE_PORT is absent."""
        monkeypatch.setenv("NX_STORAGE_BACKEND_DOCUMENT_ASPECTS", "service")
        monkeypatch.delenv("NX_SERVICE_PORT", raising=False)
        monkeypatch.delenv("NX_SERVICE_TOKEN", raising=False)

        from nexus.db.t2 import T2Database
        with pytest.raises(RuntimeError, match="NX_SERVICE_PORT"):
            T2Database(tmp_path / "memory2.db", run_migrations=False)

    def test_missing_token_raises(self, tmp_path, monkeypatch):
        """RuntimeError when NX_SERVICE_TOKEN is absent."""
        monkeypatch.setenv("NX_STORAGE_BACKEND_DOCUMENT_ASPECTS", "service")
        monkeypatch.setenv("NX_SERVICE_PORT", "9999")
        monkeypatch.delenv("NX_SERVICE_TOKEN", raising=False)

        from nexus.db.t2 import T2Database
        with pytest.raises(RuntimeError, match="NX_SERVICE_TOKEN"):
            T2Database(tmp_path / "memory3.db", run_migrations=False)
