# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for T1Database — session scratch with per-session server sharing."""
from __future__ import annotations

import json
import os
import time
import warnings
from unittest.mock import MagicMock, patch
from uuid import uuid4

import chromadb
import pytest

from nexus.db.t1 import T1Database


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ephemeral_t1(session_id: str | None = None) -> T1Database:
    """Return a T1Database backed by an in-process EphemeralClient (for unit tests).

    Uses a unique UUID session_id by default to ensure test isolation — ChromaDB's
    EphemeralClient shares state across instances in the same process.
    """
    client = chromadb.EphemeralClient()
    return T1Database(session_id=session_id or str(uuid4()), client=client)


# ---------------------------------------------------------------------------
# Constructor behaviour
# ---------------------------------------------------------------------------

class TestT1DatabaseConstructor:

    def test_uses_http_client_when_session_record_found(self) -> None:
        """When find_ancestor_session returns a record, HttpClient is used."""
        record = {
            "session_id": "parent-session-uuid",
            "server_host": "127.0.0.1",
            "server_port": 51234,
        }
        mock_http = MagicMock()
        mock_col = MagicMock()
        mock_http.get_or_create_collection.return_value = mock_col

        with (
            patch("nexus.db.t1.find_ancestor_session", return_value=record),
            patch("chromadb.HttpClient", return_value=mock_http),
        ):
            t1 = T1Database()

        assert t1._session_id == "parent-session-uuid"
        assert t1._client is mock_http

    def test_falls_back_to_ephemeral_when_no_record(self) -> None:
        """When find_ancestor_session returns None, EphemeralClient is used with warning."""
        with (
            patch("nexus.db.t1.find_ancestor_session", return_value=None),
            warnings.catch_warnings(record=True) as w,
        ):
            warnings.simplefilter("always")
            t1 = T1Database(session_id="fallback-id")

        assert t1._session_id == "fallback-id"
        # Should NOT be an HttpClient (which is the non-fallback path)
        assert not hasattr(t1._client, "_api_url")  # HttpClient has _api_url
        assert any("EphemeralClient" in str(warning.message) for warning in w)

    def test_explicit_client_injection_bypasses_chain(self) -> None:
        """When client= is passed, PPID chain is not walked."""
        client = chromadb.EphemeralClient()
        with patch("nexus.db.t1.find_ancestor_session") as mock_find:
            t1 = T1Database(session_id="injected", client=client)
        mock_find.assert_not_called()
        assert t1._session_id == "injected"

    def test_explicit_client_generates_uuid_when_no_session_id(self) -> None:
        """When client= is passed with no session_id, a UUID is generated."""
        client = chromadb.EphemeralClient()
        with patch("nexus.db.t1.find_ancestor_session"):
            t1 = T1Database(client=client)
        assert t1._session_id  # non-empty UUID

    def test_http_constructor_reads_real_session_file(
        self, tmp_path, monkeypatch
    ) -> None:
        """T1Database resolves HttpClient by reading a real session record file.

        This exercises the full path through find_ancestor_session reading a
        JSON file, rather than mocking find_ancestor_session itself.
        """
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        ppid = os.getppid()
        record = {
            "session_id": "http-path-test-session",
            "server_host": "127.0.0.1",
            "server_port": 54321,
            "server_pid": 99999,
            "created_at": time.time(),
            "tmpdir": "",
        }
        (sessions_dir / f"{ppid}.session").write_text(json.dumps(record))

        mock_http = MagicMock()
        mock_col = MagicMock()
        mock_http.get_or_create_collection.return_value = mock_col

        monkeypatch.setattr("nexus.db.t1.SESSIONS_DIR", sessions_dir)
        with patch("chromadb.HttpClient", return_value=mock_http) as mock_http_cls:
            t1 = T1Database()

        mock_http_cls.assert_called_once_with(host="127.0.0.1", port=54321)
        assert t1._session_id == "http-path-test-session"
        assert t1._client is mock_http


