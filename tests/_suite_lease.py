# SPDX-License-Identifier: AGPL-3.0-or-later
"""One substrate-heavy suite per box, enforced rather than agreed.

WHY THIS EXISTS. The build lease (scripts/lib/build-lease.sh) already makes
the box single-writer for Maven, and ``tests/conftest.py`` already REFUSES a
run while a build holds it. But the suite only ever READ that lease; it took
none of its own. So a build blocked a suite and a suite blocked nothing --
two full runs on one box saw each other not at all, which is the shape that
exhausts ``kern.sysv.shmmni`` and reports thousands of setup errors that read
as catastrophic breakage rather than as contention (nexus-6qp25).

Measured 2026-09-21: two sessions on one box ran a `tests/db/` directory and
a `pytest -n auto` concurrently for ~90 seconds, neither aware of the other.
Earlier the same day, one session's own orphaned `release-preflight.sh` ran
its lint bucket alongside that same session's release battery for seven
minutes. That second case is the argument for a lease over a protocol: both
processes belonged to one session, so no amount of announcing between
sessions would have caught it. A convention cannot see a process you forgot
you started; a lease can.

A SEPARATE RESOURCE FROM ``service``, DELIBERATELY. ``_service_fixture.
build_in_progress_reason`` is documented as REPORTS ONLY, never reclaims,
because "reclaiming from a reader would race the real acquire path in
build-lease.sh". Acquiring that same lease from Python would introduce
exactly the second writer that docstring refuses. This module owns BOTH
sides of its own ``suite`` resource, under the same lease root, so it may
reclaim a dead holder safely.

The lease root is the git COMMON dir, so every worktree of this repo shares
one lease -- the same choice build-lease.sh made after three worktree agents
ran three engine suites at once (nexus-g6xpa).
"""
from __future__ import annotations

import os
import time
from collections.abc import Callable
from pathlib import Path

#: Resource name under the shared lease root. Sibling of ``service``, which
#: belongs to build-lease.sh and is never written from Python.
RESOURCE = "suite"

#: Set in the environment by the holder, so DESCENDANTS know they are inside
#: it. A nested pytest -- and 19 test files in this repo spawn one -- is not a
#: second competitor for the box; it is one run inside another, already
#: serialised by construction. Without this the inner run asks for a lease the
#: outer run is holding and refuses, naming the OUTER RUN AS THE HOLDER, which
#: is how dd64caf9d red'd develop on test_real_config_dir_guard_wiring within
#: minutes of landing. Environment inheritance is exactly the process-tree
#: relationship we want: only children get it, and nesting depth is free.
HELD_BY_ENV = "NX_SUITE_LEASE_HELD_BY"

#: How long a caller waits for a live holder before giving up, when it has
#: asked to wait at all. Suites here run ~5-15 minutes, so a wait that cannot
#: outlast one is a wait that never succeeds.
DEFAULT_WAIT_SECONDS = 1800

#: A lease directory with no readable pid is reclaimed once it is this old.
#: Younger than this, the holder may be between its ``mkdir`` and its pid write
#: (microseconds, but a loaded or paused machine stretches that), and taking the
#: lease then would make a second holder. Older than this, nothing is coming:
#: the maker was killed (a WSL shutdown, an OOM kill, a failed ``rmdir`` in the
#: release, a directory made 0700 by another user) and every run would
#: otherwise wait out its whole timeout and exit 75 until a person renamed the
#: directory by hand (round-3 review M1).
PIDLESS_GRACE_SECONDS = 60.0


def _lease_root() -> Path:
    """Shared with the build lease, resolved per call rather than at import."""
    from tests.db._service_fixture import _build_lease_root  # noqa: PLC0415 — one source of truth for the root

    return _build_lease_root()


def lease_path(lease_root: Path | None = None) -> Path:
    """Where the suite lease directory lives, for a message that tells a person where to look."""
    return (lease_root or _lease_root()) / RESOURCE


def recovery_hint(lease_root: Path | None = None) -> str:
    """The sentence a refused run ends with: where the lease is, and how to clear one that has no live holder."""
    path = lease_path(lease_root)
    return (
        f"The lease is the directory {path}. If no pytest run is live on this box, clear it with: rm -rf {path} "
        f"(a lease with no readable pid is reclaimed automatically once it is {int(PIDLESS_GRACE_SECONDS)} s old)."
    )


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    return True


def inside_a_holder() -> bool:
    """True when this process is a DESCENDANT of the run holding the lease.

    Keyed on a live pid, not merely on the variable being set: if the holder
    died and left a child running, the child is a competitor again and should
    take its own lease rather than inherit an exemption from a corpse.
    """
    raw = os.environ.get(HELD_BY_ENV, "").strip()
    if not raw.isdigit():
        return False
    return _pid_alive(int(raw))


