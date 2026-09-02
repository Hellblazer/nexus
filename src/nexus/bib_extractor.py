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
# ``%`` is in the class deliberately (nexus-kho0p): it is not valid in a
# clean DOI, but including it lets a percent-encoded filename match FULLY
# so validation can reject the whole thing. Without it the match would
# stop at the first ``%`` and yield a plausible-looking truncation.
# Trailing punctuation (.,);] is stripped post-match because body
# text often has 'see (10.1145/X.Y).' patterns where the closing
# punctuation isn't part of the DOI.
_DOI_RE = re.compile(
    r"\b(10\.\d{4,9}/[-._;()/:%a-z0-9]+)",
    re.IGNORECASE,
)

# nexus-liir: labeled-DOI preference. Papers print their canonical
# identifier in a page-1 banner with an explicit ``DOI:`` /
# ``doi:`` / ``https://doi.org/`` label. Reference-section DOIs
# usually appear bare in numbered citation lists. Capturing the
# label-prefixed form first eliminates the "first DOI in chunk
# order is a reference, not the paper" false-positive class
# (knowledge__delos: aleph-bft, pBeeGees, lightweight-smr all
# matched citation DOIs from their references before the bare-DOI
# fallback would have reached the paper's own banner DOI).
#
# nexus-kho0p CORRECTS the "usually appear bare" assumption above. It is
# false for preprints, whose bibliographies routinely carry explicit
# ``doi:`` labels — so on a paper with no DOI of its own, the labeled
# preference reached INTO the references and returned a cited paper's
# DOI. Measured: Meta-Harness (arXiv 2603.28052) yielded 10.1145/3591300,
# its own reference [8]. The preference is still right; it is now bounded
# to the region above the bibliography (see _text_above_references), so
# it can only ever prefer a banner over body text, never over a citation.
_DOI_LABELED_RE = re.compile(
    r"(?:doi[:\s]+|https?://(?:dx\.)?doi\.org/)"
    r"(10\.\d{4,9}/[-._;()/:%a-z0-9]+)",
    re.IGNORECASE,
)
_DOI_TRAILING_PUNCT = re.compile(r"[.,;:)\]]+$")

# nexus-kho0p. A bibliography heading ends the region in which the
# paper's own identifier can appear. Matches markdown headings, numbered
# section headings, and bare all-caps forms.
_REFERENCES_HEADING_RE = re.compile(
    r"^[ \t]*(?:#{1,6}[ \t]*)?(?:\d+(?:\.\d+)*\.?[ \t]*)?"
    r"(?:references|bibliography|works\s+cited|literature\s+cited)\b",
    re.IGNORECASE | re.MULTILINE,
)

# nexus-kho0p. The load-bearing discriminator. Position alone cannot
# separate "the paper's own banner" from "a cited paper's DOI": Docling's
# chunk order can place the references section ABOVE the page-1 banner
# (that reordering is the whole reason nexus-liir exists, and its
# regression test encodes it). What DOES separate them is that a
# reference DOI sits inside a numbered bibliography entry, while a banner
# DOI sits on a line of its own.
_CITATION_LINE_RE = re.compile(
    r"^[ \t]*\[\d+\]"          # [8] Beurer-Kellner et al. ...
    r"|^[ \t]*\d{1,3}\.[ \t]+\S"  # 8. Beurer-Kellner et al. ...
    r"|et\s+al\.",              # ... anywhere on the line
    re.IGNORECASE,
)

# Trailing URL path segments that publishers append to a DOI link.
# Observed: 10.3389/fpsyg.2014.01053/pdf.
_DOI_URL_TAIL_RE = re.compile(
    r"/(?:pdf|full|abstract|epdf|html|meta)$",
    re.IGNORECASE,
)

# A document filename that reached the DOI field. Observed: a DOI whose
# value was an entire percent-encoded filename ending "Anna's Archive.pdf".
_DOI_FILE_EXT_RE = re.compile(
    r"\.(?:pdf|html?|xml|txt|docx?|epub)$",
    re.IGNORECASE,
)

_DOI_SHAPE_RE = re.compile(r"10\.\d{4,9}/\S+", re.IGNORECASE)

# Unfilled LaTeX template markers. Matched per dot/slash/underscore-
# delimited component so a component must be ENTIRELY placeholder to
# count: 'arXiv' contains an X but is not one, and 10.1109/X.Y is a
# legitimate short suffix. Thresholds are set so the observed templates
# (acmart nnnnnnn, LIPIcs XX, IEEE XXX.0000.0000000, Optica ao.XX.XXXXXX)
# are caught without touching single-character components.
_PLACEHOLDER_COMPONENT_RE = re.compile(
    r"^(?:n{4,}|x{2,}|0{4,})$",
    re.IGNORECASE,
)
_DOI_COMPONENT_SPLIT_RE = re.compile(r"[./_-]")


