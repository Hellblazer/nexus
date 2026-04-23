# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for nexus.bib_enricher — all API calls mocked."""
from unittest.mock import MagicMock, patch

import httpx
import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_response(status_code: int = 200, json_data: dict | None = None) -> MagicMock:
    """Build a mock httpx.Response."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    if json_data is not None:
        resp.json.return_value = json_data
    # raise_for_status: raise on 4xx/5xx
    if status_code >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            message=f"HTTP {status_code}",
            request=MagicMock(),
            response=resp,
        )
    else:
        resp.raise_for_status.return_value = None
    return resp


_VALID_PAPER = {
    "paperId": "abc123",
    "title": "Attention Is All You Need",
    "year": 2017,
    "venue": "NeurIPS",
    "citationCount": 90000,
    "authors": [
        {"authorId": "1", "name": "Ashish Vaswani"},
        {"authorId": "2", "name": "Noam Shazeer"},
        {"authorId": "3", "name": "Niki Parmar"},
    ],
    "externalIds": {"DOI": "10.5555/3295222.3295349"},
}

_VALID_RESPONSE = {"data": [_VALID_PAPER]}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_enrich_success(monkeypatch):
    """Valid Semantic Scholar response returns correct metadata dict."""
    from nexus.bib_enricher import enrich

    mock_resp = _make_response(200, _VALID_RESPONSE)
    with patch("httpx.get", return_value=mock_resp):
        result = enrich("Attention Is All You Need")

    assert result["year"] == 2017
    assert result["venue"] == "NeurIPS"
    assert result["citation_count"] == 90000
    assert result["semantic_scholar_id"] == "abc123"
    assert "Ashish Vaswani" in result["authors"]


def test_enrich_returns_correct_fields(monkeypatch):
    """Field types: year=int, venue=str, authors=str (comma-sep), citation_count=int, semantic_scholar_id=str."""
    from nexus.bib_enricher import enrich

    mock_resp = _make_response(200, _VALID_RESPONSE)
    with patch("httpx.get", return_value=mock_resp):
        result = enrich("Attention Is All You Need")

    assert isinstance(result["year"], int)
    assert isinstance(result["venue"], str)
    assert isinstance(result["authors"], str)
    assert isinstance(result["citation_count"], int)
    assert isinstance(result["semantic_scholar_id"], str)
    # authors is comma-separated
    names = [n.strip() for n in result["authors"].split(",")]
    assert len(names) >= 1


def test_enrich_timeout():
    """TimeoutException returns empty dict."""
    from nexus.bib_enricher import enrich

    with patch("httpx.get", side_effect=httpx.TimeoutException("timed out")):
        result = enrich("Some Paper Title")

    assert result == {}


def test_enrich_404():
    """HTTP 404 returns empty dict."""
    from nexus.bib_enricher import enrich

    mock_resp = _make_response(404, {"error": "Not Found"})
    with patch("httpx.get", return_value=mock_resp):
        result = enrich("Unknown Paper")

    assert result == {}


def test_enrich_rate_limit_429():
    """HTTP 429 (rate-limit) returns empty dict.

    ``enrich`` retries 3 times with exponential backoff
    (5s / 10s / 20s = 35s) on persistent 429s. Patch ``time.sleep``
    to a no-op so the test doesn't actually wait; we still verify
    that the backoff was invoked the expected number of times
    and with the documented durations.
    """
    from nexus.bib_enricher import enrich

    sleep_calls: list[float] = []
    mock_resp = _make_response(429, {"message": "Too Many Requests"})
    with (
        patch("httpx.get", return_value=mock_resp),
        patch("time.sleep", side_effect=sleep_calls.append),
    ):
        result = enrich("Some Paper")

    assert result == {}
    # Backoff schedule: attempt 0 waits 5s, attempt 1 waits 10s,
    # attempt 2 waits 20s, attempt 3 is the last and raises-for-status
    # without sleeping (429 without raise is the 4th attempt).
    assert sleep_calls == [5.0, 10.0, 20.0], (
        f"unexpected backoff schedule: {sleep_calls!r}"
    )


def test_enrich_network_error():
    """ConnectError returns empty dict."""
    from nexus.bib_enricher import enrich

    with patch("httpx.get", side_effect=httpx.ConnectError("connection refused")):
        result = enrich("Some Paper")

    assert result == {}


def test_enrich_no_results():
    """Empty data array returns empty dict."""
    from nexus.bib_enricher import enrich

    mock_resp = _make_response(200, {"data": []})
    with patch("httpx.get", return_value=mock_resp):
        result = enrich("Totally Unknown Paper")

    assert result == {}


def test_enrich_authors_truncated():
    """More than 5 authors: only first 5 names are joined."""
    from nexus.bib_enricher import enrich

    many_authors = [{"authorId": str(i), "name": f"Author {i}"} for i in range(10)]
    paper = dict(_VALID_PAPER, authors=many_authors)
    mock_resp = _make_response(200, {"data": [paper]})

    with patch("httpx.get", return_value=mock_resp):
        result = enrich("Multi-Author Paper")

    names = [n.strip() for n in result["authors"].split(",")]
    assert len(names) == 5
    assert names[0] == "Author 0"
    assert names[4] == "Author 4"


def test_enrich_malformed_json():
    """Non-JSON response body (e.g., HTML error page) returns {}."""
    from nexus.bib_enricher import enrich

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.side_effect = ValueError("No JSON object could be decoded")

    with patch("httpx.get", return_value=mock_resp):
        result = enrich("Some Paper Title")

    assert result == {}


def test_enrich_null_references_and_authors():
    """SS returns explicit null for references/authors — must not crash (nexus-8d6e)."""
    from nexus.bib_enricher import enrich

    paper = dict(_VALID_PAPER, references=None, authors=None)
    mock_resp = _make_response(200, {"data": [paper]})

    with patch("httpx.get", return_value=mock_resp):
        result = enrich("Paper With Null Fields")

    assert result["references"] == []
    assert result["authors"] == ""
    assert result["semantic_scholar_id"] == "abc123"
