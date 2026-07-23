"""AC1–AC8: Search engine — hybrid scoring, reranking, output formatters."""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from nexus.formatters import format_json, format_vimgrep
from nexus.scoring import (
    apply_hybrid_scoring,
    hybrid_score,
    min_max_normalize,
    round_robin_interleave,
)
from nexus.search_engine import _attach_display_paths, search_cross_corpus
from nexus.types import SearchResult


# ── AC1: Hybrid scoring ───────────────────────────────────────────────────────

def test_min_max_normalize_basic():
    """min_max_normalize maps min→0, max→1, middle proportionally."""
    values = [1.0, 3.0, 5.0]
    assert min_max_normalize(1.0, values) == pytest.approx(0.0, abs=1e-6)
    assert min_max_normalize(5.0, values) == pytest.approx(1.0, abs=1e-6)
    assert min_max_normalize(3.0, values) == pytest.approx(0.5, abs=1e-6)


def test_min_max_normalize_all_equal_returns_zero():
    """All-identical window → denominator is ε, result ≈ 0."""
    values = [2.0, 2.0, 2.0]
    result = min_max_normalize(2.0, values)
    assert result == pytest.approx(0.0, abs=1e-3)


def test_hybrid_score_weights():
    """hybrid_score = 0.7 * vector_norm + 0.3 * frecency_norm."""
    # vector_norm=0.8, frecency_norm=0.5 → 0.7*0.8 + 0.3*0.5 = 0.56 + 0.15 = 0.71
    score = hybrid_score(vector_norm=0.8, frecency_norm=0.5)
    assert score == pytest.approx(0.71, abs=1e-6)


def test_hybrid_score_zero_frecency():
    """For docs/knowledge results with no frecency, score = 0.7 * vector_norm."""
    score = hybrid_score(vector_norm=1.0, frecency_norm=0.0)
    assert score == pytest.approx(0.7, abs=1e-6)


def test_hybrid_score_ripgrep_exact_vector_norm_one():
    """Ripgrep exact-match: vector_norm=1.0 before weighted sum."""
    score = hybrid_score(vector_norm=1.0, frecency_norm=0.6)
    assert score == pytest.approx(0.7 * 1.0 + 0.3 * 0.6, abs=1e-6)


# ── AC2: --hybrid warns when no code corpus ───────────────────────────────────

def test_hybrid_no_code_corpus_warning(capsys):
    """hybrid_score_results logs a warning when no code__ collections in scope."""
    results = [
        SearchResult(id="1", content="text", distance=0.1,
                     collection="docs__papers", metadata={}),
    ]
    apply_hybrid_scoring(results, hybrid=True)
    captured = capsys.readouterr()
    assert "no code corpus" in (captured.out + captured.err).lower()


def test_hybrid_mixed_corpus_no_warning(capsys):
    """With both code__ and docs__ in scope, no warning is printed."""
    results = [
        SearchResult(id="1", content="code", distance=0.1,
                     collection="code__myrepo", metadata={"frecency_score": 1.5}),
        SearchResult(id="2", content="docs", distance=0.2,
                     collection="docs__papers", metadata={}),
    ]
    apply_hybrid_scoring(results, hybrid=True)
    out = capsys.readouterr()
    assert "no code corpus" not in (out.err + out.out).lower()


# ── AC3: Cross-corpus reranking ───────────────────────────────────────────────

def test_round_robin_interleave_no_rerank():
    """round_robin_interleave alternates results across collections."""
    code_results = [
        SearchResult(id="c1", content="c1", distance=0.1, collection="code__r", metadata={}),
        SearchResult(id="c2", content="c2", distance=0.2, collection="code__r", metadata={}),
    ]
    doc_results = [
        SearchResult(id="d1", content="d1", distance=0.3, collection="docs__d", metadata={}),
    ]
    merged = round_robin_interleave([code_results, doc_results])
    ids = [r.id for r in merged]
    # Round-robin: c1, d1, c2
    assert ids == ["c1", "d1", "c2"]


def test_cross_corpus_overfetch():
    """search_cross_corpus over-fetches per corpus: 2x code, 4x docs."""
    mock_t3 = MagicMock()
    mock_t3.search.return_value = []
    # code__r → 2x (20), docs__d → 4x (40)
    search_cross_corpus(
        query="test", collections=["code__r", "docs__d"],
        n_results=10, t3=mock_t3
    )
    calls = mock_t3.search.call_args_list
    assert len(calls) == 2
    code_call = [c for c in calls if c.args[1] == ["code__r"]][0]
    docs_call = [c for c in calls if c.args[1] == ["docs__d"]][0]
    assert code_call.kwargs.get("n_results") == 20   # 10 * 2
    assert docs_call.kwargs.get("n_results") == 40   # 10 * 4



# ── AC7: Output formatters ────────────────────────────────────────────────────

def test_format_vimgrep():
    """format_vimgrep produces path:line:0:content lines."""
    results = [
        SearchResult(id="1", content="    def authenticate(user, token):",
                     distance=0.1, collection="code__r",
                     metadata={"source_path": "./auth.py", "line_start": 42}),
    ]
    lines = format_vimgrep(results)
    assert lines[0] == "./auth.py:42:0:    def authenticate(user, token):"


def test_format_vimgrep_missing_source_path():
    """format_vimgrep falls back to empty path when metadata lacks source_path."""
    results = [
        SearchResult(id="1", content="some text", distance=0.1,
                     collection="knowledge__k", metadata={}),
    ]
    lines = format_vimgrep(results)
    assert len(lines) == 1
    assert ":0:" in lines[0]


def test_format_json_valid():
    """format_json produces valid JSON with id, content, distance fields."""
    import json
    results = [
        SearchResult(id="abc123", content="some text", distance=0.42,
                     collection="code__r",
                     metadata={"source_path": "./x.py"}),
    ]
    output = format_json(results)
    parsed = json.loads(output)
    assert isinstance(parsed, list)
    assert parsed[0]["id"] == "abc123"
    assert parsed[0]["distance"] == pytest.approx(0.42)
    assert "content" in parsed[0]


def test_format_json_includes_metadata():
    """format_json embeds metadata fields."""
    import json
    results = [
        SearchResult(id="1", content="x", distance=0.1, collection="code__r",
                     metadata={"source_path": "./a.py", "line_start": 10}),
    ]
    parsed = json.loads(format_json(results))
    assert parsed[0].get("source_path") == "./a.py"



# ── AC8: min_max_normalize over combined window ───────────────────────────────

def test_min_max_normalize_over_combined_not_per_corpus():
    """Normalization uses combined window, not per-corpus."""
    # code result: distance=0.1, doc result: distance=0.9
    # Combined window min=0.1, max=0.9
    distances = [0.1, 0.9]
    norm_code = min_max_normalize(0.1, distances)
    norm_doc = min_max_normalize(0.9, distances)
    assert norm_code == pytest.approx(0.0, abs=1e-6)
    assert norm_doc == pytest.approx(1.0, abs=1e-6)

    # If per-corpus: code [0.1] → both 0.0; docs [0.9] → both 0.0
    # Combined window correctly distinguishes them
    assert norm_code < norm_doc