def _text_above_references(text: str) -> str:
    """Return the slice of ``text`` above the first bibliography heading.

    nexus-kho0p. Returns the whole text when no heading is found — with
    nothing to bound against, behaviour is unchanged.

    Bounding at the FIRST heading is deliberate: appendices commonly follow
    the bibliography and some templates emit "References" more than once.

    This region is a PREFERENCE, not a hard cut. Extracted text is not
    reliably in reading order -- Docling can emit the references above the
    page-1 banner -- so excluding everything below the heading would lose
    real DOIs (see TestDoiLabelPreference). Citation membership, not
    position, is what actually rejects a reference DOI; see
    _CITATION_LINE_RE and extract_doi's pass order.
    """
    match = _REFERENCES_HEADING_RE.search(text)
    return text[: match.start()] if match else text


def _trim_unbalanced_parens(doi: str) -> str:
    """Drop a dangling '(' group. Observed: 10.1016/S0004-3702(01."""
    while doi.count("(") > doi.count(")"):
        cut = doi.rfind("(")
        if cut <= 0:
            break
        doi = doi[:cut]
    return doi


def _is_placeholder_doi(doi: str) -> bool:
    suffix = doi.split("/", 1)[1] if "/" in doi else ""
    return any(
        _PLACEHOLDER_COMPONENT_RE.match(part)
        for part in _DOI_COMPONENT_SPLIT_RE.split(suffix)
        if part
    )


def _normalize_doi(raw: str) -> str | None:
    """Clean a raw regex match, or return None if it is not a real DOI."""
    doi = _DOI_TRAILING_PUNCT.sub("", raw)
    doi = _DOI_URL_TAIL_RE.sub("", doi)
    doi = _trim_unbalanced_parens(doi)
    doi = _DOI_TRAILING_PUNCT.sub("", doi)
    if not doi or "%" in doi:
        return None
    if _DOI_FILE_EXT_RE.search(doi):
        return None
    if _is_placeholder_doi(doi):
        return None
    if not _DOI_SHAPE_RE.fullmatch(doi):
        return None
    return doi


def _line_containing(text: str, index: int) -> str:
    """Return the whole line of ``text`` that contains offset ``index``."""
    start = text.rfind("\n", 0, index) + 1
    end = text.find("\n", index)
    return text[start:] if end == -1 else text[start:end]


def _first_valid_doi(region: str, pattern: re.Pattern[str]) -> str | None:
    """First match of ``pattern`` in ``region`` that is neither inside a
    bibliography entry nor a placeholder/malformed string."""
    for match in pattern.finditer(region):
        if _CITATION_LINE_RE.search(_line_containing(region, match.start())):
            continue
        doi = _normalize_doi(match.group(1))
        if doi:
            return doi
    return None


def extract_doi(text: str) -> str | None:
    """Return the paper's own DOI from ``text``, or None.

    Two independent guards, because neither alone is sufficient (both
    failure modes are measured, not hypothetical):

    * **Position.** A DOI below the bibliography heading is a citation.
      Necessary because a reference entry wraps across lines — the DOI
      commonly lands on a continuation line carrying no ``[N]`` marker
      (measured on arXiv 2603.28052: "Languages, 7(PLDI):1946-1969, June
      2023. ISSN 2475-1421. doi: 10.1145/3591300. URL").
    * **Citation membership.** ``_CITATION_LINE_RE``. Necessary because
      many papers print no bibliography heading at all, so there is no
      position to reason about.

    The full-text retry exists only for the nexus-liir case, where chunk
    order placed the references ABOVE the page-1 banner. It is gated on the
    above-heading region being *empty*: that emptiness is the evidence of
    reordering. Running it unconditionally is precisely the nexus-kho0p
    defect, since in a normally-ordered document everything below the
    heading is a citation or an appendix — neither of which is the paper's
    own identifier.

    Returning None is a valid, useful answer. For a preprint the caller
    falls back to the arXiv ID (see extract_identifiers), so a missing DOI
    degrades to a correct identifier, whereas a reference DOI is a
    confidently wrong one that resolves to somebody else's paper.
    """
    if not text:
        return None
    region = _text_above_references(text)
    found = (
        _first_valid_doi(region, _DOI_LABELED_RE)
        or _first_valid_doi(region, _DOI_RE)
    )
    if found:
        return found
    if region.strip():
        # Normal document order and no DOI above the bibliography: this
        # paper printed none of its own. Do NOT reach past the heading.
        return None
    # Region empty => the heading opened the text (chunk reordering).
    return (
        _first_valid_doi(text, _DOI_LABELED_RE)
        or _first_valid_doi(text, _DOI_RE)
    )


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
