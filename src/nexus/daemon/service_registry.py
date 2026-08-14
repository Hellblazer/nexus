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
import re
import signal
import subprocess
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

    def election(self, scope_key: str) -> contextlib.AbstractContextManager[None]:
        """PUBLIC spawn-guard election for *scope_key* (nexus-1qdb9).

        Exposes the substrate's per-scope election flock as a context
        manager so on-demand spawners (the MinerU lifecycle being the
        first) get a race-free check-then-spawn critical section WITHOUT
        growing a bespoke flock outside this primitive — the lifecycle
        gate (tests/daemon/test_lifecycle_gate.py) forbids exactly that.
        Hold it only for the check + process launch; wait for health
        OUTSIDE so a slow model load cannot starve other electors, who
        will re-enter, see the fresh pid, and skip their own spawn.
        """
        return self._elect(scope_key)

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
        releasing the flock so clean shutdowns leave no ``<tier>_elect.*.lock``
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


# NO sweep_dead_t1_holders / sweep_dead_t1_elect_locks (nexus-8zfwv,
# 2026-08-07): both were the nexus-ycwec Fix #3 startup-sweep GC for the
# t1_addr.*/t1_elect.*.lock lease format T1LeasePublisher published. That
# publisher is retired (deleted at ff744321) and had ZERO production
# callers of either sweep even before its removal -- doubly dead, not
# merely orphaned by the publisher going away. T1's current lease file
# (nexus.db.t1's t1_session_lease.*) has its own orphan-reap check
# (nexus.health._check_orphan_t1_lease); this primitive-level sweep pair
# has no successor because nothing publishes an elect-lock-guarded lease
# for T1 any more.


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


# ── Process-table fallback (nexus-oyo2g) ────────────────────────────────────
#
# ``ServiceRegistry.discover()``'s liveness contract is "lease freshness, not
# pid" (see the module docstring and daemon/AGENTS.md's "Liveness is lease
# freshness, not pid" hot rule) — that invariant is UNCHANGED here. What
# follows is a second, narrower concern: a *lease MISS* is a discovery gap,
# not proof that nothing is running. A TTL-expired lease on a
# stalled-but-alive supervisor (heartbeat stuck, process serving) is
# indistinguishable from a genuinely stopped service at the registry layer.
# ``stop`` cannot honestly report "already stopped" without checking ground
# truth, so — ONLY on a lease miss, and ONLY for the idempotent ``stop``
# verb, never for ``discover()``/election/self-heal — it consults the OS
# process table. This generalizes the mechanism ``upgrade_finish.py``'s
# convergence path already built for exactly this gap
# (``service_stack_pids`` / ``_sweep_surviving_stack``, nexus-cfgo9) into the
# shared primitive so any tier's ``stop`` can reuse it instead of growing a
# second copy.
#
# The raw process-table readers below (``ps``, falling back to a Linux
# ``/proc`` walk with no userland dependency) are the same code that used to
# live in ``upgrade_finish.py``; that module now imports them from here.


#: Where Linux exposes the process table without any userland tool.
PROCFS_ROOT = Path("/proc")


def _procfs_available() -> bool:
    """True when this box exposes a Linux-shaped ``/proc``."""
    return (PROCFS_ROOT / "uptime").exists()


