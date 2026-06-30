# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-176 Phase 5 (Gap 6) — bounded transient-edge retry in the migration ETLs.

Failing-first (bead nexus-t9rmg.30). Prod evidence: the vector leg round-tripped
124k vectors over the managed edge and "succeeded only after two transient-403
retries"; today a transient nginx 403 / connection drop / read-timeout is NOT
classified retryable, so a single edge blip strands a collection leg (vectors)
or records a whole batch as failed (T2) — and there is no read-timeout, so a
hung connection can block forever.

The fix is a BOUNDED, migration-scoped retry (idempotent upsert / ON CONFLICT
makes re-sending safe). It lives IN THE ETL call sites, NOT in the shared
HTTP client `_post` — a 403 in normal runtime store use must still fail fast
(real auth failure), only the migration legs absorb transient edge blips.

These tests assert:
  1. `_is_retryable_etl_error` classifies transient 403 / drop / timeout as
     retryable and real client errors (400/404) as NOT.
  2. `_etl_with_retry` retries up to a bound, succeeds after transient failures,
     gives up after the bound, and never retries a non-transient error.
  3. The vector ETL (`_migrate_one`) and a representative T2 ETL
     (`migrate_memory_rows`) actually route their edge write through the retry,
     so a transient-403-twice-then-ok sequence SUCCEEDS instead of failing.
"""
from __future__ import annotations

import socket
import sqlite3
import urllib.error
from pathlib import Path

import httpx
import pytest

import nexus.migration.vector_etl as vetl
import nexus.retry as retry
from nexus.db.http_vector_client import VectorServiceError
from nexus.db.t2 import T2Database
from nexus.db.t2.memory_etl import migrate_memory_rows
from nexus.retry import _etl_with_retry, _is_retryable_etl_error


def _http_status_error(code: int) -> httpx.HTTPStatusError:
    req = httpx.Request("POST", "http://svc/v1/x")
    resp = httpx.Response(code, request=req)
    return httpx.HTTPStatusError(f"HTTP {code}", request=req, response=resp)


# ── 1. Classification ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "exc",
    [
        VectorServiceError("forbidden", code=403),            # nginx edge 403
        _http_status_error(403),                              # T2 httpx 403
        httpx.ConnectError("connection refused"),             # drop
        httpx.ReadTimeout("read timed out"),                  # hung read
        httpx.RemoteProtocolError("server disconnected"),     # mid-flight drop
        urllib.error.URLError("connection reset by peer"),    # urllib drop
        socket.timeout("timed out"),                          # urllib read-timeout
        ConnectionResetError("reset"),                        # TCP RST
    ],
)
def test_classifies_transient_edge_errors_retryable(exc: Exception) -> None:
    assert _is_retryable_etl_error(exc) is True


@pytest.mark.parametrize(
    "exc",
    [
        VectorServiceError("bad request", code=400),
        VectorServiceError("not found", code=404),
        _http_status_error(400),
        _http_status_error(404),
        ValueError("malformed row"),
    ],
)
def test_does_not_classify_real_client_errors_retryable(exc: Exception) -> None:
    assert _is_retryable_etl_error(exc) is False


# ── 2. _etl_with_retry behavior ────────────────────────────────────────────────


def test_etl_with_retry_succeeds_after_two_transient_403(monkeypatch) -> None:
    monkeypatch.setattr(retry.time, "sleep", lambda _s: None)  # no real backoff
    calls = {"n": 0}

    def flaky() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise VectorServiceError("edge 403", code=403)
        return "ok"

    assert retry._etl_with_retry(flaky, max_attempts=3) == "ok"
    assert calls["n"] == 3  # mirrors prod: succeeded on the 3rd attempt


def test_etl_with_retry_is_bounded(monkeypatch) -> None:
    monkeypatch.setattr(retry.time, "sleep", lambda _s: None)
    calls = {"n": 0}

    def always_403() -> None:
        calls["n"] += 1
        raise VectorServiceError("persistent 403", code=403)

    with pytest.raises(VectorServiceError):
        retry._etl_with_retry(always_403, max_attempts=3)
    assert calls["n"] == 3  # bounded — not infinite


def test_etl_with_retry_no_retry_on_non_transient(monkeypatch) -> None:
    monkeypatch.setattr(retry.time, "sleep", lambda _s: None)
    calls = {"n": 0}

    def bad_request() -> None:
        calls["n"] += 1
        raise VectorServiceError("bad request", code=400)

    with pytest.raises(VectorServiceError):
        retry._etl_with_retry(bad_request, max_attempts=3)
    assert calls["n"] == 1  # real client error — fail fast, no retry


# ── 3. Wiring: the actual ETL legs route their edge write through the retry ─────


def test_vector_etl_retries_transient_403(monkeypatch) -> None:
    """_migrate_one must survive a transient-403-twice-then-ok on upsert."""
    monkeypatch.setattr(retry.time, "sleep", lambda _s: None)
    # Decouple from the dim/passthrough model maps: force a resolvable dim and
    # the non-passthrough (embeddings=None) path.
    monkeypatch.setattr(vetl, "_dim_for_collection", lambda _n: (1024, ""))
    monkeypatch.setattr(vetl, "_is_same_model_passthrough", lambda _n, _t: False)
    monkeypatch.setattr(
        vetl, "iter_collection_chunks",
        lambda *a, **k: iter([
            {"id": f"c{i}", "document": f"d{i}", "metadata": {}} for i in range(3)
        ]),
    )

    class _ReadClient:
        def get_collection(self, name):
            class _C:
                def count(self_inner) -> int:
                    return 3
            return _C()

    class _VectorClient:
        def __init__(self) -> None:
            self.attempts = 0

        def upsert_chunks(self, *a, **k) -> None:
            self.attempts += 1
            if self.attempts < 3:
                raise VectorServiceError("transient edge 403", code=403)

        def count(self, _target) -> int:
            return 3

    vc = _VectorClient()
    result = vetl._migrate_one(
        _ReadClient(), vc, "knowledge__o__voyage-context-3__v1",
        dry_run=False, page=10,
    )
    assert vc.attempts == 3                  # retried twice, succeeded on 3rd
    assert result.status == "migrated", result.reason


def test_memory_etl_retries_transient_403(tmp_path: Path, monkeypatch) -> None:
    """A representative T2 ETL must retry a transient 403 on the batch import
    instead of recording the whole batch failed."""
    monkeypatch.setattr(retry.time, "sleep", lambda _s: None)

    db = tmp_path / "m.db"
    T2Database.bootstrap_schema(db)
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO memory (project, title, content, timestamp, tags, ttl, "
        "access_count, last_accessed) VALUES (?,?,?,?,?,?,?,?)",
        ("p", "t", "c", "2026-05-15T08:30:00Z", "a", None, 0, None),
    )
    conn.commit()
    conn.close()

    class _FlakyStore:
        def __init__(self) -> None:
            self.attempts = 0

        def build_import_row(self, **kwargs) -> dict:
            return dict(kwargs)

        def import_entries_batch(self, rows: list[dict]) -> int:
            self.attempts += 1
            if self.attempts < 3:
                raise _http_status_error(403)
            return len(rows)

    store = _FlakyStore()
    result = migrate_memory_rows(db, store)
    assert store.attempts == 3            # retried twice
    assert result["written"] == 1         # succeeded, not recorded failed
