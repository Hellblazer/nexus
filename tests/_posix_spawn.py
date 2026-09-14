"""Route test-process subprocess spawns onto posix_spawn on macOS.

On macOS 26 a fork child of a multithreaded Python parent can die before
exec, inside Network.framework's atfork handler
(``nw_settings_child_has_forked``). The crash is racy under load and lands on
whichever test spawns next. A child started by ``posix_spawn`` runs no atfork
handlers, so it cannot crash there.

CPython's ``Popen._execute_child`` takes ``posix_spawn`` only when every
condition in its gate holds. This plugin adjusts the conditions test and
product code miss:

- A bare program name is resolved on the child's ``PATH`` and passed as
  ``executable``; ``args[0]`` is unchanged.
- ``close_fds`` becomes False when no ``pass_fds`` are given. Python opens
  descriptors non-inheritable by default (PEP 446), but some libraries do not:
  measured in the full suite, Apple's Metal framework leaves its
  ``default.metallib`` files open and inheritable in any worker that loaded the
  embedding model, and sockets to system services appear briefly. So before
  such a spawn, under the lock described below, every inheritable descriptor
  above 2 is made non-inheritable, and the child gets what ``close_fds=True``
  would have given it. No code in this repository relies on an inheritable
  descriptor reaching a child (no ``pass_fds``, no ``close_fds=False``, no
  ``set_inheritable(..., True)``).
- ``cwd=`` of an existing directory becomes a ``/bin/sh`` step that changes to
  it and then ``exec``s the program, because ``os.posix_spawn`` has no chdir
  action. A missing directory is left alone, so Popen still raises as before.
- ``start_new_session=True`` and ``process_group=N``, and a ``preexec_fn`` that
  is exactly ``os.setsid`` or ``os.setpgrp``, become the ``setsid`` and
  ``setpgroup`` arguments of ``os.posix_spawn``, which the CPython 3.12 gate
  does not pass. They go through a thread-local that a thin ``os.posix_spawn``
  wrapper reads, and only for a spawn that meets every other condition.
- multiprocessing's spawn helper (``multiprocessing.util.spawnv_passfds``),
  which calls ``fork_exec`` directly and never reaches Popen, goes through
  ``os.posix_spawn``. On macOS a dup2 of a descriptor onto itself does not
  clear close-on-exec (measured: the child gets EBADF), so the passed
  descriptors are made inheritable in the parent for the spawn and restored
  after it. A lock covers that window, and every spawn whose final
  ``close_fds`` is False takes the same lock, so no other child can inherit
  them.

The adjustment is all or nothing: when the adjusted spawn would still take the
fork path, the spawn runs exactly as the caller asked.

The ``cwd=`` step differs from Popen's own chdir in ways a caller can observe:
``$PWD`` is set to the directory, a script with no ``#!`` line runs as a shell script where Popen
raises ``ENOEXEC``, and a directory removed between the check and the spawn
makes the child exit 127 where Popen raises ``FileNotFoundError``.

Every spawn that still takes the fork path is recorded with the first gate
condition it misses and its call site, and the controller prints the census at
the end of the run. Each spawn that found a stray inheritable descriptor is
counted too, with its call site and the kind and path of each descriptor it
made non-inheritable.
``NX_POSIX_SPAWN=0`` turns the adjustment off and keeps the census, for
comparing runs.
"""

from __future__ import annotations

import collections
import contextlib
import fcntl
import json
import multiprocessing.util as mp_util
import os
import shutil
import stat
import subprocess
import sys
import threading
import traceback

import pytest

_STDLIB_DIR = os.path.dirname(subprocess.__file__)
_THIS_FILE = os.path.abspath(__file__)
_SH = "/bin/sh"
_CD_THEN_EXEC = 'cd -P -- "$1" || exit 127; shift; exec "$@"'
_CD_THEN_EVAL = 'cd -P -- "$1" || exit 127; shift; eval "$1"'

ACTIVE: bool = sys.platform == "darwin" and bool(getattr(subprocess, "_USE_POSIX_SPAWN", False))
ADJUST: bool = os.environ.get("NX_POSIX_SPAWN", "1") != "0"

spawn_paths: collections.Counter[str] = collections.Counter()
fork_reasons: collections.Counter[str] = collections.Counter()
fork_sites: dict[str, collections.Counter[str]] = collections.defaultdict(collections.Counter)

