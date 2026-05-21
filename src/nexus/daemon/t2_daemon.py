# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-120 P3a.A (nexus-7aayk): T2 daemon (substrate-only scaffold).

The T2 daemon is a long-lived process that owns the eight domain-store
SQLite handles (one connection per store, all under the daemon's
process) and serves RPC over a dual-binding socket:

  - UDS  ~/.config/nexus/sockets/t2.sock   (preferred)
  - TCP  127.0.0.1:<auto>                   (loopback only; announced
                                             via discovery file)

Discovery file at ``~/.config/nexus/t2_addr.<uid>`` carries both the
UDS path and the TCP host:port. Env overrides per RDR-120 C2:
``NX_T2_SOCK`` (UDS) and ``NX_T2_ADDR`` (TCP host:port). The client
honours env-first / file-fallback / fail-loud-on-unreachable-env per
the C2 contract.

Transport: length-prefixed JSON frames via :func:`write_frame` /
:func:`read_frame`. PICKLE FORBIDDEN per RDR-120 P0 S2 lock: UDS on
shared-user hosts plus pickle = arbitrary code execution primitive.
The type-tagged encoder preserves datetime / bytes / Path / dataclass
values across the wire; the dataclass allowlist
(:data:`_ALLOWED_DATACLASS_TYPES`) prevents same-UID peer probing for
tag-based bypasses.

Substrate-only scope (RDR-120 §Out of scope moratorium until P6+30
days):

  - NO peer-credentials module (host-trust)
  - NO event_stream RPC
  - NO subspace registry
  - NO tuplespace service
  - NO cockpit binding watcher
  - NO admin-ops UDS gate (no admin ops exist in P3a; future phases
    may re-introduce with a fresh design)

The dispatch table is built from :data:`_T2_STORE_ATTRS` and
:data:`_T2_DATABASE_METHODS` only. Every method named on those tables
is proxied; underscored names and the per-op denylist
(:data:`_RPC_DENY_OPS`) are filtered.

