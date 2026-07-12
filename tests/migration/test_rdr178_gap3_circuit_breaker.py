# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-178 Gap 3 (bead nexus-ob4vc) — ETL circuit breaker for sustained 5xx.

2026-07-01 incident: two concurrent chash-import legs overloaded the
ingress; nginx answered 502 for ~10s. Every chash batch in flight during
that window failed PERMANENTLY at ~3 batches/second with zero backoff
(structlog ``chash_etl_batch_error``), and 270 catalog manifest
(``document_chunks``) rows were lost in the same window.

Root-cause (verified against the codebase, see the plan-audit correction on
nexus-ob4vc): the ORIGINAL premise that "_etl_with_retry does not cover the
call site" was stale — chash_etl and catalog_etl's per-table imports DO
route every batch through ``_etl_with_retry``. The bug was TWO-FOLD:

  1. ``_is_retryable_etl_error`` classified only HTTP 403 as a retryable
     status — 429/502/503/504 (the canonical transient-ingress class) fell
     through as "not retryable", so ``_etl_with_retry`` raised on the FIRST
     attempt with zero backoff even though the call site was correctly
     wired.
  2. ``catalog_etl.py``'s ``document_chunks`` manifest write called
     ``client._post(...)`` directly with NO retry wrapper at all — a
     genuinely bypassed call site (fixed at that call site, covered in
     ``tests/db/test_catalog_etl.py``, not here).

