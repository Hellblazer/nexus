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
import contextlib
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
import threading
import time
import traceback
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
_DISPATCH_RETRY_SLEEPS: tuple[float, ...] = (0.05, 0.1, 0.2, 0.4, 0.8)

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

# nexus-we61e: cadence + staleness window for the daemon-owned aspect-queue
# reclaim loop. The interval preserves the worker's prior ~30s effective
# cadence (poll_interval=2s × _RECLAIM_EVERY_N_POLLS=15); the staleness
# window matches the worker's old ``stale_timeout_seconds`` default so a
# crashed worker's claimed row is recovered on the same timescale as before.
_ASPECT_RECLAIM_INTERVAL: float = 30.0
_ASPECT_RECLAIM_STALE_TIMEOUT_S: int = 60
_LOSER_POLL_TIMEOUT: float = 3.0

# nexus-64w50: a spawn-lock loser that polls and finds NO reachable winner
# (``attached=False``) may have collided with an incumbent that is mid-exit
# inside the RDR-129 defer-release-to-exit drain window: the incumbent
# early-unlinks its discovery file but holds the spawn lock until the
# process actually exits. A one-shot loser that quit here left ZERO daemons
# (the incumbent finishes exiting; nothing replaced it — the live-2026-06-05
# orphan). So on ``attached=False`` we retry the spawn a bounded number of
# times; once the incumbent's process exits the OS frees the lock and our
# retry wins it. Single-writer is preserved because ``_acquire_spawn_lock``
# is non-blocking (LOCK_NB): a retry can only win when the lock is genuinely
# free, and otherwise loses again and is counted toward the bound. The
# window (≈ MAX × (poll + backoff) ≈ 6 × (3 + 2) = 30 s) covers the
# incumbent's worst-case drain. The two principal legs are HARD-bounded in
# stop(): socket teardown (_GRACEFUL_STOP_TIMEOUT 5 s, nexus-saigj) + DB
# close (_DB_CLOSE_TIMEOUT 10 s) ≈ 15 s, leaving comfortable margin. (The
# catalog handle close is a plain conn.close() with no WAL checkpoint —
# negligible, not separately timeout-wrapped.) Even if a
# retry budget is exhausted it is not a correctness failure: the loser exits
# 0 and launchd / ensure-running re-spawns. An ``attached=True`` poll exits
# immediately — a real serving winner is never disturbed, and the in-process
# retry is invisible to the ensure-running crash-loop guard (which counts
# cold spawns, not these LOCK_NB losses).
_SPAWN_LOST_RETRY_MAX: int = 6
_SPAWN_LOST_RETRY_BACKOFF: float = 2.0


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

#: RDR-151 P1.2 (nexus-5haam): idle deadline on an accepted connection between
#: frames. A peer that connects and then goes silent (or half-closes without an
#: RST the OS surfaces) otherwise holds the accepted socket — and its fd — open
#: indefinitely. This is a candidate-independent backstop to the P1.1 peg fix:
#: even if some future code path re-introduces a registered idle fd, a silent
#: connection is reaped within the deadline rather than lingering. Generous
#: enough that a normal client's inter-request think time never trips it (the
#: RPC client connects, calls, and closes promptly); finite so a dead-but-not-
#: RST peer cannot pin the connection forever.
_IDLE_READ_TIMEOUT: float = 300.0

#: RDR-151 nexus-u2vmv: cause-agnostic event-loop spin guard. If the loop polls
#: the selector for an immediate ready-return at a sustained rate above the
#: threshold, the daemon is spinning (~100% CPU) — capture and self-heal rather
#: than peg a core forever. Threshold default 10000/s with headroom above the
#: *measured* peaks of legitimate churn (~5110/s graceful memory.put, ~6872/s
#: abrupt-RST churn per the RDR-151 P2.1 synthesis — those are throughput, not a
#: peg); the historical peg sustained far higher. Env-tunable.
#:
#: NOTE: this guard is T2-specific by construction — it instruments the T2
#: daemon's asyncio SelectorEventLoop. The T3 daemon (run_t3_supervisor) is a
#: SYNCHRONOUS subprocess supervisor with no asyncio loop/selector, so the
#: shared-primitive lifecycle rule's selector-spin concern does not apply there
#: (verified 2026-06-06: t3_daemon.py has no asyncio.run / event loop).
_SPIN_THRESHOLD_PER_S: float = float(os.environ.get("NX_T2_SPIN_THRESHOLD", "10000"))
_SPIN_WINDOW_S: float = 1.0
_SPIN_CONSECUTIVE: int = 5  # ~5s sustained before declaring a spin
_SPIN_HARD_EXIT_TIMEOUT: float = 10.0  # graceful SIGTERM grace before os._exit
_SPIN_EXIT_CODE: int = 99
#: Bound self-heal restarts so a PERSISTENT trigger (e.g. a skewed client that
#: re-induces the spin on every fresh daemon) can never drive the supervisor's
#: crash-loop guard to permanent suppression (a suppressed daemon serves
#: nothing — strictly worse than a pegged-but-serving one). After this many
#: spin-heals within the window, the daemon DISARMS self-heal and stays up
#: pegged-but-serving (== the pre-guard baseline; never worse) while logging a
#: loud, actionable ERROR. nexus-u2vmv critic C2.
_SPIN_HEAL_MAX: int = 2
_SPIN_HEAL_WINDOW_S: float = 600.0


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


