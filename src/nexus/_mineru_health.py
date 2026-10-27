# SPDX-License-Identifier: AGPL-3.0-or-later
"""Honest MinerU server health (nexus-x3m8e).

``/health`` answers 200 from a server that cannot complete a single task: one
whose working directory was deleted (any ``getcwd``/``abspath``/``Path.cwd``
in it or its libraries raises ``FileNotFoundError``; a MinerU task died in
``torch/_dynamo/config.py`` that way), whose interpreter or output directory
was removed under it, or on a machine with no POSIX semaphores left (every
``ProcessPoolExecutor`` raises ``ENOSPC``). ``nx mineru status`` and the
extractor's pre-dispatch check read the faults below instead of trusting the
200. Library layer: no click, no ``nexus.commands`` import.
"""
from __future__ import annotations

import errno
import os
import re
import secrets
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import structlog

_log = structlog.get_logger(__name__)

#: A server counts as "every task fails" only with at least this many failed
#: tasks in its retention window (``MINERU_API_TASK_RETENTION_SECONDS``, 300 s,
#: so the counts are recent) AND a failure share of at least
#: :data:`ALL_TASKS_FAILING_MIN_SHARE`. Three failures with none completed
#: does count: a server that has finished nothing after three tries is the
#: broken-server pattern (every task failed in the deleted-cwd incident). The
#: share keeps a working server with a few bad PDFs healthy.
ALL_TASKS_FAILING_MIN_FAILED: int = 3
ALL_TASKS_FAILING_MIN_SHARE: float = 0.9

SEMAPHORE_REMEDY: str = (
    "No POSIX named semaphores are left on this machine "
    "(`kern.posix.sem.max` on macOS), so any multiprocessing Lock or "
    "ProcessPoolExecutor fails with ENOSPC. The leaked names cannot be "
    "listed or unlinked from userspace on macOS: reboot, or raise the limit "
    "(`sudo sysctl -w kern.posix.sem.max=<larger>`), then `nx mineru stop && nx mineru start`."
)

SEMAPHORE_REMEDY_LINUX: str = (
    "No POSIX named semaphore can be created on this machine: /dev/shm is "
    "full or out of inodes, so any multiprocessing Lock or ProcessPoolExecutor "
    "fails with ENOSPC. Unlike macOS the leaked names are plain files you can "
    "list and remove: `df -h /dev/shm; ls /dev/shm | head`. Remove `sem.*` "
    "files no running process holds open (`lsof /dev/shm/sem.*`), or enlarge "
    "/dev/shm, then `nx mineru stop && nx mineru start`."
)


def semaphore_remedy(platform: str | None = None) -> str:
    """The remedy text for exhausted POSIX named semaphores on *platform*
    (default ``sys.platform``): the Linux limit is /dev/shm, a directory the
    operator can list; macOS has a fixed kernel table with no way to list it."""
    return SEMAPHORE_REMEDY_LINUX if (platform if platform is not None else sys.platform).startswith("linux") else SEMAPHORE_REMEDY


class MineruMachineResourceError(Exception):
    """The machine, not the PDF, is why MinerU cannot run (POSIX semaphores
    exhausted). Deliberately NOT a ``RuntimeError``: the extractor's per-range
    bisect ladder catches ``RuntimeError`` and would retry every half of a
    failure that no page split can fix."""


_FILE_NOT_FOUND = re.compile(r"FileNotFoundError|No such file or directory", re.I)
_ENOSPC = re.compile(r"\[Errno 28\]|No space left on device|ENOSPC", re.I)


def is_server_file_not_found(error_text: str) -> bool:
    """Whether a server-reported task error is the missing-file/cwd fault."""
    return bool(_FILE_NOT_FOUND.search(error_text or ""))


def looks_like_enospc(error_text: str) -> bool:
    """Whether a server-reported task error is ``ENOSPC``. That is ALSO what a
    full disk says, so a hit is only a reason to run :func:`semaphores_exhausted`."""
    return bool(_ENOSPC.search(error_text or ""))


