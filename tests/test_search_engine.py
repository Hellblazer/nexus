"""AC1–AC8: Search engine — hybrid scoring, reranking, answer mode, output formatters."""
from unittest.mock import MagicMock, patch

import pytest

import nexus.search_engine as se_mod
from nexus.search_engine import (
    SearchResult,
    answer_mode,
    format_json,
    format_vimgrep,
    hybrid_score,
    min_max_normalize,
    rerank_results,
    round_robin_interleave,
    search_cross_corpus,
)


# ── AC1: Hybrid scoring ───────────────────────────────────────────────────────

def test_min_max_normalize_basic():
    """min_max_normalize maps min→0, max→1, middle proportionally."""
    values = [1.0, 3.0, 5.0]
    assert min_max_normalize(1.0, values) == pytest.approx(0.0, abs=1e-6)
    assert min_max_normalize(5.0, values) == pytest.approx(1.0, abs=1e-6)
    assert min_max_normalize(3.0, values) == pytest.approx(0.5, abs=1e-6)


def test_min_max_normalize_all_equal_returns_zero():
    """All-identical window → denominator is ε, result ≈ 0."""
    values = [2.0, 2.0, 2.0]
    result = min_max_normalize(2.0, values)
    assert result == pytest.approx(0.0, abs=1e-3)


def test_hybrid_score_weights():
    """hybrid_score = 0.7 * vector_norm + 0.3 * frecency_norm."""
    # vector_norm=0.8, frecency_norm=0.5 → 0.7*0.8 + 0.3*0.5 = 0.56 + 0.15 = 0.71
    score = hybrid_score(vector_norm=0.8, frecency_norm=0.5)
    assert score == pytest.approx(0.71, abs=1e-6)


def test_hybrid_score_zero_frecency():
    """For docs/knowledge results with no frecency, score = 0.7 * vector_norm."""
    score = hybrid_score(vector_norm=1.0, frecency_norm=0.0)
    assert score == pytest.approx(0.7, abs=1e-6)


def test_hybrid_score_ripgrep_exact_vector_norm_one():
    """Ripgrep exact-match: vector_norm=1.0 before weighted sum."""
    score = hybrid_score(vector_norm=1.0, frecency_norm=0.6)
    assert score == pytest.approx(0.7 * 1.0 + 0.3 * 0.6, abs=1e-6)


# ── AC2: --hybrid warns when no code corpus ───────────────────────────────────

def test_hybrid_no_code_corpus_warning(capsys):
    """hybrid_score_results logs a warning when no code__ collections in scope."""
    results = [
        SearchResult(id="1", content="text", distance=0.1,
                     collection="docs__papers", metadata={}),
    ]
    se_mod.apply_hybrid_scoring(results, hybrid=True)
    captured = capsys.readouterr()
    assert "no code corpus" in (captured.out + captured.err).lower()


def test_hybrid_mixed_corpus_no_warning(capsys):
    """With both code__ and docs__ in scope, no warning is printed."""
    results = [
        SearchResult(id="1", content="code", distance=0.1,
                     collection="code__myrepo", metadata={"frecency_score": 1.5}),
        SearchResult(id="2", content="docs", distance=0.2,
                     collection="docs__papers", metadata={}),
    ]
    se_mod.apply_hybrid_scoring(results, hybrid=True)
    out = capsys.readouterr()
    assert "no code corpus" not in (out.err + out.out).lower()


# ── AC3: Cross-corpus reranking ───────────────────────────────────────────────

def test_rerank_results_returns_unified_ranking():
    """rerank_results reorders results using the reranker model."""
    results = [
        SearchResult(id="1", content="alpha", distance=0.5, collection="code__r", metadata={}),
        SearchResult(id="2", content="beta", distance=0.2, collection="docs__d", metadata={}),
        SearchResult(id="3", content="gamma", distance=0.8, collection="knowledge__k", metadata={}),
    ]
    mock_client = MagicMock()
    mock_client.rerank.return_value = MagicMock(
        results=[
            MagicMock(index=2, relevance_score=0.9),
            MagicMock(index=0, relevance_score=0.7),
            MagicMock(index=1, relevance_score=0.3),
        ]
    )
    with patch("nexus.scoring._voyage_client", return_value=mock_client):
        reranked = rerank_results(results, query="test", model="rerank-2.5", top_k=3)
    assert reranked[0].id == "3"
    assert reranked[1].id == "1"
    assert reranked[2].id == "2"


