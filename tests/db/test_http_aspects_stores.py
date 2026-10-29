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

from nexus.db.t2.records import QueueRow
from nexus.db.t2.records import AspectRecord
from nexus.db.t2.records import HighlightRecord
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
        if path == "/v1/catalog/collections/upsert" and path not in handlers:
            # A registering write's pre-registration now targets THIS
            # store's own endpoint over its own (shared) client (nexus-w1ip
            # follow-up), so every store built through _store() below hits
            # this route before its real write. Answer it the way the
            # engine does -- an idempotent upsert -- unless a test
            # explicitly overrides the route via its own handlers dict.
            try:
                body = json.loads(request.content.decode("utf-8")) if request.content else {}
            except (json.JSONDecodeError, UnicodeDecodeError):
                body = {}
            return httpx.Response(200, json={"created": True, "name": body.get("name", "")})
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
        "source_uri": "nx-note://test-coll/doc.pdf",
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
        # Post-mixin-adoption (nexus-f2qvx.2): RefreshableHttpStoreMixin builds
        # each request's absolute URL from self._base_url + path and passes
        # auth headers fresh per-request (see _request_once) — the test double
        # only needs a transport, not a pinned base_url/headers pair on the
        # httpx.Client itself (there is no self._headers attribute anymore).
        store._client = httpx.Client(transport=transport)
        return store

    @pytest.mark.t2_tenant
    def test_upsert_written(self):
        store = self._store({"/v1/aspects/upsert": {"written": True}})
        record = _make_aspect()
        assert store.upsert(record) is True

    @pytest.mark.t2_tenant
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

    # ── RDR-235 E5 client half (nexus-5r2sc.3.1): the claim-fenced upsert ────

    def _capturing_store(
        self, monkeypatch: pytest.MonkeyPatch, status: int = 200, body: dict | None = None,
    ):
        """A store whose /v1/aspects/upsert answers (*status*, *body*) and
        whose request bodies are captured, as raw bytes, in the returned list.

        The collection registration that precedes a write reads the real catalog
        (the embedding profile), which would need an engine and a tenant; these
        tests pin the request body and the refusal mapping, not registration, so
        it is stubbed out (``test_upsert_written`` and the queue cases above keep
        the registered write path under a tenant)."""
        monkeypatch.setattr("nexus.corpus.ensure_collection_registered", lambda *a, **k: None)
        sent: list[bytes] = []

        def handle(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/catalog/collections/upsert":
                return httpx.Response(200, json={"created": True, "name": "test-coll"})
            if request.url.path != "/v1/aspects/upsert":
                # The collection registrar's read-before-write: not found, as in _make_transport.
                return httpx.Response(404, json={"error": f"not found: {request.url.path}"})
            sent.append(request.content)
            return httpx.Response(status, json=body if body is not None else {"written": True})

        store = HttpDocumentAspectsStore(base_url="http://test", _token="tok")
        store._client = httpx.Client(transport=httpx.MockTransport(handle))
        return store, sent

    def test_upsert_without_a_claim_pair_sends_the_unchanged_body(self, monkeypatch):
        from nexus.db.t2.http_document_aspects_store import _record_to_body

        store, sent = self._capturing_store(monkeypatch)
        record = _make_aspect()
        assert store.upsert(record) is True
        assert len(sent) == 1
        # Byte-identical to today's request: the engine 0.1.165 floor keeps working.
        assert sent[0] == httpx.Request(
            "POST", "http://test/x", json=_record_to_body(record)
        ).content
        assert "claim_id" not in json.loads(sent[0])
        assert "claimant" not in json.loads(sent[0])

    def test_upsert_with_a_claim_pair_sends_both_fields(self, monkeypatch):
        store, sent = self._capturing_store(monkeypatch)
        assert store.upsert(_make_aspect(), claim_id="c-1", claimant="W:0") is True
        body = json.loads(sent[0])
        assert body["claim_id"] == "c-1"
        assert body["claimant"] == "W:0"
        assert body["doc_id"] == "1.2.3"

    @pytest.mark.parametrize("pair", [
        {"claim_id": "c-1"},
        {"claimant": "W:0"},
        {"claim_id": "c-1", "claimant": ""},
        {"claim_id": "", "claimant": "W:0"},
    ])
    def test_upsert_refuses_half_a_pair_before_any_request(self, pair, monkeypatch):
        store, sent = self._capturing_store(monkeypatch)
        with pytest.raises(ValueError, match="claim_id.*claimant"):
            store.upsert(_make_aspect(), **pair)
        assert sent == []

    @pytest.mark.parametrize("status, wire, expected", [
        (404, {"error": "ClaimNotFound", "detail": "no live claim"}, "ClaimNotFoundError"),
        (403, {"error": "ClaimOwnership", "detail": "held by another"}, "ClaimOwnershipError"),
        (403, {"error": "ClaimTaskMismatch", "detail": "not this doc"}, "ClaimTaskMismatchError"),
        (400, {"error": "aspect upsert: claim_id and claimant must be given together"},
         "AspectUpsertBadRequestError"),
        (409, {"error": "constraint violation", "status": "conflict", "sqlstate": "23505"},
         "AspectUpsertConflictError"),
    ])
    def test_fenced_upsert_maps_each_refusal_to_its_own_typed_error(self, status, wire, expected, monkeypatch):
        from nexus.db.t2 import http_document_aspects_store as mod
        from nexus.db.t2 import http_tuple_store as tuples

        cls = getattr(mod, expected, None) or getattr(tuples, expected)
        store, sent = self._capturing_store(monkeypatch, status, wire)
        with pytest.raises(cls):
            store.upsert(_make_aspect(), claim_id="c-1", claimant="W:0")
        assert len(sent) == 1, "a refusal is not retried"

    def test_the_refusal_families_are_distinct_types(self):
        from nexus.db.t2 import http_document_aspects_store as mod
        from nexus.db.t2 import http_tuple_store as tuples

        classes = [
            tuples.ClaimNotFoundError, tuples.ClaimOwnershipError,
            tuples.ClaimTaskMismatchError,
            mod.AspectUpsertBadRequestError, mod.AspectUpsertConflictError,
        ]
        assert len(set(classes)) == 5
        for a in classes:
            for b in classes:
                if a is not b:
                    assert not issubclass(a, b), f"{a.__name__} must not be a {b.__name__}"
        # The lapsed-claim pair branches on one family; 400 and 409 are outside it.
        assert issubclass(tuples.ClaimNotFoundError, tuples.TupleError)
        assert not issubclass(mod.AspectUpsertBadRequestError, tuples.TupleError)
        assert not issubclass(mod.AspectUpsertConflictError, tuples.TupleError)

    def test_fenced_upsert_leaves_a_superseded_collection_refusal_alone(self, monkeypatch):
        from nexus.collection_errors import SupersededCollectionWriteError
        from nexus.db.t2.http_document_aspects_store import AspectUpsertConflictError

        wire = {
            "error": "collection_superseded", "reason": "collection_superseded",
            "collection": "test-coll", "superseded_by": "test-coll-2",
            "message": "retired",
        }
        store, _ = self._capturing_store(monkeypatch, 409, wire)
        with pytest.raises(SupersededCollectionWriteError) as caught:
            store.upsert(_make_aspect(), claim_id="c-1", claimant="W:0")
        assert not isinstance(caught.value, AspectUpsertConflictError)

    def test_an_unfenced_refusal_is_not_translated(self, monkeypatch):
        # Only the claim-bearing path translates; today's callers see today's error.
        store, _ = self._capturing_store(monkeypatch, 404, {"error": "ClaimNotFound", "detail": "x"})
        with pytest.raises(httpx.HTTPStatusError):
            store.upsert(_make_aspect())

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

    def test_get_by_doc_id_hands_its_timeout_to_the_request_and_defaults_to_the_clients(self):
        """nexus-xho6c: a caller spending one budget over several reads clamps each read's timeout."""
        seen: list[dict] = []

        def handle(request: httpx.Request) -> httpx.Response:
            seen.append(dict(request.extensions["timeout"]))
            return httpx.Response(404, json={})

        store = HttpDocumentAspectsStore(base_url="http://test", _token="tok")
        store._client = httpx.Client(transport=httpx.MockTransport(handle))
        assert store.get_by_doc_id("1.2.3", timeout=1.5, retry_read_timeout=False) is None
        assert store.get_by_doc_id("1.2.3") is None
        assert seen[0]["read"] == 1.5, "the per-call override reaches the request"
        assert seen[1]["read"] != 1.5, "no override: the client-wide default, as before"

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

    def test_delete_orphans_refuses_instead_of_returning_zero_zero(self):
        """Successor to test_delete_orphans_returns_zero_zero (nexus-ingey).

        The old contract returned (0, 0) so a caller could keep the signature
        without a delete happening on a backend that cannot confirm orphan
        status. The no-delete half was right; the RETURN was not. (0, 0) is
        indistinguishable from "examined everything, found nothing wrong", and
        `nx aspects gc` consumed it and printed a clean bill of health for a
        check that never ran. The value is withheld rather than made ambiguous.
        """
        store = self._store({})
        with pytest.raises(NotImplementedError, match="nexus-ingey"):
            store.delete_orphans(Path("/some/path.db"))

    @pytest.mark.t2_tenant
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

    def test_has_doc_id_pk_always_true(self):
        """PG schema always has doc_id as its primary key."""
        store = self._store({})
        assert store._has_doc_id_pk() is True


# ── HttpDocumentHighlightsStore unit tests ────────────────────────────────────


class TestHttpDocumentHighlightsStore:
    def _store(self, handlers: dict) -> HttpDocumentHighlightsStore:
        transport = _make_transport(handlers)
        store = HttpDocumentHighlightsStore(base_url="http://test", _token="tok")
        # Post-mixin-adoption (nexus-f2qvx.2): see the matching comment in
        # TestHttpDocumentAspectsStore._store above — no self._headers anymore.
        store._client = httpx.Client(transport=transport)
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
        # Post-mixin-adoption (nexus-f2qvx.2): see the matching comment in
        # TestHttpDocumentAspectsStore._store above — no self._headers anymore.
        store._client = httpx.Client(transport=transport)
        return store

    @pytest.mark.t2_tenant
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

    @pytest.mark.t2_tenant
    def test_enqueue_success_signals_local_wake(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """nexus-59611 stage 2: a successful enqueue touches the local wake
        signal so a same-box worker's poll wait returns early instead of
        waiting out DEFAULT_POLL_INTERVAL_S."""
        calls: list[None] = []
        monkeypatch.setattr(
            "nexus.aspect_worker.signal_aspect_wake",
            lambda *a, **k: calls.append(None),
        )
        store = self._queue({"/v1/aspects/queue/enqueue": {"ok": True}})
        store.enqueue("coll", "doc.pdf")
        assert len(calls) == 1

    @pytest.mark.t2_tenant
    def test_enqueue_failure_does_not_signal_wake(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A failed enqueue must not touch the wake file — there is no new
        row for a same-box worker to claim."""
        calls: list[None] = []
        monkeypatch.setattr(
            "nexus.aspect_worker.signal_aspect_wake",
            lambda *a, **k: calls.append(None),
        )
        store = self._queue({"/v1/aspects/queue/enqueue": RuntimeError("boom")})
        with pytest.raises(Exception, match="boom"):
            store.enqueue("coll", "doc.pdf")
        assert calls == []

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
        # nexus-575kd: the Java service's /queue/claim_batch sends a BARE JSON
        # ARRAY (AspectHandler.handleQueueClaimBatch -> writeValueAsString(rows)),
        # NOT a {"rows":[...]} envelope. The prior version of this test mocked
        # the envelope — i.e. the client's wrong assumption — so it stayed green
        # while the real service-mode worker claimed nothing (claim_batch raised
        # on every poll -> document_aspects=0). Mock what the service ACTUALLY
        # sends so this pins the real contract.
        rows = [
            {"collection": "c", "source_path": f"doc{i}.pdf",
             "content_hash": "", "content": "", "retry_count": 0, "doc_id": ""}
            for i in range(3)
        ]
        store = self._queue({
            "/v1/aspects/queue/claim_batch": rows  # bare array, as the service sends
        })
        result = store.claim_batch(5)
        assert len(result) == 3
        assert all(isinstance(r, QueueRow) for r in result)
        assert [r.source_path for r in result] == ["doc0.pdf", "doc1.pdf", "doc2.pdf"]

    def test_claim_batch_tolerates_rows_envelope(self):
        # Defensive: if a future engine ever wraps as {"rows":[...]} (matching
        # claim_next's envelope), the client must still parse it. Locks the
        # isinstance fallback branch.
        rows = [{"collection": "c", "source_path": "d.pdf",
                 "content_hash": "", "content": "", "retry_count": 0, "doc_id": ""}]
        store = self._queue({
            "/v1/aspects/queue/claim_batch": {"rows": rows}
        })
        result = store.claim_batch(5)
        assert len(result) == 1
        assert result[0].source_path == "d.pdf"

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

    def test_mark_retry_posts_interval_seconds(self):
        # RDR-163 P1 (nexus-ztpt6): the worker-chosen backoff must reach the
        # service so it can stamp next_retry_at = now()+interval server-side.
        captured = {}

        def handle_request(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            return httpx.Response(200, json={"ok": True})

        store = HttpAspectQueue(base_url="http://test", _token="tok")
        store._client = httpx.Client(transport=httpx.MockTransport(handle_request))
        store.mark_retry("c", "doc.pdf", interval_seconds=120)
        assert captured["body"]["interval_seconds"] == 120
        assert captured["body"]["collection"] == "c"
        assert captured["body"]["source_path"] == "doc.pdf"

    def test_reclaim_stale_returns_count(self):
        store = self._queue({"/v1/aspects/queue/reclaim_stale": {"reclaimed": 3}})
        assert store.reclaim_stale(300) == 3

    @pytest.mark.parametrize(
        ("kwargs", "expected"),
        [
            ({}, True),
            ({"count_attempt": True}, True),
            ({"count_attempt": False}, False),
        ],
    )
    def test_reclaim_stale_posts_count_attempt(self, kwargs, expected):
        # nexus-j67ix.3: the body always carries both keys; the stop sweep passes
        # count_attempt=False so only the age-based sweep spends an attempt.
        captured = {}

        def handle_request(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            return httpx.Response(200, json={"reclaimed": 2})

        store = HttpAspectQueue(base_url="http://test", _token="tok")
        store._client = httpx.Client(transport=httpx.MockTransport(handle_request))
        assert store.reclaim_stale(0, **kwargs) == 2
        assert captured["body"] == {"timeout_seconds": 0, "count_attempt": expected}

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

    def test_list_failed_returns_rows_and_preserves_doc_id(self):
        rows = [
            {"collection": "c", "source_path": "f.pdf",
             "content_hash": "h", "content": "x", "retry_count": 6, "doc_id": "1.2"}
        ]
        store = self._queue({"/v1/aspects/queue/list_failed": rows})
        result = store.list_failed()
        assert len(result) == 1
        assert isinstance(result[0], QueueRow)
        assert result[0].doc_id == "1.2"
        assert result[0].retry_count == 6

    def test_list_failed_forwards_collection_param(self):
        # nexus-2c51v: --collection scope must reach the service as a query param.
        captured = {}

        def handle_request(request: httpx.Request) -> httpx.Response:
            captured["params"] = dict(request.url.params)
            return httpx.Response(200, json=[])

        store = HttpAspectQueue(base_url="http://test", _token="tok")
        store._client = httpx.Client(transport=httpx.MockTransport(handle_request))
        store.list_failed(collection="knowledge__x")
        assert captured["params"].get("collection") == "knowledge__x"

    def test_list_pending_with_limit(self):
        rows = [
            {"collection": "c", "source_path": f"doc{i}.pdf",
             "content_hash": "", "content": "", "retry_count": 0, "doc_id": ""}
            for i in range(5)
        ]
        store = self._queue({"/v1/aspects/queue/list_pending": rows})
        result = store.list_pending(limit=5)
        assert len(result) == 5

    @pytest.mark.t2_tenant
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

    # test_sqlite_seam_when_backend_sqlite DELETED (nexus-i711w Stage 2
    # sub-stage A): the SQLite arms it pinned for aspect_queue and
    # document_highlights died with their stores; the remaining
    # document_aspects SQLite arm retires in sub-stage A3.

    @pytest.mark.t2_tenant
    def test_missing_port_raises(self, tmp_path, monkeypatch):
        """RuntimeError when NX_SERVICE_PORT is absent."""
        monkeypatch.setenv("NX_STORAGE_BACKEND_DOCUMENT_ASPECTS", "service")
        monkeypatch.delenv("NX_SERVICE_PORT", raising=False)
        monkeypatch.delenv("NX_SERVICE_TOKEN", raising=False)
        # Env hygiene: the engine-substrate fixture (and any ambient shell)
        # sets NX_SERVICE_URL, which routes resolution down the URL leg
        # before the no-port check this test pins.
        monkeypatch.delenv("NX_SERVICE_URL", raising=False)

        from nexus.db.t2 import T2Database
        with pytest.raises(RuntimeError, match="NX_SERVICE_PORT"):
            T2Database(tmp_path / "memory2.db", run_migrations=False)

    @pytest.mark.t2_tenant
    def test_missing_token_raises(self, tmp_path, monkeypatch):
        """RuntimeError when NX_SERVICE_TOKEN is absent."""
        monkeypatch.setenv("NX_STORAGE_BACKEND_DOCUMENT_ASPECTS", "service")
        monkeypatch.setenv("NX_SERVICE_PORT", "9999")
        monkeypatch.delenv("NX_SERVICE_TOKEN", raising=False)

        from nexus.db.t2 import T2Database
        with pytest.raises(RuntimeError, match="NX_SERVICE_TOKEN"):
            T2Database(tmp_path / "memory3.db", run_migrations=False)
