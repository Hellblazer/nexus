# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-g6ic6: a capped census is ONE attempt.

``subspace_stats(..., single_shot=True)`` (and ``aspects_census`` with it) sends the request
once. The mixin's default retries a ``ReadTimeout`` once and backs off a gateway 503, so a
census capped at 10 s cost about 20 s plus backoff per ``nx doctor``; and an engine
``CensusTimeout`` 503 (its own statement timeout fired, the engine is up) is not a gateway
fault a retry cures. The attempts are counted at an ``httpx.MockTransport``.

The default call keeps its retries (the positive control: the same stub shows two attempts
without the flag, so a count of one is the flag's doing, not the stub's).
"""
from __future__ import annotations

import httpx
import pytest

from nexus.db import gateway_hold
from nexus.db.t2 import _refreshable_client as rc
from nexus.db.t2.http_aspect_queue import aspects_census
from nexus.db.t2.http_tuple_store import CensusTimeoutError, HttpTupleStore
from tests._module_seam import module_time

_CENSUS_BODY = {
    "subspace": "aspects", "template": "aspects", "total": 3, "available": 3,
    "claimed": 0, "lapsed": 0, "dead": 0,
}


class _Transport:
    """Counts requests; each is answered by *respond* (a status/body or an exception to raise)."""

    def __init__(self, respond) -> None:
        self.respond = respond
        self.requests = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests += 1
        return self.respond(request, self.requests)


def _store(transport: _Transport) -> HttpTupleStore:
    store = HttpTupleStore(base_url="http://mock.test", _token="test-token")
    store._client = httpx.Client(transport=httpx.MockTransport(transport))
    return store


def _read_timeout(request: httpx.Request, n: int) -> httpx.Response:
    raise httpx.ReadTimeout("slow census", request=request)


def _census_timeout_503(request: httpx.Request, n: int) -> httpx.Response:
    return httpx.Response(503, json={"error": "CensusTimeout", "detail": "statement timeout"})


@pytest.fixture(autouse=True)
def _no_sleep_no_shared_hold(monkeypatch: pytest.MonkeyPatch, tmp_path) -> list[float]:
    """The gateway backoff sleeps, and a 503 writes a hold file every client on the box reads."""
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    sleeps: list[float] = []
    module_time(monkeypatch, rc).sleep = lambda s: sleeps.append(s)
    # A pinned store cannot re-resolve (it raises); the retry under test is the resend, not the re-resolve.
    monkeypatch.setattr(HttpTupleStore, "_invalidate_and_reresolve", lambda self: None)
    return sleeps


class TestASingleShotCensusIsNotRetried:
    def test_a_read_timeout_is_one_attempt(self) -> None:
        t = _Transport(_read_timeout)
        with pytest.raises(httpx.ReadTimeout):
            _store(t).subspace_stats("aspects", timeout=10.0, single_shot=True)
        assert t.requests == 1

    def test_the_default_call_retries_a_read_timeout_once_the_control(self) -> None:
        t = _Transport(_read_timeout)
        with pytest.raises(httpx.ReadTimeout):
            _store(t).subspace_stats("aspects", timeout=10.0)
        assert t.requests == 2, "the stub does retry: one attempt above is the flag's doing"

    def test_a_census_timeout_503_is_one_attempt_with_no_backoff(self, _no_sleep_no_shared_hold) -> None:
        t = _Transport(_census_timeout_503)
        with pytest.raises(CensusTimeoutError):
            _store(t).subspace_stats("aspects", timeout=10.0, single_shot=True)
        assert t.requests == 1
        assert _no_sleep_no_shared_hold == [], "no backoff sleep"

    def test_the_default_call_backs_off_a_census_timeout_503_the_control(self, _no_sleep_no_shared_hold) -> None:
        t = _Transport(_census_timeout_503)
        with pytest.raises(CensusTimeoutError):
            _store(t).subspace_stats("aspects", timeout=10.0)
        assert t.requests > 1 and _no_sleep_no_shared_hold, "the stub does back off: one attempt above is the flag's doing"

    def test_the_default_call_writes_the_shared_gateway_hold_on_a_census_timeout_503_the_control(self, tmp_path) -> None:
        """The positive control for the no-hold test below: in THIS fixture the default path does write the file."""
        with pytest.raises(CensusTimeoutError):
            _store(_Transport(_census_timeout_503)).subspace_stats("aspects", timeout=10.0)
        assert (tmp_path / gateway_hold.HOLD_FILENAME).exists(), "the stub does write the hold: its absence below is the flag's doing"

    def test_a_census_timeout_503_does_not_write_the_shared_gateway_hold(self, tmp_path) -> None:
        """The hold file makes EVERY client on the box wait; a slow census is not an overloaded edge."""
        with pytest.raises(CensusTimeoutError):
            _store(_Transport(_census_timeout_503)).subspace_stats("aspects", timeout=10.0, single_shot=True)
        assert not (tmp_path / gateway_hold.HOLD_FILENAME).exists()

    def test_an_answer_comes_back_unchanged(self) -> None:
        t = _Transport(lambda request, n: httpx.Response(200, json=_CENSUS_BODY))
        census = _store(t).subspace_stats("aspects", timeout=10.0, single_shot=True)
        assert census.total == 3 and t.requests == 1


class TestAspectsCensusPassesTheFlag:
    def test_aspects_census_single_shot_is_one_attempt(self) -> None:
        t = _Transport(_read_timeout)
        with pytest.raises(httpx.ReadTimeout):
            aspects_census(_store(t), timeout=10.0, single_shot=True)
        assert t.requests == 1

    def test_aspects_census_without_the_flag_is_unchanged(self) -> None:
        t = _Transport(_read_timeout)
        with pytest.raises(httpx.ReadTimeout):
            aspects_census(_store(t), timeout=10.0)
        assert t.requests == 2


