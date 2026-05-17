# SPDX-License-Identifier: AGPL-3.0-or-later
"""T2Client — synchronous RPC proxy for the T2 daemon.

RDR-112 P1.2 (nexus-qy0u): client facade that mirrors ``T2Database``'s shape
so call sites flip in Phase 3 via constructor injection only.

Architecture
------------
``T2Client`` is **synchronous** — matching ``T2Database``'s sync nature — and
maintains a small pool of persistent socket connections (UDS or TCP) to amortise
the per-call connect overhead.

Each attribute proxy (``.memory``, ``.plans``, etc.) returns a ``_StoreProxy``
whose ``__getattr__`` yields a callable with the **exact same signature** as the
corresponding domain-store method (set via ``__signature__``).  ``inspect.signature``
on any proxy method therefore returns the same result as on the real store class.

Serialization
-------------
The client uses the type-tagged encoder from ``t2_daemon``:
  - ``datetime`` -> ``{"__datetime__": "<ISO-8601>"}``
  - ``bytes``    -> ``{"__bytes__": "<base64>"}``
  - ``Path``     -> ``{"__path__": "<str>"}``
  - dataclass    -> ``{"__dataclass__": "<cls>", "fields": {...}}``

Non-serialisable arguments raise ``T2DaemonError`` immediately, before the
socket is touched.

Error re-raise policy
---------------------
Remote exceptions whose ``type`` tail (last dotted component) matches a
built-in exception class (``KeyError``, ``ValueError``, etc.) are re-raised
as that class with the remote ``message``.  All others raise
``T2DaemonError(message, type_name=..., remote_traceback=...)``.

Constants area
--------------
A ``# Constants`` section near the top of the class body is reserved for
version-handshake constants that nexus-w0et (P1.4 migration runner) will
drop in.

Connection
----------
One of ``uds_path`` or ``tcp_addr`` must be provided (mutually exclusive).

  - ``uds_path``: path to the Unix domain socket file.
  - ``tcp_addr``: ``(host, port)`` tuple for TCP loopback.

The pool is created lazily on the first RPC call.

Usage
-----
::

    client = T2Client(uds_path=Path("/tmp/nx-t2.sock"))
    row_id = client.memory.put(project="myproj", title="note.md", content="hello")
    entry  = client.memory.get(project="myproj", title="note.md")
"""
from __future__ import annotations

import builtins
import contextlib
import inspect
import random
import socket
import struct
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import structlog

from nexus.daemon.t2_daemon import (
    DAEMON_PROTOCOL_VERSION,
    DAEMON_SCHEMA_VERSION,
    t2_json_dumps,
    t2_json_loads,
)

_log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Schema-version handshake constant (nexus-w0et RDR-112 P1.4)
# ---------------------------------------------------------------------------

#: Expected schema version from the daemon's hello_ack.
#:
#: The daemon includes ``schema_version: int`` in its hello_ack response.
#: The client compares against this constant and raises ``T2DaemonError``
#: with a directional instruction on mismatch:
#:
#: * ``client_version > daemon_version`` — daemon is older; restart it so
#:   the migration runner applies the missing migrations automatically.
#: * ``client_version < daemon_version`` — daemon is newer; upgrade the
#:   client package.
#:
#: Mutable at module level so tests can monkey-patch it without subclassing.
T2_SCHEMA_VERSION_EXPECTED: int = DAEMON_SCHEMA_VERSION

# ---------------------------------------------------------------------------
# Public exception
# ---------------------------------------------------------------------------


class T2DaemonError(Exception):
    """Raised when the T2 daemon returns an error for an RPC that is not a
    recognised built-in exception.

    Attributes:
        message: Human-readable error message from the daemon.
        type_name: Fully-qualified exception type name from the daemon.
        remote_traceback: Formatted traceback from the daemon, if available.
    """

    def __init__(
        self,
        message: str,
        *,
        type_name: str = "",
        remote_traceback: str = "",
    ) -> None:
        super().__init__(message)
        self.type_name = type_name
        self.remote_traceback = remote_traceback

    def __str__(self) -> str:
        base = super().__str__()
        if self.type_name:
            return f"[{self.type_name}] {base}"
        return base


class RpcTimeoutError(Exception):
    """Raised when a T2Client RPC exceeds the configured socket timeout.

    RDR-114 Step 4 (nexus-wcs9): a hung daemon (UDS accepts connections
    but never replies) used to stall the client indefinitely because
    ``socket.recv`` had no per-call timeout. Now the connection socket
    is set to ``rpc_timeout_seconds`` and a recv-side timeout surfaces
    as this typed exception.

    The class is **deliberately not** a subclass of
    ``ConnectionRefusedError`` (or any ``OSError``) so the EventStream
    reconnect wrapper (RDR-114 Step 1, nexus-wfko) can distinguish
    "daemon hung, retryable" from "daemon gone, re-discover" by
    exception class without collapsing both into the same OSError
    branch.
    """


