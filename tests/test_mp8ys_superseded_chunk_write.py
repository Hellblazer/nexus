# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-mp8ys: the engine refuses a chunk write to a retired (superseded)
collection name with a 409 ``collection_superseded`` naming the successor, and
the client must treat that as a typed, final refusal.

The refusal is a 409 and not the "not registered" 422 on purpose:
``write_with_registration_retry`` retries that 422 by re-registering, and the
registration upsert clears ``superseded_by``, so a retry would revive the very
name the refusal protects. These tests pin the client half of that contract
without an engine: the body mapper, both transports (the urllib vector client
and the httpx store mixin the combined writes ride), the retry helpers that
must not resend it, and the one-write-one-registration count through
``HttpCatalogClient.write_manifest_many``.
"""
from __future__ import annotations

import json
import urllib.error
from unittest.mock import MagicMock, patch

import httpx
import pytest

import nexus.corpus as corpus
from nexus.collection_errors import (
    SupersededCollectionWriteError,
    superseded_write_error_from_body,
)
from nexus.db.engine_reasons import COLLECTION_SUPERSEDED_REASON
from nexus.db.http_vector_client import VectorServiceError, _post
from nexus.retry import _is_connectivity_error, _is_retryable_vector_error

_OLD = "docs__mp8ys-old-1-1__bge-base-en-v15-768__v1"
_NEW = "docs__mp8ys-old-1-1__bge-base-en-v15-768__v2"


def _engine_body(**over: object) -> dict:
    """The body ``HttpUtil.sendTypedDbError`` sends for a refused write."""
    body: dict = {
        "error": COLLECTION_SUPERSEDED_REASON,
        "reason": COLLECTION_SUPERSEDED_REASON,
        "message": f"collection '{_OLD}' was superseded by '{_NEW}', so a write to it is refused",
        "collection": _OLD,
        "superseded_by": _NEW,
    }
    body.update(over)
    return body


@pytest.fixture(autouse=True)
def _clean_cache():
    corpus._REGISTERED_COLLECTIONS.clear()
    corpus._REGISTERED_COLLECTIONS_SCOPED.clear()
    yield
    corpus._REGISTERED_COLLECTIONS.clear()
    corpus._REGISTERED_COLLECTIONS_SCOPED.clear()


@pytest.fixture(autouse=True)
def _no_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(corpus, "_profile_model_for_content_type", lambda _ct: None)


# ── the body mapper ──────────────────────────────────────────────────────────


def test_the_engine_body_becomes_the_typed_error_naming_the_successor() -> None:
    err = superseded_write_error_from_body(409, _engine_body())
    assert isinstance(err, SupersededCollectionWriteError)
    assert (err.name, err.successor) == (_OLD, _NEW)
    assert repr(_NEW) in str(err)
    assert repr(_OLD) in str(err)


@pytest.mark.parametrize(
    "status, body",
    [
        pytest.param(422, _engine_body(), id="a-422-is-never-this-refusal"),
        pytest.param(None, _engine_body(), id="no-status"),
        pytest.param(409, _engine_body(reason="collection_model_mismatch"), id="another-409-reason"),
        pytest.param(409, {"error": COLLECTION_SUPERSEDED_REASON, "collection": _OLD, "superseded_by": _NEW},
                     id="no-reason-key"),
        pytest.param(409, _engine_body(superseded_by=""), id="blank-successor"),
        pytest.param(409, _engine_body(superseded_by=None), id="null-successor"),
        pytest.param(409, _engine_body(collection=""), id="blank-collection"),
        pytest.param(409, "collection_superseded", id="non-dict-body"),
        pytest.param(409, None, id="no-body"),
    ],
)
def test_anything_else_is_not_the_typed_error(status: int | None, body: object) -> None:
    assert superseded_write_error_from_body(status, body) is None


def test_the_reason_constant_is_the_value_the_engine_sends() -> None:
    # The engine side is pinned by HttpUtilPartitionErrorsTest and the route tests; this pins the
    # literal the client keys on, so a rename on one side cannot slip past.
    assert COLLECTION_SUPERSEDED_REASON == "collection_superseded"


# ── transport 1: the urllib vector client (upsert-chunks) ────────────────────


class _FakeFP:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def close(self) -> None:
        pass


def _http_error(code: int, body: dict) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "http://engine.test/v1/vectors/upsert-chunks", code, "error", None,
        _FakeFP(json.dumps(body).encode()),
    )


@pytest.fixture
def _vector_post_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_post`` on a write path asks the production-write guard and the endpoint resolver first;
    neither is under test and both would read the operator's real configuration."""
    monkeypatch.setattr("nexus.db.service_endpoint.guard_production_write", lambda _url: None)
    monkeypatch.setattr("nexus.db.http_vector_client._resolve_endpoint", lambda: ("http://engine.test", None))


