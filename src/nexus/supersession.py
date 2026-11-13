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
2026-11-13). ``get_catalog()`` builds a new handle per call over one shared client, so the cache is keyed on that
shared handle's type, not the object (:func:`_catalog_key`). A failed read is remembered for :data:`FAILED_TTL_S`, so
a catalog that is down costs one request a minute, not one a search. Past :data:`MAX_LINKS` the set is not used at
all, rather than used partly. Successor titles come from one ``resolve_many`` call, made only when the page has a
superseded hit. Any failure leaves the results as they were.

Reach: ``search_engine.search_cross_corpus`` runs :func:`mark_superseded` only when its caller passes
``mark_superseded=True``, and only the MCP ``search`` tool does, because it is the one surface that prints the
annotation: a demotion nobody can see the reason for (``query``, ``nx search``, plan retrieval) is not made. When a
server rerank orders the page (``lexical=True``), the order is the reranker's and the hit is annotated but not
demoted.

A successor flagged sensitive (RDR-243) is named "a withheld document", never by title: it is not an owner of the
hit's chunk, so the hit's own output posture does not cover it. The owners compared are catalog doc_ids, which equal tumblers today (the contract
``scoring.apply_link_boost`` names); a doc_id scheme that differs from the tumbler would mark nothing.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from typing import Any

import structlog

from nexus.context_annotations import OWNER_RESOLVE_CAP
from nexus.types import SearchResult

__all__ = ["DISTANCE_PENALTY", "SUPERSEDED_KEY", "mark_superseded", "superseded_note"]

_log = structlog.get_logger(__name__)

#: Metadata key: ``(successor title, link date YYYY-MM-DD)`` of the newest successor. A working key, like
#: ``context_annotations.OWNER_FACTS_KEY``.
SUPERSEDED_KEY = "_superseded_by"
#: Added to a superseded hit's effective distance through a negative ``topic_boost``: enough to sink it below a
#: current hit of similar distance, small enough to keep it on the page.
DISTANCE_PENALTY = 0.1
#: Printed in place of a sensitive successor's title.
WITHHELD_TITLE = "a withheld document"
TTL_S = 300.0
FAILED_TTL_S = 60.0
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


def _catalog_key(catalog: Any) -> Any:
    """Every ``_SharedServiceCatalogHandle`` in a process forwards to the one shared client, so they share a slot;
    any other catalog (a direct client, a test double) is its own."""
    from nexus.catalog.factory import _SharedServiceCatalogHandle  # noqa: PLC0415 — deferred, like the catalog itself

    return _SharedServiceCatalogHandle if isinstance(catalog, _SharedServiceCatalogHandle) else id(catalog)


def _superseded_map(catalog: Any, now: float) -> dict[str, list[tuple[str, str]]] | None:
    """``{superseded tumbler: [(successor tumbler, link date), ...]}``, cached for :data:`TTL_S` (a failed read for
    :data:`FAILED_TTL_S`); None when the set could not be read whole."""
    key = _catalog_key(catalog)
    with _lock:
        if _cache.get("key") == key and now - _cache.get("at", -1e18) < _cache.get("ttl", TTL_S):
            return _cache.get("map")
    try:
        out = _read_links(catalog)
    except Exception:
        with _lock:
            _cache.update({"key": key, "at": now, "map": None, "ttl": FAILED_TTL_S})
        raise
    with _lock:
        _cache.update({"key": key, "at": now, "map": out, "ttl": TTL_S})
    return out


def _read_links(catalog: Any) -> dict[str, list[tuple[str, str]]] | None:
    out: dict[str, list[tuple[str, str]]] | None = {}
    seen = 0
    offset = 0
    while True:
        page = catalog.link_query(link_type="supersedes", limit=_PAGE, offset=offset)
        for link in page:
            out.setdefault(str(link.to_tumbler), []).append(  # type: ignore[union-attr] — not None inside the loop
                (str(link.from_tumbler), _date(link.created_at)))
        seen += len(page)
        if len(page) < _PAGE:
            break
        if seen >= MAX_LINKS:
            _log.warning("supersession_link_set_over_cap", cap=MAX_LINKS)
            out = None  # a partial set would mark some superseded hits and miss others
            break
        offset += _PAGE
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
            if len(r.metadata.get("_owner_doc_ids") or ()) > OWNER_RESOLVE_CAP:
                continue  # owners past the cap are unread, and one of them may be current
            if r.metadata.get("_owner_lookup_failed") is True:
                continue  # the owners are unknown, so "every owner is superseded" cannot be shown
            owners = owners_of(r)
            if owners and all(o in superseded for o in owners):
                hits.append((r, [s for o in owners for s in superseded[o]]))
        if not hits:
            return results
        successors = sorted({t for _, succ in hits for t, _d in succ})
        titles = {
            t: WITHHELD_TITLE if getattr(e, "sensitive", False) else (getattr(e, "title", "") or "")
            for t, e in (catalog.resolve_many(successors) or {}).items()
        }
        for r, succ in hits:
            tumbler, when = max(succ, key=lambda s: s[1])  # the newest link names the current successor
            r.metadata[SUPERSEDED_KEY] = (titles.get(tumbler) or tumbler, when)
            r.topic_boost -= DISTANCE_PENALTY
    except Exception:  # noqa: BLE001 — best-effort, like the link boost: a failed lookup leaves the results as they were
        _log.warning("supersession_lookup_failed", exc_info=True)
    return results


def superseded_note(metadata: Mapping[str, Any]) -> str:
    """``superseded by <title> (linked <date>)`` for a marked hit, else ``""``. The date is when the catalog link was
    made, not when the successor took effect."""
    value = metadata.get(SUPERSEDED_KEY)
    if not value:
        return ""
    title, when = value
    return f"superseded by {title}" + (f" (linked {when})" if when else "")
