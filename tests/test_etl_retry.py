# SPDX-License-Identifier: AGPL-3.0-or-later
"""``_etl_with_retry`` (RDR-178 Gap 3 migration-edge retry) + nexus-cy9u7's
shared RateLimitBrake wiring.

``_etl_with_retry`` serves TWO real production shapes: the httpx-based
catalog/chash ETL legs (``catalog_etl.py``), and the urllib-based vector
migration leg (``db/reconcile.py:664``, wrapping
``vector_client.upsert_chunks`` via ``_etl_batch_with_breaker``) — both
exercised below, the second via the SAME real-shape ``VectorServiceError``
construction ``tests/test_vector_retry.py`` uses (nexus-cy9u7 code-review
finding: this file previously tested only the httpx shape and called that
"the vector-path fiction").

No dedicated test file existed for ``_etl_with_retry`` before this one
(it was previously exercised only indirectly via
``tests/db/test_reconcile*.py``'s ``_etl_batch_with_breaker`` callers) —
covers its baseline retry/backoff contract plus the brake wiring together.
"""
from __future__ import annotations

import email.message
import http.server
import json
import threading
import urllib.error
from unittest.mock import MagicMock, call, patch

import httpx
import pytest

import nexus.retry as retry_mod
from nexus.db.http_vector_client import VectorServiceError
from nexus.rate_brake import RateLimitBrake, reset_brake
from nexus.retry import EtlCircuitBreaker, _etl_batch_with_breaker, _etl_with_retry, _is_retryable_etl_error


class _FakeClock:
    """Self-advancing fake clock — no real time.sleep, deterministic."""

    def __init__(self) -> None:
        self._now = 0.0
        self._lock = threading.Lock()

    def time(self) -> float:
        with self._lock:
            return self._now

    def sleep(self, seconds: float) -> None:
        with self._lock:
            self._now += seconds


@pytest.fixture(autouse=True)
def _isolate_default_brake(monkeypatch):
    """nexus-cy9u7 CRITICAL-2: the brake now trips on EVERY retryable
    failure, not only a narrow 429/503+Retry-After signal — see the
    identical fixture / rationale in tests/test_vector_retry.py. Installs
    a fast fake-clocked brake as the default for every test in this
    module so a test that retries anything never drives the REAL
    process-wide brake's real clock/sleep."""
    fc = _FakeClock()
    fake_brake = RateLimitBrake(clock=fc.time, sleep=fc.sleep, jitter=lambda: 0.0)
    monkeypatch.setattr(retry_mod, "get_brake", lambda: fake_brake)
    yield fake_brake
    reset_brake()


def _make_status_exc(status: int, headers: dict[str, str] | None = None) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://engine.internal/v1/etl/batch")
    response = httpx.Response(status, request=request, headers=headers or {})
    return httpx.HTTPStatusError(f"{status}", request=request, response=response)


def _make_vector_service_error(
    status: int, retry_after: str | None = None,
) -> VectorServiceError:
    """Construct a ``VectorServiceError`` the way ``db/reconcile.py``'s
    ``vector_client.upsert_chunks`` call actually raises it in production
    (via ``http_vector_client.py``'s ``_post``, chained from a real
    ``urllib.error.HTTPError``) — see the identical helper in
    ``tests/test_vector_retry.py`` for the full rationale."""
    hdrs = email.message.Message()
    if retry_after is not None:
        hdrs["Retry-After"] = retry_after
    http_err = urllib.error.HTTPError(
        "http://engine.internal/v1/vectors/upsert-chunks", status, "error", hdrs, None,
    )
    try:
        raise http_err
    except urllib.error.HTTPError as e:
        try:
            raise VectorServiceError(
                f"POST /v1/vectors/upsert-chunks -> HTTP {status}: error", code=e.code,
            ) from e
        except VectorServiceError as vse:
            return vse


# ── Baseline retry/backoff contract ──────────────────────────────────────


