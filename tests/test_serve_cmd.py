"""AC1: nx serve start/stop/status/logs — PID file lifecycle and stale PID detection."""
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
def serve_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect HOME to tmp_path so PID/log files go to a temp dir."""
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def _pid_path(home: Path) -> Path:
    return home / ".config" / "nexus" / "server.pid"


def _log_path(home: Path) -> Path:
    return home / ".config" / "nexus" / "serve.log"


# ── start ─────────────────────────────────────────────────────────────────────

def test_serve_start_writes_pid_file(runner: CliRunner, serve_home: Path) -> None:
    mock_proc = MagicMock()
    mock_proc.pid = 12345

    with patch("nexus.commands.serve.subprocess.Popen", return_value=mock_proc):
        result = runner.invoke(main, ["serve", "start"])

    assert result.exit_code == 0
    assert _pid_path(serve_home).read_text().strip() == "12345"


def test_serve_start_noop_when_already_running(runner: CliRunner, serve_home: Path) -> None:
    pid_path = _pid_path(serve_home)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text("12345")

    with patch("nexus.commands.serve._process_running", return_value=True):
        with patch("nexus.commands.serve.subprocess.Popen") as mock_popen:
            result = runner.invoke(main, ["serve", "start"])

    assert result.exit_code == 0
    mock_popen.assert_not_called()
    assert "already running" in result.output.lower()


def test_serve_start_removes_stale_pid_and_restarts(
    runner: CliRunner, serve_home: Path
) -> None:
    pid_path = _pid_path(serve_home)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text("99999")  # stale

    mock_proc = MagicMock()
    mock_proc.pid = 11111

    with patch("nexus.commands.serve._process_running", return_value=False):
        with patch("nexus.commands.serve.subprocess.Popen", return_value=mock_proc):
            result = runner.invoke(main, ["serve", "start"])

    assert result.exit_code == 0
    assert pid_path.read_text().strip() == "11111"


def test_serve_start_uses_start_new_session(runner: CliRunner, serve_home: Path) -> None:
    """Popen is called with start_new_session=True (daemonize)."""
    mock_proc = MagicMock()
    mock_proc.pid = 42

    with patch("nexus.commands.serve.subprocess.Popen", return_value=mock_proc) as mock_popen:
        runner.invoke(main, ["serve", "start"])

    call_kwargs = mock_popen.call_args.kwargs
    assert call_kwargs.get("start_new_session") is True


# ── stop ──────────────────────────────────────────────────────────────────────

def test_serve_stop_sends_sigterm(runner: CliRunner, serve_home: Path) -> None:
    pid_path = _pid_path(serve_home)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text("12345")

    with patch("nexus.commands.serve.os.kill") as mock_kill:
        result = runner.invoke(main, ["serve", "stop"])

    assert result.exit_code == 0
    mock_kill.assert_called_once_with(12345, signal.SIGTERM)
    assert not pid_path.exists()


def test_serve_stop_no_server_running(runner: CliRunner, serve_home: Path) -> None:
    result = runner.invoke(main, ["serve", "stop"])

    assert result.exit_code != 0
    output = result.output.lower()
    assert "not running" in output or "no server" in output or "pid" in output


def test_serve_stop_stale_pid_cleans_up(runner: CliRunner, serve_home: Path) -> None:
    """T4: stop when process is already dead cleans up PID file gracefully."""
    pid_path = _pid_path(serve_home)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text("99999")

    with patch("nexus.commands.serve.os.kill", side_effect=ProcessLookupError):
        result = runner.invoke(main, ["serve", "stop"])

    assert result.exit_code == 0
    assert not pid_path.exists()
    assert "stale" in result.output.lower() or "not found" in result.output.lower()


# ── status ────────────────────────────────────────────────────────────────────

def test_serve_status_no_server(runner: CliRunner, serve_home: Path) -> None:
    result = runner.invoke(main, ["serve", "status"])

    assert result.exit_code == 0
    assert "not running" in result.output.lower() or "no server" in result.output.lower()


def test_serve_status_running(runner: CliRunner, serve_home: Path) -> None:
    pid_path = _pid_path(serve_home)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text("12345")

    with patch("nexus.commands.serve._process_running", return_value=True):
        result = runner.invoke(main, ["serve", "status"])

    assert result.exit_code == 0
    assert "12345" in result.output


# ── logs ──────────────────────────────────────────────────────────────────────

def test_serve_logs_no_log_file(runner: CliRunner, serve_home: Path) -> None:
    result = runner.invoke(main, ["serve", "logs"])

    assert result.exit_code == 0
    assert "no log" in result.output.lower() or result.output.strip() == ""


def test_serve_logs_shows_tail(runner: CliRunner, serve_home: Path) -> None:
    log_path = _log_path(serve_home)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("".join(f"log line {i}\n" for i in range(100)))

    result = runner.invoke(main, ["serve", "logs", "--lines", "5"])

    assert result.exit_code == 0
    # Should show last 5 lines (95-99)
    assert "log line 99" in result.output
    assert "log line 95" in result.output
    # Should NOT show early lines
    assert "log line 0\n" not in result.output
