# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-204 Phase 1 client half (nexus-f5wwx).

``nexus.corpus.collection_registration_kwargs`` / ``ensure_collection_registered``
are the one shared derivation + idempotent-register step every client
write path that used to rely on the engine's now-retired auto-registration
(store_put, the aspects store, taxonomy persistence) reuses. These tests
cover the derivation in isolation (local and cloud mode, per the mode
census) and the registration side effect against a fake writer — no
substrate required.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest

import nexus.corpus as corpus
from nexus.corpus import (
    collection_registration_kwargs,
    effective_embedding_model_for_writes,
    ensure_collection_registered,
    write_with_registration_retry,
)


@pytest.fixture(autouse=True)
def _clear_registration_cache():
    """Each test uses its own collection name, but clear the module-level
    cache anyway so a leaked entry from a failed prior test run in the
    same worker process can never mask a regression."""
    corpus._REGISTERED_COLLECTIONS.clear()
    yield
    corpus._REGISTERED_COLLECTIONS.clear()


# ── collection_registration_kwargs: derivation ──────────────────────────


def test_conformant_name_local_mode_sends_the_bge_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A conformant 4-segment name has its content_type/owner_id/model_
    version parsed from the name; embedding_model is ALWAYS the current
    profile's write-time model, never the token in the name. (No cloud
    token named here on purpose: this test pins local mode.)"""
    monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)
    monkeypatch.setattr(
        "nexus.db.http_vector_client.is_vector_service_mode", lambda: True,
    )
    name = "docs__myrepo-1-1__minilm-l6-v2-384__v1"  # token deliberately not the local write model

    kwargs = collection_registration_kwargs(name)

    assert kwargs["content_type"] == "docs"
    assert kwargs["owner_id"] == "myrepo-1-1"
    assert kwargs["model_version"] == "v1"
    assert kwargs["embedding_model"].startswith("bge-")
    assert kwargs["embedding_model"] == effective_embedding_model_for_writes("docs")


@pytest.mark.parametrize(
    ("content_type", "collection_prefix"),
    [
        ("docs", "docs"),
        ("code", "code"),
        ("knowledge", "knowledge"),
    ],
)
def test_conformant_name_cloud_mode_sends_content_type_appropriate_model(
    cloud_mode: None, content_type: str, collection_prefix: str,
) -> None:
    name = f"{collection_prefix}__myrepo-1-1__stale-placeholder-token__v2"

    kwargs = collection_registration_kwargs(name)

    assert kwargs["content_type"] == content_type
    assert kwargs["owner_id"] == "myrepo-1-1"
    assert kwargs["model_version"] == "v2"
    assert kwargs["embedding_model"] == effective_embedding_model_for_writes(content_type)
    # The stale token in the name must never be what gets sent.
    assert kwargs["embedding_model"] != "stale-placeholder-token"


def test_legacy_two_segment_name_derives_content_type_and_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A grandfathered ``knowledge__<subject>`` name (RDR-101, no model/
    version segments) still derives a full registration: content_type
    and owner_id from the two segments, model_version defaults to v1,
    embedding_model from the current profile."""
    monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)
    monkeypatch.setattr(
        "nexus.db.http_vector_client.is_vector_service_mode", lambda: True,
    )
    name = "knowledge__distributed-systems"

    kwargs = collection_registration_kwargs(name)

    assert kwargs == {
        "content_type": "knowledge",
        "owner_id": "distributed-systems",
        "embedding_model": effective_embedding_model_for_writes("knowledge"),
        "model_version": "v1",
    }


