# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-232 Phase 3 (nexus-jypmj.3.5): ``HttpTelemetryStore`` and the run-summary capability.

An in-process fake that behaves like the two engine generations the client meets:

* **new** (engine-service 0.1.166+): ``/version`` advertises ``nx_answer_run_summary_supported``; the
  summary is stored on the run row and ``GET /query`` returns it per row (``null`` when none).
* **old** (0.1.165, deployed): no flag, and the handler reads named fields only, so an unknown
  ``run_summary`` key is dropped with HTTP 200 and ``/query`` rows have no ``run_summary`` key at all.

The real engine is exercised in ``tests/db/test_rdr232_run_summary_integration.py``; this file pins what the
client does against each generation without needing a jar: it sends the field only when the flag says the
engine keeps it, never makes a second write, refreshes its cached ``/version`` at a bounded interval, and
finds its own row again by ``(question, created_at)``.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import pytest
import structlog.testing

from nexus.db.t2.http_telemetry_store import DEFAULT_TENANT, HttpTelemetryStore
from tests.db._fake_t2_server import FakeT2HandlerBase, fake_http_server

TOKEN = "fake-run-summary-token"
STAMP = "2026-10-27T14:00:00.123456+00:00"
SUMMARY = {"version": 1, "kind": "nx_answer", "outcome": "success", "coverage_partial": 1, "run": {"cost_usd": 0.0314}}


class _Engine:
    """Mutable engine state the fake handler reads; reset per test."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.posts: list[tuple[str, dict[str, Any]]] = []
        self.queries: list[dict[str, str]] = []
        self.rows: list[dict[str, Any]] = []
        self.version: dict[str, Any] = {}
        self.stores_summary = False
        self.version_hits = 0

    def new(self) -> _Engine:
        self.version = {"nx_answer_steps_supported": True, "nx_answer_run_complete_supported": True,
                        "nx_answer_run_summary_supported": True}
        self.stores_summary = True
        return self

    def old(self) -> _Engine:
        self.version = {"nx_answer_steps_supported": True, "nx_answer_run_complete_supported": True}
        self.stores_summary = False
        return self


ENGINE = _Engine()


def _utc_z(iso: str) -> str:
    """A ``since`` filter in the engine's input form: UTC with a trailing Z."""
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(timezone.utc)
    return dt.isoformat().replace("+00:00", "Z")


def _utc_second(iso: str) -> str:
    """The engine's row timestamp form: UTC, truncated to the second (``utcIso``), trailing Z."""
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(timezone.utc).replace(microsecond=0)
    return dt.isoformat().replace("+00:00", "Z")


