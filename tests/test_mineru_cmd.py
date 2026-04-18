# SPDX-License-Identifier: AGPL-3.0-or-later
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
def pid_file(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    d = tmp_path / ".config" / "nexus"
    d.mkdir(parents=True, exist_ok=True)
    return d / "mineru.pid"


def _write_pid(pid_file: Path, pid: int = 12345, port: int = 8010) -> None:
    pid_file.write_text(json.dumps({
        "pid": pid, "port": port, "started_at": "2026-04-02T12:00:00+00:00",
    }))


def _mock_start_success(pid=42, port=8010):
    """Context managers for a successful start."""
    proc = MagicMock()
    proc.pid = pid
    proc.poll.return_value = None
    resp = MagicMock()
    resp.status_code = 200
    return (
        patch("nexus.commands.mineru.subprocess.Popen", return_value=proc),
        patch("nexus.commands.mineru.httpx.get", return_value=resp),
        patch("nexus.commands.mineru._find_free_port", return_value=port),
        patch("nexus.config.load_config", return_value={}),
        patch("nexus.config.set_config_value"),
    )


# ── nx mineru start ──────────────────────────────────────────────────────────

class TestMineruStart:
    def test_start_launches_and_writes_pid(self, runner, pid_file):
        patches = _mock_start_success(pid=42, port=8010)
        with patches[0] as mock_popen, patches[1], patches[2], patches[3], patches[4]:
            result = runner.invoke(main, ["mineru", "start"])
        assert result.exit_code == 0
        data = json.loads(pid_file.read_text())
        assert data["pid"] == 42 and data["port"] == 8010
        kw = mock_popen.call_args.kwargs
        assert kw.get("start_new_session") is True
        env = kw.get("env", {})
        for key in ("MINERU_TABLE_ENABLE", "MINERU_PROCESSING_WINDOW_SIZE",
                     "MINERU_VIRTUAL_VRAM_SIZE", "MINERU_API_OUTPUT_ROOT",
                     "MINERU_API_TASK_RETENTION_SECONDS"):
            assert key in env

    def test_start_already_running(self, runner, pid_file):
        _write_pid(pid_file)
        with patch("nexus.commands.mineru.os.kill"), \
             patch("nexus.commands.mineru.subprocess.Popen") as mock_popen:
            result = runner.invoke(main, ["mineru", "start"])
        assert result.exit_code == 0 and "already running" in result.output.lower()
        mock_popen.assert_not_called()

    def test_start_health_timeout(self, runner, pid_file):
        import httpx
        proc = MagicMock(); proc.pid = 99; proc.poll.return_value = None
        with patch("nexus.commands.mineru.subprocess.Popen", return_value=proc), \
             patch("nexus.commands.mineru.httpx.get", side_effect=httpx.ConnectError("refused")), \
             patch("nexus.commands.mineru.time.sleep"), \
             patch("nexus.commands.mineru._HEALTH_TIMEOUT_SECONDS", 0.1), \
             patch("nexus.commands.mineru._find_free_port", return_value=8010), \
             patch("nexus.config.load_config", return_value={}):
            result = runner.invoke(main, ["mineru", "start"])
        assert result.exit_code != 0

    def test_start_binary_not_found(self, runner, pid_file):
        with patch("nexus.commands.mineru.subprocess.Popen",
                    side_effect=FileNotFoundError("mineru-api")), \
             patch("nexus.commands.mineru._find_free_port", return_value=8010), \
             patch("nexus.config.load_config", return_value={}):
            result = runner.invoke(main, ["mineru", "start"])
        assert result.exit_code != 0 and "mineru-api" in result.output.lower()

    def test_start_persists_url_to_config(self, runner, pid_file):
        patches = _mock_start_success(pid=42, port=54321)
        with patches[0], patches[1], patches[2], patches[3], patches[4] as mock_set:
            result = runner.invoke(main, ["mineru", "start"])
        assert result.exit_code == 0
        mock_set.assert_called_once_with("pdf.mineru_server_url", "http://127.0.0.1:54321")

    def test_start_custom_port(self, runner, pid_file):
        proc = MagicMock(); proc.pid = 77; proc.poll.return_value = None
        resp = MagicMock(); resp.status_code = 200
        with patch("nexus.commands.mineru.subprocess.Popen", return_value=proc) as mock_popen, \
             patch("nexus.commands.mineru.httpx.get", return_value=resp), \
             patch("nexus.config.load_config", return_value={}), \
             patch("nexus.config.set_config_value"):
            result = runner.invoke(main, ["mineru", "start", "--port", "9000"])
        assert result.exit_code == 0
        data = json.loads(pid_file.read_text())
        assert data["port"] == 9000
        call_args = mock_popen.call_args[0][0]
        assert "--port" in call_args and call_args[call_args.index("--port") + 1] == "9000"


# ── nx mineru stop ───────────────────────────────────────────────────────────

class TestMineruStop:
    def _kill_tracking(self):
        call_count = 0
        signals: list[int] = []
        def side_effect(pid, sig):
            nonlocal call_count
            signals.append(sig)
            if sig == 0:
                call_count += 1
                if call_count >= 2:
                    raise OSError("No such process")
        return side_effect, signals

    def test_stop_sends_sigterm_to_process_group(self, runner, pid_file):
        """``nx mineru stop`` must SIGTERM the whole process group so
        MinerU's multiprocessing workers' resource_tracker gets a
        chance to ``sem_unlink`` POSIX named semaphores before exit.
        Regression for bead nexus-ze2a.
        """
        _write_pid(pid_file, pid=12345)
        killpg_calls: list[tuple[int, int]] = []

        def _fake_killpg(pgid: int, sig: int) -> None:
            killpg_calls.append((pgid, sig))

        # Initial liveness probe True, then dead after SIGTERM delivery.
        alive_states = [True, False, False, False]

        def _alive(pid: int) -> bool:
            return alive_states.pop(0) if alive_states else False

        with patch("nexus.commands.mineru.os.killpg", side_effect=_fake_killpg), \
             patch("nexus.commands.mineru.os.getpgid", lambda pid: pid), \
             patch("nexus.commands.mineru._is_process_alive", side_effect=_alive), \
             patch("nexus.commands.mineru.time.sleep"):
            result = runner.invoke(main, ["mineru", "stop"])
        assert result.exit_code == 0 and not pid_file.exists()
        assert (12345, signal.SIGTERM) in killpg_calls
        assert (12345, signal.SIGKILL) not in killpg_calls

    def test_stop_no_pid_file(self, runner, pid_file):
        result = runner.invoke(main, ["mineru", "stop"])
        assert result.exit_code == 0 and "not running" in result.output.lower()

    def test_stop_stale_pid(self, runner, pid_file):
        _write_pid(pid_file, pid=99999)
        with patch(
            "nexus.commands.mineru._is_process_alive", return_value=False,
        ):
            result = runner.invoke(main, ["mineru", "stop"])
        assert result.exit_code == 0 and not pid_file.exists()


# ── nx mineru status ─────────────────────────────────────────────────────────

class TestMineruStatus:
    @pytest.mark.parametrize("setup,status_code,expect_text", [
        ("healthy", 200, "healthy|running"),
        ("no_pid", None, "not running"),
        ("unhealthy", 503, "unhealthy|not healthy"),
    ])
    def test_status(self, runner, pid_file, setup, status_code, expect_text):
        if setup == "no_pid":
            result = runner.invoke(main, ["mineru", "status"])
        else:
            _write_pid(pid_file)
            resp = MagicMock(); resp.status_code = status_code
            if status_code == 200:
                resp.json.return_value = {"status": "ok", "active_tasks": 0, "completed_tasks": 5}
            with patch("nexus.commands.mineru.os.kill"), \
                 patch("nexus.commands.mineru.httpx.get", return_value=resp):
                result = runner.invoke(main, ["mineru", "status"])
        assert result.exit_code == 0
        assert any(t in result.output.lower() for t in expect_text.split("|"))
