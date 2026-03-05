"""Tests for Voyage AI retry helpers and scoring.py singleton reset.

Covers:
  - _is_retryable_voyage_error oracle (nexus-k8dv)
  - _voyage_with_retry wrapper (nexus-k8dv)
  - _reset_voyage_client singleton reset (nexus-b1m0)
  - voyageai.Client timeout + max_retries wired at all 4 sites (nexus-oh22)
"""

from unittest.mock import MagicMock, patch

import pytest
import voyageai.error as _ve

from nexus.db.t3 import _is_retryable_voyage_error, _voyage_with_retry


# ── _is_retryable_voyage_error oracle ─────────────────────────────────────────

def test_retryable_voyage_api_connection_error() -> None:
    assert _is_retryable_voyage_error(_ve.APIConnectionError("connection reset")) is True


def test_retryable_voyage_try_again() -> None:
    assert _is_retryable_voyage_error(_ve.TryAgain("try again")) is True


def test_not_retryable_voyage_timeout() -> None:
    """Timeout handled by voyageai.Client built-in max_retries — not by outer wrapper."""
    assert _is_retryable_voyage_error(_ve.Timeout("timed out")) is False


def test_not_retryable_voyage_rate_limit() -> None:
    """RateLimitError handled by built-in max_retries."""
    assert _is_retryable_voyage_error(_ve.RateLimitError("rate limited")) is False


def test_not_retryable_voyage_service_unavailable() -> None:
    """ServiceUnavailableError handled by built-in max_retries."""
    assert _is_retryable_voyage_error(_ve.ServiceUnavailableError("unavailable")) is False


def test_not_retryable_voyage_auth_error() -> None:
    assert _is_retryable_voyage_error(_ve.AuthenticationError("bad key")) is False


def test_not_retryable_voyage_invalid_request() -> None:
    assert _is_retryable_voyage_error(_ve.InvalidRequestError("bad input")) is False


def test_not_retryable_plain_exception() -> None:
    assert _is_retryable_voyage_error(ValueError("random")) is False


# ── _voyage_with_retry wrapper ─────────────────────────────────────────────────

def test_voyage_with_retry_success_no_retry() -> None:
    fn = MagicMock(return_value="ok")
    result = _voyage_with_retry(fn, "arg")
    assert result == "ok"
    fn.assert_called_once_with("arg")


def test_voyage_with_retry_success_after_transient() -> None:
    """Succeeds on second attempt after one APIConnectionError."""
    fn = MagicMock(side_effect=[_ve.APIConnectionError("down"), "ok"])
    result = _voyage_with_retry(fn)
    assert result == "ok"
    assert fn.call_count == 2


def test_voyage_with_retry_fires_3_times_then_raises() -> None:
    """Retries max_attempts=3 times on APIConnectionError then re-raises."""
    fn = MagicMock(side_effect=_ve.APIConnectionError("persistent"))
    with patch("nexus.db.t3.time.sleep"), pytest.raises(_ve.APIConnectionError):
        _voyage_with_retry(fn, max_attempts=3)
    assert fn.call_count == 3


def test_voyage_with_retry_non_retryable_raises_immediately() -> None:
    """AuthenticationError (non-retryable) raises on first attempt."""
    fn = MagicMock(side_effect=_ve.AuthenticationError("bad key"))
    with pytest.raises(_ve.AuthenticationError):
        _voyage_with_retry(fn)
    fn.assert_called_once()


def test_voyage_with_retry_try_again_retries() -> None:
    fn = MagicMock(side_effect=[_ve.TryAgain("wait"), "result"])
    result = _voyage_with_retry(fn)
    assert result == "result"
    assert fn.call_count == 2


# ── nexus-oh22: voyageai.Client timeout + max_retries at all 4 sites ──────────

def test_t3_database_voyage_client_has_timeout() -> None:
    """T3Database.__init__ passes timeout=read_timeout_seconds and max_retries=3."""
    from nexus.db.t3 import T3Database

    with patch("nexus.db.t3.voyageai.Client") as mock_ctor:
        mock_ctor.return_value = MagicMock()
        T3Database(voyage_api_key="test-key", read_timeout_seconds=60.0, _client=MagicMock())
        mock_ctor.assert_called_once_with(
            api_key="test-key",
            timeout=60.0,
            max_retries=3,
        )


