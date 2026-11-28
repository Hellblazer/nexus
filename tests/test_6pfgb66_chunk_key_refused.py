# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-6pfgb.66 (RDR-236 Phase 4 Step 2c, client half): the engine refuses a chunk write whose
metadata names a key outside the CHUNK set with a 422 ``chunk_metadata_key_refused``, and the
client must treat that as a typed, final refusal that names each key and where it lives instead.

The five routes are ``upsert-chunks`` and ``update-metadata`` (the urllib vector client) and the
combined manifest writes ``append``, ``append_many`` and ``write_many`` (the httpx store mixin).
Same shape as ``test_mp8ys_superseded_chunk_write``: the body mapper, both transports, the retry
helpers that must not resend it, the registration helper, and the two surfaces a person reads
(the CLI boundary and the MCP tool error). Only the HTTP response is faked.
"""
from __future__ import annotations

import json
import re
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import click
import httpx
import pytest

import nexus.corpus as corpus
from nexus.collection_errors import (
    ChunkMetadataKeyRefusedError,
    chunk_key_refused_error_from_body,
)
from nexus.db.engine_reasons import CHUNK_METADATA_KEY_REFUSED_REASON
from nexus.db.http_vector_client import VectorServiceError, _post
from nexus.retry import _is_connectivity_error, _is_retryable_vector_error

_COLLECTION = "docs__6pfgb66-1-1__bge-base-en-v15-768__v1"

_REFUSED = [
    {"key": "title", "class": "DOCUMENT", "home": "the catalog document (catalog_documents.title)"},
    {"key": "line_start", "class": "POSITION", "home": "the manifest row (catalog_document_chunks.line_start)"},
    {"key": "made_up_key", "class": "UNKNOWN", "home": "nowhere: drop it"},
]


def _engine_body(**over: object) -> dict:
    """The 422 body the engine sends (RDR-236 Phase 4, HttpUtil typed-body convention)."""
    body: dict = {
        "reason": CHUNK_METADATA_KEY_REFUSED_REASON,
        "error": "chunk metadata names keys a chunk row does not carry: title, line_start, made_up_key",
        "route": "/v1/vectors/upsert-chunks",
        "collection": _COLLECTION,
        "collection_id": 7,
        "refused": _REFUSED,
        "refused_chunks": 2,
        "requested_count": 5,
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


def test_the_engine_body_becomes_the_typed_error_naming_every_key_and_its_home() -> None:
    err = chunk_key_refused_error_from_body(422, _engine_body())
    assert isinstance(err, ChunkMetadataKeyRefusedError)
    text = str(err)
    for entry in _REFUSED:
        assert entry["key"] in text
        assert entry["class"] in text
        assert entry["home"] in text
    assert _COLLECTION in text
    assert "/v1/vectors/upsert-chunks" in text
    assert [(k.key, k.cls, k.home) for k in err.refused] == [(e["key"], e["class"], e["home"]) for e in _REFUSED]
    assert (err.route, err.collection, err.collection_id) == ("/v1/vectors/upsert-chunks", _COLLECTION, 7)
    assert (err.refused_chunks, err.requested_count) == (2, 5)


def test_a_body_with_no_refused_list_falls_back_to_the_engines_prose() -> None:
    body = _engine_body()
    del body["refused"]
    err = chunk_key_refused_error_from_body(422, body)
    assert isinstance(err, ChunkMetadataKeyRefusedError)
    assert err.refused == ()
    assert body["error"] in str(err)


def test_a_body_with_only_the_reason_still_maps_and_says_what_happened() -> None:
    err = chunk_key_refused_error_from_body(422, {"reason": CHUNK_METADATA_KEY_REFUSED_REASON})
    assert isinstance(err, ChunkMetadataKeyRefusedError)
    assert "chunk" in str(err).lower() and "metadata" in str(err).lower()


@pytest.mark.parametrize(
    "refused",
    [
        pytest.param("title", id="refused-is-a-string"),
        pytest.param([None, 3, "x", {"class": "DOCUMENT"}, {"key": ""}], id="entries-without-a-key"),
        pytest.param({"key": "title"}, id="refused-is-an-object"),
    ],
)
def test_a_malformed_refused_list_never_raises_from_the_mapper(refused: object) -> None:
    err = chunk_key_refused_error_from_body(422, _engine_body(refused=refused))
    assert isinstance(err, ChunkMetadataKeyRefusedError)
    assert err.refused == ()
    assert _engine_body()["error"] in str(err)


def test_a_refused_entry_missing_class_or_home_still_names_its_key() -> None:
    err = chunk_key_refused_error_from_body(422, _engine_body(refused=[{"key": "title"}, {"key": "x", "home": "h"}]))
    assert isinstance(err, ChunkMetadataKeyRefusedError)
    assert [k.key for k in err.refused] == ["title", "x"]
    assert "title" in str(err)
    assert "h" in str(err)


def test_non_numeric_counts_and_ids_are_dropped_not_raised() -> None:
    err = chunk_key_refused_error_from_body(
        422, _engine_body(refused_chunks="many", requested_count=None, collection_id="seven"))
    assert isinstance(err, ChunkMetadataKeyRefusedError)
    assert (err.refused_chunks, err.requested_count, err.collection_id) == (None, None, None)


@pytest.mark.parametrize(
    "status, body",
    [
        pytest.param(200, _engine_body(), id="a-success-is-never-this-refusal"),
        pytest.param(422, _engine_body(reason="ownerless_chunk_write"), id="another-422-reason"),
        pytest.param(422, _engine_body(reason="unregistered_collection"), id="the-unregistered-422"),
        pytest.param(422, {"error": "chunk_metadata_key_refused"}, id="no-reason-key"),
        pytest.param(422, _engine_body(reason=""), id="blank-reason"),
        pytest.param(422, _engine_body(reason=None), id="null-reason"),
        pytest.param(422, "chunk_metadata_key_refused", id="non-dict-body"),
        pytest.param(422, None, id="no-body"),
    ],
)
def test_anything_else_is_not_the_typed_error(status: int | None, body: object) -> None:
    assert chunk_key_refused_error_from_body(status, body) is None


def test_the_reason_constant_is_the_value_the_engine_sends() -> None:
    assert CHUNK_METADATA_KEY_REFUSED_REASON == "chunk_metadata_key_refused"


def test_the_client_constant_equals_the_engines_once_the_engine_declares_it() -> None:
    """Parity with ``HttpUtil`` where the engine half has landed. Until then the declaration is
    absent and this reports a skip naming that, and ``test_the_reason_constant_is_the_value_the_engine_sends``
    above is the client-side pin. After the engine branch merges, move the name into
    ``test_engine_reasons.test_the_rdr225_partition_reasons_equal_the_engines``'s list."""
    http_util = Path(__file__).resolve().parents[1] / "service/src/main/java/dev/nexus/service/http/HttpUtil.java"
    match = re.search(r'CHUNK_METADATA_KEY_REFUSED_REASON\s*=\s*"([a-z_]+)"', http_util.read_text())
    if match is None:
        pytest.skip("HttpUtil does not declare CHUNK_METADATA_KEY_REFUSED_REASON yet (engine half of nexus-6pfgb.66)")
    assert match.group(1) == CHUNK_METADATA_KEY_REFUSED_REASON


