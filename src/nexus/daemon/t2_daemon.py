# SPDX-License-Identifier: AGPL-3.0-or-later
"""T2 daemon — single-writer asyncio process owning the T2 SQLite stores.

RDR-112 P1.1 (nexus-61x6): transport scaffold + dual-bind UDS+TCP.
RDR-112 P1.2 (nexus-qy0u): domain-store RPC dispatcher.
RDR-112 P1.3 (nexus-m4gm): EventStream RPC — streaming subscription.

This module provides:
  - ``T2Daemon``: the daemon class, managing two concurrent asyncio servers
    (UDS-primary, loopback-TCP-fallback) behind a shared JSON-RPC handler.
  - ``read_frame`` / ``write_frame``: wire-frame helpers (4-byte big-endian
    length + JSON bytes + \\n trailing for human-debuggability).
  - ``DAEMON_PROTOCOL_VERSION``: the handshake version string clients must
    match.
  - ``t2_json_dumps`` / ``t2_json_loads``: type-tagged JSON serialization
    handling datetime, bytes, Path, and dataclasses.

Wire frame: ``<4-byte big-endian length><json bytes>\\n``
  - Length counts JSON bytes only (not the trailing ``\\n``).
  - Parser uses the length prefix; the ``\\n`` is for debuggability only.

Transport discipline (RDR-113):
  - UDS: ``bind → chmod(0o600) → listen`` ordering (A1-verified: the
    bind-to-chmod gap is closed because ``connect()`` against a bound-but-
    not-listening UDS returns ``ConnectionRefusedError``).
  - TCP: hard-coded ``127.0.0.1`` bind, port=0 for dynamic allocation.
  - Peer-cred check at accept for UDS: rejects UIDs != daemon UID.
  - No peer-cred check for TCP (loopback trust delegated to orchestrator).

Phase 1.1 scope:
  - Transport, dual-bind, hello/hello_ack handshake, ping/pong health-check.
  - Discovery file + stdout announce at startup.
  - Spawn-lock (fcntl.LOCK_EX | LOCK_NB) prevents double-bind.
  - Graceful SIGTERM drain + discovery-file unlink.

Phase 1.2 scope (nexus-qy0u):
  - Domain-store RPC dispatcher: ``{op: "<store>.<method>", args: {...}}``.
  - Dispatch table built at startup by introspecting T2Database attributes.
  - Each store method runs in a thread-pool executor (stores are sync).
  - Type-tagged JSON serialization: datetime (ISO-8601), bytes (base64),
    Path (str), dataclasses (dict of fields). Non-serialisable args rejected
    with a clear error.
  - Error surfacing: handler exceptions wrapped as
    ``{error: {type, message, traceback}}`` so the connection stays open.
  - ``database.rename_collection_cascade`` exposed as a top-level RPC.

Phase 1.3 scope (nexus-m4gm):
  - EventStream RPC: ``event_stream.subscribe`` op on a persistent connection.
  - Server-push mode: after subscription, the daemon streams event frames
    until the client closes or the daemon stops.
  - Backfill: ``rowid > since_cursor`` from the ``events`` table in tuples.db.
  - Live mode: ``PRAGMA data_version`` polling at 10 ms.
  - Failure-category demux: ``where: {category: <str>}`` filter supported.
  - Requires ``tuples_db_path`` arg at daemon construction.

Out of scope here (later beads):
  - Migration runner (P1.4 nexus-w0et)
  - Subspace admin (P1.5 nexus-x98k)
  - Introspection RPCs (P1.6 nexus-08i1)
"""
from __future__ import annotations

import asyncio
import base64
import dataclasses
import fcntl
import inspect
import json
import os
import signal
import socket
import struct
import sys
import traceback as _traceback_mod
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Any

import structlog

from nexus.daemon.peer import PeerCredentials, read_peer_credentials

_log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Wire-protocol version. Clients that present a different version on hello
#: are rejected with an error frame. Bump when the frame format or RPC
#: contract changes in a backward-incompatible way.
DAEMON_PROTOCOL_VERSION: str = "1.0"

