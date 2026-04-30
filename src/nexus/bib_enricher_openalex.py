# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Bibliographic metadata enrichment via the OpenAlex API (nexus-57mk).

Drop-in alternative to :mod:`nexus.bib_enricher` (Semantic Scholar)
that does not require an API key. Same ``enrich(title) -> dict``
contract; the catalog enrich hook stores the OpenAlex W-id under
``bib_openalex_id`` and the citation-link generator matches against
either backend's ID space.

Set ``OPENALEX_MAILTO`` to your email for the OpenAlex 'polite pool'
(higher rate limits). API reference: https://docs.openalex.org/.
"""
from __future__ import annotations

import os
from typing import Any

import httpx
import structlog

_log = structlog.get_logger(__name__)

_BASE_URL = "https://api.openalex.org/works"
_TIMEOUT = 10.0  # parallels bib_enricher.py; keeps both backends comparable

_MAX_RETRIES = 3
_BACKOFF_BASE = 5.0  # seconds; doubles each retry, identical to S2 backoff


def _params(title: str) -> dict[str, str]:
    """Build the ``/works`` query string. Includes ``mailto`` when
    ``OPENALEX_MAILTO`` is set so OpenAlex routes us through the polite
    pool (higher rate limits, more stable latency)."""
    params: dict[str, str] = {
        "search": title,
        "per-page": "1",
    }
    mailto = os.environ.get("OPENALEX_MAILTO", "").strip()
    if mailto:
        params["mailto"] = mailto
    return params


def _strip_openalex_prefix(value: str) -> str:
    """Strip the ``https://openalex.org/`` URL prefix from an OpenAlex
    ID, leaving the bare ``W<digits>`` form. Same convention used by
    the S2 backend's ``paperId`` field."""
    return value.rsplit("/", 1)[-1] if value else ""


def _strip_doi_prefix(value: str) -> str:
    """Strip ``https://doi.org/`` from a DOI URL, leaving the bare
    ``10.xxxx/...`` form."""
    if not value:
        return ""
    if value.startswith("https://doi.org/"):
        return value[len("https://doi.org/"):]
    if value.startswith("http://doi.org/"):
        return value[len("http://doi.org/"):]
    return value


def _build_result(work: dict) -> dict[str, Any]:
    """Shared shape-builder. Maps an OpenAlex /works object to the
    canonical bib_enricher result dict."""
    authorships = work.get("authorships") or []
    authors = ", ".join(
        (a.get("author") or {}).get("display_name", "")
        for a in authorships[:5]
    )
    primary = work.get("primary_location") or {}
    source = (primary.get("source") or {}) if isinstance(primary, dict) else {}
    venue = source.get("display_name", "") if isinstance(source, dict) else ""
    referenced = work.get("referenced_works") or []
    refs = [_strip_openalex_prefix(r) for r in referenced if r]
    return {
        "year": work.get("publication_year", 0) or 0,
        "venue": venue or "",
        "authors": authors or "",
        "citation_count": work.get("cited_by_count", 0) or 0,
        "openalex_id": _strip_openalex_prefix(work.get("id", "")) or "",
        "doi": _strip_doi_prefix(work.get("doi", "") or ""),
        "references": refs,
    }


def _direct_lookup(url: str) -> dict[str, Any]:
    """Direct ``/works/<id>`` GET. Returns the canonical bib dict on
    success, ``{}`` on failure (404, network error, malformed payload).
    Same retry shape as :func:`enrich` for 429s."""
    import time

    params = {}
    mailto = os.environ.get("OPENALEX_MAILTO", "").strip()
    if mailto:
        params["mailto"] = mailto

    for attempt in range(_MAX_RETRIES + 1):
        try:
            resp = httpx.get(url, params=params, timeout=_TIMEOUT)
            if resp.status_code == 429 and attempt < _MAX_RETRIES:
                wait = _BACKOFF_BASE * (2 ** attempt)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return _build_result(resp.json())
        except (
            httpx.HTTPError,
            httpx.TimeoutException,
            httpx.ConnectError,
            ValueError,
        ) as exc:
            _log.debug("openalex_direct_lookup_failed", url=url, error=str(exc))
            return {}
    return {}