# ── transport 1: the urllib vector client (upsert-chunks, update-metadata) ───


class _FakeFP:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def close(self) -> None:
        pass


def _http_error(code: int, body: dict, route: str = "/v1/vectors/upsert-chunks") -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        f"http://engine.test{route}", code, "error", None, _FakeFP(json.dumps(body).encode()),
    )


@pytest.fixture
def _vector_post_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("nexus.db.service_endpoint.guard_production_write", lambda _url: None)
    monkeypatch.setattr("nexus.db.http_vector_client._resolve_endpoint", lambda: ("http://engine.test", None))


@pytest.mark.parametrize("route", ["/v1/vectors/upsert-chunks", "/v1/vectors/update-metadata"])
def test_the_vector_post_raises_the_typed_error_not_a_vector_service_error(_vector_post_env, route: str) -> None:
    with patch("nexus.db.http_vector_client._request", side_effect=_http_error(422, _engine_body(route=route), route)):
        with pytest.raises(ChunkMetadataKeyRefusedError) as caught:
            _post(route, {})
    assert not isinstance(caught.value, VectorServiceError)
    assert [k.key for k in caught.value.refused] == ["title", "line_start", "made_up_key"]
    assert isinstance(caught.value.__cause__, urllib.error.HTTPError)


def test_the_vector_post_keeps_the_generic_error_for_another_422(_vector_post_env) -> None:
    other = {"error": "ownerless", "reason": "ownerless_chunk_write"}
    with patch("nexus.db.http_vector_client._request", side_effect=_http_error(422, other)):
        with pytest.raises(VectorServiceError) as caught:
            _post("/v1/vectors/upsert-chunks", {})
    assert caught.value.code == 422
    assert not isinstance(caught.value, ChunkMetadataKeyRefusedError)


