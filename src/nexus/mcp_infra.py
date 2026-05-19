# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""MCP server infrastructure: singletons, caching, test injection.

Separated from tool definitions (mcp_server.py) to isolate concerns.
"""
from __future__ import annotations

import os
import threading
import time
import warnings

from nexus.config import default_db_path

# ── Lazy singletons ──────────────────────────────────────────────────────────

_t1_instance = None
_t1_isolated = False
_t1_lock = threading.Lock()

_t3_instance = None
_t3_lock = threading.Lock()

_collections_cache: tuple[list[str], float] = ([], 0.0)
_COLLECTIONS_CACHE_TTL = 60.0

# RDR-118 P1.S3 (nexus-ipyfj): the catalog singleton moved onto
# ``nexus.runtime.NexusRuntime``. ``get_catalog`` below is now a thin
# shim resolving through ``_ensure_runtime_for_shim``; the legacy
# module-level names below stay as compatibility surfaces so the
# autouse fixture ``_restore_post_store_batch_hooks_after_test`` and
# the public ``inject_catalog`` / ``reset_singletons`` test helpers
# continue to function unchanged. Phase 4 (``nexus-yi9mq``) deletes
# both the compat names and the fixture once every caller owns its
# own runtime construction.
_catalog_instance = None
_catalog_lock = threading.Lock()
_catalog_mtime: float = 0.0

# ── Search trace cache (RDR-061 E2) ──────────────────────────────────────────
# Session-keyed cache of recent search results. Populated by the search tool,
# consumed by store_put and catalog_link to correlate agent actions with the
# queries that likely led to them.
_search_traces: dict[str, list[dict]] = {}
_search_traces_lock = threading.Lock()
_SEARCH_TRACE_TTL_SECONDS = 600  # 10 minutes
_SEARCH_TRACE_MAX_PER_SESSION = 20


def record_search_trace(
    session_id: str,
    query: str,
    chunks: list[tuple[str, str]],
) -> None:
    """Record a search result set for later correlation (RDR-061 E2).

    chunks: list of (chunk_id, collection) tuples from the search results.
    """
    if not session_id or not chunks:
        return
    trace = {
        "query": query,
        "chunks": chunks,
        "timestamp": time.monotonic(),
    }
    with _search_traces_lock:
        bucket = _search_traces.setdefault(session_id, [])
        bucket.append(trace)
        # Trim old entries (both by age and count)
        now = time.monotonic()
        trimmed = [
            t for t in bucket
            if now - t["timestamp"] < _SEARCH_TRACE_TTL_SECONDS
        ][-_SEARCH_TRACE_MAX_PER_SESSION:]
        if trimmed:
            _search_traces[session_id] = trimmed
        else:
            _search_traces.pop(session_id, None)


def get_recent_search_traces(session_id: str) -> list[dict]:
    """Return non-expired search traces for this session (RDR-061 E2).

    Evicts the session key if all traces have expired.
    """
    if not session_id:
        return []
    with _search_traces_lock:
        bucket = _search_traces.get(session_id, [])
        now = time.monotonic()
        alive = [t for t in bucket if now - t["timestamp"] < _SEARCH_TRACE_TTL_SECONDS]
        if alive:
            if len(alive) != len(bucket):
                _search_traces[session_id] = alive
            return alive
        # All expired — evict the key
        _search_traces.pop(session_id, None)
        return []


def clear_search_traces() -> None:
    """Clear all search traces (test helper)."""
    with _search_traces_lock:
        _search_traces.clear()


def get_t1():
    """Return (T1Database, is_isolated), lazy init on first call.

    Post-RDR-105 P4 the FastMCP lifespan owns chroma's lifecycle in
    full (spawn, addr-file publish, ``_t1_state.T1_ADDR``, cleanup);
    no get-t1-side init is needed. By the time any MCP tool fires,
    Claude Code has run the SessionStart hook AND the lifespan has
    completed its `__aenter__`, so the addr file (or env vars) are
    already in place for ``T1Database()`` to read. ``T1Database``'s
    four-branch fail-loud gate raises ``T1ServerNotFoundError`` if
    the lifespan did not run for any reason, which surfaces a clear
    error rather than silently degrading.
    """
    global _t1_instance, _t1_isolated
    if _t1_instance is None:
        with _t1_lock:
            if _t1_instance is None:
                with warnings.catch_warnings(record=True) as caught:
                    warnings.simplefilter("always")
                    from nexus.db.t1 import T1Database
                    _t1_instance = T1Database()
                    _t1_isolated = any("EphemeralClient" in str(w.message) for w in caught)
    return _t1_instance, _t1_isolated


def get_t3():
    """Return T3Database singleton — lazy init on first call.

    RDR-112 P3.1 (nexus-hpxl): under ``NX_STORAGE_MODE=daemon`` the
    returned T3Database wraps a ``chromadb.HttpClient`` pointed at the
    running ``nx daemon t3`` (via ``make_t3_client``). Under
    ``NX_STORAGE_MODE=direct`` the existing direct-mode path is used.

    Fail-loud-on-missing-daemon: in daemon mode with no daemon running,
    ``make_t3_client`` raises ``T3DaemonError`` with a recovery hint.
    No auto-spawn (matches T2 contract per RDR-112 §Incremental
    adoption "no auto-spawn in daemon mode").
    """
    global _t3_instance
    if _t3_instance is None:
        with _t3_lock:
            if _t3_instance is None:
                from nexus.db import is_daemon_mode
                if is_daemon_mode():
                    from nexus.daemon.t3_client import make_t3_client
                    _t3_instance = make_t3_client()
                else:
                    from nexus.db import make_t3
                    _t3_instance = make_t3()
    return _t3_instance


def get_collection_names() -> list[str]:
    """Return cached T3 collection names, refreshing every _COLLECTIONS_CACHE_TTL seconds."""
    global _collections_cache
    names, ts = _collections_cache
    now = time.monotonic()
    if now - ts > _COLLECTIONS_CACHE_TTL:
        new_names = [c["name"] for c in get_t3().list_collections()]
        _collections_cache = (new_names, now)
        return new_names
    return names


def t2_ctx(*, _path_resolver=None):
    """Return a T2Database context manager — fresh per call.

    RDR-112 P0.4 (nexus-uqqy): the MCP server is the explicit migration
    owner for its T2 path in Phase 0. ``run_if_needed`` is idempotent
    via the process-level upgrade cache, so calling it on every factory
    invocation is cheap after the first hit.

    Resolves ``default_db_path`` via this module's binding so test
    fixtures that patch ``nexus.mcp_infra.default_db_path`` continue
    to take effect (RDR-112 P0.5, nexus-oi0z).

    ``_path_resolver`` (RDR-112 P0-gate, nexus-cy3o): optional callable
    overriding the path-resolution step. Used by
    ``nexus.commands.taxonomy_cmd._t2_ctx`` to delegate here while
    preserving the long-standing test pattern of patching
    ``nexus.commands.taxonomy_cmd._default_db_path``. Production
    callers leave this ``None`` and the module-level
    ``default_db_path`` binding wins.
    """
    from nexus.db import is_daemon_mode as _is_daemon_mode

    # RDR-112 P1 prereq (foundation review, 2026-05-14): under daemon
    # mode the daemon owns the T2 path; a client-side ``_path_resolver``
    # override is meaningless and dangerous (it would silently pick a
    # different file). Reject so the contract is loud, not surprising.
    if _path_resolver is not None and _is_daemon_mode():
        raise RuntimeError(
            "t2_ctx(_path_resolver=...) is incompatible with "
            "NX_STORAGE_MODE=daemon: the daemon owns the path. "
            "Clear _path_resolver or run in direct mode."
        )

    # RDR-112 P3.1 (nexus-hpxl): under daemon mode return a T2Client
    # bound to the discovery-resolved address. The previous behaviour
    # (returning T2Database even when daemon-mode was active) raced
    # the daemon's writer on memory.db — fixed here.
    if _is_daemon_mode():
        from pathlib import Path
        from nexus.daemon.discovery import discovery_resolve
        from nexus.daemon.t2_client import T2Client

        addr = discovery_resolve("t2")
        # Prefer UDS when present (per RDR-112 §6: UDS-then-TCP fallback).
        uds = addr.get("uds_path")
        if uds:
            return T2Client(uds_path=Path(uds))
        host = addr.get("tcp_host")
        port = addr.get("tcp_port")
        if isinstance(host, str) and isinstance(port, int):
            return T2Client(tcp_addr=(host, port))
        # discovery_resolve raised DaemonNotRunningError above if
        # nothing resolved; reaching here means the resolver returned
        # a malformed payload. Fail loud rather than silently fall
        # through to direct-mode (which would race the daemon).
        raise RuntimeError(
            f"discovery_resolve('t2') returned a payload lacking both "
            f"uds_path and tcp_host/tcp_port: {addr!r}. Restart the "
            f"daemon: `nx daemon t2 stop && nx daemon t2 start`."
        )

    # Direct mode: existing behaviour preserved verbatim.
    from nexus.db.migrations import run_if_needed
    from nexus.db.t2 import T2Database

    path = (_path_resolver or default_db_path)()
    run_if_needed(path)
    return T2Database(path)


# ── T1 plan session cache (RDR-078) ──────────────────────────────────────────
# Singleton wrapper around PlanSessionCache; shared across MCP tool calls in
# one process.  reset_plan_cache_for_tests() tears it down between test cases.
#
# Three states:
#   None                      — not yet initialised, eligible for init attempt
#   _PLAN_CACHE_UNAVAILABLE   — init failed; callers short-circuit to FTS5
#                               without taking the lock on every call
#   PlanSessionCache instance — healthy, available for matching

_PLAN_CACHE_UNAVAILABLE = object()  # sentinel — distinct from None

_plan_cache_instance = None
_plan_cache_lock = threading.Lock()
_plan_cache_populated: bool = False
#: SQLite file mtime captured at the most recent populate. Used by the
#: mtime-guarded refresh in :func:`get_t1_plan_cache` to detect plan
#: library mutation (a re-seeded builtin, an added or deleted row)
#: without requiring an MCP-server restart. nexus-qgjr.
_plan_cache_mtime: float = 0.0


def _plan_library_mtime(library) -> float:
    """Return the SQLite file mtime for *library*, or 0.0 when unknown.

    RDR-112 P0.1 (nexus-j07g): delegates to ``library.plans_mtime()``
    when present so daemon-mode swaps (Phase 1) can supply the watermark
    without exposing a ``path`` attribute. Test stubs / in-memory
    libraries that expose neither method nor ``path`` get the
    legacy 0.0 fallback (repopulate-never-runs).
    """
    mtime_fn = getattr(library, "plans_mtime", None)
    if callable(mtime_fn):
        try:
            mtime = mtime_fn()
        except OSError:
            mtime = None
        return float(mtime) if mtime is not None else 0.0
    # Legacy fallback: pre-encapsulation stubs without plans_mtime().
    path = getattr(library, "path", None)
    if path is None:
        return 0.0
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def get_t1_plan_cache(*, populate_from=None):
    """Return the T1 ``plans__session`` cache, lazy-populated on first call.

    When *populate_from* (a PlanLibrary) is supplied, the cache is
    populated from its rows on first call and **repopulated whenever
    the underlying SQLite file mtime advances** (nexus-qgjr). The mtime
    check mirrors the catalog's ``_last_consistency_mtime`` pattern at
    ``catalog.py:405`` — cheap when nothing changed, rebuilds when a
    write moves the file's stat-time.

    Libraries without a ``path`` attribute fall back to populate-once
    semantics; the mtime tier costs them nothing.

    Returns ``None`` when no T1 client is reachable — the matcher falls
    back to FTS5 in that case. Subsequent calls after an init failure
    return ``None`` immediately without re-entering the lock (see
    sentinel above).
    """
    global _plan_cache_instance, _plan_cache_populated, _plan_cache_mtime
    if _plan_cache_instance is _PLAN_CACHE_UNAVAILABLE:
        return None
    if _plan_cache_instance is None:
        with _plan_cache_lock:
            if _plan_cache_instance is None:
                try:
                    t1, _ = get_t1()
                    from nexus.plans.session_cache import PlanSessionCache
                    _plan_cache_instance = PlanSessionCache(
                        client=t1._client, session_id=t1.session_id,
                    )
                except Exception:
                    _plan_cache_instance = _PLAN_CACHE_UNAVAILABLE
    if _plan_cache_instance is _PLAN_CACHE_UNAVAILABLE:
        return None
    if populate_from is not None:
        current_mtime = _plan_library_mtime(populate_from)
        with _plan_cache_lock:
            stale = (
                not _plan_cache_populated
                or (current_mtime > 0.0 and current_mtime > _plan_cache_mtime)
            )
            if stale:
                try:
                    _plan_cache_instance.populate(populate_from)
                finally:
                    _plan_cache_populated = True
                    _plan_cache_mtime = current_mtime
    return _plan_cache_instance


def reset_plan_cache_for_tests() -> None:
    """Test helper: drop the cache singleton so the next call re-init."""
    global _plan_cache_instance, _plan_cache_populated, _plan_cache_mtime
    with _plan_cache_lock:
        _plan_cache_instance = None
        _plan_cache_populated = False
        _plan_cache_mtime = 0.0


# ── Catalog management ────────────────────────────────────────────────────────


def _max_jsonl_mtime(cat) -> float:
    """Return max mtime across every catalog file written by a mutator.

    Includes ``events.jsonl`` (RDR-101 Phase 3) — without it, a
    cross-process event-sourced write would not invalidate this
    process's catalog cache and the singleton would serve stale state.
    """
    mtime = 0.0
    for path in cat.mtime_paths():
        try:
            mtime = max(mtime, path.stat().st_mtime) if path.exists() else mtime
        except OSError:
            pass
    return mtime


def get_catalog():
    """Return the Catalog singleton or None if not initialised.

    RDR-118 P1.S3 thin redirector. Delegates to
    ``nexus.runtime._ensure_runtime_for_shim().get_catalog()`` which
    preserves the A4 S1 mtime-refresh path on the cached instance and
    the A4 S2 ``(cat_path, mode)`` cache keying.

    Legacy fallback: if the module-level ``_catalog_instance`` has been
    explicitly set (via ``inject_catalog`` or ``unittest.mock.patch``)
    it takes precedence over the runtime path. The mtime-refresh logic
    runs against the override for back-compat with tests that
    monkeypatch the override + ``_max_jsonl_mtime`` to assert the
    refresh path's error propagation. Phase 4 (``nexus-yi9mq``) deletes
    this fallback once the affected tests migrate to construct an
    explicit runtime.
    """
    global _catalog_instance, _catalog_mtime
    if _catalog_instance is not None:
        try:
            current_mtime = _max_jsonl_mtime(_catalog_instance)
            if current_mtime > _catalog_mtime:
                with _catalog_lock:
                    if current_mtime > _catalog_mtime:
                        _catalog_mtime = current_mtime
                        _catalog_instance._ensure_consistent()
        except OSError:
            pass
        return _catalog_instance
    from nexus.runtime import _ensure_runtime_for_shim

    return _ensure_runtime_for_shim().get_catalog()


def require_catalog():
    """Return (catalog, None) or (None, error_message)."""
    cat = get_catalog()
    if cat is None:
        return None, "Catalog not initialized — run 'nx catalog setup' to create and populate it"
    return cat, None


def catalog_auto_link(doc_id: str) -> int:
    """Create catalog links from T1 link-context to the just-stored document.

    Returns the number of links actually created (backward-compat int).
    Skip counts are surfaced via structlog: WARNING for invalid tumbler
    skips (recipe-compliance gap), DEBUG for missing endpoint skips
    (legitimate cleanup signal). nexus-a414 made these visible after
    the prior all-DEBUG behaviour silently swallowed every recipe-
    compliant call that produced zero links.
    """
    import structlog
    _log = structlog.get_logger()

    cat = get_catalog()
    if cat is None:
        return 0
    t1, _ = get_t1()
    entries = t1.list_entries()
    link_entries = [
        e for e in entries
        if "link-context" in {t.strip() for t in (e.get("tags") or "").split(",")}
    ]
    if not link_entries:
        return 0
    entry = cat.by_doc_id(doc_id)
    if entry is None:
        _log.debug("auto_link_skip_doc_not_in_catalog", doc_id=doc_id)
        return 0
    from nexus.catalog.auto_linker import auto_link, read_link_contexts
    contexts = read_link_contexts(link_entries)
    result = auto_link(cat, entry.tumbler, contexts)

    # nexus-a414: surface non-zero outcomes so operators see what's happening.
    # The all-zero case (no contexts) is already gated above. The interesting
    # case is contexts present + zero created — that's the silent failure mode
    # the bead exists for.
    if result.created or result.skipped_invalid_tumbler or result.skipped_missing_endpoint:
        recipe_compliant_zero = (
            result.created == 0
            and result.skipped_invalid_tumbler > 0
        )
        log_method = _log.warning if recipe_compliant_zero else _log.info
        log_method(
            "auto_link_summary",
            doc_id=doc_id,
            created=result.created,
            skipped_invalid_tumbler=result.skipped_invalid_tumbler,
            skipped_missing_endpoint=result.skipped_missing_endpoint,
            recipe_compliant_zero=recipe_compliant_zero,
        )
    return result.created


def resolve_tumbler_mcp(cat, value):
    """Resolve tumbler string OR title/filename. Returns (tumbler, None) or (None, error)."""
    from nexus.catalog import resolve_tumbler
    return resolve_tumbler(cat, value)


# ── Post-store hooks (RDR-070, nexus-7h2) ────────────────────────────────────
# Synchronous hooks that fire after every store_put. Exceptions are caught
# per-hook and logged, never propagated (same non-fatal pattern as auto_linker).
#
# RDR-118 P2.S1 (nexus-kekrs): the storage moved to
# ``NexusRuntime.hooks._single``; the functions and module-level name below
# are thin redirectors / a proxy that delegate to the runtime resolver.
# Phase 2 (``nexus-0zgb3``) retires the proxy + the legacy autouse fixture
# that snapshots it.


class _SingleHooksListProxy:
    """List-like view of the resolved runtime's single-chain hook list.

    Forwards every list operation to ``runtime.hooks._single`` so the
    legacy autouse ``_restore_post_store_batch_hooks_after_test``
    (which also snapshots / restores this list) and any direct-access
    test code continue to work. Phase 2 (``nexus-0zgb3``) deletes both
    the proxy and the fixture.
    """

    @staticmethod
    def _live() -> list:
        from nexus.runtime import _ensure_runtime_for_shim
        return _ensure_runtime_for_shim().hooks._single

    def __iter__(self):
        return iter(self._live())

    def __len__(self) -> int:
        return len(self._live())

    def __getitem__(self, key):
        return self._live()[key]

    def __setitem__(self, key, value) -> None:
        if isinstance(key, slice) and key == slice(None):
            self.clear()
            for fn in value:
                self.append(fn)
            return
        self._live()[key] = value

    def __contains__(self, item) -> bool:
        return item in self._live()

    def __eq__(self, other) -> bool:
        return list(self._live()) == list(other)

    def __repr__(self) -> str:
        return repr(self._live())

    def append(self, fn) -> None:
        from nexus.runtime import _ensure_runtime_for_shim
        _ensure_runtime_for_shim().hooks.register_single(fn)

    def extend(self, fns) -> None:
        for fn in fns:
            self.append(fn)

    def remove(self, fn) -> None:
        self._live().remove(fn)

    def clear(self) -> None:
        self._live().clear()


_post_store_hooks: _SingleHooksListProxy = _SingleHooksListProxy()


def register_post_store_hook(fn) -> None:
    """Register a single-doc hook on the resolved runtime's HookRegistry.

    RDR-118 P2.S1 thin redirector. ``_post_store_hooks.append(fn)``
    forwards to ``runtime.hooks.register_single(fn)`` via the proxy.
    """
    _post_store_hooks.append(fn)


def fire_post_store_hooks(doc_id: str, collection: str, content: str) -> None:
    """Invoke every single-doc hook in ``_post_store_hooks``.

    RDR-118 P2.S1: iterates the module-level proxy which forwards to
    the resolved runtime's HookRegistry. Legacy tests that replace
    ``_post_store_hooks`` via ``unittest.mock.patch`` with a regular
    list still drive the fire path (the proxy and a plain list are
    both iterable; the runtime registry stays untouched inside the
    patch's scope). Per-hook failure isolation + T2 ``hook_failures``
    persistence preserved verbatim from the legacy dispatcher.
    """
    import structlog
    _hook_log = structlog.get_logger()
    for hook in _post_store_hooks:
        try:
            hook(doc_id, collection, content)
        except Exception as exc:
            hook_name = getattr(hook, "__name__", "?")
            _hook_log.warning(
                "post_store_hook_failed",
                hook=hook_name,
                exc_info=True,
            )
            _record_hook_failure(
                doc_id=doc_id,
                collection=collection,
                hook_name=hook_name,
                error=str(exc),
            )


def _record_hook_failure(
    *,
    doc_id: str,
    collection: str,
    hook_name: str,
    error: str,
) -> None:
    """Persist a post-store hook failure to T2 ``hook_failures`` (GH #251).

    Populates ``chain='single'`` (RDR-089 4.14.2 schema) when the column
    is present, falling back to a chain-less insert on pre-4.14.2 DBs.
    Secondary best-effort path: if the T2 write itself fails, swallow
    the second failure (the primary warning already reached structlog)
    rather than mask the original hook exception.
    """
    try:
        with t2_ctx() as t2:
            t2.memory.record_hook_failure(
                doc_id=doc_id, collection=collection,
                hook_name=hook_name, error=error, chain="single",
            )
    except Exception:
        import structlog
        structlog.get_logger().debug(
            "hook_failure_persist_failed",
            hook=hook_name,
            collection=collection,
            exc_info=True,
        )


# ── Post-store batch hooks (RDR-095, nexus-wxcb) ─────────────────────────────
# Parallel batch-shape contract for bulk-ingest enrichment. Single-document
# hooks above fire from MCP store_put; batch hooks fire from CLI indexing
# paths where N docs land in one operation and consumers benefit from
# batched dependency calls (e.g. one ChromaDB query for N centroids vs N
# sequential queries). Same per-hook failure-isolation pattern: exceptions
# captured, persisted to T2 hook_failures, never propagated.
#
# RDR-118 P1.S3 (nexus-ipyfj): the storage moved to
# ``NexusRuntime.hooks._batch`` (and ``_batch_with_catalog_doc_id``); the
# functions and module-level names below are thin redirectors / proxies
# that delegate to the runtime resolver. Phase 2 (``nexus-0zgb3``) retires
# both the proxies and the autouse fixture that backs them.


class _BatchHooksListProxy:
    """List-like view of the resolved runtime's batch hook list.

    Existing code accesses ``mcp_infra._post_store_batch_hooks`` as a
    list (iterate, len, indexed access, slice assignment, append, clear,
    extend). After RDR-118 P1.S3 the underlying storage lives on the
    runtime's ``HookRegistry``; this proxy forwards every list operation
    to that storage so the autouse fixture
    ``_restore_post_store_batch_hooks_after_test`` and the few tests
    that mutate the list directly continue to work. Phase 2 deletes
    both the proxy and the fixture (bead ``nexus-0zgb3``).
    """

    @staticmethod
    def _live() -> list:
        from nexus.runtime import _ensure_runtime_for_shim
        return _ensure_runtime_for_shim().hooks._batch

    def __iter__(self):
        return iter(self._live())

    def __len__(self) -> int:
        return len(self._live())

    def __getitem__(self, key):
        return self._live()[key]

    def __setitem__(self, key, value) -> None:
        if isinstance(key, slice) and key == slice(None):
            self.clear()
            for fn in value:
                self.append(fn)
            return
        self._live()[key] = value

    def __contains__(self, item) -> bool:
        return item in self._live()

    def __eq__(self, other) -> bool:
        return list(self._live()) == list(other)

    def __repr__(self) -> str:
        return repr(self._live())

    def append(self, fn) -> None:
        from nexus.runtime import _ensure_runtime_for_shim
        _ensure_runtime_for_shim().hooks.register_batch(fn)

    def extend(self, fns) -> None:
        for fn in fns:
            self.append(fn)

    def remove(self, fn) -> None:
        from nexus.runtime import _ensure_runtime_for_shim
        rt = _ensure_runtime_for_shim()
        rt.hooks._batch.remove(fn)
        rt.hooks._batch_with_catalog_doc_id.discard(id(fn))

    def clear(self) -> None:
        from nexus.runtime import _ensure_runtime_for_shim
        rt = _ensure_runtime_for_shim()
        rt.hooks._batch.clear()
        rt.hooks._batch_with_catalog_doc_id.clear()


class _CatalogDocIdSetProxy:
    """Set-like view of the resolved runtime's ``catalog_doc_id`` classification.

    Same migration pattern as ``_BatchHooksListProxy``; forwards every
    set operation to ``runtime.hooks._batch_with_catalog_doc_id``.
    """

    @staticmethod
    def _live() -> set:
        from nexus.runtime import _ensure_runtime_for_shim
        return _ensure_runtime_for_shim().hooks._batch_with_catalog_doc_id

    def __iter__(self):
        return iter(self._live())

    def __len__(self) -> int:
        return len(self._live())

    def __contains__(self, item) -> bool:
        return item in self._live()

    def __eq__(self, other) -> bool:
        return set(self._live()) == set(other)

    def __repr__(self) -> str:
        return repr(self._live())

    def add(self, item) -> None:
        self._live().add(item)

    def discard(self, item) -> None:
        self._live().discard(item)

    def update(self, other) -> None:
        self._live().update(other)

    def clear(self) -> None:
        self._live().clear()


_post_store_batch_hooks: _BatchHooksListProxy = _BatchHooksListProxy()
#: Proxy view of the resolved runtime's ``_batch_with_catalog_doc_id`` set.
#: Populated indirectly by :func:`register_post_store_batch_hook` via the
#: runtime's signature-classification path (RDR-108 Phase 3 contract).
_post_store_batch_hooks_with_catalog_doc_id: _CatalogDocIdSetProxy = (
    _CatalogDocIdSetProxy()
)


def register_post_store_batch_hook(fn) -> None:
    """Register a batch hook on the resolved runtime's ``HookRegistry``.

    RDR-118 P1.S3 thin redirector. Appends to
    ``_post_store_batch_hooks`` (the proxy under normal operation;
    a patched list under ``unittest.mock.patch``) and performs the
    ``inspect.signature``-based ``catalog_doc_id`` classification
    (RDR-108 Phase 3) into ``_post_store_batch_hooks_with_catalog_doc_id``.
    The proxy implementation forwards both writes to the runtime's
    HookRegistry; patched non-proxy state retains the legacy semantics
    so existing ``patch("nexus.mcp_infra._post_store_batch_hooks", ...)``
    tests continue to control which hooks fire.
    """
    _post_store_batch_hooks.append(fn)
    try:
        import inspect
        sig = inspect.signature(fn)
        params = sig.parameters
        if "catalog_doc_id" in params or any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
        ):
            _post_store_batch_hooks_with_catalog_doc_id.add(id(fn))
    except (TypeError, ValueError):
        import structlog
        structlog.get_logger().debug(
            "post_store_batch_hook_signature_unintrospectable",
            hook=getattr(fn, "__name__", repr(fn)),
        )


def fire_post_store_batch_hooks(
    doc_ids: list[str],
    collection: str,
    contents: list[str],
    embeddings: list[list[float]] | None = None,
    metadatas: list[dict] | None = None,
    *,
    catalog_doc_id: str = "",
) -> None:
    """Invoke every batch hook in ``_post_store_batch_hooks``.

    RDR-118 P1.S3: iterates the module-level proxy
    ``_post_store_batch_hooks`` which forwards to the resolved
    runtime's ``HookRegistry`` storage by default. Legacy tests that
    replace the proxy via ``unittest.mock.patch`` (substituting a
    regular list) get the same iteration semantics because both
    objects are iterable; the runtime registry stays untouched
    inside the patch's scope. Per-hook failure isolation and T2
    ``hook_failures`` persistence preserved verbatim.

    *catalog_doc_id* (RDR-108 Phase 3) is the catalog
    ``Document.tumbler`` for the document this batch belongs to.
    Required by ``manifest_write_batch_hook`` after Phase 3 retired
    ``doc_id`` from chunk metadata.
    """
    if not doc_ids:
        return
    import structlog
    _hook_log = structlog.get_logger()
    for hook in _post_store_batch_hooks:
        try:
            if id(hook) in _post_store_batch_hooks_with_catalog_doc_id:
                hook(
                    doc_ids, collection, contents, embeddings, metadatas,
                    catalog_doc_id=catalog_doc_id,
                )
            else:
                hook(doc_ids, collection, contents, embeddings, metadatas)
        except Exception as exc:
            hook_name = getattr(hook, "__name__", "?")
            _hook_log.warning(
                "post_store_batch_hook_failed",
                hook=hook_name,
                exc_info=True,
            )
            _record_batch_hook_failure(
                doc_ids=doc_ids,
                collection=collection,
                hook_name=hook_name,
                error=str(exc),
            )


def _record_batch_hook_failure(
    *,
    doc_ids: list[str],
    collection: str,
    hook_name: str,
    error: str,
) -> None:
    """Persist a batch-shape post-store hook failure to T2 ``hook_failures``.

    Writes the JSON-encoded doc_id list to ``batch_doc_ids`` and sets
    ``is_batch=1``; stores a representative scalar (first doc_id) in the
    legacy ``doc_id`` column so existing scalar readers continue to render
    something meaningful (RDR-095 schema migration adds the two new columns
    in 4.14.1).

    Falls back to scalar-only insert if the new columns don't exist yet
    (P1.1 merged before P1.2 migration ran). Same secondary best-effort
    semantics as ``_record_hook_failure``: persistence failure cannot
    break ingest.
    """
    if not doc_ids:
        # Empty batch -> no identity to persist; nothing to record.
        # record_hook_failure also rejects empty batch_doc_ids so this
        # early-return skips the t2_ctx round-trip cleanly.
        return
    representative = doc_ids[0]
    try:
        with t2_ctx() as t2:
            t2.memory.record_hook_failure(
                doc_id=representative, collection=collection,
                hook_name=hook_name, error=error,
                chain="batch", batch_doc_ids=list(doc_ids),
            )
    except Exception:
        import structlog
        structlog.get_logger().debug(
            "batch_hook_failure_persist_failed",
            hook=hook_name,
            collection=collection,
            exc_info=True,
        )


def taxonomy_assign_batch_hook(
    doc_ids: list[str],
    collection: str,
    contents: list[str],
    embeddings: list[list[float]] | None,
    metadatas: list[dict] | None,
    *,
    catalog_doc_id: str = "",
) -> None:
    """Registered batch hook: assign indexed docs to their nearest topics.

    Called via ``fire_post_store_batch_hooks`` from every storage event:
    CLI bulk ingest passes the embeddings already computed by the upsert;
    MCP ``store_put`` passes ``embeddings=None`` and the hook fetches them
    from T3 inline (one ChromaDB get per doc). The fetch falls back to
    local MiniLM embedding when T3 is unavailable, matching the legacy
    single-doc shim behaviour.

    Reads ``embeddings`` and ``contents``; ignores ``metadatas``.
    No-op when centroids don't exist (no discover run yet) or the
    collection is excluded.

    Registered once via ``register_post_store_batch_hook`` in
    ``mcp/core.py``.
    """
    from fnmatch import fnmatch

    from nexus.config import is_local_mode, load_config

    if not doc_ids:
        return

    if is_local_mode():
        exclude = load_config().get("taxonomy", {}).get("local_exclude_collections", [])
        if any(fnmatch(collection, pat) for pat in exclude):
            return

    if not embeddings:
        embeddings = _fetch_or_embed(doc_ids, collection, contents)
        if not embeddings:
            return

    try:
        with t2_ctx() as db:
            chroma_client = get_t3()._client
            # Same-collection assignment
            db.taxonomy.assign_batch(
                collection, doc_ids, embeddings, chroma_client,
            )
            # Cross-collection projection (RDR-075 SC-6)
            cross_assigned = db.taxonomy.assign_batch(
                collection, doc_ids, embeddings, chroma_client,
                cross_collection=True,
            )
            if cross_assigned:
                import structlog
                structlog.get_logger().debug(
                    "taxonomy_cross_collection_batch",
                    collection=collection,
                    cross_assigned=cross_assigned,
                )
    except Exception:
        import structlog
        structlog.get_logger().debug("taxonomy_assign_batch_failed", exc_info=True)


def _fetch_or_embed(
    doc_ids: list[str],
    collection: str,
    contents: list[str],
) -> list[list[float]] | None:
    """Fetch existing T3 embeddings for *doc_ids* in *collection*.

    Falls back to local MiniLM embedding of *contents* when T3 is
    unavailable or returns no embedding for a given id. Returns None
    if no embeddings can be produced (callers no-op in that case).
    Used by ``taxonomy_assign_batch_hook`` when called from MCP
    ``store_put`` with ``embeddings=None``.
    """
    import numpy as np

    fetched: list[list[float] | None] = [None] * len(doc_ids)
    try:
        chroma_client = get_t3()._client
        coll = chroma_client.get_collection(collection, embedding_function=None)
        result = coll.get(ids=doc_ids, include=["embeddings"])
        result_ids = result.get("ids", [])
        result_embs = result.get("embeddings")
        if result_embs is not None:
            id_index = {d: i for i, d in enumerate(doc_ids)}
            for j, rid in enumerate(result_ids):
                idx = id_index.get(rid)
                if idx is None:
                    continue
                emb = result_embs[j]
                if emb is not None and len(emb) > 0:
                    fetched[idx] = list(emb)
    except Exception:
        import structlog
        structlog.get_logger().debug(
            "taxonomy_t3_embedding_fetch_failed",
            collection=collection,
            exc_info=True,
        )

    missing = [i for i, e in enumerate(fetched) if e is None]
    if missing and contents:
        try:
            from nexus.db.local_ef import LocalEmbeddingFunction
            ef = LocalEmbeddingFunction(model_name="all-MiniLM-L6-v2")
            local_inputs = [contents[i] for i in missing if i < len(contents)]
            local_embs = ef(local_inputs)
            for k, i in enumerate(missing):
                if i < len(contents) and k < len(local_embs):
                    fetched[i] = list(np.array(local_embs[k], dtype=np.float32))
        except Exception:
            import structlog
            structlog.get_logger().debug(
                "taxonomy_local_embed_fallback_failed", exc_info=True,
            )

    if any(e is None for e in fetched):
        return None
    return [e for e in fetched if e is not None]


# ── Chash dual-write (RDR-086 Phase 1.2; migrated to batch hook in RDR-095) ──


def chash_dual_write_batch_hook(
    doc_ids: list[str],
    collection: str,
    contents: list[str],
    embeddings: list[list[float]] | None,
    metadatas: list[dict] | None,
    *,
    catalog_doc_id: str = "",
) -> None:
    """Registered batch hook (RDR-095): best-effort dual-write of
    ``chash_index`` rows after a T3 upsert.

    Called via ``fire_post_store_batch_hooks`` from every CLI indexing
    path immediately after ``t3.upsert_chunks_with_embeddings(...)``.
    Reads ``metadatas``; ignores ``contents`` and ``embeddings``. Opens a
    fresh T2Database (matching ``taxonomy_assign_batch_hook``'s
    lifecycle), delegates to the store-level
    ``dual_write_chash_index`` helper, and closes. Logs at debug level
    on any outer failure: a T2 failure must never abort the enclosing
    T3 write path.

    Registered via ``register_post_store_batch_hook`` in
    ``mcp/core.py``.
    """
    if not doc_ids or not metadatas:
        return
    try:
        from nexus.db.t2.chash_index import dual_write_chash_index

        with t2_ctx() as db:
            dual_write_chash_index(db.chash_index, collection, doc_ids, metadatas)
    except Exception:
        import structlog
        structlog.get_logger().debug("chash_dual_write_batch_failed", exc_info=True)


def manifest_write_batch_hook(
    doc_ids: list[str],
    collection: str,
    contents: list[str],
    embeddings: list[list[float]] | None,
    metadatas: list[dict] | None,
    *,
    catalog_doc_id: str = "",
) -> None:
    """Registered batch hook (nexus-572g OBS-3): UPSERT document_chunks
    manifest rows after every T3 upsert so the catalog manifest stays
    current without manual backfill.

    Calls ``Catalog.append_manifest_chunks`` (UPSERT keyed on
    ``(doc_id, position)``) once per batch. Multi-batch indexing paths
    (streaming PDF pipeline, doc_indexer incremental loop, anything
    that splits a document across multiple ``fire_post_store_batch_hooks``
    calls for the same ``catalog_doc_id``) accumulate the manifest
    correctly across batches because UPSERT on the primary key does not
    DELETE prior rows. Re-indexing with fewer chunks than before may
    leave orphan rows at higher positions; the per-document hook
    (fired once at the tail of every indexing call) is responsible for
    final cleanup and can call ``write_manifest`` to replace.
    Best-effort: any failure is logged at debug level and never
    propagates to the caller.

    Reads ``metadatas`` (``chunk_text_hash``, ``line_start``,
    ``line_end``, ``chunk_start_char``, ``chunk_end_char``); ignores
    ``contents`` and ``embeddings``.

    *catalog_doc_id* (RDR-108 Phase 3) — the catalog ``Document.tumbler``
    string for the batch's document. Phase 3 retired ``doc_id`` from
    chunk metadata; the hook now reads it from the outer call context.
    For pre-Phase-3 chunks (still re-fired during legacy reindexes) the
    field may also appear in ``meta.doc_id`` — that legacy fallback path
    preserves correctness during the transition.
    ``int(m.get("chunk_index", i))`` similarly bridges legacy chunks
    (which carry chunk_index in metadata) and Phase 3 chunks (which use
    enumeration index within the batch). The per-batch enumeration is
    safe under multi-batch streaming because callers passing a
    ``chunk_index`` in chunk metadata get global positions; Phase-3
    streaming chunks fall back to local positions which are still
    monotone within a batch — Phase 4 retargeting will pass per-call
    chunk_positions explicitly.
    """
    if not metadatas:
        return
    from collections import defaultdict

    by_doc: dict[str, list[tuple[int, dict]]] = defaultdict(list)
    for i, meta in enumerate(metadatas):
        doc_id = catalog_doc_id or meta.get("doc_id", "")
        if doc_id:
            by_doc[doc_id].append((i, meta))
    if not by_doc:
        return
    try:
        cat = get_catalog()
    except Exception:
        import structlog
        structlog.get_logger().debug("manifest_write_hook_no_catalog", exc_info=True)
        return
    if cat is None:
        return
    for doc_id, indexed_metas in by_doc.items():
        chunks = [
            {
                "chash": m.get("chunk_text_hash", ""),
                "position": int(m.get("chunk_index", i)),
                "chunk_index": m.get("chunk_index"),
                "line_start": m.get("line_start") or None,
                "line_end": m.get("line_end") or None,
                "char_start": m.get("chunk_start_char") or None,
                "char_end": m.get("chunk_end_char") or None,
            }
            for i, m in indexed_metas
        ]
        if all(not c["chash"] for c in chunks):
            continue
        try:
            # nexus-zq79 F3 / nexus-lrhg #1: shrink-reindex orphan cleanup.
            # UPSERT keyed on (doc_id, position) leaves orphan rows when a
            # file re-indexes with fewer chunks than before. When the batch
            # contains position 0 (the start of a file's chunks), wrap the
            # DELETE + INSERT + chunk_count UPDATE in one transaction via
            # ``atomic_manifest_replace`` so a partial-failure crash
            # between the purge and the new write cannot leave the catalog
            # with zero chunks for a doc the documents row still claims N.
            # Multi-batch writes never include position 0 in batches other
            # than the first, so the atomic-replace path is safe for the
            # streaming PDF / doc_indexer paths.
            if any(c["position"] == 0 for c in chunks):
                cat.atomic_manifest_replace(doc_id, chunks)
            else:
                cat.append_manifest_chunks(doc_id, chunks)
                # nexus-zq79: documents.chunk_count is a denormalised cache of
                # COUNT(*) document_chunks. The catalog-register hook runs BEFORE
                # per-file indexing (tumbler injection requires it), so chunk_count
                # is initialised to 0; nothing else updates it for code/prose
                # indexers post-Phase-3. Routed via Catalog public API to satisfy
                # the projector-only-writes invariant (RDR-101 Phase 3 ε).
                cat.resync_chunk_count_cache(doc_id)
        except Exception:
            # Post-Phase-3 the manifest hook is load-bearing: a failure
            # leaves the catalog manifest empty and chunk_count=0 for
            # this doc (silent data-correctness bug). The contract still
            # requires non-propagation (best-effort hook) but the log
            # severity is WARNING so failures are discoverable in
            # production log streams without DEBUG enabled. nexus-zq79.
            import structlog
            structlog.get_logger().warning(
                "manifest_write_hook_failed", doc_id=doc_id, exc_info=True
            )


# RDR-108 Phase 3 (nexus-bdag): the three batch hooks above are
# load-bearing for catalog correctness across BOTH CLI ingest and MCP
# tool calls (``nx index repo`` populates ``chash_index``,
# ``taxonomy_assignments``, and ``document_chunks`` on the catalog
# manifest). Pre-RDR-118 the registrations fired at module load here;
# RDR-118 P1.S3 (nexus-ipyfj) moves them into
# ``nexus.runtime.install_default_hooks(runtime)`` which is called by
# every entry point (CLI ``main``, MCP server startup) after the
# runtime is constructed. Tests that need the load-bearing hooks call
# ``install_default_hooks`` explicitly; tests that do not get a clean
# registry by default (the per-test isolation property the runtime
# container is buying).


# ── Post-store document hooks (RDR-089, nexus-yyev) ──────────────────────────
# Third hook chain. Fires once per *document* after every storage event
# (MCP store_put and every CLI ingest path). Signature is
# ``fn(source_path, collection, content)`` rather than the doc_id-keyed
# single chain or the doc_ids-list batch chain. Document-grain enrichment
# (RDR-089 aspect extraction is the canonical consumer) needs the full
# document text and a stable on-disk identifier, not chunk-level references.
#
# Content-sourcing contract (audit F4):
#   * MCP store_put has the full doc text in scope and passes ``content=<text>``.
#   * CLI sites accumulate chunks rather than full documents and pass
#     ``content=""`` as the contract signal that the hook may need to
#     read source_path itself.
#   * Hooks treat ``content`` as primary, falling back to file read when empty.
#
# Synchronous all the way down. Zero asyncio in the dispatcher (RDR-089
# load-bearing contract: routing through async operator_extract from this
# sync chain silently drops the coroutine). RDR-118 P2.S1b tightens the
# contract: ``HookRegistry.register_document`` raises ``TypeError`` on
# coroutine callables at registration time instead of accepting and
# silently dropping at fire time.
#
# Per-hook failures are captured, logged, and persisted to T2
# ``hook_failures`` with ``chain='document'``. Failures never propagate.
#
# RDR-118 P2.S1b (nexus-f2ufy): the storage moved to
# ``NexusRuntime.hooks._document`` (and ``_document_with_doc_id``); the
# functions and module-level names below are thin redirectors / proxies
# that delegate to the runtime resolver. The RDR-089 aspect-extraction
# enqueue hook self-registration moved from ``mcp/core.py:557`` to the
# ``install_default_hooks`` factory in ``nexus.runtime``.


class _DocumentHooksListProxy:
    """List-like view of the resolved runtime's document-chain hook list.

    Forwards every list operation to ``runtime.hooks._document``. Same
    migration pattern as ``_BatchHooksListProxy`` and
    ``_SingleHooksListProxy``. Phase 2 (``nexus-0zgb3``) deletes the
    proxy alongside the autouse fixture that backs it.
    """

    @staticmethod
    def _live() -> list:
        from nexus.runtime import _ensure_runtime_for_shim
        return _ensure_runtime_for_shim().hooks._document

    def __iter__(self):
        return iter(self._live())

    def __len__(self) -> int:
        return len(self._live())

    def __getitem__(self, key):
        return self._live()[key]

    def __setitem__(self, key, value) -> None:
        if isinstance(key, slice) and key == slice(None):
            self.clear()
            for fn in value:
                self.append(fn)
            return
        self._live()[key] = value

    def __contains__(self, item) -> bool:
        return item in self._live()

    def __eq__(self, other) -> bool:
        return list(self._live()) == list(other)

    def __repr__(self) -> str:
        return repr(self._live())

    def append(self, fn) -> None:
        from nexus.runtime import _ensure_runtime_for_shim
        _ensure_runtime_for_shim().hooks.register_document(fn)

    def extend(self, fns) -> None:
        for fn in fns:
            self.append(fn)

    def remove(self, fn) -> None:
        from nexus.runtime import _ensure_runtime_for_shim
        rt = _ensure_runtime_for_shim()
        rt.hooks._document.remove(fn)
        rt.hooks._document_with_doc_id.discard(id(fn))

    def clear(self) -> None:
        from nexus.runtime import _ensure_runtime_for_shim
        rt = _ensure_runtime_for_shim()
        rt.hooks._document.clear()
        rt.hooks._document_with_doc_id.clear()


class _DocumentDocIdSetProxy:
    """Set-like view of the resolved runtime's ``_document_with_doc_id``
    classification set. Mirrors ``_CatalogDocIdSetProxy``."""

    @staticmethod
    def _live() -> set:
        from nexus.runtime import _ensure_runtime_for_shim
        return _ensure_runtime_for_shim().hooks._document_with_doc_id

    def __iter__(self):
        return iter(self._live())

    def __len__(self) -> int:
        return len(self._live())

    def __contains__(self, item) -> bool:
        return item in self._live()

    def __eq__(self, other) -> bool:
        return set(self._live()) == set(other)

    def __repr__(self) -> str:
        return repr(self._live())

    def add(self, item) -> None:
        self._live().add(item)

    def discard(self, item) -> None:
        self._live().discard(item)

    def update(self, other) -> None:
        self._live().update(other)

    def clear(self) -> None:
        self._live().clear()


_post_document_hooks: _DocumentHooksListProxy = _DocumentHooksListProxy()
#: Proxy view of the resolved runtime's ``_document_with_doc_id`` set.
#: Populated indirectly by :func:`register_post_document_hook` via the
#: runtime's signature-classification path (RDR-101 Phase 4 contract).
_post_document_hooks_with_doc_id: _DocumentDocIdSetProxy = _DocumentDocIdSetProxy()


def register_post_document_hook(fn) -> None:
    """Register a synchronous document-grain hook on the resolved
    runtime's HookRegistry.

    RDR-118 P2.S1b thin redirector. ``_post_document_hooks.append(fn)``
    forwards to ``runtime.hooks.register_document(fn)`` which raises
    ``TypeError`` on coroutine callables (the dispatcher contract is
    synchronous-only; the pre-RDR-118 module-level dispatcher silently
    dropped coroutines, the new contract is louder).

    The doc_id-aware classification (RDR-101 Phase 4) runs inside
    ``register_document`` via ``inspect.signature``; hooks that accept
    ``doc_id`` or ``**kwargs`` are invoked with the kwarg at fire time.
    """
    _post_document_hooks.append(fn)


def fire_post_document_hooks(
    source_path: str, collection: str, content: str,
    *,
    doc_id: str = "",
) -> None:
    """Invoke every document-grain hook in ``_post_document_hooks``.

    RDR-118 P2.S1b: iterates the proxy which forwards to the resolved
    runtime's HookRegistry. Synchronous dispatch (no ``asyncio.to_thread``,
    no ``await``); per-hook failure isolation + T2 ``hook_failures``
    persistence preserved verbatim from the legacy dispatcher.

    Content-sourcing contract:

    * ``content`` non-empty (MCP path): passed through verbatim; the
      hook reads ``content`` directly.
    * ``content == ""`` (CLI path): the contract signal that the hook
      may need to read ``source_path`` itself. The framework forwards
      both arguments unchanged.

    ``doc_id`` (nexus-tdgc / RDR-101 Phase 4) is the catalog identity
    of the source document. Forwarded to every registered hook whose
    signature includes ``doc_id`` or ``**kwargs``; older registrations
    are called without it.
    """
    import structlog
    _hook_log = structlog.get_logger()
    for hook in _post_document_hooks:
        try:
            if id(hook) in _post_document_hooks_with_doc_id:
                hook(source_path, collection, content, doc_id=doc_id)
            else:
                hook(source_path, collection, content)
        except Exception as exc:
            hook_name = getattr(hook, "__name__", "?")
            _hook_log.warning(
                "post_document_hook_failed",
                hook=hook_name,
                source_path=source_path,
                collection=collection,
                exc_info=True,
            )
            _record_document_hook_failure(
                source_path=source_path,
                collection=collection,
                hook_name=hook_name,
                error=str(exc),
            )


def _record_document_hook_failure(
    *,
    source_path: str,
    collection: str,
    hook_name: str,
    error: str,
) -> None:
    """Persist a document-grain hook failure to T2 ``hook_failures``.

    Stores ``source_path`` in the legacy ``doc_id`` column (the column
    carries 'subject of failure' regardless of chain shape) and sets
    ``chain='document'`` so readers can render the row appropriately.

    Falls back to a chain-less insert when the 4.14.2 migration has not
    yet run (mixed-version operator scenario): the outer guard
    propagates schema errors to the secondary ``except`` only, so
    transient lock or I/O failures bubble up where the outer
    best-effort wrapper can swallow them. Same secondary best-effort
    semantics as ``_record_hook_failure`` and
    ``_record_batch_hook_failure``: persistence failure cannot break
    ingest.

    Two-tier fallback (vs. three-tier in ``_record_batch_hook_failure``)
    is correct: the document chain is new in 4.14.2, so there is no
    intermediate 4.14.1-shape schema to handle — the row either has
    ``chain`` (post-4.14.2) or it does not (pre-4.14.2 generic shape).
    """
    try:
        with t2_ctx() as t2:
            # ``source_path`` lands in the ``doc_id`` column — the column
            # carries 'subject of failure' regardless of chain shape.
            t2.memory.record_hook_failure(
                doc_id=source_path, collection=collection,
                hook_name=hook_name, error=error, chain="document",
            )
    except Exception:
        import structlog
        structlog.get_logger().debug(
            "document_hook_failure_persist_failed",
            hook=hook_name,
            collection=collection,
            exc_info=True,
        )


# ── Combined post-store fire helper (nexus-9099) ─────────────────────────────
#
# RDR-095 established symmetric-fire for the bulk CLI ingest paths
# (``nx index repo / pdf / rdr``) so every storage event invokes the
# single, batch, and document-grain hook chains. Three CLI paths were
# overlooked by the symmetric-fire commit and shipped firing zero
# chains: ``nx store put``, ``nx memory promote``, and
# ``nx store import``. The downstream effect is silent drift between
# the catalog row + chroma chunk and the T2 indexes that operator SQL
# fast paths depend on (chash_index, taxonomy assignments, aspect
# extraction queue). nexus-9099 surfaced the bug; this helper closes
# the gap by giving every T3-write path a single call site that fires
# all three chains in the correct order.
#
# Used by:
#   * MCP ``store_put`` (mcp/core.py)
#   * CLI ``nx store put`` (commands/store.py)
#   * CLI ``nx memory promote`` (commands/memory.py)
#   * CLI ``nx store import`` (commands/store.py)
#
# Bulk ``nx index *`` ingest paths still call the three fire functions
# directly (see ``code_indexer.py``, ``prose_indexer.py``,
# ``doc_indexer.py``, ``pipeline_stages.py``, ``indexer.py``) — the AST
# drift guard in ``tests/test_hook_drift_guard.py`` enforces 7 CLI
# fire sites + 1 MCP fire site for the document chain. This helper is
# additive; it does not rewire the bulk paths.


def fire_store_chains(
    doc_ids: list[str],
    collection: str,
    contents: list[str],
    *,
    source_paths: list[str] | None = None,
    embeddings: list[list[float]] | None = None,
    metadatas: list[dict] | None = None,
    catalog_doc_id: str = "",
) -> None:
    """Fire all three post-store hook chains for a batch of just-stored docs.

    Single, batch, and document-grain chains run in that order. Errors
    are caught per-hook and persisted to T2 ``hook_failures``; nothing
    is propagated to the caller (matching the existing fire functions'
    semantics).

    Parameters
    ----------
    doc_ids:
        Stable identifiers for each just-stored document.
    collection:
        Physical collection name (e.g. ``knowledge__knowledge``).
    contents:
        Document text per doc_id. Length must match ``doc_ids``.
    source_paths:
        On-disk source path per document, or ``None`` to use ``doc_ids``
        as the source identity (the MCP ``store_put`` shape — there is
        no on-disk source). Length must match ``doc_ids`` when given.
    embeddings:
        Optional dense vectors per doc; forwarded to the batch chain.
        ``taxonomy_assign_batch_hook`` accepts ``None`` and fetches
        embeddings from T3 inline.
    metadatas:
        Optional metadata dicts per doc; forwarded to the batch chain.
        ``chash_dual_write_batch_hook`` reads from this.
    catalog_doc_id:
        nexus-lf8f: the catalog ``Document.tumbler`` for these chunks
        (RDR-108 Phase 3). Post-Phase-3, chunk metadata no longer
        carries ``doc_id`` so the manifest-write hook needs the tumbler
        passed explicitly via this kwarg or it short-circuits silently
        (the exact symptom nexus-zq79 fixed for the indexer paths in
        4.32.4; this kwarg closes the same gap for the store-path
        callers — MCP ``store_put``, ``nx store put``,
        ``nx memory promote``, ``nx store import``). Default ``""``
        preserves the legacy "no catalog identity" shape for callers
        that genuinely have no tumbler (raw scratch writes pre-catalog
        registration); those callers' chunks remain catalog-orphaned by
        design.
    """
    n = len(doc_ids)
    if len(contents) != n:
        raise ValueError(
            f"contents length {len(contents)} != doc_ids length {n}"
        )
    if source_paths is None:
        source_paths = list(doc_ids)
    elif len(source_paths) != n:
        raise ValueError(
            f"source_paths length {len(source_paths)} != doc_ids length {n}"
        )

    # Single-doc chain — once per (doc_id, content).
    for doc_id, content in zip(doc_ids, contents):
        fire_post_store_hooks(doc_id, collection, content)

    # Batch chain — one call with the whole batch.
    fire_post_store_batch_hooks(
        doc_ids, collection, contents,
        embeddings=embeddings, metadatas=metadatas,
        catalog_doc_id=catalog_doc_id,
    )

    # Document-grain chain — once per (source_path, content).
    # nexus-tdgc: doc_id is forwarded so the aspect-queue hook can
    # capture it at enqueue time. The doc_ids list is the same one
    # the post_store + batch chains use; for the document chain it is
    # the catalog identity.
    for did, sp, content in zip(doc_ids, source_paths, contents):
        fire_post_document_hooks(sp, collection, content, doc_id=did)


# ── Version compatibility check (RDR-076) ─────────────────────────────────────


def check_version_compatibility() -> None:
    """Synchronous startup check for two version-drift cases.

    Called from each MCP server's ``main()`` before ``mcp.run()`` — the
    natural single binding point between plugin and CLI (the MCP server
    binaries ``nx-mcp`` / ``nx-mcp-catalog`` are conexus entry points;
    plugin/CLI coupling runs entirely through this surface).

    Two warnings, both non-fatal:

    1. **CLI ↔ T2 schema drift** — current ``conexus`` package version
       differs (minor or major) from ``_nexus_version.cli_version``
       stored in T2. Suggests ``nx upgrade``. Catches the case where
       the user upgraded conexus but hasn't run any migration-applying
       command yet.

    2. **Plugin ↔ CLI version drift** — installed Claude Code plugin's
       declared version (read from ``${CLAUDE_PLUGIN_ROOT}/.claude-plugin/
       plugin.json``) differs (minor or major) from the running CLI.
       Suggests ``/plugin update nx@nexus-plugins`` or
       ``uv tool upgrade conexus`` depending on which side is older.
       The plugin and CLI ship from the same repo at the same version
       (CI enforces marketplace.json parity); drift means one update
       command was run without the other.

    Never blocks startup — both checks log warnings only.
    """
    import json
    import sqlite3
    from pathlib import Path

    import structlog

    log = structlog.get_logger()
    try:
        from importlib.metadata import version as _pkg_version
        from nexus.db.migrations import _parse_version

        cli_ver = _pkg_version("conexus")

        # ── (1) CLI ↔ T2 schema drift ────────────────────────────────────
        # RDR-112 P1 prereq: skip the direct-open probe when the daemon
        # owns the file. The daemon's own startup will check this.
        db_path = default_db_path()
        from nexus.db import is_daemon_mode as _is_daemon_mode
        if db_path.exists() and not _is_daemon_mode():
            # storage-boundary-allow: MCP startup version-drift probe;
            # gated on `not _is_daemon_mode()` above.
            conn = sqlite3.connect(str(db_path))
            try:
                row = conn.execute(
                    "SELECT value FROM _nexus_version WHERE key='cli_version'"
                ).fetchone()
            except sqlite3.OperationalError:
                row = None  # table doesn't exist yet — fresh install
            finally:
                conn.close()
            if row is not None:
                stored_ver = row[0]
                cli_t = _parse_version(cli_ver)
                stored_t = _parse_version(stored_ver)
                # Warn on minor or major divergence, not patch.
                # Tuple slicing is safe for short tuples — (4,)[:2] == (4,).
                if cli_t[:2] != stored_t[:2]:
                    if cli_t > stored_t:
                        hint = "run 'nx upgrade' to apply pending migrations"
                    else:
                        hint = "DB was upgraded by a newer CLI version"
                    log.warning(
                        "version_mismatch",
                        cli_version=cli_ver,
                        stored_version=stored_ver,
                        hint=hint,
                    )

        # ── (2) Plugin ↔ CLI version drift ──────────────────────────────
        plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT")
        if plugin_root:
            manifest_path = Path(plugin_root) / ".claude-plugin" / "plugin.json"
            try:
                manifest = json.loads(manifest_path.read_text())
                plugin_ver = manifest.get("version")
            except (OSError, json.JSONDecodeError):
                plugin_ver = None
            if plugin_ver:
                cli_t = _parse_version(cli_ver)
                plugin_t = _parse_version(plugin_ver)
                if cli_t[:2] != plugin_t[:2]:
                    # Choose the actionable update for the lagging side.
                    if cli_t > plugin_t:
                        hint = "plugin is older — run '/plugin update nx@nexus-plugins' in Claude Code"
                    else:
                        hint = "CLI is older — run 'uv tool upgrade conexus'"
                    log.warning(
                        "plugin_cli_version_mismatch",
                        cli_version=cli_ver,
                        plugin_version=plugin_ver,
                        hint=hint,
                    )
    except Exception:
        pass  # never block MCP startup


# ── Test injection ────────────────────────────────────────────────────────────


def reset_singletons():
    """Reset lazy singletons (for tests only).

    Search review I-2: also resets the T1 plan-match cache. Previously
    the plan cache survived ``reset_singletons()`` calls (tests that
    injected a fresh T1 but kept the populated plan cache saw stale
    embeddings against the injected client and produced nondeterministic
    matches).

    RDR-118 P1.S3: the legacy ``_catalog_instance`` override is cleared
    here so a test that patched it returns to the runtime-resolved path
    on the next ``get_catalog()`` call. The runtime's catalog cache is
    also torn down via ``nexus.catalog.reset_cache`` so any cross-process
    JSONL writes are picked up on next access.

    NOTE: hook lists across the three chains (single, batch, document)
    are NOT cleared here. The batch chain is owned by the runtime's
    ``HookRegistry``; tearing it down at this point would unregister
    the load-bearing default hooks installed at CLI / MCP entry. Tests
    that need an empty hook list call ``runtime.hooks.clear()`` on the
    test's runtime fixture instead.
    """
    global _t1_instance, _t1_isolated, _t3_instance, _collections_cache, _catalog_instance, _catalog_mtime
    _t1_instance = None
    _t1_isolated = False
    _t3_instance = None
    _collections_cache = ([], 0.0)
    _catalog_instance = None
    _catalog_mtime = 0.0
    from nexus.catalog import reset_cache as _reset_catalog_cache
    _reset_catalog_cache()
    clear_search_traces()
    reset_plan_cache_for_tests()


def inject_t1(t1, *, isolated: bool = False):
    """Inject a T1Database for testing."""
    global _t1_instance, _t1_isolated
    _t1_instance = t1
    _t1_isolated = isolated


def inject_t3(t3):
    """Inject a T3Database for testing."""
    global _t3_instance
    _t3_instance = t3


def inject_catalog(cat):
    """Inject a Catalog for testing.

    Resets ``_catalog_mtime`` so the next ``get_catalog()`` call sees
    the injected catalog's current mtime and skips the (irrelevant)
    rebuild path. Without this reset the prior catalog's mtime would
    survive the swap and the next ``get_catalog()`` could either
    rebuild the injected catalog needlessly (if old mtime > new) or
    skip a needed rebuild (if old mtime < new).
    """
    global _catalog_instance, _catalog_mtime
    _catalog_instance = cat
    _catalog_mtime = 0.0
