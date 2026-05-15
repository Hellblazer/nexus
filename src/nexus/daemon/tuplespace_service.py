# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tuplespace service — daemon-side wrapper exposing the tuplespace
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
``nexus.tuplespace.api`` — there is no single store object whose methods
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
nexus-m4gm) — no new RPC needed for that.

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
from pathlib import Path
from typing import Any, Optional

import structlog

from nexus.tuplespace import api as ts_api
from nexus.tuplespace.index import TupleIndex
from nexus.tuplespace.registry import Registry, default_builtin_dir
from nexus.tuplespace.store import open_tuples_db

_log = structlog.get_logger(__name__)


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
        # writer at a time) — SQLite's WAL mode permits concurrent
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
    # RPC handlers — keyword-only contracts mirroring nexus.tuplespace.api
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
                match_text=match_text or None,
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
        """
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
                block=block,
                timeout_seconds=timeout_seconds,
            )
        if result is None:
            return None
        t_dict, claim_id = result
        return {"tuple": t_dict, "claim_id": claim_id}

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
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Close the SQLite connection. Idempotent."""
        try:
            self._conn.close()
        except Exception:  # pragma: no cover — defensive
            pass


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
        "tuplespace.ack": service.ack,
        "tuplespace.nack": service.nack,
        "tuplespace.list_subspaces": service.list_subspaces,
        "tuplespace.subspace_schema": service.subspace_schema,
        "tuplespace.subspace_stats": service.subspace_stats,
    }
    for op, fn in handlers.items():
        rpc_table[op] = fn
    _log.info("tuplespace_rpcs_registered", count=len(handlers))


#: Public list of tuplespace RPC op names. Imported by ``T2Client.tuplespace``
#: so the client and the daemon stay in lockstep — adding an op here is the
#: single point of change.
TUPLESPACE_RPC_OPS: tuple[str, ...] = (
    "out",
    "read",
    "take",
    "ack",
    "nack",
    "list_subspaces",
    "subspace_schema",
    "subspace_stats",
)
