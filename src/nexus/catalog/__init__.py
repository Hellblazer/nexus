# SPDX-License-Identifier: AGPL-3.0-or-later

import threading
from pathlib import Path

from nexus.catalog.catalog import Catalog, CatalogEntry, CatalogLink
from nexus.catalog.tumbler import (
    DocumentRecord,
    LinkRecord,
    OwnerRecord,
    Tumbler,
)

__all__ = [
    "Catalog",
    "CatalogEntry",
    "CatalogLink",
    "DocumentRecord",
    "LinkRecord",
    "OwnerRecord",
    "Tumbler",
    "open_cached",
    "reset_cache",
    "resolve_tumbler",
]


# Process-level Catalog cache (nexus-6xqk follow-up).
# Helpers like ``doc_indexer._lookup_existing_doc_id`` and
# ``commands/enrich._build_catalog_doc_id_lookup`` are read-mostly and
# get called per-file during a force-reindex. Each ``Catalog(...)``
# construction triggers ``_ensure_consistent`` which DELETE-and-rebuilds
# from events.jsonl; multiple constructions per second contend on the
# SQLite write lock and emit ``catalog_consistency_rebuild_failed`` storms.
# A cached singleton per ``cat_path`` lets these helpers reuse one
# Catalog instance instead of opening fresh ones.
_cached: dict[Path, Catalog] = {}
_cache_lock = threading.Lock()


def open_cached(cat_path: Path) -> Catalog:
    """Return a process-cached Catalog instance for *cat_path*.

    Thread-safe; constructs once and reuses. Subsequent calls bypass the
    expensive ``_ensure_consistent`` rebuild that fires on every fresh
    construction. Read-only helpers (per-file doc_id lookups, frecency
    map building, etc.) should use this instead of ``Catalog(path,
    path / ".catalog.db")`` to avoid lock contention storms.

    Callers that mutate the catalog should NOT use this accessor; they
    need a fresh instance because the singleton may have stale internal
    state (other writers in this process or peers). Mutators are rare;
    the cache is the right default for read-side accessors.
    """
    cat_path = Path(cat_path)
    inst = _cached.get(cat_path)
    if inst is not None:
        return inst
    with _cache_lock:
        inst = _cached.get(cat_path)
        if inst is not None:
            return inst
        inst = Catalog(cat_path, cat_path / ".catalog.db")
        _cached[cat_path] = inst
        return inst


def reset_cache() -> None:
    """Drop the process-level Catalog cache. Used by tests + the doctor
    rebuild path that wants a fresh consistency check after a known
    catalog mutation in another process.
    """
    with _cache_lock:
        _cached.clear()


def resolve_tumbler(
    cat: Catalog, value: str
) -> tuple[Tumbler | None, str | None]:
    """Resolve a tumbler string OR title/filename to a ``(Tumbler, None)`` pair.

    Returns ``(None, error_message)`` on failure.
    """
    try:
        t = Tumbler.parse(value)
        if cat.resolve(t) is not None:
            return t, None
        return None, f"Not found: {value!r}"
    except ValueError:
        pass
    results = cat.find(value)
    if results:
        exact = [r for r in results if r.title == value]
        if exact:
            return exact[0].tumbler, None
        if len(results) == 1:
            return results[0].tumbler, None
        return None, f"Ambiguous: {len(results)} documents match {value!r} — use tumbler"
    return None, f"Not found: {value!r}"
