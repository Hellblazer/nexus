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
prints one JSON object on standard output and exits 0 unless its arguments are
wrong::

    nx_setup.py start --mode local|cloud|cli-only|not-now [--autostart]
    nx_setup.py status
    nx_setup.py not-now

``start`` returns at once with the status and ``"started": true`` when it began a
run. While a run is live it starts nothing and returns that run's status with
``"started": false``. ``not-now`` is ``start --mode not-now``.

The setup directory is ``setup/`` under the Nexus config directory
(``NEXUS_CONFIG_DIR``, else ``NX_CONFIG_DIR``, else ``~/.config/nexus``)::

    status.json   the status (JSON, owner-only, replaced whole)
    setup.log     what the run's child programs printed
    not-now       the "not now" marker
    lock/         the lock: a directory; ``owner`` holds the holder's token and
                  ``heartbeat`` is rewritten every 10 seconds
    lock.reclaim/ the reclaim guard (below), present for an instant

``${CLAUDE_PLUGIN_DATA}`` is not used.

**Modes.** ``not-now`` writes the marker and starts nothing. Any other call removes
the marker first. ``cli-only`` stops after the build. ``local`` goes on to the
service and verifies. ``cloud`` ends at once with a ``unsupported`` status: the
credentials page that saves a cloud pair is P2.5 (nexus-sv2wx.18). ``autostart``
is false unless given, so the login unit is never registered without an explicit
yes.

**Steps**, each skipped when already done, decided from facts and not from this
file's own history alone:

1. build: skipped when ``nx`` resolves (the launcher's lookup, ``_cli_lookup``: the
   generation layout, then ``PATH``). Else ``uv tool run --python 3.12 --from
   conexus==<plugin version> nx self install --bootstrap``. The version is the
   ``plugin.json`` beside this script. There is no fallback to the two documented
   commands: they run the cache prune and leave two copies. After the build ``nx``
   is found by the same lookup, layout first, so an application started before
   the install needs no restart for this process.
