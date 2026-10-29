# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-jypmj.5: ``HttpTelemetryStore`` and the ``out_of_step_cost_usd`` capability.

A fake engine of the two generations the client meets:

* **new** (the engine carrying telemetry-017-1): ``/version`` advertises ``nx_answer_out_of_step_cost_supported``
  and the column is stored.
* **old**: no flag, and the handler reads named fields only, so an unknown ``out_of_step_cost_usd`` key is dropped
  with HTTP 200 (a 200 is no evidence of persistence).

The client sends the field only to an engine that advertises it, only when the run has one (a run that made
no planner or extraction call has none), on both the ``/record`` and the ``/complete`` route, and logs the loss once it knows
the value will not be stored. The real engine is exercised by the Java ``TelemetryHandlerOutOfStepCostTest``.
"""
from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

import pytest
import structlog.testing

import nexus.db.t2.http_telemetry_store as store_module
from nexus.db.t2.http_telemetry_store import DEFAULT_TENANT, HttpTelemetryStore
from tests.db._fake_t2_server import FakeT2HandlerBase, fake_http_server

TOKEN = "fake-out-of-step-cost-token"
STAMP = "2026-10-28T14:00:00.123456+00:00"


class _Engine:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.posts: list[tuple[str, dict[str, Any]]] = []
        self.version: dict[str, Any] = {}

    def new(self) -> _Engine:
        self.version = {"nx_answer_steps_supported": True, "nx_answer_run_complete_supported": True,
                        "nx_answer_out_of_step_cost_supported": True}
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
def _reset_engine(monkeypatch):
    ENGINE.reset()
    monkeypatch.setattr(store_module, "_OUT_OF_STEP_UNSUPPORTED_LOGGED", False)  # the process-wide latch, fresh per test
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
        assert client._supports_nx_answer_out_of_step_cost() is True

    def test_false_on_an_engine_without_the_flag(self, client):
        ENGINE.old()
        assert client._supports_nx_answer_out_of_step_cost() is False

    def test_false_on_an_explicit_false_and_on_a_non_boolean(self, client):
        ENGINE.version = {"nx_answer_out_of_step_cost_supported": False}
        assert client._supports_nx_answer_out_of_step_cost() is False
        client._version_capabilities_cache = None
        ENGINE.version = {"nx_answer_out_of_step_cost_supported": "true"}
        assert client._supports_nx_answer_out_of_step_cost() is False

    def test_false_and_no_raise_when_the_engine_is_unreachable(self):
        store = HttpTelemetryStore(base_url="http://127.0.0.1:1", tenant=DEFAULT_TENANT, _token=TOKEN)
        try:
            assert store._supports_nx_answer_out_of_step_cost() is False
        finally:
            store.close()


class TestNewEngine:
    def test_record_sends_the_out_of_step_cost(self, client):
        ENGINE.new()
        _record(client, out_of_step_cost_usd=0.004)
        assert _sent("/record")["out_of_step_cost_usd"] == 0.004

    def test_complete_sends_the_out_of_step_cost(self, client):
        ENGINE.new()
        _complete(client, out_of_step_cost_usd=0.004)
        assert _sent("/complete")["out_of_step_cost_usd"] == 0.004

    def test_a_measured_free_out_of_step_share_is_sent_as_zero(self, client):
        ENGINE.new()
        _record(client, out_of_step_cost_usd=0.0)
        assert _sent("/record")["out_of_step_cost_usd"] == 0.0

    def test_a_run_without_an_out_of_step_call_sends_no_key_at_all(self, client):
        ENGINE.new()
        _record(client)
        _complete(client, out_of_step_cost_usd=None)
        assert all("out_of_step_cost_usd" not in b for _p, b in ENGINE.posts), "unknown is absent, never 0.0"


class TestOldEngine:
    def test_record_and_complete_send_no_out_of_step_cost_and_the_run_is_still_written(self, client):
        ENGINE.old()
        with structlog.testing.capture_logs() as logs:
            _record(client, out_of_step_cost_usd=0.004)
            _complete(client, out_of_step_cost_usd=0.004)
        assert len(ENGINE.posts) == 2, "the run rows are written either way"
        assert all("out_of_step_cost_usd" not in b for _p, b in ENGINE.posts)
        assert [e["event"] for e in logs].count("nx_answer_out_of_step_cost_unsupported_by_engine") == 1, (
            "latched: the warning names a standing condition of the engine, not an event of the run"
        )
        with structlog.testing.capture_logs() as later:
            _record(client, out_of_step_cost_usd=0.004)
        assert not [e for e in later if e["event"] == "nx_answer_out_of_step_cost_unsupported_by_engine"]

    def test_the_latch_is_process_wide_because_a_store_is_built_fresh_per_call(self, fake_url):
        """``t2_ctx`` hands every ``nx_answer`` call a new ``HttpTelemetryStore``: a per-instance latch would log
        on every run."""
        ENGINE.old()
        with structlog.testing.capture_logs() as logs:
            for _ in range(3):
                fresh = HttpTelemetryStore(base_url=fake_url, tenant=DEFAULT_TENANT, _token=TOKEN)
                try:
                    _record(fresh, out_of_step_cost_usd=0.004)
                finally:
                    fresh.close()
        assert [e["event"] for e in logs].count("nx_answer_out_of_step_cost_unsupported_by_engine") == 1

    def test_a_run_with_no_out_of_step_cost_logs_nothing_on_an_old_engine(self, client):
        ENGINE.old()
        with structlog.testing.capture_logs() as logs:
            _record(client)
        assert not [e for e in logs if e["event"] == "nx_answer_out_of_step_cost_unsupported_by_engine"]
