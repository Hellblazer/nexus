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

# ── T2 daemon-unreachable rate-limiter (GH #1048) ────────────────────────────
# Emit the daemon-unreachable warning at most once per _WARN_RATE_LIMIT_SECS
# rather than once per write. Under load (nx dt index, bulk indexing) the
# function can be called dozens of times for one run; collapse the spam to a
# single emit plus a suppressed_count field on the next window's emit.
_WARN_RATE_LIMIT_SECS: float = 60.0
_warn_lock = threading.Lock()
# Per-EVENT window: event-name -> (last_emit_monotonic, suppressed_since_emit).
# Keying by event (not a single global window) means the "daemon absent" and
# "daemon alive but unresponsive" arms have independent windows, so a state
# transition (absent->alive or back) surfaces IMMEDIATELY rather than being
# masked by the other state's still-open window (review S-1, GH #1048).
_warn_state: dict[str, tuple[float, int]] = {}


def _reset_warn_rate_limiter_for_tests() -> None:
    """Test helper: reset the daemon-unreachable warning rate-limiter."""
    with _warn_lock:
        _warn_state.clear()


def _emit_unreachable_warn(event: str, **fields) -> None:
    """Emit a daemon-unreachable warning subject to per-event rate-limiting
    (GH #1048).

    The first call for an ``event`` (or the first after its window elapses)
    fires immediately, attaching any ``suppressed_count`` accumulated for that
    event since its last emit. Calls within the same window increment the
    event's counter silently.

    The structlog call is made AFTER releasing the lock (we only capture the
    payload under the lock) so a slow log handler can never serialise other
    threads on ``_warn_lock`` (review M-2).

    Known limitation (review S-2): on a bulk run shorter than the window the
    operator sees the first emit (so the condition IS surfaced) but never the
    trailing ``suppressed_count`` (it only rides the next window's emit, which
    never comes). Exact per-run totals would need an at-exit flush; deferred as
    a follow-up since first-occurrence visibility is the load-bearing signal.
    """
    payload: tuple[str, dict] | None = None
    with _warn_lock:
        last, suppressed = _warn_state.get(event, (0.0, 0))
        now = time.monotonic()
        if now - last >= _WARN_RATE_LIMIT_SECS:
            extra: dict = {"suppressed_count": suppressed} if suppressed else {}
            _warn_state[event] = (now, 0)
            payload = (event, {**fields, **extra})
        else:
            _warn_state[event] = (last, suppressed + 1)
    if payload is not None:
        import structlog
        ev, kw = payload
        structlog.get_logger().warning(ev, **kw)


# ── Lazy singletons ──────────────────────────────────────────────────────────

_t1_instance = None
_t1_isolated = False
_t1_lock = threading.Lock()

_t3_instance = None
_t3_lock = threading.Lock()

_collections_cache: tuple[list[str], float] = ([], 0.0)
_COLLECTIONS_CACHE_TTL = 60.0

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
    """Return T3Database singleton — lazy init on first call."""
    global _t3_instance
    if _t3_instance is None:
        with _t3_lock:
            if _t3_instance is None:
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


def t2_ctx():
    """Return a T2Database context manager — fresh per call.

    Reserved for the paths that genuinely cannot route through the daemon
    (RDR-128 P3): the ``aspect_worker`` persist block, whose
    ``document_aspects.upsert(record)`` takes an ``AspectRecord`` argument
    that the daemon wire protocol decodes to a plain dict server-side
    (``t2_daemon._t2_decode``), so the method would receive a dict and
    break on attribute access. The hot every-poll path routes via
    ``t2_index_write``; only the work-bounded persist falls back here.
    """
    from nexus.db.t2 import T2Database
    return T2Database(default_db_path())  # epsilon-allow: aspect_worker persist (document_aspects.upsert AspectRecord arg cannot round-trip the daemon RPC); not the every-poll hot path (RDR-128 P3)