2. service, ``local``: if the status file recorded that a run began ``nx init``
   and did not finish it, run ``nx init`` again (a run killed inside ``nx init``
   leaves the service files on disk and no service answering, which alone reads
   as "provisioned and down"). Else when an endpoint resolves
   (``_endpoint_resolve``, the hooks' definition of a usable service) there is
   nothing to do. Else when the files ``nx init`` leaves are on disk
   (``pg_credentials``, ``postgres/PG_VERSION`` and the service binary) the
   service is provisioned and down: ``nx daemon service install --autostart``
   when ``autostart``, then ``nx daemon service start``. Else ``nx init --yes``
   with ``--no-autostart`` unless ``autostart``, and always ``--no-beads-prime``
   (``nx init`` otherwise installs a user-level beads PRIME.md).
3. verify: ``nx doctor``. Exit 0 is ``done``; anything else is ``doctor-failed``
   with the ``[✗]`` rows.

**Statuses.** :data:`STATES` and :data:`TERMINAL_STATES` are the whole list. A
status that is ``running`` while the lock is stale is reported by ``status`` as
``interrupted``; that value is computed and never written.

**Lock.** The caller that starts a run takes the lock, then starts the detached
process with the token; the process verifies the token and keeps the lock alive
with a heartbeat written on a thread of its own, because its work is child
programs that take minutes. The lock is removed on every way out the process can
catch: success, failure, an exception and SIGTERM. A kill it cannot catch
(SIGKILL, TerminateProcess) leaves the lock to go stale: sixty seconds without a
heartbeat. Liveness is the heartbeat, never a process id, because the standard
library has no reliable process test on Windows. ``os.mkdir`` is the atomic
claim. Reclaiming a stale lock takes a second ``mkdir``, of ``lock.reclaim``, and
re-reads the lock under it: the lock must still be stale and still carry the
owner token that was read stale. So of any number of callers that read the same
stale lock, one removes it and the rest report the run as live. A reclaimer that
dies holding the guard leaves it for sixty seconds, and after that the guard is
removed the same way; two callers racing for the removal of a dead guard at the
same instant remain a hole too narrow to close without a process test.

**The status file** is owner-only from the call that creates it. POSIX:
``os.open(..., O_CREAT | O_EXCL, 0o600)``, so the umask can only narrow it. Windows:
``CreateFileW`` with a ``SECURITY_ATTRIBUTES`` holding the protected one-ACE
descriptor ``D:P(A;;FA;;;<user SID>)`` (the descriptor ``nexus._winsec`` applies
to credential files; that module cannot be imported here, so its two ctypes calls
are repeated and the SID comes from ``_endpoint_resolve``). Updates go to a
temporary file created the same way and are renamed over ``status.json``, so no
instant exists in which the status is wider than owner-only or half written.

**The detach** is one function, :func:`detach`. POSIX: a new session, standard
input the null device, standard output and error the log, no inherited handles
(``close_fds``). Windows: a double launch with ``DETACHED_PROCESS |
CREATE_NEW_PROCESS_GROUP``, first with ``CREATE_BREAKAWAY_FROM_JOB`` and, if the
host's job refuses that (winerror 5), again without it. Measured on native Windows
for Claude Code 2.1.296 (nexus_rdr/228-research-32 and 228-research-33): the double
launch survives the tree kill Claude Code runs on the server; a plain child does
not. Claude Desktop 1.24012.9.0 puts servers in a job with the same flags and a
force-kill of Desktop left all three watcher modes alive (228-research-35); its
graceful tray-Quit path is NOT measured. If both launches are refused the status
says setup cannot detach on this host; a WMI broker is the untried way out.

Test seams, none set in normal use: ``NX_SETUP_HEARTBEAT_SECONDS`` (the 10 second
interval) and ``NX_SETUP_RECLAIM_PAUSE`` (seconds to wait between reading a stale
lock and removing it).
"""
from __future__ import annotations

import sys

sys.dont_write_bytecode = True  # before the sibling imports: no .pyc in the plugin directory

import argparse  # noqa: E402
import contextlib  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
import secrets  # noqa: E402
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
STATES: tuple[str, ...] = (RUNNING, *TERMINAL_STATES)

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
#: A child program that runs longer than this is killed and its step fails.
STEP_TIMEOUT_SECONDS = 3600.0

_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_BREAKAWAY_FROM_JOB = 0x01000000
_WINERROR_ACCESS_DENIED = 5

_STATUS = "status.json"
_LOG = "setup.log"
_MARKER = "not-now"
_LOCK = "lock"
_GUARD = "lock.reclaim"

_DOCTOR_ROWS_MAX = 20
_DOCTOR_ROW_CHARS = 300


class DetachUnsupported(Exception):
    """The host does not let a process start one that outlives it."""


class _Terminated(SystemExit):
    """SIGTERM reached the run: unwind so its clean-up runs."""


# -- paths -----------------------------------------------------------------------------------


def config_dir() -> Path:
    return _endpoint_resolve.default_config_dir()


def setup_dir() -> Path:
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


def write_status(setup: Path, data: dict[str, Any]) -> None:
    """Replace ``status.json`` with *data*, owner-only throughout."""
    payload = json.dumps(data, indent=2, sort_keys=True).encode("utf-8")
    tmp = setup / f"status.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    fd = _create_private(str(tmp))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
        _retry_sharing(lambda: os.replace(tmp, setup / _STATUS))
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def read_status(setup: Path) -> dict[str, Any] | None:
    try:
        text = _retry_sharing(lambda: (setup / _STATUS).read_text(encoding="utf-8"))
        data = json.loads(text)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


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
    except OSError:
        return None


def _write_heartbeat(lock: Path) -> None:
    (lock / "heartbeat").write_text(str(time.time()), encoding="utf-8")


def _make_lock(lock: Path, token: str) -> bool:
    try:
        os.mkdir(lock)
    except FileExistsError:
        return False
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
        if not lock.exists():
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
        if not path.exists():
            return True
        time.sleep(0.05)
    return not path.exists()


def acquire_lock(setup: Path, *, wait_for_finish: bool = False) -> str | None:
    """Take the lock; returns the holder's token, or ``None`` when a run is live.

    *wait_for_finish*: a fresh lock beside a terminal status is a run that is
    removing its lock, and the caller waits a moment for it instead of dropping
    its own request.
    """
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

    def run(self) -> None:
        while not self._stop_event.wait(self._interval):
            if _lock_owner(self._lock_dir) != self._token:
                self.lost.set()
                return
            try:
                _write_heartbeat(self._lock_dir)
            except OSError:
                self.lost.set()
                return

    def stop(self) -> None:
        self._stop_event.set()
        self.join(timeout=5)


# -- facts -------------------------------------------------------------------------------------


def _find_nx() -> str | None:
    return _cli_lookup.resolve("nx")


def _find_uv() -> str | None:
    """``uv`` on ``PATH`` alone: never the current directory, and on POSIX never a
    relative entry (the same rule as ``_cli_lookup.resolve``)."""
    if sys.platform == "win32":
        return _exec_path.which_off_cwd("uv")
    entries = [e for e in os.environ.get("PATH", "").split(os.pathsep) if os.path.isabs(e)]
    return _exec_path.which_off_cwd("uv", path=os.pathsep.join(entries))


def plugin_version() -> str | None:
    """The version in the ``plugin.json`` of the plugin this script is part of."""
    manifest = Path(__file__).resolve().parent.parent / ".claude-plugin" / "plugin.json"
    try:
        version = json.loads(manifest.read_text(encoding="utf-8")).get("version")
    except (OSError, ValueError, AttributeError):
        return None
    return version if isinstance(version, str) and re.fullmatch(r"[0-9][0-9A-Za-z.+!_-]*", version) else None


def _service_files_on_disk(config: Path) -> bool:
    """The files ``nx init`` leaves for a local service."""
    binary = "nexus-service.exe" if sys.platform == "win32" else "nexus-service"
    return (
        (config / "pg_credentials").is_file()
        and (config / "postgres" / "PG_VERSION").is_file()
        and (config / "service" / binary).is_file()
    )


def _endpoint_resolves(config: Path) -> bool:
    try:
        _endpoint_resolve.resolve_base_url(config)
    except Exception:  # noqa: BLE001 -- any failure means no usable service
        return False
    return True


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

    def update(self, **fields: Any) -> None:
        self.data.update(fields)
        self.data["updated_at"] = _now()
        write_status(self.setup, self.data)

    def at(self, stage: str, message: str = "") -> None:
        self.stage = stage
        self.update(stage=stage, message=message or f"running: {stage}", steps_done=list(self.steps_done))

    def step_done(self, step: str) -> None:
        self.steps_done.append(step)

    def end(self, state: str, message: str, next_step: str, **fields: Any) -> int:
        self.update(state=state, stage=self.stage, message=message, next=next_step, steps_done=list(self.steps_done), **fields)
        return 0 if state in (DONE, UNSUPPORTED) else 1

    def fail(self, step: str, message: str) -> int:
        return self.end(
            FAILED, message,
            f"Call setup again: it resumes at the {step} step. Log: {self.log_path}",
            failed_step=step,
        )

    def log(self, text: str) -> None:
        with open(self.log_path, "a", encoding="utf-8", errors="replace") as handle:
            handle.write(text)

    def cmd(self, argv: list[str]) -> tuple[int | None, str]:
        """Run *argv* with its output captured into the log; ``(None, text)`` on a
        timeout. The setup directory is the working directory: uv and the CLI must
        not read a project from wherever the application started this."""
        self.log(f"\n--- {_now()} {' '.join(argv)}\n")
        proc = subprocess.Popen(  # noqa: S603 -- argv[0] is an absolute path found by the lookups above
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=str(self.setup),
        )
        try:
            raw, _ = proc.communicate(timeout=STEP_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            proc.kill()
            raw, _ = proc.communicate()
            text = raw.decode("utf-8", errors="replace")
            self.log(text + f"--- timed out after {STEP_TIMEOUT_SECONDS:.0f} s\n")
            return None, text
        except BaseException:
            proc.kill()
            proc.wait()
            raise
        text = raw.decode("utf-8", errors="replace")
        self.log(text + f"--- exit {proc.returncode}\n")
        return proc.returncode, text

    def lost_lock(self) -> bool:
        return self.heartbeat.lost.is_set()


def _doctor_rows(text: str) -> list[str]:
    rows = [line.strip()[:_DOCTOR_ROW_CHARS] for line in text.splitlines() if "[✗]" in line]
    return rows[:_DOCTOR_ROWS_MAX]


def _execute(run: _Run) -> int:
    nx = _find_nx()
    run.at("build")
    if nx is None:
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
        nx = _find_nx()
        if nx is None:
            return run.fail("build", "the build finished but `nx` is not in the Nexus install directory or on PATH.")
    run.step_done("build")
    if run.lost_lock():
        return 1
    if run.mode == MODE_CLI_ONLY:
        return run.end(
            DONE, "The Nexus CLI is installed. No service is set up.",
            "Reconnect the nexus server or start a new session. To set up a service, call setup with mode local or cloud.",
        )

    config = config_dir()
    code = _service(run, nx, config)
    if code is not None:
        return code
    if run.lost_lock():
        return 1

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
        run.step_done("service")
        return None
    if not began_init and _service_files_on_disk(config):
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
    and no lock."""
    setup = setup_dir()
    lock = setup / _LOCK
    if _lock_owner(lock) != token:
        return 1

    def on_term(signum: int, frame: Any) -> None:
        raise _Terminated(1)

    for name in ("SIGTERM", "SIGHUP"):
        if hasattr(signal, name):
            with contextlib.suppress(OSError, ValueError):
                signal.signal(getattr(signal, name), on_term)
    heartbeat = _Heartbeat(lock, token)
    heartbeat.start()
    run = _Run(setup, mode, autostart, heartbeat)
    try:
        run.at("starting", "setup is starting")
        return _execute(run)
    except _Terminated:
        return run.fail(run.stage, "setup was ended by a signal.")
    except Exception as exc:  # noqa: BLE001 -- a terminal status must be written whatever went wrong
        return run.fail(run.stage, f"unexpected {type(exc).__name__}: {exc}")
    finally:
        heartbeat.stop()
        if not heartbeat.lost.is_set():
            release_lock(setup, token)


# -- starting a run ---------------------------------------------------------------------------


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
    same double launch is tried again without breakaway. Only when that is also
    refused is :class:`DetachUnsupported` raised. The intermediate's standard
    handles are the null device, so it inherits no pipe of the server.

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
                start_new_session=True, cwd=str(Path(str(log_path)).parent),
            )
        return
    for breakaway in (True, False):
        flags = _DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP | (_CREATE_BREAKAWAY_FROM_JOB if breakaway else 0)
        relay_argv = [sys.executable, script, "_relay", *([] if breakaway else [_NO_BREAKAWAY]), *argv]
        try:
            relay = subprocess.Popen(  # noqa: S603 -- this interpreter and this file
                relay_argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=flags,
            )
            code = relay.wait(timeout=30)
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
    return 0


