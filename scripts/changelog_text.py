# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reading Liquibase changelog XML as text: the one place that blanks what is not forward SQL.

Every reader that scans changelog text for SQL (the RDR-229 censuses, the changelog lints) goes through
``strip_noise``, so the order cannot drift between them. The order matters. XML comments go FIRST: a header comment
that names ``<rollback>`` in prose (vectors-069's does) would otherwise open a rollback match that runs to the first
real ``</rollback>`` and blanks the forward body between them, which hid vectors-069-1's definition of
``nexus.gc_quarantine_orphans`` from two censuses (nexus-p2m1x.24). Then ``<comment>`` elements (prose), then
``<rollback>`` blocks, which restore older bodies verbatim and are not forward SQL.

Blanking keeps every offset and newline, so a line number computed on the result is the file's line number.
"""
from __future__ import annotations

import re

_XML_COMMENT = re.compile(r"<!--.*?-->", re.S)
_COMMENT_ELEMENT = re.compile(r"<comment>.*?</comment>", re.S)
_ROLLBACK = re.compile(r"<rollback>.*?</rollback>", re.S)


def blank(m: re.Match[str]) -> str:
    """The match with every character but a newline turned into a space."""
    return re.sub(r"[^\n]", " ", m.group(0))


def strip_noise(text: str) -> str:
    """Blank XML comments, then <comment> elements, then <rollback> blocks, keeping every offset and newline."""
    text = _XML_COMMENT.sub(blank, text)
    text = _COMMENT_ELEMENT.sub(blank, text)
    return _ROLLBACK.sub(blank, text)
