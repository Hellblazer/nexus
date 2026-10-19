# SPDX-License-Identifier: AGPL-3.0-or-later
"""Session-scoped engine-backed T2 test substrate (RDR-155 P4b P0a').

Decision D-A (nexus-g37fr, 2026-07-23): the unit suite's T2 substrate is
the REAL engine over the bundled PG — integration-over-mocks and
PG-in-every-mode, applied to the test suite itself. ONE hermetic
PG + one shaded-JAR service boot per pytest session; per-test isolation
comes from a freshly MINTED tenant + tenant-bound token per test: the
engine binds tenant to the BEARER server-side (AuthFilter Decision 1 —
the ``X-Nexus-Tenant`` header is IGNORED), so handing each test its own
token isolates every row via RLS with no sharing. The tenant's partition
leaves are dropped when the test ends (``minted_test_tenant``, RDR-225).

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
import contextlib
import hashlib
import json
import os
import platform
import random
import re
import shutil
import signal
import socket
import stat
import subprocess
import tempfile
import threading
import time
import warnings
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import structlog

from nexus._locking import lock_file, unlock_file
from nexus._winsec import grant_user_tree_access
from nexus.util.process_group import KILL_SIGNAL, safe_killpg
from tests._child_process import kill_group, popen_in_group, stop_group
from tests._ddl_counter import PROBE_SQL, READY_LABEL, START_LABEL, log_conf, mark_sql
from tests._ddl_counter import enabled as ddl_count_enabled
from tests._pg_ctl import pg_ctl_start
from tests.db._service_fixture import (
    SERVICE_ROLES_SQL,
    jar_freshness_skip_reason,
    jvm_error_file_arg,
    pg_bin_dir,
    with_substrate_sizing,
    build_in_progress_reason,
    build_lease_wait_seconds,
    wait_for_build_lease,
)

# The real ``subprocess.run``, bound at import. Fixture teardown runs while a test's own
# ``monkeypatch.setattr(subprocess, "run", ...)`` is still live (function-scoped monkeypatch
# unwinds after the fixtures that requested it), and the tenant drop must reach real psql.
_REAL_SUBPROCESS_RUN = subprocess.run

_log = structlog.get_logger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_JAR = _REPO_ROOT / "service" / "target" / "nexus-service-1.0-SNAPSHOT.jar"

_BEARER = "t2-substrate-session-bearer"
_DBNAME = "nexus_t2_substrate"


def engine_argv(java: str) -> list[str]:
    """The substrate engine's argv. One owner, so the hs_err redirect and the
    heap/CPU budget (nexus-0yym1) cannot be dropped from the launch without a test
    noticing."""
    return with_substrate_sizing(
        [java, "-Duser.timezone=UTC", jvm_error_file_arg(), "-jar", str(_JAR)]
    )


_lock = threading.Lock()
_state: dict | None = None
_boot_error: str | None = None

#: Glob for a substrate pgdata cluster dir, matched under every root in
#: :func:`substrate_pg_roots` (the process tempdir and the tmpfs root).
_STRAY_GLOB = "nexus_t2_substrate_pg_*"

#: Where substrate clusters live (nexus-me3gm). The per-worker Postgres clusters
#: wrote about 2.1 TB/day to chas's only NVMe: catalog churn from per-test tenants
#: drives WAL and heap writes, and nothing in a throwaway cluster needs to survive
#: a reboot. On Linux the clusters therefore go on tmpfs (``/dev/shm``) when it is
#: tmpfs, writable and has room; everywhere else they stay under the process
#: tempdir. ``NX_TEST_PG_ROOT=<dir>`` picks a root outright and
#: ``NX_TEST_PG_ROOT=default`` forces the old tempdir root for new clusters (the
#: sweep still looks in an existing tmpfs root).
_PG_ROOT_ENV = "NX_TEST_PG_ROOT"

#: Test seam for the SWEEP only: a directory standing in for ``/dev/shm`` so the
#: sweep script's tests do not read the box's real tmpfs root (where a peer's live
#: clusters would show up in their counts). Creation never reads it.
_SHM_DIR_SEAM_ENV = "NX_TEST_PG_SHM_DIR"
_SHM_DIR = Path("/dev/shm")
_TMPFS_DIR_PREFIX = "nexus-test-pg"

#: The tempdir this run started with. ``tests/_tmp_root.py`` (nexus-kyujr) moves
#: ``TMPDIR`` onto a private tmpfs directory inside a lab-run grant and records the
#: original here. Two things must keep the ORIGINAL: the boot-slot directory (a
#: per-run directory would split the cross-run concurrency cap) and the stray-cluster
#: sweep's disk root (clusters stranded by an earlier run live there, not in the new
#: run's private directory).
_TMP_ORIG_ENV = "NX_TEST_TMP_ORIG"


def _original_tempdir() -> Path:
    """The process tempdir as it was before ``tests/_tmp_root.py`` moved it."""
    return Path(os.environ.get(_TMP_ORIG_ENV) or tempfile.gettempdir())

#: Headroom required before clusters are put on tmpfs: 2 GiB plus a 1 GiB margin,
#: checked against the SMALLER of the tmpfs's free space and the process's cgroup
#: memory headroom (tmpfs pages are charged to the cgroup, and on a 64 GB box
#: /dev/shm "free" never runs out first). A cluster's footprint is its data
#: directory PLUS its WAL. Measured on chas (nexus-me3gm, two small T2 test
#: files, serial): 42 MB at boot, 66 MB at 10 s, 108 MB at 24 s with pg_wal at
#: its 64 MB cap; the data part keeps growing with the test catalog, so size a
#: suite as ~100 MB plus growth per worker. The threshold is a floor for
#: starting, not a budget for 32 workers.
_TMPFS_MIN_FREE_BYTES = 3 * 1024**3

#: A sidecar-less, postmaster-less cluster dir in the tmpfs root older than this
#: was killed during initdb and is reaped (disk roots keep reporting only).
_SWEEP_BARE_TMPFS_MIN_AGE_S = 600.0

#: ``sun_path`` is 108 bytes on Linux including the NUL, so a socket path may use
#: 107. A cluster dir is ``<root>/<prefix><8 random>`` and the socket
#: ``/.s.PGSQL.<port>`` inside it, so the root may use ``107 - reserve`` bytes. The
#: reserve is sized for the LONGEST prefix any cluster maker uses (the integration
#: fixtures reach 26 chars, e.g. ``nexus_recovery_inttest_pg_``; the substrate's own
#: is 22); :func:`integration_cluster_dir` also checks the actual path, so a new
#: longer prefix fails loud there instead of overrunning ``sun_path``.
_SOCKET_PATH_MAX = 107
_LONGEST_CLUSTER_PREFIX = len("nexus_recovery_inttest_pg_")
_SOCKET_SUFFIX_RESERVE = 1 + _LONGEST_CLUSTER_PREFIX + 8 + len("/.s.PGSQL.65535")

#: Appended to the substrate's ``postgresql.conf`` (nexus-me3gm). Caps pg_wal at
#: about 64 MB instead of the 1 GB default. ``wal_level = minimal`` requires
#: ``max_wal_senders = 0`` and ``archive_mode = off`` (the default). Nothing in
#: ``tests/`` or ``service/src/test`` uses replication, logical decoding or
#: ``pg_basebackup`` against this cluster (grepped 2026-10-19).
WAL_CAP_CONF = (
    "wal_level = minimal\n"
    "max_wal_senders = 0\n"
    "max_wal_size = 64MB\n"
    "min_wal_size = 32MB\n"
    "wal_keep_size = 0\n"
)

#: fsync-free durability for a throwaway cluster: nothing in a test cluster has to
#: survive an OS crash (a killed postmaster recovers from the page cache). Shared
#: by the substrate boot, :func:`throwaway_pg_cluster` and the integration
#: clusters (:func:`integration_cluster_conf`, nexus-942h9).
THROWAWAY_FSYNC_CONF = (
    "fsync = off\n"
    "synchronous_commit = off\n"
    "full_page_writes = off\n"
)


def _current_uid() -> str:
    """The service identity (``str(os.getuid())`` on POSIX); every raw ``getuid``
    in a test-support module is a lint failure (tests/test_service_identity_lint.py)."""
    from nexus.daemon.service_registry import service_identity  # noqa: PLC0415 — deferred: keep this module's import light

    return service_identity()


def _abs(path: str) -> Path:
    """``~`` expanded and made absolute (no symlink resolution), so a relative
    ``NX_TEST_PG_ROOT`` is length-checked and swept as the path it really is."""
    return Path(os.path.abspath(Path(path).expanduser()))


def _tmpfs_candidate(uid: int | str, shm_dir: Path = _SHM_DIR) -> Path:
    return shm_dir / f"{_TMPFS_DIR_PREFIX}-{uid}"


def _read_mounts() -> str | None:
    """``/proc/mounts``, or None where there is none (macOS, Windows)."""
    try:
        return Path("/proc/mounts").read_text()
    except OSError:
        return None


def _mount_fstype(mounts_text: str, mountpoint: str) -> str | None:
    """The filesystem type ``/proc/mounts``-style text gives *mountpoint*, last
    entry winning (a later mount shadows an earlier one)."""
    found: str | None = None
    for line in mounts_text.splitlines():
        fields = line.split()
        if len(fields) >= 3 and fields[1] == mountpoint:
            found = fields[2]
    return found


def _socket_path_fits(root: Path) -> bool:
    return len(str(root)) + _SOCKET_SUFFIX_RESERVE <= _SOCKET_PATH_MAX


def _cgroup_dirs(
    *, proc_cgroup: str | None, cgroup_root: Path,
) -> list[Path]:
    """The cgroup-v2 directories on this process's path, deepest first and the
    root last; ``[]`` where there is no v2 path (cgroup v1, macOS)."""
    try:
        text = Path("/proc/self/cgroup").read_text() if proc_cgroup is None else proc_cgroup
    except OSError:
        return []
    rel = next((ln[3:] for ln in text.splitlines() if ln.startswith("0::")), None)
    if rel is None:
        return []
    parts = [p for p in rel.strip().split("/") if p]
    return [cgroup_root.joinpath(*parts[:d]) for d in range(len(parts), -1, -1)]


def _reclaimable_file_bytes(d: Path) -> int:
    """Page cache the kernel can drop under pressure: ``active_file`` plus
    ``inactive_file`` of ``memory.stat`` (tmpfs pages are NOT in these; they are
    shmem). 0 when the file is unreadable."""
    try:
        text = (d / "memory.stat").read_text()
    except OSError:
        return 0
    total = 0
    for line in text.splitlines():
        key, _, val = line.partition(" ")
        if key in ("active_file", "inactive_file"):
            try:
                total += int(val)
            except ValueError:
                pass
    return total


def _cgroup_headroom(
    *,
    proc_cgroup: str | None = None,
    cgroup_root: Path = Path("/sys/fs/cgroup"),
    credit_reclaimable: bool = False,
) -> int | None:
    """Bytes of memory headroom under the tightest cgroup-v2 limit on this
    process's cgroup path (its own and every ancestor's), or None when there is no
    limit or the files are unreadable (cgroup v1, macOS).

    ``credit_reclaimable`` adds the level's droppable page cache to its room. A
    long run fills ``memory.current`` with file cache up to the limit and the
    kernel reclaims it on demand, so the raw figure reads near zero on a healthy
    run; the mid-run exhaustion check and the start decision (nexus-vo9qn) both
    use the credited one, and the decision logs both."""
    best: int | None = None
    for d in _cgroup_dirs(proc_cgroup=proc_cgroup, cgroup_root=cgroup_root):
        try:
            mx = (d / "memory.max").read_text().strip()
            cur = int((d / "memory.current").read_text().strip())
        except (OSError, ValueError):
            continue
        if mx == "max":
            continue
        try:
            room = int(mx) - cur
        except ValueError:
            continue
        if credit_reclaimable:
            room += _reclaimable_file_bytes(d)
        best = room if best is None else min(best, room)
    return best


def cgroup_memory_limit(
    *, proc_cgroup: str | None = None, cgroup_root: Path = Path("/sys/fs/cgroup"),
) -> int | None:
    """The tightest ``memory.max`` on this process's cgroup path, in bytes, or
    None when nothing on the path is limited (or there is no cgroup v2)."""
    best: int | None = None
    for d in _cgroup_dirs(proc_cgroup=proc_cgroup, cgroup_root=cgroup_root):
        try:
            mx = (d / "memory.max").read_text().strip()
            limit = int(mx)
        except (OSError, ValueError):
            continue
        best = limit if best is None else min(best, limit)
    return best


def _tmpfs_refused(reason: str) -> None:
    """A Linux box that could not use tmpfs says so, once per boot: the NVMe wear
    this bead exists to stop comes back silently otherwise."""
    _log.warning("nexus_t2_substrate.tmpfs_root_refused", reason=reason, bead="nexus-me3gm")
    warnings.warn(
        f"nexus test substrate: not using tmpfs for clusters: {reason}; "
        f"falling back to the tempdir (set {_PG_ROOT_ENV} to choose a root)",
        stacklevel=3,
    )


def decide_pg_root(
    *,
    env: Mapping[str, str] | None = None,
    system: str | None = None,
    mounts_text: str | None = None,
    free_bytes: Callable[[Path], int] | None = None,
    writable: Callable[[Path], bool] | None = None,
    cgroup_headroom: Callable[[], int | None] | None = None,
    uid: int | str | None = None,
    shm_dir: Path = _SHM_DIR,
) -> tuple[Path | None, str | None]:
    """``(root, refusal)``: the directory substrate clusters are created under, or
    ``None`` for the process tempdir, and, when a Linux box with no override could
    not use tmpfs, the reason (else ``None``). Pure decision: it creates and warns
    about nothing. Every input is injectable so the outcomes (override, forced
    default, tmpfs, no tmpfs, too small) are unit tests, not machine state.
    """
    env = os.environ if env is None else env
    override = (env.get(_PG_ROOT_ENV) or "").strip()
    if override == "default":
        return None, None
    if override:
        root = _abs(override)
        if not _socket_path_fits(root):
            raise ValueError(
                f"{_PG_ROOT_ENV}={root} is too long: a substrate cluster's unix "
                f"socket path under it would pass the {_SOCKET_PATH_MAX}-byte "
                f"sun_path limit (root may use at most "
                f"{_SOCKET_PATH_MAX - _SOCKET_SUFFIX_RESERVE} bytes)"
            )
        return root, None
    if (platform.system() if system is None else system) != "Linux":
        return None, None
    if mounts_text is None:
        mounts_text = _read_mounts()
        if mounts_text is None:
            return None, "cannot read /proc/mounts"
    fstype = _mount_fstype(mounts_text, str(shm_dir))
    if fstype != "tmpfs":
        return None, f"{shm_dir} is not tmpfs ({fstype or 'no mount entry'})"
    can_write = writable or (lambda p: os.access(p, os.W_OK | os.X_OK))
    if not can_write(shm_dir):
        return None, f"{shm_dir} is not writable by this user"
    try:
        free = (free_bytes or (lambda p: shutil.disk_usage(p).free))(shm_dir)
    except OSError as exc:
        return None, f"cannot read free space of {shm_dir} ({exc})"
    if free < _TMPFS_MIN_FREE_BYTES:
        return None, (
            f"{shm_dir} has {free // 1024**2} MiB free, below the "
            f"{_TMPFS_MIN_FREE_BYTES // 1024**2} MiB floor"
        )
    # The CREDITED figure decides, the same one the mid-run floor uses: a grant
    # warmed by a build or ``uv sync`` has its memory.current full of reclaimable
    # page cache, and the raw figure would send the whole run to disk (nexus-vo9qn).
    raw_headroom: int | None = None
    if cgroup_headroom is not None:
        headroom = cgroup_headroom()
    else:
        raw_headroom = _cgroup_headroom()
        headroom = _cgroup_headroom(credit_reclaimable=True)
    _log.info(
        "nexus_t2_substrate.root_decision_headroom",
        raw_headroom_mib=None if raw_headroom is None else raw_headroom // 1024**2,
        credited_headroom_mib=None if headroom is None else headroom // 1024**2,
        floor_mib=_TMPFS_MIN_FREE_BYTES // 1024**2,
        bead="nexus-vo9qn",
    )
    if headroom is not None and headroom < _TMPFS_MIN_FREE_BYTES:
        raw_note = (
            "" if raw_headroom is None else f", {raw_headroom // 1024**2} MiB before "
            f"crediting reclaimable page cache"
        )
        return None, (
            f"the cgroup memory headroom is {headroom // 1024**2} MiB{raw_note}, "
            f"below the {_TMPFS_MIN_FREE_BYTES // 1024**2} MiB floor (tmpfs pages "
            f"are charged to it)"
        )
    root = _tmpfs_candidate(_current_uid() if uid is None else uid, shm_dir)
    if not _socket_path_fits(root):
        return None, f"a unix socket path under {root} would pass the sun_path limit"
    return root, None


def choose_pg_root(**kw) -> Path | None:
    """:func:`decide_pg_root` for its root alone; a Linux fallback with no override
    is warned about by name (:func:`_tmpfs_refused`). Takes the same injectable
    inputs. Production code goes through :func:`session_pg_root`, which makes this
    decision once per run."""
    root, refusal = decide_pg_root(**kw)
    if refusal is not None:
        _tmpfs_refused(refusal)
    return root


# nexus-vo9qn: the tmpfs-vs-disk decision is made ONCE per run. choose_pg_root used
# to run per worker at every cluster start and refuse tmpfs when the cgroup's
# headroom was under 3 GiB; tmpfs pages are charged to the grant, so as workers
# filled memory the later clusters went to ext4 (lab-l, -n auto: 3.9 GB written in
# 4 min). The first decision is persisted in the process environment, which xdist
# workers inherit, so every cluster in the run uses it.
_PG_ROOT_SESSION_ENV = "NX_TEST_PG_ROOT_SESSION"
#: Run-scoped file (path in the env) each cluster start appends one line to; the
#: controller's terminal summary counts it. Only the pytest controller creates it.
_PG_ROOT_LOG_ENV = "NX_TEST_PG_ROOT_LOG"

#: What a mid-run cluster start needs from a session that chose tmpfs. One cluster
#: measured 108 MB at 24 s (nexus-me3gm); 512 MiB is that with margin. Below it the
#: start fails loud instead of falling back to disk: that fallback is the NVMe wear
#: this bead exists to stop.
_TMPFS_HARD_FLOOR_BYTES = 512 * 1024**2


class SubstrateRootExhausted(RuntimeError):
    """The run chose tmpfs for its clusters and a later cluster cannot fit. Never
    answered by a silent fall back to disk (nexus-vo9qn)."""


def _encode_session_root(root: Path | None, refusal: str | None) -> str:
    return f"tmpfs={root}" if root is not None else f"disk={refusal or ''}"


def _decode_session_root(raw: str | None) -> tuple[bool, Path | None, str | None]:
    """``(valid, root, refusal)`` for a :data:`_PG_ROOT_SESSION_ENV` value; an
    absent or unrecognised value is not valid and is decided afresh."""
    if raw and raw.startswith("tmpfs=") and len(raw) > len("tmpfs="):
        return True, Path(raw[len("tmpfs="):]), None
    if raw is not None and raw.startswith("disk="):
        return True, None, raw[len("disk="):] or None
    return False, None, None


def session_pg_root(
    *, env: dict[str, str] | None = None, **decide_kw,
) -> tuple[Path | None, str | None, bool]:
    """``(root, refusal, explicit)`` for this run's clusters. An explicit
    ``NX_TEST_PG_ROOT`` is a standing instruction and is read every time; the
    automatic choice is made on the first call and persisted in *env* (default
    ``os.environ``) for every later cluster, in this process and in the xdist
    workers it spawns. A Linux refusal is warned about once, by the process that
    decided."""
    env = os.environ if env is None else env
    override = (env.get(_PG_ROOT_ENV) or "").strip()
    if override:
        root, refusal = decide_pg_root(env=env, **decide_kw)
        return root, refusal, override != "default"
    valid, root, refusal = _decode_session_root(env.get(_PG_ROOT_SESSION_ENV))
    if valid:
        return root, refusal, False
    root, refusal = decide_pg_root(env=env, **decide_kw)
    if refusal is not None:
        _tmpfs_refused(refusal)
    env[_PG_ROOT_SESSION_ENV] = _encode_session_root(root, refusal)
    return root, refusal, False


_OWNED_LOGS: set[str] = set()
#: This process's own cluster starts: the summary source of a serial run, which
#: needs no cross-process log.
_LOCAL_ROWS: list[dict] = []


def begin_pg_root_session() -> None:
    """Called by the pytest xdist controller before it spawns workers: take the
    session decision now, so no worker decides on its own, and open the run's
    cluster log for the workers to append to. A nested pytest inherits both and
    leaves them alone."""
    try:
        session_pg_root()
    except ValueError:
        raise  # an over-long NX_TEST_PG_ROOT is a loud, pre-run failure
    except Exception as exc:  # noqa: BLE001 — workers must not decide on their own
        persist_controller_failure(exc)
    if not os.environ.get(_PG_ROOT_LOG_ENV):
        try:
            fd, path = tempfile.mkstemp(prefix="nexus-pg-roots-", suffix=".log")
        except OSError as exc:  # the summary is lost; the decision above stands
            _log.warning("nexus_t2_substrate.cluster_log_unavailable", error=str(exc))
            return
        os.close(fd)
        os.environ[_PG_ROOT_LOG_ENV] = path
        _OWNED_LOGS.add(path)


def persist_controller_failure(exc: BaseException) -> None:
    """The controller could not take the run's root decision. Persist a DISK
    decision naming why, so every xdist worker inherits one run-wide answer rather
    than deciding on its own as memory fills (the per-worker flip this bead
    removes), and say so loudly. Never raises, and leaves a valid decision alone."""
    with contextlib.suppress(Exception):
        valid, _root, _why = _decode_session_root(os.environ.get(_PG_ROOT_SESSION_ENV))
        if valid:
            return
        reason = f"the controller could not decide ({type(exc).__name__}: {exc})"
        os.environ[_PG_ROOT_SESSION_ENV] = _encode_session_root(None, reason)
        _tmpfs_refused(reason)


def _record_cluster_root(kind: str, root: str | None, reason: str | None) -> None:
    row = {"kind": kind, "root": root, "reason": reason, "pid": os.getpid()}
    _LOCAL_ROWS.append(row)
    path = os.environ.get(_PG_ROOT_LOG_ENV)
    if path:
        with contextlib.suppress(OSError):
            with open(path, "a") as f:  # one short O_APPEND write: atomic across workers
                f.write(json.dumps(row) + "\n")


def pg_root_run_summary(*, finish: bool = False) -> str | None:
    """The one-line "where did the clusters go" answer for this run, or None when
    no cluster started. An xdist run reads every worker's rows from the cluster
    log; a serial run reads its own. With ``finish`` the controller also removes
    the log it created."""
    path = os.environ.get(_PG_ROOT_LOG_ENV)
    rows = list(_LOCAL_ROWS)
    if path:
        try:
            rows = [json.loads(ln) for ln in Path(path).read_text().splitlines() if ln.strip()]
        except (OSError, ValueError):
            pass
        if finish and path in _OWNED_LOGS:
            with contextlib.suppress(OSError):
                os.unlink(path)
            _OWNED_LOGS.discard(path)
            os.environ.pop(_PG_ROOT_LOG_ENV, None)
    if not rows:
        return None
    tmpfs = [r for r in rows if r["kind"] == "tmpfs"]
    disk = [r for r in rows if r["kind"] != "tmpfs"]
    line = f"nexus test substrate: clusters on tmpfs {len(tmpfs)}, on disk {len(disk)}"
    why = sorted({r["reason"] for r in disk if r.get("reason")})
    if why:
        line += " (disk because: " + "; ".join(why) + ")"
    roots = sorted({r["root"] for r in rows if r.get("root")})
    if roots:
        line += " [root " + ", ".join(roots) + "]"
    return line


#: A start below the hard floor waits this long for headroom to come back before
#: it errors: a neighbour's JVM heap spike or page cache not yet reclaimed is a
#: moment, not a lack of capacity (nexus-vo9qn review S3). Polled every
#: :data:`_TMPFS_FLOOR_POLL_S`.
_TMPFS_FLOOR_WAIT_S = 45.0
_TMPFS_FLOOR_POLL_S = 3.0


def _tmpfs_floor_cause(
    root: Path,
    free_bytes: Callable[[Path], int] | None,
    cgroup_headroom: Callable[[], int | None] | None,
) -> str | None:
    """Why one more cluster does not fit right now, or None when it does."""
    try:
        free = (free_bytes or (lambda p: shutil.disk_usage(p).free))(
            root if root.exists() else root.parent
        )
    except OSError:
        free = None
    headroom = (
        cgroup_headroom or (lambda: _cgroup_headroom(credit_reclaimable=True))
    )()
    if free is not None and free < _TMPFS_HARD_FLOOR_BYTES:
        return f"{root} has {free // 1024**2} MiB free"
    if headroom is not None and headroom < _TMPFS_HARD_FLOOR_BYTES:
        return (
            f"the cgroup has {headroom // 1024**2} MiB of memory headroom left "
            f"(tmpfs pages are charged to it)"
        )
    return None


def _check_tmpfs_has_room(
    root: Path,
    *,
    free_bytes: Callable[[Path], int] | None = None,
    cgroup_headroom: Callable[[], int | None] | None = None,
    wait_s: float | None = None,
    poll_s: float | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Fail loud when a run that chose tmpfs cannot fit one more cluster, after
    polling for up to *wait_s* (default :data:`_TMPFS_FLOOR_WAIT_S`) for the
    headroom to recover. Clock and sleep are injectable."""
    wait = _TMPFS_FLOOR_WAIT_S if wait_s is None else wait_s
    poll = _TMPFS_FLOOR_POLL_S if poll_s is None else poll_s
    start = clock()
    while True:
        cause = _tmpfs_floor_cause(root, free_bytes, cgroup_headroom)
        if cause is None:
            return
        if clock() - start >= wait:
            break
        sleep(poll)
    raise SubstrateRootExhausted(
        f"nexus test substrate: no room for another cluster on tmpfs: {cause}, "
        f"below the {_TMPFS_HARD_FLOOR_BYTES // 1024**2} MiB floor a cluster start "
        f"needs, and it did not recover in {wait:g} s. "
        f"This run chose tmpfs at its start and will not fall back to disk mid-run "
        f"(that wrote 3.9 GB to the NVMe in 4 minutes, nexus-vo9qn). Read the cause "
        f"in the summary line ('clusters on tmpfs N, on disk M') and the "
        f"nexus_t2_substrate.cluster_root / root_decision_headroom log events "
        f"(info level: re-run with --log-level=INFO). "
        f"In order: run fewer workers (-n N, with N = headroom / 2.5 GiB) or a "
        f"larger lab-run class; set {_PG_ROOT_ENV}=<dir> to put clusters on a "
        f"disk you choose; {_PG_ROOT_ENV}=default puts every cluster back on the "
        f"tempdir (disk) and brings the NVMe writes this check exists to prevent."
    )


