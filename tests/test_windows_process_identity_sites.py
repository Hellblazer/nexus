# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every ``ps`` / ``/proc`` identity site has a Windows branch (nexus-f9bgu.21).

Platform and the Win32 API are injected, so each branch runs on any host. In
every test the POSIX tools are armed to explode: a Windows branch that falls
through to ``ps`` or ``/proc`` fails loudly instead of "working" on the host
running the test.
"""
from __future__ import annotations

import os
import platform as _platform
import subprocess
from pathlib import Path, PureWindowsPath
from unittest.mock import patch

import pytest

from nexus import session
from nexus._install import winproc_core
from nexus import upgrade_finish as uf
from nexus.commands import doctor
from nexus.daemon import aspect_worker_daemon as awd
from nexus.daemon import service_registry as sr
from nexus.upgrade_finish import _classify
from tests._win_proc_fake import FakeProc, FakeWinInfoApi

NOW = 2_000_000_000.0
# A PureWindowsPath so str(CFG / ...) joins with backslashes on every host.
CFG = PureWindowsPath("C:\\Users\\sam\\.config\\nexus")
ENGINE = "C:\\Users\\sam\\.config\\nexus\\service\\nexus-service.exe"


def _table() -> dict[int, FakeProc]:
    return {
        100: FakeProc(1, NOW - 900, "C:\\c\\claude.exe", '"C:\\c\\claude.exe"'),
        200: FakeProc(
            100, NOW - 120, "C:\\Py\\python.exe",
            '"C:\\Py\\python.exe" "C:\\bin\\nx-mcp.exe"',
        ),
        210: FakeProc(100, NOW - 119, "C:\\bin\\nx-mcp-catalog.exe",
                      '"C:\\bin\\nx-mcp-catalog.exe"'),
        220: FakeProc(100, NOW - 118, "C:\\v\\vim.exe", '"C:\\v\\vim.exe" "C:\\bin\\nx-mcp.exe"'),
        300: FakeProc(
            1, NOW - 60, "C:\\bin\\nx.exe",
            '"C:\\bin\\nx.exe" daemon service start --foreground '
            f'--config-dir "{CFG}"',
        ),
        310: FakeProc(300, NOW - 59, ENGINE, f'"{ENGINE}"'),
        320: FakeProc(1, NOW - 58, ENGINE.replace("sam", "other"),
                      f'"{ENGINE.replace("sam", "other")}"'),
    }


@pytest.fixture(autouse=True)
def _posix_tools_are_forbidden(monkeypatch: pytest.MonkeyPatch):
    def boom(*a, **k):
        raise AssertionError(f"Windows branch reached a POSIX probe: {a!r}")

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "check_output", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr(sr, "run_bounded", boom)
    monkeypatch.setattr(session, "run_bounded", boom)
    monkeypatch.setattr(sr, "_procfs_available", boom)
    monkeypatch.setattr(sr, "_ps_enumerate", boom)


@pytest.fixture
def api() -> FakeWinInfoApi:
    return FakeWinInfoApi(_table())


@pytest.fixture(autouse=True)
def _fixed_clock(monkeypatch: pytest.MonkeyPatch):
    real = winproc_core.enumerate_processes

    def fixed(api, *, only_ppid=None, now=lambda: NOW):
        return real(api, only_ppid=only_ppid, now=now)

    monkeypatch.setattr(winproc_core, "enumerate_processes", fixed)


class TestServiceRegistry:
    def test_all_process_rows_is_pid_age_command(self, api: FakeWinInfoApi) -> None:
        rows = {p: (a, c) for p, a, c in sr.all_process_rows(platform="win32", win_info_api=api)}
        assert rows[310] == (59, ENGINE)
        assert rows[300][1].startswith("C:\\bin\\nx.exe daemon service start")
        assert len(rows) == 7
        api.assert_no_leaked_handles()

    def test_failed_snapshot_raises_not_zero_rows(self) -> None:
        bad = FakeWinInfoApi(_table(), snapshot_ok=False)
        with pytest.raises(RuntimeError):
            sr.all_process_rows(platform="win32", win_info_api=bad)

    def test_ps_output_injection_still_wins(self) -> None:
        out = "  PID ELAPSED COMMAND\n 5 01:00 x y\n"
        assert sr.all_process_rows(out, platform="win32") == [(5, 60, "x y")]

    def test_process_command_matches_the_enumerated_row(self, api: FakeWinInfoApi) -> None:
        rows = {p: c for p, _a, c in sr.all_process_rows(platform="win32", win_info_api=api)}
        for pid in (300, 310):
            assert sr.process_command(pid, platform="win32", win_info_api=api) == rows[pid]

    def test_process_command_of_gone_pid_is_empty(self, api: FakeWinInfoApi) -> None:
        assert sr.process_command(999, platform="win32", win_info_api=api) == ""

    def test_process_state_is_unknown_never_a_zombie(self) -> None:
        assert sr.process_state(310, platform="win32") is None
        assert sr.process_state(0, platform="win32") is None

    def test_pid_running_follows_pid_alive_on_windows(self) -> None:
        # None (unknown) reads as running; pid_alive decides death.
        with patch.object(sr, "_is_windows", lambda _p: True):
            with patch.object(sr, "pid_alive", return_value=True):
                assert sr.pid_running(310) is True
            with patch.object(sr, "pid_alive", return_value=False):
                assert sr.pid_running(310) is False

    def test_matcher_finds_the_exe_engine_and_the_supervisor(self, api: FakeWinInfoApi) -> None:
        matcher = sr.storage_service_stack_matcher(CFG, platform_tag="windows-x64")
        rows = sr.all_process_rows(platform="win32", win_info_api=api)
        hit = {p for p, _a, c in rows if matcher(c)}
        # Engine .exe and supervisor of THIS config dir; not the other
        # profile's engine, not claude, not nx-mcp.
        assert hit == {300, 310}

    def test_matcher_without_the_exe_name_would_miss_the_engine(self) -> None:
        # The pre-fix literal: proves the .exe routing is what makes 310 match.
        stale = sr.storage_service_stack_matcher(CFG, platform_tag="linux-amd64")
        assert stale(ENGINE) is False

    def test_sweep_finds_and_recycle_checks_a_windows_stack(self, api: FakeWinInfoApi) -> None:
        matcher = sr.storage_service_stack_matcher(CFG, platform_tag="windows-x64")
        seen: list[list[int]] = []

        def terminate(pids, **_k):
            seen.append(sorted(pids))
            return []

        real_rows, real_cmd = sr.all_process_rows, sr.process_command
        with patch.object(sr, "terminate_pids", terminate), \
                patch.object(sr, "all_process_rows",
                             lambda *a, **k: real_rows(platform="win32", win_info_api=api)), \
                patch.object(sr, "process_command",
                             lambda pid: real_cmd(pid, platform="win32", win_info_api=api)):
            result = sr.sweep_matching_processes(matcher, exclude_pid=-1)
        assert result.available and sorted(result.pids) == [300, 310]
        assert seen == [[300, 310]]


class TestSession:
    def test_ppid_of(self, api: FakeWinInfoApi) -> None:
        assert session._ppid_of(200, platform="win32", win_info_api=api) == 100
        assert session._ppid_of(100, platform="win32", win_info_api=api) is None  # ppid 1
        assert session._ppid_of(999, platform="win32", win_info_api=api) is None

    def test_command_name_is_comm_shaped(self, api: FakeWinInfoApi) -> None:
        assert session._command_name_of(100, platform="win32", win_info_api=api) == "claude"
        assert session._command_name_of(200, platform="win32", win_info_api=api) == "python"
        assert session._command_name_of(999, platform="win32", win_info_api=api) == ""

    def test_list_processes_is_pid_ppid_args(self, api: FakeWinInfoApi) -> None:
        rows = {p: (pp, c) for p, pp, c in
                session._list_processes(platform="win32", win_info_api=api)}
        assert rows[200] == (100, "C:\\Py\\python.exe C:\\bin\\nx-mcp.exe")
        assert len(rows) == 7

    def test_list_processes_narrows_before_opening(self, api: FakeWinInfoApi) -> None:
        rows = session._list_processes(ppid=100, platform="win32", win_info_api=api)
        assert sorted(p for p, _pp, _c in rows) == [200, 210, 220]
        assert sorted(api.opened) == [200, 210, 220]

    def test_list_processes_snapshot_failure_is_empty(self) -> None:
        bad = FakeWinInfoApi(_table(), snapshot_ok=False)
        assert session._list_processes(platform="win32", win_info_api=bad) == []

    def test_find_mcp_siblings_matches_exe_launchers_and_python_exe(
        self, api: FakeWinInfoApi,
    ) -> None:
        real = session._list_processes

        def listing(**k):
            return real(platform="win32", win_info_api=api, **k)

        with patch.object(session, "_list_processes", listing):
            # python.exe + nx-mcp.exe (200) and the catalog launcher (210);
            # NOT vim.exe merely naming nx-mcp.exe (220).
            assert sorted(session.find_mcp_sibling_pids(100)) == [200, 210]

    def test_find_immediate_claude_pid_walks_windows_ancestry(
        self, api: FakeWinInfoApi,
    ) -> None:
        real_ppid, real_name = session._ppid_of, session._command_name_of
        with patch.object(session, "_ppid_of",
                          lambda pid: real_ppid(pid, platform="win32", win_info_api=api)), \
                patch.object(session, "_command_name_of",
                             lambda pid: real_name(pid, platform="win32", win_info_api=api)):
            assert session.find_immediate_claude_pid(200) == 100

    def test_orphan_tracker_sweep_is_a_noop(self) -> None:
        assert session.sweep_orphan_resource_trackers(platform="win32") == 0


class TestDoctorAndAspectWorker:
    def test_orphan_tracker_count_is_not_applicable(self) -> None:
        assert doctor._count_orphan_trackers(platform="win32") is None

    def test_aspect_worker_process_age_ms_from_creation_time(self) -> None:
        procs = {os.getpid(): FakeProc(1, NOW - 12.5, "C:\\Py\\python.exe", "python.exe")}
        age = awd._process_age_ms(
            platform="win32", win_info_api=FakeWinInfoApi(procs), now=lambda: NOW,
        )
        assert age == pytest.approx(12500.0, abs=0.1)

    def test_aspect_worker_age_none_when_unreadable(self) -> None:
        procs = {os.getpid(): FakeProc(1, NOW, "python.exe", "python.exe", denied=True)}
        assert awd._process_age_ms(
            platform="win32", win_info_api=FakeWinInfoApi(procs),
        ) is None


class TestUpgradeFinish:
    @pytest.mark.parametrize(
        ("command", "kind"),
        [
            (f"{ENGINE}", "service"),
            ("C:\\bin\\nx.exe daemon aspect-worker", "aspect-worker"),
            ("C:\\bin\\nx.exe daemon service start", "service"),
            ("C:\\Py\\python.exe C:\\bin\\nx-mcp.exe", "mcp-host"),
            ("C:\\bin\\mineru-api.exe --port 1", "mineru"),
            ("C:\\bin\\nx.exe search aspect-worker", "other"),
        ],
    )
    def test_classify_windows_commands(self, command: str, kind: str) -> None:
        assert _classify(command) == kind

    def test_stop_sweep_engine_path_is_the_exe_on_windows(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``_sweep_surviving_stack`` splits survivors into engines and
        supervisors by the engine's exact path. With the bare literal the
        Windows ``.exe`` engine lands in the supervisor list."""
        monkeypatch.setattr(_platform, "system", lambda: "Windows")
        terminated: list[list[int]] = []
        monkeypatch.setattr(uf, "_pid_alive", lambda pid: True)
        monkeypatch.setattr(uf, "process_state", lambda pid: None)
        monkeypatch.setattr(uf, "process_command", lambda pid: "")
        monkeypatch.setattr(
            uf, "terminate_pids", lambda pids, **k: terminated.append(list(pids)) or [],
        )
        # config_dir is a PureWindowsPath-shaped str on this host: use Path with
        # the engine name the Windows branch must produce.
        cfg = Path("/cfg")
        before = [(1, "nx daemon service start --foreground"),
                  (2, str(cfg / "service" / "nexus-service.exe"))]
        uf._sweep_surviving_stack(cfg, before)
        assert terminated == [[1], [2]]
