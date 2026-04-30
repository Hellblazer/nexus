# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Tests for nexus.bib_enricher_openalex (nexus-57mk).

OpenAlex is a drop-in alternative to Semantic Scholar for bibliographic
enrichment, with no API key required. The module mirrors
:mod:`nexus.bib_enricher`'s ``enrich(title) -> dict`` shape so the
catalog enrich hook and citation-link generator stay agnostic to the
backend.

All HTTP calls are mocked; tests do not touch the live OpenAlex API.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest


def _make_response(status_code: int = 200, json_data: dict | None = None) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    if json_data is not None:
        resp.json.return_value = json_data
    if status_code >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            message=f"HTTP {status_code}",
            request=MagicMock(),
            response=resp,
        )
    else:
        resp.raise_for_status.return_value = None
    return resp


# Real OpenAlex /works response shape (trimmed to the fields the
# enricher consumes). Field reference: https://docs.openalex.org/api-entities/works/work-object
_VALID_WORK = {
    "id": "https://openalex.org/W2741809807",
    "doi": "https://doi.org/10.5555/3295222.3295349",
    "display_name": "Attention Is All You Need",
    "publication_year": 2017,
    "cited_by_count": 90000,
    "primary_location": {
        "source": {"display_name": "NeurIPS"},
    },
    "authorships": [
        {"author": {"id": "A1", "display_name": "Ashish Vaswani"}},
        {"author": {"id": "A2", "display_name": "Noam Shazeer"}},
        {"author": {"id": "A3", "display_name": "Niki Parmar"}},
    ],
    "referenced_works": [
        "https://openalex.org/W11111",
        "https://openalex.org/W22222",
    ],
}

_VALID_RESPONSE = {"results": [_VALID_WORK]}


def test_enrich_success():
    from nexus.bib_enricher_openalex import enrich

    mock_resp = _make_response(200, _VALID_RESPONSE)
    with patch("httpx.get", return_value=mock_resp):
        result = enrich("Attention Is All You Need")

    assert result["year"] == 2017
    # Venue is the primary_location.source.display_name on modern OpenAlex.
    assert result["venue"] == "NeurIPS"
    assert result["citation_count"] == 90000
    # OpenAlex W-id with the URL prefix stripped.
    assert result["openalex_id"] == "W2741809807"
    # Authors comma-separated, top 5.
    assert "Ashish Vaswani" in result["authors"]
    # DOI without the URL prefix.
    assert result["doi"] == "10.5555/3295222.3295349"
    # references is the list of referenced W-ids.
    assert result["references"] == ["W11111", "W22222"]


def test_enrich_field_types():
    from nexus.bib_enricher_openalex import enrich

    mock_resp = _make_response(200, _VALID_RESPONSE)
    with patch("httpx.get", return_value=mock_resp):
        result = enrich("Attention Is All You Need")

    assert isinstance(result["year"], int)
    assert isinstance(result["venue"], str)
    assert isinstance(result["authors"], str)
    assert isinstance(result["citation_count"], int)
    assert isinstance(result["openalex_id"], str)
    assert isinstance(result["doi"], str)
    assert isinstance(result["references"], list)


def test_enrich_no_results():
    from nexus.bib_enricher_openalex import enrich

    mock_resp = _make_response(200, {"results": []})
    with patch("httpx.get", return_value=mock_resp):
        result = enrich("Totally Unknown Paper")
    assert result == {}


def test_enrich_timeout():
    from nexus.bib_enricher_openalex import enrich

    with patch("httpx.get", side_effect=httpx.TimeoutException("timed out")):
        result = enrich("Some Paper")
    assert result == {}


def test_enrich_404():
    from nexus.bib_enricher_openalex import enrich

    mock_resp = _make_response(404, {"error": "Not Found"})
    with patch("httpx.get", return_value=mock_resp):
        result = enrich("Some Paper")
    assert result == {}


def test_enrich_429_retries_with_backoff():
    """OpenAlex returns 429 on rate-limit; enricher retries with backoff."""
    from nexus.bib_enricher_openalex import enrich

    sleep_calls: list[float] = []
    mock_resp = _make_response(429, {"message": "rate limited"})
    with (
        patch("httpx.get", return_value=mock_resp),
        patch("time.sleep", side_effect=sleep_calls.append),
    ):
        result = enrich("Some Paper")

    assert result == {}
    # Same backoff schedule as the S2 enricher: 5s / 10s / 20s.
    assert sleep_calls == [5.0, 10.0, 20.0]


