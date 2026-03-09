# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for doctor.py Steps 5–7: orphan T1 detection, T2 integrity, ChromaDB pagination."""
import json
import os
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import chromadb
import pytest

from nexus.commands.doctor import (
    _check_orphan_t1,
    _check_t2_integrity,
    _check_chroma_pagination,
)
from nexus.db.t2 import T2Database


# ── Step 5: Orphan T1 process detection ──────────────────────────────────────

class TestCheckOrphanT1:
    """Tests for _check_orphan_t1."""

    def _make_session_file(self, sessions_dir: Path, name: str, pid: int) -> Path:
        """Write a minimal JSON session record with the given pid."""
        record = {
            "session_id": "test-session",
            "server_host": "127.0.0.1",
            "server_port": 12345,
            "server_pid": pid,
            "created_at": 9999999999.0,
        }
        path = sessions_dir / name
        path.write_text(json.dumps(record))
        return path

    def test_no_sessions_dir_reports_ok(self, tmp_path: Path) -> None:
        """When sessions dir does not exist, check reports ok and returns True."""
        missing_dir = tmp_path / "sessions"
        lines: list[str] = []
        with patch("nexus.commands.doctor.SESSIONS_DIR", missing_dir):
            result = _check_orphan_t1(lines)
        assert result is True
        assert len(lines) == 1
        assert "✓" in lines[0]
        assert "no sessions directory" in lines[0]

    def test_empty_sessions_dir_reports_ok(self, tmp_path: Path) -> None:
        """When sessions dir exists but has no *.session files, check reports ok."""
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        lines: list[str] = []
        with patch("nexus.commands.doctor.SESSIONS_DIR", sessions_dir):
            result = _check_orphan_t1(lines)
        assert result is True
        assert "no session files" in lines[0]

    def test_live_process_session_reports_ok(self, tmp_path: Path) -> None:
        """A session file with our own PID (definitely live) reports ✓."""
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        self._make_session_file(sessions_dir, "99999.session", os.getpid())
        lines: list[str] = []
        with patch("nexus.commands.doctor.SESSIONS_DIR", sessions_dir):
            result = _check_orphan_t1(lines)
        assert result is True
        assert "✓" in lines[0]
        assert "all processes live" in lines[0]

    def test_dead_pid_session_detected_as_orphan(self, tmp_path: Path) -> None:
        """A session file with a dead PID is detected as an orphan."""
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        # PID 2 is init/systemd's child and PID 1 belongs to init — neither
        # can be killed by a non-root user; use a clearly dead PID instead.
        # We use a PID that is very unlikely to be alive: 2**22 - 1 (max on Linux).
        dead_pid = (2 ** 22) - 1
        self._make_session_file(sessions_dir, f"{dead_pid}.session", dead_pid)
        lines: list[str] = []
        with patch("nexus.commands.doctor.SESSIONS_DIR", sessions_dir):
            result = _check_orphan_t1(lines)
        # If the PID happens to be live (extremely unlikely), we cannot assert orphan.
        # But if dead (expected), check the output.
        try:
            os.kill(dead_pid, 0)
            pid_alive = True
        except OSError:
            pid_alive = False

        if not pid_alive:
            assert result is False
            assert "✗" in lines[0]
            assert "orphaned" in lines[0]
            assert "1 orphaned" in lines[0]
            # Fix hint should be appended
            assert any("rm" in line for line in lines)

    def test_corrupt_session_file_skipped(self, tmp_path: Path) -> None:
        """Corrupt (non-JSON) session files are skipped, not counted as orphans."""
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        (sessions_dir / "corrupt.session").write_text("not-json{{{")
        lines: list[str] = []
        with patch("nexus.commands.doctor.SESSIONS_DIR", sessions_dir):
            result = _check_orphan_t1(lines)
        # Corrupt files are skipped; no valid sessions remain → "no session files"
        # is not quite right here — the glob finds the file, so we get "all live"
        # because the corrupt file has no valid pid entry.
        assert result is True

    def test_session_without_server_pid_skipped(self, tmp_path: Path) -> None:
        """Session files missing server_pid field are skipped silently."""
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        record = {"session_id": "abc", "server_host": "127.0.0.1", "server_port": 1234}
        (sessions_dir / "nopid.session").write_text(json.dumps(record))
        lines: list[str] = []
        with patch("nexus.commands.doctor.SESSIONS_DIR", sessions_dir):
            result = _check_orphan_t1(lines)
        assert result is True

    def test_multiple_orphans_count_reported(self, tmp_path: Path) -> None:
        """When multiple session files have dead PIDs, the count is reported."""
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        # Two definitely-dead PIDs
        dead_pids = [(2 ** 22) - 1, (2 ** 22) - 2]
        alive_check = []
        for pid in dead_pids:
            try:
                os.kill(pid, 0)
                alive_check.append(True)
            except OSError:
                alive_check.append(False)
            self._make_session_file(sessions_dir, f"{pid}.session", pid)

        lines: list[str] = []
        with patch("nexus.commands.doctor.SESSIONS_DIR", sessions_dir):
            result = _check_orphan_t1(lines)

        if all(not a for a in alive_check):
            # Both dead — expect 2 orphans reported
            assert result is False
            assert "2 orphaned" in lines[0]


