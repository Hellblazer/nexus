# SPDX-License-Identifier: AGPL-3.0-or-later
"""PDF text chunker with sentence-boundary awareness and page number tracking."""
from dataclasses import dataclass

import structlog

from nexus.db.chroma_quotas import SAFE_CHUNK_BYTES

_log = structlog.get_logger()

_DEFAULT_CHUNK_CHARS = 1500   # ~450 tokens at 3.3 chars/token
_DEFAULT_OVERLAP = 0.15


@dataclass
class TextChunk:
    """A text chunk with its index and metadata."""

    text: str
    chunk_index: int
    metadata: dict


class PDFChunker:
    """Split extracted PDF text into overlapping chunks at sentence boundaries."""

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
        """
        page_boundaries = extraction_metadata.get("page_boundaries", [])
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
                chunks.append(
                    TextChunk(
                        text=chunk_text,
                        chunk_index=chunk_index,
                        metadata={
                            "chunk_index": chunk_index,
                            "chunk_start_char": start,
                            "chunk_end_char": end,
                            "page_number": self._page_for(start, page_boundaries),
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
