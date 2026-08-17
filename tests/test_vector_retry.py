# SPDX-License-Identifier: AGPL-3.0-or-later
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
from nexus.retry import _is_retryable_vector_error, _vector_with_retry


class _FakeClock:
    """Self-advancing fake clock: sleep() immediately advances `now` and
    returns — no real time.sleep, deterministic (nexus-cy9u7)."""

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
    failure (not only a narrow 429/503+Retry-After signal), so a test
    that doesn't explicitly inject its own brake would otherwise drive the
    REAL process-wide default (real clock, real ``time.sleep``) into a
    genuine multi-second sleep the moment it retries anything. Install a
    fast, deterministic fake-clocked brake as the default for every test
    in this module; a test that needs to inspect a SPECIFIC brake instance
    (its own ``monkeypatch.setattr(retry_mod, "get_brake", ...)``) simply
    overrides this after the fixture runs.
    """
    fc = _FakeClock()
    fake_brake = RateLimitBrake(clock=fc.time, sleep=fc.sleep, jitter=lambda: 0.0)
    monkeypatch.setattr(retry_mod, "get_brake", lambda: fake_brake)
    yield fake_brake
    reset_brake()


def _make_vector_service_error(
    status: int, retry_after: str | None = None,
) -> VectorServiceError:
    """Construct a ``VectorServiceError`` EXACTLY the way
    ``http_vector_client.py``'s ``_post``/``_get`` raise it in production:
    from a real ``urllib.error.HTTPError``, via ``raise
    VectorServiceError(...) from e`` inside the ``except
    urllib.error.HTTPError as e:`` block — so ``__cause__``/``__context__``
    genuinely carry the original HTTPError (headers included), not a
    hand-set attribute on an unrelated synthetic Exception (nexus-cy9u7:
    the code-review finding that the brake-wiring tests below were testing
    a fictional httpx-shaped exception the real urllib-based vector client
    never raises)."""
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


# ── _is_retryable_vector_error ──────────────────────────────────────────────

def _make_chained_exc(status_code: int) -> Exception:
    """httpx-shaped chained exception — a legitimate, still-supported
    classifier input (``_is_retryable_vector_error`` genuinely recognises
    the httpx.HTTPStatusError family), used here for the classifier's
    generic-contract tests only. NOT used below to represent what the
    production T3 vector client raises — see
    :func:`_make_vector_service_error` for that."""
    request = httpx.Request("GET", "https://api.trychroma.com/")
    response = httpx.Response(status_code=status_code, request=request)
    http_err = httpx.HTTPStatusError(
        f"Server error '{status_code}'", request=request, response=response
    )
    plain_exc = Exception(f"<html>Gateway error {status_code}</html>")
    plain_exc.__context__ = http_err
    return plain_exc


@pytest.mark.parametrize("exc,expected", [
    (Exception("504 Gateway Time-out HTML"), True),
    (Exception("400 Bad Request: invalid payload"), False),
    (httpx.ConnectError("Connection refused"), True),
    (httpx.ReadTimeout("Read timed out"), True),
    (httpx.RemoteProtocolError("Server disconnected without response"), True),
], ids=["504-string", "400-string", "connect-error", "read-timeout", "remote-protocol"])
def test_retryable_basic(exc: Exception, expected: bool) -> None:
    assert _is_retryable_vector_error(exc) is expected


@pytest.mark.parametrize("status,expected", [
    (429, True), (404, False), (503, True),
], ids=["429-retryable", "404-not", "503-retryable"])
def test_retryable_chained_httpx(status: int, expected: bool) -> None:
    assert _is_retryable_vector_error(_make_chained_exc(status)) is expected


@pytest.mark.parametrize("status,expected", [
    (429, True), (404, False), (500, False), (502, True), (503, True), (504, True),
], ids=["429", "404", "500", "502", "503", "504"])
def test_retryable_real_vector_service_error_shape(status: int, expected: bool) -> None:
    # nexus-cy9u7 CRITICAL-1: this is the REAL production shape — a
    # VectorServiceError chained from urllib.error.HTTPError — the
    # classifier must recognise it authoritatively by status code, not by
    # accidentally string-matching digits in the message.
    assert _is_retryable_vector_error(_make_vector_service_error(status)) is expected


# ── nexus-cy9u7 round-3 CRITICAL C1 ──────────────────────────────────────────
#
# The production HttpVectorClient path is urllib-based. A connectivity blip
# (no HTTP response at all) surfaces in TWO real shapes, both reproduced here
# exactly as ``http_vector_client._post`` raises them:
#
# 1. LOCAL/lease topology: ``_managed_remedy()`` returns None, so ``_post``
#    re-raises the bare urllib/stdlib exception UNTOUCHED (no
#    VectorServiceError wrapper at all).
# 2. Managed-endpoint topology: ``_managed_remedy()`` returns a remedy
#    string, so ``_post`` wraps it as ``VectorServiceError(msg, code=None)
#    from e`` — no ``.code`` (unlike the HTTPError-chained shape above,
#    which always carries an int status).
#
# Pre-fix, NEITHER shape was recognised: shape 1 isn't an httpx.TransportError
# (step 2 of the classifier), and shape 2's ``VectorServiceError`` has no
# chained HTTPError/httpx.HTTPStatusError and no int ``.code`` for step 3 to
# find — both fell through to the string-fallback, which never matches a
# socket-level error message.


def _make_connectivity_vector_service_error(
    exc: BaseException,
) -> VectorServiceError:
    """The managed-endpoint shape: ``VectorServiceError(msg, code=None) from
    e`` where *e* is a bare connectivity error — mirrors ``_post``'s
    ``except (urllib.error.URLError, ConnectionError, TimeoutError) as e``
    branch when ``_managed_remedy()`` supplies a remedy."""
    try:
        raise exc
    except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
        try:
            raise VectorServiceError(f"POST /v1/vectors/upsert-chunks failed: {e}") from e
        except VectorServiceError as vse:
            return vse


@pytest.mark.parametrize("exc", [
    urllib.error.URLError("Connection refused"),
    TimeoutError("timed out"),
    ConnectionError("connection reset"),
    ConnectionRefusedError("refused"),
], ids=["url-error", "timeout-error", "connection-error", "connection-refused"])
def test_retryable_bare_urllib_connectivity_error(exc: BaseException) -> None:
    """Shape 1 (local/lease topology): the raw urllib/stdlib exception,
    never wrapped — must be retryable."""
    assert _is_retryable_vector_error(exc) is True


@pytest.mark.parametrize("exc", [
    urllib.error.URLError("Connection refused"),
    TimeoutError("timed out"),
    ConnectionError("connection reset"),
], ids=["url-error", "timeout-error", "connection-error"])
def test_retryable_vector_service_error_code_none_from_connectivity(
    exc: BaseException,
) -> None:
    """Shape 2 (managed-endpoint topology): VectorServiceError(code=None)
    chained from a bare connectivity error — must be retryable via the
    chained-cause check, not via any status-code lookup (there is none)."""
    wrapped = _make_connectivity_vector_service_error(exc)
    assert wrapped.code is None  # confirms this is genuinely the code=None shape
    assert _is_retryable_vector_error(wrapped) is True


def test_real_http_error_500_not_retryable_despite_urlerror_subclass() -> None:
    """Regression guard for the ordering fix: urllib.error.HTTPError IS a
    URLError subclass, so the new bare-connectivity check (3b) must run
    AFTER the authoritative status-code check (3) — a real 500 must stay
    non-retryable, not incorrectly match the blanket URLError branch."""
    assert _is_retryable_vector_error(_make_vector_service_error(500)) is False


# ── _vector_with_retry ──────────────────────────────────────────────────────

def test_retry_connect_error_twice_then_success() -> None:
    call_count = 0
    def flaky_fn() -> str:
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise httpx.ConnectError("transient connect failure")
        return "ok"
    # nexus-8g79.32: pin random.random()=0.5 so jittered delay equals
    # the deterministic base (jitter factor = 1 + (0.5 - 0.5) * 0.4 = 1.0).
    with patch("nexus.retry.time.sleep") as mock_sleep, patch(
        "nexus.retry.random.random", return_value=0.5,
    ):
        result = _vector_with_retry(flaky_fn)
    assert result == "ok" and call_count == 3
    # nexus-cy9u7 CRITICAL-2: the shared brake now trips on every retry too
    # (base_delay_seconds=2.0 matches this wrapper's own starting delay and
    # both escalate by doubling, so the brake floor and local backoff are
    # numerically identical here — see the module docstring's worked bound).
    assert mock_sleep.call_args_list == [call(2.0), call(4.0)]


def test_all_attempts_exhausted_on_persistent_504() -> None:
    fn = MagicMock(side_effect=Exception("504 Gateway Time-out"))
    with patch("nexus.retry.time.sleep"), pytest.raises(Exception, match="504"):
        _vector_with_retry(fn, max_attempts=5)
    assert fn.call_count == 5


def test_non_retryable_400_raises_immediately() -> None:
    fn = MagicMock(side_effect=Exception("400 Bad Request: invalid collection name"))
    with patch("nexus.retry.time.sleep") as mock_sleep, pytest.raises(Exception, match="400"):
        _vector_with_retry(fn)
    fn.assert_called_once()
    mock_sleep.assert_not_called()


def test_backoff_curve_2_4_8_16() -> None:
    call_count = 0
    def fn_succeeds_on_5th() -> str:
        nonlocal call_count
        call_count += 1
        if call_count < 5:
            raise Exception("503 Service Unavailable")
        return "done"
    # nexus-8g79.32: pin random.random()=0.5 so jitter = 1.0.
    with patch("nexus.retry.time.sleep") as mock_sleep, patch(
        "nexus.retry.random.random", return_value=0.5,
    ):
        assert _vector_with_retry(fn_succeeds_on_5th, max_attempts=5) == "done"
    assert mock_sleep.call_args_list == [call(2.0), call(4.0), call(8.0), call(16.0)]


# ── Integration: retry through public API ───────────────────────────────────

@pytest.fixture
def t3_mock():
    # RDR-155 P4a.2 (nexus-1k8s1): the CloudClient construction is retired —
    # inject the mock client directly (the retry machinery under test is
    # downstream of construction and unchanged).
    # nexus-sghyo (2026-08-06): client-side Voyage embedding is retired
    # outright (nexus.db.voyage_ef deleted) — a cloud-mode collection
    # with no _ef_override now raises at EF-construction time
    # (IncompatibleCollectionError). Pass _ef_override explicitly so
    # collection-name dispatch never reaches that raise; the retry
    # machinery under test is upstream of embedding anyway.
    mock_client = MagicMock()
    from nexus.db.t3 import T3Database
    yield T3Database(
        tenant="t", database="d", api_key="k", _client=mock_client,
        _ef_override=MagicMock(),
    ), mock_client


def test_search_retries_on_503(t3_mock) -> None:
    db, mock_client = t3_mock
    mock_col = MagicMock()
    mock_client.get_collection.return_value = mock_col
    mock_col.count.return_value = 2
    mock_col.query.side_effect = [
        _make_chained_exc(503),
        {"ids": [["id-1"]], "documents": [["content"]],
         "metadatas": [[{"source_path": "f.py"}]], "distances": [[0.1]]},
    ]
    assert len(db.search("query text", ["code__myrepo"])) == 1


def test_write_batch_retries_on_504(t3_mock) -> None:
    db, _ = t3_mock
    mock_col = MagicMock()
    mock_col.upsert.side_effect = [Exception("504 Gateway Time-out"), None]
    db._write_batch(mock_col, "code__myrepo", ["id-1"], ["def hello(): pass"],
                    [{"source_path": "hello.py"}])
    assert mock_col.upsert.call_count == 2


def test_list_store_retries_on_read_timeout(t3_mock) -> None:
    db, mock_client = t3_mock
    mock_col = MagicMock()
    mock_client.get_collection.return_value = mock_col
    mock_col.get.side_effect = [
        httpx.ReadTimeout("timed out"),
        {"ids": ["id-1"], "metadatas": [{"title": "finding.md", "tags": "",
         "ttl_days": 0, "expires_at": "", "indexed_at": "2026-01-01T00:00:00+00:00"}]},
    ]
    assert len(db.list_store("knowledge__mystore")) == 1


def test_index_code_file_retries_on_connect_error(tmp_path) -> None:
    from nexus.indexer import _index_code_file
    src = tmp_path / "hello.py"
    src.write_text("def hello(): pass\n")
    mock_col = MagicMock()
    mock_col.get.side_effect = [httpx.ConnectError("connection refused"),
                                {"ids": [], "metadatas": []}]
    mock_voyage = MagicMock()
    mock_voyage.embed.return_value = MagicMock(embeddings=[[0.1, 0.2]])
    result = _index_code_file(file=src, repo=tmp_path, collection_name="code__myrepo",
                              target_model="voyage-code-3", col=mock_col, db=MagicMock(),
                              voyage_client=mock_voyage, git_meta={},
                              now_iso="2026-01-01T00:00:00+00:00", score=1.0)
    assert result >= 0 and mock_col.get.call_count == 2


# ── RDR-020 regression: disjoint from Voyage AI ────────────────────────────

@pytest.mark.parametrize("exc,expected", [
    (httpx.ConnectError("refused"), True),
    (httpx.ReadTimeout("timeout"), True),
    (_make_chained_exc(503), True),
    (_make_chained_exc(429), True),
])
def test_chroma_error_unchanged(exc, expected) -> None:
    assert _is_retryable_vector_error(exc) is expected


def test_chroma_error_false_for_voyage_error() -> None:
    import voyageai.error as _ve
    assert _is_retryable_vector_error(_ve.APIConnectionError("down")) is False


# ── Transport-stall retryability (was nexus-jgjw's end-to-end leg) ──────────
#
# RDR-155 P4b P3: the surrounding TestChromadbTimeoutPatch class tested
# T3Database's override of chromadb's hardcoded ``httpx.Client(timeout=None)``
# (chromadb/api/fastapi.py:86,91). That override — and the client shape it
# reached into (``_server._session``) — were deleted with the chroma client
# legs; T3Database can no longer be handed such a client. The two tests that
# asserted the override itself died with it.
#
# This leg survives because it never depended on chroma: a stalled transport
# surfacing as ``httpx.ReadTimeout`` must still be classified retryable and
# must still be retried by ``_vector_with_retry``. That is a live contract for
# the HTTP vector client today.


def test_transport_readtimeout_is_retryable_and_retried() -> None:
    readtimeout = httpx.ReadTimeout("simulated transport stall")
    assert _is_retryable_vector_error(readtimeout) is True
    fn = MagicMock(side_effect=[readtimeout, readtimeout, "ok"])
    assert _vector_with_retry(fn, max_attempts=3) == "ok"
    assert fn.call_count == 3


# ── nexus-cy9u7: shared RateLimitBrake wiring ────────────────────────────────
#
# Every exception below is constructed via :func:`_make_vector_service_error`
# — the REAL shape ``HttpVectorClient`` raises (VectorServiceError chained
# from a real urllib.error.HTTPError) — replacing the pre-fix tests that used
# a synthetic httpx-shaped exception the vector client never actually
# produces (code-review finding, nexus-cy9u7).


def test_429_trips_shared_brake_and_floors_sleep_at_retry_after(monkeypatch) -> None:
    fc = _FakeClock()
    test_brake = RateLimitBrake(clock=fc.time, sleep=fc.sleep, jitter=lambda: 0.0)
    monkeypatch.setattr(retry_mod, "get_brake", lambda: test_brake)

    call_count = 0

    def flaky() -> str:
        nonlocal call_count
        call_count += 1
        if call_count < 2:
            raise _make_vector_service_error(429, retry_after="5")
        return "ok"

    # Local jittered backoff (2.0) < retry_after (5.0) -> sleep_for uses the
    # brake's floor.
    with patch("nexus.retry.time.sleep") as mock_sleep, patch(
        "nexus.retry.random.random", return_value=0.5,
    ):
        assert _vector_with_retry(flaky) == "ok"
    assert call_count == 2
    assert test_brake.trips == 1
    assert test_brake.last_retry_after == 5.0
    assert test_brake.last_source == "vector"
    mock_sleep.assert_called_once_with(5.0)


def test_second_caller_pays_the_first_callers_shared_pause(monkeypatch) -> None:
    """One caller's 429 trips the brake; a SECOND, unrelated caller (whose
    own function never fails) still pays the shared pause on its very
    first attempt — proving the brake is genuinely shared, not per-call
    state."""
    fc = _FakeClock()
    test_brake = RateLimitBrake(clock=fc.time, sleep=fc.sleep, jitter=lambda: 0.0)
    monkeypatch.setattr(retry_mod, "get_brake", lambda: test_brake)

    call_count_a = 0

    def caller_a_fn() -> str:
        nonlocal call_count_a
        call_count_a += 1
        if call_count_a < 2:
            raise _make_vector_service_error(429, retry_after="3")
        return "a-ok"

    with patch("nexus.retry.time.sleep"), patch(
        "nexus.retry.random.random", return_value=0.5,
    ):
        assert _vector_with_retry(caller_a_fn) == "a-ok"
    assert test_brake.trips == 1
    resume_at_after_a = test_brake._resume_at
    assert resume_at_after_a > 0.0

    caller_b_fn = MagicMock(return_value="b-ok")
    with patch("nexus.retry.time.sleep"):
        assert _vector_with_retry(caller_b_fn) == "b-ok"
    # Caller B's fn never raised, so any pause it paid came only from
    # brake.wait() consuming the shared deadline A set — proven by the
    # fake clock having caught up to (at least) that deadline.
    assert fc.time() >= resume_at_after_a


def test_503_without_retry_after_now_trips_brake_with_escalating_default(
    monkeypatch,
) -> None:
    """nexus-cy9u7 CRITICAL-2 fix (2026-08-16): renamed from
    ``test_503_without_retry_after_does_not_trip_brake`` — that was the
    OLD, narrow-scope behaviour. A 503 with no Retry-After is STILL a
    retryable transient failure (an overloaded upstream regardless of
    whether it characterised its own pause), so it now trips the brake
    too, floored at the brake's own escalating default rather than a
    server-supplied value."""
    test_brake = MagicMock()
    test_brake.wait.return_value = 0.0
    test_brake.trip.return_value = 2.0  # escalating default, first trip
    monkeypatch.setattr(retry_mod, "get_brake", lambda: test_brake)

    call_count = 0

    def flaky() -> str:
        nonlocal call_count
        call_count += 1
        if call_count < 2:
            raise _make_vector_service_error(503)  # no Retry-After header
        return "ok"

    with patch("nexus.retry.time.sleep") as mock_sleep, patch(
        "nexus.retry.random.random", return_value=0.5,
    ):
        assert _vector_with_retry(flaky) == "ok"
    test_brake.trip.assert_called_once_with(None, source="vector")
    mock_sleep.assert_called_once_with(2.0)  # max(local jittered 2.0, brake 2.0)


def test_502_no_retry_after_trips_brake_and_is_retried(monkeypatch) -> None:
    """A 502 with no Retry-After is the LITERAL 2026-08-15 incident shape
    (engine retrying Voyage internally; the edge's own timeout surfaces to
    the client as a 502/504 with no Retry-After at all) — must trip the
    brake exactly like 429/503 do."""
    test_brake = MagicMock()
    test_brake.wait.return_value = 0.0
    test_brake.trip.return_value = 2.0
    monkeypatch.setattr(retry_mod, "get_brake", lambda: test_brake)

    fn = MagicMock(side_effect=[_make_vector_service_error(502), "ok"])
    with patch("nexus.retry.time.sleep"), patch(
        "nexus.retry.random.random", return_value=0.5,
    ):
        assert _vector_with_retry(fn) == "ok"
    test_brake.trip.assert_called_once_with(None, source="vector")


def test_500_does_not_trip_brake_and_is_not_retried(monkeypatch) -> None:
    """A 500 is not in the retryable set at all — no trip, no retry,
    immediate raise (S2 test case)."""
    test_brake = MagicMock()
    test_brake.wait.return_value = 0.0
    monkeypatch.setattr(retry_mod, "get_brake", lambda: test_brake)

    fn = MagicMock(side_effect=_make_vector_service_error(500))
    with patch("nexus.retry.time.sleep") as mock_sleep, pytest.raises(VectorServiceError):
        _vector_with_retry(fn)
    fn.assert_called_once()
    mock_sleep.assert_not_called()
    test_brake.trip.assert_not_called()


def test_success_calls_brake_release() -> None:
    test_brake = MagicMock()
    test_brake.wait.return_value = 0.0
    with patch.object(retry_mod, "get_brake", return_value=test_brake):
        fn = MagicMock(return_value="ok")
        with patch("nexus.retry.time.sleep"):
            assert _vector_with_retry(fn) == "ok"
    test_brake.wait.assert_called_once()
    test_brake.release.assert_called_once()


def test_exhaustion_does_not_strand_brake_escalated_forever(monkeypatch) -> None:
    """nexus-cy9u7 round-3 CRITICAL C2 (decided + documented in
    rate_brake.py): a wrapper that exhausts its attempt budget and raises
    never calls brake.release() (release is success-only by design) — but
    the VERY NEXT call to succeed still resets escalation via its own
    release(), so a subsequent healthy call recovers immediately rather
    than staying escalated forever."""
    fc = _FakeClock()
    test_brake = RateLimitBrake(clock=fc.time, sleep=fc.sleep, jitter=lambda: 0.0)
    monkeypatch.setattr(retry_mod, "get_brake", lambda: test_brake)
    # The wrapper's OWN per-attempt retry-sleep (distinct from brake.wait())
    # must route through the same fake clock, or this test would sleep for
    # real — see the identical pattern in test_upsert_chunks_concurrent_
    # second_call_waits_for_shared_pause below.
    monkeypatch.setattr(retry_mod.time, "sleep", fc.sleep)

    # First call: persistently fails, exhausts max_attempts, raises. Each
    # attempt trips the brake and escalates (2.0 -> 4.0 -> ... over 4 sleeps).
    failing_fn = MagicMock(side_effect=Exception("503 Service Unavailable"))
    with patch("nexus.retry.random.random", return_value=0.5), pytest.raises(Exception, match="503"):
        _vector_with_retry(failing_fn, max_attempts=5)
    assert test_brake.trips == 4  # 4 sleeps before the 5th (final) attempt raises
    escalated_delay = test_brake._last_delay
    assert escalated_delay > test_brake._base_delay_seconds  # genuinely escalated

    # Second call: succeeds on the FIRST attempt (upstream has recovered).
    # It still calls brake.release() in its success branch.
    healthy_fn = MagicMock(return_value="ok")
    with patch("nexus.retry.random.random", return_value=0.5):
        assert _vector_with_retry(healthy_fn) == "ok"

    # Recovery proof: a FRESH trip after the healthy call starts back at the
    # base delay, not continuing the prior escalation.
    assert test_brake.trip(None, source="vector") == test_brake._base_delay_seconds


def test_non_retryable_error_does_not_touch_brake() -> None:
    test_brake = MagicMock()
    test_brake.wait.return_value = 0.0
    with patch.object(retry_mod, "get_brake", return_value=test_brake):
        fn = MagicMock(side_effect=Exception("400 Bad Request"))
        with patch("nexus.retry.time.sleep"), pytest.raises(Exception, match="400"):
            _vector_with_retry(fn)
    test_brake.trip.assert_not_called()
    test_brake.release.assert_not_called()


# ── nexus-cy9u7 round-3 CRITICAL C1: _vector_with_retry on connectivity ─────
#
# The classifier fix alone (above) isn't enough evidence — these drive the
# REAL shapes through the retry wrapper end-to-end: retried, brake-tripped
# (connectivity errors are in the unconditional-trip set, same as any other
# retryable failure), and eventually succeeding.


def test_vector_with_retry_retries_on_bare_timeout_error() -> None:
    fn = MagicMock(side_effect=[TimeoutError("timed out"), TimeoutError("timed out"), "ok"])
    with patch("nexus.retry.time.sleep"):
        assert _vector_with_retry(fn, max_attempts=3) == "ok"
    assert fn.call_count == 3


def test_vector_with_retry_retries_on_vector_service_error_code_none(monkeypatch) -> None:
    """VectorServiceError(code=None) chained from a connectivity error
    (the managed-endpoint shape) must retry and eventually succeed."""
    wrapped = _make_connectivity_vector_service_error(
        urllib.error.URLError("Connection refused")
    )
    fn = MagicMock(side_effect=[wrapped, "ok"])
    with patch("nexus.retry.time.sleep"):
        assert _vector_with_retry(fn, max_attempts=3) == "ok"
    assert fn.call_count == 2


def test_connectivity_error_trips_shared_brake(monkeypatch) -> None:
    """Bare connectivity errors are in CRITICAL-2's unconditional-trip set
    (every retryable failure trips the brake), not exempted just because
    they carry no HTTP status at all."""
    test_brake = MagicMock()
    test_brake.wait.return_value = 0.0
    test_brake.trip.return_value = 2.0
    monkeypatch.setattr(retry_mod, "get_brake", lambda: test_brake)

    fn = MagicMock(side_effect=[ConnectionError("reset"), "ok"])
    with patch("nexus.retry.time.sleep"), patch(
        "nexus.retry.random.random", return_value=0.5,
    ):
        assert _vector_with_retry(fn) == "ok"
    test_brake.trip.assert_called_once_with(None, source="vector")


# ── nexus-cy9u7 CRITICAL-1: end-to-end through the REAL HttpVectorClient ────
#
# A stub HTTP server (real loopback socket, port 0) drives
# HttpVectorClient.upsert_chunks through its ACTUAL urllib transport — the
# gap the code review found (the brake wiring was verified only against a
# synthetic exception, never against what the real client raises).


class _UpsertChunksHandler(http.server.BaseHTTPRequestHandler):
    #: class-level so the fixture can reset it and the test can assert on it.
    responses: list[tuple[int, dict[str, str], dict]] = []
    call_count = 0

    def do_POST(self) -> None:  # noqa: N802 — stdlib callback name
        length = int(self.headers.get("Content-Length", 0))
        req_body = json.loads(self.rfile.read(length)) if length else {}
        idx = min(type(self).call_count, len(type(self).responses) - 1)
        status, headers, body = type(self).responses[idx]
        type(self).call_count += 1
        if body is None:
            # Echo the ack the real engine sends: upserted == len(ids).
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


def test_upsert_chunks_429_retry_after_then_success_real_server(
    upsert_server, monkeypatch,
) -> None:
    from nexus.db import http_vector_client as hvc

    httpd, handler = upsert_server
    handler.responses = [
        (429, {"Retry-After": "2"}, {"error": "rate limited"}),
        (200, {}, None),  # None -> handler echoes the real upserted-count ack
    ]
    host, port = httpd.server_address
    monkeypatch.setattr(hvc, "_resolve_endpoint", lambda: (f"http://{host}:{port}", "tok"))

    fc = _FakeClock()
    test_brake = RateLimitBrake(clock=fc.time, sleep=fc.sleep, jitter=lambda: 0.0)
    monkeypatch.setattr(retry_mod, "get_brake", lambda: test_brake)

    # nexus-8g79.32: pin random.random()=0.5 so the local jittered backoff
    # (2.0) equals the brake floor exactly (both start at 2.0) — otherwise
    # jitter can push the local value above 2.0, and max() would pick the
    # (non-deterministic) local value instead of proving Retry-After won.
    with patch("nexus.retry.time.sleep") as mock_sleep, patch(
        "nexus.retry.random.random", return_value=0.5,
    ):
        client = hvc.HttpVectorClient()
        client.upsert_chunks("code__test", ["id-1"], ["def hello(): pass"], [{"k": "v"}])

    assert handler.call_count == 2
    assert test_brake.trips == 1
    assert test_brake.last_retry_after == 2.0
    assert test_brake.last_source == "vector"
    mock_sleep.assert_called_once_with(2.0)  # Retry-After honoured, no real sleep


def test_upsert_chunks_504_no_retry_after_escalating_default_real_server(
    upsert_server, monkeypatch,
) -> None:
    """A 502/503/504 is ALSO retried at a LOWER layer first —
    ``_request``'s own ``_GATEWAY_RETRY_CODES`` bounded backoff (2s/5s/10s,
    3 extra attempts) inside ``http_vector_client.py``, independent of
    ``_vector_with_retry`` — so this test supplies enough 504 responses to
    exhaust THAT budget too (4 attempts) before the brake-tripping outer
    retry gets a chance to see the failure at all. Both layers' sleeps are
    faked so nothing here actually blocks."""
    from nexus.db import http_vector_client as hvc

    httpd, handler = upsert_server
    handler.responses = [
        (504, {}, {"error": "gateway timeout"}),
        (504, {}, {"error": "gateway timeout"}),
        (504, {}, {"error": "gateway timeout"}),
        (504, {}, {"error": "gateway timeout"}),  # exhausts _request's own gateway retry
        (200, {}, None),  # _vector_with_retry's own retry, attempt 2
    ]
    host, port = httpd.server_address
    monkeypatch.setattr(hvc, "_resolve_endpoint", lambda: (f"http://{host}:{port}", "tok"))

    fc = _FakeClock()
    test_brake = RateLimitBrake(clock=fc.time, sleep=fc.sleep, jitter=lambda: 0.0)
    monkeypatch.setattr(retry_mod, "get_brake", lambda: test_brake)

    # ``nexus.retry.time`` and ``nexus.db.http_vector_client.time`` are the
    # SAME stdlib ``time`` module object — patching ``nexus.retry.time.sleep``
    # mutates the module attribute both modules see, so this ALSO fakes the
    # lower gateway-retry layer's sleeps (no separate patch needed).
    with patch("nexus.retry.time.sleep") as mock_sleep, patch(
        "nexus.retry.random.random", return_value=0.5,
    ):
        client = hvc.HttpVectorClient()
        client.upsert_chunks("code__test", ["id-1"], ["def hello(): pass"], [{"k": "v"}])

    assert handler.call_count == 5
    assert test_brake.trips == 1
    assert test_brake.last_retry_after is None  # no Retry-After header -> escalating default
    # 3 gateway-retry sleeps (2/5/10s, the lower layer exhausting its own
    # budget) then the outer _vector_with_retry's brake-floored sleep (2.0).
    assert mock_sleep.call_args_list == [call(2.0), call(5.0), call(10.0), call(2.0)]


def test_upsert_chunks_500_not_retried_real_server(upsert_server, monkeypatch) -> None:
    from nexus.db import http_vector_client as hvc

    httpd, handler = upsert_server
    handler.responses = [(500, {}, {"error": "internal"})]
    host, port = httpd.server_address
    monkeypatch.setattr(hvc, "_resolve_endpoint", lambda: (f"http://{host}:{port}", "tok"))

    with patch("nexus.retry.time.sleep") as mock_sleep, pytest.raises(VectorServiceError):
        client = hvc.HttpVectorClient()
        client.upsert_chunks("code__test", ["id-1"], ["def hello(): pass"], [{"k": "v"}])

    assert handler.call_count == 1  # no retry
    mock_sleep.assert_not_called()


class _SyncFakeClock:
    """A shared virtual clock for the concurrency test below: ``sleep()``
    BLOCKS the calling thread until ``advance()`` (driven by the test) has
    moved ``now`` far enough — never self-advances — so two REAL threads
    genuinely share one timeline instead of each independently
    fast-forwarding it. No real ``time.sleep`` is ever invoked; blocking
    is via ``threading.Condition``, woken by ``advance()``. (Mirrors
    ``tests/test_rate_brake.py``'s identical helper — kept local rather
    than imported, since it is test-only infrastructure, not part of
    ``nexus.rate_brake``'s production surface.)"""

    def __init__(self) -> None:
        self._now = 0.0
        self._waiting = 0
        self._cond = threading.Condition()

    def time(self) -> float:
        with self._cond:
            return self._now

    def sleep(self, seconds: float) -> None:
        with self._cond:
            target = self._now + seconds
            self._waiting += 1
            self._cond.notify_all()
            self._cond.wait_for(lambda: self._now >= target)
            self._waiting -= 1

    def advance(self, seconds: float) -> None:
        with self._cond:
            self._now += seconds
            self._cond.notify_all()

    def wait_for_waiters(self, count: int, timeout: float = 5.0) -> None:
        with self._cond:
            ok = self._cond.wait_for(lambda: self._waiting >= count, timeout=timeout)
        assert ok, f"expected {count} waiters, only {self._waiting} arrived within {timeout}s"


def test_upsert_chunks_concurrent_second_call_waits_for_shared_pause(
    upsert_server, monkeypatch,
) -> None:
    """Two concurrent ``upsert_chunks`` calls sharing one brake: caller A
    hits a 429 and trips the shared pause; caller B — whose own request
    never fails — still BLOCKS in ``brake.wait()`` on the same shared
    deadline before its request ever reaches the server (S-item 3: proves
    the pause is genuinely shared across concurrent callers, not per-call
    state)."""
    from nexus.db import http_vector_client as hvc

    httpd, handler = upsert_server
    handler.responses = [
        (429, {"Retry-After": "2"}, {"error": "rate limited"}),
        (200, {}, None),
        (200, {}, None),
    ]
    host, port = httpd.server_address
    monkeypatch.setattr(hvc, "_resolve_endpoint", lambda: (f"http://{host}:{port}", "tok"))

    fc = _SyncFakeClock()
    test_brake = RateLimitBrake(clock=fc.time, sleep=fc.sleep, jitter=lambda: 0.0)
    monkeypatch.setattr(retry_mod, "get_brake", lambda: test_brake)
    # The wrapper's OWN per-attempt retry-sleep (distinct from
    # brake.wait()) must route through the SAME synchronizing clock, or
    # caller A's post-trip sleep would use real time.sleep and desync from
    # caller B's brake.wait().
    monkeypatch.setattr(retry_mod.time, "sleep", fc.sleep)
    monkeypatch.setattr(retry_mod.random, "random", lambda: 0.5)

    results: dict[str, str] = {}

    def call_a() -> None:
        client = hvc.HttpVectorClient()
        client.upsert_chunks("code__test", ["a"], ["doc a"], [{}])
        results["a"] = "done"

    def call_b() -> None:
        client = hvc.HttpVectorClient()
        client.upsert_chunks("code__test", ["b"], ["doc b"], [{}])
        results["b"] = "done"

    thread_a = threading.Thread(target=call_a)
    thread_a.start()
    fc.wait_for_waiters(1)  # A is blocked in its post-trip retry-sleep

    thread_b = threading.Thread(target=call_b)
    thread_b.start()
    fc.wait_for_waiters(2)  # B is blocked in brake.wait() too

    # Proof of the shared block: B's request has not reached the server at
    # all yet — only A's single (failed) attempt has.
    assert handler.call_count == 1

    fc.advance(2.0)  # releases both A's retry-sleep and B's brake.wait()

    thread_a.join(timeout=5.0)
    thread_b.join(timeout=5.0)
    assert not thread_a.is_alive()
    assert not thread_b.is_alive()
    assert results == {"a": "done", "b": "done"}
    assert handler.call_count == 3
    assert test_brake.trips == 1
    assert test_brake.last_retry_after == 2.0