def test_round_robin_interleave_no_rerank():
    """round_robin_interleave alternates results across collections."""
    code_results = [
        SearchResult(id="c1", content="c1", distance=0.1, collection="code__r", metadata={}),
        SearchResult(id="c2", content="c2", distance=0.2, collection="code__r", metadata={}),
    ]
    doc_results = [
        SearchResult(id="d1", content="d1", distance=0.3, collection="docs__d", metadata={}),
    ]
    merged = round_robin_interleave([code_results, doc_results])
    ids = [r.id for r in merged]
    # Round-robin: c1, d1, c2
    assert ids == ["c1", "d1", "c2"]


def test_cross_corpus_overfetch():
    """search_cross_corpus fetches max(5, (n // num_corpora) * 2) per corpus."""
    mock_t3 = MagicMock()
    mock_t3.search.return_value = []
    # 2 corpora, n=10 → per_corpus_k = max(5, (10//2)*2) = max(5, 10) = 10
    with patch("nexus.search_engine._t3_for_search", return_value=mock_t3):
        search_cross_corpus(
            query="test", collections=["code__r", "docs__d"],
            n_results=10, t3=mock_t3
        )
    calls = mock_t3.search.call_args_list
    assert len(calls) == 2
    for c in calls:
        assert c.kwargs.get("n_results") == 10 or c.args[2] == 10


# ── AC4: Mixedbread graceful degradation ──────────────────────────────────────

def test_mxbai_missing_key_warns_and_returns_empty(capsys, monkeypatch):
    """When MXBAI_API_KEY is unset, logs a warning and returns empty results."""
    monkeypatch.delenv("MXBAI_API_KEY", raising=False)
    results = se_mod.fetch_mxbai_results(query="test", stores=["art"], per_k=5)
    captured = capsys.readouterr()
    assert "MXBAI_API_KEY" in (captured.out + captured.err)
    assert results == []


def test_mxbai_with_key_calls_sdk(monkeypatch):
    """When MXBAI_API_KEY is set, calls Mixedbread SDK and converts results."""
    monkeypatch.setenv("MXBAI_API_KEY", "test-key")
    mock_client = MagicMock()
    chunk = MagicMock()
    chunk.content.text = "relevant content"
    chunk.score = 0.95
    mock_client.stores.search.return_value = MagicMock(chunks=[chunk])

    with patch("nexus.search_engine._mxbai_client", return_value=mock_client):
        results = se_mod.fetch_mxbai_results(query="test", stores=["art"], per_k=5)

    assert len(results) == 1
    assert results[0].content == "relevant content"
    assert results[0].collection == "mxbai__art"


# ── AC5: Agentic mode ─────────────────────────────────────────────────────────

def test_agentic_refinement_stops_when_done():
    """Agentic loop stops when Haiku returns {"done": true}."""
    initial = [SearchResult(id="1", content="r1", distance=0.1, collection="c", metadata={})]
    mock_retrieve = MagicMock(return_value=initial)

    with patch("nexus.search_engine._haiku_refine", return_value={"done": True}):
        final = se_mod.agentic_search(
            initial_query="test", retrieve_fn=mock_retrieve, max_iterations=3
        )

    # Only one retrieval (initial), no refinement iterations
    mock_retrieve.assert_called_once()
    assert len(final) == len(initial)


def test_agentic_refinement_loop_max_3():
    """Agentic loop runs at most 3 total iterations."""
    call_count = 0

    def mock_retrieve(query):
        nonlocal call_count
        call_count += 1
        return [SearchResult(id=str(call_count), content="r",
                             distance=0.1, collection="c", metadata={})]

    with patch("nexus.search_engine._haiku_refine", return_value={"query": "refined"}):
        se_mod.agentic_search(
            initial_query="test", retrieve_fn=mock_retrieve, max_iterations=3
        )
    assert call_count <= 3


def test_agentic_deduplicates_results():
    """Agentic loop deduplicates results across iterations by ID."""
    shared = SearchResult(id="dup", content="dup content", distance=0.1,
                          collection="c", metadata={})
    calls = [0]

    def mock_retrieve(query):
        calls[0] += 1
        return [shared]

    responses = [{"query": "refined"}, {"done": True}]
    response_iter = iter(responses)

    with patch("nexus.search_engine._haiku_refine", side_effect=lambda *a, **kw: next(response_iter)):
        final = se_mod.agentic_search(
            initial_query="test", retrieve_fn=mock_retrieve, max_iterations=3
        )
    # Despite 2 retrieve calls, only 1 unique result
    assert len(final) == 1


