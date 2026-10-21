# SPDX-License-Identifier: AGPL-3.0-or-later
"""One substrate-heavy suite per box, enforced rather than agreed.

WHY THIS EXISTS. The build lease (scripts/lib/build-lease.sh) already makes
the box single-writer for Maven, and ``tests/conftest.py`` already REFUSES a
run while a build in its own checkout holds it (nexus-qt6q7). But the suite only ever READ that lease; it took
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

CONTENTION TELEMETRY (nexus-jqk5c). Every CONTENDED event appends one key=value
line to the per-user, per-host log build-lease.sh also writes (``record_event``):
a wait that succeeded, a refusal, a give-up, a stale reclaim. The default path is
under the user's own state dir, so each user on a box writes a file of its own; a
measurement that must see every user's events sets ``NX_LEASE_EVENTS_LOG`` to one
shared path, and a file this module creates is group-writable so users of one
group can append to it. An immediate, uncontended acquire writes nothing, so the
log is a record of contention. Best-effort: a failure to write is swallowed and
never changes an outcome.
"""
from __future__ import annotations

import os
import shutil
import socket
import time
from collections.abc import Callable
from datetime import datetime, timezone
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

#: A lease directory whose pid file is MISSING, EMPTY or GARBAGE is reclaimed
#: once it is this old. Younger than this, the holder may be between its
#: ``mkdir`` and its pid write (microseconds, but a loaded or paused machine
#: stretches that), and taking the lease then would make a second holder. Older
#: than this, nothing is coming: the maker was killed (a WSL shutdown, an OOM
#: kill, a failed ``rmdir`` in the release) and every run would otherwise wait
#: out its whole timeout and exit 75 until a person renamed the directory by
#: hand (round-3 review M1).
#:
#: An UNREADABLE pid file or lease directory (EACCES) is NOT in that set: we
#: cannot tell a corpse from a live holder whose files we may not read, and
#: guessing "corpse" steals a running suite's lease (round-4 review M1). It
#: reads as HELD and the run exits 75 naming the path and the remedy. To keep
#: peers able to read each other's leases, ``acquire`` creates the directory
#: and its files group/world-readable explicitly rather than trusting umask.
PIDLESS_GRACE_SECONDS = 60.0

#: Modes the lease directory and its files are given, chmod-ed after creation
#: so a peer's umask 077 cannot make a live holder unreadable to the next run.
#: Group-WRITABLE as well as readable (nexus-3tx9c): the lease root is shared by
#: the users of one group, and a corpse left by one user could not be cleared
#: by the other when it was 0755. This is explicit, whatever the umask, and
#: never world-writable. It does not touch the held-when-unreadable rule above.
LEASE_DIR_MODE = 0o775
LEASE_FILE_MODE = 0o664

#: A leftover ``suite.stale.*`` directory (a reclaimed lease whose removal was
#: interrupted or refused) older than this is removed by the sweep at acquire.
#: Younger than this, its reclaimer may still be removing it.
STALE_SWEEP_AGE_SECONDS = 3600.0

_STALE_PREFIX = f"{RESOURCE}.stale."

#: The per-user, per-host contention log; default ``${XDG_STATE_HOME:-$HOME/.local/state}/nexus/lease-events.log``.
#: Tests point it at a tmp file; scripts/lib/build-lease.sh reads the same variable.
EVENTS_LOG_ENV = "NX_LEASE_EVENTS_LOG"


#: Mode of a log file this module creates: group-writable, so a shared path works across a group.
EVENTS_LOG_MODE = 0o664


def events_log_path() -> Path | None:
    """Where contention events are appended, or ``None`` when no home can be found."""
    explicit = os.environ.get(EVENTS_LOG_ENV, "").strip()
    if explicit:
        return Path(explicit)
    state = os.environ.get("XDG_STATE_HOME", "").strip()
    if not state:
        home = os.environ.get("HOME", "").strip()
        if not home:
            return None
        state = str(Path(home) / ".local" / "state")
    return Path(state) / "nexus" / "lease-events.log"


def _kv(value: object) -> str:
    """A log value: bare unless it is empty or holds whitespace, a quote or a backslash, then double-quoted."""
    text = " ".join(str(value).splitlines())
    if not text or any(c.isspace() or c in '"\\' for c in text):
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return text