def _reassert_t2_daemon() -> bool:
    """RDR-141: re-assert the T2 supervisor after a schema-version mismatch.

    A version mismatch on ``hello()`` means a stale-version daemon is ALIVE,
    holds the spawn lock, and is serving — so degrading straight to a direct
    ``T2Database`` would open a SECOND writer on ``memory.db`` (the version-skew
    double-writer this exists to close). Instead drive the supervisor to reap
    the stale daemon and spawn a current one, so the caller can route the write
    through a single current daemon.

    Returns ``True`` iff a current daemon is now reachable. On any non-reachable
    outcome it emits a DISTINCT, operator-visible WARNING (so the cycle-deferred
    residual — where the stale daemon is still alive and the caller's direct
    fallback is a temporary second writer — is never silent) and returns
    ``False`` so the caller degrades to a bounded direct write.

    Never raises. ``_t2_ensure_running_inner`` returns an outcome rather than
    ``sys.exit``-ing (RDR-141 P0), but ``SystemExit`` is caught defensively
    (CA-3) so a future regression in the supervisor cannot terminate the
    calling MCP process.
    """
    import structlog
    log = structlog.get_logger()
    try:
        from nexus.commands.daemon import (
            T2EnsureOutcome,
            _t2_ensure_running_inner,
        )

        outcome = _t2_ensure_running_inner(
            config_dir_str=None, timeout=15.0, quiet=True
        )
    except SystemExit as exc:
        log.warning(
            "t2_index_write_reassert_systemexit",
            code=getattr(exc, "code", None),
            hint="supervisor re-assert exited unexpectedly; degrading to a direct write",
        )
        return False

    if outcome is T2EnsureOutcome.REACHABLE:
        return True

    # Distinct event per outcome — the cycle-deferred residuals (stale daemon
    # still ALIVE) must be distinguishable from the safe down-arm (no live
    # incumbent) for operators and for the §Validation acceptance signal.
    events = {
        T2EnsureOutcome.DEFERRED_WRITE_LOCK: (
            "t2_index_write_version_skew_cycle_deferred_writelock",
            "stale daemon still ALIVE (WAL write-lock held); cycle deferred — the "
            "direct write below is a temporary second writer (RDR-128 residual, "
            "WAL non-corrupting, errors loud)",
        ),
        T2EnsureOutcome.DEFERRED_SIGTERM: (
            "t2_index_write_version_skew_cycle_deferred_sigterm",
            "stale daemon still ALIVE (SIGTERM did not take in window); cycle "
            "deferred — the direct write below is a temporary second writer "
            "(RDR-128 residual, WAL non-corrupting, errors loud)",
        ),
        T2EnsureOutcome.CRASHLOOP_SUPPRESSED: (
            "t2_index_write_version_skew_crashloop_down",
            "no live daemon (crash-loop guard suppressed respawn); the direct "
            "write below is safe (single-writer down-arm)",
        ),
        T2EnsureOutcome.SPAWN_FAILED: (
            "t2_index_write_version_skew_spawn_failed",
            "daemon spawn failed; the direct write below is safe (no live daemon)",
        ),
    }
    event, hint = events.get(
        outcome,
        (
            "t2_index_write_version_skew_reassert_unreachable",
            "supervisor re-assert did not reach a current daemon",
        ),
    )
    log.warning(event, outcome=outcome.value, hint=hint)
    return False


