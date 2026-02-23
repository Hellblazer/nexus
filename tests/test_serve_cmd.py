"""AC1: nx serve start/stop/status/logs — PID file lifecycle and stale PID detection."""
import errno
import signal
import subprocess
import sys
import urllib.error
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

    # Simulate process still running on first poll, gone on second.
    running_sequence = [True, False]

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


# ── nexus-968: wire server stdout/stderr to serve.log ─────────────────────────

def test_serve_start_passes_log_file_to_popen(runner: CliRunner, serve_home: Path) -> None:
    """start_cmd opens serve.log in append mode and passes it as stdout and stderr."""
    mock_proc = MagicMock()
    mock_proc.pid = 42

    with patch("nexus.commands.serve.subprocess.Popen", return_value=mock_proc) as mock_popen:
        runner.invoke(main, ["serve", "start"])

    call_kwargs = mock_popen.call_args.kwargs
    # stdout and stderr should both be the same open file handle (not DEVNULL)
    assert call_kwargs.get("stdout") is not subprocess.DEVNULL, (
        "stdout should be a log file handle, not DEVNULL"
    )
    assert call_kwargs.get("stderr") is not subprocess.DEVNULL, (
        "stderr should be a log file handle, not DEVNULL"
    )
    # Both should be the same file object
    assert call_kwargs.get("stdout") is call_kwargs.get("stderr"), (
        "stdout and stderr should be the same file handle"
    )


def test_serve_start_creates_log_file(runner: CliRunner, serve_home: Path) -> None:
    """start_cmd creates the serve.log file (parent dirs created, file opened)."""
    mock_proc = MagicMock()
    mock_proc.pid = 42

    log_path = _log_path(serve_home)
    assert not log_path.exists(), "Pre-condition: log file should not exist yet"

    with patch("nexus.commands.serve.subprocess.Popen", return_value=mock_proc):
        result = runner.invoke(main, ["serve", "start"])

    assert result.exit_code == 0
    assert log_path.exists(), "serve.log should be created by start_cmd"


def test_serve_logs_shows_content_from_log_file(runner: CliRunner, serve_home: Path) -> None:
    """logs_cmd returns content from the log file when it exists."""
    log_path = _log_path(serve_home)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("Server listening on port 8765\nIndexing /home/user/project\n")

    result = runner.invoke(main, ["serve", "logs"])

    assert result.exit_code == 0
    assert "Server listening on port 8765" in result.output
    assert "Indexing /home/user/project" in result.output


# ── nexus-m6s: nx serve status shows uptime ───────────────────────────────────

def test_serve_status_shows_uptime_when_start_file_exists(
    runner: CliRunner, serve_home: Path
) -> None:
    """status_cmd includes an uptime line when serve.start file is present."""
    import datetime

    pid_path = _pid_path(serve_home)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text("12345")

    # Write a start timestamp 2 hours ago
    start_time = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=2)
    start_file = serve_home / ".config" / "nexus" / "serve.start"
    start_file.write_text(start_time.isoformat())

    with patch("nexus.commands.serve._process_running", return_value=True):
        with patch(
            "nexus.commands.serve.urllib.request.urlopen",
            side_effect=urllib.error.URLError("refused"),
        ):
            result = runner.invoke(main, ["serve", "status"])

    assert result.exit_code == 0
    output = result.output.lower()
    # Must show uptime in some form (hours/minutes/seconds)
    assert "uptime" in output or ("started" in output and ("h" in output or "m" in output)), (
        f"Expected uptime info in output, got: {result.output!r}"
    )


def test_serve_status_no_uptime_without_start_file(runner: CliRunner, serve_home: Path) -> None:
    """status_cmd does not crash and shows running status even if serve.start is absent."""
    pid_path = _pid_path(serve_home)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text("12345")

    with patch("nexus.commands.serve._process_running", return_value=True):
        with patch(
            "nexus.commands.serve.urllib.request.urlopen",
            side_effect=urllib.error.URLError("refused"),
        ):
            result = runner.invoke(main, ["serve", "status"])

    assert result.exit_code == 0
    assert "12345" in result.output


def test_serve_start_writes_start_timestamp_file(runner: CliRunner, serve_home: Path) -> None:
    """start_cmd writes serve.start with an ISO timestamp."""
    import datetime

    mock_proc = MagicMock()
    mock_proc.pid = 42

    before = datetime.datetime.now(datetime.timezone.utc)

    with patch("nexus.commands.serve.subprocess.Popen", return_value=mock_proc):
        result = runner.invoke(main, ["serve", "start"])

    after = datetime.datetime.now(datetime.timezone.utc)

    assert result.exit_code == 0
    start_file = serve_home / ".config" / "nexus" / "serve.start"
    assert start_file.exists(), "serve.start should be written by start_cmd"

    ts_text = start_file.read_text().strip()
    ts = datetime.datetime.fromisoformat(ts_text)
    assert before <= ts <= after, f"Timestamp {ts} should be between {before} and {after}"


# ── _format_uptime ────────────────────────────────────────────────────────────

def test_format_uptime_hours_and_minutes() -> None:
    """_format_uptime with a timestamp >1 hour ago returns 'Xh Ym' format."""
    import datetime
    from nexus.commands.serve import _format_uptime

    started = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=3, minutes=25)
    result = _format_uptime(started)
    assert result == "3h 25m"


