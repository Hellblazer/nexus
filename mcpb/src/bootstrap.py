#!/usr/bin/env python3
"""Resolve-with-retry bootstrap for the Claude Desktop .mcpb bundle (nexus-r433b).

Claude Desktop launches this file via ``uv tool run ... --python >=3.12
python`` (see manifest.json's mcp_config), outside the bundle's project. The
launcher is a tool run, not ``uv run --no-project``, because ``uv run`` searches
the bundle directory and every parent for a ``.venv`` and runs the first one: a
``~/.venv`` or a ``C:\\.venv`` above the bundle supplied the interpreter
(nexus-92gxf). A tool environment is built from a managed or PATH interpreter
only. The bundle's real dependency resolution — the
step that pulls ``conexus[local]>=X.Y.Z`` from PyPI — used to happen inside
the ``uv run src/server.py`` invocation itself, which meant a resolver
failure killed the extension before any of our code ran. PyPI's simple
index lags the upload by ~10-25 minutes after every release (measured on
four consecutive releases), so a Desktop install or update inside that
window died with a bare "no matching version" resolver error.

This bootstrap runs ``uv sync`` explicitly, retries the
propagation-window failure class with bounded backoff (naming the cause on
stderr each time), and then hands off to ``uv run src/server.py`` — the real
MCP entry point — with stdio passing straight through to Claude Desktop. On
POSIX the handoff is ``os.execvp``. Windows has no exec (``os.execvp`` there
spawns a child and exits the parent, so the host sees its server exit), so
there the server runs as a child that inherits this process's own stdin,
stdout and stderr handles; this process waits on it, exits with its exit
code, and a Job Object (kill-on-close) ties the child's lifetime to this
process. Any ``uv sync`` failure OUTSIDE the retry class (network down,
permissions, a genuinely missing package) fails immediately with uv's own
output: behavior unchanged from before this file existed.

Set ``NX_MCPB_SKIP_RESOLVE_RETRY=1`` to skip the sync-with-retry and hand
off to the server directly (the pre-r433b behavior).

Deliberately conservative syntax: the launcher runs this on whatever Python
>=3.12 uv finds, which need not satisfy the bundle's own
``requires-python`` (that constraint governs the project venv ``uv sync``
creates, not this file). Standard library only, for the same reason: conexus
is not installed yet when this runs.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

_WINDOWS = "win32"

# Backoff spans ~15 min after the first failure — sized against the measured
# 10-25 min propagation window (the user typically lands mid-window, so the
# remaining lag is shorter than the full window).
_RETRY_SLEEPS = (60, 120, 240, 480)

_PROPAGATION_MSG = (
    "[conexus-mcpb] PyPI has not finished propagating the pinned conexus "
    "version to its download index yet (this lags a new release by ~10-25 "
    "minutes)."
)


def _bundle_dir() -> str:
    """The mcpb/ bundle root: parent of the src/ directory holding this file."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _is_resolution_unavailable(output: str) -> bool:
    """True when uv's failure output is the version-not-yet-served resolver
    class (the PyPI propagation window), as opposed to any other failure.

    Deliberately broad within that class: a genuine (non-propagation)
    resolver conflict that mentions conexus also matches and rides the
    ~15-min retry schedule before surfacing — bounded, and preferred over
    a narrower match that misses a real propagation shape and kills the
    extension with a bare resolver error. Kept in parity with the shell
    grep in tests/e2e/fresh-install-mvv.sh's retry branch (pinned by
    test_retry_signature_parity_with_mcpb_bootstrap).
    """
    low = output.lower()
    if "conexus" not in low:
        return False
    return (
        "no solution found" in low
        or "no version of conexus" in low
        or "not found in the package registry" in low
    )


