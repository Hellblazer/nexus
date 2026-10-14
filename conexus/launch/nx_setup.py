#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# /// script
# requires-python = ">=3.12"
# ///
"""The setup process: install the Nexus CLI and, when asked, a service, in the
background, and say how far it got.

RDR-228 Phase 2 Step 2 (nexus-sv2wx.15). One copy, in the plugin. The stand-in
server starts it directly; the real ``nx-mcp`` reaches it through
``NX_SETUP_SCRIPT``. Standard library only and no ``nexus`` import, because it
runs on a machine that has no CLI yet. It runs CLI programs as child processes for
everything that needs Nexus.

Command line, so no other code re-implements the directory below. Every command
prints one JSON object on standard output and exits 0, whatever happened: an
unexpected failure prints an object whose ``state`` is ``error`` and whose
``message`` carries the error text, never a traceback. Exit 2 means the arguments
were wrong (argparse's own usage message on standard error and no object)::

    nx_setup.py start --mode local|cloud|cli-only|not-now [--autostart]
    nx_setup.py status
    nx_setup.py not-now

``start`` returns at once with the status and ``"started": true`` when it began a
run. While a run is live it starts nothing and returns that run's status with
``"started": false`` and ``"refused"``, a sentence naming the live run's mode and
the mode asked for. ``not-now`` is ``start --mode not-now``. Mode ``cloud`` is
refused outright and changes nothing (below).

**For code that imports this file** (the session-start hook, P2.4, nexus-sv2wx.17,
imports it rather than spawning it, so the setup directory has one reader):
:func:`status_view` (what ``status`` prints), :func:`provisioned` (is a local
service on disk), :func:`config_dir`, :func:`setup_dir`, :func:`read_status`, and
the constants :data:`STATES` (what a status file may hold), :data:`TERMINAL_STATES`
and :data:`ALL_STATES` (those plus the three that are only ever computed:
``interrupted``, ``none`` and ``error``). Importing this file starts nothing and
touches no file.

The setup directory is ``setup/`` under the Nexus config directory
(``NEXUS_CONFIG_DIR``, else ``NX_CONFIG_DIR``, else ``~/.config/nexus``, made
absolute)::

    status.json   the status (JSON, owner-only, replaced whole)
    status.error  written only when a run could not write status.json; says why
    setup.log     what the run's child programs printed, as they print it
    not-now       the "not now" marker
    lock/         the lock: a directory; ``owner`` holds the holder's token and
                  ``heartbeat`` is rewritten every 10 seconds
    lock.reclaim/ the reclaim guard (below), present for an instant

``${CLAUDE_PLUGIN_DATA}`` is not used.

**Modes.** ``not-now`` writes the marker and starts nothing. ``local`` and
``cli-only`` remove the marker first. ``cli-only`` stops after the build. ``local``
goes on to the service and verifies. ``cloud`` is refused: the credentials page
that saves a pair is P2.5 (nexus-sv2wx.18). The refusal reads the status and
writes nothing (no status, no marker, no lock, not even the directory), so it
cannot overwrite the record of a run. ``autostart`` is false unless given, so the
login unit is never registered without an explicit yes. When it is given and a
usable service is already running, the run does not register the unit and its
final message says so and how to do it.

**Steps**, each skipped when already done, decided from facts and not from this
file's own history alone:

1. build, skipped only for an identified Nexus CLI. The generation layout's ``nx``
   (the launcher's lookup, ``_cli_lookup``) is the install; the build is skipped
   unless ``nx self install --bootstrap --dry-run`` run by that ``nx`` exits 0,
   which is ``--bootstrap`` saying it would still act (a build died after the flip
   and before the shims: the build step resumes it). A hit on ``PATH`` counts only
   with ``nx-mcp`` beside it and ``nx --version`` printing ``nx, version <x>``;
   anything else (Nrwl's ``nx``) is never run again and the build runs. An ``nx`` that
   is found and cannot start (``_cli_lookup.cannot_start``: an interpreter ``uv``
   pruned; Sam, 2026-10-14) is not asked anything and the build runs, and
   ``nx self install --bootstrap`` replaces the generation that cannot run. The build
   is ``uv tool run --python 3.12 --from conexus==<plugin version> nx self install
   --bootstrap``. The version is the ``plugin.json`` beside this script. There is
   no fallback to the two documented commands: they run the cache prune and leave
   two copies. After the build ``nx`` is found by the same lookup, layout first,
   so an application started before the install needs no restart for this process.
2. service, ``local``: if the status file recorded that a run began ``nx init``
   and did not finish it, run ``nx init`` again (a run killed inside ``nx init``
   leaves the service files on disk and no service answering, which alone reads
   as "provisioned and down"). Else when an endpoint resolves
   (``_endpoint_resolve``, the hooks' definition of a usable service) there is
   nothing to do. Else when :func:`provisioned` the service is provisioned and
   down: ``nx daemon service install --autostart`` when ``autostart``, then ``nx
   daemon service start``. Else ``nx init --yes`` with ``--no-autostart`` unless
   ``autostart``, and always ``--no-beads-prime`` (``nx init`` otherwise installs
   a user-level beads PRIME.md).
3. verify: ``nx doctor``. Exit 0 is ``done``; anything else is ``doctor-failed``
   with the ``[✗]`` rows, read from the tail of the log.

**Child programs** print straight into ``setup.log`` (an append-mode handle, no
pipe), so the log shows a long step's output while it runs and a killed step's
output up to the kill. Each runs in its own process group (POSIX) and with
``CREATE_NO_WINDOW`` (Windows: a detached run has no console, and a console child
would get a window of its own). A step over :data:`STEP_TIMEOUT_SECONDS`, or one
whose run lost the lock, is killed with everything it started: ``killpg`` on POSIX,
``taskkill /T /F /PID`` on Windows.

**Statuses.** :data:`STATES` is the list a status file may hold. A status that is
``running`` while the lock is stale is reported by ``status`` as ``interrupted``,
no status file as ``none``, and a failure of the command itself as ``error``; those
three are computed and never written.

**Lock.** The caller that starts a run takes the lock, then starts the detached
process with the token; the process verifies the token and keeps the lock alive
with a heartbeat written on a thread of its own, because its work is child
programs that take minutes. A heartbeat write or owner read that fails is retried
a few times and, if it still fails, skipped: the lock counts as lost only when the
owner file is read and is someone else's (or the lock is gone). A lost lock stops
the run: its child is killed and it writes no status again (every status write
re-checks ownership), and it leaves the new holder's lock alone. The lock is
removed on every way out the process can catch: success, failure, an exception and
SIGTERM. A kill it cannot catch (SIGKILL, TerminateProcess) leaves the lock to go
stale: sixty seconds without a heartbeat. Liveness is the heartbeat, never a
process id, because the standard library has no reliable process test on Windows.
``os.mkdir`` is the atomic claim. Reclaiming a stale lock takes a second
``mkdir``, of ``lock.reclaim``, and re-reads the lock under it: the lock must
still be stale and still carry the owner token that was read stale. So of any
number of callers that read the same stale lock, one removes it and the rest
report the run as live. A reclaimer that dies holding the guard leaves it for
sixty seconds, and after that the guard is removed the same way; two callers racing
for the removal of a dead guard at the same instant remain a hole too narrow to
close without a process test.

**The status file** is owner-only from the call that creates it. POSIX:
``os.open(..., O_CREAT | O_EXCL, 0o600)``, so the umask can only narrow it. Windows:
``CreateFileW`` with a ``SECURITY_ATTRIBUTES`` holding the protected one-ACE
descriptor ``D:P(A;;FA;;;<user SID>)`` (the descriptor ``nexus._winsec`` applies
to credential files; that module cannot be imported here, so its two ctypes calls
are repeated and the SID comes from ``_endpoint_resolve``). Updates go to a
temporary file created the same way, flushed to disk, and renamed over
``status.json``, so no instant exists in which the status is wider than owner-only
or half written. A temporary file a killed run left behind is removed at the next
start once it is a minute old. When a run cannot write ``status.json`` at all
(disk full), it writes the reason to ``status.error`` if it can, and ``status``
reports that as a ``failed`` state with the OS error text; if even that write
fails the status stays ``running`` and reads as ``interrupted`` (the best a full
disk allows).

**The detach** is one function, :func:`detach`. POSIX: a new session, standard
input the null device, standard output and error the log, no inherited handles
(``close_fds``). Windows: a double launch with ``DETACHED_PROCESS |
CREATE_NEW_PROCESS_GROUP``, first with ``CREATE_BREAKAWAY_FROM_JOB`` and, if the
host's job refuses that (winerror 5), again without it. Measured on native Windows
for Claude Code 2.1.296 (nexus_rdr/228-research-32 and 228-research-33): the double
launch survives the tree kill Claude Code runs on the server; a plain child does
not. Claude Desktop 1.24012.9.0 puts servers in a job with the same flags and a
force-kill of Desktop left all three watcher modes alive (228-research-35); its
graceful tray-Quit path is NOT measured. Before the retry without breakaway the
process asks its own job (``IsProcessInJob``, ``QueryInformationJobObject``): when
the job has ``KILL_ON_JOB_CLOSE`` and allows neither breakaway flag, a launch
without breakaway would be killed when the job closes, so the status says setup
cannot detach on this host instead of reporting a start. If both launches are
refused it says the same; a WMI broker is the untried way out.

Test seams, none set in normal use: ``NX_SETUP_HEARTBEAT_SECONDS`` (the 10 second
interval) and ``NX_SETUP_RECLAIM_PAUSE`` (seconds to wait between reading a stale
lock and removing it).
"""
from __future__ import annotations

