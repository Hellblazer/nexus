# SPDX-License-Identifier: AGPL-3.0-or-later
"""Stand-in child processes for supervisor tests that must also run on native Windows
(RDR-224, nexus-f9bgu.44).

Three things a POSIX-written test takes for granted, named once so each platform
branch is visible:

* ``OWN_GROUP``: on Windows ``start_new_session=True`` is silently ignored, so a
  child spawned that way shares the pytest run's console process group, and the
  supervisor's real ``CTRL_BREAK`` aimed at the child ends the run with exit code
  ``0xC000013A`` (measured on qwentescence, nexus-f9bgu.19 and .44). The production
  spawns are ``CREATE_NEW_PROCESS_GROUP``; the test children must be too.
* ``CHILD_PYTHON``: the venv ``python.exe`` on Windows is a launcher that spawns
  the real interpreter as ITS child, so the ``Popen`` pid is not the process that
  sleeps and a kill by that pid leaves the sleeper behind. The base interpreter
  has no launcher.
* ``IGNORE_STOP_SIGNALS``: a child that takes neither the POSIX stop (``SIGTERM``)
  nor the Windows one (``CTRL_BREAK``, ``SIGBREAK``), so a ladder test reaches the
  hard kill on both platforms.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from nexus.util.process_group import KILL_SIGNAL

WIN: bool = sys.platform == "win32"

#: ``Popen.wait()`` result of a process taken down by ``KILL_SIGNAL``: the negated
#: signal on POSIX, the ``TerminateProcess`` exit code (the signal number itself) on
#: Windows.
KILLED_RC: int = KILL_SIGNAL if WIN else -KILL_SIGNAL

CHILD_PYTHON: str = getattr(sys, "_base_executable", sys.executable) if WIN else sys.executable

#: ``Popen`` kwargs giving a child its own process group, as the production spawns do.
OWN_GROUP: dict[str, Any] = (
    {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}  # type: ignore[attr-defined]
    if WIN
    else {"start_new_session": True}
)

IGNORE_STOP_SIGNALS: str = (
    "import signal,time\n"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "signal.signal(getattr(signal, 'SIGBREAK', signal.SIGTERM), signal.SIG_IGN)\n"
)


def break_file_for(where: Path, pid: int) -> Path:
    """The file whose appearance stands in for a ``CTRL_BREAK`` aimed at *pid*."""
    return where / f"break.{pid}"


def spawn_breakable(*, ignore_break: bool, where: Path) -> "subprocess.Popen[bytes]":
    """A real child that exits 0 when its break file appears, unless *ignore_break*.

    The stop-ladder tests need a graceful stop they can deliver on every host. A
    real ``CTRL_BREAK`` aimed at a child reaches the pytest run's own group unless
    the child has one of its own; ``SIGTERM`` is no stand-in on Windows, where
    ``os.kill`` is ``TerminateProcess``. A file the child polls is delivered the
    same way everywhere, and an ignoring child really ignores it, so the ladder
    reaches its hard kill. ``proc.break_file`` is the file a test writes to "send"
    the break. The child names its file by its own pid, which is the ``Popen`` pid
    because :data:`CHILD_PYTHON` has no launcher.
    """
    code = (
        "import os, sys, time\n"
        "bf = os.path.join(sys.argv[1], 'break.%d' % os.getpid())\n"
        "print('up', flush=True)\n"
        "end = time.time() + 120\n"
        "while time.time() < end:\n"
    )
    code += "    time.sleep(0.05)\n" if ignore_break else (
        "    if os.path.exists(bf):\n        sys.exit(0)\n    time.sleep(0.05)\n"
    )
    proc = subprocess.Popen(  # noqa: S603 -- fixed argv, this interpreter
        [CHILD_PYTHON, "-c", code, str(where)],
        stdout=subprocess.PIPE,
        **OWN_GROUP,
    )
    proc.break_file = break_file_for(where, proc.pid)  # type: ignore[attr-defined]
    assert proc.stdout is not None
    assert proc.stdout.readline().strip() == b"up", "fixture must have started"
    return proc


def spawn_sleeper(tail: list[str], *, seconds: int = 120) -> "subprocess.Popen[bytes]":
    """A live, sleeping child whose argv ends in *tail*, in its own process group, once it is UP.

    For the tests that hand a pid to a real stop (``terminate_pids``, ``stop_tier_holders``,
    uninstall). Three Windows facts decide the shape:

    * its own group (:func:`tests._child_process.popen_in_group`), because the stop is a
      ``CTRL_BREAK`` addressed to the pid, and a pid that is not a group id sends the break
      to every process on the console, the pytest run included;
    * :data:`CHILD_PYTHON`, because the venv launcher swallows a break and sleeps in a child
      of its own, so the pid the test records is not the process that sleeps;
    * it returns only after the child printed ``up``, because a break sent before a new
      process has attached to the console reaches nobody and the stop then waits out its
      whole grace window before the hard kill.
    """
    from tests._child_process import popen_in_group  # noqa: PLC0415 — keeps this module's import cost at what conftest already pays

    proc = popen_in_group(
        [CHILD_PYTHON, "-c", f"import time; print('up', flush=True); time.sleep({seconds})", *tail],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    assert proc.stdout is not None
    assert proc.stdout.readline().strip() == b"up", "fixture must have started"
    proc.stdout.close()
    return proc


@pytest.fixture
def launchd_uid(monkeypatch: pytest.MonkeyPatch) -> int:
    """A POSIX uid for the tests that run launchd's arm on any host.

    launchd's ``gui/<uid>`` domain is the POSIX uid by definition, and the
    installer reads it with ``os.getuid()`` (a lint-exempt site: macOS only). A
    Windows host has no ``os.getuid``, so a test that injects ``platform="darwin"``
    would fail there before reaching anything it means to check; this stands a
    fixed uid in for it. A POSIX host is untouched and keeps its real uid
    (RDR-224, nexus-f9bgu.44). Registered for ``tests/daemon`` by its conftest.
    """
    if not hasattr(os, "getuid"):
        monkeypatch.setattr(os, "getuid", lambda: 501, raising=False)
    return os.getuid()