class EventStreamUnavailable(Exception):
    """Raised by ``T2Client.event_stream`` when the reconnect budget is exhausted.

    RDR-114 Step 1 (nexus-wfko): the reconnect wrapper retries close-side
    failures (``RpcTimeoutError``, ``ConnectionRefusedError``,
    ``ConnectionResetError``, ``ConnectionError`` / ``OSError``) with
    capped exponential backoff + jitter. After ``max_reconnect_attempts``
    consecutive failures the wrapper gives up and raises this typed
    exception. The ``last_cursor`` attribute carries the last-yielded
    event cursor so a supervising loop can decide whether to escalate,
    re-discover, or exit.

    Like ``RpcTimeoutError``, this is **deliberately not** an
    ``OSError`` subclass so caller ``except OSError`` blocks do not
    silently swallow exhaustion.
    """

    def __init__(self, message: str, *, last_cursor: int) -> None:
        super().__init__(message)
        self.last_cursor = last_cursor


# ---------------------------------------------------------------------------
# Wire helpers (synchronous socket I/O)
# ---------------------------------------------------------------------------

#: Maximum accepted wire-frame payload size (bytes). MUST match the daemon
#: cap in :data:`nexus.daemon.t2_daemon._MAX_FRAME_BYTES` or one side leaks.
#: nexus-ex4r (RDR-113 d-i-d): 1 MiB. See the daemon-side note for rationale.
_MAX_FRAME_BYTES: int = 1 * 1024 * 1024


def _sock_write_frame(sock: socket.socket, obj: dict[str, Any]) -> None:
    """Encode ``obj`` with the T2 type-tagged encoder and send it."""
    payload = t2_json_dumps(obj)
    header = struct.pack(">I", len(payload))
    data = header + payload + b"\n"
    sock.sendall(data)


def _sock_read_frame(sock: socket.socket) -> dict[str, Any]:
    """Read one length-prefixed JSON frame from ``sock`` (blocking)."""
    # Read 4-byte header
    hdr = _recvexactly(sock, 4)
    length = struct.unpack(">I", hdr)[0]
    if length > _MAX_FRAME_BYTES:
        raise T2DaemonError(
            f"frame length {length} exceeds maximum {_MAX_FRAME_BYTES}"
        )
    # +1 for trailing \n
    data = _recvexactly(sock, length + 1)
    return t2_json_loads(data[:-1])


def _jittered_backoff_seconds(*, attempt: int, initial: float, cap: float) -> float:
    """Return the next reconnect backoff in seconds.

    Capped exponential (``initial * 2**attempt``, clipped to ``cap``) with
    ±25 % uniform jitter on top. RDR-114 §Decision Rationale calibrated
    the defaults so 10 attempts at the documented cap give a nominal
    sum of ~48 s (the geometric series
    ``0.25 + 0.5 + 1 + 2 + 4 + 8 + 8 + 8 + 8 + 8 = 47.75 s``), spanning
    ~36-60 s with ±25 % jitter. That window comfortably covers a
    systemd ``RestartSec=5s`` crash-restart cycle and matches launchd's
    ``ThrottleInterval=10s``.

    Module-level so the jitter spread test (RDR §Test Plan) can seed
    ``random`` directly and assert reproducible behaviour.
    """
    base = min(initial * (2 ** attempt), cap)
    return base * random.uniform(0.75, 1.25)


def _recvexactly(sock: socket.socket, n: int) -> bytes:
    """Receive exactly ``n`` bytes from ``sock``, blocking until available.

    nexus-wcs9 (RDR-114 Step 4): if the socket has a timeout set via
    ``sock.settimeout(...)`` and ``recv`` exceeds that budget, the
    stdlib raises ``socket.timeout`` (alias for ``TimeoutError`` since
    Python 3.10). Translate to the typed :class:`RpcTimeoutError` so
    callers can distinguish a hung daemon (this exception) from a
    gone daemon (``ConnectionRefusedError`` raised on ``connect``).
    """
    buf = bytearray()
    while len(buf) < n:
        try:
            chunk = sock.recv(n - len(buf))
        except socket.timeout as exc:
            raise RpcTimeoutError(
                f"daemon RPC timed out after {sock.gettimeout()} s "
                f"waiting for {n} bytes (received {len(buf)})"
            ) from exc
        if not chunk:
            raise ConnectionError("socket closed before expected bytes arrived")
        buf.extend(chunk)
    return bytes(buf)


