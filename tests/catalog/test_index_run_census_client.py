# SPDX-License-Identifier: AGPL-3.0-or-later
"""``HttpCatalogClient.index_run_census`` and ``next_seq_drift`` (nexus-f3yqd.6, F30): the engine's two bounded census
reads behind the doctor's index-run rows (``GET /v1/catalog/index-run/census`` and ``GET
/v1/catalog/owners/next_seq_drift``, engine 0.1.189; flags ``catalog_index_run_census_supported`` and
``catalog_next_seq_drift_read_supported``).

``None`` means "cannot say", never "none found": no flag (no request is sent), a 403, 404 or 503, any error, or a body
that is not the documented shape. A bucket the engine could not read in time travels as ``{"timed_out": True}``.
"""
from __future__ import annotations

import copy
from datetime import datetime

import httpx
import pytest

from nexus import health
from nexus.catalog.http_catalog_client import (
    INDEX_RUN_CENSUS_RULES_DIFFER,
    INDEX_RUN_FENCE_FLOOR_ISO,
    INDEX_RUN_STALE_THRESHOLD_S,
    INDEX_RUN_CENSUS_MAX_LIMIT,
    INDEX_RUN_CENSUS_TIMEOUT_S,
    NEXT_SEQ_DRIFT_TIMEOUT_S,
    HttpCatalogClient,
)

_CENSUS = {
    "limit": 10000, "threshold_s": 21600, "floor": "2026-08-07T16:00:35Z",
    "stale": {"count": 1, "capped": False, "notes": 1, "examples": [
        {"tumbler": "1.1.1", "source_uri": None, "started_at": "2026-11-10T00:00:00Z", "age_s": 90000, "note": True}]},
    "fence": {
        "checked": 4, "reported_null": 3, "undated_reported_null": 1, "floor_candidates": 2,
        "anchor": {"kind": "proven", "at": "2026-08-20T10:00:00Z", "earliest_stamped_at": "2026-08-09T09:00:00Z",
                   "proven_at": "2026-08-20T10:00:00Z"},
        "post_anchor": {"count": 1, "capped": False, "examples": [
            {"tumbler": "2.1.6", "source_uri": "file:///r/a.py", "file_path": "a.py", "indexed_at": "2026-09-01T00:00:00Z"}]}},
    "failed_hidden": {"failed": {"count": 5, "capped": False}, "count": 2, "capped": False, "examples": [
        {"tumbler": "3.1.1", "source_uri": "file:///r/b.py", "chunk_count": 3, "chunks_stored": 3}]},
}
_DRIFT = {"checked": 4, "drifted": 1, "limit": 10000, "capped": False,
          "examples": [{"owner": "1.12", "next_seq": 3, "highest_child_seq": 9}]}


def _client(monkeypatch: pytest.MonkeyPatch, *, flags: dict | None = None, get=None) -> tuple[HttpCatalogClient, list]:
    c = object.__new__(HttpCatalogClient)
    calls: list = []
    monkeypatch.setattr(c, "_version_flags", lambda: flags or {}, raising=False)

    def record(path, **kw):
        calls.append((path, kw))
        return get(path, **kw) if callable(get) else get

    monkeypatch.setattr(c, "_get", record, raising=False)
    return c, calls


_BOTH = {"catalog_index_run_census_supported": True, "catalog_next_seq_drift_read_supported": True}


def _status(status: int):
    def fail(path, **kw):
        request = httpx.Request("GET", "http://engine/v1/catalog" + path)
        raise httpx.HTTPStatusError("x", request=request, response=httpx.Response(status, request=request))
    return fail


# ── the capability ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(("flags", "want"), [
    (_BOTH, True),
    ({"catalog_index_run_census_supported": True}, False),
    ({"catalog_next_seq_drift_read_supported": True}, False),
    ({"catalog_index_run_census_supported": "true", "catalog_next_seq_drift_read_supported": True}, False),
    ({"catalog_index_run_census_supported": False, "catalog_next_seq_drift_read_supported": False}, False),
    ({}, False),
])
def test_both_routes_are_supported_only_when_both_flags_are_json_true(monkeypatch: pytest.MonkeyPatch, flags: dict, want: bool) -> None:
    c, _calls = _client(monkeypatch, flags=flags)
    assert c.index_run_routes_supported() is want


def test_a_failed_version_probe_is_told_from_an_answer_without_the_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(self, path, **kw):
        raise httpx.ConnectError("refused")

    for body, failed in (({}, False), ({"catalog_index_run_census_supported": True}, False)):
        c = object.__new__(HttpCatalogClient)
        monkeypatch.setattr("nexus.db.t2._refreshable_client.RefreshableHttpStoreMixin._get",
                            lambda self, path, **kw: body)
        assert c.index_run_routes_supported() is False and c.version_probe_failed() is failed
    c = object.__new__(HttpCatalogClient)
    monkeypatch.setattr("nexus.db.t2._refreshable_client.RefreshableHttpStoreMixin._get", refuse)
    assert c.index_run_routes_supported() is False and c.version_probe_failed() is True


# ── the census ───────────────────────────────────────────────────────────────


