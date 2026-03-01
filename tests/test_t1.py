# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for T1Database — session scratch with per-session server sharing."""
from __future__ import annotations

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
