"""AC1–AC7: nx install/uninstall claude-code, hooks, doctor."""
import json
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


# ── AC1: nx install claude-code ───────────────────────────────────────────────

def test_install_creates_skill_md(runner: CliRunner, fake_home: Path) -> None:
    """nx install claude-code writes SKILL.md with nx command documentation."""
    result = runner.invoke(main, ["install", "claude-code"])
    assert result.exit_code == 0, result.output

    skill_path = fake_home / ".claude" / "skills" / "nexus" / "SKILL.md"
    assert skill_path.exists()
    content = skill_path.read_text()
    # Must document key nx commands
    assert "nx search" in content
    assert "nx memory" in content
    assert "nx pm" in content
    assert "nx store" in content
    assert "nx scratch" in content


def test_install_adds_session_start_hook(runner: CliRunner, fake_home: Path) -> None:
    """nx install claude-code adds SessionStart hook entry to settings.json."""
    result = runner.invoke(main, ["install", "claude-code"])
    assert result.exit_code == 0, result.output

    settings_path = fake_home / ".claude" / "settings.json"
    assert settings_path.exists()
    data = json.loads(settings_path.read_text())
    hooks = data.get("hooks", {})
    start_hooks = hooks.get("SessionStart", [])
    assert any("nx" in str(h) for h in start_hooks), (
        f"No nx SessionStart hook found in {start_hooks}"
    )


def test_install_adds_session_end_hook(runner: CliRunner, fake_home: Path) -> None:
    """nx install claude-code adds SessionEnd hook entry to settings.json."""
    result = runner.invoke(main, ["install", "claude-code"])
    assert result.exit_code == 0, result.output

    settings_path = fake_home / ".claude" / "settings.json"
    data = json.loads(settings_path.read_text())
    end_hooks = data.get("hooks", {}).get("SessionEnd", [])
    assert any("nx" in str(h) for h in end_hooks), (
        f"No nx SessionEnd hook found in {end_hooks}"
    )


def test_install_merges_existing_settings(runner: CliRunner, fake_home: Path) -> None:
    """install preserves pre-existing keys in settings.json."""
    settings_path = fake_home / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps({"theme": "dark", "hooks": {"OtherHook": ["existing"]}}))

    runner.invoke(main, ["install", "claude-code"])

    data = json.loads(settings_path.read_text())
    assert data.get("theme") == "dark"
    assert "OtherHook" in data.get("hooks", {})


# ── AC2: SessionStart — UUID4 session ID ──────────────────────────────────────

def test_session_start_writes_session_file(runner: CliRunner, fake_home: Path) -> None:
    """nx hook session-start writes UUID4 session ID to the getsid-scoped session file."""
    with patch("nexus.session.os.getsid", return_value=12345):
        result = runner.invoke(main, ["hook", "session-start"])
    assert result.exit_code == 0, result.output

    session_file = fake_home / ".config" / "nexus" / "sessions" / "12345.session"
    assert session_file.exists()
    session_id = session_file.read_text().strip()
    import re
    assert re.match(
        r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
        session_id,
    ), f"Not a UUID4: {session_id!r}"


def test_session_start_prints_ready_message(runner: CliRunner, fake_home: Path) -> None:
    """nx hook session-start prints 'Nexus ready' with session ID."""
    result = runner.invoke(main, ["hook", "session-start"])
    assert "Nexus ready" in result.output
    assert "session" in result.output.lower()


# ── Behavior 4: hook and CLI use the same getsid(0) anchor ───────────────────

