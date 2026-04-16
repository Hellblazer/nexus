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


def _ephemeral_t1(session_id: str | None = None) -> T1Database:
    return T1Database(session_id=session_id or str(uuid4()), client=chromadb.EphemeralClient())


def _shared_pair() -> tuple[T1Database, T1Database, str, str]:
    client = chromadb.EphemeralClient()
    sid_a, sid_b = str(uuid4()), str(uuid4())
    return T1Database(session_id=sid_a, client=client), T1Database(session_id=sid_b, client=client), sid_a, sid_b


# ---------------------------------------------------------------------------
# Constructor behaviour
# ---------------------------------------------------------------------------

class TestT1DatabaseConstructor:

    def test_uses_http_client_when_session_record_found(self) -> None:
        record = {"session_id": "parent-session-uuid", "server_host": "127.0.0.1", "server_port": 51234}
        mock_http = MagicMock()
        mock_http.get_or_create_collection.return_value = MagicMock()
        with patch("nexus.db.t1.resolve_t1_session", return_value=record), \
             patch("chromadb.HttpClient", return_value=mock_http):
            t1 = T1Database()
        assert t1._session_id == "parent-session-uuid"
        assert t1._client is mock_http

    def test_falls_back_to_ephemeral_when_no_record(self) -> None:
        with patch("nexus.db.t1.resolve_t1_session", return_value=None), \
             warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            t1 = T1Database(session_id="fallback-id")
        assert t1._session_id == "fallback-id"
        assert not hasattr(t1._client, "_api_url")
        assert any("EphemeralClient" in str(x.message) for x in w)

    def test_explicit_client_injection_bypasses_chain(self) -> None:
        client = chromadb.EphemeralClient()
        with patch("nexus.db.t1.resolve_t1_session") as mock_find:
            t1 = T1Database(session_id="injected", client=client)
        mock_find.assert_not_called()
        assert t1._session_id == "injected"

    def test_explicit_client_generates_uuid_when_no_session_id(self) -> None:
        t1 = T1Database(client=chromadb.EphemeralClient())
        assert t1._session_id

    def test_http_constructor_reads_real_session_file(self, tmp_path, monkeypatch) -> None:
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        record = {
            "session_id": "http-path-test-session", "server_host": "127.0.0.1",
            "server_port": 54321, "server_pid": 99999, "created_at": time.time(), "tmpdir": "",
        }
        (sessions_dir / f"{os.getppid()}.session").write_text(json.dumps(record))
        mock_http = MagicMock()
        mock_http.get_or_create_collection.return_value = MagicMock()
        monkeypatch.setattr("nexus.db.t1.SESSIONS_DIR", sessions_dir)
        with patch("chromadb.HttpClient", return_value=mock_http) as cls:
            t1 = T1Database()
        cls.assert_called_once_with(host="127.0.0.1", port=54321)
        assert t1._session_id == "http-path-test-session"
        assert t1._client is mock_http


# ---------------------------------------------------------------------------
# _exec / _reconnect resilience
# ---------------------------------------------------------------------------

class TestT1DatabaseReconnect:

    def test_exec_reconnects_on_connection_error(self) -> None:
        t1 = _ephemeral_t1()
        call_count = 0
        def flaky():
            nonlocal call_count; call_count += 1
            if call_count == 1: raise ConnectionError("server gone")
            return "success"
        with patch.object(t1, "_reconnect") as mock:
            assert t1._exec(flaky) == "success"
        assert call_count == 2
        mock.assert_called_once()

    def test_exec_dead_flag_prevents_reconnect(self) -> None:
        t1 = _ephemeral_t1()
        t1._dead = True
        with patch.object(t1, "_reconnect") as mock:
            with pytest.raises(ConnectionError):
                t1._exec(lambda: (_ for _ in ()).throw(ConnectionError("still gone")))
        mock.assert_not_called()

    def test_exec_reraises_non_connection_errors(self) -> None:
        t1 = _ephemeral_t1()
        with patch.object(t1, "_reconnect") as mock:
            with pytest.raises(ValueError):
                t1._exec(lambda: (_ for _ in ()).throw(ValueError("unrelated")))
        mock.assert_not_called()

    def test_reconnect_falls_back_to_ephemeral_when_no_record(self) -> None:
        record = {"session_id": "orig-session", "server_host": "127.0.0.1", "server_port": 54321}
        mock_http = MagicMock()
        mock_http.get_or_create_collection.return_value = MagicMock()
        with patch("nexus.db.t1.resolve_t1_session", return_value=record), \
             patch("chromadb.HttpClient", return_value=mock_http):
            t1 = T1Database()
        assert t1._client is mock_http
        with patch("nexus.db.t1.resolve_t1_session", return_value=None):
            t1._reconnect()
        assert t1._dead is True and t1._client is not mock_http

    def test_reconnect_sets_dead_flag(self) -> None:
        t1 = _ephemeral_t1()
        with patch("nexus.db.t1.resolve_t1_session", return_value=None):
            t1._reconnect()
        assert t1._dead is True

    def test_reconnect_noop_when_already_dead(self) -> None:
        t1 = _ephemeral_t1()
        t1._dead = True
        original = t1._client
        with patch("nexus.db.t1.resolve_t1_session") as mock:
            t1._reconnect()
        mock.assert_not_called()
        assert t1._client is original