def test_upsert_chunks_post_raises_the_typed_error_not_a_vector_service_error(_vector_post_env) -> None:
    with patch("nexus.db.http_vector_client._request", side_effect=_http_error(409, _engine_body())):
        with pytest.raises(SupersededCollectionWriteError) as caught:
            _post("/v1/vectors/upsert-chunks", {})
    assert not isinstance(caught.value, VectorServiceError)
    assert caught.value.successor == _NEW
    assert isinstance(caught.value.__cause__, urllib.error.HTTPError)


def test_upsert_chunks_post_keeps_the_generic_error_for_another_409(_vector_post_env) -> None:
    other = _engine_body(error="idempotency_conflict", reason="collection_model_mismatch")
    with patch("nexus.db.http_vector_client._request", side_effect=_http_error(409, other)):
        with pytest.raises(VectorServiceError) as caught:
            _post("/v1/vectors/upsert-chunks", {})
    assert caught.value.code == 409
    assert not isinstance(caught.value, SupersededCollectionWriteError)


def test_the_vector_retry_wrapper_sends_it_exactly_once(_vector_post_env) -> None:
    from nexus.retry import _vector_with_retry  # noqa: PLC0415 — test-local import

    sends = MagicMock(side_effect=_http_error(409, _engine_body()))
    with patch("nexus.db.http_vector_client._request", sends), patch("time.sleep") as slept:
        with pytest.raises(SupersededCollectionWriteError):
            _vector_with_retry(_post, "/v1/vectors/upsert-chunks", {}, retry_on_timeout=False)
    assert sends.call_count == 1
    slept.assert_not_called()


def test_the_vector_retry_classifier_calls_it_final(_vector_post_env) -> None:
    with patch("nexus.db.http_vector_client._request", side_effect=_http_error(409, _engine_body())):
        with pytest.raises(SupersededCollectionWriteError) as caught:
            _post("/v1/vectors/upsert-chunks", {})
    assert _is_retryable_vector_error(caught.value) is False
    assert _is_connectivity_error(caught.value) is False


# ── transport 2: the httpx store mixin (the combined writes) ─────────────────


class _Catalog:
    """An ``HttpCatalogClient`` over an in-memory engine that answers the collection
    row as live and refuses the chunk-carrying ``write_many`` as the real engine does."""

    def __init__(self, write_many_response: httpx.Response | None = None) -> None:
        from nexus.catalog.http_catalog_client import HttpCatalogClient  # noqa: PLC0415 — test-local import

        self.paths: list[str] = []
        self._write_many = write_many_response

        def handler(request: httpx.Request) -> httpx.Response:
            self.paths.append(request.url.path)
            if request.url.path.endswith("/manifest/write_many"):
                assert self._write_many is not None
                return self._write_many
            if request.url.path.endswith("/collections/get"):
                return httpx.Response(200, json={"name": _OLD, "superseded_by": ""})
            return httpx.Response(200, json={})

        self.client = HttpCatalogClient(base_url="http://engine.test", tenant="t", _token="x")
        self.client._client = httpx.Client(transport=httpx.MockTransport(handler))

    def write(self) -> dict:
        return self.client.write_manifest_many(
            [("1.1.1", [{"position": 0, "chash": "a" * 64, "chunk_index": 0}])],
            chunks=[{"chash": "a" * 64, "text": "text", "metadata": {}}],
            collection=_OLD,
        )

    def count(self, suffix: str) -> int:
        return sum(1 for p in self.paths if p.endswith(suffix))


@pytest.fixture
def _no_write_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("nexus.db.service_endpoint.guard_production_write", lambda _url: None)
    monkeypatch.setattr("nexus.db.t2._refreshable_client.guard_production_write", lambda _url: None)