def test_retries_on_transient_503_then_succeeds() -> None:
    """Baseline retry/backoff contract. The SUBJECT here is the curve and the
    brake, not the status -- the status is only a vehicle.

    Vehicle changed 403 -> 503 (nexus-1jtob, 2026-08-23). 403 was removed from
    ``_RETRYABLE_ETL_HTTP_STATUSES``: it sat there on a "transient edge 403"
    premise that was never measured, and conexus's sweep of 3,675,603 edge
    log records over 2026-08-19..08-23 found zero 403s on this path. The real
    403s are deterministic AWS WAF refusals keyed on request-body content, and
    retrying them 5-8x with escalating brake amplified load against our own
    edge for requests that could never succeed.

    This file already knew: see the comments at the two tests below noting
    "403 is non-retryable at the vector classifier". The ETL path was the
    outlier, and this test was the pin holding it there.

    503 is genuinely transient (an ALB with no healthy target returns it), so
    the backoff assertions below are unchanged and still falsifiable.
    ``test_403_is_not_retryable_edge_refusals_are_deterministic`` pins the
    removal itself.
    """
    call_count = 0

    def flaky() -> str:
        nonlocal call_count
        call_count += 1
        if call_count < 2:
            raise _make_status_exc(503)
        return "ok"

    with patch("nexus.retry.time.sleep") as mock_sleep, patch(
        "nexus.retry.random.random", return_value=0.5,
    ):
        assert _etl_with_retry(flaky) == "ok"
    assert call_count == 2
    # nexus-cy9u7 CRITICAL-2: the brake now trips on this retry too (base
    # escalating default 2.0), floored above the local backoff's 1.0.
    mock_sleep.assert_called_once_with(2.0)


def test_403_is_not_retryable_edge_refusals_are_deterministic() -> None:
    """nexus-1jtob: the removal itself, pinned so it cannot silently return.

    A 403 on this path is either an AWS WAF refusal (deterministic in the
    request body -- the same payload is refused every time) or a control-plane
    authz verdict. Neither is transient. One attempt, no sleep.
    """
    for label, make in (
        ("httpx", lambda: _make_status_exc(403)),
        ("VectorServiceError", lambda: _make_vector_service_error(403)),
    ):
        call_count = 0

        def always_403() -> str:
            nonlocal call_count
            call_count += 1
            raise make()

        with patch("nexus.retry.time.sleep") as mock_sleep:
            with pytest.raises((httpx.HTTPStatusError, VectorServiceError)):
                _etl_with_retry(always_403)
        assert call_count == 1, f"{label}: a 403 must not be retried even once"
        mock_sleep.assert_not_called()


def test_backoff_curve_1_2() -> None:
    call_count = 0

    def fn_succeeds_on_3rd() -> str:
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise _make_status_exc(502)
        return "ok"

    with patch("nexus.retry.time.sleep") as mock_sleep, patch(
        "nexus.retry.random.random", return_value=0.5,
    ):
        assert _etl_with_retry(fn_succeeds_on_3rd, max_attempts=3) == "ok"
    # nexus-cy9u7 CRITICAL-2: brake floors both sleeps now (escalating
    # default 2.0 -> 4.0 across the two consecutive process-wide trips),
    # above the local 1.0 -> 2.0 curve.
    assert mock_sleep.call_args_list == [call(2.0), call(4.0)]


def test_exhausts_attempts_on_persistent_504() -> None:
    fn = MagicMock(side_effect=_make_status_exc(504))
    with patch("nexus.retry.time.sleep"), pytest.raises(httpx.HTTPStatusError):
        _etl_with_retry(fn, max_attempts=3)
    assert fn.call_count == 3


def test_non_retryable_404_raises_immediately() -> None:
    fn = MagicMock(side_effect=_make_status_exc(404))
    with patch("nexus.retry.time.sleep") as mock_sleep, pytest.raises(httpx.HTTPStatusError):
        _etl_with_retry(fn)
    fn.assert_called_once()
    mock_sleep.assert_not_called()


# ── nexus-cy9u7: shared RateLimitBrake wiring ────────────────────────────────


def test_429_trips_shared_brake_and_floors_sleep_at_retry_after(monkeypatch) -> None:
    fc = _FakeClock()
    test_brake = RateLimitBrake(clock=fc.time, sleep=fc.sleep, jitter=lambda: 0.0)
    monkeypatch.setattr(retry_mod, "get_brake", lambda: test_brake)

    call_count = 0

    def flaky() -> str:
        nonlocal call_count
        call_count += 1
        if call_count < 2:
            raise _make_status_exc(429, {"Retry-After": "6"})
        return "ok"

    with patch("nexus.retry.time.sleep") as mock_sleep, patch(
        "nexus.retry.random.random", return_value=0.5,
    ):
        assert _etl_with_retry(flaky) == "ok"
    assert test_brake.trips == 1
    assert test_brake.last_source == "etl"
    mock_sleep.assert_called_once_with(6.0)  # brake floor (6.0) > local backoff (1.0)


