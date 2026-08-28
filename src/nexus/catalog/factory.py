# SPDX-License-Identifier: AGPL-3.0-or-later
"""Typed reader / writer factories for the catalog (RDR-146 P1.2, service-only
since nexus-i711w).

The catalog lives in the Java Postgres engine; every reader and writer this
module hands out is backed by :class:`HttpCatalogClient`. The read/write split
is TOOLING-ENFORCED, not convention:

  - :func:`make_catalog_reader` -> a read-facing proxy over the shared
    service client (:class:`_SharedServiceCatalogHandle`).

  - :func:`make_catalog_writer` -> a write-only proxy
    (:class:`_ServiceCatalogWriter`) exposing ONLY the whitelisted
    :data:`CATALOG_WRITE_OPS` (+ service-only batch ops), so a
    dataclass-returning read can never accidentally round-trip the wire.

Mixed sites (read AND write) hold BOTH a reader and a writer. That is the
gate-resolved design (re-gate Critical): the two typed factories make the
read/write distinction visible and enforceable.

History: through RDR-158 P4 these factories fronted a local SQLite
``.catalog.db`` (reader: ``mode=ro`` local Catalog; writer: T2-daemon-routed
with a direct in-process fallback). The daemon died in nexus-i711w sub-stage B
and the local SQLite catalog itself in the terminal i711w deletion; the
``make_catalog_admin`` third factory died earlier (Hal ruling 2026-07-29,
GH #1419.4 split-brain: at one backup timestamp ``.catalog.db`` showed
532 docs / 13 links against PG's 592 / 52).
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Optional

import structlog

from nexus.catalog.catalog_protocol import CATALOG_WRITE_OPS

_log = structlog.get_logger(__name__)

# nexus-53x7s / nexus-5en9j: SERVICE-mode catalog reader/writer share ONE
# process-lifetime HttpCatalogClient instead of constructing (and
# immediately closing) one per make_catalog_reader()/make_catalog_writer()
# call. This was the LARGEST single reconstruction count in the nexus-53x7s
# shakeout evidence (394x http_catalog_client.init in one run) -- larger
# than any of the T2Database substores that bead's first fix addressed.
#
# The fresh-per-call docstrings on make_catalog_reader/make_catalog_writer
# are SQLite-mode reasoning (avoid accumulating local WAL read locks / write
# handles across a long-lived MCP process) that does not apply to
# HttpCatalogClient -- it owns pooled httpx.Client connections, exactly the
# same shape as the T2 Http*Store classes _service_t2_write_locked already
# fixed in mcp_infra.py. Same design here: a process-lifetime singleton,
# CAS-narrowed (nexus-u2u0n): the lock guards only resolving/evicting the
# singleton, never the call itself — see _SharedServiceCatalogHandle._call.
_service_catalog_lock = threading.Lock()
_service_catalog_client: Any = None

#: nexus-0dpli: in-flight caller count per instance, keyed by ``id(client)``.
#: Incremented under ``_service_catalog_lock`` at resolve time, decremented
#: under the same lock when a caller's forwarded call returns OR raises.
#: Load-bearing for eviction safety: the CAS narrowing means multiple
#: threads can be genuinely mid-call against the SAME shared instance at
#: once, so an eviction cannot simply close() the instance it just kicked
#: out of the shared slot — a sibling may still be using it. See
#: ``_release_shared_catalog_ref``.
_service_catalog_refcounts: dict[int, int] = {}

#: nexus-0dpli: ids of instances that have been evicted from the shared slot
#: (so no NEW caller will ever resolve them) but still have callers in
#: flight. The LAST caller to release its reference on a pending instance
#: is the one that physically closes it — never the evictor itself, unless
#: the evictor was also the last (or only) holder.
_service_catalog_pending_close: set[int] = set()

#: nexus-jb4pp: per-op ``{op: [calls, lock_wait_s, call_s]}`` for the shared
#: service-catalog handle. ``_service_catalog_lock`` is held for the FULL
#: duration of every forwarded call — including the network round trip — so
#: every catalog write in the process (manifest write_many, the RUNFENCE
#: begin_index_run_many, registration) is strictly serialized against every
#: other, across all indexing workers. That serialization was invisible: it
#: is billed to whichever caller's timer brackets the call, so the manifest
#: hook's measured cost has always included time spent waiting on OTHER
#: threads' catalog traffic. Splitting wait from call is what makes
#: "is the manifest cost client, network, or server?" answerable at all.
#: Counters are plain floats mutated under the lock they measure.
_service_catalog_op_stats: dict[str, list[float]] = {}


def _record_catalog_op(name: str, wait_s: float, call_s: float,
                       *, calls: int = 1) -> None:
    """Accumulate one op's timings. Called while holding the lock it
    measures, so the read-modify-write of the shared row is atomic.

    *calls* is 0 for the non-callable attribute path — that access still
    queued for the lock, so its wait is real and must be counted, but it
    is not a round trip and must not inflate the call count.
    """
    _row = _service_catalog_op_stats.setdefault(name, [0.0, 0.0, 0.0])
    _row[0] += calls
    _row[1] += wait_s
    _row[2] += call_s


def service_catalog_op_stats() -> dict[str, dict[str, float]]:
    """Snapshot of per-op shared-catalog-handle timings (nexus-jb4pp).

    ``{op: {"calls": n, "lock_wait_s": s, "call_s": s}}`` where
    ``lock_wait_s`` is time blocked on ``_service_catalog_lock`` before the
    call began and ``call_s`` is the forwarded call itself (client
    serialization + network + server). Cumulative across threads, so both
    may exceed wall clock.
    """
    with _service_catalog_lock:
        return {
            op: {"calls": v[0], "lock_wait_s": v[1], "call_s": v[2]}
            for op, v in _service_catalog_op_stats.items()
        }


def reset_service_catalog_op_stats() -> None:
    """Zero the per-op counters (review finding on nexus-jb4pp): the stats
    dict is a process-lifetime module global, so a process that runs more
    than one index pass (MCP tool, watch mode, a multi-indexing pytest
    session) would otherwise report cumulative-since-process-start numbers
    with no signal — the same mis-attribution class this instrumentation
    exists to kill. ``_run_index`` calls this at start so every
    ``index_catalog_op_stats`` event covers exactly one run."""
    with _service_catalog_lock:
        _service_catalog_op_stats.clear()


def _get_shared_service_catalog_client() -> Any:
    global _service_catalog_client
    if _service_catalog_client is None:
        from nexus.catalog.http_catalog_client import HttpCatalogClient  # noqa: PLC0415 — deliberate function-scoped import (defer heavy/optional dep, avoid circular import)

        _service_catalog_client = HttpCatalogClient()
    return _service_catalog_client


def reset_shared_service_catalog_client_for_tests() -> None:
    """Close and clear the shared SERVICE-mode catalog client (tests only)."""
    global _service_catalog_client
    with _service_catalog_lock:
        if _service_catalog_client is not None:
            _service_catalog_client.close()
        _service_catalog_client = None
        # nexus-0dpli: a test that left in-flight refcount bookkeeping
        # behind (e.g. a barrier test that closed early) must not leak
        # into the next test's assertions.
        _service_catalog_refcounts.clear()
        _service_catalog_pending_close.clear()


def _acquire_shared_catalog_ref(client: Any) -> None:
    """Record one more in-flight caller against *client*. Callers MUST
    hold ``_service_catalog_lock`` and call this immediately after
    resolving the client, in the SAME critical section — otherwise a
    concurrent eviction could run between resolve and acquire and never
    see this caller's reference (nexus-0dpli)."""
    key = id(client)
    _service_catalog_refcounts[key] = _service_catalog_refcounts.get(key, 0) + 1


