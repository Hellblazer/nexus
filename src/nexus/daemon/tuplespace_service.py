# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tuplespace service, daemon-side wrapper exposing the tuplespace
free-function API (``nexus.tuplespace.api``) as a set of RPC handlers.

RDR-112 / nexus-6s8v: Tuplespace was missing from ``_T2_STORE_ATTRS`` in
``t2_daemon.py``. Every tuplespace MCP tool ``AttributeError``ed under
``NX_STORAGE_MODE=daemon`` because ``mcp/core.py`` returned
``conn=None, index=None`` in daemon mode (no daemon RPCs existed).

Why a service class instead of adding ``"tuplespace"`` to
``_T2_STORE_ATTRS``
-------------------------------------------------------------------
The other domain stores expose bound methods on ``t2db.<attr>`` that
introspect cleanly via ``inspect.getmembers``. Tuplespace operates on
*three* injected resources (``sqlite3.Connection``, ``TupleIndex``,
``Registry``) and the public API lives as free functions in
``nexus.tuplespace.api``, there is no single store object whose methods
share a signature pattern that can be enumerated. A purpose-built
service wrapping the three resources is cleaner than retrofitting the
free-function API onto the introspection path.

RPC surface (registered in T2Daemon via ``register_tuplespace_rpcs``):
  - ``tuplespace.out``
  - ``tuplespace.read``
  - ``tuplespace.take``
  - ``tuplespace.ack``
  - ``tuplespace.nack``
  - ``tuplespace.list_subspaces``
  - ``tuplespace.subspace_schema``
  - ``tuplespace.subspace_stats``

EventStream subscriptions (``event_stream.subscribe``) are already
served by the existing ``handle_event_stream`` machinery (P1.3
nexus-m4gm), no new RPC needed for that.

Connection lifecycle
--------------------
The daemon owns a single ``sqlite3.Connection`` to ``tuples.db`` (WAL
mode). All tuplespace RPCs run in the daemon's thread-pool executor;
because the daemon serialises them on the single connection there is
no concurrent-write race. The connection is closed when ``close()`` is
called (lifecycle managed by T2Daemon).
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

import structlog

from nexus.tuplespace import api as ts_api
from nexus.tuplespace.index import TupleIndex
from nexus.tuplespace.registry import Registry, default_builtin_dir
from nexus.tuplespace.store import open_tuples_db

_log = structlog.get_logger(__name__)


# nexus-73vq: prior blocking_take poll cadence. Retained as a hint of
# the original 10 ms periodic-poll design; no longer referenced after
# nexus-z4m7 (CR-3) restored the data_version wake mechanism. Safe to
# remove once any external follow-up bead settles whether a periodic
# wake-up fallback should also be reintroduced.
_BLOCKING_TAKE_POLL_INTERVAL_S: float = 0.010


# nexus-2kld.1 (HR-1): cap concurrent in-flight blocking_take RPCs so
# the daemon's asyncio thread pool cannot be starved by long-polling
# callers. Each blocking RPC holds an executor thread + a read-only
# SQLite conn for up to 30s; without this gate, N > pool-size
# callers would silently queue at the asyncio dispatch level with no
# explicit error. 16 is the v1 default — high enough for multi-agent
# workloads, low enough to leave headroom for non-blocking RPCs in
# the default ``min(32, cpu+4)`` executor.
_BLOCKING_TAKE_MAX_CONCURRENT: int = 16


class BlockingTakeResourceExhausted(RuntimeError):
    """Raised when ``blocking_take`` cannot acquire a concurrency slot.

    nexus-2kld.1 (HR-1): explicit fail-loud error returned to the
    caller when the daemon's per-process blocking_take cap
    (``_BLOCKING_TAKE_MAX_CONCURRENT``) is saturated. Surfacing this
    as a typed exception lets clients implement back-off rather than
    silently waiting at the connection layer.
    """


