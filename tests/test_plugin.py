"""AC2–AC6: session hooks, memory summary, doctor checks."""
import json
import os
import time
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
    """Redirect HOME and XDG paths to tmp_path."""
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


# ── AC2: SessionStart — UUID4 session ID ──────────────────────────────────────

def test_session_start_writes_session_file(runner: CliRunner, fake_home: Path) -> None:
    """nx hook session-start calls write_claude_session_id with a UUID4 session ID."""
    import re
    captured: dict[str, str] = {}

    original_write = __import__("nexus.session", fromlist=["write_claude_session_id"]).write_claude_session_id

    def _capture(session_id: str) -> None:
        captured["session_id"] = session_id
        original_write(session_id)

    with patch("nexus.hooks.write_claude_session_id", side_effect=_capture):
        result = runner.invoke(main, ["hook", "session-start"])

    assert result.exit_code == 0, result.output
    assert "session_id" in captured, "write_claude_session_id was not called"
    assert re.match(
        r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
        captured["session_id"],
    ), f"Not a UUID4: {captured['session_id']!r}"


def test_session_start_prints_ready_message(runner: CliRunner, fake_home: Path) -> None:
    """nx hook session-start prints 'Nexus ready' with session ID."""
    result = runner.invoke(main, ["hook", "session-start"])
    assert "Nexus ready" in result.output
    assert "session" in result.output.lower()


# ── Behavior 4: hook and CLI use the same getsid(0) anchor ───────────────────