def _parse_etime(etime: str) -> int:
    """``[[dd-]hh:]mm:ss`` -> seconds (POSIX ps etime)."""
    days = 0
    if "-" in etime:
        d, etime = etime.split("-", 1)
        days = int(d)
    parts = [int(p) for p in etime.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    h, m, s = parts
    return ((days * 24 + h) * 60 + m) * 60 + s


def _procfs_enumerate() -> list[tuple[int, int, str]]:
    """``[(pid, age_s, command)]`` for EVERY process, read from ``/proc``.

    A minimal container (debian-slim without procps) has no ``ps`` binary
    at all; Linux always mounts ``/proc``, so this fallback removes the
    userland dependency rather than merely tolerating its absence.

    Age is derived the same way ``ps etime`` derives it: system uptime minus
    the process's ``starttime`` (field 22 of ``/proc/<pid>/stat``, in clock
    ticks since boot). A process whose files vanish mid-scan (exited between
    ``iterdir`` and ``read``) is skipped, never guessed at.
    """
    uptime_s = float((PROCFS_ROOT / "uptime").read_text().split()[0])
    hz = os.sysconf("SC_CLK_TCK") or 100
    out: list[tuple[int, int, str]] = []
    for entry in PROCFS_ROOT.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        try:
            raw_cmdline = (entry / "cmdline").read_bytes()
            stat = (entry / "stat").read_text()
        except (OSError, ValueError):
            continue  # exited mid-scan, or not ours to read
        # Kernel threads have an empty cmdline — never a conexus process.
        command = raw_cmdline.replace(b"\x00", b" ").decode(
            "utf-8", "replace",
        ).strip()
        if not command:
            continue
        # Field 2 (comm) is parenthesised and may itself contain spaces or
        # ')', so index from the LAST ')': the remainder starts at field 3,
        # making starttime (field 22) index 19.
        try:
            after = stat[stat.rindex(")") + 1:].split()
            start_ticks = float(after[19])
        except (ValueError, IndexError):
            continue
        age = int(max(0.0, uptime_s - start_ticks / hz))
        out.append((pid, age, command))
    return out


def _ps_enumerate() -> list[tuple[int, int, str]] | None:
    """``[(pid, age_s, command)]`` from POSIX ``ps``, or ``None`` when this
    box has no ``ps`` binary at all (the caller then tries ``/proc``).

    ``ps -eo pid,etime,command`` is POSIX-portable (etime, unlike lstart,
    parses identically on macOS and Linux).
    """
    try:
        proc = subprocess.run(
            ["ps", "-wweo", "pid,etime,command"],
            capture_output=True, text=True, timeout=15,
        )
    except FileNotFoundError:
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        # A silent empty ps = zero processes detected = the fail-open class
        # again. Fail loud instead.
        raise RuntimeError(
            f"ps failed (rc={proc.returncode}): {proc.stderr.strip()[:200]}"
        )
    return _parse_ps_table(proc.stdout)


def _parse_ps_table(ps_output: str) -> list[tuple[int, int, str]]:
    """Parse a ``pid etime command`` table into ``[(pid, age_s, command)]``."""
    out: list[tuple[int, int, str]] = []
    for line in ps_output.splitlines()[1:]:
        m = re.match(r"\s*(\d+)\s+(\S+)\s+(.*)", line)
        if not m:
            continue
        try:
            age = _parse_etime(m.group(2))
        except ValueError:
            continue
        out.append((int(m.group(1)), age, m.group(3)))
    return out


def all_process_rows(ps_output: str | None = None) -> list[tuple[int, int, str]]:
    """``[(pid, age_s, command)]`` for EVERY process on the box, unfiltered.

    Reads ``ps`` when a ``ps`` binary exists, else ``/proc`` (see
    :func:`_procfs_enumerate`). A box with NEITHER raises; so does a box
    whose PRESENT ``ps`` fails or returns an empty table (that is a signal
    worth surfacing — e.g. a hidepid-restricted or corrupted procps — not a
    case to silently route around). It raises rather than reporting an
    empty table: a silent "zero processes" is the fail-open this function
    exists to eliminate. ``ps_output`` is injectable for tests.
    """
    if ps_output is not None:
        return _parse_ps_table(ps_output)
    rows = _ps_enumerate()
    if rows is None:
        if not _procfs_available():
            raise RuntimeError(
                "this system has neither a 'ps' command nor a readable "
                "/proc filesystem — process-skew detection cannot run "
                "(install procps, or run on a host that provides one)"
            )
        rows = _procfs_enumerate()
    return rows


def process_command(pid: int) -> str:
    """The full command line of *pid*, or ``""`` when it is gone.

    Used by pid-recycle re-checks — a bare ``ps -p`` direct call would add
    a userland dependency this module otherwise sheds via ``/proc``.
    """
    if _procfs_available():
        try:
            raw = (PROCFS_ROOT / str(pid) / "cmdline").read_bytes()
        except OSError:
            return ""
        return raw.replace(b"\x00", b" ").decode("utf-8", "replace").strip()
    try:
        probe = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True, text=True, timeout=10,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return ""
    return probe.stdout.strip()


def pid_alive(pid: int) -> bool:
    """True when signalling 0 to *pid* succeeds.

    THE single implementation (nexus-oyo2g review finding 3): this used to
    be duplicated in ``storage_service_daemon._pid_is_alive`` with a
    diverged ``OSError`` edge case — that module now imports this function
    under its old name instead of defining its own. Kept THIS module's
    more permissive-on-ambiguity semantics: an ``OSError`` other than
    ``ProcessLookupError`` (ESRCH) is treated as "alive" rather than
    "dead". A liveness probe that decides whether to skip a kill/declare
    "nothing to signal" must not treat an ambiguous errno as proof of
    death — a false "dead" here is exactly the class of bug this bead
    fixes (declaring something stopped when it might still be running).
    """
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


def process_state(pid: int) -> str | None:
    """The kernel's scheduler-state letter for *pid* (``R``, ``S``, ``D``,
    ``Z``, ``T``, ...), or ``None`` when it cannot be determined.

    ``None`` means UNKNOWN, never "dead": the process may be gone, the
    state may be unreadable (permissions, hidepid), or this box may offer
    no way to ask at all. Callers must fall back to their permissive
    default on ``None`` rather than reading it as a state — see
    :func:`pid_running`.

    Linux answers from ``/proc/<pid>/stat``: the state is the field
    immediately after ``comm``, and ``comm`` is parenthesised and may
    itself contain spaces or ``)``, so the parse indexes from the LAST
    ``)`` (the same discipline as :func:`_procfs_enumerate`). Elsewhere
    (macOS, BSD) ``ps -o state=`` is the portable equivalent; its output
    can carry trailing flag characters (``S+``, ``R<``), so only the first
    character is significant.
    """
    if pid <= 0:
        return None
    if _procfs_available():
        try:
            stat = (PROCFS_ROOT / str(pid) / "stat").read_text()
        except OSError:
            return None
        try:
            return stat[stat.rindex(")") + 1:].split()[0]
        except (ValueError, IndexError):
            return None
    try:
        probe = subprocess.run(
            ["ps", "-o", "state=", "-p", str(pid)],
            capture_output=True, text=True, timeout=10,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    state = probe.stdout.strip()
    return state[0] if state else None


def pid_running(pid: int) -> bool:
    """True when *pid* is alive AND actually EXECUTING — i.e. NOT a zombie
    awaiting reap by its parent.

    :func:`pid_alive` is ``os.kill(pid, 0)``, which SUCCEEDS for a zombie:
    a terminated process whose exit status no parent has collected still
    owns its pid and still accepts signal 0. That is correct for "may I
    signal this pid?" and WRONG for "did my kill work?" — SIGKILL is
    unblockable, so a pid that still answers ``os.kill(pid, 0)`` after one
    is either a zombie or genuinely wedged in uninterruptible sleep, and
    only the second is worth alarming about.

    The distinction is load-bearing, not theoretical (nexus-o8dil.21): a
    storage-service supervisor + engine orphaned to a PID 1 that is not a
    real init (a container whose PID 1 is a shell script, a CI runner)
    stay zombies indefinitely, so ``nx daemon service stop`` reported
    "pid(s) N survived SIGKILL", exited 1, and told the operator not to
    run ``start`` — on a stop that had in fact succeeded completely.

    UNKNOWN state (``process_state`` -> ``None``) is treated as RUNNING,
    matching :func:`pid_alive`'s permissive-on-ambiguity discipline: a
    genuine survivor (uninterruptible ``D``) must stay loud, and an
    unaskable box must never be silently downgraded to "clean stop".
    """
    if not pid_alive(pid):
        return False
    return process_state(pid) != "Z"


#: How long a SIGKILLed pid gets to actually leave the process table (or at
#: least reach ``Z``) before :func:`terminate_pids` reports it as a
#: survivor. The predecessor was a single flat ``sleep(0.5)``, which is a
#: race even on a box with a prompt reaper: a JVM being SIGKILLed plus the
#: parent's ``wait()`` round-trip under load routinely exceeds 500 ms.
#: Polling to a bound is both faster in the common case (returns as soon as
#: the pid is gone) and correct in the slow one.
_POST_KILL_SETTLE_S: float = 5.0


def terminate_pids(pids: list[int], *, grace_s: float = 10.0) -> list[int]:
    """SIGTERM, wait up to *grace_s*, then SIGKILL. Returns pids still
    RUNNING afterwards (never zombies — see :func:`pid_running`).

    A SIGSTOPped process never acts on SIGTERM while stopped, which is
    exactly why the escalation to the uncatchable, unblockable SIGKILL is
    unconditional rather than a best-effort nicety (nexus-oyo2g repro c:
    double-spawn from a frozen supervisor). A ``T``-state process is still
    reported as running here and still gets the escalation — only ``Z``
    (already dead, merely unreaped) is excluded.

    The survivor verdict is zombie-aware and bounded-retry rather than a
    single post-SIGKILL sleep (nexus-o8dil.21), which makes this tolerant
    of a CONCURRENT killer as a side effect: a pid another sweep already
    killed and reaped reads as ESRCH, and one it killed but has not reaped
    yet reads as ``Z`` — neither is a survivor. Both legs previously
    produced a false "survived SIGKILL".
    """
    live = [p for p in pids if pid_running(p)]
    for pid in live:
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline:
        live = [p for p in live if pid_running(p)]
        if not live:
            return []
        time.sleep(0.2)
    for pid in live:
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    settle_deadline = time.monotonic() + _POST_KILL_SETTLE_S
    while True:
        live = [p for p in live if pid_running(p)]
        if not live or time.monotonic() >= settle_deadline:
            return live
        time.sleep(0.1)


def storage_service_stack_matcher(config_dir: Path) -> Callable[[str], bool]:
    """Argv predicate matching the storage-service SUPERVISOR (``nx daemon
    service start --foreground --config-dir <config_dir>``) or ENGINE
    (argv[0] under ``<config_dir>/service/nexus-service``) belonging to
    *config_dir*.

    Token-exact on ``--config-dir`` — never a substring test: ``--config-dir``
    is the documented multi-profile mechanism, and a bare
    ``str(config_dir) in command`` would match ``.config/nexus`` against
    ``.config/nexus-staging``'s command line, folding a healthy sibling
    profile's supervisor into a kill set. The engine match is argv[0]-exact
    for the same reason (never a substring match on a `tail .../nexus-service.log`
    or similar diagnostic command).
    """
    engine_path = str(config_dir / "service" / "nexus-service")
    target = str(config_dir)

    def _match(command: str) -> bool:
        if command.split()[:1] == [engine_path]:
            return True
        if "daemon service start" not in command:
            return False
        tokens = command.split()
        for i, tok in enumerate(tokens):
            if tok == "--config-dir" and i + 1 < len(tokens):
                return tokens[i + 1] == target
            if tok.startswith("--config-dir="):
                return tok[len("--config-dir="):] == target
        return False

    return _match


@dataclass(frozen=True)
class ProcessSweepResult:
    """Outcome of :func:`sweep_matching_processes`.

    ``available`` is False only when the process table itself could not be
    read (no ``ps`` and no ``/proc``) — a caller degrades gracefully on
    that leg rather than claiming a clean sweep it never performed.
    """

    available: bool
    error: str | None
    found: tuple[tuple[int, str], ...]
    stubborn: tuple[int, ...]

    @property
    def pids(self) -> tuple[int, ...]:
        return tuple(p for p, _cmd in self.found)


def sweep_matching_processes(
    matcher: Callable[[str], bool],
    *,
    exclude_pid: int | None = None,
    grace_s: float = 10.0,
) -> ProcessSweepResult:
    """Find OS processes whose command line satisfies *matcher*, terminate
    them (SIGTERM -> SIGKILL via :func:`terminate_pids`), and report what
    was found / left stubborn.

    THE shared mechanism nexus-oyo2g's ``stop_storage_service`` fix needed:
    a lease MISS from ``ServiceRegistry.discover()`` is a discovery gap, not
    proof nothing is running (a TTL-expired lease on a stalled-but-alive
    supervisor looks identical to "stopped" from the registry's point of
    view). Consulting the process table as ground truth removes that
    ambiguity. Generalizes ``upgrade_finish._sweep_surviving_stack``
    (nexus-cfgo9) — that function still exists for its own before/after
    subprocess-composition use, but the core matcher + terminate mechanism
    now has exactly one implementation, here.

    Re-verifies each candidate's argv immediately before returning it as
    "found" (guards the snapshot-to-report window against pid reuse — the
    same discipline as ``upgrade_finish``'s recycle guard, folded into one
    pass since there is no separate before/after subprocess gap here).

    All matched pids are handed to :func:`terminate_pids` together (SIGTERM
    to every pid, then escalate) rather than supervisor-then-engine in
    sequence the way ``_sweep_surviving_stack`` orders it: that ordering
    existed to ride the supervisor's PDEATHSIG cascade onto its still-live
    engine child, a mechanism RDR-175 retired (the supervisor's in-process
    respawn-on-child-death is gone, so there is no cascade left to
    sequence around) — simultaneous SIGTERM is not a regression here.
    """
    me = exclude_pid if exclude_pid is not None else os.getpid()
    try:
        rows = all_process_rows()
    except Exception as exc:  # noqa: BLE001 — no process table: surfaced to the caller, never silently "nothing found"
        return ProcessSweepResult(available=False, error=str(exc), found=(), stubborn=())

    found: list[tuple[int, str]] = []
    for pid, _age, command in rows:
        if pid == me or not matcher(command):
            continue
        current = process_command(pid)
        # An unreadable argv (permissions, zombie mid-reap) is not evidence
        # of a recycle; only a DIFFERENT readable argv is.
        if current and current.split() != command.split():
            _log.info(
                "sweep_matching_processes_pid_recycled",
                pid=pid, recorded=command[:120], current=current[:120],
            )
            continue
        found.append((pid, command))

    if not found:
        return ProcessSweepResult(available=True, error=None, found=(), stubborn=())

    stubborn = tuple(terminate_pids([pid for pid, _cmd in found], grace_s=grace_s))
    return ProcessSweepResult(
        available=True, error=None, found=tuple(found), stubborn=stubborn,
    )
