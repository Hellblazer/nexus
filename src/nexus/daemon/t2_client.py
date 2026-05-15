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
import socket
import struct
import threading
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


# ---------------------------------------------------------------------------
# Wire helpers (synchronous socket I/O)
# ---------------------------------------------------------------------------

_MAX_FRAME_BYTES: int = 16 * 1024 * 1024


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


def _recvexactly(sock: socket.socket, n: int) -> bytes:
    """Receive exactly ``n`` bytes from ``sock``, blocking until available."""
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
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
    ) -> None:
        if (uds_path is None) == (tcp_addr is None):
            raise ValueError(
                "T2Client requires exactly one of uds_path or tcp_addr"
            )

        self._uds_path = uds_path
        self._tcp_addr = tcp_addr
        self._pool_size = pool_size

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
    def database(self) -> _DatabaseProxy:
        return _DatabaseProxy(self._get_pool())

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
    ) -> Iterator[dict[str, Any]]:
        """Yield event dicts from the daemon's EventStream until the connection closes.

        Opens a **dedicated** socket connection (not from the pool) that is
        held for the generator's lifetime.  The connection is closed when the
        generator is exhausted or when the caller calls ``close()`` / uses
        ``break`` inside a ``for`` loop.

        Protocol:
          1. Connect + hello/hello_ack handshake.
          2. Send ``{"op": "event_stream.subscribe", "args": {...}}``.
          3. Read the ``{"subscribed": True, "cursor": <N>}`` ack.
          4. Yield each ``{"event": {...}}`` frame's inner event dict.
          5. Stop on connection close, error frame, or generator close.

        Args:
            subspace_prefix: Subspace glob prefix, e.g. ``"tuples/myspace"``.
                The daemon appends ``*`` when the prefix contains no wildcard.
            since_cursor: Resume cursor (rowid).  0 requests full backfill.
            where: Optional filter dict.  Currently supports
                ``{"category": "<str>"}`` for failure-category demux.

        Yields:
            Event dicts with keys: ``cursor``, ``subspace``, ``op``,
            ``tuple_id``, ``payload_summary``, ``category``, ``ts``.

        Raises:
            T2DaemonError: if the daemon returns an error on the subscribe op.
            ConnectionError: if the socket closes unexpectedly mid-stream.

        Example::

            for event in client.event_stream("tuples/myspace", since_cursor=42):
                print(event["op"], event["tuple_id"])
        """
        conn = self._connect_once()
        sock = conn._sock  # dedicated socket; not returned to pool
        try:
            # Send subscribe request
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
            # Read ack
            ack = _sock_read_frame(sock)
            if "error" in ack:
                _reraise_remote_error(ack["error"])
            if not ack.get("subscribed"):
                raise T2DaemonError(
                    f"expected subscribed ack, got: {ack!r}"
                )
            # Stream events
            while True:
                frame = _sock_read_frame(sock)
                if "error" in frame:
                    # Daemon shutting down or protocol error — stop cleanly
                    _log.debug("event_stream_server_error", error=frame["error"])
                    return
                event = frame.get("event")
                if event is not None:
                    yield event
        except (ConnectionError, OSError):
            return  # connection closed
        finally:
            try:
                sock.close()
            except OSError:
                pass
