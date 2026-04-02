# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for nx mineru start/stop/status CLI commands.

TDD — these tests define the expected behavior for the mineru command group
before the implementation exists. Bead: nexus-964u, Epic: nexus-5f2b (RDR-046).
"""
from __future__ import annotations

import json
import signal
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from nexus.cli import main


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect HOME so PID file lands in tmp_path."""
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def pid_dir(fake_home: Path) -> Path:
    """Create and return the nexus config dir for PID files."""
    d = fake_home / ".config" / "nexus"
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture
def pid_file(pid_dir: Path) -> Path:
    return pid_dir / "mineru.pid"


def _write_pid_file(pid_file: Path, pid: int = 12345, port: int = 8010) -> None:
    pid_file.write_text(json.dumps({
        "pid": pid,
        "port": port,
        "started_at": "2026-04-02T12:00:00+00:00",
    }))


# ── nx mineru start ──────────────────────────────────────────────────────────


class TestMineruStart:
    """nx mineru start launches mineru-api and writes PID file."""

    def test_start_launches_subprocess_and_writes_pid_file(
        self, runner: CliRunner, fake_home: Path, pid_file: Path,
    ) -> None:
        """start: launches mineru-api with correct env vars, polls /health,
        writes PID file with dynamically assigned port, exits 0."""
        mock_proc = MagicMock()
        mock_proc.pid = 42
        mock_proc.poll.return_value = None  # process alive

        mock_resp = MagicMock()
        mock_resp.status_code = 200

        with (
            patch("nexus.commands.mineru.subprocess.Popen", return_value=mock_proc) as mock_popen,
            patch("nexus.commands.mineru.httpx.get", return_value=mock_resp),
            patch("nexus.commands.mineru._find_free_port", return_value=8010),
            patch("nexus.config.load_config", return_value={}),
            patch("nexus.config.set_config_value"),
        ):
            result = runner.invoke(main, ["mineru", "start"])

        assert result.exit_code == 0, result.output
        assert pid_file.exists()
        data = json.loads(pid_file.read_text())
        assert data["pid"] == 42
        assert data["port"] == 8010

        # Verify subprocess options
        call_kwargs = mock_popen.call_args
        assert call_kwargs.kwargs.get("start_new_session") is True

        # Verify env vars passed to subprocess
        env = call_kwargs.kwargs.get("env") or call_kwargs[1].get("env", {})
        assert env.get("MINERU_TABLE_ENABLE") == "false"
        assert env.get("MINERU_PROCESSING_WINDOW_SIZE") == "8"
        assert env.get("MINERU_VIRTUAL_VRAM_SIZE") == "8192"
        assert env.get("MINERU_API_OUTPUT_ROOT") == "/tmp/mineru-output"
        assert env.get("MINERU_API_TASK_RETENTION_SECONDS") == "300"

    def test_start_already_running(
        self, runner: CliRunner, fake_home: Path, pid_file: Path,
    ) -> None:
        """start: server already running (PID file + process alive) → exits 0
        with 'already running' message, no new subprocess."""
        _write_pid_file(pid_file, pid=12345, port=8010)

        with (
            patch("nexus.commands.mineru.os.kill") as mock_kill,
            patch("nexus.commands.mineru.subprocess.Popen") as mock_popen,
        ):
            mock_kill.return_value = None  # process alive (no OSError)
            result = runner.invoke(main, ["mineru", "start"])

        assert result.exit_code == 0, result.output
        assert "already running" in result.output.lower()
        mock_popen.assert_not_called()

    def test_start_health_timeout(
        self, runner: CliRunner, fake_home: Path, pid_file: Path,
    ) -> None:
        """start: /health does not return 200 within timeout → exits non-zero."""
        mock_proc = MagicMock()
        mock_proc.pid = 99
        mock_proc.poll.return_value = None  # process alive

        import httpx

        with (
            patch("nexus.commands.mineru.subprocess.Popen", return_value=mock_proc),
            patch("nexus.commands.mineru.httpx.get", side_effect=httpx.ConnectError("refused")),
            patch("nexus.commands.mineru.time.sleep"),  # skip real sleeps
            patch("nexus.commands.mineru._HEALTH_TIMEOUT_SECONDS", 0.1),
            patch("nexus.commands.mineru._find_free_port", return_value=8010),
            patch("nexus.config.load_config", return_value={}),
        ):
            result = runner.invoke(main, ["mineru", "start"])

        assert result.exit_code != 0
        assert "timeout" in result.output.lower() or "health" in result.output.lower()

    def test_start_binary_not_found(
        self, runner: CliRunner, fake_home: Path,
    ) -> None:
        """start: mineru-api binary not on PATH → clear error message."""
        with (
            patch("nexus.commands.mineru.subprocess.Popen",
                  side_effect=FileNotFoundError("mineru-api")),
            patch("nexus.commands.mineru._find_free_port", return_value=8010),
            patch("nexus.config.load_config", return_value={}),
        ):
            result = runner.invoke(main, ["mineru", "start"])

        assert result.exit_code != 0
        assert "mineru-api" in result.output.lower()

    def test_start_persists_url_to_config(
        self, runner: CliRunner, fake_home: Path, pid_file: Path,
    ) -> None:
        """start: writes dynamically assigned port to config as pdf.mineru_server_url."""
        mock_proc = MagicMock()
        mock_proc.pid = 42
        mock_proc.poll.return_value = None

        mock_resp = MagicMock()
        mock_resp.status_code = 200

        with (
            patch("nexus.commands.mineru.subprocess.Popen", return_value=mock_proc),
            patch("nexus.commands.mineru.httpx.get", return_value=mock_resp),
            patch("nexus.commands.mineru._find_free_port", return_value=54321),
            patch("nexus.config.load_config", return_value={}),
            patch("nexus.config.set_config_value") as mock_set,
        ):
            result = runner.invoke(main, ["mineru", "start"])

        assert result.exit_code == 0, result.output
        mock_set.assert_called_once_with(
            "pdf.mineru_server_url", "http://127.0.0.1:54321",
        )

    def test_start_custom_port(
        self, runner: CliRunner, fake_home: Path, pid_file: Path,
    ) -> None:
        """start: --port 9000 → subprocess launched with --port 9000,
        PID file records port 9000."""
        mock_proc = MagicMock()
        mock_proc.pid = 77
        mock_proc.poll.return_value = None

        mock_resp = MagicMock()
        mock_resp.status_code = 200

        with (
            patch("nexus.commands.mineru.subprocess.Popen", return_value=mock_proc) as mock_popen,
            patch("nexus.commands.mineru.httpx.get", return_value=mock_resp),
            patch("nexus.config.load_config", return_value={}),
            patch("nexus.config.set_config_value"),
        ):
            result = runner.invoke(main, ["mineru", "start", "--port", "9000"])

        assert result.exit_code == 0, result.output
        data = json.loads(pid_file.read_text())
        assert data["port"] == 9000

        # Verify --port passed to subprocess
        call_args = mock_popen.call_args[0][0]  # positional arg list
        assert "--port" in call_args
        port_idx = call_args.index("--port")
        assert call_args[port_idx + 1] == "9000"