def _release_shared_catalog_ref(client: Any, *, evict: bool) -> bool:
    """Release this caller's in-flight reference to *client*. Callers MUST
    hold ``_service_catalog_lock``.

    When *evict* is True, also clears the shared slot if it still points
    at *client* (compare-and-swap by identity) so future resolvers build
    fresh — regardless of whether THIS caller ends up being the one that
    physically closes it.

    Returns True exactly when the CALLER must run ``client.close()``
    itself (after releasing the lock): either this is an evicting caller
    and no other in-flight caller holds a reference (refcount drains to
    zero right here), or this is a NON-evicting caller finishing its call
    normally but a PRIOR caller already evicted *client* while this one
    was still in flight (``nexus-0dpli``'s pending-close handoff) and this
    is the last reference draining it. Never returns True for two
    different callers on the same *client* — the refcount decrement and
    the pending-close check happen in one atomic (locked) step, so
    exactly one caller ever sees ``remaining <= 0``.
    """
    global _service_catalog_client
    if evict and _service_catalog_client is client:
        _service_catalog_client = None
    key = id(client)
    remaining = _service_catalog_refcounts.get(key, 1) - 1
    if remaining > 0:
        _service_catalog_refcounts[key] = remaining
        if evict:
            _service_catalog_pending_close.add(key)
        return False
    _service_catalog_refcounts.pop(key, None)
    was_pending = key in _service_catalog_pending_close
    _service_catalog_pending_close.discard(key)
    return evict or was_pending