def _has_opencv_python(bundle_dir):
    """True when the bundle venv still carries opencv-python's metadata.

    Both OpenCV dists write ``cv2/``. The bundle now overrides opencv-python
    out and keeps opencv-python-headless; uninstalling opencv-python from a
    venv that had both deletes ``cv2/`` and leaves headless installed with no
    files, so the sync that removes it must also reinstall headless.
    """
    venv = os.path.join(bundle_dir, ".venv")
    candidates = [os.path.join(venv, "Lib", "site-packages")]
    lib = os.path.join(venv, "lib")
    if os.path.isdir(lib):
        candidates += [os.path.join(lib, d, "site-packages") for d in os.listdir(lib)]
    for site in candidates:
        if os.path.isdir(site) and any(
            n.startswith("opencv_python-") and n.endswith(".dist-info") for n in os.listdir(site)
        ):
            return True
    return False


def _sync_with_retry(bundle_dir, run=subprocess.run, sleep=time.sleep, sleeps=_RETRY_SLEEPS, uv="uv"):
    """``uv sync`` the bundle env, retrying only the propagation-window
    failure class. Raises SystemExit on terminal failure."""
    attempts = len(sleeps) + 1
    output = ""
    cmd = [uv, "sync", "--directory", bundle_dir]
    if _has_opencv_python(bundle_dir):
        cmd += ["--reinstall-package", "opencv-python-headless"]
    for i in range(attempts):
        proc = run(
            cmd,
            capture_output=True,
            # uv must not read the host's protocol stdin (the MCP stream).
            stdin=subprocess.DEVNULL,
            # Not text=True: that decodes with the Windows locale codepage, so
            # an odd byte in uv's output would turn a retryable resolver
            # failure into a UnicodeDecodeError traceback.
            encoding="utf-8",
            errors="replace",
        )
        if proc.returncode == 0:
            return
        output = (proc.stdout or "") + (proc.stderr or "")
        if not _is_resolution_unavailable(output):
            sys.stderr.write(output)
            raise SystemExit(proc.returncode or 1)
        if i == attempts - 1:
            break
        wait = sleeps[i]
        print(
            "%s Retrying in %ds (attempt %d/%d)." % (_PROPAGATION_MSG, wait, i + 1, len(sleeps)),
            file=sys.stderr,
            flush=True,
        )
        sleep(wait)
    print(
        "%s All retries exhausted — try again in a few minutes." % _PROPAGATION_MSG,
        file=sys.stderr,
        flush=True,
    )
    sys.stderr.write(output)
    raise SystemExit(1)


# ── executable resolution ───────────────────────────────────────────────────

_DEFAULT_PATHEXT = ".COM;.EXE;.BAT;.CMD"


def _resolve_executable(name, platform=None, path=None, pathext=None):
    """Resolve *name* to an executable file the way the platform's own launcher
    would, or return ``None`` when nothing on the search path matches.

    POSIX: a plain PATH walk for an executable file.

    Windows: PATHEXT, which ``os.execvp`` ignores. A bare ``uv`` resolves to
    ``uv.exe``; a name that already carries a PATHEXT extension is tried as
    written first. A name with a directory part is used as given (CreateProcess
    semantics) and never searched. The current directory is deliberately NOT
    searched first, unlike cmd.exe: a bundle launched with an arbitrary cwd
    must not pick up a planted ``uv.exe``.

    ``platform``/``path``/``pathext`` are injectable so both branches run on
    any host. Not ``shutil.which``: that decides Windows-ness from the real
    ``sys.platform``, so it cannot be driven from a test on another OS.
    """
    platform = sys.platform if platform is None else platform
    windows = platform == _WINDOWS
    sep = ";" if windows else ":"
    if path is None:
        path = os.environ.get("PATH", "")
    if os.path.dirname(name) or (windows and "/" in name):
        return name if os.path.isfile(name) else None
    exts = [""]
    if windows:
        if pathext is None:
            pathext = os.environ.get("PATHEXT") or _DEFAULT_PATHEXT
        listed = [e for e in pathext.split(";") if e]
        has_ext = os.path.splitext(name)[1].upper() in [e.upper() for e in listed]
        exts = [""] + listed if has_ext else listed
    for directory in path.split(sep):
        if not directory:
            continue
        for ext in exts:
            candidate = os.path.join(directory, name + ext)
            if os.path.isfile(candidate) and (windows or os.access(candidate, os.X_OK)):
                return candidate
    return None