def record_event(
    name: str,
    outcome: str,
    waited_s: float = 0,
    *,
    holder_pid: int | None = None,
    holder_label: str | None = None,
    unguarded: bool = False,
) -> None:
    """Append ONE contention line to the per-user, per-host log. Never raises, never prints.

    Fields, space-separated: ``ts`` (ISO-8601 UTC), ``lease=suite``, ``name``,
    ``host``, ``pid``, ``waited_s``, ``outcome`` (``acquired_after_wait``,
    ``refused``, ``stale_reclaimed`` or ``gave_up``), then ``holder_pid`` and
    ``holder_label`` when known, then ``unguarded=1`` when the run went on
    despite the refusal (``NX_SUITE_LEASE_UNGUARDED=1``: that line is a bypass,
    not a blocked run) -- the shape build-lease.sh writes for ``lease=service``.
    One ``os.write`` on an ``O_APPEND`` descriptor, so concurrent writers do not
    interleave lines. A file this call creates is mode 0664 whatever the umask
    (users of one group can share a path via ``NX_LEASE_EVENTS_LOG``); an
    existing file's mode is never changed.
    """
    try:
        path = events_log_path()
        if path is None:
            return
        parts = [
            f"ts={datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}",
            "lease=suite",
            f"name={_kv(name)}",
            f"host={_kv(socket.gethostname().split('.')[0] or 'unknown')}",
            f"pid={os.getpid()}",
            f"waited_s={int(waited_s)}",
            f"outcome={outcome}",
        ]
        if holder_pid is not None:
            parts.append(f"holder_pid={holder_pid}")
        if holder_label:
            parts.append(f"holder_label={_kv(holder_label)}")
        if unguarded:
            parts.append("unguarded=1")
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_EXCL, EVENTS_LOG_MODE)
            try:
                os.fchmod(fd, EVENTS_LOG_MODE)  # the umask would otherwise drop the group bit
            except OSError:
                pass
        except FileExistsError:
            fd = os.open(path, os.O_WRONLY | os.O_APPEND)  # never chmod a file that was not ours to create
        try:
            os.write(fd, (" ".join(parts) + "\n").encode())
        finally:
            os.close(fd)
    except Exception:  # noqa: BLE001 -- telemetry must never change what the lease does
        pass


def _read_label(lease: Path) -> str | None:
    """The label the lease directory records, ``None`` when missing or unreadable."""
    try:
        return (lease / "label").read_text().strip() or None
    except Exception:  # noqa: BLE001 -- best-effort
        return None


def _holder_fields(lease: Path) -> tuple[int | None, str | None]:
    """The pid and label the lease directory records, each ``None`` when missing or unreadable."""
    pid: int | None = None
    try:
        pid = int((lease / "pid").read_text().strip())
    except Exception:  # noqa: BLE001 -- best-effort
        pid = None
    return pid, _read_label(lease)


