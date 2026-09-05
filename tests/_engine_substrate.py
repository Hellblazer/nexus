# SPDX-License-Identifier: AGPL-3.0-or-later
"""Session-scoped engine-backed T2 test substrate (RDR-155 P4b P0a').

Decision D-A (nexus-g37fr, 2026-07-23): the unit suite's T2 substrate is
the REAL engine over the bundled PG — integration-over-mocks and
PG-in-every-mode, applied to the test suite itself. ONE hermetic
PG + one shaded-JAR service boot per pytest session; per-test isolation
comes from a freshly MINTED tenant + tenant-bound token per test: the
engine binds tenant to the BEARER server-side (AuthFilter Decision 1 —
the ``X-Nexus-Tenant`` header is IGNORED), so handing each test its own
token isolates every row via RLS with no sharing and no cleanup.

Laziness contract: nothing boots at import. ``ensure_engine()`` is
memoized; the conftest autouse fixture calls it only when the collected
session actually imported ``nexus.db.t2`` (test modules import at
collection time, so ``sys.modules`` is a correct static signal). A
pure-unit dev run never pays the ~10s boot.

Fail-loud contract (gates-scripted-not-ambient): a missing or stale JAR
FAILS the tests that need the substrate with the build command — never a
silent mass-skip (the vacuous-green class).

Crash-durable teardown (nexus-lgdy1 fix b): ``atexit`` alone is not a
teardown mechanism — it never fires on SIGKILL, OOM, or a hard-killed
pytest, so a crashed session's postmaster + engine JAR reparent to PID 1
and squat their ports forever (T2 nexus/cascade-test-container-failure-
diagnosis-2026-07-31). Every ``_boot()`` now (1) writes a PID/port
sidecar next to its pgdata dir recording the owning pytest PID plus each
child's expected cmdline, and (2) sweeps stale ``nexus_t2_substrate_pg_*``
clusters at session start — a cluster is stale only when its recorded
OWNER PID is dead (never PPID-based: ``pg_ctl start`` daemonizes even for
a live session, so a fresh postmaster's PPID is 1 within moments of
boot — only the sidecar's owner-pid check tells stale from live). See
:func:`sweep_stale_substrate_clusters`.

Sidecar-write TIMING (nexus-ui654 follow-up, round 2 — critic Q1/Q3): the
sidecar is written THREE times per boot, not once at the end — (1) a
PLACEHOLDER immediately after ``initdb`` succeeds, still inside the boot
semaphore (postmaster/engine fields ``None`` — nothing is running yet, so
nothing to KILL, but the DIRECTORY itself is reapable debris from the
instant it exists); (2) updated once the postmaster is confirmed up
(postmaster identity added; engine fields still ``None``); (3) updated
again once the JVM ``Popen`` call returns (engine PID added — known
synchronously, well before the up-to-60s ``_wait_tcp`` call that
follows it). The original nexus-lgdy1 shape wrote the sidecar only once,
at the very END of ``_boot()`` — after createdb, role bootstrap, JVM
spawn, and the full TCP-wait — leaving a real, observed (not
hypothetical) window in which a kill produced a SIDECAR-LESS cluster that
``_sweep_legacy_cluster`` permanently refuses to auto-reap.

Round 1 of this follow-up moved the first sidecar write to right after
the postmaster comes up and claimed this shrank the un-reapable window to
"milliseconds" — WRONG under the exact contention this bead targets: the
boot semaphore sits BEFORE that write in program order, and its own wait
can legitimately run up to ``_BOOT_SEMAPHORE_ACQUIRE_TIMEOUT_S`` (300s)
under N-session x M-worker contention, so the true window was bounded by
THAT wait, not by milliseconds. Round 2's first attempt at a fix --
writing a placeholder sidecar immediately after ``mkdtemp()``, before
even acquiring the semaphore -- was tried and is WRONG for a different
reason: ``initdb`` refuses to initialize a non-empty target directory, so
a sidecar file landing inside pgdata before ``initdb`` runs breaks the
boot outright (verified directly against a real ``initdb`` invocation).
The actual fix: ``tempfile.mkdtemp()`` itself moved INSIDE the semaphore
(the directory does not exist at all while a boot is merely queued behind
others), and the first sidecar write happens immediately after ``initdb``
succeeds -- bounded by ``initdb``'s own runtime (critic-measured ~0.62s
uncontended), never by the semaphore's queue-wait, because directory
creation only happens once the wait is already over. Concurrency-window
control (``_boot_semaphore_slot`` itself) is a separate, complementary
mechanism — it bounds how many boots run at once; this fix bounds how
LONG each boot's own un-reapable window lasts (now: initdb's own runtime,
not the semaphore's wait).
"""
from __future__ import annotations

import atexit
import json
import os
import random
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
import warnings
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import structlog

from nexus._locking import lock_file, unlock_file
from tests.db._service_fixture import (
    SERVICE_ROLES_SQL,
    jar_freshness_skip_reason,
    pg_bin_dir,
)

_log = structlog.get_logger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_JAR = _REPO_ROOT / "service" / "target" / "nexus-service-1.0-SNAPSHOT.jar"

_BEARER = "t2-substrate-session-bearer"
_DBNAME = "nexus_t2_substrate"

_lock = threading.Lock()
_state: dict | None = None
_boot_error: str | None = None

#: Glob for a substrate pgdata cluster dir, matched under the process
#: tempdir root (``tempfile.gettempdir()`` — same default ``mkdtemp`` uses).
_STRAY_GLOB = "nexus_t2_substrate_pg_*"

#: Sidecar filename, written INSIDE the pgdata dir so it is glob-scoped by
#: construction and removed for free whenever the cluster dir is.
_SIDECAR_FILENAME = "nexus_substrate_sidecar.json"

#: Bind a fixed sub-range BELOW every ephemeral range this suite has to
#: coexist with (nexus-lgdy1 fix b item 4): Linux's default
#: ``net.ipv4.ip_local_port_range`` starts at 32768; macOS / Docker
#: Desktop's ephemeral publish range starts at the IANA 49152. A port the
#: kernel never hands out to an ephemeral allocation in the first place
#: cannot collide with one Docker later publishes a container onto — this
#: is defense in depth on top of the sweep, not a replacement for it (a
#: leak inside THIS range can still collide with a later boot inside this
#: same range).
_LOW_PORT_RANGE = range(20000, 29000)
_LOW_PORT_ATTEMPTS = 200

#: xdist worker sharding for the low port range (round-3 critique,
#: Significant-2). Under ``uv run pytest -n auto`` (the documented local
#: dev-loop invocation; CI's pytest-split matrix is cross-RUNNER, not
#: xdist, so this does not apply there), every worker is a SEPARATE
#: process that independently calls ``_boot()`` -> ``_free_port()`` — all
#: drawing from the SAME 9000-port range instead of the prior effectively
#: unbounded OS-ephemeral pool. ``_free_port``'s bind-probe-then-close is
#: not atomic across the gap to the eventual ``pg_ctl``/``java`` bind, so
#: two workers CAN race onto the same port; a loser doesn't get a soft
#: retry — ``ensure_engine()`` remembers ``_boot_error`` and re-raises it
#: for the rest of that worker's session, reading as an unrelated flaky
#: failure. Sharding by worker index removes the collision surface for
#: the common case (a worker never even PROBES another worker's slice);
#: a worker index beyond the sharded capacity, or no xdist worker at all,
#: falls back to the FULL range — narrowing the search space, never
#: silently degrading below the existing bind-probe + loud-fallback
#: behaviour.
_WORKER_SHARD_WIDTH = 500
_WORKER_SHARD_BASE = _LOW_PORT_RANGE.start
_WORKER_SHARD_MAX_INDEX = (
    (_LOW_PORT_RANGE.stop - _LOW_PORT_RANGE.start) // _WORKER_SHARD_WIDTH - 1
)

