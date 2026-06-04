# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-120 P3a.B (nexus-iebjm): T2Client (substrate-only scaffold).

Thin synchronous RPC client mirroring the ``T2Database`` facade. Calls
flow:

  T2Client.<store>.<method>(*args, **kwargs)
    -> framed JSON op = "<store>.<method>"
    -> daemon dispatches via _build_dispatch_table
    -> response framed back

Connection precedence (RDR-120 C2):

  1. ``NX_T2_SOCK`` env-var (UDS path) when set + non-empty
  2. ``NX_T2_ADDR`` env-var (TCP host:port) when set + non-empty
  3. Discovery file ``~/.config/nexus/t2_addr.<uid>``: prefer UDS
     when both UDS and TCP are present; fall back to TCP otherwise.

Fail-loud on missing daemon: raises ``T2DaemonNotReachableError``
with a recovery hint that names ``nx daemon t2 start`` as the fix.

Substrate-only scope: NO event_stream subscription methods, NO
tuplespace API, NO subspace ops. Add those in a separate post-P3a
client tier if the moratorium ever lifts.

The client serializes one in-flight request per connection (frame
protocol assumes a strict request/response pair). Concurrency is the
caller's responsibility; open multiple T2Client instances for
parallel work.
"""
from __future__ import annotations

import json
import socket
import struct
import threading
from pathlib import Path
from typing import Any, Optional

import structlog

from nexus.daemon.t2_daemon import (
    _MAX_FRAME_BYTES,
    ProtocolError,
    t2_json_dumps,
    t2_json_loads,
)

_log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class T2DaemonNotReachableError(RuntimeError):
    """Raised when discovery resolves but the underlying socket
    connection fails (daemon crashed mid-transit, address stale)."""


class T2SchemaVersionMismatchError(RuntimeError):
    """Raised when the daemon's stored schema version differs from the
    version the client was built against.

    RDR-120 P3b (nexus-e9x4l): client carries
    :func:`nexus.db.migrations.expected_t2_schema_version` as the
    expected value and exchanges it with the daemon via
    ``database.hello`` on first connect. A mismatch means client and
    daemon are running different ``conexus`` wheels; the substrate
    refuses to operate rather than silently apply migrations across
    the boundary or read against a newer schema.

    Attributes:
        client_version: Schema version the client was built against.
        daemon_version: Schema version the daemon reports.
    """

    def __init__(self, *, client_version: str, daemon_version: str) -> None:
        super().__init__(
            f"T2 schema version mismatch: client built against "
            f"{client_version!r}, daemon reports {daemon_version!r}. "
            f"Re-install conexus so both sides match, then restart the "
            f"T2 daemon: `nx daemon t2 stop && nx daemon t2 start`."
        )
        self.client_version = client_version
        self.daemon_version = daemon_version


class T2ClientError(RuntimeError):
    """Raised when the daemon returns an error frame for an RPC call.

    Attributes:
        error_type: The daemon-side exception class name (e.g.
            ``"ValueError"``, ``"ProtocolError"``).
        message: The daemon-side error message.
        op: The RPC op the client invoked when the error fired.
    """

    def __init__(self, *, error_type: str, message: str, op: str) -> None:
        super().__init__(f"{op}: {error_type}: {message}")
        self.error_type = error_type
        self.message = message
        self.op = op


# ---------------------------------------------------------------------------
# Synchronous frame helpers
# ---------------------------------------------------------------------------


def _send_frame_sync(sock: socket.socket, obj: dict[str, Any]) -> None:
    payload: bytes = t2_json_dumps(obj)
    header: bytes = struct.pack(">I", len(payload))
    sock.sendall(header + payload + b"\n")


def _recv_exactly(sock: socket.socket, nbytes: int) -> bytes:
    out = bytearray()
    while len(out) < nbytes:
        chunk = sock.recv(nbytes - len(out))
        if not chunk:
            raise T2DaemonNotReachableError(
                "daemon closed the connection mid-frame"
            )
        out.extend(chunk)
    return bytes(out)


def _recv_frame_sync(sock: socket.socket) -> dict[str, Any]:
    length_bytes = _recv_exactly(sock, 4)
    length = struct.unpack(">I", length_bytes)[0]
    if length > _MAX_FRAME_BYTES:
        raise ProtocolError(
            f"frame length {length} exceeds maximum {_MAX_FRAME_BYTES} bytes"
        )
    data = _recv_exactly(sock, length + 1)  # +1 for trailing \n
    return t2_json_loads(data[:-1])


# ---------------------------------------------------------------------------
# Connection setup
# ---------------------------------------------------------------------------


def _connect_uds(uds_path: str) -> socket.socket:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(uds_path)
    except OSError as exc:
        sock.close()
        raise T2DaemonNotReachableError(
            f"UDS connect failed at {uds_path}: {exc}"
        ) from exc
    return sock


def _connect_tcp(host: str, port: int) -> socket.socket:
    try:
        sock = socket.create_connection((host, port), timeout=5.0)
    except OSError as exc:
        raise T2DaemonNotReachableError(
            f"TCP connect failed at {host}:{port}: {exc}"
        ) from exc
    sock.settimeout(None)
    return sock


def _open_connection(
    *,
    config_dir: Optional[Path] = None,
) -> socket.socket:
    """Resolve daemon discovery and open a single socket connection.

    Honours the RDR-120 C2 precedence (env-first, file-fallback,
    fail-loud-on-unreachable). When both UDS and TCP are available
    from the discovery file, UDS is preferred.
    """
    from nexus.daemon.discovery import (
        DaemonNotRunningError,
        discovery_resolve,
    )
    try:
        payload = discovery_resolve("t2", config_dir=config_dir)
    except DaemonNotRunningError as exc:
        raise T2DaemonNotReachableError(str(exc)) from exc

    uds_path = payload.get("uds_path") or ""
    tcp_host = payload.get("tcp_host") or ""
    tcp_port = payload.get("tcp_port")

    if uds_path:
        return _connect_uds(uds_path)
    if tcp_host and isinstance(tcp_port, int):
        return _connect_tcp(tcp_host, tcp_port)
    raise T2DaemonNotReachableError(
        f"Discovery payload missing both uds_path and tcp_host/tcp_port: "
        f"{payload!r}. Re-start with: `nx daemon t2 stop && nx daemon t2 start`."
    )


# ---------------------------------------------------------------------------
# T2Client
# ---------------------------------------------------------------------------


class T2Client:
    """Synchronous T2 RPC client.

    Usage::

        client = T2Client()
        client.memory.put("rdr_120_test", "hello", title="t")
        rows = client.memory.search("hello", limit=5)
        client.close()

    Implements ``__enter__`` / ``__exit__`` for ``with``-block use.
    Methods are auto-discovered via attribute access on store proxies
    (``client.memory.put`` builds the op ``"memory.put"`` and ships
    the request to the daemon). The daemon's dispatch table is the
    authority on what ops are valid; an unknown method surfaces as
    a ``T2ClientError`` with ``error_type="ProtocolError"``.

    Args:
        config_dir: Optional override for the discovery file location
            (defaults to ``nexus.config.nexus_config_dir()``).
    """

    def __init__(
        self,
        *,
        config_dir: Optional[Path] = None,
        skip_handshake: bool = False,
    ) -> None:
        self._config_dir = config_dir
        self._sock: Optional[socket.socket] = None
        self._request_id = 0
        self._lock = threading.Lock()
        # RDR-120 P3b: handshake runs lazily on first ``call`` so
        # construction stays cheap. ``skip_handshake`` exists for tests
        # that exercise the connection plumbing without a real daemon.
        self._skip_handshake = skip_handshake
        self._handshake_done = False
        # Stores enumerated by the daemon dispatch builder. Public
        # attribute names mirror T2Database for surface parity.
        # Eight stores as of RDR-120 P5.A.1 (nexus-9zmpl):
        # seven shared-nexus.db stores + catalog (its own .catalog.db).
        for store_name in (
            "memory", "plans", "chash_index", "taxonomy", "telemetry",
            "document_aspects", "aspect_queue", "catalog", "database",
        ):
            setattr(self, store_name, _StoreProxy(self, store_name))
        # RDR-146 P1 (nexus-5p2ci.20): write-only proxy to the daemon-
        # hosted rich Catalog. Distinct from the ``catalog`` read proxy
        # above (low-level CatalogStore reads); ``catalog_write`` exposes
        # exactly the 16 mutating ops with the Tumbler<->str shim.
        self.catalog_write = _CatalogWriterProxy(self)

    # ── context manager ─────────────────────────────────────────────────

    def __enter__(self) -> "T2Client":
        self._ensure_sock()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            if self._sock is not None:
                try:
                    self._sock.close()
                except OSError:
                    pass
                self._sock = None
            self._handshake_done = False

    # ── Facade-parity passthroughs (RDR-128 P3) ─────────────────────────
    # T2Database exposes ``expire`` / ``rename_collection_cascade`` at the
    # top level (they span multiple stores), whereas the daemon dispatches
    # them under the ``database`` pseudo-store. These thin forwards give
    # T2Client the same top-level surface as T2Database, so a
    # ``t2_index_write(write_fn)`` body can call ``db.expire()`` /
    # ``db.rename_collection_cascade(old, new)`` uniformly regardless of
    # whether the daemon is reachable (routed client) or not (direct DB).

    def expire(self, *args: Any, **kwargs: Any) -> Any:
        return self.database.expire(*args, **kwargs)

    def rename_collection_cascade(self, *args: Any, **kwargs: Any) -> Any:
        return self.database.rename_collection_cascade(*args, **kwargs)

    def complete_aspect(self, *args: Any, **kwargs: Any) -> Any:
        # nexus-zir76: aspect-worker persist (document_aspects.upsert +
        # aspect_queue.mark_done) folded into one daemon-routable call so
        # the worker stays off the direct memory.db write path.
        return self.database.complete_aspect(*args, **kwargs)

    def put(self, *args: Any, **kwargs: Any) -> Any:
        # T2Database.put is a thin facade over memory.put; mirror it so a
        # write_fn (or a helper handed the writer, e.g. T1Database.promote)
        # can call db.put(...) on either a routed client or a direct DB.
        return self.memory.put(*args, **kwargs)

    # ── RPC ─────────────────────────────────────────────────────────────

    def _ensure_sock(self) -> socket.socket:
        if self._sock is None:
            self._sock = _open_connection(config_dir=self._config_dir)
        return self._sock

    def _do_handshake_locked(self, sock: socket.socket) -> None:
        """Send ``database.hello`` and verify version agreement.

        Caller holds ``self._lock`` and must pass the established
        socket. Raises ``T2SchemaVersionMismatchError`` on mismatch;
        propagates transport / protocol errors unchanged so the caller
        tears down the socket the same way as any other RPC failure.
        """
        from nexus.db.migrations import expected_t2_schema_version

        client_version = expected_t2_schema_version()
        self._request_id += 1
        request_id = self._request_id
        frame = {
            "op": "database.hello",
            "args": [],
            "kwargs": {"client_schema_version": client_version},
            "request_id": request_id,
        }
        _send_frame_sync(sock, frame)
        response = _recv_frame_sync(sock)
        if response.get("request_id") != request_id:
            raise ProtocolError(
                f"handshake response request_id mismatch: "
                f"sent {request_id}, got {response.get('request_id')!r}"
            )
        if not response.get("ok", False):
            err = response.get("error") or {}
            raise T2ClientError(
                op="database.hello",
                error_type=str(err.get("type", "Unknown")),
                message=str(err.get("message", "<no message>")),
            )
        result = response.get("result") or {}
        daemon_version = str(result.get("daemon_schema_version") or "")
        # ``"0.0.0"`` / empty daemon side means the daemon never completed
        # ``apply_pending`` (deferred steps, e.g. catalog absent). The
        # schema is still functionally OK for daemon ops — log a warning
        # and proceed. A genuine non-trivial mismatch is the fail-loud
        # case (client and daemon are running different wheels).
        if (
            daemon_version
            and daemon_version != "0.0.0"
            and daemon_version != client_version
        ):
            raise T2SchemaVersionMismatchError(
                client_version=client_version,
                daemon_version=daemon_version,
            )
        if daemon_version in ("", "0.0.0"):
            _log.warning(
                "t2_client_handshake_daemon_version_unset",
                client_version=client_version,
                daemon_version=daemon_version,
            )
        self._handshake_done = True

    def call(
        self, op: str, *args: Any, _priority: str | None = None, **kwargs: Any
    ) -> Any:
        """Invoke *op* with positional + keyword args; return the
        decoded result. Raises ``T2ClientError`` on daemon-side
        failure, ``T2DaemonNotReachableError`` on transport failure,
        ``T2SchemaVersionMismatchError`` if the lazy handshake detects
        a client/daemon schema-version skew on first connect.

        *_priority* (RDR-146 P2) is a keyword-only out-of-band frame field
        (``"interactive"`` | ``"batch"``), NOT an op argument: when set it is
        added to the frame as ``priority`` so the daemon's catalog-write
        fairness window keys off it. ``None`` omits the field entirely
        (the daemon defaults absent -> batch), keeping batch frames byte-
        identical to the pre-P2 wire shape. The leading underscore keeps it
        from colliding with an op's ``**fields`` / ``**meta`` keyword args.
        """
        with self._lock:
            sock = self._ensure_sock()
            if not self._skip_handshake and not self._handshake_done:
                try:
                    self._do_handshake_locked(sock)
                except (OSError, T2DaemonNotReachableError):
                    try:
                        sock.close()
                    except OSError:
                        pass
                    self._sock = None
                    self._handshake_done = False
                    raise
                except T2SchemaVersionMismatchError:
                    # Tear down so a retry after reinstall reconnects.
                    try:
                        sock.close()
                    except OSError:
                        pass
                    self._sock = None
                    self._handshake_done = False
                    raise
            self._request_id += 1
            request_id = self._request_id
            frame = {
                "op": op,
                "args": list(args),
                "kwargs": dict(kwargs),
                "request_id": request_id,
            }
            if _priority is not None:
                frame["priority"] = _priority
            try:
                _send_frame_sync(sock, frame)
                response = _recv_frame_sync(sock)
            except (OSError, T2DaemonNotReachableError) as exc:
                # Tear down the connection so the next call reconnects.
                try:
                    sock.close()
                except OSError:
                    pass
                self._sock = None
                self._handshake_done = False
                if isinstance(exc, T2DaemonNotReachableError):
                    raise
                raise T2DaemonNotReachableError(
                    f"transport error calling {op}: {exc}"
                ) from exc

            if response.get("request_id") != request_id:
                raise ProtocolError(
                    f"response request_id mismatch: "
                    f"sent {request_id}, got {response.get('request_id')!r}"
                )
            if not response.get("ok", False):
                err = response.get("error") or {}
                raise T2ClientError(
                    op=op,
                    error_type=str(err.get("type", "Unknown")),
                    message=str(err.get("message", "<no message>")),
                )
            return response.get("result")


# ---------------------------------------------------------------------------
# Store proxy (attribute-driven RPC dispatch)
# ---------------------------------------------------------------------------


class _StoreProxy:
    """Attribute proxy: ``client.<store>.<method>(*args, **kwargs)``
    builds the op string ``"<store>.<method>"`` and dispatches via
    :meth:`T2Client.call`. Method existence is enforced daemon-side;
    an unknown method round-trips and surfaces a ``T2ClientError``.
    """

    __slots__ = ("_client", "_store_name")

    def __init__(self, client: T2Client, store_name: str) -> None:
        self._client = client
        self._store_name = store_name

    def __getattr__(self, method_name: str) -> Any:
        if method_name.startswith("_"):
            raise AttributeError(method_name)
        store = self._store_name
        client = self._client

        def _call(*args: Any, **kwargs: Any) -> Any:
            return client.call(f"{store}.{method_name}", *args, **kwargs)

        _call.__name__ = method_name
        _call.__qualname__ = f"T2Client.{store}.{method_name}"
        return _call


class _CatalogWriterProxy:
    """RDR-146 P1 (nexus-5p2ci.20): write-only proxy to the daemon-hosted
    rich Catalog.

    Exposes exactly the 16 whitelisted mutating ops (see
    :data:`nexus.daemon.catalog_write_shim.CATALOG_WRITE_OPS`). Tumbler
    arguments are serialised to ``str`` on the wire and the three
    Tumbler-returning ops (``register_owner`` / ``ensure_owner_for_repo``
    / ``register``) are parsed back to Tumbler on receipt. Any name
    outside the whitelist raises ``AttributeError`` locally so a typo or
    an attempt to reach a read method never round-trips.
    """

    __slots__ = ("_client",)

    def __init__(self, client: "T2Client") -> None:
        self._client = client

    def __getattr__(self, method_name: str) -> Any:
        from nexus.daemon.catalog_write_shim import (
            CATALOG_WRITE_OPS,
            CATALOG_WRITE_PREFIX,
            decode_return,
            encode_tumbler_args,
        )

        if method_name not in CATALOG_WRITE_OPS:
            raise AttributeError(
                f"{method_name!r} is not a catalog write op; the 16-op "
                f"whitelist is {CATALOG_WRITE_OPS!r}"
            )
        client = self._client

        def _call(*args: Any, _priority: str | None = None, **kwargs: Any) -> Any:
            enc_args, enc_kwargs = encode_tumbler_args(args, kwargs)
            result = client.call(
                f"{CATALOG_WRITE_PREFIX}{method_name}", *enc_args,
                _priority=_priority, **enc_kwargs,
            )
            return decode_return(method_name, result)

        _call.__name__ = method_name
        _call.__qualname__ = f"T2Client.catalog_write.{method_name}"
        return _call


# ---------------------------------------------------------------------------
# Factory mirroring make_t3_client
# ---------------------------------------------------------------------------


def make_t2_client(*, config_dir: Optional[Path] = None) -> T2Client:
    """Return a :class:`T2Client` connected to the running T2 daemon.

    The connection is established lazily on first call so construction
    is cheap. Raises ``T2DaemonNotReachableError`` when the discovery
    resolves to an unreachable target; the message names ``nx daemon
    t2 start`` as the recovery action.
    """
    return T2Client(config_dir=config_dir)
