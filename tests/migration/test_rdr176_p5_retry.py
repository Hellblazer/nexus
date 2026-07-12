# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-176 Phase 5 (Gap 6) — bounded transient-edge retry in the migration ETLs.

Failing-first (bead nexus-t9rmg.30). Prod evidence: the vector leg round-tripped
124k vectors over the managed edge and "succeeded only after two transient-403
retries"; today a transient nginx 403 / connection drop / read-timeout (the
vector leg's socket timeout already fires at 600 s) is NOT classified retryable,
so a single edge blip that RAISES strands a collection leg (vectors) or records
a whole batch as failed (T2). This adds the missing bounded retry; it does not
add a timeout (the per-call timeouts pre-exist), and retrying a genuine stall
multiplies its worst-case duration — see _etl_with_retry's docstring caveats.

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
from nexus.db.t2.aspects_etl import (
    migrate_aspects,
    migrate_highlights,
    migrate_promotion_log,
    migrate_queue,
)
from nexus.db.t2.catalog_etl import _import_table
from nexus.db.t2.chash_etl import migrate_chash_rows
from nexus.db.t2.memory_etl import migrate_memory_rows
from nexus.db.t2.plan_etl import migrate_plan_rows
from nexus.db.t2.taxonomy_etl import migrate_taxonomy_rows
from nexus.db.t2.telemetry_etl import migrate_telemetry_rows
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
            # 32-char ids: the fake must model the chash identity or the
            # nexus-sot7v legacy-id guard (correctly) fails the collection
            # before the upsert this test exists to retry.
            {"id": f"c{i}".ljust(32, "0"), "document": f"d{i}", "metadata": {}}
            for i in range(3)
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
    # Non-voyage collection name on purpose: _dim_for_collection +
    # _is_same_model_passthrough are monkeypatched, so the model segment is
    # irrelevant here, and a voyage-* name would trip the cloud-mode lint
    # (test_mode_declarations_are_explicit) which scans test source.
    result = vetl._migrate_one(
        _ReadClient(), vc, "knowledge__o__minilm-l6-v2-384__v1",
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


# ── 1b. code=None transport drop on the MANAGED vector path is retryable ───────


def _vse_wrapping(cause: BaseException) -> VectorServiceError:
    """A VectorServiceError(code=None) chained from a transport error, exactly
    as http_vector_client._post reframes a managed-path drop (`raise … from e`)."""
    try:
        raise cause
    except BaseException as e:  # noqa: BLE001 — building a chained exc for the test
        try:
            raise VectorServiceError("POST /v1/vectors/upsert-chunks failed", code=None) from e
        except VectorServiceError as vse:
            return vse


@pytest.mark.parametrize(
    "cause",
    [ConnectionResetError("reset"), TimeoutError("read timed out"),
     urllib.error.URLError("connection refused")],
)
def test_vector_service_error_code_none_transport_is_retryable(cause) -> None:
    # The managed vector path wraps a transport drop as VectorServiceError(code
    # =None); it must classify by the chained cause, not fall through to False.
    assert _is_retryable_etl_error(_vse_wrapping(cause)) is True


def test_etl_retry_persistent_403_preserves_remedy(monkeypatch) -> None:
    """A persistent 403 exhausts the bound and re-raises with its remedy intact."""
    monkeypatch.setattr(retry.time, "sleep", lambda _s: None)

    def always_forbidden() -> None:
        raise VectorServiceError(
            "POST /x → HTTP 403\nRun `nx doctor` to refresh the managed token.",
            code=403,
        )

    with pytest.raises(VectorServiceError) as ei:
        _etl_with_retry(always_forbidden, max_attempts=3)
    assert "nx doctor" in str(ei.value)  # remedy survives the retry bound


# ── 3b. Wiring: every remaining T2 ETL routes its batch import through retry ───

_TS = "2026-05-15T08:30:00Z"
_COLL = "knowledge__rehearsal__minilm-l6-v2-384__v1"


class _FlakyBatchStore:
    """Implements every T2 ETL batch method; each raises a transient 403 twice
    then succeeds, so a wired call site lands on the 3rd attempt and an UNWIRED
    one (no _etl_with_retry) records the batch failed on the 1st."""

    def __init__(self) -> None:
        self.attempts = 0

    def _attempt(self, rows: list[dict]) -> int:
        self.attempts += 1
        if self.attempts < 3:
            raise _http_status_error(403)
        return len(rows)

    def build_import_row(self, **kwargs) -> dict:
        return dict(kwargs)

    def import_plans_batch(self, rows): return self._attempt(rows)
    def import_aspects_batch(self, rows): return self._attempt(rows)
    def import_highlights_batch(self, rows): return self._attempt(rows)
    def import_queue_batch(self, rows): return self._attempt(rows)
    def import_promotion_batch(self, rows): return self._attempt(rows)
    def import_rows_batch(self, _kind_or_table, rows): return self._attempt(rows)


def _db_with(tmp_path: Path, sql: str, row: tuple) -> Path:
    db = tmp_path / "etl.db"
    T2Database.bootstrap_schema(db)
    conn = sqlite3.connect(str(db))
    conn.execute(sql, row)
    conn.commit()
    conn.close()
    return db


def _seed_plans(tmp_path):
    return _db_with(
        tmp_path,
        "INSERT INTO plans (project, query, plan_json, created_at) VALUES (?,?,?,?)",
        ("p", "q", "{}", _TS),
    )


def _seed_telemetry(tmp_path):
    return _db_with(
        tmp_path,
        "INSERT INTO hook_failures (doc_id, collection, hook_name, error, "
        "occurred_at, batch_doc_ids, is_batch, chain) VALUES (?,?,?,?,?,?,?,?)",
        ("d", _COLL, "post_store", "e", _TS, None, 0, "single"),
    )


def _seed_taxonomy(tmp_path):
    return _db_with(
        tmp_path,
        "INSERT INTO topics (id, label, parent_id, collection, centroid_hash, "
        "doc_count, created_at, review_status, terms) VALUES (?,?,?,?,?,?,?,?,?)",
        (1, "t", None, _COLL, "0" * 32, 0, _TS, "approved", "a b"),
    )


def _seed_aspects(tmp_path):
    return _db_with(
        tmp_path,
        "INSERT INTO document_aspects (collection, source_path, problem_formulation, "
        "confidence, extracted_at, model_version, extractor_name) VALUES (?,?,?,?,?,?,?)",
        (_COLL, "/p/d.pdf", "problem", 0.9, _TS, "v1", "claude"),
    )


def _seed_highlights(tmp_path):
    return _db_with(
        tmp_path,
        "INSERT INTO document_highlights (doc_id, source_uri, collection, "
        "highlights_md, mentions_md, ingested_at) VALUES (?,?,?,?,?,?)",
        ("d", "file://d", _COLL, "h", "m", _TS),
    )


def _seed_queue(tmp_path):
    return _db_with(
        tmp_path,
        "INSERT INTO aspect_extraction_queue (collection, source_path, "
        "content_hash, status, enqueued_at) VALUES (?,?,?,?,?)",
        (_COLL, "/p/d.pdf", "0" * 64, "pending", _TS),
    )


def _seed_promotion(tmp_path):
    return _db_with(
        tmp_path,
        "INSERT INTO aspect_promotion_log (field_name, sql_type, column_added, "
        "rows_backfilled, rows_pruned, pruned, promoted_at) VALUES (?,?,?,?,?,?,?)",
        ("f", "TEXT", 1, 0, 0, 0, _TS),
    )


def _run_plan(db, store):
    return migrate_plan_rows(db, store)


def _run_telemetry(db, store):
    return migrate_telemetry_rows(db, store)


def _run_taxonomy(db, store):
    return migrate_taxonomy_rows(db, store)


def _run_aspects(db, store):
    return migrate_aspects(db, store)


def _run_highlights(db, store):
    return migrate_highlights(db, store)


def _run_queue(db, store):
    return migrate_queue(db, store)


def _run_promotion(db, store):
    return migrate_promotion_log(db, store)


@pytest.mark.parametrize(
    "name,seed,run",
    [
        ("plans", _seed_plans, _run_plan),
        ("telemetry", _seed_telemetry, _run_telemetry),
        ("taxonomy", _seed_taxonomy, _run_taxonomy),
        ("aspects", _seed_aspects, _run_aspects),
        ("highlights", _seed_highlights, _run_highlights),
        ("queue", _seed_queue, _run_queue),
        ("promotion", _seed_promotion, _run_promotion),
    ],
)
def test_t2_etl_retries_transient_403(name, seed, run, tmp_path, monkeypatch) -> None:
    """Each T2 ETL must route its batch import through _etl_with_retry: a
    transient-403-twice-then-ok lands the row instead of failing the batch.
    Without the wrap, the store method is called once and the row is lost."""
    monkeypatch.setattr(retry.time, "sleep", lambda _s: None)
    db = seed(tmp_path)
    store = _FlakyBatchStore()
    run(db, store)
    assert store.attempts == 3, f"{name}: expected 3 attempts (retried twice), got {store.attempts}"


def test_chash_etl_retries_transient_403(tmp_path, monkeypatch) -> None:
    """chash_etl posts via HttpChashIndex.import_rows() (nexus-f2qvx.3 —
    was the raw client + raise_for_status pre-mixin-adoption); the call
    must be retried on a transient 403."""
    monkeypatch.setattr(retry.time, "sleep", lambda _s: None)
    db = _db_with(
        tmp_path,
        "INSERT INTO chash_index (chash, physical_collection, created_at) VALUES (?,?,?)",
        ("0" * 64, _COLL, _TS),
    )

    class _FlakyStore:
        def __init__(self) -> None:
            self.attempts = 0

        def import_rows(self, rows):
            self.attempts += 1
            if self.attempts < 3:
                req = httpx.Request("POST", "http://svc/v1/chash/import")
                resp = httpx.Response(403, request=req, json={"error": "forbidden"})
                raise httpx.HTTPStatusError("403 forbidden", request=req, response=resp)
            return len(rows)

    store = _FlakyStore()
    migrate_chash_rows(db, store)
    assert store.attempts == 3


def test_catalog_import_table_retries_transient_403(monkeypatch) -> None:
    """catalog_etl._import_table runs its import_fn through _etl_with_retry."""
    monkeypatch.setattr(retry.time, "sleep", lambda _s: None)
    attempts = {"n": 0}

    def flaky_import(_rows):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise _http_status_error(403)

    result = _import_table(
        table="owners",
        rows=[{"tumbler_prefix": "1"}],
        transform=lambda r: r,
        import_fn=flaky_import,
        batch_log_every=10_000,
    )
    assert attempts["n"] == 3
    assert result["written"] == 1