#: nexus-v460j: resolution is LAZY — the import-time ``pg_bin_dir()`` call
#: this replaced could DOWNLOAD the PG bundle at collection start (cold
#: cache), so every pytest leg had a hard network dependency before a
#: single test ran; one reset connection killed the ``-m lint`` leg, a leg
#: configured to touch no substrate at all (PR #1474, 2026-08-23).
#:
#: The AMBIENT-ENV contract the old import-time resolution existed for is
#: kept by snapshotting the discovery-relevant env at MODULE IMPORT time
#: (collection start, before any per-test HOME/NEXUS_CONFIG_DIR
#: monkeypatching) and applying that snapshot around the deferred
#: resolution — so a first use that happens to run under a monkeypatched
#: test still discovers against the ambient config dir, exactly as before
#: (same contract as tests/db/_service_fixture.pg_bin_dir's own
#: import-time resolution note).
_PG_AMBIENT_ENV_KEYS = (
    "NEXUS_PG_BIN", "NEXUS_PG_BUNDLE", "NEXUS_CONFIG_DIR", "HOME", "PATH",
)
_PG_AMBIENT_ENV: dict[str, str | None] = {
    k: os.environ.get(k) for k in _PG_AMBIENT_ENV_KEYS
}
_pg_bin_resolved: Path | None = None
_pg_bin_lock = threading.Lock()


def _pg_bin() -> Path:
    """The PG bundle ``bin/`` dir, resolved once on FIRST REAL USE under the
    import-time ambient env snapshot (may download on a cold cache — which is
    exactly why it must not run at collection time). The lock serializes the
    env-swap window against any concurrent in-process caller (review round 2:
    ``sweep_stale_substrate_clusters`` can reach here from a test body)."""
    global _pg_bin_resolved
    with _pg_bin_lock:
        if _pg_bin_resolved is None:
            saved = {k: os.environ.get(k) for k in _PG_AMBIENT_ENV_KEYS}
            try:
                for k, v in _PG_AMBIENT_ENV.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v
                _pg_bin_resolved = pg_bin_dir()
            finally:
                for k, v in saved.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v
        return _pg_bin_resolved


def _worker_shard_range() -> range:
    """This process's slice of :data:`_LOW_PORT_RANGE`, derived from
    ``PYTEST_XDIST_WORKER`` (pytest-xdist sets this to ``"gw0"``,
    ``"gw1"``, ... in each worker process; unset outside ``-n auto`` — a
    plain ``pytest`` / ``-n 0`` invocation, or a non-pytest caller, gets
    the FULL range, identical to pre-sharding behaviour).

    A worker index at or beyond the range's sharding capacity (more
    workers than 500-port slices fit in 9000 ports), or a malformed
    worker id, falls back to the full range rather than computing an
    out-of-bounds or empty slice — still bind-probed, still loud on
    exhaustion, just without the disjoint-shard guarantee.
    """
    worker = os.environ.get("PYTEST_XDIST_WORKER", "")
    if not worker.startswith("gw"):
        return _LOW_PORT_RANGE
    try:
        index = int(worker[2:])
    except ValueError:
        return _LOW_PORT_RANGE
    if index < 0 or index > _WORKER_SHARD_MAX_INDEX:
        if index > _WORKER_SHARD_MAX_INDEX:
            # Beyond-capacity fallback loses the disjoint-shard TOCTOU
            # guarantee — say so, matching _free_port's loud-on-exhaustion
            # pattern (critique T2 [21526] round 3, Minor (b)).
            warnings.warn(
                f"PYTEST_XDIST_WORKER={worker!r} exceeds the "
                f"{_WORKER_SHARD_MAX_INDEX + 1}-shard port-sharding "
                "capacity; falling back to the full range without the "
                "disjoint-shard guarantee (nexus-lgdy1)",
                stacklevel=2,
            )
        return _LOW_PORT_RANGE
    start = _WORKER_SHARD_BASE + index * _WORKER_SHARD_WIDTH
    return range(start, start + _WORKER_SHARD_WIDTH)


def _free_port(
    *, prefer_range: range | None = None, attempts: int = _LOW_PORT_ATTEMPTS,
) -> int:
    """A bindable port, preferring this worker's slice of the fixed low
    sub-range (see :data:`_LOW_PORT_RANGE` / :func:`_worker_shard_range`)
    over a true OS-ephemeral one. *prefer_range* defaults to
    ``_worker_shard_range()`` — resolved per call, not at import time, so
    it reflects whatever ``PYTEST_XDIST_WORKER`` the CURRENT process
    actually has (pass it explicitly to pin a specific range, as the test
    suite does).

    Falls back to ``bind(("127.0.0.1", 0))`` — the original behaviour —
    when *prefer_range* is exhausted after *attempts* random probes (many
    concurrent xdist workers sharing a fallback range, or the range
    genuinely full). The fallback is loud: silently reverting to
    ephemeral-range collision risk without a trace would undo the whole
    point of this range.
    """
    if prefer_range is None:
        prefer_range = _worker_shard_range()
    candidates = list(prefer_range)
    random.shuffle(candidates)
    for port in candidates[:attempts]:
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    warnings.warn(
        f"nexus test substrate: low port range {prefer_range.start}-"
        f"{prefer_range.stop} exhausted after {attempts} probes; falling "
        "back to an OS-ephemeral port for this boot (Docker-publish "
        "collision risk reverts to baseline).",
        stacklevel=2,
    )
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _read_postmaster_pid(pgdata: str) -> int | None:
    """The postmaster's own PID from ``<pgdata>/postmaster.pid`` (PG's own
    lock-file convention — its first line is always the postmaster PID),
    or ``None`` when the file is absent/unreadable/malformed."""
    try:
        first_line = Path(pgdata, "postmaster.pid").read_text().splitlines()[0]
        return int(first_line.strip())
    except (OSError, ValueError, IndexError):
        return None


def _wait_tcp(host: str, port: int, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.3):
                return
        except OSError:
            time.sleep(0.1)
    raise TimeoutError(f"engine substrate: port {port} not reachable after {timeout}s")


#: Cross-process bound on concurrent PG *boot* operations (initdb +
#: pg_ctl start), NOT on concurrent LIVE substrate PG instances
#: (nexus-ui654). macOS's SysV shared-memory budget is small and
#: MACHINE-WIDE -- shared with Docker Desktop, browsers, every other
#: process on the box, not just this suite (``sysctl kern.sysv.shmmni``
#: read 32 on the box this was diagnosed on). initdb's bootstrap
#: standalone backend and pg_ctl start's postmaster each transiently
#: allocate SysV shm segments during the startup sequence; under
#: ``pytest -n auto`` every xdist worker independently boots its own
#: substrate (see the worker-sharding comment above
#: ``_WORKER_SHARD_WIDTH`` -- one boot per WORKER process, not one per
#: session), so N concurrent pytest sessions x M workers-per-session can
#: spike well past 32 in-flight segments even though the STEADY-STATE
#: running-cluster count is fine afterward -- this is what produced the
#: observed "could not create shared memory segment: No space left on
#: device" setup-error storms (bead comment, REFINED PICTURE:
#: postgres process count self-resolved once concurrent sessions
#: finished -- the acute failure is the concurrency WINDOW, not
#: permanent leakage). Bounding concurrent BOOTS (not concurrent live
#: PGs, and not the whole test session) to a small number leaves
#: headroom for everything else on the machine that also touches SysV
#: shm. 4 is conservative: 1-2 segments were the actual observed cost
#: per boot, so 4 concurrent boots stays well inside a 32-segment
#: budget with margin left for the rest of the box.
_MAX_CONCURRENT_PG_BOOTS = 4

