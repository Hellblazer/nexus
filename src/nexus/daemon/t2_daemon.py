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
import sqlite3
import struct
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

_log = structlog.get_logger(__name__)

# Grace period after SIGTERM before escalating to SIGKILL when reaping a
# lingering predecessor daemon on takeover (RDR-128 single-writer backstop).
_PREDECESSOR_REAP_TIMEOUT: float = 5.0

# RDR-140 P3.2 (nexus-7ffls): per-connection timeout for the reap-discrimination
# health-ping. A peer that does not accept a connection within this budget is
# treated as unreachable (a wedged/orphaned writer) and reaped. Module constant
# so tests can shrink it.
_HEALTH_PING_TIMEOUT: float = 1.0

# RDR-140 P3.2 option B (nexus-7ffls): a reachable, current-version peer found
# during the startup reap is a same-version daemon mid-shutdown — it released
# the spawn lock at the start of exit (we now hold it) but has not fully gone.
# Single-writer forbids opening the DB while it lives, but it is draining its
# own writes, so we give it this bounded window to exit on its own before
# forcing a reap. We NEVER coexist: it either exits here or we kill it. Module
# constant so tests can shrink it.
_GRACEFUL_PEER_EXIT_WAIT: float = 3.0

# RDR-129 B2 (nexus-qi1zb): bounded lock-retry for the serving dispatch.
# Mirrors the bootstrap-migration retry
# (``nexus.db.t2._apply_pending_with_lock_retry``): three attempts with two
# inter-attempt async sleeps. The per-store ``busy_timeout``
# (``SERVING_BUSY_TIMEOUT_MS`` = 30s) already absorbs most cross-store WAL
# contention, so this only fires when a window exceeds the timeout; it turns a
# >30s contention spike into a wait rather than a dropped best-effort write.
# Module constant so tests can shrink the sleeps.
_DISPATCH_RETRY_SLEEPS: tuple[float, ...] = (0.1, 0.25)

# RDR-146 P2 (nexus-5p2ci.12): interactive-vs-batch catalog-write fairness.
# An interactive-priority catalog write opens an in-memory deadline window;
# ``catalog.is_interactive_write_pending`` reports True until it lapses so a
# background batch indexer can yield. Imported from the catalog layer (the
# single source of truth shared with the producer-side yield loop).
from nexus.catalog.write_priority import (  # noqa: E402
    INTERACTIVE_WINDOW_S as _INTERACTIVE_WINDOW_S,
)
from nexus.daemon.service_registry import (  # noqa: E402
    LeaseRecord,
    ServiceRegistry,
    ServiceSupervisor,
)

#: RPC op name (under the ``catalog.*`` read namespace) the background
#: indexer polls. In-memory only (reads the deadline flag, touches no SQLite).
_INTERACTIVE_PROBE_OP = "catalog.is_interactive_write_pending"

# RDR-140 P1.3 (nexus-h2oko): self-healing discovery re-assert + loser poll.
# The spawn-lock HOLDER re-asserts its own t2_addr discovery file every
# ``_REASSERT_INTERVAL`` so a transient gap (a stale/lost addr file while the
# daemon is alive) self-heals instead of stranding clients. A spawn-lock LOSER
# polls for the winner's discovery file/socket for up to ``_LOSER_POLL_TIMEOUT``
# before quiet-exiting 0. Invariant: ``_LOSER_POLL_TIMEOUT`` >=
# ``_REASSERT_INTERVAL`` + worst-case write latency, so a loser polling for the
# winner's file never times out inside a window where the holder is mid-
# re-assert. Module-level so tests can shrink them.
_REASSERT_INTERVAL: float = 1.0
_LOSER_POLL_TIMEOUT: float = 3.0


def _is_locked_error(exc: BaseException) -> bool:
    """True when *exc* is transient WAL writer-lock contention
    (``database is locked`` / ``database is busy``), not a structural error.
    Mirrors the discriminator in
    ``nexus.db.t2._apply_pending_with_lock_retry``."""
    msg = str(exc).lower()
    return "locked" in msg or "busy" in msg