def _on_tmpfs_root(
    parent: str | None, *, mounts_text: str | None, shm_dir: Path = _SHM_DIR,
) -> bool:
    if parent is None:
        return False
    if mounts_text is None:
        mounts_text = _read_mounts()
        if mounts_text is None:
            return False
    return (
        _mount_fstype(mounts_text, str(shm_dir)) == "tmpfs"
        and Path(parent).is_relative_to(shm_dir)
    )


def wal_cap_conf(
    parent: str | None,
    *,
    env: Mapping[str, str] | None = None,
    mounts_text: str | None = None,
    shm_dir: Path = _SHM_DIR,
) -> str:
    """:data:`WAL_CAP_CONF` where it helps: a cluster on the tmpfs root, or on an
    explicit ``NX_TEST_PG_ROOT``. On the tempdir fallback (disk) the 64 MB cap
    only multiplies checkpoints, and macOS/Windows stay unchanged (nexus-me3gm)."""
    env = os.environ if env is None else env
    explicit = (env.get(_PG_ROOT_ENV) or "").strip() not in ("", "default")
    if parent is not None and (
        explicit or _on_tmpfs_root(parent, mounts_text=mounts_text, shm_dir=shm_dir)
    ):
        return WAL_CAP_CONF
    return ""


def dsm_conf(
    parent: str | None,
    *,
    mounts_text: str | None = None,
    shm_dir: Path = _SHM_DIR,
) -> str:
    """The ``dynamic_shared_memory_type`` line for a cluster under *parent*.

    Postgres's default (posix) puts dynamic shared memory in ``/dev/shm`` as
    ``PostgreSQL.<n>`` files, and a SIGKILLed postmaster leaves them behind with
    nothing to reap them: chas held 864 of them (444 MB) after two weeks. On a
    cluster that already lives on tmpfs, ``mmap`` keeps DSM in
    ``<pgdata>/pg_dynshmem`` instead, which dies with the cluster dir (the sweep)
    and which Postgres clears at startup. On a disk root, ``mmap`` would be real
    writes, so the default stays (nexus-me3gm).
    """
    if not _on_tmpfs_root(parent, mounts_text=mounts_text, shm_dir=shm_dir):
        return ""
    return "dynamic_shared_memory_type = mmap\n"