def test_enrich_network_error():
    from nexus.bib_enricher_openalex import enrich

    with patch("httpx.get", side_effect=httpx.ConnectError("conn refused")):
        result = enrich("Some Paper")
    assert result == {}


def test_enrich_malformed_json():
    from nexus.bib_enricher_openalex import enrich

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.side_effect = ValueError("not json")
    with patch("httpx.get", return_value=mock_resp):
        result = enrich("Some Paper")
    assert result == {}


def test_enrich_authors_truncated_to_five():
    from nexus.bib_enricher_openalex import enrich

    work = dict(_VALID_WORK)
    work["display_name"] = "Multi-Author Paper Heuristic Methods"
    work["authorships"] = [
        {"author": {"id": f"A{i}", "display_name": f"Author {i}"}}
        for i in range(10)
    ]
    mock_resp = _make_response(200, {"results": [work]})
    with patch("httpx.get", return_value=mock_resp):
        result = enrich("Multi-Author Paper Heuristic Methods")

    names = [n.strip() for n in result["authors"].split(",")]
    assert len(names) == 5
    assert names[0] == "Author 0"
    assert names[4] == "Author 4"


def test_enrich_handles_null_authorships_and_references():
    """OpenAlex sometimes returns explicit null for authorships /
    referenced_works on partial-coverage works. Enricher must not crash."""
    from nexus.bib_enricher_openalex import enrich

    work = dict(_VALID_WORK)
    work["display_name"] = "Sparse Paper Sparse Authors Sparse References"
    work["authorships"] = None
    work["referenced_works"] = None
    mock_resp = _make_response(200, {"results": [work]})
    with patch("httpx.get", return_value=mock_resp):
        result = enrich("Sparse Paper Sparse Authors Sparse References")

    assert result["authors"] == ""
    assert result["references"] == []
    assert result["openalex_id"] == "W2741809807"


def test_enrich_handles_missing_primary_location():
    """Works without a primary_location yield empty venue, no crash."""
    from nexus.bib_enricher_openalex import enrich

    work = dict(_VALID_WORK)
    work["display_name"] = "Venueless Paper Without Primary Location"
    work.pop("primary_location")
    mock_resp = _make_response(200, {"results": [work]})
    with patch("httpx.get", return_value=mock_resp):
        result = enrich("Venueless Paper Without Primary Location")
    assert result["venue"] == ""
    assert result["openalex_id"] == "W2741809807"


def test_enrich_handles_missing_doi():
    from nexus.bib_enricher_openalex import enrich

    work = dict(_VALID_WORK)
    work["display_name"] = "DOIless Paper Lacking Identifier"
    work.pop("doi")
    mock_resp = _make_response(200, {"results": [work]})
    with patch("httpx.get", return_value=mock_resp):
        result = enrich("DOIless Paper Lacking Identifier")
    assert result["doi"] == ""


def test_enrich_polite_pool_passes_mailto(monkeypatch):
    """When OPENALEX_MAILTO is set, the enricher includes it in the
    query string for the OpenAlex 'polite pool' (higher rate limits)."""
    from nexus.bib_enricher_openalex import enrich

    monkeypatch.setenv("OPENALEX_MAILTO", "test@example.com")

    captured: dict = {}

    def _capture(*args, **kwargs):
        captured.update(kwargs)
        return _make_response(200, _VALID_RESPONSE)

    with patch("httpx.get", side_effect=_capture):
        enrich("Paper")

    assert captured["params"].get("mailto") == "test@example.com"


def test_enrich_anonymous_when_mailto_missing(monkeypatch):
    """No OPENALEX_MAILTO env: no mailto param sent (anonymous pool)."""
    from nexus.bib_enricher_openalex import enrich

    monkeypatch.delenv("OPENALEX_MAILTO", raising=False)
    captured: dict = {}

    def _capture(*args, **kwargs):
        captured.update(kwargs)
        return _make_response(200, _VALID_RESPONSE)

    with patch("httpx.get", side_effect=_capture):
        enrich("Paper")

    assert "mailto" not in captured["params"]


# ── nexus-sbzr: direct-by-id lookups ────────────────────────────────────────


