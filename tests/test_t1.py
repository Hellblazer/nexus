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


@pytest.fixture(autouse=True)
def _allow_t1_record_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    """GH #567: tests in this file exercise T1Database's record-
    resolution + raise-loud paths directly. The conftest's autouse
    ``_isolate_t1_sessions`` fixture sets ``NEXUS_SKIP_T1=1`` to keep
    other test files using ephemeral fallback semantics; here we
    UNSET it so tests reach the post-fix behaviour:
      - constructor raises ``T1ServerNotFoundError`` when no record
      - finds a record when one is written
      - exercises the resolver-retry loop
    Tests that explicitly want ``NEXUS_SKIP_T1=1`` opt back in
    locally via ``monkeypatch.setenv`` inside the test body.
    """
    monkeypatch.delenv("NEXUS_SKIP_T1", raising=False)


def _ephemeral_t1(session_id: str | None = None) -> T1Database:
    return T1Database(session_id=session_id or str(uuid4()), client=chromadb.EphemeralClient())


def _shared_pair() -> tuple[T1Database, T1Database, str, str]:
    client = chromadb.EphemeralClient()
    sid_a, sid_b = str(uuid4()), str(uuid4())
    return T1Database(session_id=sid_a, client=client), T1Database(session_id=sid_b, client=client), sid_a, sid_b


# ---------------------------------------------------------------------------
# Constructor behaviour
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# _resolve_session_record_with_retry (RDR-094 CA-2 / nexus-zsqf)
# ---------------------------------------------------------------------------





# ---------------------------------------------------------------------------
# _exec / _reconnect resilience
# ---------------------------------------------------------------------------




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