def test_the_vector_retry_wrapper_sends_it_exactly_once(_vector_post_env, monkeypatch) -> None:
    import nexus.retry as retry_mod  # noqa: PLC0415 — test-local import
    from nexus.retry import _vector_with_retry  # noqa: PLC0415 — test-local import

    from tests._module_seam import setattr_in  # noqa: PLC0415 — test-local import

    sends = MagicMock(side_effect=_http_error(422, _engine_body()))
    slept = MagicMock()
    setattr_in(monkeypatch, retry_mod, "time.sleep", slept)
    with patch("nexus.db.http_vector_client._request", sends):
        with pytest.raises(ChunkMetadataKeyRefusedError):
            _vector_with_retry(_post, "/v1/vectors/upsert-chunks", {}, retry_on_timeout=False)
    assert sends.call_count == 1
    slept.assert_not_called()


def test_the_vector_retry_classifiers_call_it_final(_vector_post_env) -> None:
    with patch("nexus.db.http_vector_client._request", side_effect=_http_error(422, _engine_body())):
        with pytest.raises(ChunkMetadataKeyRefusedError) as caught:
            _post("/v1/vectors/upsert-chunks", {})
    assert _is_retryable_vector_error(caught.value) is False
    assert _is_connectivity_error(caught.value) is False


def test_a_hand_built_error_with_no_chain_is_final_too() -> None:
    """The classifier's string fallback scans the message for status tokens and wording; the typed
    error's own text must not trip it when no chain is attached."""
    err = chunk_key_refused_error_from_body(422, _engine_body())
    assert err is not None and err.__cause__ is None
    assert _is_retryable_vector_error(err) is False


# ── transport 2: the httpx store mixin (append, append_many, write_many) ─────


class _Catalog:
    """An ``HttpCatalogClient`` over an in-memory engine that answers the collection row as
    live and refuses the chunk-carrying combined write the way the real engine does."""

    def __init__(self, response: httpx.Response) -> None:
        from nexus.catalog.http_catalog_client import HttpCatalogClient  # noqa: PLC0415 — test-local import

        self.paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            self.paths.append(request.url.path)
            if request.url.path.endswith("/collections/get"):
                return httpx.Response(200, json={"name": _COLLECTION, "superseded_by": ""})
            if "/manifest/" in request.url.path:
                return response
            return httpx.Response(200, json={})

        self.client = HttpCatalogClient(base_url="http://engine.test", tenant="t", _token="x")
        self.client._client = httpx.Client(transport=httpx.MockTransport(handler))

    def count(self, suffix: str) -> int:
        return sum(1 for p in self.paths if p.endswith(suffix))


_ROWS = [{"position": 0, "chash": "a" * 64, "chunk_index": 0}]
_CHUNKS = [{"chash": "a" * 64, "text": "text", "metadata": {"title": "t"}}]


def _write_many(c: _Catalog) -> dict:
    return c.client.write_manifest_many([("1.1.1", _ROWS)], chunks=_CHUNKS, collection=_COLLECTION)


def _append(c: _Catalog) -> dict:
    return c.client.append_manifest_chunks("1.1.1", _ROWS, collection=_COLLECTION, chunk_payload=_CHUNKS)


def _append_many(c: _Catalog) -> dict:
    return c.client.append_manifest_many([("1.1.1", _ROWS)], collection=_COLLECTION, chunks=_CHUNKS)


@pytest.fixture
def _no_write_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("nexus.db.service_endpoint.guard_production_write", lambda _url: None)
    monkeypatch.setattr("nexus.db.t2._refreshable_client.guard_production_write", lambda _url: None)


@pytest.mark.parametrize(
    "call, suffix",
    [
        pytest.param(_write_many, "/manifest/write_many", id="write_many"),
        pytest.param(_append, "/manifest/append", id="append"),
        pytest.param(_append_many, "/manifest/append_many", id="append_many"),
    ],
)
def test_a_combined_write_raises_the_typed_error_after_one_send(_no_write_guard, call, suffix: str) -> None:
    cat = _Catalog(httpx.Response(422, json=_engine_body(route=f"/v1/catalog{suffix}")))
    with pytest.raises(ChunkMetadataKeyRefusedError) as caught:
        call(cat)
    assert [k.key for k in caught.value.refused] == ["title", "line_start", "made_up_key"]
    assert isinstance(caught.value.__cause__, httpx.HTTPStatusError)
    assert cat.count(suffix) == 1, "the refusal is final: no resend"
    assert _is_connectivity_error(caught.value) is False
    assert _is_retryable_vector_error(caught.value) is False


def test_a_combined_write_keeps_the_generic_error_for_another_422(_no_write_guard) -> None:
    cat = _Catalog(httpx.Response(422, json={"error": "nope", "reason": "ownerless_chunk_write"}))
    with pytest.raises(httpx.HTTPStatusError) as caught:
        _write_many(cat)
    assert caught.value.response.status_code == 422


