# SPDX-License-Identifier: AGPL-3.0-or-later
"""The repo-lock sweep judges a lock file by its platform lock, on every platform (nexus-dyfsw).

Kept apart from test_index_lock.py, which imports fcntl and so cannot run on Windows.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from nexus import _locking
from nexus.indexer import _clear_stale_lock, _same_file, _sweep_stale_locks


def _hold(lock: Path) -> int:
    """Take the platform lock on *lock* through its own descriptor, as a live indexer does."""
    fd = os.open(str(lock), os.O_RDWR | os.O_CREAT, 0o644)
    _locking.lock_fd(fd, blocking=False)
    return fd


def test_clear_stale_lock_removes_an_unheld_file_whatever_it_holds(tmp_path: Path) -> None:
    """The lock is the test (nexus-dyfsw): an unheld file is stale, fresh, empty or naming a live PID."""
    for name, body in (("fresh.lock", ""), ("live.lock", str(os.getpid())), ("garbage.lock", "x")):
        lock = tmp_path / name
        lock.write_text(body)
        _clear_stale_lock(lock)
        assert not lock.exists(), name


def test_clear_stale_lock_keeps_a_held_file_whatever_pid_it_names(tmp_path: Path) -> None:
    """A holder whose PID this process cannot see (another PID namespace) still holds the lock (nexus-dyfsw)."""
    lock = tmp_path / "held.lock"
    fd = _hold(lock)
    try:
        os.write(fd, b"999999999")  # a PID that is dead here: the old test removed the file for it
        _clear_stale_lock(lock)
        assert lock.exists()
    finally:
        os.close(fd)


def test_sweep_stale_locks_cleans_directory(tmp_path: Path) -> None:
    """_sweep_stale_locks removes every unheld lock file and keeps the held one."""
    locks_dir = tmp_path / "locks"
    locks_dir.mkdir()
    for name in ["aaa.lock", "bbb.lock", "ccc.lock"]:
        (locks_dir / name).touch()
    fd = _hold(locks_dir / "held.lock")
    try:
        _sweep_stale_locks(locks_dir)
        remaining = list(locks_dir.glob("*.lock"))
    finally:
        os.close(fd)
    assert [p.name for p in remaining] == ["held.lock"]


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot unlink an open file")
def test_same_file_tells_an_unlinked_lock_from_the_one_the_path_names(tmp_path: Path) -> None:
    """A holder that locked an inode a sweep then unlinked must notice and lock the path's file instead."""
    lock = tmp_path / "repo.lock"
    fd = os.open(str(lock), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        assert _same_file(fd, lock)
        lock.unlink()
        assert not _same_file(fd, lock)
        lock.touch()  # the next arrival's file
        assert not _same_file(fd, lock)
    finally:
        os.close(fd)