def t2_index_write(write_fn):
    """Run one T2 write through the daemon (``T2Client``) if it is
    reachable, else a direct ``T2Database`` (RDR-128 P1, nexus-kg8sj;
    generalized to all routable writers in P3, nexus-sbxbe.3).

    Returns ``write_fn``'s result so callers that need the write's return
    value (e.g. the aspect_worker's ``claim_batch`` rows, or
    ``rename_collection_cascade``'s per-store row counts) can route too;
    fire-and-forget callers simply ignore the return.

    Routing keeps the ``nx index repo`` process from opening ``memory.db``
    directly and holding its single WAL writer slot — the daemon becomes
    the writer, so a dead/slow indexer can no longer strand the lock for
    other processes. The direct-``T2Database`` fallback preserves
    functionality when the daemon is down (at the cost of the old
    direct-lock behavior), and is logged so the degraded path is visible.

    ``write_fn(db)`` receives the writer (``T2Client`` or ``T2Database``;
    both expose the same ``db.<store>.<method>(...)`` surface). Reachability
    is decided by an explicit up-front probe (``database.hello()``) rather
    than by catching an error *out of* ``write_fn`` — because some writers
    (e.g. the best-effort ``dual_write_chash_index``) swallow their own
    exceptions internally, which would otherwise hide the unreachable
    signal and silently drop the write. ``write_fn`` is therefore invoked
    against exactly one writer, never re-run, so there is no double-write
    risk regardless of how it handles errors.
    """
    from nexus.daemon.t2_client import (
        T2DaemonNotReachableError,
        T2SchemaVersionMismatchError,
        make_t2_client,
    )

    import structlog

    client = None
    # True once a degraded arm has emitted its OWN distinct, accurate event,
    # so the generic "start the daemon" banner below is suppressed (its hint is
    # WRONG when a daemon is actually running — RDR-141 version-skew arm and the
    # GH #1048 daemon-alive-but-unresponsive arm).
    degraded_logged = False
    try:
        client = make_t2_client()
        client.database.hello()  # force the lazy connect; raises if down
    except T2DaemonNotReachableError:
        # Degrading to a direct write is safe (RDR-128 documented-irreducible
        # availability fallback). GH #1048: distinguish "daemon absent" from
        # "daemon alive but unresponsive/timed-out under load" via a read-only
        # discovery liveness probe (no spawn/reap — that's the supervisor's
        # job), and rate-limit the warning so a bulk run does not spam it.
        if client is not None:
            client.close()
        client = None
        # The probe runs on every degraded write (not just emitting ones) so a
        # state transition is classified accurately each time; the cost is one
        # stat + os.kill(pid, 0), negligible against the direct DB write this
        # path is already doing (review M-1/S-3 — accepted by design).
        from nexus.daemon.discovery import find_t2_daemon

        if find_t2_daemon() is not None:
            # A live daemon IS registered but did not answer hello() — likely
            # load / timeout / contention (#1046), NOT a missing daemon.
            # "start the daemon" would be wrong (it is running).
            _emit_unreachable_warn(
                "t2_index_write_daemon_unreachable_but_alive",
                hint=(
                    "T2 daemon is running but did not respond to hello(); likely "
                    "load/timeout/contention — writes degraded to direct until it "
                    "becomes responsive again"
                ),
            )
        else:
            _emit_unreachable_warn(
                "t2_index_write_daemon_unreachable_fallback",
                hint="start the T2 daemon (`nx daemon t2 start`) to route indexer writes",
            )
        degraded_logged = True
    except T2SchemaVersionMismatchError:
        # RDR-141: a stale-VERSION daemon is ALIVE, holds the spawn lock, and
        # is actively serving. Opening a direct writer here would put a SECOND
        # live writer on memory.db (the version-skew double-writer). Instead
        # re-assert the supervisor (reap the stale daemon, spawn a current
        # one) and re-probe ONCE through the fresh daemon. Single attempt, no
        # retry loop: on a second mismatch/unreachable we fall through to the
        # bounded direct write. Every degraded sub-path here emits its OWN
        # distinct event (so the generic banner is suppressed via degraded_logged).
        if client is not None:
            client.close()
        client = None
        if _reassert_t2_daemon():  # emits a distinct event itself when it returns False
            try:
                client = make_t2_client()
                client.database.hello()
            except (T2DaemonNotReachableError, T2SchemaVersionMismatchError):
                # Re-assert reported a current daemon but it is no longer
                # reachable / still skewed on re-probe (single-attempt cap).
                # _reassert returned True, so it logged nothing — emit the
                # distinct event for this sub-path here.
                if client is not None:
                    client.close()
                client = None
                degraded_logged = True
                structlog.get_logger().warning(
                    "t2_index_write_version_skew_reprobe_failed",
                    hint=(
                        "re-assert reported a current daemon but the re-probe "
                        "found it unreachable/still-skewed; a bounded direct "
                        "write follows (single-attempt cap)"
                    ),
                )
        else:
            degraded_logged = True  # _reassert already emitted its distinct event

    if client is not None:
        try:
            return write_fn(client)
        finally:
            client.close()

    # Degraded path: this is the direct-lock behavior RDR-128 exists to
    # eliminate. Every arm that reaches here with ``client is None`` now sets
    # ``degraded_logged`` after emitting its own accurate, rate-limited event
    # (GH #1048 absent/alive split + the RDR-141 version-skew events), so in
    # practice this guard is always True here. Retained as a forward-compat
    # backstop: any future arm that degrades WITHOUT logging still surfaces a
    # warning rather than silently falling through to a direct write.
    if not degraded_logged:
        structlog.get_logger().warning(
            "t2_index_write_daemon_unreachable_fallback",
            hint="start the T2 daemon (`nx daemon t2 start`) to route indexer writes",
        )
    from nexus.db.t2 import T2Database
    with T2Database(default_db_path()) as db:  # epsilon-allow: by-design daemon-unreachable fallback so writes degrade to direct rather than failing (RDR-128 P3 documented-irreducible)
        return write_fn(db)