def test_legacy_name_with_extra_underscored_segments_joins_the_remainder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-conformant name with MORE than two ``__``-separated segments
    (not matching the strict 4-segment regex, e.g. a hand-typed name)
    still yields a usable owner_id: everything after the first segment,
    rejoined, rather than silently dropping data."""
    monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)
    monkeypatch.setattr(
        "nexus.db.http_vector_client.is_vector_service_mode", lambda: True,
    )
    name = "knowledge__foo__bar"

    kwargs = collection_registration_kwargs(name)

    assert kwargs["content_type"] == "knowledge"
    assert kwargs["owner_id"] == "foo__bar"


def test_name_with_no_separator_defaults_to_knowledge_like_t3_collection_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bare name with no ``__`` at all mirrors
    ``t3_collection_name``'s own established bare-name convention (a
    content-type-less string promotes under ``knowledge``) rather than
    raising — many existing call sites (taxonomy/aspect tests, and any
    caller that has not yet run a name through ``t3_collection_name``)
    pass a bare identifier as an opaque collection reference."""
    monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)
    monkeypatch.setattr(
        "nexus.db.http_vector_client.is_vector_service_mode", lambda: True,
    )
    kwargs = collection_registration_kwargs("proj")

    assert kwargs["content_type"] == "knowledge"
    assert kwargs["owner_id"] == "proj"
    assert kwargs["model_version"] == "v1"
    assert kwargs["embedding_model"] == effective_embedding_model_for_writes("knowledge")


def test_name_with_empty_segment_raises_valueerror() -> None:
    """A name that DOES carry a ``__`` separator but with an empty side
    (``"__foo"`` / ``"foo__"``) is genuinely malformed, not a bare
    identifier — this still fails loud."""
    with pytest.raises(ValueError, match="no <content_type>__<owner_id> shape"):
        collection_registration_kwargs("__foo")


def test_non_canonical_content_type_is_not_rejected_by_this_function(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A content_type outside the four canonical types is NOT rejected
    here: the engine's own constraint is NOT NULL + non-empty, not an
    enum, and a large pre-existing test surface uses non-canonical
    placeholder segments (``test__coll``) as opaque collection
    identifiers. Validation of content_type, where it exists, is
    effective_embedding_model_for_writes's own (cloud/voyage branch
    only, via canonical_embedding_model) — unchanged by this
    function."""
    monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)
    monkeypatch.setattr(
        "nexus.db.http_vector_client.is_vector_service_mode", lambda: True,
    )
    kwargs = collection_registration_kwargs("bogus__owner")

    assert kwargs["content_type"] == "bogus"
    assert kwargs["owner_id"] == "owner"
    assert kwargs["embedding_model"] == effective_embedding_model_for_writes("bogus")


def test_non_canonical_content_type_still_raises_under_cloud_mode(
    cloud_mode: None,
) -> None:
    """Under cloud mode, effective_embedding_model_for_writes's OWN
    validation (canonical_embedding_model) still fires for a
    non-canonical content_type — this function does not suppress it."""
    with pytest.raises(ValueError, match="unknown content_type"):
        collection_registration_kwargs("bogus__owner")


# ── ensure_collection_registered: side effect + cache ───────────────────


def _fake_writer() -> MagicMock:
    writer = MagicMock()
    writer.register_collection.return_value = None
    return writer


def test_registers_once_then_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)
    monkeypatch.setattr(
        "nexus.db.http_vector_client.is_vector_service_mode", lambda: True,
    )
    writer = _fake_writer()
    name = "knowledge__ensure-once-test"

    ensure_collection_registered(name, registrar=lambda: writer)
    ensure_collection_registered(name, registrar=lambda: writer)

    writer.register_collection.assert_called_once()
    writer.close.assert_called_once()


def test_registrar_receives_the_derived_kwargs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)
    monkeypatch.setattr(
        "nexus.db.http_vector_client.is_vector_service_mode", lambda: True,
    )
    writer = _fake_writer()
    name = "docs__myrepo-9-9__bge-base-en-v15-768__v1"

    ensure_collection_registered(name, registrar=lambda: writer)

    expected = collection_registration_kwargs(name)
    writer.register_collection.assert_called_once_with(name, **expected)


def test_409_is_treated_as_already_registered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 409 (another process won the registration race) is idempotent
    success, not a failure — the name is still cached."""
    monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)
    monkeypatch.setattr(
        "nexus.db.http_vector_client.is_vector_service_mode", lambda: True,
    )
    writer = _fake_writer()
    response = MagicMock(status_code=409)
    writer.register_collection.side_effect = httpx.HTTPStatusError(
        "conflict", request=MagicMock(), response=response,
    )
    name = "knowledge__ensure-409-test"

    ensure_collection_registered(name, registrar=lambda: writer)  # must not raise

    assert name in corpus._REGISTERED_COLLECTIONS
    writer.close.assert_called_once()