def test_503_without_retry_after_now_trips_brake_with_escalating_default(
    monkeypatch,
) -> None:
    """nexus-cy9u7 CRITICAL-2 fix (2026-08-16): renamed from
    ``test_503_without_retry_after_keeps_local_backoff_and_no_trip`` — a
    503 with no Retry-After is STILL a retryable transient failure and
    now trips the brake too, floored at the escalating default."""
    test_brake = MagicMock()
    test_brake.wait.return_value = 0.0
    test_brake.trip.return_value = 2.0  # escalating default, first trip
    call_count = 0

    def flaky() -> str:
        nonlocal call_count
        call_count += 1
        if call_count < 2:
            raise _make_status_exc(503)  # no Retry-After header
        return "ok"

    with patch.object(retry_mod, "get_brake", return_value=test_brake):
        with patch("nexus.retry.time.sleep") as mock_sleep, patch(
            "nexus.retry.random.random", return_value=0.5,
        ):
            assert _etl_with_retry(flaky) == "ok"
    test_brake.trip.assert_called_once_with(None, source="etl")
    mock_sleep.assert_called_once_with(2.0)  # max(local 1.0, brake 2.0)


def test_success_releases_brake() -> None:
    test_brake = MagicMock()
    test_brake.wait.return_value = 0.0
    with patch.object(retry_mod, "get_brake", return_value=test_brake):
        fn = MagicMock(return_value="ok")
        with patch("nexus.retry.time.sleep"):
            assert _etl_with_retry(fn) == "ok"
    test_brake.wait.assert_called_once()
    test_brake.release.assert_called_once()


def test_rate_limit_signal_classification_unchanged() -> None:
    # Sanity: _is_retryable_etl_error itself is untouched by the brake wiring.
    assert _is_retryable_etl_error(_make_status_exc(429)) is True
    assert _is_retryable_etl_error(_make_status_exc(404)) is False


# ── nexus-cy9u7 CRITICAL-1: the REAL db/reconcile.py caller shape ───────────
#
# ``db/reconcile.py:664`` wraps ``vector_client.upsert_chunks`` (urllib-based,
# raises VectorServiceError) via ``_etl_batch_with_breaker`` -> this wrapper —
# NOT the httpx shape every other test in this file uses. This is the
# "Medium: reconcile.py:664 is covered by the same detection fix; verify"
# item from the code review.


def test_real_vector_service_error_429_recognised_with_retry_after(monkeypatch) -> None:
    fc = _FakeClock()
    test_brake = RateLimitBrake(clock=fc.time, sleep=fc.sleep, jitter=lambda: 0.0)
    monkeypatch.setattr(retry_mod, "get_brake", lambda: test_brake)

    fn = MagicMock(side_effect=[_make_vector_service_error(429, retry_after="4"), "ok"])
    with patch("nexus.retry.time.sleep") as mock_sleep, patch(
        "nexus.retry.random.random", return_value=0.5,
    ):
        assert _etl_with_retry(fn) == "ok"
    assert test_brake.trips == 1
    assert test_brake.last_retry_after == 4.0
    assert test_brake.last_source == "etl"
    mock_sleep.assert_called_once_with(4.0)


def test_real_vector_service_error_502_retried_via_duck_typed_code() -> None:
    # No Retry-After -> not a narrow rate-limit signal, but still a
    # retryable transient status (502) per _RETRYABLE_ETL_HTTP_STATUSES.
    fn = MagicMock(side_effect=[_make_vector_service_error(502), "ok"])
    with patch("nexus.retry.time.sleep"):
        assert _etl_with_retry(fn) == "ok"
    assert fn.call_count == 2


# ── nexus-cy9u7 round-3 CRITICAL C2: single-retry-layer composition ─────────
#
# db/reconcile.py:664's verify-fill call site wraps
# HttpVectorClient.upsert_chunks in _etl_batch_with_breaker (-> this
# module's _etl_with_retry). Pre-fix, upsert_chunks ALSO self-retried via
# its own _vector_with_retry — three nested retry layers on one failure
# (this breaker/etl stack, upsert_chunks's own wrap, and _request's inner
# gateway retry underneath both), each independently tripping/escalating
# the shared rate-limit brake. The fix: upsert_chunks(retry=False) skips
# ITS OWN wrap; reconcile.py's call site passes it. This test drives the
# REAL HttpVectorClient through a real loopback server — the same
# real-server pattern as tests/test_vector_retry.py's ``upsert_server``
# fixture, duplicated here per that file's own "test-only infra stays
# local" convention — proving exactly ONE retry layer is active.


class _UpsertChunksHandler(http.server.BaseHTTPRequestHandler):
    responses: list[tuple[int, dict[str, str], dict]] = []
    call_count = 0

    def do_POST(self) -> None:  # noqa: N802 — stdlib callback name
        length = int(self.headers.get("Content-Length", 0))
        req_body = json.loads(self.rfile.read(length)) if length else {}
        idx = min(type(self).call_count, len(type(self).responses) - 1)
        status, headers, body = type(self).responses[idx]
        type(self).call_count += 1
        if body is None:
            body = {"upserted": len(req_body.get("ids", []))}
        payload = json.dumps(body).encode()
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_a: object) -> None:
        pass