# ---------------------------------------------------------------------------
# Connection pool
# ---------------------------------------------------------------------------

_POOL_DEFAULT_SIZE: int = 4


class _SocketConnection:
    """A single handshaked socket connection to the T2 daemon."""

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock

    def call(self, op: str, args: dict[str, Any]) -> Any:
        """Send ``{op, args}`` and return the decoded result.

        For domain-store ops the daemon wraps the return value in
        ``{result: <value>}``; for built-in ops (ping) it returns a
        flat dict.  This method always returns the full response dict for
        non-store ops, and the unwrapped result value for store ops.

        Raises:
            T2DaemonError: on remote error that is not a recognised builtin.
            <builtin exception>: when the daemon reports a stdlib exception.
            ConnectionError: if the socket breaks mid-call.
        """
        _sock_write_frame(self._sock, {"op": op, "args": args})
        resp = _sock_read_frame(self._sock)
        if "error" in resp:
            _reraise_remote_error(resp["error"])
        # Domain-store ops wrap their return value under "result"
        if "result" in resp:
            return resp["result"]
        # Built-in ops (ping) return flat dicts
        return resp

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass


class _ConnectionPool:
    """Thread-safe pool of pre-connected ``_SocketConnection`` instances.

    Connections are created lazily up to ``max_size``. On acquire the pool
    pops an idle connection (or creates a new one). On release the connection
    is returned to the pool unless it's broken (in which case it's discarded).

    Args:
        connect_fn: No-arg callable that returns a new ``_SocketConnection``.
        max_size: Maximum number of idle connections to keep.
    """

    def __init__(
        self,
        connect_fn: Any,
        max_size: int = _POOL_DEFAULT_SIZE,
    ) -> None:
        self._connect_fn = connect_fn
        self._max_size = max_size
        self._pool: list[_SocketConnection] = []
        self._lock = threading.Lock()

    @contextlib.contextmanager
    def acquire(self):  # type: ignore[return]
        """Context manager: yields a connected ``_SocketConnection``."""
        conn = self._checkout()
        ok = False
        try:
            yield conn
            ok = True
        finally:
            if ok:
                self._checkin(conn)
            else:
                conn.close()

    def _checkout(self) -> _SocketConnection:
        with self._lock:
            if self._pool:
                return self._pool.pop()
        return self._connect_fn()

    def _checkin(self, conn: _SocketConnection) -> None:
        with self._lock:
            if len(self._pool) < self._max_size:
                self._pool.append(conn)
                return
        conn.close()

    def close_all(self) -> None:
        """Close every idle connection in the pool."""
        with self._lock:
            conns, self._pool = self._pool, []
        for c in conns:
            c.close()


# ---------------------------------------------------------------------------
# Error re-raise
# ---------------------------------------------------------------------------


def _reraise_remote_error(error: Any) -> None:
    """Re-raise a remote error dict as a Python exception.

    If the remote ``type`` resolves to a built-in exception, raise it
    directly.  Otherwise raise ``T2DaemonError``.
    """
    if isinstance(error, str):
        # Bare string error (transport / protocol errors, not store errors)
        raise T2DaemonError(error)

    if not isinstance(error, dict):
        raise T2DaemonError(repr(error))

    type_name: str = error.get("type", "")
    message: str = error.get("message", str(error))
    remote_tb: str = error.get("traceback", "")

    # Try to resolve the last component as a builtin exception
    simple_name = type_name.rsplit(".", 1)[-1] if type_name else ""
    builtin_cls = getattr(builtins, simple_name, None)
    if (
        builtin_cls is not None
        and isinstance(builtin_cls, type)
        and issubclass(builtin_cls, Exception)
    ):
        raise builtin_cls(message)

    raise T2DaemonError(message, type_name=type_name, remote_traceback=remote_tb)


# ---------------------------------------------------------------------------
# Store proxy
# ---------------------------------------------------------------------------


class _StoreProxy:
    """Proxy for a single domain store.

    Each method on the proxy has the **same signature** as the corresponding
    method on the real store class (via ``__signature__``), so
    ``inspect.signature(client.memory.get)`` matches
    ``inspect.signature(MemoryStore.get)`` (minus ``self``).
    """

    def __init__(
        self,
        store_attr: str,
        store_class: type,
        pool: _ConnectionPool,
    ) -> None:
        self._store_attr = store_attr
        self._pool = pool
        self._methods = _build_store_methods(store_attr, store_class, pool)

    def __getattr__(self, name: str) -> Any:
        try:
            return self._methods[name]
        except KeyError:
            raise AttributeError(
                f"store {self._store_attr!r} has no RPC method {name!r}"
            ) from None