# ── Phase 1.1 (RDR-087 / nexus-yi4b.1.1): threshold_override ─────────────────


class _ThresholdFakeT3:
    """Minimal T3 stand-in that returns preset per-collection rows.

    Distances are raw (pre-filter). ``_voyage_client`` is toggled so the
    callee's ``apply_thresholds`` gate can be exercised.
    """

    def __init__(self, results_by_col: dict[str, list[dict]], voyage: bool = True):
        self._results = results_by_col
        self._voyage_client = "fake-voyage" if voyage else None

    def search(self, query, collection_names, n_results=10, where=None):
        return self._results.get(collection_names[0], [])


class TestThresholdOverride:
    """``threshold_override`` replaces the per-collection config threshold
    uniformly across collections. Entry point for the RDR-087 Phase 1
    workaround for silent threshold-drop on dense-prose collections.
    """

    def test_none_preserves_config_threshold(self):
        """Override=None falls back to per-collection config (code=0.45)."""
        t3 = _ThresholdFakeT3({
            "code__nexus": [
                {"id": "a", "content": "keep", "distance": 0.30},
                {"id": "b", "content": "drop", "distance": 0.50},
            ],
        })
        results = search_cross_corpus(
            "test", ["code__nexus"], 10, t3, threshold_override=None,
        )
        assert {r.id for r in results} == {"a"}

    def test_strict_override_filters_more(self):
        """Override=0.35 filters results that the 0.45 config would keep."""
        t3 = _ThresholdFakeT3({
            "code__nexus": [
                {"id": "a", "content": "keep", "distance": 0.30},
                {"id": "b", "content": "edge", "distance": 0.40},
            ],
        })
        results = search_cross_corpus(
            "test", ["code__nexus"], 10, t3, threshold_override=0.35,
        )
        assert {r.id for r in results} == {"a"}

    def test_permissive_override_keeps_more(self):
        """Override=1.0 keeps results the 0.65 knowledge threshold would drop."""
        t3 = _ThresholdFakeT3({
            "knowledge__papers": [
                {"id": "a", "content": "below", "distance": 0.60},
                {"id": "b", "content": "above", "distance": 0.80},
            ],
        })
        results = search_cross_corpus(
            "test", ["knowledge__papers"], 10, t3, threshold_override=1.0,
        )
        assert {r.id for r in results} == {"a", "b"}

    def test_infinity_override_disables_filter(self):
        """Override=inf keeps everything regardless of distance."""
        t3 = _ThresholdFakeT3({
            "knowledge__papers": [
                {"id": "a", "content": "fine", "distance": 0.30},
                {"id": "b", "content": "noise", "distance": 0.95},
                {"id": "c", "content": "garbage", "distance": 1.50},
            ],
        })
        results = search_cross_corpus(
            "test", ["knowledge__papers"], 10, t3,
            threshold_override=float("inf"),
        )
        assert {r.id for r in results} == {"a", "b", "c"}

    def test_override_applies_without_voyage_client(self):
        """``threshold_override`` bypasses the Voyage gate — explicit user
        intent overrides the local-mode skip heuristic."""
        t3 = _ThresholdFakeT3(
            {"knowledge__papers": [
                {"id": "a", "content": "keep", "distance": 0.50},
                {"id": "b", "content": "drop", "distance": 0.90},
            ]},
            voyage=False,
        )
        results = search_cross_corpus(
            "test", ["knowledge__papers"], 10, t3, threshold_override=0.70,
        )
        assert {r.id for r in results} == {"a"}


class _ConcurrentFakeT3:
    """T3 stand-in that records concurrency and lets per-collection latency be
    tuned so completion order can be forced to differ from input order.

    ``delays`` maps a collection name to the seconds its ``search`` sleeps.
    ``raises`` is a set of collections that raise ``VectorServiceError``.
    """

    def __init__(self, results_by_col, delays=None, raises=None):
        import threading
        self._results = results_by_col
        self._delays = delays or {}
        self._raises = raises or set()
        self._voyage_client = "fake-voyage"
        self._lock = threading.Lock()
        self._inflight = 0
        self.max_inflight = 0

    def search(self, query, collection_names, n_results=10, where=None):
        import time
        from nexus.db.http_vector_client import VectorServiceError
        name = collection_names[0]
        with self._lock:
            self._inflight += 1
            self.max_inflight = max(self.max_inflight, self._inflight)
        try:
            time.sleep(self._delays.get(name, 0.0))
            if name in self._raises:
                raise VectorServiceError(f"{name} unservable")
            return self._results.get(name, [])
        finally:
            with self._lock:
                self._inflight -= 1


class TestParallelFanOut:
    """nexus-o51et: the per-collection fan-out runs concurrently but the merge
    is deterministic in ``collections`` order regardless of completion order,
    and a single unservable collection does not sink the whole search.
    """

    def test_result_order_independent_of_completion_order(self):
        # Earlier collections sleep longer, so they complete LAST. If the merge
        # depended on completion order the per-collection blocks would be
        # reversed; assert they follow input order instead.
        cols = ["knowledge__a", "knowledge__b", "knowledge__c"]
        t3 = _ConcurrentFakeT3(
            {
                "knowledge__a": [{"id": "a", "content": "x", "distance": 0.10}],
                "knowledge__b": [{"id": "b", "content": "x", "distance": 0.10}],
                "knowledge__c": [{"id": "c", "content": "x", "distance": 0.10}],
            },
            delays={"knowledge__a": 0.06, "knowledge__b": 0.03, "knowledge__c": 0.0},
            raises=set(),
        )
        results = search_cross_corpus(
            "test", cols, 10, t3, threshold_override=float("inf"),
            cluster_by=None,
        )
        assert [r.collection for r in results] == cols
        assert [r.id for r in results] == ["a", "b", "c"]

    def test_fan_out_runs_concurrently(self):
        cols = [f"knowledge__c{i}" for i in range(5)]
        t3 = _ConcurrentFakeT3(
            {c: [{"id": c, "content": "x", "distance": 0.10}] for c in cols},
            delays={c: 0.05 for c in cols},
        )
        search_cross_corpus(
            "test", cols, 10, t3, threshold_override=float("inf"),
            cluster_by=None,
        )
        assert t3.max_inflight > 1  # genuinely parallel, not serialized

    def test_one_failed_collection_does_not_sink_search(self):
        cols = ["knowledge__ok", "knowledge__bad", "knowledge__ok2"]
        t3 = _ConcurrentFakeT3(
            {
                "knowledge__ok": [{"id": "ok", "content": "x", "distance": 0.10}],
                "knowledge__ok2": [{"id": "ok2", "content": "x", "distance": 0.10}],
            },
            raises={"knowledge__bad"},
        )
        results = search_cross_corpus(
            "test", cols, 10, t3, threshold_override=float("inf"),
            cluster_by=None,
        )
        assert {r.id for r in results} == {"ok", "ok2"}

    def test_override_applies_uniformly_across_collections(self):
        """One override replaces the per-corpus threshold for every collection."""
        t3 = _ThresholdFakeT3({
            "code__nexus": [
                {"id": "c1", "content": "c1", "distance": 0.40},
                {"id": "c2", "content": "c2", "distance": 0.60},
            ],
            "knowledge__papers": [
                {"id": "k1", "content": "k1", "distance": 0.40},
                {"id": "k2", "content": "k2", "distance": 0.60},
            ],
        })
        results = search_cross_corpus(
            "test", ["code__nexus", "knowledge__papers"], 10, t3,
            threshold_override=0.50,
        )
        assert {r.id for r in results} == {"c1", "k1"}