def _pause_reading(transport: asyncio.BaseTransport | None) -> bool:
    """Remove the accepted connection's read fd from the event-loop selector for
    the duration of a dispatch. Returns True iff reading was actually paused (so
    the caller knows to resume). RDR-151 P1.1 (nexus-th4dh).

    ``pause_reading`` raises ``RuntimeError`` if the transport is already paused
    or closing, and may be absent on non-selector transports; both are benign
    here and mean "nothing to pause".
    """
    try:
        transport.pause_reading()  # type: ignore[union-attr]
        return True
    except (AttributeError, RuntimeError, NotImplementedError):
        return False


def _resume_reading(transport: asyncio.BaseTransport | None) -> None:
    """Re-register the accepted connection's read fd after a dispatch. The
    inverse of :func:`_pause_reading`; tolerant of a transport that has since
    started closing. RDR-151 P1.1 (nexus-th4dh)."""
    try:
        transport.resume_reading()  # type: ignore[union-attr]
    except (AttributeError, RuntimeError, NotImplementedError):
        pass


def _abort_transport(transport: asyncio.BaseTransport | None) -> None:
    """Synchronously and immediately tear a connection down, guaranteeing its
    read fd is removed from the event-loop selector. RDR-151 P1.1 (nexus-th4dh).

    The graceful ``writer.close()`` + ``await writer.wait_closed()`` path can
    stall or error on a half-dead peer (observed live: ``BrokenPipeError`` from
    ``wait_closed``'s final ``sendmsg``), and a swallowed error there leaves the
    transport half-torn-down with the accepted socket's READ fd still registered
    and — because the peer is gone — perpetually reported-ready. That stuck fd is
    the sustained 100% CPU selector spin. ``transport.abort()`` drops both
    directions at once (no flush, no await) and calls ``_remove_reader``
    immediately, so the fd cannot linger in the selector. The response has
    already been drained in the dispatch loop before we reach teardown, so
    abandoning any residual buffer here loses nothing.
    """
    try:
        transport.abort()  # type: ignore[union-attr]
    except (AttributeError, RuntimeError, NotImplementedError, OSError):
        pass


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

#: RDR-151 P2.1a (nexus-gcu07): op-prefixes whose dispatch is serialised through
#: ``_catalog_write_lock``. ``catalog_write.*`` (RDR-146) plus ``taxonomy.*`` —
#: the latter's writes (``persist_assignments`` / ``assign_topic`` / ...) were the
#: live-captured peg site, racing N threads at the SQLite write lock. The whole
#: ``taxonomy.`` prefix is included (safe-by-default) so a future taxonomy writer
#: cannot silently bypass serialisation. ``str.startswith`` accepts this tuple.
_WRITE_SERIALIZED_PREFIXES: tuple[str, ...] = (_CATALOG_WRITE_PREFIX, "taxonomy.")

