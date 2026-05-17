# SPDX-License-Identifier: AGPL-3.0-or-later
"""T2 daemon, single-writer asyncio process owning the T2 SQLite stores.

RDR-112 P1.1 (nexus-61x6): transport scaffold + dual-bind UDS+TCP.
RDR-112 P1.2 (nexus-qy0u): domain-store RPC dispatcher.
RDR-112 P1.3 (nexus-m4gm): EventStream RPC, streaming subscription.
RDR-112 P1.5 (nexus-x98k): subspace_add admin RPC + daemon-side registry.
RDR-112 P1.6 (nexus-pce1.1): admin-RPC UDS-only gate.

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

Phase 1.6 scope (nexus-pce1.1):
  - ``_ADMIN_OPS`` frozenset: forward-looking gate for ops that must only be
    callable over UDS (peer-cred-verified transport). Adding any admin op to
    the dispatch table also requires adding it to ``_ADMIN_OPS``.
  - ``is_uds`` derived once in ``_handle_connection`` and threaded to
    ``_dispatch``. Admin ops sent over TCP receive a ``PermissionDenied``
    error frame; the connection is NOT closed.
  - ``admin_ping`` op: test-scaffold only (``enable_admin_ping=True``).
    Never enabled in production. Documents the pattern for real admin ops.

Out of scope here (later beads):
  - Migration runner (P1.4 nexus-w0et)
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
import sqlite3
import struct
import sys
import traceback as _traceback_mod
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Any

import structlog

# nexus-04zd: import the ``peer`` module rather than its functions so the
# call site (`peer.read_peer_credentials(...)`) resolves through the source
# module's binding. Tests can then patch the source attribute and have it
# affect this daemon's lookup, instead of having to patch a local name
# bound at this module's import time.
from nexus.daemon import peer
from nexus.daemon.peer import PeerCredentials

_log = structlog.get_logger(__name__)


def _cockpit_bindings_disabled() -> bool:
    """Return True if ``NX_COCKPIT_BINDINGS_DISABLE`` is set to a truthy value.

    Falsy tokens (watcher runs): unset, ``""``, ``"0"``, ``"false"``, ``"False"``.
    Any other non-empty value disables the cockpit binding watcher. Mirrors
    the ``NX_BRIDGE_DISABLE`` opt-out semantics so operators have a single
    mental model for cockpit feature kill switches.
    """
    val = os.environ.get("NX_COCKPIT_BINDINGS_DISABLE", "").strip()
    return val not in ("", "0", "false", "False")


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
except Exception:  # pragma: no cover, fallback for editable / pre-install envs
    _NEXUS_VERSION = "0.0.0+unknown"

#: Maximum accepted wire-frame payload size (bytes). Guards against a malicious
#: or buggy peer announcing a multi-gigabyte length header that would otherwise
#: cause ``readexactly`` to block until OOM.
#:
#: nexus-ex4r (RDR-113 d-i-d): tightened from 16 MiB to 1 MiB. Typical T2 RPC
#: payloads (single rows, small batches, search params) are well under 64 KiB;
#: 16 MiB allowed a misbehaving same-UID peer to force the daemon to allocate
#: that buffer per connection in ``readexactly``. Bulk ops that legitimately
#: need a larger frame (introspection export, schema dumps) must page their
#: results or lift the cap on a per-RPC basis after explicit review.
_MAX_FRAME_BYTES: int = 1 * 1024 * 1024

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

#: Allowlist of dataclass qualnames that may appear inside a ``__dataclass__``
#: tag on the wire. nexus-ac2l (RDR-113 d-i-d): ``_t2_decode`` previously
#: unpacked any tagged dict to a plain dict, silently discarding the qualname.
#: A same-UID client could feed an unexpected tag to bypass a downstream
#: "looks like a dataclass payload?" check. Not exploitable today, but the
#: strict-allowlist stance is correct.
#:
#: Maintenance rule: when a new dataclass starts crossing the RPC boundary
#: (either as a return value from a non-denied dispatch op, or as an arg if
#: a future op accepts dataclass kwargs), add its bare ``__qualname__`` here.
#: Encode is permissive: anything ``dataclasses.is_dataclass(...)`` gets
#: tagged on outbound. Decode is strict: unknown tag raises ``ValueError``.
_ALLOWED_DATACLASS_TYPES: frozenset[str] = frozenset({
    # aspect_extraction_queue: returned by claim_next / claim_batch / list_pending
    "QueueRow",
    # document_aspects: methods are in _RPC_DENY_OPS today, but the encoder
    # would still tag the type if a future non-denied op returned it.
    "AspectRecord",
    # catalog.tumbler
    "Tumbler",
    "OwnerRecord",
    "DocumentRecord",
    "LinkRecord",
    # catalog.catalog
    "CatalogEntry",
    "CatalogLink",
    # catalog.catalog_writes
    "ManifestRow",
    # catalog.dedupe
    "OrphanPlan",
    "DedupePlan",
})

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
    "catalog",  # RDR-112 P2.1 (nexus-7ejx): eighth domain store
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
    # nexus-7ejx (RDR-112 P2.1): @contextmanager methods on CatalogStore.
    # Calling these as plain RPCs returns the underlying generator object,
    # not a meaningful value (the with-block never runs daemon-side).
    # Phase 4 callers needing transactional bulk load must use direct
    # store access or a purpose-built non-context-manager RPC.
    "catalog.transaction",
    "catalog.bulk_load_documents",
})

#: Ops that may ONLY be called over a UDS connection (RDR-112 P1.6 / RDR-113).
#:
#: Rationale: UDS connections have been peer-credential-verified (UID == daemon
#: UID). TCP is loopback-only but carries no per-process identity proof; admin
#: ops that mutate schema or subspace configuration must not be reachable over
#: TCP even on a single-user host.
#:
#: **Maintenance rule**: whenever a new admin op is added to the dispatch table
#: (e.g. ``subspace_add`` in nexus-x98k, ``apply_pending_migrations`` if it
#: ever becomes an explicit RPC), add it here too. The gate in ``_dispatch``
#: enforces the constraint at runtime.
#:
#: ``admin_ping`` is a test-scaffold op registered only when T2Daemon is
#: constructed with ``enable_admin_ping=True``. It exercises the UDS gate in
#: the test suite without requiring a real admin op in the dispatch table.
_ADMIN_OPS: frozenset[str] = frozenset({
    "admin_ping",                  # test-scaffold only (enable_admin_ping=True)
    "subspace_add",                # RDR-112 P1.5 nexus-x98k
    "apply_pending_migrations",    # currently NOT in dispatch table (internal to daemon start)
    "import",                      # future
    "exec_raw",                    # RDR-112 P1.6 nexus-08i1, arbitrary read-only SQL
    "export",                      # RDR-112 P1.6 nexus-08i1, daemon-side file write
})

#: Names that future beads MUST treat as admin (UDS-only) if they appear in
#: the dispatch table. Independent of ``_ADMIN_OPS`` so the startup integrity
#: check can detect "dispatch table registered the op but _ADMIN_OPS missed
#: it" without a tautology.
#:
#: When you ship a new admin RPC:
#:  1. Add it to ``_ADMIN_OPS`` above (so the gate rejects TCP requests).
#:  2. Add it here (so the startup check catches future regressions).
#:  3. Register it in the dispatch table.
#:
#: The integrity check fails loud at startup if any name here ends up in the
#: dispatch table without also being in ``_ADMIN_OPS``.
_KNOWN_ADMIN_NAMES: frozenset[str] = frozenset({
    "admin_ping",
    "subspace_add",
    "apply_pending_migrations",
    "import",
    "exec_raw",
    "export",
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

    Dataclasses are unpacked to a plain ``dict`` of their fields; the
    qualname is consulted only as an allowlist gate, not for actual
    reconstruction. Callers that need typed instances import the class
    themselves before the call. nexus-ac2l: an unknown ``__dataclass__``
    qualname raises ``ValueError`` rather than silently passing through
    (defence-in-depth against a same-UID peer probing for tag-based
    bypasses; the wire allowlist is :data:`_ALLOWED_DATACLASS_TYPES`).
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
        registry_store: Any = None,
        tuplespace_service: Any = None,
        enable_admin_ping: bool = False,
        builtin_dir: Path | None = None,
        announce_stdout: bool = False,
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
            registry_store: Optional ``RegistryStore`` instance (P1.5 nexus-x98k).
                When provided, the ``subspace_add`` admin RPC is registered in
                the dispatch table. When ``None``, ``subspace_add`` returns an
                unknown-op error. Typically constructed with
                ``RegistryStore(tuples_db_path=config_dir / "tuples.db")``.
            tuplespace_service: Optional ``TuplespaceService`` instance
                (RDR-112 nexus-6s8v). When provided, the ``tuplespace.*``
                RPCs (``out``, ``read``, ``take``, ``ack``, ``nack``,
                ``list_subspaces``, ``subspace_schema``, ``subspace_stats``)
                are registered in the dispatch table. When ``None``, those
                ops return an unknown-op error and direct-mode is the only
                way to reach the tuplespace.
            enable_admin_ping: If ``True``, register the ``admin_ping``
                test-scaffold op in the dispatch table. This op is in
                ``_ADMIN_OPS`` and is used by tests to exercise the UDS-only
                gate without requiring a real admin op. Never set this in
                production code.
        """
        self._config_dir = config_dir
        self._socket_dir: Path = config_dir / _SOCKET_SUBDIR

        # nexus-l712 (RDR-112 A2 bundle C): the stdout discovery announce
        # is now opt-in. Containerised orchestrators that capture stdout
        # for the discovery payload must pass ``announce_stdout=True``;
        # the default suppresses the PID + UDS-path + registry-digest
        # leak that would otherwise land in any shared stdout sink.
        self._announce_stdout_enabled: bool = announce_stdout

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
        # Set by stop() so callers blocked in run_until_signal() wake without
        # needing an actual SIGTERM/SIGINT. Lazily created on the first
        # run_until_signal() call so the Event is bound to the right loop.
        self._stop_event: asyncio.Event | None = None

        # P1.2 nexus-qy0u: domain-store dispatch table.
        # Keys: "<store_attr>.<method_name>"; values: bound callables.
        self._t2db = t2db
        self._rpc_table: dict[str, Any] = {}
        if t2db is not None:
            self._rpc_table = _build_dispatch_table(t2db)

        # P1.6 nexus-pce1.1: test-scaffold admin op.
        # admin_ping is in _ADMIN_OPS; registering it here lets tests exercise
        # the UDS-only gate. Production code never sets enable_admin_ping.
        if enable_admin_ping:
            self._rpc_table["admin_ping"] = lambda: {"ok": True}

        # P1.5 nexus-x98k: subspace admin RPC.
        # subspace_add is in _ADMIN_OPS (UDS-only gate enforced by _dispatch).
        # Wire contract uses kwarg ``yaml`` while the implementation takes
        # ``yaml_str`` (the implementation avoids shadowing the module-level
        # ``yaml`` import). A thin lambda bridges the two so the wire-level
        # kwarg name is stable for clients.
        self._registry_store = registry_store
        # nexus-me9y: builtin-seed dir (None -> resolved lazily in start()
        # from default_builtin_dir() when a registry_store is wired).
        self._builtin_dir: Path | None = builtin_dir
        if registry_store is not None:
            def _subspace_add_handler(yaml: str) -> dict[str, str]:  # noqa: A006
                # Add to the registry, then rewrite the discovery file so its
                # subspace_schema_digest field reflects the new state. hello_ack
                # already computes the digest fresh per handshake (live clients
                # always see the right value); the discovery rewrite is for
                # operators inspecting daemon state from disk.
                result = registry_store.add(yaml_str=yaml)
                try:
                    self._write_discovery()
                except Exception as exc:  # pragma: no cover, log + continue
                    _log.warning("discovery_rewrite_failed_after_subspace_add", error=str(exc))
                return result
            self._rpc_table["subspace_add"] = _subspace_add_handler

        # nexus-6s8v (RDR-112): tuplespace RPC suite.
        # Registers tuplespace.{out,read,take,ack,nack,list_subspaces,
        # subspace_schema,subspace_stats}. The service owns its own SQLite
        # connection to tuples.db (daemon is single writer per RDR-112 §9).
        self._tuplespace_service = tuplespace_service
        if tuplespace_service is not None:
            from nexus.daemon.tuplespace_service import register_tuplespace_rpcs
            register_tuplespace_rpcs(self._rpc_table, tuplespace_service)

        # P1.6 nexus-08i1: introspection RPCs.
        # exec_raw and export are admin-only (in _ADMIN_OPS); schema, peek,
        # stats are read-only metadata and safe over TCP.
        if t2db is not None:
            from nexus.daemon.introspection import IntrospectionService
            _intr = IntrospectionService(
                memory_db_path=t2db.path,
                tuples_db_path=(
                    tuples_db_path if tuples_db_path is not None
                    else config_dir / "tuples.db"
                ),
            )
            self._rpc_table["exec_raw"] = _intr.exec_raw
            self._rpc_table["schema"] = _intr.schema
            self._rpc_table["peek"] = _intr.peek
            self._rpc_table["stats"] = _intr.stats
            self._rpc_table["export"] = _intr.export

        # P1.6 nexus-pce1.1: startup integrity check.
        # The UDS-only gate relies on _ADMIN_OPS membership. If a future bead
        # registers an admin op in the dispatch table but forgets to add it
        # here, the op would be TCP-callable, silently regressing the gate.
        # Fail loud at startup rather than at first malicious request.
        _unprotected_admin_ops = {
            op for op in self._rpc_table
            if op in _KNOWN_ADMIN_NAMES and op not in _ADMIN_OPS
        }
        if _unprotected_admin_ops:
            raise RuntimeError(
                f"Admin ops registered in dispatch table but missing from _ADMIN_OPS: "
                f"{sorted(_unprotected_admin_ops)}. Add them to _ADMIN_OPS in t2_daemon.py "
                "to enforce the UDS-only gate."
            )

        # P1.3 nexus-m4gm: tuples.db path for EventStream subscriptions.
        self._tuples_db_path: Path | None = tuples_db_path

        # nexus-kk9h (RDR-111): tuples.db retention sweeper task handle.
        # Started in start(); cancelled in stop().
        self._retention_task: asyncio.Task | None = None  # type: ignore[type-arg]

        # nexus-r7dy: startup-sweep future (dispatched in start() via
        # run_in_executor). Held so stop() can wait for it briefly during
        # graceful shutdown without re-entering it.
        self._startup_sweep_future: asyncio.Future[Any] | None = None

        # nexus-9eiw (RDR-111 §Phase 2 Step 6): binding watcher reaction loop.
        # Constructed in start() when NX_COCKPIT_BINDINGS_DISABLE is not set
        # and at least one binding profile is loaded. Stopped in stop().
        self._binding_watcher: Any = None
        self._binding_watcher_conn: sqlite3.Connection | None = None
        self._binding_watcher_memory_conn: sqlite3.Connection | None = None

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
        # nexus-1uni (S360-log): event name follows the snake_case
        # convention used by every other event in this module / arc.
        _log.info(
            "daemon_migration_applied",
            **{"from": from_ver, "to": to_ver},
        )

        # nexus-me9y: seed the registry from nx/tuplespace/builtin/ BEFORE
        # sockets bind. Builtin schemas are the single source of truth for
        # reserved-prefix namespaces (tasks/, mailbox/, hook_events/, ...);
        # third-party subspace_add cannot mint into those prefixes. Seed
        # is idempotent across restarts -- a YAML bump becomes an UPDATE,
        # an unchanged YAML is a no-op.
        if self._registry_store is not None:
            from nexus.tuplespace.registry import default_builtin_dir  # noqa: PLC0415

            seed_dir = self._builtin_dir if self._builtin_dir is not None else default_builtin_dir()
            if seed_dir.is_dir():
                try:
                    written = self._registry_store.seed_from_builtin_dir(seed_dir)
                    # nexus-1uni (S360-log): canonical snake_case.
                    _log.info(
                        "daemon_builtin_seed_completed",
                        builtin_dir=str(seed_dir),
                        rows_written=written,
                    )
                except Exception as exc:
                    _log.error(
                        "daemon_builtin_seed_failed",
                        builtin_dir=str(seed_dir),
                        error=str(exc),
                    )
                    raise
            else:
                _log.warning(
                    "daemon_builtin_seed_skipped",
                    reason="builtin_dir_missing",
                    builtin_dir=str(seed_dir),
                )

        uds_sock = self._bind_uds()
        tcp_sock = self._bind_tcp()

        # asyncio servers from pre-bound sockets. nexus-y6fk: each transport
        # gets its own handler closure that carries an explicit ``is_uds``
        # tag, so the admin-RPC gate downstream does not depend on re-
        # deriving the transport family from ``transport.get_extra_info``
        # (which can return None for future ssl/proxy wrappers).
        self._uds_server = await asyncio.start_unix_server(
            self._make_handler(is_uds=True), sock=uds_sock
        )
        self._tcp_server = await asyncio.start_server(
            self._make_handler(is_uds=False), sock=tcp_sock
        )

        self._write_discovery()
        if self._announce_stdout_enabled:
            self._announce_stdout()

        # nexus-kk9h: run one retention sweep at startup and schedule a
        # recurring 6-hour sweep. Done after sockets bind so clients can
        # connect immediately. nexus-r7dy: dispatch the startup sweep via
        # ``run_in_executor`` and do not await it; on a ``tuples.db`` with
        # weeks of expired rows the DELETE can block socket-bind completion
        # for seconds-to-minutes. The future is held only so the daemon
        # can surface failures in the scheduled-loop's warning path.
        loop = asyncio.get_running_loop()
        self._startup_sweep_future = loop.run_in_executor(
            None, self._run_retention_sweep_sync
        )
        # nexus-71kc (S360-async S3): attach a done-callback so a sweep
        # exception surfaces in the operator log immediately rather
        # than dying silently at Future-GC time. ``stop()`` also awaits
        # the future before closing the SQLite connection (nexus-abhy
        # S360-conc S2 dedup) to prevent the DELETE from racing
        # conn.close().
        def _log_startup_sweep_result(fut: Any) -> None:
            exc = fut.exception() if not fut.cancelled() else None
            if exc is not None:
                _log.warning(
                    "startup_sweep_failed",
                    exc=str(exc),
                    exc_type=type(exc).__qualname__,
                )

        self._startup_sweep_future.add_done_callback(
            _log_startup_sweep_result
        )
        self._retention_task = asyncio.create_task(self._retention_loop())

        # nexus-9eiw (RDR-111 §Phase 2 Step 6): start the binding watcher
        # unless NX_COCKPIT_BINDINGS_DISABLE is truthy. The watcher polls
        # the events table and fires loaded profiles with action_idempotency
        # dedup (nexus-8wvs) on the memory.db connection it owns.
        if _cockpit_bindings_disabled():
            _log.info("binding_watcher_skipped_disable_env")
        else:
            self._start_binding_watcher(memory_db_path, tuples_db_path)

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
        if self._stop_event is not None:
            self._stop_event.set()

        # nexus-kk9h: cancel the retention sweeper before tearing down servers.
        if self._retention_task is not None:
            self._retention_task.cancel()
            try:
                await self._retention_task
            except (asyncio.CancelledError, Exception):
                pass
            self._retention_task = None

        # nexus-abhy / nexus-71kc (S360-conc S2 + S360-async S3):
        # drain the startup retention sweep so a still-running DELETE
        # does not race the SQLite close below. Bounded so a wedged
        # sweep cannot stall shutdown forever; the done-callback
        # already wired during start() logs any failure.
        if self._startup_sweep_future is not None:
            try:
                await asyncio.wait_for(
                    asyncio.wrap_future(self._startup_sweep_future),
                    timeout=30.0,
                )
            except asyncio.TimeoutError:
                _log.warning("startup_sweep_timeout_on_stop")
            except Exception as exc:
                _log.warning(
                    "startup_sweep_failed_on_stop",
                    exc=str(exc),
                    exc_type=type(exc).__qualname__,
                )
            self._startup_sweep_future = None

        # nexus-9eiw: signal the binding watcher to exit and close its
        # SQLite connections. Bounded by the watcher's own stop_timeout.
        if self._binding_watcher is not None:
            try:
                await self._binding_watcher.stop()
            except Exception as exc:  # pragma: no cover, defensive
                _log.warning("binding_watcher_stop_failed", error=str(exc))
            self._binding_watcher = None
        if self._binding_watcher_conn is not None:
            try:
                self._binding_watcher_conn.close()
            except sqlite3.Error:
                pass
            self._binding_watcher_conn = None
        if self._binding_watcher_memory_conn is not None:
            try:
                self._binding_watcher_memory_conn.close()
            except sqlite3.Error:
                pass
            self._binding_watcher_memory_conn = None

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

        # nexus-6s8v: release tuplespace SQLite connection.
        if self._tuplespace_service is not None:
            try:
                self._tuplespace_service.close()
            except Exception:  # pragma: no cover, defensive
                _log.warning("tuplespace_service_close_failed", exc_info=True)

        self._unlink_discovery()
        self._release_spawn_lock()

        _log.info("t2_daemon_stopped")

    async def run_until_signal(self) -> None:
        """Block until SIGTERM or SIGINT, then perform graceful shutdown.

        Registers asyncio signal handlers so the event loop drives shutdown
        rather than Python's synchronous signal module.
        """
        if self._stop_event is None:
            self._stop_event = asyncio.Event()
        stop_event = self._stop_event
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

    def _make_handler(self, *, is_uds: bool):
        """Return the asyncio stream handler coroutine for one transport.

        nexus-y6fk: the ``is_uds`` tag is captured at handler-construction
        time (one handler per transport) rather than derived from the
        connection's ``transport.get_extra_info("socket")`` inside the
        request loop. Re-derivation was structurally one line away from a
        silent regression: a future asyncio backend that returned ``None``
        from ``get_extra_info`` (or wrapped the UDS in a proxy stream
        whose socket family was not ``AF_UNIX``) would have flipped
        ``is_uds`` to ``False``, opening admin RPCs to TCP callers.
        """

        async def _handler(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            task = asyncio.current_task()
            if task is not None:
                self._active_handlers.add(task)
            try:
                await self._handle_connection(reader, writer, is_uds=is_uds)
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
        *,
        is_uds: bool,
    ) -> None:
        """Process a single client connection.

        Protocol:
          1. Check peer-cred on UDS (reject foreign UIDs per RDR-113).
          2. Read hello frame; validate protocol_version.
          3. Respond hello_ack.
          4. Serve RPCs until client closes or daemon is stopping.

        Args:
            is_uds: True if this connection arrived on the UDS server,
                False for TCP. Threaded from ``_make_handler`` rather than
                derived from the transport so the admin-RPC gate is
                immune to future asyncio backend changes (nexus-y6fk).
        """
        transport = writer.transport
        raw_sock: socket.socket | None = transport.get_extra_info("socket")

        # --- Peer-cred check (UDS only) ---
        if is_uds:
            # Defense-in-depth: the closure tagged this handler as UDS,
            # so the underlying transport must be an AF_UNIX socket. If
            # asyncio surprises us with a missing or non-UNIX socket we
            # fail closed (no RPC dispatch) rather than fall through and
            # open admin ops to an unauthenticated peer.
            if raw_sock is None or raw_sock.family != socket.AF_UNIX:
                _log.error(
                    "uds_handler_transport_mismatch",
                    raw_sock_family=(raw_sock.family if raw_sock is not None else None),
                )
                write_frame(
                    writer,
                    {"error": "internal: UDS handler received non-UDS transport"},
                )
                await writer.drain()
                return
            try:
                creds: PeerCredentials = peer.read_peer_credentials(raw_sock)
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

        registry_digest: str | None = (
            self._registry_store.digest()
            if self._registry_store is not None
            else None
        )
        write_frame(
            writer,
            {
                "op": "hello_ack",
                "daemon_protocol_version": DAEMON_PROTOCOL_VERSION,
                "daemon_version": _NEXUS_VERSION,
                "schema_version": DAEMON_SCHEMA_VERSION,
                "registry_digest": registry_digest,
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

            response = await self._dispatch(msg, is_uds=is_uds)
            write_frame(writer, response)
            await writer.drain()

    async def _dispatch(
        self, msg: dict[str, Any], *, is_uds: bool = False
    ) -> dict[str, Any]:
        """Dispatch a single RPC message and return the response frame.

        Phase 1.1 ops:
          - ping -> pong with version + start_time

        Phase 1.2 ops (nexus-qy0u):
          - ``{op: "<store>.<method>", args: {...}}`` -> dispatched to the
            registered domain-store method. The method runs in a thread-pool
            executor (stores are synchronous). Return value is wrapped in
            ``{result: <t2_encoded>}``; exceptions in
            ``{error: {type, message, traceback}}``.

        Phase 1.6 ops (nexus-pce1.1):
          - Admin ops listed in ``_ADMIN_OPS`` are rejected over TCP with a
            ``PermissionDenied`` error frame. The connection stays open.

        Unknown ops return an error frame (not an exception) so the
        connection remains open.

        Args:
            msg: Decoded RPC message from the client.
            is_uds: ``True`` when the connection arrived over a UDS socket
                (``AF_UNIX``). Admin ops require ``is_uds=True``.
        """
        op = msg.get("op")

        # --- Admin-RPC gate (P1.6 nexus-pce1.1) ---
        # Admin ops in _ADMIN_OPS require a UDS connection. TCP connections
        # have no per-process identity proof; even loopback TCP must not reach
        # admin ops. Returning an error frame (not raising) keeps the
        # connection open so the client can issue non-admin RPCs.
        if op in _ADMIN_OPS and not is_uds:
            _log.warning(
                "admin_op_rejected_over_tcp",
                op=op,
            )
            return {
                "error": {
                    "type": "PermissionDenied",
                    "message": (
                        f"admin op {op!r} requires UDS transport; "
                        "TCP connections cannot invoke admin ops"
                    ),
                }
            }

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
            case str() if op in self._rpc_table:
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
    # Binding watcher (nexus-9eiw, RDR-111 §Phase 2 Step 6)
    # ------------------------------------------------------------------

    def _start_binding_watcher(
        self, memory_db_path: Path, tuples_db_path: Path
    ) -> None:
        """Construct and start the cockpit binding watcher.

        Loads profiles from the default profiles directory and opens
        dedicated SQLite connections to both ``tuples.db`` (for the
        events poll) and ``memory.db`` (for the ``action_idempotency``
        dedup gate). When no profiles are loaded the watcher is not
        constructed, the loop would have nothing to do.
        """
        from nexus.cockpit.bindings import (  # noqa: PLC0415
            BindingContext,
            BindingWatcher,
            default_profiles_dir,
            load_profiles_dir,
            user_profiles_dir,
        )

        # nexus-7lb9: scan BOTH the shipped builtin dir and the user dir
        # so operator CRUD writes under ~/.config/nexus/bindings/profiles
        # land in the same watcher. The watcher's mtime-poll reload picks
        # up changes to either dir without a daemon restart.
        builtin_dir = default_profiles_dir()
        user_dir = user_profiles_dir()
        candidate_dirs = [
            d for d in (builtin_dir, user_dir) if d.is_dir()
        ]
        if not candidate_dirs:
            _log.info(
                "binding_watcher_no_profiles_dir",
                builtin=str(builtin_dir),
                user=str(user_dir),
            )
            return
        try:
            profiles: list = []
            for d in candidate_dirs:
                profiles.extend(load_profiles_dir(d))
        except Exception as exc:  # pragma: no cover, defensive
            _log.warning(
                "binding_watcher_profile_load_failed",
                dirs=[str(d) for d in candidate_dirs],
                error=str(exc),
            )
            return
        if not profiles:
            _log.info(
                "binding_watcher_no_profiles",
                dirs=[str(d) for d in candidate_dirs],
            )
            return

        # Dedicated connections so the watcher can run on the event loop
        # thread without contending with the daemon's writer.
        tuples_conn = sqlite3.connect(
            str(tuples_db_path), check_same_thread=False
        )
        tuples_conn.row_factory = sqlite3.Row
        memory_conn = sqlite3.connect(
            str(memory_db_path), check_same_thread=False
        )
        memory_conn.row_factory = sqlite3.Row

        # Wire the tuplespace index + registry from the running service
        # so python actions can write derived tuples via the same backend.
        # nexus-qggv (S360-mod): use the public accessors instead of
        # reaching into the sibling service's private attributes.
        if self._tuplespace_service is not None:
            index = self._tuplespace_service.tuple_index()
            registry = self._tuplespace_service.registry()
        else:
            index = None
            registry = None

        context = BindingContext(
            conn=tuples_conn,
            index=index,
            registry=registry,
            memory_conn=memory_conn,
        )
        watcher = BindingWatcher(
            conn=tuples_conn,
            profiles=profiles,
            context=context,
            profiles_dirs=candidate_dirs,
        )
        self._binding_watcher = watcher
        self._binding_watcher_conn = tuples_conn
        self._binding_watcher_memory_conn = memory_conn
        watcher.start()
        _log.info(
            "binding_watcher_scheduled",
            profile_count=len(profiles),
        )

    # ------------------------------------------------------------------
    # Retention sweeper (nexus-kk9h, RDR-111)
    # ------------------------------------------------------------------

    #: Interval between retention sweeps. 6 hours per the bead brief.
    _RETENTION_SWEEP_INTERVAL_SECONDS: int = 6 * 3600

    def _run_retention_sweep_sync(self) -> int:
        """Run one tuples.db retention sweep synchronously.

        nexus-dc9a: when a TuplespaceService is wired in at construction
        time (production path), the sweep passes its TupleIndex to
        ``prune_expired_tuples`` so paired Chroma vectors are deleted
        alongside the SQLite rows. Without the index, orphan Chroma
        vectors accumulate and resurface in semantic ``read()`` /
        ``take()`` calls after their SQLite row has been deleted (the
        impact grew with write volume and TTL usage). Test-construction
        of the daemon without a TuplespaceService still works: the sweep
        falls back to SQLite-only and logs a one-time warning.

        Also sweeps expired ``action_idempotency`` rows from
        ``memory.db`` (nexus-8wvs). Both sweeps are best-effort, a
        failure on either side logs and continues.
        """
        if self._tuples_db_path is None:
            return 0
        from nexus.db.migrations import sweep_action_idempotency  # noqa: PLC0415
        from nexus.tuplespace.store import (  # noqa: PLC0415
            prune_expired_tuples,
            prune_old_events,
        )

        # Resolve the TupleIndex from the wired TuplespaceService when
        # available. SQLite-only sweep is retained as a safety fallback
        # so retention still runs even on a daemon built without the
        # service (early-test construction path).
        # nexus-qggv (S360-mod): public accessor; no more reach-through.
        index = (
            self._tuplespace_service.tuple_index()
            if self._tuplespace_service is not None
            else None
        )

        # Short-lived connections so we don't contend with the daemon's
        # main writer for long (WAL allows concurrent readers; the DELETE
        # acquires the writer lock briefly).
        conn = sqlite3.connect(str(self._tuples_db_path))
        try:
            deleted = prune_expired_tuples(conn, index=index)
            # nexus-anjo: also prune the events table so its monotonic
            # growth cannot pessimise the binding watcher and EventStream
            # backfill query. Failure here is logged but does not abort
            # the tuples-prune result (we still want the count back).
            try:
                events_deleted = prune_old_events(conn)
                if events_deleted:
                    _log.info(
                        "events_retention_swept_daemon",
                        deleted=events_deleted,
                    )
            except sqlite3.Error as exc:
                _log.warning(
                    "events_retention_sweep_failed",
                    error=str(exc),
                )
        finally:
            conn.close()

        memory_db_path = self._config_dir / "memory.db"
        if memory_db_path.exists():
            mem_conn = sqlite3.connect(str(memory_db_path))
            try:
                idem_deleted = sweep_action_idempotency(mem_conn)
                if idem_deleted:
                    _log.info(
                        "action_idempotency_sweep",
                        deleted=idem_deleted,
                    )
            except sqlite3.Error as exc:
                _log.warning(
                    "action_idempotency_sweep_failed",
                    error=str(exc),
                )
            finally:
                mem_conn.close()

        return deleted

    async def _retention_loop(self) -> None:
        """Recurring 6-hour retention sweep. Cancelled on daemon stop."""
        try:
            while not self._stopping:
                try:
                    await asyncio.sleep(self._RETENTION_SWEEP_INTERVAL_SECONDS)
                except asyncio.CancelledError:
                    raise
                if self._stopping:
                    return
                try:
                    loop = asyncio.get_running_loop()
                    deleted = await loop.run_in_executor(
                        None, self._run_retention_sweep_sync
                    )
                    _log.info("retention_sweep_completed", deleted=deleted)
                except Exception as exc:  # pragma: no cover, defensive
                    _log.warning("retention_sweep_failed", error=str(exc))
        except asyncio.CancelledError:
            return

    # ------------------------------------------------------------------
    # Discovery file + stdout announce
    # ------------------------------------------------------------------

    def _discovery_payload(self) -> dict[str, Any]:
        registry_digest: str | None = (
            self._registry_store.digest()
            if self._registry_store is not None
            else None
        )
        return {
            "uds_path": str(self._uds_path),
            "tcp_host": self._tcp_host,
            "tcp_port": self._tcp_port,
            "daemon_version": _NEXUS_VERSION,
            "daemon_protocol_version": DAEMON_PROTOCOL_VERSION,
            "pid": os.getpid(),
            "start_time": self._start_time,
            "subspace_schema_digest": registry_digest,
        }

    def _write_discovery(self) -> None:
        payload = self._discovery_payload()
        # Atomic write: a reader polling the discovery file between open() and
        # the completed write() must never observe partial JSON.
        tmp = self._discovery_path.with_suffix(self._discovery_path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload))
        os.replace(str(tmp), str(self._discovery_path))

    def _unlink_discovery(self) -> None:
        """Best-effort discovery-file removal at shutdown.

        nexus-12gb: stamp a shutdown marker into the file BEFORE attempting
        unlink, then retry the unlink once on ``OSError``. Combined with the
        ``find_t2_daemon()`` PID-liveness probe (nexus-j6dj), this closes the
        stale-discovery race: a reader that arrives while the daemon is
        shutting down sees ``status == "shutting_down"`` in the JSON even if
        the unlink itself transiently fails (NFS hiccups, read-only mount,
        permission flake) and skips the file rather than routing to the
        dying PID.

        Never raises: shutdown cleanup cannot abort the rest of the stop
        sequence (handlers waiting on graceful drain, lock release, etc.).
        """
        # Step 1: stamp a shutdown marker. Best-effort: failure to write the
        # marker (e.g. EROFS, permission denied) is logged but does not
        # block the unlink attempt.
        #
        # nexus-3tl3.3 (SR-3, 2026-05-17): catch ``Exception`` not just
        # ``OSError``. ``_discovery_payload()`` reaches into
        # ``self._registry_store.digest()`` which calls ``sqlite3.connect``
        # internally; ``sqlite3.OperationalError`` is NOT an OSError
        # subclass, so the prior catch let it propagate and violated the
        # "never raises" contract advertised in this docstring.
        try:
            marker_payload = self._discovery_payload()
            marker_payload["status"] = "shutting_down"
            marker_payload["shutdown_at"] = datetime.now(timezone.utc).isoformat()
            # nexus-bkvg (FS-1): atomic write via tmp + os.replace
            # mirrors ``_write_discovery`` above. A crash mid-write
            # with the prior in-place ``write_text`` left a truncated
            # marker file that ``find_t2_daemon`` could not parse.
            tmp = self._discovery_path.with_suffix(
                self._discovery_path.suffix + ".tmp"
            )
            tmp.write_text(json.dumps(marker_payload))
            os.replace(str(tmp), str(self._discovery_path))
        except Exception as exc:
            _log.warning(
                "discovery_marker_write_failed",
                path=str(self._discovery_path),
                exc=str(exc),
                exc_type=type(exc).__qualname__,
            )

        # Step 2: unlink with one retry. NFS-style transient errors usually
        # clear on the second attempt; permanent errors (EROFS, EPERM) fall
        # through to the final warning log.
        for attempt in (1, 2):
            try:
                self._discovery_path.unlink(missing_ok=True)
                return
            except OSError as exc:
                if attempt == 1:
                    _log.info(
                        "discovery_unlink_retry",
                        path=str(self._discovery_path),
                        exc=str(exc),
                    )
                    continue
                _log.warning(
                    "discovery_unlink_failed",
                    path=str(self._discovery_path),
                    exc=str(exc),
                )

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
            RuntimeError: another daemon already holds the lock, OR the
                process is running on native Windows (nexus-dl3g: out of
                v1 scope; use the TCP fallback from a Linux or macOS
                host).
        """
        if sys.platform == "win32":
            # nexus-dl3g: the previous implementation logged a warning and
            # returned without acquiring the lock, allowing two daemons to
            # start concurrently on Windows. ``fcntl`` is unavailable on
            # Windows and the equivalent ``msvcrt.locking()`` carries
            # different invariants (record-level, not file-level). Native
            # Windows is explicitly out of v1 scope (RDR-112 §Host-Trust
            # Model). Refuse hard so operators can't end up with a
            # double-bound daemon.
            raise RuntimeError(
                "nx daemon t2 start is not supported natively on Windows. "
                "Run the T2 daemon on a Linux or macOS host and connect "
                "Windows-VM clients via the TCP fallback transport."
            )

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
