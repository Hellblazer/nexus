# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Stale-reference validator for user-facing prose (RDR-081).

Scans markdown for collection-name references (``docs__<name>``,
``knowledge__<name>``, etc.) and proximate chunk-count claims
(``"12,900 chunks"``, ``"~13k chunks"``), looks up current T3 state, and
reports drift. Pure regex + SQL/ChromaDB ``count()`` calls — no LLM.

Fenced code blocks (``` … ``` and ``~~~`` … ``~~~``) are skipped so
tutorial snippets don't false-positive.

Public surface:

* :func:`scan_markdown(path, prefixes)` → list of :class:`Reference`
* :func:`validate(refs, t3, tolerance)` → list of :class:`Drift`
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


#: Sentinel verdicts. Exposed as strings so JSON output is trivial.
VERDICT_OK = "OK"
VERDICT_DRIFT = "Drift"
VERDICT_MISSING = "Missing"


@dataclass(frozen=True)
class Reference:
    """One collection reference found in a markdown file.

    Attributes
    ----------
    path : Path
        Source file.
    line : int
        1-based line number where the reference appears.
    collection : str
        Full collection name (e.g. ``docs__art-architecture``).
    prefix : str
        Prefix portion (``docs``, ``knowledge``, etc.).
    claimed_count : int | None
        Chunk-count asserted in the same paragraph as the reference, or
        ``None`` if no count-like phrase is present.
    """

    path: Path
    line: int
    collection: str
    prefix: str
    claimed_count: int | None = None


@dataclass(frozen=True)
class Drift:
    """A validated reference with verdict + actual state.

    ``verdict`` is one of :data:`VERDICT_OK`, :data:`VERDICT_DRIFT`,
    :data:`VERDICT_MISSING`.

    ``delta`` is ``actual_count - claimed_count`` when both are known,
    else ``None``.
    """

    ref: Reference
    verdict: str
    actual_count: int | None
    delta: int | None = None
    note: str = ""


# ── Scanner ──────────────────────────────────────────────────────────────────

from nexus.doc._common import FENCE_RE as _FENCE_RE

#: Chunk-count claim patterns.  Order matters — longer/more-specific wins.
#: ``\b`` anchors so we don't match "4k7chunks" or similar tokens.
#:
#:   "12,900 chunks"
#:   "~13k chunks" / "about 13k chunks" / "≈13k chunks"
#:   "13,000 chunks"
_COUNT_RES: tuple[re.Pattern, ...] = (
    # N,NNN chunks | NNN chunks (plain integer)
    re.compile(r"\b(?P<n>\d[\d,]*)\s+chunks?\b", re.IGNORECASE),
    # ~Nk / ≈Nk / ~N.Nk chunks
    re.compile(
        r"(?P<prefix>~|≈|about\s+|approximately\s+)?"
        r"(?P<n>\d+(?:\.\d+)?)\s*[kK]\s+chunks?\b"
    ),
)


def _collection_regex(prefixes: list[str]) -> re.Pattern:
    """Build a compiled regex that matches ``<prefix>__<name>`` for any
    prefix in *prefixes*.  Prefixes are validated to match ``[a-z][\\w-]*``
    so a caller-supplied string doesn't smuggle regex metacharacters in.
    """
    safe = []
    for p in prefixes:
        p = p.strip().lower()
        if not re.fullmatch(r"[a-z][\w-]*", p):
            raise ValueError(f"invalid prefix for ref scanner: {p!r}")
        safe.append(re.escape(p))
    alternation = "|".join(safe)
    return re.compile(
        rf"(?<![\w-])(?P<prefix>{alternation})__(?P<name>[\w-]+)"
    )


from nexus.doc._common import iter_plain_lines as _iter_plain_lines


def _paragraph_for_line(lines: list[tuple[int, str]], target_lineno: int) -> list[str]:
    """Return the block of consecutive non-empty plain lines containing
    *target_lineno*. A paragraph is delimited by a blank line on either side.
    """
    idx = next((i for i, (ln, _) in enumerate(lines) if ln == target_lineno), -1)
    if idx < 0:
        return []
    # Scan upward for paragraph start
    start = idx
    while start > 0 and lines[start - 1][1].strip() and lines[start - 1][0] == lines[start][0] - 1:
        start -= 1
    # Scan downward for paragraph end
    end = idx
    while end + 1 < len(lines) and lines[end + 1][1].strip() and lines[end + 1][0] == lines[end][0] + 1:
        end += 1
    return [ln[1] for ln in lines[start : end + 1]]