# ── T1 plan session cache (RDR-078) ──────────────────────────────────────────
# Plan-cache wrappers. The cache state itself lives in
# nexus.mcp.plan_cache_registry.PlanCacheRegistry as a single module-
# level singleton (nexus-sl69o, 2026-05-20). These two functions
# preserve the historical public API; new code may prefer
# get_plan_cache_registry().get(...) directly.


def get_t1_plan_cache(*, populate_from=None):
    """Return the T1 ``plans__session`` cache, lazy-populated on first call.

    When *populate_from* (a PlanLibrary) is supplied, the cache is
    populated from its rows on first call and **repopulated whenever
    the underlying SQLite file mtime advances** (nexus-qgjr). The mtime
    check mirrors the catalog's ``_last_consistency_mtime`` pattern at
    ``catalog.py:405``: cheap when nothing changed, rebuilds when a
    write moves the file's stat-time.

    Libraries without a ``path`` attribute fall back to populate-once
    semantics; the mtime tier costs them nothing.

    Returns ``None`` when no T1 client is reachable; the matcher falls
    back to FTS5 in that case. Subsequent calls after an init failure
    return ``None`` immediately without re-entering the lock.

    Backed by :class:`nexus.mcp.plan_cache_registry.PlanCacheRegistry`.
    """
    from nexus.mcp.plan_cache_registry import get_plan_cache_registry
    return get_plan_cache_registry().get(populate_from=populate_from)


def reset_plan_cache_for_tests() -> None:
    """Test helper: drop the cache so the next call re-initialises.

    Backed by
    :func:`nexus.mcp.plan_cache_registry.reset_plan_cache_registry_for_tests`.
    """
    from nexus.mcp.plan_cache_registry import reset_plan_cache_registry_for_tests
    reset_plan_cache_registry_for_tests()


# ── Catalog management ────────────────────────────────────────────────────────


def get_catalog():
    """Return a fresh Catalog or None when not initialised.

    No process-level caching; each call constructs a Catalog at
    ``catalog_path()``. ``Catalog.__init__`` runs ``_ensure_consistent``
    so cross-process JSONL refreshes are picked up automatically.
    Callers that issue many lookups in tight loops should construct
    once and pass the result down (top-down DI) — see also
    ``commands/catalog.py:_get_catalog`` for the established pattern.

    Tests that need a specific Catalog instance under test set
    ``NEXUS_CATALOG_PATH`` (which ``catalog_path()`` reads) so this
    function constructs at the test's location; the autouse
    ``_isolate_catalog`` fixture in ``tests/conftest.py`` provides
    a per-test default.
    """
    from nexus.catalog import Catalog
    from nexus.config import catalog_path
    path = catalog_path()
    if not Catalog.is_initialized(path):
        return None
    return Catalog(path, path / ".catalog.db")


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