def test_enrich_by_doi_calls_correct_endpoint():
    from nexus.bib_enricher_openalex import enrich_by_doi

    captured: list[str] = []

    def _capture(url, *args, **kwargs):
        captured.append(url)
        return _make_response(200, _VALID_WORK)

    with patch("httpx.get", side_effect=_capture):
        result = enrich_by_doi("10.5555/3295222.3295349")

    assert captured == ["https://api.openalex.org/works/doi:10.5555/3295222.3295349"]
    assert result["openalex_id"] == "W2741809807"
    assert result["year"] == 2017
    assert result["doi"] == "10.5555/3295222.3295349"


def test_enrich_by_doi_strips_url_prefix():
    """If caller passes ``https://doi.org/10.x/y`` the helper still
    constructs the bare-DOI endpoint URL."""
    from nexus.bib_enricher_openalex import enrich_by_doi

    captured: list[str] = []

    def _capture(url, *args, **kwargs):
        captured.append(url)
        return _make_response(200, _VALID_WORK)

    with patch("httpx.get", side_effect=_capture):
        enrich_by_doi("https://doi.org/10.5555/3295222.3295349")

    assert "doi:10.5555/3295222.3295349" in captured[0]
    assert "doi.org" not in captured[0]


def test_enrich_by_doi_returns_empty_on_404():
    from nexus.bib_enricher_openalex import enrich_by_doi

    with patch("httpx.get", return_value=_make_response(404, {"error": "not found"})):
        assert enrich_by_doi("10.x/missing") == {}


def test_enrich_by_doi_empty_input_returns_empty():
    from nexus.bib_enricher_openalex import enrich_by_doi

    assert enrich_by_doi("") == {}
    assert enrich_by_doi(None) == {}  # type: ignore[arg-type]


def test_enrich_by_arxiv_id_calls_correct_endpoint():
    """OpenAlex doesn't have a native arxiv: external-ID lookup, so
    enrich_by_arxiv_id constructs the arXiv-DOI form
    (10.48550/arXiv.<id>) and uses the by-DOI endpoint."""
    from nexus.bib_enricher_openalex import enrich_by_arxiv_id

    captured: list[str] = []

    def _capture(url, *args, **kwargs):
        captured.append(url)
        return _make_response(200, _VALID_WORK)

    with patch("httpx.get", side_effect=_capture):
        result = enrich_by_arxiv_id("2503.07641")

    assert captured == [
        "https://api.openalex.org/works/doi:10.48550/arXiv.2503.07641"
    ]
    assert result["openalex_id"] == "W2741809807"


def test_enrich_by_arxiv_id_returns_empty_on_404():
    from nexus.bib_enricher_openalex import enrich_by_arxiv_id

    with patch("httpx.get", return_value=_make_response(404, {})):
        assert enrich_by_arxiv_id("9999.99999") == {}


def test_enrich_by_arxiv_id_empty_input_returns_empty():
    from nexus.bib_enricher_openalex import enrich_by_arxiv_id

    assert enrich_by_arxiv_id("") == {}


def test_direct_lookup_includes_mailto(monkeypatch):
    from nexus.bib_enricher_openalex import enrich_by_doi

    monkeypatch.setenv("OPENALEX_MAILTO", "test@example.com")
    captured: dict = {}

    def _capture(url, *args, **kwargs):
        captured.update(kwargs)
        return _make_response(200, _VALID_WORK)

    with patch("httpx.get", side_effect=_capture):
        enrich_by_doi("10.x/y")

    assert captured["params"]["mailto"] == "test@example.com"


# ── nexus-yy1m: title-validation post-lookup ────────────────────────────────


def test_titles_compatible_helper_basics():
    """The helper is exported so callers (commands/enrich._resolve_bib_for_title)
    can validate title shapes outside the OpenAlex backend too. Source-paper
    title vs. citation-poisoned title is the canonical reject case."""
    from nexus.bib_enricher_openalex import _titles_compatible

    # CacheRAG-ish vs the embedded-systems paper W2912099628 returned for
    # the citation DOI 10.1145/3742872. These share zero substantive tokens.
    assert not _titles_compatible(
        "Cacherag A Semantic Caching System For Retrieval Augmented Generation In Knowledge Graph Question Answering",
        "Power Management Strategies for Embedded Multicore Systems",
    )

    # Same paper, slightly different capitalization / punctuation.
    assert _titles_compatible(
        "CacheRAG: A Semantic Caching System for RAG in Knowledge Graph QA",
        "CacheRAG - a semantic caching system for retrieval-augmented generation in knowledge graph question answering",
    )

    # Empty inputs do not crash; treat as incompatible (caller should
    # fall through to next path, not stamp empty metadata).
    assert not _titles_compatible("", "anything")
    assert not _titles_compatible("anything", "")
    assert not _titles_compatible("", "")


