"""Session hook tests: session_start and session_end lifecycle."""
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from nexus.hooks import session_end, session_start


# ── session_start ────────────────────────────────────────────────────────────

@patch("nexus.hooks.write_session_file")
@patch("nexus.hooks.generate_session_id", return_value="test-uuid")
@patch("nexus.hooks._infer_repo", return_value="myrepo")
def test_session_start_no_pm_no_entries(mock_repo, mock_sid, mock_write, tmp_path: Path) -> None:
    """Non-PM repo with no memory entries outputs fallback message."""
    db_path = tmp_path / "memory.db"
    with patch("nexus.hooks._default_db_path", return_value=db_path):
        output = session_start()

    assert "test-uuid" in output
    assert "No memory entries" in output
    mock_write.assert_called_once()


@patch("nexus.hooks.write_session_file")
@patch("nexus.hooks.generate_session_id", return_value="test-uuid")
@patch("nexus.hooks._infer_repo", return_value="myrepo")
def test_session_start_with_memory_entries(mock_repo, mock_sid, mock_write, tmp_path: Path) -> None:
    """Non-PM repo with memory entries lists them."""
    from nexus.db.t2 import T2Database

    db_path = tmp_path / "memory.db"
    with T2Database(db_path) as db:
        db.put(project="myrepo", title="findings.md", content="some content")

    with patch("nexus.hooks._default_db_path", return_value=db_path):
        output = session_start()

    assert "findings.md" in output
    assert "Recent memory" in output


@patch("nexus.hooks.write_session_file")
@patch("nexus.hooks.generate_session_id", return_value="test-uuid")
@patch("nexus.hooks._infer_repo", return_value="myrepo")
def test_session_start_db_unavailable(mock_repo, mock_sid, mock_write) -> None:
    """When T2 database raises, outputs graceful fallback."""
    import sqlite3

    with patch("nexus.hooks._open_t2", side_effect=sqlite3.Error("disk I/O error")):
        output = session_start()

    assert "memory unavailable" in output


@patch("nexus.hooks.write_session_file")
@patch("nexus.hooks.generate_session_id", return_value="test-uuid")
@patch("nexus.hooks._infer_repo", return_value="myrepo")
def test_session_start_pm_project(mock_repo, mock_sid, mock_write, tmp_path: Path) -> None:
    """PM project triggers pm_resume instead of memory listing."""
    from nexus.db.t2 import T2Database

    db_path = tmp_path / "memory.db"
    with T2Database(db_path) as db:
        db.put(project="myrepo", title="BLOCKERS.md", content="no blockers", tags="pm")

    with patch("nexus.hooks._default_db_path", return_value=db_path):
        output = session_start()

    # PM resume should have been called (it won't find full PM structure,
    # but it shouldn't crash)
    assert "test-uuid" in output


# ── session_end ──────────────────────────────────────────────────────────────

def test_session_end_no_session_file(tmp_path: Path) -> None:
    """When session file doesn't exist, session_end completes gracefully."""
    session_file = tmp_path / "sessions" / "12345.session"
    db_path = tmp_path / "memory.db"

    with (
        patch("nexus.hooks.session_file_path", return_value=session_file),
        patch("nexus.hooks._default_db_path", return_value=db_path),
    ):
        output = session_end()

    assert "Flushed 0" in output
    assert "Expired 0" in output


def test_session_end_with_session_file(tmp_path: Path) -> None:
    """When session file exists, reads session ID and cleans up."""
    session_file = tmp_path / "test.session"
    session_file.write_text("test-session-uuid")
    db_path = tmp_path / "memory.db"

    mock_t1 = MagicMock()
    mock_t1.flagged_entries.return_value = []

    with (
        patch("nexus.hooks.session_file_path", return_value=session_file),
        patch("nexus.hooks._default_db_path", return_value=db_path),
        patch("nexus.hooks._open_t1", return_value=mock_t1),
    ):
        output = session_end()

    assert "Flushed 0" in output
    assert not session_file.exists()  # cleaned up


def test_session_end_flushes_flagged_entries(tmp_path: Path) -> None:
    """Flagged T1 entries are flushed to T2."""
    from nexus.db.t2 import T2Database

    session_file = tmp_path / "test.session"
    session_file.write_text("test-session-uuid")
    db_path = tmp_path / "memory.db"

    mock_t1 = MagicMock()
    mock_t1.flagged_entries.return_value = [
        {"content": "hypothesis A", "flush_project": "proj", "flush_title": "hyp.md", "tags": ""},
    ]

    with (
        patch("nexus.hooks.session_file_path", return_value=session_file),
        patch("nexus.hooks._default_db_path", return_value=db_path),
        patch("nexus.hooks._open_t1", return_value=mock_t1),
    ):
        output = session_end()

    assert "Flushed 1" in output
    mock_t1.clear.assert_called_once()

    # Verify entry landed in T2
    with T2Database(db_path) as db:
        entry = db.get(project="proj", title="hyp.md")
    assert entry is not None
    assert entry["content"] == "hypothesis A"


def test_session_end_db_error_doesnt_crash(tmp_path: Path) -> None:
    """Storage errors during flush are caught gracefully."""
    session_file = tmp_path / "test.session"
    session_file.write_text("test-uuid")

    # Point to unwritable location
    bad_path = tmp_path / "nonexistent_dir" / "memory.db"

    with (
        patch("nexus.hooks.session_file_path", return_value=session_file),
        patch("nexus.hooks._default_db_path", return_value=bad_path),
    ):
        output = session_end()

    # Should not crash, and session file should still be cleaned up
    assert "Session ended" in output


# ── _infer_repo ──────────────────────────────────────────────────────────────

def test_infer_repo_git_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When not in a git repo, falls back to cwd name."""
    from nexus.hooks import _infer_repo

    monkeypatch.chdir(tmp_path)
    name = _infer_repo()
    assert name == tmp_path.name
