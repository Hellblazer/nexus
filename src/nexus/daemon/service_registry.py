# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-149: the leased / fenced / atomic service-registry substrate.

ONE pure, deterministic, tier-agnostic primitive for ephemeral local
service lifecycle. T1, T2 and T3 each migrate onto it (RDR-149 P2-P5),
parameterized only by a scope key (uid for T2/T3, session-id for T1) and
a tier file prefix. No tier-specific code lives here.

The primitive replaces three divergent bespoke implementations (pid
sweeps, PPID walks, per-tier election) with one mechanism whose parts
subsume the per-tier features that drifted apart:

- **Lease, not PID.** Identity is a server-unique ``owner_token``
  (uuid4 per owner instance); liveness is TTL freshness on a wall-clock
  heartbeat stamp. A dead owner's lease simply ages out past ``ttl`` no
  matter what the kernel does with its pid -> pid-reuse immunity for
  free, and "process alive" is no longer conflated with "endpoint live".
- **Heartbeat == self-heal == reap.** The owner re-stamps the lease every
  ``heartbeat_interval``; that same re-stamp re-creates a transiently
  lost record (RF-1, the RDR-140 re-assert), and a reader treats an
  expired lease as absent and unlinks it (orphan reap).
- **Monotonic generation fencing.** Each publish bumps a per-scope
  ``generation`` counter under the election flock (read-increment-write,
  RF-3). A stale lower-generation owner can neither overwrite nor unlink
  a newer higher-generation owner's record (CA-4). The counter lives
  inside the record, so it survives restarts with no clock dependency.
- **Atomic publish.** Every write is temp-file + ``os.replace`` so a
  concurrent reader sees either the old or the new record, never a torn
  one.
- **Scope-keyed election.** A per-scope ``fcntl.flock`` serializes the
  generation read-increment-write, so concurrent siblings converge to
  exactly one owner per scope with strictly increasing generations.

The TTL/heartbeat defaults reuse the RDR-140 T2 constants
(``heartbeat_interval`` = ``_REASSERT_INTERVAL`` = 1.0,
``ttl`` = ``_LOSER_POLL_TIMEOUT`` = 3.0); the constructor enforces the
RF-1 invariant ``ttl >= heartbeat_interval`` so a discoverer's poll
window can never straddle a mid-heartbeat gap.