_original_execute_child = subprocess.Popen._execute_child  # type: ignore[attr-defined]
_original_os_posix_spawn = os.posix_spawn
_original_spawnv_passfds = None
_spawn_extra = threading.local()
_inherit_lock = threading.Lock()
inheritable_sites: collections.Counter[str] = collections.Counter()
inheritable_samples: collections.Counter[str] = collections.Counter()


def _posix_spawn_with_extra(path, argv, env, **kwargs):
    """``os.posix_spawn`` plus the setsid or setpgroup this thread asked for."""
    extra = getattr(_spawn_extra, "kwargs", None)
    if extra:
        kwargs = {**kwargs, **extra}
    return _original_os_posix_spawn(path, argv, env, **kwargs)


def _spawnv_passfds(path, args, passfds):
    """multiprocessing's spawn helper, through posix_spawn.

    The passed descriptors are inheritable only for the spawn, under the lock
    every close_fds=False spawn takes; every other descriptor Python opened is
    non-inheritable and closes at exec.
    """
    assert _original_spawnv_passfds is not None, "installed by pytest_configure"
    if not ADJUST:
        spawn_paths["fork"] += 1
        fork_reasons["multiprocessing"] += 1
        fork_sites["multiprocessing"][_call_site()] += 1
        return _original_spawnv_passfds(path, args, passfds)
    passed = {int(fd) for fd in passfds}
    with _inherit_lock:
        _close_on_exec_strays(passed)
        opened = [fd for fd in sorted(passed) if not os.get_inheritable(fd)]
        for fd in opened:
            os.set_inheritable(fd, True)
        try:
            pid = _original_os_posix_spawn(path, list(args), dict(os.environ))
        finally:
            for fd in opened:
                os.set_inheritable(fd, False)
    spawn_paths["posix_spawn"] += 1
    return pid


def _describe_fd(fd: int) -> str:
    """The descriptor's kind, and on macOS its path, for the census samples."""
    try:
        mode = os.fstat(fd).st_mode
    except OSError:
        return "closed"
    kind = next(
        (name for test, name in (
            (stat.S_ISFIFO, "fifo"), (stat.S_ISSOCK, "socket"), (stat.S_ISREG, "file"),
            (stat.S_ISCHR, "chardev"), (stat.S_ISDIR, "dir"),
        ) if test(mode)),
        oct(mode),
    )
    getpath = getattr(fcntl, "F_GETPATH", None)
    if getpath is not None and kind in ("file", "dir", "chardev"):
        try:
            raw = fcntl.fcntl(fd, getpath, bytes(1024))
            path = raw.split(b"\x00", 1)[0].decode(errors="replace")
            return f"{kind} {path}"
        except OSError:
            pass
    return kind


def _inheritable_fds(exclude: set[int]) -> list[int]:
    """Open descriptors above 2 that a child would inherit, outside ``exclude``.

    Each one found is also counted by kind and path in ``inheritable_samples``.
    """
    found = []
    for name in os.listdir("/dev/fd"):
        fd = int(name)
        if fd <= 2 or fd in exclude:
            continue
        try:
            if os.get_inheritable(fd):
                found.append(fd)
        except OSError:
            continue
    for fd in found:
        inheritable_samples[_describe_fd(fd)] += 1
    return found


def _close_on_exec_strays(exclude: set[int]) -> None:
    """Give the child what close_fds=True would have given it.

    A descriptor above 2 that a library left inheritable becomes close-on-exec
    before the spawn. The parent keeps using it; only the child loses it.
    Callers hold ``_inherit_lock``.
    """
    strays = _inheritable_fds(exclude)
    if strays:
        inheritable_sites[_call_site()] += 1
        for fd in strays:
            with contextlib.suppress(OSError):
                os.set_inheritable(fd, False)


def _argv(args, shell: bool, executable) -> list | None:
    """The argv CPython builds in ``_execute_child``; None when it would raise."""
    if isinstance(args, (str, bytes)):
        argv = [args]
    elif isinstance(args, os.PathLike):
        if shell:
            return None
        argv = [args]
    else:
        argv = list(args)
    if shell:
        argv = [_SH, "-c", *argv]
        if executable:
            argv[0] = executable
    return argv