def test_a_combined_write_is_judged_refused_by_the_engine_not_in_flight(_no_write_guard) -> None:
    """``write_outcome.judge`` walks the chain: a 4xx from the engine is a definitive refusal, so a
    note writer reports 'not stored' and never 'may have been stored'."""
    from nexus.catalog.write_outcome import REFUSED, REFUSED_BY_ENGINE, judge  # noqa: PLC0415 — test-local import

    cat = _Catalog(httpx.Response(422, json=_engine_body()))
    with pytest.raises(ChunkMetadataKeyRefusedError) as caught:
        _write_many(cat)
    assert judge(caught.value) == (REFUSED, REFUSED_BY_ENGINE)


def test_the_manifest_retry_wrapper_sends_it_exactly_once(_no_write_guard, monkeypatch) -> None:
    import nexus.retry as retry_mod  # noqa: PLC0415 — test-local import

    from tests._module_seam import setattr_in  # noqa: PLC0415 — test-local import

    slept = MagicMock()
    setattr_in(monkeypatch, retry_mod, "time.sleep", slept)
    cat = _Catalog(httpx.Response(422, json=_engine_body()))
    with pytest.raises(ChunkMetadataKeyRefusedError):
        retry_mod._manifest_write_with_retry(lambda: _write_many(cat))
    assert cat.count("/manifest/write_many") == 1
    slept.assert_not_called()


# ── the registration helper ──────────────────────────────────────────────────


def test_the_registration_helper_neither_retries_nor_re_registers_on_it() -> None:
    w = MagicMock()
    w.get_collection.return_value = {"superseded_by": ""}
    attempts: list[int] = []

    def write_fn() -> None:
        attempts.append(1)
        raise chunk_key_refused_error_from_body(422, _engine_body())  # type: ignore[misc]

    with pytest.raises(ChunkMetadataKeyRefusedError):
        corpus.write_with_registration_retry(_COLLECTION, write_fn, registrar=lambda: w)
    assert len(attempts) == 1, "the write is not sent again"
    assert w.register_collection.call_count == 1, "only the up-front registration of a cold name"


# ── the two surfaces a person reads ──────────────────────────────────────────


def test_the_cli_boundary_renders_one_clean_message_not_a_traceback() -> None:
    from click.testing import CliRunner  # noqa: PLC0415 — test-local import

    from nexus.cli import _StorageBackendGuardGroup  # noqa: PLC0415 — test-local import

    err = chunk_key_refused_error_from_body(422, _engine_body())
    assert err is not None

    @click.group(cls=_StorageBackendGuardGroup)
    def grp() -> None:
        pass

    @grp.command()
    def boom() -> None:
        raise err

    result = CliRunner().invoke(grp, ["boom"])
    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit), "a ClickException ends the run; a raw error would leave it as is"
    assert "Traceback" not in result.output
    assert result.output.startswith("Error: ")
    assert result.output.count("\n") <= 1 + len(_REFUSED) + 2
    for entry in _REFUSED:
        assert entry["key"] in result.output
        assert entry["home"] in result.output


def test_the_mcp_tool_error_is_one_message_naming_the_keys() -> None:
    from nexus.mcp.core import _mcp_tool_error  # noqa: PLC0415 — test-local import

    err = chunk_key_refused_error_from_body(422, _engine_body())
    assert err is not None
    text = _mcp_tool_error("store_put", err)
    assert text.startswith("Error: ")
    assert "Traceback" not in text
    assert "unreachable" not in text, "a refusal is not a dead service: no restart hint"
    for entry in _REFUSED:
        assert entry["key"] in text
        assert entry["home"] in text


# ── the message a person reads (fix round 1) ─────────────────────────────────


def test_the_message_lists_at_most_ten_keys_then_says_how_many_more() -> None:
    many = [{"key": f"k{i:02d}", "class": "UNKNOWN", "home": "nowhere"} for i in range(15)]
    text = str(chunk_key_refused_error_from_body(422, _engine_body(refused=many)))
    assert all(f"'k{i:02d}'" in text for i in range(10))
    assert "'k10'" not in text
    assert "and 5 more" in text


def test_the_message_tells_the_reader_what_to_do_and_claims_nothing_about_what_was_written() -> None:
    text = str(chunk_key_refused_error_from_body(422, _engine_body()))
    assert "retrying will not help" in text
    assert "Upgrade nx if it is older than the engine; otherwise report this message" in text
    assert "The engine reported this request refused" in text
    lowered = text.lower()
    for claim in ("nothing was written", "no chunk was left", "zero chunks landed", "rolled back"):
        assert claim not in lowered
