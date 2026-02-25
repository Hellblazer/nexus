"""nx install/uninstall claude-code — integration management tests."""
import json
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from nexus.commands.install import (
    _load_settings,
    _nx_hook_entry,
    _save_settings,
    _warn_if_transient_install,
)


# ── Settings helpers ─────────────────────────────────────────────────────────

def test_load_settings_missing_file(tmp_path: Path) -> None:
    """Missing settings.json returns empty dict."""
    with patch("nexus.commands.install._settings_path", return_value=tmp_path / "nope.json"):
        assert _load_settings() == {}


def test_load_settings_corrupt_json(tmp_path: Path) -> None:
    """Corrupt JSON returns empty dict."""
    path = tmp_path / "settings.json"
    path.write_text("{invalid json")
    with patch("nexus.commands.install._settings_path", return_value=path):
        assert _load_settings() == {}


def test_load_settings_valid(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text('{"hooks": {}}')
    with patch("nexus.commands.install._settings_path", return_value=path):
        assert _load_settings() == {"hooks": {}}


def test_save_settings_creates_parent(tmp_path: Path) -> None:
    path = tmp_path / "sub" / "settings.json"
    with patch("nexus.commands.install._settings_path", return_value=path):
        _save_settings({"key": "value"})
    assert path.exists()
    assert json.loads(path.read_text()) == {"key": "value"}


def test_nx_hook_entry_format() -> None:
    assert _nx_hook_entry("nx hook session-start") == {"command": "nx hook session-start"}


# ── Install command ──────────────────────────────────────────────────────────

def test_install_writes_skill_and_hooks(tmp_path: Path) -> None:
    """install claude-code writes SKILL.md and adds hook entries."""
    from nexus.cli import main

    settings_path = tmp_path / "settings.json"
    skill_path = tmp_path / "skills" / "nexus" / "SKILL.md"

    with (
        patch("nexus.commands.install._settings_path", return_value=settings_path),
        patch("nexus.commands.install._SKILL_MD_PATH", tmp_path / "source_skill.md"),
        patch("nexus.commands.install._warn_if_transient_install"),
    ):
        # Create the source SKILL.md
        source = tmp_path / "source_skill.md"
        source.write_text("# Nexus Skill")

        # Override the skill_path in the command to use tmp_path
        with patch("nexus.commands.install.Path.home", return_value=tmp_path):
            runner = CliRunner()
            result = runner.invoke(main, ["install", "claude-code"])

    assert result.exit_code == 0, result.output
    assert "installed" in result.output.lower()

    # Verify settings.json has hooks
    data = json.loads(settings_path.read_text())
    hooks = data.get("hooks", {})
    session_start_cmds = [e["command"] for e in hooks.get("SessionStart", []) if isinstance(e, dict)]
    session_end_cmds = [e["command"] for e in hooks.get("SessionEnd", []) if isinstance(e, dict)]
    assert "nx hook session-start" in session_start_cmds
    assert "nx hook session-end" in session_end_cmds


def test_install_idempotent(tmp_path: Path) -> None:
    """Running install twice does not duplicate hooks."""
    settings_path = tmp_path / "settings.json"
    existing = {
        "hooks": {
            "SessionStart": [{"command": "nx hook session-start"}],
            "SessionEnd": [{"command": "nx hook session-end"}],
        }
    }
    settings_path.write_text(json.dumps(existing))

    with (
        patch("nexus.commands.install._settings_path", return_value=settings_path),
        patch("nexus.commands.install._SKILL_MD_PATH", tmp_path / "source.md"),
        patch("nexus.commands.install._warn_if_transient_install"),
        patch("nexus.commands.install.Path.home", return_value=tmp_path),
    ):
        (tmp_path / "source.md").write_text("# Skill")
        from nexus.cli import main

        runner = CliRunner()
        runner.invoke(main, ["install", "claude-code"])

    data = json.loads(settings_path.read_text())
    # Should still have exactly 1 entry each, not 2
    assert len(data["hooks"]["SessionStart"]) == 1
    assert len(data["hooks"]["SessionEnd"]) == 1


# ── Uninstall command ────────────────────────────────────────────────────────

def test_uninstall_removes_hooks(tmp_path: Path) -> None:
    """uninstall claude-code removes nx hook entries from settings."""
    settings_path = tmp_path / "settings.json"
    existing = {
        "hooks": {
            "SessionStart": [
                {"command": "nx hook session-start"},
                {"command": "some-other-hook"},
            ],
            "SessionEnd": [{"command": "nx hook session-end"}],
        }
    }
    settings_path.write_text(json.dumps(existing))

    with (
        patch("nexus.commands.install._settings_path", return_value=settings_path),
        patch("nexus.commands.install.Path.home", return_value=tmp_path),
    ):
        from nexus.cli import main

        runner = CliRunner()
        result = runner.invoke(main, ["uninstall", "claude-code"])

    assert result.exit_code == 0, result.output
    data = json.loads(settings_path.read_text())
    # nx hooks removed, other hooks preserved
    session_start = data["hooks"]["SessionStart"]
    assert len(session_start) == 1
    assert session_start[0]["command"] == "some-other-hook"
    assert data["hooks"]["SessionEnd"] == []


def test_uninstall_no_hooks_present(tmp_path: Path) -> None:
    """uninstall when no nx hooks exist completes gracefully."""
    settings_path = tmp_path / "settings.json"
    settings_path.write_text('{"hooks": {"SessionStart": [{"command": "other-hook"}]}}')

    with (
        patch("nexus.commands.install._settings_path", return_value=settings_path),
        patch("nexus.commands.install.Path.home", return_value=tmp_path),
    ):
        from nexus.cli import main

        runner = CliRunner()
        result = runner.invoke(main, ["uninstall", "claude-code"])

    assert result.exit_code == 0
    assert "No nx hook entries" in result.output


# ── SKILL.md missing ─────────────────────────────────────────────────────────

def test_read_skill_md_missing() -> None:
    """_read_skill_md raises ClickException when SKILL.md not found."""
    import click

    from nexus.commands.install import _read_skill_md

    with patch("nexus.commands.install._SKILL_MD_PATH", Path("/nonexistent/SKILL.md")):
        with patch.object(Path, "exists", return_value=False):
            import pytest

            with pytest.raises(click.ClickException, match="SKILL.md not found"):
                _read_skill_md()


# ── Transient install warning ────────────────────────────────────────────────

def test_warn_if_transient_venv(capsys) -> None:
    """Warns when nx is running from a .venv path."""
    with patch("nexus.commands.install.shutil.which", return_value="/home/user/project/.venv/bin/nx"):
        _warn_if_transient_install()

    captured = capsys.readouterr()
    assert "Warning" in captured.err


def test_warn_if_not_transient(capsys) -> None:
    """No warning when nx is on a normal PATH."""
    with patch("nexus.commands.install.shutil.which", return_value="/usr/local/bin/nx"):
        _warn_if_transient_install()

    captured = capsys.readouterr()
    assert "Warning" not in captured.err
