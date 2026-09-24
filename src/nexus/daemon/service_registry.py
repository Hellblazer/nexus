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
- **Scope-keyed election.** A per-scope advisory file lock
  (:func:`nexus._locking.lock_fd`) serializes the generation
  read-increment-write, so concurrent siblings converge to exactly one
  owner per scope with strictly increasing generations.

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
import json
import os
import re
import signal
import subprocess
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Optional, Protocol, TypeVar

import structlog

from nexus import _locking
from nexus.bounded_subprocess import run_bounded

_log = structlog.get_logger(__name__)

_T = TypeVar("_T")

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


#: Tiers whose TTL-expired lease is still read as live when the reader can
#: independently confirm it (nexus-wo6sc half one; Sam DECIDED 2026-09-24):
#: the recorded owner pid is alive AND the recorded port answers ``/health``
#: as the expected service. This never narrows ``discover()``'s existing
#: contract -- a fresh lease is unaffected, and every tier NOT in this set
#: keeps the plain "liveness is lease freshness, not pid" rule unchanged.
#: Scoped explicitly to ``storage_service``: it is the only tier whose
#: published lease names a real HTTP endpoint at all (``aspect_worker`` is
#: not an HTTP server, so its lease's ``endpoint`` carries no health-probable
#: port -- the probe below simply cannot succeed for it, and grace never
#: fires there even if this set is widened by mistake). The 2026-09-12
#: incident this closes (two heartbeat ticks at 19.098s / 31.622s against a
#: 15s TTL, the supervisor alive and healthy throughout) was measured
#: against storage_service specifically. A future tier that earns the same
#: grace adds itself here once, rather than growing a tier-local copy
#: (AGENTS.md's standing "no per-tier lifecycle copy" gate).
TIER_READER_GRACE: frozenset[str] = frozenset({"storage_service"})

#: How far PAST its own TTL a lease may still be graced, as a MULTIPLE of
#: that lease's own ``ttl`` (never an absolute constant), so the bound scales
#: with whichever tier's TTL window applies. The worst tick actually
#: measured (nexus-wo6sc, 2026-09-12) was 31.622s against a 15s TTL — about
#: 2x. Ten TTL windows is an order of magnitude more headroom than the worst
#: stall on record while still refusing to resurrect a lease that has been
#: stale essentially forever (a suspended box, an abandoned record, a
#: process alive but never heartbeating again): past this bound, an
#: "alive and healthy"-looking owner that has gone ten TTL windows with no
#: successful heartbeat is far more likely wedged or orphaned than mid-stall,
#: and the grace must not paper over that indefinitely.
STALE_LEASE_GRACE_MAX_TTL_MULTIPLE: float = 10.0

#: Bounded timeout for the grace path's OWN ``/health`` probe. Deliberately
#: SHORTER than ``storage_service_daemon._HEALTH_TIMEOUT`` (4.0s, which
#: bounds the SUPERVISOR's own heartbeat tick against ITS OWN
#: pool-contention tolerance) — this bounds an incidental READER-side probe
#: that must not itself add multi-second latency to every ``discover()``
#: call during a genuine outage. Only the stale path pays this cost at all:
#: a fresh lease never reaches this probe.
STALE_LEASE_GRACE_HEALTH_TIMEOUT_S: float = 1.5

#: The only hosts a grace probe may ever contact — loopback, never a network
#: address. Every current lease-serving tier's endpoint IS loopback
#: (``storage_service_daemon._SERVICE_HOST == "127.0.0.1"`` unconditionally),
#: but this is a belt-and-suspenders refusal at the primitive itself: a
#: shared registry must never turn a stale-lease read into an outbound
#: network call, no matter what a future or malformed record's endpoint
#: names. Deliberately NOT ``"::1"`` (code review, nexus-wo6sc review
#: round, 2026-09-24): ``_probe_health_identity`` builds its URL as
#: ``f"http://{host}:{port}/health"``, which is malformed for a bare IPv6
#: literal (needs bracketing: ``http://[::1]:{port}/health``) — admitting
#: it here without also bracketing the URL would have been dead code that
#: MISBEHAVED the one time it was ever reached, rather than dead code that
#: is merely unreachable. Chose removal over bracketing: ``_SERVICE_HOST``
#: is a hardcoded literal (``"127.0.0.1"``), never ``"::1"``, in every
#: current caller, so there is no real endpoint this would ever admit —
#: bracketing would be correctness work for a case that cannot occur,
#: adding branch complexity to a function this primitive's own standing
#: gate wants easy to audit. ``"localhost"`` stays: URL construction with
#: it is syntactically correct (no bracketing hazard), so it costs nothing
#: to keep as headroom for a future caller that names loopback by hostname
#: rather than literal IP.
_GRACE_PROBE_LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "localhost"})