# ---------------------------------------------------------------------------
# _exec / _reconnect resilience
# ---------------------------------------------------------------------------

class TestT1DatabaseReconnect:

    def test_exec_reconnects_on_connection_error(self) -> None:
        """_exec() calls _reconnect() and retries the op once after a connection error."""
        t1 = _ephemeral_t1()
        call_count = 0

        def flaky_op():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ConnectionError("server gone")
            return "success"

        with patch.object(t1, "_reconnect") as mock_reconnect:
            result = t1._exec(flaky_op)

        assert result == "success"
        assert call_count == 2
        mock_reconnect.assert_called_once()

    def test_exec_dead_flag_prevents_reconnect(self) -> None:
        """When _dead is True, the wrapper does not reconnect and re-raises the error."""
        t1 = _ephemeral_t1()
        t1._dead = True

        def always_fails():
            raise ConnectionError("still gone")

        with patch.object(t1, "_reconnect") as mock_reconnect:
            with pytest.raises(ConnectionError):
                t1._exec(always_fails)

        mock_reconnect.assert_not_called()

    def test_exec_reraises_non_connection_errors(self) -> None:
        """The wrapper re-raises errors that are not connectivity-related."""
        t1 = _ephemeral_t1()

        def bad_value():
            raise ValueError("completely unrelated error")

        with patch.object(t1, "_reconnect") as mock_reconnect:
            with pytest.raises(ValueError):
                t1._exec(bad_value)

        mock_reconnect.assert_not_called()

    def test_reconnect_falls_back_to_ephemeral_when_no_record(self) -> None:
        """_reconnect() switches to EphemeralClient when no session record is found."""
        record = {
            "session_id": "orig-session",
            "server_host": "127.0.0.1",
            "server_port": 54321,
        }
        mock_http = MagicMock()
        mock_col = MagicMock()
        mock_http.get_or_create_collection.return_value = mock_col

        with (
            patch("nexus.db.t1.find_ancestor_session", return_value=record),
            patch("chromadb.HttpClient", return_value=mock_http),
        ):
            t1 = T1Database()

        assert t1._client is mock_http

        # Simulate server death: reconnect finds no record → falls back to Ephemeral.
        with patch("nexus.db.t1.find_ancestor_session", return_value=None):
            t1._reconnect()

        assert t1._dead is True
        assert t1._client is not mock_http  # switched away from the dead HttpClient

    def test_reconnect_sets_dead_flag(self) -> None:
        """_reconnect() always sets _dead=True to prevent cascading reconnect loops."""
        t1 = _ephemeral_t1()
        assert t1._dead is False
        with patch("nexus.db.t1.find_ancestor_session", return_value=None):
            t1._reconnect()
        assert t1._dead is True

    def test_reconnect_noop_when_already_dead(self) -> None:
        """_reconnect() is a no-op when _dead is already True."""
        t1 = _ephemeral_t1()
        t1._dead = True
        original_client = t1._client
        with patch("nexus.db.t1.find_ancestor_session") as mock_find:
            t1._reconnect()
        mock_find.assert_not_called()
        assert t1._client is original_client


# ---------------------------------------------------------------------------
# put / get / list_entries
# ---------------------------------------------------------------------------

