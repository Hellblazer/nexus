# SPDX-License-Identifier: AGPL-3.0-or-later
from unittest.mock import MagicMock, patch

import pytest
import voyageai.error as _ve

from nexus.retry import _is_retryable_voyage_error, _voyage_with_retry


# ── _is_retryable_voyage_error oracle ───────────────────────────────────────

@pytest.mark.parametrize("exc,expected", [
    (_ve.APIConnectionError("connection reset"), True),
    (_ve.TryAgain("try again"), True),
    (_ve.Timeout("timed out"), False),
    (_ve.RateLimitError("rate limited"), False),
    (_ve.ServiceUnavailableError("unavailable"), False),
    (_ve.AuthenticationError("bad key"), False),
    (_ve.InvalidRequestError("bad input"), False),
    (ValueError("random"), False),
])
def test_retryable_oracle(exc: Exception, expected: bool) -> None:
    assert _is_retryable_voyage_error(exc) is expected


# ── _voyage_with_retry wrapper ──────────────────────────────────────────────

def test_success_no_retry() -> None:
    fn = MagicMock(return_value="ok")
    assert _voyage_with_retry(fn, "arg") == "ok"
    fn.assert_called_once_with("arg")


def test_success_after_transient() -> None:
    fn = MagicMock(side_effect=[_ve.APIConnectionError("down"), "ok"])
    assert _voyage_with_retry(fn) == "ok"
    assert fn.call_count == 2


def test_exhausted_then_raises() -> None:
    fn = MagicMock(side_effect=_ve.APIConnectionError("persistent"))
    with patch("nexus.retry.time.sleep"), pytest.raises(_ve.APIConnectionError):
        _voyage_with_retry(fn, max_attempts=3)
    assert fn.call_count == 3


def test_non_retryable_raises_immediately() -> None:
    fn = MagicMock(side_effect=_ve.AuthenticationError("bad key"))
    with pytest.raises(_ve.AuthenticationError):
        _voyage_with_retry(fn)
    fn.assert_called_once()


def test_try_again_retries() -> None:
    fn = MagicMock(side_effect=[_ve.TryAgain("wait"), "result"])
    assert _voyage_with_retry(fn) == "result"


# ── voyageai.Client timeout + max_retries at all 4 sites ───────────────────

def test_t3_database_voyage_client_has_timeout() -> None:
    from nexus.db.t3 import T3Database
    with patch("nexus.db.t3.voyageai.Client") as mock_ctor:
        mock_ctor.return_value = MagicMock()
        T3Database(voyage_api_key="test-key", read_timeout_seconds=60.0, _client=MagicMock())
        mock_ctor.assert_called_once_with(api_key="test-key", timeout=60.0, max_retries=3)


def test_embed_with_fallback_voyage_client_has_timeout() -> None:
    from nexus.doc_indexer import _embed_with_fallback
    mock_client = MagicMock()
    cce_result = MagicMock()
    cce_result.results = [MagicMock()]
    cce_result.results[0].embeddings = [[0.1] * 1024, [0.2] * 1024]
    mock_client.contextualized_embed.return_value = cce_result
    with patch("voyageai.Client", return_value=mock_client) as mock_ctor, \
         patch("nexus.retry.time.sleep"):
        _embed_with_fallback(["chunk one", "chunk two"], "voyage-context-3", "test-key", timeout=75.0)
        mock_ctor.assert_called_once_with(api_key="test-key", timeout=75.0, max_retries=3)


def test_scoring_voyage_client_has_timeout() -> None:
    import nexus.scoring as scoring
    scoring._reset_voyage_client()
    with patch("voyageai.Client") as mock_ctor, \
         patch("nexus.config.get_credential", return_value="test-key"), \
         patch("nexus.config.load_config", return_value={"voyageai": {"read_timeout_seconds": 55}}):
        mock_ctor.return_value = MagicMock()
        scoring._voyage_client()
        mock_ctor.assert_called_once_with(api_key="test-key", timeout=55, max_retries=3)
    scoring._reset_voyage_client()


# ── _voyage_with_retry wraps all call sites ─────────────────────────────────

def _make_cce_success() -> MagicMock:
    r = MagicMock()
    r.results = [MagicMock()]
    r.results[0].embeddings = [[0.1] * 1024]
    return r


def _make_cce_success_2chunk() -> MagicMock:
    r = MagicMock()
    r.results = [MagicMock()]
    r.results[0].embeddings = [[0.1] * 1024, [0.2] * 1024]
    return r