# ── Default post-store hook consumers ────────────────────────────────────────
# Pure functions used as default registrations on every HookRegistry the
# CLI / MCP entry points build. The HookRegistry class + its three
# dispatchers (single / batch / document) live in nexus.hook_registry; the
# install_default_hooks(registry) factory there wires the consumers below
# onto every freshly constructed registry. Registration order within the
# batch chain is load-bearing: chash dual-write must precede taxonomy
# assignment so chash rows exist before topic assignment runs (mirrors
# the legacy chash-before-taxonomy invariant).


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

    Called via ``HookRegistry.fire_batch`` from every storage event:
    CLI bulk ingest passes the embeddings already computed by the upsert;
    MCP ``store_put`` passes ``embeddings=None`` and the hook fetches them
    from T3 inline (one ChromaDB get per doc). The fetch falls back to
    local MiniLM embedding when T3 is unavailable, matching the legacy
    single-doc shim behaviour.

    Reads ``embeddings`` and ``contents``; ignores ``metadatas``.
    No-op when centroids don't exist (no discover run yet) or the
    collection is excluded.

    Wired by :func:`nexus.hook_registry.install_default_hooks` onto every
    runtime-constructed registry.
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
        # RDR-128 P1 (nexus-fkq5q): compute assignments client-side (the
        # ChromaDB client can't cross the RPC boundary), then persist the
        # serializable result through the daemon so the indexer does not
        # open memory.db directly.
        from nexus.db.t2.catalog_taxonomy import CatalogTaxonomy

        chroma_client = get_t3()._client
        same = CatalogTaxonomy.compute_assignments(
            collection, doc_ids, embeddings, chroma_client,
            cross_collection=False,
        )
        cross = CatalogTaxonomy.compute_assignments(
            collection, doc_ids, embeddings, chroma_client,
            cross_collection=True,
        )
        assignments = same + cross
        if assignments:
            t2_index_write(
                lambda db: db.taxonomy.persist_assignments(assignments)
            )
        if cross:
            import structlog
            structlog.get_logger().debug(
                "taxonomy_cross_collection_batch",
                collection=collection,
                cross_assigned=len(cross),
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

    Called via ``HookRegistry.fire_batch`` from every CLI indexing
    path immediately after ``t3.upsert_chunks_with_embeddings(...)``.
    Reads ``metadatas``; ignores ``contents`` and ``embeddings``. Opens a
    fresh T2Database (matching ``taxonomy_assign_batch_hook``'s
    lifecycle), delegates to the store-level
    ``dual_write_chash_index`` helper, and closes. Logs at debug level
    on any outer failure: a T2 failure must never abort the enclosing
    T3 write path.

    Wired by :func:`nexus.hook_registry.install_default_hooks` onto every
    runtime-constructed registry.
    """
    if not doc_ids or not metadatas:
        return
    try:
        from nexus.db.t2.chash_index import dual_write_chash_index

        # RDR-128 P1 (kg8sj): route through the daemon so the indexer does
        # not hold memory.db's writer lock; dual_write batches to one RPC.
        t2_index_write(
            lambda db: dual_write_chash_index(
                db.chash_index, collection, doc_ids, metadatas
            )
        )
    except Exception as exc:
        import structlog

        # RDR-129 B4 (nexus-uq8a4): a ``database is locked`` / ``busy`` failure
        # here is an *unrecovered* best-effort write — the daemon could not
        # commit the chash dual-write because memory.db's single WAL writer
        # slot was held. Meter it so the completeness gap is a number
        # ``nx doctor`` surfaces, not an invisible debug line (RDR-129 Gap 4).
        # A write that an inner retry recovers never reaches this except, so
        # the counter only ever counts true drops. Non-lock failures are a
        # different bug class and stay unmetered debug.
        msg = str(exc).lower()
        if "locked" in msg or "busy" in msg:
            try:
                from nexus.dropped_writes import record_drop

                record_drop(
                    hook="chash_dual_write_batch_hook",
                    collection=collection,
                    rows=len(doc_ids),
                    error=str(exc),
                )
            except Exception:
                pass  # metering must never break the best-effort hook
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
    that splits a document across multiple ``HookRegistry.fire_batch``
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
       Suggests ``/plugin update conexus@nexus-plugins`` or
       ``uv tool upgrade conexus`` depending on which side is older.
       The plugin and CLI ship from the same repo at the same version
       (CI enforces marketplace.json parity); drift means one update
       command was run without the other.

    Never blocks startup — both checks log warnings only.
    """
    import json
    from pathlib import Path

    import structlog

    log = structlog.get_logger()
    try:
        from importlib.metadata import version as _pkg_version
        from nexus.db.migrations import _parse_version

        cli_ver = _pkg_version("conexus")

        # ── (1) CLI ↔ T2 schema drift ────────────────────────────────────
        # RDR-120 P4: routed through T2Client when the daemon is reachable;
        # silently skipped when it isn't (best-effort drift warning, not a
        # gate). The daemon's ``database.hello`` op surfaces its stored
        # _nexus_version row.
        db_path = default_db_path()
        stored_ver: str | None = None
        if db_path.exists():
            try:
                from nexus.daemon.t2_client import (
                    T2DaemonNotReachableError,
                    make_t2_client,
                )

                client = make_t2_client()
                try:
                    hello = client.database.hello()
                    raw = (hello or {}).get("daemon_schema_version") or ""
                    stored_ver = raw if raw and raw != "0.0.0" else None
                finally:
                    client.close()
            except (T2DaemonNotReachableError, Exception):
                # Daemon unreachable or RPC failed — drift check is
                # best-effort. Operator can run `nx doctor` for the
                # full diagnostic.
                stored_ver = None
        if stored_ver is not None:
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
                plugin_name = manifest.get("name")
            except (OSError, json.JSONDecodeError):
                plugin_ver = None
                plugin_name = None

            # ── (3) Plugin NAME drift (nexus-mkj6u) ─────────────────────
            # The 2026-05-23 rename moved the plugin name from ``nx`` to
            # ``conexus``. Migration requires TWO Claude Code commands:
            # ``/plugin install conexus@nexus-plugins`` to register the
            # new plugin, then ``/reload-plugins`` to activate it.
            # Until both run, the local cache at
            # ``~/.claude/plugins/cache/nexus-plugins/nx/...`` continues
            # to back the OLD nx plugin name; the user is running the
            # NEW conexus CLI under that stale install. The earlier
            # nexus-v4m7y-adjacent guidance that ``/reload-plugins``
            # alone suffices was wrong — empirically confirmed when a
            # user on a fresh shell ran reload and saw no effect until
            # the explicit install ran.
            #
            # Fire this warning EVERY MCP startup until resolved. It is
            # the most reliable surface to catch the gap because every
            # Claude Code session spawns nx-mcp.
            if plugin_name and plugin_name != EXPECTED_PLUGIN_NAME:
                log.warning(
                    "plugin_name_mismatch",
                    installed_plugin_name=plugin_name,
                    expected_plugin_name=EXPECTED_PLUGIN_NAME,
                    hint=(
                        f"Plugin was renamed '{plugin_name}' -> "
                        f"'{EXPECTED_PLUGIN_NAME}' (nexus-mkj6u). In "
                        f"Claude Code, run: /plugin install "
                        f"{EXPECTED_PLUGIN_NAME}@nexus-plugins "
                        "&& /reload-plugins"
                    ),
                )

            if plugin_ver:
                cli_t = _parse_version(cli_ver)
                plugin_t = _parse_version(plugin_ver)
                if cli_t[:2] != plugin_t[:2]:
                    # Choose the actionable update for the lagging side.
                    if cli_t > plugin_t:
                        hint = f"plugin is older — run '/plugin update {EXPECTED_PLUGIN_NAME}@nexus-plugins' in Claude Code"
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


# Plugin identity (nexus-mkj6u 2026-05-23). The 2026-05-23 rename
# moved the Claude Code plugin name from ``nx`` to ``conexus``. The
# CLI knows its own identity; this constant is what
# ``check_version_compatibility`` and ``nexus.health`` compare
# against to detect drift in the installed plugin's manifest.
EXPECTED_PLUGIN_NAME: str = "conexus"


# ── Test injection ────────────────────────────────────────────────────────────


def reset_singletons():
    """Reset lazy singletons (for tests only).

    Search review I-2: also resets the T1 plan-match cache. Previously
    the plan cache survived ``reset_singletons()`` calls — tests that
    injected a fresh T1 but kept the populated plan cache saw stale
    embeddings against the injected client and produced nondeterministic
    matches.

    Post-store hook chains are owned by per-invocation ``HookRegistry``
    instances (see ``nexus.hook_registry``); they are no longer
    module-globals on ``mcp_infra`` and therefore not cleared here.
    """
    global _t1_instance, _t1_isolated, _t3_instance, _collections_cache
    _t1_instance = None
    _t1_isolated = False
    _t3_instance = None
    _collections_cache = ([], 0.0)
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
