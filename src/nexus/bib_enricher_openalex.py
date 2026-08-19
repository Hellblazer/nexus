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
        "per-page": str(_TITLE_SEARCH_CANDIDATES),
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
    canonical bib_enricher result dict.

    The transient ``_lookup_title`` field carries the OpenAlex
    ``display_name`` so callers (or :func:`_direct_lookup`) can
    validate identity post-lookup. It is stripped before storage.
    """
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
        "_lookup_title": work.get("display_name", "") or "",
    }


# nexus-yy1m: title-validation post-lookup. The DOI/arXiv-aware lookup
# (PR #394 / #395 / #396) trusts whatever identifier shows up in the
# document body, but academic papers have entire reference lists with
# DOIs that belong to OTHER papers. v4.21.0 shakeout caught this: a
# CacheRAG preprint without its own DOI got a citation DOI extracted
# and stamped with the citation's metadata. The validator below is the
# fallback gate: if the OpenAlex-returned title shares too few
# substantive tokens with the source title, reject the result so the
# caller falls through to fuzzy title search.

# Stopwords kept short. Punctuation strip + lowercase + length>=4 token
# filter does most of the work, and over-aggressive stopwording just
# undershoots the "are these the same paper" question.
_TITLE_STOPWORDS: frozenset[str] = frozenset({
    "with", "from", "into", "this", "that", "these", "those",
    "their", "them", "they", "your", "yours", "ours", "have",
    "been", "being", "such", "what", "when", "where", "which",
    "while", "without", "within", "about", "above", "after",
    "before", "between", "during", "through", "based", "over",
    "under",
})

# Title-validation policy (4.21.2 refinement after v4.21.1 shakeout).
# Pure Jaccard overpenalised short source titles ("Pbeegees" vs the
# matching "pBeeGees: A Prudent Approach to Certificate-Decoupled BFT
# Consensus" gave 1/6 = 0.167, rejecting a true positive). The
# asymmetric rule below preserves the citation-poisoning rejection
# (zero token overlap) while accepting the short-source case where one
# title is genuinely just the matching tokens:
#
#   * intersection >= 2 substantive tokens: accept. Multi-token coincidence
#     between unrelated papers is rare enough that this is a safe ceiling.
#   * intersection == 1: accept only when the smaller token set has
#     <= MAX_SHORT_SET_SIZE tokens (i.e., one side is essentially the
#     intersection). Catches "Pbeegees" / "Hex Bloom" cases. Rejects
#     coincidental single-token overlap in long titles (e.g.
#     "Bloom Filter Survey" vs "Bloom Effects in Computer Graphics").
#   * intersection == 0: reject. Disjoint vocabularies almost certainly
#     mean different papers.
_TITLE_MIN_INTERSECTION_FOR_AUTO_ACCEPT: int = 2
# 2026-08-19 (nexus-ov5tc critique): was 2 — a 2-token source sharing ONE
# token with a long stranger ("Attention Is All You Need" -> {attention,
# need} vs "Attention Mechanisms in Convolutional Vision Models") accepted.
# The relaxation exists for a source that IS the single rare token
# ("Pbeegees", "Hex Bloom"); it is now limited to exactly that shape.
_TITLE_MAX_SHORT_SET_SIZE: int = 1

# 2026-08-19 (knowledge__semantic-operators mis-attribution): a raw ">= 2
# shared tokens" accept is the wrong shape for the two places it guards.
# A DOI scraped from a paper's REFERENCE LIST resolves to a work in the
# same subfield by construction, so it shares vocabulary ("Rethinking Query
# Optimization for Multi-Agent Systems" vs the cited "LIMAO: ... Learned
# Query Optimization" — 2 tokens, accepted, wrong authors stamped on 76
# chunks + the catalog row). OpenAlex title search likewise ranks a
# vocabulary-sharing stranger first when the real paper is #2 ("Edge
# Intelligence: Paving the Last Mile ..." for the NL2Pipe title). The
# multi-token overlap must also be the BULK of the shorter title: the
# intersection divided by the smaller token set must reach this ratio.
# Genuine variants (subtitle added/dropped, punctuation drift, filename-
# truncated source) keep containment near 1.0; same-subfield strangers sit
# at 0.2-0.35. The single-token short-source relaxation below is unchanged.
_TITLE_MIN_CONTAINMENT: float = 0.5

# A 2-token shorter side is trivially "contained" (2/2 = 1.0) in ANY longer
# title that carries both words — "Query Optimization" vs "LIMAO: ... Learned
# Query Optimization" (nexus-ov5tc critique, C2). When the shorter side has
# exactly _TITLE_MIN_INTERSECTION_FOR_AUTO_ACCEPT tokens, the longer side may
# be at most this many times longer (+1) or the overlap is treated as the
# generic-vocabulary coincidence it usually is. Genuine short-title variants
# ("Deep Learning" vs "Deep Learning: A New Approach", 2 vs 3) still accept.
_TITLE_SHORT_SIDE_MAX_EXPANSION: int = 2

# OpenAlex fuzzy title search: fetch this many ranked candidates and stamp
# the FIRST one that passes :func:`_titles_compatible`, instead of trusting
# ``results[0]`` (2026-08-19: the real paper was ranked #2 behind a stranger).
_TITLE_SEARCH_CANDIDATES: int = 5

# nexus-5cez: denylist of common single-word title coincidences.
# When the source title's only substantive token is one of these
# (e.g. "Survey", "Methods", "Notes"), the short-source relaxation
# at len(intersection) == 1 must NOT auto-accept -- the overlap is
# almost certainly coincidental, since these words appear in the
# titles of thousands of unrelated papers. Rare invented words
# ("Pbeegees", "Hex Bloom") are NOT in this list, so the legitimate
# filename-derived short-title case still accepts. Tokens are stored
# lowercase to match the tokenizer output (cleaned + lowercased).
_TITLE_COMMON_SINGLEWORD_DENYLIST: frozenset[str] = frozenset({
    "survey", "methods", "notes", "model", "system", "paper",
    "results", "abstract", "review", "study", "report", "data",
    "analysis", "framework", "approach", "introduction", "summary",
    "evaluation", "experiment", "experiments", "discussion",
    "conclusion", "conclusions", "background", "related", "work",
    "research", "design", "implementation", "comparison", "demo",
})


def _tokenize_title(title: str) -> frozenset[str]:
    """Lowercase, strip non-alphanumerics, drop short / stopword tokens.

    Returns a frozenset of substantive tokens. Caller passes two such
    sets to :func:`_titles_compatible`.
    """
    if not title:
        return frozenset()
    cleaned = "".join(c if c.isalnum() else " " for c in title.lower())
    tokens = (t for t in cleaned.split() if len(t) >= 4)
    return frozenset(t for t in tokens if t not in _TITLE_STOPWORDS)


def _titles_compatible(source: str, returned: str) -> bool:
    """Return True when *source* and *returned* are plausibly the same paper.

    Used to gate identifier-based lookups (DOI / arXiv) so a citation
    DOI extracted from the references section cannot stamp a foreign
    paper's metadata. Empty inputs are treated as incompatible (caller
    should fall through, not stamp empty bib).

    The rule (4.21.2, tightened 2026-08-19): two-or-more substantive token
    matches accept only when the intersection is also at least
    ``_TITLE_MIN_CONTAINMENT`` of the SHORTER title's token set (same-
    subfield strangers — a reference-list DOI, OpenAlex's rank-1 search
    hit — share 2 tokens out of 7 and are rejected; genuine variants sit
    near 1.0); a single-token match accepts only when one side is short
    enough that the match is the bulk of the title (catches
    filename-derived short source titles like "Pbeegees" matching the
    full OpenAlex title "pBeeGees: A Prudent Approach to ...");
    zero matches always reject.
    """
    a = _tokenize_title(source)
    b = _tokenize_title(returned)
    if not a or not b:
        return False
    intersection = a & b
    if len(intersection) >= _TITLE_MIN_INTERSECTION_FOR_AUTO_ACCEPT:
        short, long_ = min(len(a), len(b)), max(len(a), len(b))
        # Containment on the shorter side (see _TITLE_MIN_CONTAINMENT).
        if len(intersection) / short < _TITLE_MIN_CONTAINMENT:
            return False
        # Generic 2-token shorter side vs a much longer title (see
        # _TITLE_SHORT_SIDE_MAX_EXPANSION).
        if short <= _TITLE_MIN_INTERSECTION_FOR_AUTO_ACCEPT:
            return long_ <= short * _TITLE_SHORT_SIDE_MAX_EXPANSION + 1
        return True
    if len(intersection) == 1:
        # nexus-5cez: when the source title is just ONE substantive
        # token AND that token is a common research-paper-vocabulary
        # word ("Survey", "Methods", "Notes"), the 1-overlap case is
        # almost certainly a coincidence -- thousands of unrelated
        # papers contain these words, and the matched-bib stamp would
        # be wrong. Reject so the caller falls through to fuzzy
        # search, which uses OpenAlex's relevance score rather than
        # set overlap. Rare invented short titles like "Pbeegees" do
        # NOT trigger this guard (token not in the denylist), so the
        # filename-derived legitimate case still accepts under the
        # original short-set relaxation below.
        only = next(iter(intersection))
        if (
            len(a) == 1
            and only in _TITLE_COMMON_SINGLEWORD_DENYLIST
        ):
            return False
        return min(len(a), len(b)) <= _TITLE_MAX_SHORT_SET_SIZE
    return False


def _direct_lookup(url: str, *, expected_title: str = "") -> dict[str, Any]:
    """Direct ``/works/<id>`` GET. Returns the canonical bib dict on
    success, ``{}`` on failure (404, network error, malformed payload).
    Same retry shape as :func:`enrich` for 429s.

    When ``expected_title`` is non-empty, the OpenAlex-returned title
    is validated against it via :func:`_titles_compatible`. A
    low-similarity match returns ``{}`` and logs the rejection so
    the caller can fall through (nexus-yy1m citation-DOI guard).
    The transient ``_lookup_title`` field is stripped from the
    returned dict regardless.
    """
    import time  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance

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
            result = _build_result(resp.json())
            lookup_title = result.pop("_lookup_title", "")
            if expected_title and not _titles_compatible(expected_title, lookup_title):
                _log.warning(
                    "openalex_title_mismatch_rejected",
                    url=url,
                    expected_title=expected_title,
                    returned_title=lookup_title,
                    rejected_openalex_id=result.get("openalex_id", ""),
                )
                return {}
            return result
        except (
            httpx.HTTPError,
            httpx.TimeoutException,
            httpx.ConnectError,
            ValueError,
        ) as exc:
            _log.debug("openalex_direct_lookup_failed", url=url, error=str(exc))
            return {}
    return {}


def enrich_by_doi(doi: str, *, expected_title: str = "") -> dict[str, Any]:
    """Look up a paper by DOI directly. ``doi`` is the bare form
    (``10.1145/X.Y``); the URL-prefixed form is also accepted.

    When ``expected_title`` is non-empty (nexus-yy1m), the returned
    OpenAlex title is validated against it via :func:`_titles_compatible`.
    A low-similarity match returns ``{}`` so the caller can fall
    through to fuzzy title search instead of stamping the wrong paper.
    The validator is what catches the "citation-DOI poisoning" case
    where a DOI extracted from a paper's references section resolves
    a foreign paper.

    Returns ``{}`` on miss / network error / malformed payload, or on
    title-validation rejection.
    """
    if not doi:
        return {}
    bare = _strip_doi_prefix(doi)
    return _direct_lookup(
        f"https://api.openalex.org/works/doi:{bare}",
        expected_title=expected_title,
    )


def enrich_by_arxiv_id(arxiv_id: str, *, expected_title: str = "") -> dict[str, Any]:
    """Look up an arXiv paper directly. ``arxiv_id`` is the bare
    form (``2503.07641``, no version suffix).

    OpenAlex does NOT support an ``arxiv:`` external-ID lookup
    natively. The convention (via Crossref) is to use arXiv's own
    DOI namespace: ``10.48550/arXiv.<id>``. We construct that DOI
    and reuse the by-DOI endpoint. Unambiguous when the paper is
    in OpenAlex; returns ``{}`` on 404 (paper not indexed).

    When ``expected_title`` is non-empty (nexus-yy1m), the returned
    OpenAlex title is validated; see :func:`enrich_by_doi`.
    """
    if not arxiv_id:
        return {}
    # arXiv-DOI form: registered with Crossref since 2022 for all
    # arXiv submissions. Older papers may not be retroactively
    # registered, in which case OpenAlex 404s and we fall through
    # to title search.
    arxiv_doi = f"10.48550/arXiv.{arxiv_id}"
    return _direct_lookup(
        f"https://api.openalex.org/works/doi:{arxiv_doi}",
        expected_title=expected_title,
    )


def enrich(title: str) -> dict[str, Any]:
    """Query OpenAlex for a paper matching ``title``.

    Returns a dict with keys: ``year``, ``venue``, ``authors``,
    ``citation_count``, ``openalex_id``, ``doi``, ``references``.
    Returns ``{}`` on any failure (timeout, HTTP error, network
    error, empty result, or malformed payload).

    Retries up to 3 times on HTTP 429 (rate-limit) with the same
    5s/10s/20s backoff schedule as the Semantic Scholar enricher.
    """
    import time  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance

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
            # nexus-yy1m: OpenAlex title search returns SOMETHING for
            # almost every query, ranked by its relevance score. When
            # the real paper isn't indexed (preprint, not yet accepted),
            # the first result is whatever happens to share some tokens
            # with the query, frequently a completely unrelated paper.
            # Apply the same title-validation guard as the by-id paths
            # to refuse wildly-mismatched fuzzy matches. 2026-08-19: walk
            # the top _TITLE_SEARCH_CANDIDATES in rank order and stamp the
            # first compatible one — the genuine paper is routinely ranked
            # behind a vocabulary-sharing stranger.
            first_rejected: dict[str, Any] | None = None
            for work in data[:_TITLE_SEARCH_CANDIDATES]:
                result = _build_result(work)
                lookup_title = result.pop("_lookup_title", "")
                if _titles_compatible(title, lookup_title):
                    return result
                if first_rejected is None:
                    first_rejected = {
                        "returned_title": lookup_title,
                        "rejected_openalex_id": result.get("openalex_id", ""),
                    }
            _log.warning(
                "openalex_title_search_rejected",
                query_title=title,
                candidates=len(data[:_TITLE_SEARCH_CANDIDATES]),
                **(first_rejected or {}),
            )
            return {}
        except (
            httpx.HTTPError,
            httpx.TimeoutException,
            httpx.ConnectError,
            ValueError,
        ) as exc:
            _log.debug("openalex_lookup_failed", title=title, error=str(exc))
            return {}
    return {}