class TestT1DatabaseCRUD:

    def test_put_returns_id(self) -> None:
        t1 = _ephemeral_t1()
        doc_id = t1.put("test content")
        assert isinstance(doc_id, str)
        assert len(doc_id) > 0

    def test_get_roundtrip(self) -> None:
        t1 = _ephemeral_t1()
        doc_id = t1.put("hello world", tags="test")
        entry = t1.get(doc_id)
        assert entry is not None
        assert entry["content"] == "hello world"
        assert entry["tags"] == "test"

    def test_get_missing_returns_none(self) -> None:
        t1 = _ephemeral_t1()
        assert t1.get("nonexistent-id") is None

    def test_list_entries_session_scoped(self) -> None:
        """Entries from a different session are not visible."""
        client = chromadb.EphemeralClient()
        sid_a, sid_b = str(uuid4()), str(uuid4())
        t1_a = T1Database(session_id=sid_a, client=client)
        t1_b = T1Database(session_id=sid_b, client=client)

        t1_a.put("entry for A")
        t1_b.put("entry for B")

        a_entries = t1_a.list_entries()
        b_entries = t1_b.list_entries()

        assert len(a_entries) == 1
        assert a_entries[0]["content"] == "entry for A"
        assert len(b_entries) == 1
        assert b_entries[0]["content"] == "entry for B"

    def test_search_returns_results(self) -> None:
        t1 = _ephemeral_t1()
        t1.put("authentication errors in JWT middleware")
        t1.put("database connection pool exhausted")
        results = t1.search("JWT auth problems", n_results=1)
        assert len(results) == 1
        assert "JWT" in results[0]["content"]

    def test_search_empty_session_returns_empty(self) -> None:
        t1 = _ephemeral_t1()
        results = t1.search("anything")
        assert results == []

    def test_search_session_scoped(self) -> None:
        """Search only returns results from the current session."""
        client = chromadb.EphemeralClient()
        sid_a, sid_b = str(uuid4()), str(uuid4())
        t1_a = T1Database(session_id=sid_a, client=client)
        t1_b = T1Database(session_id=sid_b, client=client)

        t1_a.put("authentication error in middleware")
        results_b = t1_b.search("authentication")
        assert results_b == []


# ---------------------------------------------------------------------------
# flag / unflag / flagged_entries
# ---------------------------------------------------------------------------

class TestT1DatabaseFlag:

    def test_flag_and_flagged_entries(self) -> None:
        t1 = _ephemeral_t1()
        doc_id = t1.put("important finding")
        t1.flag(doc_id, project="proj", title="finding.md")
        flagged = t1.flagged_entries()
        assert len(flagged) == 1
        assert flagged[0]["flush_project"] == "proj"
        assert flagged[0]["flush_title"] == "finding.md"

    def test_unflag(self) -> None:
        t1 = _ephemeral_t1()
        doc_id = t1.put("temp note")
        t1.flag(doc_id)
        t1.unflag(doc_id)
        assert t1.flagged_entries() == []

    def test_flag_nonexistent_raises(self) -> None:
        t1 = _ephemeral_t1()
        with pytest.raises(KeyError):
            t1.flag("nonexistent-id")

    def test_persist_pre_flags(self) -> None:
        t1 = _ephemeral_t1()
        doc_id = t1.put("auto-flagged", persist=True, flush_project="p", flush_title="t")
        flagged = t1.flagged_entries()
        assert any(e["id"] == doc_id for e in flagged)


# ---------------------------------------------------------------------------
# clear
# ---------------------------------------------------------------------------

class TestT1DatabaseClear:

    def test_clear_removes_session_entries(self) -> None:
        t1 = _ephemeral_t1()
        t1.put("entry 1")
        t1.put("entry 2")
        count = t1.clear()
        assert count == 2
        assert t1.list_entries() == []

    def test_clear_session_scoped(self) -> None:
        """Clearing one session does not affect another."""
        client = chromadb.EphemeralClient()
        sid_a, sid_b = str(uuid4()), str(uuid4())
        t1_a = T1Database(session_id=sid_a, client=client)
        t1_b = T1Database(session_id=sid_b, client=client)
        t1_a.put("A entry")
        t1_b.put("B entry")
        t1_a.clear()
        assert t1_a.list_entries() == []
        assert len(t1_b.list_entries()) == 1

    def test_clear_empty_session_returns_zero(self) -> None:
        t1 = _ephemeral_t1()
        assert t1.clear() == 0