def _extract_count(paragraph_text: str) -> int | None:
    """Find the first chunk-count claim in *paragraph_text* and return
    the integer value. ``~13k`` → ``13000``; ``12,900`` → ``12900``.
    Returns ``None`` if no claim is present.
    """
    # First pattern (plain integer) wins when both match — it's more precise.
    m = _COUNT_RES[0].search(paragraph_text)
    if m:
        try:
            return int(m.group("n").replace(",", ""))
        except ValueError:
            pass
    m = _COUNT_RES[1].search(paragraph_text)
    if m:
        try:
            # "~13k" → 13000; "13.5k" → 13500
            raw = float(m.group("n"))
            return int(raw * 1000)
        except ValueError:
            pass
    return None


def scan_markdown(path: Path, prefixes: list[str]) -> list[Reference]:
    """Return every ``<prefix>__<name>`` reference in *path*.

    References inside fenced code blocks are ignored. When a reference
    line is part of a paragraph containing a chunk-count claim, the
    claim is associated with every reference in that paragraph
    (same-paragraph nearest-match heuristic — cheap, and good enough
    for the advisory scope).
    """
    # Validate prefixes up-front so a bad config fails fast, even on
    # empty documents (where scan would otherwise short-circuit).
    coll_re = _collection_regex(prefixes)

    path = Path(path)
    text = path.read_text(encoding="utf-8", errors="replace")
    plain_lines = list(_iter_plain_lines(text))
    if not plain_lines:
        return []
    refs: list[Reference] = []

    for lineno, line in plain_lines:
        for m in coll_re.finditer(line):
            prefix = m.group("prefix")
            name = m.group("name")
            collection = f"{prefix}__{name}"
            paragraph = _paragraph_for_line(plain_lines, lineno)
            claimed = _extract_count(" ".join(paragraph)) if paragraph else None
            refs.append(
                Reference(
                    path=path,
                    line=lineno,
                    collection=collection,
                    prefix=prefix,
                    claimed_count=claimed,
                )
            )
    return refs


# ── Validator ────────────────────────────────────────────────────────────────


def _list_collection_names(t3: Any) -> set[str]:
    """Return the set of live collection names from *t3*."""
    try:
        result = t3.list_collections()
    except Exception:
        return set()
    # Accept both ``list[str]`` and ``list[{"name": ...}]`` shapes.
    names: set[str] = set()
    for entry in result or []:
        if isinstance(entry, str):
            names.add(entry)
        elif isinstance(entry, dict) and "name" in entry:
            names.add(entry["name"])
        else:
            names.add(getattr(entry, "name", str(entry)))
    return names


def _count_collection(t3: Any, name: str) -> int | None:
    """Return the chunk count for *name*, or ``None`` if unavailable."""
    try:
        col = t3.get_or_create_collection(name)
    except Exception:
        return None
    try:
        return int(col.count())
    except Exception:
        return None


def validate(
    refs: list[Reference],
    t3: Any,
    *,
    tolerance: float = 0.10,
) -> list[Drift]:
    """Classify each ref as :data:`VERDICT_OK`, :data:`VERDICT_DRIFT`,
    or :data:`VERDICT_MISSING` against the live T3.

    *tolerance* is a fractional window around the claimed count
    (``0.10`` = ±10%). References without a ``claimed_count`` are
    treated as OK when the collection exists (no count to drift from)
    and Missing otherwise.

    A single call to ``list_collections()`` caches existence lookups;
    a per-name ``count()`` is issued only for references that carry a
    claim and whose collection exists.
    """
    live_names = _list_collection_names(t3)
    count_cache: dict[str, int | None] = {}
    out: list[Drift] = []

    for ref in refs:
        if ref.collection not in live_names:
            out.append(Drift(ref=ref, verdict=VERDICT_MISSING, actual_count=None))
            continue
        if ref.claimed_count is None:
            out.append(Drift(ref=ref, verdict=VERDICT_OK, actual_count=None))
            continue
        if ref.collection not in count_cache:
            count_cache[ref.collection] = _count_collection(t3, ref.collection)
        actual = count_cache[ref.collection]
        if actual is None:
            out.append(Drift(
                ref=ref, verdict=VERDICT_MISSING, actual_count=None,
                note="collection exists but count() failed",
            ))
            continue
        delta = actual - ref.claimed_count
        if ref.claimed_count == 0:
            within = actual == 0
        else:
            within = abs(delta) / max(ref.claimed_count, 1) <= tolerance
        verdict = VERDICT_OK if within else VERDICT_DRIFT
        out.append(Drift(ref=ref, verdict=verdict, actual_count=actual, delta=delta))

    return out