import sys

sys.dont_write_bytecode = True  # before the sibling imports: no .pyc in the plugin directory

import contextlib  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
import shutil  # noqa: E402
import signal  # noqa: E402
import subprocess  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any  # noqa: E402

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hooks", "scripts"),
)
import _cli_lookup  # noqa: E402 -- must follow the sys.path insert
import _endpoint_resolve  # noqa: E402
import _exec_path  # noqa: E402

# -- the statuses: the one place they are spelled ------------------------------------------

RUNNING = "running"
DONE = "done"
DOCTOR_FAILED = "doctor-failed"
FAILED = "failed"
UNSUPPORTED = "unsupported"
TERMINAL_STATES: tuple[str, ...] = (DONE, DOCTOR_FAILED, FAILED, UNSUPPORTED)
#: What a status file may hold.
STATES: tuple[str, ...] = (RUNNING, *TERMINAL_STATES)

#: Computed by :func:`status_view` (and the command line), never written.
INTERRUPTED = "interrupted"
NONE = "none"
ERROR = "error"
COMPUTED_STATES: tuple[str, ...] = (INTERRUPTED, NONE, ERROR)
#: Every value ``state`` can take in what ``status`` prints.
ALL_STATES: tuple[str, ...] = (*STATES, *COMPUTED_STATES)

#: What the status file records about ``nx init``, across runs.
INIT_BEGUN = "begun"
INIT_FINISHED = "finished"

MODE_LOCAL = "local"
MODE_CLOUD = "cloud"
MODE_CLI_ONLY = "cli-only"
MODE_NOT_NOW = "not-now"
MODES: tuple[str, ...] = (MODE_LOCAL, MODE_CLOUD, MODE_CLI_ONLY, MODE_NOT_NOW)

#: A heartbeat older than this is stale.
STALE_SECONDS = 60.0
#: How often the run rewrites its heartbeat.
HEARTBEAT_SECONDS = 10.0
#: A beat that cannot read the owner or write the heartbeat tries this many times...
HEARTBEAT_ATTEMPTS = 3
#: ...this many seconds apart, then waits for the next beat.
HEARTBEAT_RETRY_PAUSE = 0.2
#: A child program that runs longer than this is killed and its step fails.
STEP_TIMEOUT_SECONDS = 3600.0
#: An identity probe (``nx --version``, the bootstrap dry run) that runs longer than this is dropped.
PROBE_TIMEOUT_SECONDS = 60.0

_POLL_SECONDS = 0.25
_LOG_TAIL_BYTES = 256 * 1024
_TMP_MAX_AGE_SECONDS = 60.0

_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_BREAKAWAY_FROM_JOB = 0x01000000
_CREATE_NO_WINDOW = 0x08000000
_WINERROR_ACCESS_DENIED = 5
_JOB_BREAKAWAY_OK = 0x00000800
_JOB_SILENT_BREAKAWAY_OK = 0x00001000
_JOB_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_EXTENDED_LIMIT_INFORMATION = 9

_STATUS = "status.json"
_ERROR_FILE = "status.error"
_LOG = "setup.log"
_MARKER = "not-now"
_LOCK = "lock"
_GUARD = "lock.reclaim"

_DOCTOR_ROWS_MAX = 20
_DOCTOR_ROW_CHARS = 300

#: ``nx --version`` of the Nexus CLI (click's ``version_option(prog_name="nx")``).
_VERSION_LINE = re.compile(r"nx, version [0-9][0-9A-Za-z.+!_-]*")

_AUTOSTART_NOTE = (
    "A usable service was already running, so the login unit was not registered. "
    "To start the service at login, run `nx daemon service install --autostart` in a terminal."
)
_NEXT_NONE = "Ask the user which they want (local, cli-only or not-now), then call setup with that mode."
_REFUSED_MODE = "This build of the plugin cannot set up a managed service yet: it has no credentials page for one."
_REFUSED_MODE_NEXT = "Ask the user to choose local or cli-only, then call setup with that mode."


