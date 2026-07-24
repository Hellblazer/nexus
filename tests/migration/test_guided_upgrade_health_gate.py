# SPDX-License-Identifier: AGPL-3.0-or-later
"""ez5.5 — bounded health-gate for ``nx guided-upgrade``.

After provisioning the engine-service (ez5.6) the command must wait for it to
become ready BEFORE handing off to ``nx migrate-to-service`` (ez5.7) — but the
wait must be BOUNDED: a service that never comes up must fail the gate within a
deadline, never block forever. The ready contract mirrors the doctor probe:
HTTP 200 AND ``body["db"] == "up"`` (the ez5.1 pinned /health shape).
"""

from __future__ import annotations

import pytest

from nexus.upgrade_ladder.provisioning import (
    HealthGateResult,
    wait_for_service_health,
)

SERVICE_URL = "http://127.0.0.1:8099"


class _Resp:
    def __init__(self, status_code: int, body: dict) -> None:
        self.status_code = status_code
        self._body = body

    def json(self) -> dict:
        return self._body


_OK = _Resp(200, {"status": "ok", "db": "up"})
_DOWN = _Resp(503, {"status": "error", "db": "down", "detail": "SELECT 1 failed"})


class _ScriptedGet:
    """A scripted http_get(url, timeout) -> resp, raising injected errors.

    Each script entry is either a `_Resp` or an Exception instance (raised).
    Runs off the end by repeating the LAST entry forever (models a service
    that stays in its terminal state).
    """

    def __init__(self, script: list) -> None:
        self._script = list(script)
        self.calls: list[tuple[str, float]] = []

    def __call__(self, url: str, timeout: float):  # noqa: ANN204
        self.calls.append((url, timeout))
        idx = min(len(self.calls) - 1, len(self._script) - 1)
        item = self._script[idx]
        if isinstance(item, Exception):
            raise item
        return item


class _FakeClock:
    """Monotonic clock whose time only advances when ``sleep`` is called."""

    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, dt: float) -> None:
        self.sleeps.append(dt)
        self.t += dt


class TestWaitForServiceHealth:
    def test_ready_on_first_attempt_no_sleep(self) -> None:
        clk = _FakeClock()
        get = _ScriptedGet([_OK])
        result = wait_for_service_health(
            service_url=SERVICE_URL,
            timeout_s=30.0,
            interval_s=1.0,
            http_get=get,
            sleep=clk.sleep,
            clock=clk.now,
        )
        assert isinstance(result, HealthGateResult)
        assert result.ready is True
        assert result.attempts == 1
        assert clk.sleeps == []  # never slept — ready immediately
        assert get.calls[0][0] == f"{SERVICE_URL}/health"

    def test_ready_after_retries(self) -> None:
        clk = _FakeClock()
        get = _ScriptedGet([_DOWN, _DOWN, _OK])
        result = wait_for_service_health(
            service_url=SERVICE_URL,
            timeout_s=30.0,
            interval_s=1.0,
            http_get=get,
            sleep=clk.sleep,
            clock=clk.now,
        )
        assert result.ready is True
        assert result.attempts == 3
        assert clk.sleeps == [1.0, 1.0]  # slept between the three attempts

    def test_never_ready_is_bounded_not_infinite(self) -> None:
        clk = _FakeClock()
        get = _ScriptedGet([_DOWN])  # stays down forever
        result = wait_for_service_health(
            service_url=SERVICE_URL,
            timeout_s=5.0,
            interval_s=1.0,
            http_get=get,
            sleep=clk.sleep,
            clock=clk.now,
        )
        assert result.ready is False
        # Bounded: total waited never exceeds the deadline.
        assert clk.now() <= 5.0
        # Did retry several times, then stopped.
        assert 1 < result.attempts <= 6
        assert result.last_status == 503
        assert result.last_error is not None and "SELECT 1" in result.last_error

    def test_transport_error_then_ready(self) -> None:
        clk = _FakeClock()
        get = _ScriptedGet([ConnectionError("connection refused"), _OK])
        result = wait_for_service_health(
            service_url=SERVICE_URL,
            timeout_s=30.0,
            interval_s=1.0,
            http_get=get,
            sleep=clk.sleep,
            clock=clk.now,
        )
        assert result.ready is True
        assert result.attempts == 2

    def test_transport_error_until_deadline(self) -> None:
        clk = _FakeClock()
        get = _ScriptedGet([ConnectionError("connection refused")])
        result = wait_for_service_health(
            service_url=SERVICE_URL,
            timeout_s=3.0,
            interval_s=1.0,
            http_get=get,
            sleep=clk.sleep,
            clock=clk.now,
        )
        assert result.ready is False
        assert result.last_status is None  # never got an HTTP response
        assert result.last_error is not None and "refused" in result.last_error
        assert clk.now() <= 3.0

    def test_zero_timeout_still_makes_one_attempt(self) -> None:
        # A deadline of 0 must not skip the probe entirely — exactly one shot.
        clk = _FakeClock()
        get = _ScriptedGet([_DOWN])
        result = wait_for_service_health(
            service_url=SERVICE_URL,
            timeout_s=0.0,
            interval_s=1.0,
            http_get=get,
            sleep=clk.sleep,
            clock=clk.now,
        )
        assert result.ready is False
        assert result.attempts == 1
        assert clk.sleeps == []  # no sleep — deadline already reached

    def test_200_but_db_not_up_is_not_ready(self) -> None:
        # A 200 with db != up (e.g. mid-startup) must NOT count as ready.
        clk = _FakeClock()
        weird = _Resp(200, {"status": "ok", "db": "starting"})
        get = _ScriptedGet([weird])
        result = wait_for_service_health(
            service_url=SERVICE_URL,
            timeout_s=0.0,
            interval_s=1.0,
            http_get=get,
            sleep=clk.sleep,
            clock=clk.now,
        )
        assert result.ready is False
        assert result.last_status == 200

    def test_negative_timeout_rejected(self) -> None:
        with pytest.raises(ValueError):
            wait_for_service_health(service_url=SERVICE_URL, timeout_s=-1.0)


class TestHealthGateAuthToken:
    """nexus-bwulw / conexus relay [21082] decision (b): the managed edge
    auth-gates /health; the gate's default transport sends the bearer when a
    token is supplied and stays unauthenticated (ez5.1) without one."""

    def _capture_httpx(self, monkeypatch):
        import httpx

        calls: list[dict] = []

        class _Resp:
            status_code = 200

            @staticmethod
            def json() -> dict:
                return {"status": "ok", "db": "up"}

        def _fake_get(url, timeout=None, headers=None, **kw):
            calls.append({"url": url, "headers": headers or {}})
            return _Resp()

        monkeypatch.setattr(httpx, "get", _fake_get)
        return calls

    def test_token_sends_bearer_header(self, monkeypatch) -> None:
        calls = self._capture_httpx(monkeypatch)
        result = wait_for_service_health(
            service_url=SERVICE_URL, timeout_s=5.0, token="sekrit",
        )
        assert result.ready is True
        assert calls[0]["headers"] == {"Authorization": "Bearer sekrit"}

    def test_no_token_stays_unauthenticated(self, monkeypatch) -> None:
        calls = self._capture_httpx(monkeypatch)
        result = wait_for_service_health(service_url=SERVICE_URL, timeout_s=5.0)
        assert result.ready is True
        assert calls[0]["headers"] == {}
