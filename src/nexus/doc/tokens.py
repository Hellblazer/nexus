# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-082: Token parser for ``nx doc render``.

Grammar::

    {{NAMESPACE:KEY[.FIELD][|FILTER=VALUE]*}}

    namespace : [a-z][a-z0-9-]*   (e.g. ``bd``, ``rdr``, ``nx-anchor``)
    key       : [^.|}]+           (bead id, rdr id, collection name, ...)
    field     : [^|}]+            (resolver-specific dotted path)
    filter    : <name>=<value>    (resolver-specific, multiple allowed)

Tokens inside fenced markdown code (```` ``` ```` / ``~~~``) are
ignored so tutorial snippets that *demonstrate* the syntax don't get
resolved. Callers that need the literal fence behaviour (e.g. rendered
HTML passthrough) should use a different escape.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from nexus.doc._common import FENCE_RE

__all__ = ["Token", "parse_tokens"]


# Grammar regex. Capture groups:
#   1: namespace   — alphanum + hyphen, must start with a letter
#   2: key         — one or more chars not in ".|}"
#   3: field       — optional, starts after "."
#   4: filter blob — optional, starts after "|"
_TOKEN_RE = re.compile(
    r"""
    \{\{\s*
    (?P<ns>[a-z][a-z0-9-]*)           # namespace
    \s*:\s*
    (?P<key>[^.|}]+?)                 # key (non-greedy)
    (?:\.(?P<field>[^|}]+?))?         # optional .field
    (?P<filters>(?:\|[^}]+)?)         # optional |k=v|k=v blob
    \s*\}\}
    """,
    re.VERBOSE,
)

_FILTER_RE = re.compile(r"([a-z_][a-z0-9_-]*)=([^|]+)")


@dataclass(slots=True)
class Token:
    """One resolved-at-render token occurrence."""

    namespace: str
    key: str
    field: str | None
    filters: dict[str, str] = field(default_factory=dict)
    span: tuple[int, int] = (0, 0)   # (start, end) byte offsets in source
    lineno: int = 0                   # 1-based
    col: int = 0                      # 1-based column of the opening ``{{``
    raw: str = ""                     # literal source text for error reporting


def parse_tokens(text: str) -> list[Token]:
    """Scan *text* for tokens outside fenced code blocks.

    Returns tokens in source order. Malformed ``{{…}}`` sequences that
    do not match the grammar are silently skipped — render-time
    validation decides whether to fail the build (the default) or
    preserve the literal text.
    """
    # Walk the text once, tracking fence state. Within fenced zones,
    # skip token extraction entirely — but keep byte offsets correct
    # for any tokens encountered later so error reporting matches the
    # real file position.
    tokens: list[Token] = []
    in_fence = False
    fence_marker: str | None = None

    offset = 0
    for lineno, line in enumerate(text.splitlines(keepends=True), 1):
        stripped = line.rstrip("\n\r")
        m = FENCE_RE.match(stripped)
        if m:
            if not in_fence:
                in_fence = True
                fence_marker = m.group(1)
            elif m.group(1) == fence_marker:
                in_fence = False
                fence_marker = None
            offset += len(line)
            continue
        if in_fence:
            offset += len(line)
            continue

        for tm in _TOKEN_RE.finditer(stripped):
            filters = _parse_filters(tm.group("filters") or "")
            col = tm.start() + 1
            start = offset + tm.start()
            end = offset + tm.end()
            tokens.append(
                Token(
                    namespace=tm.group("ns"),
                    key=tm.group("key").strip(),
                    field=(tm.group("field") or "").strip() or None,
                    filters=filters,
                    span=(start, end),
                    lineno=lineno,
                    col=col,
                    raw=tm.group(0),
                )
            )
        offset += len(line)

    return tokens


def _parse_filters(blob: str) -> dict[str, str]:
    """Parse ``|key=val|key2=val2`` into a flat dict. Malformed entries
    are dropped — the resolver sees only well-formed pairs."""
    out: dict[str, str] = {}
    if not blob:
        return out
    # Strip leading '|'
    for m in _FILTER_RE.finditer(blob.lstrip("|")):
        out[m.group(1)] = m.group(2).strip()
    return out