def test_non_409_http_error_propagates_and_is_not_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)
    monkeypatch.setattr(
        "nexus.db.http_vector_client.is_vector_service_mode", lambda: True,
    )
    writer = _fake_writer()
    response = MagicMock(status_code=422)
    writer.register_collection.side_effect = httpx.HTTPStatusError(
        "profile mismatch", request=MagicMock(), response=response,
    )
    name = "knowledge__ensure-422-test"

    with pytest.raises(httpx.HTTPStatusError):
        ensure_collection_registered(name, registrar=lambda: writer)

    assert name not in corpus._REGISTERED_COLLECTIONS
    writer.close.assert_called_once()


def test_writer_is_closed_even_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)
    monkeypatch.setattr(
        "nexus.db.http_vector_client.is_vector_service_mode", lambda: True,
    )
    writer = _fake_writer()
    writer.register_collection.side_effect = RuntimeError("boom")
    name = "knowledge__ensure-close-on-failure-test"

    with pytest.raises(RuntimeError):
        ensure_collection_registered(name, registrar=lambda: writer)

    writer.close.assert_called_once()


def test_default_registrar_is_make_catalog_writer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No registrar supplied: falls back to
    ``nexus.catalog.factory.make_catalog_writer`` — proves the default
    wiring without a live service (the factory call itself is faked)."""
    monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)
    monkeypatch.setattr(
        "nexus.db.http_vector_client.is_vector_service_mode", lambda: True,
    )
    writer = _fake_writer()
    made = MagicMock(return_value=writer)
    monkeypatch.setattr("nexus.catalog.factory.make_catalog_writer", made)
    name = "knowledge__ensure-default-registrar-test"

    ensure_collection_registered(name)

    made.assert_called_once_with()
    writer.register_collection.assert_called_once()


# ── write_with_registration_retry: the boot-sweep race ──────────────────


def _http_422(body_text: str) -> httpx.HTTPStatusError:
    response = MagicMock(status_code=422)
    response.text = body_text
    return httpx.HTTPStatusError(body_text, request=MagicMock(), response=response)


def test_normal_path_registers_once_and_writes_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)
    monkeypatch.setattr(
        "nexus.db.http_vector_client.is_vector_service_mode", lambda: True,
    )
    writer = _fake_writer()
    name = "knowledge__retry-happy-path-test"
    write_fn = MagicMock(return_value="ok")

    result = write_with_registration_retry(name, write_fn, registrar=lambda: writer)

    assert result == "ok"
    writer.register_collection.assert_called_once()
    write_fn.assert_called_once()


def test_stale_registration_422_retries_once_after_re_registering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The engine's per-tenant boot sweep (RDR-204 Technical Design step
    3) deleted the collection this process registered earlier — the
    write's first attempt 422s "not registered"; the helper evicts the
    cache, re-registers, and retries the write once, successfully."""
    monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)
    monkeypatch.setattr(
        "nexus.db.http_vector_client.is_vector_service_mode", lambda: True,
    )
    writer = _fake_writer()
    name = "knowledge__retry-stale-sweep-test"
    write_fn = MagicMock(
        side_effect=[
            _http_422("collection 'knowledge__retry-stale-sweep-test' is not "
                       "registered for tenant 'nexus'"),
            "ok-on-retry",
        ],
    )

    result = write_with_registration_retry(name, write_fn, registrar=lambda: writer)

    assert result == "ok-on-retry"
    assert write_fn.call_count == 2
    # Registered twice: once up front, once after the eviction.
    assert writer.register_collection.call_count == 2
    assert name in corpus._REGISTERED_COLLECTIONS