class _SharedServiceCatalogHandle:
    """Read-facing proxy over the shared SERVICE-mode ``HttpCatalogClient``.

    ``close()`` is deliberately a no-op — callers historically closed a
    fresh-per-call client; the shared client outlives any single caller and
    is only torn down via error-triggered eviction or
    :func:`reset_shared_service_catalog_client_for_tests`.

    CAS-NARROWED (nexus-u2u0n): ``_service_catalog_lock`` is held ONLY long
    enough to resolve (get-or-build) the current client — never across the
    forwarded call's own network round trip. ``_call`` resolves under the
    lock, releases, makes the call, and on failure re-acquires the lock to
    decide whether to evict.

    REFCOUNTED (nexus-0dpli, critique finding on the first cut of this
    narrowing): releasing the lock around the round trip means MULTIPLE
    threads can be genuinely mid-call against the SAME shared instance at
    once — proven by ``test_call_releases_lock_before_network_round_trip``.
    An eviction that simply ``close()``s the instance it just CAS'd out of
    the shared slot can therefore tear down a sibling's still-in-flight
    call (empirically confirmed: closing a live ``httpx.Client`` from
    another thread aborts a concurrent in-flight request on it). The fix
    is two-part:

    1. EVICTION TRIGGER IS NARROW, not "any exception". Only a genuine
       connectivity failure (:func:`nexus.retry._is_connectivity_error` —
       ``httpx.TransportError``/``ConnectionError``/``TimeoutError``,
       including chained causes) evicts. A routine domain outcome (e.g.
       ``IndexRunVerifyRefused``'s 409) propagates WITHOUT touching the
       shared client — eviction exists to recover from a singleton whose
       underlying connections are broken, not to react to an expected
       per-doc refusal. Under-evict, never over-evict: a wrongly-KEPT
       stale client fails loudly on its next call and gets evicted then;
       a wrongly-CLOSED healthy client silently aborts an innocent
       sibling's call right now.
    2. EVICTION DOES NOT CLOSE OUT FROM UNDER IN-FLIGHT SIBLINGS. Every
       caller acquires an in-flight reference (:func:`_acquire_shared_catalog_ref`)
       on the instance it resolved, under the SAME lock as the resolve, and
       releases it (:func:`_release_shared_catalog_ref`) under the lock
       once its own call returns or raises. An eviction clears the shared
       SLOT immediately (so new callers always build fresh — never
       observe a doomed instance) but only physically closes the OLD
       instance once its reference count has drained to zero — either the
       evicting caller itself (no one else was using it) or whichever
       sibling's release happens to be the last one out. This is a
       COMPARE-AND-SWAP OF THE SLOT plus a REFCOUNTED, DEFERRED CLOSE — a
       resolver arriving after the slot is cleared always builds fresh,
       and no thread ever observes (or is issued) a call against an
       instance already mid-``close()``.

    RELEASE IS UNCONDITIONAL (nexus-0dpli round 3, delta-review touch-up):
    the reference release lives in a single ``finally``, not duplicated
    across ``except``/``else`` branches — a ``BaseException`` that is not
    a plain ``Exception`` (``KeyboardInterrupt``/``SystemExit`` mid-call)
    must still release this call's reference, or it leaks forever and can
    strand a sibling's already-evicted, pending-close instance with no one
    left to drain it to zero. The eviction decision (``_evict``) defaults
    to ``False`` and is set ``True`` only inside ``except Exception`` —
    so a non-``Exception`` ``BaseException`` correctly releases WITHOUT
    evicting, the same safe default as no exception at all.
    """

    def __getattr__(self, name: str) -> Any:
        # nexus-jb4pp: this acquisition is NOT a formality — before the
        # nexus-u2u0n narrowing it could block for the full duration of any
        # in-flight call on another thread, since _call held the same lock
        # across its network round trip. Instrumenting only _call read a
        # near-zero wait while threads were demonstrably serializing,
        # because they were queueing HERE. A timer that measures the wrong
        # side of a convoy is worse than none. Kept post-narrowing: this
        # resolution is in-process (no round trip), so the wait it can
        # still show is queueing behind ANOTHER thread's brief resolution,
        # not behind a network call.
        _w0 = time.monotonic()
        with _service_catalog_lock:
            _wait = time.monotonic() - _w0
            client = _get_shared_service_catalog_client()
            attr = getattr(client, name)  # may raise (e.g. local-mode-only ._db) — let it propagate untouched
            _non_callable = not callable(attr)
            if _non_callable:
                # Recorded INSIDE the lock: the counters are plain floats
                # whose only mutual exclusion is this lock.
                _record_catalog_op(name, _wait, 0.0, calls=0)
        if _non_callable:
            return attr

        def _call(*args: Any, **kwargs: Any) -> Any:
            _w1 = time.monotonic()
            with _service_catalog_lock:
                _wait2 = time.monotonic() - _w1
                current = _get_shared_service_catalog_client()
                _acquire_shared_catalog_ref(current)
            _c0 = time.monotonic()
            _evict = False
            try:
                return getattr(current, name)(*args, **kwargs)
            except Exception as exc:
                # Only a genuine connectivity failure evicts — see the
                # class docstring's point 1. ``_evict`` stays False (the
                # safe default) for any OTHER exception, including a
                # BaseException that skips this clause entirely
                # (KeyboardInterrupt/SystemExit — see the class docstring's
                # "RELEASE IS UNCONDITIONAL" note).
                from nexus.retry import _is_connectivity_error  # noqa: PLC0415 — deferred to avoid import cost on the happy path
                _evict = _is_connectivity_error(exc)
                raise
            finally:
                # nexus-0dpli round 3: release lives in ONE unconditional
                # finally, not duplicated across except/else — a
                # BaseException that _is_connectivity_error never sees
                # (KeyboardInterrupt/SystemExit) must still release this
                # call's reference, or it leaks and can strand a sibling's
                # already-evicted, pending-close instance forever.
                #
                # _record_catalog_op is folded into the SAME locked
                # section: its own docstring has always said "called
                # while holding the lock it measures" (true pre-CAS-
                # narrowing, when the whole call ran inside
                # `with _service_catalog_lock`), but the narrowing moved
                # this call outside any lock, leaving concurrent callers
                # racing an unguarded read-modify-write on the shared
                # stats dict. Unlike `mcp_infra._record_t2_op` (given its
                # own dedicated `_service_t2_stats_lock` in the same
                # round), this function was never given one — restored
                # the invariant here instead, at zero extra cost since
                # this section already re-acquires `_service_catalog_lock`
                # for the refcount release.
                with _service_catalog_lock:
                    _close_now = _release_shared_catalog_ref(current, evict=_evict)
                    _record_catalog_op(
                        name, _wait + _wait2, time.monotonic() - _c0)
                if _close_now:
                    current.close()

        return _call

    def close(self) -> None:
        pass  # nexus-5en9j: shared instance outlives any single caller