def _lsof_cwd_output(pid: int) -> str:
    """``lsof -F`` output naming *pid*'s cwd: its device (``D``), inode (``i``)
    and name (``n``) fields (macOS and other non-Linux POSIX)."""
    from nexus.bounded_subprocess import run_bounded  # noqa: PLC0415 — deferred: only the non-Linux POSIX branch spawns

    return run_bounded(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-FnDi"], timeout=5).stdout


def _cwd_fields(out: str) -> tuple[str | None, int | None, int | None]:
    """``(path, device, inode)`` from ``lsof -FnDi`` output; each ``None`` when absent."""
    path: str | None = None
    device: int | None = None
    inode: int | None = None
    for line in out.splitlines():
        if line.startswith("n") and path is None:
            path = line[1:]
        elif line.startswith("D") and device is None:
            try:
                device = int(line[1:], 0)
            except ValueError:
                pass
        elif line.startswith("i") and inode is None:
            try:
                inode = int(line[1:])
            except ValueError:
                pass
    return path, device, inode


def inspect_process_cwd(
    pid: int,
    *,
    _lsof: Callable[[int], str] | None = None,
    _platform: str | None = None,
    _readlink: Callable[[str], str] | None = None,
) -> tuple[str | None, str | None]:
    """``(path, state)`` for process *pid*'s working directory. *state* is
    ``"gone"`` when the directory no longer exists, ``"ok"`` when it does,
    ``None`` when it cannot be determined (no such process, no ``lsof``,
    Windows); *path* is the directory the process was started in, or ``None``.

    The path alone cannot say: on macOS ``lsof`` keeps naming the OLD path of a
    deleted directory, and a path deleted and recreated (a worktree removed and
    made again, or a config directory a later step recreates) is ``isdir`` yet
    NOT the directory the process holds, so ``getcwd`` still raises in it. The
    process's device and inode are compared with ``os.stat(path)``; a
    different inode is a different directory. Linux reads ``/proc/<pid>/cwd``,
    whose target the kernel suffixes `` (deleted)`` whether or not the path was
    recreated."""
    platform = _platform if _platform is not None else sys.platform
    if platform == "win32":
        return None, None
    if platform.startswith("linux"):
        try:
            cwd = (_readlink if _readlink is not None else os.readlink)(f"/proc/{pid}/cwd")
        except OSError:
            return None, None
        if cwd.endswith(" (deleted)"):
            return cwd[: -len(" (deleted)")], "gone"
        return cwd or None, ("ok" if cwd and os.path.isdir(cwd) else ("gone" if cwd else None))
    try:
        out = (_lsof if _lsof is not None else _lsof_cwd_output)(pid)
    except (OSError, subprocess.SubprocessError):
        return None, None
    cwd, device, inode = _cwd_fields(out)
    if not cwd:
        return None, None
    try:
        st = os.stat(cwd)
    except OSError:
        return cwd, "gone"
    if not os.path.isdir(cwd):
        return cwd, "gone"
    if inode is not None and st.st_ino != inode:
        return cwd, "gone"
    if device is not None and st.st_dev != device:
        return cwd, "gone"
    return cwd, "ok"


def process_cwd_state(
    pid: int,
    *,
    _lsof: Callable[[int], str] | None = None,
    _platform: str | None = None,
) -> str | None:
    """``"gone"`` when process *pid*'s working directory no longer exists,
    ``"ok"`` when it does, ``None`` when it cannot be determined. See
    :func:`inspect_process_cwd`."""
    return inspect_process_cwd(pid, _lsof=_lsof, _platform=_platform)[1]


def process_cwd_path(pid: int) -> str | None:
    """The directory process *pid* was started in, or ``None``. See
    :func:`inspect_process_cwd`."""
    return inspect_process_cwd(pid)[0]


def tasks_all_failing(health: dict[str, Any]) -> bool:
    """Whether a ``/health`` body shows a server that only fails: at least
    :data:`ALL_TASKS_FAILING_MIN_FAILED` failed tasks in the retention window
    and at least :data:`ALL_TASKS_FAILING_MIN_SHARE` of its finished tasks
    failed. A server that completed 20 and failed 3 is a server with some bad
    PDFs, not one that cannot work."""
    try:
        failed = int(health.get("failed_tasks", 0) or 0)
        completed = int(health.get("completed_tasks", 0) or 0)
    except (TypeError, ValueError):
        return False
    if failed < ALL_TASKS_FAILING_MIN_FAILED:
        return False
    return failed >= ALL_TASKS_FAILING_MIN_SHARE * (failed + completed)


def _create_probe_semaphore() -> object:
    """Create ONE throwaway named semaphore and unlink it in the same call.

    ``unlink=True`` makes ``SemLock`` ``sem_unlink`` the name straight away and
    leaves its ``name`` ``None``, so nothing registers with a
    ``multiprocessing`` resource tracker and no tracker process is started: the
    probe can neither leak the semaphore nor spawn anything. The name is short
    (macOS allows 31 characters, slash included) and unique per call."""
    from _multiprocessing import SemLock  # type: ignore[attr-defined]  # noqa: PLC0415 — deferred: only the probe needs it

    return SemLock(0, 0, 1, f"/nxp-{os.getpid():x}-{secrets.token_hex(3)}", True)


def probe_semaphore(*, _create: Callable[[], object] | None = None) -> OSError | None:
    """``None`` when a POSIX named semaphore can be created (or this build has
    no ``_multiprocessing.SemLock`` to ask), else the ``OSError`` the kernel
    raised. The ONE probe: ``nx doctor --check-resources`` and
    :func:`semaphores_exhausted` both read it, each applying its own policy to
    the error."""
    try:
        (_create if _create is not None else _create_probe_semaphore)()
    except ImportError:
        return None
    except OSError as exc:
        return exc
    return None


def semaphores_exhausted(
    *,
    _create: Callable[[], object] | None = None,
    _platform: str | None = None,
) -> str | None:
    """The machine-level message when a POSIX named semaphore cannot be created
    (``ENOSPC``), else ``None``. Creates ONE semaphore and drops it. Windows has
    none to exhaust. Any other ``OSError`` is not this fault and reads as
    ``None``: the probe never invents a diagnosis."""
    platform = _platform if _platform is not None else sys.platform
    if platform == "win32":
        return None
    try:
        exc = probe_semaphore(_create=_create)
    except ValueError:
        return None  # a seam that cannot build its context: nothing to probe with
    if exc is None:
        return None
    if exc.errno == errno.ENOSPC:
        return semaphore_remedy(platform)
    _log.debug("mineru_semaphore_probe_other_error", error=str(exc))
    return None


def _missing_paths(info: dict[str, Any] | None) -> list[str]:
    """The paths the server was started with (its interpreter, its executable
    and its output root, as the pid file records them) that no longer exist."""
    if not info:
        return []
    out: list[str] = []
    for key, label in (
        ("python", "interpreter"), ("executable", "mineru-api executable"),
        ("output_root", "output directory"),
    ):
        value = info.get(key)
        if isinstance(value, str) and value and not os.path.exists(value):
            out.append(f"{label} {value}")
    return out


def diagnose_server(
    pid: int,
    health: dict[str, Any],
    *,
    info: dict[str, Any] | None = None,
    _cwd_state: Callable[[int], str | None] | None = None,
    _semaphores: Callable[[], str | None] | None = None,
) -> list[str]:
    """Every reason a server answering ``/health`` 200 is not actually usable.
    Empty means no fault found. Order: the cause before its symptom.

    *info* is the server's pid file (``read_pid_file()``), which records the
    interpreter, executable and output directory it was started with; any of
    them gone is a fault (a tool environment replaced under a running server
    takes its libraries with it)."""
    # Resolved at call time so a test fixture can patch the module-level probes.
    cwd_state = _cwd_state if _cwd_state is not None else process_cwd_state
    semaphores = _semaphores if _semaphores is not None else semaphores_exhausted
    faults: list[str] = []
    if cwd_state(pid) == "gone":
        faults.append(
            "its working directory was deleted (it was started from a "
            "directory that is gone; any getcwd/abspath call in it or its "
            "libraries fails with FileNotFoundError); restart it with "
            "`nx mineru stop && nx mineru start`"
        )
    missing = _missing_paths(info)
    if missing:
        faults.append(
            "it was started with paths that no longer exist ("
            + "; ".join(missing)
            + "); restart it with `nx mineru stop && nx mineru start`"
        )
    sem = semaphores()
    if sem is not None:
        faults.append(sem)
    if tasks_all_failing(health):
        faults.append(
            f"every recent task failed ({health.get('failed_tasks')} failed, "
            f"{health.get('completed_tasks')} completed)"
        )
    return faults


# ── the registered server, as the extractor sees a failing task ──────────────

#: Seconds between two replacements of a broken server, machine-wide.
BROKEN_RESTART_COOLDOWN_S: float = 120.0

_RESTART_STAMP_NAME = "mineru_broken_restart.ts"


def _restart_stamp_path() -> Path:
    from nexus._mineru_pid import _pid_file_path  # noqa: PLC0415 — deferred: patchable at the source module

    return _pid_file_path().with_name(_RESTART_STAMP_NAME)


def claim_broken_restart(
    cooldown_s: float = BROKEN_RESTART_COOLDOWN_S, *, _now: float | None = None,
) -> bool:
    """Whether this process may replace a broken server now; records the claim.

    The cooldown is MACHINE-wide: a timestamp file next to ``mineru.pid``,
    replaced atomically (temp file and ``os.replace``), so every nexus process
    sharing the config directory sees one clock, where a module global gave each
    process its own and let N parallel indexers restart the shared server N
    times. Two processes that read the same stale stamp at once can both claim:
    harmless, because the replacement is keyed on the pid it found broken and
    the second finds a different pid file under the spawn election and probes
    the server the first started. A stamp from the future (clock change) does
    not block."""
    now = _now if _now is not None else time.time()
    path = _restart_stamp_path()
    try:
        last = float(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        last = None
    if last is not None and 0 <= now - last < cooldown_s:
        return False
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(2)}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(repr(now), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        _log.debug("mineru_restart_stamp_write_failed", exc_info=True)
        _unlink_quietly(tmp)
    return True


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _is_registered_server_url(url: str, info: dict[str, Any]) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
        return False
    return parsed.port is not None and parsed.port == info.get("port")


def is_registered_server_url(url: str | None, info: dict[str, Any] | None) -> bool:
    """Whether *url* is the registered server's own address (the pid file's port on loopback).

    An operator-pinned or remote server is not ours to restart; the timeout rule of RDR-237 asks
    this before it replaces a server by pid, so a slow server it was only talking to is never the
    one it kills."""
    return bool(url) and bool(info) and _is_registered_server_url(url, info)  # type: ignore[arg-type]


def server_is_broken(url: str, error_text: str, info: dict[str, Any] | None) -> bool:
    """Whether a task failure at *url* with *error_text* is the REGISTERED
    server being broken, as opposed to one bad request.

    All of: the failing URL is the registered server's (the pid file's port on
    loopback: an operator-pinned or remote server is not ours to restart), and
    either its working directory is gone (:func:`process_cwd_state`) or the
    error text names a path the server owns (its working directory, its output
    root, its interpreter's prefix). A bare ``FileNotFoundError`` that names no
    server path (a PDF that was not there) restarts nothing: killing the
    machine-shared server for it cost every other indexer its in-flight work."""
    if not info or not isinstance(info.get("pid"), int):
        return False
    if not _is_registered_server_url(url, info):
        return False
    if process_cwd_state(info["pid"]) == "gone":
        return True
    owned: list[str] = []
    cwd = process_cwd_path(info["pid"])
    if cwd:
        owned.append(cwd)
    if isinstance(info.get("output_root"), str):
        owned.append(info["output_root"])
    python = info.get("python")
    if isinstance(python, str) and python:
        # Every traceback frame sits under the interpreter's prefix, so it
        # counts only when the prefix is gone (a replaced venv); a live prefix
        # in a request's traceback says nothing about the server.
        prefix = os.path.dirname(os.path.dirname(python))
        if not os.path.exists(prefix):
            owned.append(prefix)
    # At least two components, to be distinctive: "/" or "/tmp" in an error names
    # nothing in particular (a server started by this code has "/" as its cwd).
    return any(p.count(os.sep) >= 2 and p in (error_text or "") for p in owned)