def holder(lease_root: Path | None = None) -> str | None:
    """Describe the live holder, or ``None`` when the resource is free.

    A dead holder reads as free. Never raises: a malformed or half-written
    lease must not block every future run on this module's own bug -- the
    same posture ``build_in_progress_reason`` takes, and for the same reason.
    """
    try:
        lease = (lease_root or _lease_root()) / RESOURCE
        if not lease.is_dir():
            return None
        pid_file = lease / "pid"
        if not pid_file.exists():
            return None
        pid = int(pid_file.read_text().strip())
        if not _pid_alive(pid):
            return None
        label = ""
        label_file = lease / "label"
        if label_file.exists():
            label = label_file.read_text().strip()
        return f"pid {pid}" + (f" ({label})" if label else "")
    except Exception:  # noqa: BLE001 — a lease reader's bug must not wedge the suite
        return None


def _set_aside(lease: Path) -> None:
    """Rename a lease to a unique name. Atomic, so of two reclaimers exactly one succeeds."""
    lease.rename(lease.with_name(f"{RESOURCE}.stale.{os.getpid()}.{time.time_ns()}"))


def _reclaim_if_dead(lease: Path, now: Callable[[], float] = time.time) -> None:
    """Drop a lease whose holder is gone, or that never got as far as naming one.

    Two cases, both reclaimed by the same atomic rename:

    * the pid file names a process that no longer exists;
    * the pid file is missing, empty, non-numeric or unreadable AND the lease is
      older than ``PIDLESS_GRACE_SECONDS``. A holder is written to disk in two
      steps (``mkdir``, then the pid), so a young pid-less directory is a holder
      in the middle of acquiring, not a corpse. Age is the newest mtime of the
      directory and its pid file, read against *now* (injectable, so the tests
      run on a fixed clock).

    Safe to race: the reclaim is a rename to a unique name, which is atomic,
    so of two processes reclaiming the same stale lease exactly one succeeds
    and the other's rename fails harmlessly. Removing the pid file in place
    would let both proceed to believe they had reclaimed it.
    """
    try:
        pid_file = lease / "pid"
        pid: int | None = None
        try:
            pid = int(pid_file.read_text().strip())
        except (OSError, ValueError):
            pid = None
        if pid is not None:
            if _pid_alive(pid):
                return
            _set_aside(lease)
            return
        newest = lease.stat().st_mtime
        try:
            newest = max(newest, pid_file.stat().st_mtime)
        except OSError:
            pass
        if now() - newest >= PIDLESS_GRACE_SECONDS:
            _set_aside(lease)
    except Exception:  # noqa: BLE001 — losing the reclaim race is the expected case
        return


def acquire(label: str, *, wait_seconds: int = 0, lease_root: Path | None = None,
            now: Callable[[], float] = time.time):
    """Take the suite lease, or report who holds it.

    Returns a zero-argument release callable on success, or ``None`` when the
    resource is held by someone still alive after *wait_seconds*. The caller
    decides what to do about that; this module never exits the process.

    ``os.mkdir`` is the whole mutual exclusion: it is atomic, so of N racing
    acquirers exactly one creates the directory and the rest see EEXIST.

    *now* is the clock the pid-less-lease grace is read against; only tests pass it.
    """
    root = lease_root or _lease_root()
    lease = root / RESOURCE
    deadline = time.monotonic() + max(0, wait_seconds)

    while True:
        try:
            root.mkdir(parents=True, exist_ok=True)
            os.mkdir(lease)
        except FileExistsError:
            _reclaim_if_dead(lease, now)
            try:
                os.mkdir(lease)
            except FileExistsError:
                if time.monotonic() >= deadline:
                    return None
                time.sleep(1.0)
                continue
        except Exception:  # noqa: BLE001 — an unwritable lease root must not block the suite
            return lambda: None

        try:
            (lease / "pid").write_text(f"{os.getpid()}\n")
            (lease / "label").write_text(f"{label}\n")
        except Exception:  # noqa: BLE001 — a half-written lease reads as free, which is correct
            pass

        # Descendants inherit this and skip the lease entirely.
        os.environ[HELD_BY_ENV] = str(os.getpid())

        def _release() -> None:
            os.environ.pop(HELD_BY_ENV, None)
            try:
                for child in lease.iterdir():
                    child.unlink(missing_ok=True)
                lease.rmdir()
            except Exception:  # noqa: BLE001 — a release that cannot finish leaves a lease its pid disowns
                pass

        return _release