# ── Windows Job Object (kill-on-close) ──────────────────────────────────────
#
# Standard library only: this file runs under the tool-run launcher BEFORE
# conexus is installed, so it cannot import nexus.util.win_job. The constants
# and struct layout below duplicate that module's; tests/test_mcpb_bootstrap.py
# pins the two against drift.

_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
# A child spawned with CREATE_BREAKAWAY_FROM_JOB may leave the job. Everything
# else still joins it: the MCP server tree keeps the kill-on-close guarantee,
# and only a daemon that must outlive the extension (the aspect worker, spawned
# by nexus.daemon.aspect_worker_daemon) asks to leave.
_JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x00000800
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001


def _job_struct():
    """``JOBOBJECT_EXTENDED_LIMIT_INFORMATION``, built lazily so a POSIX run
    never imports ctypes.wintypes."""
    import ctypes  # noqa: PLC0415
    import ctypes.wintypes as wintypes  # noqa: PLC0415

    class IoCounters(ctypes.Structure):
        _fields_ = (
            ("ReadOperationCount", ctypes.c_uint64),
            ("WriteOperationCount", ctypes.c_uint64),
            ("OtherOperationCount", ctypes.c_uint64),
            ("ReadTransferCount", ctypes.c_uint64),
            ("WriteTransferCount", ctypes.c_uint64),
            ("OtherTransferCount", ctypes.c_uint64),
        )

    class BasicLimit(ctypes.Structure):
        _fields_ = (
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        )

    class ExtendedLimit(ctypes.Structure):
        _fields_ = (
            ("BasicLimitInformation", BasicLimit),
            ("IoInfo", IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        )

    return ExtendedLimit


def _load_kernel32():
    import ctypes  # noqa: PLC0415
    import ctypes.wintypes as wintypes  # noqa: PLC0415

    dll = ctypes.WinDLL("kernel32", use_last_error=True)
    dll.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    dll.CreateJobObjectW.restype = wintypes.HANDLE
    dll.SetInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
    ]
    dll.SetInformationJobObject.restype = wintypes.BOOL
    dll.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    dll.OpenProcess.restype = wintypes.HANDLE
    dll.GetCurrentProcess.argtypes = []
    dll.GetCurrentProcess.restype = wintypes.HANDLE
    dll.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    dll.AssignProcessToJobObject.restype = wintypes.BOOL
    dll.CloseHandle.argtypes = [wintypes.HANDLE]
    dll.CloseHandle.restype = wintypes.BOOL
    return dll


class _KillOnCloseJob(object):
    """A Job Object whose last handle closing terminates every member.

    The handle is held for the life of this process and never closed
    explicitly: when the host kills the bootstrap, or the bootstrap exits, the
    OS closes the handle and takes every process still in the job with it.
    """

    def __init__(self, kernel32=None):
        import ctypes  # noqa: PLC0415

        self._k = kernel32 if kernel32 is not None else _load_kernel32()
        handle = self._k.CreateJobObjectW(None, None)
        if not handle:
            raise OSError("CreateJobObjectW failed")
        info = _job_struct()()
        info.BasicLimitInformation.LimitFlags = (
            _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | _JOB_OBJECT_LIMIT_BREAKAWAY_OK
        )
        # ctypes.pointer, not byref: a test double can read .contents back.
        ok = self._k.SetInformationJobObject(
            handle,
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.pointer(info),
            ctypes.sizeof(info),
        )
        if not ok:
            self._k.CloseHandle(handle)
            raise OSError("SetInformationJobObject failed")
        self.handle = handle

    def assign_self(self):
        """Put THIS process in the job, so every child spawned afterwards joins
        it with no spawn-to-assign race. False when refused."""
        return bool(self._k.AssignProcessToJobObject(self.handle, self._k.GetCurrentProcess()))

    def assign_pid(self, pid):
        """Put process *pid* in the job. False when it cannot be opened or the
        assignment is refused."""
        hproc = self._k.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid)
        if not hproc:
            return False
        try:
            return bool(self._k.AssignProcessToJobObject(self.handle, hproc))
        finally:
            self._k.CloseHandle(hproc)


