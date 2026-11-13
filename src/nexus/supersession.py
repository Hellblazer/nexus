# SPDX-License-Identifier: AGPL-3.0-or-later
"""Superseded documents, read at search time (nexus-2k66t.6, 2A).

A catalog ``supersedes`` link runs from the successor to the document it replaces
(``link_generator._RDR_DEPENDENCY_FIELDS``). Before this, the link's boost weight was 0.0 and nothing else read it,
so a superseded document ranked and read exactly like a current one. Now a hit is superseded when every live document
holding its chunk is the target of a ``supersedes`` link; it is marked (:data:`SUPERSEDED_KEY`, rendered by
``context_annotations.hit_annotation_line``) and demoted by :data:`DISTANCE_PENALTY`, in distance units, on
``topic_boost``: the one adjustment made before ``apply_hybrid_scoring`` that the hybrid score keeps. A chunk one
current document still holds is the current text and is left alone.

Lookup: there is no batch route for incoming links, so the whole ``supersedes`` set is fetched with
``link_query(link_type="supersedes")`` and kept for :data:`TTL_S` per process (14 links in the live catalog on
2026-11-13). Past :data:`MAX_LINKS` the set is not used at all, rather than used partly. Successor titles come from
one ``resolve_many`` call, made only when the page has a superseded hit. Any failure leaves the results as they were.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from typing import Any

import structlog

from nexus.types import SearchResult

__all__ = ["DISTANCE_PENALTY", "SUPERSEDED_KEY", "mark_superseded", "superseded_note"]

_log = structlog.get_logger(__name__)

#: Metadata key: ``(successor title, link date YYYY-MM-DD)`` of the newest successor. A working key, like
#: ``context_annotations.OWNER_FACTS_KEY``.
SUPERSEDED_KEY = "_superseded_by"
#: Added to a superseded hit's effective distance through a negative ``topic_boost``: enough to sink it below a
#: current hit of similar distance, small enough to keep it on the page.
DISTANCE_PENALTY = 0.1
TTL_S = 300.0
MAX_LINKS = 5000
_PAGE = 1000

_lock = threading.Lock()
_cache: dict[str, Any] = {}


def _clear() -> None:
    """Tests only."""
    with _lock:
        _cache.clear()


def _date(created_at: str) -> str:
    return (created_at or "")[:10]


def _superseded_map(catalog: Any, now: float) -> dict[str, list[tuple[str, str]]] | None:
    """``{superseded tumbler: [(successor tumbler, link date), ...]}``, cached for :data:`TTL_S`; None when the set
    could not be read whole."""
    with _lock:
        if _cache.get("catalog") is catalog and now - _cache.get("at", -1e18) < TTL_S:
            return _cache.get("map")
    out: dict[str, list[tuple[str, str]]] = {}
    seen = 0
    offset = 0
    while True:
        page = catalog.link_query(link_type="supersedes", limit=_PAGE, offset=offset)
        for link in page:
            out.setdefault(str(link.to_tumbler), []).append((str(link.from_tumbler), _date(link.created_at)))
        seen += len(page)
        if len(page) < _PAGE:
            break
        if seen >= MAX_LINKS:
            _log.warning("supersession_link_set_over_cap", cap=MAX_LINKS)
            out = None  # type: ignore[assignment] — a partial set would mark some superseded hits and miss others
            break
        offset += _PAGE
    with _lock:
        _cache.update({"catalog": catalog, "at": now, "map": out})
    return out


def mark_superseded(
    results: list[SearchResult],
    catalog: Any,
    *,
    owners_of: Callable[[SearchResult], list[str]],
    clock: Callable[[], float] = time.monotonic,
) -> list[SearchResult]:
    """Mark and demote the hits every one of whose owners is superseded (see the module docstring). *owners_of*
    names a hit's owning documents (``scoring._link_owners``). Mutates and returns *results*, order unchanged."""
    if not catalog or not results:
        return results
    try:
        superseded = _superseded_map(catalog, clock())
        if not superseded:
            return results
        hits: list[tuple[SearchResult, list[tuple[str, str]]]] = []
        for r in results:
            owners = owners_of(r)
            if owners and all(o in superseded for o in owners):
                hits.append((r, [s for o in owners for s in superseded[o]]))
        if not hits:
            return results
        successors = sorted({t for _, succ in hits for t, _d in succ})
        titles = {t: getattr(e, "title", "") or "" for t, e in (catalog.resolve_many(successors) or {}).items()}
        for r, succ in hits:
            tumbler, when = max(succ, key=lambda s: s[1])  # the newest link names the current successor
            r.metadata[SUPERSEDED_KEY] = (titles.get(tumbler) or tumbler, when)
            r.topic_boost -= DISTANCE_PENALTY
    except Exception:  # noqa: BLE001 — best-effort, like the link boost: a failed lookup leaves the results as they were
        _log.warning("supersession_lookup_failed", exc_info=True)
    return results


def superseded_note(metadata: Mapping[str, Any]) -> str:
    """``superseded by <title> (<date>)`` for a marked hit, else ``""``."""
    value = metadata.get(SUPERSEDED_KEY)
    if not value:
        return ""
    title, when = value
    return f"superseded by {title}" + (f" ({when})" if when else "")
