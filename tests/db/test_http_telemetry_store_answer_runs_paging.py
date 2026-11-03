# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-1ss4n: ``HttpTelemetryStore`` pages ``nx_answer_runs`` by an ``id`` keyset.

The engine clamps a ``/nx_answer_runs/query`` page to 300 rows and, before this bead, had no way to
reach past the newest 300. The new bound is ``before_id=N`` (rows with ``id < N``, highest id first),
advertised on ``GET /version`` as ``nx_answer_runs_before_id_supported``.

The fake engine below models both generations the client meets:

* **new**: honours ``before_id`` (ordering by id, as the real engine does under the bound), clamps to
  300, and returns the whole-since-set aggregates on every page.
* **old**: no flag, and ``before_id`` is an unknown query parameter it ignores with HTTP 200, so it
  returns the newest page again. That is the failure the client must refuse to walk into.

The real engine is exercised by the Java tests (``TelemetryHandlerNxAnswerRunsLimitClampTest``,
``TelemetryRepositoryTest``) and by ``tests/db/test_eho3u_nx_answer_runs_read.py``-style integration.
"""
from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

import pytest

from nexus.db.limits import MAX_QUERY_RESULTS
from nexus.db.t2.http_telemetry_store import (
    DEFAULT_TENANT,
    AnswerRunsPagingUnsupportedError,
    HttpTelemetryStore,
)
from tests.db._fake_t2_server import FakeT2HandlerBase, fake_http_server

TOKEN = "fake-answer-runs-paging-token"
ENGINE_CLAMP = 300
INT64_MAX = 2**63 - 1


class _Engine:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.queries: list[dict[str, str]] = []
        self.rows: list[dict[str, Any]] = []
        self.version: dict[str, Any] = {}
        self.honours_before_id = False

    def new(self, n_rows: int) -> _Engine:
        self.version = {"nx_answer_runs_before_id_supported": True}
        self.honours_before_id = True
        self.rows = [{"id": i, "question": f"q{i}", "created_at": "2026-08-01T00:00:00Z"} for i in range(1, n_rows + 1)]
        return self

    def old(self, n_rows: int) -> _Engine:
        self.version = {"nx_answer_steps_supported": True}
        self.honours_before_id = False
        self.rows = [{"id": i, "question": f"q{i}", "created_at": "2026-08-01T00:00:00Z"} for i in range(1, n_rows + 1)]
        return self


ENGINE = _Engine()


class _Handler(FakeT2HandlerBase):
    TOKEN = TOKEN

    def do_GET(self):  # noqa: N802
        path = urlparse(self.path).path
        if path == "/version":
            self._send(200, dict(ENGINE.version))
            return
        if not self._check_auth():
            return
        if path == "/v1/telemetry/nx_answer_runs/query":
            qs = self._qs()
            ENGINE.queries.append(qs)
            rows = sorted(ENGINE.rows, key=lambda r: r.get("id", 0), reverse=True)
            total = len(rows)
            if ENGINE.honours_before_id and qs.get("before_id"):
                rows = [r for r in rows if r.get("id", 0) < int(qs["before_id"])]
            limit = min(int(qs.get("limit", "20")), ENGINE_CLAMP)
            self._send(200, {"rows": rows[:limit], "total": total, "hit_count": 1, "fallback_count": total - 1})
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


class TestCapabilityProbe:
    def test_true_only_for_a_json_true(self, client):
        ENGINE.new(1)
        assert client._supports_nx_answer_runs_before_id() is True

    def test_absent_flag_is_unsupported(self, client):
        ENGINE.old(1)
        assert client._supports_nx_answer_runs_before_id() is False

    @pytest.mark.parametrize("value", ["true", 1, "yes", None, False])
    def test_anything_but_true_is_unsupported(self, client, value):
        ENGINE.new(1)
        ENGINE.version = {"nx_answer_runs_before_id_supported": value}
        assert client._supports_nx_answer_runs_before_id() is False


class TestBeforeIdOnASinglePage:
    def test_sent_on_the_wire_and_bounds_the_page(self, client):
        ENGINE.new(10)
        out = client.query_nx_answer_runs(limit=3, before_id=6)
        assert ENGINE.queries[-1]["before_id"] == "6"
        assert [r["id"] for r in out["rows"]] == [5, 4, 3]

    def test_not_sent_when_not_asked_for(self, client):
        ENGINE.new(3)
        client.query_nx_answer_runs(limit=2)
        assert "before_id" not in ENGINE.queries[-1]

    def test_old_engine_refused_loudly_and_nothing_queried(self, client):
        # An old engine would ignore before_id and return the newest rows again (HTTP 200).
        ENGINE.old(10)
        with pytest.raises(AnswerRunsPagingUnsupportedError, match="nx_answer_runs_before_id_supported"):
            client.query_nx_answer_runs(limit=3, before_id=6)
        assert ENGINE.queries == [], "refusal must come before any query is sent"

    @pytest.mark.parametrize("bad", [0, -5, True, "7", 2.5])
    def test_a_bound_that_is_not_a_positive_int_is_a_value_error(self, client, bad):
        ENGINE.new(3)
        with pytest.raises(ValueError, match="before_id"):
            client.query_nx_answer_runs(before_id=bad)
        assert ENGINE.queries == []

    def test_limit_above_the_paging_ceiling_is_a_value_error(self, client):
        ENGINE.new(3)
        with pytest.raises(ValueError, match=str(MAX_QUERY_RESULTS)):
            client.query_nx_answer_runs(limit=MAX_QUERY_RESULTS + 1, before_id=10)


class TestQueryAll:
    def test_pages_past_the_clamp_to_the_start_of_the_window(self, client):
        ENGINE.new(614)  # the bead's own figure: 614 runs, only the newest 300 readable
        out = client.query_nx_answer_runs_all()
        ids = [r["id"] for r in out["rows"]]
        assert ids == list(range(614, 0, -1)), "every row once, newest id first, to the oldest"
        # 300 + 300 + 14, then the empty page that proves the end.
        assert len(ENGINE.queries) == 4
        assert all(int(q["limit"]) <= MAX_QUERY_RESULTS for q in ENGINE.queries)
        assert ENGINE.queries[0]["before_id"] == str(INT64_MAX)
        assert [q["before_id"] for q in ENGINE.queries[1:]] == ["315", "15", "1"]

    def test_aggregates_are_the_whole_since_set_not_the_window(self, client):
        ENGINE.new(614)
        out = client.query_nx_answer_runs_all()
        assert out["total"] == 614
        assert out["hit_count"] == 1 and out["fallback_count"] == 613

    def test_an_empty_set_returns_no_rows_after_one_query(self, client):
        ENGINE.new(0)
        out = client.query_nx_answer_runs_all()
        assert out["rows"] == [] and out["total"] == 0
        assert len(ENGINE.queries) == 1

    def test_exactly_one_clamp_of_rows_takes_a_second_query_to_prove_the_end(self, client):
        ENGINE.new(300)
        out = client.query_nx_answer_runs_all()
        assert len(out["rows"]) == 300
        assert len(ENGINE.queries) == 2

    def test_forwards_since_and_include_steps(self, client):
        ENGINE.new(2)
        client.query_nx_answer_runs_all(since="2026-08-01T00:00:00Z", include_steps=True)
        assert ENGINE.queries[0]["since"] == "2026-08-01T00:00:00Z"
        assert ENGINE.queries[0]["include_steps"] == "true"

    def test_old_engine_refused_loudly_instead_of_looping_on_the_newest_page(self, client):
        ENGINE.old(614)
        with pytest.raises(AnswerRunsPagingUnsupportedError):
            client.query_nx_answer_runs_all()
        assert ENGINE.queries == []

    def test_an_engine_that_advertises_but_ignores_the_bound_cannot_loop_forever(self, client):
        # Defence in depth: the flag says yes, the engine returns the newest page again.
        ENGINE.new(614)
        ENGINE.honours_before_id = False
        with pytest.raises(AnswerRunsPagingUnsupportedError, match="did not advance"):
            client.query_nx_answer_runs_all()
        assert len(ENGINE.queries) == 2, "stops on the first page that fails to move the cursor"

    def test_a_row_without_an_id_is_an_error_not_a_silent_stop(self, client):
        ENGINE.new(3)
        ENGINE.rows[1].pop("id")
        with pytest.raises(AnswerRunsPagingUnsupportedError, match="id"):
            client.query_nx_answer_runs_all()