#: Fixed pool of N lockfiles under the process tempdir root -- try-acquire
#: in slot order (0, 1, 2, ...), never a per-boot-named file, so this
#: directory itself never accumulates debris the way a per-boot tempdir
#: would.
_BOOT_SEMAPHORE_DIR = Path(tempfile.gettempdir()) / "nexus_t2_substrate_boot_locks"

#: Generous: a slow/loaded box waiting out a genuine queue of concurrent
#: boots is expected, not a hang. Failing loud after this window (rather
#: than blocking forever) is what makes contention diagnosable instead of
#: looking like an unrelated stall.
_BOOT_SEMAPHORE_ACQUIRE_TIMEOUT_S = 300.0
_BOOT_SEMAPHORE_POLL_S = 1.0
_BOOT_SEMAPHORE_LOG_EVERY_S = 10.0


def _try_acquire_boot_slot(lock_dir: Path, max_concurrent: int):
    """Non-blocking: try every slot in order (0, 1, ..., max_concurrent-1),
    return the held-and-locked file handle for the first free one, or
    ``None`` if every slot is currently held elsewhere. Side-effect-free on
    failure -- every opened-but-unlocked fd is closed before moving to the
    next slot or returning.
    """
    for slot in range(max_concurrent):
        path = lock_dir / f"slot-{slot}.lock"
        fh = open(path, "a+")  # noqa: SIM115 - lifetime is the caller's held slot, closed by _boot_semaphore_slot
        try:
            lock_file(fh, blocking=False)
        except BlockingIOError:
            fh.close()
            continue
        return fh
    return None


@contextmanager
def _boot_semaphore_slot(
    *,
    max_concurrent: int = _MAX_CONCURRENT_PG_BOOTS,
    lock_dir: Path = _BOOT_SEMAPHORE_DIR,
    timeout_s: float = _BOOT_SEMAPHORE_ACQUIRE_TIMEOUT_S,
    poll_s: float = _BOOT_SEMAPHORE_POLL_S,
) -> Iterator[None]:
    """Hold one of *max_concurrent* cross-process boot slots for the
    duration of the context.

    Bounds how many PG boot sequences (initdb + pg_ctl start) can run
    CONCURRENTLY across every pytest process on the machine (nexus-ui654)
    -- deliberately NOT the substrate's full session lifetime; callers
    wrap only the shm-heavy initdb/pg_ctl-start window and release
    immediately after, so a booted-and-running substrate never occupies a
    slot for the rest of its session.

    Waits LOUDLY: a structlog line every ``_BOOT_SEMAPHORE_LOG_EVERY_S``
    while contended, and a fail-loud ``RuntimeError`` naming this bead if
    *timeout_s* elapses with no slot free -- never a silent hang.
    """
    lock_dir.mkdir(parents=True, exist_ok=True)
    start = time.monotonic()
    deadline = start + timeout_s
    last_log = start
    fh = _try_acquire_boot_slot(lock_dir, max_concurrent)
    waited = False
    while fh is None:
        waited = True
        now = time.monotonic()
        if now >= deadline:
            raise RuntimeError(
                f"T2 engine substrate: timed out after {timeout_s}s waiting "
                f"for a boot slot -- all {max_concurrent} concurrent-PG-boot "
                f"slots busy under {lock_dir} (nexus-ui654: concurrent PG "
                "boots are bounded to protect macOS's tiny SysV shm "
                "budget). If this many pytest sessions are genuinely "
                "booting substrates at once, wait for one to finish; if the "
                "bound itself is wrong for this machine, raise "
                "_MAX_CONCURRENT_PG_BOOTS deliberately -- this is not a "
                "silent hang, it is the semaphore doing its job."
            )
        if now - last_log >= _BOOT_SEMAPHORE_LOG_EVERY_S:
            _log.info(
                "nexus_t2_substrate.boot_semaphore_wait",
                max_concurrent=max_concurrent,
                lock_dir=str(lock_dir),
                waited_s=round(now - start, 1),
                timeout_s=timeout_s,
                bead="nexus-ui654",
            )
            last_log = now
        time.sleep(poll_s)
        fh = _try_acquire_boot_slot(lock_dir, max_concurrent)
    if waited:
        _log.info(
            "nexus_t2_substrate.boot_semaphore_acquired",
            waited_s=round(time.monotonic() - start, 1),
            bead="nexus-ui654",
        )
    try:
        yield
    finally:
        unlock_file(fh)
        fh.close()


