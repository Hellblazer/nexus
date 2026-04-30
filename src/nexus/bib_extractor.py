# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""DOI + arXiv-ID extractors for paper text (nexus-sbzr).

Filename slugs and PDF metadata titles are unreliable signals for
identifying a paper at OpenAlex / Semantic Scholar (live evidence:
``mfaz.pdf`` matched a 1996 Developmental Brain Research paper at
OpenAlex via fuzzy title search). DOIs and arXiv IDs are
authoritative — papers print them on page 1, and both backends
support direct ID lookup with no fuzzy matching.

This module pulls the canonical identifier out of body text (or the
filename, for arXiv preprints distributed as ``<id>.pdf``). The
caller in ``nx enrich bib`` tries DOI first, then arXiv ID, then
falls back to title search.

All functions are pure-Python regex; no PDF parsing, no network.
"""
from __future__ import annotations

import re
from typing import TypedDict

# DOI structure (Crossref / DataCite spec):
#   10.NNNN(N)?/<suffix>
#   - registrant: 10.<4-9 digits>
#   - separator: /
#   - suffix: any character except whitespace; in practice
#     [-._;()/:A-Z0-9]+ covers ACM, IEEE, Nature, Springer, arXiv-DOI.
# Trailing punctuation (.,);] is stripped post-match because body
# text often has 'see (10.1145/X.Y).' patterns where the closing
# punctuation isn't part of the DOI.
_DOI_RE = re.compile(
    r"\b(10\.\d{4,9}/[-._;()/:a-z0-9]+)",
    re.IGNORECASE,
)
_DOI_TRAILING_PUNCT = re.compile(r"[.,;:)\]]+$")


def extract_doi(text: str) -> str | None:
    """Return the first DOI in ``text``, or None.

    Strips trailing punctuation that body text accumulates around
    the identifier ('see (10.1145/X.Y).' yields ``10.1145/X.Y``).
    """
    if not text:
        return None
    match = _DOI_RE.search(text)
    if not match:
        return None
    doi = match.group(1)
    return _DOI_TRAILING_PUNCT.sub("", doi)


# arXiv IDs (post-April-2007): YYMM.NNNNN with optional vN suffix.
# YY = 07-99, MM = 01-12 in practice but we don't enforce — the
# 4-digit-then-dot-then-4-or-5-digit shape is distinctive enough.
# Body-text matches require either an explicit ``arXiv:`` prefix
# or filename context; bare 8-digit patterns in prose are too
# ambiguous (page numbers, dates, etc.).
_ARXIV_BODY_RE = re.compile(
    # Two disambiguating shapes:
    #   1. ``arXiv:NNNN.NNNNN`` — the canonical citation form, with or
    #      without a version suffix.
    #   2. ``NNNN.NNNNNvN`` — bare ID with a mandatory ``vN`` version
    #      suffix. The version suffix excludes random year.page patterns
    #      ('Page 2024.34567') from matching.
    r"\barxiv[: ]\s*(\d{4}\.\d{4,5})(?:v\d+)?\b"
    r"|"
    r"\b(\d{4}\.\d{4,5})v\d+\b",
    re.IGNORECASE,
)
# Filename match: <prefix>?<id>.pdf where id is the YYMM.NNNNN form.
# The optional prefix lets ``deep-artmap-2503.07641.pdf`` parse —
# many publishers prepend a slug before the arXiv id when archiving.
_ARXIV_FILENAME_RE = re.compile(
    r"(?:^|[/\-_])(\d{4}\.\d{4,5})(?:v\d+)?\.pdf$",
    re.IGNORECASE,
)


def extract_arxiv_id(text: str) -> str | None:
    """Return the first arXiv ID in ``text`` (body or filename), or None.

    Body-text matches require an explicit ``arXiv:`` prefix to avoid
    false positives on year+page-number patterns. Filename matches
    accept the bare ID form since arXiv distributes papers as
    ``<id>.pdf`` and other publishers re-archive them with prefix
    slugs (``deep-artmap-2503.07641.pdf``).
    """
    if not text:
        return None
    fn_match = _ARXIV_FILENAME_RE.search(text)
    if fn_match:
        return fn_match.group(1)
    body_match = _ARXIV_BODY_RE.search(text)
    if body_match:
        # Two alternatives in the regex; whichever matched.
        return body_match.group(1) or body_match.group(2)
    return None


class _Identifiers(TypedDict):
    doi: str | None
    arxiv: str | None


def extract_identifiers(
    *, filename: str = "", body_text: str = "",
) -> _Identifiers:
    """Combined entry point. Pulls both DOI and arXiv ID from the
    available context (body text + filename).

    The caller decides preference order: DOI is more authoritative
    (resolves both arXiv preprints and non-arXiv venues); arXiv ID
    is the fallback for arXiv-only papers without a registered DOI.
    """
    doi = extract_doi(body_text)
    arxiv = extract_arxiv_id(filename) or extract_arxiv_id(body_text)
    return {"doi": doi, "arxiv": arxiv}