#: Schema version for the T2 + tuples.db databases managed by this daemon.
#: Represents the highest migration version in the MIGRATIONS registry that
#: this daemon version applies. Clients compare their
#: ``T2_SCHEMA_VERSION_EXPECTED`` against this value in the hello_ack to
#: detect schema drift before issuing any RPC.
#: Integer (monotonically increasing) to avoid version-string comparison bugs.
#: nexus-w0et (RDR-112 P1.4): initial value = 1 (watcher_state era).
DAEMON_SCHEMA_VERSION: int = 1

#: Nexus package version embedded in discovery file and pong responses.
try:
    from importlib.metadata import version as _pkg_version

    _NEXUS_VERSION: str = _pkg_version("conexus")
except Exception:  # pragma: no cover — fallback for editable / pre-install envs
    _NEXUS_VERSION = "0.0.0+unknown"

#: Maximum accepted wire-frame payload size (bytes). Guards against a malicious
#: or buggy peer announcing a multi-gigabyte length header that would otherwise
#: cause ``readexactly`` to block until OOM.
_MAX_FRAME_BYTES: int = 16 * 1024 * 1024

#: Backlog for listen() on both UDS and TCP sockets.
_LISTEN_BACKLOG: int = 64


class ProtocolError(Exception):
    """Raised when a peer sends a malformed or oversized wire frame."""

#: Socket directory inside the config dir (mode 0o700, defense-in-depth).
_SOCKET_SUBDIR: str = "sockets"

#: Discovery file name template: t2_addr.<uid>
_DISCOVERY_FILE_TEMPLATE: str = "t2_addr.{uid}"

#: Spawn-lock file name (fcntl exclusive lock held for daemon lifetime).
_SPAWN_LOCK_FILE: str = "t2_spawn.lock"

# ---------------------------------------------------------------------------
# Type-tagged JSON serialization (P1.2 nexus-qy0u)
# ---------------------------------------------------------------------------

#: Sentinel type tag for datetime values in RPC frames.
_TAG_DATETIME = "__datetime__"
#: Sentinel type tag for bytes values in RPC frames.
_TAG_BYTES = "__bytes__"
#: Sentinel type tag for Path values in RPC frames.
_TAG_PATH = "__path__"
#: Sentinel type tag for dataclass instances in RPC frames.
_TAG_DATACLASS = "__dataclass__"

#: Store-attribute names on T2Database that are domain stores (used by
#: ``_build_dispatch_table`` to enumerate RPC targets).
_T2_STORE_ATTRS: tuple[str, ...] = (
    "memory",
    "plans",
    "chash_index",
    "taxonomy",
    "telemetry",
    "document_aspects",
    "aspect_queue",
)

#: Top-level T2Database methods exposed under the "database" pseudo-store.
_T2_DATABASE_METHODS: tuple[str, ...] = ("rename_collection_cascade",)

#: Method names denied at dispatch-table build for ALL stores. ``close`` is
#: filtered to prevent a client from tearing down the daemon's SQLite handles
#: via RPC; underscored names are already filtered separately.
_RPC_DENY_METHODS: frozenset[str] = frozenset({"close"})

#: Per-op denylist (qualified ``<store>.<method>``). Methods whose signature
#: accepts a dataclass instance cannot round-trip JSON until a typed-arg
#: reconstructor lands. Re-enable as the reconstructor adds coverage.
_RPC_DENY_OPS: frozenset[str] = frozenset({
    "document_aspects.upsert",
    "document_aspects.get",
    "document_aspects.get_by_doc_id",
})


