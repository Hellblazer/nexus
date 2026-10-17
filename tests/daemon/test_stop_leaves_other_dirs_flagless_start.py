# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-oacd3: ``nx daemon service stop`` for one config directory must not
signal a flagless ``nx daemon service start`` that serves another.

The release battery's local-service gate runs a flagless ``uv run nx daemon
service start`` with the directory in ``NEXUS_CONFIG_DIR`` only. Since
nexus-mqfu0 the stack matcher accepts a flagless command line for the DEFAULT
directory, so a sibling leg's default-dir stop signalled it (the gate exited
143 before any test ran). A flagless command line names no directory; the
process's own environment does, and the sweeps now ask it.

This module is the platform-independent half and runs on Windows too: the
KERN_PROCARGS2 parser on synthetic blocks, the fail-closed reader shapes, the
Windows no-reader contract, the owner-directory rules, and the member check
over injected environments. The real-process tests (POSIX-only by
construction) are ``test_stop_flagless_start_real_processes.py``, which the
Windows test set leaves out.
"""
from __future__ import annotations

import os
import struct
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from nexus.commands import daemon as daemon_mod
from nexus.daemon import service_registry as sr
from nexus.daemon import storage_service_daemon as ssd
from nexus.daemon.binary_lifecycle import well_known_binary_path


class TestEnvironmentReaderWindows:
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


class TestUnreadableFlaglessCandidateReporting:
    """A flagless start whose directory cannot be established is left alone, and
    the stop says so instead of reporting "already stopped" (nexus-oacd3). The
    real-process legs are in ``test_stop_flagless_start_real_processes.py``."""

    def test_a_candidate_that_exited_since_the_snapshot_is_not_reported(self) -> None:
        gone = subprocess.Popen([sys.executable, "-c", "pass"])
        gone.wait(timeout=30)
        row = (gone.pid, 5, "/py/bin/python /py/bin/nx daemon service start")
        with patch.object(sr, "all_process_rows", return_value=[row]):
            result = sr.sweep_matching_processes(
                lambda command: True, exclude_pid=-1, member_check=lambda pid, command: "unreadable",
            )
        assert result.unreadable == ()

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
