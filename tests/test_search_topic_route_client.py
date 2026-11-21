# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-134 Step 5a-i (nexus-7fku2.1.5): the client half of ``topic_route`` on search-per-collection.

The REAL ``HttpVectorClient`` and ``search_cross_corpus`` run against a stubbed engine (the
``_FakeEngine`` transport of ``test_search_per_collection_route``, extended to advertise
``vector_topic_route_supported`` and to answer a routed request the way the engine's Step 3 tests pin:
``per_collection[].topic_route_status`` per routed collection, ``skipped`` for one the request did not name, no
status on an errored arm). No live engine.
"""
from __future__ import annotations

import inspect
import json
import logging
from typing import Any

import pytest
import structlog

import nexus.db.http_vector_client as hvc
from nexus.db.http_vector_client import TOPIC_ROUTE_CAPABILITY_FLAG, VectorServiceError
from nexus.plans.document_confinement import chash_allow_scope
from nexus.search_engine import search_cross_corpus
from nexus.tables.topic_route_status import STATUSES
from tests.test_search_per_collection_route import (  # type: ignore[import-not-found]
    _BGE,
    _MINI,
    _FakeEngine,
    _cols,
    _fresh_process_state,  # noqa: F401 — autouse fixture of the route tests: opts the route in, resets the memos
    _rows,
)

pytestmark = pytest.mark.usefixtures("named_collection_rows")  # RDR-229 P2.9: prefix-named fixtures get a catalog row

_EVENT = "search_topic_route"
_TOP1 = {"mode": "top1"}


@pytest.fixture(autouse=True)
def _info_events_are_logged():
    """The suite's default level is WARNING, which filters the info-level event before ``capture_logs`` sees it;
    conftest restores the config after every test."""
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.INFO))


class _TopicEngine(_FakeEngine):
    """The route double plus ``/version`` and the Step 3 response shape."""

    def __init__(self, monkeypatch, data, *, capable: bool = True, **kw) -> None:
        super().__init__(monkeypatch, data, **kw)
        self.version: dict = {TOPIC_ROUTE_CAPABILITY_FLAG: True} if capable else {}
        self.version_error: Exception | None = None
        self.version_calls = 0
        #: collection -> topic_route_status the engine answers for a routed collection (default ``routed``)
        self.statuses: dict[str, str] = {}
        #: collection -> extra per_collection keys (topic_route_error, _coverage, _topics)
        self.extras: dict[str, dict] = {}

    def _request(self, method, path, *, tenant, timeout, body):
        if method == "GET" and path == "/version":
            self.version_calls += 1
            if self.version_error is not None:
                raise self.version_error
            return {**super()._request(method, path, tenant=tenant, timeout=timeout, body=body), **self.version}
        return super()._request(method, path, tenant=tenant, timeout=timeout, body=body)

    def _serve_route(self, body):
        envelope = super()._serve_route(body)
        route = body.get("topic_route")
        if route is None:  # an unrouted request, or an engine that predates the field: no status anywhere
            return envelope
        named = route.get("collections")
        for stat in envelope["per_collection"]:
            col = stat["collection"]
            if stat["error"] is not None or stat["error_kind"] is not None:
                continue  # an errored arm carries no status
            stat["topic_route_status"] = (
                self.statuses.get(col, "routed") if named is None or col in named else "skipped"
            )
            stat.update(self.extras.get(col, {}))
        return envelope


def _client_search(engine: _TopicEngine, cols: list[str], **kw):
    return engine.client.search_per_collection(
        "q", cols, per_collection_k=5, limit=10, sensitivity="include", **kw,
    )


def _search(engine: _TopicEngine, cols: list[str], **kw):
    kw.setdefault("threshold_override", float("inf"))
    kw.setdefault("cluster_by", None)
    return search_cross_corpus("q", cols, 5, engine.client, **kw, sensitivity="include")


def _events(run) -> tuple[list[dict], Any]:
    """The ``search_topic_route`` events *run* logged, and what it returned."""
    with structlog.testing.capture_logs() as logs:
        out = run()
    return [e for e in logs if e["event"] == _EVENT], out


def _engine(monkeypatch, cols: list[str], **kw) -> _TopicEngine:
    return _TopicEngine(monkeypatch, {c: _rows(c[:6], 4, 0.2) for c in cols}, **kw)


# ── the capability reader ────────────────────────────────────────────────────


class TestCapabilityReader:
    def test_the_flag_is_spelled_as_the_engine_spells_it(self) -> None:
        assert TOPIC_ROUTE_CAPABILITY_FLAG == "vector_topic_route_supported"

    @pytest.mark.parametrize("version, expected", [
        ({"vector_topic_route_supported": True}, True),
        ({"vector_topic_route_supported": False}, False),
        ({}, False),
        ({"vector_topic_route_supported": "true"}, False),  # only a JSON true counts
        ({"vector_topic_route_supported": 1}, False),
        ({"vector_search_topic_route_supported": True}, False),  # a near-miss spelling is a different flag
    ])
    def test_only_the_exact_flag_set_to_true_reads_as_supported(self, monkeypatch, version, expected) -> None:
        engine = _engine(monkeypatch, _cols("docs", _BGE, 1), capable=False)
        engine.version = version
        assert engine.client.supports_topic_route() is expected

    # The cloud edge relays an allowlisted /version body (conexus PR #721, nexus-7fku2.1.6): public fields only,
    # the flag as a JSON boolean when the engine sent one, the key dropped when the engine omitted it or sent a
    # non-boolean, and no schema_* fields.
    _EDGE_BODY = {
        "app_version": "8.4.0", "release_version": "0.1.196", "tenant_import_supported": True,
        "vector_topic_route_supported": True, "process_uptime_seconds": 900,
    }

    def test_an_edge_trimmed_body_with_the_flag_reads_as_supported(self, monkeypatch) -> None:
        engine = _engine(monkeypatch, _cols("docs", _BGE, 1), capable=False)
        engine.version = dict(self._EDGE_BODY)
        assert engine.client.supports_topic_route() is True

    def test_an_edge_trimmed_body_without_the_flag_reads_as_unsupported(self, monkeypatch) -> None:
        engine = _engine(monkeypatch, _cols("docs", _BGE, 1), capable=False)
        engine.version = {k: v for k, v in self._EDGE_BODY.items() if k != "vector_topic_route_supported"}
        assert engine.client.supports_topic_route() is False

    def test_an_outage_reads_as_unsupported_and_never_raises(self, monkeypatch) -> None:
        engine = _engine(monkeypatch, _cols("docs", _BGE, 1))
        engine.version_error = VectorServiceError("down", code=503)
        with structlog.testing.capture_logs() as logs:
            assert engine.client.supports_topic_route() is False
        assert [e["event"] for e in logs if e["log_level"] == "warning"] == ["topic_route_probe_failed"]

    def test_the_answer_is_cached_for_the_ttl_then_probed_again(self, monkeypatch) -> None:
        engine = _engine(monkeypatch, _cols("docs", _BGE, 1))
        now = [1000.0]
        monkeypatch.setattr(hvc, "_monotonic", lambda: now[0])
        assert engine.client.supports_topic_route() is True
        assert engine.client.supports_topic_route() is True
        assert engine.version_calls == 1
        now[0] += hvc.HttpVectorClient._TOPIC_ROUTE_TTL_S - 1
        engine.client.supports_topic_route()
        assert engine.version_calls == 1
        now[0] += 2
        engine.version = {}  # an engine rollback: the flag is gone
        assert engine.client.supports_topic_route() is False
        assert engine.version_calls == 2


# ── the request body ─────────────────────────────────────────────────────────


class TestRequestBody:
    def test_a_caller_that_passes_no_topic_route_sends_the_same_body_and_spends_no_probe(self, monkeypatch) -> None:
        cols = _cols("docs", _BGE, 2)
        engine = _engine(monkeypatch, cols)
        _client_search(engine, cols)
        _client_search(engine, cols, topic_route=None)
        first, second = engine.route_calls()
        assert first == second == {"query": "q", "collections": cols, "per_collection_k": 5, "limit": 10}
        assert engine.version_calls == 0

    @pytest.mark.parametrize("version", [{}, {"vector_topic_route_supported": False}])
    def test_without_the_capability_the_body_has_no_field(self, monkeypatch, version) -> None:
        cols = _cols("docs", _BGE, 2)
        engine = _engine(monkeypatch, cols, capable=False)
        engine.version = version
        envelope = _client_search(engine, cols, topic_route=_TOP1)
        assert "topic_route" not in engine.route_calls()[0]
        assert [e["collection"] for e in envelope["per_collection"]] == cols  # the request still ran, unrouted

    def test_an_outage_of_the_probe_sends_an_unrouted_request(self, monkeypatch) -> None:
        cols = _cols("docs", _BGE, 2)
        engine = _engine(monkeypatch, cols)
        engine.version_error = VectorServiceError("down", code=503)
        assert _client_search(engine, cols, topic_route=_TOP1)["results"]
        assert "topic_route" not in engine.route_calls()[0]

    def test_with_the_capability_the_body_carries_mode_and_the_explicit_prose_list(self, monkeypatch) -> None:
        cols = _cols("docs", _BGE, 2)
        engine = _engine(monkeypatch, cols)
        _client_search(engine, cols, topic_route=_TOP1)
        body = engine.route_calls()[0]
        assert body["topic_route"] == {"mode": "top1", "collections": cols}
        assert body["collections"] == cols  # the request itself is untouched

    def test_band_carries_its_width_and_top1_never_does(self, monkeypatch) -> None:
        cols = _cols("docs", _BGE, 1)
        engine = _engine(monkeypatch, cols)
        _client_search(engine, cols, topic_route={"mode": "band", "width": 0.05})
        _client_search(engine, cols, topic_route={"mode": "top1", "width": 0.05})
        band, top1 = [b["topic_route"] for b in engine.route_calls()]
        assert band == {"mode": "band", "width": 0.05, "collections": cols}
        assert top1 == {"mode": "top1", "collections": cols}

    @pytest.mark.parametrize("given, sent", [
        (0.5, 0.5), (0, 0.0), (1, 1.0), (0.9, 0.9), (1.0, 1.0),
    ])
    def test_min_coverage_is_forwarded_in_the_field_as_a_float(self, monkeypatch, given, sent) -> None:
        cols = _cols("docs", _BGE, 1)
        engine = _engine(monkeypatch, cols)
        _client_search(engine, cols, topic_route={"mode": "top1", "min_coverage": given})
        carrier = engine.route_calls()[0]["topic_route"]
        assert carrier == {"mode": "top1", "min_coverage": sent, "collections": cols}
        assert isinstance(carrier["min_coverage"], float)

    @pytest.mark.parametrize("given", [{"mode": "top1"}, {"mode": "top1", "min_coverage": None}])
    def test_without_a_min_coverage_the_field_has_no_such_key_and_the_engine_keeps_its_default(
        self, monkeypatch, given,
    ) -> None:
        cols = _cols("docs", _BGE, 1)
        engine = _engine(monkeypatch, cols)
        _client_search(engine, cols, topic_route=given)
        assert "min_coverage" not in engine.route_calls()[0]["topic_route"]

    def test_min_coverage_reaches_the_body_from_search_cross_corpus_too(self, monkeypatch) -> None:
        cols = _cols("docs", _BGE, 1)
        engine = _engine(monkeypatch, cols)
        _search(engine, cols, topic_route={"mode": "band", "width": 0.05, "min_coverage": 0.25})
        assert engine.route_calls()[0]["topic_route"] == {
            "mode": "band", "width": 0.05, "min_coverage": 0.25, "collections": cols,
        }

    @pytest.mark.parametrize("given", [{"mode": "top1"}, {"mode": "top1", "collections": None}])
    def test_the_client_always_sends_an_explicit_list_never_a_null_or_an_absent_key(self, monkeypatch, given) -> None:
        # The engine reads an absent or JSON-null ``collections`` as "route every collection of the request",
        # code ones included; only an explicit list is prose-only.
        cols = _cols("docs", _BGE, 1) + _cols("code", _BGE, 1)
        engine = _engine(monkeypatch, cols)
        _client_search(engine, cols, topic_route=given)
        carrier = engine.route_calls()[0]["topic_route"]
        assert isinstance(carrier["collections"], list) and carrier["collections"] == cols[:1]
        assert '"collections": null' not in json.dumps(engine.route_calls()[0])

    def test_code_names_are_filtered_out_and_a_code_only_group_gets_no_field(self, monkeypatch) -> None:
        code = _cols("code", _BGE, 2)
        engine = _engine(monkeypatch, code)
        _client_search(engine, code, topic_route=_TOP1)
        assert "topic_route" not in engine.route_calls()[0]
        assert engine.version_calls == 0  # nothing eligible: the capability is not even asked

    def test_a_mixed_prose_and_code_group_names_only_the_prose(self, monkeypatch) -> None:
        # Local bge puts code and docs in one model group, so one request holds both.
        cols = _cols("code", _BGE, 2) + _cols("docs", _BGE, 2) + _cols("rdr", _BGE, 1)
        engine = _engine(monkeypatch, cols)
        _search(engine, cols, topic_route=_TOP1)
        (body,) = engine.route_calls()
        assert body["topic_route"]["collections"] == _cols("docs", _BGE, 2) + _cols("rdr", _BGE, 1)
        assert body["collections"] == cols
        assert not any(c.startswith("code__") for c in body["topic_route"]["collections"])

    def test_every_prose_prefix_is_eligible_and_nothing_else_is(self, monkeypatch) -> None:
        prose = [f"{p}__c0__{_BGE}__v1" for p in ("knowledge", "docs", "rdr", "paper")]
        other = [f"code__c0__{_BGE}__v1", f"taxonomy__c0__{_BGE}__v1", f"mydocs__c0__{_BGE}__v1"]
        engine = _engine(monkeypatch, prose + other)
        _client_search(engine, other + prose, topic_route=_TOP1)
        assert engine.route_calls()[0]["topic_route"]["collections"] == prose

    def test_a_listed_subset_narrows_further_and_an_empty_list_sends_no_field(self, monkeypatch) -> None:
        cols = _cols("docs", _BGE, 3) + _cols("code", _BGE, 1)
        engine = _engine(monkeypatch, cols)
        _client_search(engine, cols, topic_route={"mode": "top1", "collections": [cols[1], cols[3]]})
        _client_search(engine, cols, topic_route={"mode": "top1", "collections": []})
        first, second = engine.route_calls()
        assert first["topic_route"]["collections"] == [cols[1]]  # the listed code name is filtered, not sent
        assert "topic_route" not in second

    def test_chash_allow_and_where_leave_the_request_unrouted_and_unprobed(self, monkeypatch) -> None:
        cols = _cols("docs", _BGE, 1)
        engine = _engine(monkeypatch, cols)
        _client_search(engine, cols, topic_route=_TOP1, where={"a": 1})
        _client_search(engine, cols, topic_route=_TOP1, chash_allow=["a" * 64])
        assert all("topic_route" not in b for b in engine.route_calls())
        assert engine.version_calls == 0

    @pytest.mark.parametrize("bad", [
        {}, {"mode": "top2"}, {"mode": "band"}, {"mode": "band", "width": -0.1},
        {"mode": "band", "width": float("inf")}, {"mode": "band", "width": "wide"},
        {"mode": "band", "width": True}, {"mode": "top1", "collections": "docs__x"},
        {"mode": "top1", "collections": ["docs__not_requested"]},
        # min_coverage is a number from 0 to 1, as the engine's 400 says (nan, bool, string, list are not numbers in range)
        {"mode": "top1", "min_coverage": -0.01}, {"mode": "top1", "min_coverage": 1.01},
        {"mode": "top1", "min_coverage": 2}, {"mode": "top1", "min_coverage": float("nan")},
        {"mode": "top1", "min_coverage": float("inf")}, {"mode": "top1", "min_coverage": "0.5"},
        {"mode": "top1", "min_coverage": True}, {"mode": "band", "width": 0.1, "min_coverage": [0.5]},
    ])
    def test_a_bad_value_raises_before_any_request(self, monkeypatch, bad) -> None:
        cols = _cols("docs", _BGE, 1)
        engine = _engine(monkeypatch, cols)
        with pytest.raises(ValueError):
            _client_search(engine, cols, topic_route=bad)
        assert engine.calls == [] and engine.version_calls == 0

    def test_a_bad_min_coverage_raises_even_when_the_request_would_go_unrouted(self, monkeypatch) -> None:
        # a bad value must not hide behind a where, a chash_allow or a code-only group
        cols = _cols("docs", _BGE, 1)
        code = _cols("code", _BGE, 1)
        engine = _engine(monkeypatch, cols + code)
        bad = {"mode": "top1", "min_coverage": 3}
        with pytest.raises(ValueError, match="min_coverage"):
            _client_search(engine, cols, topic_route=bad, where={"a": 1})
        with pytest.raises(ValueError, match="min_coverage"):
            _client_search(engine, code, topic_route=bad)
        with pytest.raises(ValueError, match="min_coverage"):
            _search(engine, cols, topic_route=bad)
        assert engine.calls == []


# ── the envelope ─────────────────────────────────────────────────────────────


class TestEnvelope:
    def test_the_four_topic_route_keys_survive_the_validator(self, monkeypatch) -> None:
        cols = _cols("docs", _BGE, 2)
        engine = _engine(monkeypatch, cols)
        engine.statuses = {cols[1]: "no_topics"}
        engine.extras = {cols[0]: {"topic_route_coverage": 0.97, "topic_route_topics": 3},
                         cols[1]: {"topic_route_error": "coverage lookup: SQLSTATE 57014"}}
        entries = {e["collection"]: e for e in _client_search(engine, cols, topic_route=_TOP1)["per_collection"]}
        assert entries[cols[0]]["topic_route_status"] == "routed"
        assert entries[cols[0]]["topic_route_coverage"] == 0.97 and entries[cols[0]]["topic_route_topics"] == 3
        assert entries[cols[1]]["topic_route_status"] == "no_topics"
        assert entries[cols[1]]["topic_route_error"] == "coverage lookup: SQLSTATE 57014"

    def test_the_min_coverage_echo_survives_the_validator_and_a_malformed_one_is_dropped(self, monkeypatch) -> None:
        cols = _cols("docs", _BGE, 3)
        engine = _engine(monkeypatch, cols)
        engine.extras = {cols[0]: {"topic_route_min_coverage": 0.5},
                         cols[1]: {"topic_route_min_coverage": 2},       # out of range: dropped
                         cols[2]: {"topic_route_min_coverage": "0.5"}}   # not a number: dropped
        entries = {e["collection"]: e for e in _client_search(engine, cols, topic_route=_TOP1)["per_collection"]}
        assert entries[cols[0]]["topic_route_min_coverage"] == 0.5
        assert "topic_route_min_coverage" not in entries[cols[1]]
        assert "topic_route_min_coverage" not in entries[cols[2]]
        assert all(e["topic_route_status"] == "routed" for e in entries.values())  # none refused the envelope

    def test_an_unrouted_envelope_is_exactly_what_it_was(self, monkeypatch) -> None:
        cols = _cols("docs", _BGE, 1)
        engine = _engine(monkeypatch, cols)
        (entry,) = _client_search(engine, cols)["per_collection"]
        assert set(entry) == {"collection", "raw_count", "dropped", "min_raw_distance", "min_dropped_distance",
                              "error", "error_kind"}

    def test_a_malformed_topic_route_key_is_dropped_not_refused(self, monkeypatch) -> None:
        # A diagnostic must not switch the route off (a refused envelope writes it off for a minute).
        cols = _cols("docs", _BGE, 1)
        engine = _engine(monkeypatch, cols)
        engine.extras = {cols[0]: {"topic_route_status": 7, "topic_route_error": ["x"],
                                   "topic_route_coverage": "high", "topic_route_topics": -1}}
        (entry,) = _client_search(engine, cols, topic_route=_TOP1)["per_collection"]
        assert not [k for k in entry if k.startswith("topic_route_")]


# ── the search_topic_route event ─────────────────────────────────────────────


class TestEvent:
    def test_one_event_per_routed_request_with_statuses_and_counts(self, monkeypatch) -> None:
        # Two model groups = two engine requests, and still ONE event for the search.
        bge, mini = _cols("docs", _BGE, 3), _cols("knowledge", _MINI, 1)
        engine = _engine(monkeypatch, bge + mini)
        engine.statuses = {bge[1]: "no_topics", bge[2]: "fallback"}
        engine.extras = {bge[1]: {"topic_route_error": "centroid ranking cut by its bound"},
                         bge[0]: {"topic_route_coverage": 0.95, "topic_route_topics": 2}}
        events, _ = _events(lambda: _search(engine, bge + mini, topic_route={"mode": "band", "width": 0.04}))
        assert len(events) == 1 and len(engine.route_calls()) == 2
        (e,) = events
        assert e["statuses"] == {bge[0]: "routed", bge[1]: "no_topics", bge[2]: "fallback", mini[0]: "routed"}
        assert (e["routed"], e["no_topics"], e["fallback"], e["skipped"], e["unreported"]) == (2, 1, 1, 0, 0)
        assert e["mode"] == "band" and e["width"] == 0.04 and e["eligible"] == 4 and e["ineligible"] == 0
        assert e["errors"] == {bge[1]: "centroid ranking cut by its bound"}
        assert e["coverage"] == {bge[0]: 0.95} and e["topics"] == {bge[0]: 2}
        assert e["unrouted_reason"] is None

    def test_the_event_carries_raw_counts_and_dropped_per_collection_for_the_entries_with_a_status(
        self, monkeypatch,
    ) -> None:
        # S1: a routed collection whose rows all fell to the client's threshold (raw 5, dropped 5) reads routed, and
        # only these two maps tell it from a short page (raw 4, dropped 0)
        cols = _cols("docs", _BGE, 3)
        code = _cols("code", _BGE, 1)
        engine = _engine(monkeypatch, cols + code)
        engine.extras = {cols[0]: {"raw_count": 5, "dropped": 5}, cols[1]: {"raw_count": 4, "dropped": 1}}
        (e,), _ = _events(lambda: _search(engine, cols + code, topic_route=_TOP1))
        assert e["raw_counts"] == {cols[0]: 5, cols[1]: 4, cols[2]: 4}
        assert e["dropped"] == {cols[0]: 5, cols[1]: 1, cols[2]: 0}
        assert code[0] not in e["raw_counts"] and code[0] not in e["dropped"]  # ineligible: no status, not listed

    def test_the_maps_cover_a_collection_only_when_the_engine_sent_it_a_status(self, monkeypatch) -> None:
        cols = _cols("docs", _BGE, 2)
        engine = _engine(monkeypatch, cols)
        engine.errors = {cols[1]: ("statement_timeout", "slow")}
        (e,), _ = _events(lambda: _search(engine, cols, topic_route=_TOP1))
        assert e["raw_counts"] == {cols[0]: 4} and e["dropped"] == {cols[0]: 0}
        assert e["unreported"] == 1

    def test_a_request_the_client_leaves_unrouted_has_no_counts_to_report(self, monkeypatch) -> None:
        # chash_allow/where: the client counts every prose collection skipped itself and no engine entry exists
        cols = _cols("docs", _BGE, 1)
        engine = _engine(monkeypatch, cols)
        (e,), _ = _events(lambda: _search(engine, cols, topic_route=_TOP1, where={"a": 1}))
        assert e["statuses"] == {cols[0]: "skipped"}
        assert "raw_counts" not in e and "dropped" not in e

    def test_the_event_records_the_min_coverage_asked_for_and_the_one_the_engine_echoed(self, monkeypatch) -> None:
        cols = _cols("docs", _BGE, 2)
        engine = _engine(monkeypatch, cols)
        (asked,), _ = _events(lambda: _search(engine, cols, topic_route={"mode": "top1", "min_coverage": 0.5}))
        assert asked["min_coverage"] == 0.5                      # asked for, engine echoed nothing
        engine.extras = {c: {"topic_route_min_coverage": 0.5} for c in cols}
        (echoed,), _ = _events(lambda: _search(engine, cols, topic_route={"mode": "top1", "min_coverage": 0.5}))
        assert echoed["min_coverage"] == 0.5
        engine.extras = {c: {"topic_route_min_coverage": 0.9} for c in cols}
        (default,), _ = _events(lambda: _search(engine, cols, topic_route=_TOP1))
        assert default["min_coverage"] == 0.9, "no value asked for: the engine's echoed default is what was in force"
        engine.extras = {}
        (none,), _ = _events(lambda: _search(engine, cols, topic_route=_TOP1))
        assert none["min_coverage"] is None, "nothing asked for and nothing echoed: the engine's default, unreported"

    def test_the_counts_are_named_by_the_status_table(self, monkeypatch) -> None:
        cols = _cols("docs", _BGE, 1)
        engine = _engine(monkeypatch, cols)
        (e,), _ = _events(lambda: _search(engine, cols, topic_route=_TOP1))
        assert all(s in e for s in STATUSES) and sum(e[s] for s in STATUSES) == 1

    def test_no_event_and_no_field_for_a_request_that_asked_for_no_routing(self, monkeypatch) -> None:
        cols = _cols("docs", _BGE, 2)
        engine = _engine(monkeypatch, cols)
        events, _ = _events(lambda: _search(engine, cols))
        assert events == [] and "topic_route" not in engine.route_calls()[0]
        assert engine.version_calls == 0

    def test_results_do_not_change_for_a_caller_that_passes_no_topic_route(self, monkeypatch) -> None:
        cols = _cols("docs", _BGE, 2) + _cols("code", _BGE, 1)
        capable = _engine(monkeypatch, cols)
        with_cap = [(r.id, r.collection, r.distance) for r in _search(capable, cols)]
        old = _engine(monkeypatch, cols, capable=False)
        without = [(r.id, r.collection, r.distance) for r in _search(old, cols)]
        assert with_cap == without and with_cap
        assert capable.route_calls() == old.route_calls()

    def test_a_where_leaves_the_request_unrouted_and_every_prose_collection_counts_as_skipped(self, monkeypatch) -> None:
        cols = _cols("docs", _BGE, 2) + _cols("code", _BGE, 1)
        engine = _engine(monkeypatch, cols)
        (e,), _ = _events(lambda: _search(engine, cols, where={"lang": "en"}, topic_route=_TOP1))
        assert "topic_route" not in engine.route_calls()[0]
        assert e["skipped"] == 2 and e["eligible"] == 2 and e["ineligible"] == 1
        assert e["unrouted_reason"] == "where"
        assert set(e["statuses"].values()) == {"skipped"}

    def test_chash_allow_leaves_the_request_unrouted_and_counts_as_skipped(self, monkeypatch) -> None:
        cols = _cols("docs", _BGE, 2)
        engine = _engine(monkeypatch, cols)

        def run():
            with chash_allow_scope(["a" * 64]):
                return _search(engine, cols, topic_route=_TOP1)

        (e,), _ = _events(run)
        assert all("topic_route" not in b for b in engine.route_calls())
        assert e["skipped"] == 2 and e["unrouted_reason"] == "chash_allow" and e["routed"] == 0

    def test_subtree_never_reaches_this_layer_so_its_count_here_is_structurally_zero(self) -> None:
        # RDR-134 lines 347-351: a subtree or catalog-route query goes through search_metadata_scoped and friends,
        # not search_cross_corpus. This tripwire fails the day search_cross_corpus grows a subtree argument: that
        # change must add "subtree" to the event's unrouted reasons.
        assert "subtree" not in inspect.signature(search_cross_corpus).parameters

    def test_a_backend_without_the_capability_reports_the_request_as_unrouted_not_skipped(self, monkeypatch) -> None:
        cols = _cols("docs", _BGE, 2)
        engine = _engine(monkeypatch, cols, capable=False)
        (e,), results = _events(lambda: _search(engine, cols, topic_route=_TOP1))
        assert "topic_route" not in engine.route_calls()[0] and results
        assert (e["unreported"], e["skipped"], e["routed"]) == (2, 0, 0)
        assert e["unrouted_reason"] == "capability_absent" and e["statuses"] == {}

    def test_a_routed_call_takes_the_per_collection_route_without_the_opt_in(self, monkeypatch) -> None:
        # RDR-134 Step 7: the opt-in keeps the DEFAULT path off the slower route; a call that asks for topic
        # routing has asked for the one route that can route.
        cols = _cols("docs", _BGE, 2)
        engine = _engine(monkeypatch, cols)
        monkeypatch.delenv(hvc.PER_COLLECTION_ROUTE_ENV)
        (e,), _ = _events(lambda: _search(engine, cols, topic_route=_TOP1))
        (call,) = engine.route_calls()
        assert call["topic_route"]["collections"] == cols
        assert e["unrouted_reason"] is None and e["routed"] == 2

    def test_without_the_opt_in_an_unrouted_call_stays_off_the_route(self, monkeypatch) -> None:
        cols = _cols("docs", _BGE, 2)
        engine = _engine(monkeypatch, cols)
        monkeypatch.delenv(hvc.PER_COLLECTION_ROUTE_ENV)
        _search(engine, cols)
        assert engine.route_calls() == [], "the default path is unchanged"

    @pytest.mark.parametrize("case", ["where", "chash_allow", "code_only"])
    def test_without_the_opt_in_a_call_that_cannot_route_stays_off_the_route(self, monkeypatch, case) -> None:
        # the slower route buys nothing when the request goes unrouted anyway
        cols = _cols("code", _BGE, 2) if case == "code_only" else _cols("docs", _BGE, 2)
        engine = _engine(monkeypatch, cols)
        monkeypatch.delenv(hvc.PER_COLLECTION_ROUTE_ENV)
        kw: dict = {"topic_route": _TOP1}
        if case == "where":
            kw["where"] = {"bib_year": 2020}
        if case == "chash_allow":
            with chash_allow_scope(["a" * 64]):
                _search(engine, cols, **kw)
        else:
            _search(engine, cols, **kw)
        assert engine.route_calls() == []

    def test_without_the_opt_in_an_engine_that_cannot_route_keeps_the_default_path(self, monkeypatch) -> None:
        # An engine without vector_topic_route_supported (0.1.196, or a cloud edge whose /version allowlist
        # trims the flag) answers unrouted; forcing it onto the slower route would buy nothing.
        cols = _cols("docs", _BGE, 2)
        engine = _engine(monkeypatch, cols, capable=False)
        monkeypatch.delenv(hvc.PER_COLLECTION_ROUTE_ENV)
        (e,), results = _events(lambda: _search(engine, cols, topic_route=_TOP1))
        assert engine.route_calls() == [] and results
        assert e["routed"] == 0 and e["unrouted_reason"] == "per_collection_route_off"

    def test_a_client_without_the_route_cannot_route(self, monkeypatch) -> None:
        cols = _cols("docs", _BGE, 2)
        engine = _engine(monkeypatch, cols)
        monkeypatch.setattr(engine.client, "supports_per_collection_search", False, raising=False)
        (e,), _ = _events(lambda: _search(engine, cols, topic_route=_TOP1))
        assert engine.route_calls() == [] and e["unrouted_reason"] == "per_collection_route_off"
        assert e["unreported"] == 2

    def test_a_code_only_request_gets_an_event_that_says_there_was_nothing_to_route(self, monkeypatch) -> None:
        cols = _cols("code", _BGE, 2)
        engine = _engine(monkeypatch, cols)
        (e,), _ = _events(lambda: _search(engine, cols, topic_route=_TOP1))
        assert "topic_route" not in engine.route_calls()[0]
        assert e["unrouted_reason"] == "no_prose_collections" and e["eligible"] == 0 and e["ineligible"] == 2

    def test_an_errored_arm_has_no_status_and_is_neither_skipped_nor_a_crash(self, monkeypatch) -> None:
        cols = _cols("docs", _BGE, 3)
        engine = _engine(monkeypatch, cols)
        engine.errors = {cols[1]: ("statement_timeout", "canceling statement")}
        (e,), _ = _events(lambda: _search(engine, cols, topic_route=_TOP1))
        assert cols[1] not in e["statuses"]
        assert (e["routed"], e["skipped"], e["unreported"]) == (2, 0, 1)

    def test_an_engine_that_reports_no_status_at_all_is_all_unreported(self, monkeypatch) -> None:
        cols = _cols("docs", _BGE, 2)
        engine = _engine(monkeypatch, cols)
        # The flag reads true, yet the response carries no status (a base-class response, no routing added).
        monkeypatch.setattr(engine, "_serve_route", lambda body: _FakeEngine._serve_route(engine, body))
        (e,), _ = _events(lambda: _search(engine, cols, topic_route=_TOP1))
        assert (e["unreported"], e["skipped"], e["routed"]) == (2, 0, 0) and e["statuses"] == {}

    def test_a_status_outside_the_table_is_counted_apart_not_as_any_status(self, monkeypatch) -> None:
        cols = _cols("docs", _BGE, 2)
        engine = _engine(monkeypatch, cols)
        engine.statuses = {cols[0]: "quarantined"}
        (e,), _ = _events(lambda: _search(engine, cols, topic_route=_TOP1))
        assert e["unrecognized"] == 1 and e["routed"] == 1 and e["statuses"][cols[0]] == "quarantined"

    def test_a_route_the_engine_does_not_serve_still_returns_rows_and_logs_the_unrouted_request(self, monkeypatch) -> None:
        cols = _cols("docs", _BGE, 2)
        engine = _engine(monkeypatch, cols, route=False)
        (e,), results = _events(lambda: _search(engine, cols, topic_route=_TOP1))
        assert results and e["unreported"] == 2 and e["routed"] == 0

    def test_a_group_the_engine_refused_counts_as_unreported(self, monkeypatch) -> None:
        bge, mini = _cols("docs", _BGE, 1), _cols("docs", _MINI, 1)
        engine = _engine(monkeypatch, bge + mini)
        engine.fail_when = lambda body: (400, {"error": "bad"}) if body["collections"] == mini else None
        (e,), _ = _events(lambda: _search(engine, bge + mini, topic_route=_TOP1))
        assert e["statuses"] == {bge[0]: "routed"} and e["unreported"] == 1