# nexus-z4m7 (CR-3): adaptive cadence for the daemon-side data_version
# watcher. Mirrors ``tuplespace/watcher.py`` direct-mode constants so
# the daemon delivers RDR-110 CA #5's 1-2 ms median wake claim while
# letting idle systems amortise the polling cost. Active polls run at
# 1 ms (CA #6 verified the cost is negligible on M-series hardware);
# after ``_DAEMON_WAKE_IDLE_RAMP`` consecutive idle ticks (~100 ms of
# dead air) the cadence doubles each tick until it reaches
# ``_DAEMON_WAKE_POLL_MAX_S``, cutting idle CPU by ~1000x. Activity
# (any data_version increment) resets the interval back to baseline.
_DAEMON_WAKE_POLL_BASELINE_S: float = 0.001
_DAEMON_WAKE_POLL_MAX_S: float = 1.0
_DAEMON_WAKE_IDLE_RAMP: int = 100


def _next_daemon_wake_interval(
    *, idle_polls: int, current: Optional[float]
) -> float:
    """Adaptive cadence helper for the daemon's data_version watcher.

    Pure function: deterministic, no side effects, no I/O. Mirrors
    ``tuplespace.watcher._next_poll_interval`` but kept separate so
    the daemon-internal path does not depend on the direct-mode
    module's daemon-mode guard.
    """
    if idle_polls == 0:
        return _DAEMON_WAKE_POLL_BASELINE_S
    if idle_polls <= _DAEMON_WAKE_IDLE_RAMP or current is None:
        return _DAEMON_WAKE_POLL_BASELINE_S
    doubled = min(current * 2.0, _DAEMON_WAKE_POLL_MAX_S)
    return max(doubled, _DAEMON_WAKE_POLL_BASELINE_S)