def pg_cluster_parent() -> str | None:
    """The session's cluster root (:func:`session_pg_root`) plus creating the
    directory (0700, owned by us). ``None`` means "use the tempdir". A tmpfs root
    that cannot be made private falls back to the tempdir with a warning (a
    permissions fact, the same for every cluster); an explicit ``NX_TEST_PG_ROOT``
    that cannot be made fails loud, and so does a tmpfs session with no room
    left. Every start logs where the cluster went and why, and is counted for
    the run's summary line (nexus-vo9qn)."""
    root, refusal, explicit = session_pg_root()
    if root is None:
        _log_cluster_root(None, refusal)
        return None
    if not explicit:
        _check_tmpfs_has_room(root)
    try:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not explicit:
            # /dev/shm is world-writable: refuse a dir someone else pre-created
            # under our predictable name.
            st = root.lstat()
            if not stat.S_ISDIR(st.st_mode):
                raise PermissionError(f"{root} is not a real directory")
            if st.st_uid != int(_current_uid()):
                raise PermissionError(f"{root} is owned by uid {st.st_uid}")
            if st.st_mode & 0o077:
                root.chmod(0o700)
    except OSError as exc:
        if explicit:
            raise
        warnings.warn(
            f"nexus test substrate: cannot use {root} for clusters ({exc}); "
            f"falling back to the tempdir",
            stacklevel=2,
        )
        _log_cluster_root(None, f"cannot use {root} ({exc})")
        return None
    _log_cluster_root(root, None)
    return str(root)


def _log_cluster_root(root: Path | None, reason: str | None) -> None:
    """One structured line per cluster start and one row in the run's cluster
    log. A disk start with a reason is a warning (visible at the default log
    level); the rest are info."""
    kind = "tmpfs" if root is not None and _on_tmpfs_root(str(root), mounts_text=None) else "disk"
    fields = {"root": str(root) if root else "tempdir", "on": kind, "reason": reason}
    if reason:
        _log.warning("nexus_t2_substrate.cluster_root", bead="nexus-vo9qn", **fields)
    else:
        _log.info("nexus_t2_substrate.cluster_root", bead="nexus-vo9qn", **fields)
    _record_cluster_root(kind, str(root) if root else None, reason)


def substrate_pg_roots(
    *,
    env: Mapping[str, str] | None = None,
    system: str | None = None,
    uid: int | str | None = None,
    tempdir: Path | None = None,
    shm_dir: Path | None = None,
) -> list[Path]:
    """Every root the stray-cluster sweep must look in: the tempdir (where the
    older clusters live), ``NX_TEST_PG_ROOT`` when set, and the tmpfs root
    whenever its directory EXISTS on Linux. The override never hides the tmpfs
    root: ``=default`` and ``=<dir>`` change where NEW clusters go, never what the
    sweep looks at, because a cluster stranded in tmpfs is RAM until reaped.
    """
    env = os.environ if env is None else env
    base = _original_tempdir() if tempdir is None else tempdir
    if shm_dir is None:
        shm_dir = Path(env.get(_SHM_DIR_SEAM_ENV) or _SHM_DIR)
    override = (env.get(_PG_ROOT_ENV) or "").strip()
    roots: list[Path] = []
    if override and override != "default":
        roots.append(_abs(override))
    if (platform.system() if system is None else system) == "Linux":
        cand = _tmpfs_candidate(_current_uid() if uid is None else uid, shm_dir)
        if cand.is_dir():
            roots.append(cand)
    roots.append(base)
    unique: list[Path] = []
    seen: set[Path] = set()
    for r in roots:
        key = r.resolve()
        if key not in seen:
            seen.add(key)
            unique.append(r)
    return unique

