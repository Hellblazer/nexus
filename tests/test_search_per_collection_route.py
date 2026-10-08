# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-tu8wp.2: the client half of ``POST /v1/vectors/search-per-collection``.

Every test drives the REAL ``HttpVectorClient`` and ``search_cross_corpus``
through a fake transport (the module-level ``_request`` every ``_post``
funnels through), so the request the client builds and the envelope it parses
are the production code. The fake engine implements the route's documented
semantics (per-collection top-K, thresholds, global cut, ``per_collection``
stats, ``error_kind``) and the old flat ``/search`` for the fallback path.
The same flows against the real engine jar are in
``tests/test_search_per_collection_journey.py``.
"""
from __future__ import annotations

import io
import json
import threading
import time
import urllib.error

import pytest

import nexus.db.http_vector_client as hvc
import nexus.search_engine as se
from nexus.db.http_vector_client import PerCollectionEnvelopeError, VectorServiceError
from nexus.errors import SearchEmbeddingProfileMismatchError
from nexus.search_engine import SearchDiagnostics, search_cross_corpus

_ROUTE = "/v1/vectors/search-per-collection"
_SEARCH = "/v1/vectors/search"
_HYBRID = "/v1/vectors/hybrid-search"

#: Two model groups; ``bge`` mixes code (x2) and docs (x4) multipliers, as
#: local bge-768 does.
_BGE = "bge-base-en-v15-768"
_MINI = "minilm-l6-v2-384"


def _cols(kind: str, model: str, n: int) -> list[str]:
    return [f"{kind}__c{i}__{model}__v1" for i in range(n)]


def _http_error(
    path: str, code: int, body: dict, headers: dict | None = None,
) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        path, code, "err", headers or {},  # type: ignore[arg-type]
        io.BytesIO(json.dumps(body).encode()),
    )


#: What the AWS edge adds to a response it generated itself.
_EDGE_HEADERS = {"Server": "awselb/2.0"}


@pytest.fixture(autouse=True)
def _fresh_process_state(monkeypatch):
    """The route's process-wide state: the mixed-model group memo, the
    "route absent already logged" flag and the kill-switch env. Each test
    starts as a fresh process would."""
    monkeypatch.setattr(se, "_mixed_model_groups", {})
    monkeypatch.setattr(hvc, "_per_collection_absent_warned", False)
    monkeypatch.delenv(hvc.PER_COLLECTION_ROUTE_ENV, raising=False)


class _FakeEngine:
    """A transport double for ``/v1/vectors/search-per-collection`` (served or
    absent), the flat ``/search`` and ``/hybrid-search``.

    ``data`` maps collection -> ``[(id, distance), ...]``.
    """

    def __init__(self, monkeypatch, data: dict[str, list[tuple[str, float]]], *,
                 route: bool = True) -> None:
        self.data = {c: sorted(rows, key=lambda r: (r[1], r[0])) for c, rows in data.items()}
        self.route = route
        self.calls: list[tuple[str, dict]] = []
        #: collection -> (error_kind, text) reported in its per_collection entry
        self.errors: dict[str, tuple[str | None, str]] = {}
        #: names the engine drops with X-Nexus-Skipped-Collections
        self.skipped: set[str] = set()
        #: edit the 200 envelope before it is returned (malformed-envelope tests)
        self.mutate = None
        #: raise this HTTP error for a route request:
        #: ``fail_when(body) -> (code, body[, headers]) | None``
        self.fail_when = None
        #: called with the request body at the start of every route request
        #: (concurrency tests block in it); may sleep or wait on a barrier
        self.on_route = None
        #: lexical rows /hybrid-search returns: list of row dicts
        self.lexical_rows: list[dict] = []
        self.client = hvc.HttpVectorClient()
        monkeypatch.setattr(hvc, "_request", self._request)
        monkeypatch.setattr(
            "nexus.search_engine.load_config",
            lambda: {"search": {"contradiction_check": False}},
        )

    def route_calls(self) -> list[dict]:
        return [b for p, b in self.calls if p == _ROUTE]

    def paths(self) -> list[str]:
        return [p for p, _ in self.calls]

    @staticmethod
    def _row(col: str, rid: str, dist: float) -> dict:
        return {"id": rid, "content": f"text {rid}", "distance": dist, "collection": col}

    def _request(self, method, path, *, tenant, timeout, body):
        self.calls.append((path, body))
        if path == _ROUTE:
            if self.on_route is not None:
                self.on_route(body)
            if not self.route:
                raise _http_error(path, 404, {"error": "not found"})
            if self.fail_when is not None:
                failure = self.fail_when(body)
                if failure is not None:
                    raise _http_error(path, *failure)
            return self._serve_route(body)
        if path == _SEARCH:
            return self._serve_flat(body)
        if path == _HYBRID:
            return list(self.lexical_rows)
        raise AssertionError(f"unexpected request {method} {path}")

    def _serve_route(self, body: dict) -> dict:
        k, limit = body["per_collection_k"], body["limit"]
        thresholds = body.get("thresholds", {})
        survivors: list[dict] = []
        stats = []
        skipped = [c for c in body["collections"] if c in self.skipped]
        for col in body["collections"]:
            if col in self.skipped:
                continue
            if col in self.errors:
                kind, text = self.errors[col]
                stats.append({"collection": col, "raw_count": 0, "dropped": 0,
                              "min_raw_distance": None, "min_dropped_distance": None,
                              "error": text, "error_kind": kind})
                continue
            raw = self.data.get(col, [])[:k]
            thr = thresholds.get(col)
            kept = [r for r in raw if thr is None or r[1] <= thr]
            dropped = [r for r in raw if thr is not None and r[1] > thr]
            survivors.extend(self._row(col, rid, d) for rid, d in kept)
            stats.append({
                "collection": col, "raw_count": len(raw), "dropped": len(dropped),
                "min_raw_distance": raw[0][1] if raw else None,
                "min_dropped_distance": dropped[0][1] if dropped else None,
                "error": None, "error_kind": None,
            })
        survivors.sort(key=lambda r: (r["distance"], r["id"], r["collection"]))
        envelope = {"results": survivors[:limit], "per_collection": stats,
                    "per_collection_k": k, "limit": limit}
        if self.mutate is not None:
            envelope = self.mutate(envelope)
        hvc._response_header_capture.skipped_collections = (
            ",".join(skipped) if skipped else None
        )
        return envelope

    def _serve_flat(self, body: dict) -> list[dict]:
        merged = [
            self._row(c, rid, d)
            for c in body["collections"] for rid, d in self.data.get(c, [])
        ]
        merged.sort(key=lambda r: (r["distance"], r["id"]))
        return merged[: body["n_results"]]


def _rows(prefix: str, n: int, base: float, step: float = 0.001) -> list[tuple[str, float]]:
    return [(f"{prefix}{i}", base + i * step) for i in range(n)]


def _search(engine: _FakeEngine, cols: list[str], n: int = 5, **kw):
    kw.setdefault("threshold_override", float("inf"))
    kw.setdefault("cluster_by", None)
    return search_cross_corpus("q", cols, n, engine.client, **kw)


# ── the flow: one request per model group ────────────────────────────────────


class TestOneRequestPerModelGroup:
    def test_each_model_group_is_one_route_request_and_no_flat_search(self, monkeypatch):
        bge = _cols("code", _BGE, 3) + _cols("docs", _BGE, 2)
        mini = _cols("docs", _MINI, 4)
        engine = _FakeEngine(monkeypatch, {c: _rows(c[:8], 6, 0.2) for c in bge + mini})

        results = _search(engine, bge + mini, n=5)

        assert engine.paths().count(_ROUTE) == 2
        assert _SEARCH not in engine.paths()
        sent = {tuple(b["collections"]) for b in engine.route_calls()}
        assert sent == {tuple(bge), tuple(mini)}
        # Every collection contributed its per-collection top-K.
        assert {r.collection for r in results} == set(bge + mini)

    def test_per_collection_k_is_the_prebatching_count_with_the_groups_max_multiplier(
        self, monkeypatch,
    ):
        # One bge group mixing code (x2) and docs (x4): n=10 -> max(5, 10*4).
        cols = _cols("code", _BGE, 2) + _cols("docs", _BGE, 2)
        engine = _FakeEngine(monkeypatch, {c: _rows("r", 3, 0.2) for c in cols})
        _search(engine, cols, n=10)
        assert engine.route_calls()[0]["per_collection_k"] == 40
        # A code-only group keeps its own multiplier (x2): 10*2.
        engine2 = _FakeEngine(monkeypatch, {c: _rows("r", 3, 0.2) for c in _cols("code", _BGE, 2)})
        _search(engine2, _cols("code", _BGE, 2), n=10)
        assert engine2.route_calls()[0]["per_collection_k"] == 20

    @pytest.mark.parametrize("n, rerank, expected_limit", [
        (10, False, 300),     # max(300, 4n)
        (100, False, 400),
        (300, False, 1200),   # 4n = 1200: the route's ceiling
        (500, False, 1200),   # 4n = 2000 would be a 400: clamped
        (300, True, 1000),    # the route 400s on rerank with limit > 1000
        (500, True, 1000),    # 4n = 2000: still the rerank maximum
        # Rerank asks for the deepest pool the route allows, whatever n is:
        # the reranked page is pool-sensitive (nexus-abdp2).
        (100, True, 1000),
        (10, True, 1000),
    ])
    def test_limit_is_the_pool_cap_without_rerank_and_the_rerank_maximum_with_it(
        self, monkeypatch, n, rerank, expected_limit,
    ):
        cols = _cols("code", _BGE, 2)
        engine = _FakeEngine(monkeypatch, {c: _rows("r", 3, 0.2) for c in cols})

        # A rerank-capable backend: the fake route returns the plain envelope
        # plus the degrade flag a real rerank envelope carries.
        def with_rerank_flags(env):
            if rerank:
                env = {**env, "rerank_degraded": False, "rerank_model": "fake"}
            return env

        engine.mutate = with_rerank_flags
        _search(engine, cols, n=n, rerank=rerank)

        body = engine.route_calls()[0]
        assert body["limit"] == expected_limit
        assert body.get("rerank", False) is rerank

    def test_a_group_past_the_collection_cap_is_sent_as_several_requests(self, monkeypatch):
        cols = _cols("code", _BGE, 300)
        engine = _FakeEngine(monkeypatch, {c: _rows("r", 1, 0.2) for c in cols})
        results = _search(engine, cols, n=5)
        sizes = sorted(len(b["collections"]) for b in engine.route_calls())
        assert sizes == [44, 256]
        assert {r.collection for r in results} == set(cols)

    def test_the_requests_of_a_split_group_run_in_parallel(self, monkeypatch):
        cols = _cols("code", _BGE, 300)
        engine = _FakeEngine(monkeypatch, {c: _rows("r", 1, 0.2) for c in cols})
        # Confirm the route first so the probe lock is out of the picture.
        engine.client.search_per_collection("q", cols[:2], per_collection_k=5, limit=300)
        barrier = threading.Barrier(2)
        engine.on_route = lambda body: barrier.wait(timeout=5)  # serial -> BrokenBarrierError
        results = _search(engine, cols, n=5)
        assert {r.collection for r in results} == set(cols)


# ── thresholds ───────────────────────────────────────────────────────────────


class TestThresholds:
    def test_a_non_finite_override_sends_no_thresholds_key(self, monkeypatch):
        # threshold_override=inf is what --no-threshold and the parity gate
        # use; Infinity is not valid JSON, the engine's Jackson rejects it.
        cols = _cols("code", _BGE, 2)
        engine = _FakeEngine(monkeypatch, {c: _rows("r", 3, 0.9) for c in cols})
        results = _search(engine, cols, threshold_override=float("inf"))
        assert "thresholds" not in engine.route_calls()[0]
        assert len(results) == 6

    def test_a_finite_override_is_sent_for_every_collection_and_filters(self, monkeypatch):
        cols = _cols("code", _BGE, 2)
        data = {cols[0]: [("near", 0.1), ("far", 0.9)], cols[1]: [("mid", 0.3)]}
        engine = _FakeEngine(monkeypatch, data)
        diags: list[SearchDiagnostics] = []
        results = _search(engine, cols, threshold_override=0.5, diagnostics_out=diags)
        assert engine.route_calls()[0]["thresholds"] == {cols[0]: 0.5, cols[1]: 0.5}
        assert {r.id for r in results} == {"near", "mid"}
        # The engine's per_collection stats feed the diagnostics unchanged.
        raw, dropped, thr, min_dropped = diags[0].per_collection[cols[0]]
        assert (raw, dropped, thr, min_dropped) == (2, 1, 0.5, 0.9)

    def test_client_drops_none_nonfinite_and_unrequested_threshold_keys(self, monkeypatch):
        cols = _cols("code", _BGE, 2)
        engine = _FakeEngine(monkeypatch, {c: _rows("r", 1, 0.2) for c in cols})
        engine.client.search_per_collection(
            "q", cols, per_collection_k=5, limit=300,
            thresholds={cols[0]: float("nan"), cols[1]: None, "other": 0.3},
        )
        assert "thresholds" not in engine.route_calls()[0]
        engine.client.search_per_collection(
            "q", cols, per_collection_k=5, limit=300,
            thresholds={cols[0]: 0.4, cols[1]: float("inf"), "other": 0.3},
        )
        assert engine.route_calls()[1]["thresholds"] == {cols[0]: 0.4}


# ── crowd-out ────────────────────────────────────────────────────────────────


class TestCrowdOut:
    def test_a_dominant_collection_does_not_starve_a_small_one(self, monkeypatch):
        big, small = _cols("code", _BGE, 1)[0], "code__small__%s__v1" % _BGE
        # 60 near rows against 5 farther ones, n=4: the lean floor's flat call
        # asks for max(8, 2*5)=10 rows and A takes all of them.
        data = {big: _rows("a", 60, 0.10), small: _rows("b", 5, 0.50)}

        batched = _FakeEngine(monkeypatch, data, route=False)
        starved = _search(batched, [big, small], n=4)
        assert not [r for r in starved if r.collection == small]  # the old defect

        served = _FakeEngine(monkeypatch, data, route=True)
        fixed = _search(served, [big, small], n=4)
        assert len([r for r in fixed if r.collection == small]) == 5


# ── fallback: the engine without the route ───────────────────────────────────


class TestFallbackOnAnOldEngine:
    def test_404_uses_the_batched_path_with_the_same_results_as_before(self, monkeypatch):
        cols = _cols("code", _BGE, 3)
        data = {c: _rows(c[:7], 4, 0.2) for c in cols}
        engine = _FakeEngine(monkeypatch, data, route=False)

        results = _search(engine, cols, n=5)

        assert engine.paths().count(_ROUTE) == 1  # the probe
        assert _SEARCH in engine.paths()          # the unchanged batched call
        assert len(results) == 12

    def test_the_404_is_remembered_for_600_seconds(self, monkeypatch):
        cols = _cols("code", _BGE, 2)
        engine = _FakeEngine(monkeypatch, {c: _rows("r", 2, 0.2) for c in cols}, route=False)
        clock = [1000.0]
        monkeypatch.setattr(hvc, "_monotonic", lambda: clock[0])

        _search(engine, cols)
        _search(engine, cols)
        assert engine.paths().count(_ROUTE) == 1, "second search must not re-probe"

        clock[0] += 599.0
        _search(engine, cols)
        assert engine.paths().count(_ROUTE) == 1

        clock[0] += 2.0  # 601 s after the miss: probed again
        _search(engine, cols)
        assert engine.paths().count(_ROUTE) == 2

    def test_a_route_that_appears_after_the_memo_expires_is_used(self, monkeypatch):
        cols = _cols("code", _BGE, 2)
        engine = _FakeEngine(monkeypatch, {c: _rows("r", 2, 0.2) for c in cols}, route=False)
        clock = [0.0]
        monkeypatch.setattr(hvc, "_monotonic", lambda: clock[0])
        _search(engine, cols)
        engine.route = True  # the engine was upgraded
        clock[0] += 601.0
        flat_before = engine.paths().count(_SEARCH)
        _search(engine, cols)
        assert engine.paths().count(_SEARCH) == flat_before  # served by the route now

    def test_a_backend_without_the_capability_marker_never_asks(self, monkeypatch):
        cols = _cols("code", _BGE, 2)
        engine = _FakeEngine(monkeypatch, {c: _rows("r", 2, 0.2) for c in cols})
        engine.client.supports_per_collection_search = False  # type: ignore[misc]
        _search(engine, cols)
        assert _ROUTE not in engine.paths()
        assert _SEARCH in engine.paths()

    def test_with_two_model_groups_both_fall_back(self, monkeypatch):
        bge, mini = _cols("code", _BGE, 2), _cols("docs", _MINI, 2)
        engine = _FakeEngine(monkeypatch, {c: _rows("r", 2, 0.2) for c in bge + mini}, route=False)
        results = _search(engine, bge + mini)
        assert engine.paths().count(_SEARCH) == 2
        assert {r.collection for r in results} == set(bge + mini)


# ── a route failure falls back, or fails the group, by status ────────────────

#: status, response headers, the memo window the client keeps. Every row here
#: is "the engine or edge does not serve the route": the batched path serves
#: the group and the route is skipped for the window.
_FALLBACK_STATUSES = [
    pytest.param(404, None, 600.0, id="404-engine-without-the-route"),
    pytest.param(403, None, 600.0, id="403-forbidden"),
    pytest.param(403, _EDGE_HEADERS, 600.0, id="403-edge-refusal"),
    pytest.param(400, _EDGE_HEADERS, 600.0, id="400-edge-refusal"),
    pytest.param(405, None, 600.0, id="405-method-not-allowed"),
    pytest.param(501, None, 600.0, id="501-not-implemented"),
    pytest.param(500, None, 60.0, id="500-a-route-bug"),
]

#: Failures of the GROUP: load shedding (the gateway already retried 502-504)
#: and validation errors. The batched path is not tried, the route is not
#: written off.
_GROUP_FAILURE_STATUSES = [
    pytest.param(429, None, id="429"),
    pytest.param(502, None, id="502"),
    pytest.param(503, None, id="503"),
    pytest.param(504, None, id="504"),
    pytest.param(503, _EDGE_HEADERS, id="503-edge-generated"),
    pytest.param(422, None, id="422"),
    pytest.param(400, None, id="400-validation"),
]


class TestRouteFailureFallsBackByStatus:
    @pytest.mark.parametrize("code, headers, window", _FALLBACK_STATUSES)
    def test_the_group_is_served_by_the_batched_path_and_the_route_is_remembered_off(
        self, monkeypatch, code, headers, window,
    ):
        cols = _cols("code", _BGE, 3)
        engine = _FakeEngine(monkeypatch, {c: _rows(c[:7], 4, 0.2) for c in cols})
        engine.fail_when = lambda body: (code, {"error": "nope"}, headers)
        clock = [0.0]
        monkeypatch.setattr(hvc, "_monotonic", lambda: clock[0])
        warnings = []
        monkeypatch.setattr(hvc._log, "warning", lambda event, **kw: warnings.append((event, kw)))

        results = _search(engine, cols, n=5)

        assert len(results) == 12
        assert engine.paths().count(_ROUTE) == 1
        assert _SEARCH in engine.paths()
        # A WARNING that names the status.
        assert [kw["status"] for e, kw in warnings if "route_unavailable" in e] == [code]
        # Remembered for exactly the window.
        _search(engine, cols)
        assert engine.paths().count(_ROUTE) == 1, "the memo must skip the route"
        clock[0] += window - 1.0
        _search(engine, cols)
        assert engine.paths().count(_ROUTE) == 1
        clock[0] += 2.0
        _search(engine, cols)
        assert engine.paths().count(_ROUTE) == 2

    @pytest.mark.parametrize("code, headers", _GROUP_FAILURE_STATUSES)
    def test_load_shedding_and_validation_errors_fail_the_group_without_a_fallback(
        self, monkeypatch, code, headers,
    ):
        cols = _cols("code", _BGE, 3)
        engine = _FakeEngine(monkeypatch, {c: _rows(c[:7], 4, 0.2) for c in cols})
        engine.fail_when = lambda body: (code, {"error": "busy"}, headers)
        with pytest.raises(VectorServiceError, match="all 3 collections failed"):
            _search(engine, cols)
        assert _SEARCH not in engine.paths(), "no fallback: the batched path would hit the same pool"
        # Not written off: the next search asks again.
        with pytest.raises(VectorServiceError):
            _search(engine, cols)
        assert engine.paths().count(_ROUTE) == 2

    def test_a_route_bug_in_one_group_still_returns_every_collection_of_both(self, monkeypatch):
        # The write-off is per client, so whether the second group is served by
        # the route (it ran first) or by the batched path (it ran after the
        # write-off) depends on scheduling; either way nothing is lost.
        bge, mini = _cols("code", _BGE, 2), _cols("docs", _MINI, 2)
        engine = _FakeEngine(monkeypatch, {c: _rows(c[:7], 3, 0.2) for c in bge + mini})
        engine.fail_when = lambda body: (
            (500, {"error": "boom"}, None) if body["collections"][0] in bge else None
        )
        diags: list[SearchDiagnostics] = []
        results = _search(engine, bge + mini, diagnostics_out=diags)
        assert {r.collection for r in results} == set(bge + mini)
        assert diags[0].failed_collections == {}

    def test_the_first_route_missing_fallback_per_process_is_a_warning_the_rest_are_debug(
        self, monkeypatch,
    ):
        cols = _cols("code", _BGE, 2)
        engine = _FakeEngine(monkeypatch, {c: _rows("r", 2, 0.2) for c in cols}, route=False)
        clock = [0.0]
        monkeypatch.setattr(hvc, "_monotonic", lambda: clock[0])
        warnings, debugs = [], []
        monkeypatch.setattr(hvc._log, "warning", lambda event, **kw: warnings.append((event, kw)))
        monkeypatch.setattr(hvc._log, "debug", lambda event, **kw: debugs.append((event, kw)))

        _search(engine, cols)
        assert len(warnings) == 1 and warnings[0][1]["status"] == 404
        assert not [e for e, _ in debugs if "route_absent" in e]

        clock[0] += 601.0  # the memo expires, the next probe misses again
        _search(engine, cols)
        assert len(warnings) == 1, "the 404 is already on record for this process"
        assert [kw["status"] for e, kw in debugs if "route_absent" in e] == [404]


# ── kill switch ──────────────────────────────────────────────────────────────


class TestKillSwitch:
    @pytest.mark.parametrize("value", ["0", "false", "FALSE", "off", "Off", "no", " 0 "])
    def test_the_env_switch_turns_the_route_off_and_the_batched_path_serves(
        self, monkeypatch, value,
    ):
        cols = _cols("code", _BGE, 2)
        engine = _FakeEngine(monkeypatch, {c: _rows("r", 3, 0.2) for c in cols})
        monkeypatch.setenv(hvc.PER_COLLECTION_ROUTE_ENV, value)
        results = _search(engine, cols)
        assert _ROUTE not in engine.paths()
        assert _SEARCH in engine.paths()
        assert len(results) == 6

    @pytest.mark.parametrize("value", ["", "1", "true", "on", "yes", "anything"])
    def test_any_other_value_leaves_the_route_on(self, monkeypatch, value):
        cols = _cols("code", _BGE, 2)
        engine = _FakeEngine(monkeypatch, {c: _rows("r", 3, 0.2) for c in cols})
        monkeypatch.setenv(hvc.PER_COLLECTION_ROUTE_ENV, value)
        _search(engine, cols)
        assert _ROUTE in engine.paths()
        assert _SEARCH not in engine.paths()

    def test_the_switch_is_read_on_every_search(self, monkeypatch):
        cols = _cols("code", _BGE, 2)
        engine = _FakeEngine(monkeypatch, {c: _rows("r", 3, 0.2) for c in cols})
        _search(engine, cols)
        assert engine.paths().count(_ROUTE) == 1
        monkeypatch.setenv(hvc.PER_COLLECTION_ROUTE_ENV, "0")
        _search(engine, cols)
        assert engine.paths().count(_ROUTE) == 1
        monkeypatch.delenv(hvc.PER_COLLECTION_ROUTE_ENV)
        _search(engine, cols)
        assert engine.paths().count(_ROUTE) == 2


# ── the first probe is single-flight ─────────────────────────────────────────


class TestFirstProbeIsSingleFlight:
    def test_two_model_groups_of_one_search_probe_an_absent_route_once(self, monkeypatch):
        bge, mini = _cols("code", _BGE, 2), _cols("docs", _MINI, 2)
        engine = _FakeEngine(monkeypatch, {c: _rows("r", 2, 0.2) for c in bge + mini}, route=False)
        engine.on_route = lambda body: time.sleep(0.1)  # let the second group arrive
        results = _search(engine, bge + mini)
        assert engine.paths().count(_ROUTE) == 1
        assert {r.collection for r in results} == set(bge + mini)

    def test_concurrent_callers_share_one_probe(self, monkeypatch):
        cols = _cols("code", _BGE, 2)
        engine = _FakeEngine(monkeypatch, {c: _rows("r", 2, 0.2) for c in cols}, route=False)
        engine.on_route = lambda body: time.sleep(0.1)
        barrier = threading.Barrier(6)
        out: list = []

        def go():
            barrier.wait(timeout=5)
            out.append(engine.client.search_per_collection(
                "q", cols, per_collection_k=5, limit=300))

        threads = [threading.Thread(target=go) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        assert out == [None] * 6
        assert engine.paths().count(_ROUTE) == 1

    def test_once_the_route_is_confirmed_requests_run_in_parallel(self, monkeypatch):
        cols = _cols("code", _BGE, 2)
        engine = _FakeEngine(monkeypatch, {c: _rows("r", 2, 0.2) for c in cols})
        engine.client.search_per_collection("q", cols, per_collection_k=5, limit=300)
        barrier = threading.Barrier(4)
        engine.on_route = lambda body: barrier.wait(timeout=5)  # BrokenBarrierError if serialised
        results: list = []
        errors: list = []

        def go():
            try:
                results.append(engine.client.search_per_collection(
                    "q", cols, per_collection_k=5, limit=300))
            except Exception as exc:  # noqa: BLE001 - the assertion below names it
                errors.append(exc)

        threads = [threading.Thread(target=go) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        assert not errors, errors
        assert len(results) == 4 and all(r is not None for r in results)


# ── a malformed envelope is refused ──────────────────────────────────────────


def _good(env):
    return env


_MALFORMED = {
    "non_object_body": lambda env: ["not", "an", "object"],
    "missing_per_collection_k_echo": lambda env: {
        k: v for k, v in env.items() if k != "per_collection_k"},
    "wrong_per_collection_k_echo": lambda env: {**env, "per_collection_k": 7},
    "missing_limit_echo": lambda env: {k: v for k, v in env.items() if k != "limit"},
    "wrong_limit_echo": lambda env: {**env, "limit": 12},
    "results_not_a_list": lambda env: {**env, "results": {}},
    "per_collection_not_a_list": lambda env: {**env, "per_collection": "x"},
    "an_entry_without_a_name": lambda env: {**env, "per_collection": [{"raw_count": 1}]},
    "an_unrequested_collection": lambda env: {
        **env, "per_collection": env["per_collection"] + [{"collection": "ghost"}]},
    "a_requested_collection_unaccounted_for": lambda env: {
        **env, "per_collection": env["per_collection"][:-1]},
    # Row and count shapes (nexus-tu8wp.2 fix round): each used to escape as a
    # bare ValueError / KeyError / AttributeError past the group.
    "a_row_that_is_not_an_object": lambda env: {**env, "results": ["a string row"]},
    "a_row_without_an_id": lambda env: {
        **env, "results": [{k: v for k, v in env["results"][0].items() if k != "id"}]},
    "a_row_without_a_distance": lambda env: {
        **env, "results": [{k: v for k, v in env["results"][0].items() if k != "distance"}]},
    "a_row_with_a_non_numeric_distance": lambda env: {
        **env, "results": [{**env["results"][0], "distance": "near"}]},
    "a_row_with_a_boolean_distance": lambda env: {
        **env, "results": [{**env["results"][0], "distance": True}]},
    "a_row_with_a_non_string_collection": lambda env: {
        **env, "results": [{**env["results"][0], "collection": ["x"]}]},
    "a_non_numeric_raw_count": lambda env: {
        **env, "per_collection": [{**env["per_collection"][0], "raw_count": "many"},
                                  *env["per_collection"][1:]]},
    "a_non_integral_dropped_count": lambda env: {
        **env, "per_collection": [{**env["per_collection"][0], "dropped": 1.5},
                                  *env["per_collection"][1:]]},
    "a_negative_count": lambda env: {
        **env, "per_collection": [{**env["per_collection"][0], "raw_count": -1},
                                  *env["per_collection"][1:]]},
    "a_list_as_a_count": lambda env: {
        **env, "per_collection": [{**env["per_collection"][0], "dropped": [1]},
                                  *env["per_collection"][1:]]},
    "a_non_numeric_min_distance": lambda env: {
        **env, "per_collection": [{**env["per_collection"][0], "min_raw_distance": "0.1"},
                                  *env["per_collection"][1:]]},
}


class TestMalformedEnvelopeIsRefused:
    @pytest.mark.parametrize("name", sorted(_MALFORMED))
    def test_the_client_raises_and_never_returns_a_partial_envelope(self, monkeypatch, name):
        cols = _cols("code", _BGE, 2)
        engine = _FakeEngine(monkeypatch, {c: _rows("r", 2, 0.2) for c in cols})
        engine.mutate = _MALFORMED[name]
        with pytest.raises(PerCollectionEnvelopeError):
            engine.client.search_per_collection("q", cols, per_collection_k=5, limit=300)

    def test_a_refusal_switches_the_route_off_for_60_seconds(self, monkeypatch):
        cols = _cols("code", _BGE, 2)
        engine = _FakeEngine(monkeypatch, {c: _rows("r", 2, 0.2) for c in cols})
        engine.mutate = _MALFORMED["wrong_limit_echo"]
        clock = [0.0]
        monkeypatch.setattr(hvc, "_monotonic", lambda: clock[0])
        with pytest.raises(PerCollectionEnvelopeError):
            engine.client.search_per_collection("q", cols, per_collection_k=5, limit=300)
        n_calls = len(engine.calls)
        assert engine.client.search_per_collection(
            "q", cols, per_collection_k=5, limit=300) is None
        assert len(engine.calls) == n_calls
        clock[0] += 61.0
        with pytest.raises(PerCollectionEnvelopeError):
            engine.client.search_per_collection("q", cols, per_collection_k=5, limit=300)
        assert len(engine.calls) == n_calls + 1

    def test_the_search_falls_back_to_the_batched_path_instead_of_failing(self, monkeypatch):
        cols = _cols("code", _BGE, 2)
        engine = _FakeEngine(monkeypatch, {c: _rows("r", 2, 0.2) for c in cols})
        engine.mutate = _MALFORMED["missing_per_collection_k_echo"]
        results = _search(engine, cols)
        assert _SEARCH in engine.paths()
        assert len(results) == 4

    @pytest.mark.parametrize("name", sorted(_MALFORMED))
    def test_no_malformed_shape_escapes_search_cross_corpus_as_a_raw_exception(
        self, monkeypatch, name,
    ):
        # The envelope is refused INSIDE the client, so the group falls back and
        # the route is switched off: a ValueError/KeyError/AttributeError here
        # would abort the whole search instead.
        cols = _cols("code", _BGE, 2)
        engine = _FakeEngine(monkeypatch, {c: _rows("r", 2, 0.2) for c in cols})
        engine.mutate = _MALFORMED[name]
        results = _search(engine, cols)
        assert _SEARCH in engine.paths()
        assert len(results) == 4
        _search(engine, cols)
        assert engine.paths().count(_ROUTE) == 1, "the refusal is memoized"

    def test_the_skipped_header_accounts_for_a_collection_with_no_entry(self, monkeypatch):
        cols = _cols("code", _BGE, 3)
        engine = _FakeEngine(monkeypatch, {c: _rows("r", 2, 0.2) for c in cols})
        engine.skipped = {cols[2]}  # no per_collection entry for it
        diags: list[SearchDiagnostics] = []
        results = _search(engine, cols, diagnostics_out=diags)
        assert _SEARCH not in engine.paths(), "an accounted-for skip is not a malformed envelope"
        assert {r.collection for r in results} == set(cols[:2])
        # As on the batched path, a skipped collection reads as "no rows".
        assert diags[0].per_collection[cols[2]][:2] == (0, 0)
        assert diags[0].failed_collections == {}

    def test_the_header_is_logged_through_the_shared_warning(self, monkeypatch):
        cols = _cols("code", _BGE, 2)
        engine = _FakeEngine(monkeypatch, {c: _rows("r", 2, 0.2) for c in cols})
        engine.skipped = {cols[1]}
        events = []
        monkeypatch.setattr(hvc._log, "warning", lambda event, **kw: events.append((event, kw)))
        engine.client.search_per_collection("q", cols, per_collection_k=5, limit=300)
        assert events and events[0][0] == "vector_read_skipped_unregistered_collections"
        assert events[0][1]["skipped"] == [cols[1]]
        assert events[0][1]["route"] == "search_per_collection"


# ── error_kind mapping ───────────────────────────────────────────────────────


class TestErrorKindMapping:
    def test_statement_timeout_fails_the_collection_not_the_search(self, monkeypatch):
        cols = _cols("code", _BGE, 3)
        engine = _FakeEngine(monkeypatch, {c: _rows(c[:7], 3, 0.2) for c in cols})
        engine.errors[cols[1]] = ("statement_timeout", "canceling statement due to statement timeout")
        diags: list[SearchDiagnostics] = []
        results = _search(engine, cols, diagnostics_out=diags)
        assert {r.collection for r in results} == {cols[0], cols[2]}
        assert diags[0].failed_collections == {
            cols[1]: "canceling statement due to statement timeout"}

    def test_fanout_budget_exhausted_fails_the_collection_not_the_search(self, monkeypatch):
        cols = _cols("code", _BGE, 3)
        engine = _FakeEngine(monkeypatch, {c: _rows(c[:7], 3, 0.2) for c in cols})
        engine.errors[cols[0]] = ("fanout_budget_exhausted", "fan-out budget of 20000 ms spent")
        engine.errors[cols[2]] = ("fanout_budget_exhausted", "fan-out budget of 20000 ms spent")
        diags: list[SearchDiagnostics] = []
        results = _search(engine, cols, diagnostics_out=diags)
        assert {r.collection for r in results} == {cols[1]}
        assert set(diags[0].failed_collections) == {cols[0], cols[2]}

    def test_every_collection_timing_out_raises_the_all_failed_error(self, monkeypatch):
        cols = _cols("code", _BGE, 2)
        engine = _FakeEngine(monkeypatch, {c: _rows("r", 3, 0.2) for c in cols})
        for c in cols:
            engine.errors[c] = ("statement_timeout", "timed out")
        with pytest.raises(VectorServiceError, match="all 2 collections failed"):
            _search(engine, cols)

    def test_a_dimension_mismatch_in_one_collection_is_a_partial_failure(self, monkeypatch):
        cols = _cols("code", _BGE, 3)
        engine = _FakeEngine(monkeypatch, {c: _rows(c[:7], 3, 0.2) for c in cols})
        # Deliberately a text with no "dim" in it: the kind alone must classify.
        engine.errors[cols[0]] = ("dimension_mismatch", "embedder width disagrees")
        warnings = []
        monkeypatch.setattr(se._log, "warning", lambda event, **kw: warnings.append((event, kw)))
        diags: list[SearchDiagnostics] = []
        results = _search(engine, cols, diagnostics_out=diags)
        assert {r.collection for r in results} == {cols[1], cols[2]}
        assert cols[0] in diags[0].failed_collections
        assert any(e == "collection_search_failed" and kw["collection"] == cols[0]
                   for e, kw in warnings)

    @pytest.mark.parametrize("kind", ["dimension_mismatch", "unsupported_dimension"])
    def test_a_dimension_class_failure_of_the_whole_scope_raises_the_profile_mismatch(
        self, monkeypatch, kind,
    ):
        # vply6 fix round 2: the stale-orphan class consuming the WHOLE
        # requested scope is a loud refusal, not an empty result.
        cols = _cols("code", _BGE, 2)
        engine = _FakeEngine(monkeypatch, {c: _rows("r", 3, 0.2) for c in cols})
        for c in cols:
            engine.errors[c] = (kind, "width disagrees")
        with pytest.raises(SearchEmbeddingProfileMismatchError):
            _search(engine, cols)

    def test_a_timeout_is_not_counted_as_the_dimension_class(self, monkeypatch):
        cols = _cols("code", _BGE, 2)
        engine = _FakeEngine(monkeypatch, {c: _rows("r", 3, 0.2) for c in cols})
        # Text that contains "dim": the kind wins, so this is NOT the
        # embedding-profile mismatch.
        for c in cols:
            engine.errors[c] = ("statement_timeout", "slow dimension scan")
        with pytest.raises(VectorServiceError, match="all 2 collections failed"):
            _search(engine, cols)

    def test_an_unknown_error_kind_is_a_failed_collection(self, monkeypatch):
        cols = _cols("code", _BGE, 2)
        engine = _FakeEngine(monkeypatch, {c: _rows(c[:7], 3, 0.2) for c in cols})
        engine.errors[cols[0]] = ("a_kind_from_the_future", "something new")
        diags: list[SearchDiagnostics] = []
        results = _search(engine, cols, diagnostics_out=diags)
        assert {r.collection for r in results} == {cols[1]}
        assert diags[0].failed_collections == {cols[0]: "something new"}

    def test_a_whole_request_failure_marks_the_group_failed_with_the_engines_text(
        self, monkeypatch,
    ):
        bge, mini = _cols("code", _BGE, 2), _cols("docs", _MINI, 2)
        engine = _FakeEngine(monkeypatch, {c: _rows(c[:7], 3, 0.2) for c in bge + mini})
        engine.fail_when = lambda body: (
            (422, {"error": "model unavailable"}) if body["collections"][0] in bge else None
        )
        diags: list[SearchDiagnostics] = []
        results = _search(engine, bge + mini, diagnostics_out=diags)
        assert {r.collection for r in results} == set(mini)
        assert set(diags[0].failed_collections) == set(bge)
        assert "model unavailable" in diags[0].failed_collections[bge[0]]


# ── mixed-model 400 ──────────────────────────────────────────────────────────


class TestMixedModelFallsBackToSingletons:
    def test_a_mixed_model_400_retries_one_request_per_collection(self, monkeypatch):
        cols = _cols("code", _BGE, 3)
        engine = _FakeEngine(monkeypatch, {c: _rows(c[:7], 3, 0.2) for c in cols})
        engine.fail_when = lambda body: (
            (400, {"error": "mixed embedding models in one combined-query call: 'a' vs 'b'"})
            if len(body["collections"]) > 1 else None
        )
        results = _search(engine, cols)
        sizes = [len(b["collections"]) for b in engine.route_calls()]
        assert sizes == [3, 1, 1, 1]
        assert {r.collection for r in results} == set(cols)
        assert _SEARCH not in engine.paths()

    @staticmethod
    def _mixed(engine):
        engine.fail_when = lambda body: (
            (400, {"error": "mixed embedding models in one combined-query call: 'a' vs 'b'"}, None)
            if len(body["collections"]) > 1 else None
        )

    def test_the_singleton_retries_run_in_parallel(self, monkeypatch):
        cols = _cols("code", _BGE, 6)
        engine = _FakeEngine(monkeypatch, {c: _rows(c[:7], 3, 0.2) for c in cols})
        self._mixed(engine)
        barrier = threading.Barrier(6)

        def hold_singletons(body):
            # All six singleton requests must be in flight at once; a serial
            # loop times the barrier out (BrokenBarrierError -> group failure).
            if len(body["collections"]) == 1:
                barrier.wait(timeout=5)

        engine.on_route = hold_singletons
        results = _search(engine, cols)
        assert {r.collection for r in results} == set(cols)

    def test_a_group_that_drew_the_mixed_model_400_goes_straight_to_singletons_for_600_s(
        self, monkeypatch,
    ):
        cols = _cols("code", _BGE, 3)
        engine = _FakeEngine(monkeypatch, {c: _rows(c[:7], 3, 0.2) for c in cols})
        self._mixed(engine)
        clock = [0.0]
        monkeypatch.setattr(se, "_monotonic", lambda: clock[0])

        _search(engine, cols)
        first = sorted(len(b["collections"]) for b in engine.route_calls())
        assert first == [1, 1, 1, 3]

        _search(engine, cols)  # remembered: no grouped request this time
        second = sorted(len(b["collections"]) for b in engine.route_calls()[len(first):])
        assert second == [1, 1, 1]

        clock[0] += 601.0  # expired: the grouped request is tried again
        _search(engine, cols)
        third = sorted(len(b["collections"]) for b in engine.route_calls()[len(first) + 3:])
        assert third == [1, 1, 1, 3]

    def test_a_different_set_of_collections_is_not_covered_by_the_memo(self, monkeypatch):
        cols = _cols("code", _BGE, 4)
        engine = _FakeEngine(monkeypatch, {c: _rows(c[:7], 3, 0.2) for c in cols})
        self._mixed(engine)
        _search(engine, cols[:3])
        before = len(engine.route_calls())
        _search(engine, cols[1:])  # a different group: pays its own 400
        sizes = sorted(len(b["collections"]) for b in engine.route_calls()[before:])
        assert sizes == [1, 1, 1, 3]

    def test_a_400_that_is_not_the_model_mix_fails_the_group_without_a_retry_storm(
        self, monkeypatch,
    ):
        cols = _cols("code", _BGE, 3)
        engine = _FakeEngine(monkeypatch, {c: _rows("r", 3, 0.2) for c in cols})
        engine.fail_when = lambda body: (400, {"error": "field 'where' is malformed"})
        with pytest.raises(VectorServiceError, match="all 3 collections failed"):
            _search(engine, cols)
        assert len(engine.route_calls()) == 1


# ── the rerank scores a capped candidate set (Sam, 2026-10-08) ───────────────


def _rerank_flags(env):
    return {**env, "rerank_degraded": False, "rerank_model": "fake"}


def _set_mode(monkeypatch, *, local: bool) -> None:
    """The cap's default depends on the install mode; pin it, never read the box's."""
    monkeypatch.setattr("nexus.search_engine.is_local_mode", lambda: local)


def _set_cap_config(monkeypatch, value) -> None:
    monkeypatch.setattr(
        "nexus.search_engine.load_config",
        lambda: {"search": {"contradiction_check": False, "rerank_max_candidates": value}},
    )


class TestRerankCandidateCap:
    """In LOCAL mode the default cap is ``max(3 * n_results, 60)``; in cloud mode
    there is none. ``search.rerank_max_candidates`` overrides either (0 turns the
    cap off). It rides the request as ``rerank_max_candidates`` so the ENGINE
    scores that many rows, not all."""

    @pytest.fixture(autouse=True)
    def _local_mode(self, monkeypatch):
        _set_mode(monkeypatch, local=True)

    @pytest.mark.parametrize("n, expected", [
        (1, 60), (5, 60), (10, 60), (20, 60), (21, 63), (100, 300),
    ])
    def test_default_cap_is_three_times_n_with_a_floor_of_sixty(
        self, monkeypatch, n, expected,
    ):
        cols = _cols("code", _BGE, 2)
        engine = _FakeEngine(monkeypatch, {c: _rows("r", 3, 0.2) for c in cols})
        engine.mutate = _rerank_flags
        _search(engine, cols, n=n, rerank=True)
        assert engine.route_calls()[0]["rerank_max_candidates"] == expected

    def test_no_cap_field_without_rerank(self, monkeypatch):
        cols = _cols("code", _BGE, 2)
        engine = _FakeEngine(monkeypatch, {c: _rows("r", 3, 0.2) for c in cols})
        _search(engine, cols, n=5)
        assert "rerank_max_candidates" not in engine.route_calls()[0]

    def test_the_batched_fallback_carries_the_same_cap(self, monkeypatch):
        cols = _cols("code", _BGE, 2)
        engine = _FakeEngine(monkeypatch, {c: _rows("r", 3, 0.2) for c in cols}, route=False)
        # The flat /search must answer with a rerank envelope when rerank is on.
        flat = engine._serve_flat
        engine._serve_flat = lambda body: {
            "results": flat(body), "rerank_degraded": False, "rerank_model": "fake",
        }
        _search(engine, cols, n=10, rerank=True)
        sent = [b for p, b in engine.calls if p == _SEARCH]
        assert sent and all(b["rerank_max_candidates"] == 60 for b in sent)

    def test_the_lexical_leg_is_not_capped(self, monkeypatch):
        # A lexical-only hit sits deep in vector order; capping its rerank would
        # leave it unscored and drop it from the page, which is what the leg is for.
        cols = _cols("code", _BGE, 2)
        engine = _FakeEngine(monkeypatch, {c: _rows("r", 3, 0.2) for c in cols})
        engine.mutate = _rerank_flags
        engine.lexical_rows = []
        _search(engine, cols, n=5, rerank=True, lexical=True)
        hybrid = [b for p, b in engine.calls if p == _HYBRID]
        assert hybrid and all("rerank_max_candidates" not in b for b in hybrid)

    @pytest.mark.parametrize("local, configured, expected", [
        (True, 50, 50),         # a fixed cap
        (True, 0, None),        # off: score every candidate, as before
        (True, -3, 60),         # nonsense falls back to the mode's default, loudly in the log
        (True, "many", 60),
        (True, True, 60),       # a bool is not a count
        (False, 50, 50),        # an explicit cap applies in cloud mode too
        (False, 0, None),
        (False, -3, None),      # nonsense in cloud mode: the cloud default, which is no cap
        (False, "many", None),
        (False, True, None),
    ])
    def test_config_overrides_the_cap(self, monkeypatch, local, configured, expected):
        _set_mode(monkeypatch, local=local)
        cols = _cols("code", _BGE, 2)
        engine = _FakeEngine(monkeypatch, {c: _rows("r", 3, 0.2) for c in cols})
        engine.mutate = _rerank_flags
        _set_cap_config(monkeypatch, configured)
        _search(engine, cols, n=5, rerank=True)
        body = engine.route_calls()[0]
        if expected is None:
            assert "rerank_max_candidates" not in body
        else:
            assert body["rerank_max_candidates"] == expected

    def test_cloud_mode_has_no_default_cap(self, monkeypatch):
        # The latency evidence is the local cross-encoder; the page-quality evidence
        # on file (nexus-abdp2) is cloud, and cloud keeps scoring every candidate.
        _set_mode(monkeypatch, local=False)
        cols = _cols("code", _BGE, 2)
        engine = _FakeEngine(monkeypatch, {c: _rows("r", 3, 0.2) for c in cols})
        engine.mutate = _rerank_flags
        _search(engine, cols, n=10, rerank=True)
        assert "rerank_max_candidates" not in engine.route_calls()[0]

    def test_cloud_mode_batched_fallback_has_no_default_cap(self, monkeypatch):
        _set_mode(monkeypatch, local=False)
        cols = _cols("code", _BGE, 2)
        engine = _FakeEngine(monkeypatch, {c: _rows("r", 3, 0.2) for c in cols}, route=False)
        flat = engine._serve_flat
        engine._serve_flat = lambda body: {
            "results": flat(body), "rerank_degraded": False, "rerank_model": "fake",
        }
        _search(engine, cols, n=10, rerank=True)
        sent = [b for p, b in engine.calls if p == _SEARCH]
        assert sent and all("rerank_max_candidates" not in b for b in sent)

    @pytest.mark.parametrize("local", [True, False])
    @pytest.mark.parametrize("configured", [None, 50, 0])
    def test_a_post_filtering_caller_is_never_capped(self, monkeypatch, local, configured):
        # `nx search --path` / `--max-file-chunks` pass deep_candidates and filter AFTER
        # retrieval: with a cap, most rows that survive the filter would come back
        # unscored behind the scored rows. No cap, whatever the mode or the config.
        _set_mode(monkeypatch, local=local)
        cols = _cols("code", _BGE, 2)
        engine = _FakeEngine(monkeypatch, {c: _rows("r", 3, 0.2) for c in cols})
        engine.mutate = _rerank_flags
        if configured is not None:
            _set_cap_config(monkeypatch, configured)
        _search(engine, cols, n=10, rerank=True, deep_candidates=True)
        assert "rerank_max_candidates" not in engine.route_calls()[0]

    def test_a_post_filtering_caller_is_not_capped_on_the_batched_fallback_either(self, monkeypatch):
        cols = _cols("code", _BGE, 2)
        engine = _FakeEngine(monkeypatch, {c: _rows("r", 3, 0.2) for c in cols}, route=False)
        flat = engine._serve_flat
        engine._serve_flat = lambda body: {
            "results": flat(body), "rerank_degraded": False, "rerank_model": "fake",
        }
        _search(engine, cols, n=10, rerank=True, deep_candidates=True)
        sent = [b for p, b in engine.calls if p == _SEARCH]
        assert sent and all("rerank_max_candidates" not in b for b in sent)

    def test_without_deep_candidates_the_same_search_is_capped(self, monkeypatch):
        # Control for the test above: only deep_candidates differs.
        cols = _cols("code", _BGE, 2)
        engine = _FakeEngine(monkeypatch, {c: _rows("r", 3, 0.2) for c in cols})
        engine.mutate = _rerank_flags
        _search(engine, cols, n=10, rerank=True)
        assert engine.route_calls()[0]["rerank_max_candidates"] == 60

    def test_rows_past_the_cap_keep_vector_order_behind_the_scored_rows(self, monkeypatch):
        # Emulate the engine: score the first `cap` rows, leave the rest unscored.
        # docs (x4 over-fetch): n=20 asks for 80 rows per collection, the cap is 60.
        cols = _cols("docs", _BGE, 1)
        engine = _FakeEngine(monkeypatch, {cols[0]: _rows("r", 100, 0.2)})
        self._engine_that_honours_the_cap(engine)

        results = _search(engine, cols, n=20, rerank=True)
        scored = [r for r in results if "rerank_score" in r.metadata]
        unscored = [r for r in results if "rerank_score" not in r.metadata]
        assert len(scored) == 60 and len(unscored) == 20
        # unscored rows stay in vector (distance) order, r60..r79
        assert [r.id for r in unscored] == [f"r{i}" for i in range(60, 80)]
        assert max(r.distance for r in scored) <= min(r.distance for r in unscored)

        # The result count is what an uncapped search returns.
        monkeypatch.setattr(
            "nexus.search_engine.load_config",
            lambda: {"search": {"contradiction_check": False, "rerank_max_candidates": 0}},
        )
        uncapped = _search(engine, cols, n=20, rerank=True)
        assert len(uncapped) == len(results) == 80
        assert all("rerank_score" in r.metadata for r in uncapped)

    def test_the_default_cap_never_starves_a_page(self):
        # The cap must leave the reranker at least the n rows the page needs. The
        # engine half of "a pool under the cap is scored in full" is pinned by
        # RerankStageTest.maxCandidatesAtOrAboveRowCountBehavesExactlyAsNoCap; this
        # is the client half, on the function the request is built from.
        for n in range(1, 301):
            cap = se._rerank_candidate_cap(n, {}, deep_candidates=False)
            assert cap is not None and cap >= n, n

    @staticmethod
    def _engine_that_honours_the_cap(engine):
        """Emulate the engine's stage: score the first ``rerank_max_candidates``
        merged rows (every row when the field is absent), leave the rest unscored."""

        def stage(env):
            body = engine.route_calls()[-1]
            cap = body.get("rerank_max_candidates", len(env["results"]))
            for i, row in enumerate(env["results"][:cap]):
                row["rerank_score"] = 1.0 - i / 1000
            return _rerank_flags(env)

        engine.mutate = stage


# ── lexical stays on its route ───────────────────────────────────────────────


class TestLexicalLeg:
    def test_lexical_rows_are_unioned_in_and_exempt_from_the_threshold(self, monkeypatch):
        cols = _cols("code", _BGE, 2)
        data = {cols[0]: [("v-near", 0.1), ("both-far", 0.9)], cols[1]: [("v-mid", 0.3)]}
        engine = _FakeEngine(monkeypatch, data)
        engine.lexical_rows = [
            # also a vector row, past the threshold: the lexical copy keeps it
            {"id": "both-far", "content": "t", "distance": 0.9, "collection": cols[0]},
            # lexical only
            {"id": "lex-only", "content": "t", "distance": 0.8, "collection": cols[1]},
            # also a kept vector row: the vector copy wins, no duplicate
            {"id": "v-near", "content": "t", "distance": 0.1, "collection": cols[0]},
        ]
        results = _search(engine, cols, threshold_override=0.5, lexical=True)

        assert _HYBRID in engine.paths()
        assert _ROUTE in engine.paths()
        ids = [r.id for r in results]
        assert sorted(ids) == ["both-far", "lex-only", "v-mid", "v-near"]
        assert len(ids) == len(set(ids))
        # the threshold is sent to the route, so the vector leg's far row is
        # dropped there and only the lexical copy survives
        assert engine.route_calls()[0]["thresholds"] == {cols[0]: 0.5, cols[1]: 0.5}