def _initdb_cluster(
    bin_dir: Path, *, prefix: str, parent_dir: str | None = None,
) -> str:
    """``mkdtemp`` + ``initdb`` a fresh cluster dir, cleaning up on failure.

    Call INSIDE :func:`_boot_semaphore_slot` — ``initdb`` starts a bootstrap
    standalone backend, which is one of the two SysV-shm allocations the
    semaphore exists to bound.

    Two behaviours the ``check=True, capture_output=True`` call this replaces
    did not have (both nexus-rbc7k, both measured):

    * **The directory is removed when initdb fails.** ``initdb`` removes the
      CONTENTS of a data directory it failed to initialize but leaves the
      directory itself, because it did not create it -- it says so:
      ``initdb: removing contents of data directory "..."``. Every failed
      boot therefore stranded an EMPTY ``nexus_t2_substrate_pg_*`` dir with
      no sidecar, which :func:`_sweep_legacy_cluster` reports and by design
      refuses to auto-reap. That is where the 21 empty cluster dirs on the
      dev box came from; they are debris from failed boots, not from live
      ones. ``_kill_pg`` only exists from ``pg_ctl start`` onward, so this
      earlier window had no cleanup at all.
    * **initdb's own stderr reaches the caller.** ``CalledProcessError``'s
      message carries the argv and the return code and DISCARDS the captured
      stderr, so a failed boot reported ``exit status 1`` and nothing else.
      The failure underneath nexus-rbc7k was a one-line PG FATAL
      (``could not create shared memory segment: No space left on device``,
      ``shmget`` -- macOS ``kern.sysv.shmmni`` is 32) that no consumer of
      this substrate could see.
    """
    pgdata = tempfile.mkdtemp(prefix=prefix, dir=parent_dir)
    proc = subprocess.run(
        [str(bin_dir / "initdb"), "-D", pgdata, "--no-locale", "-E", "UTF8",
         "--auth=trust"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        shutil.rmtree(pgdata, ignore_errors=True)
        raise RuntimeError(
            f"initdb failed (rc={proc.returncode}) for a throwaway cluster "
            f"under {prefix!r}:\n{proc.stderr}"
        )
    return pgdata


#: PostgreSQL's own wording when ``shmget`` returns ENOSPC. The FULL text is
#: ``could not create shared memory segment: No space left on device`` with
#: a DETAIL line naming the failed ``shmget`` call and a HINT that says to
#: raise SHMMNI -- and it is not about disk space, which the HINT also says.
_SHM_EXHAUSTED_MARKER = "could not create shared memory segment"

#: Bounded retry budget for a throwaway boot that loses the ``shmget`` race
#: (nexus-rbc7k). NOT a fallback: nothing degrades, nothing is skipped, and
#: an exhausted budget still raises with PG's own FATAL attached. It is a
#: resource-acquisition retry against a transient MACHINE-WIDE ceiling, the
#: same shape as ``nexus.retry._voyage_with_retry``. The window it rides out
#: is another boot's in-flight segments (a boot is ~1-3s), so the budget is
#: sized in single-digit seconds; a box whose STEADY state is at the ceiling
#: (measured: two concurrent 16-worker sessions peg it at exactly 32) is not
#: transient and correctly fails loud after the budget.
_THROWAWAY_BOOT_ATTEMPTS = 5
_THROWAWAY_BOOT_BACKOFF_S = 2.0


@contextmanager
def throwaway_pg_cluster(
    *,
    prefix: str = "nexus_throwaway_pg_",
    lock_dir: Path = _BOOT_SEMAPHORE_DIR,
    boot_timeout_s: float = _BOOT_SEMAPHORE_ACQUIRE_TIMEOUT_S,
    parent_dir: str | None = None,
    attempts: int = _THROWAWAY_BOOT_ATTEMPTS,
    backoff_s: float = _THROWAWAY_BOOT_BACKOFF_S,
) -> Iterator[Path]:
    """A real, socket-only PG cluster for a test that needs a live postmaster
    it may kill, torn down (``pg_ctl stop -m immediate`` + ``rmtree``) on the
    way out.

    Boots INSIDE the cross-process boot semaphore, and retries a boot that
    loses the SysV-shared-memory race (nexus-rbc7k). A PG boot needs two
    transient SysV segments -- ``initdb``'s bootstrap backend, then the
    postmaster -- against a machine-wide ``kern.sysv.shmmni``, which is 32 on
    macOS and cannot be raised without a reboot; under ``pytest -n auto``
    every xdist worker already holds one segment for its own session-long
    substrate postmaster, and other fixtures boot their own. Measured on the
    dev box: the segment table pegs at exactly 32 under load and the boot
    fails with ``could not create shared memory segment: No space left on
    device`` / ``shmget``.

    Both halves matter and neither is sufficient alone:

    * the semaphore stops this boot from BEING the extra concurrent boot
      (``test_stops_a_real_postmaster_via_pg_ctl_immediate`` booted by hand
      and was the only PG boot in the suite outside that bound);
    * the retry rides out the segments some OTHER in-flight boot holds,
      which no bound on this caller can control.

    ``listen_addresses = ''`` -- Unix socket only, inside the cluster dir.
    There is no TCP port to collide on, so this needs no port allocation.
    Cluster dirs come from ``tempfile.mkdtemp``, NOT pytest's ``tmp_path``,
    whose ``pytest-of-<user>/pytest-<N>/<test-name>/`` nesting overruns the
    103-byte Unix-domain-socket path limit on macOS.

    ``_boot`` deliberately does NOT share the retry: its own answer to this
    hazard is the semaphore plus a fail-loud setup error (nexus-ui654), and
    changing the substrate's boot contract is not this bead's business.
    """
    pgdata: Path | None = None
    bin_dir: Path | None = None
    for attempt in range(1, attempts + 1):
        try:
            with _boot_semaphore_slot(lock_dir=lock_dir, timeout_s=boot_timeout_s):
                # Resolved INSIDE the slot, unlike _boot's own call: nothing
                # before the slot means a caller that never gets one (the
                # contended case) never touches the PG bundle at all, which
                # is what lets the bound-ness of this path be tested without
                # a PG bundle present.
                bin_dir = _pg_bin()
                pgdata = Path(
                    _initdb_cluster(bin_dir, prefix=prefix, parent_dir=parent_dir)
                )
                try:
                    with open(pgdata / "postgresql.conf", "a") as f:
                        f.write("listen_addresses = ''\n")
                    proc = subprocess.run(
                        [str(bin_dir / "pg_ctl"), "-D", str(pgdata), "-l",
                         str(pgdata / "pg.log"), "-o", f"-k {pgdata}",
                         "start", "-w"],
                        capture_output=True, text=True,
                    )
                    if proc.returncode != 0:
                        try:
                            log_tail = (pgdata / "pg.log").read_text()[-2000:]
                        except OSError:
                            log_tail = "<no pg.log>"
                        raise RuntimeError(
                            f"pg_ctl start failed (rc={proc.returncode}) for "
                            f"the throwaway cluster at {pgdata}:\n"
                            f"{proc.stderr}\n--- pg.log tail ---\n{log_tail}"
                        )
                except BaseException:
                    shutil.rmtree(pgdata, ignore_errors=True)
                    pgdata = None
                    raise
            break
        except RuntimeError as exc:
            if _SHM_EXHAUSTED_MARKER not in str(exc) or attempt == attempts:
                raise
            _log.warning(
                "nexus_t2_substrate.throwaway_boot_shm_exhausted",
                attempt=attempt,
                attempts=attempts,
                backoff_s=backoff_s,
                bead="nexus-rbc7k",
                hint="machine-wide SysV segment table full (kern.sysv.shmmni)",
            )
            time.sleep(backoff_s)
    assert pgdata is not None and bin_dir is not None  # loop either broke or raised
    try:
        yield pgdata
    finally:
        subprocess.run(
            [str(bin_dir / "pg_ctl"), "-D", str(pgdata), "stop", "-m", "immediate"],
            capture_output=True,
        )
        shutil.rmtree(pgdata, ignore_errors=True)


def _boot() -> dict:
    """Boot hermetic PG + the shaded service JAR. Called once, under _lock."""
    try:
        sweep_stale_substrate_clusters()
    except Exception as exc:  # noqa: BLE001 - the sweep must never block boot (nexus-lgdy1)
        warnings.warn(
            f"nexus test substrate: session-start sweep failed unexpectedly: "
            f"{exc}",
            stacklevel=2,
        )
    stale = jar_freshness_skip_reason(_JAR)
    if stale:
        raise RuntimeError(
            f"T2 engine substrate unavailable: {stale}. The unit suite's T2 "
            "substrate is the real engine (RDR-155 P4b P0a', decision D-A) — "
            "build it with: mvn -f service/pom.xml package -DskipTests"
        )
    bin_dir = _pg_bin()
    if not bin_dir.exists():
        raise RuntimeError(
            "T2 engine substrate unavailable: no PostgreSQL binaries "
            "discoverable (NEXUS_PG_BIN / config-dir bundle / Homebrew / "
            "PATH). Install the PG bundle (nx init) or set NEXUS_PG_BIN."
        )

    pg_port = _free_port()
    pg_user = os.environ["USER"]
    # Concurrency-window control's EARLIEST checkpoint (nexus-ui654
    # follow-up round 2, critic Q1/Q3 -- corrects round 1's "millisecond
    # window" docstring claim, which the critic showed was wrong under
    # real contention: round 1 wrote the first sidecar right after the
    # postmaster came up, but the boot semaphore's own WAIT to even start
    # sits before that point and can legitimately run up to
    # `_BOOT_SEMAPHORE_ACQUIRE_TIMEOUT_S` (300s) under N-session x
    # M-worker contention).
    #
    # A naive fix -- writing a placeholder sidecar immediately after
    # ``mkdtemp()``, before the semaphore -- was TRIED and is WRONG:
    # ``initdb`` refuses to initialize a non-empty target directory
    # (verified directly: "initdb: error: directory ... exists but is not
    # empty" the instant a sidecar file lands inside pgdata first). So
    # ``mkdtemp()`` itself moves INSIDE the semaphore below, and the FIRST
    # sidecar write happens immediately after ``initdb`` succeeds (pgdata
    # is no longer empty at that point regardless -- initdb has already
    # populated it) but still well before ``pg_ctl start``. This closes
    # the gap correctly: the cluster directory now never exists without a
    # sidecar for longer than ``initdb``'s own runtime (critic-measured
    # ~0.62s uncontended), because directory creation and the first
    # sidecar write both happen only AFTER the semaphore wait is already
    # over, not before or during it.
    #
    # Nothing is running yet at first-write time (no postmaster, no
    # engine), so a sweep that reaps this placeholder has nothing to
    # kill -- but an unreapable directory (even a PG-data-only one) is
    # still exactly the debris shape the bead was filed about (31 stale
    # dirs manually swept in one day). Verified (not assumed) against
    # `_reap_cluster`: with every leg `None`, its per-leg
    # `isinstance(pid, int)` guard produces an EMPTY `legs` list,
    # `"mismatch" in {}.values()` is `False`, the kill loop over zero legs
    # is a no-op, and the function falls straight through to
    # `shutil.rmtree(cluster_dir, ...)` -- i.e. a fully-placeholder
    # sidecar IS swept and counted as `reaped`, not silently skipped by
    # any earlier candidate filter. Updated (not re-created) at each
    # later checkpoint below as more identity becomes known.
    with _boot_semaphore_slot():
        # Cleans the dir up if initdb fails, and carries initdb's stderr out
        # (nexus-rbc7k) -- see _initdb_cluster's docstring for both, and for
        # the 21 empty cluster dirs the previous shape stranded here.
        pgdata = _initdb_cluster(bin_dir, prefix="nexus_t2_substrate_pg_")
        sidecar_started_at = _write_sidecar(
            pgdata, pg_port=pg_port, svc_port=None,
            postmaster_pid=None, engine_pid=None,
        )
        with open(os.path.join(pgdata, "postgresql.conf"), "a") as f:
            f.write(f"\nport = {pg_port}\nlisten_addresses = '127.0.0.1'\n")
            # The suite issues thousands of tiny transactions; keep fsync off
            # for the throwaway test cluster.
            f.write("fsync = off\nsynchronous_commit = off\nfull_page_writes = off\n")
        subprocess.run(
            [str(bin_dir / "pg_ctl"), "-D", pgdata, "-l",
             os.path.join(pgdata, "pg.log"),
             "-o", f"-p {pg_port} -k {pgdata}", "start", "-w"],
            check=True, capture_output=True,
        )
    postmaster_pid = _read_postmaster_pid(pgdata)
    # Checkpoint 2 of 3 (nexus-ui654 follow-up, critic Q3): UPDATE the
    # sidecar (not re-create -- `started_at` stays pinned to the earliest
    # write above) now that the postmaster identity is known -- still well
    # before createdb + role bootstrap + JVM spawn + up to 60s of
    # TCP-wait, which is where the ORIGINAL nexus-lgdy1 shape wrote the
    # sidecar for the first and only time. engine_pid/svc_port are
    # genuinely not known yet -- still None; the reaper skips a None leg
    # rather than treating it as dead or mismatched (verified above), so
    # this partial sidecar is fully reapable for the postmaster leg it
    # does record.
    _write_sidecar(
        pgdata, pg_port=pg_port, svc_port=None,
        postmaster_pid=postmaster_pid, engine_pid=None,
        started_at=sidecar_started_at,
    )

    def _kill_pg() -> None:
        # Review finding (P0 remainder, Important 1): any failure after
        # pg_ctl start must stop PG before re-raising, or repeated failed
        # boots accumulate zombie postgres + tempdirs (the exact leak
        # class observed live during the flip dry-runs).
        subprocess.run(
            [str(bin_dir / "pg_ctl"), "-D", pgdata, "stop", "-m", "immediate"],
            capture_output=True,
        )
        shutil.rmtree(pgdata, ignore_errors=True)

    try:
        subprocess.run(
            [str(bin_dir / "createdb"), "-h", "127.0.0.1", "-p", str(pg_port),
             "-U", pg_user, _DBNAME],
            check=True, capture_output=True,
        )
        proc = subprocess.run(
            [str(bin_dir / "psql"), "-h", "127.0.0.1", "-p", str(pg_port),
             "-U", pg_user, "-d", _DBNAME, "-v", "ON_ERROR_STOP=1",
             "-c", SERVICE_ROLES_SQL],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"T2 engine substrate: role bootstrap failed:\n{proc.stderr}"
            )
    except BaseException:
        _kill_pg()
        raise

    svc_port = _free_port()
    env = {
        **os.environ,
        "NX_SERVICE_PORT": str(svc_port),
        "NX_SERVICE_TOKEN": _BEARER,
        "NX_DB_URL": f"jdbc:postgresql://127.0.0.1:{pg_port}/{_DBNAME}",
        "NX_DB_USER": "nexus_svc",
        "NX_DB_PASS": "nexus_svc_pass",
        "NX_POOL_SIZE": "8",
        "NX_DB_ADMIN_URL": f"jdbc:postgresql://127.0.0.1:{pg_port}/{_DBNAME}",
        "NX_DB_ADMIN_USER": pg_user,
        "NX_DB_ADMIN_PASS": "",
    }
    env.pop("NX_STORAGE_BACKEND", None)
    java = shutil.which("java")
    if java is None:
        _kill_pg()
        raise RuntimeError("T2 engine substrate: no java on PATH")
    # Engine output goes to a FILE, never a PIPE (nexus-j0nec root cause):
    # an undrained 64KB stdout pipe fills after ~250 tenant mints of
    # Logback console logging, write(2) blocks holding the PrintStream
    # monitor, and every logging thread pins behind it — presenting as a
    # total engine wedge. Evidence: jstack pin in FileOutputStream.writeBytes
    # from TokenStore.issueToken's log.info; draining exactly the pipe's
    # 65,702 buffered bytes instantly unwedged the next mint.
    svc_log_path = os.path.join(pgdata, "engine.log")
    svc_log = open(svc_log_path, "wb")  # noqa: SIM115 — lifetime spans the pytest session, closed with the process
    svc = subprocess.Popen(
        [java, "-jar", str(_JAR)], env=env,
        stdout=svc_log, stderr=subprocess.STDOUT,
        preexec_fn=os.setsid,
    )
    # Checkpoint 3 of 3: svc.pid is known SYNCHRONOUSLY the instant Popen
    # returns -- no reason to defer recording it until _wait_tcp succeeds
    # (which can legitimately take up to 60s). Update the sidecar now,
    # before the wait, so a kill during the wait still leaves a complete,
    # reapable sidecar (nexus-ui654 follow-up, critic Q3).
    _write_sidecar(
        pgdata, pg_port=pg_port, svc_port=svc_port,
        postmaster_pid=postmaster_pid, engine_pid=svc.pid,
        started_at=sidecar_started_at,
    )
    try:
        _wait_tcp("127.0.0.1", svc_port, timeout=60.0)
    except TimeoutError:
        try:
            os.killpg(os.getpgid(svc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
        svc_log.flush()
        with open(svc_log_path, encoding="utf-8", errors="replace") as fh:
            out = fh.read()[-2000:]
        _kill_pg()
        raise RuntimeError(
            f"T2 engine substrate: service did not bind port {svc_port}. "
            f"Tail of engine log ({svc_log_path}):\n{out}"
        ) from None

    state = {
        "base_url": f"http://127.0.0.1:{svc_port}",
        "bearer": _BEARER,
        "pgdata": pgdata,
        "pg_bin": bin_dir,
        # PG coordinates, so a test can query the substrate's schema DIRECTLY rather
        # than through the engine's HTTP surface. Added for nexus-20890, whose gate
        # asks information_schema what tables carry a denormalized collection column
        # — a question the API cannot answer and a changelog parser can only guess at.
        "pg_port": pg_port,
        "pg_user": pg_user,
        "pg_dbname": _DBNAME,
        "svc": svc,
    }
    atexit.register(_teardown)
    return state


def _teardown() -> None:
    global _state
    if _state is None:
        return
    svc = _state["svc"]
    try:
        os.killpg(os.getpgid(svc.pid), signal.SIGTERM)
        svc.wait(timeout=5)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(os.getpgid(svc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
    subprocess.run(
        [str(_state["pg_bin"] / "pg_ctl"), "-D", _state["pgdata"],
         "stop", "-m", "immediate"],
        capture_output=True,
    )
    shutil.rmtree(_state["pgdata"], ignore_errors=True)
    _state = None


# ── Crash-durable teardown: sidecar + session-start sweep (nexus-lgdy1) ──────
#
# atexit alone leaks a postmaster+engine PAIR on a hard-killed pytest; both
# reparent to PID 1 and squat an ephemeral port apiece, which Docker Desktop
# can then silently redirect a testcontainers client onto (T2 nexus/cascade-
# test-container-failure-diagnosis-2026-07-31). The sidecar records enough
# to re-identify each PID before ever signalling it; the sweep, run at the
# top of every _boot(), reaps a cluster ONLY when its recorded OWNER pytest
# PID is dead — a live owner (including this very boot, before its own
# sidecar exists) is never touched.
#
# NOTE for readers of a test's warning capture: a killed process that is a
# DIRECT CHILD of the caller (as the test suite's fake process trees are —
# real orphans reparented to PID 1 are not) stays a zombie in
# ``pid_alive``'s eyes until ITS OWN parent ``wait()``s on it, which can
# make ``_kill_engine_leg``/``_kill_postmaster_leg``'s post-SIGKILL check
# fire a "survived SIGKILL" warning even though the kill succeeded.
# Harmless test-harness artifact, not a production concern (init reaps its
# orphans promptly).


def _write_sidecar(
    pgdata: str, *, pg_port: int, svc_port: int | None,
    postmaster_pid: int | None, engine_pid: int | None,
    started_at: float | None = None,
) -> float:
    """Record enough identity to re-verify both children before a future
    sweep ever signals them: PID + full expected cmdline for each, plus
    the owning pytest PID whose liveness is the sole staleness signal.

    Written THREE times per boot (nexus-ui654 follow-up round 2, critic
    Q1/Q3 -- see ``_boot()``'s call sites, all inside/after the boot
    semaphore): (1) a PLACEHOLDER immediately after ``initdb`` succeeds
    (``postmaster_pid``/``engine_pid``/``svc_port`` all ``None`` -- none
    of it is known yet; a write BEFORE ``initdb`` was tried and rejected
    -- ``initdb`` refuses a non-empty target directory, so the sidecar
    cannot land inside pgdata until initdb has already populated it);
    (2) updated immediately once the postmaster is confirmed up
    (postmaster identity added); (3) updated again immediately after the
    JVM ``Popen`` call returns (engine identity added -- known
    synchronously, well before ``_wait_tcp`` ever gets a chance to time
    out). A leg recorded as ``None`` here is skipped by the reaper
    (``_reap_cluster``'s ``isinstance(pid, int)`` guard -- verified
    directly: an all-``None`` sidecar produces an EMPTY ``legs`` list,
    which short-circuits straight to ``shutil.rmtree`` and is reported as
    ``reaped``, never silently dropped by an earlier candidate filter),
    never mistaken for "dead" or "mismatch" -- so even the earliest,
    fully-placeholder sidecar is immediately reapable once its owner dies.
    ``mkdtemp()`` itself happens inside the boot semaphore (see
    ``_boot()``), so the cluster directory never exists at all during the
    semaphore's own (up to 300s) queue-wait -- only during ``initdb``'s
    own runtime (critic-measured ~0.62s uncontended) is there ever a
    directory with no sidecar in it.

    *started_at*: pass the value returned by an earlier call to keep it
    stable across all three writes for the same boot; omitted (``None``)
    captures a fresh timestamp -- used only by the first, placeholder call.

    Returns the ``started_at`` value actually written, so the caller can
    thread it into the later calls.
    """
    from nexus.daemon.service_registry import process_command

    resolved_started_at = started_at if started_at is not None else time.time()
    payload = {
        "owner_pytest_pid": os.getpid(),
        "owner_cmdline": process_command(os.getpid()),
        "postmaster_pid": postmaster_pid,
        "postmaster_cmdline": (
            process_command(postmaster_pid) if postmaster_pid else ""
        ),
        "engine_pid": engine_pid,
        "engine_cmdline": process_command(engine_pid) if engine_pid else "",
        "pg_port": pg_port,
        "svc_port": svc_port,
        "pgdata": pgdata,
        "started_at": resolved_started_at,
    }
    path = Path(pgdata) / _SIDECAR_FILENAME
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(path)  # atomic: a reader never sees a torn sidecar
    return resolved_started_at


@dataclass
class SweepResult:
    """Structured summary of one :func:`sweep_stale_substrate_clusters`
    pass. The sweep's primary voice is ``warnings.warn`` (loud, per the
    fail-loud contract); this is the assertable counterpart for tests."""

    reaped: list[str] = field(default_factory=list)
    live_untouched: list[str] = field(default_factory=list)
    mismatch_refused: list[str] = field(default_factory=list)
    legacy_reported: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def sweep_stale_substrate_clusters(*, tmp_root: Path | None = None) -> SweepResult:
    """Reap crash-orphaned ``nexus_t2_substrate_pg_*`` clusters.

    Called at the top of every ``_boot()`` (session-start sweep). Never
    raises: a per-cluster failure is a loud warning and the sweep moves on
    to the next cluster; nothing here may block substrate boot.
    """
    result = SweepResult()
    root = tmp_root if tmp_root is not None else Path(tempfile.gettempdir())
    if not root.exists():
        return result
    for cluster_dir in sorted(root.glob(_STRAY_GLOB)):
        if not cluster_dir.is_dir():
            continue
        try:
            _sweep_one_cluster(cluster_dir, result)
        except Exception as exc:  # noqa: BLE001 - loud, never fatal (nexus-lgdy1)
            result.errors.append(str(cluster_dir))
            warnings.warn(
                f"nexus test substrate sweep: error processing "
                f"{cluster_dir}: {exc}",
                stacklevel=2,
            )
    return result


def _sweep_one_cluster(cluster_dir: Path, result: SweepResult) -> None:
    sidecar_path = cluster_dir / _SIDECAR_FILENAME
    if sidecar_path.exists():
        _sweep_sidecar_cluster(cluster_dir, sidecar_path, result)
    else:
        _sweep_legacy_cluster(cluster_dir, result)


def _owner_is_live(owner_pid: int, expected_cmdline: str) -> bool:
    """True iff the recorded owner pytest process is STILL genuinely that
    process — not merely that some process now exists at that PID number.

    Round-3 critique, Significant-1: ``_write_sidecar`` has always
    recorded ``owner_cmdline``, but until now nothing ever read it back —
    owner liveness was ``pid_alive(owner_pid)`` alone, so a REUSED owner
    pid (a long-lived unrelated process — Docker daemon, an IDE, an MCP
    server — later started with that exact pid number) made a genuinely
    stale cluster ``live_untouched`` FOREVER, since nothing could ever
    prove the "owner" was gone. Same exact-compare discipline as
    :func:`_identify_leg`.

    Safe-direction on ambiguity, deliberately asymmetric: an EMPTY
    *expected_cmdline* (a sidecar written before this check existed, or a
    write-time read that came back empty) falls back to the pid-only
    signal rather than manufacturing a mismatch out of missing data. An
    UNREADABLE current cmdline (the pid exited between the liveness check
    and this read, or a permission edge) is treated as still-live — never
    a false-stale that could reap a cluster genuinely still in use.
    """
    from nexus.daemon.service_registry import pid_alive, process_command

    if not pid_alive(owner_pid):
        return False
    if not expected_cmdline:
        return True
    actual = process_command(owner_pid)
    if not actual:
        return True  # unreadable -- safe direction: assume still live
    return actual.strip() == expected_cmdline.strip()


def _sweep_sidecar_cluster(
    cluster_dir: Path, sidecar_path: Path, result: SweepResult,
) -> None:
    try:
        info = json.loads(sidecar_path.read_text())
    except (OSError, ValueError) as exc:
        result.errors.append(str(cluster_dir))
        warnings.warn(
            f"nexus test substrate sweep: unreadable sidecar {sidecar_path}: "
            f"{exc} — leaving cluster untouched",
            stacklevel=2,
        )
        return

    owner_pid = info.get("owner_pytest_pid")
    if not isinstance(owner_pid, int):
        result.errors.append(str(cluster_dir))
        warnings.warn(
            f"nexus test substrate sweep: sidecar {sidecar_path} missing/"
            "invalid owner_pytest_pid — leaving cluster untouched",
            stacklevel=2,
        )
        return

    if _owner_is_live(owner_pid, info.get("owner_cmdline", "") or ""):
        # Concurrent-session safety: a live owner's cluster (including this
        # very boot's own, pre-sidecar) is never touched.
        result.live_untouched.append(str(cluster_dir))
        return

    if _reap_cluster(cluster_dir, info, result):
        result.mismatch_refused.append(str(cluster_dir))
    else:
        result.reaped.append(str(cluster_dir))


#: (label, sidecar pid key, sidecar cmdline key) per leg, in REAP ORDER
#: (engine first — pool connections close before the server dies).
_LEG_SPECS = (
    ("engine", "engine_pid", "engine_cmdline"),
    ("postmaster", "postmaster_pid", "postmaster_cmdline"),
)


def _reap_cluster(cluster_dir: Path, info: dict, result: SweepResult) -> bool:
    """Verify identity of EVERY present leg FIRST (side-effect-free); kill
    NOTHING unless every present leg is confirmed either already-dead or a
    genuine identity match. Only then are the "ok" legs actually killed,
    in order (engine before postmaster — Hal's manual reap order: pool
    connections close before the server dies).

    Round-2 review, critical-adjacent finding: the previous shape reaped
    each leg independently INSIDE the identity-check loop, so a matched
    postmaster got killed even when its paired engine leg mismatched —
    reported as ``mismatch_refused`` (as if the cluster had been left
    fully untouched) while actually half-reaped, silently breaking both
    the engine-then-postmaster ordering invariant and the "leave the
    cluster for manual review on any mismatch" contract. Checking ALL
    legs before touching ANY of them is what makes "mismatch blocks the
    whole cluster" true rather than aspirational.

    Returns True when a PID-reuse mismatch blocked the reap (the cluster
    dir is then left in place for manual review rather than removed —
    deleting pgdata out from under a possibly-still-relevant investigation
    is worse than leaving one more stale directory around).
    """
    legs: list[tuple[str, int, str]] = []
    for label, pid_key, cmd_key in _LEG_SPECS:
        pid = info.get(pid_key)
        if not isinstance(pid, int) or pid <= 0:
            continue
        legs.append((label, pid, info.get(cmd_key, "") or ""))

    statuses = {label: _identify_leg(pid, cmdline) for label, pid, cmdline in legs}

    if "mismatch" in statuses.values():
        for label, pid, _cmdline in legs:
            if statuses[label] == "mismatch":
                warnings.warn(
                    f"nexus test substrate sweep: refusing to reap "
                    f"{cluster_dir} — {label} pid {pid}'s cmdline no "
                    "longer matches the sidecar (PID-reuse guard); "
                    "leaving cluster in place for manual review "
                    "(NOTHING in this cluster was signalled)",
                    stacklevel=2,
                )
        return True

    for label, pid, _cmdline in legs:
        if statuses[label] != "ok":
            continue  # "dead" -- nothing to signal
        if label == "engine":
            _kill_engine_leg(pid)
        else:
            _kill_postmaster_leg(pid, cluster_dir)
    shutil.rmtree(cluster_dir, ignore_errors=True)
    return False


def _identify_leg(pid: int, expected_cmdline: str) -> str:
    """Side-effect-free: NEVER signals *pid*. Returns ``"dead"`` (nothing
    to do), ``"mismatch"`` (a different process now owns this PID number —
    the PID-reuse guard), or ``"ok"`` (confirmed still the recorded
    process, safe to kill).

    Split out from the kill step deliberately (round-2 review): a caller
    that needs to check several legs before acting on any of them must be
    able to do so without any of the checks themselves having side
    effects.
    """
    from nexus.daemon.service_registry import pid_alive, process_command

    if not pid_alive(pid):
        return "dead"
    actual = process_command(pid)
    if not actual:
        return "dead"  # exited between the liveness check and this read
    if not expected_cmdline or actual.strip() != expected_cmdline.strip():
        return "mismatch"
    return "ok"


def _kill_engine_leg(pid: int, *, grace_s: float = 5.0) -> None:
    """SIGTERM then SIGKILL the engine's PROCESS GROUP, not just *pid*.

    Matches ``_boot()``'s ``preexec_fn=os.setsid`` and ``_teardown()``'s
    ``os.killpg`` for this exact process shape (round-2 review, Important:
    a single-PID signal would miss any child the JVM ever spawned, unlike
    every OTHER teardown path for this same process). Falls back to a
    bare single-PID signal only if ``getpgid`` fails (the process exited
    between the identity check and here — a race, not a design choice).
    """
    from nexus.daemon.service_registry import pid_alive

    def _signal_group(sig: int) -> bool:
        try:
            pgid = os.getpgid(pid)
        except ProcessLookupError:
            return False
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            return False
        return True

    if not _signal_group(signal.SIGTERM):
        return
    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return
        time.sleep(0.1)
    if not pid_alive(pid):
        return
    if not _signal_group(signal.SIGKILL):
        return
    time.sleep(0.2)
    if pid_alive(pid):
        warnings.warn(
            f"nexus test substrate sweep: engine pid {pid} survived "
            "process-group SIGKILL",
            stacklevel=2,
        )


def _kill_postmaster_leg(pid: int, cluster_dir: Path, *, grace_s: float = 5.0) -> None:
    """Stop via ``pg_ctl -D <cluster_dir> stop -m immediate`` — this
    file's OWN established convention (``_kill_pg``, ``_teardown``, and
    even this diff's own ``_sweep_legacy_cluster`` manual-reap message all
    use it), and Hal's own fast-shutdown precedent from this bead's
    original manual reap (bd comment 2026-07-31 23:30: SIGINT/fast-
    shutdown for postmasters — never a raw SIGKILL straight to the
    postmaster PID, which bypasses PG's own crash-shutdown propagation to
    its backend children; round-2 review, Important).

    Falls back to a raw SIGKILL on the already identity-verified *pid*
    ONLY if ``pg_ctl``'s own immediate-mode stop does not clear it within
    *grace_s* — a last resort (e.g. the pgdata is not a live PG cluster
    ``pg_ctl`` can parse at all), not the normal path.
    """
    from nexus.daemon.service_registry import pid_alive

    subprocess.run(
        [str(_pg_bin() / "pg_ctl"), "-D", str(cluster_dir), "stop", "-m", "immediate"],
        capture_output=True,
    )
    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return
        time.sleep(0.1)
    if not pid_alive(pid):
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    time.sleep(0.2)
    if pid_alive(pid):
        warnings.warn(
            f"nexus test substrate sweep: postmaster pid {pid} survived "
            "pg_ctl stop -m immediate AND a fallback SIGKILL",
            stacklevel=2,
        )


def _sweep_legacy_cluster(cluster_dir: Path, result: SweepResult) -> None:
    """A sidecar-less cluster predates this fix — NAME it loudly, never
    auto-reap it. Decision (nexus-lgdy1 fix b item 3), and why:

    A postmaster's ``-D <cluster_dir>`` cmdline argument IS an airtight
    identity match (the exact tempdir path is unique by construction) —
    but identity is not the same question as STALENESS, and staleness is
    what a kill decision actually needs. ``pg_ctl start -w`` always
    daemonizes: pg_ctl forks postgres, waits for readiness, and exits —
    which reparents the postgres process to PID 1 within moments of boot,
    for a LIVE session exactly as much as for an abandoned one. PPID==1 is
    therefore not a stale/live discriminant for this daemonizing
    supervisor's process model, unlike for a genuinely detached daemon —
    it is the sidecar's recorded OWNER PID that answers "is anyone still
    using this cluster", and a pre-sidecar directory has no such record.
    Auto-killing on path-match + PPID==1 alone risks tearing down a PG
    serving a concurrently running OLD-code pytest session (e.g. a
    still-running job from a commit predating this fix).

    NOT a one-time, shrinking population (correction, nexus-ui654
    follow-up round 1: the original "shrinking population" claim here was
    empirically FALSE — a critic run observed this exact warning firing
    repeatedly long after nexus-lgdy1 shipped; round 1's own replacement
    text was ALSO corrected in round 2 after the critic showed the
    "millisecond window" it described was actually bounded by the boot
    semaphore's own up-to-300s wait under real contention, not
    milliseconds). Round 2's fix: ``tempfile.mkdtemp()`` moved INSIDE the
    boot semaphore, and the first sidecar write happens immediately after
    ``initdb`` succeeds (a write attempt BEFORE ``initdb`` was tried and
    rejected -- ``initdb`` refuses a non-empty target directory) -- see
    ``_boot()``'s three-checkpoint sidecar writes. One residual source
    remains after round 2's fix: any cluster created by an OLDER pytest
    process (a build predating this fix, or predating nexus-lgdy1
    entirely) that is still on disk — the live-code window left in
    ``_boot()`` itself that can produce a fresh sidecar-less cluster is
    now bounded by ``initdb``'s own runtime (critic-measured ~0.62s
    uncontended), never by the semaphore's queue-wait, since directory
    creation only happens once a boot already holds its slot. This
    function's own conservative refusal is exactly why old-build clusters
    keep accumulating as reported-not-reaped debris rather than being
    silently dropped — reap them manually via
    ``scripts/sweep-test-substrates.sh`` or the ``pg_ctl``/``rm -rf`` line
    in the warning below once you've confirmed they're genuinely orphaned.
    """
    postmaster_pid = _read_postmaster_pid(str(cluster_dir))
    detail = (
        f"postmaster pid {postmaster_pid}" if postmaster_pid is not None
        else "no readable postmaster.pid"
    )
    result.legacy_reported.append(str(cluster_dir))
    warnings.warn(
        f"nexus test substrate sweep: sidecar-less legacy cluster "
        f"{cluster_dir} ({detail}) predates the crash-durable sidecar "
        "(nexus-lgdy1) and was NOT auto-reaped — see _sweep_legacy_cluster "
        "docstring for why path+PPID identity cannot prove staleness here. "
        "Reap manually once confirmed orphaned: "
        f"pg_ctl -D {cluster_dir} stop -m immediate && rm -rf {cluster_dir} "
        "— and separately check for its paired engine JAR "
        "(`ps -eo pid,ppid,lstart,command | grep "
        "nexus-service-1.0-SNAPSHOT.jar`, kill any PPID=1 match; it cannot "
        "be auto-identified — its cmdline carries no back-reference to a "
        "specific pgdata dir).",
        stacklevel=2,
    )


def ensure_engine() -> dict:
    """Return the live substrate state, booting once per process.

    Raises RuntimeError (fail-loud, with remedy) when the JAR or PG
    binaries are missing — a prior boot failure is remembered and
    re-raised immediately so one broken prerequisite doesn't retry the
    boot for thousands of tests.
    """
    global _state, _boot_error
    with _lock:
        if _boot_error is not None:
            raise RuntimeError(_boot_error)
        if _state is None:
            try:
                _state = _boot()
            except Exception as exc:
                _boot_error = str(exc)
                raise
        return _state


_mint_counter = 0


def mint_test_tenant(state: dict) -> tuple[str, str]:
    """Mint a fresh tenant + its first bound token via /v1/tenants/create.

    The boot bearer (the engine's NX_SERVICE_TOKEN root) authorizes the
    admin surface; the returned token is strictly bound to the new
    tenant, which IS the per-test isolation boundary.
    """
    global _mint_counter
    import httpx

    with _lock:
        _mint_counter += 1
        name = f"t{os.getpid()}-{_mint_counter}"
    # 60s + one retry: the dry-run sweep observed intermittent >10s
    # /v1/tenants/create latency after a few hundred mints in one engine
    # (recorded on nexus-g37fr as an engine observation — the bandaid
    # keeps the suite honest about WHAT failed, not silently flaky).
    resp = None
    last_exc: Exception | None = None
    for _attempt in range(2):
        try:
            resp = httpx.post(
                f"{state['base_url']}/v1/tenants/create",
                json={"name": name},
                headers={"Authorization": f"Bearer {state['bearer']}",
                         "Content-Type": "application/json"},
                timeout=60.0,
            )
            break
        except httpx.TimeoutException as exc:
            last_exc = exc
    if resp is None:
        raise RuntimeError(
            f"T2 engine substrate: tenant mint timed out twice: {last_exc}"
        )
    if resp.status_code != 200:
        raise RuntimeError(
            f"T2 engine substrate: tenant mint failed "
            f"({resp.status_code}): {resp.text[:300]}"
        )
    body = resp.json()
    token = body.get("token")
    if not token:
        raise RuntimeError(f"tenant mint returned no token: {body}")
    return name, token
