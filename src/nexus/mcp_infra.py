# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""MCP server infrastructure: singletons, caching, test injection.

Separated from tool definitions (mcp_server.py) to isolate concerns.
"""
from __future__ import annotations

import os
import threading
import time

from nexus.config import default_db_path


def _parse_version(ver: str) -> tuple[int, ...]:
    """Parse a dotted version string into a comparable 3-component tuple.

    Normalises to exactly 3 components so ``(3, 7)`` doesn't compare
    less than ``(3, 7, 0)``.  Falls back to ``(0, 0, 0)`` for
    pre-release tags or malformed input.

    REHOMED from ``nexus.db.migrations`` in RDR-158 P4 Stage 4
    (nexus-i711w): the migration registry is deleted; this module's
    plugin↔CLI drift check is the surviving consumer.
    """
    try:
        parts = tuple(int(x) for x in ver.split(".")[:3])
        return parts + (0,) * (3 - len(parts))
    except ValueError:
        return (0, 0, 0)


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
        import structlog  # noqa: PLC0415 — structlog deferred to function scope (lazy logger init)
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

# nexus-53x7s: SERVICE-mode t2_index_write cache. Reuses one T2Database (and
# its 8 pooled httpx.Client connections) across calls instead of building one
# per write, which was defeating keep-alive pooling and drowning per-run logs
# in per-store connection-init noise (measured: 387 rebuilds/run, hooks ~13x
# the actual upload time).
#
# Process-lifetime singleton, NOT a TTL cache (review correction, nexus-53x7s
# stacked review 2026-07-05): each Http*Store bakes its base_url/token in at
# construction and never re-reads them, so a TTL window doesn't bound
# staleness against the thing that actually rotates -- the service_registry
# storage_service lease (15s TTL) -- it just rebuilds on an unrelated clock
# while still leaving up to that whole window stale. Recovery from a rotated
# lease is instead reactive: any write_fn failure evicts the cached instance
# so the NEXT call rebuilds against a freshly-resolved endpoint, mirroring
# the recover-on-error pattern already used by http_token_store/
# http_scratch_store (`recover_endpoint_from_lease`).
#
# `_service_t2_lock` (nexus-ldab2, CAS-narrowed): held only long enough to
# RESOLVE the singleton, never across write_fn's own network round trip. See
# `t2_index_write`'s docstring for the compare-and-swap eviction shape this
# replaced the old checkout-through-use span with — identical narrowing to
# `catalog/factory.py`'s `_service_catalog_lock` (nexus-u2u0n).
_service_t2_db: object | None = None
_service_t2_lock = threading.Lock()

#: nexus-0dpli: in-flight caller count per instance, keyed by ``id(db)``.
#: Same refcounted-eviction shape as ``catalog/factory.py``'s
#: ``_service_catalog_refcounts`` — see that module's comment for the full
#: rationale. Load-bearing here specifically because THIS bundle's own
#: ``taxonomy_assign_batch_hook.serialize = False`` is what first makes
#: genuinely concurrent ``t2_index_write`` calls against the shared
#: ``T2Database`` routine in production.
_service_t2_refcounts: dict[int, int] = {}

#: nexus-0dpli: ids of ``T2Database`` instances evicted from the shared slot
#: but still in flight for at least one caller — see
#: ``_release_shared_t2_ref``.
_service_t2_pending_close: set[int] = set()

#: nexus-ldab2: per-op ``{op: [calls, lock_wait_s, call_s]}`` for the
#: service-backed T2 singleton, mirroring
#: ``catalog/factory.service_catalog_op_stats()`` (nexus-jb4pp). *op* is the
#: caller-supplied label passed to :func:`t2_index_write` (defaults to
#: ``"t2_write"`` when the caller does not name one — most ``write_fn``
#: callables are anonymous lambdas, so a name has to be supplied explicitly
#: to be meaningful; see the ``taxonomy_assign``/``aspect_enqueue`` call
#: sites for the pattern). Counters are plain floats mutated under
#: ``_service_t2_stats_lock``, a DIFFERENT lock from ``_service_t2_lock``
#: itself — CAS narrowing means the singleton lock is no longer held across
#: the round trip, so the stats mutation needs its own guard rather than
#: piggybacking on whichever lock happened to be held.
_service_t2_op_stats: dict[str, list[float]] = {}
_service_t2_stats_lock = threading.Lock()


def _record_t2_op(op: str, wait_s: float, call_s: float) -> None:
    """Accumulate one op's timings, guarded by ``_service_t2_stats_lock``
    (not ``_service_t2_lock`` — see that dict's module comment)."""
    with _service_t2_stats_lock:
        row = _service_t2_op_stats.setdefault(op, [0.0, 0.0, 0.0])
        row[0] += 1
        row[1] += wait_s
        row[2] += call_s


def service_t2_op_stats() -> dict[str, dict[str, float]]:
    """Snapshot of per-op service-T2-singleton timings (nexus-ldab2).

    ``{op: {"calls": n, "lock_wait_s": s, "call_s": s}}`` — ``lock_wait_s``
    is time blocked on ``_service_t2_lock`` resolving the singleton (now a
    narrow, non-round-trip critical section); ``call_s`` is ``write_fn``
    itself (client serialization + network + server), run OUTSIDE the lock.
    Cumulative across threads, so both may exceed wall clock. Mirrors
    ``nexus.catalog.factory.service_catalog_op_stats()``.
    """
    with _service_t2_stats_lock:
        return {
            op: {"calls": v[0], "lock_wait_s": v[1], "call_s": v[2]}
            for op, v in _service_t2_op_stats.items()
        }


def reset_service_t2_op_stats() -> None:
    """Zero the per-op counters (test / per-run reset, mirrors
    ``nexus.catalog.factory.reset_service_catalog_op_stats()``)."""
    with _service_t2_stats_lock:
        _service_t2_op_stats.clear()


# nexus-7lw6a: process-lifetime-global counters for taxonomy-assign batch
# outcomes, mirroring ``_service_t2_op_stats`` above (same
# reset-at-run-start / read-at-run-end contract, same "must not accumulate
# across runs" rationale — nexus.indexer.index_repository resets this at
# the same point it resets the two op-stats dicts, and reads it once at the
# end of the run). ``attempted`` counts every real
# ``assign_from_chashes`` call (success or failure); ``failed_batches`` /
# ``failed_chunks`` count only the EXCEPTION path in
# ``taxonomy_assign_batch_hook`` — a whole batch's assignments lost to a
# transport/server failure (the GH #1432-class total-loss case this bead's
# exit-code policy keys on). The separate ``unmatched_chashes`` tripwire
# (a partial gap inside an otherwise-successful batch) is deliberately NOT
# folded in here: that batch DID succeed, so counting it would corrupt the
# "every batch failed" total-loss determination.
_taxonomy_assign_run_stats: dict[str, int] = {
    "attempted": 0, "failed_batches": 0, "failed_chunks": 0,
}
_taxonomy_assign_stats_lock = threading.Lock()


def _record_taxonomy_assign_attempt() -> None:
    """Count one ``assign_from_chashes`` call, whether it goes on to
    succeed or fail. Denominator for the "every batch failed" exit-code
    check in ``nexus.commands.index``."""
    with _taxonomy_assign_stats_lock:
        _taxonomy_assign_run_stats["attempted"] += 1


def _record_taxonomy_assign_batch_failure(chunk_count: int) -> None:
    """Count one whole-batch taxonomy-assign failure (the exception path)
    and the chunks it affected."""
    with _taxonomy_assign_stats_lock:
        _taxonomy_assign_run_stats["failed_batches"] += 1
        _taxonomy_assign_run_stats["failed_chunks"] += chunk_count


def taxonomy_assign_run_stats() -> dict[str, int]:
    """Snapshot of ``{attempted, failed_batches, failed_chunks}`` for the
    current run. Mirrors ``service_t2_op_stats()``'s snapshot contract."""
    with _taxonomy_assign_stats_lock:
        return dict(_taxonomy_assign_run_stats)


def reset_taxonomy_assign_run_stats() -> None:
    """Zero the counters (test / per-run reset, mirrors
    ``reset_service_t2_op_stats()``)."""
    with _taxonomy_assign_stats_lock:
        _taxonomy_assign_run_stats["attempted"] = 0
        _taxonomy_assign_run_stats["failed_batches"] = 0
        _taxonomy_assign_run_stats["failed_chunks"] = 0

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


#: nexus-brw1s (GH #1405): a callable the lifespan registers when T1 session
#: minting was DEFERRED at startup (storage service unreachable). Consulted by
#: get_t1() before first construction: it either completes the mint (env vars
#: land, the hook unregisters itself) or raises an actionable per-call error.
#: The seam lives HERE and the logic in mcp/core.py because core imports this
#: module, never the reverse.
_t1_pre_init_hook = None


def set_t1_pre_init_hook(hook) -> None:
    """Register (or clear, with None) the deferred-T1-mint hook."""
    global _t1_pre_init_hook
    _t1_pre_init_hook = hook


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

    nexus-brw1s: when the lifespan DEFERRED the T1 session mint (storage
    service unreachable at MCP start — the crash that used to take down the
    whole server and every non-T1 tool with it), the registered pre-init hook
    runs FIRST, under the same lock. It either completes the mint now (the
    service came up; env vars land and construction proceeds normally) or
    raises an actionable error for THIS call only — nothing is cached, so the
    next T1-touching call retries. The hook must run BEFORE construction:
    constructing with neither session env var set would silently route this
    MCP into the shared CLI-dedicated identity, the session-isolation
    regression the lifespan's own comments treat as security-relevant.
    """
    global _t1_instance, _t1_isolated
    if _t1_instance is None:
        with _t1_lock:
            if _t1_instance is None:
                if _t1_pre_init_hook is not None:
                    _t1_pre_init_hook()  # raises => propagate; cache stays empty
                from nexus.db.t1 import get_t1_database  # noqa: PLC0415 — deferred to avoid circular import (db.t1)
                _t1_instance = get_t1_database()
                # nexus-4lkmz: get_t1_database() with no injected client
                # can no longer return a T1Database (the InMemoryVectorClient
                # leg it used to construct is retired outright — T1 is
                # PG-only). Isolated-mode detection is therefore always
                # False on THIS path; ``inject_t1(t1, isolated=True)``
                # remains the way tests exercise the "[T1 isolated] "
                # operator prefix (an orthogonal, still-live test-support
                # feature — see tests/test_mcp_server.py).
                _t1_isolated = False
    return _t1_instance, _t1_isolated


def reset_t1_for_release() -> None:
    """Drop the cached T1 singleton so the next :func:`get_t1` call
    reconstructs against a freshly-swapped ``NX_T1_SESSION`` /
    ``NX_T1_SESSION_ID`` (nexus-d76vc T1 handoff re-lease).

    Deliberately narrower than :func:`reset_singletons` (which is
    test-only and also tears down T3, the catalog client, the plan
    cache, and search traces — the wrong blast radius for a production
    T1-only scope swap that must leave every other tool's live state
    untouched). This clears ONLY the T1 singleton, under the SAME lock
    :func:`get_t1` takes for construction, so a re-lease can never race a
    concurrent tool call's first-touch construction into observing a
    half-swapped state: either the call sees the OLD instance (built
    before the swap) or triggers a fresh construction against the NEW
    env vars, never a torn state in between.

    Safe to call even when no T1 singleton was ever constructed yet (a
    no-op) — the MCP lifespan's handoff watcher calls this
    unconditionally on every consumed marker.
    """
    global _t1_instance, _t1_isolated
    with _t1_lock:
        _t1_instance = None
        _t1_isolated = False


def get_t3():
    """Return the T3 handle singleton — lazy init on first call.

    RDR-155 P4a.2 (bead nexus-1k8s1): ``make_t3()`` itself returns the
    service-backed :class:`~nexus.db.http_vector_client.HttpVectorClient`
    whenever no test client is injected (the Chroma serving paths are
    retired), so the RDR-152 Seam B env-flag routing gate that used to live
    here is gone — there is exactly one production path. Capability checks
    on the returned handle use
    :func:`nexus.db.http_vector_client.is_service_backed`.
    """
    global _t3_instance
    if _t3_instance is None:
        with _t3_lock:
            if _t3_instance is None:
                from nexus.db import make_t3  # noqa: PLC0415 — deferred to avoid circular import (db make_t3)
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

    Remaining routable writers in mcp/core.py that still use _t2_ctx
    (not yet converted; see nexus-j5geq commit notes for audit):
    - memory.put (line ~1850): scratch_put tool
    - memory.delete (line ~1943): scratch_delete tool
    - memory.merge_memories (line ~2069): memory_consolidate tool
    - memory.flag_stale_memories (line ~2080): memory_consolidate tool
    - plans.increment_run_outcome (line ~3617): _nx_answer_record_outcome
    - plans.increment_run_started (line ~4012): nx_answer
    - (nexus-pyzk7, resolved) _nx_answer_record_run + _record_tier_write now
      route through db.telemetry.record_* (backend-blind: SQLite raw OR the
      service's /v1/telemetry/*/record endpoint), not a raw db.telemetry.conn.
    """
    from nexus.db.t2 import T2Database  # noqa: PLC0415 — deferred to avoid circular import (db.t2)
    return T2Database(default_db_path())  # boundary-allow: aspect_worker persist (document_aspects.upsert AspectRecord arg cannot round-trip the daemon RPC); not the every-poll hot path (RDR-128 P3)




def _acquire_shared_t2_ref(db: object) -> None:
    """Record one more in-flight caller against *db*. Callers MUST hold
    ``_service_t2_lock`` and call this immediately after resolving the
    instance, in the SAME critical section (nexus-0dpli — identical
    contract to ``catalog/factory._acquire_shared_catalog_ref``)."""
    key = id(db)
    _service_t2_refcounts[key] = _service_t2_refcounts.get(key, 0) + 1


def _release_shared_t2_ref(db: object, *, evict: bool) -> bool:
    """Release this caller's in-flight reference to *db*. Callers MUST hold
    ``_service_t2_lock``. Identical contract to
    ``catalog/factory._release_shared_catalog_ref`` — see that function's
    docstring for the full CAS-plus-refcount reasoning. Returns True
    exactly when the CALLER must run ``db.close()`` itself after releasing
    the lock."""
    global _service_t2_db
    if evict and _service_t2_db is db:
        _service_t2_db = None
    key = id(db)
    remaining = _service_t2_refcounts.get(key, 1) - 1
    if remaining > 0:
        _service_t2_refcounts[key] = remaining
        if evict:
            _service_t2_pending_close.add(key)
        return False
    _service_t2_refcounts.pop(key, None)
    was_pending = key in _service_t2_pending_close
    _service_t2_pending_close.discard(key)
    return evict or was_pending


def _service_t2_write_locked(write_fn, *, op: str = "t2_write"):
    """Resolve the process-lifetime service ``T2Database`` singleton
    (nexus-53x7s) and run ``write_fn`` against it.

    CAS-NARROWED (nexus-ldab2, identical shape to
    ``catalog/factory.py``'s ``_SharedServiceCatalogHandle._call``,
    nexus-u2u0n): ``_service_t2_lock`` is held ONLY to resolve (get-or-build)
    the singleton — never across ``write_fn``'s own network round trip.

    REFCOUNTED EVICTION (nexus-0dpli, critique finding on the first cut of
    this narrowing): releasing the lock around the round trip means
    MULTIPLE threads can be genuinely mid-``write_fn`` against the SAME
    shared ``T2Database`` at once — this bundle's own
    ``taxonomy_assign_batch_hook.serialize = False`` is what first makes
    that routine in production. Blast radius if an eviction naively
    ``close()``s the instance it evicts: ``T2Database.close()`` tears down
    ALL its substores' own httpx clients (memory, plans, taxonomy,
    telemetry, chash_index, document_aspects, aspect_queue,
    document_highlights), so one failing call (e.g. a routine
    aspect-enqueue conflict) could abort an unrelated, healthy, concurrent
    ``taxonomy_assign`` call sharing the same singleton. Fixed identically
    to the catalog handle:

    1. Eviction fires ONLY on a genuine connectivity failure
       (:func:`nexus.retry._is_connectivity_error`) — never on a routine
       domain/business exception. Under-evict, never over-evict (see
       ``catalog/factory.py``'s docstring for the full asymmetry argument).
    2. Every caller acquires an in-flight reference on the instance it
       resolved (:func:`_acquire_shared_t2_ref`) and releases it
       (:func:`_release_shared_t2_ref`) once its own call returns or
       raises, both under ``_service_t2_lock``. An eviction clears the
       shared slot immediately (new callers always build fresh) but only
       physically closes the OLD instance once its reference count drains
       to zero — the evicting caller if no one else was using it, or
       whichever sibling's release happens to be the last one out.

    RELEASE IS UNCONDITIONAL (nexus-0dpli round 3, delta-review touch-up):
    the reference release lives in a single ``finally``, not duplicated
    across ``except``/``else`` branches — a ``BaseException`` that is not
    a plain ``Exception`` (``KeyboardInterrupt``/``SystemExit`` mid-call)
    must still release this call's reference, or it leaks forever and can
    strand a sibling's already-evicted, pending-close instance with no one
    left to drain it to zero. ``_evict`` defaults to ``False`` and is set
    ``True`` only inside ``except Exception``, so a non-``Exception``
    ``BaseException`` correctly releases WITHOUT evicting — the same safe
    default as no exception at all.

    *op* differentiates :func:`service_t2_op_stats`'s timing buckets the
    same way ``catalog/factory.service_catalog_op_stats()`` differentiates
    by attribute name — most ``write_fn`` callables are anonymous lambdas,
    so a caller that wants a meaningful bucket (e.g. ``"taxonomy_assign"``)
    must pass it explicitly; unnamed callers share the ``"t2_write"``
    default bucket.
    """
    global _service_t2_db
    from nexus.db.t2 import T2Database  # noqa: PLC0415 — deferred to avoid circular import (db.t2)

    _w0 = time.monotonic()
    with _service_t2_lock:
        _wait = time.monotonic() - _w0
        if _service_t2_db is None:
            _service_t2_db = T2Database(default_db_path(), run_migrations=False)  # boundary-allow: service mode, PG is the arbiter
        current = _service_t2_db
        _acquire_shared_t2_ref(current)
    _c0 = time.monotonic()
    _evict = False
    try:
        return write_fn(current)
    except Exception as exc:
        # Only a genuine connectivity failure evicts — see the docstring's
        # point 1. ``_evict`` stays False (the safe default) for any OTHER
        # exception, including a BaseException that skips this clause
        # entirely (KeyboardInterrupt/SystemExit — see the docstring's
        # "RELEASE IS UNCONDITIONAL" note).
        from nexus.retry import _is_connectivity_error  # noqa: PLC0415 — deferred to avoid import cost on the happy path
        _evict = _is_connectivity_error(exc)
        raise
    finally:
        # nexus-0dpli round 3: release lives in ONE unconditional finally,
        # not duplicated across except/else — a BaseException that
        # _is_connectivity_error never sees (KeyboardInterrupt/SystemExit)
        # must still release this call's reference, or it leaks and can
        # strand a sibling's already-evicted, pending-close instance
        # forever.
        with _service_t2_lock:
            _close_now = _release_shared_t2_ref(current, evict=_evict)
        if _close_now:
            current.close()  # error-triggered eviction, not per-call teardown
        _record_t2_op(op, _wait, time.monotonic() - _c0)


def t2_index_write(write_fn, *, op: str = "t2_write"):
    """Run one T2 write against the service-backed :class:`T2Database`.

    ``write_fn(db)`` receives the writer and its return value is passed back,
    so callers that need it (the aspect_worker's ``claim_batch`` rows,
    ``rename_collection_cascade``'s per-store counts) can route too;
    fire-and-forget callers simply ignore it.

    WAS A DAEMON ROUTER (RDR-128 P1/P3, nexus-kg8sj/sbxbe.3). The T2 daemon
    existed to stop ``nx index repo`` from opening ``memory.db`` directly and
    holding its single WAL writer slot — a SQLite-only problem. The engine owns
    write serialization, so the daemon, its reachability probe and its
    version-skew arm are gone (nexus-i711w Stage 2 sub-stage B), and the
    direct-SQLite arm that outlived them went with the ``=sqlite`` opt-out
    (RDR-158 P3, nexus-7bomn).

    CAS-NARROWED (nexus-ldab2): see :func:`_service_t2_write_locked`'s
    docstring — ``_service_t2_lock`` no longer spans this call's network
    round trip, only the singleton's get-or-build decision. *op* is threaded
    straight through for :func:`service_t2_op_stats` attribution.
    """
    return _service_t2_write_locked(write_fn, op=op)


# ── T1 plan session cache (RDR-078) ──────────────────────────────────────────
# Plan-cache wrappers. The cache state itself lives in
# nexus.mcp.plan_cache_registry.PlanCacheRegistry as a single module-
# level singleton (nexus-sl69o, 2026-05-20). These two functions
# preserve the historical public API; new code may prefer
# get_plan_cache_registry().get(...) directly.


def get_t1_plan_cache(*, populate_from=None):
    """Return the T1 ``plans__session`` cache, lazy-populated on first call.

    When *populate_from* (a PlanLibrary) is supplied, the cache is
    populated from its rows on first call and repopulated no less often
    than every ``_HTTP_PLAN_LIBRARY_STALENESS_SECONDS`` seconds
    (nexus-ie7o8, bounded staleness). The SQLite file-mtime refresh that
    once sat beside it (nexus-qgjr) died with the SQLite plan library
    (nexus-x1de2 (54)).

    Returns ``None`` when no T1 client is reachable; the matcher falls
    back to FTS5 in that case. Subsequent calls after an init failure
    return ``None`` immediately without re-entering the lock.

    Backed by :class:`nexus.mcp.plan_cache_registry.PlanCacheRegistry`.
    """
    from nexus.mcp.plan_cache_registry import get_plan_cache_registry  # noqa: PLC0415 — deferred to avoid circular import (mcp.plan_cache_registry)
    return get_plan_cache_registry().get(populate_from=populate_from)


def reset_plan_cache_for_tests() -> None:
    """Test helper: drop the cache so the next call re-initialises.

    Backed by
    :func:`nexus.mcp.plan_cache_registry.reset_plan_cache_registry_for_tests`.
    """
    from nexus.mcp.plan_cache_registry import reset_plan_cache_registry_for_tests  # noqa: PLC0415 — deferred to avoid circular import (mcp.plan_cache_registry)
    reset_plan_cache_registry_for_tests()


# ── Catalog management ────────────────────────────────────────────────────────


def get_catalog():
    """Return a catalog reader (HttpCatalogClient) or None when unavailable.

    RDR-146 P1.2: get_catalog() is the READ funnel. Writers must use
    :func:`get_catalog_writer`; the boundary lint bans bare catalog
    construction in consumer code. Since the local SQLite catalog's
    deletion (RDR-158 P4, nexus-i711w) the factory is service-only —
    this returns the shared ``HttpCatalogClient`` against the engine.
    """
    from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415 — deferred to avoid circular import (catalog.factory)
    return make_catalog_reader()


def get_catalog_writer():
    """Return a write-only catalog proxy (RDR-146 P1.2).

    Routes the whitelisted write ops (``CATALOG_WRITE_OPS``) to the
    engine's HTTP catalog via ``_ServiceCatalogWriter`` (the T2-daemon
    routing this originally described died in RDR-158 P4, nexus-i711w).
    Always returns a writer proxy; callers ``.close()`` it when done.

    RDR-146 P2 (nexus-5p2ci.12): MCP tool invocations are user-initiated and
    latency-sensitive (``store_put`` / ``memory promote`` register through
    ``catalog/store_hook.py``). The MCP server process is non-tty, so the
    ``isatty()`` fallback would misclassify these as batch and make them yield
    to (or be deferred behind) a background index burst. Tag interactive so
    they take fairness priority, same as the foreground ``nx dt`` writes.
    """
    from nexus.catalog.factory import make_catalog_writer  # noqa: PLC0415 — deferred to avoid circular import (catalog.factory)
    return make_catalog_writer(priority="interactive")


def require_catalog():
    """Return (catalog, None) or (None, error_message)."""
    cat = get_catalog()
    if cat is None:
        return None, "Catalog not initialized — run 'nx catalog setup' to create and populate it"
    return cat, None


def catalog_auto_link(tumbler: str) -> int:
    """Create catalog links from T1 link-context to the just-stored document.

    Takes the catalog TUMBLER, not a T3 chash (nexus-5axey class A3).
    The predecessor took the chash and resolved it via ``by_doc_id``, which
    asks a DIFFERENT question on each substrate — service mode tumbler-resolves
    it, so a chash never matched and this returned 0 for every agent-stored
    document on a service install, with a DEBUG line as the only signal. That
    is the same silence nexus-a414 already had to fix once here. The caller
    (mcp/core.py store_put) has the real tumbler in scope as
    ``catalog_doc_id``; taking it directly removes both the wrong-key lookup
    and a round trip. Tumbler is the only document identity (RDR-108, Hal
    2026-07-26).

    Returns the number of links actually created (backward-compat int).
    Skip counts are surfaced via structlog: WARNING for invalid tumbler
    skips (recipe-compliance gap), DEBUG for missing endpoint skips
    (legitimate cleanup signal). nexus-a414 made these visible after
    the prior all-DEBUG behaviour silently swallowed every recipe-
    compliant call that produced zero links.
    """
    import structlog  # noqa: PLC0415 — structlog deferred to function scope (lazy logger init)
    _log = structlog.get_logger()

    # RDR-146 P1.2: fires on every store_put with T1 link-context entries in
    # the long-lived MCP server. Close the read handle on every exit path so
    # SQLite connections do not accumulate across a session.
    cat = get_catalog()
    if cat is None:
        return 0
    try:
        t1, _ = get_t1()
        entries = t1.list_entries()
        link_entries = [
            e for e in entries
            if "link-context" in {t.strip() for t in (e.get("tags") or "").split(",")}
        ]
        if not link_entries:
            return 0
        if not tumbler:
            # The catalog hook failed upstream, so there is no document to
            # link to. Distinct from "no link contexts": worth a signal.
            _log.debug("auto_link_skip_no_catalog_tumbler")
            return 0
    finally:
        try:
            cat.close()  # nexus-qnp5s: HttpCatalogClient.close() is safe; Catalog._db.close() is internal
        except Exception:  # noqa: BLE001 — best-effort handle cleanup in finally; close failure must not mask the real result
            pass
    from nexus.catalog.auto_linker import auto_link, read_link_contexts  # noqa: PLC0415 — deferred to avoid circular import (catalog.auto_linker)
    contexts = read_link_contexts(link_entries)
    # RDR-146 P1.2: auto_link writes (link_if_absent) — route through the
    # write-only daemon proxy, not the read-only get_catalog() handle.
    writer = get_catalog_writer()
    try:
        result = auto_link(writer, tumbler, contexts)
    finally:
        writer.close()

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
            tumbler=tumbler,
            created=result.created,
            skipped_invalid_tumbler=result.skipped_invalid_tumbler,
            skipped_missing_endpoint=result.skipped_missing_endpoint,
            recipe_compliant_zero=recipe_compliant_zero,
        )
    return result.created


