# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-229 P2.9 (nexus-p2m1x.14): routing by content type reads the collection's catalog row.

A router that sends a collection to an extractor, a threshold, an identity scheme or a chunk type asks
the row for its ``content_type``; it never tests the name against ``code__``, ``docs__``, ``rdr__`` or
``knowledge__`` (RDR-229 Technical Design item 12). A caller holding a listing row reads the key from
that row. A caller holding only a name reads the client row cache, :func:`nexus.mcp_infra.get_collection_row`.

A name no row backs answers ``None``: it is not any content type, so it routes nowhere, the bucket an
unrecognised name always fell into (``scoring``'s code detection has done the same since nexus-ft04v.26).
This module parses no name.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Collection, Mapping
from typing import Any

import structlog

_log = structlog.get_logger(__name__)


#: One re-read of the client row cache per this many seconds before NoRow is taken: not while the cache is
#: younger than this (a scan over any number of rowless names pays for at most one extra listing per interval),
#: and not twice for one name. The unreadable warning is likewise once per name per interval.
_NOROW_RECHECK_INTERVAL_S: float = 10.0
_norow_rechecked: dict[str, float] = {}
_unreadable_warned: dict[str, float] = {}
_norow_lock = threading.Lock()


def once_per_interval(seen: dict[str, float], name: str) -> bool:
    """True the first time *name* is seen in *seen* within the interval (and records it)."""
    now = time.monotonic()
    with _norow_lock:
        last = seen.get(name)
        if last is not None and now - last < _NOROW_RECHECK_INTERVAL_S:
            return False
        seen[name] = now
        if len(seen) > 4096:
            seen.clear()
            seen[name] = now
        return True


def norow_recheck_due(name: str) -> bool:
    """Whether to invalidate the row cache and read again before taking NoRow for *name*."""
    from nexus.mcp_infra import collections_cache_age  # noqa: PLC0415 — circular-dep avoidance (mcp_infra)

    age = collections_cache_age()
    if age is not None and age < _NOROW_RECHECK_INTERVAL_S:
        return False  # the cache was filled inside the interval: a miss now means no row as of then
    return once_per_interval(_norow_rechecked, name)


def read_row_for_write(name: str, *, db: Any = None) -> str | None:
    """The row's ``content_type`` for a router that WRITES (item 1's Row and NoRow outcomes), or ``None``.

    A name with no row gets one bounded cache re-read (:func:`norow_recheck_due`) before ``None``, so a
    collection whose first chunk landed after the cache was filled is found. A listing that cannot be read
    is the Unreadable outcome: ``None`` and a warning once per name per interval, never a silent miss.
    """
    from nexus.db.http_vector_client import VectorServiceError  # noqa: PLC0415 — circular-dep avoidance (http_vector_client)
    from nexus.db.service_endpoint import ServiceEndpointUnresolvableError  # noqa: PLC0415 — keep the module import light
    from nexus.mcp_infra import ServiceNotReadyError, invalidate_collections_cache  # noqa: PLC0415 — circular-dep avoidance (mcp_infra)

    try:
        content_type = row_content_type(name, db=db, strict=True)
        if content_type is None and norow_recheck_due(name):
            invalidate_collections_cache()
            content_type = row_content_type(name, db=db, strict=True)
    except (ServiceNotReadyError, VectorServiceError, ServiceEndpointUnresolvableError) as exc:
        # An endpoint that cannot be resolved (no service on this box yet) is a sibling of the two
        # listing errors, not a subclass: it is the same Unreadable outcome for a write router.
        if once_per_interval(_unreadable_warned, name):
            _log.warning("routing_content_type_unreadable", collection=name, error=f"{type(exc).__name__}: {exc}")
        return None
    return content_type


def row_content_type(name: str, *, db: Any = None, strict: bool = False) -> str | None:
    """The ``content_type`` of collection *name*'s catalog row, or ``None`` when no row backs it.

    *db*, when it has its own row resolver (``HttpVectorClient._resolve_collection_row``), reads through
    it, so a caller working for another tenant's client is never answered from this process's cache.

    *strict* makes a listing that could not be read raise (``ServiceNotReadyError`` or
    ``VectorServiceError``) instead of answering ``None``, for a caller that must not read "unreadable"
    as "no row": a read-only scan, or :func:`nexus.corpus.routing_content_type`, which tells the two apart.
    """
    resolver = getattr(db, "_resolve_collection_row", None) if db is not None else None
    if callable(resolver):
        row = resolver(name, strict=True) if strict else resolver(name)
    else:
        from nexus.mcp_infra import get_collection_row  # noqa: PLC0415 — circular-dep avoidance (mcp_infra)

        row = get_collection_row(name, strict=True) if strict else get_collection_row(name)
    return listing_content_type(row)


def listing_content_type(row: Mapping[str, Any] | None) -> str | None:
    """The ``content_type`` a catalog row or a ``list_collections()`` row carries; the key is absent
    from a listing row when no catalog row backs the collection."""
    value = row.get("content_type") if row else None
    return value if isinstance(value, str) and value else None


def taxonomy_local_exclude_types() -> list[str]:
    """The content types the local-mode taxonomy hooks skip; empty outside local mode.

    ``taxonomy.local_exclude_collections`` holds glob patterns that name LABELS and stay label patterns.
    ``taxonomy.local_exclude_content_types`` holds content types, matched against the row (shipped default
    ``["code"]``: general-purpose local embeddings cluster source code poorly, and cloud mode uses
    ``voyage-code-3``). Set both to ``[]`` to run taxonomy over every collection.
    """
    from nexus.config import is_local_mode, load_config  # noqa: PLC0415 — deferred: config import is heavier than this module's callers need at load

    if not is_local_mode():
        return []
    return list(load_config().get("taxonomy", {}).get("local_exclude_content_types", []))


def content_type_excluded(name: str, content_types: Collection[str], *, content_type: str | None = None) -> bool:
    """True when *name*'s row says one of *content_types*.

    *content_type* is the type the caller already read from a listing row; omit it to read the client row cache.
    """
    if not content_types:
        return False
    return (content_type if content_type is not None else row_content_type(name)) in content_types