def test_enrich_by_doi_title_validation_rejects_mismatch():
    """nexus-yy1m: when expected_title is supplied, a low-similarity
    OpenAlex result is rejected with {}, so callers can fall through to
    the next lookup path instead of stamping the wrong paper."""
    from nexus.bib_enricher_openalex import enrich_by_doi

    # OpenAlex returns a paper unrelated to the expected title.
    foreign_work = {
        **_VALID_WORK,
        "id": "https://openalex.org/W9999",
        "display_name": "Power Management for Embedded Multicore Systems",
    }
    with patch("httpx.get", return_value=_make_response(200, foreign_work)):
        result = enrich_by_doi(
            "10.1145/3742872",
            expected_title="CacheRAG: A Semantic Caching System for RAG in KGQA",
        )

    assert result == {}, (
        f"expected {{}} when lookup-title and expected-title disagree, got {result!r}"
    )


def test_enrich_by_doi_title_validation_accepts_match():
    """nexus-yy1m: a high-similarity title passes through unchanged."""
    from nexus.bib_enricher_openalex import enrich_by_doi

    matching_work = {
        **_VALID_WORK,
        "id": "https://openalex.org/W9001",
        "display_name": "Attention Is All You Need: Transformers for Sequence Transduction",
    }
    with patch("httpx.get", return_value=_make_response(200, matching_work)):
        result = enrich_by_doi(
            "10.5555/3295222.3295349",
            expected_title="Attention Is All You Need",
        )

    assert result.get("openalex_id") == "W9001", (
        f"expected the lookup result through unchanged, got {result!r}"
    )


def test_enrich_by_doi_no_expected_title_keeps_legacy_behavior():
    """nexus-yy1m back-compat: callers that don't pass expected_title
    keep the pre-fix shape (return whatever OpenAlex says without
    title-checking)."""
    from nexus.bib_enricher_openalex import enrich_by_doi

    foreign_work = {
        **_VALID_WORK,
        "display_name": "Some Completely Different Paper",
    }
    with patch("httpx.get", return_value=_make_response(200, foreign_work)):
        result = enrich_by_doi("10.x/y")  # no expected_title

    assert result.get("openalex_id") == "W2741809807"


def test_enrich_title_search_rejects_irrelevant_top_hit():
    """nexus-yy1m: OpenAlex /works?search= returns SOMETHING for almost
    every query, ranked by its relevance score. When the real paper
    isn't indexed (preprint, not yet accepted), the first result is
    frequently a completely unrelated paper that shares a token or two
    with the query. The validator must reject these so the caller does
    not stamp the wrong paper's metadata.

    Live shakeout case: a CacheRAG title query against OpenAlex returned
    a 'Proceedings of ... Compilers, Architecture, and Synthesis for
    Embedded Systems' paper (W2912099628) at the top of the results.
    """
    from nexus.bib_enricher_openalex import enrich

    irrelevant_work = {
        **_VALID_WORK,
        "id": "https://openalex.org/W2912099628",
        "display_name": "Proceedings of the International Conference on Compilers, Architecture, and Synthesis for Embedded Systems",
    }
    mock_resp = _make_response(200, {"results": [irrelevant_work]})
    with patch("httpx.get", return_value=mock_resp):
        result = enrich(
            "Cacherag A Semantic Caching System For Retrieval Augmented Generation In Knowledge Graph Question Answering"
        )

    assert result == {}, (
        f"expected {{}} when the title-search top hit is irrelevant, got {result!r}"
    )


def test_enrich_by_arxiv_id_title_validation_rejects_mismatch():
    """nexus-yy1m: same guard on the arXiv path."""
    from nexus.bib_enricher_openalex import enrich_by_arxiv_id

    foreign_work = {
        **_VALID_WORK,
        "display_name": "Power Management for Embedded Multicore Systems",
    }
    with patch("httpx.get", return_value=_make_response(200, foreign_work)):
        result = enrich_by_arxiv_id(
            "2503.07641",
            expected_title="CacheRAG: A Semantic Caching System for RAG",
        )

    assert result == {}