This file covers (1) plus the new ``EtlCircuitBreaker`` pacing mechanism
that keeps a SUSTAINED outage from burning through every batch in a leg at
import speed once (1) alone bounds a single retry cycle to ~3s.
"""
from __future__ import annotations

import socket
import sqlite3
import urllib.error
from pathlib import Path

import httpx
import pytest

import nexus.retry as retry
from nexus.db.http_vector_client import VectorServiceError
from nexus.db.t2 import T2Database
from nexus.db.t2.chash_etl import migrate_chash_rows
from nexus.retry import (
    EtlCircuitBreaker,
    _etl_batch_with_breaker,
    _is_retryable_etl_error,
)

_TS = "2026-05-15T08:30:00Z"
_COLL = "knowledge__rehearsal__minilm-l6-v2-384__v1"


def _http_status_error(code: int) -> httpx.HTTPStatusError:
    req = httpx.Request("POST", "http://svc/v1/x")
    resp = httpx.Response(code, request=req)
    return httpx.HTTPStatusError(f"HTTP {code}", request=req, response=resp)


# ── 1. Classification: 429/502/503/504 are now retryable ────────────────────


@pytest.mark.parametrize("code", [429, 502, 503, 504])
def test_classifies_ingress_5xx_and_429_retryable_httpx(code: int) -> None:
    assert _is_retryable_etl_error(_http_status_error(code)) is True


@pytest.mark.parametrize("code", [429, 502, 503, 504])
def test_classifies_ingress_5xx_and_429_retryable_urllib(code: int) -> None:
    err = urllib.error.HTTPError("http://svc/x", code, "gateway", {}, None)  # type: ignore[arg-type]
    assert _is_retryable_etl_error(err) is True


@pytest.mark.parametrize("code", [429, 502, 503, 504])
def test_classifies_ingress_5xx_and_429_retryable_vectorserviceerror(code: int) -> None:
    assert _is_retryable_etl_error(VectorServiceError("edge blip", code=code)) is True


@pytest.mark.parametrize("code", [400, 404, 422])
def test_real_client_errors_still_not_retryable(code: int) -> None:
    assert _is_retryable_etl_error(_http_status_error(code)) is False


# ── 2. EtlCircuitBreaker / _etl_batch_with_breaker mechanics ─────────────────


def test_breaker_retries_same_batch_across_exhausted_cycles(monkeypatch) -> None:
    """Below the trip threshold, an exhausted-but-retryable cycle is retried
    immediately (no pause) rather than propagating to the caller."""
    monkeypatch.setattr(retry.time, "sleep", lambda _s: None)
    calls = {"n": 0}

    def flaky() -> str:
        calls["n"] += 1
        # Fails for the first 5 calls (< 2 full 3-attempt cycles), then ok.
        if calls["n"] <= 5:
            raise _http_status_error(502)
        return "ok"

    breaker = EtlCircuitBreaker(trip_threshold=3)
    result = _etl_batch_with_breaker(flaky, breaker=breaker, max_attempts=3)
    assert result == "ok"
    assert breaker.trip_count == 0          # never reached the threshold
    assert breaker.consecutive_failures == 0  # reset on success


def test_breaker_trips_after_n_consecutive_exhausted_cycles(monkeypatch) -> None:
    """After trip_threshold consecutive exhausted cycles, the breaker pauses
    (loud WARN events) and resets before continuing to retry."""
    monkeypatch.setattr(retry.time, "sleep", lambda _s: None)
    sleep_calls: list[float] = []
    monkeypatch.setattr(retry.time, "sleep", lambda s: sleep_calls.append(s))
    calls = {"n": 0}

    def flaky() -> str:
        calls["n"] += 1
        # 3 full exhausted cycles = 9 failing calls, then recover.
        if calls["n"] <= 9:
            raise _http_status_error(503)
        return "ok"

    breaker = EtlCircuitBreaker(trip_threshold=3, pause_seconds=30.0)
    result = _etl_batch_with_breaker(flaky, breaker=breaker, max_attempts=3)
    assert result == "ok"
    assert breaker.trip_count == 1
    assert sleep_calls.count(30.0) == 1     # the breaker pause, distinct from backoff sleeps
    assert breaker.consecutive_failures == 0


def test_breaker_does_not_intercept_non_retryable_errors(monkeypatch) -> None:
    """A real client error (400) must still fail fast — breaker never engages."""
    monkeypatch.setattr(retry.time, "sleep", lambda _s: None)
    calls = {"n": 0}

    def bad_request() -> None:
        calls["n"] += 1
        raise _http_status_error(400)

    breaker = EtlCircuitBreaker(trip_threshold=3)
    with pytest.raises(httpx.HTTPStatusError):
        _etl_batch_with_breaker(bad_request, breaker=breaker, max_attempts=3)
    assert calls["n"] == 1  # single attempt — no retry, no breaker involvement
    assert breaker.consecutive_failures == 0
    assert breaker.trip_count == 0


def test_breaker_gives_up_after_max_trips(monkeypatch) -> None:
    """A genuinely dead endpoint (never recovers) must not hang forever — the
    breaker gives up after max_trips and re-raises for the caller to record."""
    monkeypatch.setattr(retry.time, "sleep", lambda _s: None)

    def always_502() -> None:
        raise _http_status_error(502)

    breaker = EtlCircuitBreaker(trip_threshold=1, pause_seconds=0.0, max_trips=2)
    with pytest.raises(httpx.HTTPStatusError):
        _etl_batch_with_breaker(always_502, breaker=breaker, max_attempts=1)
    assert breaker.trip_count == 2


# ── 3. Regression: transport-level 502 burst — zero permanently-failed batches ──


def test_chash_etl_survives_502_burst_zero_permanent_failures(
    tmp_path: Path, monkeypatch
) -> None:
    """Integration-style regression for the 2026-07-01 incident shape: a
    transport-level fake (httpx.MockTransport — not a mock of the retry
    function) returns a burst of 502s long enough to trip the breaker at
    least once, then recovers. The chash batch must land with ZERO
    permanently-failed rows, and the breaker must have tripped.
    """
    monkeypatch.setattr(retry.time, "sleep", lambda _s: None)

    db = tmp_path / "chash.db"
    T2Database.bootstrap_schema(db)
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO chash_index (chash, physical_collection, created_at) "
        "VALUES (?,?,?)",
        ("0" * 64, _COLL, _TS),
    )
    conn.commit()
    conn.close()

    # 11 consecutive 502s: 3 full exhausted 3-attempt cycles (9 calls) trips
    # the default trip_threshold=3 breaker exactly once; the 4th cycle then
    # consumes the remaining 2 burst calls (10, 11) and succeeds on its 3rd
    # attempt (call 12) once the burst has cleared.
    burst = 11
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] <= burst:
            return httpx.Response(502, request=request, json={"detail": "bad gateway"})
        return httpx.Response(200, request=request, json={"imported": 1})

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport, base_url="http://svc")

    class _Store:
        """nexus-f2qvx.3: import_rows() is the public HttpChashIndex wrapper
        chash_etl.py now calls (was a raw ``self._client.post(...)``
        reach-through pre-mixin-adoption). Drives the request through this
        fake's own httpx.Client (backed by the MockTransport 502-burst
        handler above) internally, so the fault-injection is unchanged."""

        def __init__(self) -> None:
            self._client = client

        def import_rows(self, rows: list[dict]) -> int:
            resp = self._client.post("/v1/chash/import", json={"rows": rows})
            resp.raise_for_status()
            return resp.json().get("imported", 0)

    store = _Store()
    breaker = EtlCircuitBreaker()  # production defaults: trip_threshold=3, pause=30s
    result = migrate_chash_rows(db, store, breaker=breaker)

    assert result["errors"] == 0          # zero permanently-failed rows/batches
    assert result["imported"] == 1
    assert breaker.trip_count >= 1         # the breaker actually engaged
    assert calls["n"] == burst + 1         # recovered on the first post-burst call