# ── Phase 1.2 (RDR-087 / nexus-yi4b.1.2): SearchDiagnostics ──────────────────


class TestSearchDiagnostics:
    """``search_cross_corpus`` populates a ``SearchDiagnostics`` struct when
    caller passes ``diagnostics_out=[]``. The CLI uses it to emit the
    RDR-087 silent-zero stderr line naming the worst-offender collection.
    """

    def test_diagnostics_absent_by_default(self):
        """Omitting ``diagnostics_out`` preserves the pre-1.2 return type."""
        t3 = _ThresholdFakeT3({
            "code__nexus": [{"id": "a", "content": "ok", "distance": 0.30}],
        })
        results = search_cross_corpus("test", ["code__nexus"], 10, t3)
        assert len(results) == 1

    def test_diagnostics_populated_when_requested(self):
        """``diagnostics_out=[]`` gets exactly one ``SearchDiagnostics`` appended."""
        from nexus.search_engine import SearchDiagnostics

        t3 = _ThresholdFakeT3({
            "code__nexus": [
                {"id": "a", "content": "keep", "distance": 0.30},
                {"id": "b", "content": "drop", "distance": 0.50},
            ],
        })
        diag_out: list = []
        search_cross_corpus(
            "test", ["code__nexus"], 10, t3, diagnostics_out=diag_out,
        )
        assert len(diag_out) == 1
        diag = diag_out[0]
        assert isinstance(diag, SearchDiagnostics)
        raw, dropped, threshold, top_dist = diag.per_collection["code__nexus"]
        assert raw == 2 and dropped == 1
        assert threshold == pytest.approx(0.45)
        assert top_dist == pytest.approx(0.50)
        assert diag.total_dropped == 1

    def test_diagnostics_no_drops_tracks_zero(self):
        """Diagnostics reports zero drops when every candidate passes."""
        t3 = _ThresholdFakeT3({
            "code__nexus": [{"id": "a", "content": "ok", "distance": 0.30}],
        })
        diag_out: list = []
        search_cross_corpus(
            "test", ["code__nexus"], 10, t3, diagnostics_out=diag_out,
        )
        diag = diag_out[0]
        assert diag.total_dropped == 0
        raw, dropped, _, top_dist = diag.per_collection["code__nexus"]
        assert raw == 1 and dropped == 0
        assert top_dist is None

    def test_worst_offender_picks_highest_top_distance(self):
        """Worst offender = full-drop collection with highest top_distance."""
        t3 = _ThresholdFakeT3({
            "code__a": [
                {"id": "a1", "content": "x", "distance": 0.50},
                {"id": "a2", "content": "x", "distance": 0.55},
            ],
            "knowledge__b": [
                {"id": "b1", "content": "x", "distance": 0.80},
                {"id": "b2", "content": "x", "distance": 0.95},
            ],
        })
        diag_out: list = []
        search_cross_corpus(
            "test", ["code__a", "knowledge__b"], 10, t3,
            diagnostics_out=diag_out,
        )
        worst = diag_out[0].worst_offender()
        assert worst is not None
        name, threshold, top_distance = worst
        assert name == "knowledge__b"
        assert top_distance == pytest.approx(0.80)

    def test_worst_offender_ignores_partial_drops(self):
        """Collections with any survivor are not eligible as 'worst'."""
        t3 = _ThresholdFakeT3({
            "code__a": [
                {"id": "a1", "content": "keep", "distance": 0.30},
                {"id": "a2", "content": "drop", "distance": 0.80},
            ],
            "knowledge__b": [
                {"id": "b1", "content": "x", "distance": 0.70},
            ],
        })
        diag_out: list = []
        search_cross_corpus(
            "test", ["code__a", "knowledge__b"], 10, t3,
            diagnostics_out=diag_out,
        )
        worst = diag_out[0].worst_offender()
        assert worst is not None
        assert worst[0] == "knowledge__b"

    def test_worst_offender_none_when_no_full_drops(self):
        """``None`` when no collection had every candidate dropped."""
        t3 = _ThresholdFakeT3({
            "code__a": [{"id": "a", "content": "keep", "distance": 0.30}],
        })
        diag_out: list = []
        search_cross_corpus(
            "test", ["code__a"], 10, t3, diagnostics_out=diag_out,
        )
        assert diag_out[0].worst_offender() is None

    def test_collections_with_drops_counts_nonzero(self):
        """``collections_with_drops`` counts collections with ``dropped >= 1``."""
        t3 = _ThresholdFakeT3({
            "code__a": [
                {"id": "a1", "content": "keep", "distance": 0.30},
                {"id": "a2", "content": "drop", "distance": 0.80},
            ],
            "knowledge__b": [{"id": "b1", "content": "drop", "distance": 0.90}],
            "docs__c": [{"id": "c1", "content": "keep", "distance": 0.30}],
        })
        diag_out: list = []
        search_cross_corpus(
            "test", ["code__a", "knowledge__b", "docs__c"], 10, t3,
            diagnostics_out=diag_out,
        )
        assert diag_out[0].collections_with_drops() == 2


# ── Phase 2.2 (RDR-087 / nexus-yi4b.2.2): hot-path telemetry INSERT ──────────


class _StubTelemetry:
    """Stand-in for ``db.t2.telemetry.Telemetry`` capturing log_search_batch calls."""

    def __init__(self) -> None:
        self.batches: list[list[tuple]] = []

    def log_search_batch(self, rows):
        self.batches.append(list(rows))


