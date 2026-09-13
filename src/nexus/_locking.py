# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.

"""Cross-platform advisory exclusive locking.

Wraps POSIX ``fcntl.flock`` (Linux/macOS) and ``msvcrt.locking`` (Windows)
behind a small uniform API so the rest of the codebase doesn't carry
platform branches. Locks are advisory at the OS level: processes that
don't participate in the protocol can still write through them, but every
nexus writer routes through this module so the protocol holds.

Two patterns:

* :func:`acquire_directory_lock` — serializes catalog writers. On Unix
  we ``flock`` the directory fd directly; Windows can't lock directory
  handles, so we lock a sentinel file ``<dir>/.lock`` instead. The
  caller's contract is unchanged — opaque token in, ``release_lock``
  with the token out.

* :func:`lock_file` / :func:`unlock_file` — exclusive lock on an
  already-open regular file (used by the per-repo PID lock in the
  indexer). Non-blocking failures raise ``BlockingIOError`` on both
  platforms so callers handle them with a single except clause.

* :func:`lock_fd` / :func:`unlock_fd` — the same contract against a raw
  descriptor. The mint/election locks in ``db/t1.py``,
  ``db/data_token.py`` and ``daemon/service_registry.py`` hold an fd from
  ``os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)``, never a file
  object, and each used to call ``fcntl.flock`` on it directly — a
  module-level ``import fcntl`` that broke the whole CLI on Windows.
  ``lock_file`` delegates here, so there is one implementation of the
  platform branch rather than two.

  Windows note: ``msvcrt.locking`` requires write access on the handle,
  which ``O_WRONLY`` satisfies, and it locks a byte range from the
  current file position — every caller locks one byte from position 0 of
  a dedicated lock file, so the ranges coincide across processes.

``msvcrt.locking(LK_LOCK)`` only retries ten times before raising; we
loop it for true blocking semantics matching ``fcntl.LOCK_EX``.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import IO, Any

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

__all__ = [
    "acquire_directory_lock",
    "release_lock",
    "lock_fd",
    "lock_file",
    "unlock_fd",
    "unlock_file",
]


_DIR_LOCK_SENTINEL = ".lock"


def acquire_directory_lock(directory: Path) -> int:
    """Take an exclusive lock that serializes writers on ``directory``.

    Blocks until granted. Returns an opaque integer token that must be
    passed to :func:`release_lock`.
    """
    if sys.platform == "win32":
        sentinel = directory / _DIR_LOCK_SENTINEL
        fd = os.open(str(sentinel), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            while True:
                try:
                    msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
                    break
                except OSError:
                    time.sleep(0.5)
        except BaseException:
            os.close(fd)
            raise
        return fd
    fd = os.open(str(directory), os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
    except BaseException:
        os.close(fd)
        raise
    return fd


def release_lock(fd: int) -> None:
    """Release a lock acquired by :func:`acquire_directory_lock`."""
    try:
        if sys.platform == "win32":
            try:
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def lock_fd(fd: int, *, blocking: bool) -> None:
    """Take an exclusive lock on the open descriptor ``fd``.

    Caller retains ownership of ``fd`` and is responsible for calling
    :func:`unlock_fd` and closing it. The lock is released by a close
    too, on both platforms, which is what makes a holder that dies
    mid-critical-section safe.

    Raises ``BlockingIOError`` if ``blocking=False`` and the lock is
    contended — the single exception type every call site branches on.
    """
    if sys.platform == "win32":
        if blocking:
            while True:
                try:
                    msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
                    return
                except OSError:
                    time.sleep(0.5)
        else:
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            except OSError as e:
                raise BlockingIOError(str(e)) from e
    else:
        flag = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
        fcntl.flock(fd, flag)  # lifecycle-gate-allow: the shared advisory-lock primitive itself, not a lifecycle election


def unlock_fd(fd: int) -> None:
    """Release a lock acquired by :func:`lock_fd`."""
    if sys.platform == "win32":
        try:
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)


def lock_file(file_obj: IO[Any], *, blocking: bool) -> None:
    """Take an exclusive lock on ``file_obj`` (an open regular file).

    Caller retains ownership of ``file_obj`` and is responsible for
    calling :func:`unlock_file` and closing the file.

    Raises ``BlockingIOError`` if ``blocking=False`` and the lock is
    contended.
    """
    lock_fd(file_obj.fileno(), blocking=blocking)


def unlock_file(file_obj: IO[Any]) -> None:
    """Release a lock acquired by :func:`lock_file`."""
    unlock_fd(file_obj.fileno())
