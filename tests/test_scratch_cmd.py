"""CLI-layer tests for nx scratch commands — covers wiring gaps in commands/scratch.py."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import uuid4

import click
import pytest
from click.testing import CliRunner

from nexus.cli import main


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect HOME so T1 uses a tmp directory, not ~/.config/nexus/scratch.

    Also patches CLAUDE_SESSION_FILE to a per-test path (it's precomputed at
    import time so HOME env var alone is insufficient) and seeds a unique
    session ID so EphemeralClient fallback doesn't leak state between tests.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("NX_SESSION_PID", raising=False)
    # Redirect the precomputed CLAUDE_SESSION_FILE to a per-test path.
    claude_session_file = tmp_path / ".config" / "nexus" / "current_session"
    claude_session_file.parent.mkdir(parents=True, exist_ok=True)
    # Write a unique session ID so each test gets isolated scratch state.
    claude_session_file.write_text(str(uuid4()))
    monkeypatch.setattr("nexus.session.CLAUDE_SESSION_FILE", claude_session_file)
    return tmp_path


# ── put from stdin ────────────────────────────────────────────────────────────

def test_scratch_put_from_stdin(runner: CliRunner, fake_home: Path) -> None:
    """When content is '-', scratch put reads from stdin."""
    with patch("nexus.session.os.getsid", return_value=99900):
        result = runner.invoke(main, ["scratch", "put", "-"], input="hello from stdin\n")

    assert result.exit_code == 0, result.output
    assert "Stored:" in result.output


# ── get success ──────────────────────────────────────────────────────────────

def test_scratch_get_success(runner: CliRunner, fake_home: Path) -> None:
    """get with a valid ID echoes the content of the scratch entry."""
    with patch("nexus.session.os.getsid", return_value=99910):
        # Put an entry and capture the ID
        put_result = runner.invoke(main, ["scratch", "put", "hello scratch world"])
        assert put_result.exit_code == 0, put_result.output
        doc_id = put_result.output.strip().split("Stored: ")[1]

        # Get it back
        get_result = runner.invoke(main, ["scratch", "get", doc_id])
        assert get_result.exit_code == 0, get_result.output
        assert "hello scratch world" in get_result.output


# ── get missing entry ─────────────────────────────────────────────────────────

def test_scratch_get_missing_entry_shows_error(runner: CliRunner, fake_home: Path) -> None:
    """get with non-existent ID raises ClickException (exit code 1, 'Not found')."""
    with patch("nexus.session.os.getsid", return_value=99901):
        result = runner.invoke(main, ["scratch", "get", "nonexistent-id-000"])

    assert result.exit_code != 0
    assert "not found" in result.output.lower()


# ── search no results ─────────────────────────────────────────────────────────

def test_scratch_search_no_results(runner: CliRunner, fake_home: Path) -> None:
    """search with no matches shows 'No results.' message."""
    with patch("nexus.session.os.getsid", return_value=99902):
        result = runner.invoke(main, ["scratch", "search", "nonexistent query"])

    assert result.exit_code == 0
    assert "No results." in result.output


# ── flag nonexistent ──────────────────────────────────────────────────────────

def test_scratch_flag_nonexistent_raises(runner: CliRunner, fake_home: Path) -> None:
    """flag with bad ID raises ClickException."""
    with patch("nexus.session.os.getsid", return_value=99903):
        result = runner.invoke(main, ["scratch", "flag", "bad-id-000"])

    assert result.exit_code != 0
    assert "No scratch entry" in result.output


# ── unflag success ────────────────────────────────────────────────────────────

def test_scratch_unflag_success(runner: CliRunner, fake_home: Path) -> None:
    """unflag command works on a previously-flagged entry."""
    with patch("nexus.session.os.getsid", return_value=99904):
        # Put an entry, capture the ID
        put_result = runner.invoke(main, ["scratch", "put", "will be flagged"])
        assert put_result.exit_code == 0, put_result.output
        doc_id = put_result.output.strip().split("Stored: ")[1]

        # Flag it
        flag_result = runner.invoke(main, ["scratch", "flag", doc_id])
        assert flag_result.exit_code == 0, flag_result.output
        assert "Flagged:" in flag_result.output

        # Unflag it
        unflag_result = runner.invoke(main, ["scratch", "unflag", doc_id])
        assert unflag_result.exit_code == 0, unflag_result.output
        assert "Unflagged:" in unflag_result.output


# ── unflag nonexistent ────────────────────────────────────────────────────────

def test_scratch_unflag_nonexistent_raises(runner: CliRunner, fake_home: Path) -> None:
    """unflag with bad ID raises ClickException."""
    with patch("nexus.session.os.getsid", return_value=99905):
        result = runner.invoke(main, ["scratch", "unflag", "bad-id-000"])

    assert result.exit_code != 0
    assert "No scratch entry" in result.output


# ── promote success ───────────────────────────────────────────────────────────

def test_scratch_promote_success(runner: CliRunner, fake_home: Path) -> None:
    """promote command copies T1 entry to T2 successfully."""
    with patch("nexus.session.os.getsid", return_value=99906):
        # Put an entry
        put_result = runner.invoke(main, ["scratch", "put", "promote me"])
        assert put_result.exit_code == 0, put_result.output
        doc_id = put_result.output.strip().split("Stored: ")[1]

        # Promote it to T2
        promote_result = runner.invoke(
            main, ["scratch", "promote", doc_id, "-p", "testproj", "-t", "notes.md"]
        )
        assert promote_result.exit_code == 0, promote_result.output
        assert "Promoted" in promote_result.output
        assert "testproj/notes.md" in promote_result.output


# ── promote nonexistent ──────────────────────────────────────────────────────

def test_scratch_promote_nonexistent_raises(runner: CliRunner, fake_home: Path) -> None:
    """promote with bad ID raises ClickException."""
    with patch("nexus.session.os.getsid", return_value=99907):
        result = runner.invoke(
            main, ["scratch", "promote", "bad-id-000", "-p", "proj", "-t", "t.md"]
        )

    assert result.exit_code != 0
    assert "No scratch entry" in result.output
