# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx daemon service stop --with-pg`` also stops a MinerU server nexus started.

Indexing auto-starts ``mineru-api`` (about 420 MB resident) and nothing in the
full-stack stop ended it, so a box that had been "stopped" kept a model server
until someone ran ``nx mineru stop``. The server is found the way
``nx mineru stop`` and ``nx uninstall`` find it: ``mineru.pid`` under the
config dir, never a process-name match, and ``nx mineru stop`` re-checks the live
command before it signals (a reused pid is not ours).

The children here are REAL processes, so "stopped" and "left alone" are observed.
"""
from __future__ import annotations

import contextlib
import json
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from nexus.cli import main
from nexus.daemon.storage_service_daemon import StopOutcome
from nexus.upgrade_finish import process_command

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="the children are python scripts stopped by process group; the Windows stop has no group (nexus-6y4e0)",
)


@pytest.fixture
def config_dir(tmp_path: Path, monkeypatch) -> Path:
    cd = tmp_path / "cfg"
    cd.mkdir(mode=0o700)
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(cd))
    return cd


def _spawn(script_name: str, where: Path) -> subprocess.Popen[bytes]:
    """A sleeping child in its own session whose command classifies as
    ``script_name`` (``mineru-api`` classifies as MinerU)."""
    script = where / script_name
    script.write_text("import time\ntime.sleep(300)\n")
    proc = subprocess.Popen([sys.executable, str(script)], start_new_session=True)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not process_command(proc.pid):
        time.sleep(0.05)
    return proc


@contextlib.contextmanager
def _reaped(proc: subprocess.Popen[bytes]):
    try:
        yield proc
    finally:
        with contextlib.suppress(OSError):
            proc.kill()
        with contextlib.suppress(subprocess.TimeoutExpired, ChildProcessError):
            proc.wait(timeout=5)


def _write_pid_file(config_dir: Path, pid: int) -> Path:
    path = config_dir / "mineru.pid"
    path.write_text(json.dumps({"pid": pid, "port": 8010, "started_at": "2026-10-08T00:00:00+00:00"}))
    return path


def _stop(config_dir: Path, *args: str):
    outcome = StopOutcome(pids=(), stubborn=(), source="none")
    with patch("nexus.daemon.storage_service_daemon.stop_storage_service", return_value=outcome):
        return CliRunner().invoke(
            main, ["daemon", "service", "stop", "--config-dir", str(config_dir), *args],
        )


def test_with_pg_stops_a_mineru_server_nexus_started(config_dir: Path, tmp_path: Path) -> None:
    with _reaped(_spawn("mineru-api", tmp_path)) as server:
        pid_file = _write_pid_file(config_dir, server.pid)

        result = _stop(config_dir, "--with-pg")

        assert result.exit_code == 0, result.output
        assert server.wait(timeout=15) is not None, "the server was signalled and exited"
        assert not pid_file.exists()
        assert f"MinerU server stopped (PID {server.pid})" in result.output, result.output


def test_a_stop_without_with_pg_leaves_mineru_alone(config_dir: Path, tmp_path: Path) -> None:
    with _reaped(_spawn("mineru-api", tmp_path)) as server:
        pid_file = _write_pid_file(config_dir, server.pid)

        result = _stop(config_dir)

        assert result.exit_code == 0, result.output
        assert server.poll() is None, "the plain service stop does not touch MinerU"
        assert pid_file.exists()


def test_a_mineru_nexus_did_not_start_is_left_alone(config_dir: Path, tmp_path: Path) -> None:
    """A mineru-api the user started themselves has no mineru.pid: nothing
    names it, and a process-name match is exactly what this must not use."""
    with _reaped(_spawn("mineru-api", tmp_path)) as foreign:
        assert not (config_dir / "mineru.pid").exists()

        result = _stop(config_dir, "--with-pg")

        assert result.exit_code == 0, result.output
        assert foreign.poll() is None, "a server no pid file names is not ours to stop"
        assert "MinerU" not in result.output, result.output


def test_a_pid_file_naming_a_reused_pid_signals_nothing(config_dir: Path, tmp_path: Path) -> None:
    with _reaped(_spawn("some-other-tool", tmp_path)) as bystander:
        _write_pid_file(config_dir, bystander.pid)

        result = _stop(config_dir, "--with-pg")

        assert result.exit_code == 0, result.output
        assert bystander.poll() is None, "the live command is not a mineru-api, so it is not signalled"


def test_a_different_config_dir_does_not_reach_the_users_mineru(
    config_dir: Path, tmp_path: Path,
) -> None:
    """MinerU state lives under the default config dir. A sandbox run with its
    own --config-dir must not stop the real user's server."""
    other = tmp_path / "sandbox"
    other.mkdir(mode=0o700)
    with _reaped(_spawn("mineru-api", tmp_path)) as server:
        _write_pid_file(config_dir, server.pid)

        result = _stop(other, "--with-pg")

        assert result.exit_code == 0, result.output
        assert server.poll() is None
        assert "not the default config dir" in result.output, result.output
