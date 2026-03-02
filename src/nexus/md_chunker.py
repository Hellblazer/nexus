# SPDX-License-Identifier: AGPL-3.0-or-later
"""Semantic markdown chunking using markdown-it-py AST with naive fallback."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import structlog
import yaml

_log = structlog.get_logger()

try:
    from markdown_it import MarkdownIt
    from markdown_it.token import Token

    MARKDOWN_IT_AVAILABLE = True
except ImportError:
    MARKDOWN_IT_AVAILABLE = False
    _log.warning("markdown-it-py not available; SemanticMarkdownChunker uses naive splitting")

_CHARS_PER_TOKEN = 3.3

# Block-level open/close tokens that carry .map but no .content.
# Their .map fallback in _token_content() would duplicate the text already
# produced by the inline child tokens.  Filter them at the caller site in
# _build_sections so _token_content stays single-responsibility.
_STRUCTURAL_TOKEN_TYPES: frozenset[str] = frozenset({
    "paragraph_open", "paragraph_close",
    "bullet_list_open", "bullet_list_close",
    "ordered_list_open", "ordered_list_close",
    "list_item_open", "list_item_close",
    "blockquote_open", "blockquote_close",
    "table_open", "table_close",
    "thead_open", "thead_close",
    "tbody_open", "tbody_close",
    "tr_open", "tr_close",
    "td_open", "td_close",
    "th_open", "th_close",
})


@dataclass
class MarkdownChunk:
    """A markdown chunk with semantic metadata."""

    text: str
    chunk_index: int
    metadata: dict
    header_path: list[str]


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Extract YAML frontmatter from *text*.

    Returns ``(metadata_dict, body)`` where *body* has the frontmatter block
    stripped.  Returns ``({}, text)`` when no frontmatter is detected.
    """
    if not text.startswith("---"):
        return {}, text
    idx = text.find("\n---", 3)
    if idx == -1:
        return {}, text
    fm_content = text[3:idx].strip()
    rest = text[idx + 4:].lstrip("\r\n")
    try:
        data = yaml.safe_load(fm_content) or {}
        if not isinstance(data, dict):
            data = {}
    except yaml.YAMLError:
        data = {}
    return data, rest