@pytest.mark.parametrize("test_id", ["cce_embed", "embed_fallback_cce", "embed_fallback_std"])
def test_retry_at_call_site(test_id: str) -> None:
    if test_id == "cce_embed":
        from nexus.db.t3 import T3Database
        mock_voyage = MagicMock()
        mock_voyage.contextualized_embed.side_effect = [
            _ve.APIConnectionError("down"), _make_cce_success(),
        ]
        with patch("nexus.db.t3.voyageai.Client", return_value=mock_voyage), \
             patch("nexus.retry.time.sleep"):
            db = T3Database(voyage_api_key="test-key", _client=MagicMock())
            result = db._cce_embed("hello world")
        assert mock_voyage.contextualized_embed.call_count == 2
        assert result == [0.1] * 1024

    elif test_id == "embed_fallback_cce":
        from nexus.doc_indexer import _embed_with_fallback
        mock_client = MagicMock()
        mock_client.contextualized_embed.side_effect = [
            _ve.APIConnectionError("down"), _make_cce_success_2chunk(),
        ]
        with patch("voyageai.Client", return_value=mock_client), \
             patch("nexus.retry.time.sleep"):
            _, model = _embed_with_fallback(["chunk one", "chunk two"], "voyage-context-3", "test-key")
        assert mock_client.contextualized_embed.call_count == 2
        assert model == "voyage-context-3"

    else:  # embed_fallback_std
        from nexus.doc_indexer import _embed_with_fallback
        mock_client = MagicMock()
        success = MagicMock()
        success.embeddings = [[0.1] * 1024]
        mock_client.embed.side_effect = [_ve.APIConnectionError("down"), success]
        with patch("voyageai.Client", return_value=mock_client), \
             patch("nexus.retry.time.sleep"):
            _, model = _embed_with_fallback(["one chunk"], "voyage-code-3", "test-key")
        assert mock_client.embed.call_count == 2
        assert model == "voyage-code-3"


def test_index_code_file_embed_retries(tmp_path) -> None:
    from nexus.indexer import _index_code_file
    py_file = tmp_path / "hello.py"
    py_file.write_text("def hello():\n    return 'world'\n")
    mock_col = MagicMock()
    mock_col.get.return_value = {"ids": [], "metadatas": []}
    success = MagicMock()
    success.embeddings = [[0.1] * 1024]
    mock_voyage = MagicMock()
    mock_voyage.embed.side_effect = [_ve.APIConnectionError("down"), success, success, success]
    with patch("nexus.retry.time.sleep"):
        _index_code_file(py_file, tmp_path, "code__test", "voyage-code-3",
                         mock_col, MagicMock(), mock_voyage, {}, "2026-03-05T00:00:00", score=0.5)
    assert mock_voyage.embed.call_count >= 2


def test_rerank_retries_then_degrades() -> None:
    import nexus.scoring as scoring
    scoring._reset_voyage_client()
    from nexus.types import SearchResult
    results = [SearchResult(id="1", content="text", collection="code__repo", distance=0.1, metadata={})]
    mock_client = MagicMock()
    mock_client.rerank.side_effect = _ve.APIConnectionError("persistent")
    with patch("voyageai.Client", return_value=mock_client), \
         patch("nexus.config.get_credential", return_value="test-key"), \
         patch("nexus.config.load_config", return_value={"voyageai": {"read_timeout_seconds": 120}}), \
         patch("nexus.retry.time.sleep"):
        returned = scoring.rerank_results(results, "query", top_k=1)
    assert mock_client.rerank.call_count == 3
    assert returned == results
    scoring._reset_voyage_client()


# ── _reset_voyage_client singleton ──────────────────────────────────────────

def test_reset_clears_singleton() -> None:
    import nexus.scoring as scoring
    scoring._reset_voyage_client()
    assert scoring._voyage_instance is None
    scoring._voyage_instance = sentinel = MagicMock()
    assert scoring._voyage_instance is sentinel
    scoring._reset_voyage_client()
    assert scoring._voyage_instance is None


# ── Integration: propagation and exhaustion ─────────────────────────────────

def test_cce_retry_then_split_then_propagate() -> None:
    from nexus.doc_indexer import _embed_with_fallback
    mock_client = MagicMock()
    mock_client.contextualized_embed.side_effect = _ve.APIConnectionError("persistent down")
    with patch("voyageai.Client", return_value=mock_client), \
         patch("nexus.retry.time.sleep"), \
         pytest.raises(_ve.APIConnectionError):
        _embed_with_fallback(["chunk one", "chunk two"], "voyage-context-3", "test-key")
    assert mock_client.contextualized_embed.call_count >= 3
    mock_client.embed.assert_not_called()


def test_standard_path_propagates_after_exhaustion() -> None:
    from nexus.doc_indexer import _embed_with_fallback
    mock_client = MagicMock()
    mock_client.embed.side_effect = _ve.APIConnectionError("persistent")
    with patch("voyageai.Client", return_value=mock_client), \
         patch("nexus.retry.time.sleep"), \
         pytest.raises(_ve.APIConnectionError):
        _embed_with_fallback(["one chunk"], "voyage-code-3", "test-key")
    assert mock_client.embed.call_count == 3


def test_client_constructed_with_config_timeout() -> None:
    from nexus.doc_indexer import _embed_with_fallback
    mock_client = MagicMock()
    mock_client.embed.return_value = MagicMock(embeddings=[[0.1] * 1024])
    with patch("voyageai.Client", return_value=mock_client) as mock_ctor, \
         patch("nexus.retry.time.sleep"):
        _embed_with_fallback(["chunk"], "voyage-code-3", "test-key", timeout=60.0)
        mock_ctor.assert_called_once_with(api_key="test-key", timeout=60.0, max_retries=3)