_NEXT_NONE = "Call setup with a mode (local, cloud, cli-only or not-now)."


def status_view(setup: Path) -> dict[str, Any]:
    """What ``status`` prints: the status file plus what is true now. Reads only."""
    data = read_status(setup) or {}
    lock = setup / _LOCK
    live = lock.exists() and not _lock_stale(lock)
    state = data.get("state", "none")
    if state == RUNNING and not live:
        state = "interrupted"
        data["next"] = f"Setup was interrupted. Call setup again: it resumes. Log: {setup / _LOG}"
    data.setdefault("next", _NEXT_NONE)
    data.update(state=state, live=live, terminal=state in TERMINAL_STATES, not_now=(setup / _MARKER).exists())
    if setup.exists():
        data.setdefault("log", str(setup / _LOG))
    return data


def start(mode: str, autostart: bool) -> dict[str, Any]:
    """The one way to start a run. Returns the status, with ``started``."""
    setup = setup_dir()
    os.makedirs(setup, mode=0o700, exist_ok=True)
    marker = setup / _MARKER
    if mode == MODE_NOT_NOW:
        marker.write_text(_now(), encoding="utf-8")
        return {**status_view(setup), "started": False}
    with contextlib.suppress(FileNotFoundError):
        marker.unlink()
    token = acquire_lock(setup, wait_for_finish=True)
    if token is None:
        return {**status_view(setup), "started": False}
    prior = read_status(setup) or {}
    log_path = setup / _LOG
    base: dict[str, Any] = {
        "state": RUNNING, "stage": "starting", "mode": mode, "autostart": autostart, "log": str(log_path),
        "started_at": _now(), "updated_at": _now(), "message": "setup is starting",
        "next": "Call setup_status to follow it.",
    }
    if "init" in prior:
        base["init"] = prior["init"]
    try:
        if mode == MODE_CLOUD:
            base.update(
                state=UNSUPPORTED, stage="cloud",
                message="This build of the plugin does not have the credentials page that a cloud setup needs.",
                next="Connect a managed service in a terminal with `nx config init`, or call setup with mode local or cli-only.",
            )
            write_status(setup, base)
            return {**status_view(setup), "started": False}
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
                next="Call setup again.",
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
        return run_setup(args.mode, args.autostart, args.token)
    if args.command == "status":
        out = status_view(setup_dir())
    elif args.command == MODE_NOT_NOW:
        out = start(MODE_NOT_NOW, False)
    else:
        out = start(args.mode, args.autostart)
    sys.stdout.write(json.dumps(out) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
