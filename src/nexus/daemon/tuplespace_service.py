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


# nexus-73vq: blocking_take poll cadence. Short enough that wake
# latency after a competing out() is operator-acceptable (well under
# the CA-5 reactive-take latency budget); long enough that idle
# RPCs aren't burning CPU. 10ms matches the daemon's EventStream
# data_version poll interval in t2_daemon (nexus-m4gm).
_BLOCKING_TAKE_POLL_INTERVAL_S: float = 0.010


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
        self._index: TupleIndex = TupleIndex.from_registry(
            self._registry, chroma_client
        )
        _log.info(
            "tuplespace_service_started",
            tuples_db=str(tuples_db_path),
        )

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
        in the daemon's thread-pool executor). Competing ``out()``
        calls increment ``PRAGMA data_version`` which this loop
        observes via a dedicated read-only connection (no lock
        contention against the service's main writer).

        Args:
            subspace: Concrete subspace string.
            query: Semantic query text.
            claimant: Unique identifier for the claiming agent.
            where: Optional ChromaDB metadata filter dict.
            floor: Minimum similarity threshold (overrides schema default).
            lease_seconds: Lease duration override.
            timeout_seconds: Maximum wait. Capped at 30s by ``api.take``
                contract (``InvalidTimeoutError``).

        Returns:
            ``{"tuple": <dict>, "claim_id": <str>}`` on success, or
            ``None`` when the deadline elapses with no candidate.
        """
        # api.take(block=True) raises BlockingNotSupported, so we drive
        # the poll loop ourselves and call api.take(block=False) inside.
        deadline = time.perf_counter() + max(0.0, float(timeout_seconds))

        # Dedicated read-only connection for the PRAGMA data_version
        # poll so the polling loop doesn't contend with the main
        # writer's lock. WAL mode allows multiple readers concurrently.
        # storage-boundary-allow: daemon-internal-data_version-poll (nexus-73vq)
        version_conn = sqlite3.connect(
            f"file:{self._tuples_db_path}?mode=ro",
            uri=True,
            check_same_thread=False,
        )
        try:
            last_version = self._read_data_version(version_conn)
            while True:
                # Try a non-blocking claim under the service lock.
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
                if result is not None:
                    t_dict, claim_id = result
                    return {"tuple": t_dict, "claim_id": claim_id}

                # No candidate. Compute remaining budget.
                remaining = deadline - time.perf_counter()
                if remaining <= 0:
                    return None

                # Poll data_version with a short sleep cap, so we
                # observe new commits within at most one tick.
                sleep_for = min(_BLOCKING_TAKE_POLL_INTERVAL_S, remaining)
                time.sleep(sleep_for)

                current_version = self._read_data_version(version_conn)
                if current_version == last_version:
                    # Nothing committed; loop will check the deadline at
                    # the top of the next iteration.
                    continue
                last_version = current_version
        finally:
            try:
                version_conn.close()
            except Exception:
                pass

    @staticmethod
    def _read_data_version(conn: sqlite3.Connection) -> int:
        row = conn.execute("PRAGMA data_version").fetchone()
        return int(row[0]) if row else 0

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
        """Close the SQLite connection.

        nexus-dxap: failure on close (e.g. ``sqlite3.OperationalError``
        when the connection's thread is gone, or an EROFS on the
        journal file) is logged as a warning. The previous behaviour
        was a silent ``except Exception: pass`` that left operators
        with no signal when shutdown didn't actually release the
        underlying handle. Idempotent: a follow-up call on an
        already-closed connection is harmless.
        """
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