def _t2_encode(obj: Any) -> Any:
    """Recursively encode ``obj`` into a JSON-safe structure.

    Handles:
    - ``datetime`` -> ``{"__datetime__": "<ISO-8601>"}``
    - ``bytes``    -> ``{"__bytes__": "<base64>"}``
    - ``Path``     -> ``{"__path__": "<str>"}``
    - dataclass    -> ``{"__dataclass__": "<cls>", "fields": {<field>: <value>}}``
    - ``tuple``    -> list (JSON round-trip; restored as list on client)
    - dict / list  -> recurse into values
    - primitives (str, int, float, bool, None) -> pass through

    Raises:
        TypeError: for any other type.
    """
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, datetime):
        return {_TAG_DATETIME: obj.isoformat()}
    if isinstance(obj, bytes):
        return {_TAG_BYTES: base64.b64encode(obj).decode("ascii")}
    if isinstance(obj, Path):
        return {_TAG_PATH: str(obj)}
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        fields = {
            f.name: _t2_encode(getattr(obj, f.name))
            for f in dataclasses.fields(obj)
        }
        return {_TAG_DATACLASS: type(obj).__qualname__, "fields": fields}
    if isinstance(obj, dict):
        return {k: _t2_encode(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_t2_encode(v) for v in obj]
    raise TypeError(
        f"value of type {type(obj).__qualname__!r} is not JSON-serialisable via t2_encode"
    )


def _t2_decode(obj: Any) -> Any:
    """Recursively decode a structure produced by ``_t2_encode``.

    Dataclasses are reconstructed only to the extent that the receiver
    knows the class; the daemon uses this for incoming args, the client
    for outgoing results. Unknown ``__dataclass__`` tags are passed
    through as plain dicts (the actual class is imported by the proxy
    before the call).
    """
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, list):
        return [_t2_decode(v) for v in obj]
    if isinstance(obj, dict):
        if _TAG_DATETIME in obj:
            return datetime.fromisoformat(obj[_TAG_DATETIME])
        if _TAG_BYTES in obj:
            return base64.b64decode(obj[_TAG_BYTES])
        if _TAG_PATH in obj:
            return Path(obj[_TAG_PATH])
        if _TAG_DATACLASS in obj:
            # Return as plain dict; caller reconstructs if needed.
            return {k: _t2_decode(v) for k, v in obj["fields"].items()}
        return {k: _t2_decode(v) for k, v in obj.items()}
    return obj


def t2_json_dumps(obj: Any) -> bytes:
    """Serialize ``obj`` to JSON bytes using the T2 type-tagged encoder.

    Suitable for the RPC wire frame when ``obj`` contains datetime, bytes,
    Path, or dataclass values.

    Raises:
        TypeError: if ``obj`` contains a value that cannot be serialised.
    """
    return json.dumps(_t2_encode(obj), separators=(",", ":")).encode()


def t2_json_loads(data: bytes | str) -> Any:
    """Deserialize JSON bytes produced by ``t2_json_dumps``."""
    return _t2_decode(json.loads(data))


# ---------------------------------------------------------------------------
# Wire-frame helpers
# ---------------------------------------------------------------------------


def write_frame(writer: asyncio.StreamWriter, obj: dict[str, Any]) -> None:
    """Encode ``obj`` as a length-prefixed JSON frame and buffer it.

    Frame layout: ``<4-byte big-endian uint32 length><json bytes>\\n``
    The trailing newline is for human-debuggability (``cat`` the socket);
    the length prefix is what the parser uses.

    Uses the T2 type-tagged encoder so that datetime, bytes, Path, and
    dataclass values are preserved across the wire (P1.2 nexus-qy0u).

    Args:
        writer: asyncio StreamWriter to buffer into.
        obj: mapping to send. All values must be t2_encode-compatible.
    """
    payload: bytes = t2_json_dumps(obj)
    header: bytes = struct.pack(">I", len(payload))
    writer.write(header + payload + b"\n")


async def read_frame(reader: asyncio.StreamReader) -> dict[str, Any]:
    """Read one length-prefixed JSON frame from ``reader``.

    Args:
        reader: asyncio StreamReader to read from.

    Returns:
        Decoded JSON mapping.

    Raises:
        asyncio.IncompleteReadError: connection closed mid-frame.
        json.JSONDecodeError: frame payload is not valid JSON.
    """
    length_bytes = await reader.readexactly(4)
    length = struct.unpack(">I", length_bytes)[0]
    if length > _MAX_FRAME_BYTES:
        raise ProtocolError(
            f"frame length {length} exceeds maximum {_MAX_FRAME_BYTES} bytes"
        )
    # +1 for the trailing \n
    data = await reader.readexactly(length + 1)
    return t2_json_loads(data[:-1])  # strip \n before parsing


# ---------------------------------------------------------------------------
# Dispatch table builder (P1.2 nexus-qy0u)
# ---------------------------------------------------------------------------


