# SPDX-License-Identifier: AGPL-3.0-or-later
"""Contract tests for structlog events (R4-3).

These events are monitoring / alerting integration points. The event names
and field names are de-facto contracts — changing them silently breaks
downstream consumers. This test file pins the contract.

When adding a new structured log event, add a test here to lock the
event name and expected fields.
"""
from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
import structlog
from structlog.testing import capture_logs

from nexus.db.t2 import T2Database
from nexus.types import SearchResult


@pytest.fixture(autouse=True)
def _enable_debug_logging():
    """conftest.py configures structlog at WARNING level by default, which
    drops debug/info events before capture_logs can see them. Temporarily
    lower the filter so structured log events flow through."""
    prev_wrapper = structlog.get_config()["wrapper_class"]
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG),
    )
    yield
    structlog.configure(wrapper_class=prev_wrapper)


@pytest.fixture()
def t2():
    with tempfile.TemporaryDirectory() as d:
        db = T2Database(Path(d) / "t2.db")
        yield db


# ── expire_complete ──────────────────────────────────────────────────────────


def test_expire_complete_event_emitted_with_required_fields(t2):
    """T2.expire() emits expire_complete with memory_deleted and relevance_log_deleted."""
    with capture_logs() as logs:
        t2.expire()
    events = [e for e in logs if e.get("event") == "expire_complete"]
    assert len(events) == 1
    event = events[0]
    assert "memory_deleted" in event
    assert "relevance_log_deleted" in event
    # relevance_log_error is absent when there was no error (noise reduction)
    assert "relevance_log_error" not in event


def test_expire_complete_includes_error_when_log_purge_fails(t2, monkeypatch):
    """When expire_relevance_log raises, expire_complete includes relevance_log_error."""
    def boom(*args, **kwargs):
        raise RuntimeError("simulated")

    monkeypatch.setattr(t2, "expire_relevance_log", boom)
    with capture_logs() as logs:
        t2.expire()
    events = [e for e in logs if e.get("event") == "expire_complete"]
    assert len(events) == 1
    assert events[0]["relevance_log_error"] == "RuntimeError"


# ── embedding_fetch_failed / embedding_fetch_shape_mismatch ─────────────────


def test_embedding_fetch_failed_event():
    """_fetch_embeddings_for_results emits embedding_fetch_failed on exception."""
    from nexus.search_engine import _fetch_embeddings_for_results

    class _BrokenT3:
        def get_embeddings(self, col, ids):
            raise RuntimeError("simulated fetch fault")

    results = [
        SearchResult(id="a", content="t", distance=0.1, collection="code__x", metadata={}),
    ]
    with capture_logs() as logs:
        _fetch_embeddings_for_results(results, _BrokenT3())
    events = [e for e in logs if e.get("event") == "embedding_fetch_failed"]
    assert len(events) == 1
    assert events[0]["collection"] == "code__x"
    assert events[0]["requested"] == 1


def test_embedding_fetch_shape_mismatch_event():
    """_fetch_embeddings_for_results emits shape_mismatch on dimension error."""
    from nexus.search_engine import _fetch_embeddings_for_results

    class _ShortT3:
        def get_embeddings(self, col, ids):
            return np.zeros((len(ids) - 1, 4), dtype=np.float32)

    results = [
        SearchResult(id="a", content="t", distance=0.1, collection="code__x", metadata={}),
        SearchResult(id="b", content="t", distance=0.1, collection="code__x", metadata={}),
    ]
    with capture_logs() as logs:
        _fetch_embeddings_for_results(results, _ShortT3())
    events = [e for e in logs if e.get("event") == "embedding_fetch_shape_mismatch"]
    assert len(events) == 1
    assert events[0]["collection"] == "code__x"
    assert events[0]["requested"] == 2
    assert events[0]["got"] == 1


# ── contradiction_check ──────────────────────────────────────────────────────


def test_contradiction_check_event():
    """_flag_contradictions emits contradiction_check with counts."""
    from nexus.search_engine import _flag_contradictions

    embs = np.array([
        [1.0, 0.0, 0.0],
        [0.99, 0.01, 0.0],
    ], dtype=np.float32)
    results = [
        SearchResult(id="a", content="t", distance=0.1, collection="code__x",
                     metadata={"source_agent": "alpha"}),
        SearchResult(id="b", content="t", distance=0.1, collection="code__x",
                     metadata={"source_agent": "beta"}),
    ]
    with capture_logs() as logs:
        _flag_contradictions(results, embs)
    events = [e for e in logs if e.get("event") == "contradiction_check"]
    assert len(events) == 1
    event = events[0]
    assert event["collections"] == 1
    assert event["results"] == 2
    assert "pairs_checked" in event
    assert event["flagged"] == 2


