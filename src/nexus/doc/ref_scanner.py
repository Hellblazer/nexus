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


# Markdown list-item markers: -, *, +, or N. / N) — leading whitespace allowed.
_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")


def _is_list_item(line: str) -> bool:
    """True if *line* starts a markdown bullet or ordered-list item."""
    return bool(_LIST_ITEM_RE.match(line))


def _scope_for_line(
    lines: list[tuple[int, str]],
    target_idx: int,
) -> tuple[list[str], int]:
    """Return ``(scope_lines, target_within_scope)`` for count-proximity binding.

    Each list-item line is its own scope — count claims from sibling
    bullets do not leak (nexus-7ay). Non-list lines expand to the
    surrounding consecutive prose paragraph, with list-item boundaries
    treated as paragraph breaks.
    """
    target_line = lines[target_idx][1]
    if _is_list_item(target_line):
        return [target_line], 0

    start = target_idx
    while (
        start > 0
        and lines[start - 1][1].strip()
        and lines[start - 1][0] == lines[start][0] - 1
        and not _is_list_item(lines[start - 1][1])
    ):
        start -= 1
    end = target_idx
    while (
        end + 1 < len(lines)
        and lines[end + 1][1].strip()
        and lines[end + 1][0] == lines[end][0] + 1
        and not _is_list_item(lines[end + 1][1])
    ):
        end += 1
    return [ln[1] for ln in lines[start : end + 1]], target_idx - start


def _iter_count_matches(scope_text: str):
    """Yield ``(start_position, value)`` for every chunk-count claim
    in *scope_text*. Both plain-integer and k-shorthand patterns are
    scanned; positions are character offsets into *scope_text*.

    The generator yields plain-integer matches first, then k-shorthand
    matches. ``_extract_count_near`` consumes via ``min(..., key=abs)``
    which is O(N) in candidate count and correctly finds the nearest
    regardless of iteration order, so no explicit sort is needed.
    """
    for m in _COUNT_RES[0].finditer(scope_text):
        try:
            yield m.start(), int(m.group("n").replace(",", ""))
        except ValueError:
            continue
    for m in _COUNT_RES[1].finditer(scope_text):
        try:
            yield m.start(), int(float(m.group("n")) * 1000)
        except ValueError:
            continue


def _extract_count_near(scope_text: str, ref_pos: int) -> int | None:
    """Return the value of the count claim nearest *ref_pos* in
    *scope_text*, or ``None`` when no claim is present.

    Tie-break (Reviewer C/I-3): when two count claims are exactly
    equidistant from *ref_pos*, Python's stable ``min()`` picks the one
    that appears first in the candidate list. ``_iter_count_matches``
    yields plain-integer matches ahead of k-shorthand matches, so
    ``"5,000 chunks"`` wins over ``"5k chunks"`` at the same distance.
    Within a pattern, the match with the lower ``start`` position wins.
    """
    candidates = list(_iter_count_matches(scope_text))
    if not candidates:
        return None
    return min(candidates, key=lambda c: abs(c[0] - ref_pos))[1]


def scan_markdown(path: Path, prefixes: list[str]) -> list[Reference]:
    """Return every ``<prefix>__<name>`` reference in *path*.

    References inside fenced code blocks are ignored. Each bullet-list
    item is its own count-binding scope; within a prose paragraph each
    reference binds to the textually nearest chunk-count claim.
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

    for idx, (lineno, line) in enumerate(plain_lines):
        for m in coll_re.finditer(line):
            prefix = m.group("prefix")
            name = m.group("name")
            collection = f"{prefix}__{name}"

            scope_lines, target_within = _scope_for_line(plain_lines, idx)
            scope_text = " ".join(scope_lines)
            # Offset of the target line within the joined scope. `" "` join
            # means a single-char separator between lines.
            offset = sum(len(s) + 1 for s in scope_lines[:target_within])
            claimed = _extract_count_near(scope_text, offset + m.start())

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