def test_embed_with_fallback_voyage_client_has_timeout() -> None:
    """_embed_with_fallback passes timeout and max_retries=3 to voyageai.Client."""
    from nexus.doc_indexer import _embed_with_fallback

    mock_client = MagicMock()
    # CCE result structure: result.results[0].embeddings (not result.embeddings)
    cce_result = MagicMock()
    cce_result.results = [MagicMock()]
    cce_result.results[0].embeddings = [[0.1] * 1024, [0.2] * 1024]
    mock_client.contextualized_embed.return_value = cce_result
    with patch("voyageai.Client", return_value=mock_client) as mock_ctor, \
         patch("nexus.db.t3.time.sleep"):
        _embed_with_fallback(["chunk one", "chunk two"], "voyage-context-3", "test-key", timeout=75.0)
        mock_ctor.assert_called_once_with(api_key="test-key", timeout=75.0, max_retries=3)


def test_scoring_voyage_client_has_timeout() -> None:
    """_voyage_client() reads timeout from config and passes it to voyageai.Client."""
    import nexus.scoring as scoring
    scoring._reset_voyage_client()

    with patch("voyageai.Client") as mock_ctor, \
         patch("nexus.config.get_credential", return_value="test-key"), \
         patch("nexus.config.load_config", return_value={"voyageai": {"read_timeout_seconds": 55}}):
        mock_ctor.return_value = MagicMock()
        scoring._voyage_client()
        mock_ctor.assert_called_once_with(api_key="test-key", timeout=55, max_retries=3)

    scoring._reset_voyage_client()


# ── nexus-vdly: _voyage_with_retry wraps all Voyage AI call sites ─────────────

def test_cce_embed_retries_api_connection_error() -> None:
    """_cce_embed retries once on APIConnectionError then returns the result."""
    from nexus.db.t3 import T3Database

    success_result = MagicMock()
    success_result.results = [MagicMock()]
    success_result.results[0].embeddings = [[0.1] * 1024]
    mock_voyage = MagicMock()
    mock_voyage.contextualized_embed.side_effect = [_ve.APIConnectionError("down"), success_result]

    with patch("nexus.db.t3.voyageai.Client", return_value=mock_voyage), \
         patch("nexus.db.t3.time.sleep"):
        db = T3Database(voyage_api_key="test-key", _client=MagicMock())
        result = db._cce_embed("hello world")

    assert mock_voyage.contextualized_embed.call_count == 2
    assert result == [0.1] * 1024


def test_embed_with_fallback_cce_path_retries_api_connection_error() -> None:
    """CCE path in _embed_with_fallback retries before returning the result."""
    from nexus.doc_indexer import _embed_with_fallback

    success_result = MagicMock()
    success_result.results = [MagicMock()]
    success_result.results[0].embeddings = [[0.1] * 1024, [0.2] * 1024]
    mock_client = MagicMock()
    mock_client.contextualized_embed.side_effect = [_ve.APIConnectionError("down"), success_result]

    with patch("voyageai.Client", return_value=mock_client), \
         patch("nexus.db.t3.time.sleep"):
        embeddings, model = _embed_with_fallback(["chunk one", "chunk two"], "voyage-context-3", "test-key")

    assert mock_client.contextualized_embed.call_count == 2
    assert model == "voyage-context-3"


def test_embed_with_fallback_standard_path_retries_api_connection_error() -> None:
    """Standard embed path in _embed_with_fallback retries on APIConnectionError."""
    from nexus.doc_indexer import _embed_with_fallback

    success_result = MagicMock()
    success_result.embeddings = [[0.1] * 1024]
    mock_client = MagicMock()
    mock_client.embed.side_effect = [_ve.APIConnectionError("down"), success_result]

    with patch("voyageai.Client", return_value=mock_client), \
         patch("nexus.db.t3.time.sleep"):
        embeddings, model = _embed_with_fallback(["one chunk"], "voyage-4", "test-key")

    assert mock_client.embed.call_count == 2
    assert model == "voyage-4"


def test_index_code_file_embed_retries_api_connection_error(tmp_path) -> None:
    """_index_code_file batch embed retries on APIConnectionError."""
    from nexus.indexer import _index_code_file

    py_file = tmp_path / "hello.py"
    py_file.write_text("def hello():\n    return 'world'\n")

    mock_col = MagicMock()
    mock_col.get.return_value = {"ids": [], "metadatas": []}
    mock_db = MagicMock()

    success_result = MagicMock()
    success_result.embeddings = [[0.1] * 1024]
    mock_voyage = MagicMock()
    mock_voyage.embed.side_effect = [
        _ve.APIConnectionError("down"),
        success_result, success_result, success_result,
    ]

    with patch("nexus.db.t3.time.sleep"):
        _index_code_file(
            py_file, tmp_path, "code__test", "voyage-code-3",
            mock_col, mock_db, mock_voyage, {}, "2026-03-05T00:00:00",
            score=0.5,
        )

    assert mock_voyage.embed.call_count >= 2


