# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Bibliographic metadata enrichment via Semantic Scholar public API."""
from __future__ import annotations

from typing import Any

import httpx
import structlog

_log = structlog.get_logger(__name__)
_BASE_URL = "https://api.semanticscholar.org/graph/v1/paper/search"
_FIELDS = "year,venue,authors,citationCount,externalIds,references.paperId"
_TIMEOUT = 3.0  # fast-fail for inline use during indexing


def enrich(title: str) -> dict[str, Any]:
    """Query Semantic Scholar for bibliographic metadata.

    Returns a dict with keys: year, venue, authors, citation_count,
    semantic_scholar_id — or an empty dict on any failure (timeout, HTTP
    error, network error, or no matching results).
    """
    try:
        resp = httpx.get(
            _BASE_URL,
            params={"query": title, "fields": _FIELDS, "limit": 1},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json().get("data", [])
        if not data:
            return {}
        paper = data[0]
        refs = [
            r.get("paperId", "") for r in paper.get("references", [])
            if r and r.get("paperId")
        ]
        return {
            "year": paper.get("year", 0) or 0,
            "venue": paper.get("venue", "") or "",
            "authors": ", ".join(
                a.get("name", "") for a in paper.get("authors", [])[:5]
            ),
            "citation_count": paper.get("citationCount", 0) or 0,
            "semantic_scholar_id": paper.get("paperId", "") or "",
            "references": refs,
        }
    except (httpx.HTTPError, httpx.TimeoutException, httpx.ConnectError, ValueError) as exc:
        _log.debug("bib_enricher_lookup_failed", title=title, error=str(exc))
        return {}