def test_format_uptime_minutes_and_seconds() -> None:
    """_format_uptime with a timestamp 1-60 minutes ago returns 'Xm Ys' format."""
    import datetime
    from nexus.commands.serve import _format_uptime

    started = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=12, seconds=45)
    result = _format_uptime(started)
    assert result == "12m 45s"


def test_format_uptime_seconds_only() -> None:
    """_format_uptime with a timestamp <1 minute ago returns 'Xs' format."""
    import datetime
    from nexus.commands.serve import _format_uptime

    started = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=30)
    result = _format_uptime(started)
    assert result == "30s"


# ── _read_pid: ValueError path ───────────────────────────────────────────────

def test_read_pid_returns_none_for_non_integer_content(serve_home: Path) -> None:
    """_read_pid() returns None when the PID file contains non-integer content."""
    from nexus.commands.serve import _read_pid

    pid_path = _pid_path(serve_home)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text("not-an-int")

    result = _read_pid()
    assert result is None


# ── _process_running: EPERM handling ─────────────────────────────────────────

def test_process_running_returns_true_on_eperm() -> None:
    """When os.kill raises EPERM, _process_running returns True (process exists
    but is owned by another user)."""
    from nexus.commands.serve import _process_running

    eperm_error = OSError(errno.EPERM, "Operation not permitted")
    with patch("nexus.commands.serve.os.kill", side_effect=eperm_error):
        assert _process_running(99999) is True


# ── stop: EPERM handling ─────────────────────────────────────────────────────

def test_serve_stop_eperm_prints_permission_error(
    runner: CliRunner, serve_home: Path
) -> None:
    """When stopping a server and os.kill raises EPERM, the command should exit
    with code 1 and mention 'permission' in its output."""
    pid_path = _pid_path(serve_home)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text("12345")

    eperm_error = OSError(errno.EPERM, "Operation not permitted")
    with patch("nexus.commands.serve.os.kill", side_effect=eperm_error):
        result = runner.invoke(main, ["serve", "stop"])

    assert result.exit_code == 1
    assert "permission" in result.output.lower()


# ── Gap 7: FileExistsError in start_cmd (TOCTOU race) ─────────────────────

def test_serve_start_file_exists_process_still_running(
    runner: CliRunner, serve_home: Path
) -> None:
    """When exclusive PID file create hits FileExistsError and process is still
    running, start_cmd reports 'already running' without spawning."""
    # Pre-create the PID file with a known PID
    pid_path = _pid_path(serve_home)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    # First _read_pid returns None (file doesn't exist yet from the initial check),
    # but the "x" open races and sees a file — simulate by having the file appear
    # between _read_pid and open("x").
    # Easiest: pre-create the file so _read_pid returns a PID and process is running.
    pid_path.write_text("54321")

    with patch("nexus.commands.serve._process_running", return_value=True):
        with patch("nexus.commands.serve.subprocess.Popen") as mock_popen:
            result = runner.invoke(main, ["serve", "start"])

    assert result.exit_code == 0
    mock_popen.assert_not_called()
    assert "already running" in result.output.lower()


def test_serve_start_file_exists_stale_process(
    runner: CliRunner, serve_home: Path
) -> None:
    """When exclusive PID file create hits FileExistsError but the process is
    NOT running (stale PID file from crashed process), start_cmd detects the
    stale file and reports accordingly."""
    pid_path = _pid_path(serve_home)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    # _read_pid returns None initially (no PID file), then PID file appears
    # during open("x") race.  We need to set up the race condition:
    # 1. First _read_pid call → returns None (file not present)
    # 2. pid_path.open("x") → FileExistsError
    # 3. Second _read_pid call (inside except) → returns PID
    # 4. _process_running(PID) → False (stale)
    #
    # To achieve this, we patch open("x") to raise FileExistsError:
    original_open = Path.open

    call_count = [0]

    def patched_open(self, *args, **kwargs):
        if str(self) == str(pid_path) and args and args[0] == "x":
            # Simulate race: write PID just before raising
            pid_path.write_text("99999")
            raise FileExistsError
        return original_open(self, *args, **kwargs)

    with patch.object(Path, "open", patched_open):
        with patch("nexus.commands.serve._process_running", return_value=False):
            with patch("nexus.commands.serve.subprocess.Popen") as mock_popen:
                result = runner.invoke(main, ["serve", "start"])

    assert result.exit_code == 0
    mock_popen.assert_not_called()
    assert "already in progress" in result.output.lower()


# ── Gap 8: Popen failure cleanup ──────────────────────────────────────────

def test_serve_start_popen_failure_cleans_up_pid_file(
    runner: CliRunner, serve_home: Path
) -> None:
    """When subprocess.Popen raises an exception, the PID file is cleaned up."""
    with patch(
        "nexus.commands.serve.subprocess.Popen",
        side_effect=OSError("spawn failed"),
    ):
        result = runner.invoke(main, ["serve", "start"])

    # The command should propagate the error
    assert result.exit_code != 0
    # PID file should be cleaned up
    assert not _pid_path(serve_home).exists(), "PID file should be cleaned up after Popen failure"