class DetachUnsupported(Exception):
    """The host does not let a process start one that outlives it."""


class _Terminated(SystemExit):
    """SIGTERM reached the run: unwind so its clean-up runs."""


class _LockLost(Exception):
    """The lock is someone else's now: this run writes nothing more."""


# -- paths -----------------------------------------------------------------------------------


def config_dir() -> Path:
    """The Nexus config directory, absolute (a relative one would be read against
    the setup directory once the detached run changes into it).

    ``NEXUS_CONFIG_DIR``, else ``NX_CONFIG_DIR``, else ``~/.config/nexus``. The CLI
    (``nexus.config``) reads only ``NEXUS_CONFIG_DIR``; honouring ``NX_CONFIG_DIR``
    here is the bead's rule and a mismatch to settle in P2.R2 (nexus-sv2wx.22).
    """
    return Path(os.path.abspath(_endpoint_resolve.default_config_dir()))


def _child_env() -> dict[str, str]:
    """This process's environment with ``NEXUS_CONFIG_DIR`` set to the absolute
    :func:`config_dir`. A detached run starts in the setup directory, so a relative
    value read there would name a different tree; the CLI it runs reads only
    ``NEXUS_CONFIG_DIR``, so this also hands it the same directory."""
    env = dict(os.environ)
    env["NEXUS_CONFIG_DIR"] = str(config_dir())
    return env


def setup_dir() -> Path:
    """``setup/`` under :func:`config_dir`."""
    return config_dir() / "setup"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# -- the status file: owner-only from the creating call ----------------------------------------


def _create_private_windows(path: str) -> int:
    """Create *path* exclusively with a protected one-ACE descriptor in the same
    ``CreateFileW`` call. Windows only; raises OSError."""
    import ctypes  # noqa: PLC0415 -- Windows-only branch
    import msvcrt  # noqa: PLC0415 -- Windows-only branch
    from ctypes import wintypes  # noqa: PLC0415 -- Windows-only branch

    class SecurityAttributes(ctypes.Structure):
        _fields_ = [
            ("nLength", wintypes.DWORD),
            ("lpSecurityDescriptor", ctypes.c_void_p),
            ("bInheritHandle", wintypes.BOOL),
        ]

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p,
    ]
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(SecurityAttributes),
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    sddl = f"D:P(A;;FA;;;{_endpoint_resolve._windows_user_sid()})"  # noqa: SLF001 -- the SID reader is a mirror of nexus._winsec's, kept in one place
    descriptor = ctypes.c_void_p()
    if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), None):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        attrs = SecurityAttributes(ctypes.sizeof(SecurityAttributes), descriptor, False)
        # GENERIC_WRITE; FILE_SHARE_READ; CREATE_NEW; FILE_ATTRIBUTE_NORMAL
        handle = kernel32.CreateFileW(path, 0x40000000, 0x1, ctypes.byref(attrs), 1, 0x80, None)
    finally:
        kernel32.LocalFree(descriptor)
    if handle is None or handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    return msvcrt.open_osfhandle(handle, os.O_WRONLY | os.O_BINARY)  # type: ignore[attr-defined]


def _create_private(path: str) -> int:
    """A new file at *path*, owner-only from the call that makes it."""
    if os.name == "nt":
        return _create_private_windows(path)
    return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0), 0o600)


def _retry_sharing(action: Any) -> Any:
    """Run *action*, retrying a Windows sharing violation (a reader holds the file
    during a rename) for up to two seconds. Elsewhere a PermissionError is real."""
    if os.name != "nt":
        return action()
    deadline = time.monotonic() + 2.0
    while True:
        try:
            return action()
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.01)


def _write_private_json(setup: Path, name: str, data: dict[str, Any]) -> None:
    """Replace ``setup/<name>`` with *data*: owner-only throughout, flushed to disk
    before it replaces the old file."""
    payload = json.dumps(data, indent=2, sort_keys=True).encode("utf-8")
    import secrets  # noqa: PLC0415 -- lazy: the session-start hook imports this file and has 100 ms

    tmp = setup / f"{name}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    fd = _create_private(str(tmp))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        _retry_sharing(lambda: os.replace(tmp, setup / name))
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def write_status(setup: Path, data: dict[str, Any]) -> None:
    """Replace ``status.json`` with *data*, owner-only throughout."""
    _write_private_json(setup, _STATUS, data)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        text = _retry_sharing(lambda: path.read_text(encoding="utf-8"))
        data = json.loads(text)
    except (OSError, ValueError):  # UnicodeDecodeError is a ValueError
        return None
    return data if isinstance(data, dict) else None


def read_status(setup: Path) -> dict[str, Any] | None:
    """``status.json`` as a dict, or ``None`` when it is missing or not JSON."""
    return _read_json(setup / _STATUS)


def _sweep_tmp(setup: Path) -> None:
    """Remove temporary status files a killed writer left behind. A live writer's
    file lives for microseconds, so a minute-old one is an orphan."""
    for path in setup.glob("*.tmp"):
        age = _mtime_age(path)
        if age is not None and age > _TMP_MAX_AGE_SECONDS:
            with contextlib.suppress(OSError):
                path.unlink()


# -- the lock ----------------------------------------------------------------------------------


def _mtime_age(path: Path) -> float | None:
    try:
        return time.time() - path.stat().st_mtime
    except OSError:
        return None


def _lock_stale(lock: Path) -> bool:
    """No heartbeat for :data:`STALE_SECONDS`. A lock whose heartbeat is not yet
    written (its maker is between ``mkdir`` and the first write) is judged by the
    directory's own age."""
    age = _mtime_age(lock / "heartbeat")
    if age is None:
        age = _mtime_age(lock)
    return age is not None and age > STALE_SECONDS


def _lock_owner(lock: Path) -> str | None:
    try:
        return (lock / "owner").read_text(encoding="utf-8").strip() or None
    except (OSError, ValueError):
        return None


def _owner_check(lock: Path, token: str) -> bool | None:
    """``True``: the lock is *token*'s. ``False``: it is someone else's or gone.
    ``None``: it cannot be told now (a sharing violation, an unreadable or empty
    owner file), which is not a loss."""
    try:
        text = (lock / "owner").read_text(encoding="utf-8")
    except FileNotFoundError:
        return False
    except (OSError, ValueError):
        return None
    owner = text.strip()
    if not owner:
        return None
    return owner == token


def _write_heartbeat(lock: Path) -> None:
    (lock / "heartbeat").write_text(str(time.time()), encoding="utf-8")


