# SPDX-License-Identifier: AGPL-3.0-or-later
"""A live aspect worker holds its lock file's platform lock; the sweep judges by that lock, not the PID (nexus-dyfsw)."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

import nexus.aspect_worker as aw
from nexus import _locking


@pytest.fixture(autouse=True)
def _no_held_lock():
    yield
    if aw._worker_lock_fd is not None:
        os.close(aw._worker_lock_fd)
        aw._worker_lock_fd = None


def _held(path: Path) -> int:
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
    _locking.lock_fd(fd, blocking=False)
    return fd


def _is_held(path: Path) -> bool:
    fd = os.open(str(path), os.O_RDWR)
    try:
        _locking.lock_fd(fd, blocking=False)
    except (BlockingIOError, OSError):
        return True
    finally:
        os.close(fd)
    return False


def test_an_unheld_file_is_swept_even_when_its_pid_is_alive(tmp_path: Path) -> None:
    # The PID test kept this file forever while its PID (here, our parent) lived on.
    stale = tmp_path / f"aspect_worker.{os.getppid()}"
    stale.write_text(str(os.getppid()))
    aw._sweep_dead_worker_locks(tmp_path)
    assert not stale.exists()


def test_a_held_file_survives_the_sweep_even_when_its_pid_is_invisible(tmp_path: Path) -> None:
    # A worker in another PID namespace: its PID is dead here, its lock is not.
    live = tmp_path / "aspect_worker.999999999"
    fd = _held(live)
    try:
        aw._sweep_dead_worker_locks(tmp_path)
        assert live.exists()
    finally:
        os.close(fd)


def test_own_file_and_other_names_are_left_alone(tmp_path: Path) -> None:
    own = tmp_path / f"aspect_worker.{os.getpid()}"
    own.write_text("x")
    odd = tmp_path / "aspect_worker.notapid"
    odd.write_text("x")
    aw._sweep_dead_worker_locks(tmp_path)
    assert own.exists() and odd.exists()


def test_a_worker_holds_its_lock_from_write_until_remove(tmp_path: Path) -> None:
    aw._write_worker_lock(tmp_path)
    mine = aw._worker_lock_path(tmp_path)
    assert os.stat(mine).st_size == len(str(os.getpid()))  # a read would hit the held byte on Windows
    assert _is_held(mine)
    aw._remove_worker_lock(tmp_path)
    assert not mine.exists()
    assert aw._worker_lock_fd is None


def test_an_idempotent_start_keeps_the_held_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    aw._write_worker_lock(tmp_path)
    fd = aw._worker_lock_fd
    relocked: list[int] = []
    real_lock_fd = _locking.lock_fd
    monkeypatch.setattr(_locking, "lock_fd", lambda n, **kw: (relocked.append(n), real_lock_fd(n, **kw))[1])
    aw._write_worker_lock(tmp_path)
    assert relocked == []  # not closed, reopened and locked again: no gap for a sweep
    assert aw._worker_lock_fd == fd
