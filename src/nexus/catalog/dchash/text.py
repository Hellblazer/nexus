# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-171 canonical text, its hash, and the ``dchash:`` span grammar.

A dchash span pins a passage by code-point offsets into a document's
canonical text: strict UTF-8, one leading U+FEFF removed, CRLF and then
lone CR turned into LF, nothing else changed. The hash is the SHA-256 of
that text, so the same passage keeps its span across re-chunking and
across a CRLF/LF checkout difference.
"""

from __future__ import annotations

import hashlib
import re

DCHASH_PREFIX: str = "dchash:"

# [0-9], never \d: \d matches non-ASCII digits.
_DCHASH_SPAN: re.Pattern[str] = re.compile(
    r"dchash:([0-9a-f]{64}):(0|[1-9][0-9]*)-(0|[1-9][0-9]*)"
)


def canonical_text(raw: bytes) -> str:
    """Return the canonical text of *raw*; a UnicodeDecodeError propagates."""
    text = raw.decode("utf-8")
    if text.startswith("\ufeff"):
        text = text[1:]
    return text.replace("\r\n", "\n").replace("\r", "\n")


def text_hash(text: str) -> str:
    """Lowercase hex SHA-256 of *text* encoded as UTF-8."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def parse_dchash_span(span: str) -> tuple[str, int, int]:
    """Parse ``dchash:<hash>:<start>-<end>`` into (hash, start, end).

    Raises ValueError naming the span when it does not match the grammar
    or when start is not below end.
    """
    m = _DCHASH_SPAN.fullmatch(span)
    if m is None:
        raise ValueError(f"malformed dchash span: {span!r}")
    try:
        start, end = int(m.group(2)), int(m.group(3))
    except ValueError:  # more digits than int() accepts
        raise ValueError(f"malformed dchash span (offset too long): {span!r}") from None
    if start >= end:
        raise ValueError(f"malformed dchash span (start must be below end): {span!r}")
    return m.group(1), start, end