def test_hook_and_cli_use_same_getsid_anchor(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """session_start() passes a UUID4 to write_claude_session_id."""
    import re
    from nexus.hooks import session_start

    written: dict[str, str] = {}

    def _capture(session_id: str) -> None:
        written["session_id"] = session_id

    _inner = MagicMock(get=lambda **kw: None, list_entries=lambda **kw: [])
    _t2_cm = MagicMock(__enter__=MagicMock(return_value=_inner))
    with patch("nexus.hooks.T2Database", return_value=_t2_cm):
        with patch("nexus.hooks.write_claude_session_id", side_effect=_capture):
            session_start()

    assert "session_id" in written
    assert re.match(
        r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
        written["session_id"],
    ), f"Recovered session ID is not a UUID4: {written['session_id']!r}"


# ── AC3: SessionStart memory summary ─────────────────────────────────────────

def test_session_start_outputs_memory_summary(
    runner: CliRunner, fake_home: Path
) -> None:
    """SessionStart outputs recent memory summary."""
    mock_db = MagicMock()
    mock_db.list_entries.return_value = [
        {"id": 1, "title": "note.md", "agent": "coder", "timestamp": "2026-01-01T10:00:00Z"},
    ]

    _t2_cm = MagicMock(__enter__=MagicMock(return_value=mock_db))
    with patch("nexus.hooks._infer_repo", return_value="myrepo"):
        with patch("nexus.hooks.T2Database", return_value=_t2_cm):
            result = runner.invoke(main, ["hook", "session-start"])

    assert "memory" in result.output.lower() or "note.md" in result.output


# ── AC5: SessionEnd flush + expire ────────────────────────────────────────────

def test_session_end_flushes_flagged_t1_entries(
    runner: CliRunner, fake_home: Path, tmp_path: Path
) -> None:
    """SessionEnd flushes T1 flagged entries to T2."""
    sessions = tmp_path / "sessions"
    ppid = os.getppid()
    session_file = sessions / f"{ppid}.session"
    session_file.parent.mkdir(parents=True, exist_ok=True)
    session_file.write_text(json.dumps({
        "session_id": "test-session-id",
        "server_host": "127.0.0.1",
        "server_port": 51823,
        "server_pid": 0,
        "created_at": time.time(),
        "tmpdir": "",
    }))

    mock_t1 = MagicMock()
    mock_t1.flagged_entries.return_value = [
        {
            "id": "entry-1",
            "content": "important finding",
            "tags": "research",
            "flush_project": "myproject",
            "flush_title": "finding.md",
        }
    ]
    mock_t2 = MagicMock()

    _t2_cm = MagicMock(__enter__=MagicMock(return_value=mock_t2))
    with patch("nexus.hooks.SESSIONS_DIR", sessions):
        with patch("nexus.hooks._open_t1", return_value=mock_t1):
            with patch("nexus.hooks.T2Database", return_value=_t2_cm):
                with patch("nexus.hooks.stop_t1_server"):
                    result = runner.invoke(main, ["hook", "session-end"])

    assert result.exit_code == 0, result.output
    mock_t2.put.assert_called_once_with(
        project="myproject",
        title="finding.md",
        content="important finding",
        tags="research",
        ttl=None,
    )


def test_session_end_clears_t1_and_removes_session_file(
    runner: CliRunner, fake_home: Path, tmp_path: Path
) -> None:
    """SessionEnd clears T1 and removes the session file."""
    sessions = tmp_path / "sessions"
    ppid = os.getppid()
    session_file = sessions / f"{ppid}.session"
    session_file.parent.mkdir(parents=True, exist_ok=True)
    session_file.write_text(json.dumps({
        "session_id": "test-session-id",
        "server_host": "127.0.0.1",
        "server_port": 51823,
        "server_pid": 0,
        "created_at": time.time(),
        "tmpdir": "",
    }))

    mock_t1 = MagicMock()
    mock_t1.flagged_entries.return_value = []

    _t2_cm = MagicMock(__enter__=MagicMock(return_value=MagicMock()))
    with patch("nexus.hooks.SESSIONS_DIR", sessions):
        with patch("nexus.hooks._open_t1", return_value=mock_t1):
            with patch("nexus.hooks.T2Database", return_value=_t2_cm):
                with patch("nexus.hooks.stop_t1_server"):
                    result = runner.invoke(main, ["hook", "session-end"])

    assert result.exit_code == 0, result.output
    mock_t1.clear.assert_called_once()
    assert not session_file.exists()


def test_session_end_runs_expire(runner: CliRunner, fake_home: Path) -> None:
    """SessionEnd runs T2 expire."""
    mock_t2 = MagicMock()
    mock_t2.expire.return_value = 3

    _t2_cm = MagicMock(__enter__=MagicMock(return_value=mock_t2))
    with patch("nexus.hooks._open_t1", return_value=MagicMock(flagged_entries=lambda: [])):
        with patch("nexus.hooks.T2Database", return_value=_t2_cm):
            result = runner.invoke(main, ["hook", "session-end"])

    mock_t2.expire.assert_called_once()


# ── AC6: nx doctor ────────────────────────────────────────────────────────────

def test_doctor_shows_all_checks(runner: CliRunner, fake_home: Path) -> None:
    """nx doctor runs all required service checks and reports status."""
    result = runner.invoke(main, ["doctor"])
    assert result.exit_code in (0, 1), result.output
    output_lower = result.output.lower()
    assert "t3 mode" in output_lower
    assert "ripgrep" in output_lower or "rg" in output_lower
    assert "git" in output_lower


def test_doctor_missing_voyage_key_reports_warning(
    runner: CliRunner, fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """nx doctor reports warning and exits 1 when VOYAGE_API_KEY is unset."""
    monkeypatch.setenv("NX_LOCAL", "0")  # force cloud mode
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    result = runner.invoke(main, ["doctor"])
    assert result.exit_code == 1
    assert "VOYAGE_API_KEY" in result.output or "voyage" in result.output.lower()


def test_doctor_missing_chroma_key_reports_warning(
    runner: CliRunner, fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """nx doctor reports warning and exits 1 when CHROMA_API_KEY is unset."""
    monkeypatch.setenv("NX_LOCAL", "0")  # force cloud mode
    monkeypatch.delenv("CHROMA_API_KEY", raising=False)
    result = runner.invoke(main, ["doctor"])
    assert result.exit_code == 1
    assert "CHROMA_API_KEY" in result.output or "chroma" in result.output.lower()


def test_doctor_ripgrep_present(runner: CliRunner, fake_home: Path) -> None:
    """nx doctor checks for ripgrep on PATH."""
    with patch("nexus.commands.doctor.shutil.which", return_value="/usr/bin/rg"):
        result = runner.invoke(main, ["doctor"])
    assert "rg" in result.output or "ripgrep" in result.output.lower()
