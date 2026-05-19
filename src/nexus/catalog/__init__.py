# SPDX-License-Identifier: AGPL-3.0-or-later

import threading
from pathlib import Path
from typing import Any

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
    "open_catalog",
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

# RDR-112 6shq.2 (nexus-3gdg): process-singleton T2Client for daemon
# mode. Every ``open_catalog`` call under daemon mode would otherwise
# spawn a fresh ``T2Client`` (one socket pool per Catalog construction).
# CLI verbs that don't cache their Catalog (the common case for
# ``_get_catalog`` flows) would then leak the underlying sockets until
# process exit; in test sequences that spin up + tear down a T2 daemon
# repeatedly, the orphan client sockets prevent the daemon's
# ``server.wait_closed`` from completing within its 5 s timeout.
# Sharing one T2Client across Catalog instances mirrors the T3
# singleton pattern at ``mcp_infra.get_t3`` and lets ``reset_cache``
# close the client deterministically.
_t2_client: Any = None
_t2_client_lock = threading.Lock()


def open_catalog(cat_path: Path) -> Catalog:
    """Return a fresh, daemon-aware Catalog instance for *cat_path*.

    RDR-112 6shq.2 (nexus-3gdg): the shared construction seam for CLI
    verbs that need their own Catalog handle (write-heavy paths,
    short-lived helpers, ``nx catalog setup``, the doc-id-coverage
    audit). Read-mostly helpers (per-file lookups during indexing)
    should prefer :func:`open_cached`, which keys on
    ``(cat_path, mode)`` and reuses the constructed instance.

    Under ``NX_STORAGE_MODE=daemon`` builds the Catalog with an
    ``ExecuteProxy`` over a process-singleton ``T2Client`` (see
    :func:`_get_t2_client`). Multiple ``open_catalog`` calls under
    daemon mode return distinct Catalog wrappers but share one
    underlying socket pool; the daemon serialises writes via its own
    lock, so callers do not need to coordinate. The singleton is
    torn down by :func:`reset_cache`. Under direct mode constructs a
    fresh ``CatalogDB`` over ``cat_path / ".catalog.db"`` (the legacy
    path).

    Fail-loud-on-missing-daemon: in daemon mode with no daemon running,
    ``t2_ctx()`` raises ``DaemonNotRunningError`` (a ``RuntimeError``
    subclass). 3gdg review IMPORTANT-1: callers that cannot tolerate a
    missing daemon must catch ``RuntimeError`` and translate to
    ``click.ClickException``; otherwise the operator sees a Python
    traceback rather than a clean error line.
    """
    from nexus.db import is_daemon_mode

    cat_path = Path(cat_path)
    if is_daemon_mode():
        from nexus.catalog.catalog_proxy import ExecuteProxy
        t2 = _get_t2_client()
        return Catalog(cat_path, cat_path / ".catalog.db", db=ExecuteProxy(t2))
    return Catalog(cat_path, cat_path / ".catalog.db")


def _get_t2_client():
    """Return the process-singleton T2Client for daemon mode.

    Lazy-constructed on first call; subsequent calls reuse the same
    instance so multiple Catalogs in one process share one socket
    pool. ``reset_cache`` closes it.
    """
    global _t2_client
    if _t2_client is not None:
        return _t2_client
    with _t2_client_lock:
        if _t2_client is not None:
            return _t2_client
        from nexus.mcp_infra import t2_ctx
        _t2_client = t2_ctx()
        return _t2_client


def open_cached(cat_path: Path) -> Catalog:
    """Return a process-cached Catalog instance for *cat_path*.

    Thread-safe; constructs once per ``(cat_path, mode)`` pair. Subsequent
    calls bypass the expensive ``_ensure_consistent`` rebuild that fires
    on every fresh construction. Read-only helpers (per-file doc_id
    lookups, frecency map building, etc.) should use this instead of
    ``Catalog(path, path / ".catalog.db")`` to avoid lock contention
    storms.

    Under ``NX_STORAGE_MODE=daemon`` the cached Catalog is constructed
    via :func:`open_catalog`, which uses the process-singleton
    ``T2Client`` from :func:`_get_t2_client`. The singleton is shared
    across every Catalog opened under daemon mode in this process, not
    pinned per-cache-entry; ``reset_cache()`` tears it down.

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
        # RDR-112 6shq.2 (nexus-3gdg): single construction seam.
        # ``open_catalog`` encapsulates the direct-vs-daemon decision
        # so the cache reuses the same logic as one-shot CLI verbs.
        inst = open_catalog(cat_path)
        _cached[key] = inst
        return inst


def reset_cache() -> None:
    """Drop the process-level Catalog cache. Used by tests + the doctor
    rebuild path that wants a fresh consistency check after a known
    catalog mutation in another process.

    RDR-112 6shq.2 (nexus-3gdg): also tears down the shared T2Client
    singleton used by ``open_catalog`` so test sequences that stop and
    restart a T2 daemon between cases do not leak socket pools holding
    the previous daemon's connection open (which would block the new
    daemon's ``server.wait_closed`` at teardown).

    RDR-112 6shq.6 (nexus-chak): close the two-lock gap flagged in
    the 3gdg review. Hold ``_cache_lock`` across the inner
    ``_t2_client_lock`` acquisition so a concurrent
    ``open_cached`` / ``open_catalog`` cannot observe a cleared
    cache alongside a not-yet-nulled singleton, cache a fresh
    Catalog backed by the about-to-die client, and then strand
    that Catalog with a dead proxy. Lock order matches the
    ``open_cached`` -> ``open_catalog`` -> ``_get_t2_client`` chain
    (``_cache_lock`` then ``_t2_client_lock``), so no deadlock.
    The actual ``client.close()`` runs outside both locks because
    the socket close is a slow I/O call and nothing else needs the
    locks held during it.
    """
    global _t2_client
    with _cache_lock:
        _cached.clear()
        with _t2_client_lock:
            client = _t2_client
            _t2_client = None
    if client is not None:
        try:
            client.close()
        except Exception:
            pass


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
