# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-jypmj.5: ``HttpTelemetryStore`` and the ``planner_cost_usd`` capability.

A fake engine of the two generations the client meets:

* **new** (the engine carrying telemetry-017-1): ``/version`` advertises ``nx_answer_planner_cost_supported``
  and the column is stored.
* **old**: no flag, and the handler reads named fields only, so an unknown ``planner_cost_usd`` key is dropped
  with HTTP 200 (a 200 is no evidence of persistence).

The client sends the field only to an engine that advertises it, only when the run has one (a plan-match hit
has no planner dispatch), on both the ``/record`` and the ``/complete`` route, and logs the loss once it knows
the value will not be stored. The real engine is exercised by the Java ``TelemetryHandlerPlannerCostTest``.
"""
from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

import pytest
import structlog.testing

from nexus.db.t2.http_telemetry_store import DEFAULT_TENANT, HttpTelemetryStore
from tests.db._fake_t2_server import FakeT2HandlerBase, fake_http_server

TOKEN = "fake-planner-cost-token"
STAMP = "2026-10-28T14:00:00.123456+00:00"


class _Engine:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.posts: list[tuple[str, dict[str, Any]]] = []
        self.version: dict[str, Any] = {}

    def new(self) -> _Engine:
        self.version = {"nx_answer_steps_supported": True, "nx_answer_run_complete_supported": True,
                        "nx_answer_planner_cost_supported": True}
        return self

    def old(self) -> _Engine:
        self.version = {"nx_answer_steps_supported": True, "nx_answer_run_complete_supported": True}
        return self


ENGINE = _Engine()


class _Handler(FakeT2HandlerBase):
    TOKEN = TOKEN

    def do_GET(self):  # noqa: N802
        if urlparse(self.path).path == "/version":
            self._send(200, dict(ENGINE.version))
            return
        self._send(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        if not self._check_auth():
            return
        path = urlparse(self.path).path
        ENGINE.posts.append((path, self._body()))
        self._send(200, {"ok": True})


@pytest.fixture(scope="module")
def fake_url():
    with fake_http_server(_Handler) as url:
        yield url


@pytest.fixture(autouse=True)
def _reset_engine():
    ENGINE.reset()
    yield
    ENGINE.reset()


@pytest.fixture
def client(fake_url):
    c = HttpTelemetryStore(base_url=fake_url, tenant=DEFAULT_TENANT, _token=TOKEN)
    yield c
    c.close()


def _record(client: HttpTelemetryStore, **over: Any):
    kw: dict[str, Any] = dict(
        question="q", plan_id=0, matched_confidence=None, step_count=2, final_text="the answer",
        cost_usd=0.0125, duration_ms=40, steps=None,
    )
    kw.update(over)
    return client.record_nx_answer_run(**kw)


def _complete(client: HttpTelemetryStore, **over: Any):
    kw: dict[str, Any] = dict(
        question="q", plan_id=0, matched_confidence=None, step_count=2, final_text="the answer",
        cost_usd=0.0125, duration_ms=40, created_at=STAMP, steps=None, success=True,
    )
    kw.update(over)
    return client.record_nx_answer_run_complete(**kw)


def _sent(route: str) -> dict[str, Any]:
    (body,) = [b for p, b in ENGINE.posts if p.endswith(route)]
    return body


class TestCapabilityProbe:
    def test_true_only_when_the_engine_advertises_it(self, client):
        ENGINE.new()
        assert client._supports_nx_answer_planner_cost() is True

    def test_false_on_an_engine_without_the_flag(self, client):
        ENGINE.old()
        assert client._supports_nx_answer_planner_cost() is False

    def test_false_on_an_explicit_false_and_on_a_non_boolean(self, client):
        ENGINE.version = {"nx_answer_planner_cost_supported": False}
        assert client._supports_nx_answer_planner_cost() is False
        client._version_capabilities_cache = None
        ENGINE.version = {"nx_answer_planner_cost_supported": "true"}
        assert client._supports_nx_answer_planner_cost() is False

    def test_false_and_no_raise_when_the_engine_is_unreachable(self):
        store = HttpTelemetryStore(base_url="http://127.0.0.1:1", tenant=DEFAULT_TENANT, _token=TOKEN)
        try:
            assert store._supports_nx_answer_planner_cost() is False
        finally:
            store.close()


class TestNewEngine:
    def test_record_sends_the_planner_cost(self, client):
        ENGINE.new()
        _record(client, planner_cost_usd=0.004)
        assert _sent("/record")["planner_cost_usd"] == 0.004

    def test_complete_sends_the_planner_cost(self, client):
        ENGINE.new()
        _complete(client, planner_cost_usd=0.004)
        assert _sent("/complete")["planner_cost_usd"] == 0.004

    def test_a_measured_free_planner_is_sent_as_zero(self, client):
        ENGINE.new()
        _record(client, planner_cost_usd=0.0)
        assert _sent("/record")["planner_cost_usd"] == 0.0

    def test_a_run_without_a_planner_dispatch_sends_no_key_at_all(self, client):
        ENGINE.new()
        _record(client)
        _complete(client, planner_cost_usd=None)
        assert all("planner_cost_usd" not in b for _p, b in ENGINE.posts), "unknown is absent, never 0.0"


class TestOldEngine:
    def test_record_and_complete_send_no_planner_cost_and_the_run_is_still_written(self, client):
        ENGINE.old()
        with structlog.testing.capture_logs() as logs:
            _record(client, planner_cost_usd=0.004)
            _complete(client, planner_cost_usd=0.004)
        assert len(ENGINE.posts) == 2, "the run rows are written either way"
        assert all("planner_cost_usd" not in b for _p, b in ENGINE.posts)
        assert [e["event"] for e in logs].count("nx_answer_planner_cost_unsupported_by_engine") == 2

    def test_a_run_with_no_planner_cost_logs_nothing_on_an_old_engine(self, client):
        ENGINE.old()
        with structlog.testing.capture_logs() as logs:
            _record(client)
        assert not [e for e in logs if e["event"] == "nx_answer_planner_cost_unsupported_by_engine"]