def _build_dispatch_table(t2db: Any) -> dict[str, Any]:
    """Build the ``{op: bound_callable}`` dispatch table from a T2Database.

    Introspects each domain-store attribute on ``t2db`` (``memory``,
    ``plans``, ``chash_index``, ``taxonomy``, ``telemetry``,
    ``document_aspects``, ``aspect_queue``) and registers every public
    method (non-dunder, non-underscore-prefixed) as
    ``"<store_attr>.<method_name>"``.

    Also registers ``"database.<method>"`` for the top-level T2Database
    methods listed in ``_T2_DATABASE_METHODS`` (currently
    ``rename_collection_cascade``).

    Args:
        t2db: A ``T2Database`` instance whose stores are already open.

    Returns:
        Mapping of RPC op string to bound callable.
    """
    table: dict[str, Any] = {}

    # Domain stores (use the module-level constant for single source of truth)
    for attr in _T2_STORE_ATTRS:
        store = getattr(t2db, attr, None)
        if store is None:
            _log.warning("t2_store_attr_missing", attr=attr)
            continue
        for name, method in inspect.getmembers(store, predicate=inspect.ismethod):
            if name.startswith("_") or name in _RPC_DENY_METHODS:
                continue  # skip private/dunder methods + denylist
            op = f"{attr}.{name}"
            if op in _RPC_DENY_OPS:
                continue  # per-op denylist (e.g. dataclass-arg methods until reconstructor lands)
            table[op] = method
            _log.debug("rpc_registered", op=op)

    # Top-level T2Database methods
    for name in _T2_DATABASE_METHODS:
        method = getattr(t2db, name, None)
        if method is None or not callable(method):
            _log.warning("t2_database_method_missing", method=name)
            continue
        op = f"database.{name}"
        table[op] = method
        _log.debug("rpc_registered", op=op)

    _log.info("rpc_table_built", count=len(table))
    return table


# ---------------------------------------------------------------------------
# Daemon
# ---------------------------------------------------------------------------