def _build_store_methods(
    store_attr: str,
    store_class: type,
    pool: _ConnectionPool,
) -> dict[str, Any]:
    """For each public method on ``store_class``, build a signature-faithful proxy."""
    methods: dict[str, Any] = {}
    for name, fn in inspect.getmembers(store_class, predicate=inspect.isfunction):
        if name.startswith("_"):
            continue
        orig_sig = inspect.signature(fn)
        # Drop the leading 'self' parameter
        params = list(orig_sig.parameters.values())[1:]
        proxy_sig = orig_sig.replace(parameters=params)

        op = f"{store_attr}.{name}"

        def _make_proxy(op_: str, sig_: inspect.Signature) -> Any:
            def proxy(*args: Any, **kwargs: Any) -> Any:
                # Bind positional + keyword args using the original signature
                bound = sig_.bind(*args, **kwargs)
                bound.apply_defaults()
                with pool.acquire() as conn:
                    return conn.call(op_, dict(bound.arguments))

            proxy.__name__ = name
            proxy.__qualname__ = f"_StoreProxy.{name}"
            proxy.__signature__ = sig_  # type: ignore[attr-defined]
            return proxy

        methods[name] = _make_proxy(op, proxy_sig)
    return methods


# ---------------------------------------------------------------------------
# Database-level proxy (rename_collection_cascade, etc.)
# ---------------------------------------------------------------------------


def _build_database_methods(
    pool: _ConnectionPool,
) -> dict[str, Any]:
    """Build proxies for top-level T2Database methods under 'database.*'."""
    from nexus.db.t2 import T2Database

    methods: dict[str, Any] = {}
    for name in ("rename_collection_cascade",):
        fn = getattr(T2Database, name, None)
        if fn is None:
            continue
        orig_sig = inspect.signature(fn)
        # Drop 'self'; also drop '_conn' (private test-seam, not for RPC callers)
        params = [
            p
            for p in list(orig_sig.parameters.values())[1:]
            if not p.name.startswith("_")
        ]
        proxy_sig = orig_sig.replace(parameters=params)
        op = f"database.{name}"

        def _make_db_proxy(op_: str, sig_: inspect.Signature) -> Any:
            def proxy(*args: Any, **kwargs: Any) -> Any:
                bound = sig_.bind(*args, **kwargs)
                bound.apply_defaults()
                with pool.acquire() as conn:
                    return conn.call(op_, dict(bound.arguments))

            proxy.__name__ = name
            proxy.__signature__ = sig_  # type: ignore[attr-defined]
            return proxy

        methods[name] = _make_db_proxy(op, proxy_sig)
    return methods


class _DatabaseProxy:
    """Proxy for top-level T2Database methods (rename_collection_cascade, etc.)."""

    def __init__(self, pool: _ConnectionPool) -> None:
        self._pool = pool
        self._methods = _build_database_methods(pool)

    def __getattr__(self, name: str) -> Any:
        try:
            return self._methods[name]
        except KeyError:
            raise AttributeError(
                f"T2Client.database has no RPC method {name!r}"
            ) from None


# ---------------------------------------------------------------------------
# Tuplespace proxy (RDR-112 nexus-6s8v)
# ---------------------------------------------------------------------------