@pytest.fixture
def upsert_server():
    _UpsertChunksHandler.call_count = 0
    _UpsertChunksHandler.responses = []
    httpd = http.server.HTTPServer(("127.0.0.1", 0), _UpsertChunksHandler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield httpd, _UpsertChunksHandler
    httpd.shutdown()
    httpd.server_close()


def test_verify_fill_call_site_uses_exactly_one_retry_layer_real_server(
    upsert_server, monkeypatch, _isolate_default_brake,
) -> None:
    """The reconcile-style call — ``_etl_batch_with_breaker`` wrapping
    ``upsert_chunks(retry=False)`` — sees EXACTLY the attempt count
    ``_etl_with_retry``'s OWN budget (default 3: 2 failures + 1 success)
    produces. The stimulus is a 429: retryable at BOTH the etl layer
    (``_RETRYABLE_ETL_HTTP_STATUSES``) and ``_is_retryable_vector_error``,
    yet absent from ``_request``'s inner ``_GATEWAY_RETRY_CODES``
    ({502, 503, 504}) — so the only two candidate retry owners are the two
    this test discriminates between. (A 403 stimulus, used in an earlier
    revision, was NOT falsifiable: 403 is non-retryable at the vector
    classifier, so ``_vector_with_retry`` contributed zero attempts whether
    or not ``retry=False`` was honored — round-3 review Critical.)

    The discriminating assertion is the brake's ``last_source``: with
    ``retry=False`` honored, both 429 failures surface to (and trip the
    brake from) the ETL layer — ``source="etl"``. Were ``upsert_chunks``'s
    own ``_vector_with_retry`` ALSO active (the pre-fix bug — drop
    ``retry=False`` to reproduce), the failures would be absorbed INSIDE
    ``upsert_chunks`` and the trips would carry ``source="vector"``
    instead, with ``_etl_with_retry`` never observing a failure."""
    from nexus.db import http_vector_client as hvc

    httpd, handler = upsert_server
    handler.responses = [
        (429, {}, {"error": "rate limited"}),
        (429, {}, {"error": "rate limited"}),
        (200, {}, None),  # None -> handler echoes the real upserted-count ack
    ]
    host, port = httpd.server_address
    monkeypatch.setattr(hvc, "_resolve_endpoint", lambda: (f"http://{host}:{port}", "tok"))

    with patch("nexus.retry.time.sleep"), patch(
        "nexus.retry.random.random", return_value=0.5,
    ):
        client = hvc.HttpVectorClient()
        breaker = EtlCircuitBreaker()
        _etl_batch_with_breaker(
            client.upsert_chunks,
            "code__test", ["id-1"], ["def hello(): pass"], [{"k": "v"}],
            breaker=breaker,
            retry=False,
        )

    assert handler.call_count == 3
    # Ownership proof (the falsifiable core): both trips came from the ETL
    # layer. Under the pre-fix nested-retry bug these would be
    # source="vector" — that is exactly the regression this pins.
    assert _isolate_default_brake.trips == 2
    assert _isolate_default_brake.last_source == "etl"
    # The batch succeeded within _etl_with_retry's own budget — the
    # breaker's pause/trip machinery (a SEPARATE, higher-level concern)
    # was never even engaged.
    assert breaker.consecutive_failures == 0
    assert breaker.trip_count == 0


def test_upsert_chunks_retry_false_does_not_self_retry_real_server(
    upsert_server, monkeypatch,
) -> None:
    """Direct proof of the opt-out contract in isolation (no etl layer at
    all): ``upsert_chunks(retry=False)`` makes exactly ONE attempt and
    raises on the first failure — its own ``_vector_with_retry`` wrap is
    genuinely skipped, not merely reduced. The stimulus is a 429
    precisely BECAUSE ``_vector_with_retry`` WOULD retry it (it is in the
    vector classifier's retryable set and absent from ``_request``'s
    gateway codes): if ``retry=False`` were silently ignored, the call
    count would exceed 1. (The earlier 403 stimulus was unfalsifiable —
    403 is non-retryable at the vector classifier, so a single attempt
    proved nothing about the opt-out; round-3 review Critical.)"""
    from nexus.db import http_vector_client as hvc

    httpd, handler = upsert_server
    handler.responses = [(429, {}, {"error": "rate limited"})]
    host, port = httpd.server_address
    monkeypatch.setattr(hvc, "_resolve_endpoint", lambda: (f"http://{host}:{port}", "tok"))

    with patch("nexus.retry.time.sleep") as mock_sleep, pytest.raises(VectorServiceError):
        client = hvc.HttpVectorClient()
        client.upsert_chunks(
            "code__test", ["id-1"], ["def hello(): pass"], [{"k": "v"}], retry=False,
        )

    assert handler.call_count == 1  # no self-retry
    mock_sleep.assert_not_called()