class _Handler(FakeT2HandlerBase):
    TOKEN = TOKEN

    def do_GET(self):  # noqa: N802
        path = urlparse(self.path).path
        if path == "/version":
            ENGINE.version_hits += 1
            self._send(200, dict(ENGINE.version))
            return
        if not self._check_auth():
            return
        if path == "/v1/telemetry/nx_answer_runs/query":
            qs = self._qs()
            ENGINE.queries.append(qs)
            rows = list(ENGINE.rows)
            if qs.get("since"):
                # The engine filters on the stored instant (microseconds) and reports it to the second.
                rows = [r for r in rows if r["_exact"] >= _utc_z(qs["since"])]
            out = [{k: v for k, v in r.items() if k != "_exact"} for r in rows]
            self._send(200, {"rows": out[: int(qs.get("limit", "20"))], "total": len(rows)})
            return
        self._send(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        if not self._check_auth():
            return
        path = urlparse(self.path).path
        body = self._body()
        ENGINE.posts.append((path, body))
        if path in ("/v1/telemetry/nx_answer_runs/record", "/v1/telemetry/nx_answer_runs/complete"):
            exact = _utc_z(body["created_at"]) if body.get("created_at") else "2026-10-27T14:00:01Z"
            row = {
                "question": body["question"], "final_text": body.get("final_text", ""),
                "duration_ms": body.get("duration_ms"), "step_count": body.get("step_count"),
                "created_at": _utc_second(exact), "_exact": exact,
            }
            if ENGINE.stores_summary:
                # jsonb re-orders keys and the engine re-serialises; model both.
                row["run_summary"] = json.loads(json.dumps(body.get("run_summary"), sort_keys=True)) if body.get("run_summary") is not None else None
            ENGINE.rows.insert(0, row)
            self._send(200, {"ok": True})
            return
        if path == "/v1/telemetry/import":
            self._send(200, {"ok": True})
            return
        if path == "/v1/telemetry/import_batch":
            self._send(200, {"imported": len(body.get("rows", []))})
            return
        self._send(404, {"error": "not found"})


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


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _record(client: HttpTelemetryStore, **over: Any):
    kw: dict[str, Any] = dict(
        question="q", plan_id=3, matched_confidence=0.7, step_count=2, final_text="the answer",
        cost_usd=0.01, duration_ms=40, steps=None,
    )
    kw.update(over)
    return client.record_nx_answer_run(**kw)


def _complete(client: HttpTelemetryStore, **over: Any):
    kw: dict[str, Any] = dict(
        question="q", plan_id=3, matched_confidence=0.7, step_count=2, final_text="the answer",
        cost_usd=0.01, duration_ms=40, created_at=STAMP, steps=None, success=True,
    )
    kw.update(over)
    return client.record_nx_answer_run_complete(**kw)


class TestCapabilityProbe:
    def test_true_only_when_the_engine_advertises_it(self, client):
        ENGINE.new()
        assert client._supports_nx_answer_run_summary() is True

    def test_false_on_an_engine_without_the_flag(self, client):
        ENGINE.old()
        assert client._supports_nx_answer_run_summary() is False

    def test_false_on_an_explicit_false_and_on_a_non_boolean(self, client):
        ENGINE.version = {"nx_answer_run_summary_supported": False}
        assert client._supports_nx_answer_run_summary() is False
        client._version_capabilities_cache = None
        ENGINE.version = {"nx_answer_run_summary_supported": "true"}
        assert client._supports_nx_answer_run_summary() is False

    def test_false_and_no_raise_when_the_engine_is_unreachable(self):
        store = HttpTelemetryStore(base_url="http://127.0.0.1:1", tenant=DEFAULT_TENANT, _token=TOKEN)
        try:
            assert store._supports_nx_answer_run_summary() is False
        finally:
            store.close()

    def test_it_is_the_same_cached_body_as_the_other_flags(self, client):
        ENGINE.new()
        assert client._supports_nx_answer_steps() is True
        assert client._supports_nx_answer_run_summary() is True
        assert ENGINE.version_hits == 1


class TestCapabilityRefresh:
    """Step 3: refreshed at a bounded interval, and on demand when a readback disagrees."""

    def test_the_cache_holds_inside_the_interval_and_refreshes_after_it(self, client):
        clock = _Clock()
        client._capability_clock = clock
        ENGINE.new()
        assert client._supports_nx_answer_run_summary() is True
        ENGINE.old()  # the engine is rolled back under the running client
        clock.now += client._CAPABILITY_TTL_S - 1
        assert client._supports_nx_answer_run_summary() is True, "inside the interval the cache stands"
        assert ENGINE.version_hits == 1
        clock.now += 2
        assert client._supports_nx_answer_run_summary() is False, "past the interval the flag is re-read"
        assert ENGINE.version_hits == 2

    def test_the_interval_is_bounded(self, client):
        assert 0 < client._CAPABILITY_TTL_S <= 600

    def test_a_failed_probe_is_retried_after_the_interval_not_forever(self, client):
        clock = _Clock()
        client._capability_clock = clock
        ENGINE.version = {}
        assert client._supports_nx_answer_run_summary() is False
        ENGINE.new()
        assert client._supports_nx_answer_run_summary() is False, "the negative result is cached inside the interval"
        clock.now += client._CAPABILITY_TTL_S + 1
        assert client._supports_nx_answer_run_summary() is True

    def test_refresh_on_demand_reports_whether_the_body_changed(self, client):
        ENGINE.new()
        assert client._supports_nx_answer_run_summary() is True
        assert client._refresh_version_capabilities() is False, "same body, nothing changed"
        ENGINE.old()
        with structlog.testing.capture_logs() as logs:
            assert client._refresh_version_capabilities() is True
        assert client._supports_nx_answer_run_summary() is False
        assert any(e["event"] == "nx_answer_version_capabilities_changed" for e in logs)

    def test_refresh_never_raises(self):
        store = HttpTelemetryStore(base_url="http://127.0.0.1:1", tenant=DEFAULT_TENANT, _token=TOKEN)
        try:
            assert store._refresh_version_capabilities() is False
        finally:
            store.close()

    def test_a_cache_set_by_hand_is_not_aged_out(self, client):
        # Existing tests (and the 404 downgrade) set the cache dict directly; no timestamp means no expiry.
        client._version_capabilities_cache = {"nx_answer_run_summary_supported": True}
        client._capability_clock = lambda: 10**9
        assert client._supports_nx_answer_run_summary() is True
        assert ENGINE.version_hits == 0


class TestRecordWire:
    """Scenario 2 and 3, store half: the old engine gets today's record, byte for byte, and one write."""

    def test_new_engine_record_carries_the_summary_and_the_stamp(self, client):
        ENGINE.new()
        assert _record(client, run_summary=SUMMARY, created_at=STAMP) is None
        [(path, body)] = ENGINE.posts
        assert path == "/v1/telemetry/nx_answer_runs/record"
        assert body["run_summary"] == SUMMARY and body["created_at"] == STAMP

    def test_old_engine_record_is_today_s_record_with_one_write(self, client):
        ENGINE.old()
        with structlog.testing.capture_logs() as logs:
            _record(client, run_summary=SUMMARY, created_at=STAMP)
        [(path, body)] = ENGINE.posts
        assert path == "/v1/telemetry/nx_answer_runs/record"
        assert "run_summary" not in body and "created_at" not in body
        assert set(body) == {"question", "plan_id", "matched_confidence", "step_count", "final_text", "cost_usd", "duration_ms"}
        assert any(e["event"] == "nx_answer_run_summary_unsupported_by_engine" for e in logs)

    def test_no_summary_asked_means_no_summary_key_and_no_probe(self, client):
        ENGINE.new()
        _record(client)
        [(_, body)] = ENGINE.posts
        assert "run_summary" not in body and "created_at" not in body
        assert ENGINE.version_hits == 0

    def test_new_engine_complete_carries_the_summary(self, client):
        ENGINE.new()
        assert _complete(client, run_summary=SUMMARY) is None
        [(path, body)] = ENGINE.posts
        assert path == "/v1/telemetry/nx_answer_runs/complete"
        assert body["run_summary"] == SUMMARY and body["created_at"] == STAMP

    def test_old_engine_complete_omits_the_summary_and_keeps_the_record(self, client):
        ENGINE.old()
        _complete(client, run_summary=SUMMARY)
        [(_, body)] = ENGINE.posts
        assert "run_summary" not in body
        assert body["created_at"] == STAMP and body["outcome"] == "success"
        assert len(ENGINE.posts) == 1, "never a second write"

    def test_a_summary_over_the_engine_cap_is_not_sent(self, client):
        # Over the cap the engine answers 400 and the whole run record is lost; the store holds the line.
        ENGINE.new()
        big = {"version": 1, "junk": "x" * 70_000}
        with structlog.testing.capture_logs() as logs:
            _record(client, run_summary=big, created_at=STAMP)
        [(_, body)] = ENGINE.posts
        assert "run_summary" not in body
        assert any(e["event"] == "nx_answer_run_summary_oversize" for e in logs)


class TestReadback:
    def test_finds_its_row_by_question_and_second_across_timestamp_forms(self, client):
        ENGINE.new()
        _record(client, question="other", run_summary={"version": 1, "x": 1}, created_at="2026-10-27T14:00:00.5+00:00")
        _record(client, run_summary=SUMMARY, created_at=STAMP)
        found, stored = client._read_nx_answer_run_summary(question="q", created_at=STAMP)
        assert found is True and stored == [SUMMARY]
        [q] = [q for q in ENGINE.queries]
        assert q["since"].startswith("2026-10-27T14:00:00"), "the window starts at the stamp, not at the epoch"

    def test_the_engine_reports_the_second_so_the_match_is_at_second_resolution(self, client):
        # The real engine's utcIso truncates; a match on the microsecond would never find the row.
        ENGINE.new()
        _record(client, run_summary=SUMMARY, created_at=STAMP)
        assert ENGINE.rows[0]["created_at"] == "2026-10-27T14:00:00Z"
        assert client._read_nx_answer_run_summary(question="q", created_at=STAMP)[0] is True

    def test_another_second_is_another_run(self, client):
        ENGINE.new()
        _record(client, run_summary=SUMMARY, created_at="2026-10-27T14:00:05.123456+00:00")
        found, _ = client._read_nx_answer_run_summary(question="q", created_at=STAMP)
        assert found is False

    def test_same_question_and_second_is_narrowed_by_the_runs_own_text_duration_and_steps(self, client):
        ENGINE.new()
        _record(client, final_text="twin", duration_ms=1, step_count=9, run_summary={"version": 1, "who": "twin"}, created_at=STAMP)
        _record(client, run_summary=SUMMARY, created_at=STAMP)
        both = client._read_nx_answer_run_summary(question="q", created_at=STAMP)
        assert both[0] is True and len(both[1]) == 2
        mine = client._read_nx_answer_run_summary(
            question="q", created_at=STAMP, final_text="the answer", duration_ms=40, step_count=2,
        )
        assert mine == (True, [SUMMARY])

    @pytest.mark.parametrize("dimension,twin", [
        ("final_text", {"final_text": "another answer"}),
        ("duration_ms", {"duration_ms": 41}),
        ("step_count", {"step_count": 3}),
    ])
    def test_each_identity_dimension_alone_separates_a_twin(self, client, dimension, twin):
        # The twin matches the run on question, second and the other two dimensions, so only this
        # dimension's filter can tell it apart.
        ENGINE.new()
        _record(client, run_summary={"version": 1, "who": "twin"}, created_at=STAMP,
                **{"final_text": "the answer", "duration_ms": 40, "step_count": 2, **twin})
        _record(client, run_summary=SUMMARY, created_at=STAMP)
        mine = client._read_nx_answer_run_summary(
            question="q", created_at=STAMP, final_text="the answer", duration_ms=40, step_count=2,
        )
        assert mine == (True, [SUMMARY]), dimension

    def test_old_engine_rows_have_no_summary_key(self, client):
        ENGINE.old()
        _record(client, created_at=STAMP)
        # An old engine would not have kept created_at either; stage the row the way it would read back.
        ENGINE.rows[0]["created_at"] = _utc_second(STAMP)
        ENGINE.rows[0]["_exact"] = _utc_z(STAMP)
        found, stored = client._read_nx_answer_run_summary(question="q", created_at=STAMP)
        assert (found, stored) == (True, [None])

    def test_a_missing_row_is_reported_not_raised(self, client):
        ENGINE.new()
        assert client._read_nx_answer_run_summary(question="nobody", created_at=STAMP) == (False, [])


class TestImport:
    def test_single_import_gates_the_summary(self, client):
        kw = dict(question="q", plan_id=None, matched_confidence=None, step_count=0, final_text="t",
                  cost_usd=None, duration_ms=1, created_at="2026-01-01T00:00:00Z")
        ENGINE.new()
        client.import_nx_answer_run(**kw, run_summary=SUMMARY)
        assert ENGINE.posts[-1][1]["run_summary"] == SUMMARY
        ENGINE.old()
        client._version_capabilities_cache = None
        client.import_nx_answer_run(**kw, run_summary=SUMMARY)
        assert "run_summary" not in ENGINE.posts[-1][1]

    def test_batch_import_strips_the_summary_on_an_old_engine_without_touching_the_callers_rows(self, client):
        rows = [{"question": "q", "created_at": "2026-01-01T00:00:00Z", "run_summary": SUMMARY}]
        ENGINE.old()
        client.import_rows_batch("nx_answer_runs", rows)
        assert "run_summary" not in ENGINE.posts[-1][1]["rows"][0]
        assert rows[0]["run_summary"] == SUMMARY, "the caller's rows are not mutated"
        ENGINE.new()
        client._version_capabilities_cache = None
        client.import_rows_batch("nx_answer_runs", rows)
        assert ENGINE.posts[-1][1]["rows"][0]["run_summary"] == SUMMARY

    def test_batch_import_of_other_tables_is_untouched(self, client):
        ENGINE.old()
        rows = [{"query": "q", "run_summary": "not ours"}]
        client.import_rows_batch("relevance_log", rows)
        assert ENGINE.posts[-1][1]["rows"] == rows
        assert ENGINE.version_hits == 0
