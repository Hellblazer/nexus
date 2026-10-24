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
off to the server directly (the pre-r433b behavior). The release's dependency
lock (below) is then NOT applied, and one left in ``pyproject.toml`` by an
earlier launch is removed: the unpinned fallback lives in the sync step, and
the handoff is an exec with no process left to retry, so a pin that cannot
resolve would end the start with no way back (nexus-puzdu).

Dependencies install under the release's lock (nexus-2k66t.4.6). The release
workflow stages ``src/nexus/_install/constraints.txt`` into the bundle as
``constraints.txt`` (``scripts/stage_mcpb_constraints.py``); before the sync
this file writes it into ``pyproject.toml`` as ``[tool.uv]
constraint-dependencies``, the one place a project ``uv sync`` reads
constraints from (it has no flag, and ignores ``UV_CONSTRAINT``). Without it
every transitive dependency resolves to whatever PyPI serves that day.
``NX_INSTALL_UNPINNED=1`` opts out, the same variable the CLI installers read
(Desktop does not inherit a shell's environment, so set it where the app reads
it: ``launchctl setenv`` on macOS, a user environment variable on Windows). The
extension's own ``install_unpinned`` setting (manifest ``user_config``) reaches
the launcher as ``NX_MCPB_INSTALL_UNPINNED=true``; ``1`` and ``true`` both count
on either name, anything else (``false``, an unsubstituted placeholder) does not. A missing or unreadable ``constraints.txt`` falls back
to the unpinned install and says so on stderr; a pin problem never fails the
start. The edited ``pyproject.toml`` is parsed (``tomllib``) before it is
written, and a result that does not parse is never written. If the sync fails
for any reason other than the release-propagation window while the pin is in
force, the pin is removed and the sync runs once more without it.