def throwaway_cluster_conf(
    parent: str | None,
    *,
    env: Mapping[str, str] | None = None,
    mounts_text: str | None = None,
    shm_dir: Path = _SHM_DIR,
) -> str:
    """The ``postgresql.conf`` tail for a throwaway cluster created under *parent*:
    :data:`THROWAWAY_FSYNC_CONF` always, the WAL cap and mmap DSM where
    :func:`wal_cap_conf` and :func:`dsm_conf` give them (nexus-942h9)."""
    return (
        THROWAWAY_FSYNC_CONF
        + wal_cap_conf(parent, env=env, mounts_text=mounts_text, shm_dir=shm_dir)
        + dsm_conf(parent, mounts_text=mounts_text, shm_dir=shm_dir)
    )


def integration_cluster_conf(pgdata: str, **kw) -> str:
    """:func:`throwaway_cluster_conf` for a cluster made by
    :func:`integration_cluster_dir` (the root is the cluster dir's parent). An
    integration fixture appends it to ``postgresql.conf`` beside its port line.
    No integration caller needs durability semantics (nexus-942h9: they stop the
    cluster with ``pg_ctl stop -m immediate`` at teardown and none reads pg_wal,
    sets ``wal_level`` or replicates); one that ever does passes
    ``parent_dir=<a disk directory of its own>`` to ``pg_data_tempdir`` and skips this
    (not ``tempfile.gettempdir()``: inside a lab-run grant that is the run's tmpfs,
    nexus-kyujr)."""
    return throwaway_cluster_conf(str(Path(pgdata).parent), **kw)


#: The settings :func:`integration_cluster_conf` can give that Postgres reads only at a postmaster start. A cluster
#: that is already running (``pg_provision.provision`` initdbs and starts in one call) cannot take them by a conf
#: append and a reload, so :func:`reloadable_cluster_conf` leaves them out (nexus-0ajjm).
RESTART_ONLY_SETTINGS = frozenset({"wal_level", "max_wal_senders", "dynamic_shared_memory_type"})


def reloadable_cluster_conf(pgdata: str, **kw) -> str:
    """:func:`integration_cluster_conf` minus :data:`RESTART_ONLY_SETTINGS`: ``fsync``, ``synchronous_commit``,
    ``full_page_writes`` and, where the cluster is on the tmpfs root, ``max_wal_size`` / ``min_wal_size`` /
    ``wal_keep_size``. A conf append plus ``pg_ctl reload`` applies all of them to a running cluster."""
    return "".join(
        line + "\n"
        for line in integration_cluster_conf(pgdata, **kw).splitlines()
        if line.split("=", 1)[0].strip() not in RESTART_ONLY_SETTINGS
    )


# ── Integration clusters: owner records (nexus-942h9) ────────────────────────
#
# An integration fixture (``tests._child_process.pg_data_tempdir``) makes its
# cluster dir BEFORE initdb and starts the engine JVM in a different fixture, so
# the substrate's in-dir sidecar does not fit: initdb refuses a non-empty dir, and
# the JVM is not known when the dir is made. Instead the process keeps ONE record
# per chosen root, ``<root>/nexus_integration_owner_<pid>.json``: its identity,
# every cluster dir it made there, and every engine child it spawned
# (:func:`record_integration_child`). The sweep reaps a record whose owner is
# dead, with the same identity checks as the sidecar clusters. Only a cluster in
# a chosen root (tmpfs or ``NX_TEST_PG_ROOT``) is recorded: on the tempdir it is
# inert disk debris, as before.
_OWNER_RECORD_PREFIX = "nexus_integration_owner_"
_OWNER_RECORD_GLOB = f"{_OWNER_RECORD_PREFIX}*.json"
_owner_lock = threading.Lock()
#: root -> this process's record contents (the file is a mirror of this).
_owner_records: dict[Path, dict] = {}
_integration_swept = False
_owner_exit_registered = False


def _owner_record_path(root: Path) -> Path:
    return root / f"{_OWNER_RECORD_PREFIX}{os.getpid()}.json"


def _flush_owner_record(root: Path, info: dict) -> None:
    path = _owner_record_path(root)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(info, indent=2))
    tmp.replace(path)  # atomic: the sweep never reads a torn record


def _load_owner_record(root: Path) -> dict:
    """This process's record for *root*: fresh, plus whatever an earlier process
    with the same pid left in the file (its orphans become ours to be reaped)."""
    from nexus.daemon.service_registry import process_command

    info: dict = {
        "owner_pytest_pid": os.getpid(),
        "owner_cmdline": process_command(os.getpid()),
        "pgdatas": [],
        "children": [],
    }
    try:
        old = json.loads(_owner_record_path(root).read_text())
        info["pgdatas"] = [p for p in old.get("pgdatas", []) if isinstance(p, str)]
        info["children"] = [c for c in old.get("children", []) if isinstance(c, dict)]
    except (OSError, ValueError, AttributeError):
        pass
    return info


def register_owned_cluster(root: Path, pgdata: str) -> None:
    """Record *pgdata* (a cluster dir this process just made under *root*) so the
    sweep can reap it if this process is killed. Best effort: a record that cannot
    be written is a warning, never a test failure."""
    global _owner_exit_registered
    try:
        with _owner_lock:
            info = _owner_records.get(root) or _load_owner_record(root)
            info["pgdatas"] = [p for p in info["pgdatas"] if Path(p).exists()] + [pgdata]
            _owner_records[root] = info
            _flush_owner_record(root, info)
            if not _owner_exit_registered:
                _owner_exit_registered = True
                atexit.register(_drop_finished_owner_records)
    except OSError as exc:
        warnings.warn(
            f"nexus integration clusters: cannot record {pgdata} under {root} ({exc}); "
            f"a kill of this process will leave it for a manual sweep",
            stacklevel=3,
        )


def record_integration_child(proc: subprocess.Popen) -> None:
    """Record an engine child (spawned by the integration fixture) so the sweep can
    kill it with its cluster. A no-op unless this process recorded a cluster."""
    from nexus.daemon.service_registry import pid_alive, process_command

    if not _owner_records:
        return
    cmdline = process_command(proc.pid)
    if not cmdline:
        return
    with _owner_lock:
        for root, info in _owner_records.items():
            info["children"] = [
                c for c in info["children"] if pid_alive(c.get("pid", 0))
            ] + [{"pid": proc.pid, "cmdline": cmdline}]
            try:
                _flush_owner_record(root, info)
            except OSError as exc:
                warnings.warn(
                    f"nexus integration clusters: cannot record engine pid {proc.pid} "
                    f"under {root} ({exc})",
                    stacklevel=2,
                )


def _drop_finished_owner_records() -> None:
    """At exit: remove each record with no cluster dir left and no live child; a
    record that still guards something stays for the sweep."""
    from nexus.daemon.service_registry import pid_alive

    with _owner_lock:
        for root, info in list(_owner_records.items()):
            if any(Path(p).exists() for p in info["pgdatas"]):
                continue
            if any(pid_alive(c.get("pid", 0)) for c in info["children"]):
                continue
            try:
                _owner_record_path(root).unlink(missing_ok=True)
            except OSError:
                continue
            del _owner_records[root]


def _sweep_once_per_process() -> None:
    """Reap dead owners' integration clusters before this process makes its first:
    an integration-only run never boots the substrate, whose boot is otherwise the
    only caller of the sweep. Owner records only -- the substrate's own stray
    clusters (and their loud legacy warnings) stay the substrate boot's business."""
    global _integration_swept
    if _integration_swept:
        return
    _integration_swept = True
    try:
        sweep_integration_owner_records()
    except Exception as exc:  # noqa: BLE001 - the sweep must never block a cluster (nexus-lgdy1)
        warnings.warn(
            f"nexus integration clusters: session-start sweep failed unexpectedly: {exc}",
            stacklevel=3,
        )


def integration_cluster_dir(prefix: str) -> str:
    """A fresh cluster dir for an integration fixture: ``mkdtemp`` under
    :func:`pg_cluster_parent` (tmpfs on Linux with the substrate's gates and loud
    fallback, else the tempdir), recorded for the crash sweep when it is in a
    chosen root."""
    _sweep_once_per_process()
    parent = pg_cluster_parent()
    # parent None = the cluster root decided on disk: the ORIGINAL tempdir, never the run's
    # tmpfs TMPDIR (nexus-kyujr), or the disk decision would land in RAM and the sweep
    # (which looks at the original tempdir) would never see a stranded one.
    path = tempfile.mkdtemp(prefix=prefix, dir=parent if parent is not None else _original_tempdir())
    if parent is not None:
        # The reserve is a guess about prefixes; this is the real path. Only a
        # chosen root: the tempdir fallback keeps the callers' older behaviour.
        if len(path) + len("/.s.PGSQL.65535") > _SOCKET_PATH_MAX:
            shutil.rmtree(path, ignore_errors=True)
            raise ValueError(
                f"cluster dir {path} is too long: its unix socket path would pass "
                f"the {_SOCKET_PATH_MAX}-byte sun_path limit; use a shorter "
                f"{_PG_ROOT_ENV} or cluster prefix"
            )
    grant_user_tree_access(path)
    if parent is not None:
        register_owned_cluster(Path(parent), path)
    return path


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
#: dev-loop invocation), every worker is a SEPARATE process that
#: independently calls ``_boot()`` -> ``_free_port()`` — all drawing from the
#: SAME 9000-port range instead of the prior effectively unbounded
#: OS-ephemeral pool. ``_free_port``'s bind-probe-then-close is not atomic
#: across the gap to the eventual ``pg_ctl``/``java`` bind, so two workers CAN
#: race onto the same port. Sharding by worker index removes that collision
#: surface between the workers of ONE session (a worker never even PROBES
#: another worker's slice); a worker index beyond the sharded capacity, or no
#: xdist worker at all, falls back to the FULL range — narrowing the search
#: space, never silently degrading below the existing bind-probe +
#: loud-fallback behaviour.
#:
#: Sessions are NOT separate runners (nexus-kmfz6). CI shard jobs share one
#: runner host, and two agents' suites share a laptop, so several pytest
#: sessions run at once as one user. The worker index alone put gw0 of EVERY
#: session on the same slice, gw1 on the next, and so on. The slice index is
#: therefore ROTATED by a per-session offset (``_session_port_offset``). A
#: rotation PERMUTES the slice table: the workers of one session keep disjoint
#: slices, but two sessions that each use most of the slices (two ``-n auto``
#: runs) still share exactly as many slices as before. It lowers collisions only
#: when the sessions' combined worker count is below the slice count. What
#: closes the hole is not the rotation: ``_boot`` proves each listener is its
#: own (``_wait_engine_ready``) and retries a lost race
#: (``_PORT_RACE_ATTEMPTS``).
_WORKER_SHARD_WIDTH = 500
_WORKER_SHARD_BASE = _LOW_PORT_RANGE.start
_WORKER_SHARD_MAX_INDEX = (
    (_LOW_PORT_RANGE.stop - _LOW_PORT_RANGE.start) // _WORKER_SHARD_WIDTH - 1
)
#: The narrowest slice a worker is ever given. A worker boots one PG and one
#: engine per session, plus the odd test-local listener, so 50 ports is ample;
#: below it the random bind-probe would start to exhaust its own slice.
_WORKER_SHARD_MIN_WIDTH = 50


