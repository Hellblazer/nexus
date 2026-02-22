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
    rest = text[idx + 4:].lstrip("\n")
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
        sections: list[dict] = []
        current_section: dict | None = None
        header_stack: list[dict] = []

        i = 0
        while i < len(tokens):
            token = tokens[i]
            if token.type == "heading_open":
                level = int(token.tag[1])
                heading_text = ""
                if i + 1 < len(tokens) and tokens[i + 1].type == "inline":
                    heading_text = tokens[i + 1].content
                if current_section is not None:
                    sections.append(current_section)
                header_stack = [h for h in header_stack if h["level"] < level]
                header_stack.append({"level": level, "text": heading_text})
                current_section = {
                    "level": level,
                    "header": heading_text,
                    "header_path": [h["text"] for h in header_stack],
                    "content_parts": [],
                }
                # Advance past heading_close, searching forward in case
                # the token stream has unexpected structure (e.g. empty heading).
                i += 1
                while i < len(tokens) and tokens[i].type != "heading_close":
                    i += 1
                i += 1  # step past heading_close
                continue
            if current_section is not None:
                content = self._token_content(token, source_text)
                if content:
                    current_section["content_parts"].append(
                        {
                            "type": token.type,
                            "content": content,
                            "is_code_block": token.type in ("code_block", "fence"),
                        }
                    )
            i += 1

        if current_section is not None:
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
                }
            ]
        return sections

    def _token_content(self, token: Any, source_text: str) -> str:
        if token.content:
            return token.content
        if token.map:
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
                chunks.append(self._make_chunk(section_text, chunk_index, base_metadata, section["header_path"]))
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

        for part in section["content_parts"]:
            part_text = part["content"]
            part_tokens = len(part_text) / _CHARS_PER_TOKEN
            if current_tokens + part_tokens <= self.chunk_size:
                current_parts.append(part_text)
                current_tokens += part_tokens
            else:
                if current_parts:
                    chunks.append(
                        self._make_chunk(
                            "\n\n".join(current_parts), chunk_index, base_metadata, section["header_path"]
                        )
                    )
                    chunk_index += 1
                # Truncate oversized parts to prevent unbounded chunk sizes
                if len(part_text) > self.max_chars:
                    part_text = part_text[: self.max_chars]
                current_parts = ([header_text, part_text] if header_text else [part_text])
                current_tokens = (len(header_text) / _CHARS_PER_TOKEN if header_text else 0) + len(part_text) / _CHARS_PER_TOKEN

        if current_parts:
            chunks.append(
                self._make_chunk(
                    "\n\n".join(current_parts), chunk_index, base_metadata, section["header_path"]
                )
            )
        return chunks

    def _make_chunk(
        self,
        text: str,
        chunk_index: int,
        base_metadata: dict,
        header_path: list[str],
    ) -> MarkdownChunk:
        meta = {
            **base_metadata,
            "chunk_index": chunk_index,
            "header_path": " > ".join(header_path) if header_path else "",
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