def test_the_census_travels_the_wire_with_the_engines_whole_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    c, calls = _client(monkeypatch, flags=_BOTH, get=_CENSUS)
    got = c.index_run_census()
    assert got == {k: _CENSUS[k] for k in ("stale", "fence", "failed_hidden")}
    ((path, kw),) = calls
    assert path == "/index-run/census" and kw["limit"] == INDEX_RUN_CENSUS_MAX_LIMIT == 10_000 and kw["idempotent"] is False
    # Under the public edge's roughly 30 s cut, which is also the engine's worst case (three 10 s buckets in series):
    # a longer timeout buys nothing through the edge.
    assert kw["timeout"] == INDEX_RUN_CENSUS_TIMEOUT_S and 25 < INDEX_RUN_CENSUS_TIMEOUT_S < 30


def test_a_limit_the_caller_names_is_the_limit_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    c, calls = _client(monkeypatch, flags=_BOTH, get=_CENSUS)
    c.index_run_census(limit=50)
    assert calls[0][1]["limit"] == 50


@pytest.mark.parametrize("flags", [{}, {"catalog_index_run_census_supported": False},
                                   {"catalog_next_seq_drift_read_supported": True}])
def test_an_engine_without_the_census_flag_is_not_asked(monkeypatch: pytest.MonkeyPatch, flags: dict) -> None:
    c, calls = _client(monkeypatch, flags=flags, get=_CENSUS)
    assert c.index_run_census() is None and calls == []


