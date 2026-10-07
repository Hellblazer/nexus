# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The one place a test starts and stops an engine or PostgreSQL child, on POSIX and Windows.

Before this module, ``tests/_engine_substrate.py``, ``tests/db/_service_fixture.py`` and about
twenty per-file integration fixtures each spawned the engine JVM with ``preexec_fn=os.setsid``
and stopped it with ``os.killpg(os.getpgid(pid), ...)``, and each resolved Java as
``Path(JAVA_HOME) / "bin" / "java"``. None of that exists on Windows: ``preexec_fn`` raises,
``os.killpg`` and ``os.getpgid`` are absent, and the JDK's launcher is ``java.exe``, so every
java-gated suite skipped there with Java installed (T2 ``nexus_rdr/224-win-batch-final-windows``).

POSIX behaviour is unchanged from those copies: the child is spawned with
``preexec_fn=os.setsid``, stopped with ``SIGTERM`` to its process group, waited for, and sent
``SIGKILL`` to the group if it outlives the grace window.

Windows uses what the product's own engine supervisor uses (``nexus.util.process_group``):
``CREATE_NEW_PROCESS_GROUP`` at spawn, then a Job Object with ``KILL_ON_JOB_CLOSE`` assigned right
after ``Popen`` returns. Closing the job is the tree kill. There is no graceful signal to send a
JVM there short of ``CTRL_BREAK`` through a console helper, which a test teardown does not need,
so the "graceful" stop is the job kill followed by a wait.

``tests/test_child_process_spawn_lint.py`` rejects a ``preexec_fn=os.setsid``, an ``os.killpg``
or a bare ``"bin" / "java"`` anywhere else in ``tests/`` outside a frozen allowlist.
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from nexus._install.layout_core import exe_name
from nexus._winsec import grant_user_tree_access
from nexus.util import process_group, win_job

#: True where process groups exist (every POSIX host). Windows has no ``os.killpg``.
_POSIX: bool = getattr(os, "killpg", None) is not None

#: pid -> Windows job handle for children spawned by :func:`popen_in_group`. Always empty on
#: POSIX, where the process group is the containment.
_JOBS: dict[int, int] = {}


def group_popen_kwargs() -> dict[str, Any]:
    """``Popen`` kwargs that put the child in its own group.

    POSIX: ``preexec_fn=os.setsid``, the exact form the copies this replaced used. Windows:
    ``creationflags=CREATE_NEW_PROCESS_GROUP``.
    """
    if _POSIX:
        return {"preexec_fn": os.setsid}
    return {"creationflags": win_job.CREATE_NEW_PROCESS_GROUP}


def popen_in_group(
    argv: list[str], *, popen: Callable[..., subprocess.Popen] | None = None, **kwargs: Any,
) -> subprocess.Popen:
    """``subprocess.Popen(argv, **kwargs)`` in a new process group, contained on Windows.

    On Windows the child goes into a fresh Job Object right after spawn; :func:`kill_group`
    and :func:`stop_group` close it. When the job cannot be created or assigned, stopping falls
    back to terminating the one process (``process_group.kill_tree``'s degraded reach).

    *popen* is the caller's own ``subprocess.Popen`` binding, so a test that patches the
    caller's module still intercepts the spawn.
    """
    proc = (popen or subprocess.Popen)(argv, **kwargs, **group_popen_kwargs())
    job = process_group.contain(proc)
    if job is not None:
        _JOBS[proc.pid] = job
    return proc


def _signal_group(proc: subprocess.Popen, sig: int) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), sig)
    except (ProcessLookupError, PermissionError):
        pass


def kill_group(proc: subprocess.Popen) -> None:
    """Kill *proc* and everything in its group now. Does not wait.

    POSIX: ``SIGKILL`` to the process group. Windows: close the child's job, which terminates
    every process in it; without a job, terminate the one process.
    """
    if _POSIX:
        _signal_group(proc, signal.SIGKILL)
        return
    process_group.kill_tree(proc, _JOBS.pop(proc.pid, None))


def stop_group(proc: subprocess.Popen, *, grace_s: float = 5.0) -> None:
    """Stop *proc*'s group: ``SIGTERM``, wait *grace_s*, then ``SIGKILL`` (POSIX).

    Windows has no graceful stop to send, so the job is killed and the process reaped, with
    the same wait.
    """
    if _POSIX:
        _signal_group(proc, signal.SIGTERM)
        try:
            proc.wait(timeout=grace_s)
        except subprocess.TimeoutExpired:
            _signal_group(proc, signal.SIGKILL)
        return
    kill_group(proc)
    try:
        proc.wait(timeout=grace_s)
    except subprocess.TimeoutExpired:
        pass


def pid_alive(pid: int) -> bool:
    """Is *pid* alive? POSIX: ``kill(pid, 0)``, EPERM counting as alive. Windows: the
    product's ``service_registry.pid_alive``; ``os.kill(pid, 0)`` there is
    ``TerminateProcess`` with exit code 0, not a probe."""
    if not _POSIX:
        from nexus.daemon.service_registry import pid_alive as _registry_pid_alive  # noqa: PLC0415 — deferred: Windows path only

        return _registry_pid_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def group_alive(pgid: int) -> bool:
    """Any member of process group *pgid* alive? ``kill -0 -- -PGID``, EPERM counting as
    alive. Windows has no process groups, so the recorded leader pid is probed instead."""
    if not _POSIX:
        return pid_alive(pgid)
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def java_executable() -> Path:
    """The ``java`` launcher a test should run: ``$JAVA_HOME/bin/java`` (``java.exe`` on
    Windows) when ``JAVA_HOME`` is set, otherwise ``java`` from ``PATH`` (which honours
    ``PATHEXT``), otherwise the bare name."""
    home = os.environ.get("JAVA_HOME", "")
    if home:
        return Path(home) / "bin" / exe_name("java")
    return Path(shutil.which("java") or "java")


def java_available() -> bool:
    """Is :func:`java_executable` runnable? With ``JAVA_HOME`` set, the launcher under it must
    exist; without it, ``java`` must be on ``PATH``."""
    if os.environ.get("JAVA_HOME", ""):
        return java_executable().exists()
    return shutil.which("java") is not None


def pg_data_tempdir(prefix: str, *, parent_dir: str | None = None) -> str:
    """``tempfile.mkdtemp`` for a throwaway PostgreSQL data directory, provisioned the way the
    product provisions its own (``nexus.db.pg_provision``).

    On Windows, ``initdb`` and ``postgres`` drop to a restricted token on which Administrators
    is deny-only, and ``mkdtemp``'s ``0o700`` ACL grants only SYSTEM, Administrators and OWNER
    RIGHTS, so under an elevated session ``initdb`` fails with Permission denied.
    ``grant_user_tree_access`` adds an inheritable ACE for the user's own SID. POSIX: plain
    ``mkdtemp``.
    """
    path = tempfile.mkdtemp(prefix=prefix, dir=parent_dir)
    grant_user_tree_access(path)
    return path


__all__ = [
    "group_alive",
    "group_popen_kwargs",
    "java_available",
    "java_executable",
    "kill_group",
    "pg_data_tempdir",
    "pid_alive",
    "popen_in_group",
    "stop_group",
]
