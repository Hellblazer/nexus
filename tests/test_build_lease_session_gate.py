# SPDX-License-Identifier: AGPL-3.0-or-later
"""The suite refuses to START while a service build holds the shared lease,
or waits for it when asked (nexus-pv93h).

Before this gate, a full run beside any Maven run on the box collected and
then errored every substrate-backed test at setup: 1216 setup errors in one
lint-bucket run on 2026-09-07, all one fact. Now ``pytest_sessionstart`` on
the controller decides once. These tests drive a real child pytest against
a lease held under ``NX_BUILD_LEASE_ROOT`` so the wiring is what is proven,
not only the helper.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
#: A file with no substrate need of its own, so only the session gate can
#: decide the child's exit status.
TARGET = "tests/scripts/test_git_push_develop_sh.py"


def _hold(root: Path, pid: int) -> None:
    d = root / "service"
    d.mkdir(parents=True)
    (d / "pid").write_text(f"{pid}\n")
    (d / "ts").write_text("2026-09-07T20:53:27Z\n")
    (d / "label").write_text("gate-test\n")
    (d / "command").write_text("scripts/mvnw-leased.sh ./mvnw test\n")


def _child(root: Path, **extra: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in {"NX_TEST_T2_SUBSTRATE", "NX_BUILD_LEASE_WAIT"}}
    env.update(NX_BUILD_LEASE_ROOT=str(root), PYTEST_ADDOPTS="", **extra)
    return subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", TARGET],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=180,
    )


def test_a_held_lease_refuses_the_session_once_with_exit_75(tmp_path: Path) -> None:
    _hold(tmp_path, os.getpid())
    proc = _child(tmp_path)
    assert proc.returncode == 75, proc.stdout + proc.stderr
    out = proc.stdout + proc.stderr
    assert "refusing to start" in out and "gate-test" in out and "NX_BUILD_LEASE_WAIT" in out
    assert "collected" not in proc.stdout.lower() or "0 tests collected" in proc.stdout.lower()


def test_a_no_substrate_run_is_never_gated(tmp_path: Path) -> None:
    _hold(tmp_path, os.getpid())
    proc = _child(tmp_path, NX_TEST_T2_SUBSTRATE="none")
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_a_dead_holder_does_not_gate(tmp_path: Path) -> None:
    _hold(tmp_path, 4194304)
    proc = _child(tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_waiting_starts_the_session_once_the_holder_exits(tmp_path: Path) -> None:
    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(3)"])
    # Reap concurrently: an unreaped child is a zombie, and a zombie still
    # answers kill(pid, 0), so the gate would keep seeing a live holder.
    reaper = threading.Thread(target=holder.wait, daemon=True)
    reaper.start()
    try:
        _hold(tmp_path, holder.pid)
        proc = _child(tmp_path, NX_BUILD_LEASE_WAIT="60")
    finally:
        reaper.join(timeout=30)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "build lease: held — waiting" in proc.stderr
    assert "build lease: clear after" in proc.stderr


@pytest.mark.parametrize("bound", ["0", "5"])
def test_an_exhausted_wait_still_refuses(tmp_path: Path, bound: str) -> None:
    _hold(tmp_path, os.getpid())
    proc = _child(tmp_path, NX_BUILD_LEASE_WAIT=bound)
    assert proc.returncode == 75, proc.stdout + proc.stderr