def fork_reason(
    executable, preexec_fn, close_fds, pass_fds, cwd, p2cread, c2pwrite, errwrite,
    start_new_session, process_group, gid, gids, uid, umask,
) -> str | None:
    """The first condition of CPython 3.12's posix_spawn gate that fails, or None."""
    if not os.path.dirname(os.fspath(executable)):
        return "bare executable"
    if preexec_fn is not None:
        return "preexec_fn"
    if close_fds:
        return "close_fds"
    if pass_fds:
        return "pass_fds"
    if cwd is not None:
        return "cwd"
    if not all(fd == -1 or fd > 2 for fd in (p2cread, c2pwrite, errwrite)):
        return "std fd <= 2"
    if start_new_session:
        return "start_new_session"
    if process_group != -1:
        return "process_group"
    if gid is not None or gids is not None or uid is not None:
        return "uid/gid"
    if umask >= 0:
        return "umask"
    return None


def _adjust(args, executable, close_fds, pass_fds, cwd, env, shell):
    """Meet the bare-name, close_fds and cwd gate conditions where it is safe."""
    caller_executable = executable
    if close_fds and not pass_fds:
        close_fds = False
    argv = _argv(args, shell, executable)
    if not argv or any(isinstance(a, bytes) for a in argv):
        return args, executable, close_fds, cwd, shell
    if not shell:
        program = os.fspath(executable if executable is not None else argv[0])
        if not os.path.dirname(program):
            path = (env if env is not None else os.environ).get("PATH", os.defpath)
            found = shutil.which(program, path=path)
            if found:
                executable = found
    if cwd is None or not os.path.isdir(cwd) or not os.access(cwd, os.X_OK):
        return args, executable, close_fds, cwd, shell
    target = os.fspath(cwd)
    if shell:
        if executable is None and isinstance(args, str):
            return [_SH, "-c", _CD_THEN_EVAL, "sh", target, args], _SH, close_fds, None, False
        return args, executable, close_fds, cwd, shell
    program = os.fspath(executable if executable is not None else argv[0])
    if not os.path.dirname(program):
        return args, executable, close_fds, cwd, shell
    if caller_executable is not None and os.fspath(caller_executable) != os.fspath(argv[0]):
        # The cd step would present the executable as argv[0]; keep the caller's.
        return args, executable, close_fds, cwd, shell
    # Hand the shell the caller's own argv[0]: a bare name is found on the
    # same PATH shutil.which just searched, so the program keeps its argv[0].
    first = os.fspath(argv[0]) if caller_executable is None else program
    rest = [os.fspath(a) for a in argv[1:]]
    return [_SH, "-c", _CD_THEN_EXEC, "sh", target, first, *rest], _SH, close_fds, None, False


def _call_site() -> str:
    for frame in reversed(traceback.extract_stack(limit=40)[:-2]):
        name = os.path.abspath(frame.filename)
        if name == _THIS_FILE or name.startswith(_STDLIB_DIR):
            continue
        try:
            name = os.path.relpath(name)
        except ValueError:
            pass
        return f"{name}:{frame.lineno}"
    return "?"


def _execute_child(
    self, args, executable, preexec_fn, close_fds, pass_fds, cwd, env, startupinfo,
    creationflags, shell, p2cread, p2cwrite, c2pread, c2pwrite, errread, errwrite,
    restore_signals, gid, gids, uid, umask, start_new_session, process_group,
):
    requested = (args, executable, preexec_fn, close_fds, cwd, shell, start_new_session, process_group)
    extra = None
    dropped_close_fds = False
    if ADJUST:
        args, executable, close_fds, cwd, shell = _adjust(
            args, executable, close_fds, pass_fds, cwd, env, shell
        )
        if preexec_fn is os.setsid:
            preexec_fn, start_new_session = None, True
        elif preexec_fn is os.setpgrp:
            preexec_fn, process_group = None, 0
    argv = _argv(args, shell, executable)
    if not argv:
        (args, executable, preexec_fn, close_fds, cwd, shell, start_new_session, process_group) = requested
    else:
        program = executable if executable is not None else argv[0]
        wants_session = bool(start_new_session)
        wants_group = process_group != -1
        if ADJUST and wants_session != wants_group and fork_reason(
            program, preexec_fn, close_fds, pass_fds, cwd, p2cread, c2pwrite, errwrite,
            False, -1, gid, gids, uid, umask,
        ) is None:
            extra = {"setsid": True} if wants_session else {"setpgroup": process_group}
            start_new_session, process_group = False, -1
        reason = fork_reason(
            program, preexec_fn, close_fds, pass_fds, cwd, p2cread, c2pwrite, errwrite,
            start_new_session, process_group, gid, gids, uid, umask,
        )
        if reason is not None:
            # The adjustments did not buy posix_spawn, so spawn exactly as the
            # caller asked; the census keeps the condition that still forks.
            (args, executable, preexec_fn, close_fds, cwd, shell, start_new_session, process_group) = requested
            extra = None
        if reason is None:
            spawn_paths["posix_spawn"] += 1
            dropped_close_fds = bool(requested[3]) and not close_fds
        else:
            spawn_paths["fork"] += 1
            fork_reasons[reason] += 1
            fork_sites[reason][_call_site()] += 1
    _spawn_extra.kwargs = extra
    try:
        with contextlib.nullcontext() if close_fds else _inherit_lock:
            if dropped_close_fds:
                _close_on_exec_strays({p2cread, p2cwrite, c2pread, c2pwrite, errread, errwrite})
            return _original_execute_child(
                self, args, executable, preexec_fn, close_fds, pass_fds, cwd, env, startupinfo,
                creationflags, shell, p2cread, p2cwrite, c2pread, c2pwrite, errread, errwrite,
                restore_signals, gid, gids, uid, umask, start_new_session, process_group,
            )
    finally:
        _spawn_extra.kwargs = None