def test_second_failure_after_retry_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)
    monkeypatch.setattr(
        "nexus.db.http_vector_client.is_vector_service_mode", lambda: True,
    )
    writer = _fake_writer()
    name = "knowledge__retry-double-fail-test"
    err = _http_422("collection 'knowledge__retry-double-fail-test' is not registered")
    write_fn = MagicMock(side_effect=[err, err])

    with pytest.raises(httpx.HTTPStatusError):
        write_with_registration_retry(name, write_fn, registrar=lambda: writer)

    assert write_fn.call_count == 2


def test_a_different_422_is_never_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A profile-mismatch 422 (RDR-204 Technical Design step 2, 'names
    a different model') must propagate immediately, unretried — only
    the specific 'not registered' shape triggers the one-shot repair."""
    monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)
    monkeypatch.setattr(
        "nexus.db.http_vector_client.is_vector_service_mode", lambda: True,
    )
    writer = _fake_writer()
    name = "knowledge__retry-profile-mismatch-test"
    err = _http_422("embedding_model 'some-other-model' does not match the "
                     "install profile's 'bge-base-en-v15-768'")
    write_fn = MagicMock(side_effect=err)

    with pytest.raises(httpx.HTTPStatusError):
        write_with_registration_retry(name, write_fn, registrar=lambda: writer)

    write_fn.assert_called_once()


def test_a_non_http_error_is_never_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)
    monkeypatch.setattr(
        "nexus.db.http_vector_client.is_vector_service_mode", lambda: True,
    )
    writer = _fake_writer()
    name = "knowledge__retry-non-http-error-test"
    write_fn = MagicMock(side_effect=RuntimeError("boom"))

    with pytest.raises(RuntimeError):
        write_with_registration_retry(name, write_fn, registrar=lambda: writer)

    write_fn.assert_called_once()


class _FakeVectorServiceError(RuntimeError):
    """Duck-typed stand-in for ``nexus.db.http_vector_client.
    VectorServiceError`` (a plain ``.code`` int, message carries the
    engine's error body) — this test suite does not import that class
    directly, matching ``_looks_like_stale_registration_error``'s own
    deliberate duck-typed match (no import cycle with http_vector_client)."""

    def __init__(self, message: str, *, code: int) -> None:
        super().__init__(message)
        self.code = code


def test_vector_service_error_shaped_422_is_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The T3 vector client (http_vector_client.py) is urllib-based and
    raises VectorServiceError, not httpx.HTTPStatusError — the duck-typed
    ``.code`` branch must catch this shape too."""
    monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)
    monkeypatch.setattr(
        "nexus.db.http_vector_client.is_vector_service_mode", lambda: True,
    )
    writer = _fake_writer()
    name = "knowledge__retry-vector-service-error-test"
    write_fn = MagicMock(
        side_effect=[
            _FakeVectorServiceError(
                "POST /v1/vectors/upsert-chunks -> HTTP 422: collection "
                "'knowledge__retry-vector-service-error-test' is not "
                "registered for tenant 'nexus'",
                code=422,
            ),
            "ok-on-retry",
        ],
    )

    result = write_with_registration_retry(name, write_fn, registrar=lambda: writer)

    assert result == "ok-on-retry"
    assert write_fn.call_count == 2


def test_vector_service_error_shaped_non_422_is_never_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)
    monkeypatch.setattr(
        "nexus.db.http_vector_client.is_vector_service_mode", lambda: True,
    )
    writer = _fake_writer()
    name = "knowledge__retry-vector-service-error-500-test"
    write_fn = MagicMock(
        side_effect=_FakeVectorServiceError("POST failed: HTTP 500", code=500),
    )

    with pytest.raises(_FakeVectorServiceError):
        write_with_registration_retry(name, write_fn, registrar=lambda: writer)

    write_fn.assert_called_once()
