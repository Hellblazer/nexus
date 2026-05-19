# SPDX-License-Identifier: AGPL-3.0-or-later

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
    "open_catalog",
    "open_cached",
    "reset_cache",
    "resolve_tumbler",
]


# RDR-118 P1.S2 (nexus-2bino): the process-level Catalog cache and
# T2Client socket pool that used to live as module-level mutables moved
# onto ``nexus.runtime.NexusRuntime``. The module-level entry points
# below are thin redirectors that resolve through ``_ensure_runtime_for_shim``
# so any CLI/MCP entry point that constructs an explicit runtime gets the
# benefit immediately; legacy callers without a runtime in context still
# work via a lazy process-default the resolver builds from env. Phase 4
# (``nexus-yi9mq``) deletes both the redirectors and the process-default
# fallback once every entry point owns its own runtime construction.
#
# Cache keying remains ``(cat_path, mode)`` (RDR-118 A4 S2 preservation,
# RDR-112 6shq.1). The double-checked-locking semantics of the legacy
# ``_cached`` access live on ``NexusRuntime._get_cached_catalog``;
# ``reset_cache`` continues to tear down the shared T2Client deterministically
# (RDR-112 6shq.2 / 6shq.6).


def open_catalog(cat_path: Path) -> Catalog:
    """Return a fresh, daemon-aware Catalog instance for *cat_path*.

    RDR-118 P1.S2 thin redirector. Resolves through
    ``nexus.runtime._ensure_runtime_for_shim()`` which prefers the
    ContextVar runtime when set, else a lazy-built process-default.
    The fresh-construction contract (no cache) is preserved by routing
    to ``runtime.fresh_catalog`` rather than ``runtime.get_catalog``.

    Under ``NX_STORAGE_MODE=daemon`` the returned Catalog wraps an
    ``ExecuteProxy`` over the runtime's shared T2Client. Fail-loud
    semantics carry through verbatim: in daemon mode with no daemon
    running, ``T2Client`` construction raises ``DaemonNotRunningError``.
    """
    from nexus.runtime import _ensure_runtime_for_shim

    return _ensure_runtime_for_shim().fresh_catalog(Path(cat_path))


def open_cached(cat_path: Path) -> Catalog:
    """Return a process-cached Catalog instance for *cat_path*.

    RDR-118 P1.S2 thin redirector. Routes to
    ``runtime.get_catalog(cat_path)`` on the runtime resolved by
    ``_ensure_runtime_for_shim``. Cache keying remains
    ``(cat_path, mode)`` (A4 S2 preservation) so direct-mode and
    daemon-mode Catalog instances do not alias.

    Read-only helpers (per-file doc_id lookups, frecency map building,
    etc.) should prefer this accessor to avoid the
    ``_ensure_consistent`` rebuild storm on every ``Catalog(...)``
    construction.
    """
    from nexus.runtime import _ensure_runtime_for_shim

    return _ensure_runtime_for_shim().get_catalog(Path(cat_path))


def reset_cache() -> None:
    """Drop the process-level Catalog cache and tear down any shared
    T2Client.

    RDR-118 P1.S2: clears the cache of the active ContextVar runtime
    when one is set, then tears down the process-default runtime so a
    subsequent access reads current env. Tests call this between cases
    to escape per-test ``NEXUS_CATALOG_PATH`` redirections; the
    behaviour preserved verbatim from the legacy ``reset_cache`` is
    that the next ``open_cached`` constructs against the new path.

    Under daemon mode this also closes the underlying T2Client socket
    pool, mirroring the RDR-112 6shq.2 / 6shq.6 semantics: the daemon's
    ``server.wait_closed`` cannot complete while a stray T2Client holds
    a connection open.
    """
    from nexus.runtime import _close_process_default, _runtime_var

    rt = _runtime_var.get()
    if rt is not None and not rt._closed:
        with rt._cache_lock:
            rt._cached.clear()
            with rt._t2_client_lock:
                client = rt._t2_client
                rt._t2_client = None
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
    _close_process_default()


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