def _is_catalog_service_mode() -> bool:
    """Return True — the catalog is service-backed in every mode.

    Collapsed to a constant by the terminal i711w deletion (the local
    SQLite catalog no longer exists). Kept as a function because tests
    and callers patch/probe it by name.
    """
    return True


def make_catalog_reader(*, config_dir: Optional[Path] = None) -> Optional[Any]:
    """Return a read-facing catalog proxy backed by the service.

    Returns a :class:`_SharedServiceCatalogHandle` forwarding reads to the
    Java Postgres service. The client is always considered "initialised" —
    if the service is unreachable, the first HTTP call will raise.

    The ``Optional`` return annotation is historical (the deleted SQLite
    leg returned ``None`` when the catalog dir was uninitialised); callers'
    None-guards are now dead but harmless.

    The resolver call is validation only (RDR-158 P3/Stage 5): with the
    local catalog deleted, no seam resolved ``storage_backend_for("catalog")``
    any more, so a stranded ``NX_STORAGE_BACKEND_CATALOG=sqlite`` export was
    silently ignored — the exact silent-ignore the fail-loud directive bans.
    The factory is where every catalog consumer routes, so it fails here
    with the stranded-install redirect.
    """
    from nexus.db.storage_mode import storage_backend_for  # noqa: PLC0415 — deferred to avoid import cycle

    storage_backend_for("catalog")
    _log.debug("catalog_reader_service_mode")
    return _SharedServiceCatalogHandle()


