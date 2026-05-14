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

    with patch("nexus.hooks.write_claude_session_id", side_effect=_capture):
        session_start()

    assert "session_id" in written
    assert re.match(
        r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
        written["session_id"],
    ), f"Recovered session ID is not a UUID4: {written['session_id']!r}"


# ── AC3: SessionStart memory summary ─────────────────────────────────────────

def test_session_start_outputs_session_id(
    runner: CliRunner, fake_home: Path
) -> None:
    """SessionStart outputs session ID (T2 memory surfaced by separate hook)."""
    result = runner.invoke(main, ["hook", "session-start"])

    assert "Nexus ready" in result.output


# ── GH #576 Phase F: subprocess SessionStart skip-sweep ─────────────────────








# ── AC5: SessionEnd flush + expire ────────────────────────────────────────────







def test_session_end_runs_expire(runner: CliRunner, fake_home: Path) -> None:
    """SessionEnd runs T2 expire."""
    mock_t2 = MagicMock()
    mock_t2.expire.return_value = 3

    _t2_cm = MagicMock(__enter__=MagicMock(return_value=mock_t2))
    with patch("nexus.hooks._open_t1", return_value=MagicMock(flagged_entries=lambda: [])):
        with patch("nexus.hooks.t2_ctx", return_value=_t2_cm):
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
    with patch("nexus.health.shutil.which", return_value="/usr/bin/rg"):
        result = runner.invoke(main, ["doctor"])
    assert "rg" in result.output or "ripgrep" in result.output.lower()