def _pid_is_alive(pid: int) -> bool:
    """True iff signalling pid 0 to *pid* succeeds (EPERM => exists, treat
    as alive). Mirrors the T3 daemon's liveness probe."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        return exc.errno != errno.ESRCH
    return True


def _is_t2_daemon_process(pid: int) -> bool:
    """Best-effort: confirm *pid*'s command line looks like a T2 daemon.

    Guards against PID reuse before we send a kill signal: the addr-file
    pid could have been recycled by an unrelated process after the old
    daemon died. When the command line cannot be read we return ``False``
    (refuse to kill rather than risk collateral)."""
    try:
        out = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return False
    cmd = out.stdout.strip()
    return "daemon t2" in cmd or "t2_daemon" in cmd


def _lsof_holder_pids(target: str) -> list[int]:
    """Pids with *target* open, via ``lsof -t`` (macOS + any host with lsof).

    ``lsof`` is not POSIX-standard but ships on macOS by default; on Linux it
    is the fallback when ``/proc`` is unavailable. Best-effort: any failure
    (lsof missing, non-zero exit because nothing holds the file) yields ``[]``.
    """
    try:
        out = subprocess.run(
            ["lsof", "-t", "--", target],
            capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return []
    pids: list[int] = []
    for tok in out.stdout.split():
        try:
            pids.append(int(tok))
        except ValueError:
            continue
    return pids


def _proc_holder_pids(target: str) -> list[int]:
    """Pids with *target* open, via a ``/proc/<pid>/fd`` symlink scan (Linux)."""
    pids: list[int] = []
    proc = Path("/proc")
    try:
        entries = list(proc.iterdir())
    except OSError:
        return []
    for entry in entries:
        if not entry.name.isdigit():
            continue
        fd_dir = entry / "fd"
        try:
            fds = list(fd_dir.iterdir())
        except OSError:
            continue  # process gone, or not ours to inspect
        for fd in fds:
            try:
                if os.readlink(str(fd)) == target:
                    pids.append(int(entry.name))
                    break
            except OSError:
                continue
    return pids


def _open_fd_holder_pids(target: str) -> list[int]:
    """Best-effort: every pid holding *target* open. Linux uses ``/proc``;
    other platforms (notably darwin) use ``lsof``."""
    if sys.platform != "darwin" and Path("/proc").is_dir():
        return _proc_holder_pids(target)
    return _lsof_holder_pids(target)


def _enumerate_t2_daemon_pids_for_db(db_path: Path) -> list[int]:
    """Live t2-daemon pids holding *db_path* open (RDR-129 A1, nexus-exa2p).

    The single-daemon invariant is "exactly one t2 daemon per db path." The
    addr file names only the canonical pid, so a side-orphan that started
    after it is invisible to the addr-file reap. An open-fd probe on the data
    file finds every holder regardless of how it was started (the cmdline
    ``nx daemon t2 start`` does not name the db). Results are filtered to
    processes whose command line looks like a t2 daemon, guarding against an
    unrelated process that happens to hold the file.

    Best-effort: returns ``[]`` when the db file does not exist (no holders
    possible) or any probe step fails. Used by the startup sweep
    (:meth:`T2Daemon._reap_predecessor_daemon`) and by ``nx doctor``'s
    multiplicity check.
    """
    if not db_path.exists():
        return []
    target = str(db_path.resolve())
    return [pid for pid in _open_fd_holder_pids(target) if _is_t2_daemon_process(pid)]


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

#: Legacy spawn-lock file name (fcntl exclusive lock held for daemon
#: lifetime). The lock is now keyed on a hash of the resolved db_path
#: so two daemons against the same data file collide regardless of
#: which config_dir they were started from (RDR-120 P3b code-review
#: item 2). The legacy ``t2_spawn.lock`` filename is still acquired
#: alongside the path-scoped lock to preserve the "same config_dir"
#: invariant operators already rely on.
_SPAWN_LOCK_FILE: str = "t2_spawn.lock"


def _spawn_lock_path_for_db(db_path: Path) -> Path:
    """Return the path-scoped spawn-lock file for *db_path*.

    The lock lives as a sibling of the data file
    (``<db_path>.spawn_lock``) so daemons started against the same
    ``db_path`` from different ``config_dir``s contend on the same
    lock file. The actual race surface is the data file, so the lock
    is anchored where the race exists.
    """
    return db_path.parent / f"{db_path.name}.spawn_lock"

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

#: nexus-azsqe (RDR-129 A2 follow-up): bound on the blocking
#: ``T2Database.close()`` inside ``stop()``. A close that stalls on a
#: pending WAL checkpoint must not wedge ``stop()`` open-ended (which,
#: with defer-release-to-exit, would hold the spawn lock and block an
#: upgrade restart on the stale daemon version). Generous enough that a
#: legitimate checkpoint completes; finite so a genuinely hung close is
#: bounded. On timeout ``stop()`` logs and proceeds to exit — the OS
#: reaps the offloaded thread and releases the lock.
_DB_CLOSE_TIMEOUT: float = 10.0


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ProtocolError(Exception):
    """Raised when a peer sends a malformed or oversized wire frame."""


class T2DaemonError(RuntimeError):
    """Raised on daemon lifecycle errors (bind failed, address in use,
    discovery write failed, etc.)."""


class T2SpawnLockLost(T2DaemonError):
    """RDR-140 P1.3 (nexus-h2oko): the spawn lock is already held by a live
    winner, so this process must quiet-attach and exit 0 — NOT crash.

    Subclasses :class:`T2DaemonError` so existing callers that catch the base
    (and assert ``"spawn lock"`` in the message) keep working; ``run_t2_daemon``
    catches this subtype first to short-circuit the crash path. A1-verified:
    raised from ``_acquire_spawn_lock`` (start():645) strictly BEFORE any
    ``T2Database`` construction (:658), so a loser never opens the DB."""


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
#: Eight stores as of RDR-120 P5.A.1 (nexus-9zmpl): the seven shared-
#: nexus.db stores plus the catalog store (which uniquely opens
#: ``.catalog.db`` under ``catalog_path()``).
_T2_STORE_ATTRS: tuple[str, ...] = (
    "memory",
    "plans",
    "chash_index",
    "taxonomy",
    "telemetry",
    "document_aspects",
    "aspect_queue",
    "catalog",
)

#: Top-level T2Database methods exposed under the "database" pseudo-store.
#: ``hello`` is the RDR-120 P3b connection handshake — T2Client invokes
#: it on first connect to validate schema-version compatibility.
#: ``expire`` (RDR-128 P3) is the multi-store TTL sweep (memory rows +
#: relevance_log); routing it lets the SessionEnd flush hook reach the
#: daemon instead of opening memory.db directly. Its ``int`` arg and
#: ``int`` return round-trip framed JSON cleanly.
_T2_DATABASE_METHODS: tuple[str, ...] = (
    "rename_collection_cascade",
    "expire",
    "complete_aspect",
    "hello",
)

#: Methods filtered from every store. ``close`` is denied to prevent a
#: client from tearing down the daemon's SQLite handles via RPC;
#: underscored names are already filtered separately.
_RPC_DENY_METHODS: frozenset[str] = frozenset({"close"})

#: Per-op denylist (qualified ``<store>.<method>``). Methods whose
#: signature or return type don't round-trip framed JSON (typed
#: dataclasses, context managers, raw ``sqlite3.Cursor`` handles).
_RPC_DENY_OPS: frozenset[str] = frozenset({
    "document_aspects.upsert",
    "document_aspects.get",
    "document_aspects.get_by_doc_id",
    # RDR-120 P5.A.1 (nexus-9zmpl) — catalog op denylist:
    # ``execute`` returns a live ``sqlite3.Cursor`` that cannot be
    # serialised across the framed-JSON boundary. ``transaction`` and
    # ``bulk_load_documents`` are ``@contextmanager`` generators that
    # likewise have no JSON shape — clients that need them must hold
    # a local CatalogStore instance (the P5.A.2 shim will route
    # local-mode catalog operations directly without going over RPC).
    "catalog.execute",
    "catalog.transaction",
    "catalog.bulk_load_documents",
    # ``rebuild`` is event-replay scoped to the catalog process tree
    # and not suitable for RPC mediation — keep it client-local.
    "catalog.rebuild",
})


#: RDR-146 P1 (nexus-5p2ci.20): op-prefix for the daemon-hosted rich
#: Catalog write whitelist. Ops under this prefix are serialised via
#: ``_catalog_write_lock`` (see ``T2Daemon._dispatch``). Mirrors
#: ``catalog_write_shim.CATALOG_WRITE_PREFIX``.
_CATALOG_WRITE_PREFIX = "catalog_write."


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


def _health_ping(payload: dict[str, Any]) -> bool:
    """Best-effort liveness probe: can we open a connection to the peer named in
    its discovery *payload*? Tries the UDS path first, then TCP. Never raises.

    RDR-140 P3.2: a peer that accepts a connection is serving (healthy); one
    that refuses/times out is a wedged or orphaned writer and must be reaped.
    """
    uds = payload.get("uds_path")
    if isinstance(uds, str) and uds:
        sock = None
        try:
            # socket() itself can raise (EMFILE) — create inside the try so the
            # "never raises" contract holds even under fd exhaustion.
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(_HEALTH_PING_TIMEOUT)
            sock.connect(uds)
            return True
        except OSError:
            pass
        finally:
            if sock is not None:
                sock.close()
    host, port = payload.get("tcp_host"), payload.get("tcp_port")
    if isinstance(host, str) and host and isinstance(port, int) and port > 0:
        try:
            with socket.create_connection((host, port), timeout=_HEALTH_PING_TIMEOUT):
                return True
        except OSError:
            pass
    return False


def _peer_handshake(pid: int, payload: dict[str, Any] | None) -> tuple[str | None, bool]:
    """Reap-discrimination probe for *pid*: return ``(daemon_version, reachable)``.

    RDR-140 P3.2 (nexus-7ffls). ``daemon_version`` is read from the discovery
    *payload* (the ``t2_addr`` token carries it — A2; no new persisted state).
    ``reachable`` is a best-effort health-ping to the token's socket. A peer with
    no token (``payload is None`` — an open-fd-only side-orphan) returns
    ``(None, False)``: it has no socket we can reach and no version we can trust,
    so the caller cannot spare it (single-writer backstop).
    """
    if not isinstance(payload, dict):
        return (None, False)
    version = payload.get("daemon_version")
    return (version if isinstance(version, str) else None, _health_ping(payload))


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
        # RDR-146 P1 (nexus-5p2ci.20): the daemon hosts exactly one rich
        # Catalog (sole owner of the .catalog.db write handle + JSONL
        # append path). Constructed at start(); the 16-op write whitelist
        # is merged into the dispatch table. ``_catalog_write_lock``
        # serialises every catalog write so the hosted Catalog's multi-
        # step JSONL+SQLite mutations stay atomic against the dispatch
        # thread pool (the per-instance _owner_register_lock at
        # catalog.py:533 only covers the owner check-then-register window,
        # and the directory flock does not serialise sibling threads).
        self._catalog: Any = None
        self._catalog_write_lock: asyncio.Lock | None = None
        # RDR-146 P2 (nexus-5p2ci.12): interactive-vs-batch fairness. An
        # interactive-priority catalog write sets this deadline; the probe op
        # reports pending until ``self._monotonic()`` passes it. In-memory
        # only. ``_monotonic`` is injectable so fairness tests use a fixed
        # clock (project rule: deterministic, fixed clocks).
        self._interactive_write_deadline: float = 0.0
        self._monotonic: Any = time.monotonic
        self._dispatch_table: dict[str, Any] = {}
        self._uds_server: asyncio.AbstractServer | None = None
        self._tcp_server: asyncio.AbstractServer | None = None
        self._uds_path: Path | None = None
        self._tcp_port: int | None = None
        self._discovery_path: Path | None = None
        self._spawn_lock_fd: int | None = None
        self._spawn_lock_fd_path: int | None = None
        self._stop_event: asyncio.Event | None = None
        # RDR-149 P2 (nexus-fx77g): discovery identity now rides the leased
        # service-registry substrate. The wall-clock used for the lease
        # heartbeat stamp is injectable (tests pin it), distinct from the
        # monotonic fairness clock above. The supervisor owns the lease +
        # heartbeat; the spawn-lock above still owns single-writer (RDR-128).
        self._lease_clock: Any = time.time
        self._registry: ServiceRegistry | None = None
        self._supervisor: ServiceSupervisor | None = None
        self._lease_record: LeaseRecord | None = None
        # RDR-140 P1.3: self-healing discovery re-assert (holder only).
        self._reassert_task: asyncio.Task[None] | None = None

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
        # RDR-128 single-writer backstop (nexus-070e2): we now hold the spawn
        # lock, so we are the legitimate single writer. Reap any predecessor
        # daemon still named in the addr file BEFORE opening T2Database, so its
        # WAL writer is gone before we migrate and two daemons cannot coexist
        # when the flock was bypassed (version transition / released-but-alive).
        self._reap_predecessor_daemon()

        # RDR-120 P3b (nexus-e9x4l): daemon is the sole ``apply_pending``
        # caller. T2Database.__init__ no longer auto-runs migrations; the
        # daemon passes ``run_migrations=True`` so its construction
        # bootstraps the schema before any client connects.
        from nexus.db.t2 import T2Database
        self._t2db = T2Database(self._db_path, run_migrations=True)
        self._dispatch_table = _build_dispatch_table(self._t2db)

        # RDR-146 P1 (nexus-5p2ci.20): host the rich Catalog and merge its
        # write-only 16-op whitelist into the dispatch table. Construction
        # WRITES (events.jsonl backfill + mtime-gated _ensure_consistent
        # rebuild) — that is correct here: the daemon is the designated
        # single writer. The asyncio.Lock is created now that we are
        # inside the running loop.
        self._catalog_write_lock = asyncio.Lock()
        self._catalog = self._build_hosted_catalog()
        from nexus.daemon.catalog_write_shim import build_catalog_write_dispatch
        self._dispatch_table.update(build_catalog_write_dispatch(self._catalog))
        # RDR-146 P2 (nexus-5p2ci.12): the interactive-pending probe. A real
        # daemon method (no SQLite touch) registered under the catalog.* read
        # namespace so the background indexer reaches it via the read proxy.
        # The low-level CatalogStore has no method of this name, so there is
        # no collision with the auto-enumerated catalog.* reads.
        self._dispatch_table[_INTERACTIVE_PROBE_OP] = self._is_interactive_write_pending

        uds_sock = self._bind_uds()
        tcp_sock = self._bind_tcp()

        self._uds_server = await asyncio.start_unix_server(
            self._make_handler(is_uds=True), sock=uds_sock,
        )
        self._tcp_server = await asyncio.start_server(
            self._make_handler(is_uds=False), sock=tcp_sock,
        )

        # RDR-149 P2: publish the discovery identity as a lease record via
        # the shared registry. Scope key is the uid, so the record path is
        # the same ``t2_addr.<uid>`` the legacy payload used; only the
        # on-disk shape (lease + generation + endpoint) and the liveness
        # mechanism (TTL freshness, not pid) change. The endpoint carries
        # the uds/tcp connection fields clients resolve, plus the pid for
        # the loser-poll breadcrumb and legacy-reader compatibility.
        self._discovery_path = t2_discovery_path(self._config_dir)
        scope_key = str(os.getuid())
        endpoint = {
            "uds_path": str(self._uds_path),
            "tcp_host": _T2_HOST,
            "tcp_port": self._tcp_port,
            "pid": os.getpid(),
        }
        self._registry = ServiceRegistry(
            dir=self._config_dir, tier="t2", clock=self._lease_clock
        )
        self._supervisor = ServiceSupervisor(
            self._registry,
            scope_key,
            version=_daemon_version(),
            endpoint_provider=lambda: endpoint,
        )
        self._lease_record = self._supervisor.publish_once()

        # RDR-140 P1.3: start the self-healing re-assert task now that the
        # discovery file is written. It re-creates the file if it ever goes
        # missing while we (the spawn-lock holder) are alive. stop() cancels
        # it BEFORE unlinking the discovery file so it cannot resurrect a
        # mid-shutdown daemon's addr (RDR-129 early-unlink ordering).
        self._reassert_task = asyncio.create_task(self._reassert_discovery_loop())

        self._stop_event = asyncio.Event()
        _log.info(
            "t2_daemon_started",
            pid=os.getpid(),
            uds=str(self._uds_path),
            tcp_port=self._tcp_port,
            db_path=str(self._db_path),
        )

    async def _reassert_discovery_loop(self) -> None:
        """Periodically re-assert our own discovery file (holder self-heal).

        RDR-140 P1.3 (nexus-h2oko): a transient loss of the t2_addr file while
        the daemon is alive otherwise strands clients (the race-harness saw all
        racers crash in the discovery-gap case). Every ``_REASSERT_INTERVAL``
        we re-write the file iff it is missing or no longer names our pid;
        when it is intact this is a cheap stat + read with no write. Cancelled
        at the START of :meth:`stop` so it can never resurrect a file the
        shutdown unlink just removed.
        """
        while True:
            await asyncio.sleep(_REASSERT_INTERVAL)
            supervisor = self._supervisor
            if supervisor is None:
                continue
            try:
                # RDR-149 P2: one supervisor tick re-stamps the lease,
                # which self-heals a transiently lost record at the same
                # generation (the RDR-140 re-assert) and learns if a newer
                # owner fenced us (cannot happen while we hold the spawn
                # lock, but the loser-quiet-exit is logged if it ever does).
                supervisor.heartbeat_tick()
                if supervisor.fenced:
                    _log.warning(
                        "t2_daemon_discovery_fenced",
                        pid=os.getpid(),
                        scope=str(os.getuid()),
                    )
                else:
                    self._lease_record = supervisor.record
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                # Self-heal is best-effort; never let it crash the daemon.
                _log.warning("t2_daemon_discovery_reassert_failed", exc=str(exc))

    async def run_until_signal(self) -> None:
        """Block until SIGTERM/SIGINT arrives."""
        if self._stop_event is None:
            raise T2DaemonError("run_until_signal called before start")
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self._stop_event.set)
        await self._stop_event.wait()
        _log.info("t2_daemon_stop_requested", signal_received=True)
        # nexus-61539: make the breadcrumb durable BEFORE stop() runs.
        # stop() can stall (a hung WAL checkpoint in close()), and under
        # CI load the process could otherwise exit before the
        # RotatingFileHandler flushed this line — losing the shutdown
        # diagnostic in production, not just flaking the observability
        # test. flush() pushes the record to the OS; a peer process
        # reading the log then sees it.
        from nexus.logging_setup import flush_logging
        flush_logging()

    async def stop(self) -> None:
        """Close servers, drop discovery file, close T2Database.

        RDR-129 A2 (nexus-kwqhd): ``stop()`` deliberately does NOT release the
        spawn lock. The lock is held for the whole process lifetime and dropped
        by the OS when the process exits. Releasing it here (the prior
        behaviour) opened a *released-but-alive* window: ``stop()`` runs while
        the process is still draining/exiting, so a respawn (notably
        ``ensure-running`` on version skew) could acquire the freed lock and
        run alongside the still-living predecessor, violating single-writer.
        Deferring the release to OS-on-exit closes that window: the lock stops
        being held at exactly the moment the pid stops being alive. The
        co-dependent ``ensure-running`` interlock (``commands/daemon.py``) now
        waits on the predecessor's PID liveness before cold-spawning, so it
        never spawns into the lock-still-held window. ``_release_spawn_lock``
        remains callable for explicit teardown (tests simulate process exit).
        """
        # RDR-140 P1.3 (nexus-h2oko): cancel the self-healing re-assert task
        # FIRST — before the discovery-file unlink below — so it can never
        # re-create the addr file for a daemon that is shutting down (the
        # resurrection race against RDR-129 early-unlink-on-stop ordering).
        if self._reassert_task is not None:
            self._reassert_task.cancel()
            try:
                await self._reassert_task
            except BaseException:  # noqa: BLE001
                pass
            self._reassert_task = None

        if self._uds_server is not None:
            self._uds_server.close()
            await self._uds_server.wait_closed()
            self._uds_server = None
        if self._tcp_server is not None:
            self._tcp_server.close()
            await self._tcp_server.wait_closed()
            self._tcp_server = None
        if self._registry is not None and self._lease_record is not None:
            # RDR-149 P2: relinquish is own-record-only, so a fenced
            # predecessor's delayed stop cannot unlink a successor's record
            # (CA-4) — the same invariant the early-unlink ordering above
            # protects (reassert cancelled BEFORE this).
            self._registry.relinquish(self._lease_record)
            self._lease_record = None
            self._supervisor = None
            self._registry = None
        self._discovery_path = None
        if self._uds_path is not None and self._uds_path.exists():
            self._uds_path.unlink(missing_ok=True)
        if self._t2db is not None:
            # nexus-azsqe (RDR-129 A2 follow-up): bound the blocking close.
            # close() is synchronous and can stall on a pending WAL
            # checkpoint; offload it to a thread and cap it with
            # _DB_CLOSE_TIMEOUT so a hung close can't wedge stop()
            # open-ended and block an upgrade restart. On timeout, log and
            # proceed to exit — the OS reaps the thread and releases the
            # spawn lock at process exit. Defense-in-depth: the
            # ensure-running PID-liveness interlock already prevents the
            # zero-daemon case; this prevents the wedged-on-stale-version
            # case. The existing _GRACEFUL_STOP_TIMEOUT guards socket
            # teardown above, not this DB close.
            try:
                await asyncio.wait_for(
                    asyncio.to_thread(self._t2db.close),
                    timeout=_DB_CLOSE_TIMEOUT,
                )
            except TimeoutError:
                _log.warning(
                    "t2_daemon_t2db_close_timeout",
                    timeout_s=_DB_CLOSE_TIMEOUT,
                )
            except Exception as exc:  # noqa: BLE001
                _log.warning("t2_daemon_t2db_close_failed", error=str(exc))
            self._t2db = None
        # RDR-146 P1: close the hosted Catalog's SQLite handle. Catalog
        # exposes no close(); the writer lives on its private CatalogDB
        # (_db). Guard defensively — a missing/closed handle must not
        # wedge stop().
        if self._catalog is not None:
            try:
                self._catalog._db.close()
            except Exception as exc:  # noqa: BLE001
                _log.warning("t2_daemon_catalog_close_failed", error=str(exc))
            self._catalog = None
            self._catalog_write_lock = None
        # NB: spawn lock intentionally NOT released here — see docstring.
        _log.info("t2_daemon_stopped")

    def _build_hosted_catalog(self) -> Any:
        """Construct the single rich Catalog the daemon hosts.

        Resolves the catalog directory through ``nexus.config.catalog_path``
        (the same resolution every CLI verb and the MCP server use), so the
        daemon writes to the canonical ``.catalog.db`` + JSONL set. Tests
        redirect it via the autouse ``_isolate_catalog`` fixture
        (``NEXUS_CATALOG_PATH``), so daemon startup never touches the real
        user catalog.
        """
        from nexus.catalog import Catalog
        from nexus.config import catalog_path
        path = catalog_path()
        return Catalog(path, path / ".catalog.db")

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
        # RDR-146 P2 (nexus-5p2ci.12): interactive-vs-batch fairness. An
        # interactive-priority catalog WRITE opens (or refreshes) the in-memory
        # deadline window BEFORE the op runs, so a background batch indexer
        # polling ``catalog.is_interactive_write_pending`` yields for the whole
        # interactive burst. Reads/probe and batch writes never touch it; an
        # absent ``priority`` field defaults to batch (back-compat).
        if (
            frame.get("priority", "batch") == "interactive"
            and op.startswith(_CATALOG_WRITE_PREFIX)
        ):
            self._interactive_write_deadline = self._monotonic() + _INTERACTIVE_WINDOW_S
        # RDR-146 P1 (nexus-5p2ci.20): catalog writes are serialised. The
        # hosted rich Catalog performs multi-step JSONL+SQLite mutations
        # that are not atomic across the dispatch thread pool (the
        # directory flock does not block sibling threads, and the per-
        # instance _owner_register_lock only covers owner registration).
        # Holding an asyncio.Lock across the threaded invocation makes
        # catalog write dispatch single-threaded / serial regardless of
        # how many client connections fan in concurrently.
        if op.startswith(_CATALOG_WRITE_PREFIX) and self._catalog_write_lock is not None:
            async with self._catalog_write_lock:
                return await self._invoke_with_lock_retry(callable_, op, args, kwargs)
        return await self._invoke_with_lock_retry(callable_, op, args, kwargs)

    async def _invoke_with_lock_retry(
        self, callable_: Any, op: str, args: list[Any], kwargs: dict[str, Any],
    ) -> Any:
        # All current dispatch methods are sync; offload to a thread so
        # the event loop doesn't block on SQLite writes.
        #
        # RDR-129 B2 (nexus-qi1zb): retry on transient WAL writer-lock
        # contention so a cross-store contention window past the per-store
        # busy_timeout becomes a wait, not a dropped best-effort write (the
        # shakeout drop class). Non-lock errors propagate on the first attempt;
        # the final attempt re-raises so a genuinely stuck lock still surfaces
        # (logged upstream as t2_daemon_dispatch_failed). Re-running the whole
        # op is safe: SQLITE_BUSY fires at lock acquisition before the
        # statement executes, and the store writes are upserts / single
        # transactions that roll back cleanly on failure.
        sleeps = _DISPATCH_RETRY_SLEEPS
        max_attempts = len(sleeps) + 1
        for attempt in range(1, max_attempts + 1):
            try:
                return await asyncio.to_thread(callable_, *args, **kwargs)
            except sqlite3.OperationalError as exc:
                if not _is_locked_error(exc) or attempt == max_attempts:
                    raise
                _log.warning(
                    "t2_daemon_dispatch_lock_retry",
                    op=op, attempt=attempt, exc=str(exc),
                )
                await asyncio.sleep(sleeps[attempt - 1])

    def _is_interactive_write_pending(self) -> bool:
        """RDR-146 P2 probe: True while an interactive catalog write window is
        open. In-memory read of the deadline flag; touches no SQLite. Reached
        over RPC as ``catalog.is_interactive_write_pending`` by the background
        indexer's yield loop."""
        return self._monotonic() < self._interactive_write_deadline

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

    def _reap_predecessor_daemon(self) -> None:
        """Sweep and reap every lingering t2 daemon for this db path.

        Precondition: the spawn lock is held (called from :meth:`start`
        immediately after :meth:`_acquire_spawn_lock`). Holding the lock
        means we are the legitimate single writer, so any OTHER live t2 daemon
        against this ``memory.db`` is a zombie that escaped the flock (a
        version transition, a released-but-alive window) and must be reaped to
        honour RDR-128 single-writer — otherwise two daemons contend on the
        same WAL ("FTS5: database is locked"; the 5.0.2-5.0.4 daemon-churn
        class). Run BEFORE opening ``T2Database`` so each predecessor's WAL
        writer is gone before we migrate (and so self does not yet hold the db
        open, keeping it out of the open-fd probe).

        Two reap targets are unioned (RDR-129 A1, nexus-exa2p):

        1. The addr-file pid — the cheap, reliable signal for the common
           takeover case (and the only one the 5.1.5 ``nexus-070e2`` reap
           covered).
        2. Same-db open-fd holders — a side-orphan that started AFTER the
           canonical daemon was never the addr-file pid and is invisible to
           (1). Enumerating holders of the data file (RF-A2 open-fd probe)
           generalises "reap the addr-file predecessor" to "guarantee single
           occupancy."

        Best-effort and non-fatal: any failure to read the file, probe fds, or
        signal a pid leaves startup to proceed (the flock already guarantees
        we are the sole new writer).
        """
        # 1. addr-file pid + its discovery token. The token is the ONLY input
        #    that lets us spare a healthy current-version peer (RDR-140 P3:
        #    it carries daemon_version + the socket to health-ping).
        addr_pid: int | None = None
        addr_payload: dict[str, Any] | None = None
        disc_path = t2_discovery_path(self._config_dir)
        try:
            raw = disc_path.read_text()
            payload = json.loads(raw) if raw.strip() else None
        except (OSError, ValueError):
            payload = None
        if isinstance(payload, dict):
            addr_payload = payload
            pid = payload.get("pid")
            if isinstance(pid, int):
                addr_pid = pid

        # 2. same-db open-fd sweep (catches side-orphans absent from the addr
        #    file). These have NO token, so they pass payload=None and can
        #    never be spared — they escaped the db spawn lock we hold and are
        #    exactly the orphan case this sweep exists to reap.
        seen: set[int] = set()

        def _reap(pid: int, tok: dict[str, Any] | None) -> None:
            if pid <= 0 or pid == os.getpid() or pid in seen:
                return
            seen.add(pid)
            self._reap_one_daemon(pid, tok)

        if addr_pid is not None:
            _reap(addr_pid, addr_payload)
        for pid in _enumerate_t2_daemon_pids_for_db(self._db_path):
            _reap(pid, None)

    def _reap_one_daemon(
        self, pid: int, payload: dict[str, Any] | None = None,
    ) -> None:
        """SIGTERM (escalating to SIGKILL after ``_PREDECESSOR_REAP_TIMEOUT``)
        a single live t2-daemon *pid*. Guarded by a liveness check and a
        cmdline check (PID-reuse guard: refuse to kill a recycled pid whose
        command line is not a t2 daemon). Best-effort; never raises.

        RDR-140 P3.2 option B (nexus-7ffls): ownership/version-aware
        discrimination — WAIT, never coexist. When *payload* is the peer's
        ``t2_addr`` token and it health-pings AND its ``daemon_version`` equals
        ours, the peer is a same-version daemon mid-shutdown (it could not still
        hold the db spawn lock we just acquired, so it has released it at the
        start of its own exit). We do NOT SIGTERM it immediately — it is
        draining its own writes — but we also NEVER open the DB alongside it:
        we wait up to ``_GRACEFUL_PEER_EXIT_WAIT`` for it to exit on its own,
        and only if it overstays do we force the reap. Either way this method
        does not return until the peer is gone, so ``start()`` (which opens
        ``T2Database`` right after the sweep) is guaranteed single-writer.

        A stale-version or unreachable addr peer, and every open-fd-only peer
        (``payload is None``), is reaped immediately: the RDR-128/129
        single-writer backstop is preserved, never weakened.
        """
        if not _pid_is_alive(pid):
            return
        if not _is_t2_daemon_process(pid):
            _log.warning("t2_predecessor_pid_not_daemon_skip_reap", pid=pid)
            return

        if payload is not None:
            version, reachable = _peer_handshake(pid, payload)
            if reachable and version == _daemon_version():
                # Same-version peer mid-shutdown: let it drain and exit, but
                # never coexist — force the reap if it overstays the window.
                deadline = time.monotonic() + _GRACEFUL_PEER_EXIT_WAIT
                while time.monotonic() < deadline:
                    if not _pid_is_alive(pid):
                        _log.info(
                            "t2_predecessor_exited_gracefully",
                            pid=pid, daemon_version=version,
                        )
                        return
                    time.sleep(0.1)
                _log.warning(
                    "t2_predecessor_overstayed_graceful_wait_forcing_reap",
                    pid=pid, daemon_version=version,
                )

        _log.warning("t2_reaping_predecessor_daemon", pid=pid)
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        except OSError as exc:
            _log.warning("t2_predecessor_sigterm_failed", pid=pid, err=str(exc))
            return

        deadline = time.monotonic() + _PREDECESSOR_REAP_TIMEOUT
        while time.monotonic() < deadline:
            if not _pid_is_alive(pid):
                _log.info("t2_predecessor_reaped", pid=pid, via="SIGTERM")
                return
            time.sleep(0.1)

        try:
            os.kill(pid, signal.SIGKILL)
            _log.warning("t2_predecessor_sigkilled", pid=pid)
        except ProcessLookupError:
            return  # already gone
        except OSError:
            return

        # RDR-140 P3 (nexus-dzf1q re-review): SIGKILL delivery is asynchronous —
        # os.kill returning does NOT mean the kernel has finished reaping the
        # process and released its db fds. start() opens T2Database the instant
        # _reap_predecessor_daemon returns, so we MUST confirm the pid is gone
        # before returning, or two writers could briefly overlap. SIGKILL is
        # unblockable, so this poll terminates quickly in practice; if it
        # overstays (zombie / kernel stall) we log and return — the pid is
        # doomed and its writer is effectively dead.
        deadline = time.monotonic() + _PREDECESSOR_REAP_TIMEOUT
        while time.monotonic() < deadline:
            if not _pid_is_alive(pid):
                _log.info("t2_predecessor_reaped", pid=pid, via="SIGKILL")
                return
            time.sleep(0.1)
        _log.warning("t2_predecessor_still_alive_after_sigkill", pid=pid)

    def _acquire_spawn_lock(self) -> None:
        """Acquire exclusive fcntl locks on both the config_dir-scoped
        and the db_path-scoped spawn-lock files. Both are held for the
        daemon's lifetime; both are released in :meth:`stop`.

        Two locks because the race surface is the data file but
        operators still rely on the "one daemon per config_dir"
        invariant. The db_path-scoped lock (RDR-120 P3b code-review
        item 2) prevents two daemons against the same data file from
        different ``config_dir``s both running ``apply_pending``.
        """
        import fcntl

        # 1. Legacy config_dir-scoped lock.
        lock_path = self._config_dir / _SPAWN_LOCK_FILE
        fd = os.open(str(lock_path), os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            if exc.errno in (errno.EAGAIN, errno.EACCES):
                raise T2SpawnLockLost(
                    f"another T2 daemon already holds the spawn lock at "
                    f"{lock_path}; refusing to start a second instance"
                ) from exc
            raise
        self._spawn_lock_fd = fd

        # 2. db_path-scoped lock. Anchored where the race exists so
        # two daemons against the same data file from different
        # config_dirs still collide.
        path_lock = _spawn_lock_path_for_db(self._db_path)
        path_lock.parent.mkdir(parents=True, exist_ok=True)
        fd2 = os.open(str(path_lock), os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd2, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd2)
            # Release the first lock so we don't leak it on failure.
            try:
                fcntl.flock(self._spawn_lock_fd, fcntl.LOCK_UN)
                os.close(self._spawn_lock_fd)
            except OSError:
                pass
            self._spawn_lock_fd = None
            if exc.errno in (errno.EAGAIN, errno.EACCES):
                raise T2SpawnLockLost(
                    f"another T2 daemon already holds the db_path spawn "
                    f"lock at {path_lock}; refusing to start a second "
                    f"instance against the same data file"
                ) from exc
            raise
        self._spawn_lock_fd_path = fd2

    def _release_spawn_lock(self) -> None:
        import fcntl

        for attr in ("_spawn_lock_fd_path", "_spawn_lock_fd"):
            fd = getattr(self, attr, None)
            if fd is None:
                continue
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(fd)
            except OSError:
                pass
            setattr(self, attr, None)


# ---------------------------------------------------------------------------
# Sync entrypoint for the CLI verb
# ---------------------------------------------------------------------------


def _poll_for_winner(config_dir: Path, timeout: float) -> bool:
    """Poll for the spawn-lock winner's discovery file + reachable socket.

    RDR-140 P1.3 (nexus-h2oko): a spawn-lock loser calls this purely for
    observability before exiting 0. Returns True once the winner's t2_addr
    lease resolves to a fresh owner AND its UDS is connectable; False if the
    timeout elapses first. A discovered-but-unreachable target (set file,
    socket not yet accepting) or a stale lease (aged past TTL) is NOT
    treated as a live attach — we keep polling and report False at timeout
    rather than claim a stale attach. Never raises.

    RDR-149 P2: liveness is now lease freshness, not pid; resolution goes
    through ``find_t2_daemon`` so the loser sees the winner exactly as a
    real client would (the lease-aware reader handles both the new lease
    record and a legacy payload mid-upgrade).
    """
    from nexus.daemon.discovery import find_t2_daemon

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            payload = find_t2_daemon(config_dir)
            if payload is not None:
                uds = payload.get("uds_path")
                if uds:
                    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    try:
                        sock.settimeout(0.5)
                        sock.connect(str(uds))
                        return True
                    except OSError:
                        pass  # set-but-unreachable — keep polling.
                    finally:
                        sock.close()
        except OSError:
            pass
        time.sleep(0.05)
    return False


def run_t2_daemon(
    *,
    config_dir: Path,
    db_path: Path,
) -> None:
    """Run a T2 daemon to completion (start -> serve -> stop on SIGTERM).

    Called by ``nx daemon t2 start --foreground``. Synchronous wrapper
    that owns the asyncio event loop; the caller is the supervisor
    (launchd / systemd / shell) and treats this process as the daemon.

    Routes the daemon's structlog events to a rotating file at
    ``<config_dir>/logs/t2_daemon.log`` (nexus-n8sbw). Without this the
    daemon was spawned with stdout/stderr -> DEVNULL and produced no
    log, so a crash or signal-kill left no record and the cause was
    undiagnosable.
    """
    from nexus.logging_setup import configure_logging

    configure_logging("t2_daemon", config_dir=config_dir)

    async def _main() -> None:
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(
            lambda _loop, ctx: _log.error(
                "t2_daemon_loop_exception",
                message=ctx.get("message"),
                exception=repr(ctx.get("exception")),
            )
        )
        daemon = T2Daemon(config_dir=config_dir, db_path=db_path)
        await daemon.start()
        try:
            await daemon.run_until_signal()
        finally:
            await daemon.stop()

    try:
        asyncio.run(_main())
    except T2SpawnLockLost:
        # RDR-140 P1.3 (nexus-h2oko): losing the spawn lock is a benign
        # quiet-attach, NOT a crash. A live winner already owns the data
        # file; poll briefly for its discovery file/socket (best-effort
        # observability — a discovered-but-unreachable or stale addr just
        # keeps polling until the timeout), then exit 0 with an info-level
        # breadcrumb and no traceback. A1-verified: T2Database was never
        # constructed, so there is nothing to tear down.
        attached = _poll_for_winner(config_dir, _LOSER_POLL_TIMEOUT)
        _log.info("t2_daemon_spawn_lost", attached=attached)
        return
    except Exception:
        # Last-resort: an exception escaping the loop (e.g. start()
        # raising before the handler is installed) must hit the log
        # file, not a DEVNULL'd stderr.
        _log.exception("t2_daemon_crashed")
        raise