# ---------------------------------------------------------------------------
# CRUD + search + session scoping
# ---------------------------------------------------------------------------

class TestT1DatabaseCRUD:

    def test_put_returns_id(self) -> None:
        t1 = _ephemeral_t1()
        assert isinstance(t1.put("test content"), str)

    def test_get_roundtrip(self) -> None:
        t1 = _ephemeral_t1()
        doc_id = t1.put("hello world", tags="test")
        entry = t1.get(doc_id)
        assert entry["content"] == "hello world" and entry["tags"] == "test"

    def test_get_missing_returns_none(self) -> None:
        assert _ephemeral_t1().get("nonexistent-id") is None

    def test_list_entries_session_scoped(self) -> None:
        t1_a, t1_b, _, _ = _shared_pair()
        t1_a.put("entry for A"); t1_b.put("entry for B")
        assert len(t1_a.list_entries()) == 1 and t1_a.list_entries()[0]["content"] == "entry for A"
        assert len(t1_b.list_entries()) == 1 and t1_b.list_entries()[0]["content"] == "entry for B"

    def test_search_returns_results(self) -> None:
        t1 = _ephemeral_t1()
        t1.put("authentication errors in JWT middleware")
        t1.put("database connection pool exhausted")
        results = t1.search("JWT auth problems", n_results=1)
        assert len(results) == 1 and "JWT" in results[0]["content"]

    def test_search_empty_session_returns_empty(self) -> None:
        assert _ephemeral_t1().search("anything") == []

    def test_search_session_scoped(self) -> None:
        t1_a, t1_b, _, _ = _shared_pair()
        t1_a.put("authentication error in middleware")
        assert t1_b.search("authentication") == []


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
        assert flagged[0]["flush_project"] == "proj" and flagged[0]["flush_title"] == "finding.md"

    def test_unflag(self) -> None:
        t1 = _ephemeral_t1()
        doc_id = t1.put("temp note")
        t1.flag(doc_id); t1.unflag(doc_id)
        assert t1.flagged_entries() == []

    def test_flag_nonexistent_raises(self) -> None:
        with pytest.raises(KeyError):
            _ephemeral_t1().flag("nonexistent-id")

    def test_persist_pre_flags(self) -> None:
        t1 = _ephemeral_t1()
        doc_id = t1.put("auto-flagged", persist=True, flush_project="p", flush_title="t")
        assert any(e["id"] == doc_id for e in t1.flagged_entries())


# ---------------------------------------------------------------------------
# clear
# ---------------------------------------------------------------------------

class TestT1DatabaseClear:

    def test_clear_removes_session_entries(self) -> None:
        t1 = _ephemeral_t1()
        t1.put("entry 1"); t1.put("entry 2")
        assert t1.clear() == 2 and t1.list_entries() == []

    def test_clear_session_scoped(self) -> None:
        t1_a, t1_b, _, _ = _shared_pair()
        t1_a.put("A entry"); t1_b.put("B entry")
        t1_a.clear()
        assert t1_a.list_entries() == [] and len(t1_b.list_entries()) == 1

    def test_clear_empty_session_returns_zero(self) -> None:
        assert _ephemeral_t1().clear() == 0
