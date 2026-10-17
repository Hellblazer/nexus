# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-oacd3: ``nx daemon service stop`` for one config directory must not
signal a flagless ``nx daemon service start`` that serves another.

The release battery's local-service gate runs a flagless ``uv run nx daemon
service start`` with the directory in ``NEXUS_CONFIG_DIR`` only. Since
nexus-mqfu0 the stack matcher accepts a flagless command line for the DEFAULT
directory, so a sibling leg's default-dir stop signalled it (the gate exited
143 before any test ran). A flagless command line names no directory; the
process's own environment does, and the sweeps now ask it.

The sweep tests use real child processes with the argv shape of the gate's
start (``<python> .../nx daemon service start``) and a process table cut down
to those children, so no other process on the box can be matched or signalled.
"""
from __future__ import annotations

import os
import struct
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from nexus.commands import daemon as daemon_mod
from nexus.daemon import service_registry as sr
from nexus.daemon import storage_service_daemon as ssd
from nexus.daemon.binary_lifecycle import well_known_binary_path
from nexus.daemon.storage_service_daemon import stop_storage_service
from nexus.upgrade_finish import service_stack_pids

posix_only = pytest.mark.skipif(
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


@posix_only
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

    def test_windows_has_no_reader(self) -> None:
        assert sr.process_environment_values(os.getpid(), ("HOME",), platform="win32") is None


class TestEnvironmentReaderFailsClosed:
    """Every shape that is not a clean read is ``None``, on every platform."""

    def test_a_zero_byte_proc_environ_is_unreadable_not_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        proc_root = tmp_path / "proc"
        (proc_root / "77").mkdir(parents=True)
        (proc_root / "uptime").write_text("1 1\n", encoding="utf-8")
        (proc_root / "77" / "environ").write_bytes(b"")
        monkeypatch.setattr(sr, "PROCFS_ROOT", proc_root)
        if sys.platform == "win32":
            pytest.skip("the Windows branch has no reader at all")
        assert sr.process_environment_values(77, ("HOME",), platform="linux") is None

    def test_a_libc_without_sysctl_is_unreadable_not_a_crash(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(sr, "_procfs_available", lambda: False)
        monkeypatch.setattr(sr, "_load_libc", lambda: object())  # no .sysctl
        assert sr.process_environment_values(1, ("HOME",), platform="darwin") is None

    def test_a_struct_error_is_unreadable_not_a_crash(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def boom(pid: int):
            raise struct.error("unpack requires a buffer of 4 bytes")

        monkeypatch.setattr(sr, "_procfs_available", lambda: False)
        monkeypatch.setattr(sr, "_darwin_procargs_environment", boom)
        assert sr.process_environment_values(1, ("HOME",), platform="darwin") is None


def _procargs2(argc: int, argv: list[bytes], env: list[bytes], *, path: bytes = b"/usr/bin/nx", pad: int = 3) -> bytes:
    """A synthetic KERN_PROCARGS2 block: argc, exec path, NUL padding, argv, env."""
    return (
        struct.pack("i", argc) + path + b"\x00" + b"\x00" * pad
        + b"".join(a + b"\x00" for a in argv) + b"".join(e + b"\x00" for e in env)
    )


class TestProcargs2Parser:
    """The KERN_PROCARGS2 byte parse is a pure function so Linux CI covers it."""

    ARGV = [b"nx", b"daemon", b"service", b"start"]
    ENV = [b"HOME=/h", b"NEXUS_CONFIG_DIR=/c"]

    def test_a_normal_block_yields_exactly_the_environment(self) -> None:
        assert sr.parse_procargs2_environment(_procargs2(4, self.ARGV, self.ENV)) == self.ENV

    def test_an_empty_environment_is_an_empty_list_not_none(self) -> None:
        assert sr.parse_procargs2_environment(_procargs2(4, self.ARGV, [])) == []

    @pytest.mark.parametrize("blob", [b"", b"\x01\x00", b"abc"])
    def test_a_block_shorter_than_the_argc_word_is_none(self, blob: bytes) -> None:
        assert sr.parse_procargs2_environment(blob) is None

    def test_a_negative_argc_is_none(self) -> None:
        assert sr.parse_procargs2_environment(_procargs2(-1, self.ARGV, self.ENV)) is None

    def test_an_argc_larger_than_the_strings_present_is_none(self) -> None:
        assert sr.parse_procargs2_environment(_procargs2(99, self.ARGV, self.ENV)) is None

    def test_a_block_cut_inside_argv_is_none(self) -> None:
        whole = _procargs2(4, self.ARGV, [])
        assert sr.parse_procargs2_environment(whole[: whole.index(b"service") + 3]) is None

    def test_a_block_cut_inside_the_exec_path_is_none(self) -> None:
        assert sr.parse_procargs2_environment(struct.pack("i", 1) + b"/usr/bi") is None

    def test_a_block_cut_inside_the_environment_is_none(self) -> None:
        whole = _procargs2(4, self.ARGV, self.ENV)
        assert sr.parse_procargs2_environment(whole[:-3]) is None


class TestOwnerDirectory:
    def test_override_wins_and_home_is_the_fallback(self, tmp_path: Path) -> None:
        env = {1: {"NEXUS_CONFIG_DIR": str(tmp_path / "a")}, 2: {"HOME": str(tmp_path / "h")}}
        read = lambda pid, names: env.get(pid)  # noqa: E731
        assert sr.process_config_dir(1, environment_of=read) == (tmp_path / "a").resolve()
        assert sr.process_config_dir(2, environment_of=read) == (tmp_path / "h" / ".config" / "nexus").resolve()
        assert sr.process_config_dir(4, environment_of=read) is None  # unreadable

    def test_an_empty_override_falls_back_to_the_process_home(self, tmp_path: Path) -> None:
        read = lambda pid, names: {"NEXUS_CONFIG_DIR": " ", "HOME": str(tmp_path / "h")}  # noqa: E731
        assert sr.process_config_dir(1, environment_of=read) == (tmp_path / "h" / ".config" / "nexus").resolve()

    @pytest.mark.parametrize(
        "env",
        [
            {},  # neither name: never the STOPPER's home
            {"HOME": ""},
            {"NEXUS_CONFIG_DIR": "relative/dir"},  # would resolve against the stopper's cwd
            {"NEXUS_CONFIG_DIR": "relative/dir", "HOME": "/abs/home"},
            {"HOME": "rel/home"},
        ],
    )
    def test_an_environment_that_names_no_absolute_directory_is_none(self, env: dict[str, str]) -> None:
        assert sr.process_config_dir(1, environment_of=lambda pid, names: env) is None

    def test_member_check_decides_from_the_process_not_the_text(self, tmp_path: Path) -> None:
        mine, theirs = tmp_path / "mine", tmp_path / "theirs"
        env = {10: {"NEXUS_CONFIG_DIR": str(mine)}, 11: {"NEXUS_CONFIG_DIR": str(theirs)}, 14: {}}
        asked: list[int] = []

        def read(pid, names):
            asked.append(pid)
            return env.get(pid)

        check = sr.storage_service_member_check(mine, environment_of=read)
        flagless = "/py/bin/python /py/bin/nx daemon service start"
        assert check(10, flagless) == "member"
        assert check(11, flagless) == "other"
        assert check(12, flagless) == "unreadable"  # unreadable environment fails closed
        assert check(14, flagless) == "unreadable"  # readable but names no directory
        asked.clear()
        explicit = f"/py/bin/python /py/bin/nx daemon service start --foreground --config-dir {mine}"
        assert check(11, explicit) == "member"  # text names the dir: environment not consulted
        assert check(13, f"{well_known_binary_path(mine)} -Xmx1g") == "member"
        assert asked == []


@posix_only
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
                sr.sweep_matching_processes(matcher, exclude_pid=-1, member_check=None)
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

    def test_a_stop_for_the_gates_own_scratch_dir_matches_nothing_flagless(self, box) -> None:
        """The matcher still accepts a flagless command only for the default dir,
        so a stop for another dir signals no flagless start at all."""
        gate = box.start(["daemon", "service", "start"], NEXUS_CONFIG_DIR=str(box.other_dir), HOME=str(box.home_dir))
        matcher = sr.storage_service_stack_matcher(box.other_dir)
        check = sr.storage_service_member_check(box.other_dir)
        with patch.object(sr, "all_process_rows", return_value=box.table(gate)):
            result = sr.sweep_matching_processes(matcher, exclude_pid=-1, grace_s=5, member_check=check)
        assert result.pids == ()
        assert _alive(gate)

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


@posix_only
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


@posix_only
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

    def test_a_candidate_that_exited_since_the_snapshot_is_not_reported(self) -> None:
        gone = subprocess.Popen([sys.executable, "-c", "pass"])
        gone.wait(timeout=30)
        row = (gone.pid, 5, "/py/bin/python /py/bin/nx daemon service start")
        with patch.object(sr, "all_process_rows", return_value=[row]):
            result = sr.sweep_matching_processes(
                lambda command: True, exclude_pid=-1, member_check=lambda pid, command: "unreadable",
            )
        assert result.unreadable == ()

    def test_stop_is_neither_already_stopped_nor_verified_while_it_lives(self, box, caplog) -> None:
        mystery = box.start(["daemon", "service", "start"])
        with patch.object(sr, "all_process_rows", return_value=box.table(mystery)):
            outcome = stop_storage_service(config_dir=box.default_dir)
        assert outcome.pids == ()
        assert outcome.unreadable == (mystery.pid,)
        assert outcome.sweep_verified is False
        assert outcome.already_stopped is False
        assert _alive(mystery)

    def test_the_cli_names_the_pid_and_does_not_say_already_stopped(self, tmp_path: Path) -> None:
        outcome = ssd.StopOutcome(
            pids=(), stubborn=(), source="none", lease_seen=False, sweep_verified=False,
            unreadable=(4242,),
        )
        with patch.object(ssd, "stop_storage_service", return_value=outcome):
            result = CliRunner().invoke(
                daemon_mod.service_stop_cmd, ["--config-dir", str(tmp_path)],
            )
        assert "4242" in result.output
        assert "already stopped" not in result.output
        assert "could not be verified" in result.output
        assert "process table could not be checked" not in result.output


@posix_only
def test_service_stack_pids_lists_only_the_flagless_start_that_serves_the_stopped_dir(box) -> None:
    """Pins the `member(pid, command)` conjunct in ``upgrade_finish.service_stack_pids``:
    the matcher alone accepts both flagless children (the default-dir text rule),
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