def test_write_many_to_a_retired_name_raises_the_typed_error_after_one_send(_no_write_guard) -> None:
    cat = _Catalog(httpx.Response(409, json=_engine_body()))
    with pytest.raises(SupersededCollectionWriteError) as caught:
        cat.write()
    assert caught.value.successor == _NEW
    assert isinstance(caught.value.__cause__, httpx.HTTPStatusError)
    assert cat.count("/manifest/write_many") == 1, "the refusal is final: no resend"
    assert cat.count("/collections/upsert") <= 1, "no second registration (that upsert would revive the name)"
    assert _is_connectivity_error(caught.value) is False


def test_write_many_keeps_the_generic_error_for_another_409(_no_write_guard) -> None:
    cat = _Catalog(httpx.Response(409, json={"error": "idempotency_conflict", "reason": "something_else"}))
    with pytest.raises(httpx.HTTPStatusError) as caught:
        cat.write()
    assert caught.value.response.status_code == 409


def test_write_many_control_a_live_collection_still_succeeds(_no_write_guard) -> None:
    cat = _Catalog(httpx.Response(200, json={"chunks_written": 1, "dropped_chashes": {}, "complete_refused_count": 0}))
    assert cat.write()["chunks_written"] == 1


# ── write_with_registration_retry ────────────────────────────────────────────


def _writer() -> MagicMock:
    w = MagicMock()
    w.get_collection.return_value = {"superseded_by": ""}
    return w


def test_the_registration_helper_neither_retries_nor_re_registers_on_it() -> None:
    w = _writer()
    attempts: list[int] = []

    def write_fn() -> None:
        attempts.append(1)
        raise SupersededCollectionWriteError(_OLD, _NEW)

    with pytest.raises(SupersededCollectionWriteError) as caught:
        corpus.write_with_registration_retry(_OLD, write_fn, registrar=lambda: w)

    assert caught.value.successor == _NEW
    assert len(attempts) == 1, "the write is not sent again"
    assert w.register_collection.call_count == 1, "only the up-front registration of a cold name; no re-registration"


def test_the_registration_helper_expires_the_entry_so_the_next_write_revalidates() -> None:
    w = _writer()

    def write_fn() -> None:
        raise SupersededCollectionWriteError(_OLD, _NEW)

    with pytest.raises(SupersededCollectionWriteError):
        corpus.write_with_registration_retry(_OLD, write_fn, registrar=lambda: w)
    assert corpus._registration_cache_state(None, _OLD) == "stale"

    # The next write takes the cheap read and, the row now retired, is refused before it uploads.
    w.get_collection.return_value = {"superseded_by": _NEW}
    sent: list[int] = []
    with pytest.raises(SupersededCollectionWriteError):
        corpus.write_with_registration_retry(_OLD, lambda: sent.append(1), registrar=lambda: w)
    assert sent == []
    assert w.register_collection.call_count == 1


def test_a_scoped_registrar_expires_its_own_partition() -> None:
    w = _writer()

    def registrar() -> MagicMock:
        return w

    registrar.scope = ("http://engine", "tenant", "digest")  # type: ignore[attr-defined]

    def write_fn() -> None:
        raise SupersededCollectionWriteError(_OLD, _NEW)

    with pytest.raises(SupersededCollectionWriteError):
        corpus.write_with_registration_retry(_OLD, write_fn, registrar=registrar)
    assert corpus._registration_cache_state(registrar.scope, _OLD) == "stale"  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "exc",
    [
        pytest.param(VectorServiceError("409", code=409, reason=COLLECTION_SUPERSEDED_REASON), id="vector-409"),
        pytest.param(
            httpx.HTTPStatusError(
                "409", request=httpx.Request("POST", "http://e/x"),
                response=httpx.Response(409, json=_engine_body()),
            ),
            id="httpx-409",
        ),
        pytest.param(SupersededCollectionWriteError(_OLD, _NEW), id="typed"),
    ],
)
def test_it_is_never_the_422_the_registration_retry_repairs(exc: BaseException) -> None:
    assert corpus._looks_like_stale_registration_error(exc) is False


def test_the_unregistered_422_still_takes_the_repair_path() -> None:
    """Control: the narrowing above did not disable the retry it must not borrow."""
    w = _writer()
    calls: list[int] = []

    def write_fn() -> str:
        calls.append(1)
        if len(calls) == 1:
            raise VectorServiceError("collection is not registered", code=422, reason="unregistered_collection")
        return "ok"

    assert corpus.write_with_registration_retry(_OLD, write_fn, registrar=lambda: w) == "ok"
    assert len(calls) == 2
