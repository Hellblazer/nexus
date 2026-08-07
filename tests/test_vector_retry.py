# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

from unittest.mock import MagicMock, call, patch

import httpx
import pytest

from nexus.retry import _vector_with_retry, _is_retryable_vector_error


# ── _is_retryable_vector_error ──────────────────────────────────────────────

def _make_chained_exc(status_code: int) -> Exception:
    request = httpx.Request("GET", "https://api.trychroma.com/")
    response = httpx.Response(status_code=status_code, request=request)
    http_err = httpx.HTTPStatusError(
        f"Server error '{status_code}'", request=request, response=response
    )
    plain_exc = Exception(f"<html>Gateway error {status_code}</html>")
    plain_exc.__context__ = http_err
    return plain_exc


@pytest.mark.parametrize("exc,expected", [
    (Exception("504 Gateway Time-out HTML"), True),
    (Exception("400 Bad Request: invalid payload"), False),
    (httpx.ConnectError("Connection refused"), True),
    (httpx.ReadTimeout("Read timed out"), True),
    (httpx.RemoteProtocolError("Server disconnected without response"), True),
], ids=["504-string", "400-string", "connect-error", "read-timeout", "remote-protocol"])
def test_retryable_basic(exc: Exception, expected: bool) -> None:
    assert _is_retryable_vector_error(exc) is expected


@pytest.mark.parametrize("status,expected", [
    (429, True), (404, False), (503, True),
], ids=["429-retryable", "404-not", "503-retryable"])
def test_retryable_chained_httpx(status: int, expected: bool) -> None:
    assert _is_retryable_vector_error(_make_chained_exc(status)) is expected


# ── _vector_with_retry ──────────────────────────────────────────────────────

def test_retry_connect_error_twice_then_success() -> None:
    call_count = 0
    def flaky_fn() -> str:
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise httpx.ConnectError("transient connect failure")
        return "ok"
    # nexus-8g79.32: pin random.random()=0.5 so jittered delay equals
    # the deterministic base (jitter factor = 1 + (0.5 - 0.5) * 0.4 = 1.0).
    with patch("nexus.retry.time") as mock_time, patch(
        "nexus.retry.random.random", return_value=0.5,
    ):
        result = _vector_with_retry(flaky_fn)
    assert result == "ok" and call_count == 3
    assert mock_time.sleep.call_args_list == [call(2.0), call(4.0)]


def test_all_attempts_exhausted_on_persistent_504() -> None:
    fn = MagicMock(side_effect=Exception("504 Gateway Time-out"))
    with patch("nexus.retry.time"), pytest.raises(Exception, match="504"):
        _vector_with_retry(fn, max_attempts=5)
    assert fn.call_count == 5


def test_non_retryable_400_raises_immediately() -> None:
    fn = MagicMock(side_effect=Exception("400 Bad Request: invalid collection name"))
    with patch("nexus.retry.time") as mock_time, pytest.raises(Exception, match="400"):
        _vector_with_retry(fn)
    fn.assert_called_once()
    mock_time.sleep.assert_not_called()


def test_backoff_curve_2_4_8_16() -> None:
    call_count = 0
    def fn_succeeds_on_5th() -> str:
        nonlocal call_count
        call_count += 1
        if call_count < 5:
            raise Exception("503 Service Unavailable")
        return "done"
    # nexus-8g79.32: pin random.random()=0.5 so jitter = 1.0.
    with patch("nexus.retry.time") as mock_time, patch(
        "nexus.retry.random.random", return_value=0.5,
    ):
        assert _vector_with_retry(fn_succeeds_on_5th, max_attempts=5) == "done"
    assert mock_time.sleep.call_args_list == [call(2.0), call(4.0), call(8.0), call(16.0)]


# ── Integration: retry through public API ───────────────────────────────────

@pytest.fixture
def t3_mock():
    # RDR-155 P4a.2 (nexus-1k8s1): the CloudClient construction is retired —
    # inject the mock client directly (the retry machinery under test is
    # downstream of construction and unchanged).
    # nexus-sghyo (2026-08-06): client-side Voyage embedding is retired
    # outright (nexus.db.voyage_ef deleted) — a cloud-mode collection
    # with no _ef_override now raises at EF-construction time
    # (IncompatibleCollectionError). Pass _ef_override explicitly so
    # collection-name dispatch never reaches that raise; the retry
    # machinery under test is upstream of embedding anyway.
    mock_client = MagicMock()
    from nexus.db.t3 import T3Database
    yield T3Database(
        tenant="t", database="d", api_key="k", _client=mock_client,
        _ef_override=MagicMock(),
    ), mock_client


