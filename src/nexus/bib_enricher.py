# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Bibliographic metadata enrichment via Semantic Scholar API.

Set ``S2_API_KEY`` env var for 100 req/s (vs 100/5min unauthenticated).
Get a free key at https://www.semanticscholar.org/product/api#api-key
"""
from __future__ import annotations

import os
from typing import Any

import httpx
import structlog

_log = structlog.get_logger(__name__)
_BASE_URL = "https://api.semanticscholar.org/graph/v1/paper/search"
_FIELDS = "title,year,venue,authors,citationCount,externalIds,references.paperId"
# 2026-08-19 (nexus-ov5tc): walk the top-N relevance hits and stamp the first
# whose title is compatible with the query — never data[0] blindly.
_SEARCH_CANDIDATES = 5
_TIMEOUT = 10.0  # 10s for authenticated bulk use; 3s was too aggressive


def _s2_headers() -> dict[str, str]:
    key = os.environ.get("S2_API_KEY", "")
    return {"x-api-key": key} if key else {}


_MAX_RETRIES = 3
_BACKOFF_BASE = 5.0  # seconds; doubles on each retry


def enrich(title: str) -> dict[str, Any]:
    """Query Semantic Scholar for bibliographic metadata.

    Returns a dict with keys: year, venue, authors, citation_count,
    semantic_scholar_id — or an empty dict on any failure (timeout, HTTP
    error, network error, or no matching results).

    Retries up to 3 times with exponential backoff on 429 rate-limit.
    Set ``S2_API_KEY`` env var for higher rate limits (100 req/s).
    """
    import time  # noqa: PLC0415 — deferred import — branch-local / circular-dep avoidance

    for attempt in range(_MAX_RETRIES + 1):
        try:
            resp = httpx.get(
                _BASE_URL,
                params={"query": title, "fields": _FIELDS, "limit": _SEARCH_CANDIDATES},
                headers=_s2_headers(),
                timeout=_TIMEOUT,
            )
            if resp.status_code == 429 and attempt < _MAX_RETRIES:
                wait = _BACKOFF_BASE * (2 ** attempt)
                _log.debug("bib_enricher_rate_limited", title=title, retry_in=wait)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            data = resp.json().get("data", [])
            if not data:
                return {}
            # nexus-ov5tc (2026-08-19): this backend had NO title validation —
            # the OpenAlex backend's nexus-yy1m guard never applied here, and
            # ``auto`` picks S2 whenever S2_API_KEY is set (the recommended
            # config), so the at-index-time ``--enrich`` path and every S2 run
            # stamped whatever S2 ranked first. Same guard, same top-N walk.
            from nexus.bib_enricher_openalex import _titles_compatible  # noqa: PLC0415 — sibling backend; deferred to keep the two modules import-independent

            paper = None
            first_rejected = ""
            for cand in data[:_SEARCH_CANDIDATES]:
                cand_title = str((cand or {}).get("title") or "")
                if _titles_compatible(title, cand_title):
                    paper = cand
                    break
                if not first_rejected:
                    first_rejected = cand_title
            if paper is None:
                _log.warning(
                    "s2_title_search_rejected",
                    query_title=title,
                    candidates=len(data[:_SEARCH_CANDIDATES]),
                    returned_title=first_rejected,
                )
                return {}
            refs = [
                r.get("paperId", "") for r in (paper.get("references") or [])
                if r and r.get("paperId")
            ]
            return {
                "year": paper.get("year", 0) or 0,
                "venue": paper.get("venue", "") or "",
                "authors": ", ".join(
                    a.get("name", "") for a in (paper.get("authors") or [])[:5]
                ),
                "citation_count": paper.get("citationCount", 0) or 0,
                "semantic_scholar_id": paper.get("paperId", "") or "",
                "references": refs,
            }
        except (httpx.HTTPError, httpx.TimeoutException, httpx.ConnectError, ValueError) as exc:
            _log.debug("bib_enricher_lookup_failed", title=title, error=str(exc))
            return {}
    return {}