def test_hook_and_cli_use_same_getsid_anchor(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """session_start() and read_session_id() both resolve to the same session file.

    If the SessionStart hook writes the session file using getsid(0)=X as the
    filename anchor, a CLI command that calls read_session_id() with the same
    getsid(0)=X must find the same file and recover the same session ID.
    """
    from nexus.hooks import session_start
    from nexus.session import read_session_id

    monkeypatch.delenv("NX_SESSION_PID", raising=False)

    with patch("nexus.session.os.getsid", return_value=98765):
        _inner = MagicMock(get=lambda **kw: None, list_entries=lambda **kw: [])
        _t2_cm = MagicMock(__enter__=MagicMock(return_value=_inner))
        with patch("nexus.hooks.T2Database", return_value=_t2_cm):
            session_start()
        recovered = read_session_id()

    assert recovered is not None
    import re
    assert re.match(
        r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
        recovered,
    ), f"Recovered session ID is not a UUID4: {recovered!r}"


# ── AC3: SessionStart PM detection ────────────────────────────────────────────

def test_session_start_pm_detection_injects_computed_resume(
    runner: CliRunner, fake_home: Path
) -> None:
    """SessionStart injects computed PM resume for PM projects (detected via BLOCKERS.md with pm tag)."""
    mock_db = MagicMock()
    # Simulate PM project detected via BLOCKERS.md with pm tag
    mock_db.get.return_value = {
        "content": "# Blockers\n",
        "title": "BLOCKERS.md",
        "tags": "pm,blockers",
    }
    # Simulate repo name detection
    _t2_cm = MagicMock(__enter__=MagicMock(return_value=mock_db))
    with patch("nexus.hooks._infer_repo", return_value="myrepo"):
        with patch("nexus.hooks.T2Database", return_value=_t2_cm):
            with patch("nexus.pm.pm_resume", return_value="## PM Resume: myrepo\nPhase: 3"):
                result = runner.invoke(main, ["hook", "session-start"])

    assert "Phase: 3" in result.output or "myrepo" in result.output


# ── AC4: SessionStart non-PM memory summary ───────────────────────────────────

def test_session_start_non_pm_outputs_memory_summary(
    runner: CliRunner, fake_home: Path
) -> None:
    """SessionStart outputs recent memory summary for non-PM projects."""
    mock_db = MagicMock()
    mock_db.get.return_value = None  # No PM project
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
    runner: CliRunner, fake_home: Path
) -> None:
    """SessionEnd flushes T1 flagged entries to T2."""
    # Create session file so session_end can find the session ID
    with patch("nexus.session.os.getsid", return_value=1):
        session_file = fake_home / ".config" / "nexus" / "sessions" / "1.session"
        session_file.parent.mkdir(parents=True, exist_ok=True)
        session_file.write_text("test-session-id")

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
        with patch("nexus.hooks._open_t1", return_value=mock_t1):
            with patch("nexus.hooks.T2Database", return_value=_t2_cm):
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
    runner: CliRunner, fake_home: Path
) -> None:
    """SessionEnd clears T1 and removes the session file."""
    mock_t1 = MagicMock()
    mock_t1.flagged_entries.return_value = []

    with patch("nexus.session.os.getsid", return_value=42):
        session_file = fake_home / ".config" / "nexus" / "sessions" / "42.session"
        session_file.parent.mkdir(parents=True, exist_ok=True)
        session_file.write_text("test-session-id")

        _t2_cm = MagicMock(__enter__=MagicMock(return_value=MagicMock()))
        with patch("nexus.hooks._open_t1", return_value=mock_t1):
            with patch("nexus.hooks.T2Database", return_value=_t2_cm):
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
    # Exit 0 when all creds present, 1 when some are missing (both valid outcomes)
    assert result.exit_code in (0, 1), result.output
    # Should check: ChromaDB, Voyage AI, Anthropic, ripgrep, git
    output_lower = result.output.lower()
    assert "chroma" in output_lower or "chromadb" in output_lower
    assert "voyage" in output_lower
    assert "anthropic" in output_lower
    assert "ripgrep" in output_lower or "rg" in output_lower
    assert "git" in output_lower


def test_doctor_missing_voyage_key_reports_warning(
    runner: CliRunner, fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """nx doctor reports warning and exits 1 when VOYAGE_API_KEY is unset."""
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    result = runner.invoke(main, ["doctor"])
    assert result.exit_code == 1
    assert "VOYAGE_API_KEY" in result.output or "voyage" in result.output.lower()


def test_doctor_missing_chroma_key_reports_warning(
    runner: CliRunner, fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """nx doctor reports warning and exits 1 when CHROMA_API_KEY is unset."""
    monkeypatch.delenv("CHROMA_API_KEY", raising=False)
    result = runner.invoke(main, ["doctor"])
    assert result.exit_code == 1
    assert "CHROMA_API_KEY" in result.output or "chroma" in result.output.lower()


def test_doctor_ripgrep_present(runner: CliRunner, fake_home: Path) -> None:
    """nx doctor checks for ripgrep on PATH."""
    with patch("nexus.commands.doctor.shutil.which", return_value="/usr/bin/rg"):
        result = runner.invoke(main, ["doctor"])
    assert "rg" in result.output or "ripgrep" in result.output.lower()


# ── AC7: nx uninstall claude-code ────────────────────────────────────────────

def test_uninstall_removes_skill_md(runner: CliRunner, fake_home: Path) -> None:
    """nx uninstall claude-code removes SKILL.md."""
    runner.invoke(main, ["install", "claude-code"])
    skill_path = fake_home / ".claude" / "skills" / "nexus" / "SKILL.md"
    assert skill_path.exists()

    result = runner.invoke(main, ["uninstall", "claude-code"])
    assert result.exit_code == 0, result.output
    assert not skill_path.exists()


def test_uninstall_removes_hook_entries(runner: CliRunner, fake_home: Path) -> None:
    """nx uninstall claude-code removes hook entries from settings.json."""
    runner.invoke(main, ["install", "claude-code"])

    result = runner.invoke(main, ["uninstall", "claude-code"])
    assert result.exit_code == 0, result.output

    settings_path = fake_home / ".claude" / "settings.json"
    data = json.loads(settings_path.read_text())
    start_hooks = data.get("hooks", {}).get("SessionStart", [])
    end_hooks = data.get("hooks", {}).get("SessionEnd", [])
    assert not any("nx" in str(h) for h in start_hooks)
    assert not any("nx" in str(h) for h in end_hooks)


def test_uninstall_preserves_other_hooks(runner: CliRunner, fake_home: Path) -> None:
    """nx uninstall only removes nx hooks, preserves other hook entries."""
    settings_path = fake_home / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps({
        "hooks": {
            "SessionStart": [{"command": "other-tool start"}],
        }
    }))

    runner.invoke(main, ["install", "claude-code"])
    runner.invoke(main, ["uninstall", "claude-code"])

    data = json.loads(settings_path.read_text())
    start_hooks = data.get("hooks", {}).get("SessionStart", [])
    assert any("other-tool" in str(h) for h in start_hooks)
