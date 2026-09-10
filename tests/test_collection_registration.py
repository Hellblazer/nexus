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


# ── RDR-204 Phase 3 item 3 (nexus-ft04v.26): the registration-seam profile
# check, coordinator design correction 2026-09-09. A first attempt put this
# check INSIDE effective_embedding_model_for_writes (commit 5935b1bf8) and a
# full-suite run measured 155 failures: that chokepoint is reached from every
# write path with only the db/T3 layer mocked, so a real network call there
# broke tests that never anticipated one. The check moved HERE instead --
# ensure_collection_registered, immediately before its writer.register_collection
# call, where a catalog client is already about to be used for real I/O.


def _stub_profile_reader(monkeypatch: pytest.MonkeyPatch, rows: dict[str, str]) -> None:
    """Stub nexus.catalog.factory.make_catalog_reader() with a fixed
    {content_type: embedding_model} profile -- for pinning the seam's
    agree/mismatch/empty outcomes precisely."""
    import nexus.catalog.factory as factory_mod

    class _FixedReader:
        def embedding_profile(self) -> list[dict]:
            return [
                {"content_type": ct, "embedding_model": model, "dimension": 1024}
                for ct, model in rows.items()
            ]

    monkeypatch.setattr(factory_mod, "make_catalog_reader", lambda: _FixedReader())