class TestSearchTelemetryHotPath:
    """``search_cross_corpus`` writes one row per collection to
    ``Telemetry.log_search_batch`` when a telemetry store is injected.
    Row shape: ``(ts, query_hash, collection, raw_count, kept_count,
    top_distance, threshold)``. ``top_distance`` here is the min distance
    across ALL raw candidates (not just the dropped subset).
    """

    def test_writes_one_row_per_collection(self):
        telemetry = _StubTelemetry()
        t3 = _ThresholdFakeT3({
            "code__a": [
                {"id": "a1", "content": "x", "distance": 0.30},
                {"id": "a2", "content": "x", "distance": 0.50},
            ],
            "knowledge__b": [
                {"id": "b1", "content": "x", "distance": 0.60},
            ],
        })
        search_cross_corpus(
            "query one", ["code__a", "knowledge__b"], 10, t3,
            telemetry=telemetry,
        )
        assert len(telemetry.batches) == 1
        rows = telemetry.batches[0]
        assert len(rows) == 2
        names = {r[2] for r in rows}
        assert names == {"code__a", "knowledge__b"}

    def test_row_shape_matches_spec(self):
        """Row layout: (ts, query_hash, collection, raw, kept, top_distance, threshold)."""
        import hashlib

        telemetry = _StubTelemetry()
        t3 = _ThresholdFakeT3({
            "code__a": [
                {"id": "a1", "content": "x", "distance": 0.30},
                {"id": "a2", "content": "x", "distance": 0.50},
            ],
        })
        search_cross_corpus(
            "my query", ["code__a"], 10, t3, telemetry=telemetry,
        )
        rows = telemetry.batches[0]
        assert len(rows) == 1
        ts, query_hash, collection, raw_count, kept_count, top_distance, threshold = rows[0]
        expected_hash = hashlib.sha256("my query".encode()).hexdigest()[:64]
        assert query_hash == expected_hash
        assert collection == "code__a"
        assert raw_count == 2
        assert kept_count == 1  # 2 raw − 1 dropped (0.50 > 0.45)
        assert top_distance == pytest.approx(0.30)  # min over RAW (kept or dropped)
        assert threshold == pytest.approx(0.45)
        assert isinstance(ts, str) and "T" in ts

    def test_top_distance_null_when_raw_empty(self):
        """Collection returning zero raw candidates → top_distance is None."""
        telemetry = _StubTelemetry()
        t3 = _ThresholdFakeT3({"code__empty": []})
        search_cross_corpus(
            "any", ["code__empty"], 10, t3, telemetry=telemetry,
        )
        rows = telemetry.batches[0]
        assert len(rows) == 1
        _, _, _, raw_count, kept_count, top_distance, _ = rows[0]
        assert raw_count == 0
        assert kept_count == 0
        assert top_distance is None

    def test_no_write_when_telemetry_absent(self):
        """Omitting ``telemetry`` preserves pre-2.2 engine behaviour — no writes."""
        t3 = _ThresholdFakeT3({
            "code__a": [{"id": "a", "content": "x", "distance": 0.30}],
        })
        results = search_cross_corpus("q", ["code__a"], 10, t3)
        assert len(results) == 1

    def test_config_flag_disables_insert(self, monkeypatch):
        """``telemetry.search_enabled=false`` in config suppresses writes even
        when a telemetry store is provided."""
        telemetry = _StubTelemetry()
        t3 = _ThresholdFakeT3({
            "code__a": [{"id": "a", "content": "x", "distance": 0.30}],
        })

        def fake_load_config():
            return {"telemetry": {"search_enabled": False}}

        monkeypatch.setattr("nexus.search_engine.load_config", fake_load_config)
        search_cross_corpus(
            "q", ["code__a"], 10, t3, telemetry=telemetry,
        )
        assert telemetry.batches == []

    def test_threshold_is_none_when_filtering_skipped(self):
        """Non-Voyage + no override → no threshold applied, row reports None."""
        telemetry = _StubTelemetry()
        t3 = _ThresholdFakeT3(
            {"code__a": [{"id": "a", "content": "x", "distance": 0.30}]},
            voyage=False,
        )
        search_cross_corpus(
            "q", ["code__a"], 10, t3, telemetry=telemetry,
        )
        rows = telemetry.batches[0]
        _, _, _, raw, kept, _, threshold = rows[0]
        assert raw == 1
        assert kept == 1  # no filtering → all raw are kept
        assert threshold is None

    def test_duplicate_insert_is_ignored_not_raised(self, tmp_path):
        """INSERT OR IGNORE on the real Telemetry store absorbs a duplicate PK."""
        from nexus.db.t2.telemetry import Telemetry

        telemetry = Telemetry(tmp_path / "mem.db")
        try:
            row = (
                "2026-04-17T18:00:00Z", "h" * 64, "code__a",
                3, 1, 0.30, 0.45,
            )
            telemetry.log_search_batch([row])
            telemetry.log_search_batch([row])  # duplicate PK — must not raise
            count = telemetry.conn.execute(
                "SELECT COUNT(*) FROM search_telemetry"
            ).fetchone()[0]
            assert count == 1
        finally:
            telemetry.close()


# ── nexus-1qed: _attach_display_paths catalog projection ────────────────────


class TestAttachDisplayPaths:
    """nexus-1qed: search_engine attaches catalog-resolved display paths
    to results so formatters never need to import the catalog. Best-
    effort: missing catalog / missing doc_ids leave the field unset."""

    def _result(self, doc_id: str = "", source_path: str = "") -> SearchResult:
        meta: dict = {}
        if doc_id:
            meta["doc_id"] = doc_id
        if source_path:
            meta["source_path"] = source_path
        return SearchResult(
            id=f"r-{doc_id or source_path or 'x'}",
            content="x", distance=0.1, collection="code__c", metadata=meta,
        )

    def test_no_catalog_is_noop(self):
        r = self._result(doc_id="ART-x", source_path="src/legacy.py")
        _attach_display_paths([r], catalog=None)
        assert "_display_path" not in r.metadata

    def test_no_doc_ids_is_noop(self):
        catalog = MagicMock()
        r = self._result(source_path="src/legacy.py")
        _attach_display_paths([r], catalog=catalog)
        catalog.by_doc_id.assert_not_called()
        assert "_display_path" not in r.metadata

    def test_attaches_catalog_resolved_path(self):
        """WITH TEETH: the resolved file_path lands on the metadata
        dict under ``_display_path``. A regression that drops the
        write or routes through the wrong key fails this test.

        Updated for nexus-7lm3q batch path: catalogs now expose resolve_many()
        which is called instead of by_doc_id(). The OldCatalog helper class
        (without resolve_many) tests the legacy fallback path."""
        class _Catalog:
            """Catalog that exposes resolve_many (the new batch path)."""
            def resolve_many(self, doc_ids):
                return {
                    did: SimpleNamespace(file_path="/abs/from/catalog.py")
                    for did in doc_ids
                }

        r = self._result(doc_id="ART-deadbeef", source_path="src/legacy.py")
        _attach_display_paths([r], catalog=_Catalog())
        assert r.metadata["_display_path"] == "/abs/from/catalog.py"
        # source_path stays untouched: the prune verb owns its removal.
        assert r.metadata["source_path"] == "src/legacy.py"

    def test_doc_id_with_no_catalog_entry_leaves_field_unset(self):
        class _Catalog:
            def resolve_many(self, doc_ids):
                return {}  # no entries found

        r = self._result(doc_id="ART-orphan", source_path="src/legacy.py")
        _attach_display_paths([r], catalog=_Catalog())
        assert "_display_path" not in r.metadata

    def test_repeated_doc_id_only_hits_catalog_once(self):
        """Multi-chunk results sharing the same doc_id share one
        catalog lookup. Pre-fix code that loops per result without a
        cache hits the catalog N times (slow on large result sets).

        Updated for nexus-7lm3q: resolve_many is called ONCE for all
        distinct doc_ids, even when the same doc_id repeats across results."""
        call_count = 0

        class _Catalog:
            def resolve_many(self, doc_ids):
                nonlocal call_count
                call_count += 1
                return {
                    did: SimpleNamespace(file_path="/abs/foo.py")
                    for did in doc_ids
                }

        results = [
            self._result(doc_id="ART-shared"),
            self._result(doc_id="ART-shared"),
            self._result(doc_id="ART-shared"),
        ]
        _attach_display_paths(results, catalog=_Catalog())
        assert call_count == 1
        for r in results:
            assert r.metadata["_display_path"] == "/abs/foo.py"

    def test_catalog_exception_does_not_break_search(self):
        """A catalog read failure is logged-and-swallowed; the result
        passes through with no _display_path. Display always degrades
        gracefully."""
        catalog = MagicMock()
        catalog.by_doc_id.side_effect = RuntimeError("catalog dead")
        r = self._result(doc_id="ART-x", source_path="src/legacy.py")
        # Must not raise.
        _attach_display_paths([r], catalog=catalog)
        assert "_display_path" not in r.metadata


