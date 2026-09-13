# SPDX-License-Identifier: AGPL-3.0-or-later
"""Behaviour contract for the raw-fd half of :mod:`nexus._locking`.

``lock_fd`` / ``unlock_fd`` exist because the advisory locks in
``db/t1.py``, ``db/data_token.py`` and ``daemon/service_registry.py`` all
hold a raw descriptor from ``os.open(path, os.O_WRONLY | os.O_CREAT,
0o600)`` rather than a Python file object, and each of them called
``fcntl.flock`` directly — a module-level ``import fcntl`` that does not
exist on Windows. These tests pin the semantics the call sites depend on,
on every platform:

* an exclusive lock can be taken on a **write-only** descriptor (the
  Windows ``msvcrt.locking`` path needs write access, and ``O_WRONLY``
  is exactly what every call site passes);
* a second process sees contention, and ``blocking=False`` surfaces it
  as ``BlockingIOError`` — the one exception type all three call sites
  already catch;
* ``unlock_fd`` and a plain ``close`` both release.

Integration over mocks: these drive a real subprocess against a real
file on a real filesystem, because the whole point of the primitive is
the kernel behaviour underneath it.
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from nexus._locking import lock_fd, unlock_fd

#: Child program: try a NON-BLOCKING acquire on *argv[1]* and report which
#: way it went. Runs in a fresh interpreter so the lock is genuinely
#: cross-process (advisory locks held by the test process itself would be
#: a weaker assertion).
_PROBE = textwrap.dedent(
    """
    import os, sys
    sys.path.insert(0, sys.argv[2])
    from nexus._locking import lock_fd, unlock_fd

    fd = os.open(sys.argv[1], os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        try:
            lock_fd(fd, blocking=False)
        except BlockingIOError:
            print("CONTENDED")
        else:
            print("ACQUIRED")
            unlock_fd(fd)
    finally:
        os.close(fd)
    """
)

_SRC_ROOT = str(Path(__file__).parent.parent / "src")


def _probe(path: Path) -> str:
    """Return ``ACQUIRED`` or ``CONTENDED`` from a separate process."""
    result = subprocess.run(
        [sys.executable, "-c", _PROBE, str(path), _SRC_ROOT],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, f"probe failed: {result.stderr}"
    return result.stdout.strip()


@pytest.fixture
def lock_path(tmp_path: Path) -> Path:
    return tmp_path / "mint.lock"


def _open_lock(path: Path) -> int:
    """Open *path* exactly as the production call sites do."""
    return os.open(str(path), os.O_WRONLY | os.O_CREAT, 0o600)


def test_exclusive_lock_on_write_only_fd(lock_path: Path) -> None:
    """A write-only descriptor can be locked (Windows needs write access)."""
    fd = _open_lock(lock_path)
    try:
        lock_fd(fd, blocking=False)
        unlock_fd(fd)
    finally:
        os.close(fd)


def test_second_process_is_contended_while_held(lock_path: Path) -> None:
    """A held lock is visible to another process as ``BlockingIOError``."""
    fd = _open_lock(lock_path)
    try:
        lock_fd(fd, blocking=True)
        assert _probe(lock_path) == "CONTENDED"
    finally:
        unlock_fd(fd)
        os.close(fd)


def test_unlock_releases_for_another_process(lock_path: Path) -> None:
    """After ``unlock_fd`` the lock is available again — the release path
    the ``finally:`` block in every call site depends on."""
    fd = _open_lock(lock_path)
    try:
        lock_fd(fd, blocking=True)
        unlock_fd(fd)
        assert _probe(lock_path) == "ACQUIRED"
    finally:
        os.close(fd)


def test_close_releases_for_another_process(lock_path: Path) -> None:
    """Closing the descriptor releases the lock even without an explicit
    unlock — the crash-safety property the mint locks rely on when a
    holder dies mid-critical-section."""
    fd = _open_lock(lock_path)
    lock_fd(fd, blocking=True)
    os.close(fd)
    assert _probe(lock_path) == "ACQUIRED"


def test_non_blocking_raises_blocking_io_error_not_os_error(lock_path: Path) -> None:
    """The contended non-blocking acquire raises ``BlockingIOError``
    specifically. ``db/data_token.py`` and ``db/t1.py`` both branch on
    that exact type; a bare ``OSError`` would escape their except clause
    and turn a routine sibling collision into a hard failure.

    Same-process, second descriptor: ``flock`` associates the lock with
    the open file description, so a second ``os.open`` contends exactly
    as another process would (verified against the cross-process probe
    above, which pins the same behaviour).
    """
    first = _open_lock(lock_path)
    second = _open_lock(lock_path)
    try:
        lock_fd(first, blocking=True)
        with pytest.raises(BlockingIOError):
            lock_fd(second, blocking=False)
        unlock_fd(first)
    finally:
        os.close(second)
        os.close(first)


def test_lock_file_still_delegates(tmp_path: Path) -> None:
    """The pre-existing file-object API keeps working after the refactor —
    ``indexer.py`` and ``tuple_watch.py`` are unchanged consumers."""
    from nexus._locking import lock_file, unlock_file

    path = tmp_path / "repo.pid"
    with open(path, "w", encoding="utf-8") as handle:
        lock_file(handle, blocking=False)
        unlock_file(handle)
