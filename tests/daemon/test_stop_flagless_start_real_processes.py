# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-oacd3: the real-process half of the flagless-start stop tests.

``nx daemon service stop`` for one config directory must not signal a flagless
``nx daemon service start`` that serves another. These tests spawn real child
processes with the argv shape of the release battery's local-service gate
(``<python> .../nx daemon service start``) and cut the process table down to
those children, so no other process on the box can be matched or signalled.

POSIX-only by construction: the children are ``#!``-script processes started in
a session of their own, and the environment of a foreign process is read from
``/proc`` or ``KERN_PROCARGS2``, which Windows has no counterpart for. The
module skips itself on win32 and the Windows test set leaves it out
(``.github/workflows/windows-pg-bundle-rehearsal.yml``) rather than widening
the skip ceiling. The platform-independent half (the procargs parser, the
fail-closed reader shapes, the Windows no-reader contract, the owner-directory
rules) is ``test_stop_leaves_other_dirs_flagless_start.py`` and runs on Windows.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from nexus.daemon import service_registry as sr
from nexus.daemon.storage_service_daemon import stop_storage_service
from nexus.upgrade_finish import service_stack_pids

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="environment of a foreign process is read on POSIX only"
)


def _fake_nx(bin_dir: Path) -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "nx"
    script.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(120)\n", encoding="utf-8")
    script.chmod(0o755)
    return script


def _spawn(nx: Path, args: list[str], env: dict[str, str]) -> subprocess.Popen:
    proc = subprocess.Popen(
        [str(nx), *args],
        env={"PATH": os.environ.get("PATH", ""), **env},
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
    )
    # Wait until the exec happened: the process table shows the script's argv.
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if "daemon service" in sr.process_command(proc.pid):
            return proc
        time.sleep(0.02)
    proc.kill()
    raise AssertionError("child never showed its command line")


def _alive(proc: subprocess.Popen) -> bool:
    return proc.poll() is None