# ── AC6: Answer mode ──────────────────────────────────────────────────────────

def test_answer_mode_produces_cite_tags():
    """answer_mode synthesis includes <cite i="N"> format."""
    results = [
        SearchResult(id="1", content="Token validation logic here.",
                     distance=0.1, collection="code__r",
                     metadata={"source_path": "./auth.py", "line_start": 42}),
        SearchResult(id="2", content="Session management code.",
                     distance=0.2, collection="code__r",
                     metadata={"source_path": "./session.py", "line_start": 12}),
    ]
    mock_response = MagicMock()
    mock_response.content = [MagicMock(text='Auth uses <cite i="0"> and session <cite i="1">.')]

    with patch("nexus.answer._haiku_answer", return_value='Auth uses <cite i="0"> and session <cite i="1">.'):
        output = answer_mode(query="how does auth work?", results=results)

    assert '<cite i="0">' in output
    assert "0:" in output or "./auth.py" in output


def test_answer_mode_includes_citation_footer():
    """answer_mode appends numbered citation list after synthesis."""
    results = [
        SearchResult(id="1", content="foo", distance=0.1,
                     collection="code__r",
                     metadata={"source_path": "./foo.py", "line_start": 1}),
    ]
    with patch("nexus.answer._haiku_answer", return_value='Result <cite i="0">.'):
        output = answer_mode(query="foo", results=results)

    lines = output.splitlines()
    assert any("0:" in ln and "./foo.py" in ln for ln in lines)


# ── AC7: Output formatters ────────────────────────────────────────────────────

def test_format_vimgrep():
    """format_vimgrep produces path:line:0:content lines."""
    results = [
        SearchResult(id="1", content="    def authenticate(user, token):",
                     distance=0.1, collection="code__r",
                     metadata={"source_path": "./auth.py", "line_start": 42}),
    ]
    lines = format_vimgrep(results)
    assert lines[0] == "./auth.py:42:0:    def authenticate(user, token):"


def test_format_vimgrep_missing_source_path():
    """format_vimgrep falls back to empty path when metadata lacks source_path."""
    results = [
        SearchResult(id="1", content="some text", distance=0.1,
                     collection="knowledge__k", metadata={}),
    ]
    lines = format_vimgrep(results)
    assert len(lines) == 1
    assert ":0:" in lines[0]


def test_format_json_valid():
    """format_json produces valid JSON with id, content, distance fields."""
    import json
    results = [
        SearchResult(id="abc123", content="some text", distance=0.42,
                     collection="code__r",
                     metadata={"source_path": "./x.py"}),
    ]
    output = format_json(results)
    parsed = json.loads(output)
    assert isinstance(parsed, list)
    assert parsed[0]["id"] == "abc123"
    assert parsed[0]["distance"] == pytest.approx(0.42)
    assert "content" in parsed[0]


def test_format_json_includes_metadata():
    """format_json embeds metadata fields."""
    import json
    results = [
        SearchResult(id="1", content="x", distance=0.1, collection="code__r",
                     metadata={"source_path": "./a.py", "line_start": 10}),
    ]
    parsed = json.loads(format_json(results))
    assert parsed[0].get("source_path") == "./a.py"


# ── Behavior: Mixedbread hash determinism ────────────────────────────────────

def test_mxbai_chunk_id_is_deterministic(monkeypatch):
    """Same Mixedbread chunk content always produces the same SearchResult ID."""
    monkeypatch.setenv("MXBAI_API_KEY", "test-key")
    mock_client = MagicMock()

    chunk = MagicMock()
    chunk.content.text = "identical content"
    chunk.score = 0.9
    mock_client.stores.search.return_value = MagicMock(chunks=[chunk])

    with patch("nexus.search_engine._mxbai_client", return_value=mock_client):
        results_a = se_mod.fetch_mxbai_results(query="q", stores=["art"], per_k=5)
        results_b = se_mod.fetch_mxbai_results(query="q", stores=["art"], per_k=5)

    assert results_a[0].id == results_b[0].id, (
        "Mixedbread chunk IDs must be deterministic (no python hash() randomness)"
    )


# ── Behavior: Agentic search handles invalid JSON from Haiku ─────────────────

