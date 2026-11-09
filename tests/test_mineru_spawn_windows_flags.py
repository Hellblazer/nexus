# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-dmhxw: the MinerU server spawn on Windows.

``start_new_session`` means nothing on Windows, so the server was a plain child
inside any Job Object its spawner sat in (the Desktop extension's bootstrap, a
contained ``run_bounded``), and closing that job killed it. The spawn now takes
the same flags as the storage supervisor (``commands.daemon._supervisor_popen_kwargs``,
nexus-9pc2f): ``CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW``, through the shared
``service_registry.popen_outliving_host_job``, which adds ``CREATE_BREAKAWAY_FROM_JOB``.
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from nexus import _mineru_spawn
from nexus.daemon.service_registry import pid_alive
from nexus.util import win_job
from tests._module_seam import patch_in


def test_windows_kwargs_are_the_supervisors_flags_and_no_session() -> None:
    kwargs = _mineru_spawn._server_popen_kwargs("win32")  # noqa: SLF001 — the unit under test
    assert kwargs["creationflags"] == win_job.CREATE_NEW_PROCESS_GROUP | win_job.CREATE_NO_WINDOW
    # One rule: if the supervisor's flags change, this goes red until the server follows them.
    from nexus.commands.daemon import _supervisor_popen_kwargs

    assert kwargs["creationflags"] == _supervisor_popen_kwargs("win32")["creationflags"]
    assert "start_new_session" not in kwargs
    assert "cwd" in kwargs  # nexus-x3m8e: stable cwd on every platform


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_posix_kwargs_keep_the_new_session(platform: str) -> None:
    kwargs = _mineru_spawn._server_popen_kwargs(platform)  # noqa: SLF001 — the unit under test
    assert kwargs["start_new_session"] is True
    assert "creationflags" not in kwargs
    assert "cwd" in kwargs


def _spawn(tmp_path: Path, platform: str) -> MagicMock:
    proc = MagicMock(pid=4321)
    with patch(
        "nexus._mineru_spawn._resolve_mineru_api_bin", return_value="/fake/bin/mineru-api",
    ), patch(
        "nexus._mineru_spawn._mineru_output_root", return_value=tmp_path,
    ), patch(
        "nexus._mineru_pid._pid_file_path", return_value=tmp_path / "mineru.pid",
    ), patch(
        "nexus.logging_setup.open_child_log_or_devnull", return_value=-3,
    ), patch_in("nexus._mineru_spawn", "subprocess.Popen", return_value=proc) as popen:
        assert _mineru_spawn.spawn_server_process(8011, platform=platform) is proc
    return popen


def test_the_windows_spawn_asks_to_break_away_from_the_host_job(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    popen = _spawn(tmp_path, "win32")
    _, kwargs = popen.call_args
    flags = kwargs["creationflags"]
    assert flags & win_job.CREATE_BREAKAWAY_FROM_JOB, "the server must leave its spawner's job"
    assert flags & win_job.CREATE_NEW_PROCESS_GROUP and flags & win_job.CREATE_NO_WINDOW
    assert "start_new_session" not in kwargs


def test_a_job_that_refuses_breakaway_still_gets_its_server(tmp_path: Path, monkeypatch) -> None:
    """The shared primitive retries once without the flag when the job does not allow breakaway."""
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    proc = MagicMock(pid=4321)
    calls: list[int] = []

    def popen(argv, **kwargs):
        calls.append(kwargs["creationflags"])
        if kwargs["creationflags"] & win_job.CREATE_BREAKAWAY_FROM_JOB:
            raise PermissionError(5, "Access is denied")
        return proc

    with patch(
        "nexus._mineru_spawn._resolve_mineru_api_bin", return_value="/fake/bin/mineru-api",
    ), patch(
        "nexus._mineru_spawn._mineru_output_root", return_value=tmp_path,
    ), patch(
        "nexus._mineru_pid._pid_file_path", return_value=tmp_path / "mineru.pid",
    ), patch(
        "nexus.logging_setup.open_child_log_or_devnull", return_value=-3,
    ), patch_in("nexus._mineru_spawn", "subprocess.Popen", side_effect=popen):
        assert _mineru_spawn.spawn_server_process(8011, platform="win32") is proc
    assert len(calls) == 2 and not calls[1] & win_job.CREATE_BREAKAWAY_FROM_JOB


def test_the_posix_spawn_is_unchanged(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    popen = _spawn(tmp_path, "darwin")
    _, kwargs = popen.call_args
    assert kwargs["start_new_session"] is True and "creationflags" not in kwargs


# ── native Windows: the kernel honours the flags (the guest check the bead names) ──

_PARENT = r'''
import importlib.util, subprocess, sys, time
spec = importlib.util.spec_from_file_location("mcpb_bootstrap", sys.argv[1])
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)
job = bootstrap._KillOnCloseJob()  # the Desktop extension's own job: kill-on-close, breakaway allowed
assert job.assign_self()
argv = [sys.executable, "-c", "import time; time.sleep(60)"]
if sys.argv[3] == "fixed":
    from nexus import _mineru_spawn
    from nexus.daemon.service_registry import popen_outliving_host_job
    child = popen_outliving_host_job(subprocess.Popen, argv, **_mineru_spawn._server_popen_kwargs("win32"))
else:
    child = subprocess.Popen(argv, start_new_session=True)  # the spawn before nexus-dmhxw
open(sys.argv[2], "w").write(str(child.pid))
'''


@pytest.mark.skipif(sys.platform != "win32", reason="a real Job Object needs native Windows")
@pytest.mark.parametrize("mode, survives", [("fixed", True), ("before", False)])
def test_the_server_outlives_the_desktop_extensions_job(tmp_path: Path, mode: str, survives: bool) -> None:
    """The parent puts itself in mcpb/src/bootstrap.py's kill-on-close job, spawns a stand-in server
    and exits, so the OS closes the job. The fixed spawn breaks away and lives; the old one dies
    with the job (the control that shows the test can fail)."""
    repo = Path(__file__).resolve().parents[1]
    pid_file = tmp_path / "child.pid"
    subprocess.run(
        [sys.executable, "-c", _PARENT, str(repo / "mcpb" / "src" / "bootstrap.py"), str(pid_file), mode],
        check=True, timeout=60,
    )
    pid = int(pid_file.read_text(encoding="utf-8"))
    try:
        time.sleep(2)  # the job closes when the parent's last handle does
        assert pid_alive(pid) is survives
    finally:
        if pid_alive(pid):
            subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, check=False)
