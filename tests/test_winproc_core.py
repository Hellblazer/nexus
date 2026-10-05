# SPDX-License-Identifier: AGPL-3.0-or-later
"""``winproc_core``: Windows process identity (RDR-224, nexus-f9bgu.21).

The API is injected, so every branch runs on macOS and Linux. Only
:class:`TestRealWindowsKernel` needs a real Windows host, and it asserts it
read real values (non-vacuity), never skip-passes into green by reading
nothing.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from nexus._install import winproc_core as wp
from tests._win_proc_fake import FakeProc, FakeWinInfoApi, filetime

NOW = 2_000_000_000.0


def _now() -> float:
    return NOW


class TestSplitCommandLine:
    """CommandLineToArgvW rules, including the argv[0] special case."""

    @pytest.mark.parametrize(
        ("raw", "argv"),
        [
            ("", []),
            ("   ", []),
            ("a b  c", ["a", "b", "c"]),
            ('"C:\\Program Files\\nx.exe" daemon service start',
             ["C:\\Program Files\\nx.exe", "daemon", "service", "start"]),
            ("C:\\x\\nx.exe --config-dir=C:\\c",
             ["C:\\x\\nx.exe", "--config-dir=C:\\c"]),
            # argv[0]: backslashes are never escapes, even before a quote.
            ('"C:\\x\\" a', ["C:\\x\\", "a"]),
            # From argv[1]: 2n backslashes + quote -> n backslashes, toggle.
            ('x "a b" c', ["x", "a b", "c"]),
            ('x "a\\\\" c', ["x", "a\\", "c"]),
            # 2n+1 backslashes + quote -> n backslashes + literal quote.
            ('x a\\"b', ["x", 'a"b']),
            ('x "a\\"b"', ["x", 'a"b']),
            ('x a\\\\\\"b', ["x", 'a\\"b']),
            # Backslashes not before a quote are literal.
            ("x C:\\a\\b", ["x", "C:\\a\\b"]),
            # Empty quoted argument survives.
            ('x "" y', ["x", "", "y"]),
            # Unterminated quote in argv[0] takes the rest.
            ('"C:\\x y', ["C:\\x y"]),
        ],
    )
    def test_cases(self, raw: str, argv: list[str]) -> None:
        assert wp.split_command_line(raw) == argv

    def test_render_is_the_space_joined_argv(self) -> None:
        # The shape every POSIX matcher was written against.
        raw = '"C:\\Users\\a b\\.config\\nexus\\service\\nexus-service.exe" --x'
        assert wp.render_command_line(raw) == (
            "C:\\Users\\a b\\.config\\nexus\\service\\nexus-service.exe --x"
        )


class TestExecutableStem:
    @pytest.mark.parametrize(
        ("token", "stem"),
        [
            ("nx-mcp.exe", "nx-mcp"),
            ("C:\\x\\NX.EXE", "NX"),
            ("C:\\x\\python3.12.exe", "python3.12"),
            ("/usr/bin/python3", "python3"),
            ("nexus-service", "nexus-service"),
            ("C:/mixed\\sep/claude.exe", "claude"),
            ("", ""),
        ],
    )
    def test_stem(self, token: str, stem: str) -> None:
        assert wp.executable_stem(token) == stem

    def test_is_basename_for_every_posix_name(self) -> None:
        for name in ("nx", "python3", "nexus-service", "mineru-api", "a.b.c"):
            assert wp.executable_stem(f"/opt/{name}") == os.path.basename(f"/opt/{name}")


def _table() -> dict[int, FakeProc]:
    return {
        10: FakeProc(ppid=1, created=NOW - 500, image="C:\\Windows\\explorer.exe",
                     cmdline="C:\\Windows\\explorer.exe"),
        20: FakeProc(ppid=10, created=NOW - 100, image="C:\\Py\\python.exe",
                     cmdline='"C:\\Py\\python.exe" "C:\\bin\\nx-mcp.exe" --flag'),
        30: FakeProc(ppid=20, created=NOW - 60, image="C:\\bin\\nx.exe",
                     cmdline='"C:\\bin\\nx.exe" daemon aspect-worker'),
    }


class TestReads:
    def test_age_is_now_minus_creation(self) -> None:
        api = FakeWinInfoApi(_table())
        assert wp.process_age_seconds(20, api, now=_now) == pytest.approx(100.0)
        api.assert_no_leaked_handles()

    def test_age_clamped_at_zero(self) -> None:
        api = FakeWinInfoApi({1: FakeProc(0, NOW + 50, "x.exe", "x.exe")})
        assert wp.process_age_seconds(1, api, now=_now) == 0.0

    def test_gone_process_reads_empty_everywhere(self) -> None:
        api = FakeWinInfoApi(_table())
        assert wp.process_age_seconds(999, api, now=_now) is None
        assert wp.process_image_path(999, api) == ""
        assert wp.process_command_line(999, api) == ""
        assert wp.process_command_name(999, api) == ""
        assert wp.parent_pid(999, api) is None

    @pytest.mark.parametrize("pid", [0, -5, 2**32])
    def test_impossible_pid_is_not_asked_about(self, pid: int) -> None:
        api = FakeWinInfoApi(_table())
        assert wp.process_command_line(pid, api) == ""
        assert api.opened == []

    def test_image_path_and_name(self) -> None:
        api = FakeWinInfoApi(_table())
        assert wp.process_image_path(30, api) == "C:\\bin\\nx.exe"
        assert wp.process_command_name(30, api) == "nx"
        api.assert_no_leaked_handles()

    def test_command_line_is_rendered_argv(self) -> None:
        api = FakeWinInfoApi(_table())
        assert wp.process_command_line(20, api) == (
            "C:\\Py\\python.exe C:\\bin\\nx-mcp.exe --flag"
        )

    def test_unreadable_command_line_falls_back_to_image_path(self) -> None:
        procs = _table()
        procs[30].cmdline = None
        api = FakeWinInfoApi(procs)
        assert wp.process_command_line(30, api) == "C:\\bin\\nx.exe"

    def test_single_read_and_enumeration_agree_on_the_fallback_text(self) -> None:
        # The recycle re-check compares process_command() against the
        # enumerated row; they must be the same text for the same process.
        procs = _table()
        procs[30].cmdline = None
        api = FakeWinInfoApi(procs)
        row = next(r for r in wp.enumerate_processes(api, now=_now) if r[0] == 30)
        assert row[3] == wp.process_command_line(30, api)


class TestParentPid:
    def test_live_parent(self) -> None:
        api = FakeWinInfoApi(_table())
        assert wp.parent_pid(30, api) == 20
        api.assert_no_leaked_handles()

    @pytest.mark.parametrize("ppid", [0, 1])
    def test_pid_zero_or_one_parent_is_none(self, ppid: int) -> None:
        api = FakeWinInfoApi({5: FakeProc(ppid, NOW - 5, "a.exe", "a.exe")})
        assert wp.parent_pid(5, api) is None

    def test_exited_parent_is_none(self) -> None:
        procs = _table()
        del procs[20]
        api = FakeWinInfoApi(procs)
        assert wp.parent_pid(30, api) is None

    def test_recycled_parent_number_is_none(self) -> None:
        # Windows never reparents: the child still names pid 20, and a NEW
        # process now owns that number. It was created after the child, so it
        # cannot be its parent.
        procs = _table()
        procs[20].created = NOW - 10  # newer than child 30 (NOW - 60)
        api = FakeWinInfoApi(procs)
        assert wp.parent_pid(30, api) is None

    def test_access_denied_parent_still_counts(self) -> None:
        procs = _table()
        procs[20].denied = True
        api = FakeWinInfoApi(procs)
        assert wp.parent_pid(30, api) == 20


class TestEnumerate:
    def test_rows_carry_pid_ppid_age_and_rendered_command(self) -> None:
        api = FakeWinInfoApi(_table())
        rows = {r[0]: r for r in wp.enumerate_processes(api, now=_now)}
        assert set(rows) == {10, 20, 30}
        assert rows[30][1] == 20
        assert rows[30][2] == pytest.approx(60.0)
        assert rows[30][3] == "C:\\bin\\nx.exe daemon aspect-worker"
        api.assert_no_leaked_handles()

    def test_unopenable_processes_are_skipped_not_guessed(self) -> None:
        procs = _table()
        procs[10].denied = True
        procs[0] = FakeProc(0, NOW, "idle", "idle")
        api = FakeWinInfoApi(procs)
        pids = {r[0] for r in wp.enumerate_processes(api, now=_now)}
        assert pids == {20, 30}

    def test_only_ppid_opens_nothing_else(self) -> None:
        api = FakeWinInfoApi(_table())
        rows = wp.enumerate_processes(api, only_ppid=20, now=_now)
        assert [r[0] for r in rows] == [30]
        assert api.opened == [30]

    def test_failed_snapshot_raises_rather_than_reporting_zero_processes(self) -> None:
        api = FakeWinInfoApi(_table(), snapshot_ok=False)
        with pytest.raises(RuntimeError, match="unreadable"):
            wp.enumerate_processes(api)


@pytest.mark.skipif(sys.platform != "win32", reason="needs the real Windows kernel")
class TestRealWindowsKernel:
    def test_real_identity_of_self_and_a_child(self) -> None:
        api = wp.ctypes_win_info_api()
        me = os.getpid()
        age = wp.process_age_seconds(me, api)
        # Non-vacuity: the kernel gave real values, not the empty answers.
        assert age is not None and 0 <= age < 3600
        assert wp.process_image_path(me, api).lower().endswith(".exe")
        assert wp.process_command_line(me, api) != ""
        assert wp.parent_pid(me, api) == os.getppid()

        marker = f"NXMARK_{me}"
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)", marker, "two words"],
        )
        try:
            time.sleep(1.0)
            command = wp.process_command_line(child.pid, api)
            assert marker in command and command.endswith("two words")
            assert wp.parent_pid(child.pid, api) == me
            rows = [r for r in wp.enumerate_processes(api) if r[0] == child.pid]
            assert rows and marker in rows[0][3] and rows[0][1] == me
        finally:
            child.kill()
            child.wait(timeout=30)
        assert wp.process_command_line(child.pid, api) == ""
        assert wp.process_command_line(1 << 30, api) == ""