# ── Step 6: T2 database integrity ────────────────────────────────────────────

class TestCheckT2Integrity:
    """Tests for _check_t2_integrity."""

    def test_db_not_exists_reports_ok(self, tmp_path: Path) -> None:
        """When memory.db does not exist, check reports 'not created yet' and returns True."""
        missing_db = tmp_path / "nonexistent.db"
        lines: list[str] = []
        with patch("nexus.commands.doctor.default_db_path", return_value=missing_db):
            result = _check_t2_integrity(lines)
        assert result is True
        assert "not created yet" in lines[0]
        assert "✓" in lines[0]

    def test_valid_t2_database_passes(self, tmp_path: Path) -> None:
        """A properly created T2 database passes both PRAGMA and FTS5 checks."""
        db_path = tmp_path / "memory.db"
        # Create a valid T2 database using the real T2Database class.
        with T2Database(db_path) as db:
            db.put(project="test", title="item1", content="hello world", ttl=30)

        lines: list[str] = []
        with patch("nexus.commands.doctor.default_db_path", return_value=db_path):
            result = _check_t2_integrity(lines)

        assert result is True
        assert "✓" in lines[0]
        assert "PRAGMA ok" in lines[0]
        assert "FTS5 ok" in lines[0]

    def test_empty_t2_database_passes(self, tmp_path: Path) -> None:
        """A freshly created but empty T2 database also passes integrity checks."""
        db_path = tmp_path / "memory.db"
        with T2Database(db_path):
            pass  # create schema only

        lines: list[str] = []
        with patch("nexus.commands.doctor.default_db_path", return_value=db_path):
            result = _check_t2_integrity(lines)

        assert result is True
        assert "PRAGMA ok" in lines[0]

    def test_truncated_database_fails_pragma(self, tmp_path: Path) -> None:
        """A truncated (corrupt) database file fails PRAGMA integrity_check."""
        db_path = tmp_path / "memory.db"
        # Create valid DB first.
        with T2Database(db_path) as db:
            db.put(project="p", title="t", content="data", ttl=1)
        # Truncate to 512 bytes — corrupts the SQLite file format.
        with open(str(db_path), "r+b") as f:
            f.truncate(512)

        lines: list[str] = []
        with patch("nexus.commands.doctor.default_db_path", return_value=db_path):
            result = _check_t2_integrity(lines)

        # A truncated file may raise on connect or on PRAGMA — either way, check fails.
        assert result is False
        assert "✗" in lines[0]

    def test_unparseable_database_fails_gracefully(self, tmp_path: Path) -> None:
        """A file that is not a SQLite database at all fails gracefully (no exception raised)."""
        db_path = tmp_path / "memory.db"
        db_path.write_bytes(b"this is not sqlite" * 100)

        lines: list[str] = []
        with patch("nexus.commands.doctor.default_db_path", return_value=db_path):
            result = _check_t2_integrity(lines)

        assert result is False
        assert "✗" in lines[0]