#: RDR-151 nexus-xmohw: every mutating dispatch op must serialise through the
#: daemon's single write lock. The peg root cause (issue #1137) is intra-daemon
#: WAL write contention: a burst of un-serialised ``memory.*`` writes plus the
#: internal reclaim loop each launch parallel ``to_thread`` writers racing the
#: one SQLite writer lock → SQLITE_BUSY → tight retry → selector spin / 100% CPU.
#: 2.1a serialised only ``catalog_write.*`` + ``taxonomy.*``; this extends it to
#: ALL writers so the daemon is a genuine single serialised writer (RDR-129 B3 /
#: Datasette write-queue model): one write at a time, no intra-daemon contention,
#: no busy-spin. READS are intentionally NOT here — WAL allows concurrent readers,
#: and serialising reads (esp. ``database.hello``) behind writes would re-create
#: the slow-hello → ensure-running-declares-stale → takeover-churn cascade.
#:
#: ``tests/daemon/test_rdr151_xmohw_write_serialization.py::test_write_op_coverage``
#: is the forcing function: it verb-matches every dispatchable store method and
#: fails if a mutating op is not serialised here — closing the silent-recurrence
#: gap (a future writer added without serialisation).
_WRITE_OPS: frozenset[str] = frozenset({
    # memory store
    "memory.put", "memory.delete", "memory.expire", "memory.merge_memories",
    "memory.put_or_merge", "memory.flag_stale_memories",
    # plan library
    "plans.save_plan", "plans.delete_plan", "plans.set_plan_disabled",
    "plans.set_plan_enabled", "plans.set_scope_tags",
    "plans.increment_match_metrics", "plans.increment_run_started",
    "plans.increment_run_outcome",
    # chash index
    "chash_index.upsert", "chash_index.upsert_many",
    "chash_index.delete_collection", "chash_index.delete_stale",
    "chash_index.rename_collection",
    # telemetry
    "telemetry.log_relevance", "telemetry.log_relevance_batch",
    "telemetry.expire_relevance_log", "telemetry.log_search_batch",
    "telemetry.trim_search_telemetry", "telemetry.rename_collection",
    # document aspects (upsert/get are RPC-denied; these are the dispatchable writes)
    "document_aspects.set_salient_sentences",
    "document_aspects.set_salient_sentences_by_key",
    "document_aspects.delete", "document_aspects.delete_orphans",
    "document_aspects.rename_collection",
    # aspect extraction queue
    "aspect_queue.enqueue", "aspect_queue.claim_next", "aspect_queue.claim_batch",
    "aspect_queue.mark_done", "aspect_queue.mark_failed", "aspect_queue.mark_retry",
    "aspect_queue.reclaim_stale", "aspect_queue.rename_collection",
    # T2Database top-level write methods (dispatched as ``database.*``)
    "database.rename_collection_cascade", "database.expire",
    "database.complete_aspect",
    # catalog read-store commit (flushes pending writes); catalog mutations
    # proper go through the catalog_write.* prefix.
    "catalog.commit",
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
    # nexus-xmohw: ``aspect_queue.reclaim_stale`` is daemon-owned since
    # nexus-we61e — ``T2Daemon._reclaim_stale_loop`` calls it DIRECTLY on
    # ``t2db`` (not through this table), exactly once per interval. No
    # current worker RPCs it (aspect_worker.py deliberately stopped after
    # we61e), but version-skewed workers from before we61e (<=5.10.0) still
    # do on every poll. Honouring each as a real full-table UPDATE+commit
    # floods the SQLite write lock and pegs the daemon at ~100% CPU, which
    # makes it slow to answer ``hello()`` -> ensure-running declares it
    # stale and spawns a replacement -> takeover churn -> 2+ daemons
    # contend on memory.db -> ``nx memory put`` hard-fails with
    # ``database is locked``. Neutralise the flood at the RPC boundary:
    # the client-facing entry is a cheap no-op returning 0 that never
    # touches the DB. The daemon's own loop is unaffected (it bypasses
    # this table), so legitimate reclaim still happens.
    # The override must run AFTER the store-enumeration loop above (which
    # registers the real bound method); the membership check is the guard
    # for that ordering. If reclaim_stale is ever legitimately removed from
    # the client table (e.g. added to _RPC_DENY_OPS), absence is also safe —
    # a denied op raises unknown-op, which is likewise a cheap no-DB path, so
    # the flood still cannot peg the daemon. The dispatch-table test asserts
    # the entry is present and is the no-op, so a silent skip fails CI.
    if "aspect_queue.reclaim_stale" in table:
        # warn-once-per-daemon-instance: the no-op silences the T2-write-
        # failure symptom, so an operator would otherwise have NO signal that
        # version-skewed workers are still present and need restarting. Emit
        # one WARNING (visible at INFO-level production logging) the first
        # time a stale client RPCs reclaim; stay at DEBUG thereafter so a
        # 1800/hr-per-worker flood does not spam the log (reviewer: critic S1).
        _warned = [False]

        def _reclaim_stale_rpc_noop(*_args: Any, **_kwargs: Any) -> int:
            if not _warned[0]:
                _warned[0] = True
                _log.warning(
                    "t2_daemon_reclaim_stale_rpc_ignored",
                    hint=(
                        "a version-skewed (<=5.10.0) nx-mcp worker is RPC'ing "
                        "aspect_queue.reclaim_stale; reclaim is daemon-owned "
                        "since we61e and this RPC is a no-op — restart stale "
                        "nx-mcp processes to clear the source (nexus-xmohw)"
                    ),
                )
            else:
                _log.debug("t2_daemon_reclaim_stale_rpc_noop")
            return 0

        table["aspect_queue.reclaim_stale"] = _reclaim_stale_rpc_noop
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
        # nexus-we61e: the daemon owns aspect-queue stale-row reclamation.
        # Reclaim was previously run from every per-process aspect worker,
        # so N workers RPC'd N redundant reclaim UPDATEs into this one
        # daemon — the WAL contention that pegged a core on `database is
        # locked` after a restart with a stale-row backlog. Running it on
        # the daemon's own loop makes it singular by construction.
        self._reclaim_task: asyncio.Task[None] | None = None

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
        # Two flocks, two concerns (do NOT conflate when migrating T3/T1):
        # the spawn-lock acquired above is the RDR-128 single-writer
        # guarantee and IS T2's election (exactly one daemon opens the WAL).
        # The primitive's per-scope election flock (t2_elect.<uid>.lock,
        # taken briefly inside publish/heartbeat) does NOT replace it; it
        # only serializes the generation read-increment-write so the fencing
        # token is monotonic. Both are required. For T1 (no spawn-lock,
        # session-scoped) the primitive's flock IS the whole election.
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

        # nexus-we61e: the daemon owns aspect-queue stale-row reclaim.
        self._reclaim_task = asyncio.create_task(self._reclaim_stale_loop())

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
                #
                # RDR-151 P1.4 (nexus-tjgl2): offload to a thread. heartbeat_tick
                # takes a blocking ``fcntl.flock(LOCK_EX)`` on the per-scope
                # election lock (service_registry.py); running it inline on the
                # event-loop thread stalls the whole loop for the duration of any
                # lock contention (Gap 4). A stall that exceeds the lease TTL can
                # even get a live primary falsely fenced (RF-2). to_thread keeps
                # the loop serving while the blocking lock is acquired.
                await asyncio.to_thread(supervisor.heartbeat_tick)
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

    async def _reclaim_stale_loop(self) -> None:
        """Periodically reclaim stale ``in_progress`` aspect-queue rows.

        nexus-we61e: this is the SOLE caller of ``reclaim_stale`` in
        production. It previously ran inside every per-process aspect
        worker's poll loop; with N nx-mcp processes that meant N
        redundant reclaim UPDATEs RPC'd into this one daemon, the
        WAL-lock contention that pegged a core after a restart with a
        stale-row backlog. The daemon is singular by construction
        (spawn lock + lease), so running reclaim here runs it exactly
        once per interval regardless of how many workers are polling.

        Calls ``reclaim_stale`` directly on the hosted T2Database — no
        RPC, no cross-process contention; it serialises only against the
        daemon's own dispatch writes through the store's internal lock.
        Best-effort: a transient ``database is locked`` (already retried
        with backoff inside ``reclaim_stale``) is logged and the loop
        continues. Cancelled in :meth:`stop`.

        nexus-nhqll: reclaim runs FIRST, then sleeps — so a freshly
        (re)started daemon clears any stale-row backlog left by a prior
        worker death at once, rather than waiting a full interval. This
        is the recovery path for the post-restart backlog case.

        Residual gap (accepted): while a daemon is crash-loop-suppressed,
        workers still run and claim/complete rows via the direct-write
        fallback, so a worker crash there can strand an ``in_progress``
        row with no reclaimer (reclaim is daemon-only). We do NOT add a
        worker-side reclaim for it: that would reintroduce the N-fold WAL
        contention RDR-128 was filed to close, to cover a transient
        degraded state the crash-loop guard or an operator resolves — and
        the moment any daemon comes back up, this loop's reclaim-first
        entry clears the backlog immediately.
        """
        while True:
            t2db = self._t2db
            if t2db is not None:
                try:
                    reclaimed = await self._reclaim_stale_once(t2db)
                    if reclaimed:
                        _log.info(
                            "t2_daemon_aspect_reclaim", reclaimed=reclaimed
                        )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    # Best-effort janitor; never let it crash the daemon.
                    _log.warning(
                        "t2_daemon_aspect_reclaim_failed", exc=str(exc)
                    )
            await asyncio.sleep(_ASPECT_RECLAIM_INTERVAL)

    async def _reclaim_stale_once(self, t2db: Any) -> int:
        """Run one aspect-queue stale-reclaim pass — RDR-151 nexus-xmohw: the
        daemon's OWN reclaim writer serialises through the SAME write lock as
        serve-path writes (issue #1137 mechanism 2: the 30s reclaim loop raced a
        ``memory.delete`` burst on the WAL writer lock → SQLITE_BUSY → retry
        spin). Under the lock there is one writer at a time, so no contention.
        The lock is created in ``start()``; the pre-start window falls back to a
        direct write (no other writer exists yet)."""
        lock = self._catalog_write_lock
        if lock is not None:
            async with lock:
                return await asyncio.to_thread(
                    t2db.aspect_queue.reclaim_stale, _ASPECT_RECLAIM_STALE_TIMEOUT_S
                )
        return await asyncio.to_thread(
            t2db.aspect_queue.reclaim_stale, _ASPECT_RECLAIM_STALE_TIMEOUT_S
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

        # RDR-151 P1.3 (nexus-yd6fy): publish the shutdown marker FIRST, before
        # the (potentially slow) server drain and DB close below, so discoverers
        # stop resolving us the instant stop() begins rather than during the
        # whole teardown window. A clean shutdown thus hands off immediately
        # instead of leaving a healthy-looking record that resolves to a daemon
        # that is already draining (the TTL-expiry wait is for crashes only).
        # The reassert task is cancelled above first; a heartbeat thread it had
        # already dispatched to the pool (RDR-151 P1.4) may still complete after
        # this mark, but ServiceRegistry.heartbeat preserves a non-"live" status,
        # so a late tick cannot resurrect the record back to healthy.
        # mark_shutting_down lives in the shared registry primitive
        # (service_registry.py) per the RDR-149 lifecycle invariant; stop() only
        # orchestrates the call.
        if self._registry is not None and self._lease_record is not None:
            with contextlib.suppress(Exception):
                self._registry.mark_shutting_down(self._lease_record)

        # nexus-we61e: stop the daemon-owned reclaim loop. Order is not
        # load-bearing (it touches only the aspect queue, not discovery),
        # but cancel before the T2Database close below so an in-flight
        # reclaim cannot race the handle teardown.
        if self._reclaim_task is not None:
            self._reclaim_task.cancel()
            try:
                await self._reclaim_task
            except BaseException:  # noqa: BLE001
                pass
            self._reclaim_task = None

        # nexus-saigj: bound socket teardown. ``wait_closed()`` blocks until
        # every open connection drains; a connection holding a long in-flight
        # RPC at SIGTERM could otherwise extend the drain (and thus the
        # spawn-lock hold) without limit. Cap each with _GRACEFUL_STOP_TIMEOUT
        # (the same defense-in-depth pattern as the _DB_CLOSE_TIMEOUT'd close
        # below); on timeout the server is already closed to new connections,
        # so we log and proceed — the OS reaps the rest at process exit.
        for name, server in (
            ("uds", self._uds_server),
            ("tcp", self._tcp_server),
        ):
            if server is None:
                continue
            server.close()
            try:
                await asyncio.wait_for(
                    server.wait_closed(), timeout=_GRACEFUL_STOP_TIMEOUT
                )
            except TimeoutError:
                _log.warning(
                    "t2_daemon_socket_close_timeout",
                    server=name,
                    timeout_s=_GRACEFUL_STOP_TIMEOUT,
                )
        self._uds_server = None
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
        transport = writer.transport
        try:
            while True:
                try:
                    # RDR-151 P1.2 (nexus-5haam): bound the idle wait between
                    # frames so a silent peer cannot hold the accepted socket
                    # (and its fd) open indefinitely. On timeout, treat it as a
                    # closed client and tear the connection down.
                    frame = await asyncio.wait_for(
                        read_frame(reader), timeout=_IDLE_READ_TIMEOUT
                    )
                except (asyncio.IncompleteReadError, TimeoutError):
                    break  # client closed or went idle past the deadline
                except (ProtocolError, json.JSONDecodeError) as exc:
                    self._send_error(writer, None, "protocol", str(exc))
                    await writer.drain()
                    break

                request_id = frame.get("request_id")
                # RDR-151 P1.1 (nexus-th4dh): connection-teardown hardening.
                # Pause reading on the accepted connection for the dispatch
                # window so its read fd is not left registered in the selector
                # while the handler is parked in ``to_thread``. The RPC protocol
                # is strictly synchronous (the client blocks on the response
                # before sending its next frame), so declining to read here
                # drops nothing.
                #
                # NOTE (RF-1 REFUTED, 2026-06-05): this was originally believed
                # to be the fix for the 100% CPU peg. A live capture
                # (T2 memory nexus/rdr151-LIVE-MECHANISM-CAPTURED-2026-06-05)
                # proved the peg is NOT a leaked accepted fd — zero accepted fds
                # are registered during the peg — but a select-spin while an
                # executor thread is wedged on a contended catalog write. Kept as
                # defensible teardown hygiene, NOT the peg fix.
                paused = _pause_reading(transport)
                try:
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
                    try:
                        await writer.drain()
                    except (ConnectionError, OSError) as exc:
                        # RDR-151 Gap 2 (nexus-th4dh): the peer died during
                        # dispatch and is gone by the time we flush the
                        # response. Necessary hygiene to suppress the
                        # uncaught-ConnectionResetError traceback flood (RC-6,
                        # 404k observed live); it is NOT the peg fix (the pause
                        # above is). Close the connection cleanly and stop.
                        _log.debug(
                            "t2_daemon_peer_gone_on_response",
                            op=frame.get("op"),
                            error=str(exc),
                        )
                        break
                finally:
                    if paused:
                        _resume_reading(transport)
        finally:
            # RDR-151 P1.1 (nexus-th4dh): abort rather than graceful-close. A
            # graceful ``wait_closed()`` on a half-dead peer can stall or raise
            # (BrokenPipeError) and leave the read fd registered + perpetually
            # ready — the sustained selector spin. abort() removes the fd from
            # the selector synchronously. The response was already drained above.
            _abort_transport(transport)

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
        #
        # RDR-151 P2.1a (nexus-gcu07): the same serialisation MUST cover the
        # ``taxonomy.*`` ops. The 100% CPU peg (captured live 2026-06-05) was N
        # concurrent ``taxonomy.persist_assignments`` RPCs each launching a
        # parallel ``to_thread`` writer, all racing for the single SQLite write
        # lock; one wedged on the contended lock spun the event loop. Serialising
        # the taxonomy prefix here makes those writers cooperative (one at a time,
        # yielding the loop between them) instead of a thread pile-up. The whole
        # prefix is serialised (not an enumerated write-op set) so a future
        # taxonomy writer is covered by default — a missed writer is exactly the
        # silent-recurrence class this fix closes. Taxonomy reads serialising
        # behind a write is acceptable: they are not a hot daemon read path.
        # Lock hold-time is bounded to one write attempt (backoff happens outside
        # the lock in _invoke_serialized_with_retry).
        if (
            (op in _WRITE_OPS or op.startswith(_WRITE_SERIALIZED_PREFIXES))
            and self._catalog_write_lock is not None
        ):
            return await self._invoke_serialized_with_retry(
                callable_, op, args, kwargs
            )
        return await self._invoke_with_lock_retry(callable_, op, args, kwargs)

    async def _invoke_serialized_with_retry(
        self, callable_: Any, op: str, args: list[Any], kwargs: dict[str, Any],
    ) -> Any:
        """Serialised write path (RDR-151 nexus-xmohw): hold the daemon write
        lock for ONE write attempt, then release it BEFORE the backoff sleep, so
        a write waiting on a cross-process writer never blocks the daemon's other
        writes for the whole retry budget (critic C2 head-of-line bound). Intra-
        daemon contention is gone by construction (one writer at a time); the
        retry only fires against an out-of-daemon writer."""
        lock = self._catalog_write_lock
        assert lock is not None  # caller checked
        sleeps = _DISPATCH_RETRY_SLEEPS
        max_attempts = len(sleeps) + 1
        for attempt in range(1, max_attempts + 1):
            async with lock:
                try:
                    return await asyncio.to_thread(callable_, *args, **kwargs)
                except sqlite3.OperationalError as exc:
                    if not _is_locked_error(exc) or attempt == max_attempts:
                        raise
                    _log.warning(
                        "t2_daemon_dispatch_lock_retry",
                        op=op, attempt=attempt, exc=str(exc),
                    )
            await asyncio.sleep(sleeps[attempt - 1])  # backoff OUTSIDE the lock

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
            # RDR-149 P2: the addr token may be a lease record (pid +
            # connection fields under ``endpoint``, version under
            # ``version``). Normalize to the legacy-shaped flat view WITHOUT
            # a freshness filter (reap must inspect even a stale predecessor)
            # so the pid-extraction, version-aware spare, and health-ping
            # below all keep working across the upgrade window.
            from nexus.daemon.discovery import normalize_discovery_view

            addr_payload = normalize_discovery_view(payload)
            pid = addr_payload.get("pid")
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


def _spin_heal_count_in_window(config_dir: Path, now: float) -> int:
    """Append *now* to the persistent spin-heal log and return the number of
    heals within ``_SPIN_HEAL_WINDOW_S`` (including this one). Best-effort: on
    any IO error returns 1 (treat as first heal — self-heal allowed)."""
    path = config_dir / ".t2_spin_heals"
    cutoff = now - _SPIN_HEAL_WINDOW_S
    stamps: list[float] = []
    try:
        if path.exists():
            for line in path.read_text().splitlines():
                try:
                    ts = float(line.strip())
                except ValueError:
                    continue
                if ts >= cutoff:
                    stamps.append(ts)
        stamps.append(now)
        path.write_text("\n".join(f"{ts:.3f}" for ts in stamps) + "\n")
    except Exception:  # noqa: BLE001
        return 1
    return len(stamps)


def _t2_spin_capture_and_heal(
    config_dir: Path, loop: Any, info: dict[str, Any]
) -> None:
    """RDR-151 nexus-u2vmv: on a detected event-loop spin, capture ground-truth
    diagnostics (the missing-until-now selector + stack + loop._ready dump) and
    self-heal — UNLESS self-heal has fired too often, in which case stay up
    pegged-but-serving (never worse than the pre-guard baseline; critic C2).

    Runs on the watchdog thread (scheduled even under a spinning loop — CPython
    releases the GIL on the switch interval). Self-heal is SIGTERM → graceful
    stop (the spinning loop still iterates, so the signal self-pipe callback
    fires promptly); a hard ``os._exit`` DAEMON timer guarantees the process
    dies even if graceful stop wedges, so the supervisor respawns a clean
    daemon (a fresh daemon does not spin)."""
    pid = os.getpid()
    frames = [
        f"--- thread {tid} ---\n" + "".join(traceback.format_stack(fr))
        for tid, fr in sys._current_frames().items()
    ]
    # loop._ready distinguishes a perpetually-ready-fd spin (empty ready queue)
    # from a self-rescheduling call_soon spin (ready queue full) — the diagnostic
    # dimension sys._current_frames() alone cannot show (critic C4).
    ready = getattr(loop, "_ready", None)
    ready_len = len(ready) if ready is not None else None
    ready_sample = [repr(h) for h in list(ready or [])[:20]]

    heals = _spin_heal_count_in_window(config_dir, time.time())
    disarm = heals > _SPIN_HEAL_MAX

    _log.error(
        "t2_daemon_spin_detected",
        pid=pid,
        hot_fd=info.get("hot_fd"),
        rate_per_s=info.get("rate_per_s"),
        ready_fd_hits=info.get("ready_fd_hits"),
        threshold_per_s=info.get("threshold_per_s"),
        loop_ready_len=ready_len,
        heals_in_window=heals,
        action="serve_degraded" if disarm else "self_heal",
        hint=(
            "event loop spinning ~100% CPU; "
            + (
                "self-heal disarmed (persistent trigger — a stale/skewed client "
                "is likely present); staying up pegged-but-serving. Restart "
                "stale nx-mcp/desktop clients."
                if disarm
                else "capturing + self-healing (RDR-151 u2vmv)"
            )
        ),
    )
    try:
        logs = config_dir / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        path = logs / f"t2_spin_{pid}_{int(time.time())}.txt"
        path.write_text(
            f"hot_fd={info.get('hot_fd')} rate_per_s={info.get('rate_per_s')} "
            f"threshold_per_s={info.get('threshold_per_s')} "
            f"loop_ready_len={ready_len} heals_in_window={heals} disarm={disarm}\n"
            f"ready_fd_hits={info.get('ready_fd_hits')}\n"
            f"loop._ready sample:\n  " + "\n  ".join(ready_sample) + "\n\n"
            + "\n".join(frames)
        )
        _log.error("t2_daemon_spin_capture_written", path=str(path))
    except Exception:  # noqa: BLE001 — capture is best-effort; heal regardless
        pass

    if disarm:
        # Never make it worse than baseline: a persistent trigger would otherwise
        # drive 5 spin-restarts into the supervisor crash-loop guard and leave
        # the daemon permanently suppressed (serving nothing). Stay up.
        return

    # Hard-exit fallback first, so a wedged graceful stop cannot leave the daemon
    # pegged. DAEMON thread so a normal (sub-timeout) graceful stop exits cleanly
    # with code 0 and the timer is reaped silently — without daemon=True the
    # interpreter would join it and force exit 99 on every fire (critic CRITICAL).
    timer = threading.Timer(_SPIN_HARD_EXIT_TIMEOUT, lambda: os._exit(_SPIN_EXIT_CODE))
    timer.daemon = True
    timer.start()
    try:
        os.kill(pid, signal.SIGTERM)
    except Exception:  # noqa: BLE001
        os._exit(_SPIN_EXIT_CODE)


def _run_main_spin_guarded(
    main_factory: "Callable[[], Any]", config_dir: Path
) -> None:
    """Run *main_factory()* on a loop whose selector is spin-instrumented, with a
    watchdog that captures + self-heals on a sustained spin (RDR-151 u2vmv).
    Mirrors ``asyncio.run`` (set loop, run, cancel pending, shutdown asyncgens,
    close)."""
    from nexus.daemon.spin_guard import SpinGuardSelector, SpinWatchdog

    sel = SpinGuardSelector()
    loop = asyncio.SelectorEventLoop(sel)
    watchdog = SpinWatchdog(
        sel,
        threshold_per_s=_SPIN_THRESHOLD_PER_S,
        window_s=_SPIN_WINDOW_S,
        consecutive=_SPIN_CONSECUTIVE,
        on_spin=lambda spin_info: _t2_spin_capture_and_heal(
            config_dir, loop, spin_info
        ),
    )
    try:
        asyncio.set_event_loop(loop)
        watchdog.start(loop)
        loop.run_until_complete(main_factory())
    finally:
        watchdog.stop()
        try:
            # Safety net matching asyncio.run: cancel any task _main did not
            # await, then drain, so nothing leaks / warns.
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True)
                )
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception:  # noqa: BLE001
            pass
        asyncio.set_event_loop(None)
        loop.close()


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

    for attempt in range(1, _SPAWN_LOST_RETRY_MAX + 1):
        try:
            _run_main_spin_guarded(_main, config_dir)
            return
        except T2SpawnLockLost:
            # RDR-140 P1.3 (nexus-h2oko): losing the spawn lock is a benign
            # quiet-attach, NOT a crash. A live winner already owns the data
            # file; poll briefly for its discovery file/socket (best-effort
            # observability — a discovered-but-unreachable or stale addr just
            # keeps polling until the timeout). A1-verified: T2Database was
            # never constructed, so there is nothing to tear down.
            attached = _poll_for_winner(config_dir, _LOSER_POLL_TIMEOUT)
            if attached:
                # A real winner is serving — never disturb it. Exit 0.
                _log.info("t2_daemon_spawn_lost", attached=True)
                return
            # nexus-64w50: no reachable winner. The incumbent is likely
            # mid-exit in the defer-release-to-exit drain window (lock still
            # held, discovery file already unlinked). Retry so the freed
            # lock is re-acquired by us, rather than quit and leave zero
            # daemons. _acquire_spawn_lock is LOCK_NB, so a retry that still
            # collides simply loses again and counts toward the bound.
            if attempt < _SPAWN_LOST_RETRY_MAX:
                _log.info(
                    "t2_daemon_spawn_lost_retry",
                    attempt=attempt,
                    max_attempts=_SPAWN_LOST_RETRY_MAX,
                )
                time.sleep(_SPAWN_LOST_RETRY_BACKOFF)
                continue
            # Exhausted the bound without acquiring or finding a winner.
            _log.info("t2_daemon_spawn_lost", attached=False)
            return
        except Exception:
            # Last-resort: an exception escaping the loop (e.g. start()
            # raising before the handler is installed) must hit the log
            # file, not a DEVNULL'd stderr.
            _log.exception("t2_daemon_crashed")
            raise