@pytest.fixture
def box(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Sandbox HOME (so "the default dir" is ``home/.config/nexus``), a fake
    ``nx``, and cleanup of every child spawned."""
    home = tmp_path / "home"
    (home / ".config" / "nexus").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("NEXUS_CONFIG_DIR", raising=False)
    monkeypatch.delenv("NEXUS_SERVICE_BIN", raising=False)
    monkeypatch.delenv("NEXUS_SERVICE_JAR", raising=False)
    nx = _fake_nx(tmp_path / "bin")
    children: list[subprocess.Popen] = []

    class Box:
        default_dir = home / ".config" / "nexus"
        other_dir = tmp_path / "gate-scratch"
        second_dir = tmp_path / "second-scratch"
        home_dir = home

        def start(self, args: list[str], **env: str) -> subprocess.Popen:
            proc = _spawn(nx, args, env)
            children.append(proc)
            return proc

        def table(self, *procs: subprocess.Popen):
            return [(p.pid, 5, sr.process_command(p.pid)) for p in procs]

    yield Box()
    for proc in children:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)


class TestEnvironmentReader:
    def test_returns_only_the_requested_names(self, tmp_path: Path) -> None:
        nx = _fake_nx(tmp_path / "bin")
        proc = _spawn(nx, ["daemon", "service", "start"], {
            "NEXUS_CONFIG_DIR": "/some/dir with space", "A_SECRET_LOOKING_NAME": "hunter2",
        })
        try:
            got = sr.process_environment_values(proc.pid, ("NEXUS_CONFIG_DIR", "HOME"))
        finally:
            proc.kill()
            proc.wait(timeout=10)
        assert got == {"NEXUS_CONFIG_DIR": "/some/dir with space"}

    def test_a_missing_process_is_unreadable_not_empty(self) -> None:
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait(timeout=30)
        assert sr.process_environment_values(proc.pid, ("HOME",)) is None


class TestSweepWithRealProcesses:
    def test_before_the_check_a_default_dir_sweep_takes_the_other_dirs_flagless_start(self, box) -> None:
        """The bug, as the release battery met it: the matcher alone accepts both."""
        gate = box.start(["daemon", "service", "start"], NEXUS_CONFIG_DIR=str(box.other_dir), HOME=str(box.home_dir))
        own = box.start(["daemon", "service", "start"], HOME=str(box.home_dir))
        rows = box.table(gate, own)
        matcher = sr.storage_service_stack_matcher(box.default_dir)
        with patch.object(sr, "all_process_rows", return_value=rows):
            terminated: list[list[int]] = []
            with patch.object(sr, "terminate_pids", side_effect=lambda pids, **_: terminated.append(sorted(pids)) or []):
                sr.sweep_matching_processes(
                    matcher, exclude_pid=-1,
                    member_check=lambda _pid, _command: "member",  # the text-only answer, no second stage
                )
        assert terminated == [sorted([gate.pid, own.pid])]

    def test_a_default_dir_sweep_leaves_the_other_dirs_flagless_start_and_stops_its_own(self, box) -> None:
        gate = box.start(["daemon", "service", "start"], NEXUS_CONFIG_DIR=str(box.other_dir), HOME=str(box.home_dir))
        own = box.start(["daemon", "service", "start"], HOME=str(box.home_dir))  # nexus-mqfu0: flagless default-dir
        matcher = sr.storage_service_stack_matcher(box.default_dir)
        check = sr.storage_service_member_check(box.default_dir)
        with patch.object(sr, "all_process_rows", return_value=box.table(gate, own)):
            result = sr.sweep_matching_processes(matcher, exclude_pid=-1, grace_s=5, member_check=check)
        assert result.pids == (own.pid,)
        assert own.wait(timeout=10) is not None
        assert _alive(gate)

    def test_a_stop_for_a_non_default_dir_stops_the_flagless_start_that_serves_it(self, box) -> None:
        """nexus-uy35j: the matcher used to offer a flagless command only for the
        default dir, so ``stop(D)`` for a non-default ``D`` never reached a bare
        start whose own ``NEXUS_CONFIG_DIR`` is ``D``. The member verdict decides."""
        gate = box.start(["daemon", "service", "start"], NEXUS_CONFIG_DIR=str(box.other_dir), HOME=str(box.home_dir))
        matcher = sr.storage_service_stack_matcher(box.other_dir)
        check = sr.storage_service_member_check(box.other_dir)
        with patch.object(sr, "all_process_rows", return_value=box.table(gate)):
            result = sr.sweep_matching_processes(matcher, exclude_pid=-1, grace_s=5, member_check=check)
        assert result.pids == (gate.pid,)
        assert gate.wait(timeout=10) is not None

    def test_a_non_default_dir_sweep_leaves_the_flagless_start_of_a_different_dir(self, box) -> None:
        """Two real flagless children under different ``NEXUS_CONFIG_DIR`` values:
        a stop for one signals that one and leaves the other, and the default
        dir's own bare start is left too."""
        mine = box.start(["daemon", "service", "start"], NEXUS_CONFIG_DIR=str(box.other_dir), HOME=str(box.home_dir))
        theirs = box.start(["daemon", "service", "start"], NEXUS_CONFIG_DIR=str(box.second_dir), HOME=str(box.home_dir))
        home_start = box.start(["daemon", "service", "start"], HOME=str(box.home_dir))
        matcher = sr.storage_service_stack_matcher(box.other_dir)
        check = sr.storage_service_member_check(box.other_dir)
        with patch.object(sr, "all_process_rows", return_value=box.table(mine, theirs, home_start)):
            result = sr.sweep_matching_processes(matcher, exclude_pid=-1, grace_s=5, member_check=check)
        assert result.pids == (mine.pid,)
        assert result.unreadable == ()
        assert mine.wait(timeout=10) is not None
        assert _alive(theirs) and _alive(home_start)

    def test_an_unreadable_flagless_start_is_reported_for_a_non_default_dir_too(self, box) -> None:
        mystery = box.start(["daemon", "service", "start"])  # no NEXUS_CONFIG_DIR, no HOME
        matcher = sr.storage_service_stack_matcher(box.other_dir)
        check = sr.storage_service_member_check(box.other_dir)
        with patch.object(sr, "all_process_rows", return_value=box.table(mystery)):
            result = sr.sweep_matching_processes(matcher, exclude_pid=-1, grace_s=2, member_check=check)
        assert result.pids == () and result.unreadable == (mystery.pid,)
        assert _alive(mystery)

    def test_an_explicit_supervisor_is_stopped_by_its_own_dir_only(self, box) -> None:
        sup = box.start(
            ["daemon", "service", "start", "--foreground", "--config-dir", str(box.other_dir)],
            HOME=str(box.home_dir),
        )
        other = sr.storage_service_stack_matcher(box.default_dir)
        with patch.object(sr, "all_process_rows", return_value=box.table(sup)):
            miss = sr.sweep_matching_processes(
                other, exclude_pid=-1, member_check=sr.storage_service_member_check(box.default_dir),
            )
        assert miss.pids == () and _alive(sup)
        mine = sr.storage_service_stack_matcher(box.other_dir)
        with patch.object(sr, "all_process_rows", return_value=box.table(sup)):
            hit = sr.sweep_matching_processes(
                mine, exclude_pid=-1, grace_s=5, member_check=sr.storage_service_member_check(box.other_dir),
            )
        assert hit.pids == (sup.pid,)
        assert sup.wait(timeout=10) is not None


def test_stop_storage_service_for_the_default_dir_spares_the_gates_start(box) -> None:
    """End to end through ``stop_storage_service`` (the verb behind ``nx daemon
    service stop``), the process table cut down to the two children."""
    gate = box.start(["daemon", "service", "start"], NEXUS_CONFIG_DIR=str(box.other_dir), HOME=str(box.home_dir))
    own = box.start(["daemon", "service", "start"], HOME=str(box.home_dir))
    with patch.object(sr, "all_process_rows", return_value=box.table(gate, own)):
        outcome = stop_storage_service(config_dir=box.default_dir)
    assert outcome.pids == (own.pid,)
    assert own.wait(timeout=10) is not None
    assert _alive(gate)


def test_stop_storage_service_for_a_non_default_dir_finds_its_flagless_start(box) -> None:
    """nexus-uy35j end to end through ``stop_storage_service``: no lease exists
    for the non-default dir, so the stop reaches the start only through the
    process-table fallback. The other dir's start survives."""
    mine = box.start(["daemon", "service", "start"], NEXUS_CONFIG_DIR=str(box.other_dir), HOME=str(box.home_dir))
    theirs = box.start(["daemon", "service", "start"], NEXUS_CONFIG_DIR=str(box.second_dir), HOME=str(box.home_dir))
    with patch.object(sr, "all_process_rows", return_value=box.table(mine, theirs)):
        outcome = stop_storage_service(config_dir=box.other_dir)
    assert outcome.pids == (mine.pid,)
    assert outcome.already_stopped is False
    assert mine.wait(timeout=10) is not None
    assert _alive(theirs)


class TestUnreadableFlaglessCandidate:
    """A flagless start whose directory cannot be established is left alone, and
    the stop says so instead of reporting "already stopped" (nexus-oacd3)."""

    def test_the_sweep_lists_it_and_does_not_signal_it(self, box) -> None:
        # No NEXUS_CONFIG_DIR and no HOME: the process names no directory at all.
        mystery = box.start(["daemon", "service", "start"])
        matcher = sr.storage_service_stack_matcher(box.default_dir)
        check = sr.storage_service_member_check(box.default_dir)
        with patch.object(sr, "all_process_rows", return_value=box.table(mystery)):
            result = sr.sweep_matching_processes(matcher, exclude_pid=-1, grace_s=2, member_check=check)
        assert result.pids == () and result.unreadable == (mystery.pid,)
        assert _alive(mystery)

    def test_stop_is_neither_already_stopped_nor_verified_while_it_lives(self, box, caplog) -> None:
        mystery = box.start(["daemon", "service", "start"])
        with patch.object(sr, "all_process_rows", return_value=box.table(mystery)):
            outcome = stop_storage_service(config_dir=box.default_dir)
        assert outcome.pids == ()
        assert outcome.unreadable == (mystery.pid,)
        assert outcome.sweep_verified is False
        assert outcome.already_stopped is False
        assert _alive(mystery)


def test_service_stack_pids_lists_only_the_flagless_start_that_serves_the_stopped_dir(box) -> None:
    """Pins the `member(pid, command)` conjunct in ``upgrade_finish.service_stack_pids``:
    the matcher alone offers both flagless children (a bare start names no directory),
    so only the per-process environment check can drop the one that serves another
    directory."""
    elsewhere = box.start(
        ["daemon", "service", "start"], NEXUS_CONFIG_DIR=str(box.other_dir), HOME=str(box.home_dir),
    )
    under_home = box.start(["daemon", "service", "start"], HOME=str(box.home_dir))
    rows = box.table(elsewhere, under_home)
    assert all(sr.storage_service_stack_matcher(box.default_dir)(cmd) for _p, _a, cmd in rows)
    with patch("nexus.upgrade_finish.all_process_rows", return_value=rows):
        listed = [pid for pid, _command in service_stack_pids(box.default_dir)]
    assert listed == [under_home.pid]


def test_service_stack_pids_lists_the_flagless_start_that_serves_a_non_default_dir(box) -> None:
    """nexus-uy35j: the upgrade path's snapshot shares the matcher, so it sees a
    non-default dir's bare start too, and only that one."""
    mine = box.start(["daemon", "service", "start"], NEXUS_CONFIG_DIR=str(box.other_dir), HOME=str(box.home_dir))
    theirs = box.start(["daemon", "service", "start"], NEXUS_CONFIG_DIR=str(box.second_dir), HOME=str(box.home_dir))
    with patch("nexus.upgrade_finish.all_process_rows", return_value=box.table(mine, theirs)):
        listed = [pid for pid, _command in service_stack_pids(box.other_dir)]
    assert listed == [mine.pid]
