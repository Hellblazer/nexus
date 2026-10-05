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


def _http_error(path: str, code: int, body: dict) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        path, code, "err", {}, io.BytesIO(json.dumps(body).encode()),  # type: ignore[arg-type]
    )


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
        #: raise this HTTP error for a route request (code, body), once per
        #: predicate hit: ``fail_when(body) -> (code, body) | None``
        self.fail_when = None
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
            if not self.route:
                raise _http_error(path, 404, {"error": "not found"})
            if self.fail_when is not None:
                failure = self.fail_when(body)
                if failure is not None:
                    raise _http_error(path, failure[0], failure[1])
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
        (100, True, 400),
        (10, True, 300),
    ])
    def test_limit_is_the_pool_cap_clamped_for_the_route_and_for_rerank(
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

    def test_a_400_that_is_not_the_model_mix_fails_the_group_without_a_retry_storm(
        self, monkeypatch,
    ):
        cols = _cols("code", _BGE, 3)
        engine = _FakeEngine(monkeypatch, {c: _rows("r", 3, 0.2) for c in cols})
        engine.fail_when = lambda body: (400, {"error": "field 'where' is malformed"})
        with pytest.raises(VectorServiceError, match="all 3 collections failed"):
            _search(engine, cols)
        assert len(engine.route_calls()) == 1


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