Determinism: the wall-clock used for the lease stamp is injectable
(``clock``), mirroring ``T2Daemon._monotonic``; the supervisor exposes a
synchronous ``heartbeat_tick`` so tests drive cadence with a fixed clock
and never sleep.
"""
from __future__ import annotations

import contextlib
import errno
import fcntl
import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Optional, Protocol

import structlog

_log = structlog.get_logger(__name__)

# RF-1: substrate defaults reuse the RDR-140 T2 lifecycle constants.
DEFAULT_HEARTBEAT_INTERVAL: float = 1.0
DEFAULT_TTL: float = 3.0

#: Per-tier lease-TTL overrides (nexus-lz3f2). The per-tier TTL is a SUBSTRATE
#: parameter, so it lives here in the shared primitive — not in any tier's daemon
#: module (RDR-149: "no tier-specific lifecycle code outside the substrate"). The
#: 3s default fits the light T1/T2 daemons; the storage-service supervisor's
#: heartbeat tick can take up to its /health probe timeout + the heartbeat
#: interval (~3s), grazing a 3s TTL, so it gets a wider 15s window (~15 missed
#: beats) — a transient stall never false-expires a LIVE service's lease, while a
#: genuinely dead supervisor is still reaped within 15s. Discoverers honour the
#: TTL stamped in the record, so this needs setting only where each tier
#: publishes. Consumers MUST resolve via ``ttl_for_tier`` so the conformance
#: suite and every publisher track one source of truth.
TIER_TTLS: dict[str, float] = {"storage_service": 15.0}


def ttl_for_tier(tier: str) -> float:
    """Lease TTL for *tier* — the per-tier override, else the substrate default."""
    return TIER_TTLS.get(tier, DEFAULT_TTL)


_FORMAT_VERSION: int = 1

Clock = Callable[[], float]


class ServiceRegistryError(RuntimeError):
    """Base error for the service-registry substrate."""


class StaleOwnerError(ServiceRegistryError):
    """Raised when an owner tries to heartbeat a lease that a newer
    (higher-generation) or different owner now holds. The caller has been
    fenced and must stop; it must not re-create or overwrite the record.
    """


def mint_owner_token() -> str:
    """A server-unique owner identity. Never a pid (pid-reuse immunity)."""
    return uuid.uuid4().hex


@dataclass(frozen=True)
class LeaseRecord:
    """One owner's lease over a scope. Serialized to the discovery file.

    ``generation`` is the fencing token; ``owner_token`` is the identity;
    ``heartbeat_epoch`` is the wall-clock liveness stamp checked against
    ``ttl``. ``endpoint`` / ``version`` / ``payload`` carry the
    tier-specific connection details (the registry never interprets
    them).
    """

    scope_key: str
    generation: int
    owner_token: str
    heartbeat_epoch: float
    ttl: float
    endpoint: dict[str, Any]
    version: str
    payload: dict[str, Any] = field(default_factory=dict)
    status: str = "live"
    format_version: int = _FORMAT_VERSION

    def is_fresh(self, now: float) -> bool:
        """Live iff status is ``live`` and the lease has not aged past TTL."""
        if self.status != "live":
            return False
        return (now - self.heartbeat_epoch) < self.ttl

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> "LeaseRecord":
        data = json.loads(text)
        return cls(
            scope_key=data["scope_key"],
            generation=int(data["generation"]),
            owner_token=data["owner_token"],
            heartbeat_epoch=float(data["heartbeat_epoch"]),
            ttl=float(data["ttl"]),
            endpoint=dict(data["endpoint"]),
            version=str(data["version"]),
            payload=dict(data.get("payload", {})),
            status=str(data.get("status", "live")),
            format_version=int(data.get("format_version", _FORMAT_VERSION)),
        )


class ServiceRegistry:
    """File-backed leased registry, parameterized by tier prefix + scope.

    One instance serves any number of scopes within a tier; per-call
    ``scope_key`` selects the record + election lock. All mutating
    operations take the per-scope flock for the duration of their
    read-modify-write so generation bumps are serialized across
    processes.
    """

    def __init__(
        self,
        *,
        dir: Path,
        tier: str,
        clock: Clock = time.time,
        ttl: float = DEFAULT_TTL,
        heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL,
    ) -> None:
        if ttl < heartbeat_interval:
            raise ValueError(
                f"ttl ({ttl}) must be >= heartbeat_interval "
                f"({heartbeat_interval}) (RF-1: a discoverer's poll window "
                f"must not straddle a mid-heartbeat gap)"
            )
        self._dir = dir
        self._tier = tier
        self._clock = clock
        self._ttl = ttl
        self._heartbeat_interval = heartbeat_interval

    # -- paths --------------------------------------------------------------

    @property
    def clock(self) -> Clock:
        return self._clock

    @property
    def heartbeat_interval(self) -> float:
        return self._heartbeat_interval

    def _record_path(self, scope_key: str) -> Path:
        return self._dir / f"{self._tier}_addr.{scope_key}"

    def _election_path(self, scope_key: str) -> Path:
        return self._dir / f"{self._tier}_elect.{scope_key}.lock"

    def _ensure_dir(self) -> None:
        self._dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    # -- election -----------------------------------------------------------

    @contextlib.contextmanager
    def _elect(self, scope_key: str) -> Iterator[None]:
        """Hold the per-scope election flock for a read-modify-write.

        Blocking ``LOCK_EX``: the critical section (read current record,
        increment generation, atomic write) is short, and a publisher
        must wait its turn rather than fail, so concurrent siblings
        serialize into strictly increasing generations.
        """
        self._ensure_dir()
        path = self._election_path(scope_key)
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    # -- atomic IO ----------------------------------------------------------

    def _read_record(self, scope_key: str) -> Optional[LeaseRecord]:
        path = self._record_path(scope_key)
        try:
            text = path.read_text()
        except OSError:
            return None
        try:
            return LeaseRecord.from_json(text)
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            _log.warning(
                "service_registry_corrupt_record", path=str(path), error=str(exc)
            )
            return None

    def _write_record_atomic(self, record: LeaseRecord) -> None:
        self._ensure_dir()
        path = self._record_path(record.scope_key)
        tmp = path.with_suffix(path.suffix + f".{os.getpid()}.{uuid.uuid4().hex}.tmp")
        fd = os.open(str(tmp), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        try:
            try:
                os.write(fd, record.to_json().encode("utf-8"))
            finally:
                os.close(fd)
            os.replace(str(tmp), str(path))
        except BaseException:
            with contextlib.suppress(OSError):
                tmp.unlink()
            raise

    # -- publish / heartbeat / discover / relinquish ------------------------

    def publish(
        self,
        scope_key: str,
        *,
        endpoint: dict[str, Any],
        version: str,
        owner_token: str,
        payload: Optional[dict[str, Any]] = None,
    ) -> LeaseRecord:
        """Claim (or re-claim) ``scope_key``, bumping the generation.

        Under the election flock: read the current record, set the new
        generation to ``current.generation + 1`` (or 1 if none), and
        atomically write the new lease stamped at the current clock. The
        winner of a concurrent race is the last to enter the critical
        section and therefore carries the highest generation.
        """
        with self._elect(scope_key):
            current = self._read_record(scope_key)
            generation = (current.generation + 1) if current is not None else 1
            record = LeaseRecord(
                scope_key=scope_key,
                generation=generation,
                owner_token=owner_token,
                heartbeat_epoch=self._clock(),
                ttl=self._ttl,
                endpoint=dict(endpoint),
                version=version,
                payload=dict(payload or {}),
            )
            self._write_record_atomic(record)
            return record

    def heartbeat(self, record: LeaseRecord) -> LeaseRecord:
        """Re-stamp ``record``'s lease, preserving generation + identity.

        Self-heal (RF-1): if the record was transiently lost, re-create
        it at the SAME generation. Fencing (CA-4): if a newer owner has
        taken the scope (higher generation, or the same generation under
        a different ``owner_token``), raise ``StaleOwnerError`` and write
        nothing.
        """
        with self._elect(record.scope_key):
            current = self._read_record(record.scope_key)
            if current is not None:
                if current.generation > record.generation:
                    raise StaleOwnerError(
                        f"scope {record.scope_key!r} fenced: a generation "
                        f"{current.generation} owner superseded generation "
                        f"{record.generation}"
                    )
                if current.owner_token != record.owner_token:
                    raise StaleOwnerError(
                        f"scope {record.scope_key!r} owned by a different "
                        f"token at generation {current.generation}"
                    )
            # RDR-151 P1.3/P1.4 (nexus-yd6fy): preserve a non-"live" status (e.g.
            # ``shutting_down``) already published for this scope. A heartbeat
            # defaults a fresh record to ``status="live"``; without this, a late
            # heartbeat — notably the now-threaded ``to_thread(heartbeat_tick)``
            # that may still be blocked on the election flock when ``stop()``
            # cancels its driver and publishes the shutdown marker — would
            # resurrect a shutting-down record back to live and re-expose a
            # daemon that is already tearing down. We only re-stamp the
            # heartbeat freshness; we never upgrade status back to live here.
            status = current.status if (
                current is not None and current.status != "live"
            ) else "live"
            refreshed = LeaseRecord(
                scope_key=record.scope_key,
                generation=record.generation,
                owner_token=record.owner_token,
                heartbeat_epoch=self._clock(),
                ttl=self._ttl,
                endpoint=dict(record.endpoint),
                version=record.version,
                payload=dict(record.payload),
                status=status,
            )
            self._write_record_atomic(refreshed)
            return refreshed

    def discover(self, scope_key: str) -> Optional[LeaseRecord]:
        """Resolve the live owner of ``scope_key``, or ``None``.

        Returns ``None`` for a missing, expired (TTL), or shutdown-marked
        record. An expired record is best-effort reaped so the next
        lookup is fast. No pid is consulted at this level: liveness here is
        purely lease freshness. The T2 client-side resolver (discovery.py
        ``_resolve_lease_record``) adds process-liveness checks on top of the
        heartbeat-age check for the T2 tier (nexus-md90p): a stale-but-answering
        UDS rescue and a dead-pid fast-path. The invariant "liveness is purely
        lease freshness" applies to this registry layer only.
        """
        record = self._read_record(scope_key)
        if record is None:
            return None
        if not record.is_fresh(self._clock()):
            self._reap_if_still_stale(record)
            return None
        return record

    def _reap_if_still_stale(self, stale: LeaseRecord) -> None:
        """Reap an expired record, but only under the election flock and only
        if the SAME record is still present and still stale (nexus-2mpns).

        The naive ``unlink`` after an unguarded ``is_fresh`` check is a TOCTOU:
        a concurrent ``publish``/``heartbeat`` can take the election flock and
        ``os.replace`` a fresh, higher-generation live record into the window
        between the freshness check and the unlink — the blind unlink would then
        delete the *successor's* just-published live record by path. Mirror
        ``relinquish``: re-read under the lock and only unlink when the record we
        still see is the same stale lease (owner_token match) AND is still not
        fresh. If a successor has published, leave it alone — it stays a
        resolvable endpoint with no transient gap.
        """
        with self._elect(stale.scope_key):
            current = self._read_record(stale.scope_key)
            if current is None:
                return
            if current.owner_token != stale.owner_token:
                return  # a successor owns it now; not ours to reap
            if current.is_fresh(self._clock()):
                return  # re-stamped fresh under the lock; leave the live record
            with contextlib.suppress(OSError):
                self._record_path(stale.scope_key).unlink()

    def mark_shutting_down(self, record: LeaseRecord) -> None:
        """Publish a shutdown marker so discoverers stop resolving us
        immediately, before the record is unlinked."""
        with self._elect(record.scope_key):
            current = self._read_record(record.scope_key)
            if current is None or current.owner_token != record.owner_token:
                return
            marker = LeaseRecord(
                scope_key=current.scope_key,
                generation=current.generation,
                owner_token=current.owner_token,
                heartbeat_epoch=current.heartbeat_epoch,
                ttl=current.ttl,
                endpoint=dict(current.endpoint),
                version=current.version,
                payload=dict(current.payload),
                status="shutting_down",
            )
            self._write_record_atomic(marker)

    def relinquish(self, record: LeaseRecord) -> None:
        """Release ``scope_key`` on graceful shutdown, but only if we still
        own it. A delayed shutdown from a fenced predecessor must not
        unlink a successor's record (CA-4).

        nexus-ycwec GAP C: also removes the per-scope elect lock file after
        releasing the flock so clean shutdowns leave no ``t1_elect.*.lock``
        orphan. The unlink only fires when the addr record belongs to us
        (owner_token match); a fenced predecessor leaves the successor's
        lock intact. The lock is unlinked OUTSIDE the election context
        (after the flock is released).  A process that opens the path
        AFTER the unlink gets a fresh inode and starts a new election.
        A process that already holds the old-inode fd still acquires
        LOCK_EX (the kernel inode survives until all fds close), but any
        write it attempts is caught by generation fencing: the os.replace
        + generation counter in ``_write_record_atomic`` will raise
        StaleOwnerError on the mismatched generation.  Inode freshness is
        therefore NOT the safety mechanism -- generation fencing is.
        """
        _we_owned = False
        with self._elect(record.scope_key):
            current = self._read_record(record.scope_key)
            if current is None:
                return
            if current.owner_token != record.owner_token:
                return  # a successor owns it now; leave it alone
            with contextlib.suppress(OSError):
                self._record_path(record.scope_key).unlink()
            _we_owned = True
        # Unlink the elect lock only when WE owned the scope.  Done AFTER the
        # flock is released.  Openers after the unlink get a fresh inode;
        # openers holding the old inode's fd are harmless -- generation fencing
        # (not inode freshness) is the correctness mechanism.
        if _we_owned:
            with contextlib.suppress(OSError):
                self._election_path(record.scope_key).unlink()


def sweep_dead_t1_holders(
    *,
    config_dir: "Path",
) -> int:
    """Remove stale T1 lease records whose owner processes are all dead.

    Scans all ``t1_addr.*`` files in *config_dir* and removes any record
    where BOTH the ``claude_pid`` (payload) AND the ``server_pid``
    (payload/endpoint) are not alive. A record is left alone when either
    pid is alive OR when ``claude_pid`` is absent (old/transient format;
    insufficient info to verify fully).

    CRITICAL SAFETY: a record is NEVER removed if ANY of the following:
    - ``claude_pid`` is missing from the payload (cannot verify fully)
    - ``claude_pid`` is alive
    - ``server_pid`` is alive

    Returns the exact count of records removed. Best-effort; errors on
    individual records are logged and skipped. Caller must hold no election
    flocks (this function takes its own under the shared primitive's rules).

    This is the startup-sweep half of the nexus-ycwec Fix #3 lifecycle GC.
    Session-end cleanup is :meth:`T1LeasePublisher.relinquish` (already in
    t1_lease.py). Both halves live here or in the shared primitive per the
    RDR-149 standing gate (daemon/AGENTS.md).

    TOCTOU NOTE: the liveness-check → unlink sequence is a point-in-time
    race.  OS PID reuse could in principle allow a newly started process to
    inherit the same PID as the dead owner, causing the sweep to skip removal
    (safe) or, if the PID wraps after the check, to unlink an addr file that
    a different same-scope session just published.  The latter is bounded by
    heartbeat self-heal: the owning MCP re-publishes its addr within one
    heartbeat interval (~1 s); the in-process ``T1_ADDR`` state is unaffected
    so the owner continues to function.  The only observable consequence is a
    brief sibling-discovery miss -- never data loss.  The sweep intentionally
    omits the election flock to avoid deadlock with live publishers.
    """
    from nexus.session import _is_pid_alive  # noqa: PLC0415 - branch-local; avoids circular at module import

    config_path = Path(config_dir)
    if not config_path.exists():
        return 0

    removed = 0
    for addr_file in config_path.glob("t1_addr.*"):
        if not addr_file.is_file():
            continue
        try:
            text = addr_file.read_text(encoding="utf-8")
            record = LeaseRecord.from_json(text)
        except (OSError, json.JSONDecodeError, KeyError, ValueError):
            _log.debug(
                "sweep_dead_t1_holders_skip_unreadable",
                path=str(addr_file),
            )
            continue

        payload = record.payload
        # Safety gate: must have claude_pid to verify the holder.
        claude_pid_raw = payload.get("claude_pid")
        if claude_pid_raw is None:
            _log.debug(
                "sweep_dead_t1_holders_skip_no_claude_pid",
                scope=record.scope_key,
                path=str(addr_file),
            )
            continue

        try:
            claude_pid = int(claude_pid_raw)
        except (TypeError, ValueError):
            continue  # malformed payload — skip

        # server_pid can live in payload or endpoint.
        server_pid_raw = payload.get("server_pid") or record.endpoint.get("server_pid")
        if server_pid_raw is None:
            continue  # no server_pid — skip (insufficient info)

        try:
            server_pid = int(server_pid_raw)
        except (TypeError, ValueError):
            continue

        # SAFETY: only remove when BOTH pids are dead.
        claude_alive = _is_pid_alive(claude_pid)
        server_alive = _is_pid_alive(server_pid)
        if claude_alive or server_alive:
            _log.debug(
                "sweep_dead_t1_holders_skip_live",
                scope=record.scope_key,
                claude_pid=claude_pid,
                server_pid=server_pid,
                claude_alive=claude_alive,
                server_alive=server_alive,
            )
            continue

        # Both pids dead — remove the addr file.
        with contextlib.suppress(OSError):
            addr_file.unlink()

        if not addr_file.exists():
            removed += 1
            _log.info(
                "sweep_dead_t1_holders_removed",
                scope=record.scope_key,
                claude_pid=claude_pid,
                server_pid=server_pid,
            )

    return removed


def sweep_dead_t1_elect_locks(
    *,
    config_dir: "Path",
) -> int:
    """Remove stale T1 election lock files whose scope has no live owner.

    Scans all ``t1_elect.*.lock`` files in *config_dir*. A lock is
    considered orphaned (and is removed) when either of these holds:

    1. No matching ``t1_addr.<scope>`` file exists — the scope is dead
       and nothing holds the lock actively.
    2. A matching ``t1_addr.<scope>`` file exists but its ``claude_pid``
       and ``server_pid`` are BOTH dead (same invariant as
       :func:`sweep_dead_t1_holders`).

    CRITICAL SAFETY: a lock is NEVER removed when:
    - Its scope has a live ``t1_addr`` record with ``claude_pid`` alive.
    - Its scope has a live ``t1_addr`` record with ``server_pid`` alive.
    - The ``t1_addr`` record cannot be parsed (insufficient info — skip).

    Returns the exact count of locks removed. Best-effort; errors on
    individual files are logged and skipped.

    This is the elect-lock half of the nexus-ycwec Fix #3 lifecycle GC.
    It runs alongside :func:`sweep_dead_t1_holders` in the startup sweep.

    RACE WINDOW (narrow, safe): this sweep does NOT hold the election flock
    while it checks for a matching ``t1_addr`` file.  A session that is
    mid-publish at MCP startup can therefore appear orphaned: the elect lock
    is O_CREAT'd before the addr file is written under the flock, so there
    is a brief window where the lock exists but the addr file does not.  If
    the sweep fires in that window it will unlink the lock.  This is safe
    because generation fencing resolves any resulting double-elect: the
    session that lost its lock re-creates it when it enters ``_elect``; the
    concurrent sweeping process raises StaleOwnerError on its next heartbeat
    and self-fences.  No data loss; the only consequence is an extra
    election round.
    """
    from nexus.session import _is_pid_alive  # noqa: PLC0415 - branch-local; avoids circular at module import

    config_path = Path(config_dir)
    if not config_path.exists():
        return 0

    removed = 0
    for lock_file in config_path.glob("t1_elect.*.lock"):
        if not lock_file.is_file():
            continue

        # Extract scope key: t1_elect.<scope>.lock → scope = stem minus prefix
        name = lock_file.name  # e.g. "t1_elect.my-session.lock"
        # Strip "t1_elect." prefix and ".lock" suffix.
        inner = name[len("t1_elect."):-len(".lock")]  # the scope_key
        if not inner:
            _log.debug("sweep_dead_t1_elect_locks_malformed_name", path=str(lock_file))
            continue

        addr_file = config_path / f"t1_addr.{inner}"
        if not addr_file.exists():
            # No matching addr file → scope is orphaned.
            with contextlib.suppress(OSError):
                lock_file.unlink()
            if not lock_file.exists():
                removed += 1
                _log.info(
                    "sweep_dead_t1_elect_locks_removed_no_addr",
                    scope=inner,
                    path=str(lock_file),
                )
            continue

        # Addr file exists: check liveness of its owner pids.
        try:
            text = addr_file.read_text(encoding="utf-8")
            record = LeaseRecord.from_json(text)
        except (OSError, json.JSONDecodeError, KeyError, ValueError):
            # Cannot parse → insufficient info; leave the lock alone.
            _log.debug(
                "sweep_dead_t1_elect_locks_skip_unreadable_addr",
                scope=inner,
                path=str(lock_file),
            )
            continue

        payload = record.payload
        claude_pid_raw = payload.get("claude_pid")
        server_pid_raw = payload.get("server_pid") or record.endpoint.get("server_pid")

        if claude_pid_raw is None or server_pid_raw is None:
            # Insufficient pid metadata — skip conservatively.
            _log.debug(
                "sweep_dead_t1_elect_locks_skip_no_pids",
                scope=inner,
            )
            continue

        try:
            claude_pid = int(claude_pid_raw)
            server_pid = int(server_pid_raw)
        except (TypeError, ValueError):
            continue

        # Safety: only remove when BOTH pids are dead.
        if _is_pid_alive(claude_pid) or _is_pid_alive(server_pid):
            _log.debug(
                "sweep_dead_t1_elect_locks_skip_live",
                scope=inner,
                claude_pid=claude_pid,
                server_pid=server_pid,
            )
            continue

        with contextlib.suppress(OSError):
            lock_file.unlink()

        if not lock_file.exists():
            removed += 1
            _log.info(
                "sweep_dead_t1_elect_locks_removed",
                scope=inner,
                claude_pid=claude_pid,
                server_pid=server_pid,
            )

    return removed


class ServiceSupervisor:
    """Owns one scope's heartbeat cadence and version-cycle.

    Generic over tier: the supervisor mints the owner token, publishes
    the lease, re-stamps it each tick (stopping itself when fenced), and
    orchestrates a version-skew cycle via tier-supplied ``stop_owner`` /
    ``start_owner`` hooks. The version-cycle is what #1112 lacked for T3;
    here it is uniform across tiers, driven by version-skew on the lease.
    """

    def __init__(
        self,
        registry: ServiceRegistry,
        scope_key: str,
        *,
        version: str,
        endpoint_provider: Callable[[], dict[str, Any]],
        payload: Optional[dict[str, Any]] = None,
        owner_token: Optional[str] = None,
    ) -> None:
        self._registry = registry
        self._scope_key = scope_key
        self._version = version
        self._endpoint_provider = endpoint_provider
        self._payload = dict(payload or {})
        self._owner_token = owner_token or mint_owner_token()
        self._record: Optional[LeaseRecord] = None
        self.fenced: bool = False

    @property
    def owner_token(self) -> str:
        return self._owner_token

    @property
    def record(self) -> Optional[LeaseRecord]:
        return self._record

    def publish_once(self) -> LeaseRecord:
        """Claim the scope and remember our lease."""
        self._record = self._registry.publish(
            self._scope_key,
            endpoint=self._endpoint_provider(),
            version=self._version,
            owner_token=self._owner_token,
            payload=self._payload,
        )
        return self._record

    def heartbeat_tick(self) -> None:
        """Re-stamp the lease once. If we have been fenced by a newer
        owner, set ``fenced`` and stop trying (the loser-quiet-exit)."""
        if self._record is None or self.fenced:
            return
        try:
            self._record = self._registry.heartbeat(self._record)
        except StaleOwnerError:
            self.fenced = True
            _log.info(
                "service_supervisor_fenced",
                scope=self._scope_key,
                owner_token=self._owner_token,
            )

    def cycle_to_current(
        self,
        current_version: str,
        *,
        stop_owner: Callable[[], None],
        start_owner: Callable[[], None],
    ) -> bool:
        """Replace a running owner whose version differs from
        ``current_version``. Returns True if a cycle was performed.

        The running owner's version is read from the live lease; on skew,
        ``stop_owner`` tears the old process down and ``start_owner``
        spawns the new-version owner (which re-publishes with the next
        generation). On a version match this is a no-op.
        """
        running = self._registry.discover(self._scope_key)
        if running is None or running.version == current_version:
            return False
        _log.info(
            "service_supervisor_version_cycle",
            scope=self._scope_key,
            running_version=running.version,
            current_version=current_version,
        )
        stop_owner()
        start_owner()
        return True


class SupervisedResource(Protocol):
    """Structural contract a tier's run loop needs from its Supervisor
    wrapper to use ``exit_if_process_unowned`` (GH #1369). ``owns_process``
    is tier-specific (each tier's ``_proc`` handle is a different kind of
    child process — a Java jar, a chroma subprocess, ...), so it stays on
    each tier's own Supervisor class; only the shared "don't heartbeat what
    you don't own" run-loop skeleton lives here."""

    @property
    def owns_process(self) -> bool: ...

    def stop(self) -> None: ...


def exit_if_process_unowned(
    sup: SupervisedResource,
    flush_logging: Callable[[], None],
    *,
    log: Any,
    event: str,
) -> bool:
    """Shared run-loop prelude (RDR-149 §shared primitive, GH #1369): every
    tier's supervise loop calls this immediately after ``sup.start()``, before
    entering its heartbeat loop. Returns True when the caller's run loop must
    exit 0 right away, False when it should proceed to heartbeat as usual.

    Root cause this closes: a tier's ``start()`` can short-circuit on an
    existing, healthy lease (another supervisor already owns the resource)
    without ever assigning the tier's own ``_proc``. Every tier's
    ``heartbeat_once()`` reads "no owned process" as "process died" (it has
    no other way to detect an owned process's exit) and forces a non-zero
    exit — under an OS unit with ``KeepAlive``/``Restart=on-failure`` that
    turns a perfectly healthy coexistence into an unbounded respawn loop,
    since nothing ever kills the ACTUAL owner to free the lease. Checking
    ``owns_process`` before the loop even starts avoids ever making that
    call. ``sup.stop()`` is called before returning True; on the short-circuit
    path this is a proven no-op in every tier that currently uses this helper
    (each guards its lease-touching cleanup on ``self._registry``/
    ``self._supervisor`` being non-``None``, which the short-circuit branch
    never assigns) — kept for defensive symmetry with the loop's other exit
    paths, not because it does anything observable here.
    """
    if sup.owns_process:
        return False
    log.info(event, msg="another supervisor owns the process; exiting cleanly")
    flush_logging()
    sup.stop()
    return True