class T2Daemon:
    """Asyncio daemon owning T2 SQLite stores over dual-bind UDS+TCP.

    Instantiate, then ``await daemon.start()``. The daemon runs until
    ``await daemon.stop()`` is called (or SIGTERM / SIGINT is received
    when using ``run_until_signal()``).

    Attributes set after ``start()``:
        uds_path: Path to the bound UDS socket (mode 0o600).
        tcp_host: TCP bind address (always ``"127.0.0.1"``).
        tcp_port: Dynamically allocated TCP port.
        discovery_path: Path to the written discovery JSON file.
        start_time: ISO-8601 UTC timestamp of daemon startup.
    """

    def __init__(
        self,
        config_dir: Path,
        *,
        t2db: Any = None,
        tuples_db_path: Path | None = None,
    ) -> None:
        """Initialise the daemon.

        Args:
            config_dir: Directory for the discovery file, spawn-lock file,
                and socket subdir.
            t2db: Optional ``T2Database`` instance. When provided, the daemon
                builds a dispatch table at startup and serves domain-store
                RPCs (P1.2 nexus-qy0u). When ``None``, only the handshake
                and ping ops are available (useful for tests that only test
                transport).
            tuples_db_path: Path to tuples.db for EventStream subscriptions
                (P1.3 nexus-m4gm). When ``None``, ``event_stream.subscribe``
                returns an error.  Typically ``config_dir / "tuples.db"``.
        """
        self._config_dir = config_dir
        self._socket_dir: Path = config_dir / _SOCKET_SUBDIR

        uid = os.getuid()
        self._discovery_path: Path = config_dir / _DISCOVERY_FILE_TEMPLATE.format(uid=uid)
        self._spawn_lock_path: Path = config_dir / _SPAWN_LOCK_FILE

        # Populated by start()
        self._uds_path: Path | None = None
        self._tcp_host: str = "127.0.0.1"
        self._tcp_port: int | None = None
        self._uds_server: asyncio.Server | None = None
        self._tcp_server: asyncio.Server | None = None
        self._start_time: str | None = None
        self._spawn_lock_fh: IO | None = None
        self._active_handlers: set[asyncio.Task] = set()  # type: ignore[type-arg]
        self._stopping: bool = False

        # P1.2 nexus-qy0u: domain-store dispatch table.
        # Keys: "<store_attr>.<method_name>"; values: bound callables.
        self._t2db = t2db
        self._rpc_table: dict[str, Any] = {}
        if t2db is not None:
            self._rpc_table = _build_dispatch_table(t2db)

        # P1.3 nexus-m4gm: tuples.db path for EventStream subscriptions.
        self._tuples_db_path: Path | None = tuples_db_path

    # ------------------------------------------------------------------
    # Public properties (set after start())
    # ------------------------------------------------------------------

    @property
    def uds_path(self) -> Path:
        if self._uds_path is None:
            raise RuntimeError("daemon not started")
        return self._uds_path

    @property
    def tcp_host(self) -> str:
        return self._tcp_host

    @property
    def tcp_port(self) -> int:
        if self._tcp_port is None:
            raise RuntimeError("daemon not started")
        return self._tcp_port

    @property
    def discovery_path(self) -> Path:
        return self._discovery_path

    @property
    def start_time(self) -> str:
        if self._start_time is None:
            raise RuntimeError("daemon not started")
        return self._start_time

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Bind both transports, write discovery file, announce to stdout.

        Migration ownership (RDR-112 P1.4 / nexus-w0et): applies all pending
        T2 migrations to both ``memory.db`` and ``tuples.db`` BEFORE binding
        any socket. No client can connect to a partially-migrated daemon.

        Raises:
            RuntimeError: if the spawn lock is already held (another daemon
                is running for this UID / config_dir).
            MigrationError: if a data-precondition migration audit fails.
        """
        self._acquire_spawn_lock()
        self._ensure_dirs()
        self._start_time = datetime.now(timezone.utc).isoformat()

        # --- Migration runner: BEFORE sockets are bound (RDR-112 P1.4) ---
        from nexus.db.migrations import run_daemon_migrations  # noqa: PLC0415

        memory_db_path = self._config_dir / "memory.db"
        tuples_db_path = self._config_dir / "tuples.db"
        from_ver, to_ver = run_daemon_migrations(memory_db_path, tuples_db_path)
        _log.info(
            "daemon/t2/lifecycle",
            op="migration-applied",
            **{"from": from_ver, "to": to_ver},
        )

        uds_sock = self._bind_uds()
        tcp_sock = self._bind_tcp()

        # asyncio servers from pre-bound sockets
        handler = self._make_handler()
        self._uds_server = await asyncio.start_unix_server(handler, sock=uds_sock)
        self._tcp_server = await asyncio.start_server(handler, sock=tcp_sock)

        self._write_discovery()
        self._announce_stdout()

        _log.info(
            "t2_daemon_started",
            uds_path=str(self._uds_path),
            tcp_host=self._tcp_host,
            tcp_port=self._tcp_port,
            pid=os.getpid(),
        )

    async def stop(self) -> None:
        """Graceful shutdown: stop accepting, drain in-flight, unlink discovery."""
        self._stopping = True

        for srv in (self._uds_server, self._tcp_server):
            if srv is not None:
                srv.close()

        # Wait for servers to fully close
        for srv in (self._uds_server, self._tcp_server):
            if srv is not None:
                try:
                    await asyncio.wait_for(srv.wait_closed(), timeout=5.0)
                except asyncio.TimeoutError:
                    _log.warning("t2_daemon_stop_timeout", which=repr(srv))

        # Cancel and drain in-flight handlers.
        # Without cancellation, handlers blocked in read_frame() (60-second
        # timeout) would keep stop() waiting for up to 60 seconds after
        # servers stop accepting. Cancelling lets the gather complete promptly.
        if self._active_handlers:
            for task in list(self._active_handlers):
                task.cancel()
            await asyncio.gather(*self._active_handlers, return_exceptions=True)

        self._unlink_discovery()
        self._release_spawn_lock()

        _log.info("t2_daemon_stopped")

    async def run_until_signal(self) -> None:
        """Block until SIGTERM or SIGINT, then perform graceful shutdown.

        Registers asyncio signal handlers so the event loop drives shutdown
        rather than Python's synchronous signal module.
        """
        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGTERM, stop_event.set)
        loop.add_signal_handler(signal.SIGINT, stop_event.set)
        await stop_event.wait()
        await self.stop()

    # ------------------------------------------------------------------
    # Transport bind helpers (RDR-113 ordering: bind → chmod → listen)
    # ------------------------------------------------------------------

    def _bind_uds(self) -> socket.socket:
        """Create and bind the UDS socket with mode 0o600.

        Ordering per RDR-113 A1: bind() → chmod(0o600) → listen().
        The bind→chmod window is safe because connect() against a bound-but-
        not-listening UDS returns ConnectionRefusedError.

        Note on path length: macOS enforces a 104-byte limit on AF_UNIX
        socket paths (UNIX_PATH_MAX). The socket is placed in the socket
        subdir; callers that override config_dir (e.g. tests) must ensure
        the resulting path is under 104 bytes on macOS.
        """
        uds_path = self._socket_dir / "t2.sock"
        if uds_path.exists():
            uds_path.unlink()

        _uds_str = str(uds_path)
        if len(_uds_str.encode()) > 103:
            # macOS UNIX_PATH_MAX is 104 including the NUL terminator.
            # Fall back to a shorter /tmp-based path using a hash of the
            # socket dir so multiple test instances don't collide.
            import hashlib
            short = hashlib.shake_128(str(self._socket_dir).encode()).hexdigest(6)
            uds_path = Path(f"/tmp/nx-t2-{short}.sock")  # noqa: S108
            if uds_path.exists():
                uds_path.unlink()

        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.setblocking(False)
        sock.bind(str(uds_path))
        os.chmod(str(uds_path), 0o600)
        sock.listen(_LISTEN_BACKLOG)

        self._uds_path = uds_path
        return sock

    def _bind_tcp(self) -> socket.socket:
        """Bind TCP to loopback with dynamic port (port=0)."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setblocking(False)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        sock.listen(_LISTEN_BACKLOG)

        self._tcp_host = "127.0.0.1"
        self._tcp_port = sock.getsockname()[1]
        return sock

    # ------------------------------------------------------------------
    # Connection handler factory
    # ------------------------------------------------------------------

    def _make_handler(self):
        """Return the asyncio stream handler coroutine (closure over self)."""

        async def _handler(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            task = asyncio.current_task()
            if task is not None:
                self._active_handlers.add(task)
            try:
                await self._handle_connection(reader, writer)
            finally:
                if task is not None:
                    self._active_handlers.discard(task)
                try:
                    writer.close()
                    await writer.wait_closed()
                except Exception:
                    pass

        return _handler

    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Process a single client connection.

        Protocol:
          1. Check peer-cred on UDS (reject foreign UIDs per RDR-113).
          2. Read hello frame; validate protocol_version.
          3. Respond hello_ack.
          4. Serve RPCs until client closes or daemon is stopping.
        """
        # --- Peer-cred check (UDS only) ---
        transport = writer.transport
        raw_sock: socket.socket | None = transport.get_extra_info("socket")

        if raw_sock is not None and raw_sock.family == socket.AF_UNIX:
            try:
                creds: PeerCredentials = read_peer_credentials(raw_sock)
            except Exception as exc:
                _log.error("peer_cred_read_failed", exc=str(exc))
                write_frame(writer, {"error": "peer credential read failed"})
                await writer.drain()
                return

            daemon_uid = os.geteuid()
            if creds.uid != daemon_uid:
                _log.info(
                    "peer_uid_rejected",
                    peer_uid=creds.uid,
                    daemon_uid=daemon_uid,
                )
                write_frame(
                    writer,
                    {
                        "error": (
                            f"peer uid {creds.uid} rejected; "
                            f"daemon uid is {daemon_uid}"
                        )
                    },
                )
                await writer.drain()
                return

        # --- Handshake ---
        if self._stopping:
            write_frame(writer, {"error": "daemon is shutting down"})
            await writer.drain()
            return

        try:
            hello = await asyncio.wait_for(read_frame(reader), timeout=5.0)
        except asyncio.TimeoutError:
            write_frame(writer, {"error": "hello timeout"})
            await writer.drain()
            return
        except asyncio.IncompleteReadError:
            return

        if hello.get("op") != "hello":
            write_frame(writer, {"error": f"expected hello op, got {hello.get('op')!r}"})
            await writer.drain()
            return

        client_version = hello.get("protocol_version", "")
        if client_version != DAEMON_PROTOCOL_VERSION:
            write_frame(
                writer,
                {
                    "error": (
                        f"protocol version mismatch: client={client_version!r}, "
                        f"daemon={DAEMON_PROTOCOL_VERSION!r}"
                    )
                },
            )
            await writer.drain()
            return

        write_frame(
            writer,
            {
                "op": "hello_ack",
                "daemon_protocol_version": DAEMON_PROTOCOL_VERSION,
                "daemon_version": _NEXUS_VERSION,
                "schema_version": DAEMON_SCHEMA_VERSION,
            },
        )
        await writer.drain()

        # --- RPC loop ---
        while True:
            if self._stopping:
                write_frame(writer, {"error": "daemon is shutting down"})
                await writer.drain()
                return

            try:
                msg = await asyncio.wait_for(read_frame(reader), timeout=60.0)
            except asyncio.TimeoutError:
                continue  # keep-alive: just poll again
            except asyncio.IncompleteReadError:
                return  # client closed
            except Exception as exc:
                _log.warning("rpc_read_error", exc=str(exc))
                return

            # P1.3 nexus-m4gm: EventStream op hijacks the connection into
            # server-push mode.  Return immediately after the stream ends.
            if msg.get("op") == "event_stream.subscribe":
                await self._handle_event_stream(reader, writer, msg.get("args") or {})
                return

            response = await self._dispatch(msg)
            write_frame(writer, response)
            await writer.drain()

    async def _dispatch(self, msg: dict[str, Any]) -> dict[str, Any]:
        """Dispatch a single RPC message and return the response frame.

        Phase 1.1 ops:
          - ping -> pong with version + start_time

        Phase 1.2 ops (nexus-qy0u):
          - ``{op: "<store>.<method>", args: {...}}`` -> dispatched to the
            registered domain-store method. The method runs in a thread-pool
            executor (stores are synchronous). Return value is wrapped in
            ``{result: <t2_encoded>}``; exceptions in
            ``{error: {type, message, traceback}}``.

        Unknown ops return an error frame (not an exception) so the
        connection remains open.
        """
        op = msg.get("op")
        match op:
            case "ping":
                return {
                    "pong": True,
                    "version": _NEXUS_VERSION,
                    "daemon_protocol_version": DAEMON_PROTOCOL_VERSION,
                    "schema_version": DAEMON_SCHEMA_VERSION,
                    "start_time": self._start_time,
                    "pid": os.getpid(),
                }
            case str() if "." in op and op in self._rpc_table:
                return await self._dispatch_store_rpc(op, msg)
            case str() if "." in op and op not in self._rpc_table:
                return {"error": f"unknown RPC op: {op!r}"}
            case _:
                return {"error": f"unknown op: {op!r}"}

    async def _dispatch_store_rpc(
        self, op: str, msg: dict[str, Any]
    ) -> dict[str, Any]:
        """Run a domain-store method in the executor and return a response frame.

        Args:
            op: RPC op string, e.g. ``"memory.get"``.
            msg: Full RPC message (``args`` key holds kwargs dict).

        Returns:
            ``{result: <encoded>}`` on success;
            ``{error: {type, message, traceback}}`` on failure.
        """
        fn = self._rpc_table[op]
        raw_args: dict[str, Any] = msg.get("args", {})

        loop = asyncio.get_running_loop()
        try:
            result = await loop.run_in_executor(None, lambda: fn(**raw_args))
            return {"result": result}
        except Exception as exc:
            tb_text = _traceback_mod.format_exc()
            qname = f"{type(exc).__module__}.{type(exc).__qualname__}"
            _log.warning(
                "rpc_handler_error",
                op=op,
                exc_type=qname,
                exc=str(exc),
            )
            return {
                "error": {
                    "type": qname,
                    "message": str(exc),
                    "traceback": tb_text,
                }
            }

    async def _handle_event_stream(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        args: dict[str, Any],
    ) -> None:
        """Handle an ``event_stream.subscribe`` request in server-push mode.

        Delegates to ``nexus.daemon.event_stream.handle_event_stream`` with
        this daemon's tuples_db_path.  If ``tuples_db_path`` was not provided
        at construction, responds with an error frame and returns.

        Args:
            reader: asyncio StreamReader for the connection.
            writer: asyncio StreamWriter for the connection.
            args: Parsed ``args`` dict from the subscribe request.
        """
        if self._tuples_db_path is None:
            write_frame(
                writer,
                {"error": "event_stream not available: daemon has no tuples_db_path"},
            )
            await writer.drain()
            return

        from nexus.daemon.event_stream import handle_event_stream

        await handle_event_stream(
            reader=reader,
            writer=writer,
            tuples_db_path=self._tuples_db_path,
            args=args,
            stopping_fn=lambda: self._stopping,
        )

    # ------------------------------------------------------------------
    # Discovery file + stdout announce
    # ------------------------------------------------------------------

    def _discovery_payload(self) -> dict[str, Any]:
        return {
            "uds_path": str(self._uds_path),
            "tcp_host": self._tcp_host,
            "tcp_port": self._tcp_port,
            "daemon_version": _NEXUS_VERSION,
            "daemon_protocol_version": DAEMON_PROTOCOL_VERSION,
            "pid": os.getpid(),
            "start_time": self._start_time,
            # TODO(nexus-x98k P1.5): replace placeholder with real digest
            # once subspace schema registry is wired up.
            "subspace_schema_digest": "TODO",
        }

    def _write_discovery(self) -> None:
        payload = self._discovery_payload()
        # Atomic write: a reader polling the discovery file between open() and
        # the completed write() must never observe partial JSON.
        tmp = self._discovery_path.with_suffix(self._discovery_path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload))
        os.replace(str(tmp), str(self._discovery_path))

    def _unlink_discovery(self) -> None:
        try:
            self._discovery_path.unlink(missing_ok=True)
        except OSError as exc:
            _log.warning("discovery_unlink_failed", path=str(self._discovery_path), exc=str(exc))

    def _announce_stdout(self) -> None:
        """Emit a single JSON line on stdout for orchestrator capture.

        Containerised orchestrators capture the daemon's announce frame from
        stdout without needing filesystem access to the discovery file. The
        write goes via ``sys.stdout`` directly so it isn't confused with
        library-code logging output (CLAUDE.md prohibits ``print()`` in
        library code; this single line is the intentional orchestrator
        contract, not log output).
        """
        payload = self._discovery_payload()
        sys.stdout.write(json.dumps(payload) + "\n")
        sys.stdout.flush()

    # ------------------------------------------------------------------
    # Directory setup
    # ------------------------------------------------------------------

    def _ensure_dirs(self) -> None:
        """Create config_dir and socket subdir with appropriate modes."""
        self._config_dir.mkdir(parents=True, exist_ok=True)
        self._socket_dir.mkdir(parents=True, exist_ok=True)
        # Defense-in-depth: parent socket dir mode 0o700
        os.chmod(str(self._socket_dir), 0o700)

    # ------------------------------------------------------------------
    # Spawn lock
    # ------------------------------------------------------------------

    def _acquire_spawn_lock(self) -> None:
        """Acquire an exclusive non-blocking lock on the spawn-lock file.

        Raises:
            RuntimeError: if another daemon already holds the lock.
        """
        if sys.platform == "win32":
            # fcntl not available on Windows; use best-effort file existence
            # check. The T2 daemon is not designed to run natively on Windows
            # (TCP fallback serves Windows-VM clients), but allow startup
            # without the lock for completeness.
            _log.warning("spawn_lock_unavailable", platform="win32")
            return

        self._config_dir.mkdir(parents=True, exist_ok=True)
        fh = open(self._spawn_lock_path, "w")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            fh.close()
            raise RuntimeError(
                f"T2 daemon is already running (lock held at {self._spawn_lock_path}). "
                "Use `nx daemon t2 stop` to stop it, or `nx daemon t2 info` for details."
            ) from exc
        self._spawn_lock_fh = fh

    def _release_spawn_lock(self) -> None:
        if self._spawn_lock_fh is not None:
            try:
                fcntl.flock(self._spawn_lock_fh.fileno(), fcntl.LOCK_UN)
                self._spawn_lock_fh.close()
            except OSError:
                pass
            finally:
                self._spawn_lock_fh = None
