"""AC1: nx serve start/stop/status/logs — PID file lifecycle and stale PID detection."""
import signal
import sys
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
        with patch("nexus.commands.serve._process_running", return_value=False):
            result = runner.invoke(main, ["serve", "stop"])

    assert result.exit_code == 0
    mock_kill.assert_called_once_with(12345, signal.SIGTERM)
    assert not pid_path.exists()


def test_serve_stop_no_server_running(runner: CliRunner, serve_home: Path) -> None:
    result = runner.invoke(main, ["serve", "stop"])

    assert result.exit_code != 0
    output = result.output.lower()
    assert "not running" in output or "no server" in output or "pid" in output


def test_serve_stop_waits_for_process_exit(runner: CliRunner, serve_home: Path) -> None:
    """stop_cmd polls until the process exits before reporting success."""
    pid_path = _pid_path(serve_home)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text("12345")

    # Simulate process still running on first check, gone on second.
    # The third False is for the final _process_running() check after the loop.
    running_sequence = [True, False, False]

    with patch("nexus.commands.serve.os.kill"):
        with patch("nexus.commands.serve._process_running", side_effect=running_sequence):
            result = runner.invoke(main, ["serve", "stop"])

    assert result.exit_code == 0
    assert "stopped" in result.output.lower()
    assert not pid_path.exists()


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


# ── start: command args ────────────────────────────────────────────────────────

def test_serve_start_spawns_server_main_module(runner: CliRunner, serve_home: Path) -> None:
    """start_cmd spawns nexus.server_main via python -m with port argument."""
    mock_proc = MagicMock()
    mock_proc.pid = 42

    with patch("nexus.commands.serve.subprocess.Popen", return_value=mock_proc) as mock_popen:
        runner.invoke(main, ["serve", "start"])

    assert mock_popen.called
    cmd = mock_popen.call_args.args[0]
    assert cmd[:3] == [sys.executable, "-m", "nexus.server_main"]
    assert len(cmd) == 4  # port is passed as 4th argument


# ── nexus-hzk: port comes from config ─────────────────────────────────────────

# ── nexus-5dh: serve status shows per-repo indexing state ─────────────────────

def test_serve_status_shows_per_repo_state(runner: CliRunner, serve_home: Path) -> None:
    """When server is running, status fetches /repos and shows per-repo status."""
    import json
    pid_path = _pid_path(serve_home)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text("12345")

    repos_payload = json.dumps({
        "repos": {
            "/home/user/project": {"status": "ready", "collection": "code__project"},
            "/home/user/other": {"status": "indexing", "collection": "code__other"},
        }
    }).encode()

    mock_resp = MagicMock()
    mock_resp.read.return_value = repos_payload
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)

    with patch("nexus.commands.serve._process_running", return_value=True):
        with patch("nexus.commands.serve.urllib.request.urlopen", return_value=mock_resp):
            result = runner.invoke(main, ["serve", "status"])

    assert result.exit_code == 0
    assert "ready" in result.output
    assert "indexing" in result.output
    assert "/home/user/project" in result.output


def test_serve_status_handles_server_unreachable(runner: CliRunner, serve_home: Path) -> None:
    """If /repos is unreachable, status shows a graceful message."""
    import urllib.error
    pid_path = _pid_path(serve_home)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text("12345")

    with patch("nexus.commands.serve._process_running", return_value=True):
        with patch("nexus.commands.serve.urllib.request.urlopen",
                   side_effect=urllib.error.URLError("connection refused")):
            result = runner.invoke(main, ["serve", "status"])

    assert result.exit_code == 0
    assert "12345" in result.output  # PID still shown
    assert "unavailable" in result.output.lower() or "unreachable" in result.output.lower()


def test_serve_start_passes_port_from_config(runner: CliRunner, serve_home: Path) -> None:
    """start_cmd reads server.port from load_config and passes it to server_main."""
    import yaml

    # Write a custom port to the config file
    config_path = serve_home / ".config" / "nexus" / "config.yml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(yaml.dump({"server": {"port": 9999}}))

    mock_proc = MagicMock()
    mock_proc.pid = 42

    with patch("nexus.commands.serve.subprocess.Popen", return_value=mock_proc) as mock_popen:
        runner.invoke(main, ["serve", "start"])

    cmd = mock_popen.call_args.args[0]
    assert cmd[-1] == "9999", f"Expected port '9999' in cmd, got {cmd}"