def _probe_health_identity(host: str, port: int, *, timeout: float) -> bool:
    """True iff ``GET http://{host}:{port}/health`` answers the
    storage-service's documented shape (``HealthHandler.java``: 200
    ``{"status": "ok", "db": "up"}``) — not just "something answered on this
    port". Checking the BODY, not merely the status code, is the identity
    check the DECISION requires: port reuse means an unrelated local process
    could be listening where the recorded service used to be, and an
    unrelated process happening to answer this exact shape on a bare GET
    ``/health`` is not a realistic accident.

    Bounded and best-effort: any failure at all — timeout, connection
    refused, a non-200 status, an unparseable or wrong-shaped body — reads
    as False ("not this service"). Never raises.
    """
    import json as _json  # noqa: PLC0415 — deferred import — only the grace path needs it
    import urllib.error  # noqa: PLC0415 — deferred import — branch-local
    import urllib.request  # noqa: PLC0415 — deferred import — platform/heavy dep loaded only on the path that needs it

    url = f"http://{host}:{port}/health"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — fixed loopback URL built from the recorded endpoint, never user input
            if resp.status != 200:
                return False
            body = _json.loads(resp.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 — best-effort probe: any failure reads as "not this service"
        return False
    return isinstance(body, dict) and body.get("status") == "ok"


_FORMAT_VERSION: int = 1

Clock = Callable[[], float]


class ServiceRegistryError(RuntimeError):
    """Base error for the service-registry substrate."""


class StaleOwnerError(ServiceRegistryError):
    """Raised when an owner tries to heartbeat a lease that a newer
    (higher-generation) or different owner now holds. The caller has been
    fenced and must stop; it must not re-create or overwrite the record.
    """


class ElectionBusyError(ServiceRegistryError):
    """Raised when a BOUNDED election (``heartbeat``) could not take the
    per-scope flock inside its budget (nexus-59bah). Not a fence: the caller
    still owns its lease and simply skipped one stamp. The next tick retries.
    """


#: Fraction of the lease TTL a heartbeat may spend waiting for the election
#: flock (nexus-59bah). One third: a tick that spends its whole budget still
#: returns with two thirds of the TTL left, so a transient holder costs a
#: skipped stamp and the next tick retries before discoverers read the lease
#: as absent; a wedged holder can never keep the heartbeat loop itself blocked
#: past the TTL (the 2026-09-06 skew-window shape). Two consecutive busy ticks
#: do age the lease out; that is the visible failure, not a silent wedge.
HEARTBEAT_ELECTION_BUDGET_FRACTION: float = 1.0 / 3.0

#: Default budget for the STOP-path election calls (``mark_shutting_down``,
#: ``relinquish``), nexus-cd1k0 review round 3 finding 6. Both previously took
#: ``budget=None`` -- an unconditionally BLOCKING ``LOCK_EX`` -- which made a
#: tier's documented outer stop grace (e.g.
#: ``storage_service_daemon._SUPERVISOR_STOP_GRACE``) false advertising: that
#: constant's docstring claims to strictly exceed the inner worst case, but an
#: unbounded flock wait inside ``stop()`` has no worst case to exceed. A
#: contested election during a graceful stop is rare and momentary (the
#: contending side is itself either heartbeating, under its OWN
#: ``heartbeat_election_budget``-bounded wait, or another stop/relinquish
#: racing the same scope) -- a few seconds is ample headroom without
#: reproducing the heartbeat path's TTL-fraction reasoning, which does not
#: apply here (there is no lease-aging clock counting down during a stop).
#: On exhaustion this raises ``ElectionBusyError`` exactly like the heartbeat
#: path; every stop-path caller treats that as best-effort (already true for
#: ``mark_shutting_down`` at both call sites, and now also true for
#: ``relinquish`` -- see storage_service_daemon.StorageServiceSupervisor.stop
#: and aspect_worker_daemon.AspectWorkerDaemon.stop) so a busy flock degrades
#: to "the record ages out via TTL instead of being explicitly relinquished",
#: never to a stop that itself hangs.
DEFAULT_STOP_ELECTION_BUDGET: float = 2.0

#: LOCK_NB poll cadence for the bounded election.
_ELECTION_POLL_INTERVAL: float = 0.05


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
        monotonic: Clock = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
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
        self._monotonic = monotonic
        self._sleep = sleep
        # nexus-wo6sc: per-tick stamp sub-phase timings, replaced (never
        # accumulated) on every heartbeat. See ``last_heartbeat_phases``.
        self._last_heartbeat_phases: dict[str, float] = {}
        self._last_heartbeat_total: float = 0.0

    # -- paths --------------------------------------------------------------

    @property
    def clock(self) -> Clock:
        return self._clock

    @property
    def heartbeat_interval(self) -> float:
        return self._heartbeat_interval

    @property
    def heartbeat_election_budget(self) -> float:
        """Seconds a heartbeat may wait for the election flock (nexus-59bah)."""
        return self._ttl * HEARTBEAT_ELECTION_BUDGET_FRACTION

    @property
    def last_heartbeat_phases(self) -> Mapping[str, float]:
        """Sub-phase seconds for the most recent :meth:`heartbeat` (nexus-wo6sc).

        Keys are syscall groups inside the stamp: ``elect_open`` (mkdir +
        opening the lock file), ``elect_flock`` (waiting for the election
        lock), ``read`` (reading the current record), and ``write_open`` /
        ``write_body`` / ``write_replace`` (the atomic write's three parts).
        Replaced wholesale each tick, so a reading is this tick's, never a
        running total.
        """
        return dict(self._last_heartbeat_phases)

    @property
    def last_heartbeat_unaccounted(self) -> float:
        """Seconds the last heartbeat spent inside NO timed syscall group.

        This is the discriminator the 2026-09-12 incident needed and did not
        have. Wall clock around the stamp as a whole cannot tell a blocked
        syscall from a thread that lost the CPU -- both show the same elapsed
        time and both show near-zero process CPU. Split out, they separate:
        a filesystem stall lands in one syscall sub-phase, while descheduling
        lands here, because a thread that is not running is not inside any
        call. Never negative (a clock that went backwards reads as 0.0).
        """
        return max(0.0, self._last_heartbeat_total - sum(self._last_heartbeat_phases.values()))

    def _timed(self, phases: Optional[dict[str, float]], name: str, fn: Callable[[], _T]) -> _T:
        """Run *fn*, charging its wall-clock seconds to ``phases[name]``.

        ``phases=None`` is the uninstrumented path (publish, relinquish, reap):
        those are not on the heartbeat hot loop and pay nothing for this.
        """
        if phases is None:
            return fn()
        t0 = self._monotonic()
        try:
            return fn()
        finally:
            phases[name] = phases.get(name, 0.0) + (self._monotonic() - t0)

    def _record_path(self, scope_key: str) -> Path:
        return self._dir / f"{self._tier}_addr.{scope_key}"

    def _election_path(self, scope_key: str) -> Path:
        return self._dir / f"{self._tier}_elect.{scope_key}.lock"

    def _ensure_dir(self) -> None:
        self._dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    # -- election -----------------------------------------------------------

    @contextlib.contextmanager
    def _elect(
        self,
        scope_key: str,
        *,
        budget: Optional[float] = None,
        phases: Optional[dict[str, float]] = None,
    ) -> Iterator[None]:
        """Hold the per-scope election flock for a read-modify-write.

        ``budget=None`` (publish, reap; ``mark_shutting_down``/``relinquish``
        callers that pass nothing, e.g. any caller outside a bounded stop
        path): blocking ``LOCK_EX``. The critical section (read current
        record, increment generation, atomic write) is short, and a
        publisher must wait its turn rather than fail, so concurrent
        siblings serialize into strictly increasing generations.

        ``budget=<seconds>`` (heartbeat, nexus-59bah; the STOP path's
        ``mark_shutting_down``/``relinquish``, nexus-cd1k0 review round 3
        finding 6): ``LOCK_NB`` polled against the injected monotonic clock;
        raises ``ElectionBusyError`` once the budget is spent. A heartbeat
        already holds a lease, so a skipped stamp is cheap and a blocked
        tick is not: the 2026-09-06 skew window had a live supervisor
        wedged in one tick past the TTL. The stop path reasons identically:
        a stop that cannot finish relinquishing is still a stop, and must
        not itself become the thing that hangs (see
        ``DEFAULT_STOP_ELECTION_BUDGET``'s docstring).
        """
        def _open() -> int:
            self._ensure_dir()
            path = self._election_path(scope_key)
            return os.open(str(path), os.O_WRONLY | os.O_CREAT, 0o600)

        # nexus-wo6sc: the mkdir + open are filesystem calls too, and they sit
        # INSIDE the stamp but OUTSIDE the election budget -- an fs stall lands
        # here just as readily as in the write, so they get their own phase.
        fd = self._timed(phases, "elect_open", _open)
        try:
            if budget is None:
                self._timed(phases, "elect_flock", lambda: _locking.lock_fd(fd, blocking=True))
            else:
                self._timed(
                    phases, "elect_flock", lambda: self._flock_within(fd, scope_key, budget)
                )
            yield
        finally:
            try:
                _locking.unlock_fd(fd)
            finally:
                os.close(fd)

    def _flock_within(self, fd: int, scope_key: str, budget: float) -> None:
        deadline = self._monotonic() + budget
        while True:
            try:
                _locking.lock_fd(fd, blocking=False)
                return
            except BlockingIOError:
                now = self._monotonic()
                if now >= deadline:
                    raise ElectionBusyError(
                        f"scope {scope_key!r}: election flock still held after "
                        f"{budget:.2f}s budget; heartbeat stamp skipped"
                    ) from None
                self._sleep(min(_ELECTION_POLL_INTERVAL, deadline - now))

    # -- atomic IO ----------------------------------------------------------

    def _read_record(self, scope_key: str) -> Optional[LeaseRecord]:
        path = self._record_path(scope_key)
        try:
            text = path.read_text()
        except OSError:
            return None
        except UnicodeDecodeError as exc:
            # nexus-cd1k0.6 finding (8): a UnicodeDecodeError is a ValueError
            # subclass, not an OSError, so the OSError clause above never
            # caught it -- a non-UTF-8 record raised straight out of
            # discover/publish/heartbeat and crash-looped the supervisor.
            # Treated the same as a corrupt-JSON record: log and report "no
            # lease here" rather than let the reader see raw garbage.
            _log.warning(
                "service_registry_corrupt_record", path=str(path), error=str(exc)
            )
            return None
        try:
            return LeaseRecord.from_json(text)
        except (json.JSONDecodeError, KeyError, ValueError, TypeError) as exc:
            # TypeError (nexus-cd1k0.6 finding (8)): valid JSON of the wrong
            # SHAPE -- `null`, a bare list, or a dict whose "endpoint" is not
            # itself a mapping -- raises TypeError out of from_json's field
            # access/dict() coercion, not one of the exceptions formerly
            # caught here. Same corrupt-record handling as a JSON syntax
            # error: this is valid JSON, just not a valid LeaseRecord.
            _log.warning(
                "service_registry_corrupt_record", path=str(path), error=str(exc)
            )
            return None

    def _write_record_atomic(
        self, record: LeaseRecord, phases: Optional[dict[str, float]] = None
    ) -> None:
        self._ensure_dir()
        path = self._record_path(record.scope_key)
        tmp = path.with_suffix(path.suffix + f".{os.getpid()}.{uuid.uuid4().hex}.tmp")
        fd = self._timed(
            phases,
            "write_open",
            lambda: os.open(str(tmp), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600),
        )
        try:
            def _body() -> None:
                try:
                    os.write(fd, record.to_json().encode("utf-8"))
                finally:
                    os.close(fd)

            self._timed(phases, "write_body", _body)
            self._timed(phases, "write_replace", lambda: os.replace(str(tmp), str(path)))
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
        nothing. Bounded election (nexus-59bah): raises ``ElectionBusyError``
        if the flock is not free within ``heartbeat_election_budget``. The
        flock is the target by elimination: every probe in the supervisor's
        tick (health, livez, pg) is timeout-bounded below the TTL, so the
        flock plus the record write were the only unbounded calls. The write
        itself (``_read_record`` / ``_write_record_atomic``) stays unbounded
        by decision: a filesystem stall long enough to age the lease out is
        reported by the tier's missed-TTL log, not masked here.

        nexus-wo6sc (2026-09-12) narrowed that "by elimination" reasoning with
        a measurement, and it did not land where nexus-59bah expected. Two
        ticks blew a 15 s TTL at 19.098 s and 31.622 s with ZERO
        ``service_supervisor_heartbeat_election_busy`` events in the run, so
        the flock was free and at most one budget's worth of that time was
        election. What remains is unbounded, but which unbounded thing is not
        yet known: the write here is ~464 bytes with no fsync, microseconds of
        real work, so a filesystem stall is no more plausible on its face than
        the supervisor simply losing the CPU under a 4-wide battery. Wall
        clock cannot separate the two -- a blocked syscall and an unscheduled
        thread show the same elapsed time and the same near-zero process CPU.
        Hence the sub-phase timings (``last_heartbeat_phases``) and the
        ``last_heartbeat_unaccounted`` term: a stalled syscall is charged to
        its own phase, while time inside no call at all can only be
        descheduling. Do not re-narrow this to one cause until a tick with
        sub-phases in it has been captured in the wild.
        """
        phases: dict[str, float] = {}
        self._last_heartbeat_phases = phases
        self._last_heartbeat_total = 0.0
        started = self._monotonic()
        try:
            return self._heartbeat_locked(record, phases)
        finally:
            self._last_heartbeat_total = self._monotonic() - started

    def _heartbeat_locked(
        self, record: LeaseRecord, phases: dict[str, float]
    ) -> LeaseRecord:
        with self._elect(
            record.scope_key, budget=self.heartbeat_election_budget, phases=phases
        ):
            current = self._timed(phases, "read", lambda: self._read_record(record.scope_key))
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
            self._write_record_atomic(refreshed, phases)
            return refreshed

    def discover(self, scope_key: str) -> Optional[LeaseRecord]:
        """Resolve the live owner of ``scope_key``, or ``None``.

        Returns ``None`` for a missing, expired (TTL) and ungraced,
        shutdown-marked, or expired-and-still-dead-on-grace-check record. An
        expired record that is not resurrected by grace is best-effort
        reaped so the next lookup is fast. Liveness here is purely lease
        freshness for every tier NOT in ``TIER_READER_GRACE`` — no pid is
        consulted, unchanged from before nexus-wo6sc. For a tier IN that set
        (currently ``storage_service`` only), a TTL-expired-but-``live``
        record gets ONE more chance before being read as absent: see
        :meth:`_stale_lease_still_live` (nexus-wo6sc half one, Sam DECIDED
        2026-09-24) — the recorded owner pid alive AND the recorded port
        answering ``/health`` as the expected service. This only ever WIDENS
        a freshness miss into a hit; a fresh record is returned exactly as
        before and pays no extra cost. The T2 client-side resolver
        (discovery.py ``_resolve_lease_record``) separately adds its own
        process-liveness checks on top of the heartbeat-age check for the T2
        tier (nexus-md90p): a stale-but-answering UDS rescue and a dead-pid
        fast-path — unrelated to the grace here, which lives in THIS layer
        instead precisely so every caller of this primitive benefits (AGENTS.md's
        "no per-tier lifecycle copy" gate).
        """
        record = self._read_record(scope_key)
        if record is None:
            return None
        if record.is_fresh(self._clock()):
            return record
        if self._tier in TIER_READER_GRACE and self._stale_lease_still_live(record):
            return record
        self._reap_if_still_stale(record)
        return None

    def _stale_lease_still_live(self, record: LeaseRecord) -> bool:
        """Reader-side grace (nexus-wo6sc half one, Sam DECIDED 2026-09-24).

        True iff a TTL-expired *record* should still be read as live because
        THIS reader can independently confirm it: the recorded owner pid is
        alive AND the recorded port answers ``/health`` as the expected
        service. Liveness must not depend on the heartbeat stamp write
        landing within TTL for the two stall causes the DECISION was
        written against — an I/O stall or the writer losing the CPU
        (nexus-wo6sc's own still-open question about which one the
        2026-09-12 incident was); only when BOTH checks fail does this
        return False, matching the DECISION verbatim ("only both failing
        means down"). NOT a claim that grace always resolves a stall: under
        SYSTEM-WIDE CPU contention (not just the writer starved, the whole
        box is), this reader-side ``/health`` probe can itself starve the
        same way the stamp did, and the bounded timeout below simply times
        it out. That FAILS SAFE — the caller sees "down", the same answer
        it got before this change existed — but it is not a guarantee that
        grace helps under that specific condition, only that it never makes
        the outcome worse.

        Guards, each independently sufficient to deny grace:

        - ``record.status`` must be ``"live"`` — a published shutdown marker
          (``mark_shutting_down``) must never be resurrected by this path.
        - Not too stale: :data:`STALE_LEASE_GRACE_MAX_TTL_MULTIPLE` bounds
          how far past TTL a lease may still be graced.
        - A recorded ``payload["supervisor_pid"]`` must be present — a
          legacy/non-supervised record has nothing to confirm against and is
          treated as down exactly as before this change (mirrors
          :func:`reclaim_lease_if_dead_owner`'s identical guard).
        - :func:`pid_alive` on that pid (THE shared liveness primitive —
          never a second implementation).
        - The recorded endpoint host must be loopback
          (``_GRACE_PROBE_LOOPBACK_HOSTS``) and the port must be positive.
        - :func:`_probe_health_identity` at that host/port, bounded by
          :data:`STALE_LEASE_GRACE_HEALTH_TIMEOUT_S`, must answer the
          expected service's ``/health`` shape — a dead pid gives down; an
          alive pid whose port does not answer, or answers as something
          else (pid/port reuse), also gives down.

        Only the stale path pays for any of this — a fresh lease never
        calls here.
        """
        if record.status != "live":
            return False
        now = self._clock()
        age = now - record.heartbeat_epoch
        if age > record.ttl * STALE_LEASE_GRACE_MAX_TTL_MULTIPLE:
            return False
        supervisor_pid = record.payload.get("supervisor_pid")
        if not (isinstance(supervisor_pid, int) and supervisor_pid > 0):
            return False
        if not pid_alive(supervisor_pid):
            return False
        host = str(record.endpoint.get("host", ""))
        if host not in _GRACE_PROBE_LOOPBACK_HOSTS:
            return False
        try:
            port = int(record.endpoint.get("port", 0))
        except (TypeError, ValueError):
            return False
        if port <= 0:
            return False
        healthy = _probe_health_identity(
            host, port, timeout=STALE_LEASE_GRACE_HEALTH_TIMEOUT_S
        )
        if healthy:
            _log.info(
                "service_registry_stale_lease_grace_accepted",
                scope_key=record.scope_key,
                tier=self._tier,
                heartbeat_age_s=round(age, 3),
                ttl=record.ttl,
                supervisor_pid=supervisor_pid,
            )
        return healthy

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

    def mark_shutting_down(
        self, record: LeaseRecord, *, budget: Optional[float] = None
    ) -> None:
        """Publish a shutdown marker so discoverers stop resolving us
        immediately, before the record is unlinked.

        ``budget=None`` (default): blocking ``LOCK_EX``, unchanged from
        before nexus-cd1k0 review round 3 finding 6. ``budget=<seconds>``
        (the stop path, both tiers): ``LOCK_NB`` polled against the
        injected monotonic clock, raising ``ElectionBusyError`` once the
        budget is spent -- see ``DEFAULT_STOP_ELECTION_BUDGET``'s docstring
        for why every stop-path caller treats that as best-effort rather
        than fatal.
        """
        with self._elect(record.scope_key, budget=budget):
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

    def relinquish(
        self, record: LeaseRecord, *, budget: Optional[float] = None
    ) -> None:
        """Release ``scope_key`` on graceful shutdown, but only if we still
        own it. A delayed shutdown from a fenced predecessor must not
        unlink a successor's record (CA-4).

        ``budget=None`` (default): blocking ``LOCK_EX``, unchanged from
        before nexus-cd1k0 review round 3 finding 6. ``budget=<seconds>``
        (the stop path, both tiers): ``LOCK_NB`` polled, raising
        ``ElectionBusyError`` once spent -- see
        ``DEFAULT_STOP_ELECTION_BUDGET``'s docstring. On that error nothing
        is written and nothing is unlinked (the ``with self._elect(...)``
        block is never entered), so a busy flock leaves the lease exactly
        as it was: still ours on disk, but about to be torn down anyway by
        the caller's own kill path -- it ages out via TTL instead of being
        explicitly relinquished.

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
        with self._elect(record.scope_key, budget=budget):
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
        except ElectionBusyError as exc:
            # nexus-59bah: not a fence. We still own the lease; one stamp is
            # skipped and the next tick retries. Logged so a repeated holder
            # is visible before the lease ages out.
            _log.warning(
                "service_supervisor_heartbeat_election_busy",
                scope=self._scope_key,
                budget_s=self._registry.heartbeat_election_budget,
                error=str(exc),
            )

    @property
    def last_heartbeat_phases(self) -> Mapping[str, float]:
        """The registry's stamp sub-phase seconds for the last tick
        (nexus-wo6sc). Exposed here so a tier's timing wrapper can fold them
        into its own log line without reaching into the registry."""
        return self._registry.last_heartbeat_phases

    @property
    def last_heartbeat_unaccounted(self) -> float:
        """Seconds the last stamp spent inside no timed syscall (nexus-wo6sc)."""
        return self._registry.last_heartbeat_unaccounted

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


def fenced_exit_code(fenced: bool) -> int | None:
    """The run-loop exit code a tier's supervisor MUST use once its
    ``ServiceSupervisor.fenced`` flag is True, or ``None`` when not
    fenced (keep heartbeating as usual).

    Shared primitive (RDR-149 §shared primitive, nexus-cd1k0.2): fencing
    itself already lives here (``ServiceSupervisor.heartbeat_tick`` sets
    ``.fenced`` on ``StaleOwnerError``); what was missing was a SINGLE
    place naming what a fenced owner's run loop does next, so two tiers
    could not independently pick two different answers (one exiting 0,
    one exiting non-zero, for the identical fact). The answer is always
    0: a fenced owner already lost the race for this scope to a
    strictly-higher-generation successor — there is no rematch to win by
    respawning, so exiting non-zero here would only trip the OS unit's
    restart policy (``Restart=on-failure`` / ``KeepAlive
    SuccessfulExit=false``) into a doomed repeat.

    This helper is a pure predicate; it does not call ``sup.stop()``.
    The caller's own run-loop tail already calls ``stop()`` uniformly for
    every exit reason, and doing so on a fenced owner is safe BY
    CONSTRUCTION: ``ServiceRegistry.mark_shutting_down``/``relinquish``
    both compare ``owner_token`` against the CURRENT record under the
    election flock and no-op on a mismatch (CA-4) — a fenced
    predecessor's own last-known record never matches the successor's,
    so its shutdown path can never touch the successor's lease. Nothing
    else in a tier's ``stop()`` reaches into a resource this owner does
    not exclusively hold (Postgres, notably, is never stopped by any
    tier's ``stop()``); only THIS owner's own child process is torn down.
    """
    return 0 if fenced else None


def reclaim_lease_if_dead_owner(
    registry: "ServiceRegistry",
    record: "LeaseRecord",
    *,
    log: Any = None,
    event: str = "service_registry_dead_owner_reclaimed",
) -> bool:
    """True when *record* was held by a DEAD supervisor and has just been
    relinquished — the caller must proceed to (re)spawn rather than
    short-circuit onto it. False when the record's owner is genuinely
    alive, or the record carries no ``supervisor_pid`` at all (legacy /
    non-supervised — left untouched, never reclaimed spuriously), and
    should be honored as the live owner.

    Shared primitive (RDR-149 §shared primitive, nexus-cd1k0.17): a
    hard-crashed supervisor (OOM-kill, SIGKILL with no relinquish) leaves
    a lease that is still TTL-FRESH — ``ServiceRegistry.discover()``'s
    pure lease-freshness contract (this module's "liveness is lease
    freshness, not pid" invariant) correctly returns it as live, and that
    invariant is UNCHANGED here. This answers a narrower, DIFFERENT
    question on top of a lease HIT (never a miss): is the fresh lease's
    OWNER actually still running, so a self-healing spawner can decide
    whether to honor it or reclaim and respawn. Previously implemented
    ONCE, in the CLI client-spawn path
    (``commands/daemon.py.ensure_storage_supervisor``) only — the
    foreground unit path (``storage_service_daemon._start_locked``) had
    no equivalent, so a unit-launched supervisor discovering a
    dead-owner's fresh lease exited 0 (via ``exit_if_process_unowned``)
    and the OS unit's restart-on-success-exit=never policy left the
    stack down until the lease aged out on its own.

    ``_pid_is_running`` (not ``_pid_is_alive``, nexus-o8dil.21): a ZOMBIE
    supervisor — hard-killed, parent not yet reaped it (routine under a
    non-init PID 1, e.g. a container or CI runner) — answers
    ``os.kill(pid, 0)`` indefinitely; the alive-only probe would never
    fire for exactly the crashed-supervisor case this exists to catch.

    Relinquish is best-effort: a failure is logged (when *log* is given)
    but never raised — the caller's own spawn attempt will publish a
    higher generation regardless (CA-4 fencing prevents double-ownership
    even if the stale record briefly lingers), so a relinquish failure
    must not block the respawn it exists to enable.

    RECYCLED-PID TRADE-OFF (nexus-cd1k0 review round 2, finding 3 —
    stated explicitly, not left implicit): ``pid_running`` is a pure
    ``kill(pid, 0)`` + zombie check, with no identity or cmdline
    verification against the pid it is asked about. If the kernel
    recycles ``supervisor_pid`` to an UNRELATED live process within the
    lease's TTL window (15s for storage_service), that unrelated
    process's mere existence reads as "the owner is alive" — the heal
    does NOT fire, and the stack stays down until the lease naturally
    ages out past its TTL, at which point ``discover()``'s own
    lease-freshness reap takes over regardless. This FAILS SAFE: the
    consequence is a bounded DELAY (at most one TTL window), never
    double-ownership or data corruption — CA-4 generation fencing still
    protects ownership even if this heal is late or never fires at all.
    Recorded here per AGENTS.md's pid-liveness exception list (this
    function is now named there alongside ``sweep_matching_processes``,
    the primitive's other documented pid-based mechanism).
    """
    supervisor_pid = record.payload.get("supervisor_pid")
    if not (isinstance(supervisor_pid, int) and supervisor_pid > 0):
        return False
    if pid_running(supervisor_pid):
        return False
    if log is not None:
        log.warning(
            event,
            supervisor_pid=supervisor_pid,
            scope=record.scope_key,
            msg="fresh lease held by a dead supervisor; relinquishing + re-spawning",
        )
    try:
        registry.relinquish(record)
    except Exception as exc:  # noqa: BLE001 — best-effort reclaim; generation fencing still protects ownership
        if log is not None:
            log.warning(
                f"{event}_relinquish_failed",
                supervisor_pid=supervisor_pid,
                scope=record.scope_key,
                error=str(exc),
            )
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
        proc = run_bounded(
            ["ps", "-wweo", "pid,etime,command"],
            timeout=15,
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
        probe = run_bounded(
            ["ps", "-ww", "-p", str(pid), "-o", "command="],
            timeout=10,
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
        probe = run_bounded(
            ["ps", "-o", "state=", "-p", str(pid)],
            timeout=10,
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
    profile's supervisor into a kill set. The engine match is a literal
    prefix match on the whole *engine_path* string for the same reason
    (never a substring match on a `tail .../nexus-service.log` or similar
    diagnostic command) — see the space-safety note below for why this is
    a prefix check rather than a ``.split()``-then-compare.

    Flagless unit-launched supervisors — LEGACY ONLY (nexus-cd1k0.3,
    narrowed to legacy by nexus-cd1k0.19 review round 2, finding 4): the
    shipped launchd/systemd units USED TO exec ``nx daemon service start
    --foreground`` with NO ``--config-dir`` token at all —
    ``ensure_storage_supervisor`` (the client spawn path) always passed
    the flag, but a unit's ``ExecStart``/``ProgramArguments`` never went
    through that path, so a unit-launched supervisor was invisible to
    this matcher entirely: with an expired lease, ``nx daemon service
    stop`` swept only the engine, the supervisor exited non-zero on its
    child's death, and the OS unit restarted the whole stack. That gap is
    now closed at the SOURCE, not just papered over here: ``nx daemon
    service install`` (``daemon/installer.py`` + ``commands/daemon.py``'s
    ``_render_template``) now bakes an explicit, resolved-absolute
    ``--config-dir`` into every unit it generates, and
    ``ensure_storage_supervisor``/``upgrade_finish.py``'s own ``nx daemon
    service start``/``stop`` invocations both resolve theirs explicitly
    too — a supervisor spawned or unit generated from this point on is
    NEVER flagless. The flagless-matches-default fallback below therefore
    exists ONLY for a unit installed BEFORE this fix (legacy on-disk
    units this code does not rewrite in place): a command with no
    ``--config-dir`` token is treated as belonging to the DEFAULT config
    dir — the directory a flagless process resolves to on its own
    (``nexus.config.nexus_config_dir()``'s own fallback, mirrored here as
    a literal so this check never depends on THIS process's own
    ``NEXUS_CONFIG_DIR``, only on what a bare invocation would resolve
    to) — and to NOTHING else: an explicit non-default *config_dir* (an
    isolated test stack, a second profile) must never be matched by a
    flagless command, or a live default-dir ``stop`` would sweep an
    unrelated isolated stack and vice versa.

    Known, DOCUMENTED limitation, ASYMMETRIC (not merely "unresolvable" —
    nexus-cd1k0 review round 2, finding stated precisely rather than
    understated): this matcher sees only argv (the process table's
    ``command`` string), never a process's environment. A process that
    sets ``NEXUS_CONFIG_DIR`` itself (rather than passing
    ``--config-dir``) resolves to that env var's directory in the real
    process, but is indistinguishable here from a flagless process that
    truly means the default — both look identical from argv alone. The
    failure direction is NOT neutral: it is biased toward matching the
    DEFAULT dir, so a live default-dir ``stop`` could wrongly sweep an
    unrelated env-scoped process (false-positive collateral kill), while
    an explicit ``stop`` targeting THAT env-scoped stack by its real
    config_dir would find nothing (false negative — it looks flagless,
    which only ever matches the default). Two REAL vectors for this,
    identified and closed at the source above rather than left live:
    ``upgrade_finish.py``'s two bare CLI invocations (now pass
    ``--config-dir`` explicitly) and the e2e sandbox scripts (which set
    ``NEXUS_CONFIG_DIR`` via environment for real ``nx`` subprocess
    invocations — those invocations' OWN spawned supervisors are
    argv-explicit via ``ensure_storage_supervisor``, so they were never
    actually flagless once spawned; the risk was specifically the CLI
    calls that never spawned anything of their own and relied on
    ``stop``/``start``'s own bare re-derivation). Currently DORMANT for
    any REMAINING flagless case: the two shipped unit templates
    (``conexus/daemon/com.nexus.service.plist``,
    ``conexus/daemon/nexus-service.service``) never set
    ``NEXUS_CONFIG_DIR`` via environment, only ``PATH`` — there is no
    portable, unprivileged way to read another process's environment
    from this module (see ``process_command``'s procfs/``ps`` split), so
    this case is accepted as unresolvable rather than silently
    mismatched against a guess.

    Space-safety (nexus-cd1k0.3): the process table's ``command`` string
    is already a SPACE-JOINED rendering of the real argv (``/proc/<pid>/
    cmdline`` NUL bytes replaced with spaces, or ``ps``'s single-string
    output) — true argv boundaries are lost before this function ever
    sees the string, so a config_dir containing a space cannot be
    recovered by re-splitting on whitespace: ``command.split()`` would
    slice it apart and never match. Every comparison below is therefore
    a literal SUBSTRING/PREFIX/SUFFIX check against the whole
    *engine_path* / *target* string, never a re-tokenization — correct
    for a config_dir with an EMBEDDED space, as far as this platform's
    process-table abstraction allows; it cannot help a config_dir that
    also embeds a value indistinguishable from a following flag or the
    NUL-turned-space bytes.
    """
    engine_path = str(config_dir / "service" / "nexus-service")
    target = str(config_dir)
    # nexus-cd1k0.6 finding (9): an engine launched via an EXPLICIT
    # NEXUS_SERVICE_BIN / NEXUS_SERVICE_JAR override (the dev/test opt-in
    # storage_service_daemon.py's _resolve_launch_artifact honours) runs
    # from a path OUTSIDE <config_dir>/service/nexus-service, so the
    # well-known-path check above never matched its process-table row —
    # every caller of this matcher (the changelog-lock liveness gate that
    # gates `_release_stale_changelog_lock`, `stop`, `restart-stale`'s
    # sweep) saw no engine at all and could treat a genuinely alive,
    # possibly-migrating engine as dead. These overrides are read from
    # THIS process's own environment, same as the launch that resolved
    # them (`_resolve_launch_artifact` reads the identical env vars), and
    # canonicalized the same way (`Path(...).resolve(strict=False)`) so
    # the comparison matches what actually landed in the spawned argv.
    # Native (argv[0] = binary path): same position-anchored check as the
    # well-known path. The alternate JVM launch kind's argv marker is
    # resolved via a helper HOSTED IN storage_service_daemon.py, not
    # constructed here — RDR-161's amendment confines every literal
    # identifier for that launch kind to that one sanctioned module (see
    # tests/daemon/test_rdr161_native_only_gate.py), so this module never
    # spells the launch flag itself, only calls the helper that does.
    bin_override = os.environ.get("NEXUS_SERVICE_BIN", "").strip()
    engine_override_path = (
        str(Path(bin_override).resolve(strict=False)) if bin_override else None
    )
    from nexus.daemon.storage_service_daemon import (  # noqa: PLC0415 — deferred, avoids an import cycle (storage_service_daemon imports FROM this module at load time)
        jar_launch_stack_marker,
    )

    jar_override_marker = jar_launch_stack_marker()
    # The literal default a FLAGLESS process resolves to on its own
    # (nexus.config.nexus_config_dir()'s fallback branch) — NOT that
    # function itself, so this never depends on this process's own
    # NEXUS_CONFIG_DIR.
    is_default_target = config_dir == (Path.home() / ".config" / "nexus")
    config_dir_eq = f" --config-dir={target}"
    config_dir_sp = f" --config-dir {target}"

    def _match(command: str) -> bool:
        if command == engine_path or command.startswith(engine_path + " "):
            return True
        if engine_override_path is not None and (
            command == engine_override_path
            or command.startswith(engine_override_path + " ")
        ):
            return True
        if jar_override_marker is not None and jar_override_marker in command:
            return True
        if "daemon service start" not in command:
            return False
        if command.endswith(config_dir_eq) or (config_dir_eq + " ") in command:
            return True
        if command.endswith(config_dir_sp) or (config_dir_sp + " ") in command:
            return True
        if "--config-dir" in command:
            return False  # an explicit OTHER config-dir; never match by omission
        return is_default_target

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