@pytest.mark.parametrize("status", [403, 404, 500, 503])
def test_a_refusal_a_missing_route_or_a_timeout_is_cannot_say(monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    c, _calls = _client(monkeypatch, flags=_BOTH, get=_status(status))
    assert c.index_run_census() is None


def test_a_transport_error_is_cannot_say(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(path, **kw):
        raise httpx.ConnectError("refused")

    c, _calls = _client(monkeypatch, flags=_BOTH, get=fail)
    assert c.index_run_census() is None


def test_a_bucket_that_timed_out_travels_beside_the_ones_that_answered(monkeypatch: pytest.MonkeyPatch) -> None:
    body = {**_CENSUS, "fence": {"timed_out": True}}
    c, _calls = _client(monkeypatch, flags=_BOTH, get=body)
    got = c.index_run_census()
    assert got["fence"] == {"timed_out": True} and got["stale"] == _CENSUS["stale"] and got["failed_hidden"] == _CENSUS["failed_hidden"]


def _mutated(path: tuple, value) -> dict:
    body = copy.deepcopy(_CENSUS)
    node = body
    for key in path[:-1]:
        node = node[key]
    if value is KeyError:
        del node[path[-1]]
    else:
        node[path[-1]] = value
    return body


@pytest.mark.parametrize(("path", "value"), [
    (("stale",), KeyError), (("fence",), None), (("failed_hidden",), []),
    (("stale", "count"), "1"), (("stale", "count"), True), (("stale", "count"), -1), (("stale", "capped"), "no"),
    (("stale", "notes"), KeyError), (("stale", "examples"), {}), (("stale", "examples", 0, "age_s"), 1.5),
    (("stale", "examples", 0, "note"), 1), (("stale", "examples", 0, "tumbler"), None),
    (("stale", "count"), 0),  # an example the count does not hold
    (("fence", "checked"), KeyError), (("fence", "anchor", "kind"), "other"),
    (("fence", "anchor", "at"), None), (("fence", "anchor", "at"), "not a time"),
    (("fence", "post_anchor", "count"), 0), (("fence", "post_anchor", "examples", 0, "indexed_at"), None),
    (("fence", "post_anchor", "examples", 0, "indexed_at"), "yesterday"), (("fence", "post_anchor", "capped"), 0),
    (("failed_hidden", "failed"), 5), (("failed_hidden", "failed", "capped"), "x"), (("failed_hidden", "count"), 6),
    (("failed_hidden", "examples", 0, "chunk_count"), "3"),
    (("fence", "timed_out"), True),  # a timed-out marker mixed into an answered bucket
])
def test_a_malformed_census_body_is_cannot_say(monkeypatch: pytest.MonkeyPatch, path: tuple, value) -> None:
    c, _calls = _client(monkeypatch, flags=_BOTH, get=_mutated(path, value))
    assert c.index_run_census() is None


@pytest.mark.parametrize("body", [None, [], "x", {}, {"stale": {"timed_out": True}},
                                  {"stale": {"timed_out": False}, "fence": {"timed_out": True}, "failed_hidden": {"timed_out": True}}])
def test_a_body_without_all_three_buckets_is_cannot_say(monkeypatch: pytest.MonkeyPatch, body: object) -> None:
    c, _calls = _client(monkeypatch, flags=_BOTH, get=body)
    assert c.index_run_census() is None


@pytest.mark.parametrize(("key", "value"), [
    ("threshold_s", 3600), ("threshold_s", 21601), ("threshold_s", 0),
    ("floor", "2026-08-07T16:00:36Z"), ("floor", "2026-08-01T00:00:00Z"),
])
def test_a_census_computed_with_another_threshold_or_floor_says_so(monkeypatch: pytest.MonkeyPatch, key: str, value) -> None:
    """A well-formed ``threshold_s`` or ``floor`` that is not the client's is its own answer, not a malformed body."""
    c, _calls = _client(monkeypatch, flags=_BOTH, get={**_CENSUS, key: value})
    assert c.index_run_census() is INDEX_RUN_CENSUS_RULES_DIFFER


@pytest.mark.parametrize(("key", "value"), [
    ("threshold_s", "21600"), ("threshold_s", True), ("threshold_s", None), ("threshold_s", -1),
    ("floor", "not a time"), ("floor", None), ("floor", 0),
])
def test_a_census_whose_threshold_or_floor_is_malformed_is_cannot_say(monkeypatch: pytest.MonkeyPatch, key: str, value) -> None:
    c, _calls = _client(monkeypatch, flags=_BOTH, get={**_CENSUS, key: value})
    assert c.index_run_census() is None


@pytest.mark.parametrize("key", ["threshold_s", "floor"])
def test_a_census_that_does_not_echo_its_threshold_and_floor_is_cannot_say(monkeypatch: pytest.MonkeyPatch, key: str) -> None:
    body = {k: v for k, v in _CENSUS.items() if k != key}
    c, _calls = _client(monkeypatch, flags=_BOTH, get=body)
    assert c.index_run_census() is None


def test_the_clients_threshold_and_floor_are_the_rules_the_oracle_applies() -> None:
    assert health._STALE_INDEXING_THRESHOLD_HOURS * 3600 == INDEX_RUN_STALE_THRESHOLD_S
    assert health._PRODUCER_FENCE_RELEASE_DT == datetime.fromisoformat(INDEX_RUN_FENCE_FLOOR_ISO.replace("Z", "+00:00"))


def test_an_unanchored_fence_with_no_examples_is_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    fence = {"checked": 0, "reported_null": 3, "undated_reported_null": 1, "floor_candidates": 0,
             "anchor": {"kind": None, "at": None, "earliest_stamped_at": None, "proven_at": None},
             "post_anchor": {"count": 0, "capped": False, "examples": []}}
    c, _calls = _client(monkeypatch, flags=_BOTH, get={**_CENSUS, "fence": fence})
    assert c.index_run_census()["fence"] == fence


# ── the drift read ───────────────────────────────────────────────────────────


def test_the_drift_travels_the_wire_with_the_engines_whole_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    c, calls = _client(monkeypatch, flags=_BOTH, get=_DRIFT)
    assert c.next_seq_drift() == {"checked": 4, "drifted": 1, "capped": False, "examples": _DRIFT["examples"]}
    ((path, kw),) = calls
    assert path == "/owners/next_seq_drift" and kw["limit"] == 10_000 and kw["idempotent"] is False
    # The engine re-arms a 10 s statement timeout per probe after a call-level deadline check: about 20 s worst case.
    assert kw["timeout"] == NEXT_SEQ_DRIFT_TIMEOUT_S and 20 < NEXT_SEQ_DRIFT_TIMEOUT_S < 30


@pytest.mark.parametrize("flags", [{}, {"catalog_next_seq_drift_read_supported": False},
                                   {"catalog_index_run_census_supported": True}])
def test_an_engine_without_the_drift_flag_is_not_asked(monkeypatch: pytest.MonkeyPatch, flags: dict) -> None:
    c, calls = _client(monkeypatch, flags=flags, get=_DRIFT)
    assert c.next_seq_drift() is None and calls == []


@pytest.mark.parametrize("status", [403, 404, 500, 503])
def test_a_drift_refusal_a_missing_route_or_a_timeout_is_cannot_say(monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    c, _calls = _client(monkeypatch, flags=_BOTH, get=_status(status))
    assert c.next_seq_drift() is None


@pytest.mark.parametrize("patch", [
    {"checked": "4"}, {"checked": True}, {"drifted": -1}, {"capped": 1}, {"examples": None}, {"examples": [3]},
    {"drifted": 0},  # an example the count does not hold
    {"checked": 0},  # more drifted than examined
    {"examples": [{"owner": "", "next_seq": 3, "highest_child_seq": 9}]},
    {"examples": [{"owner": "1.1", "next_seq": "3", "highest_child_seq": 9}]},
    {"examples": [{"owner": "1.1", "next_seq": 9, "highest_child_seq": 9}]},  # equality is not drift
])
def test_a_malformed_drift_body_is_cannot_say(monkeypatch: pytest.MonkeyPatch, patch: dict) -> None:
    c, _calls = _client(monkeypatch, flags=_BOTH, get={**_DRIFT, **patch})
    assert c.next_seq_drift() is None


def test_a_capped_drift_keeps_its_total_beyond_the_examples(monkeypatch: pytest.MonkeyPatch) -> None:
    body = {**_DRIFT, "drifted": 5, "checked": 9, "limit": 1, "capped": True}
    c, _calls = _client(monkeypatch, flags=_BOTH, get=body)
    got = c.next_seq_drift(limit=1)
    assert got["drifted"] == 5 and got["capped"] is True and len(got["examples"]) == 1
