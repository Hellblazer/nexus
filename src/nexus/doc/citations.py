# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-083: Citation scanner + grounding / author-extension reports.

Three citation shapes are recognised:

  * ``chash`` — ``[display](chash:<64-hex>)`` markdown link.  The
    chash span is the primary grounding primitive; v1 reports shape
    only (cross-collection hash-to-chunk resolution is deferred).
  * ``prose`` — ``[Author <year>]`` patterns (``[Grossberg 2013]``).
    Not machine-verifiable; reported as prose-only coverage gap.
  * ``bracket`` — ``[NN]`` numeric references that resolve to a
    bibliography elsewhere in the doc.

Fenced code blocks are skipped (tutorial snippets that demonstrate
the syntax are not scanned).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

from nexus.doc._common import iter_plain_lines

__all__ = [
    "Citation",
    "GroundingReport",
    "ExtensionsReport",
    "scan_citations",
    "grounding_report",
    "extensions_report",
]


CitationKind = Literal["chash", "prose", "bracket"]


@dataclass(slots=True)
class Citation:
    """One citation occurrence."""

    kind: CitationKind
    display: str                     # visible text (link display or raw token)
    lineno: int                       # 1-based source line
    col: int                          # 1-based column of opener
    chash: str | None = None          # set iff kind == "chash"


# Grammar regexes.
#
# chash link: [text](chash:<hex>) — require EXACTLY 64 hex chars so
#   we never accept a truncated / malformed hash.  The display text
#   can contain any non-bracket non-newline character.
_CHASH_LINK_RE = re.compile(
    r"\[(?P<display>[^\]\n]+)\]\(chash:(?P<hash>[0-9a-f]{64})(?::\d+-\d+)?\)"
)

# Prose citation: [<Author optional-initial> <year>]
#   - Author starts with a capital letter
#   - year is 4 digits
#   - case: [Grossberg 2013], [Smith, J. 2020]
#
# Stop-list suppresses common non-citation bracketed tokens in dev
# prose (changelogs, commit messages, RDR bodies): [Error 2013],
# [RFC 2119], [Note 2024], [Closes 2025], [Figure 2020], etc.
_PROSE_CITE_RE = re.compile(
    r"\[(?P<author>[A-Z][A-Za-z.\-' ,]{1,40}?)\s+(?P<year>\d{4})\]"
)

_PROSE_CITE_STOPLIST = frozenset({
    # Commit / PR / issue framing
    "closes", "closed", "fix", "fixes", "fixed", "resolve", "resolves",
    "resolved", "issue", "pr", "note", "notes", "draft", "release",
    "version", "v", "rev", "revision",
    # Doc structure
    "figure", "fig", "table", "section", "chapter", "appendix",
    "equation", "eq",
    # Standards / IDs
    "rfc", "iso", "iec", "ietf", "ansi", "w3c",
    # Log levels
    "error", "warn", "warning", "info", "debug", "trace", "fatal",
    "critical",
    # Generic markers
    "todo", "fixme", "xxx", "hack", "deprecated",
})


def _is_real_prose_citation(author: str) -> bool:
    """Filter false-positives from the prose-citation regex.

    Returns False for stop-list entries, single letters, and 2-char
    segments that are almost never real author names.
    """
    head = author.split()[0].lower().rstrip(",.")
    if head in _PROSE_CITE_STOPLIST:
        return False
    alpha = sum(ch.isalpha() for ch in head)
    return alpha >= 3

# Bracketed number: [12], [3], [99] — must be digits only to avoid
#   conflict with wiki/task-list markers like [x] or [12 ideas].
_BRACKET_CITE_RE = re.compile(r"\[(?P<num>\d{1,4})\]")


def scan_citations(md_text: str) -> list[Citation]:
    """Return every citation occurrence, in source order."""
    cites: list[Citation] = []
    for lineno, line in iter_plain_lines(md_text):
        # chash first — consumes its bracketed text so prose / bracket
        # scanners don't double-count the same span
        chash_spans: list[tuple[int, int]] = []
        for m in _CHASH_LINK_RE.finditer(line):
            cites.append(Citation(
                kind="chash",
                display=m.group("display"),
                lineno=lineno,
                col=m.start() + 1,
                chash=m.group("hash"),
            ))
            chash_spans.append((m.start(), m.end()))

        def _in_chash(pos: int) -> bool:
            return any(s <= pos < e for s, e in chash_spans)

        for m in _PROSE_CITE_RE.finditer(line):
            if _in_chash(m.start()):
                continue
            author = m.group("author")
            if not _is_real_prose_citation(author):
                continue
            cites.append(Citation(
                kind="prose",
                display=f"{author} {m.group('year')}",
                lineno=lineno,
                col=m.start() + 1,
            ))

        for m in _BRACKET_CITE_RE.finditer(line):
            if _in_chash(m.start()):
                continue
            # Skip positions that overlap an already-captured prose citation
            covered = any(
                c.lineno == lineno and c.col <= m.start() + 1 < c.col + len(c.display) + 2
                for c in cites if c.kind == "prose"
            )
            if covered:
                continue
            cites.append(Citation(
                kind="bracket",
                display=m.group(0),
                lineno=lineno,
                col=m.start() + 1,
            ))

    return cites


# ── Grounding report ─────────────────────────────────────────────────────────


@dataclass(slots=True)
class GroundingReport:
    """Per-doc citation coverage."""

    total: int = 0
    chash_count: int = 0
    prose_count: int = 0
    bracket_count: int = 0

    @property
    def coverage(self) -> float:
        """chash citations as a fraction of all citations."""
        return (self.chash_count / self.total) if self.total else 0.0


def grounding_report(cites: list[Citation]) -> GroundingReport:
    """Compute coverage metrics from a scanned citation list."""
    r = GroundingReport()
    for c in cites:
        r.total += 1
        if c.kind == "chash":
            r.chash_count += 1
        elif c.kind == "prose":
            r.prose_count += 1
        elif c.kind == "bracket":
            r.bracket_count += 1
    return r


# ── Extensions report ────────────────────────────────────────────────────────


@dataclass(slots=True)
class ExtensionsReport:
    """Author-extension candidates derived from projection data."""

    checked: int = 0
    candidates: list[tuple[str, float]] = field(default_factory=list)   # (doc_id, similarity)
    no_data: list[str] = field(default_factory=list)


def extensions_report(
    doc_ids: list[str],
    *,
    primary_source: str,
    threshold: float,
    taxonomy: Any,
) -> ExtensionsReport:
    """For each doc, check whether it projects into *primary_source* at
    or above *threshold*. Docs below → candidates for `> [Author extension]`.

    *taxonomy* must expose ``chunk_grounded_in(doc_id, source_collection,
    threshold)`` returning the top similarity of that doc's chunks
    against the source collection, or ``None`` when no projection data
    is available.
    """
    out = ExtensionsReport()
    for did in doc_ids:
        out.checked += 1
        sim = taxonomy.chunk_grounded_in(
            did, primary_source, threshold=threshold,
        )
        if sim is None:
            out.no_data.append(did)
            continue
        if sim < threshold:
            out.candidates.append((did, sim))
    return out