# ── nx mineru stop ───────────────────────────────────────────────────────────


class TestMineruStop:
    """nx mineru stop terminates the server and cleans up PID file."""

    def test_stop_sends_sigterm_and_cleans_pid(
        self, runner: CliRunner, fake_home: Path, pid_file: Path,
    ) -> None:
        """stop: reads PID file, sends SIGTERM, waits for exit, deletes PID file."""
        _write_pid_file(pid_file, pid=12345)

        # First os.kill(pid, 0) → alive, then os.kill(pid, SIGTERM),
        # then _is_process_alive polling returns False
        call_count = 0

        def kill_side_effect(pid: int, sig: int) -> None:
            nonlocal call_count
            if sig == 0:
                call_count += 1
                # First call: alive (from _is_process_alive in stop),
                # second call: dead (from polling loop)
                if call_count >= 2:
                    raise OSError("No such process")

        with (
            patch("nexus.commands.mineru.os.kill", side_effect=kill_side_effect) as mock_kill,
            patch("nexus.commands.mineru.time.sleep"),
        ):
            result = runner.invoke(main, ["mineru", "stop"])

        assert result.exit_code == 0, result.output
        mock_kill.assert_any_call(12345, signal.SIGTERM)
        assert not pid_file.exists()

    def test_stop_no_pid_file(
        self, runner: CliRunner, fake_home: Path, pid_file: Path,
    ) -> None:
        """stop: PID file absent → reports 'not running', exits 0."""
        assert not pid_file.exists()
        result = runner.invoke(main, ["mineru", "stop"])

        assert result.exit_code == 0
        assert "not running" in result.output.lower()

    def test_stop_stale_pid(
        self, runner: CliRunner, fake_home: Path, pid_file: Path,
    ) -> None:
        """stop: PID file present but process already dead → cleans up PID file,
        reports 'not running'."""
        _write_pid_file(pid_file, pid=99999)

        with patch(
            "nexus.commands.mineru.os.kill",
            side_effect=OSError("No such process"),
        ):
            result = runner.invoke(main, ["mineru", "stop"])

        assert result.exit_code == 0
        assert not pid_file.exists()
        assert "not running" in result.output.lower()

    def test_stop_sends_sigterm_not_sigkill(
        self, runner: CliRunner, fake_home: Path, pid_file: Path,
    ) -> None:
        """stop: graceful shutdown verified — SIGTERM sent, not SIGKILL."""
        _write_pid_file(pid_file, pid=12345)

        kill_signals: list[int] = []
        call_count = 0

        def track_kill(pid: int, sig: int) -> None:
            nonlocal call_count
            kill_signals.append(sig)
            if sig == 0:
                call_count += 1
                if call_count >= 2:
                    raise OSError("No such process")

        with (
            patch("nexus.commands.mineru.os.kill", side_effect=track_kill),
            patch("nexus.commands.mineru.time.sleep"),
        ):
            result = runner.invoke(main, ["mineru", "stop"])

        assert result.exit_code == 0, result.output
        assert signal.SIGTERM in kill_signals
        assert signal.SIGKILL not in kill_signals


