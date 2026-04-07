# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests and integration tests for ChromaDB transient-error retry.

Unit tests (T1/T2): test _is_retryable_chroma_error and _chroma_with_retry
helpers directly.

Integration tests (T3): verify that retry propagates through public API
methods — search(), _write_batch(), list_store(), and _index_code_file().
These tests are added in TDD RED phase; they fail until the call sites in
db/t3.py (T4) and indexer.py (T5) are wrapped with _chroma_with_retry.

RDR: docs/rdr/rdr-019-chromadb-transient-retry.md
"""
from __future__ import annotations

from unittest.mock import MagicMock, call, patch

import chromadb.errors
import httpx
import pytest

from nexus.retry import _chroma_with_retry, _is_retryable_chroma_error


# ── _is_retryable_chroma_error ────────────────────────────────────────────────


def test_retryable_504_string_fallback() -> None:
    """Exception whose message contains '504' gateway text returns True (string fallback path)."""
    exc = Exception("504 Gateway Time-out HTML")
    assert _is_retryable_chroma_error(exc) is True


def test_non_retryable_400_string() -> None:
    """Exception whose message is a 400 Bad Request returns False (non-retryable)."""
    exc = Exception("400 Bad Request: invalid payload")
    assert _is_retryable_chroma_error(exc) is False


def _make_chained_exc(status_code: int) -> Exception:
    """Build a plain Exception with an httpx.HTTPStatusError as __context__."""
    request = httpx.Request("GET", "https://api.trychroma.com/")
    response = httpx.Response(status_code=status_code, request=request)
    http_err = httpx.HTTPStatusError(
        f"Server error '{status_code}'", request=request, response=response
    )
    plain_exc = Exception(f"<html>Gateway error {status_code}</html>")
    plain_exc.__context__ = http_err
    return plain_exc


def test_retryable_429_via_chained_httpx_status() -> None:
    """Exception with chained httpx.HTTPStatusError(429) returns True (integer check path)."""
    exc = _make_chained_exc(429)
    assert _is_retryable_chroma_error(exc) is True


def test_non_retryable_404_via_chained_httpx_status() -> None:
    """Exception with chained httpx.HTTPStatusError(404) returns False (integer check path)."""
    exc = _make_chained_exc(404)
    assert _is_retryable_chroma_error(exc) is False


def test_retryable_connect_error_transport() -> None:
    """httpx.ConnectError returns True (transport isinstance path)."""
    exc = httpx.ConnectError("Connection refused")
    assert _is_retryable_chroma_error(exc) is True


def test_retryable_read_timeout_transport() -> None:
    """httpx.ReadTimeout returns True (transport isinstance path)."""
    exc = httpx.ReadTimeout("Read timed out")
    assert _is_retryable_chroma_error(exc) is True


def test_retryable_remote_protocol_error_transport() -> None:
    """httpx.RemoteProtocolError returns True (transport isinstance path)."""
    exc = httpx.RemoteProtocolError("Server disconnected without response")
    assert _is_retryable_chroma_error(exc) is True


# ── _chroma_with_retry ────────────────────────────────────────────────────────


def test_retry_connect_error_twice_then_success() -> None:
    """fn raises ConnectError on attempts 1 and 2, succeeds on attempt 3.

    Verifies: fn called 3 times, time.sleep called with 2.0 then 4.0.
    """
    call_count = 0

    def flaky_fn() -> str:
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise httpx.ConnectError("transient connect failure")
        return "ok"

    with patch("nexus.retry.time") as mock_time:
        result = _chroma_with_retry(flaky_fn)

    assert result == "ok"
    assert call_count == 3
    assert mock_time.sleep.call_args_list == [call(2.0), call(4.0)]


def test_all_attempts_exhausted_on_persistent_504() -> None:
    """fn raises 504 on every attempt; _chroma_with_retry raises after 5 attempts."""
    fn = MagicMock(side_effect=Exception("504 Gateway Time-out"))

    with patch("nexus.retry.time"):
        with pytest.raises(Exception, match="504"):
            _chroma_with_retry(fn, max_attempts=5)

    assert fn.call_count == 5


def test_non_retryable_400_raises_immediately() -> None:
    """fn raises 400 on first attempt; _chroma_with_retry re-raises immediately without sleeping."""
    fn = MagicMock(side_effect=Exception("400 Bad Request: invalid collection name"))

    with patch("nexus.retry.time") as mock_time:
        with pytest.raises(Exception, match="400"):
            _chroma_with_retry(fn)

    fn.assert_called_once()
    mock_time.sleep.assert_not_called()


def test_backoff_curve_2_4_8_16() -> None:
    """Exponential backoff: sleep args follow 2.0, 4.0, 8.0, 16.0 over 4 retries before 5th attempt."""
    call_count = 0

    def fn_succeeds_on_5th() -> str:
        nonlocal call_count
        call_count += 1
        if call_count < 5:
            raise Exception("503 Service Unavailable")
        return "done"

    with patch("nexus.retry.time") as mock_time:
        result = _chroma_with_retry(fn_succeeds_on_5th, max_attempts=5)

    assert result == "done"
    assert mock_time.sleep.call_args_list == [
        call(2.0),
        call(4.0),
        call(8.0),
        call(16.0),
    ]


# ── Integration: retry propagation through public API methods ─────────────────
#
# These tests are TDD RED: they fail until call sites are wrapped (T4/T5).
# Tests 1, 3, 4 pass after T4 (db/t3.py wrapping).
# Test 2 passes after T5 (indexer.py wrapping).


@pytest.fixture
def t3_mock():
    """T3Database wired to a single MagicMock ChromaDB client."""
    with patch("nexus.db.t3.chromadb") as chromadb_m:
        mock_client = MagicMock()

        def _cloud_client_factory(**kwargs):
            if kwargs.get("database", "").endswith("_code"):
                raise chromadb.errors.NotFoundError("probe")
            return mock_client

        chromadb_m.CloudClient.side_effect = _cloud_client_factory
        from nexus.db.t3 import T3Database
        db = T3Database(tenant="t", database="d", api_key="k")
        yield db, mock_client


def test_search_retries_on_503(t3_mock) -> None:
    """search() retries when col.query raises a chained 503 on the first attempt.

    Fails at TDD RED (col.query not wrapped); passes after T4.
    """
    db, mock_client = t3_mock
    mock_col = MagicMock()
    mock_client.get_collection.return_value = mock_col
    mock_col.count.return_value = 2

    exc = _make_chained_exc(503)
    valid_result = {
        "ids": [["id-1"]],
        "documents": [["content"]],
        "metadatas": [[{"source_path": "f.py"}]],
        "distances": [[0.1]],
    }
    mock_col.query.side_effect = [exc, valid_result]

    with patch("nexus.retry.time"):
        results = db.search("query text", ["code__myrepo"])

    assert len(results) == 1


def test_write_batch_retries_on_504(t3_mock) -> None:
    """_write_batch retries when col.upsert raises 504 on the first attempt.

    Fails at TDD RED (col.upsert not wrapped); passes after T4.
    """
    db, _ = t3_mock
    mock_col = MagicMock()
    mock_col.upsert.side_effect = [Exception("504 Gateway Time-out"), None]

    with patch("nexus.retry.time"):
        db._write_batch(
            mock_col,
            "code__myrepo",
            ["id-1"],
            ["def hello(): pass"],
            [{"source_path": "hello.py"}],
        )

    assert mock_col.upsert.call_count == 2


def test_list_store_retries_on_read_timeout(t3_mock) -> None:
    """list_store retries when col.get raises ReadTimeout on the first attempt.

    Fails at TDD RED (col.get not wrapped); passes after T4.
    """
    db, mock_client = t3_mock
    mock_col = MagicMock()
    mock_client.get_collection.return_value = mock_col
    mock_col.get.side_effect = [
        httpx.ReadTimeout("timed out"),
        {
            "ids": ["id-1"],
            "metadatas": [{
                "title": "finding.md", "tags": "", "ttl_days": 0,
                "expires_at": "", "indexed_at": "2026-01-01T00:00:00+00:00",
            }],
        },
    ]

    with patch("nexus.retry.time"):
        results = db.list_store("knowledge__mystore")

    assert len(results) == 1


def test_index_code_file_retries_on_connect_error(tmp_path) -> None:
    """_index_code_file retries when col.get raises ConnectError on the first attempt.

    Fails at TDD RED (col.get in indexer.py not wrapped); passes after T5.
    """
    from nexus.indexer import _index_code_file

    src = tmp_path / "hello.py"
    src.write_text("def hello(): pass\n")

    mock_col = MagicMock()
    mock_col.get.side_effect = [
        httpx.ConnectError("connection refused"),
        {"ids": [], "metadatas": []},
    ]
    mock_db = MagicMock()
    mock_voyage = MagicMock()
    mock_voyage.embed.return_value = MagicMock(embeddings=[[0.1, 0.2]])

    with patch("nexus.retry.time"):
        result = _index_code_file(
            file=src,
            repo=tmp_path,
            collection_name="code__myrepo",
            target_model="voyage-code-3",
            col=mock_col,
            db=mock_db,
            voyage_client=mock_voyage,
            git_meta={},
            now_iso="2026-01-01T00:00:00+00:00",
            score=1.0,
        )

    assert result >= 0  # completed without raising
    assert mock_col.get.call_count == 2


# ── RDR-020 regression: _is_retryable_chroma_error disjoint from Voyage AI ───

def test_chroma_error_unchanged_transport() -> None:
    """_is_retryable_chroma_error still True for httpx.TransportError subclasses."""
    assert _is_retryable_chroma_error(httpx.ConnectError("refused")) is True
    assert _is_retryable_chroma_error(httpx.ReadTimeout("timeout")) is True


def test_chroma_error_unchanged_503() -> None:
    """_is_retryable_chroma_error still True for chained 503."""
    assert _is_retryable_chroma_error(_make_chained_exc(503)) is True


def test_chroma_error_unchanged_429() -> None:
    """_is_retryable_chroma_error still True for chained 429."""
    assert _is_retryable_chroma_error(_make_chained_exc(429)) is True


def test_chroma_error_false_for_voyage_api_connection_error() -> None:
    """_is_retryable_chroma_error returns False for voyageai.error.APIConnectionError.

    The two error oracles must be disjoint: a Voyage AI error must not be
    treated as a ChromaDB retryable error.
    """
    import voyageai.error as _ve
    assert _is_retryable_chroma_error(_ve.APIConnectionError("down")) is False