Deliberately conservative syntax: the launcher runs this on whatever Python
>=3.12 uv finds, which need not satisfy the bundle's own
``requires-python`` (that constraint governs the project venv ``uv sync``
creates, not this file). Standard library only, for the same reason: conexus
is not installed yet when this runs.
"""
from __future__ import annotations

import ntpath
import os
import re
import subprocess
import sys
import time
import tomllib
from pathlib import PureWindowsPath

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


def _is_pinned_release_unserved(output: str) -> bool:
    """While the release's pin names ``conexus==<version>``, the ONLY failure worth
    waiting out: that version is not served yet. Every other resolver failure
    names conexus too (it is the root dependency), so the broad class above would
    hold a pin-caused conflict (a pinned wheel this platform cannot take) on the
    ~15-minute schedule at every Desktop start (nexus-2k66t.4.6 confirmation)."""
    low = output.lower()
    return "conexus" in low and (
        "no version of conexus" in low or "not found in the package registry" in low
    )


# ── dependency lock (nexus-2k66t.4.6) ───────────────────────────────────────

_CONSTRAINTS_FILE = "constraints.txt"
_UNPINNED_ENV = "NX_INSTALL_UNPINNED"
# What the manifest's `install_unpinned` setting becomes. A separate name: the manifest's env
# overlay always sets it (a boolean substitutes "false"), and must not shadow a
# NX_INSTALL_UNPINNED the user set in the host environment.
_UNPINNED_ENV_MANIFEST = "NX_MCPB_INSTALL_UNPINNED"
_PIN_BEGIN = "# >>> nx-constraints: written by src/bootstrap.py from constraints.txt; do not edit"
_PIN_END = "# <<< nx-constraints"
_TOOL_UV_HEADER = re.compile(r"^\[tool\.uv\][ \t]*\r?$", re.MULTILINE)
# The END marker may close the file with no newline after it.
_PIN_BLOCK = re.compile(
    r"^" + re.escape(_PIN_BEGIN) + r"[^\n]*\n.*?^" + re.escape(_PIN_END) + r"[ \t]*(?:\r?\n|\Z)",
    re.MULTILINE | re.DOTALL,
)
# `name==version`, optionally `; marker`. The file is uv export output, so
# anything else means it is not the file we think it is.
_PIN_LINE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*==[A-Za-z0-9.!+*_-]+(?:\s*;\s*\S.*)?$")
_OPT_OUT_VALUES = ("1", "true")


def _toml_string(text):
    """A TOML basic string. JSON string syntax is a subset of it once ASCII is
    forced, so ``\\uXXXX`` escapes cover anything exotic in a marker."""
    import json  # noqa: PLC0415

    return json.dumps(text, ensure_ascii=True)


def _read_constraints(path):
    """The pin lines of *path*, or ``(None, reason)`` when it is unusable."""
    try:
        with open(path, "r", encoding="utf-8", newline=None) as handle:
            text = handle.read()
    except FileNotFoundError:
        return None, "no %s in the bundle" % _CONSTRAINTS_FILE
    except (OSError, UnicodeDecodeError) as exc:
        return None, "%s is unreadable (%s)" % (_CONSTRAINTS_FILE, type(exc).__name__)
    pins = []
    for raw in text.split("\n"):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if not _PIN_LINE.match(line):
            return None, "%s has a line that is not a pin" % _CONSTRAINTS_FILE
        pins.append(line)
    if not pins:
        return None, "%s holds no pins" % _CONSTRAINTS_FILE
    return pins, ""


def _pin_block(pins, newline):
    rows = ["    %s," % _toml_string(pin) for pin in pins]
    return newline.join([_PIN_BEGIN, "constraint-dependencies = ["] + rows + ["]", _PIN_END]) + newline


def _write_pyproject(path, text):
    """Replace *path* with *text* in one step, so a launcher killed mid-write
    leaves the old file or the new one, never half of either. The scratch name
    carries the pid (two Desktop launches can overlap) and is removed on every
    path."""
    tmp = "%s.nx-tmp.%d" % (path, os.getpid())
    try:
        with open(tmp, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _tool_uv(text):
    """``[tool.uv]`` of *text* as a dict, or ``None`` when the text is not TOML."""
    try:
        return tomllib.loads(text).get("tool", {}).get("uv", {})
    except tomllib.TOMLDecodeError:
        return None


def _check_edit(original, new, pins):
    """Why *new* may not replace *original*, or ``""`` when it may. The edit is
    only ever written when both texts parse and, if a pin was asked for, the
    result carries exactly the pins asked for: a hand-written
    ``constraint-dependencies``, a marker with no partner or a damaged file
    would otherwise give uv a pyproject it refuses outright."""
    if _tool_uv(original) is None:
        return "pyproject.toml does not parse as TOML"
    uv = _tool_uv(new)
    if uv is None:
        return "the edited pyproject.toml would not parse as TOML (a stray marker or an existing constraint-dependencies)"
    if pins is not None and len(uv.get("constraint-dependencies", ())) != len(pins):
        return "the edited pyproject.toml would not carry exactly the release's pins"
    return ""


def _apply_pin(bundle_dir, environ=None):
    """Make ``pyproject.toml`` reflect the install policy and return the one
    line to state on stderr. Never raises: whatever goes wrong, the bundle
    starts as it did before the lock existed.

    Pinned: the managed block (re)written from ``constraints.txt``.
    Opted out (``NX_INSTALL_UNPINNED=1``) or degraded (file missing, unreadable
    or not pins; pyproject without ``[tool.uv]``): the block removed, so a pin
    from an earlier install does not outlive the decision.
    An edit that would not parse is not written: the file stays as it was and
    the line says "not pinned" (and that an earlier constraint is still in
    force, when one is).
    """
    environ = os.environ if environ is None else environ
    pyproject = os.path.join(bundle_dir, "pyproject.toml")
    pins, reason = None, ""
    opted_out = False
    for name in (_UNPINNED_ENV, _UNPINNED_ENV_MANIFEST):
        value = str(environ.get(name, "")).strip()
        if value.lower() in _OPT_OUT_VALUES:
            opted_out, reason = True, "%s=%s is set" % (name, value)
            break
    if not opted_out:
        pins, reason = _read_constraints(os.path.join(bundle_dir, _CONSTRAINTS_FILE))
    final = None
    try:
        with open(pyproject, "r", encoding="utf-8", newline="") as handle:
            text = handle.read()
        newline = "\r\n" if "\r\n" in text else "\n"
        stripped = _PIN_BLOCK.sub("", text)
        if pins is None:
            new = stripped
        else:
            header = _TOOL_UV_HEADER.search(stripped)
            if header is None:
                pins, reason, new = None, "pyproject.toml has no [tool.uv] table to pin in", stripped
            else:
                end = stripped.find("\n", header.end())
                end = len(stripped) if end < 0 else end + 1
                head = stripped[:end] if stripped[:end].endswith("\n") else stripped[:end] + newline
                new = head + _pin_block(pins, newline) + stripped[end:]
        final = text
        if new != text:
            refusal = _check_edit(text, new, pins)
            if refusal:
                pins, reason = None, "not pinned: %s, so it was left as it was" % refusal
            else:
                _write_pyproject(pyproject, new)
                final = new
    except (OSError, UnicodeError) as exc:
        return (
            "[conexus-mcpb] WARNING: not pinned: could not update pyproject.toml (%s); dependencies "
            "resolve as the bundle ships them, unpinned." % type(exc).__name__
        )
    # Whatever the outcome, a constraint-dependencies the edit could not remove
    # is still in force: say so instead of claiming "unpinned".
    uv = _tool_uv(final)
    stale = (
        " An earlier constraint-dependencies in pyproject.toml is still in force."
        if pins is None and uv and "constraint-dependencies" in uv
        else ""
    )
    if pins is not None:
        return "[conexus-mcpb] dependencies pinned to the release's lock (%d constraints)." % len(pins)
    if opted_out:
        return "[conexus-mcpb] %s; dependencies resolve fresh from the package index, unpinned.%s" % (reason, stale)
    return (
        "[conexus-mcpb] WARNING: %s; dependencies resolve fresh from the package index, "
        "unpinned, so versions may differ from the release's lock.%s" % (reason, stale)
    )


def _pin_state(bundle_dir):
    """``(present, names_conexus)``: whether the managed block is in the bundle's
    pyproject, and whether it pins ``conexus`` itself."""
    try:
        with open(os.path.join(bundle_dir, "pyproject.toml"), "r", encoding="utf-8", newline="") as handle:
            text = handle.read()
    except (OSError, UnicodeError):
        return False, False
    block = _PIN_BLOCK.search(text)
    if block is None:
        return False, False
    return True, '"conexus==' in block.group(0)


def _strip_pin(bundle_dir):
    """Remove the managed block from the bundle's pyproject; ``True`` when it is
    gone. Same parse-before-write rule as ``_apply_pin``."""
    path = os.path.join(bundle_dir, "pyproject.toml")
    try:
        with open(path, "r", encoding="utf-8", newline="") as handle:
            text = handle.read()
        new = _PIN_BLOCK.sub("", text)
        if new == text or _check_edit(text, new, None):
            return False
        _write_pyproject(path, new)
        return True
    except (OSError, UnicodeError):
        return False


def _skip_pin(bundle_dir):
    """The line to state when ``NX_MCPB_SKIP_RESOLVE_RETRY`` skips the sync: the
    release's lock is not applied, and a block from an earlier launch is removed."""
    _strip_pin(bundle_dir)
    if _pin_state(bundle_dir)[0]:
        return (
            "[conexus-mcpb] NX_MCPB_SKIP_RESOLVE_RETRY is set, so the sync fallback is skipped; "
            "the pin from an earlier launch could not be removed and is still in force."
        )
    return (
        "[conexus-mcpb] NX_MCPB_SKIP_RESOLVE_RETRY is set, so the sync fallback is skipped and "
        "dependencies are not pinned to the release's lock."
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
    failure class. Raises SystemExit on terminal failure.

    While the release's pin is in force, a failure that is not the propagation
    class (or is, but the pin names no conexus version to wait for) removes the
    pin and syncs once without it: a lock that cannot resolve on this machine
    must not take the extension down (the start is then unpinned, and says so).
    """
    attempts = len(sleeps) + 1
    output = ""
    cmd = [uv, "sync", "--directory", bundle_dir]
    if _has_opencv_python(bundle_dir):
        cmd += ["--reinstall-package", "opencv-python-headless"]
    pinned, names_conexus = _pin_state(bundle_dir)
    i = 0
    while True:
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
        propagation = _is_resolution_unavailable(output)
        if pinned and not (names_conexus and _is_pinned_release_unserved(output)):
            pinned = False  # one unpinned attempt, whatever happens
            if _strip_pin(bundle_dir):
                sys.stderr.write(output)
                print(
                    "[conexus-mcpb] WARNING: the sync failed with the release's dependency lock "
                    "applied; removed the pins and syncing once more unpinned, so versions "
                    "may differ from the release's lock.",
                    file=sys.stderr,
                    flush=True,
                )
                continue
        if not propagation:
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
        i += 1
    print(
        "%s All retries exhausted — try again in a few minutes." % _PROPAGATION_MSG,
        file=sys.stderr,
        flush=True,
    )
    sys.stderr.write(output)
    raise SystemExit(1)


# ── executable resolution ───────────────────────────────────────────────────

_DEFAULT_PATHEXT = ".COM;.EXE;.BAT;.CMD"
_BATCH_SUFFIXES = frozenset({".bat", ".cmd"})


def _is_search_dir(directory, cwd):
    """True when a Windows ``PATH`` entry may honestly be searched: it has a drive
    and a root (so not ``tools``, ``.``, ``C:x`` or ``\\x``, which resolve against
    the working directory or its drive) and is not the working directory itself.

    On a real Windows host only a drive-and-root entry counts; ``os.path.isabs``
    also accepts a rooted ``\\x`` before Python 3.13. A Windows run injected on a
    POSIX host (the tests) also takes a POSIX absolute path, as its fixtures are
    real temporary directories.
    """
    if not directory:
        return False
    entry = PureWindowsPath(directory)
    absolute = entry.is_absolute() or (os.name != "nt" and os.path.isabs(directory))
    # normpath: an entry that spells the working directory with ``.`` or ``..`` parts is still it.
    return absolute and PureWindowsPath(ntpath.normpath(directory)) != cwd


def _resolve_executable(name, platform=None, path=None, pathext=None):
    """Resolve *name* to an executable file the way the platform's own launcher
    would, or return ``None`` when nothing on the search path matches.

    POSIX: a plain PATH walk for an executable file.

    Windows: PATHEXT, which ``os.execvp`` ignores. A bare ``uv`` resolves to
    ``uv.exe``; a name that already carries a PATHEXT extension is tried as
    written only. A name with a directory part is used as given (CreateProcess
    semantics) and never searched. The current directory is deliberately NOT
    searched, neither first like cmd.exe nor through a ``PATH`` entry that is the
    cwd or is relative (``tools``, ``.``, ``C:x``): a bundle launched with an
    arbitrary cwd must not pick up a planted ``uv.exe``. A ``.cmd`` or ``.bat`` hit
    is not returned, as in ``nexus.util.nx_argv``: a batch file runs through
    ``cmd.exe`` with quoting CPython does not escape for it, and uv installs as
    ``uv.exe``. A ``uv`` found only as a batch file counts as not found.

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
        # Already ends in a listed extension: tried as written only, as shutil.which
        # does (appending to it would find ``uv.exe.cmd``).
        exts = [""] if has_ext else listed
    names = [name + e for e in exts]
    if windows:
        names = [n for n in names if os.path.splitext(n)[1].lower() not in _BATCH_SUFFIXES]
    cwd = PureWindowsPath(ntpath.normpath(os.getcwd()))
    for directory in path.split(sep):
        if not directory or (windows and not _is_search_dir(directory, cwd)):
            continue
        for candidate_name in names:
            candidate = os.path.join(directory, candidate_name)
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
        _report_missing(argv[0])
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


def _report_missing(name):
    sys.stderr.write(
        "[conexus-mcpb] cannot find %r on PATH (PATHEXT honoured). Install uv "
        "(https://docs.astral.sh/uv/) and restart Claude Desktop.\n" % name
    )


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
        uv = _resolve_executable("uv", platform=platform)
        if uv is None:
            # Not the bare name: CreateProcess looks in the working directory before PATH, so
            # a "uv" spawned by name would run a uv.exe planted there. Same message and code
            # as the launch step gives; nothing has been spawned yet.
            _report_missing("uv")
            sys.exit(127)
    if os.environ.get("NX_MCPB_SKIP_RESOLVE_RETRY", "").strip().lower() in ("1", "true"):
        # No sync step means no unpinned fallback (the launch is an exec), so no pin.
        print(_skip_pin(bundle_dir), file=sys.stderr, flush=True)
    else:
        # Before the sync AND before the launch's own `uv run`, which resolves too.
        print(_apply_pin(bundle_dir), file=sys.stderr, flush=True)
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