class TuplespaceService:
    """Daemon-side service wrapping the tuplespace free-function API.

    Construct once at daemon start with the path to ``tuples.db`` and a
    chromadb client. The service opens its own SQLite connection (the
    daemon is the single writer per RDR-112 §9) and builds a
    ``TupleIndex`` from the configured registry.

    Args:
        tuples_db_path: Filesystem path to ``tuples.db``. Created if absent.
        chroma_client: Any chromadb ``ClientAPI``-compatible instance.
            Production daemons pass ``PersistentClient(path=<chroma_dir>)``;
            tests pass ``EphemeralClient()``.
        registry: Loaded :class:`Registry`. When ``None``, the default
            builtin registry is loaded (production path). Tests inject
            registries with custom subspace YAML for isolation.
    """

    def __init__(
        self,
        *,
        tuples_db_path: Path,
        chroma_client: Any,
        registry: Optional[Registry] = None,
    ) -> None:
        self._tuples_db_path = tuples_db_path
        self._registry = registry if registry is not None else Registry.load(
            default_builtin_dir()
        )
        # Initialise schema via the public helper, then reopen the
        # connection with ``check_same_thread=False`` because the daemon
        # dispatches handlers in the thread-pool executor. The daemon
        # serialises tuplespace RPCs across the single connection (one
        # writer at a time), SQLite's WAL mode permits concurrent
        # readers safely so passing the connection between executor
        # threads is well-defined here.
        open_tuples_db(tuples_db_path).close()
        self._conn: sqlite3.Connection = sqlite3.connect(
            str(tuples_db_path),
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        # Serialise SQL execution across executor threads. Single shared
        # connection + threading.Lock is the standard CPython pattern for
        # a sync-API service called from an asyncio thread-pool.
        self._lock = threading.Lock()
        # nexus-2kld.1 (HR-1): semaphore caps concurrent blocking_take
        # RPCs. Initialised from the module-level constant at construct
        # time so tests can monkey-patch the constant before the
        # service is built.
        self._blocking_take_sema = threading.Semaphore(
            _BLOCKING_TAKE_MAX_CONCURRENT
        )
        self._index: TupleIndex = TupleIndex.from_registry(
            self._registry, chroma_client
        )
        # nexus-z4m7 (CR-3): data_version watcher. Polls PRAGMA
        # data_version on a dedicated read-only connection and fires
        # ``self._wake_event`` on each detected commit. Blocking
        # ``take`` callers wait on the event with their own timeout so
        # RDR-110 CA #5's 1-2 ms median wake claim holds. Started
        # eagerly so the first blocking_take pays no cold-start cost.
        self._wake_event = threading.Event()
        self._wake_stop = threading.Event()
        # _wake_baselined fires once the watcher has captured its first
        # data_version reading, so callers (and tests) can synchronise
        # on "watcher is now observing changes" rather than racing the
        # thread's startup. Without this, a commit landing between
        # `_wake_thread.start()` and the first poll would be invisible
        # to the watcher (baseline captures the post-commit version).
        self._wake_baselined = threading.Event()
        self._wake_thread: Optional[threading.Thread] = None
        self._start_wake_watcher()
        # Block __init__ until the watcher has captured baseline so
        # any subsequent commit is guaranteed to fire wake_event.
        self._wake_baselined.wait(timeout=2.0)
        _log.info(
            "tuplespace_service_started",
            tuples_db=str(tuples_db_path),
        )

    def _start_wake_watcher(self) -> None:
        """Spawn the data_version watcher thread (idempotent)."""
        if self._wake_thread is not None and self._wake_thread.is_alive():
            return
        self._wake_stop.clear()
        self._wake_thread = threading.Thread(
            target=self._wake_watcher_loop,
            name="t2-tuplespace-data-version-watcher",
            daemon=True,
        )
        self._wake_thread.start()

    def _wake_watcher_loop(self) -> None:
        """Run on the watcher thread: open a read conn and poll data_version."""
        try:
            # storage-boundary-allow: daemon-internal data_version polling
            # for RDR-110 CA #5 wake source (nexus-z4m7). Read-only WAL
            # reader cannot contend with the service's main writer.
            conn = sqlite3.connect(
                f"file:{self._tuples_db_path}?mode=ro",
                uri=True,
                check_same_thread=False,
            )
        except sqlite3.Error as exc:
            _log.warning(
                "wake_watcher_connect_failed",
                db=str(self._tuples_db_path),
                error=str(exc),
            )
            return
        try:
            last_version: Optional[int] = None
            idle_polls: int = 0
            interval: float = _DAEMON_WAKE_POLL_BASELINE_S
            while not self._wake_stop.is_set():
                activity = False
                try:
                    row = conn.execute("PRAGMA data_version").fetchone()
                    version = int(row[0]) if row else 0
                except sqlite3.Error as exc:
                    _log.warning(
                        "wake_watcher_poll_failed",
                        error=str(exc),
                    )
                    self._wake_stop.wait(interval)
                    continue
                if last_version is None:
                    last_version = version
                    self._wake_baselined.set()
                elif version != last_version:
                    last_version = version
                    activity = True
                    self._wake_event.set()
                if activity:
                    idle_polls = 0
                else:
                    idle_polls += 1
                interval = _next_daemon_wake_interval(
                    idle_polls=idle_polls, current=interval
                )
                self._wake_stop.wait(timeout=interval)
        finally:
            try:
                conn.close()
            except sqlite3.Error:
                pass

    # ------------------------------------------------------------------
    # RPC handlers, keyword-only contracts mirroring nexus.tuplespace.api
    # ------------------------------------------------------------------

    def out(
        self,
        *,
        subspace: str,
        content: str,
        dimensions: dict[str, Any],
        match_text: Optional[str] = None,
        ttl_seconds: Optional[float] = None,
    ) -> str:
        with self._lock:
            return ts_api.out(
                conn=self._conn,
                index=self._index,
                registry=self._registry,
                subspace=subspace,
                content=content,
                dimensions=dimensions,
                match_text=match_text,
                ttl_seconds=ttl_seconds,
            )

    def read(
        self,
        *,
        subspace: str,
        query: str,
        where: Optional[dict[str, Any]] = None,
        floor: Optional[float] = None,
        n: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        with self._lock:
            return ts_api.read(
                conn=self._conn,
                index=self._index,
                registry=self._registry,
                subspace=subspace,
                query=query,
                where=where,
                floor=floor,
                n=n,
            )

    def take(
        self,
        *,
        subspace: str,
        query: str,
        claimant: str,
        where: Optional[dict[str, Any]] = None,
        floor: Optional[float] = None,
        lease_seconds: Optional[float] = None,
        block: bool = False,
        timeout_seconds: Optional[float] = None,
    ) -> Optional[dict[str, Any]]:
        """Wraps ``api.take``; returns a single dict (or None) for JSON
        friendliness.

        ``api.take`` returns ``(tuple_dict, claim_id) | None``. The RPC
        wraps the tuple as ``{"tuple": <dict>, "claim_id": <str>}`` so the
        client sees a single dict result.

        nexus-ry0v (RDR-110 P3.1, 2026-05-17): when ``block=True`` is
        requested, this method delegates to :meth:`blocking_take`
        rather than forwarding the flag to ``api.take`` (which raises
        ``BlockingNotSupported`` for the direct-mode path). The daemon
        owns the SQLite handle and has a real ``PRAGMA data_version``
        wake source, so the blocking semantics are well-defined here.
        ``block=False`` keeps the legacy fast-path through ``api.take``.
        """
        if block:
            return self.blocking_take(
                subspace=subspace,
                query=query,
                claimant=claimant,
                where=where,
                floor=floor,
                lease_seconds=lease_seconds,
                timeout_seconds=(
                    float(timeout_seconds)
                    if timeout_seconds is not None
                    else 30.0
                ),
            )
        with self._lock:
            result = ts_api.take(
                conn=self._conn,
                index=self._index,
                registry=self._registry,
                subspace=subspace,
                query=query,
                claimant=claimant,
                where=where,
                floor=floor,
                lease_seconds=lease_seconds,
                block=False,
                timeout_seconds=timeout_seconds,
            )
        if result is None:
            return None
        t_dict, claim_id = result
        return {"tuple": t_dict, "claim_id": claim_id}

    # nexus-3tl3.2 (SR-2): timeout_seconds is capped at 30 s to match
    # the api.take(block=True) contract documented in
    # nexus.tuplespace.api.InvalidTimeoutError. Without this cap, a
    # same-UID client could call blocking_take(timeout_seconds=99999)
    # and hold a thread-pool worker + read-only SQLite connection for
    # the requested duration; ~9 such connections would starve the
    # dispatcher (RDR-114 §A1, post-cutover review 2026-05-17).
    _BLOCKING_TAKE_MAX_TIMEOUT_S: float = 30.0

    def blocking_take(
        self,
        *,
        subspace: str,
        query: str,
        claimant: str,
        where: Optional[dict[str, Any]] = None,
        floor: Optional[float] = None,
        lease_seconds: Optional[float] = None,
        timeout_seconds: float = 30.0,
    ) -> Optional[dict[str, Any]]:
        """Poll ``take()`` until a candidate is claimed or the deadline fires.

        nexus-73vq (RDR-112 P1.3.1): the daemon-side companion to
        RDR-110's direct-mode ``_DataVersionWatcher``. The daemon
        owns the SQLite handle, so the polling loop lives in this
        process (one polling task per blocking_take RPC, dispatched
        in the daemon's thread-pool executor).

        nexus-z4m7 (CR-3): blocking_take waits on ``self._wake_event``
        which the daemon-internal data_version watcher fires on each
        detected commit. The watcher polls at ``_DAEMON_WAKE_POLL_BASELINE_S``
        (1 ms baseline, ramping toward ``_DAEMON_WAKE_POLL_MAX_S`` when
        idle), restoring the RDR-110 CA #5 1-2 ms median wake claim
        that HR-3's ``vestigial cleanup`` had silently weakened to the
        prior 10 ms poll floor.

        nexus-2kld.1 (HR-1): each in-flight blocking_take holds a
        slot on ``self._blocking_take_sema``. Beyond
        ``_BLOCKING_TAKE_MAX_CONCURRENT`` concurrent calls,
        ``BlockingTakeResourceExhausted`` is raised explicitly rather
        than letting callers queue silently at the asyncio dispatch
        layer.

        Cross-subspace wake characteristic (DR-5): any commit anywhere
        on the file fires the shared wake_event, so callers in
        unrelated subspaces wake spuriously and re-poll. On
        multi-agent multi-subspace deployments with high cross-
        subspace write rates this is O(N callers x commit rate)
        speculative claim attempts. Acceptable for v1; a future
        per-subspace ``threading.Event`` channel is tracked under the
        umbrella backlog (nexus-ku5k.1).

        Args:
            subspace: Concrete subspace string.
            query: Semantic query text.
            claimant: Unique identifier for the claiming agent.
            where: Optional ChromaDB metadata filter dict.
            floor: Minimum similarity threshold (overrides schema default).
            lease_seconds: Lease duration override.
            timeout_seconds: Maximum wait. Capped at 30 s
                (``InvalidTimeoutError`` when exceeded).

        Returns:
            ``{"tuple": <dict>, "claim_id": <str>}`` on success, or
            ``None`` when the deadline elapses with no candidate.

        Raises:
            InvalidTimeoutError: ``timeout_seconds`` > 30.
            BlockingTakeResourceExhausted: daemon's concurrent
                blocking_take cap is saturated.
        """
        # nexus-3tl3.2 (SR-2): enforce the same 30 s cap that
        # api.take(block=True) advertises via InvalidTimeoutError.
        # Without this gate the polling loop would honour any caller-
        # supplied timeout, including absurd values that starve the
        # daemon's thread-pool.
        from nexus.tuplespace.api import InvalidTimeoutError  # noqa: PLC0415

        if (
            timeout_seconds is not None
            and float(timeout_seconds) > self._BLOCKING_TAKE_MAX_TIMEOUT_S
        ):
            raise InvalidTimeoutError(
                f"timeout_seconds={timeout_seconds} exceeds the daemon's "
                f"blocking_take cap of {self._BLOCKING_TAKE_MAX_TIMEOUT_S} s "
                "(MCP transport budget, RDR-110 §Technical Design)"
            )

        # nexus-2kld.1 (HR-1): non-blocking semaphore acquire. If the
        # cap is saturated, fail loud with a typed exception so the
        # caller can back off rather than wait at the connection layer.
        if not self._blocking_take_sema.acquire(blocking=False):
            raise BlockingTakeResourceExhausted(
                f"daemon's concurrent blocking_take cap "
                f"({_BLOCKING_TAKE_MAX_CONCURRENT}) is saturated; "
                "retry later or back off"
            )
        try:
            # api.take(block=True) raises BlockingNotSupported, so we drive
            # the poll loop ourselves and call api.take(block=False) inside.
            deadline = time.perf_counter() + max(0.0, float(timeout_seconds))
            while True:
                # nexus-abhy (S360-conc S1): bail out promptly when the
                # service is shutting down so executor threads do not
                # keep polling for the remainder of their 30 s budget.
                # ``close()`` sets both ``_wake_stop`` and ``_wake_event``
                # so an in-flight wait wakes too.
                if self._wake_stop.is_set():
                    return None
                # nexus-z4m7 (CR-3): clear the wake event BEFORE the
                # take attempt so any commit observed after this point
                # is guaranteed to fire a fresh wake. This is the
                # canonical edge-triggered Event pattern; a wake that
                # races the take is caught by the wait() below.
                self._wake_event.clear()
                try:
                    with self._lock:
                        result = ts_api.take(
                            conn=self._conn,
                            index=self._index,
                            registry=self._registry,
                            subspace=subspace,
                            query=query,
                            claimant=claimant,
                            where=where,
                            floor=floor,
                            lease_seconds=lease_seconds,
                            block=False,
                        )
                except sqlite3.ProgrammingError:
                    # nexus-abhy (S360-conc S1): the underlying conn was
                    # closed beneath us (shutdown race). Treat as a
                    # graceful timeout rather than surfacing the raw
                    # sqlite error to the caller.
                    if self._wake_stop.is_set():
                        return None
                    raise
                if result is not None:
                    t_dict, claim_id = result
                    return {"tuple": t_dict, "claim_id": claim_id}

                # No candidate. Compute remaining budget.
                remaining = deadline - time.perf_counter()
                if remaining <= 0:
                    return None

                # nexus-z4m7 (CR-3): wait on the data_version wake
                # event. The watcher fires it within the active
                # cadence (1 ms baseline, RDR-110 CA #5 / CA #6) of
                # any commit, so the median wake here is ~1-2 ms. The
                # ``remaining`` budget bounds the wait so a missed
                # signal still surfaces as a timeout.
                self._wake_event.wait(timeout=remaining)
        finally:
            self._blocking_take_sema.release()

    def ack(self, *, claim_id: str, claimant: str) -> str:
        with self._lock:
            ts_api.ack(conn=self._conn, claim_id=claim_id, claimant=claimant)
        return "ok"

    def nack(self, *, claim_id: str, claimant: str) -> str:
        with self._lock:
            ts_api.nack(conn=self._conn, claim_id=claim_id, claimant=claimant)
        return "ok"

    def list_subspaces(self) -> list[str]:
        return ts_api.list_subspaces(registry=self._registry)

    def subspace_schema(self, *, subspace: str) -> dict[str, Any]:
        return ts_api.subspace_schema(registry=self._registry, subspace=subspace)

    def subspace_stats(self, *, subspace: str) -> dict[str, Any]:
        with self._lock:
            return ts_api.subspace_stats(conn=self._conn, subspace=subspace)

    # ------------------------------------------------------------------
    # Read-only RPCs for cockpit panels (RDR-112 boundary; nexus-x65c)
    # ------------------------------------------------------------------

    def list_active_claims(
        self, *, now: Optional[float] = None
    ) -> list[dict[str, Any]]:
        """Return active claims as a list of dicts (JSON-friendly).

        Mirrors ``nexus.cockpit.panels.active_claims.fetch_active_claims``
        so cockpit panels under ``NX_STORAGE_MODE=daemon`` can avoid
        opening a second SQLite handle on ``tuples.db``. The daemon is
        the single writer (RDR-112 §9); read RPCs must originate from
        this process to preserve the boundary.
        """
        import time as _time

        ts = _time.time() if now is None else float(now)
        with self._lock:
            cur = self._conn.execute(
                "SELECT subspace, id AS tuple_id, claim_id, claimant, "
                "claim_expires_at "
                "FROM tuples "
                "WHERE claim_state = 'claimed' "
                "  AND consumed_at IS NULL "
                "  AND (claim_expires_at IS NULL OR claim_expires_at > ?) "
                "ORDER BY subspace, claim_expires_at",
                (ts,),
            )
            rows = [dict(r) for r in cur.fetchall()]
        out: list[dict[str, Any]] = []
        for r in rows:
            expires = r.get("claim_expires_at")
            out.append(
                {
                    "subspace": r["subspace"],
                    "tuple_id": r["tuple_id"],
                    "claim_id": r.get("claim_id") or "",
                    "claimant": r.get("claimant") or "",
                    "ttl_remaining_seconds": (
                        None
                        if expires is None
                        else max(0.0, float(expires) - ts)
                    ),
                }
            )
        return out

    def recent_events(self, *, limit: int = 25) -> list[dict[str, Any]]:
        """Return the newest ``limit`` rows from the events table.

        Mirrors ``nexus.cockpit.panels.recent_events.fetch_recent_events``
        so cockpit panels under daemon mode read through the daemon
        instead of opening ``tuples.db`` directly (RDR-112 boundary).
        """
        n = int(limit)
        if n <= 0:
            return []
        with self._lock:
            cur = self._conn.execute(
                "SELECT rowid, subspace, op, tuple_id, payload_summary, "
                "category, ts "
                "FROM events "
                "ORDER BY rowid DESC LIMIT ?",
                (n,),
            )
            rows = [dict(r) for r in cur.fetchall()]
        out: list[dict[str, Any]] = []
        for r in rows:
            out.append(
                {
                    "cursor": int(r["rowid"]),
                    "subspace": r["subspace"],
                    "op": r["op"],
                    "tuple_id": r["tuple_id"],
                    "ts": float(r["ts"]),
                    "payload_summary": r.get("payload_summary"),
                    "category": r.get("category"),
                }
            )
        return out

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Stop the wake watcher and close the SQLite connection.

        nexus-dxap: failure on close (e.g. ``sqlite3.OperationalError``
        when the connection's thread is gone, or an EROFS on the
        journal file) is logged as a warning. The previous behaviour
        was a silent ``except Exception: pass`` that left operators
        with no signal when shutdown didn't actually release the
        underlying handle. Idempotent: a follow-up call on an
        already-closed connection is harmless.

        nexus-z4m7 (CR-3): signal the wake watcher to stop and join
        it before tearing down the writer. The thread is daemon=True
        so a hard-exit will not deadlock, but join keeps the resource
        story clean for tests and graceful shutdown.
        """
        self._wake_stop.set()
        # Re-set the event so any waiter in blocking_take returns
        # immediately rather than waiting out its full timeout.
        self._wake_event.set()
        if self._wake_thread is not None and self._wake_thread.is_alive():
            self._wake_thread.join(timeout=1.5)
        try:
            self._conn.close()
        except Exception as exc:
            _log.warning(
                "tuplespace_service_close_failed",
                error=str(exc),
                error_type=type(exc).__qualname__,
            )


# ---------------------------------------------------------------------------
# Registration helper
# ---------------------------------------------------------------------------


def register_tuplespace_rpcs(
    rpc_table: dict[str, Any],
    service: TuplespaceService,
) -> None:
    """Register all ``tuplespace.*`` RPC handlers into ``rpc_table``.

    Used by ``T2Daemon.__init__`` after the dispatch table is built.
    Keeping the registration list in one place ensures the daemon and
    the corresponding ``T2Client.tuplespace`` proxy stay in sync.

    Args:
        rpc_table: The mutable dispatch table on ``T2Daemon``.
        service: A constructed ``TuplespaceService``.
    """
    handlers = {
        "tuplespace.out": service.out,
        "tuplespace.read": service.read,
        "tuplespace.take": service.take,
        # nexus-73vq (RDR-112 P1.3.1)
        "tuplespace.blocking_take": service.blocking_take,
        "tuplespace.ack": service.ack,
        "tuplespace.nack": service.nack,
        "tuplespace.list_subspaces": service.list_subspaces,
        "tuplespace.subspace_schema": service.subspace_schema,
        "tuplespace.subspace_stats": service.subspace_stats,
        "tuplespace.list_active_claims": service.list_active_claims,
        "tuplespace.recent_events": service.recent_events,
    }
    for op, fn in handlers.items():
        rpc_table[op] = fn
    _log.info("tuplespace_rpcs_registered", count=len(handlers))


#: Public list of tuplespace RPC op names. Imported by ``T2Client.tuplespace``
#: so the client and the daemon stay in lockstep, adding an op here is the
#: single point of change.
TUPLESPACE_RPC_OPS: tuple[str, ...] = (
    "out",
    "read",
    "take",
    # nexus-73vq (RDR-112 P1.3.1): daemon-side polling take that
    # wakes on PRAGMA data_version increments until the deadline.
    "blocking_take",
    "ack",
    "nack",
    "list_subspaces",
    "subspace_schema",
    "subspace_stats",
    "list_active_claims",
    "recent_events",
)