def resolve_tumbler_mcp(cat, value):
    """Resolve tumbler string OR title/filename. Returns (tumbler, None) or (None, error)."""
    from nexus.catalog import resolve_tumbler  # noqa: PLC0415 — deferred to avoid circular import (catalog)
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


def _record_taxonomy_tripwire(
    collection: str, doc_ids: list, error: str, *, kind: str = "",
) -> None:
    """nexus-gednd: loudness tripwire for taxonomy assignment (the RDR-172
    aspect-enqueue pattern). The assign hook is best-effort — it swallows its
    own exceptions so fire_batch sees success and hook_registry records NO
    hook_failures row, making topic-scoped search silently incomplete.
    Persist a structured row directly (itself best-effort: a telemetry-write
    failure must never block indexing) and log at warning, not debug.
    """
    import structlog  # noqa: PLC0415 — structlog deferred to function scope (lazy logger init)
    # ``kind`` suffixes the hook_name so nx taxonomy status can distinguish a
    # designed SKIP (embed_unavailable — an eventual-consistency race, not a
    # bug) from a genuine assignment failure (critique 2026-07-13).
    hook_name = "taxonomy_assign_batch_hook" + (f".{kind}" if kind else "")
    structlog.get_logger().warning(
        "taxonomy_assign_batch_failed",
        collection=collection,
        batch_size=len(doc_ids),
        kind=kind or "failure",
        error=error[:500],
    )
    try:
        t2_index_write(
            lambda t2: t2.telemetry.record_hook_failure(
                doc_id=(doc_ids[0] if doc_ids else ""),
                collection=collection,
                hook_name=hook_name,
                error=error[:2000],
                chain="batch",
            )
        )
    except Exception:  # noqa: BLE001 — tripwire persist is best-effort; never block indexing
        structlog.get_logger().warning(
            "taxonomy_tripwire_persist_failed", collection=collection, exc_info=True,
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

    Called via ``HookRegistry.fire_batch`` from every storage event. Since
    nexus-yu9w5, ``doc_ids`` (the chunk chashes just upserted into
    ``collection`` by THIS SAME flush) is the only chunk-identifying input
    the hook needs — assignment is computed server-side via
    ``POST /v1/taxonomy/assignments/assign_from_chashes`` from the
    already-persisted ``chunks_<dim>`` rows. ``contents``, ``embeddings``,
    and ``metadatas`` are accepted for ``HookRegistry.fire_batch``'s shared
    call shape but not read.

    No-op when the local-mode exclude-collections config matches, or the
    engine reports no assignable chashes (empty ``doc_ids``). No client-side
    fallback: an engine that lacks the route fails the batch loud via the
    RDR-172 tripwire (a ``hook_failures`` row + a warning log), never a
    silent client-side recompute.

    Wired by :func:`nexus.hook_registry.install_default_hooks` onto every
    runtime-constructed registry.
    """
    from fnmatch import fnmatch  # noqa: PLC0415 — stdlib fnmatch deferred to function scope

    from nexus.config import is_local_mode, load_config  # noqa: PLC0415 — deferred to avoid circular import (config)

    if not doc_ids:
        return

    # RDR-152 Seam B: taxonomy-via-Chroma-client is not supported on the
    # service path.  HttpVectorClient has no ._client attribute; accessing it
    # would raise AttributeError, swallowed by the bare except below, causing
    # silent taxonomy loss.  Log once and return cleanly.  Taxonomy-on-service
    # is a tracked follow-on (bead nexus-gmiaf.21+).
    # RDR-155 P4a.2 (nexus-1k8s1): guard is INSTANCE-based — the env flag and
    # the handle type diverge now that make_t3() returns the service client
    # unconditionally while tests inject chroma-backed T3Database fixtures.
    from nexus.db.http_vector_client import is_service_backed  # noqa: PLC0415 — deferred to avoid circular import (http_vector_client)
    if is_service_backed(get_t3()):
        # nexus-yu9w5 (lns3o client half): server-side compute-and-persist via
        # POST /v1/taxonomy/assignments/assign_from_chashes. The engine
        # already holds both the embeddings (chunks_<dim>, just upserted by
        # THIS SAME flush) and the centroids (taxonomy_centroids_<dim>), so
        # compute-and-persist collapses into ONE round trip — eliminating the
        # per-flush ~3MB embedding re-download + the client-side
        # compute_assignments/persist_assignments dance this route replaces
        # (T2 flush-tail-attribution-2026-08-07: the 50.9% flush-grain hook
        # term). ``doc_ids`` ARE the chunk chashes here — the T3 natural
        # chunk id every upsert call site in this codebase passes as
        # ``ids=`` — so no re-fetch and no client-side cosine math are
        # needed at all; the nexus-reskd/h8rf6.11 empty-placeholder-embedding
        # dance this replaced no longer applies (``embeddings`` is unread).
        if is_local_mode():
            exclude = load_config().get("taxonomy", {}).get("local_exclude_collections", [])
            if any(fnmatch(collection, pat) for pat in exclude):
                return
        # NO FALLBACK (nexus-yu9w5, mirrors the nexus-sghyo precedent): an
        # engine below REQUIRED_ENGINE_VERSION that lacks this route 404s;
        # that (or any other transport failure) is caught below and reported
        # via the SAME tripwire every other service-path failure uses — the
        # hook fails loud and reports, it never recomputes client-side.
        # nexus-7lw6a: counted regardless of outcome — the denominator the
        # run-summary exit-code check uses to tell "some batches failed"
        # from "every batch failed" (total loss).
        _record_taxonomy_assign_attempt()
        try:
            result = t2_index_write(
                lambda db: db.taxonomy.assign_from_chashes(
                    collection, doc_ids, cross_collection=True,
                ),
                op="taxonomy_assign",
            )
        except Exception as exc:  # noqa: BLE001 — taxonomy service path best-effort; tripwire-recorded, returns
            _record_taxonomy_tripwire(
                collection, doc_ids, f"service path: {type(exc).__name__}: {exc}",
            )
            # nexus-7lw6a: this IS the whole-batch-loss case the bead is
            # about (e.g. an HTTP 500 from the assign endpoint) — every
            # doc_id in this batch lost its taxonomy assignment.
            _record_taxonomy_assign_batch_failure(len(doc_ids))
            return
        unmatched = result.get("unmatched_chashes") if isinstance(result, dict) else None
        if unmatched:
            # Route contract: a chash never actually upserted into
            # `collection` is named here rather than silently dropped. The
            # batch's OTHER chashes still assigned — not a hard failure, but
            # a real gap (topic-scoped search misses these chunks) worth the
            # same loudness the RDR-172 tripwire pattern gives every other
            # taxonomy-assignment gap.
            _record_taxonomy_tripwire(
                collection, unmatched,
                f"assign_from_chashes: {len(unmatched)}/{len(doc_ids)} chashes "
                "unmatched in chunks_<dim> for this collection — never "
                "upserted, or upserted under a different collection",
                kind="unmatched_chashes",
            )
        return

    # NO RAW PATH BELOW THIS POINT. A ~40-line twin stood here: it read
    # ``get_t3()._client`` and called ``CatalogTaxonomy.compute_assignments``
    # twice (same + cross), then persisted via ``t2_index_write``. It was
    # already unreachable on every shipping install — the guard above returns
    # unconditionally whenever T3 is service-backed, which is the default in
    # BOTH local and cloud mode since RDR-152 — and its two entry points
    # (``._client`` on an HttpVectorClient, and CatalogTaxonomy itself) are
    # deleted in nexus-i711w Stage 2 sub-stage C. Reaching this line at all
    # would now mean a non-service T3 handle, which no longer exists.


# nexus-duoak: FLUSH grain, not per-file. The batched indexer fires this once
# per flush so clustering sees a whole batch of embeddings rather than one
# file's worth. This declaration sat at the tail of the raw arm deleted above
# and went with it — a silent demotion to "file" grain that changed dispatch
# frequency without changing any behaviour the deletion was about. Restored,
# and pinned by tests/test_hook_grain.py.
taxonomy_assign_batch_hook.batch_grain = "flush"

# nexus-eslkl: opts OUT of LockedHookRegistry's per-hook serialization.
# Sole justification (corrected from the design memo's original framing,
# which also cited _service_t2_lock — that leg is REMOVED by nexus-ldab2's
# CAS narrowing of the very same lock, so it cannot be part of this claim
# any more): server-side idempotency ALONE. TaxonomyRepository.assignFromChashes
# runs in ONE tenantScope transaction; the own-collection pass is
# `ON CONFLICT DO NOTHING` and the cross-collection pass is `GREATEST`-wins —
# both commutative and safely re-runnable under any interleaving. Per-flush
# chash sets are disjoint by construction (ChunkBatcher is file-atomic), so
# concurrent fires of this hook never contend for the same row. Nothing else
# about this hook's execution needs mutual exclusion with itself or with any
# other registered hook.
taxonomy_assign_batch_hook.serialize = False


# _fetch_or_embed lived here. It fetched T3 embeddings for a batch and fell
# back to a local MiniLM encode, and its ONLY caller was the raw taxonomy-assign
# path deleted above (nexus-i711w Stage 2 sub-stage C). The service branch does
# its own re-fetch via ``get_t3().get_embeddings`` with a count-skew guard and
# tripwire (nexus-reskd / nexus-h8rf6.11), so nothing was left to call this —
# one of its own tests asserted it returns None in service mode, i.e. a no-op on
# the only surviving path.


# ── Manifest-write failure surfacing (GH #1371) ──────────────────────────────
#
# manifest_write_batch_hook is best-effort by contract (nexus-zq79): it must
# never propagate a failure to the indexing caller. Before this fix the only
# signal on a persistent failure (a real 4xx, or a connection-class error
# that outlasted the retry in _manifest_write_with_retry) was a single
# structlog WARNING — invisible unless an operator had log capture wired up.
# The reported incident found 17 of 24 audited documents silently missing
# their catalog_document_chunks manifest linkage this way. This collector
# lets `nx index`'s end-of-run summary surface the gap directly, with the
# remediation command (`nx catalog reconcile`).
_manifest_write_failures_lock = threading.Lock()
_MANIFEST_WRITE_FAILURES: list[str] = []


def get_manifest_write_failures() -> list[str]:
    """Return the doc_ids whose manifest write failed this process/run.

    Snapshot copy (safe to iterate without holding the lock).
    """
    with _manifest_write_failures_lock:
        return list(_MANIFEST_WRITE_FAILURES)


def reset_manifest_write_failures() -> None:
    """Clear the collector. CLI callers invoke this at the start of an
    indexing run so the end-of-run summary reflects only that run's
    failures (mirrors ``nexus.retry.reset_retry_stats``)."""
    with _manifest_write_failures_lock:
        _MANIFEST_WRITE_FAILURES.clear()


def _record_manifest_write_failure(doc_id: str) -> None:
    with _manifest_write_failures_lock:
        _MANIFEST_WRITE_FAILURES.append(doc_id)


# GH #1397 / nexus-94fxl: batches the manifest hook DROPPED because no chunk in
# the batch carried a document identity (no catalog_doc_id from the caller, no
# legacy meta doc_id). Distinct from _MANIFEST_WRITE_FAILURES (a write that was
# ATTEMPTED and failed): these batches never reach the write at all, so a clean
# "0 failed" end-of-run summary said nothing about them — the documents' catalog
# rows stayed at chunk_count=0 and were invisible to catalog-aware search.
_manifest_identity_drops_lock = threading.Lock()
_MANIFEST_IDENTITY_DROPS: list[dict] = []


def get_manifest_identity_drops() -> list[dict]:
    """Batches dropped for missing doc identity this process/run.

    Each entry: ``{"collection": str, "batch_size": int}``. Snapshot copy.
    """
    with _manifest_identity_drops_lock:
        return [dict(d) for d in _MANIFEST_IDENTITY_DROPS]


def reset_manifest_identity_drops() -> None:
    """Clear the collector (CLI callers reset at the start of an indexing run,
    mirroring ``reset_manifest_write_failures``)."""
    with _manifest_identity_drops_lock:
        _MANIFEST_IDENTITY_DROPS.clear()


def _record_manifest_identity_drop(collection: str, batch_size: int) -> None:
    with _manifest_identity_drops_lock:
        _MANIFEST_IDENTITY_DROPS.append(
            {"collection": collection, "batch_size": batch_size}
        )


# nexus-5xn3k.4 (RUNFENCE): docs whose manifest rows were written correctly but
# whose completion stamp was REFUSED by the engine's fail-closed verify
# (missing > 0 or referenced != chunk_count). Distinct from both collectors
# above: the write SUCCEEDED (over-work-never-under-work holds — no data lost),
# but the document is not actually whole in T3, index_state stays 'indexing',
# and the run MUST NOT report it as fully indexed. Silently discarding these
# while reporting the batch successful reproduces, one layer up, the exact
# "Indexed N record(s)" silent-success shape the fence exists to close.
_complete_refusals_lock = threading.Lock()
_COMPLETE_REFUSALS: list[str] = []


def get_complete_refusals() -> list[str]:
    """doc_ids whose fail-closed completion stamp was refused this
    process/run. Snapshot copy."""
    with _complete_refusals_lock:
        return list(_COMPLETE_REFUSALS)


def reset_complete_refusals() -> None:
    """Clear the collector (CLI callers reset at the start of an indexing
    run, mirroring ``reset_manifest_write_failures``)."""
    with _complete_refusals_lock:
        _COMPLETE_REFUSALS.clear()


# nexus-u8n4r: files whose REGISTERED path (owner repo_root + relative
# path, or the stored absolute file_path) sits under an agent worktree or
# a system temp dir, and were refused registration rather than silently
# polluting the primary owner's collections (the 4,002-doc rdr__1-1
# orphan class, 2026-08-03 production cleanup). A structlog WARNING
# already fires per file at each guard call site
# (``ephemeral_path_registration_skipped``); this collector backs the
# end-of-run CLI summary, mirroring ``_MANIFEST_WRITE_FAILURES``.
_ephemeral_registration_skips_lock = threading.Lock()
_EPHEMERAL_REGISTRATION_SKIPS: list[dict] = []


def get_ephemeral_registration_skips() -> list[dict]:
    """Paths refused registration this process/run under the nexus-u8n4r
    worktree/tempdir guard. Each entry: ``{"path": str, "owner": str,
    "reason": str}`` — ``reason`` is one of ``"worktree_or_tempdir"``
    (structurally under a worktree/tempdir marker) or
    ``"worktree_unique_no_main_mirror"`` (worktree-invoked indexing of a
    file with no mirror in the main repo — an uncommitted draft that
    didn't make it into the index). Snapshot copy."""
    with _ephemeral_registration_skips_lock:
        return [dict(d) for d in _EPHEMERAL_REGISTRATION_SKIPS]


def reset_ephemeral_registration_skips() -> None:
    """Clear the collector (CLI callers reset at the start of an indexing
    run, mirroring ``reset_manifest_write_failures``)."""
    with _ephemeral_registration_skips_lock:
        _EPHEMERAL_REGISTRATION_SKIPS.clear()


def _record_ephemeral_registration_skip(path: str, owner: str, reason: str = "") -> None:
    with _ephemeral_registration_skips_lock:
        _EPHEMERAL_REGISTRATION_SKIPS.append(
            {"path": path, "owner": owner, "reason": reason}
        )


def _record_complete_refusal(doc_id: str) -> None:
    # Idempotent per doc_id: a duplicated engine response row (or the
    # count-mismatch conservative branch overlapping the listed refusals)
    # must not double-count once .6 wires a count-based consumer.
    with _complete_refusals_lock:
        if doc_id not in _COMPLETE_REFUSALS:
            _COMPLETE_REFUSALS.append(doc_id)


# nexus-39upx hazard 4 (honest output): the in-band superseded-vector sweep
# (_sweep_superseded_vectors below) previously reported ONLY via structlog —
# invisible without log capture wired up, the exact "every catalog-level
# check reports clean" shape this bead exists to close one layer up. Mirrors
# _MANIFEST_WRITE_FAILURES / _COMPLETE_REFUSALS: a per-process collector the
# CLI summary layer (commands/_helpers.py) reads and resets around a run.
_superseded_sweep_stats_lock = threading.Lock()
_SUPERSEDED_SWEEP_SWEPT_TOTAL = 0
_SUPERSEDED_SWEEP_SKIPS: list[dict] = []


def get_superseded_sweep_stats() -> dict:
    """Superseded-vector sweep outcomes this process/run.

    ``{"swept": int, "skipped": [{"doc_id": str, "collection": str,
    "reason": str}, ...]}``. ``swept`` is the total count of T3 rows
    actually deleted; ``skipped`` names every run where the sweep could
    not complete (and therefore may have left superseded rows searchable)
    — never silent. Snapshot copy.
    """
    with _superseded_sweep_stats_lock:
        return {
            "swept": _SUPERSEDED_SWEEP_SWEPT_TOTAL,
            "skipped": [dict(d) for d in _SUPERSEDED_SWEEP_SKIPS],
        }


def reset_superseded_sweep_stats() -> None:
    """Clear the collector (CLI callers reset at the start of an indexing
    run, mirroring ``reset_manifest_write_failures``)."""
    global _SUPERSEDED_SWEEP_SWEPT_TOTAL
    with _superseded_sweep_stats_lock:
        _SUPERSEDED_SWEEP_SWEPT_TOTAL = 0
        _SUPERSEDED_SWEEP_SKIPS.clear()


def _record_superseded_swept(count: int) -> None:
    global _SUPERSEDED_SWEEP_SWEPT_TOTAL
    if count <= 0:
        return
    with _superseded_sweep_stats_lock:
        _SUPERSEDED_SWEEP_SWEPT_TOTAL += count


def _record_superseded_sweep_skip(doc_id: str, collection: str | None, reason: str) -> None:
    with _superseded_sweep_stats_lock:
        _SUPERSEDED_SWEEP_SKIPS.append(
            {"doc_id": doc_id, "collection": collection or "", "reason": reason}
        )


# nexus-2t63u round 2 (substantive-critic observation 4): a per-process
# collector for ``doc_indexer._register_or_lookup_doc_id``'s
# ``physical_collection`` reconciliation, mirroring the
# ``_SUPERSEDED_SWEEP_SWEPT_TOTAL`` shape exactly (single int, not a list —
# a reconciliation is a single per-document event, no per-item detail worth
# retaining beyond the WARNING log line already emitted at the reconcile
# site). A ``--dir`` batch that mass-retargets --collection by accident
# should surface that count in the run's own summary line, not require the
# operator to scroll back through WARNING-level structlog output to notice.
_reconciled_collections_lock = threading.Lock()
_RECONCILED_COLLECTIONS_COUNT = 0


def get_reconciled_collections_count() -> int:
    """Count of ``physical_collection`` reconciliations this process/run."""
    with _reconciled_collections_lock:
        return _RECONCILED_COLLECTIONS_COUNT


def reset_reconciled_collections_count() -> None:
    """Clear the collector (CLI callers reset at the start of an indexing
    run, mirroring ``reset_superseded_sweep_stats``)."""
    global _RECONCILED_COLLECTIONS_COUNT
    with _reconciled_collections_lock:
        _RECONCILED_COLLECTIONS_COUNT = 0


def _record_physical_collection_reconciled() -> None:
    global _RECONCILED_COLLECTIONS_COUNT
    with _reconciled_collections_lock:
        _RECONCILED_COLLECTIONS_COUNT += 1


def manifest_write_batch_hook(
    doc_ids: list[str],
    collection: str,
    contents: list[str],
    embeddings: list[list[float]] | None,
    metadatas: list[dict] | None,
    *,
    catalog_doc_id: str = "",
    manifest_complete: dict[str, str] | None = None,
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
    from collections import defaultdict  # noqa: PLC0415 — stdlib collections deferred to function scope

    by_doc: dict[str, list[tuple[int, dict]]] = defaultdict(list)
    for i, meta in enumerate(metadatas):
        doc_id = catalog_doc_id or meta.get("doc_id", "")
        if doc_id:
            by_doc[doc_id].append((i, meta))
    if not by_doc:
        # GH #1397 / nexus-94fxl: this was a zero-log return — chunks landed
        # in T3 but the manifest was never written, leaving the document's
        # catalog row at chunk_count=0 and invisible to catalog-aware search,
        # with the end-of-run summary reporting a clean "0 failed". Record it
        # for the run summary and log loud.
        import structlog  # noqa: PLC0415 — structlog deferred to function scope (lazy logger init)
        _record_manifest_identity_drop(collection, len(metadatas))
        structlog.get_logger().warning(
            "manifest_hook_batch_missing_doc_identity",
            collection=collection,
            batch_size=len(metadatas),
        )
        return
    # RDR-146 P1.2: gate on the read handle (None when the catalog is
    # uninitialised — the old skip), then route the manifest WRITES through
    # the write-only daemon proxy. The read-guard handle is closed
    # immediately: get_catalog() now opens a fresh read-only SQLite
    # connection (a WAL read lock) on every call, and this hook fires per
    # T3 batch in the long-lived MCP server — leaking them would accumulate
    # read locks and contribute to the very write starvation this RDR closes.
    try:
        _gate = get_catalog()
    except Exception:  # noqa: BLE001 — no-catalog path best-effort; logged at debug, returns
        import structlog  # noqa: PLC0415 — structlog deferred to function scope (lazy logger init)
        structlog.get_logger().debug("manifest_write_hook_no_catalog", exc_info=True)
        return
    if _gate is None:
        import structlog  # noqa: PLC0415 — structlog deferred to function scope (lazy logger init)
        structlog.get_logger().debug(
            "manifest_write_hook_catalog_uninitialised", collection=collection,
        )
        return
    # (The local-mode read-handle cleanup that lived here — a lint-dodging
    # ``getattr(_gate, "_db", None)`` — died with the local catalog,
    # nexus-i711w: the reader is always an HttpCatalogClient now.)
    # RDR-146 P1.2: this hook fires per T3 batch in the long-lived MCP
    # server — close the writer in finally so socket / SQLite handles do
    # not accumulate across a session.
    cat = get_catalog_writer()
    try:
        # nexus-kgos1: `_gate` is the READER, already resolved above. The sweep's
        # two reads must use it — `cat` is write-only and raises on both.
        _manifest_write_loop(cat, by_doc, collection, reader=_gate,
                             manifest_complete=manifest_complete)
    finally:
        # Production get_catalog_writer() returns a CatalogWriter (has close);
        # tests may patch it to a raw Catalog (no close). Guard so the hot
        # path closes the proxy without assuming the type.
        _close = getattr(cat, "close", None)
        if callable(_close):
            _close()


def _manifest_chunk_rows(indexed_metas) -> list[dict]:
    return [
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


def _apply_combined_write_response(
    res: dict, complete_map: dict[str, str], collection: str | None,
) -> list[str]:
    """Record accounting from a nexus-kl2z6/nexus-wxjr6 combined write's
    response: failed docs, completion refusals, and — the flush-grain
    path's whole reason for existing — the ENGINE's own sweep accounting.

    Deliberately NOT a reuse of :func:`_manifest_write_loop`'s write_many
    branch parsing: that block ALSO computes a local before/after chash
    diff and calls :func:`_sweep_superseded_vectors_many` — the client-side
    sweep this function's caller (the ChunkBatcher combined-write flush
    path) no longer needs, because the combined write's atomicity means
    the engine already did the sweep INSIDE the same per-doc transaction
    that wrote the manifest (design memo T2 design-kl2z6-combined-write
    §3: "This closes kl2z6 for the flush-grain indexing path by
    construction"). Building a fresh, smaller function here — rather than
    threading a flag through the 200-line existing branch — keeps that
    well-tested branch (still load-bearing for every OTHER
    ``manifest_write_batch_hook`` caller, see the module docstring update
    below) completely untouched.

    Sweep accounting (nexus-39upx: skips are never silent) —
    ``res["swept"]`` -> :func:`_record_superseded_swept`; each
    ``res["sweep_detail"]`` entry with ``errored`` true ->
    :func:`_record_superseded_sweep_skip` with the engine's closed
    ``reason`` vocabulary (``gate_timeout`` / ``statement_timeout`` /
    ``before_read_failed`` / ``sweep_failed`` — CatalogRepository.java's
    ``classifySweepFailureReason``). A missing ``reason`` on an errored
    entry (a shape a well-behaved engine never sends, but ``.get`` never
    KeyErrors) is recorded as ``sweep_failed`` rather than silently
    dropped, matching the closed vocabulary's catch-all bucket.

    Returns the list of failed doc_ids so the caller can log/record them
    the same way the write_many branch's own failure handling does.
    """
    import structlog  # noqa: PLC0415 — structlog deferred to function scope (lazy logger init)

    failed = list(res.get("failed_doc_ids") or [])
    for doc_id in failed:
        _record_manifest_write_failure(doc_id)
    refused = res.get("complete_refused") or []
    refused_count = int(res.get("complete_refused_count") or 0)
    if refused_count != len(refused):
        structlog.get_logger().warning(
            "complete_refused_count_mismatch",
            count_field=refused_count,
            list_len=len(refused),
            path="write_many_combined",
            note="refusal list truncated or shape drift — treating "
                 "every claimed doc as unstamped",
        )
        # Conservative direction (over-work, never under-report — same as
        # _manifest_write_loop's identical mismatch handling): a stamp we
        # cannot CONFIRM is a stamp we do not claim.
        listed = {str(r.get("doc_id", "")) for r in refused}
        for cid in complete_map:
            if cid not in listed:
                _record_complete_refusal(cid)
    for r in refused:
        rid = str(r.get("doc_id", ""))
        structlog.get_logger().warning(
            "write_manifest_many_complete_refused",
            doc_id=rid,
            path="write_many_combined",
            referenced=r.get("referenced"),
            missing=r.get("missing"),
            chunk_count=r.get("chunk_count"),
        )
        if rid:
            _record_complete_refusal(rid)
    _record_superseded_swept(int(res.get("swept") or 0))
    for outcome in res.get("sweep_detail") or []:
        if not isinstance(outcome, dict) or not outcome.get("errored"):
            continue
        _record_superseded_sweep_skip(
            str(outcome.get("doc_id", "")), collection,
            str(outcome.get("reason") or "sweep_failed"),
        )
    return failed


def _sweep_superseded_vectors(cat, doc_id, before: set[str], chunks: list[dict],
                              collection: str | None, *, reader, notes_provider) -> None:
    """Delete T3 rows this document's manifest no longer references (nexus-39upx).

    A re-index that changes the extracted text writes chunks under NEW chashes —
    content addressing working as designed — and ``atomic_manifest_replace``
    correctly repoints the manifest at them. Nothing removed the old vector
    rows, so they persisted, unreferenced by any manifest and still returned by
    vector search, which reads T3 directly. That is how a corruption fix could
    leave the corrupted text searchable while every catalog-level check
    reported the document clean.

    UNION GUARD, and it is the whole reason this is not a two-line delete:
    identical chunk text collapses to ONE T3 row shared by every document that
    contains it (CLAUDE.md, catalog/T3 split). "Not in THIS document's
    manifest" is therefore NOT "unreferenced" — deleting on that basis would
    silently remove chunks other documents still depend on. Each candidate is
    checked against ``docs_for_chashes`` and kept if ANY other document
    references it.

    NOTE GUARD (nexus-39upx hazard 2 / RDR-145): ``docs_for_chashes`` only
    sees MANIFESTED references, so it cannot tell a chash that fell out of
    THIS document's manifest from a chash that never had one at all — a
    manifest-less ``store_put`` / ``nx store put`` note, live by design
    (``catalog-003-soft-delete.xml``'s ``live_chunks`` contract). Surviving
    union-guard candidates are additionally checked against
    ``notes_provider()`` (typically ``nexus.indexer_utils.live_note_chashes``
    over a ``CollectionDocumentsCache``-memoized document list — round 2
    SIGNIFICANT 1: a batch reindex calls this function once per
    orphan-triggering document, so the caller shares ONE cache across the
    whole batch instead of this function re-fetching per document) before
    anything is deleted.

    Fail-open throughout: a sweep that cannot prove a row is orphaned leaves it
    alone. Over-retention is recoverable; over-deletion is not. Every skip that
    stems from an actual failure (as opposed to "nothing to do") is recorded
    via ``_record_superseded_sweep_skip`` so the CLI summary layer can surface
    it — a skip must never be silent (nexus-39upx hazard 4).
    """
    import structlog  # noqa: PLC0415 — structlog deferred to function scope (lazy logger init)

    if not collection:
        return
    new = {c.get("chash") for c in chunks if c.get("chash")}
    dropped = {h for h in before if h and h not in new}
    if not dropped:
        return
    # nexus-kgos1: reads go to the READER. `cat` is a _ServiceCatalogWriter, a
    # closed-whitelist write-only proxy that raises AttributeError for every
    # read op — so routing this through it could never prove orphanhood and the
    # sweep could never delete a row.
    #
    # `reader` is KEYWORD-ONLY AND REQUIRED, deliberately. It was briefly
    # `reader=None` falling back to `cat`, to spare updating the older tests —
    # and that default immediately re-created this very defect one call site
    # over: tests/catalog/test_manifest_write_many.py drove the loop with a
    # double carrying no read methods, so the sweep silently no-opped there and
    # covered none of this. A missing wire-up must fail at the call, loudly,
    # not degrade into the no-op we are here to remove.
    #
    # nexus-tp8yk D3: the union-guard LOGIC (docs_for_chashes lookup,
    # fail-open, event name) now lives in the shared
    # ``nexus.indexer_utils.orphaned_chashes`` — this call is a pure
    # delegation, zero behaviour change; the three T3-deleting prune sites
    # in doc_indexer.py/pipeline_stages.py route through the same helper.
    from nexus.indexer_utils import orphaned_chashes  # noqa: PLC0415 — deferred: avoids a module-load-time cross-import

    orphaned = orphaned_chashes(reader, doc_id, dropped, collection=collection)
    shared = len(dropped) - len(orphaned)
    if not orphaned:
        return
    try:
        notes = notes_provider()
    except Exception as exc:  # noqa: BLE001 — cannot prove note-safety: keep everything, same fail-open direction as orphaned_chashes
        structlog.get_logger().warning(
            "superseded_sweep_skipped_note_lookup_failed",
            doc_id=doc_id, collection=collection, candidates=len(orphaned),
            error=str(exc))
        _record_superseded_sweep_skip(doc_id, collection, "note_lookup_failed")
        return
    kept_notes = len(orphaned)
    orphaned = [h for h in orphaned if h not in notes]
    kept_notes -= len(orphaned)
    if not orphaned:
        return
    try:
        from nexus.db import make_t3  # noqa: PLC0415 — deferred: hot path

        result = make_t3().get_collection(collection).delete(ids=orphaned)
    except Exception as exc:  # noqa: BLE001 — the index must not fail on cleanup
        structlog.get_logger().warning(
            "superseded_sweep_failed", doc_id=doc_id, collection=collection,
            orphans=len(orphaned), error=str(exc))
        _record_superseded_sweep_skip(doc_id, collection, "delete_failed")
        return
    # nexus-tl5qh (RDR-191 F10c follow-up, o8dil.45's sibling site): report
    # the server's ACTUAL delete count, not the requested candidate size.
    # The engine's anti-join can legitimately refuse part of the batch — a
    # chash another live document's manifest still references — and that
    # is not a failure of this sweep, just a candidate that was never a
    # true orphan; discarding the response would silently over-report.
    actual = result if isinstance(result, int) else len(orphaned)
    if actual < len(orphaned):
        structlog.get_logger().warning(
            "superseded_sweep_partial_delete", doc_id=doc_id, collection=collection,
            requested=len(orphaned), actual=actual,
            note="the server's anti-join refused to delete some candidates "
                 "-- they are still referenced by a live catalog manifest "
                 "row and were never true orphans")
    else:
        structlog.get_logger().info(
            "superseded_vectors_swept", doc_id=doc_id, collection=collection,
            deleted=actual, kept_shared=shared, kept_notes=kept_notes)
    _record_superseded_swept(actual)


def _sweep_superseded_vectors_many(
    cat, dropped_by_doc: dict[str, set[str]], collection: str | None, *,
    reader, notes_provider,
) -> None:
    """Batch-safe sibling of :func:`_sweep_superseded_vectors` for the
    ``write_manifest_many`` fast branch (nexus-tgrgs / nexus-jk88j,
    2026-08-08).

    ``_manifest_write_loop`` calls this ONLY after ``write_manifest_many``'s
    POST has returned — every doc in the batch has therefore already
    committed (or is known-failed and excluded) server-side by the time any
    candidate is considered, and every candidate handed in here has ALREADY
    had every chash any document in THIS SAME BATCH still references
    subtracted (successful docs via their new manifest, failed docs via
    their unchanged prior manifest — see ``_manifest_write_loop``'s
    ``_live_union``). That two-part ordering is what closes the intra-batch
    TOCTOU the design memo documents (T2
    ``nexus/design-eslkl-hook-lock-narrowing`` §2b): sweeping doc A's drop
    before doc B's write of the identical chash had committed could delete
    a row B's manifest still needs. Deferring every sweep decision until
    the WHOLE batch has settled, and pre-subtracting the batch's own live
    set before any network read, removes that window by construction
    rather than by locking. The cross-FIRE race (two DIFFERENT flush-grain
    hook fires, i.e. two separate calls to this function/its caller,
    running concurrently) is a separate, still-open hazard.

    UPDATED post-nexus-eslkl (the hook-lock narrowing LANDED, not "before"
    as this comment previously read): ``LockedHookRegistry`` now locks
    PER HOOK rather than around the whole flush-grain dispatch.
    ``manifest_write_batch_hook`` (this function's caller, via
    ``_manifest_write_loop``) keeps its lock — it has NO ``serialize =
    False`` declaration — so manifest(A) || manifest(B) cross-fire races
    remain closed exactly as before, just via a narrower per-hook critical
    section. What changed: ``taxonomy_assign_batch_hook`` now opts OUT of
    locking entirely (server-side idempotency is sufficient for
    taxonomy||taxonomy and taxonomy||itself), which means manifest(A)'s
    sweep and taxonomy(B)'s ``assign_from_chashes`` probe (a DIFFERENT
    flush's chain) can now interleave for the first time on the
    ``LockedHookRegistry``-wrapped path — the design memo's §2c hazard.
    Accepted deliberately: 2c's failure mode is a loud, already-instrumented
    ``unmatched_chashes`` tripwire (RDR-172), not silent corruption like
    2b, and shares the identical root cause and fix (the engine-side
    per-doc-txn sweep fold, tracked at nexus-11gh6 / nexus-wxjr6 — kept
    open by design, write-skew not yet closed at READ COMMITTED). CAVEAT
    (nexus-uxkq5, critic finding, still applies unchanged): serialization
    of the hooks that DO keep a lock is CONDITIONAL —
    ``LockedHookRegistry`` wraps the registry only when
    ``resolve_index_concurrency() > 1``, while ``ChunkBatcher``'s
    ``flush_concurrency=3`` is unconditional, so under
    ``NX_INDEX_CONCURRENCY=1`` flush fires run on a BARE registry and the
    cross-fire window is live there regardless of any hook's lock
    declaration; batching also widens it there (every doc's before-read
    now spans the whole batch's write, not just its own). The real closure
    for both 2b and 2c is the engine-side per-doc-txn sweep fold; tracked
    in nexus-uxkq5 / nexus-11gh6 / nexus-wxjr6.

    One ``docs_for_chashes`` round trip (via the shared
    :func:`nexus.indexer_utils.orphaned_chashes` union guard, reused
    unmodified — passing a synthetic non-doc-id ``doc_id`` disables its
    single-document self-exclusion, which is exactly what a
    multi-document candidate set needs: ANY live reference, from ANY
    document, protects the candidate) and one T3 ``delete`` for the WHOLE
    BATCH, versus one of each per document in the per-doc path — the same
    round-trip-count discipline nexus-67qsd applies to the write half.

    Same fail-open contract as :func:`_sweep_superseded_vectors`: any read
    failure (reverse lookup or note lookup) deletes nothing and records a
    named skip for every document whose candidates were in flight, never
    silently. Over-retention is recoverable; over-deletion is not.

    RETIREMENT SCOPE (nexus-wxjr6, 2026-08-09): this function is NOT
    deleted. It retires only for the ChunkBatcher-driven flush-grain call
    site — that path now goes through the combined write
    (:meth:`nexus.catalog.http_catalog_client.HttpCatalogClient.write_manifest_many`
    with ``chunks``/``sweep=True``) directly from ``indexer.py``'s
    ``_batch_flush``, bypassing ``manifest_write_batch_hook`` entirely for
    ``grain="flush"`` dispatch (see :func:`_apply_combined_write_response`
    and ``HookRegistry.fire_batch``'s ``skip_hooks``). Every OTHER caller
    of ``manifest_write_batch_hook`` (``grain="all"``, the default —
    ``mcp/core.py``'s ``store_put``, ``doc_indexer.py``'s per-file and
    streaming paths, ``pipeline_stages.py``, ``prose_indexer.py``,
    ``code_indexer.py``'s oversize-per-file fallback) still reaches
    ``_manifest_write_loop``'s write_many branch below UNCHANGED, and this
    function remains the ONLY sweep mechanism for those callers until
    THEY are also converted to the combined write — see nexus-wxjr6's
    §7.2 per-caller residual table. Deleting this function now would
    silently reopen nexus-39upx for every one of them.
    """
    import structlog  # noqa: PLC0415 — deferred to function scope (lazy logger init)

    if not collection:
        return
    candidates: set[str] = set()
    for s in dropped_by_doc.values():
        candidates |= s
    if not candidates:
        return
    from nexus.indexer_utils import orphaned_chashes  # noqa: PLC0415 — deferred: avoids a module-load-time cross-import

    _batch_label = f"write_many_batch[{len(dropped_by_doc)}_docs]"
    orphaned = orphaned_chashes(reader, _batch_label, candidates, collection=collection)
    shared = len(candidates) - len(orphaned)
    if not orphaned:
        return
    try:
        notes = notes_provider()
    except Exception as exc:  # noqa: BLE001 — cannot prove note-safety: keep everything, same fail-open direction as orphaned_chashes
        structlog.get_logger().warning(
            "superseded_sweep_skipped_note_lookup_failed",
            doc_id=_batch_label, collection=collection, candidates=len(orphaned),
            error=str(exc))
        for doc_id in dropped_by_doc:
            _record_superseded_sweep_skip(doc_id, collection, "note_lookup_failed")
        return
    kept_notes = len(orphaned)
    orphaned = [h for h in orphaned if h not in notes]
    kept_notes -= len(orphaned)
    if not orphaned:
        return
    try:
        from nexus.db import make_t3  # noqa: PLC0415 — deferred: hot path

        result = make_t3().get_collection(collection).delete(ids=orphaned)
    except Exception as exc:  # noqa: BLE001 — the index must not fail on cleanup
        structlog.get_logger().warning(
            "superseded_sweep_failed", doc_id=_batch_label, collection=collection,
            orphans=len(orphaned), error=str(exc))
        for doc_id in dropped_by_doc:
            _record_superseded_sweep_skip(doc_id, collection, "delete_failed")
        return
    # nexus-tl5qh: report the server's ACTUAL delete count — see the
    # per-doc sibling (_sweep_superseded_vectors) for the full rationale.
    actual = result if isinstance(result, int) else len(orphaned)
    if actual < len(orphaned):
        structlog.get_logger().warning(
            "superseded_sweep_batch_partial_delete", doc_count=len(dropped_by_doc),
            collection=collection, requested=len(orphaned), actual=actual,
            note="the server's anti-join refused to delete some candidates "
                 "-- they are still referenced by a live catalog manifest "
                 "row and were never true orphans")
    else:
        structlog.get_logger().info(
            "superseded_vectors_swept_batch", doc_count=len(dropped_by_doc),
            collection=collection, deleted=actual, kept_shared=shared,
            kept_notes=kept_notes)
    _record_superseded_swept(actual)


def _stamp_index_run_complete(cat, doc_id: str, content_hash: str,
                              chunk_count: int) -> None:
    """Fail-closed completion stamp on the PER-DOC manifest path (nexus-dcv2k).

    ``write_manifest_many``'s ``complete`` map (nexus-5xn3k.4) rides the batch
    POST and is the zero-extra-round-trip form. Until nexus-67qsd (2026-08-08)
    it was UNREACHABLE in service mode: ``write_manifest_many`` was absent
    from both ``CATALOG_WRITE_OPS`` and ``_SERVICE_ONLY_WRITE_OPS``, so the
    capability check one frame up was False on every real run and the ride
    never fired — this per-doc branch was the ONLY path production actually
    took. ``write_manifest_many`` is now whitelisted and IS the hot path for
    a full-file flush-grain batch (see ``_manifest_write_loop``); this
    per-doc stamp remains load-bearing belt-and-braces for the paths that
    still bypass the fast branch — a 404 (pre-v0.1.24 engine), an
    all-continuation batch, or a writer double lacking the capability — so
    the fence still lands (never stays ``'indexing'`` forever, forcing every
    subsequent index to re-embed the document; the .3 three-way reads
    ``'indexing'`` as stale-definitively, by design) on every path, not only
    the common one.

    Contract parity with ``stampCompleteIfVerified``'s ride, deliberately:

    * *chunk_count* is the number of manifest rows just written, matching the
      engine's ``rows.size()`` in the ride — the verify predicate is
      ``missing == 0 AND referenced == chunk_count``.
    * A 409 refusal is NOT an error of this write: the rows are correct
      (over-work-never-under-work). It is recorded via
      :func:`_record_complete_refusal` and logged at WARNING under the SAME
      event name the ride uses, so the .6 consumer and the log-grep surface
      are identical whichever path stamped.
    * ``None`` (pre-fence engine 404) is NOT success and is NOT a refusal —
      the fence recorded nothing at all on that engine, ``begin`` 404'd the
      same way, and the staleness three-way falls through to its verify
      fallback. Recording a refusal there would cry wolf on every doc.
    * Never propagates: the manifest hook is non-propagating by contract, and
      an unstamped fence is over-work (a future re-index), never data loss.
    """
    import structlog  # noqa: PLC0415 — deferred (lazy logger)

    from nexus.errors import IndexRunVerifyRefused  # noqa: PLC0415 — deferred: import cycle at module load
    from nexus.retry import _manifest_write_with_retry  # noqa: PLC0415 — deferred (leaf module)

    try:
        result = _manifest_write_with_retry(
            cat.complete_index_run, doc_id, content_hash, chunk_count)
    except IndexRunVerifyRefused as refused:
        structlog.get_logger().warning(
            "write_manifest_many_complete_refused",
            doc_id=doc_id,
            referenced=refused.referenced,
            missing=refused.missing,
            chunk_count=refused.chunk_count,
            path="per_doc",
            note="manifest rows written; completion stamp refused — "
                 "doc is NOT fully indexed, index_state left as-was. "
                 "A candidate cause (nexus-2t63u): a stale "
                 "catalog_documents.physical_collection from a prior run "
                 "targeting a different collection makes manifest_verify "
                 "check the wrong collection and misreport present "
                 "chunks as missing — check via 'nx catalog show "
                 f"{doc_id}'.",
        )
        _record_complete_refusal(doc_id)
        return
    except Exception as exc:  # noqa: BLE001 — advisory: leave 'indexing' (over-work), never fail the write
        structlog.get_logger().warning(
            "index_run_complete_write_failed", doc_id=doc_id, error=str(exc),
        )
        return
    if result is None:
        structlog.get_logger().debug(
            "index_run_complete_engine_floor", doc_id=doc_id,
            note="pre-fence engine — no stamp landed and none was possible",
        )


def _manifest_write_loop(cat, by_doc, collection: str, *, reader,
                         manifest_complete: dict[str, str] | None = None) -> None:
    # RDR-191 (Hal ruling 2026-08-12, "stop guessing the collection"):
    # 'collection' is now a REQUIRED str, not an optional guess the engine
    # would otherwise infer from chunk membership. The sole caller
    # (manifest_write_batch_hook, ~:1235) already has it in scope from its
    # own required `collection: str` parameter — fail loud here rather than
    # let a blank value silently reach any of the manifest-write calls
    # below, which now all require it too.
    if not collection:
        raise ValueError(
            "_manifest_write_loop: 'collection' is required and must be "
            "non-blank (RDR-191) — every manifest write below requires it"
        )
    # nexus-u2kwq: multi-doc batches (the flush-grain aggregate path) go
    # through ONE write_many POST when the writer supports it; a 404
    # (engine < v0.1.24) or missing capability falls back to the per-doc
    # loop below. Flush-grain batches always carry complete files
    # (position 0 present), matching write_many's replace semantics.
    #
    # nexus-tgrgs/jk88j (2026-08-08): this is now the HOT path in service
    # mode (nexus-67qsd's whitelist add makes write_manifest_many reachable
    # through the production writer for the first time) — the per-doc loop
    # below is the fallback (404 / missing capability / continuation
    # slices), not the common case it was when write_many was dead.
    _warned_partial_claims = False
    # nexus-39upx round 2 SIGNIFICANT 1: ONE cache per call (per collection),
    # shared across every doc_id below (both the write_many fast path's
    # batch sweep and the per-doc fallback loop) — not one catalog-documents
    # fetch per orphan-triggering document. Hoisted to the top of the
    # function (nexus-tgrgs) so the fast branch can share it too; cheap to
    # construct even when nothing ever triggers a sweep — the underlying
    # fetch is fully lazy (CollectionDocumentsCache.get() is never called
    # unless something actually has surviving union-guard candidates).
    from nexus.indexer_utils import CollectionDocumentsCache, live_note_chashes  # noqa: PLC0415 — deferred: avoids a module-load-time cross-import

    _notes_cache = CollectionDocumentsCache(reader, collection or "")

    def _notes_provider() -> set[str]:
        return live_note_chashes(_notes_cache.get())

    if callable(getattr(cat, "write_manifest_many", None)):
        # No len(by_doc) gate (critique Critical, nexus-u2kwq): write_many
        # handles N=1 fine, and single-doc batches MUST take it — the
        # endpoint folds the documents.chunk_count update in, which the
        # per-doc HTTP replace path does not (stale chunk_count = docs
        # misread as empty by chunk_count>0 consumers).
        full_docs: list[tuple[str, list[dict]]] = []
        continuation: dict = {}
        for doc_id, indexed_metas in by_doc.items():
            chunks = _manifest_chunk_rows(indexed_metas)
            if not any(c["chash"] for c in chunks):
                continue
            # POSITION-0 GATE (review Important #1): write_many is
            # REPLACE — a doc whose batch lacks position 0 is a
            # continuation slice, and replacing would DELETE its
            # earlier rows (silent manifest corruption). Today's only
            # flush-grain producer (ChunkBatcher) is file-atomic so
            # position 0 is always present; this guard defends the
            # invariant against any future producer.
            if not any(c["position"] == 0 for c in chunks):
                continuation[doc_id] = indexed_metas
                continue
            full_docs.append((doc_id, chunks))
        if continuation:
            import structlog  # noqa: PLC0415 — deferred (lazy logger)
            structlog.get_logger().warning(
                "manifest_write_many_partial_doc_skipped",
                count=len(continuation),
                note="continuation slices routed to per-doc append path",
            )
            # nexus-5xn3k.4: a doc the producer claimed COMPLETE landing in
            # the continuation bucket is a contract violation — its batch
            # lacks position 0, so it cannot be the whole file. Never stamp
            # it; the claim was wrong, say so loudly.
            _claimed_partial = [d for d in continuation if d in (manifest_complete or {})]
            if _claimed_partial:
                structlog.get_logger().warning(
                    "manifest_complete_claim_on_continuation_slice",
                    doc_ids=_claimed_partial,
                )
                # dcv2k review: derive the once-guard from the warning having
                # FIRED, not from wrote_many — an all-continuation batch or a
                # write_many 404 leaves wrote_many False while this warning
                # already fired, and the per-doc loop would re-emit it.
                _warned_partial_claims = True
        wrote_many = False
        if full_docs:
            from nexus.retry import _manifest_write_with_retry  # noqa: PLC0415 — deferred (leaf module, avoid import cost on the no-op path)

            # nexus-5xn3k.4 (RUNFENCE): completion stamps ride the same POST
            # for docs the producer asserted file-atomic — restricted to docs
            # actually in THIS write.
            _full_ids = {d for d, _ in full_docs}
            _complete_map = {
                d: h for d, h in (manifest_complete or {}).items() if d in _full_ids
            }
            # nexus-tgrgs/jk88j (2026-08-08): the 39upx sweep's "before" read,
            # batched via the existing get_manifests (already paged/count-
            # reconciled client-side — no new engine endpoint needed here;
            # see the design memo for why a dedicated get_chunk_chashes_many
            # was considered and found unnecessary). MUST happen before the
            # write below: it captures what each doc's manifest referenced
            # PRE-replace. Fail-open per doc — a read failure means that
            # doc's sweep is skipped, never that the whole batch's write is
            # blocked (no sweep beats a wrong sweep, but the manifest write
            # itself is unrelated and must proceed regardless).
            _before_by_doc: dict[str, set[str]] = {}
            try:
                _manifests_before = reader.get_manifests(list(_full_ids)) or {}
                for _d in _full_ids:
                    _before_by_doc[_d] = {
                        row.chash for row in _manifests_before.get(_d, []) if row.chash
                    }
            except Exception as _exc:  # noqa: BLE001 — no sweep beats a wrong sweep
                import structlog  # noqa: PLC0415 — deferred: hot path
                structlog.get_logger().warning(
                    "superseded_sweep_before_read_failed",
                    doc_ids=sorted(_full_ids), collection=collection,
                    error=str(_exc), path="write_many")
                for _d in _full_ids:
                    _before_by_doc[_d] = set()
                    _record_superseded_sweep_skip(_d, collection, "before_read_failed")
            try:
                if _complete_map:
                    res = _manifest_write_with_retry(
                        cat.write_manifest_many, full_docs, complete=_complete_map,
                        collection=collection)
                else:
                    res = _manifest_write_with_retry(
                        cat.write_manifest_many, full_docs, collection=collection)
                wrote_many = True
                # Dual return shape: dict (current client — carries the
                # complete_refused contract) or bare failed-ids list (legacy
                # doubles). The COUNT field MUST be parsed alongside the list
                # (bead amendment): a truncated/absent list with a non-zero
                # scalar must never read as zero refusals.
                if isinstance(res, dict):
                    failed = res.get("failed_doc_ids") or []
                    _refused = res.get("complete_refused") or []
                    _refused_count = int(res.get("complete_refused_count") or 0)
                else:
                    failed = res or []
                    _refused, _refused_count = [], 0
                if _refused_count != len(_refused):
                    import structlog  # noqa: PLC0415 — deferred (lazy logger)
                    structlog.get_logger().warning(
                        "complete_refused_count_mismatch",
                        count_field=_refused_count,
                        list_len=len(_refused),
                        note="refusal list truncated or shape drift — treating "
                             "every claimed doc as unstamped",
                    )
                    # Conservative direction (over-work, never under-report):
                    # a stamp we cannot CONFIRM is a stamp we do not claim.
                    _listed = {str(r.get("doc_id", "")) for r in _refused}
                    for _cid in _complete_map:
                        if _cid not in _listed:
                            _record_complete_refusal(_cid)
                for _r in _refused:
                    import structlog  # noqa: PLC0415 — deferred (lazy logger)
                    _rid = str(_r.get("doc_id", ""))
                    structlog.get_logger().warning(
                        "write_manifest_many_complete_refused",
                        doc_id=_rid,
                        path="write_many",
                        referenced=_r.get("referenced"),
                        missing=_r.get("missing"),
                        chunk_count=_r.get("chunk_count"),
                        note="manifest rows written; completion stamp refused — "
                             "doc is NOT fully indexed, index_state left as-was. "
                             "A candidate cause (nexus-2t63u): a stale "
                             "catalog_documents.physical_collection from a "
                             "prior run targeting a different collection "
                             "makes manifest_verify check the wrong "
                             "collection and misreport present chunks as "
                             f"missing — check via 'nx catalog show {_rid}'.",
                    )
                    if _rid:
                        _record_complete_refusal(_rid)
                if failed:
                    import structlog  # noqa: PLC0415 — deferred (lazy logger)
                    structlog.get_logger().warning(
                        "manifest_write_many_partial", failed_doc_ids=failed,
                    )
                    for doc_id in failed:
                        _record_manifest_write_failure(doc_id)
                # nexus-tgrgs/jk88j (2026-08-08): the 39upx sweep, folded
                # into the fast branch. Runs only here — after the POST has
                # returned — because every doc's write has now committed
                # (or is known-failed, in `failed`). Computing dropped sets
                # and the batch-wide "still live" union BEFORE any network
                # read is what closes the intra-batch TOCTOU (design memo
                # §2b): a chash any doc in this batch currently references —
                # successes via their NEW manifest, failures via their
                # UNCHANGED prior manifest (their write did not land, so
                # `before` is still their truth, not `new`) — is subtracted
                # before the reverse-lookup ever asks the network, so no
                # sibling write in this same POST can race the check.
                _failed_ids = set(failed)
                _new_by_doc = {
                    _d: {c["chash"] for c in _chunks if c["chash"]}
                    for _d, _chunks in full_docs
                }
                _live_union: set[str] = set()
                for _d in _full_ids:
                    _live_union |= (
                        _new_by_doc[_d] if _d not in _failed_ids
                        else _before_by_doc.get(_d, set())
                    )
                _dropped_by_doc: dict[str, set[str]] = {}
                for _d in _full_ids:
                    if _d in _failed_ids:
                        continue  # unknown write outcome: no sweep beats a wrong sweep
                    _dropped = _before_by_doc.get(_d, set()) - _new_by_doc[_d]
                    _dropped -= _live_union
                    if _dropped:
                        _dropped_by_doc[_d] = _dropped
                if _dropped_by_doc:
                    _sweep_superseded_vectors_many(
                        cat, _dropped_by_doc, collection,
                        reader=reader, notes_provider=_notes_provider)
            except Exception as exc:  # noqa: BLE001 — GH #1371: non-propagating by contract; 404 falls back per-doc, anything else is recorded+swallowed here
                status = getattr(
                    getattr(exc, "response", None), "status_code", None
                ) or getattr(exc, "code", None)
                if status == 404:
                    pass  # older engine — fall through to the per-doc loop below
                else:
                    # A persistent (retries-exhausted or non-retryable) failure.
                    # Re-attempting these same docs one-by-one below would very
                    # likely fail again for the same reason (a dead connection
                    # doesn't heal by switching endpoints) while burning a full
                    # retry cycle per doc — record the whole batch as failed
                    # instead, honouring the hook's non-propagation contract.
                    import structlog  # noqa: PLC0415 — deferred (lazy logger)
                    structlog.get_logger().warning(
                        "manifest_write_many_failed",
                        doc_ids=[doc_id for doc_id, _ in full_docs],
                        error=str(exc),
                        exc_info=True,
                    )
                    for doc_id, _ in full_docs:
                        _record_manifest_write_failure(doc_id)
                    wrote_many = True
        if wrote_many:
            # per-doc loop handles ONLY the continuation remainder (may
            # be empty). Critique Significant: an all-continuation batch
            # must fall through to the loop, never early-return.
            by_doc = continuation
            # (_warned_partial_claims is set at warning-fire time above.)
        # else: 404 or nothing eligible — per-doc loop covers everything.
        if not by_doc:
            return
    from nexus.retry import _manifest_write_with_retry  # noqa: PLC0415 — deferred (leaf module, avoid import cost on the no-op path)

    # (_notes_cache / _notes_provider constructed once, at the top of this
    # function — nexus-tgrgs hoisted them so the write_many fast branch's
    # batch sweep above shares the same memoized cache.)
    for doc_id, indexed_metas in by_doc.items():
        chunks = _manifest_chunk_rows(indexed_metas)
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
                # nexus-39upx: capture what the manifest referenced BEFORE the
                # replace, so the vector rows that fall out of it can be swept.
                # atomic_manifest_replace already fixes the CATALOG side of a
                # shrink-reindex (the comment above); the T3 side was never
                # done. Because a re-extraction that CHANGES text produces new
                # chashes, the old rows stay in T3 referenced by nothing — and
                # vector search reads T3, not the manifest, so superseded text
                # keeps being retrieved while `nx catalog show` looks clean.
                # Measured on 1.14.19 after the nexus-gtltb fix: 17 such rows.
                #
                # nexus-kgos1: this read goes to the READER. It was routed
                # through `cat` — a write-only proxy that raises AttributeError
                # for every read op — into a bare `except` that logged NOTHING,
                # so `_before` was always empty, `dropped` was always empty, and
                # the sweep returned immediately. It had never deleted a row.
                # The silence is what hid it: the OTHER guard in the sweep logs.
                _before: set[str] = set()
                try:
                    _before = {h for h in (reader.get_chunk_chashes(doc_id) or []) if h}
                except Exception as _exc:  # noqa: BLE001 — no sweep beats a wrong sweep
                    import structlog  # noqa: PLC0415 — deferred: hot path
                    structlog.get_logger().warning(
                        "superseded_sweep_before_read_failed",
                        doc_id=doc_id, collection=collection, error=str(_exc))
                    _before = set()
                    _record_superseded_sweep_skip(doc_id, collection, "before_read_failed")
                _manifest_write_with_retry(
                    cat.atomic_manifest_replace, doc_id, chunks, collection=collection)
                _sweep_superseded_vectors(cat, doc_id, _before, chunks, collection,
                                          reader=reader, notes_provider=_notes_provider)
                # chunk_count parity (critique Critical): the HTTP
                # client's replace does NOT touch documents.chunk_count
                # (only write_many folds it in); the local Catalog does
                # it in-txn, where this resync is an idempotent no-op.
                _resync = getattr(cat, "resync_chunk_count_cache", None)
                if callable(_resync):
                    _manifest_write_with_retry(_resync, doc_id)
                # nexus-dcv2k (RUNFENCE .4 regression): the completion stamp
                # normally rides write_manifest_many's `complete` map — but a
                # doc that reached THIS per-doc branch (404 fallback,
                # capability-less writer, or a continuation slice claimed
                # complete in error) never went through that POST, so it
                # would land here unstamped if this fallback did not stamp
                # it too. Stamp after the rows, the sweep and the
                # chunk_count resync have settled: the engine's verify reads
                # the manifest this doc just wrote. Position-0 only — a
                # continuation slice is not a whole document (handled in the
                # else branch).
                _claimed_hash = (manifest_complete or {}).get(doc_id)
                if _claimed_hash:
                    _stamp_index_run_complete(
                        cat, doc_id, _claimed_hash, len(chunks))
            else:
                _manifest_write_with_retry(
                    cat.append_manifest_chunks, doc_id, chunks, collection=collection)
                # Same contract violation the write_many branch flags: a doc
                # claimed COMPLETE whose batch lacks position 0 cannot be the
                # whole file. Never stamp it; say so loudly (once — the
                # write_many branch already warned for docs it routed here).
                if not _warned_partial_claims and doc_id in (manifest_complete or {}):
                    import structlog  # noqa: PLC0415 — deferred (lazy logger)
                    structlog.get_logger().warning(
                        "manifest_complete_claim_on_continuation_slice",
                        doc_ids=[doc_id],
                    )
                # nexus-zq79: documents.chunk_count is a denormalised cache of
                # COUNT(*) document_chunks. The catalog-register hook runs BEFORE
                # per-file indexing (tumbler injection requires it), so chunk_count
                # is initialised to 0; nothing else updates it for code/prose
                # indexers post-Phase-3. Routed via Catalog public API to satisfy
                # the projector-only-writes invariant (RDR-101 Phase 3 ε).
                _manifest_write_with_retry(cat.resync_chunk_count_cache, doc_id)
        except Exception:  # noqa: BLE001 — manifest hook non-propagating by contract; logged at WARNING for discoverability
            # Post-Phase-3 the manifest hook is load-bearing: a failure
            # leaves the catalog manifest empty and chunk_count=0 for
            # this doc (silent data-correctness bug). The contract still
            # requires non-propagation (best-effort hook) but the log
            # severity is WARNING so failures are discoverable in
            # production log streams without DEBUG enabled. nexus-zq79.
            # GH #1371: also recorded in the process-local failure collector
            # so the CLI's end-of-run summary can surface it (and point at
            # `nx catalog reconcile`) instead of it being visible only to an
            # operator tailing structlog output.
            import structlog  # noqa: PLC0415 — structlog deferred to function scope (lazy logger init)
            structlog.get_logger().warning(
                "manifest_write_hook_failed", doc_id=doc_id, exc_info=True
            )
            _record_manifest_write_failure(doc_id)


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
    import json  # noqa: PLC0415 — stdlib json deferred to function scope
    from pathlib import Path  # noqa: PLC0415 — stdlib pathlib deferred to function scope

    import structlog  # noqa: PLC0415 — structlog deferred to function scope (lazy logger init)

    log = structlog.get_logger()
    try:
        from importlib.metadata import version as _pkg_version  # noqa: PLC0415 — stdlib importlib.metadata deferred to function scope

        cli_ver = _pkg_version("conexus")

        # NO CLI ↔ T2 schema-drift check: RDR-120 P4 read the stored
        # ``_nexus_version`` via the daemon's ``database.hello`` op, and the
        # daemon is its ONLY transport. With the daemon retired (nexus-i711w
        # Stage 2 sub-stage B) there is nothing left to ask, so the check is
        # removed rather than left permanently reading ``None``. No service-mode
        # equivalent is built here on purpose: the engine's schema is Liquibase-
        # managed and its drift surface is the engine-version floor
        # (``REQUIRED_ENGINE_VERSION``), not a per-boot version compare.

        # ── Plugin ↔ CLI version drift ──────────────────────────────────
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
                        from nexus import install_advice  # noqa: PLC0415 — deferred import

                        hint = (
                            "CLI is older — run '"
                            + install_advice.upgrade_command(
                                "uv tool upgrade conexus"
                            )
                            + "'"
                        )
                    log.warning(
                        "plugin_cli_version_mismatch",
                        cli_version=cli_ver,
                        plugin_version=plugin_ver,
                        hint=hint,
                    )
    except Exception:  # noqa: BLE001 — version-skew warning must never block MCP startup
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
    global _t1_instance, _t1_isolated, _t3_instance, _collections_cache, _service_t2_db
    _t1_instance = None
    _t1_isolated = False
    _t3_instance = None
    _collections_cache = ([], 0.0)
    with _service_t2_lock:
        if _service_t2_db is not None:
            _service_t2_db.close()
        _service_t2_db = None
        # nexus-0dpli: a test that left in-flight refcount bookkeeping
        # behind must not leak into the next test's assertions.
        _service_t2_refcounts.clear()
        _service_t2_pending_close.clear()
    clear_search_traces()
    reset_plan_cache_for_tests()
    # nexus-5en9j: also reset the shared SERVICE-mode catalog client singleton
    try:
        from nexus.catalog.factory import reset_shared_service_catalog_client_for_tests  # noqa: PLC0415 — deferred to avoid circular import (catalog.factory)
        reset_shared_service_catalog_client_for_tests()
    except ImportError:
        pass
    # RDR-152 Seam B: also reset the http_vector_client singleton
    try:
        from nexus.db.http_vector_client import reset_http_vector_client_for_tests  # noqa: PLC0415 — deferred to avoid circular import (http_vector_client)
        reset_http_vector_client_for_tests()
    except ImportError:
        pass


def inject_t1(t1, *, isolated: bool = False):
    """Inject a T1Database for testing."""
    global _t1_instance, _t1_isolated
    _t1_instance = t1
    _t1_isolated = isolated


def inject_t3(t3):
    """Inject a T3Database for testing."""
    global _t3_instance
    _t3_instance = t3


# nexus-u2kwq: see grain comment above taxonomy/chash declarations.
manifest_write_batch_hook.batch_grain = "flush"