def _worker_shard_width() -> int:
    """The slice width for this run: :data:`_WORKER_SHARD_WIDTH`, narrowed so
    every worker of the run gets its own slice.

    nexus-wvyvn: a fixed 500-port width fits 18 workers in the 9000-port range,
    and ``-n auto`` on a 15-core box already spawned 19, which dropped the
    extras onto the shared full range the sharding exists to avoid. xdist sets
    ``PYTEST_XDIST_WORKER_COUNT`` in every worker, so the width is derived from
    it; a run of 18 or fewer workers keeps the 500-port width exactly.
    """
    try:
        count = int(os.environ.get("PYTEST_XDIST_WORKER_COUNT", ""))
    except ValueError:
        return _WORKER_SHARD_WIDTH
    if count <= 0:
        return _WORKER_SHARD_WIDTH
    span = _LOW_PORT_RANGE.stop - _LOW_PORT_RANGE.start
    return max(_WORKER_SHARD_MIN_WIDTH, min(_WORKER_SHARD_WIDTH, span // count))

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
    "XDG_CACHE_HOME", "NX_TEST_SUBSTRATE_CACHE", "NX_ONNX_MODEL_DIR", "NX_SERVICE_BGE_DIR",
    "NX_SERVICE_CROSSENCODER_DIR",
)
_PG_AMBIENT_ENV: dict[str, str | None] = {
    k: os.environ.get(k) for k in _PG_AMBIENT_ENV_KEYS
}
_pg_bin_resolved: Path | None = None
_pg_bin_lock = threading.Lock()


@contextmanager
def _ambient_env() -> Iterator[None]:
    """Run the body under the import-time ambient env snapshot. The lock
    serializes the env-swap window against any concurrent in-process caller
    (review round 2: ``sweep_stale_substrate_clusters`` can reach
    :func:`_pg_bin` from a test body).

    The swap has to cover the whole body, downloads included, because the
    product seams read the env at call time; so the lock spans a cold PG
    bundle download (as it always has in :func:`_pg_bin`) and a cold ONNX
    fetch. Both run once per process, inside ``_boot``'s own lock."""
    with _pg_bin_lock:
        saved = {k: os.environ.get(k) for k in _PG_AMBIENT_ENV_KEYS}
        try:
            for k, v in _PG_AMBIENT_ENV.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            yield
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


def _pg_bin() -> Path:
    """The PG bundle ``bin/`` dir, resolved once on FIRST REAL USE under the
    import-time ambient env snapshot (may download on a cold cache — which is
    exactly why it must not run at collection time)."""
    global _pg_bin_resolved
    if _pg_bin_resolved is None:
        with _ambient_env():
            if _pg_bin_resolved is None:
                _pg_bin_resolved = pg_bin_dir()
    return _pg_bin_resolved


def _no_pg_message() -> str:
    """The setup error when no PG bundle is usable, naming the provisioning
    failure when there was one.

    nexus-wvyvn: a fresh host reported ~23k setup errors reading "no
    PostgreSQL binaries discoverable ... Install the PG bundle (nx init)",
    which points at discovery, while the real cause (the self-provisioning
    download failing) had gone to a warning nobody could find among them.
    """
    from tests.db._service_fixture import last_provision_failure, substrate_cache_root  # noqa: PLC0415 — deferred, matches this module's lazy PG resolution

    failure = last_provision_failure()
    if failure:
        return (
            "T2 engine substrate unavailable: self-provisioning the pinned PG "
            f"bundle into {substrate_cache_root()} failed ({failure}). Fix "
            "connectivity and rerun, or set NEXUS_PG_BIN to a PostgreSQL bin "
            "dir that has pgvector."
        )
    return (
        "T2 engine substrate unavailable: no PostgreSQL binaries "
        "discoverable (NEXUS_PG_BIN / config-dir bundle / Homebrew / "
        "PATH). Install the PG bundle (nx init) or set NEXUS_PG_BIN."
    )


def _ensure_onnx_models(spawn_env: dict[str, str]) -> Path | None:
    """Make sure the engine's local-mode ONNX models exist before it boots;
    return the onnx_models root the engine must read, or ``None`` when the
    engine will run on Voyage and loads no local model.

    nexus-wvyvn: on a fresh host with an empty ``~/.cache/nexus/onnx_models``
    the engine died constructing ``Bge768Embedder`` and the substrate reported
    only "service did not bind port", so every substrate-backed test errored
    at setup with a message that names the port rather than the missing
    model (hellmini, 2026-09-28). Provisioning goes through the product's own
    fetchers under one cross-process lock, since they stream straight into
    the destination file and two xdist workers must never write it at once.
    bge-768 is required (the engine cannot boot without it); the
    cross-encoder only degrades rerank, so its failure warns.

    Resolved under the import-time ambient env, like :func:`_pg_bin`, so a
    test's monkeypatched HOME cannot move the check somewhere the engine
    does not read. The caller passes the root to the engine as
    ``NX_ONNX_MODEL_DIR``, so both sides use one directory by construction.
    """
    if spawn_env.get("NX_VOYAGE_API_KEY", "").strip():
        return None
    from nexus.db import service_bge_model as bge  # noqa: PLC0415 — deferred, heavy imports stay off collection
    from nexus.db import service_crossencoder_model as ce  # noqa: PLC0415 — deferred, heavy imports stay off collection
    from nexus.db.onnx_model_root import service_onnx_models_root  # noqa: PLC0415 — deferred, heavy imports stay off collection

    with _ambient_env():
        root = service_onnx_models_root()
        # The engine reads only <root>/<model>/onnx; a Python-only per-model
        # override pointed elsewhere would provision one dir while the engine
        # crashes on another (production warns on the same check at spawn,
        # storage_service_daemon; critique of 5ded8b067).
        for env_name, mismatch in (
            ("NX_SERVICE_BGE_DIR", bge.service_bge_engine_dir_mismatch()),
            ("NX_SERVICE_CROSSENCODER_DIR", ce.service_crossencoder_engine_dir_mismatch()),
        ):
            if mismatch is not None:
                raise RuntimeError(
                    f"T2 engine substrate unavailable: {env_name} points at "
                    f"{mismatch[0]}, but the engine reads only {mismatch[1]}. "
                    f"Unset {env_name}, or set NX_ONNX_MODEL_DIR so the override "
                    "sits at <root>/<model>/onnx."
                )
        present = lambda: bge.service_bge_model_present() and ce.service_crossencoder_model_present()  # noqa: E731 — two call sites, one line
        if present():
            return root
        root.mkdir(parents=True, exist_ok=True)
        with (root.parent / "onnx_models.provision.lock").open("a+") as lock_fh:
            lock_file(lock_fh, blocking=True)
            try:
                # A waiter takes the holder's result rather than fetching again.
                if present():
                    return root
                try:
                    bge.fetch_service_bge_onnx()
                except RuntimeError as exc:
                    raise RuntimeError(
                        f"T2 engine substrate unavailable: the engine's bge-768 ONNX "
                        f"model is not provisioned under {root} and fetching it "
                        f"failed ({exc}). Run `nx init --service` once on this "
                        "host, or point NX_ONNX_MODEL_DIR at a provisioned "
                        "onnx_models root."
                    ) from exc
                try:
                    ce.fetch_service_crossencoder_onnx()
                except RuntimeError as exc:
                    warnings.warn(
                        f"nexus test substrate: cross-encoder ONNX not provisioned "
                        f"({exc}); rerank tests will see a degraded engine.",
                        stacklevel=2,
                    )
            finally:
                unlock_file(lock_fh)
    return root


def _session_port_offset(slices: int) -> int:
    """This session's rotation of the slice table, in ``range(slices)``.

    A rotation permutes slices; it does not make two busy sessions disjoint (see
    the comment above ``_WORKER_SHARD_WIDTH``). Keyed on what every xdist worker of
    one session shares and no two concurrent sessions do:
    ``PYTEST_XDIST_TESTRUNUID`` (xdist sets it in each worker, in
    ``xdist/remote.py``, together with ``PYTEST_XDIST_WORKER`` from the controller's
    per-run uuid). The only caller, ``_worker_shard_range``, asks for an offset
    only when ``PYTEST_XDIST_WORKER`` names a ``gw<N>`` worker; non-xdist runs and
    the controller take the full range and never reach here. So the
    ``os.getppid()`` fallback (the controller's pid, which is the worker's parent)
    applies only to an xdist too old to set ``PYTEST_XDIST_TESTRUNUID``, or to a
    caller that sets ``PYTEST_XDIST_WORKER`` by hand. Deterministic within a
    session, so a worker computes the same offset on every call.
    """
    salt = os.environ.get("PYTEST_XDIST_TESTRUNUID") or f"ppid-{os.getppid()}"
    digest = hashlib.sha256(salt.encode()).digest()
    return int.from_bytes(digest[:8], "big") % slices


def _worker_shard_range() -> range:
    """This process's slice of :data:`_LOW_PORT_RANGE`, derived from
    ``PYTEST_XDIST_WORKER`` (pytest-xdist sets this to ``"gw0"``,
    ``"gw1"``, ... in each worker process; unset outside ``-n auto`` — a
    plain ``pytest`` / ``-n 0`` invocation, or a non-pytest caller, gets
    the FULL range, identical to pre-sharding behaviour).

    The slice width comes from :func:`_worker_shard_width`, so a run's
    workers always fit. The slice INDEX is the worker index rotated by
    :func:`_session_port_offset`, so concurrent sessions' same-index workers
    land on different slices (nexus-kmfz6). A worker index beyond the capacity at that width
    (more than 180 workers, or a stale worker count), or a malformed
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
    width = _worker_shard_width()
    max_index = (_LOW_PORT_RANGE.stop - _LOW_PORT_RANGE.start) // width - 1
    if index < 0 or index > max_index:
        if index > max_index:
            # Beyond-capacity fallback loses the disjoint-shard TOCTOU
            # guarantee — say so, matching _free_port's loud-on-exhaustion
            # pattern (critique T2 [21526] round 3, Minor (b)).
            warnings.warn(
                f"PYTEST_XDIST_WORKER={worker!r} exceeds the "
                f"{max_index + 1}-shard port-sharding "
                "capacity; falling back to the full range without the "
                "disjoint-shard guarantee (nexus-lgdy1)",
                stacklevel=2,
            )
        return _LOW_PORT_RANGE
    slices = (_LOW_PORT_RANGE.stop - _LOW_PORT_RANGE.start) // width
    start = _WORKER_SHARD_BASE + ((index + _session_port_offset(slices)) % slices) * width
    return range(start, start + width)


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


#: How many ports a boot tries for each of its two listeners (Postgres, engine) before
#: it gives up and names the port a foreign listener holds (nexus-kmfz6).
_PORT_RACE_ATTEMPTS = 4

#: How long the engine has to log that it bound its port. A cold JVM runs schema
#: migration and model init first, so this is the old connect-wait's 60 s.
_ENGINE_READY_TIMEOUT_S = 60.0
_ENGINE_POLL_S = 0.1

#: The fixed part of the line ``NexusService.start`` logs once ``HttpServer.start``
#: returned: ``log.info("event=service_started port={}", getPort())``. The boot
#: appends the port and matches to end of line, so ``port=2018`` never matches a
#: line for ``port=20184``. ``tests/test_engine_ready_log_line_lint.py`` pins this
#: text against the Java source; a rename there would otherwise cost every boot the
#: full readiness timeout.
_ENGINE_READY_PREFIX = "event=service_started port="

#: Text a failed bind leaves behind: the JVM's ``BindException`` message (Windows
#: appends ``: bind``) and Postgres's ``could not bind ... Address already in use``.
_BIND_CONFLICT_MARKERS = ("address already in use", "only one usage of each socket address")


def _is_bind_conflict(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in _BIND_CONFLICT_MARKERS)


class _PortLost(RuntimeError):
    """The port this boot picked is held by someone else (nexus-kmfz6)."""

    def __init__(self, port: int, what: str, detail: str) -> None:
        self.port = port
        super().__init__(
            f"T2 engine substrate: {what} port {port} is held by a foreign listener "
            f"(another pytest session on this host drew the same port). {detail}"
        )


def _wait_engine_ready(
    proc: subprocess.Popen, port: int, log_path: str, timeout: float,
) -> None:
    """Return once THE ENGINE THIS BOOT SPAWNED is serving *port*.

    A connect to the port proves only that something listens there, and a sibling
    session's postmaster or engine does (nexus-kmfz6). The engine's own log is the
    proof instead: ``NexusService.start`` logs ``event=service_started port=<N>``
    only after ``server.start()`` returned (the line must END at the port number,
    see ``_ENGINE_READY_PREFIX``), and the log is the file this boot's JVM writes
    (truncated per attempt), so no other process can put the line there. A JVM that cannot bind exits, which this reads from ``proc.poll()``.

    Raises :class:`_PortLost` when the process exited on a bind failure,
    ``RuntimeError`` when it exited for any other reason, and ``TimeoutError`` when
    the line never appeared.
    """
    marker = f"{_ENGINE_READY_PREFIX}{port}"
    ready_line = re.compile(rf"^.*{re.escape(marker)}\s*$", re.MULTILINE)
    deadline = time.monotonic() + timeout
    while True:
        rc = proc.poll()
        try:
            with open(log_path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            text = ""
        if rc is not None:
            if _is_bind_conflict(text):
                raise _PortLost(port, "engine", f"Engine log tail:\n{text[-2000:]}")
            raise RuntimeError(
                f"T2 engine substrate: service did not bind port {port}; the engine "
                f"exited with rc={rc}. Tail of engine log ({log_path}):\n{text[-2000:]}"
            )
        if ready_line.search(text):
            # Confirm it answers, then that the same process is still the one answering.
            _wait_tcp("127.0.0.1", port, timeout=max(1.0, deadline - time.monotonic()))
            if proc.poll() is None:
                return
            continue
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"engine substrate: no {marker!r} in {log_path} after {timeout}s"
            )
        time.sleep(_ENGINE_POLL_S)


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
#:
#: PER USER (nexus-c6lsu): the tempdir is shared by every Unix user on a box, and the first
#: user to boot creates the directory 0775, so a second user gets PermissionError opening
#: ``slot-0.lock`` and every substrate test errors at setup. The uid is part of the name, so
#: each user gets a directory it owns; the cap then bounds one user's concurrent boots.
_BOOT_SEMAPHORE_DIR_PREFIX = "nexus_t2_substrate_boot_locks"


def _boot_semaphore_dir(root: Path, uid: int | str | None = None) -> Path:
    """The boot-lock directory for one user under ``root`` (``uid`` defaults to ours).

    The default is the service identity (``str(os.getuid())`` on POSIX, so the
    directory name is byte for byte what it was; the user SID on Windows, where
    ``os.getuid`` does not exist and this runs at import time, so a native
    Windows pytest run died in ``tests/conftest.py`` before collecting a test,
    RDR-224, nexus-f9bgu.19).
    """
    if uid is None:
        from nexus.daemon.service_registry import service_identity  # noqa: PLC0415 — deferred: keep this module's import light until the default is needed

        uid = service_identity()
    return root / f"{_BOOT_SEMAPHORE_DIR_PREFIX}-{uid}"


_BOOT_SEMAPHORE_DIR = _boot_semaphore_dir(_original_tempdir())

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
    CONCURRENTLY across every pytest process of THIS Unix user (the lock directory is
    per uid, nexus-c6lsu; the SysV shm budget it protects is machine-wide, so N users
    can each hold ``max_concurrent`` slots) (nexus-ui654)
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
    username: str | None = None,
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

    *username* names the bootstrap superuser (``initdb -U``); None keeps
    initdb's own default, the OS account name.
    """
    # parent_dir None = the ORIGINAL tempdir, not the run's tmpfs TMPDIR (nexus-kyujr): see
    # integration_cluster_dir.
    pgdata = tempfile.mkdtemp(
        prefix=prefix, dir=parent_dir if parent_dir is not None else _original_tempdir(),
    )
    # Windows: the user's own SID gets an inheritable ACE, or initdb's
    # restricted token cannot write the directory under an elevated session
    # (tests._child_process.pg_data_tempdir). A no-op on POSIX.
    grant_user_tree_access(pgdata)
    proc = subprocess.run(
        [str(bin_dir / "initdb"), "-D", pgdata, "--no-locale", "-E", "UTF8",
         "--auth=trust", *(["-U", username] if username else [])],
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
    # nexus-942h9: the same root and conf as the substrate and the integration
    # clusters, unless the caller names a parent (the unit tests of this helper do).
    chosen_root = parent_dir is None
    if chosen_root:
        parent_dir = pg_cluster_parent()
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
                    if chosen_root and parent_dir is not None:
                        register_owned_cluster(Path(parent_dir), str(pgdata))
                    with open(pgdata / "postgresql.conf", "a") as f:
                        f.write("listen_addresses = ''\n")
                        f.write(throwaway_cluster_conf(str(pgdata.parent)))
                    proc = pg_ctl_start(
                        bin_dir / "pg_ctl", pgdata, pgdata / "pg.log",
                        f"-k {pgdata}", check=False,
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
                    # A postmaster can be half up when pg_ctl's wait gives
                    # up; stop it before the rmtree, or it outlives its
                    # pgdata holding the SysV segment this helper exists
                    # to conserve (rbc7k review). A no-op when nothing
                    # started.
                    subprocess.run(
                        [str(bin_dir / "pg_ctl"), "-D", str(pgdata), "stop", "-m", "immediate"],
                        capture_output=True, text=True, check=False,
                    )
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


def _pin_ownerless_write_mode(env: dict[str, str]) -> None:
    """RDR-223 Phase 3 Step 2 (nexus-z0o2p.24): an UNSET NX_OWNERLESS_WRITE_MODE is log-only on
    the engine, but the suite pins the posture the local launch ships (enforce) so every
    substrate-backed test sees the refusal. An explicit value in the caller's environment (a
    log-only census run) wins.

    An EMPTY or blank value counts as unset, the same rule the local launcher applies
    (``storage_service_daemon._spawn_service``): the engine parses blank as log-only, so
    ``env.setdefault``, which keeps an empty string, would hand the suite a silently log-only
    engine and every refusal test would pass vacuously or fail for the wrong reason.
    """
    if not env.get("NX_OWNERLESS_WRITE_MODE", "").strip():
        env["NX_OWNERLESS_WRITE_MODE"] = "enforce"


def _jar_ready_reason(jar: Path) -> str | None:
    """The boot-time jar verdict, waiting for a build first (nexus-wwaqm).

    A build that took the lease after the session-start gate
    (tests/conftest.py ``_gate_on_build_lease``) is first seen here, at this
    worker's first boot. Wait for it through NX_BUILD_LEASE_WAIT as the gate
    does; the default 0 is a single look. Only the boot path waits:
    ``jar_freshness_skip_reason`` stays an immediate check, because the
    session-start preflight (tests/conftest.py ``_preflight_engine_substrate``)
    also calls it and must never block.
    """
    if build_in_progress_reason():
        wait_for_build_lease(build_lease_wait_seconds())
    return jar_freshness_skip_reason(jar)


def _psql_command(
    bin_dir: Path, pg_port: int, pg_user: str, sql: str, *, timeout: float | None = None,
) -> None:
    """Run *sql* in the substrate database as the cluster owner; RuntimeError on failure."""
    try:
        proc = subprocess.run(
            [str(bin_dir / "psql"), "-X", "-q", "-h", "127.0.0.1", "-p", str(pg_port),
             "-U", pg_user, "-d", _DBNAME, "-v", "ON_ERROR_STOP=1", "-c", sql],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"psql timed out after {timeout}s") from exc
    if proc.returncode != 0:
        raise RuntimeError(f"psql failed: {proc.stderr.strip()[-300:]}")


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
    # The cheapest precondition first: before PG is booted or models fetched
    # (critique of 5ded8b067).
    java = shutil.which("java")
    if java is None:
        raise RuntimeError("T2 engine substrate: no java on PATH")
    stale = _jar_ready_reason(_JAR)
    if stale:
        raise RuntimeError(
            f"T2 engine substrate unavailable: {stale}. The unit suite's T2 "
            "substrate is the real engine (RDR-155 P4b P0a', decision D-A) — "
            "build it with: scripts/build-gate-jar.sh"
        )
    bin_dir = _pg_bin()
    if not bin_dir.exists():
        raise RuntimeError(_no_pg_message())
    # Before PG boots and before any port is probed: a cold fetch is slow, and
    # a probed port is released the moment the probe returns (review of
    # 5ded8b067). The engine's spawn env is os.environ plus fixed NX_* keys,
    # none of which is NX_VOYAGE_API_KEY, so os.environ decides the posture.
    onnx_root = _ensure_onnx_models(dict(os.environ))

    pg_port = _free_port()
    # Named explicitly to initdb below: Windows sets no USER, and its
    # superuser is the SID-derived bootstrap_superuser() name (nexus-ja4pq).
    from nexus.db.pg_provision import bootstrap_superuser  # noqa: PLC0415 — deferred import, function-local as in tests/db/_service_fixture.py

    pg_user = bootstrap_superuser()
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
        cluster_parent = pg_cluster_parent()
        pgdata = _initdb_cluster(
            bin_dir, prefix="nexus_t2_substrate_pg_", username=pg_user,
            parent_dir=cluster_parent,
        )
        sidecar_started_at = _write_sidecar(
            pgdata, pg_port=pg_port, svc_port=None,
            postmaster_pid=None, engine_pid=None,
        )
        with open(os.path.join(pgdata, "postgresql.conf"), "a") as f:
            f.write(f"\nport = {pg_port}\nlisten_addresses = '127.0.0.1'\n")
            # The suite issues thousands of tiny transactions; keep fsync off
            # for the throwaway test cluster.
            f.write(THROWAWAY_FSYNC_CONF)
            # nexus-me3gm: cap pg_wal (default max_wal_size is 1 GB per cluster) where
            # the cluster is in RAM or on an explicit root; mmap DSM on tmpfs only.
            f.write(wal_cap_conf(cluster_parent))
            f.write(dsm_conf(cluster_parent))
            # nexus-sz651: DDL-per-run counting reads this cluster's pg.log (tests/_ddl_counter.py).
            f.write(log_conf())
        pg_log = os.path.join(pgdata, "pg.log")
        for pg_attempt in range(1, _PORT_RACE_ATTEMPTS + 1):
            # A lost race leaves its failure in pg.log; start each attempt on a clean log
            # so the classification below reads only this attempt's output.
            with contextlib.suppress(OSError):
                os.unlink(pg_log)
            _start = pg_ctl_start(
                bin_dir / "pg_ctl", pgdata, pg_log,
                f"-p {pg_port} -k {pgdata}", check=False,
            )
            if _start.returncode == 0:
                break
            # Carry pg_ctl's stderr and the log tail out (rbc7k critique):
            # CalledProcessError drops stderr, which is where PG names the
            # shmget ENOSPC that the throwaway helper retries and this
            # shared boot deliberately does not. Stop a half-started
            # postmaster first so it cannot outlive its pgdata holding a
            # SysV segment.
            subprocess.run(
                [str(bin_dir / "pg_ctl"), "-D", pgdata, "stop", "-m", "immediate"],
                capture_output=True, check=False,
            )
            try:
                _log_tail = open(pg_log).read()[-2000:]
            except OSError:
                _log_tail = "<no pg.log>"
            if _is_bind_conflict(_log_tail + _start.stderr):
                # nexus-kmfz6: another session's listener took the port between the bind
                # probe and the postmaster's own bind. pg_ctl -w waits on THIS data
                # directory's postmaster.pid, so a foreign postmaster can never satisfy it:
                # a nonzero exit is the whole story. Take a fresh port and go again.
                if pg_attempt < _PORT_RACE_ATTEMPTS:
                    lost, pg_port = pg_port, _free_port()
                    warnings.warn(
                        f"nexus test substrate: Postgres port {lost} is held by a foreign "
                        f"listener (attempt {pg_attempt} of {_PORT_RACE_ATTEMPTS}, bind "
                        f"failed); retrying on port {pg_port} (nexus-kmfz6)",
                        stacklevel=2,
                    )
                    with open(os.path.join(pgdata, "postgresql.conf"), "a") as f:
                        f.write(f"\nport = {pg_port}\n")  # last assignment wins
                    _write_sidecar(
                        pgdata, pg_port=pg_port, svc_port=None,
                        postmaster_pid=None, engine_pid=None,
                        started_at=sidecar_started_at,
                    )
                    continue
                _failure: str = str(_PortLost(
                    pg_port, "Postgres",
                    f"Gave up after {_PORT_RACE_ATTEMPTS} ports.\n"
                    f"{_start.stderr}\n--- pg.log tail ---\n{_log_tail}",
                ))
            else:
                _failure = (
                    f"nexus test substrate: pg_ctl start failed (rc={_start.returncode}) "
                    f"for {pgdata}:\n{_start.stderr}\n--- pg.log tail ---\n{_log_tail}"
                )
            shutil.rmtree(pgdata, ignore_errors=True)
            raise RuntimeError(_failure)
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
        # nexus-sz651: before the engine migrates, so its DDL (and every nested leaf a
        # tenant mint creates) reaches pg.log through the event trigger.
        # NX_DDL_COUNT=0 (nexus-3cwsw) skips the probe and both marks, the logging lines above and the plugin.
        if ddl_count_enabled():
            _psql_command(bin_dir, pg_port, pg_user, PROBE_SQL)
            # Everything in pg.log before this mark is boot (the bootstrap and the probe's own
            # install, never counted); the engine's migration is after it. Best effort: a missing
            # mark leaves the run unmeasured, which the ratchet refuses, but must not fail the boot.
            try:
                _psql_command(bin_dir, pg_port, pg_user, mark_sql(START_LABEL))
            except RuntimeError as exc:
                warnings.warn(f"nexus test substrate: DDL start mark failed: {exc}", stacklevel=2)
    except BaseException:
        _kill_pg()
        raise

    env = {
        **os.environ,
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
    _pin_ownerless_write_mode(env)
    if onnx_root is not None:
        env["NX_ONNX_MODEL_DIR"] = str(onnx_root)
    # Engine output goes to a FILE, never a PIPE (nexus-j0nec root cause):
    # an undrained 64KB stdout pipe fills after ~250 tenant mints of
    # Logback console logging, write(2) blocks holding the PrintStream
    # monitor, and every logging thread pins behind it — presenting as a
    # total engine wedge. Evidence: jstack pin in FileOutputStream.writeBytes
    # from TokenStore.issueToken's log.info; draining exactly the pipe's
    # 65,702 buffered bytes instantly unwedged the next mint.
    svc_log_path = os.path.join(pgdata, "engine.log")
    for engine_attempt in range(1, _PORT_RACE_ATTEMPTS + 1):
        svc_port = _free_port()
        spawn_env = {**env, "NX_SERVICE_PORT": str(svc_port)}
        # "wb" truncates, so the log below holds only THIS attempt's JVM.
        svc_log = open(svc_log_path, "wb")  # noqa: SIM115 — lifetime spans the pytest session, closed with the process
        svc = popen_in_group(
            engine_argv(java), popen=subprocess.Popen, env=spawn_env,
            stdout=svc_log, stderr=subprocess.STDOUT,
        )
        # Checkpoint 3 of 3: svc.pid is known SYNCHRONOUSLY the instant Popen
        # returns -- no reason to defer recording it until the engine is ready
        # (which can legitimately take up to 60s). Update the sidecar now,
        # before the wait, so a kill during the wait still leaves a complete,
        # reapable sidecar (nexus-ui654 follow-up, critic Q3).
        _write_sidecar(
            pgdata, pg_port=pg_port, svc_port=svc_port,
            postmaster_pid=postmaster_pid, engine_pid=svc.pid,
            started_at=sidecar_started_at,
        )
        try:
            # Proves the listener is THIS engine, not merely that something answers
            # (nexus-kmfz6); see _wait_engine_ready.
            _wait_engine_ready(svc, svc_port, svc_log_path, _ENGINE_READY_TIMEOUT_S)
            break
        except _PortLost as lost:
            kill_group(svc)
            svc_log.close()
            if engine_attempt == _PORT_RACE_ATTEMPTS:
                _kill_pg()
                raise RuntimeError(
                    f"{lost} Gave up after {_PORT_RACE_ATTEMPTS} ports."
                ) from None
            warnings.warn(
                f"nexus test substrate: engine port {svc_port} is held by a foreign "
                f"listener (attempt {engine_attempt} of {_PORT_RACE_ATTEMPTS}, the JVM "
                "failed to bind); retrying on a new port (nexus-kmfz6)",
                stacklevel=2,
            )
        except TimeoutError:
            kill_group(svc)
            svc_log.close()
            with open(svc_log_path, encoding="utf-8", errors="replace") as fh:
                out = fh.read()[-2000:]
            _kill_pg()
            raise RuntimeError(
                f"T2 engine substrate: service did not bind port {svc_port}. "
                f"Tail of engine log ({svc_log_path}):\n{out}"
            ) from None
        except BaseException:
            kill_group(svc)
            svc_log.close()
            _kill_pg()
            raise

    # nexus-sz651: everything in pg.log before this line is boot and migration, everything
    # after it is the tests' own DDL. Best effort: a failed mark must not fail the boot.
    if ddl_count_enabled():
        try:
            _psql_command(bin_dir, pg_port, pg_user, mark_sql(READY_LABEL))
        except RuntimeError as exc:
            warnings.warn(f"nexus test substrate: DDL ready mark failed: {exc}", stacklevel=2)

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
        # The provisioned ONNX model dir (None when the posture needs none), so a
        # test that spawns its own engine can hand it the same models (nexus-o5xyx.2).
        "onnx_root": str(onnx_root) if onnx_root is not None else None,
        "svc": svc,
    }
    atexit.register(_teardown)
    return state


def _teardown() -> None:
    global _state
    if _state is None:
        return
    stop_group(_state["svc"], grace_s=5)
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
    #: Owner records the sweep would not trust (not ours, or not a plain file).
    untrusted: list[str] = field(default_factory=list)
    legacy_reported: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def sweep_stale_substrate_clusters(*, tmp_root: Path | None = None) -> SweepResult:
    """Reap crash-orphaned ``nexus_t2_substrate_pg_*`` clusters.

    Called at the top of every ``_boot()`` (session-start sweep). Never
    raises: a per-cluster failure is a loud warning and the sweep moves on
    to the next cluster; nothing here may block substrate boot.
    """
    result = SweepResult()
    # No explicit root: every root clusters may live under (the tempdir AND the
    # tmpfs root, nexus-me3gm), so a crash-orphaned cluster in RAM is reaped
    # exactly like one on disk.
    roots = [tmp_root] if tmp_root is not None else substrate_pg_roots()
    for root in roots:
        if not root.exists():
            continue
        tmpfs = _is_tmpfs_root(root)
        for cluster_dir in sorted(root.glob(_STRAY_GLOB)):
            if not cluster_dir.is_dir():
                continue
            try:
                _sweep_one_cluster(cluster_dir, result, tmpfs=tmpfs)
            except Exception as exc:  # noqa: BLE001 - loud, never fatal (nexus-lgdy1)
                result.errors.append(str(cluster_dir))
                warnings.warn(
                    f"nexus test substrate sweep: error processing "
                    f"{cluster_dir}: {exc}",
                    stacklevel=2,
                )
        _sweep_owner_records(root, result)
    return result


def sweep_integration_owner_records() -> SweepResult:
    """:func:`sweep_stale_substrate_clusters` restricted to the integration owner
    records, under every root the sweep covers."""
    result = SweepResult()
    for root in substrate_pg_roots():
        if root.exists():
            _sweep_owner_records(root, result)
    return result


def _sweep_owner_records(root: Path, result: SweepResult) -> None:
    """Reap the integration clusters of dead owners recorded under *root*."""
    for rec in sorted(root.glob(_OWNER_RECORD_GLOB)):
        try:
            _sweep_owner_record(rec, root, result)
        except Exception as exc:  # noqa: BLE001 - loud, never fatal (nexus-lgdy1)
            result.errors.append(str(rec))
            warnings.warn(
                f"nexus test substrate sweep: error processing {rec}: {exc}",
                stacklevel=2,
            )


def _identify_postmaster(pid: int, pgdata: Path) -> str:
    """Like :func:`_identify_leg`, with no recorded cmdline: a postmaster started
    by ``pg_ctl -D <pgdata>`` carries that unique path in its own command line."""
    from nexus.daemon.service_registry import pid_alive, process_command

    if not pid_alive(pid):
        return "dead"
    actual = process_command(pid)
    if not actual:
        return "dead"
    return "ok" if str(pgdata) in actual else "mismatch"


def _owner_record_untrusted(rec: Path, result: SweepResult) -> bool | None:
    """Is *rec* a plain file owned by the service identity? ``None``: it vanished
    (another worker reaped it), which is not an error. Anything else untrusted is
    recorded in ``result.untrusted`` and left alone. A symlink or special file is
    warned about; a file of another uid is routine in a shared root and silent."""
    try:
        st = rec.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(st.st_mode):
        result.untrusted.append(str(rec))
        warnings.warn(
            f"nexus test substrate sweep: owner record {rec} is not a regular file "
            f"(symlink or special) -- ignoring it",
            stacklevel=3,
        )
        return True
    if os.name != "nt" and st.st_uid != int(_current_uid()):
        result.untrusted.append(str(rec))
        return True
    return False


def _owner_pgdata_refusal(d: Path, root: Path) -> str | None:
    """Why *d* (a path read from a record) must not be removed, or ``None``.
    ``rmtree`` follows nothing here: the name must be a real child of *root*
    (resolved on both sides, so ``<root>/..`` and symlinked dirs fail)."""
    if d.name in ("", ".", "..") or d.parent != root:
        return f"outside {root}"
    if d.is_symlink():
        return "a symlink"
    if d.exists() and d.resolve().parent != root.resolve():
        return f"outside {root}"
    if (d / _NESTED_PGDATA).is_symlink():
        # postmaster.pid is read through it and the postmaster it names is signalled.
        return f"holding a symlinked {_NESTED_PGDATA}/"
    return None


#: ``pg_provision.provision(config_dir)`` puts the cluster at ``<config_dir>/postgres``. A test that
#: provisions on a recorded dir (nexus-0ajjm) leaves its postmaster.pid one level below the dir the
#: record names.
_NESTED_PGDATA = "postgres"

#: How long the owner-record sweep waits for a stopped postmaster to stop RUNNING before it counts it as a
#: survivor and keeps its dir. A signal is asynchronous: on Linux a SIGKILLed pid still reads ``R`` in
#: ``/proc/<pid>/stat`` for about 100 us after ``kill()`` returns, so a single probe right after the stop
#: kept dirs whose postmaster was already dying (nexus-0ajjm, seen on chas).
_SURVIVOR_SETTLE_S: float = 5.0


def _owner_pgdata_candidates(d: Path) -> list[Path]:
    """The directories under a recorded cluster dir that can hold a ``postmaster.pid``: *d* itself (a cluster
    ``initdb`` made there) and ``<d>/postgres`` (one ``provision(d)`` made)."""
    nested = d / _NESTED_PGDATA
    return [d, nested] if nested.is_dir() else [d]


def _sweep_owner_record(rec: Path, root: Path, result: SweepResult) -> None:
    from nexus.daemon.service_registry import wait_for_exit  # noqa: PLC0415 — deferred: keep this module's import light

    untrusted = _owner_record_untrusted(rec, result)
    if untrusted is None or untrusted:
        return
    try:
        info = json.loads(rec.read_text())
        owner_pid = info.get("owner_pytest_pid")
    except FileNotFoundError:
        return
    except (OSError, ValueError, AttributeError) as exc:
        result.errors.append(str(rec))
        warnings.warn(
            f"nexus test substrate sweep: unreadable owner record {rec}: {exc} "
            f"-- leaving it untouched",
            stacklevel=2,
        )
        return
    if not isinstance(owner_pid, int):
        result.errors.append(str(rec))
        warnings.warn(
            f"nexus test substrate sweep: owner record {rec} has no valid "
            f"owner_pytest_pid -- leaving it untouched",
            stacklevel=2,
        )
        return
    if _owner_is_live(owner_pid, info.get("owner_cmdline", "") or ""):
        result.live_untouched.append(str(rec))
        return

    dirs: list[Path] = []
    for raw in info.get("pgdatas") or []:
        d = Path(str(raw))
        refusal = _owner_pgdata_refusal(d, root)
        if refusal is not None:
            # rmtree on a path read from JSON: only ever a real child of the
            # root the record sits in. Nothing is signalled either.
            result.errors.append(str(rec))
            warnings.warn(
                f"nexus test substrate sweep: owner record {rec} names {d}, "
                f"{refusal} -- leaving it untouched",
                stacklevel=2,
            )
            return
        if d.is_dir():
            dirs.append(d)

    engines: list[tuple[int, str]] = []
    for child in info.get("children") or []:
        pid = child.get("pid") if isinstance(child, dict) else None
        if isinstance(pid, int) and pid > 0:
            engines.append((pid, child.get("cmdline", "") or ""))
    postmasters: list[tuple[int, Path]] = []  # (postmaster pid, the pgdata its pid file sits in)
    pm_dir: dict[Path, Path] = {}  # that pgdata -> the recorded dir that holds it
    for d in dirs:
        for pgdata in _owner_pgdata_candidates(d):
            pm = _read_postmaster_pid(str(pgdata))
            if pm is not None:
                postmasters.append((pm, pgdata))
                pm_dir[pgdata] = d

    # Identify EVERY leg before signalling any (as _reap_cluster does).
    engine_status = {pid: _identify_leg(pid, cmd) for pid, cmd in engines}
    pm_status = {pid: _identify_postmaster(pid, d) for pid, d in postmasters}
    mismatched = [
        f"pid {pid}" for pid, st in {**engine_status, **pm_status}.items() if st == "mismatch"
    ]
    if mismatched:
        result.mismatch_refused.append(str(rec))
        warnings.warn(
            f"nexus test substrate sweep: refusing to reap {rec} -- "
            f"{', '.join(mismatched)} no longer matches what was recorded "
            f"(PID-reuse guard); NOTHING was signalled",
            stacklevel=2,
        )
        return

    for pid, _cmd in engines:
        if engine_status[pid] == "ok":
            _kill_engine_leg(pid)
    for pid, pgdata in postmasters:
        if pm_status[pid] == "ok":
            _kill_postmaster_leg(pid, pgdata)
    # A postmaster that survived its stop keeps its dir: removing the tree under it is the hazard this sweep
    # exists to avoid (and it would take a nested <dir>/postgres with it). The record stays for the next sweep.
    # A stop is confirmed by the exit, never by the signal: wait (zombie-aware) up to the settle bound.
    still_running = set(wait_for_exit([pid for pid, _ in postmasters], timeout_s=_SURVIVOR_SETTLE_S))
    survivors = {pm_dir[pgdata]: pid for pid, pgdata in postmasters if pid in still_running}
    for d in dirs:
        if d in survivors:
            continue
        shutil.rmtree(d, ignore_errors=True)
        result.reaped.append(str(d))
    if survivors:
        result.errors.append(str(rec))
        warnings.warn(
            f"nexus test substrate sweep: postmaster pid(s) {sorted(set(survivors.values()))} "
            f"still running after a stop attempt -- leaving {sorted(str(d) for d in survivors)} "
            f"and {rec} in place",
            stacklevel=2,
        )
        return
    rec.unlink(missing_ok=True)


def _is_tmpfs_root(root: Path) -> bool:
    """Is *root* a ``<shm>/nexus-test-pg-<id>`` directory (honouring the sweep's
    ``NX_TEST_PG_SHM_DIR`` seam)?"""
    shm = Path(os.environ.get(_SHM_DIR_SEAM_ENV) or _SHM_DIR)
    return root.name.startswith(f"{_TMPFS_DIR_PREFIX}-") and root.parent == shm


def _sweep_one_cluster(
    cluster_dir: Path, result: SweepResult, *, tmpfs: bool = False,
) -> None:
    sidecar_path = cluster_dir / _SIDECAR_FILENAME
    if sidecar_path.exists():
        _sweep_sidecar_cluster(cluster_dir, sidecar_path, result)
    elif tmpfs and _abandoned_bare_dir(cluster_dir):
        # Killed during initdb, before the first sidecar write. On disk that is
        # inert debris (and stays reported-only below); in tmpfs it pins RAM.
        shutil.rmtree(cluster_dir, ignore_errors=True)
        result.reaped.append(str(cluster_dir))
    else:
        _sweep_legacy_cluster(cluster_dir, result)


def _abandoned_bare_dir(cluster_dir: Path) -> bool:
    """No sidecar, no ``postmaster.pid`` (nothing ever started), and untouched for
    :data:`_SWEEP_BARE_TMPFS_MIN_AGE_S` -- long past initdb's ~1 s."""
    if (cluster_dir / "postmaster.pid").exists():
        return False
    try:
        age = time.time() - cluster_dir.stat().st_mtime
    except OSError:
        return False
    return age > _SWEEP_BARE_TMPFS_MIN_AGE_S


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

    Matches ``_boot()``'s group spawn and ``_teardown()``'s group stop
    (``tests._child_process``) for this exact process shape (round-2 review, Important:
    a single-PID signal would miss any child the JVM ever spawned, unlike
    every OTHER teardown path for this same process). Falls back to a
    bare single-PID signal only if ``getpgid`` fails (the process exited
    between the identity check and here — a race, not a design choice).
    """
    from nexus.daemon.service_registry import pid_alive

    def _signal_group(sig: int) -> bool:
        if getattr(os, "killpg", None) is None:
            # Windows: no process group to reach. The owner's job object
            # (tests._child_process.popen_in_group) already killed the tree
            # when the owner died; this is the single-process fallback.
            return safe_killpg(pid, sig)
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
    if not _signal_group(KILL_SIGNAL):
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
    from nexus.daemon.service_registry import hard_kill_pid, pid_alive

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
    # SIGKILL on POSIX, TerminateProcess on Windows; a pid that is already
    # gone (ESRCH, or WinError 87 on Windows) is False, never a raise.
    if not hard_kill_pid(pid):
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


def drop_test_tenant(state: dict, tenant: str) -> bool:
    """Drop a minted tenant's partition leaves, and the rows that reference them (RDR-225).

    Every tenant owns a leaf under each model partition of nexus.chunks and nexus.taxonomy_centroids, so a suite
    that mints one per test and never removes it grows its worker's PG without bound (past 2,000 tenants the
    engine's token insert outran its 1 s x 3 bound and answered 503 tenant_creation_busy). This calls
    ``nexus.drop_tenant_partitions`` over the substrate's direct PG access as the owning role: the function is not
    granted to any engine role.

    Never raises: it runs in fixture teardown, where an exception would replace the test's own result. A failure
    is logged as ``test_tenant_drop_failed`` and the call returns False.
    """
    try:
        proc = _REAL_SUBPROCESS_RUN(
            [str(Path(state["pg_bin"]) / "psql"), "-X", "-q", "-h", "127.0.0.1", "-p", str(state["pg_port"]),
             "-U", state["pg_user"], "-d", state["pg_dbname"], "-t", "-A", "-v", "ON_ERROR_STOP=1",
             "-v", f"tenant={tenant}", "-f", "-"],
            input="SELECT nexus.drop_tenant_partitions(:'tenant');",
            capture_output=True, text=True, timeout=60,
            env={**os.environ, "PGCONNECT_TIMEOUT": "10"},
        )
    except Exception as exc:  # noqa: BLE001 - teardown must not mask the test's result
        _log.warning("test_tenant_drop_failed", tenant=tenant, error=repr(exc))
        return False
    if proc.returncode != 0:
        _log.warning("test_tenant_drop_failed", tenant=tenant,
                     error=(proc.stderr or proc.stdout).strip()[-300:])
        return False
    return True


@contextmanager
def minted_test_tenant(state: dict) -> Iterator[tuple[str, str]]:
    """``mint_test_tenant`` for the length of a ``with`` block, then ``drop_test_tenant`` however the block ends."""
    tenant, token = mint_test_tenant(state)
    try:
        yield tenant, token
    finally:
        drop_test_tenant(state, tenant)