# ── nx mineru status ─────────────────────────────────────────────────────────


class TestMineruStatus:
    """nx mineru status reports server state."""

    def test_status_healthy(
        self, runner: CliRunner, fake_home: Path, pid_file: Path,
    ) -> None:
        """status: PID file present + process alive + /health 200 → reports healthy."""
        _write_pid_file(pid_file, pid=12345, port=8010)

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "status": "ok",
            "active_tasks": 0,
            "completed_tasks": 5,
        }

        with (
            patch("nexus.commands.mineru.os.kill") as mock_kill,
            patch("nexus.commands.mineru.httpx.get", return_value=mock_resp),
        ):
            mock_kill.return_value = None  # process alive
            result = runner.invoke(main, ["mineru", "status"])

        assert result.exit_code == 0, result.output
        assert "healthy" in result.output.lower() or "running" in result.output.lower()

    def test_status_no_pid_file(
        self, runner: CliRunner, fake_home: Path, pid_file: Path,
    ) -> None:
        """status: no PID file → reports 'not running'."""
        assert not pid_file.exists()
        result = runner.invoke(main, ["mineru", "status"])

        assert result.exit_code == 0
        assert "not running" in result.output.lower()

    def test_status_unhealthy(
        self, runner: CliRunner, fake_home: Path, pid_file: Path,
    ) -> None:
        """status: PID file present but /health returns 503 → reports unhealthy."""
        _write_pid_file(pid_file, pid=12345, port=8010)

        mock_resp = MagicMock()
        mock_resp.status_code = 503

        with (
            patch("nexus.commands.mineru.os.kill") as mock_kill,
            patch("nexus.commands.mineru.httpx.get", return_value=mock_resp),
        ):
            mock_kill.return_value = None  # process alive
            result = runner.invoke(main, ["mineru", "status"])

        assert result.exit_code == 0
        assert "unhealthy" in result.output.lower() or "not healthy" in result.output.lower()