def test_search_retries_on_503(t3_mock) -> None:
    db, mock_client = t3_mock
    mock_col = MagicMock()
    mock_client.get_collection.return_value = mock_col
    mock_col.count.return_value = 2
    mock_col.query.side_effect = [
        _make_chained_exc(503),
        {"ids": [["id-1"]], "documents": [["content"]],
         "metadatas": [[{"source_path": "f.py"}]], "distances": [[0.1]]},
    ]
    with patch("nexus.retry.time"):
        assert len(db.search("query text", ["code__myrepo"])) == 1


def test_write_batch_retries_on_504(t3_mock) -> None:
    db, _ = t3_mock
    mock_col = MagicMock()
    mock_col.upsert.side_effect = [Exception("504 Gateway Time-out"), None]
    with patch("nexus.retry.time"):
        db._write_batch(mock_col, "code__myrepo", ["id-1"], ["def hello(): pass"],
                        [{"source_path": "hello.py"}])
    assert mock_col.upsert.call_count == 2


def test_list_store_retries_on_read_timeout(t3_mock) -> None:
    db, mock_client = t3_mock
    mock_col = MagicMock()
    mock_client.get_collection.return_value = mock_col
    mock_col.get.side_effect = [
        httpx.ReadTimeout("timed out"),
        {"ids": ["id-1"], "metadatas": [{"title": "finding.md", "tags": "",
         "ttl_days": 0, "expires_at": "", "indexed_at": "2026-01-01T00:00:00+00:00"}]},
    ]
    with patch("nexus.retry.time"):
        assert len(db.list_store("knowledge__mystore")) == 1


def test_index_code_file_retries_on_connect_error(tmp_path) -> None:
    from nexus.indexer import _index_code_file
    src = tmp_path / "hello.py"
    src.write_text("def hello(): pass\n")
    mock_col = MagicMock()
    mock_col.get.side_effect = [httpx.ConnectError("connection refused"),
                                {"ids": [], "metadatas": []}]
    mock_voyage = MagicMock()
    mock_voyage.embed.return_value = MagicMock(embeddings=[[0.1, 0.2]])
    with patch("nexus.retry.time"):
        result = _index_code_file(file=src, repo=tmp_path, collection_name="code__myrepo",
                                  target_model="voyage-code-3", col=mock_col, db=MagicMock(),
                                  voyage_client=mock_voyage, git_meta={},
                                  now_iso="2026-01-01T00:00:00+00:00", score=1.0)
    assert result >= 0 and mock_col.get.call_count == 2


# ── RDR-020 regression: disjoint from Voyage AI ────────────────────────────

@pytest.mark.parametrize("exc,expected", [
    (httpx.ConnectError("refused"), True),
    (httpx.ReadTimeout("timeout"), True),
    (_make_chained_exc(503), True),
    (_make_chained_exc(429), True),
])
def test_chroma_error_unchanged(exc, expected) -> None:
    assert _is_retryable_vector_error(exc) is expected


def test_chroma_error_false_for_voyage_error() -> None:
    import voyageai.error as _ve
    assert _is_retryable_vector_error(_ve.APIConnectionError("down")) is False


# ── Transport-stall retryability (was nexus-jgjw's end-to-end leg) ──────────
#
# RDR-155 P4b P3: the surrounding TestChromadbTimeoutPatch class tested
# T3Database's override of chromadb's hardcoded ``httpx.Client(timeout=None)``
# (chromadb/api/fastapi.py:86,91). That override — and the client shape it
# reached into (``_server._session``) — were deleted with the chroma client
# legs; T3Database can no longer be handed such a client. The two tests that
# asserted the override itself died with it.
#
# This leg survives because it never depended on chroma: a stalled transport
# surfacing as ``httpx.ReadTimeout`` must still be classified retryable and
# must still be retried by ``_vector_with_retry``. That is a live contract for
# the HTTP vector client today.


def test_transport_readtimeout_is_retryable_and_retried() -> None:
    readtimeout = httpx.ReadTimeout("simulated transport stall")
    assert _is_retryable_vector_error(readtimeout) is True
    from unittest.mock import MagicMock
    fn = MagicMock(side_effect=[readtimeout, readtimeout, "ok"])
    with patch("nexus.retry.time"):
        assert _vector_with_retry(fn, max_attempts=3) == "ok"
    assert fn.call_count == 3