def _make_job():
    """A kill-on-close job on real Windows; ``None`` anywhere else or when the
    OS refuses (the caller names the degraded containment on stderr)."""
    if sys.platform != _WINDOWS:
        return None
    try:
        return _KillOnCloseJob()
    except (OSError, AttributeError):
        return None


# ── handoff to the server ───────────────────────────────────────────────────


def _launch_windows(argv, popen=None, make_job=None, resolve=None):
    """Run *argv* as a child inheriting THIS process's stdin/stdout/stderr and
    return its exit code.

    Windows has no exec. The MCP server must read and write the very pipe
    handles Claude Desktop gave this process, so the child gets fds 0/1/2
    explicitly (``STARTF_USESTDHANDLES`` with those handles) rather than the
    implicit inheritance a bare ``Popen()`` relies on. Never PIPE: a relay
    here would sit between host and server and stall the handshake on
    buffering.

    Lifetime: the job holds this process too, so the child (and what it
    spawns) dies when this process does, however it dies. If the host's own
    job refuses the self-assignment, the child alone is assigned after spawn.
    """
    popen = subprocess.Popen if popen is None else popen
    make_job = _make_job if make_job is None else make_job
    if resolve is None:
        # This IS the Windows launcher, so resolve with Windows rules whatever
        # host runs it (tests drive it from POSIX).
        def resolve(name):
            return _resolve_executable(name, platform=_WINDOWS)
    exe = resolve(argv[0])
    if exe is None:
        sys.stderr.write(
            "[conexus-mcpb] cannot find %r on PATH (PATHEXT honoured). Install uv "
            "(https://docs.astral.sh/uv/) and restart Claude Desktop.\n" % argv[0]
        )
        return 127
    job = make_job()
    contained_self = job.assign_self() if job is not None else False
    child = popen([exe] + list(argv[1:]), stdin=0, stdout=1, stderr=2)
    if job is not None:
        if not contained_self and not job.assign_pid(child.pid):
            sys.stderr.write(
                "[conexus-mcpb] warning: could not tie the server process to this "
                "launcher; it may outlive it if the host kills the launcher.\n"
            )
    elif sys.platform == _WINDOWS:
        sys.stderr.write(
            "[conexus-mcpb] warning: no Job Object; the server process may outlive "
            "this launcher if the host kills it.\n"
        )
    try:
        return child.wait()
    except KeyboardInterrupt:
        child.terminate()
        return child.wait()


def _launch(argv, platform=None, execvp=None, launch_windows=None):
    """Hand off to *argv*: exec on POSIX (stdio lands on the server process
    itself), child-with-inherited-handles on Windows. Returns the exit code on
    Windows; on POSIX it does not return."""
    platform = sys.platform if platform is None else platform
    if platform == _WINDOWS:
        return (launch_windows or _launch_windows)(argv)
    (execvp or os.execvp)(argv[0], argv)
    return None


def main(platform=None) -> None:
    platform = sys.platform if platform is None else platform
    bundle_dir = _bundle_dir()
    uv = "uv"
    if platform == _WINDOWS:
        # PATHEXT: a bare "uv" is uv.exe. Sync and launch both resolve it.
        uv = _resolve_executable("uv", platform=platform) or "uv"
    if not os.environ.get("NX_MCPB_SKIP_RESOLVE_RETRY"):
        if platform == _WINDOWS:
            _sync_with_retry(bundle_dir, uv=uv)
        else:
            _sync_with_retry(bundle_dir)
    # POSIX: exec, so Claude Desktop's stdio pipes land on the server process
    # itself for the MCP handshake. Windows: see _launch_windows.
    code = _launch(["uv", "run", "--directory", bundle_dir, "src/server.py"], platform=platform)
    if code is not None:
        sys.exit(code)


if __name__ == "__main__":
    main()
