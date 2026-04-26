# SPDX-License-Identifier: AGPL-3.0-or-later
"""PDF text chunker with sentence-boundary awareness, page tracking, and
section-type classification.

Section detection rationale (RDR-089 follow-up): MinerU and Docling
emit markdown with ``#``-prefixed headings; PyMuPDF emits raw text but
academic papers carry ``1 Introduction`` / ``2.3 Method`` numbered
headings consistently. Both styles are detected here so every chunk
gets ``section_type`` (one of ``abstract`` / ``introduction`` /
``methods`` / ``results`` / ``discussion`` / ``conclusion`` /
``references`` / ``acknowledgements`` / ``appendix`` / ``other`` /
``""``) and ``section_title`` (the raw heading text) tagged at index
time. Downstream consumers (aspect_extractor, search filters) read
section_type to scope retrieval to relevant sections without re-reading
the source PDF.
"""
import re
from bisect import bisect_right
from dataclasses import dataclass

import structlog

from nexus.db.chroma_quotas import SAFE_CHUNK_BYTES
from nexus.md_chunker import classify_section_type

_log = structlog.get_logger()

_DEFAULT_CHUNK_CHARS = 1500   # ~450 tokens at 3.3 chars/token
_DEFAULT_OVERLAP = 0.20

# Heading detectors. Order matters: markdown wins because MinerU/Docling
# already emit clean ``# Heading`` lines; numbered academic headings are
# the PyMuPDF fallback.
_MARKDOWN_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)
# Numbered academic heading: optional leading whitespace, 1-3 dotted
# numerals, single space, capitalised word, up to ~80 chars total. The
# anchor on a capital letter and the length cap keep this from matching
# in-line numbered references like "1. Smith et al.".
_NUMBERED_HEADING_RE = re.compile(
    r"^[ \t]*((?:\d+\.){0,3}\d+)\s+([A-Z][\w\-\s,&/():]{2,78})\s*$",
    re.MULTILINE,
)
# Bare-word section heading (e.g. "Abstract", "References") on its own
# line. Restricted to a small whitelist to avoid catching paragraphs
# whose first word happens to be capitalised.
_BARE_HEADING_RE = re.compile(
    r"^[ \t]*(Abstract|References|Acknowledgements?|Acknowledgments?|Appendix(?:\s+[A-Z])?)\s*$",
    re.MULTILINE,
)


@dataclass
class TextChunk:
    """A text chunk with its index and metadata."""

    text: str
    chunk_index: int
    metadata: dict


def _heading_number_prefix(text: str) -> str:
    """Return the leading dotted-numeral prefix of a heading (``"3.1.2"``
    for ``"3.1.2 Subsection"``), or ``""`` when no numeric prefix is
    present (e.g. ``"Abstract"``)."""
    m = re.match(r"^\s*((?:\d+\.){0,3}\d+)(?:\s|$)", text)
    return m.group(1) if m else ""


def _extract_headings(text: str) -> list[tuple[int, str]]:
    """Return list of ``(char_offset, clean_heading_text)`` tuples,
    sorted ascending by offset.

    Detects three heading styles:

    * Markdown ``# Heading`` (MinerU / Docling output)
    * Numbered academic ``1.2 Method`` (PyMuPDF on academic papers)
    * Bare-word section labels (``Abstract``, ``References``, ...)

    Heading text is returned verbatim with leading numeric prefix
    preserved — :func:`nexus.md_chunker.classify_section_type` already
    tolerates the optional number, and downstream consumers (display,
    citation) want the original text.
    """
    found: list[tuple[int, str]] = []
    for m in _MARKDOWN_HEADING_RE.finditer(text):
        found.append((m.start(), m.group(2).strip()))
    for m in _NUMBERED_HEADING_RE.finditer(text):
        found.append((m.start(), f"{m.group(1)} {m.group(2).strip()}"))
    for m in _BARE_HEADING_RE.finditer(text):
        found.append((m.start(), m.group(1).strip()))
    found.sort(key=lambda h: h[0])
    return found


def _ancestor_chain(
    idx: int,
    headings: list[tuple[int, str]],
) -> list[str]:
    """Return the dotted-numeral ancestor chain for the heading at
    ``idx``, top-down, including the heading itself.

    For ``3.1.2 Deep`` with prior ``3 METHODOLOGY`` and ``3.1 Approach``
    on the page, returns ``["3 METHODOLOGY", "3.1 Approach", "3.1.2 Deep"]``.

    Headings without a numeric prefix (``Abstract``, ``References``,
    bare markdown ``# Heading``) have no parent chain — return ``[heading]``.
    """
    leaf_title = headings[idx][1]
    leaf_prefix = _heading_number_prefix(leaf_title)
    if not leaf_prefix:
        return [leaf_title]
    leaf_depth = len(leaf_prefix.split("."))
    # Find the nearest ancestor at each shallower depth by walking back.
    ancestors_by_depth: dict[int, str] = {leaf_depth: leaf_title}
    for j in range(idx - 1, -1, -1):
        anc_title = headings[j][1]
        anc_prefix = _heading_number_prefix(anc_title)
        if not anc_prefix:
            continue
        anc_depth = len(anc_prefix.split("."))
        if anc_depth >= leaf_depth:
            continue
        if anc_depth in ancestors_by_depth:
            continue
        # Strict prefix check: "3" is parent of "3.1" / "3.1.2"
        if leaf_prefix.startswith(anc_prefix + "."):
            ancestors_by_depth[anc_depth] = anc_title
        if len(ancestors_by_depth) == leaf_depth:
            break
    return [ancestors_by_depth[d] for d in sorted(ancestors_by_depth)]