# ── nexus-rehf: _attach_doc_ids_from_catalog (RDR-108 Phase 4 review D-H1+H2) ─


class TestAttachDocIdsFromCatalog:
    """nexus-rehf (RDR-108 Phase 4 review D-H1+H2): the search
    orchestrator must resolve ``doc_id`` via the catalog manifest and
    inject it into ``r.metadata["doc_id"]`` BEFORE downstream
    consumers (apply_link_boost, _attach_display_paths) read it.
    Phase 3 (nexus-bdag) removed doc_id from chunk metadata; without
    this attach step both downstream functions silently no-op on
    Phase-3 chunks.
    """

    def _result(self, *, chunk_text_hash: str = "", doc_id: str = "") -> SearchResult:
        from nexus.types import SearchResult
        meta: dict = {}
        if chunk_text_hash:
            meta["chunk_text_hash"] = chunk_text_hash
        if doc_id:
            meta["doc_id"] = doc_id
        return SearchResult(
            id=f"r-{chunk_text_hash[:8] or doc_id or 'x'}",
            content="x", distance=0.1, collection="docs__c", metadata=meta,
        )

    def test_no_catalog_is_noop(self):
        from nexus.search_engine import _attach_doc_ids_from_catalog
        r = self._result(chunk_text_hash="a" * 64)
        _attach_doc_ids_from_catalog([r], catalog=None)
        assert "doc_id" not in r.metadata

    def test_no_chashes_is_noop(self):
        from nexus.search_engine import _attach_doc_ids_from_catalog
        catalog = MagicMock()
        r = self._result()  # no chunk_text_hash
        _attach_doc_ids_from_catalog([r], catalog=catalog)
        catalog.docs_for_chashes.assert_not_called()
        assert "doc_id" not in r.metadata

    def test_injects_doc_id_from_manifest(self):
        """WITH TEETH: a Phase-3 chunk (no doc_id in metadata, only
        chunk_text_hash) gets its catalog doc_id injected. Reverting
        the helper or skipping the orchestrator wiring breaks this.
        """
        from nexus.search_engine import _attach_doc_ids_from_catalog
        chash = "a" * 64
        catalog = MagicMock()
        catalog.docs_for_chashes.return_value = {chash: ["1.1.5"]}
        r = self._result(chunk_text_hash=chash)
        _attach_doc_ids_from_catalog([r], catalog=catalog)
        assert r.metadata["doc_id"] == "1.1.5"
        catalog.docs_for_chashes.assert_called_once_with([chash])

    def test_preserves_existing_doc_id_legacy_fallback(self):
        """Pre-Phase-3 chunks may still carry doc_id in metadata; the
        helper must NOT overwrite the legacy field, only fill the gap.

        nexus-7lm3q critic Sig-3: this previously used ``MagicMock()``, whose
        auto-created ``get_manifests`` made ``manifest_cache.update(mock)`` a
        silent no-op — so the batch path ran but chunk_count/chunk_index were
        never exercised. Use a concrete catalog so the batch path is genuinely
        driven and the gap-fill is asserted (LEGACY-doc preserved, chunk_count
        filled in from the manifest)."""
        from nexus.search_engine import _attach_doc_ids_from_catalog
        chash = "b" * 64

        class _Catalog:
            """Batch-capable catalog (has get_manifests)."""
            def docs_for_chashes(self, chashes):
                # The chunk already carries doc_id, so this fallback resolution
                # is not used; return a different doc to prove it's ignored.
                return {chash: ["MANIFEST-doc"]}
            def get_manifests(self, doc_ids):
                # Keyed on the LEGACY doc_id (the preserved one), not MANIFEST-doc.
                return {did: [{"chash": chash, "position": 0}] for did in doc_ids}

        r = self._result(chunk_text_hash=chash, doc_id="LEGACY-doc")
        _attach_doc_ids_from_catalog([r], catalog=_Catalog())
        # Legacy doc_id preserved (not overwritten by MANIFEST-doc) ...
        assert r.metadata["doc_id"] == "LEGACY-doc"
        # ... and the gap (chunk_count/chunk_index) IS filled via the batch.
        assert r.metadata["chunk_count"] == 1
        assert r.metadata["chunk_index"] == 0

    def test_chash_with_no_manifest_entry_leaves_field_unset(self):
        from nexus.search_engine import _attach_doc_ids_from_catalog
        catalog = MagicMock()
        catalog.docs_for_chashes.return_value = {}  # nothing matched
        r = self._result(chunk_text_hash="c" * 64)
        _attach_doc_ids_from_catalog([r], catalog=catalog)
        assert "doc_id" not in r.metadata

    def test_catalog_exception_does_not_break_search(self):
        """Best-effort: a manifest-lookup failure is logged-and-
        swallowed; results pass through with no doc_id (downstream
        consumers degrade gracefully)."""
        from nexus.search_engine import _attach_doc_ids_from_catalog
        catalog = MagicMock()
        catalog.docs_for_chashes.side_effect = RuntimeError("catalog dead")
        r = self._result(chunk_text_hash="d" * 64)
        # Must not raise.
        _attach_doc_ids_from_catalog([r], catalog=catalog)
        assert "doc_id" not in r.metadata

    def test_link_boost_now_works_on_phase3_chunks(self):
        """End-to-end contract: a Phase-3 chunk (no doc_id in metadata)
        whose catalog doc has outgoing links MUST receive a link boost
        after the search orchestrator runs. Reverting the helper
        wiring in search_cross_corpus drops this back to silently-no-op.
        """
        from nexus.scoring import apply_link_boost
        from nexus.search_engine import _attach_doc_ids_from_catalog

        chash = "e" * 64
        catalog = MagicMock()
        catalog.docs_for_chashes.return_value = {chash: ["DOC-with-links"]}
        # nexus-qnp5s: apply_link_boost now uses links_from_batch() (public API).
        catalog.links_from_batch.return_value = {
            "DOC-with-links": [
                {"from_tumbler": "DOC-with-links", "link_type": "implements"},
                {"from_tumbler": "DOC-with-links", "link_type": "cites"},
            ]
        }
        r = self._result(chunk_text_hash=chash)
        r.hybrid_score = 1.0

        _attach_doc_ids_from_catalog([r], catalog=catalog)
        apply_link_boost([r], catalog=catalog)

        # Boost = 0.15 * min(1.0+0.5, 1.0) = 0.15 * 1.0 = 0.15
        assert r.hybrid_score > 1.0, (
            "Phase-3 chunk did NOT receive link boost. The attach helper "
            "did not inject doc_id, OR apply_link_boost regressed."
        )