def _snapshot() -> dict:
    return {
        "paths": dict(spawn_paths),
        "reasons": dict(fork_reasons),
        "sites": {r: dict(c) for r, c in fork_sites.items()},
        "inheritable": dict(inheritable_sites),
        "inheritable_kinds": dict(inheritable_samples),
    }


def _merge(data: dict) -> None:
    spawn_paths.update(data.get("paths", {}))
    fork_reasons.update(data.get("reasons", {}))
    for reason, sites in data.get("sites", {}).items():
        fork_sites[reason].update(sites)
    inheritable_sites.update(data.get("inheritable", {}))
    inheritable_samples.update(data.get("inheritable_kinds", {}))


_worker_snapshots: list[dict] = []


def pytest_configure(config: pytest.Config) -> None:
    global _original_spawnv_passfds
    if ACTIVE:
        subprocess.Popen._execute_child = _execute_child  # type: ignore[attr-defined]
        os.posix_spawn = _posix_spawn_with_extra
        _original_spawnv_passfds = mp_util.spawnv_passfds
        mp_util.spawnv_passfds = _spawnv_passfds


def pytest_sessionfinish(session: pytest.Session) -> None:
    workeroutput = getattr(session.config, "workeroutput", None)
    if workeroutput is not None:
        workeroutput["posix_spawn_census"] = json.dumps(_snapshot())


@pytest.hookimpl(optionalhook=True)
def pytest_testnodedown(node, error) -> None:
    raw = getattr(node, "workeroutput", {}).get("posix_spawn_census")
    if raw:
        _worker_snapshots.append(json.loads(raw))


def pytest_terminal_summary(terminalreporter) -> None:
    if not ACTIVE or getattr(terminalreporter.config, "workeroutput", None) is not None:
        return
    for data in _worker_snapshots:
        _merge(data)
    _worker_snapshots.clear()
    total = sum(spawn_paths.values())
    mode = "adjusting" if ADJUST else "census only (NX_POSIX_SPAWN=0)"
    tr = terminalreporter
    tr.write_sep("-", f"posix_spawn census, {mode}")
    tr.write_line(
        f"{total} spawns: {spawn_paths.get('posix_spawn', 0)} posix_spawn, "
        f"{spawn_paths.get('fork', 0)} fork"
    )
    for reason, count in fork_reasons.most_common():
        tr.write_line(f"  fork, {reason}: {count}")
        for site, n in fork_sites[reason].most_common(5):
            tr.write_line(f"      {n:5d}  {site}")
    if inheritable_sites:
        tr.write_line(
            f"  posix_spawn that made a stray inheritable descriptor close-on-exec first: "
            f"{sum(inheritable_sites.values())}"
        )
        for site, n in inheritable_sites.most_common(5):
            tr.write_line(f"      {n:5d}  {site}")
        tr.write_line("    descriptors seen:")
        for kind, n in inheritable_samples.most_common(5):
            tr.write_line(f"      {n:5d}  {kind}")
    out = os.environ.get("NX_SPAWN_CENSUS_OUT")
    if out:
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(_snapshot(), fh, indent=2, sort_keys=True)