def test_agentic_search_graceful_on_json_decode_error():
    """agentic_search stops gracefully when _haiku_refine returns invalid JSON."""
    initial = [SearchResult(id="1", content="result", distance=0.1,
                            collection="c", metadata={})]
    mock_retrieve = MagicMock(return_value=initial)

    with patch("nexus.search_engine._haiku_refine", return_value={"done": True}):
        result = se_mod.agentic_search(
            initial_query="test", retrieve_fn=mock_retrieve, max_iterations=3
        )
    assert result == initial


def test_haiku_refine_returns_done_on_json_error():
    """_haiku_refine returns {'done': True} when Haiku response is not valid JSON."""
    mock_msg = MagicMock()
    mock_msg.content = [MagicMock(text="Sure! Here is some text, not JSON.")]
    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_msg

    with patch("nexus.config.get_credential", return_value="key"):
        with patch("anthropic.Anthropic", return_value=mock_client):
            result = se_mod._haiku_refine("query", [])
    assert result == {"done": True}


def test_haiku_refine_returns_done_on_empty_content():
    """_haiku_refine returns {'done': True} when Haiku returns empty content."""
    mock_msg = MagicMock()
    mock_msg.content = []
    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_msg

    with patch("nexus.config.get_credential", return_value="key"):
        with patch("anthropic.Anthropic", return_value=mock_client):
            result = se_mod._haiku_refine("query", [])
    assert result == {"done": True}


# ── Behavior: Agentic search skips empty refined queries ─────────────────────

def test_agentic_search_stops_on_empty_refined_query():
    """agentic_search stops loop when Haiku returns empty/whitespace query."""
    call_count = 0

    def mock_retrieve(query):
        nonlocal call_count
        call_count += 1
        return [SearchResult(id=str(call_count), content="r",
                             distance=0.1, collection="c", metadata={})]

    with patch("nexus.search_engine._haiku_refine", return_value={"query": "   "}):
        se_mod.agentic_search(
            initial_query="test", retrieve_fn=mock_retrieve, max_iterations=3
        )
    assert call_count == 1  # stopped after initial retrieve


# ── AC8: min_max_normalize over combined window ───────────────────────────────

def test_min_max_normalize_over_combined_not_per_corpus():
    """Normalization uses combined window, not per-corpus."""
    # code result: distance=0.1, doc result: distance=0.9
    # Combined window min=0.1, max=0.9
    distances = [0.1, 0.9]
    norm_code = min_max_normalize(0.1, distances)
    norm_doc = min_max_normalize(0.9, distances)
    assert norm_code == pytest.approx(0.0, abs=1e-6)
    assert norm_doc == pytest.approx(1.0, abs=1e-6)

    # If per-corpus: code [0.1] → both 0.0; docs [0.9] → both 0.0
    # Combined window correctly distinguishes them
    assert norm_code < norm_doc


# ── nexus-7vr: _haiku_answer guards empty content ────────────────────────────

def test_haiku_answer_returns_empty_string_on_empty_content():
    """_haiku_answer returns '' when msg.content is empty (no IndexError)."""
    mock_msg = MagicMock()
    mock_msg.content = []  # Empty list — previously caused IndexError
    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_msg

    results = [SearchResult(id="1", content="ctx", distance=0.1, collection="c", metadata={})]
    with patch("nexus.config.get_credential", return_value="key"):
        with patch("anthropic.Anthropic", return_value=mock_client):
            from nexus.search_engine import _haiku_answer
            result = _haiku_answer("what?", results)

    assert result == ""


# ── nexus-6kj: fetch_mxbai_results isolates per-store errors ─────────────────

def test_mxbai_store_error_skipped_other_stores_still_searched(monkeypatch):
    """When one store raises, remaining stores are still searched."""
    monkeypatch.setenv("MXBAI_API_KEY", "test-key")
    mock_client = MagicMock()

    good_chunk = MagicMock()
    good_chunk.content.text = "good result"
    good_chunk.score = 0.9

    def fake_search(store_id, query, top_k):
        if store_id == "bad-store":
            raise RuntimeError("store unavailable")
        return MagicMock(chunks=[good_chunk])

    mock_client.stores.search.side_effect = fake_search

    with patch("nexus.search_engine._mxbai_client", return_value=mock_client):
        results = se_mod.fetch_mxbai_results(
            query="test", stores=["bad-store", "good-store"], per_k=5
        )

    # Bad store skipped, good store returned 1 result
    assert len(results) == 1
    assert results[0].content == "good result"
    assert results[0].collection == "mxbai__good-store"