# ── Per-collection error isolation (nexus-pebfx.8) ──────────────────────────


class _FailingT3:
    """T3 stand-in where selected collections raise VectorServiceError.

    Models the service-mode failure where a collection's embedding space
    doesn't match the query embedding (HTTP 400 from /v1/vectors/search).
    """

    def __init__(
        self,
        results_by_col: dict[str, list[dict]],
        failing: set[str],
        voyage: bool = True,
    ):
        self._results = results_by_col
        self._failing = set(failing)
        self._voyage_client = "fake-voyage" if voyage else None

    def search(self, query, collection_names, n_results=10, where=None):
        from nexus.db.http_vector_client import VectorServiceError

        col = collection_names[0]
        if col in self._failing:
            raise VectorServiceError(
                "POST /v1/vectors/search → HTTP 400: query embedder produced "
                "a 1024-dim vector but the collections dispatch to chunks_384",
            )
        return self._results.get(col, [])


class TestPerCollectionErrorIsolation:
    """One unservable collection must not sink the whole cross-corpus search.

    nexus-pebfx.8: the default knowledge,code,docs search died with HTTP 400
    because the 384-dim seam-b-test collection was in the prefix expansion
    alongside 1024-dim collections — the loop re-raised on the first
    unservable collection and the user got zero results plus a traceback.
    """

    def test_one_failing_collection_does_not_sink_the_search(self):
        t3 = _FailingT3(
            {"code__nexus": [{"id": "a", "content": "hit", "distance": 0.30}]},
            failing={"knowledge__seam-b-test__minilm-l6-v2-384__v1"},
        )
        results = search_cross_corpus(
            "q",
            ["code__nexus", "knowledge__seam-b-test__minilm-l6-v2-384__v1"],
            10,
            t3,
        )
        assert {r.id for r in results} == {"a"}

    def test_failure_recorded_in_diagnostics(self):
        from nexus.search_engine import SearchDiagnostics

        t3 = _FailingT3(
            {"code__nexus": [{"id": "a", "content": "hit", "distance": 0.30}]},
            failing={"knowledge__seam"},
        )
        diags: list[SearchDiagnostics] = []
        search_cross_corpus(
            "q", ["code__nexus", "knowledge__seam"], 10, t3,
            diagnostics_out=diags,
        )
        assert list(diags[0].failed_collections) == ["knowledge__seam"]
        assert "HTTP 400" in diags[0].failed_collections["knowledge__seam"]

    def test_failed_collection_absent_from_per_collection_diag(self):
        from nexus.search_engine import SearchDiagnostics

        t3 = _FailingT3(
            {"code__nexus": [{"id": "a", "content": "hit", "distance": 0.30}]},
            failing={"knowledge__seam"},
        )
        diags: list[SearchDiagnostics] = []
        search_cross_corpus(
            "q", ["code__nexus", "knowledge__seam"], 10, t3,
            diagnostics_out=diags,
        )
        assert "knowledge__seam" not in diags[0].per_collection
        assert list(diags[0].per_collection) == ["code__nexus"]

    def test_all_collections_failing_reraises(self):
        from nexus.db.http_vector_client import VectorServiceError

        t3 = _FailingT3({}, failing={"code__a", "knowledge__b"})
        with pytest.raises(VectorServiceError, match="all 2 collections failed"):
            search_cross_corpus("q", ["code__a", "knowledge__b"], 10, t3)

    def test_duplicate_collections_all_failing_still_reraises(self):
        """The all-fail guard must not be defeated by a duplicated input
        name (failed_collections is keyed by name; pre-dedup the input)."""
        from nexus.db.http_vector_client import VectorServiceError

        t3 = _FailingT3({}, failing={"code__a"})
        with pytest.raises(VectorServiceError, match="all 1 collections failed"):
            search_cross_corpus("q", ["code__a", "code__a"], 10, t3)

    def test_failure_log_event_name_locked(self):
        """Lock the ``collection_search_failed`` structlog event name —
        downstream log consumers grep for it."""
        from structlog.testing import capture_logs

        t3 = _FailingT3(
            {"code__nexus": [{"id": "a", "content": "hit", "distance": 0.30}]},
            failing={"knowledge__seam"},
        )
        with capture_logs() as logs:
            search_cross_corpus("q", ["code__nexus", "knowledge__seam"], 10, t3)
        assert any(
            entry["event"] == "collection_search_failed"
            and entry["collection"] == "knowledge__seam"
            for entry in logs
        )


# ── nexus-9tsdf / GH #1113: quieter dimension-mismatch search logging ───────


