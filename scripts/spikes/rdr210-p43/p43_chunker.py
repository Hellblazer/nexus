# SPDX-License-Identifier: AGPL-3.0-or-later
"""Deterministic paragraph/heading chunker for the P4.3 corpora.

Pure: text in, list of chunk strings out. The same text always yields the same
chunks (no randomness, no clock, no locale), which is what lets the corpus hash
key idempotent re-runs.

Blocks are cut at blank lines and at heading lines (Markdown ``#`` headings,
Python ``def``/``class``/``async def``/decorator lines). A heading block starts a
new chunk; other consecutive blocks are packed greedily up to ``max_chars``. A block longer than
``max_chars`` is split by lines, and a single line longer than ``max_chars`` by
characters.
"""
from __future__ import annotations

import re

DEFAULT_MAX_CHARS = 1500

_HEADING = re.compile(r"^(#{1,6}\s|(?:async\s+def|def|class)\s|@\w)")


def _blocks(text: str) -> list[str]:
    """Split *text* at blank lines and before heading lines."""
    out: list[str] = []
    cur: list[str] = []

    def flush() -> None:
        if cur:
            block = "\n".join(cur).strip("\n")
            if block.strip():
                out.append(block)
            cur.clear()

    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if not line.strip():
            flush()
            continue
        # A heading starts a new block unless the previous line is a decorator
        # (a decorator and its def stay together).
        if _HEADING.match(line) and cur and not cur[-1].startswith("@"):
            flush()
        cur.append(line)
    flush()
    return out


def _split_oversize(block: str, max_chars: int) -> list[str]:
    """Split one block longer than *max_chars* by lines, then by characters."""
    pieces: list[str] = []
    cur = ""
    for line in block.split("\n"):
        while len(line) > max_chars:
            if cur:
                pieces.append(cur)
                cur = ""
            pieces.append(line[:max_chars])
            line = line[max_chars:]
        if cur and len(cur) + 1 + len(line) > max_chars:
            pieces.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        pieces.append(cur)
    return pieces


def chunk_text(text: str, max_chars: int = DEFAULT_MAX_CHARS) -> list[str]:
    """Chunk *text* into strings of at most *max_chars* characters.

    Deterministic. Whitespace-only input yields ``[]``. Every chunk is
    non-empty and stripped of leading and trailing blank lines.
    """
    if max_chars < 1:
        raise ValueError("max_chars must be positive")
    chunks: list[str] = []
    cur = ""
    for block in _blocks(text):
        if cur and _HEADING.match(block):
            # A heading starts a new chunk, so a chunk never opens mid-section.
            chunks.append(cur)
            cur = ""
        parts = _split_oversize(block, max_chars) if len(block) > max_chars else [block]
        for part in parts:
            if cur and len(cur) + 2 + len(part) > max_chars:
                chunks.append(cur)
                cur = part
            else:
                cur = f"{cur}\n\n{part}" if cur else part
    if cur:
        chunks.append(cur)
    return chunks
