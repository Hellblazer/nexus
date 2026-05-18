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
# A cached singleton per ``(cat_path, mode)`` lets these helpers reuse
# one Catalog instance instead of opening fresh ones.
#
# RDR-112 6shq.1 (nexus-lj2l): the cache key is ``(cat_path, mode)`` so
# direct-mode and daemon-mode Catalog instances do not alias. Test
# sequences that flip ``NX_STORAGE_MODE`` across cases would otherwise
# return the previous-mode instance and read against the wrong backend.
_cached: dict[tuple[Path, str], Catalog] = {}
_cache_lock = threading.Lock()


def open_cached(cat_path: Path) -> Catalog:
    """Return a process-cached Catalog instance for *cat_path*.

    Thread-safe; constructs once per ``(cat_path, mode)`` pair. Subsequent
    calls bypass the expensive ``_ensure_consistent`` rebuild that fires
    on every fresh construction. Read-only helpers (per-file doc_id
    lookups, frecency map building, etc.) should use this instead of
    ``Catalog(path, path / ".catalog.db")`` to avoid lock contention
    storms.

    Under ``NX_STORAGE_MODE=daemon`` the cached Catalog is constructed
    with an ``ExecuteProxy`` over a ``T2Client`` resolved via
    ``mcp_infra.t2_ctx()``. The T2Client stays alive for the cache
    entry's lifetime; ``reset_cache()`` tears down both the proxy and
    the underlying client pool.

    Callers that mutate the catalog should NOT use this accessor; they
    need a fresh instance because the singleton may have stale internal
    state (other writers in this process or peers). Mutators are rare;
    the cache is the right default for read-side accessors.
    """
    from nexus.db import is_daemon_mode

    cat_path = Path(cat_path)
    mode = "daemon" if is_daemon_mode() else "direct"
    key = (cat_path, mode)
    inst = _cached.get(key)
    if inst is not None:
        return inst
    with _cache_lock:
        inst = _cached.get(key)
        if inst is not None:
            return inst
        if mode == "daemon":
            from nexus.catalog.catalog_proxy import ExecuteProxy
            from nexus.mcp_infra import t2_ctx
            # ``t2_ctx()`` returns a ``T2Client`` in daemon mode (a
            # T2Database in direct mode, but we have already branched
            # on ``mode``). Hold the client reference on the proxy so
            # the connection pool survives the cache lifetime.
            t2 = t2_ctx()
            inst = Catalog(cat_path, cat_path / ".catalog.db", db=ExecuteProxy(t2))
        else:
            inst = Catalog(cat_path, cat_path / ".catalog.db")
        _cached[key] = inst
        return inst


def reset_cache() -> None:
    """Drop the process-level Catalog cache. Used by tests + the doctor
    rebuild path that wants a fresh consistency check after a known
    catalog mutation in another process.

    RDR-112 6shq.1 (nexus-lj2l): under daemon mode the cached Catalog
    holds an ``ExecuteProxy`` that pins a ``T2Client`` (and its socket
    pool). Closing the client on eviction prevents leaked connections
    across rapid test resets. Failures during close are swallowed —
    eviction must remain idempotent for safety paths.
    """
    with _cache_lock:
        for cat in _cached.values():
            db = getattr(cat, "_db", None)
            client = getattr(db, "_t2", None)
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass
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