def make_catalog_writer(
    *, config_dir: Optional[Path] = None, priority: Optional[str] = None,
) -> Any:
    """Return a write-only catalog proxy backed by the service.

    Returns a :class:`_ServiceCatalogWriter` that enforces the
    :data:`CATALOG_WRITE_OPS` whitelist and forwards writes to the Java
    Postgres service via HTTP. *priority* is ignored (the service enforces
    its own fairness); the parameter survives for call-site compatibility.
    The resolver call is validation only — see :func:`make_catalog_reader`.
    """
    from nexus.db.storage_mode import storage_backend_for  # noqa: PLC0415 — deferred to avoid import cycle

    storage_backend_for("catalog")
    _log.debug("catalog_writer_service_mode")
    return _ServiceCatalogWriter(_SharedServiceCatalogHandle())


#: nexus-xedhp: extra ops allowed ONLY on the service-mode writer, layered on
#: top of CATALOG_WRITE_OPS rather than added to that shared whitelist. The
#: SQLite/daemon-mode CatalogWriter (below) has no ``update_many`` RPC op in
#: its dispatch table; adding it to the shared CATALOG_WRITE_OPS would make
#: ``getattr(writer, "update_many", None)`` return a bound proxy method there
#: too (CatalogWriter's __getattr__ forwards ANY whitelisted name to a
#: dynamic RPC proxy without validating the daemon actually implements it),
#: defeating the ``callable(getattr(cat, "update_many", None))`` capability
#: check the indexer's catalog hook uses to decide whether to batch — it
#: would look supported and then fail deep in the per-file loop instead of
#: safely falling back. Keeping this service-only means the same capability
#: check is honest for both backends: SQLite mode always falls back to the
#: existing serial ``update()`` loop (unchanged behaviour); service mode
#: gets the batched path.
#: nexus-3ck2g: ``purge_trash`` joins this set for the same reason as
#: ``update_many``/``delete_many`` above — it is a service-only op with no
#: SQLite/daemon-mode equivalent (the local catalog and its daemon are gone,
#: RDR-158 P4). It is service-only for a second, independent reason too: the
#: dry-run COUNT PREVIEW is itself an engine-side read behind the write
#: surface (``purge_trash(dry_run=True)``), not something a caller could
#: compute client-side, so it belongs on the writer even for its read-only
#: mode. Reads never go through ``make_catalog_writer()`` — see the module
#: docstring — so this whitelist entry does not create a reader-through-
#: writer path; it just means the *dry-run preview itself* is a writer op.
#: nexus-fduai: ``record_gc_audit`` is the client-facing gc_audit producer
#: (``POST /v1/catalog/gc_audit/record``) ``nx t3 gc`` reports its own T3
#: delete through — append-only, engine-side, no local equivalent ever.
_SERVICE_ONLY_WRITE_OPS: frozenset[str] = frozenset({
    "update_many", "delete_many", "purge_trash", "record_gc_audit",
})