def _exists(path: Path) -> bool:
    """``path.exists()`` that answers True rather than raising when the path
    cannot be examined: on Windows a directory being deleted while another
    process holds a handle to it (an indexer, antivirus) refuses ``stat`` with
    access denied for a moment."""
    try:
        return path.exists()
    except OSError:
        return True


def _make_lock(lock: Path, token: str) -> bool:
    try:
        os.mkdir(lock)
    except FileExistsError:
        return False
    except PermissionError:
        if os.name == "nt":  # the previous lock is still being deleted
            return False
        raise
    (lock / "owner").write_text(token, encoding="utf-8")
    _write_heartbeat(lock)
    return True


def _reclaim(setup: Path, lock: Path, seen_owner: str | None) -> bool:
    """Remove the stale *lock* this caller read (owner *seen_owner*), or learn it
    is not ours to remove. True means "try to make the lock now"; False means the
    run is live or another caller has it."""
    guard = setup / _GUARD
    try:
        os.mkdir(guard)
    except FileExistsError:
        age = _mtime_age(guard)
        if age is None or age <= STALE_SECONDS:
            return False
        with contextlib.suppress(OSError):
            os.rmdir(guard)
        try:
            os.mkdir(guard)
        except FileExistsError:
            return False
    try:
        if not _exists(lock):
            return True
        if not _lock_stale(lock) or _lock_owner(lock) != seen_owner:
            return False
        try:
            shutil.rmtree(lock)
        except OSError:
            return False
        return True
    finally:
        with contextlib.suppress(OSError):
            os.rmdir(guard)