Daemon owns migration on its own path: on startup, after the SQLite
handles are open, ``T2Database.__init__`` runs ``apply_pending`` as
usual (RDR-120 P3a constraint; direct-open ``T2Database``
construction continues to call ``apply_pending`` per A6's P3
transition mitigation; the daemon's call is the same code path). The
P3b bead later removes the redundant ``apply_pending`` from
``T2Database.__init__``.
"""
from __future__ import annotations

import asyncio
import base64
import dataclasses
import errno
import json
import os
import signal
import socket
import struct
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

_log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

#: Maximum wire-frame payload in bytes. Cap is defence against a buggy
#: or hostile peer announcing a multi-gigabyte length header that would
#: otherwise force the daemon to allocate the whole frame before parsing.
_MAX_FRAME_BYTES: int = 4 * 1024 * 1024  # 4 MiB

#: TCP host: loopback only (RDR-120 §Approach: no cross-host federation,
#: no non-loopback TCP).
_T2_HOST: str = "127.0.0.1"

#: socket.listen() backlog for both UDS and TCP.
_LISTEN_BACKLOG: int = 64

#: Discovery file template (uid-suffixed so multi-user hosts don't collide).
_DISCOVERY_FILE_TEMPLATE: str = "t2_addr.{uid}"

#: Spawn-lock file name (fcntl exclusive lock held for daemon lifetime).
_SPAWN_LOCK_FILE: str = "t2_spawn.lock"

#: Subdirectory under config_dir holding the UDS path. Mode 0o700 at
#: create time so other UIDs cannot stat() into it.
_SOCKET_SUBDIR: str = "sockets"

#: UDS filename within _SOCKET_SUBDIR.
_UDS_FILENAME: str = "t2.sock"

#: Discovery payload format version. Bump on shape change.
_DISCOVERY_FORMAT_VERSION: int = 1

#: How long to wait for the SQLite handles to come up before declaring
#: a failed start.
_READY_TIMEOUT: float = 30.0

#: Graceful-stop window before escalating to forced socket teardown.
_GRACEFUL_STOP_TIMEOUT: float = 5.0


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ProtocolError(Exception):
    """Raised when a peer sends a malformed or oversized wire frame."""


class T2DaemonError(RuntimeError):
    """Raised on daemon lifecycle errors (bind failed, address in use,
    discovery write failed, etc.)."""


# ---------------------------------------------------------------------------
# Type-tagged JSON serialization (verbatim from archive frame protocol)
# ---------------------------------------------------------------------------

_TAG_DATETIME = "__datetime__"
_TAG_BYTES = "__bytes__"
_TAG_PATH = "__path__"
_TAG_DATACLASS = "__dataclass__"

#: Allowlist of dataclass qualnames permitted across the RPC wire.
#: Strict allowlist on decode prevents a same-UID client from feeding
#: arbitrary tagged dicts to bypass downstream type checks.
#:
#: Maintenance rule: when a new dataclass starts crossing the RPC
#: boundary (either as a return value or as an arg), add its bare
#: ``__qualname__`` here. Encode is permissive
#: (``dataclasses.is_dataclass(...)`` tags on outbound); decode is
#: strict (unknown tag raises ``ValueError``).
_ALLOWED_DATACLASS_TYPES: frozenset[str] = frozenset({
    "QueueRow",
    "AspectRecord",
    "Tumbler",
    "OwnerRecord",
    "DocumentRecord",
    "LinkRecord",
    "CatalogEntry",
    "CatalogLink",
    "ManifestRow",
    "OrphanPlan",
    "DedupePlan",
})


def _t2_encode(obj: Any) -> Any:
    """Recursively encode *obj* into a JSON-safe structure with type tags."""
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
    """Recursively decode a structure produced by :func:`_t2_encode`."""
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
            qualname = obj[_TAG_DATACLASS]
            if qualname not in _ALLOWED_DATACLASS_TYPES:
                raise ValueError(
                    f"unknown __dataclass__ tag {qualname!r}; "
                    "not in t2 wire allowlist"
                )
            return {k: _t2_decode(v) for k, v in obj["fields"].items()}
        return {k: _t2_decode(v) for k, v in obj.items()}
    return obj


def t2_json_dumps(obj: Any) -> bytes:
    """Serialize *obj* to JSON bytes using the T2 type-tagged encoder."""
    return json.dumps(_t2_encode(obj), separators=(",", ":")).encode()


def t2_json_loads(data: bytes | str) -> Any:
    """Deserialize JSON bytes produced by :func:`t2_json_dumps`."""
    return _t2_decode(json.loads(data))


# ---------------------------------------------------------------------------
# Wire-frame helpers (verbatim from archive)
# ---------------------------------------------------------------------------


def write_frame(writer: asyncio.StreamWriter, obj: dict[str, Any]) -> None:
    """Encode *obj* as a length-prefixed JSON frame and buffer it.

    Frame layout: ``<4-byte big-endian uint32 length><json bytes>\\n``.
    The trailing newline is for human-debuggability (``cat`` the
    socket); the length prefix is what the parser uses.
    """
    payload: bytes = t2_json_dumps(obj)
    header: bytes = struct.pack(">I", len(payload))
    writer.write(header + payload + b"\n")


async def read_frame(reader: asyncio.StreamReader) -> dict[str, Any]:
    """Read one length-prefixed JSON frame from *reader*."""
    length_bytes = await reader.readexactly(4)
    length = struct.unpack(">I", length_bytes)[0]
    if length > _MAX_FRAME_BYTES:
        raise ProtocolError(
            f"frame length {length} exceeds maximum {_MAX_FRAME_BYTES} bytes"
        )
    data = await reader.readexactly(length + 1)  # +1 for the trailing \n
    return t2_json_loads(data[:-1])


# ---------------------------------------------------------------------------
# Dispatch table
# ---------------------------------------------------------------------------

#: Store attribute names on T2Database that participate in the dispatch
#: table. Order matters: client expectations follow the documented
#: store list in src/nexus/db/t2/__init__.py.
#:
#: Seven stores at P3a. The ``catalog`` eighth store is added at P5
#: (catalog collapse into T2); the entry is intentionally absent here
#: so the dispatch table reflects what the daemon can actually serve
#: today.
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

#: Methods filtered from every store. ``close`` is denied to prevent a
#: client from tearing down the daemon's SQLite handles via RPC;
#: underscored names are already filtered separately.
_RPC_DENY_METHODS: frozenset[str] = frozenset({"close"})

#: Per-op denylist (qualified ``<store>.<method>``). Methods whose
#: signature accepts a typed dataclass instance can't round-trip JSON
#: until a typed-arg reconstructor lands. (The catalog @contextmanager
#: methods are not on this list yet because the catalog isn't in
#: :data:`_T2_STORE_ATTRS` until P5; re-add when the eighth store
#: lands.)
_RPC_DENY_OPS: frozenset[str] = frozenset({
    "document_aspects.upsert",
    "document_aspects.get",
    "document_aspects.get_by_doc_id",
})


def _build_dispatch_table(t2db: Any) -> dict[str, Any]:
    """Build the ``{op: bound_callable}`` dispatch table from *t2db*.

    Walks every attribute name in :data:`_T2_STORE_ATTRS`, enumerates
    its public callable methods, and registers them as
    ``<store>.<method>`` ops. Adds the top-level methods named in
    :data:`_T2_DATABASE_METHODS` under the ``database`` pseudo-store.
    Methods on :data:`_RPC_DENY_METHODS` and ops on
    :data:`_RPC_DENY_OPS` are skipped.
    """
    table: dict[str, Any] = {}
    for store_name in _T2_STORE_ATTRS:
        store = getattr(t2db, store_name, None)
        if store is None:
            continue
        for attr_name in dir(store):
            if attr_name.startswith("_"):
                continue
            if attr_name in _RPC_DENY_METHODS:
                continue
            op = f"{store_name}.{attr_name}"
            if op in _RPC_DENY_OPS:
                continue
            candidate = getattr(store, attr_name)
            if callable(candidate):
                table[op] = candidate
    for method_name in _T2_DATABASE_METHODS:
        candidate = getattr(t2db, method_name, None)
        if callable(candidate):
            table[f"database.{method_name}"] = candidate
    return table


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def t2_discovery_path(config_dir: Path) -> Path:
    """Return the canonical discovery-file path for the T2 daemon."""
    from nexus.daemon.discovery import discovery_path as _disc
    return _disc(config_dir, tier="t2")


def _build_discovery_payload(
    *,
    uds_path: Path,
    tcp_host: str,
    tcp_port: int,
    pid: int,
    daemon_version: str,
) -> dict[str, Any]:
    return {
        "format_version": _DISCOVERY_FORMAT_VERSION,
        "uds_path": str(uds_path),
        "tcp_host": tcp_host,
        "tcp_port": tcp_port,
        "pid": pid,
        "daemon_version": daemon_version,
        "start_time": datetime.now(timezone.utc).isoformat(),
    }


def _write_discovery_atomic(path: Path, payload: dict[str, Any]) -> None:
    """Atomically write *path* with 0o600 perms (see T3 daemon for rationale)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    body = json.dumps(payload).encode("utf-8")
    fd = os.open(
        str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600,
    )
    try:
        os.write(fd, body)
    finally:
        os.close(fd)
    os.replace(str(tmp), str(path))


def _daemon_version() -> str:
    try:
        from importlib.metadata import version
        return version("conexus")
    except Exception:
        return "0.0.0"


def _allocate_free_port() -> int:
    """Bind a free loopback port, then close it. No SO_REUSEADDR (see T3 daemon)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind((_T2_HOST, 0))
    port: int = sock.getsockname()[1]
    sock.close()
    return port


# ---------------------------------------------------------------------------
# T2 Daemon
# ---------------------------------------------------------------------------


class T2Daemon:
    """Long-lived process serving T2 RPC over UDS + loopback TCP.

    Substrate-only scope: dispatches the eight domain-store methods
    enumerated by :func:`_build_dispatch_table`. No admin ops, no
    event-stream subscriptions, no tuplespace, no subspace registry.

    Lifecycle::

        daemon = T2Daemon(config_dir=..., db_path=...)
        await daemon.start()                 # opens sockets, writes discovery
        await daemon.run_until_signal()      # blocks until SIGTERM/SIGINT
        await daemon.stop()                  # closes sockets, unlinks discovery

    The constructor takes a *db_path* rather than an injected T2Database
    so the daemon owns the SQLite handles itself (RDR-120 §A6: the
    daemon owns migration on its own path).
    """

    def __init__(
        self,
        *,
        config_dir: Path,
        db_path: Path,
    ) -> None:
        self._config_dir = config_dir
        self._db_path = db_path
        self._t2db: Any = None
        self._dispatch_table: dict[str, Any] = {}
        self._uds_server: asyncio.AbstractServer | None = None
        self._tcp_server: asyncio.AbstractServer | None = None
        self._uds_path: Path | None = None
        self._tcp_port: int | None = None
        self._discovery_path: Path | None = None
        self._spawn_lock_fd: int | None = None
        self._stop_event: asyncio.Event | None = None

    # ── public properties ───────────────────────────────────────────────

    @property
    def uds_path(self) -> Path:
        if self._uds_path is None:
            raise T2DaemonError("T2Daemon not started; uds_path unavailable")
        return self._uds_path

    @property
    def tcp_host(self) -> str:
        return _T2_HOST

    @property
    def tcp_port(self) -> int:
        if self._tcp_port is None:
            raise T2DaemonError("T2Daemon not started; tcp_port unavailable")
        return self._tcp_port

    @property
    def discovery_path(self) -> Path:
        if self._discovery_path is None:
            raise T2DaemonError("T2Daemon not started; discovery_path unavailable")
        return self._discovery_path

    # ── lifecycle ───────────────────────────────────────────────────────

    async def start(self) -> None:
        """Acquire spawn lock, open T2Database, bind sockets, write discovery."""
        self._ensure_dirs()
        self._acquire_spawn_lock()

        # Open the T2Database. apply_pending runs in its __init__ per
        # RDR-120 §A6 P3 transition mitigation; daemon is the sole
        # opener at this path during its lifetime.
        from nexus.db.t2 import T2Database
        self._t2db = T2Database(self._db_path)
        self._dispatch_table = _build_dispatch_table(self._t2db)

        uds_sock = self._bind_uds()
        tcp_sock = self._bind_tcp()

        self._uds_server = await asyncio.start_unix_server(
            self._make_handler(is_uds=True), sock=uds_sock,
        )
        self._tcp_server = await asyncio.start_server(
            self._make_handler(is_uds=False), sock=tcp_sock,
        )

        self._discovery_path = t2_discovery_path(self._config_dir)
        payload = _build_discovery_payload(
            uds_path=self._uds_path,
            tcp_host=_T2_HOST,
            tcp_port=self._tcp_port,
            pid=os.getpid(),
            daemon_version=_daemon_version(),
        )
        _write_discovery_atomic(self._discovery_path, payload)

        self._stop_event = asyncio.Event()
        _log.info(
            "t2_daemon_started",
            pid=os.getpid(),
            uds=str(self._uds_path),
            tcp_port=self._tcp_port,
            db_path=str(self._db_path),
        )

    async def run_until_signal(self) -> None:
        """Block until SIGTERM/SIGINT arrives."""
        if self._stop_event is None:
            raise T2DaemonError("run_until_signal called before start")
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self._stop_event.set)
        await self._stop_event.wait()
        _log.info("t2_daemon_stop_requested", signal_received=True)

    async def stop(self) -> None:
        """Close servers, drop discovery file, release spawn lock,
        close T2Database."""
        if self._uds_server is not None:
            self._uds_server.close()
            await self._uds_server.wait_closed()
            self._uds_server = None
        if self._tcp_server is not None:
            self._tcp_server.close()
            await self._tcp_server.wait_closed()
            self._tcp_server = None
        if self._discovery_path is not None:
            self._discovery_path.unlink(missing_ok=True)
            self._discovery_path = None
        if self._uds_path is not None and self._uds_path.exists():
            self._uds_path.unlink(missing_ok=True)
        if self._t2db is not None:
            try:
                self._t2db.close()
            except Exception as exc:  # noqa: BLE001
                _log.warning("t2_daemon_t2db_close_failed", error=str(exc))
            self._t2db = None
        self._release_spawn_lock()
        _log.info("t2_daemon_stopped")

    # ── socket binding ──────────────────────────────────────────────────

    def _bind_uds(self) -> socket.socket:
        socket_dir = self._config_dir / _SOCKET_SUBDIR
        socket_dir.mkdir(parents=True, exist_ok=True)
        try:
            socket_dir.chmod(0o700)
        except OSError:
            pass  # best-effort on filesystems without chmod
        uds_path = socket_dir / _UDS_FILENAME
        uds_path.unlink(missing_ok=True)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.bind(str(uds_path))
        except OSError as exc:
            sock.close()
            raise T2DaemonError(
                f"UDS bind failed at {uds_path}: {exc}"
            ) from exc
        # Restrict the socket itself to UID-only.
        try:
            os.chmod(str(uds_path), 0o600)
        except OSError:
            pass
        sock.listen(_LISTEN_BACKLOG)
        self._uds_path = uds_path
        return sock

    def _bind_tcp(self) -> socket.socket:
        port = _allocate_free_port()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind((_T2_HOST, port))
        except OSError as exc:
            sock.close()
            raise T2DaemonError(
                f"TCP bind failed at {_T2_HOST}:{port}: {exc}"
            ) from exc
        sock.listen(_LISTEN_BACKLOG)
        self._tcp_port = port
        return sock

    # ── connection handler + dispatch ───────────────────────────────────

    def _make_handler(self, *, is_uds: bool):
        async def _handler(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
        ) -> None:
            await self._handle_connection(reader, writer, is_uds=is_uds)
        return _handler

    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        is_uds: bool,
    ) -> None:
        try:
            while True:
                try:
                    frame = await read_frame(reader)
                except asyncio.IncompleteReadError:
                    break  # client closed
                except (ProtocolError, json.JSONDecodeError) as exc:
                    self._send_error(writer, None, "protocol", str(exc))
                    await writer.drain()
                    break

                request_id = frame.get("request_id")
                try:
                    result = await self._dispatch(frame, is_uds=is_uds)
                    write_frame(writer, {
                        "request_id": request_id,
                        "ok": True,
                        "result": result,
                    })
                except Exception as exc:  # noqa: BLE001
                    _log.warning(
                        "t2_daemon_dispatch_failed",
                        op=frame.get("op"),
                        error=str(exc),
                    )
                    self._send_error(
                        writer, request_id, type(exc).__name__, str(exc),
                    )
                await writer.drain()
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass

    async def _dispatch(
        self, frame: dict[str, Any], *, is_uds: bool,
    ) -> Any:
        op = frame.get("op")
        if not isinstance(op, str):
            raise ProtocolError(f"frame missing or invalid 'op': {op!r}")
        if op not in self._dispatch_table:
            raise ProtocolError(f"unknown op: {op!r}")
        args = frame.get("args") or []
        kwargs = frame.get("kwargs") or {}
        if not isinstance(args, list) or not isinstance(kwargs, dict):
            raise ProtocolError("frame 'args' must be list, 'kwargs' must be dict")
        callable_ = self._dispatch_table[op]
        # All current dispatch methods are sync; offload to a thread so
        # the event loop doesn't block on SQLite writes.
        return await asyncio.to_thread(callable_, *args, **kwargs)

    @staticmethod
    def _send_error(
        writer: asyncio.StreamWriter,
        request_id: Any,
        error_type: str,
        message: str,
    ) -> None:
        try:
            write_frame(writer, {
                "request_id": request_id,
                "ok": False,
                "error": {"type": error_type, "message": message},
            })
        except Exception:  # noqa: BLE001
            pass

    # ── housekeeping ────────────────────────────────────────────────────

    def _ensure_dirs(self) -> None:
        self._config_dir.mkdir(parents=True, exist_ok=True)
        try:
            self._config_dir.chmod(0o700)
        except OSError:
            pass

    def _acquire_spawn_lock(self) -> None:
        """Acquire an fcntl exclusive lock on a spawn-lock file. Held
        for the daemon's lifetime; released in :meth:`stop`. Prevents
        a second daemon from starting against the same config_dir."""
        import fcntl
        lock_path = self._config_dir / _SPAWN_LOCK_FILE
        fd = os.open(str(lock_path), os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            if exc.errno in (errno.EAGAIN, errno.EACCES):
                raise T2DaemonError(
                    f"another T2 daemon already holds the spawn lock at "
                    f"{lock_path}; refusing to start a second instance"
                ) from exc
            raise
        self._spawn_lock_fd = fd

    def _release_spawn_lock(self) -> None:
        import fcntl
        if self._spawn_lock_fd is None:
            return
        try:
            fcntl.flock(self._spawn_lock_fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(self._spawn_lock_fd)
        except OSError:
            pass
        self._spawn_lock_fd = None


# ---------------------------------------------------------------------------
# Sync entrypoint for the CLI verb
# ---------------------------------------------------------------------------


def run_t2_daemon(
    *,
    config_dir: Path,
    db_path: Path,
) -> None:
    """Run a T2 daemon to completion (start -> serve -> stop on SIGTERM).

    Called by ``nx daemon t2 start --foreground``. Synchronous wrapper
    that owns the asyncio event loop; the caller is the supervisor
    (launchd / systemd / shell) and treats this process as the daemon.
    """
    async def _main() -> None:
        daemon = T2Daemon(config_dir=config_dir, db_path=db_path)
        await daemon.start()
        try:
            await daemon.run_until_signal()
        finally:
            await daemon.stop()

    asyncio.run(_main())