def test_rerank_results_retries_then_degrades() -> None:
    """rerank_results retries on APIConnectionError then degrades to unranked results."""
    import nexus.scoring as scoring
    scoring._reset_voyage_client()

    from nexus.types import SearchResult
    results = [
        SearchResult(id="1", content="text", collection="code__repo", distance=0.1, metadata={})
    ]
    mock_client = MagicMock()
    mock_client.rerank.side_effect = _ve.APIConnectionError("persistent")

    with patch("voyageai.Client", return_value=mock_client), \
         patch("nexus.config.get_credential", return_value="test-key"), \
         patch("nexus.config.load_config", return_value={"voyageai": {"read_timeout_seconds": 120}}), \
         patch("nexus.db.t3.time.sleep"):
        returned = scoring.rerank_results(results, "query", top_k=1)

    assert mock_client.rerank.call_count == 3  # 3 attempts then re-raises → caught by outer except
    assert returned == results  # graceful degradation

    scoring._reset_voyage_client()


# ── _reset_voyage_client singleton reset ──────────────────────────────────────

def test_reset_voyage_client_clears_singleton() -> None:
    """_reset_voyage_client() sets _voyage_instance = None; next call re-creates."""
    import nexus.scoring as scoring
    # Always get _reset_voyage_client from the CURRENT module object (not cached via 'from')
    # test_no_circular_imports reloads nexus.scoring, detaching cached imports.
    _reset_voyage_client = scoring._reset_voyage_client

    # Ensure clean slate (idempotent)
    _reset_voyage_client()
    assert scoring._voyage_instance is None

    # Inject a fake singleton
    sentinel = MagicMock()
    scoring._voyage_instance = sentinel
    assert scoring._voyage_instance is sentinel

    # Reset clears it
    _reset_voyage_client()
    assert scoring._voyage_instance is None



# ── nexus-n4v9: integration tests ─────────────────────────────────────────────

def test_embed_with_fallback_cce_retry_then_degrade_e2e() -> None:
    """Full flow: CCE raises APIConnectionError 3 times (exhausting _voyage_with_retry),
    except block catches and falls back to voyage-4 embed.
    Verify: CCE called 3 times, fallback embed called once, returned model is 'voyage-4'.
    """
    from nexus.doc_indexer import _embed_with_fallback

    cce_failure = _ve.APIConnectionError("persistent down")
    voyage4_result = MagicMock()
    voyage4_result.embeddings = [[0.1] * 1024]

    mock_client = MagicMock()
    mock_client.contextualized_embed.side_effect = cce_failure
    mock_client.embed.return_value = voyage4_result

    with patch("voyageai.Client", return_value=mock_client), \
         patch("nexus.db.t3.time.sleep"):
        # CCE requires >= 2 chunks; use two chunks to trigger the CCE path
        embeddings, model = _embed_with_fallback(
            ["chunk one", "chunk two"], "voyage-context-3", "test-key"
        )

    assert mock_client.contextualized_embed.call_count == 3  # 3 retry attempts
    assert mock_client.embed.call_count == 1                  # fallback fired once
    assert model == "voyage-4"


def test_embed_with_fallback_standard_path_propagates_after_retry_exhaustion() -> None:
    """Standard embed() raises APIConnectionError 3 times; exception propagates to caller."""
    from nexus.doc_indexer import _embed_with_fallback

    mock_client = MagicMock()
    mock_client.embed.side_effect = _ve.APIConnectionError("persistent")

    with patch("voyageai.Client", return_value=mock_client), \
         patch("nexus.db.t3.time.sleep"), \
         pytest.raises(_ve.APIConnectionError):
        _embed_with_fallback(["one chunk"], "voyage-4", "test-key")

    assert mock_client.embed.call_count == 3


def test_client_constructed_with_config_timeout() -> None:
    """When config has voyageai.read_timeout_seconds=60, Client is built with timeout=60."""
    from nexus.doc_indexer import _embed_with_fallback

    mock_client = MagicMock()
    mock_client.embed.return_value = MagicMock(embeddings=[[0.1] * 1024])

    with patch("voyageai.Client", return_value=mock_client) as mock_ctor, \
         patch("nexus.db.t3.time.sleep"):
        _embed_with_fallback(["chunk"], "voyage-4", "test-key", timeout=60.0)
        mock_ctor.assert_called_once_with(api_key="test-key", timeout=60.0, max_retries=3)