def enrich_by_doi(doi: str) -> dict[str, Any]:
    """Look up a paper by DOI directly. ``doi`` is the bare form
    (``10.1145/X.Y``); the URL-prefixed form is also accepted.

    Unambiguous: OpenAlex resolves DOIs without fuzzy matching, so
    ``mfaz`` no longer becomes a 1996 Developmental Brain Research
    paper. Returns ``{}`` on miss / network error / malformed payload.
    """
    if not doi:
        return {}
    bare = _strip_doi_prefix(doi)
    return _direct_lookup(f"https://api.openalex.org/works/doi:{bare}")


def enrich_by_arxiv_id(arxiv_id: str) -> dict[str, Any]:
    """Look up an arXiv paper directly. ``arxiv_id`` is the bare
    form (``2503.07641``, no version suffix).

    OpenAlex does NOT support an ``arxiv:`` external-ID lookup
    natively. The convention (via Crossref) is to use arXiv's own
    DOI namespace: ``10.48550/arXiv.<id>``. We construct that DOI
    and reuse the by-DOI endpoint. Unambiguous when the paper is
    in OpenAlex; returns ``{}`` on 404 (paper not indexed).
    """
    if not arxiv_id:
        return {}
    # arXiv-DOI form: registered with Crossref since 2022 for all
    # arXiv submissions. Older papers may not be retroactively
    # registered, in which case OpenAlex 404s and we fall through
    # to title search.
    arxiv_doi = f"10.48550/arXiv.{arxiv_id}"
    return _direct_lookup(f"https://api.openalex.org/works/doi:{arxiv_doi}")


def enrich(title: str) -> dict[str, Any]:
    """Query OpenAlex for a paper matching ``title``.

    Returns a dict with keys: ``year``, ``venue``, ``authors``,
    ``citation_count``, ``openalex_id``, ``doi``, ``references``.
    Returns ``{}`` on any failure (timeout, HTTP error, network
    error, empty result, or malformed payload).

    Retries up to 3 times on HTTP 429 (rate-limit) with the same
    5s/10s/20s backoff schedule as the Semantic Scholar enricher.
    """
    import time

    params = _params(title)
    for attempt in range(_MAX_RETRIES + 1):
        try:
            resp = httpx.get(_BASE_URL, params=params, timeout=_TIMEOUT)
            if resp.status_code == 429 and attempt < _MAX_RETRIES:
                wait = _BACKOFF_BASE * (2 ** attempt)
                _log.debug(
                    "openalex_rate_limited", title=title, retry_in=wait,
                )
                time.sleep(wait)
                continue
            resp.raise_for_status()
            data = resp.json().get("results") or []
            if not data:
                return {}
            work = data[0]

            authorships = work.get("authorships") or []
            authors = ", ".join(
                (a.get("author") or {}).get("display_name", "")
                for a in authorships[:5]
            )

            primary = work.get("primary_location") or {}
            source = (primary.get("source") or {}) if isinstance(primary, dict) else {}
            venue = source.get("display_name", "") if isinstance(source, dict) else ""

            referenced = work.get("referenced_works") or []
            refs = [_strip_openalex_prefix(r) for r in referenced if r]

            return {
                "year": work.get("publication_year", 0) or 0,
                "venue": venue or "",
                "authors": authors or "",
                "citation_count": work.get("cited_by_count", 0) or 0,
                "openalex_id": _strip_openalex_prefix(work.get("id", "")) or "",
                "doi": _strip_doi_prefix(work.get("doi", "") or ""),
                "references": refs,
            }
        except (
            httpx.HTTPError,
            httpx.TimeoutException,
            httpx.ConnectError,
            ValueError,
        ) as exc:
            _log.debug("openalex_lookup_failed", title=title, error=str(exc))
            return {}
    return {}
