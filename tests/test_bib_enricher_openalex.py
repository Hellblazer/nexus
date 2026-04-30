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
    work["authorships"] = [
        {"author": {"id": f"A{i}", "display_name": f"Author {i}"}}
        for i in range(10)
    ]
    mock_resp = _make_response(200, {"results": [work]})
    with patch("httpx.get", return_value=mock_resp):
        result = enrich("Multi-Author Paper")

    names = [n.strip() for n in result["authors"].split(",")]
    assert len(names) == 5
    assert names[0] == "Author 0"
    assert names[4] == "Author 4"


def test_enrich_handles_null_authorships_and_references():
    """OpenAlex sometimes returns explicit null for authorships /
    referenced_works on partial-coverage works. Enricher must not crash."""
    from nexus.bib_enricher_openalex import enrich

    work = dict(_VALID_WORK)
    work["authorships"] = None
    work["referenced_works"] = None
    mock_resp = _make_response(200, {"results": [work]})
    with patch("httpx.get", return_value=mock_resp):
        result = enrich("Sparse Paper")

    assert result["authors"] == ""
    assert result["references"] == []
    assert result["openalex_id"] == "W2741809807"


def test_enrich_handles_missing_primary_location():
    """Works without a primary_location yield empty venue, no crash."""
    from nexus.bib_enricher_openalex import enrich

    work = dict(_VALID_WORK)
    work.pop("primary_location")
    mock_resp = _make_response(200, {"results": [work]})
    with patch("httpx.get", return_value=mock_resp):
        result = enrich("Venue-Less Paper")
    assert result["venue"] == ""
    assert result["openalex_id"] == "W2741809807"


def test_enrich_handles_missing_doi():
    from nexus.bib_enricher_openalex import enrich

    work = dict(_VALID_WORK)
    work.pop("doi")
    mock_resp = _make_response(200, {"results": [work]})
    with patch("httpx.get", return_value=mock_resp):
        result = enrich("DOI-Less Paper")
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