# ── Step 7: ChromaDB pagination audit ────────────────────────────────────────

class TestCheckChromaPagination:
    """Tests for _check_chroma_pagination using EphemeralClient."""

    @pytest.fixture()
    def ephemeral_client(self):
        """Return a ChromaDB client with all pre-existing collections removed."""
        client = chromadb.EphemeralClient()
        # Clean up any collections leaked from previous tests (singleton client)
        for col in client.list_collections():
            client.delete_collection(col.name)
        return client

    def test_no_collections_reports_ok(self, ephemeral_client) -> None:
        """When there are no non-empty collections, check reports ok."""
        lines: list[str] = []
        result = _check_chroma_pagination(lines, ephemeral_client, "test_db")
        assert result is True
        assert "✓" in lines[0]
        assert "no non-empty" in lines[0]

    def test_empty_collection_reports_ok(self, ephemeral_client) -> None:
        """An empty collection is skipped; check reports ok (nothing to audit)."""
        ephemeral_client.create_collection("empty_col")
        lines: list[str] = []
        result = _check_chroma_pagination(lines, ephemeral_client, "test_db")
        assert result is True
        assert "no non-empty" in lines[0]

    def test_single_page_collection_passes(self, ephemeral_client) -> None:
        """A collection with fewer than 300 records: count() == paginated result."""
        col = ephemeral_client.create_collection("small_col")
        n = 10
        col.add(
            ids=[f"id{i}" for i in range(n)],
            documents=[f"doc {i}" for i in range(n)],
        )
        lines: list[str] = []
        result = _check_chroma_pagination(lines, ephemeral_client, "test_db")
        assert result is True
        assert "✓" in lines[0]
        assert f"count={n}" in lines[0]
        assert f"paginated={n}" in lines[0]

    def test_multi_page_collection_passes(self, ephemeral_client) -> None:
        """A collection with >300 records paginates correctly and passes."""
        col = ephemeral_client.create_collection("large_col")
        n = 350
        col.add(
            ids=[f"id{i}" for i in range(n)],
            documents=[f"document number {i}" for i in range(n)],
        )
        lines: list[str] = []
        result = _check_chroma_pagination(lines, ephemeral_client, "test_db")
        assert result is True
        assert f"count={n}" in lines[0]
        assert f"paginated={n}" in lines[0]

    def test_count_mismatch_fails(self, ephemeral_client) -> None:
        """When mocked count() disagrees with paginated get(), check returns False."""
        col = ephemeral_client.create_collection("mismatch_col")
        n = 5
        col.add(
            ids=[f"id{i}" for i in range(n)],
            documents=[f"doc {i}" for i in range(n)],
        )
        # Wrap the real collection in a mock that inflates count()
        mock_col = MagicMock(wraps=col)
        mock_col.name = col.name
        mock_col.count.return_value = n + 100  # inflated — pagination will return only n

        mock_client = MagicMock()
        mock_client.list_collections.return_value = [mock_col]

        lines: list[str] = []
        result = _check_chroma_pagination(lines, mock_client, "test_db")
        assert result is False
        assert "✗" in lines[0]

    def test_list_collections_exception_fails_gracefully(self) -> None:
        """If list_collections() raises, check returns False without propagating."""
        bad_client = MagicMock()
        bad_client.list_collections.side_effect = RuntimeError("network error")
        lines: list[str] = []
        result = _check_chroma_pagination(lines, bad_client, "bad_db")
        assert result is False
        assert "✗" in lines[0]
        assert "list failed" in lines[0]

    def test_only_one_collection_audited(self, ephemeral_client) -> None:
        """Only the first non-empty collection is spot-checked — not all of them."""
        for i in range(3):
            col = ephemeral_client.create_collection(f"col_{i}")
            col.add(ids=[f"id{i}"], documents=[f"doc {i}"])
        lines: list[str] = []
        _check_chroma_pagination(lines, ephemeral_client, "test_db")
        # Only one line added (one audit, not three).
        assert len(lines) == 1
