# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nexus.tuple_watch.watcher_alive`` (nexus-6konb.19): the liveness check
``nx hook mailbox-arm`` makes before it prints an arm instruction."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from nexus.tuple_watch import WATCH_COMMAND_MARK, lock_path, watcher_alive

ADDRESS = "sess-alive"


def _write_lock(state_dir: Path, pid: int) -> None:
    path = lock_path(state_dir, ADDRESS)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"pid={pid} session={ADDRESS} address={ADDRESS} started_at=2026-09-14T00:00:00Z")


@pytest.fixture
def fake_watcher():
    """A live process whose command line carries the watcher's mark."""
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(300)", *WATCH_COMMAND_MARK.split()],
    )
    try:
        yield proc.pid
    finally:
        proc.kill()
        proc.wait()


def test_no_lock_file_is_no_watcher(tmp_path: Path) -> None:
    assert watcher_alive(tmp_path, ADDRESS) is False


def test_a_live_watcher_process_is_alive(tmp_path: Path, fake_watcher: int) -> None:
    _write_lock(tmp_path, fake_watcher)
    assert watcher_alive(tmp_path, ADDRESS) is True


def test_a_live_pid_that_is_not_a_watcher_is_not_alive(tmp_path: Path) -> None:
    """A dead watcher's pid reused by another process."""
    _write_lock(tmp_path, os.getpid())
    assert watcher_alive(tmp_path, ADDRESS) is False


def test_a_dead_pid_is_not_alive(tmp_path: Path) -> None:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    _write_lock(tmp_path, proc.pid)
    assert watcher_alive(tmp_path, ADDRESS) is False


def test_the_mark_is_the_watch_command_path() -> None:
    """The mark is what argv holds when the watcher runs as nx tuple watch."""
    from nexus.cli import main as cli

    tuple_group = cli.commands["tuple"]
    assert "watch" in tuple_group.commands
    assert WATCH_COMMAND_MARK == "tuple watch"