def _classify_with_hierarchy(
    idx: int,
    headings: list[tuple[int, str]],
) -> str:
    """Classify section_type for the heading at ``idx``, inheriting
    from the top-level ancestor when the leaf heading itself doesn't
    match a known pattern.

    Subsections like ``3.1 Chunked Attention`` should inherit
    ``methods`` from their parent ``3 METHODOLOGY`` rather than
    falling to ``other``. Walks the ancestor chain (see
    :func:`_ancestor_chain`) from leaf to root, returning the first
    classification that matches a top-level pattern.
    """
    chain = _ancestor_chain(idx, headings)
    # Walk leaf → root (chain is top-down); return first matching type.
    for title in reversed(chain):
        t = classify_section_type([title])
        if t and t != "other":
            return t
    return classify_section_type([chain[-1]]) if chain else ""


class PDFChunker:
    """Split extracted PDF text into overlapping chunks at sentence boundaries.

    Each chunk is tagged with ``section_type`` and ``section_title``
    (derived from the most recent heading at or before the chunk's start
    offset), in addition to position and page metadata. See
    :func:`_extract_headings` for heading detection details.
    """

    def __init__(
        self,
        chunk_chars: int = _DEFAULT_CHUNK_CHARS,
        overlap_percent: float = _DEFAULT_OVERLAP,
    ) -> None:
        self.chunk_chars = chunk_chars
        self.overlap_chars = max(1, int(chunk_chars * overlap_percent))

    def chunk(self, text: str, extraction_metadata: dict) -> list[TextChunk]:
        """Split *text* into chunks.

        *extraction_metadata* is used to extract page boundaries for assigning
        page numbers to each chunk; it is not forwarded wholesale into chunk metadata.

        Chunks are tagged with ``chunk_type``: ``"table_page"`` when the chunk's
        page appears in ``extraction_metadata["table_regions"]``, else ``"text"``.
        This is page-level granularity — all chunks on a table page are tagged
        regardless of their exact content. The ``table_page`` name makes the
        coarseness explicit (it means "page containing a table", not "this
        chunk is table data").

        Section tagging: each chunk inherits the section identified by the
        most recent heading at or before the chunk's start offset. Chunks
        before any heading get ``section_type=""`` and ``section_title=""``.
        """
        page_boundaries = extraction_metadata.get("page_boundaries", [])
        table_regions = extraction_metadata.get("table_regions", [])
        table_pages: set[int] = {r["page"] for r in table_regions} if table_regions else set()
        headings = _extract_headings(text)
        heading_offsets = [h[0] for h in headings]
        chunks: list[TextChunk] = []
        start = 0
        chunk_index = 0

        while start < len(text):
            end = min(start + self.chunk_chars, len(text))

            # Prefer to break at a sentence boundary in the last 20% of the window
            if end < len(text):
                search_start = end - max(1, int(self.chunk_chars * 0.2))
                sentence_end = text.rfind(". ", search_start, end)
                if sentence_end != -1:
                    end = sentence_end + 1  # include the period

            chunk_text = text[start:end].strip()
            if chunk_text:
                page_number = self._page_for(start, page_boundaries)
                chunk_type = "table_page" if page_number in table_pages else "text"
                section_title, section_type = self._section_for(
                    start, headings, heading_offsets,
                )
                chunks.append(
                    TextChunk(
                        text=chunk_text,
                        chunk_index=chunk_index,
                        metadata={
                            "chunk_index": chunk_index,
                            "chunk_start_char": start,
                            "chunk_end_char": end,
                            "page_number": page_number,
                            "chunk_type": chunk_type,
                            "section_title": section_title,
                            "section_type": section_type,
                        },
                    )
                )
                chunk_index += 1

            next_start = end - self.overlap_chars
            if next_start <= start:
                break
            start = next_start

        # Byte cap post-pass: truncate any chunk that exceeds the storage limit.
        for c in chunks:
            if len(c.text.encode()) > SAFE_CHUNK_BYTES:
                c.text = c.text.encode()[:SAFE_CHUNK_BYTES].decode("utf-8", errors="ignore")
        return chunks

    def _page_for(self, char_pos: int, page_boundaries: list[dict]) -> int:
        """Return the 1-indexed page number that contains *char_pos*, or 0."""
        for page in page_boundaries:
            page_start = page["start_char"]
            page_end = page_start + page["page_text_length"]
            if page_start <= char_pos < page_end:
                return page["page_number"]
        # If we reach here, chunk_start is past all page boundaries — unexpected
        last = page_boundaries[-1] if page_boundaries else None
        _log.debug(
            "chunk_start past all page boundaries, using last page",
            chunk_start=char_pos,
            last_page_number=last["page_number"] if last else None,
        )
        return page_boundaries[-1]["page_number"] if page_boundaries else 0

    def _section_for(
        self,
        char_pos: int,
        headings: list[tuple[int, str]],
        heading_offsets: list[int],
    ) -> tuple[str, str]:
        """Return ``(section_title, section_type)`` for the heading active
        at *char_pos*, or ``("", "")`` when no heading precedes it.

        ``heading_offsets`` is the parallel list of offsets used for
        :func:`bisect.bisect_right`; the caller passes both to avoid
        rebuilding it per chunk. Section type walks the dotted-numeral
        hierarchy via :func:`_classify_with_hierarchy` so subsections
        inherit from their top-level parent.
        """
        if not headings:
            return ("", "")
        idx = bisect_right(heading_offsets, char_pos) - 1
        if idx < 0:
            return ("", "")
        chain = _ancestor_chain(idx, headings)
        # Match SemanticMarkdownChunker convention: " > "-joined path.
        section_title = " > ".join(chain) if chain else ""
        return (section_title, _classify_with_hierarchy(idx, headings))