class TestRegistrationSeamProfileCheck:
    # nexus-0y4c6: class attributes so the four tests below no longer
    # carry these literals in their own bodies -- the "intent" model fed
    # to a FAKE profile reader (_stub_profile_reader); the real profile
    # reader is never constructed.
    _MODEL = "voyage-code-3"
    _AGREE_NAME = "code__seam-agree-test__voyage-code-3__v1"
    _MISMATCH_NAME = "code__seam-mismatch-test__voyage-code-3__v1"
    _EMPTY_PROFILE_NAME = "code__seam-empty-profile-test__voyage-code-3__v1"
    _PRE_PHASE2_NAME = "code__seam-pre-phase2-test__voyage-code-3__v1"

    def test_profile_agrees_registration_proceeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)
        monkeypatch.setattr("nexus.config.local_embed_model_choice", lambda: self._MODEL)
        monkeypatch.setattr("nexus.config.get_credential", lambda name: "configured-key")
        _stub_profile_reader(monkeypatch, {"code": self._MODEL})
        writer = _fake_writer()
        name = self._AGREE_NAME

        ensure_collection_registered(name, registrar=lambda: writer)

        writer.register_collection.assert_called_once_with(
            name, content_type="code", owner_id="seam-agree-test",
            embedding_model=self._MODEL, model_version="v1",
        )

    def test_profile_disagrees_raises_mismatch_before_the_register_call(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The canonical repro: local intent is voyage (key present), but
        the engine's profile still says bge -- the service has not been
        restarted since the key was configured. Must raise
        EmbeddingProfileMismatchError naming the restart, and the wire
        call must NEVER happen."""
        from nexus.corpus import EmbeddingProfileMismatchError

        monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)
        monkeypatch.setattr("nexus.config.local_embed_model_choice", lambda: self._MODEL)
        monkeypatch.setattr("nexus.config.get_credential", lambda name: "configured-key")
        _stub_profile_reader(monkeypatch, {"code": "bge-base-en-v15-768"})
        writer = _fake_writer()
        name = self._MISMATCH_NAME

        with pytest.raises(EmbeddingProfileMismatchError, match="restart"):
            ensure_collection_registered(name, registrar=lambda: writer)

        writer.register_collection.assert_not_called()
        assert name not in corpus._REGISTERED_COLLECTIONS

    def test_empty_profile_proceeds_with_intent_bootstrap_case(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Engine-verified correction (CatalogRepository.upsertCollection):
        NO profile row for content_type is the bootstrap case, not a
        stale-config case -- registration proceeds with the derived
        intent, since that is exactly what the engine's own handler would
        accept and use to seed the row."""
        monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)
        monkeypatch.setattr("nexus.config.local_embed_model_choice", lambda: self._MODEL)
        monkeypatch.setattr("nexus.config.get_credential", lambda name: "configured-key")
        _stub_profile_reader(monkeypatch, {})  # no row for "code" at all
        writer = _fake_writer()
        name = self._EMPTY_PROFILE_NAME

        ensure_collection_registered(name, registrar=lambda: writer)

        writer.register_collection.assert_called_once_with(
            name, content_type="code", owner_id="seam-empty-profile-test",
            embedding_model=self._MODEL, model_version="v1",
        )

    def test_pre_phase_2_engine_route_missing_propagates_uncaught(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Against a pre-Phase-2 engine, EmbeddingProfileRouteMissingError
        propagates uncaught -- never wrapped, never a silent fallback,
        and the register call never happens."""
        from nexus.catalog.http_catalog_client import EmbeddingProfileRouteMissingError
        import nexus.catalog.factory as factory_mod

        monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)
        monkeypatch.setattr("nexus.config.local_embed_model_choice", lambda: self._MODEL)
        monkeypatch.setattr("nexus.config.get_credential", lambda name: "configured-key")

        class _PrePhase2Reader:
            def embedding_profile(self) -> list[dict]:
                raise EmbeddingProfileRouteMissingError("GET /v1/catalog/embedding_profile is not served")

        monkeypatch.setattr(factory_mod, "make_catalog_reader", lambda: _PrePhase2Reader())
        writer = _fake_writer()
        name = self._PRE_PHASE2_NAME

        with pytest.raises(EmbeddingProfileRouteMissingError):
            ensure_collection_registered(name, registrar=lambda: writer)

        writer.register_collection.assert_not_called()

    def test_none_catalog_reader_raises_named_error_not_attributeerror(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """make_catalog_reader() returning None (a storage-backend
        misconfiguration, or an incompletely faked test double -- its
        own docstring calls the Optional return type "historical" and
        callers' None-guards "dead but harmless" for a real install)
        must raise CatalogReaderUnavailableError, never a bare
        AttributeError two frames later on ``None.embedding_profile()``.
        Found live: tests/test_doc_indexer_pagination.py's
        TestStaleChunkPaginatedPruning tests reached exactly this path
        once doc_indexer.py started registering before its first read
        (nexus-ft04v.16 client half)."""
        from nexus.corpus import CatalogReaderUnavailableError
        import nexus.catalog.factory as factory_mod

        # nexus-ft04v.28 item 9 (2026-09-09): this test asserts nothing
        # about cloud/voyage intent -- only that a None catalog reader
        # raises CatalogReaderUnavailableError before the register call.
        # local_embed_model_choice / the collection name's model token
        # need only be SOME string collection_registration_kwargs can
        # derive a valid embedding_model from; a bge token exercises the
        # exact same code path (local_embed_model_is_voyage() is False
        # either way is fine here) without tripping the mode-declarations
        # lint's voyage-(context|code)-3 regex.
        monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)
        monkeypatch.setattr("nexus.config.local_embed_model_choice", lambda: "bge-base-en-v15-768")
        monkeypatch.setattr("nexus.config.get_credential", lambda name: "configured-key")
        monkeypatch.setattr(factory_mod, "make_catalog_reader", lambda: None)
        writer = _fake_writer()
        name = "code__seam-none-reader-test__bge-base-en-v15-768__v1"

        with pytest.raises(CatalogReaderUnavailableError, match="make_catalog_reader"):
            ensure_collection_registered(name, registrar=lambda: writer)

        writer.register_collection.assert_not_called()


class TestEnsureCollectionRegisteredExplicitKwargsOverride:
    """RDR-204 Phase 3 fix round (nexus-ft04v.28 item 4): ``kwargs``
    bypasses ``collection_registration_kwargs``'s generic name-derivation
    for *name*, for the callers that already hold the four fields (the
    backfill and rename commands, which read them off an existing row).
    The examples below use a name whose own shape would derive something
    else entirely, so a passing assertion can only mean the override drove
    the call. (The quarantine sibling is deliberately NOT a caller: the
    engine's GC function registers it from the origin's row on first
    insert; see ``indexer._prune_collection_serverside``.)"""

    # nexus-0y4c6: class attributes so the two tests below no longer
    # carry these literals in their own bodies -- the voyage-code-3 token
    # is part of the QUARANTINE COLLECTION NAME / the explicit kwargs
    # override dict, opaque data proving `kwargs=` bypasses (or still
    # profile-checks) name-derivation; no real embedder or credential
    # path is exercised.
    _NAME = "quarantine-code__myrepo__voyage-code-3__v1"
    _MODEL = "voyage-code-3"

    def test_explicit_kwargs_bypasses_name_derivation(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)
        monkeypatch.setattr(
            "nexus.db.http_vector_client.is_vector_service_mode", lambda: True,
        )
        writer = _fake_writer()
        # A name whose OWN shape would derive completely different kwargs
        # (content_type="quarantine-code", owner_id="myrepo__voyage-
        # code-3__v1") if collection_registration_kwargs(name) ran --
        # proving the override, not the name, drove the register call.
        name = self._NAME
        override = {
            "content_type": "code", "owner_id": "myrepo",
            "embedding_model": self._MODEL, "model_version": "v1",
        }

        ensure_collection_registered(name, registrar=lambda: writer, kwargs=override)

        writer.register_collection.assert_called_once_with(name, **override)

    def test_explicit_kwargs_still_runs_the_profile_check(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An explicit override does not bypass Technical Design 1a's
        mismatch guard -- only the generic name-parsing step that used
        to feed it."""
        from nexus.corpus import EmbeddingProfileMismatchError

        monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)
        monkeypatch.setattr("nexus.config.local_embed_model_choice", lambda: self._MODEL)
        monkeypatch.setattr("nexus.config.get_credential", lambda name: "configured-key")
        _stub_profile_reader(monkeypatch, {"code": "bge-base-en-v15-768"})
        writer = _fake_writer()
        name = self._NAME
        override = {
            "content_type": "code", "owner_id": "myrepo",
            "embedding_model": self._MODEL, "model_version": "v1",
        }

        with pytest.raises(EmbeddingProfileMismatchError, match="restart"):
            ensure_collection_registered(name, registrar=lambda: writer, kwargs=override)

        writer.register_collection.assert_not_called()

    def test_a_non_canonical_content_type_without_override_raises_under_cloud_mode(
        self, cloud_mode: None,
    ) -> None:
        """Without an override, a name whose content_type segment is not
        one of the canonical types cannot derive an embedding model under
        cloud mode and raises ValueError -- why a caller holding the real
        fields passes them instead of letting the name be parsed
        (nexus-ft04v.28 C1 was this failure, swallowed)."""
        with pytest.raises(ValueError, match="unknown content_type"):
            ensure_collection_registered("quarantine-code__myrepo__voyage-code-3__v1")


class TestEnsureCollectionRegisteredInvalidatesCollectionsCache:
    """RDR-204 Phase 3 (nexus-ft04v.26, fixture-seam round 2, coordinator
    diagnosis 2026-09-09): ensure_collection_registered must invalidate
    nexus.mcp_infra's collection-row cache on every successful path
    (fresh registration and the 409-already-registered race alike) --
    store_put/store_delete already do this on write (mcp/core.py); this
    registration funnel, which EVERY OTHER write path routes through
    (T3 chunks, aspects, taxonomy, the doc indexer), did not, so a
    newly-registered collection could stay invisible to resolve_corpus's
    bare-corpus fan-out for the cache's remaining 60s TTL in any
    long-lived process (the MCP server; an in-process CliRunner test
    chaining index-then-search calls in one Python process) -- found
    live via test_scenario_journeys.py::test_index_repo_routes_code_to_
    code_corpus, whose ``nx search --corpus code --json`` (a second
    CliRunner.invoke in the SAME test/process) returned empty stdout
    immediately after ``nx index repo`` registered the code__ collection
    moments earlier."""

    def test_successful_registration_invalidates_the_stale_cache(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import time

        import nexus.mcp_infra as mcp_infra

        monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)
        monkeypatch.setattr(
            "nexus.db.http_vector_client.is_vector_service_mode", lambda: True,
        )
        _stub_profile_reader(monkeypatch, {})
        # Simulate a cache warmed BEFORE this collection existed (an
        # earlier list_collections() call in the same process, well
        # within the 60s TTL) -- a fresh timestamp, but no row for the
        # collection this test is about to register.
        monkeypatch.setattr(
            mcp_infra, "_collections_cache",
            ([], {}, {}, time.monotonic()),
        )
        writer = _fake_writer()
        name = "knowledge__cache-invalidation-test"

        ensure_collection_registered(name, registrar=lambda: writer)

        assert mcp_infra._collections_cache == ([], {}, {}, 0.0), (
            "ensure_collection_registered must invalidate the stale "
            "collections cache (reset to the sentinel empty/zero-"
            "timestamp state _refresh_collections_cache_if_stale treats "
            "as unconditionally stale) so the NEXT get_collection_row "
            "call in this process re-fetches and sees the row this "
            "registration just created, instead of serving the "
            "pre-registration snapshot for the rest of the TTL window."
        )

    def test_409_already_registered_race_also_invalidates(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The 409 (another process won the race to register the same
        name) path must ALSO invalidate -- the collection is new to
        THIS process's cache either way."""
        import time

        import httpx

        import nexus.mcp_infra as mcp_infra

        monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)
        monkeypatch.setattr(
            "nexus.db.http_vector_client.is_vector_service_mode", lambda: True,
        )
        _stub_profile_reader(monkeypatch, {})
        monkeypatch.setattr(
            mcp_infra, "_collections_cache",
            ([], {}, {}, time.monotonic()),
        )
        writer = _fake_writer()
        response = MagicMock()
        response.status_code = 409
        writer.register_collection.side_effect = httpx.HTTPStatusError(
            "already registered", request=MagicMock(), response=response,
        )
        name = "knowledge__cache-invalidation-409-test"

        ensure_collection_registered(name, registrar=lambda: writer)

        assert mcp_infra._collections_cache == ([], {}, {}, 0.0)


class TestRegisterCollectionCallSitesRouteThroughTheSeam:
    """RDR-204 Phase 3 fix round (nexus-ft04v.28 item 4): every
    ``.register_collection(`` call site outside ``corpus.py`` itself
    must either go through :func:`ensure_collection_registered` (the
    seam, which gains the early ``EmbeddingProfileMismatchError``
    diagnostic) or be one of a small, explicitly named set of accepted
    exceptions this test pins. A NEW site landing outside both means a
    write path bypassed the seam's profile check silently -- exactly
    the coverage gap code-review-nexus-ft04v.29 flagged (Significant
    finding: "four call sites register without going through
    ensure_collection_registered").

    ``ensure_collection_registered`` itself calls ``writer.
    register_collection(`` internally (corpus.py) -- that ONE call site
    is what every routed caller now reaches through, and is excluded by
    name (it IS the seam, not a bypass of it).
    """

    #: Files (relative to the repo root) that call ``.register_collection(``
    #: WITHOUT going through the seam, reviewed and accepted as out of
    #: nexus-ft04v.28's scope. A NEW entry here requires a documented
    #: reason, same discipline as every other census allowlist in this
    #: suite (tests/test_collection_name_parse_census.py's own doctrine).
    _ACCEPTED_NON_SEAM_SITES = frozenset({
        # indexer.py:792/800 -- RDR-103 Phase 4 migration rename-cascade:
        # best-effort, non-fatal registration immediately after a
        # legacy->conformant data-plane rename, inside its own dedicated
        # try/except (phase4_register_collection_failed_after_rename).
        # Not one of the four sites nexus-ft04v.29's finding named.
        "src/nexus/indexer.py",
        # commands/catalog_cmds/migration.py:190 -- legacy migration
        # command, not one of the four named sites.
        "src/nexus/commands/catalog_cmds/migration.py",
        # commands/catalog_cmds/doctor.py:454 -- a STRING inside an
        # error-message remediation suggestion
        # ("w.register_collection('<TARGET>'); ..."), never a real call;
        # matches the substring scan below but is not executable code.
        "src/nexus/commands/catalog_cmds/doctor.py",
    })

    def test_non_seam_register_collection_call_sites_are_the_pinned_set(self) -> None:
        import pathlib

        repo_root = pathlib.Path(__file__).resolve().parent.parent
        src_root = repo_root / "src" / "nexus"
        needle = ".register_collection("
        offenders: set[str] = set()
        for path in src_root.rglob("*.py"):
            if path == src_root / "corpus.py":
                continue
            if needle in path.read_text():
                offenders.add(str(path.relative_to(repo_root)))

        assert offenders == set(self._ACCEPTED_NON_SEAM_SITES), (
            f"register_collection call sites outside corpus.py changed: "
            f"got {sorted(offenders)}, expected "
            f"{sorted(self._ACCEPTED_NON_SEAM_SITES)}. A NEW site here "
            f"bypasses the registration seam's profile-mismatch check -- "
            f"route it through ensure_collection_registered, or add it "
            f"to _ACCEPTED_NON_SEAM_SITES with a documented reason if it "
            f"is a genuine, reviewed exception. A site that DISAPPEARED "
            f"(now routed through the seam) should be removed from "
            f"_ACCEPTED_NON_SEAM_SITES, not left stale."
        )

    def test_the_four_named_sites_are_gone_from_the_offender_set(self) -> None:
        """Non-vacuity: the four sites code-review-nexus-ft04v.29 named
        (commands/collection.py, commands/catalog_cmds/collections.py x2,
        commands/index.py) must NOT appear in the pinned allowlist --
        proving this test would have caught them before the fix, not
        just after."""
        named_before_the_fix = {
            "src/nexus/commands/collection.py",
            "src/nexus/commands/catalog_cmds/collections.py",
            "src/nexus/commands/index.py",
        }
        assert not (named_before_the_fix & self._ACCEPTED_NON_SEAM_SITES)