def _wait_gone(path: Path, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not _exists(path):
            return True
        time.sleep(0.05)
    return not _exists(path)


def acquire_lock(setup: Path, *, wait_for_finish: bool = False) -> str | None:
    """Take the lock; returns the holder's token, or ``None`` when a run is live.

    *wait_for_finish*: a fresh lock beside a terminal status is a run that is
    removing its lock, and the caller waits a moment for it instead of dropping
    its own request.
    """
    import secrets  # noqa: PLC0415 -- see _write_private_json

    token = secrets.token_hex(8)
    lock = setup / _LOCK
    for _ in range(3):
        if _make_lock(lock, token):
            return token
        if not _lock_stale(lock):
            status = read_status(setup)
            if wait_for_finish and status is not None and status.get("state") in TERMINAL_STATES and _wait_gone(lock, 3.0):
                continue
            return None
        seen_owner = _lock_owner(lock)
        with contextlib.suppress(ValueError):
            time.sleep(float(os.environ.get("NX_SETUP_RECLAIM_PAUSE", "0") or 0))
        if not _reclaim(setup, lock, seen_owner):
            return None
    return None


def release_lock(setup: Path, token: str) -> None:
    """Remove the lock if it is still this holder's."""
    lock = setup / _LOCK
    if _lock_owner(lock) == token:
        shutil.rmtree(lock, ignore_errors=True)


class _Heartbeat(threading.Thread):
    """Rewrites the lock's heartbeat on a thread of its own."""

    def __init__(self, lock: Path, token: str) -> None:
        super().__init__(name="nx-setup-heartbeat", daemon=True)
        self._lock_dir = lock
        self._token = token
        self._stop_event = threading.Event()
        self.lost = threading.Event()
        try:
            self._interval = float(os.environ.get("NX_SETUP_HEARTBEAT_SECONDS", "") or HEARTBEAT_SECONDS)
        except ValueError:
            self._interval = HEARTBEAT_SECONDS

    def holds(self) -> bool:
        """Whether the lock is still this run's. Checked again here, on the
        caller's thread, so a status write never waits for the next beat."""
        if self.lost.is_set():
            return False
        if _owner_check(self._lock_dir, self._token) is False:
            self.lost.set()
            return False
        return True

    def _beat(self) -> bool:
        """One beat. ``False`` only when the lock is no longer ours; a beat that
        could not read or write is skipped (the next one tries again)."""
        for attempt in range(HEARTBEAT_ATTEMPTS):
            verdict = _owner_check(self._lock_dir, self._token)
            if verdict is False:
                return False
            if verdict is True:
                try:
                    _write_heartbeat(self._lock_dir)
                    return True
                except FileNotFoundError:  # the lock directory was removed under us
                    return False
                except OSError:
                    pass
            if attempt + 1 < HEARTBEAT_ATTEMPTS and self._stop_event.wait(HEARTBEAT_RETRY_PAUSE):
                return True
        return True

    def run(self) -> None:
        while not self._stop_event.wait(self._interval):
            if not self._beat():
                self.lost.set()
                return

    def stop(self) -> None:
        self._stop_event.set()
        self.join(timeout=5)


# -- facts -------------------------------------------------------------------------------------


def _on_path(name: str) -> str | None:
    """*name* on ``PATH`` alone: never the current directory, and on POSIX never a
    relative entry (the same rule as ``_cli_lookup.resolve``)."""
    if sys.platform == "win32":
        return _exec_path.which_off_cwd(name)
    entries = [e for e in os.environ.get("PATH", "").split(os.pathsep) if os.path.isabs(e)]
    return _exec_path.which_off_cwd(name, path=os.pathsep.join(entries))


def _find_uv() -> str | None:
    return _on_path("uv")


def _layout_nx() -> str | None:
    """The ``nx`` in the generation ``<tools>/current`` names, or ``None``."""
    generation = _cli_lookup.current_generation()
    if generation is None:
        return None
    return _exec_path.which_off_cwd("nx", path=os.path.join(generation, "bin"))


def plugin_version() -> str | None:
    """The version in the ``plugin.json`` of the plugin this script is part of."""
    manifest = Path(__file__).resolve().parent.parent / ".claude-plugin" / "plugin.json"
    try:
        version = json.loads(manifest.read_text(encoding="utf-8")).get("version")
    except (OSError, ValueError, AttributeError):
        return None
    return version if isinstance(version, str) and re.fullmatch(r"[0-9][0-9A-Za-z.+!_-]*", version) else None


def provisioned(config: Path | None = None) -> bool:
    """Whether the files ``nx init`` leaves for a local service are on disk:
    ``pg_credentials``, ``postgres/PG_VERSION`` and the service binary, under
    *config* (default :func:`config_dir`). All three, or it is not provisioned."""
    config = config_dir() if config is None else config
    binary = "nexus-service.exe" if sys.platform == "win32" else "nexus-service"
    return (
        (config / "pg_credentials").is_file()
        and (config / "postgres" / "PG_VERSION").is_file()
        and (config / "service" / binary).is_file()
    )


def _endpoint_resolves(config: Path) -> bool:
    """Whether a usable service is already configured: the CLI's own definition
    (``resolve_service_endpoint()`` returning), restated in ``_endpoint_resolve``. A
    saved address with no token is not usable (nexus-sv2wx.17, residual 9), so the
    service step does not call such a machine finished."""
    return _endpoint_resolve.endpoint_usable(config)


# -- children: no window, their own process group, killed whole ----------------------------------


def _child_kwargs(windows: bool | None = None) -> dict[str, Any]:
    """The ``Popen`` keywords for a child program. Windows: ``CREATE_NO_WINDOW``
    (this process has no console, so a console child would open its own window).
    POSIX: a new session, so the whole group can be killed."""
    windows = (os.name == "nt") if windows is None else windows
    if windows:
        return {"creationflags": _CREATE_NO_WINDOW}
    return {"start_new_session": True}


def _kill_tree(proc: Any, windows: bool | None = None) -> None:
    """Kill *proc* and everything it started. Never raises."""
    windows = (os.name == "nt") if windows is None else windows
    if windows:
        taskkill = os.path.join(os.environ.get("SystemRoot") or r"C:\Windows", "System32", "taskkill.exe")
        with contextlib.suppress(Exception):
            subprocess.run(  # noqa: S603 -- the system's own taskkill, by absolute path
                [taskkill, "/T", "/F", "/PID", str(proc.pid)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=30, check=False, creationflags=_CREATE_NO_WINDOW,
            )
    else:
        with contextlib.suppress(OSError):
            os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(OSError):
        proc.kill()
    with contextlib.suppress(Exception):
        proc.wait(timeout=10)


# -- the run -----------------------------------------------------------------------------------


class _Run:
    """The detached process's state: the status it keeps current, the log it
    appends to, and the heartbeat that holds the lock."""

    def __init__(self, setup: Path, mode: str, autostart: bool, heartbeat: _Heartbeat) -> None:
        self.setup = setup
        self.mode = mode
        self.autostart = autostart
        self.heartbeat = heartbeat
        self.log_path = setup / _LOG
        self.data: dict[str, Any] = read_status(setup) or {}
        self.data.update(state=RUNNING, mode=mode, autostart=autostart, log=str(self.log_path), pid=os.getpid())
        self.steps_done: list[str] = []
        self.stage = "starting"
        self.notes: list[str] = []

    def update(self, **fields: Any) -> None:
        """Write the status. Raises :class:`_LockLost` and writes nothing when the
        lock is not this run's."""
        if not self.heartbeat.holds():
            raise _LockLost
        self.data.update(fields)
        self.data["updated_at"] = _now()
        write_status(self.setup, self.data)

    def at(self, stage: str, message: str = "") -> None:
        self.stage = stage
        self.update(stage=stage, message=message or f"running: {stage}", steps_done=list(self.steps_done))

    def step_done(self, step: str) -> None:
        self.steps_done.append(step)

    def end(self, state: str, message: str, next_step: str, **fields: Any) -> int:
        """Write the terminal status and return the exit code. Never raises."""
        text = " ".join([message, *self.notes])
        try:
            self.update(state=state, stage=self.stage, message=text, next=next_step, steps_done=list(self.steps_done), **fields)
        except _LockLost:
            return 1
        except OSError as exc:
            self._record_write_failure(exc)
            return 1
        return 0 if state in (DONE, UNSUPPORTED) else 1

    def _record_write_failure(self, exc: OSError) -> None:
        """The terminal status could not be written: say why where ``status`` will
        find it, if that can be written at all."""
        message = f"setup could not record its own progress ({type(exc).__name__}: {exc})"
        record = {
            "state": FAILED, "stage": self.stage, "failed_step": self.stage, "mode": self.mode, "log": str(self.log_path),
            "updated_at": _now(), "message": message,
            "next": f"Free disk space or fix the permissions on {self.setup}, then, if the user agrees, call setup again. Log: {self.log_path}",
        }
        with contextlib.suppress(Exception):
            _write_private_json(self.setup, _ERROR_FILE, record)
        with contextlib.suppress(Exception):
            sys.stderr.write(message + "\n")

    def fail(self, step: str, message: str) -> int:
        return self.end(
            FAILED, message,
            f"If the user agrees, call setup again: it resumes at the {step} step. Log: {self.log_path}",
            failed_step=step,
        )

    def log(self, text: str) -> None:
        with open(self.log_path, "a", encoding="utf-8", errors="replace") as handle:
            handle.write(text)

    def _log_size(self) -> int:
        try:
            return self.log_path.stat().st_size
        except OSError:
            return 0

    def _output_since(self, offset: int) -> str:
        """What the log holds after *offset*, its last :data:`_LOG_TAIL_BYTES` at most."""
        try:
            start = max(offset, self._log_size() - _LOG_TAIL_BYTES)
            with open(self.log_path, "rb") as handle:
                handle.seek(start)
                return handle.read().decode("utf-8", errors="replace")
        except OSError:
            return ""

    def cmd(self, argv: list[str], *, timeout: float | None = None) -> tuple[int | None, str]:
        """Run *argv* with its output going straight into the log; returns the exit
        code (``None`` after a timeout) and the output it printed (the log's tail).
        The setup directory is the working directory: uv and the CLI must not read a
        project from wherever the application started this. Raises
        :class:`_LockLost`, after killing the child, when the lock is lost."""
        limit = STEP_TIMEOUT_SECONDS if timeout is None else timeout
        if not self.heartbeat.holds():
            raise _LockLost
        self.log(f"\n--- {_now()} {' '.join(argv)}\n")
        offset = self._log_size()
        with open(self.log_path, "ab") as sink:
            proc = subprocess.Popen(  # noqa: S603 -- argv[0] is an absolute path found by the lookups above
                argv, stdin=subprocess.DEVNULL, stdout=sink, stderr=subprocess.STDOUT, cwd=str(self.setup),
                **_child_kwargs(),
            )
            deadline = time.monotonic() + limit
            try:
                while True:
                    try:
                        code = proc.wait(timeout=_POLL_SECONDS)
                        break
                    except subprocess.TimeoutExpired:
                        pass
                    if not self.heartbeat.holds():
                        _kill_tree(proc)
                        self.log("--- killed: setup no longer holds the lock\n")
                        raise _LockLost
                    if time.monotonic() >= deadline:
                        _kill_tree(proc)
                        text = self._output_since(offset)
                        self.log(f"--- timed out after {limit:.0f} s\n")
                        return None, text
            except BaseException:
                if proc.poll() is None:
                    _kill_tree(proc)
                raise
        text = self._output_since(offset)
        self.log(f"--- exit {code}\n")
        return code, text


def _doctor_rows(text: str) -> list[str]:
    rows = [line.strip()[:_DOCTOR_ROW_CHARS] for line in text.splitlines() if "[✗]" in line]
    return rows[:_DOCTOR_ROWS_MAX]


def _is_nexus_cli(run: _Run, hit: str) -> bool:
    """A ``PATH`` hit is the Nexus CLI only with ``nx-mcp`` beside it and a
    version line in the Nexus form. A hit without ``nx-mcp`` is never run."""
    if _exec_path.which_off_cwd("nx-mcp", path=os.path.dirname(hit)) is None:
        return False
    if _cli_lookup.cannot_start(hit) is not None:  # found and cannot start: never run, never trusted
        return False
    try:
        code, text = run.cmd([hit, "--version"], timeout=PROBE_TIMEOUT_SECONDS)
    except OSError:
        return False
    return code == 0 and any(_VERSION_LINE.fullmatch(line.strip()) for line in text.splitlines())


def _bootstrap_would_act(run: _Run, nx: str) -> bool:
    """Whether the generation layout's *nx* says ``--bootstrap`` would still do
    something. ``nx self install --bootstrap --dry-run`` exits 0 when it would
    finish an interrupted install or build one and refuses (non-zero) when a
    complete install exists: the same judgment the build step would make, asked of
    the program that makes it."""
    try:
        code, _ = run.cmd([nx, "self", "install", "--bootstrap", "--dry-run"], timeout=PROBE_TIMEOUT_SECONDS)
    except OSError:
        return True  # it cannot even be started: whatever is installed is broken, and the build repairs it
    return code == 0


def _locate_nexus_cli(run: _Run) -> tuple[str | None, bool]:
    """``(nx, build_needed)``: the identified Nexus CLI, if any, and whether the
    build step must run."""
    layout = _layout_nx()
    if layout is not None:
        if _cli_lookup.cannot_start(layout) is not None:
            # Found and cannot start (an interpreter uv pruned; Sam, 2026-10-14). It is
            # not asked anything, so it cannot say "an install exists": the build runs,
            # and ``nx self install --bootstrap`` replaces the generation that cannot run.
            return None, True
        return layout, _bootstrap_would_act(run, layout)
    hit = _on_path("nx")
    if hit is not None and _is_nexus_cli(run, hit):
        return hit, False
    return None, True


def _nx_after_build(run: _Run) -> str | None:
    layout = _layout_nx()
    if layout is not None:
        return layout if _cli_lookup.cannot_start(layout) is None else None
    hit = _on_path("nx")
    return hit if hit is not None and _is_nexus_cli(run, hit) else None


def _execute(run: _Run) -> int:
    run.at("build")
    nx, build_needed = _locate_nexus_cli(run)
    if build_needed:
        uv = _find_uv()
        if uv is None:
            return run.fail("build", "`uv` was not found on PATH, so the Nexus CLI cannot be built.")
        version = plugin_version()
        if version is None:
            return run.fail("build", "the plugin's version could not be read from its plugin.json.")
        build = [uv, "tool", "run", "--python", "3.12", "--from", f"conexus=={version}", "nx", "self", "install", "--bootstrap"]
        try:
            code, _ = run.cmd(build)
        except OSError as exc:
            return run.fail("build", f"the build could not start: {exc}")
        if code != 0:
            return run.fail("build", "the build timed out." if code is None else f"the build exited {code}.")
        nx = _nx_after_build(run)
        if nx is None:
            layout = _layout_nx()
            reason = _cli_lookup.cannot_start(layout) if layout is not None else None
            if reason is not None:
                return run.fail("build", f"the build finished but the `nx` it left cannot start: {reason}.")
            return run.fail("build", "the build finished but `nx` is not in the Nexus install directory or on PATH.")
    assert nx is not None
    run.step_done("build")
    if run.mode == MODE_CLI_ONLY:
        return run.end(
            DONE, "The Nexus CLI is installed. No service is set up.",
            "Reconnect the nexus server or start a new session. To set up a service, ask the user first; if they agree, call setup with mode local.",
        )

    config = config_dir()
    code = _service(run, nx, config)
    if code is not None:
        return code

    run.at("verify")
    try:
        code, text = run.cmd([nx, "doctor"])
    except OSError as exc:
        return run.fail("verify", f"nx doctor could not start: {exc}")
    if code == 0:
        run.step_done("verify")
        return run.end(
            DONE, "Nexus is installed and nx doctor passes.",
            "Reconnect the nexus server or start a new session.", doctor="pass", doctor_failing=[],
        )
    rows = _doctor_rows(text)
    return run.end(
        DOCTOR_FAILED, "nx doctor reported problems." if code is not None else "nx doctor timed out.",
        f"Run `nx doctor` in a terminal to see the failing rows. Log: {run.log_path}",
        doctor="fail", doctor_failing=rows,
    )


def _service(run: _Run, nx: str, config: Path) -> int | None:
    """The service step for mode ``local``. ``None`` means it went well."""
    run.at("service")
    began_init = run.data.get("init") == INIT_BEGUN
    if not began_init and _endpoint_resolves(config):
        if run.autostart:
            run.notes.append(_AUTOSTART_NOTE)
        run.step_done("service")
        return None
    if not began_init and provisioned(config):
        if run.autostart:
            run.at("service-install")
            try:
                code, _ = run.cmd([nx, "daemon", "service", "install", "--autostart"])
            except OSError as exc:
                return run.fail("service-install", f"the login unit could not be registered: {exc}")
            if code != 0:
                return run.fail("service-install", "registering the login unit failed.")
        run.at("service-start")
        try:
            code, _ = run.cmd([nx, "daemon", "service", "start"])
        except OSError as exc:
            return run.fail("service-start", f"the service could not be started: {exc}")
        if code != 0:
            return run.fail("service-start", "starting the service failed.")
        run.step_done("service")
        return None
    run.at("init")
    run.update(init=INIT_BEGUN)
    argv = [nx, "init", "--yes"] + ([] if run.autostart else ["--no-autostart"]) + ["--no-beads-prime"]
    try:
        code, _ = run.cmd(argv)
    except OSError as exc:
        return run.fail("init", f"nx init could not start: {exc}")
    if code != 0:
        return run.fail("init", "nx init timed out." if code is None else f"nx init exited {code}.")
    run.update(init=INIT_FINISHED)
    run.step_done("service")
    return None


def run_setup(mode: str, autostart: bool, token: str) -> int:
    """The detached process: hold the lock, do the steps, leave a terminal status
    and no lock (a run that lost the lock leaves the new holder's alone and writes
    no status)."""
    setup = setup_dir()
    lock = setup / _LOCK
    if _owner_check(lock, token) is not True:
        sys.stderr.write("setup: this process does not hold the lock; exiting\n")
        return 1

    def on_term(signum: int, frame: Any) -> None:
        raise _Terminated(1)

    previous: dict[str, Any] = {}
    for name in ("SIGTERM", "SIGHUP"):
        if hasattr(signal, name):
            with contextlib.suppress(OSError, ValueError):
                previous[name] = signal.signal(getattr(signal, name), on_term)
    heartbeat = _Heartbeat(lock, token)
    heartbeat.start()
    run = _Run(setup, mode, autostart, heartbeat)
    try:
        try:
            run.at("starting", "setup is starting")
            return _execute(run)
        except _LockLost:
            return 1
        except _Terminated:
            return run.fail(run.stage, "setup was ended by a signal.")
        except Exception as exc:  # noqa: BLE001 -- a terminal status must be written whatever went wrong
            return run.fail(run.stage, f"unexpected {type(exc).__name__}: {exc}")
    finally:
        heartbeat.stop()
        release_lock(setup, token)  # a no-op unless the lock is still this run's
        for name, handler in previous.items():
            with contextlib.suppress(OSError, ValueError):
                signal.signal(getattr(signal, name), handler)


# -- starting a run ---------------------------------------------------------------------------


def _job_limit_flags() -> int | None:
    """``LimitFlags`` of the job this process is in, or ``None`` when it is in no
    job, is not on Windows, or the query fails (then the caller cannot tell and
    goes ahead)."""
    if os.name != "nt":
        return None
    try:
        import ctypes  # noqa: PLC0415 -- Windows-only branch
        from ctypes import wintypes  # noqa: PLC0415 -- Windows-only branch

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
        kernel32.IsProcessInJob.restype = wintypes.BOOL
        kernel32.QueryInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.QueryInformationJobObject.restype = wintypes.BOOL
        in_job = wintypes.BOOL()
        if not kernel32.IsProcessInJob(kernel32.GetCurrentProcess(), None, ctypes.byref(in_job)) or not in_job.value:
            return None

        class Basic(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD),
            ]

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
            )]

        class Extended(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", Basic), ("IoInfo", IoCounters), ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        info = Extended()
        if not kernel32.QueryInformationJobObject(
            None, _JOB_EXTENDED_LIMIT_INFORMATION, ctypes.byref(info), ctypes.sizeof(info), None,
        ):
            return None
        return int(info.BasicLimitInformation.LimitFlags)
    except Exception:  # noqa: BLE001 -- cannot ask: do not refuse on a guess
        return None


def _job_would_kill_an_unbroken_child() -> bool:
    """The host's job closes on the last handle (``KILL_ON_JOB_CLOSE``) and allows
    no breakaway, so a process started without breakaway dies with it."""
    flags = _job_limit_flags()
    return (
        flags is not None
        and bool(flags & _JOB_KILL_ON_JOB_CLOSE)
        and not flags & (_JOB_BREAKAWAY_OK | _JOB_SILENT_BREAKAWAY_OK)
    )


def detach(argv: list[str], log_path: Path | str, *, windows: bool | None = None) -> None:
    """Start ``nx_setup.py *argv*`` so that it outlives the caller and every
    process above it.

    POSIX: a new session, standard input the null device, standard output and
    error the log, no inherited handles.

    Windows: a double launch. The intermediate (``_relay``) starts the run and
    exits, so the run's parent pid is dead and a tree kill of the server cannot
    reach it. Flags ``DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP``, plus
    ``CREATE_BREAKAWAY_FROM_JOB`` first. When the host's job forbids breakaway the
    creation is refused with winerror 5 (the intermediate's or the run's), and the
    same double launch is tried again without breakaway, unless the host's job
    would kill it (:func:`_job_would_kill_an_unbroken_child`). Only when that is
    also refused is :class:`DetachUnsupported` raised. An intermediate that does
    not exit in 30 seconds is killed and reported as an :class:`OSError` (it is not
    retried: it may have started the run, which then finds the lock released and
    exits). The intermediate's standard handles are the null device, so it
    inherits no pipe of the server.

    Measured, native Windows, one host each: nexus_rdr/228-research-32 (a
    Popen flag set never survives a kill-on-close job that forbids breakaway; a
    double launch survives a tree kill) and 228-research-33 (Claude Code 2.1.296
    puts the server in a job with KILL_ON_JOB_CLOSE, BREAKAWAY_OK and
    SILENT_BREAKAWAY_OK, closes its stdin, and about 350 ms later runs a tree kill;
    a double launch survived 4 of 4 with or without breakaway, a plain child died).
    228-research-35: Claude Desktop 1.24012.9.0 uses the same job flags, and on a
    force-kill of Desktop all three watcher modes survived (silent breakaway keeps
    children out of the job; no tree kill ran). Desktop's graceful tray-Quit path is
    NOT measured.
    """
    windows = (os.name == "nt") if windows is None else windows
    script = os.path.abspath(__file__)
    if not windows:
        with open(str(log_path), "ab") as log:
            subprocess.Popen(  # noqa: S603 -- this interpreter and this file
                [sys.executable, script, *argv], stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                start_new_session=True, cwd=str(Path(str(log_path)).parent), env=_child_env(),
            )
        return
    for breakaway in (True, False):
        if not breakaway and _job_would_kill_an_unbroken_child():
            raise DetachUnsupported(
                "setup cannot detach on this host (its job closes on exit and allows no breakaway)"
            )
        flags = _DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP | (_CREATE_BREAKAWAY_FROM_JOB if breakaway else 0)
        relay_argv = [sys.executable, script, "_relay", *([] if breakaway else [_NO_BREAKAWAY]), *argv]
        try:
            relay = subprocess.Popen(  # noqa: S603 -- this interpreter and this file
                relay_argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=flags, env=_child_env(),
            )
            code = relay.wait(timeout=30)
        except subprocess.TimeoutExpired:
            relay.kill()
            with contextlib.suppress(Exception):
                relay.wait(timeout=5)
            raise OSError("the setup relay did not return within 30 s and was ended") from None
        except OSError as exc:
            if getattr(exc, "winerror", None) != _WINERROR_ACCESS_DENIED:
                raise
            code = _WINERROR_ACCESS_DENIED
        if code == 0:
            return
        if code != _WINERROR_ACCESS_DENIED:
            raise OSError(f"the setup relay exited {code}")
    raise DetachUnsupported("setup cannot detach on this host (no WMI broker yet)")


#: The relay's first argument when the double launch is made without breakaway.
_NO_BREAKAWAY = "--no-breakaway"


def _relay(argv: list[str]) -> int:
    """The Windows intermediate: start the run as a process whose parent is this one,
    then exit, so the run's parent pid is dead."""
    breakaway = argv[:1] != [_NO_BREAKAWAY]
    if not breakaway:
        argv = argv[1:]
    flags = _DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP | (_CREATE_BREAKAWAY_FROM_JOB if breakaway else 0)
    try:
        with open(str(setup_dir() / _LOG), "ab") as log:
            subprocess.Popen(  # noqa: S603 -- this interpreter and this file
                [sys.executable, os.path.abspath(__file__), *argv], stdin=subprocess.DEVNULL, stdout=log,
                stderr=subprocess.STDOUT, creationflags=flags,
            )
    except OSError as exc:
        return _WINERROR_ACCESS_DENIED if getattr(exc, "winerror", None) == _WINERROR_ACCESS_DENIED else 1
    except Exception:  # noqa: BLE001 -- the caller reads only the exit code
        return 1
    return 0


def status_view(setup: Path) -> dict[str, Any]:
    """What ``status`` prints: the status file plus what is true now. Reads only.

    ``state`` is one of :data:`ALL_STATES`. A status file that is missing, not
    JSON, not an object or holds a ``state`` outside :data:`STATES` reads as
    ``none``. ``running`` with no live lock reads as ``interrupted``. A
    ``status.error`` record (a run that could not write its status) overrides
    them with ``failed`` and the OS error text. Also ``live`` (a fresh lock),
    ``terminal``, ``not_now`` (the marker), ``next`` and ``log``.
    """
    data = read_status(setup) or {}
    if data.get("state") not in STATES:
        data = {}
    recorded = _read_json(setup / _ERROR_FILE)
    if recorded is not None and recorded.get("state") == FAILED:
        data = {**data, **recorded}
    lock = setup / _LOCK
    live = _exists(lock) and not _lock_stale(lock)
    state = data.get("state", NONE)
    if state == RUNNING and not live:
        state = INTERRUPTED
        data["next"] = f"Setup was interrupted. If the user agrees, call setup again with the same mode: it resumes. Log: {setup / _LOG}"
    data.setdefault("next", _NEXT_NONE)
    data.update(state=state, live=live, terminal=state in TERMINAL_STATES, not_now=(setup / _MARKER).exists())
    if setup.exists():
        data.setdefault("log", str(setup / _LOG))
    return data


def _error_view(message: str) -> dict[str, Any]:
    """What a command prints when it could not do its work."""
    return {
        "state": ERROR, "started": False, "live": False, "terminal": False, "not_now": False,
        "message": message, "next": "Nothing was started. Fix the problem the message names, then, if the user agrees, call setup again.",
    }


def start(mode: str, autostart: bool) -> dict[str, Any]:
    """The one way to start a run. Returns the status, with ``started``; an object
    whose ``state`` is ``error`` when the setup directory cannot be used."""
    if mode == MODE_CLOUD:  # refused before anything is read or written
        return {
            **status_view(setup_dir()), "started": False, "refused": _REFUSED_MODE, "next": _REFUSED_MODE_NEXT,
        }
    setup = setup_dir()
    try:
        return _start(setup, mode, autostart)
    except OSError as exc:
        return _error_view(f"setup could not start: {type(exc).__name__}: {exc}")


def _start(setup: Path, mode: str, autostart: bool) -> dict[str, Any]:
    os.makedirs(setup, mode=0o700, exist_ok=True)
    marker = setup / _MARKER
    if mode == MODE_NOT_NOW:
        marker.write_text(_now(), encoding="utf-8")
        return {**status_view(setup), "started": False}
    with contextlib.suppress(FileNotFoundError):
        marker.unlink()
    token = acquire_lock(setup, wait_for_finish=True)
    if token is None:
        live = status_view(setup)
        return {
            **live, "started": False, "requested_mode": mode,
            "refused": (
                f"A setup run is already in progress (mode {live.get('mode', 'unknown')}, stage "
                f"{live.get('stage', 'unknown')}). This request for mode {mode} started nothing."
            ),
        }
    try:
        _sweep_tmp(setup)
        with contextlib.suppress(FileNotFoundError):
            (setup / _ERROR_FILE).unlink()
        prior = read_status(setup) or {}
        log_path = setup / _LOG
        base: dict[str, Any] = {
            "state": RUNNING, "stage": "starting", "mode": mode, "autostart": autostart, "log": str(log_path),
            "started_at": _now(), "updated_at": _now(), "message": "setup is starting",
            "next": "Call setup_status to follow it.",
        }
        if "init" in prior:
            base["init"] = prior["init"]
        write_status(setup, base)
        argv = ["run", "--mode", mode, "--token", token] + (["--autostart"] if autostart else [])
        try:
            detach(argv, log_path)
        except DetachUnsupported as exc:
            base.update(state=UNSUPPORTED, message=str(exc), next=f"Install Nexus in a terminal. Log: {log_path}")
            write_status(setup, base)
            return {**status_view(setup), "started": False}
        except OSError as exc:
            base.update(
                state=FAILED, failed_step="start", message=f"the setup process could not be started: {exc}",
                next="If the user agrees, call setup again.",
            )
            write_status(setup, base)
            return {**status_view(setup), "started": False}
        token = ""  # the detached process owns the lock now
        return {**status_view(setup), "started": True}
    finally:
        if token:
            release_lock(setup, token)


def main(argv: list[str]) -> int:
    if argv[:1] == ["_relay"]:
        return _relay(argv[1:])
    import argparse  # noqa: PLC0415 -- only the command line needs it

    parser = argparse.ArgumentParser(prog="nx_setup.py", description="Install Nexus in the background.")
    sub = parser.add_subparsers(dest="command", required=True)
    start_p = sub.add_parser("start", help="start a run")
    start_p.add_argument("--mode", choices=MODES, required=True)
    start_p.add_argument("--autostart", action="store_true", help="register the login unit (local mode)")
    sub.add_parser("status", help="print the status")
    sub.add_parser(MODE_NOT_NOW, help="write the not-now marker")
    run_p = sub.add_parser("run", help=argparse.SUPPRESS)
    run_p.add_argument("--mode", choices=MODES, required=True)
    run_p.add_argument("--autostart", action="store_true")
    run_p.add_argument("--token", required=True)
    args = parser.parse_args(argv)
    if args.command == "run":
        try:
            return run_setup(args.mode, args.autostart, args.token)
        except Exception as exc:  # noqa: BLE001 -- the log is the only place this process can speak
            sys.stderr.write(f"setup: {type(exc).__name__}: {exc}\n")
            return 1
    try:
        if args.command == "status":
            out = status_view(setup_dir())
        elif args.command == MODE_NOT_NOW:
            out = start(MODE_NOT_NOW, False)
        else:
            out = start(args.mode, args.autostart)
    except Exception as exc:  # noqa: BLE001 -- every command prints one object, whatever happened
        out = _error_view(f"{type(exc).__name__}: {exc}")
    with contextlib.suppress(OSError):  # a caller that closed the pipe has stopped reading
        sys.stdout.write(json.dumps(out) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