def record_refusal(waited_s: float = 0, lease_root: Path | None = None) -> None:
    """Log a refusal (no wait requested), naming whoever the lease directory says holds it."""
    try:
        pid, label = _holder_fields(lease_path(lease_root))
    except Exception:  # noqa: BLE001 -- best-effort
        pid, label = None, None
    record_event(RESOURCE, "refused", waited_s, holder_pid=pid, holder_label=label)


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
    log = events_log_path()
    hint = (
        f"The lease is the directory {path}. If no pytest run is live on this box, set it aside with: "
        f"mv {path} {path}.stale-$(date +%s) (works across users because the lease root is group-writable "
        f"and not sticky; rm -rf {path} works only for the user who owns it) "
        f"(a lease with a missing, empty or garbage pid is reclaimed automatically once it is {int(PIDLESS_GRACE_SECONDS)} s old; "
        f"one whose files this user cannot read is never reclaimed, so check who owns {path} before clearing it)."
    )
    return hint + (f" Waits and refusals are logged to {log}." if log is not None else "")


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

    A dead holder reads as free; so does a lease whose pid file is missing,
    empty or garbage (``acquire`` reclaims those once they age past
    ``PIDLESS_GRACE_SECONDS``). A lease whose pid file or directory cannot be
    READ (EACCES) reads as HELD by an unknown process, never as free: a live
    holder we may not read must not be mistaken for a corpse (round-4 review
    M1). Never raises: a malformed or half-written lease must not block every
    future run on this module's own bug -- the same posture
    ``build_in_progress_reason`` takes, and for the same reason.
    """
    try:
        lease = (lease_root or _lease_root()) / RESOURCE
        if not lease.is_dir():
            return None
        pid_file = lease / "pid"
        try:
            pid = int(pid_file.read_text().strip())
        except (FileNotFoundError, ValueError):
            return None
        except OSError:
            return _UNREADABLE_HOLDER
        if not _pid_alive(pid):
            return None
        label = ""
        try:
            label = (lease / "label").read_text().strip()
        except OSError:
            pass
        return f"pid {pid}" + (f" ({label})" if label else "")
    except PermissionError:
        return _UNREADABLE_HOLDER
    except Exception:  # noqa: BLE001 — a lease reader's bug must not wedge the suite
        return None


#: What ``holder`` says when the lease exists but this user may not read it.
_UNREADABLE_HOLDER = "a holder whose lease files this user cannot read (permission denied)"


def _remove_best_effort(path: Path) -> None:
    """Delete a set-aside directory. A failure (the other user owns it) is ignored: the sweep retries."""
    try:
        shutil.rmtree(path)
    except Exception:  # noqa: BLE001 — cleanup must never raise into, or change the outcome of, an acquire
        pass


def _set_aside(lease: Path) -> None:
    """Rename a lease to a unique name, then remove it best-effort.

    The rename is atomic, so of two reclaimers exactly one succeeds, and it is
    the step that decides the winner; the removal after it is housekeeping and
    never raises (nexus-3tx9c). The renamed path is unique to this call, so no
    other process looks at it, and a removal that fails leaves it for the sweep.
    """
    aside = lease.with_name(f"{_STALE_PREFIX}{os.getpid()}.{time.time_ns()}")
    lease.rename(aside)
    _remove_best_effort(aside)


def _sweep_stale(root: Path, now: Callable[[], float]) -> None:
    """Remove leftover ``suite.stale.*`` directories at least ``STALE_SWEEP_AGE_SECONDS`` old.

    Best-effort and bounded to *root*: only direct children whose name starts
    with ``suite.stale.`` are considered, so the live ``suite`` lease and the
    build lease's ``service`` resource are never candidates. A symlink is
    skipped, not followed or removed, so a link planted in the lease root
    cannot make the sweep delete anything outside it.
    """
    try:
        cutoff = now() - STALE_SWEEP_AGE_SECONDS
        for entry in os.scandir(root):
            try:
                if not entry.name.startswith(_STALE_PREFIX) or entry.is_symlink():
                    continue
                if not entry.is_dir(follow_symlinks=False):
                    continue
                if entry.stat(follow_symlinks=False).st_mtime > cutoff:
                    continue
                _remove_best_effort(Path(entry.path))
            except Exception:  # noqa: BLE001 — one unreadable entry must not stop the rest
                continue
    except Exception:  # noqa: BLE001 — an unreadable or missing root is not this sweep's problem
        return


def _reclaim_if_dead(lease: Path, now: Callable[[], float] = time.time) -> tuple[int | None, str | None] | None:
    """Drop a lease whose holder is gone, or that never got as far as naming one.

    Two cases, both reclaimed by the same atomic rename:

    * the pid file names a process that no longer exists;
    * the pid file is MISSING, EMPTY or GARBAGE (non-numeric) AND the lease is
      older than ``PIDLESS_GRACE_SECONDS``. A holder is written to disk in two
      steps (``mkdir``, then the pid), so a young pid-less directory is a holder
      in the middle of acquiring, not a corpse. Age is the newest mtime of the
      directory and its pid file, read against *now* (injectable, so the tests
      run on a fixed clock).

    An UNREADABLE pid file or directory (EACCES, or any other OSError that is
    not "no such file") is neither: it is treated as HELD and never reclaimed,
    at any age. We cannot see whose it is, and a live holder with umask 077
    looks exactly like this to a peer. Failing closed costs a person one
    ``mv`` of the lease aside after the exit-75 message names the path
    (round-4 review M1; ``rm -rf`` fails across users, ``mv`` does not).

    Known gaps, both needing extreme conditions (round-4 review L4): (a) if the
    pid write fails after ``mkdir`` (ENOSPC, say) the live holder's lease is
    pid-less and is reclaimed after the grace; (b) a maker stalled longer than
    the grace between ``mkdir`` and its pid write would have its pid file
    overwrite the reclaimer's. Neither is guarded; the lease is a convention
    between cooperating suites, not a security boundary.

    The reclaim is a rename to a unique name, which is atomic, so of two
    processes reclaiming the same stale lease one rename succeeds and the
    other fails harmlessly. It is NOT a compare-and-swap: between reading a
    dead pid and renaming, a peer can reclaim, take a fresh lease at the same
    path, and have that fresh lease renamed aside, leaving two holders. The
    window is the few microseconds between the read and the rename, and the
    lease is a convention between cooperating suites; accepted, not guarded
    (round-5 review L1). Removing the pid file in place would widen it.

    Returns ``(pid, label)`` of the lease it set aside (each ``None`` when the
    lease did not record one), or ``None`` when it set nothing aside; the caller
    logs the ``stale_reclaimed`` event from it, after its own re-claim has
    succeeded: nothing here, and nothing between the rename and that re-claim,
    may do I/O the original code did not.
    """
    try:
        # Read BEFORE the pid: the window the docstring accepts runs from the
        # pid read to the rename, and this keeps it exactly as it was.
        label = _read_label(lease)
        pid_file = lease / "pid"
        pid: int | None = None
        try:
            pid = int(pid_file.read_text().strip())
        except (FileNotFoundError, ValueError):
            pid = None  # missing, empty or garbage: a pid-less lease
        except OSError:
            return None  # unreadable (EACCES and kin): HELD, fail closed
        if pid is not None:
            if _pid_alive(pid):
                return None
            _set_aside(lease)
            return pid, label
        newest = lease.stat().st_mtime
        try:
            newest = max(newest, pid_file.stat().st_mtime)
        except OSError:
            pass
        if now() - newest >= PIDLESS_GRACE_SECONDS:
            _set_aside(lease)
            return None, label
        return None
    except Exception:  # noqa: BLE001 — losing the reclaim race is the expected case
        return None


def _write_shared(path: Path, text: str) -> None:
    """Write *text* and make the file readable by peers regardless of umask."""
    path.write_text(text)
    path.chmod(LEASE_FILE_MODE)


def acquire(label: str, *, wait_seconds: int = 0, lease_root: Path | None = None,
            now: Callable[[], float] = time.time, unguarded: bool = False):
    """Take the suite lease, or report who holds it.

    Returns a zero-argument release callable on success, or ``None`` when the
    resource is held by someone still alive after *wait_seconds*. The caller
    decides what to do about that; this module never exits the process.

    ``os.mkdir`` is the whole mutual exclusion: it is atomic, so of N racing
    acquirers exactly one creates the directory and the rest see EEXIST.

    *now* is the clock the pid-less-lease grace and the stale-directory sweep
    are read against; only tests pass it. *unguarded* only marks the refusal and
    give-up lines (``unguarded=1``) when the caller will run on without the lease.

    One wait writes at most one telemetry line, its final outcome, carrying the
    whole wait: ``stale_reclaimed`` or ``acquired_after_wait`` once the lease is
    held (after its files are written, so no logging sits inside a race window),
    ``refused`` or ``gave_up`` when it is not.
    """
    root = lease_root or _lease_root()
    lease = root / RESOURCE
    started = time.monotonic()
    deadline = started + max(0, wait_seconds)
    contended = False  # slept at least once: a wait, not an immediate acquire
    seen: tuple[int | None, str | None] = (None, None)  # the holder last waited on

    _sweep_stale(root, now)
    while True:
        gone: tuple[int | None, str | None] | None = None  # what THIS pass set aside; used only if its re-claim wins
        try:
            root.mkdir(parents=True, exist_ok=True)
            os.mkdir(lease)
        except FileExistsError:
            gone = _reclaim_if_dead(lease, now)
            try:
                os.mkdir(lease)
            except FileExistsError:
                if time.monotonic() >= deadline:
                    # No wait budget is a refusal; a spent one is a give-up.
                    holder_pid, holder_label = _holder_fields(lease)
                    record_event(RESOURCE, "refused" if wait_seconds <= 0 else "gave_up",
                                 time.monotonic() - started, holder_pid=holder_pid, holder_label=holder_label,
                                 unguarded=unguarded)
                    return None
                seen = _holder_fields(lease)
                contended = True
                time.sleep(1.0)
                continue
        except Exception:  # noqa: BLE001 — an unwritable lease root must not block the suite
            return lambda: None

        # The pid first, each step on its own: a lease whose pid write is
        # skipped because an earlier step raised reads as pid-less and is
        # reclaimed after the grace while its holder is live (round-5 review I1).
        try:
            (lease / "pid").write_text(f"{os.getpid()}\n")
        except Exception:  # noqa: BLE001 — a half-written lease reads as free, which is correct
            pass
        for target, mode in ((lease / "pid", LEASE_FILE_MODE), (lease, LEASE_DIR_MODE)):
            try:
                target.chmod(mode)  # umask 077 would hide a live holder from the next run
            except Exception:  # noqa: BLE001 — unreadable to peers reads as HELD, which is safe
                pass
        try:
            _write_shared(lease / "label", f"{label}\n")
        except Exception:  # noqa: BLE001
            pass

        # Logged here, after the claim and the populate, never inside the
        # reclaim window. A pass that set a corpse aside and WON the re-claim
        # reports it (with the whole wait); otherwise a wait reports the holder
        # it waited on.
        if gone is not None:
            record_event(RESOURCE, "stale_reclaimed", time.monotonic() - started,
                         holder_pid=gone[0], holder_label=gone[1])
        elif contended:
            record_event(RESOURCE, "acquired_after_wait", time.monotonic() - started,
                         holder_pid=seen[0], holder_label=seen[1])

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
