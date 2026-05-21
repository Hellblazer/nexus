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

    def __init__(self, *, config_dir: Optional[Path] = None) -> None:
        self._config_dir = config_dir
        self._sock: Optional[socket.socket] = None
        self._request_id = 0
        self._lock = threading.Lock()
        # Stores enumerated by the daemon dispatch builder. Public
        # attribute names mirror T2Database for surface parity.
        # Seven stores at P3a; catalog joins at P5.
        for store_name in (
            "memory", "plans", "chash_index", "taxonomy", "telemetry",
            "document_aspects", "aspect_queue", "database",
        ):
            setattr(self, store_name, _StoreProxy(self, store_name))

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

    # ── RPC ─────────────────────────────────────────────────────────────

    def _ensure_sock(self) -> socket.socket:
        if self._sock is None:
            self._sock = _open_connection(config_dir=self._config_dir)
        return self._sock

    def call(self, op: str, *args: Any, **kwargs: Any) -> Any:
        """Invoke *op* with positional + keyword args; return the
        decoded result. Raises ``T2ClientError`` on daemon-side
        failure, ``T2DaemonNotReachableError`` on transport failure."""
        with self._lock:
            sock = self._ensure_sock()
            self._request_id += 1
            request_id = self._request_id
            frame = {
                "op": op,
                "args": list(args),
                "kwargs": dict(kwargs),
                "request_id": request_id,
            }
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
