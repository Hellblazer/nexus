# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Shared helpers for the ``nexus.doc`` module.

Used by :mod:`nexus.doc.ref_scanner` (RDR-081) and
:mod:`nexus.doc.tokens` (RDR-082) — anything that walks markdown and
must respect fenced code blocks lands here.
"""
from __future__ import annotations

import re
from collections.abc import Iterator


#: Markdown fence opener/closer (triple-backtick or triple-tilde).
#: Matches any leading whitespace so indented fences inside list items
#: are still recognised.
FENCE_RE = re.compile(r"^\s*(```|~~~)")


def iter_plain_lines(text: str) -> Iterator[tuple[int, str]]:
    """Yield ``(1-based lineno, line)`` for every non-fenced line.

    Content inside ```` ``` ```` or ``~~~`` fences is skipped so tutorial
    snippets don't false-positive. Line numbers preserve original file
    positions — callers reporting errors can report ``file:line:col``
    against the real source.
    """
    in_fence = False
    fence_marker: str | None = None
    for lineno, line in enumerate(text.splitlines(), 1):
        m = FENCE_RE.match(line)
        if m:
            if not in_fence:
                in_fence = True
                fence_marker = m.group(1)
            elif m.group(1) == fence_marker:
                in_fence = False
                fence_marker = None
            continue
        if in_fence:
            continue
        yield lineno, line