class _ServiceCatalogWriter:
    """Write-only proxy backed by :class:`HttpCatalogClient` in service mode.

    Enforces the same :data:`CATALOG_WRITE_OPS` whitelist as
    :class:`CatalogWriter`, plus :data:`_SERVICE_ONLY_WRITE_OPS`. Reads are
    blocked.
    """

    def __init__(self, client: Any) -> None:
        self._client = client

    def __getattr__(self, name: str) -> Any:
        if name not in CATALOG_WRITE_OPS and name not in _SERVICE_ONLY_WRITE_OPS:
            raise AttributeError(
                f"{name!r} is not a catalog write op; _ServiceCatalogWriter "
                f"exposes only the {len(CATALOG_WRITE_OPS)}-op whitelist "
                f"(+ {sorted(_SERVICE_ONLY_WRITE_OPS)}). "
                f"For reads use make_catalog_reader()."
            )
        return getattr(self._client, name)

    @property
    def routed(self) -> bool:
        return True

    @property
    def priority(self) -> str:
        return "batch"

    def is_interactive_write_pending(self) -> bool:
        return False

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "_ServiceCatalogWriter":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()


def make_catalog_client_for_migration(
    *,
    base_url: Optional[str] = None,
    token: str = "",
) -> Any:
    """Return an :class:`HttpCatalogClient` for the ``storage migrate catalog`` ETL.

    This is the sole authorised site for constructing an ``HttpCatalogClient``
    with an explicit *base_url* and *token* outside the service-mode defaults.
    Migration needs direct control over the target URL because it runs against
    a specific Postgres service endpoint that may differ from the configured
    default (e.g. a fresh staging instance during an initial data load).

    RDR-176 P2 (Gap 3): the primary callers now pass NO arguments — the CLI
    migrate subcommands resolve ``(base_url, token)`` config-first themselves and
    pass both, while ``migrate all`` / the orchestrator call this no-arg so the
    client resolves URL+token config-first via ``resolve_service_endpoint``. The
    explicit-args form remains for a caller that must target a non-default URL.

    Args:
        base_url: Override the service URL.  ``None`` falls back to the
            client's built-in env/config resolution (``NX_SERVICE_URL``).
        token: Bearer token for ``X-Nexus-Token`` authentication.
            Required; the caller is responsible for sourcing it.

    Returns:
        A live ``HttpCatalogClient`` configured for *base_url* / *token*.
        Callers must call ``.close()`` or use it as a context manager.
    """
    from nexus.catalog.http_catalog_client import HttpCatalogClient  # noqa: PLC0415 — deliberate function-scoped import (defer heavy/optional dep, avoid circular import)

    _log.debug("catalog_client_for_migration", base_url=base_url)
    if base_url:
        # code-review WORTH-TRACKING (nexus-wrwb7 fix pass, nexus-ssqk9
        # relay): deliberately stays fully pinned, unlike the no-base_url
        # branch below. This branch exists specifically for a caller that
        # names a NON-DEFAULT target endpoint (module docstring: "a
        # specific Postgres service endpoint that may differ from the
        # configured default"). A configured mint_token credential is
        # scoped to the DEFAULT managed engine's mint contract; self-minting
        # against it and presenting the result to a DIFFERENT, explicitly-
        # named migration target would be silently wrong, not an
        # improvement — so this call site does NOT apply the data-token
        # override. No live call site passes base_url= today (grepped
        # 2026-08-16); revisit if one appears and genuinely wants
        # self-minting against its own explicit target.
        return HttpCatalogClient(base_url=base_url, _token=token)
    if not token:
        return HttpCatalogClient()
    # code-review Sig#1 addendum (nexus-ssqk9): this branch resolves the
    # SAME default managed endpoint every other T2 store uses (no base_url
    # override), so it gets the identical treatment as the SessionEnd
    # summary fix above — apply the data-token override BEFORE
    # construction so a configured mint_token credential is not silently
    # skipped on this low-traffic migration path either.
    from nexus.db.data_token import get_data_token_manager  # noqa: PLC0415 — deliberate function-scoped import (defer heavy/optional dep, avoid circular import)
    from nexus.db.service_endpoint import resolve_service_endpoint  # noqa: PLC0415 — deliberate function-scoped import (defer heavy/optional dep, avoid circular import)
    from nexus.db.t2._refreshable_client import DEFAULT_TENANT  # noqa: PLC0415 — deliberate function-scoped import (defer heavy/optional dep, avoid circular import)

    resolved_base_url, _ = resolve_service_endpoint()
    data_token = get_data_token_manager().bearer_for(resolved_base_url, DEFAULT_TENANT)
    return HttpCatalogClient(
        base_url=resolved_base_url,
        _token=data_token if data_token is not None else token,
    )