class TestDimensionMismatchLoggingQuieted:
    """A stale, orphaned dimension-mismatched collection (leftover from a
    prior embedder generation) fails every search with an embedding-space
    HTTP 400. Pre-fix this logged one WARNING per search, every search,
    for as long as the orphan existed -- even when it was 1 of 80
    collections and everything else searched fine. Quieted per AC3: <5%
    of the requested scope -> DEBUG (noise); >=5% -> WARNING (real
    problem). Never silent either way."""

    _DIM_ERROR = (
        "POST /v1/vectors/search -> HTTP 400: query embedder produced a "
        "1024-dim vector but the collection dispatches to chunks_384"
    )

    def test_small_fraction_dimension_mismatch_downgraded_to_debug(self):
        """1 mismatched collection out of 21 (~4.8%) is noise -- DEBUG,
        not WARNING. The event still fires (never silent).

        The suite's default structlog wrapper (tests/conftest.py) filters
        below WARNING, matching the default runtime -- exactly the
        visibility change this fix is for. Raise the wrapper threshold to
        DEBUG for this test only so the (correctly suppressed-by-default)
        debug entry is observable; the autouse
        ``_restore_structlog_after_test`` fixture resets it after.
        """
        import logging

        import structlog
        from structlog.testing import capture_logs

        structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG))

        good_cols = [f"code__c{i}" for i in range(20)]
        t3 = _FailingT3(
            {c: [] for c in good_cols},
            failing={"knowledge__orphan__minilm-l6-v2-384__v1"},
        )
        with capture_logs() as logs:
            search_cross_corpus(
                "q", [*good_cols, "knowledge__orphan__minilm-l6-v2-384__v1"],
                10, t3,
            )
        matches = [
            e for e in logs
            if e["event"] == "collection_search_failed"
            and e.get("collection") == "knowledge__orphan__minilm-l6-v2-384__v1"
        ]
        assert matches, "the failure must still be logged -- never silent"
        assert matches[0]["log_level"] == "debug"

    def test_large_fraction_dimension_mismatch_stays_warning(self):
        """40 of 80 collections mismatched (50%) is a real problem --
        stays at WARNING, per the AC3 example."""
        from structlog.testing import capture_logs

        good_cols = [f"code__c{i}" for i in range(40)]
        bad_cols = [f"knowledge__orphan{i}__minilm-l6-v2-384__v1" for i in range(40)]
        t3 = _FailingT3(
            {c: [] for c in good_cols}, failing=set(bad_cols),
        )
        with capture_logs() as logs:
            search_cross_corpus("q", [*good_cols, *bad_cols], 10, t3)
        levels = {
            e["log_level"] for e in logs
            if e["event"] == "collection_search_failed" and e.get("collection") in bad_cols
        }
        assert levels == {"warning"}

    def test_non_dimension_failure_unaffected_stays_warning(self):
        """A genuine (non-dimension) service failure keeps its immediate
        per-collection WARNING regardless of how small a fraction it is --
        only the dimension-mismatch class is reclassified."""
        from structlog.testing import capture_logs

        good_cols = [f"code__c{i}" for i in range(20)]
        t3 = _FailingT3({c: [] for c in good_cols}, failing=set())
        # Monkeypatch one collection to raise a non-dimension error.
        from nexus.db.http_vector_client import VectorServiceError

        real_search = t3.search

        def _search(query, collection_names, n_results=10, where=None):
            if collection_names[0] == "code__c0":
                raise VectorServiceError("POST /v1/vectors/search -> HTTP 503: service unavailable")
            return real_search(query, collection_names, n_results, where)

        t3.search = _search
        with capture_logs() as logs:
            search_cross_corpus("q", good_cols, 10, t3)
        matches = [
            e for e in logs
            if e["event"] == "collection_search_failed" and e.get("collection") == "code__c0"
        ]
        assert matches
        assert matches[0]["log_level"] == "warning"


# ── nexus-7lm3q: batch manifest/resolve, backward-compat fallback ────────────


class TestAttachDocIdsBatchManifest:
    """nexus-7lm3q: _attach_doc_ids_from_catalog uses get_manifests() batch
    when available, falls back to per-doc get_manifest() loop when the
    catalog predates the new method."""

    def _result(self, *, chunk_text_hash: str = "", doc_id: str = "") -> SearchResult:
        meta: dict = {}
        if chunk_text_hash:
            meta["chunk_text_hash"] = chunk_text_hash
        if doc_id:
            meta["doc_id"] = doc_id
        return SearchResult(
            id=f"r-{chunk_text_hash[:8] or doc_id or 'x'}",
            content="x", distance=0.1, collection="docs__c", metadata=meta,
        )

    def test_batch_path_uses_get_manifests_when_available(self):
        """WITH TEETH: catalog with get_manifests() gets ONE batch call,
        NOT N per-doc get_manifest() calls. Reverting the batch path
        regresses to N calls and this test fails."""
        from nexus.search_engine import _attach_doc_ids_from_catalog

        chash_a = "a" * 64
        chash_b = "b" * 64
        catalog = MagicMock()
        catalog.docs_for_chashes.return_value = {
            chash_a: ["doc-A"],
            chash_b: ["doc-B"],
        }
        # Return one chunk row per doc (position=0, chash matches)
        catalog.get_manifests.return_value = {
            "doc-A": [{"chash": chash_a, "position": 0}],
            "doc-B": [{"chash": chash_b, "position": 0}],
        }
        r_a = self._result(chunk_text_hash=chash_a)
        r_b = self._result(chunk_text_hash=chash_b)
        _attach_doc_ids_from_catalog([r_a, r_b], catalog=catalog)

        # Exactly one batch call, zero per-doc calls.
        catalog.get_manifests.assert_called_once()
        catalog.get_manifest.assert_not_called()

        assert r_a.metadata["chunk_count"] == 1
        assert r_b.metadata["chunk_count"] == 1
        assert r_a.metadata["chunk_index"] == 0
        assert r_b.metadata["chunk_index"] == 0

    def test_fallback_to_per_doc_when_get_manifests_absent(self):
        """Catalog without get_manifests() must fall through to the existing
        per-doc loop. Both old and new backends pass."""
        from nexus.search_engine import _attach_doc_ids_from_catalog

        chash = "c" * 64

        class OldCatalog:
            """Simulates a pre-batch catalog - no get_manifests attribute."""
            def docs_for_chashes(self, chashes):
                return {chash: ["doc-C"]}
            def get_manifest(self, doc_id):
                return [{"chash": chash, "position": 0}]

        r = self._result(chunk_text_hash=chash)
        _attach_doc_ids_from_catalog([r], catalog=OldCatalog())

        assert r.metadata["doc_id"] == "doc-C"
        assert r.metadata["chunk_count"] == 1
        assert r.metadata["chunk_index"] == 0

    def test_batch_failure_does_not_poison_search(self):
        """get_manifests() raising must degrade gracefully (no chunk_count/
        chunk_index stamped) without aborting the search."""
        from nexus.search_engine import _attach_doc_ids_from_catalog

        chash = "d" * 64
        catalog = MagicMock()
        catalog.docs_for_chashes.return_value = {chash: ["doc-D"]}
        catalog.get_manifests.side_effect = RuntimeError("service dead")
        r = self._result(chunk_text_hash=chash)
        _attach_doc_ids_from_catalog([r], catalog=catalog)

        # doc_id should still be injected; chunk_count/chunk_index absent.
        assert r.metadata["doc_id"] == "doc-D"
        assert "chunk_count" not in r.metadata
        assert "chunk_index" not in r.metadata

    def test_legacy_chunk_count_not_overwritten_by_batch(self):
        """Legacy pre-Phase-3 chunks that already carry chunk_count must
        not have it overwritten by the batch manifest path."""
        from nexus.search_engine import _attach_doc_ids_from_catalog

        chash = "e" * 64
        catalog = MagicMock()
        catalog.docs_for_chashes.return_value = {chash: ["doc-E"]}
        catalog.get_manifests.return_value = {
            "doc-E": [{"chash": chash, "position": 0}],
        }
        r = self._result(chunk_text_hash=chash)
        r.metadata["chunk_count"] = 99  # legacy field already set
        _attach_doc_ids_from_catalog([r], catalog=catalog)

        assert r.metadata["chunk_count"] == 99  # must be preserved

    def test_partial_batch_response_handled(self):
        """nexus-7lm3q critic obs: a manifest batch that returns only SOME of
        the requested doc_ids stamps chunk_count for the present ones and
        leaves the absent ones unstamped (no crash, no cross-contamination)."""
        from nexus.search_engine import _attach_doc_ids_from_catalog

        chash_a = "a" * 64
        chash_b = "b" * 64
        catalog = MagicMock()
        catalog.docs_for_chashes.return_value = {
            chash_a: ["doc-A"],
            chash_b: ["doc-B"],
        }
        # Only doc-A comes back; doc-B absent from the batch response.
        catalog.get_manifests.return_value = {
            "doc-A": [{"chash": chash_a, "position": 0}],
        }
        r_a = self._result(chunk_text_hash=chash_a)
        r_b = self._result(chunk_text_hash=chash_b)
        _attach_doc_ids_from_catalog([r_a, r_b], catalog=catalog)

        assert r_a.metadata["chunk_count"] == 1
        assert r_a.metadata["chunk_index"] == 0
        # doc-B still gets its doc_id resolved, but no manifest-derived fields.
        assert r_b.metadata["doc_id"] == "doc-B"
        assert "chunk_count" not in r_b.metadata
        assert "chunk_index" not in r_b.metadata