class SemanticMarkdownChunker:
    """Chunk markdown preserving section structure via markdown-it-py AST.

    Falls back to naive paragraph-boundary splitting when markdown-it-py is
    unavailable.
    """

    def __init__(
        self,
        chunk_size: int = 512,
        chunk_overlap: int = 50,
    ) -> None:
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.max_chars = int(chunk_size * _CHARS_PER_TOKEN)
        self.overlap_chars = int(chunk_overlap * _CHARS_PER_TOKEN)
        self.md = MarkdownIt() if MARKDOWN_IT_AVAILABLE else None

    def chunk(self, text: str, metadata: dict) -> list[MarkdownChunk]:
        """Split *text* into MarkdownChunk objects carrying *metadata*."""
        if not text or not text.strip():
            return []
        if MARKDOWN_IT_AVAILABLE and self.md:
            try:
                return self._semantic_chunking(text, metadata)
            except Exception as exc:
                _log.warning("Semantic chunking failed (%s); falling back to naive.", exc)
        return self._naive_chunking(text, metadata)

    # ── semantic path ─────────────────────────────────────────────────────────

    def _semantic_chunking(self, text: str, metadata: dict) -> list[MarkdownChunk]:
        tokens = self.md.parse(text)
        sections = self._build_sections(tokens, text)
        return self._chunk_sections(sections, metadata)

    def _build_sections(self, tokens: list[Any], source_text: str) -> list[dict]:
        # Build a cumulative line-to-char-offset table for char position tracking.
        lines = source_text.split("\n")
        line_offsets: list[int] = [0]
        for line in lines[:-1]:
            line_offsets.append(line_offsets[-1] + len(line) + 1)  # +1 for \n

        def token_start_char(tok: Any) -> int:
            if tok.map and tok.map[0] < len(line_offsets):
                return line_offsets[tok.map[0]]
            return 0

        def token_end_char(tok: Any) -> int:
            if tok.map and tok.map[1] <= len(line_offsets):
                # map[1] is exclusive end line; char offset of that line's start
                # equals the end of the previous line's content.
                end_line = tok.map[1]
                if end_line < len(line_offsets):
                    return line_offsets[end_line]
                return len(source_text)
            return len(source_text)

        sections: list[dict] = []
        # Initialize a default level-0 section to capture pre-heading content.
        current_section: dict = {
            "level": 0,
            "header": "",
            "header_path": [],
            "content_parts": [],
            "start_char": 0,
            "end_char": len(source_text),
        }
        header_stack: list[dict] = []

        i = 0
        while i < len(tokens):
            token = tokens[i]
            if token.type == "heading_open":
                level = int(token.tag[1])
                heading_text = ""
                if i + 1 < len(tokens) and tokens[i + 1].type == "inline":
                    heading_text = tokens[i + 1].content
                # Close out the current section before starting a new one.
                # For the default level-0 section, only emit it if it has content.
                if current_section["level"] > 0 or current_section["content_parts"]:
                    current_section["end_char"] = token_start_char(token)
                    sections.append(current_section)
                header_stack = [h for h in header_stack if h["level"] < level]
                header_stack.append({"level": level, "text": heading_text})
                current_section = {
                    "level": level,
                    "header": heading_text,
                    "header_path": [h["text"] for h in header_stack],
                    "content_parts": [],
                    "start_char": token_start_char(token),
                    "end_char": len(source_text),
                }
                # Advance past heading_close, searching forward in case
                # the token stream has unexpected structure (e.g. empty heading).
                i += 1
                while i < len(tokens) and tokens[i].type != "heading_close":
                    i += 1
                i += 1  # step past heading_close
                continue
            content = self._token_content(token, source_text)
            if content and token.type not in _STRUCTURAL_TOKEN_TYPES:
                current_section["content_parts"].append(
                    {
                        "type": token.type,
                        "content": content,
                        "is_code_block": token.type in ("code_block", "fence"),
                        "end_char": token_end_char(token),
                    }
                )
            i += 1

        # Emit the last section; for level-0, only if it has content.
        if current_section["level"] > 0 or current_section["content_parts"]:
            sections.append(current_section)

        if not sections:
            sections = [
                {
                    "level": 0,
                    "header": "",
                    "header_path": [],
                    "content_parts": [
                        {"type": "text", "content": source_text, "is_code_block": False}
                    ],
                    "start_char": 0,
                    "end_char": len(source_text),
                }
            ]
        return sections

    def _token_content(self, token: Any, source_text: str) -> str:
        if token.content:
            return token.content
        if token.map:
            # Re-splits source_text on each call. Acceptable because most tokens
            # have .content populated; the .map fallback path is rare. If this
            # becomes a bottleneck, pre-split and pass lines as a parameter.
            lines = source_text.split("\n")
            start, end = token.map
            return "\n".join(lines[start:end])
        return ""

    def _chunk_sections(self, sections: list[dict], base_metadata: dict) -> list[MarkdownChunk]:
        chunks: list[MarkdownChunk] = []
        chunk_index = 0
        for section in sections:
            section_text = self._section_text(section)
            if len(section_text) / _CHARS_PER_TOKEN <= self.chunk_size:
                chunks.append(
                    self._make_chunk(
                        section_text,
                        chunk_index,
                        base_metadata,
                        section["header_path"],
                        chunk_start_char=section.get("start_char", 0),
                        chunk_end_char=section.get("end_char", 0),
                    )
                )
                chunk_index += 1
            else:
                sub = self._split_large_section(section, base_metadata, chunk_index)
                chunks.extend(sub)
                chunk_index += len(sub)
        return chunks

    def _section_text(self, section: dict) -> str:
        parts: list[str] = []
        if section["header"]:
            parts.append("#" * section["level"] + " " + section["header"])
        parts.extend(p["content"] for p in section["content_parts"])
        return "\n\n".join(parts)

    def _split_large_section(
        self, section: dict, base_metadata: dict, start_index: int
    ) -> list[MarkdownChunk]:
        chunks: list[MarkdownChunk] = []
        current_parts: list[str] = []
        current_tokens = 0.0
        chunk_index = start_index

        header_text = "#" * section["level"] + " " + section["header"] if section["header"] else ""
        if header_text:
            current_parts.append(header_text)
            current_tokens += len(header_text) / _CHARS_PER_TOKEN

        section_start_char = section.get("start_char", 0)
        section_end_char = section.get("end_char", 0)
        current_end_char = section_start_char

        for part in section["content_parts"]:
            part_text = part["content"]
            part_tokens = len(part_text) / _CHARS_PER_TOKEN
            part_end_char = part.get("end_char", current_end_char)
            if current_tokens + part_tokens <= self.chunk_size:
                current_parts.append(part_text)
                current_tokens += part_tokens
                current_end_char = part_end_char
            else:
                if current_parts:
                    chunks.append(
                        self._make_chunk(
                            "\n\n".join(current_parts),
                            chunk_index,
                            base_metadata,
                            section["header_path"],
                            chunk_start_char=section_start_char,
                            chunk_end_char=current_end_char,
                        )
                    )
                    chunk_index += 1
                    section_start_char = current_end_char
                # Truncate oversized parts to prevent unbounded chunk sizes
                if len(part_text) > self.max_chars:
                    part_text = part_text[: self.max_chars]
                header_tokens = len(header_text) / _CHARS_PER_TOKEN if header_text else 0.0
                current_parts = [header_text, part_text] if header_text else [part_text]
                current_tokens = header_tokens + len(part_text) / _CHARS_PER_TOKEN
                current_end_char = part_end_char

        if current_parts:
            chunks.append(
                self._make_chunk(
                    "\n\n".join(current_parts),
                    chunk_index,
                    base_metadata,
                    section["header_path"],
                    chunk_start_char=section_start_char,
                    chunk_end_char=section_end_char,
                )
            )
        return chunks

    def _make_chunk(
        self,
        text: str,
        chunk_index: int,
        base_metadata: dict,
        header_path: list[str],
        chunk_start_char: int = 0,
        chunk_end_char: int = 0,
    ) -> MarkdownChunk:
        meta = {
            **base_metadata,
            "chunk_index": chunk_index,
            "header_path": " > ".join(header_path) if header_path else "",
            "chunk_start_char": chunk_start_char,
            "chunk_end_char": chunk_end_char,
        }
        return MarkdownChunk(
            text=text,
            chunk_index=chunk_index,
            metadata=meta,
            header_path=header_path,
        )

    # ── naive fallback ─────────────────────────────────────────────────────────

    def _naive_chunking(self, text: str, metadata: dict) -> list[MarkdownChunk]:
        chunks: list[MarkdownChunk] = []
        chunk_index = 0
        overlap_chars = int(self.chunk_overlap * _CHARS_PER_TOKEN)
        start = 0

        while start < len(text):
            end = min(start + self.max_chars, len(text))
            if end < len(text):
                search_start = end - max(1, int(self.max_chars * 0.2))
                para_end = text.rfind("\n\n", search_start, end)
                if para_end != -1:
                    end = para_end + 2

            chunk_text = text[start:end].strip()
            if chunk_text:
                meta = {**metadata, "chunk_index": chunk_index, "chunk_start_char": start, "chunk_end_char": end, "header_path": ""}
                chunks.append(MarkdownChunk(text=chunk_text, chunk_index=chunk_index, metadata=meta, header_path=[]))
                chunk_index += 1

            next_start = end - overlap_chars
            if next_start <= start:
                break
            start = next_start

        return chunks
