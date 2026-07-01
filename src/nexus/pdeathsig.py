# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared PR_SET_PDEATHSIG preexec helper.

A spawned child armed with ``PR_SET_PDEATHSIG=SIGKILL`` is killed by the kernel
when its parent process dies — by ANY means, including an uncatchable SIGKILL
of the parent. This is the only mechanism that stops a long-running child from
being orphaned when the parent cannot run cleanup code.

Two consumers share this one implementation (nexus-4r9ja, RDR-149-aligned
shared-primitive discipline):

- ``storage_service_daemon`` — arms it on the JVM engine child so an OOM-killed
  supervisor leaves no orphaned-but-serving JVM (nexus-03bcg).
- ``aspect_extractor`` — arms it on the ``claude -p`` child so a killed
  aspect-worker daemon does not orphan claude burning API quota (RDR-173 RF-8).

Linux-only: ``PR_SET_PDEATHSIG`` has no macOS/BSD equivalent, so the helper is a
no-op off Linux and callers must gate their ``preexec_fn`` on ``LIBC is not
None``. The orphan hazard persists off Linux by OS limitation; each caller
documents its own fallback (for the aspect worker: reclaim-first recovers the
stranded row, only bounded quota-burn remains, and macOS is dev-only).
"""
from __future__ import annotations

import ctypes
import os
import signal
import sys

#: PR_SET_PDEATHSIG (linux/prctl.h): deliver a signal to THIS process when its
#: parent dies.
_PR_SET_PDEATHSIG: int = 1

#: The pid of the process that imported this module. The fork-vs-parent-death
#: race guard compares the child's ``getppid()`` against this to detect that the
#: parent already died before ``prctl`` ran (subreaper-agnostic — unlike a
#: ``getppid()==1`` check, which misses systemd/container subreapers). Captured
#: at import; every process that spawns an armed child imports this module in
#: that same process, so this equals the spawning process's pid.
#:
#: CONSTRAINT: this module must be imported in the SAME process that calls
#: ``Popen`` with the preexec. A ``multiprocessing`` fork-start child would
#: inherit ``_IMPORTER_PID`` from its parent, then call ``Popen`` — the
#: grandchild's ``getppid()`` (the fork-child's pid) would differ from the
#: inherited ``_IMPORTER_PID`` and the race guard would fire ``os._exit(0)`` on
#: every spawn even with the parent alive. Exec-based subprocess spawning (the
#: only pattern in this codebase — CLI, MCP server, daemons) is safe.
_IMPORTER_PID: int = os.getpid()

#: libc handle for the prctl(2) call, loaded ONCE at import (not in the
#: preexec_fn — only a pre-loaded function pointer is async-signal-safe to call
#: post-fork). ``CDLL(None)`` resolves already-loaded symbols (glibc AND musl);
#: ``None`` off Linux, where PR_SET_PDEATHSIG does not exist.
LIBC: ctypes.CDLL | None
if sys.platform.startswith("linux"):
    try:
        LIBC = ctypes.CDLL(None, use_errno=True)
        LIBC.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong]
        LIBC.prctl.restype = ctypes.c_int
    except (OSError, AttributeError):  # pragma: no cover — libc/prctl must exist on Linux, but never crash import
        LIBC = None
else:
    LIBC = None


def set_pdeathsig_preexec() -> None:
    """``preexec_fn``: arm PR_SET_PDEATHSIG=SIGKILL so the child dies when its
    parent (the spawning process) dies.

    Runs in the child after ``fork()``, before ``exec()``. Async-signal-safe:
    only the import-time-loaded ``LIBC.prctl``, ``os.getppid``, ``os.write``,
    and ``os._exit`` (NO structlog / allocation post-fork). No-op off Linux.

    Two robustness details:
    - A failed prctl (seccomp/SELinux/ancient kernel) is surfaced via an
      async-signal-safe ``os.write`` to fd 2 — otherwise the orphan hazard
      would silently reinstate with no diagnostic.
    - Closes the fork-vs-parent-death race: if the parent died before prctl
      ran, the signal was missed; ``getppid() != _IMPORTER_PID`` means we were
      reparented, so exit rather than orphan.
    """
    if LIBC is None:  # non-Linux: PR_SET_PDEATHSIG unavailable
        return
    if LIBC.prctl(_PR_SET_PDEATHSIG, signal.SIGKILL) != 0:
        os.write(2, b"nexus: prctl(PR_SET_PDEATHSIG) failed; child will not die with parent\n")
    if os.getppid() != _IMPORTER_PID:  # reparented → parent already dead → don't orphan
        os._exit(0)