class TestAttachDisplayPathsBatch:
    """nexus-7lm3q: _attach_display_paths uses resolve_many() batch
    when available, falls back to per-doc by_doc_id() loop when absent."""

    def _result(self, doc_id: str) -> SearchResult:
        return SearchResult(
            id=f"r-{doc_id}", content="x", distance=0.1,
            collection="code__c", metadata={"doc_id": doc_id},
        )

    def test_batch_path_uses_resolve_many_when_available(self):
        """WITH TEETH: catalog with resolve_many() gets ONE call, no
        by_doc_id() calls. Reverting regresses to N calls."""
        from nexus.search_engine import _attach_display_paths

        catalog = MagicMock()
        catalog.resolve_many.return_value = {
            "doc-1": SimpleNamespace(file_path="/a/b/one.py"),
            "doc-2": SimpleNamespace(file_path="/a/b/two.py"),
        }
        r1 = self._result("doc-1")
        r2 = self._result("doc-2")
        _attach_display_paths([r1, r2], catalog=catalog)

        catalog.resolve_many.assert_called_once()
        catalog.by_doc_id.assert_not_called()

        assert r1.metadata["_display_path"] == "/a/b/one.py"
        assert r2.metadata["_display_path"] == "/a/b/two.py"

    def test_fallback_to_by_doc_id_when_resolve_many_absent(self):
        """Catalog without resolve_many() must use per-doc by_doc_id()."""
        from nexus.search_engine import _attach_display_paths

        class OldCatalog:
            def by_doc_id(self, doc_id):
                return SimpleNamespace(file_path=f"/path/{doc_id}.py")

        r = self._result("old-doc")
        _attach_display_paths([r], catalog=OldCatalog())

        assert r.metadata["_display_path"] == "/path/old-doc.py"

    def test_batch_failure_does_not_poison_search(self):
        """resolve_many() raising must degrade gracefully."""
        from nexus.search_engine import _attach_display_paths

        catalog = MagicMock()
        catalog.resolve_many.side_effect = RuntimeError("service dead")
        r = self._result("doc-X")
        _attach_display_paths([r], catalog=catalog)

        assert "_display_path" not in r.metadata

    def test_partial_batch_response_handled(self):
        """resolve_many() may return fewer entries than requested
        (missing doc_ids). Results with no entry remain unpopulated."""
        from nexus.search_engine import _attach_display_paths

        catalog = MagicMock()
        catalog.resolve_many.return_value = {
            "doc-found": SimpleNamespace(file_path="/found.py"),
            # doc-missing intentionally absent
        }
        r_found = self._result("doc-found")
        r_missing = self._result("doc-missing")
        _attach_display_paths([r_found, r_missing], catalog=catalog)

        assert r_found.metadata["_display_path"] == "/found.py"
        assert "_display_path" not in r_missing.metadata

    def test_multi_chunk_results_all_populated(self):
        """Multiple results with the same doc_id all get the display path."""
        from nexus.search_engine import _attach_display_paths

        catalog = MagicMock()
        catalog.resolve_many.return_value = {
            "shared-doc": SimpleNamespace(file_path="/shared.py"),
        }
        results = [self._result("shared-doc") for _ in range(3)]
        _attach_display_paths(results, catalog=catalog)

        catalog.resolve_many.assert_called_once()
        for r in results:
            assert r.metadata["_display_path"] == "/shared.py"


# ── nexus-h8rf6.9: threshold gate in service mode ────────────────────────────


class TestThresholdGateServiceMode:
    """RDR-188 P3.2 (nexus-9o6y2.14, supersedes the nexus-h8rf6.9 client-key
    heuristic): in service mode the ``apply_thresholds`` gate consults the
    SERVER's reported embedder family (``HttpVectorClient.embedding_mode()``
    from GET /version) — thresholds on iff the engine embeds with Voyage.
    The client's voyage credential has ZERO influence (Gap 3: removing the
    now-unconsumed key must not silently regress filtering). Unknown mode
    (probe failed) → thresholds off, never guess Voyage. Non-HttpVectorClient
    handles (test fakes, injected stubs) keep the attribute-only gate.
    """

    _ROWS = {
        "code__nexus": [
            {"id": "a", "content": "keep", "distance": 0.30},
            {"id": "b", "content": "drop", "distance": 0.50},
        ],
    }

    def _service_t3(self, monkeypatch, mode):
        from nexus.db.http_vector_client import HttpVectorClient
        client = HttpVectorClient()
        monkeypatch.setattr(
            client.__class__, "search",
            lambda self, query, collection_names, n_results=10, where=None:
                TestThresholdGateServiceMode._ROWS.get(collection_names[0], []),
        )
        monkeypatch.setattr(client.__class__, "embedding_mode", lambda self: mode)
        return client

    def test_server_voyage_mode_filters(self, monkeypatch):
        t3 = self._service_t3(monkeypatch, "voyage")
        results = search_cross_corpus("test", ["code__nexus"], 10, t3)
        assert {r.id for r in results} == {"a"}  # 0.50 > code threshold 0.45

    def test_server_onnx_local_mode_skips_filtering(self, monkeypatch):
        # The engine embeds bge locally — Voyage-calibrated thresholds stay
        # off REGARDLESS of any client credential state.
        monkeypatch.setenv("VOYAGE_API_KEY", "sk-configured-but-irrelevant")
        t3 = self._service_t3(monkeypatch, "onnx-local")
        results = search_cross_corpus("test", ["code__nexus"], 10, t3)
        assert {r.id for r in results} == {"a", "b"}

    def test_unknown_mode_skips_filtering(self, monkeypatch):
        # /version unreachable → unknown, not "voyage": thresholds off.
        t3 = self._service_t3(monkeypatch, None)
        results = search_cross_corpus("test", ["code__nexus"], 10, t3)
        assert {r.id for r in results} == {"a", "b"}

    def test_non_service_fake_keeps_attribute_gate(self):
        # Regression guard for the existing unit-test contract: a fake t3
        # without _voyage_client (and not an HttpVectorClient) stays unfiltered.
        t3 = _ThresholdFakeT3(self._ROWS, voyage=False)
        results = search_cross_corpus("test", ["code__nexus"], 10, t3)
        assert {r.id for r in results} == {"a", "b"}