class _TuplespaceProxy:
    """Proxy for the daemon's tuplespace.* RPC suite.

    The tuplespace API lives as free functions (``nexus.tuplespace.api``)
    operating on three injected resources (conn, index, registry); there
    is no single class whose methods can be introspected the way
    ``_StoreProxy`` introspects domain stores. This proxy hand-defines
    each method, mirroring the keyword-only public API.

    Returns are JSON-decoded results from the daemon-side
    ``TuplespaceService`` (see ``nexus.daemon.tuplespace_service``).
    """

    def __init__(self, pool: _ConnectionPool) -> None:
        self._pool = pool

    def _call(self, op: str, args: dict[str, Any]) -> Any:
        with self._pool.acquire() as conn:
            return conn.call(f"tuplespace.{op}", args)

    def out(
        self,
        *,
        subspace: str,
        content: str,
        dimensions: dict[str, Any],
        match_text: str | None = None,
        ttl_seconds: float | None = None,
    ) -> str:
        return self._call(
            "out",
            {
                "subspace": subspace,
                "content": content,
                "dimensions": dimensions,
                "match_text": match_text,
                "ttl_seconds": ttl_seconds,
            },
        )

    def read(
        self,
        *,
        subspace: str,
        query: str,
        where: dict[str, Any] | None = None,
        floor: float | None = None,
        n: int | None = None,
    ) -> list[dict[str, Any]]:
        return self._call(
            "read",
            {
                "subspace": subspace,
                "query": query,
                "where": where,
                "floor": floor,
                "n": n,
            },
        )

    def take(
        self,
        *,
        subspace: str,
        query: str,
        claimant: str,
        where: dict[str, Any] | None = None,
        floor: float | None = None,
        lease_seconds: float | None = None,
        block: bool = False,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any] | None:
        """Returns ``{"tuple": <dict>, "claim_id": <str>} | None``.

        The two-tuple ``(tuple_dict, claim_id)`` of ``api.take`` is wrapped
        into a single dict on the daemon side for JSON friendliness.
        """
        return self._call(
            "take",
            {
                "subspace": subspace,
                "query": query,
                "claimant": claimant,
                "where": where,
                "floor": floor,
                "lease_seconds": lease_seconds,
                "block": block,
                "timeout_seconds": timeout_seconds,
            },
        )

    def ack(self, *, claim_id: str, claimant: str) -> str:
        return self._call("ack", {"claim_id": claim_id, "claimant": claimant})

    def nack(self, *, claim_id: str, claimant: str) -> str:
        return self._call("nack", {"claim_id": claim_id, "claimant": claimant})

    def list_subspaces(self) -> list[str]:
        return self._call("list_subspaces", {})

    def subspace_schema(self, *, subspace: str) -> dict[str, Any]:
        return self._call("subspace_schema", {"subspace": subspace})

    def subspace_stats(self, *, subspace: str) -> dict[str, Any]:
        return self._call("subspace_stats", {"subspace": subspace})

    def list_active_claims(
        self, *, now: float | None = None
    ) -> list[dict[str, Any]]:
        """RDR-112 cockpit-boundary RPC (nexus-x65c).

        Returns active-claim rows shaped as
        ``{"subspace": str, "tuple_id": str, "claim_id": str,
        "claimant": str, "ttl_remaining_seconds": float | None}``.
        """
        return self._call("list_active_claims", {"now": now})

    def recent_events(self, *, limit: int = 25) -> list[dict[str, Any]]:
        """RDR-112 cockpit-boundary RPC (nexus-x65c).

        Returns events-table rows shaped as
        ``{"cursor": int, "subspace": str, "op": str, "tuple_id": str,
        "ts": float, "payload_summary": str | None,
        "category": str | None}``.
        """
        return self._call("recent_events", {"limit": int(limit)})


# ---------------------------------------------------------------------------
# T2Client
# ---------------------------------------------------------------------------


class T2Client:
    """Synchronous RPC client for the T2 daemon, mirroring ``T2Database``'s shape.

    Exactly one of ``uds_path`` or ``tcp_addr`` must be provided.

    Args:
        uds_path: Path to the Unix domain socket.
        tcp_addr: ``(host, port)`` for TCP loopback.
        pool_size: Number of persistent connections to maintain (default 4).

    Attributes:
        memory: Proxy for ``MemoryStore`` — same public method signatures.
        plans: Proxy for ``PlanLibrary``.
        chash_index: Proxy for ``ChashIndex``.
        taxonomy: Proxy for ``CatalogTaxonomy``.
        telemetry: Proxy for ``Telemetry``.
        document_aspects: Proxy for ``DocumentAspects``.
        aspect_queue: Proxy for ``AspectExtractionQueue``.
        database: Proxy for T2Database-level methods (``rename_collection_cascade``).

    Example::

        client = T2Client(uds_path=Path("/run/nexus/t2.sock"))
        row_id = client.memory.put(project="p", title="t.md", content="body")
        entry  = client.memory.get(project="p", title="t.md")

    Note:
        ``T2Client`` does **not** trigger migrations on connect.  The daemon
        owns schema migrations (RDR-112 §9, nexus-uqqy + nexus-w0et).

    .. # Constants
    .. # nexus-w0et (P1.4): add EXPECTED_SCHEMA_VERSION here.
    """

    # Constants
    # nexus-w0et (P1.4 migration runner): schema version handshake.
    # Must match DAEMON_SCHEMA_VERSION in t2_daemon.py. Integer comparison
    # avoids version-string ordering bugs. Mismatch raises T2DaemonError
    # with a directional upgrade instruction before any RPC is issued.

    def __init__(
        self,
        *,
        uds_path: Path | None = None,
        tcp_addr: tuple[str, int] | None = None,
        pool_size: int = _POOL_DEFAULT_SIZE,
        rpc_timeout_seconds: float = 5.0,
    ) -> None:
        if (uds_path is None) == (tcp_addr is None):
            raise ValueError(
                "T2Client requires exactly one of uds_path or tcp_addr"
            )

        self._uds_path = uds_path
        self._tcp_addr = tcp_addr
        self._pool_size = pool_size
        # nexus-wcs9 (RDR-114 Step 4): per-RPC socket timeout. Default 5.0 s
        # is generous for local-mode (spike measured p99=48 ms) and accommodates
        # cloud-mode Voyage RTT under load. Operators driving high-latency
        # embeddings can override at construction time. Applied via
        # sock.settimeout() in _connect_once; a recv exceeding the budget
        # surfaces as RpcTimeoutError, NOT ConnectionRefusedError, so the
        # reconnect wrapper (nexus-wfko) can distinguish hung-daemon from
        # gone-daemon by exception class.
        self._rpc_timeout_seconds = rpc_timeout_seconds

        # Lazy init: pool is created on the first attribute proxy access
        self._pool: _ConnectionPool | None = None
        self._pool_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Pool lifecycle
    # ------------------------------------------------------------------

    def _get_pool(self) -> _ConnectionPool:
        """Return (creating if needed) the connection pool."""
        if self._pool is not None:
            return self._pool
        with self._pool_lock:
            if self._pool is None:
                self._pool = _ConnectionPool(
                    self._connect_once, max_size=self._pool_size
                )
        return self._pool

    def _connect_once(self) -> _SocketConnection:
        """Open a new socket, perform the hello/hello_ack handshake."""
        if self._uds_path is not None:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.connect(str(self._uds_path))
        else:
            host, port = self._tcp_addr  # type: ignore[misc]
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.connect((host, port))

        # nexus-wcs9: apply the per-RPC timeout AFTER connect so a slow
        # connect (separate concern, governed by the OS) does not trip it.
        # The handshake read below uses this timeout; subsequent recv calls
        # inherit it because settimeout is socket-level state.
        sock.settimeout(self._rpc_timeout_seconds)

        _sock_write_frame(
            sock,
            {"op": "hello", "protocol_version": DAEMON_PROTOCOL_VERSION},
        )
        ack = _sock_read_frame(sock)
        if "error" in ack:
            sock.close()
            raise T2DaemonError(
                f"handshake failed: {ack['error']}"
            )
        if ack.get("op") != "hello_ack":
            sock.close()
            raise T2DaemonError(
                f"expected hello_ack, got {ack.get('op')!r}"
            )

        # --- Schema-version handshake (nexus-w0et RDR-112 P1.4) ---
        daemon_sv = ack.get("schema_version")
        if daemon_sv is not None:
            # Read from the module-level constant so tests can monkey-patch it
            # by assigning to nexus.daemon.t2_client.T2_SCHEMA_VERSION_EXPECTED.
            import sys as _sys  # noqa: PLC0415
            _mod = _sys.modules[__name__]
            expected_sv: int = _mod.T2_SCHEMA_VERSION_EXPECTED
            if expected_sv > daemon_sv:
                sock.close()
                raise T2DaemonError(
                    f"Daemon schema version is older than this client expects "
                    f"(daemon={daemon_sv}, client expects={expected_sv}). "
                    "Run: nx daemon t2 stop && nx daemon t2 start "
                    "(migration applies automatically)."
                )
            if expected_sv < daemon_sv:
                sock.close()
                raise T2DaemonError(
                    f"Daemon schema version is newer than this client "
                    f"(daemon={daemon_sv}, client expects={expected_sv}). "
                    "Upgrade conexus: uv pip install -U conexus."
                )

        # P1.5 nexus-x98k: record registry_digest from hello_ack.
        # No enforcement in this bead — the field is logged and stored for
        # future beads that may add warn-or-refuse on digest mismatch.
        registry_digest: str | None = ack.get("registry_digest")
        _log.debug(
            "t2_client_connected",
            transport="uds" if self._uds_path else "tcp",
            daemon_version=ack.get("daemon_version"),
            schema_version=daemon_sv,
            registry_digest=registry_digest,
        )
        return _SocketConnection(sock)

    def close(self) -> None:
        """Close all idle pooled connections and detach the pool.

        Detaching the pool under ``_pool_lock`` ensures a later call to
        ``_get_pool()`` builds a fresh pool rather than handing back the
        emptied one — otherwise an accidental call after ``close()`` would
        silently open new connections through the stale handle.
        """
        with self._pool_lock:
            pool, self._pool = self._pool, None
        if pool is not None:
            pool.close_all()

    def __enter__(self) -> "T2Client":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Bare-op invocation (admin RPCs and introspection verbs)
    # ------------------------------------------------------------------

    def call(self, op: str, args: dict[str, Any] | None = None) -> Any:
        """Invoke a bare-op RPC and return its ``result`` payload.

        Use for ops that are not exposed via a store proxy — admin ops
        (``subspace_add``), introspection verbs (``exec_raw``, ``schema``,
        ``peek``, ``stats``, ``export``), or any future bare-op handler.
        Raises ``T2DaemonError`` on remote error frames.

        Public alternative to reaching into ``_get_pool()`` directly so the
        CLI does not bind to internal pool internals.
        """
        with self._get_pool().acquire() as conn:
            return conn.call(op, args or {})

    # ------------------------------------------------------------------
    # Store proxies (attribute properties)
    # ------------------------------------------------------------------

    @property
    def memory(self) -> _StoreProxy:
        from nexus.db.t2.memory_store import MemoryStore
        return _StoreProxy("memory", MemoryStore, self._get_pool())

    @property
    def plans(self) -> _StoreProxy:
        from nexus.db.t2.plan_library import PlanLibrary
        return _StoreProxy("plans", PlanLibrary, self._get_pool())

    @property
    def chash_index(self) -> _StoreProxy:
        from nexus.db.t2.chash_index import ChashIndex
        return _StoreProxy("chash_index", ChashIndex, self._get_pool())

    @property
    def taxonomy(self) -> _StoreProxy:
        from nexus.db.t2.catalog_taxonomy import CatalogTaxonomy
        return _StoreProxy("taxonomy", CatalogTaxonomy, self._get_pool())

    @property
    def telemetry(self) -> _StoreProxy:
        from nexus.db.t2.telemetry import Telemetry
        return _StoreProxy("telemetry", Telemetry, self._get_pool())

    @property
    def document_aspects(self) -> _StoreProxy:
        from nexus.db.t2.document_aspects import DocumentAspects
        return _StoreProxy("document_aspects", DocumentAspects, self._get_pool())

    @property
    def aspect_queue(self) -> _StoreProxy:
        from nexus.db.t2.aspect_extraction_queue import AspectExtractionQueue
        return _StoreProxy("aspect_queue", AspectExtractionQueue, self._get_pool())

    @property
    def catalog(self) -> _StoreProxy:
        """RDR-112 P2.1 (nexus-7ejx): eighth domain store — catalog tables.

        Method names mirror ``CatalogDB`` so Phase 4 (catalog port) flips
        call sites by swapping the constructor injection only.
        """
        from nexus.db.t2.catalog_store import CatalogStore
        return _StoreProxy("catalog", CatalogStore, self._get_pool())

    @property
    def database(self) -> _DatabaseProxy:
        return _DatabaseProxy(self._get_pool())

    @property
    def tuplespace(self) -> "_TuplespaceProxy":
        """RDR-112 (nexus-6s8v): tuplespace RPC proxy.

        Mirrors the tuplespace free-function API
        (``nexus.tuplespace.api``) as keyword-only methods. Each method
        round-trips through the daemon's ``tuplespace.<op>`` RPC.
        """
        return _TuplespaceProxy(self._get_pool())

    # ------------------------------------------------------------------
    # Convenience ping
    # ------------------------------------------------------------------

    def ping(self) -> dict[str, Any]:
        """Send a ping to the daemon and return the pong payload dict."""
        with self._get_pool().acquire() as conn:
            # ping returns a flat {pong: True, ...} dict, not {result: ...}
            return conn.call("ping", {})

    # ------------------------------------------------------------------
    # EventStream (P1.3 nexus-m4gm)
    # ------------------------------------------------------------------

    def event_stream(
        self,
        subspace_prefix: str,
        since_cursor: int = 0,
        where: dict[str, Any] | None = None,
        *,
        reconnect: bool = True,
        max_reconnect_attempts: int = 10,
        initial_backoff_seconds: float = 0.25,
        max_backoff_seconds: float = 8.0,
    ) -> Iterator[dict[str, Any]]:
        """Yield event dicts from the daemon's EventStream, reconnecting across daemon restarts.

        RDR-114 Step 1 (nexus-wfko): the wrapper records the last-yielded
        event cursor and retries close-side failures (``RpcTimeoutError`` from
        a hung daemon; ``ConnectionRefusedError`` / ``ConnectionResetError`` /
        ``ConnectionError`` / ``OSError`` from a gone or restarting daemon)
        with capped exponential backoff + ±25 % uniform jitter. After
        ``max_reconnect_attempts`` consecutive failures, raises
        :class:`EventStreamUnavailable` with the last-seen cursor.

        **Delivery is at-least-once.** The server advances its emit cursor
        when writing each frame; the client cursor is only persisted after
        the caller processes the event (i.e., after this generator yields).
        A caller that processes an event and then crashes before persisting
        the cursor will see the event re-delivered on reconnect. Callers
        requiring exactly-once must dedup via the ``action_idempotency``
        table (RDR-111 / nexus-8wvs, see ``nexus.db.migrations``) keyed on
        ``tuple_id``.

        Args:
            subspace_prefix: Subspace glob prefix, e.g. ``"tuples/myspace"``.
                The daemon appends ``*`` when the prefix contains no wildcard.
            since_cursor: Resume cursor (rowid).  0 requests full backfill.
            where: Optional filter dict.  Currently supports
                ``{"category": "<str>"}`` for failure-category demux.
            reconnect: If True (default), retry close-side failures with
                jittered backoff. If False, legacy single-subscribe
                semantics: the generator exits cleanly on close (no
                retry, no raise) so existing callers can opt out.
            max_reconnect_attempts: Maximum consecutive reconnect attempts
                before raising ``EventStreamUnavailable``. Default 10.
                A successful event yield resets the attempt counter to 0.
            initial_backoff_seconds: Backoff for the first reconnect
                attempt (before jitter). Default 0.25.
            max_backoff_seconds: Cap on the per-attempt backoff (before
                jitter). Default 8.0. Total budget for 10 attempts at
                these defaults is the geometric sum
                ``0.25 + 0.5 + 1 + 2 + 4 + 8 + 8 + 8 + 8 + 8 = 47.75 s``
                nominal (range ~36-60 s with ±25 % jitter).

        Yields:
            Event dicts with keys: ``cursor``, ``subspace``, ``op``,
            ``tuple_id``, ``payload_summary``, ``category``, ``ts``.

        Raises:
            EventStreamUnavailable: when the reconnect budget is exhausted.
            T2DaemonError: if the daemon returns a non-recoverable error
                on the subscribe op.

        Example::

            for event in client.event_stream("tuples/myspace", since_cursor=42):
                process(event["tuple_id"])
        """
        if not reconnect:
            # Legacy single-subscribe semantics: socket-close exits the
            # generator cleanly (no retry, no raise).
            try:
                yield from self._event_stream_once(subspace_prefix, since_cursor, where)
            except (ConnectionError, OSError, RpcTimeoutError):
                return
            return

        last_cursor = since_cursor
        attempt = 0
        while True:
            try:
                for event in self._event_stream_once(subspace_prefix, last_cursor, where):
                    # Successful event delivery: advance the cursor and
                    # reset the retry counter so subsequent outages get a
                    # fresh budget.
                    cursor_val = event.get("cursor")
                    if isinstance(cursor_val, int) and cursor_val > last_cursor:
                        last_cursor = cursor_val
                    attempt = 0
                    yield event
                # The generator returned cleanly. The daemon either closed
                # gracefully (SIGTERM drain) or the server-side handler
                # exited. Either way the wrapper treats it as reconnectable.
                _log.debug(
                    "event_stream_clean_close_reconnect",
                    last_cursor=last_cursor,
                    attempt=attempt,
                )
            except (RpcTimeoutError, ConnectionError, OSError) as exc:
                # Close-side failure. Sleep + retry below.
                _log.debug(
                    "event_stream_close_side_reconnect",
                    error_type=type(exc).__name__,
                    last_cursor=last_cursor,
                    attempt=attempt,
                )

            if attempt >= max_reconnect_attempts:
                raise EventStreamUnavailable(
                    f"event_stream gave up after {attempt} reconnect attempts; "
                    f"last seen cursor: {last_cursor}",
                    last_cursor=last_cursor,
                )

            backoff = _jittered_backoff_seconds(
                attempt=attempt,
                initial=initial_backoff_seconds,
                cap=max_backoff_seconds,
            )
            time.sleep(backoff)
            attempt += 1

    def _event_stream_once(
        self,
        subspace_prefix: str,
        since_cursor: int,
        where: dict[str, Any] | None,
    ) -> Iterator[dict[str, Any]]:
        """Single-subscribe inner generator. Raises on close so the wrapper
        can reconnect; ``event_stream`` swallows the close when
        ``reconnect=False`` is passed.

        Opens a dedicated socket (not pooled) so the long-lived stream does
        not contend with short-lived RPC traffic for pool slots.
        """
        conn = self._connect_once()
        sock = conn._sock  # dedicated socket; not returned to pool
        try:
            _sock_write_frame(
                sock,
                {
                    "op": "event_stream.subscribe",
                    "args": {
                        "subspace_prefix": subspace_prefix,
                        "since_cursor": since_cursor,
                        "where": where or {},
                    },
                },
            )
            ack = _sock_read_frame(sock)
            if "error" in ack:
                _reraise_remote_error(ack["error"])
            if not ack.get("subscribed"):
                raise T2DaemonError(
                    f"expected subscribed ack, got: {ack!r}"
                )
            while True:
                frame = _sock_read_frame(sock)
                if "error" in frame:
                    _log.debug("event_stream_server_error", error=frame["error"])
                    return
                event = frame.get("event")
                if event is not None:
                    yield event
        finally:
            try:
                sock.close()
            except OSError:
                pass
