"""AC1–AC8: Search engine — hybrid scoring, reranking, output formatters."""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from nexus.formatters import format_json, format_vimgrep
from nexus.scoring import (
    apply_hybrid_scoring,
    hybrid_score,
    min_max_normalize,
    rerank_results,
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

def test_rerank_results_returns_unified_ranking():
    """rerank_results reorders results using the reranker model."""
    results = [
        SearchResult(id="1", content="alpha", distance=0.5, collection="code__r", metadata={}),
        SearchResult(id="2", content="beta", distance=0.2, collection="docs__d", metadata={}),
        SearchResult(id="3", content="gamma", distance=0.8, collection="knowledge__k", metadata={}),
    ]
    mock_client = MagicMock()
    mock_client.rerank.return_value = MagicMock(
        results=[
            MagicMock(index=2, relevance_score=0.9),
            MagicMock(index=0, relevance_score=0.7),
            MagicMock(index=1, relevance_score=0.3),
        ]
    )
    with patch("nexus.scoring._voyage_client", return_value=mock_client):
        reranked = rerank_results(results, query="test", model="rerank-2.5", top_k=3)
    assert reranked[0].id == "3"
    assert reranked[1].id == "1"
    assert reranked[2].id == "2"


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
        write or routes through the wrong key fails this test."""
        catalog = MagicMock()
        catalog.by_doc_id.return_value = SimpleNamespace(
            file_path="/abs/from/catalog.py",
        )
        r = self._result(doc_id="ART-deadbeef", source_path="src/legacy.py")
        _attach_display_paths([r], catalog=catalog)
        catalog.by_doc_id.assert_called_once_with("ART-deadbeef")
        assert r.metadata["_display_path"] == "/abs/from/catalog.py"
        # source_path stays untouched: the prune verb owns its removal.
        assert r.metadata["source_path"] == "src/legacy.py"

    def test_doc_id_with_no_catalog_entry_leaves_field_unset(self):
        catalog = MagicMock()
        catalog.by_doc_id.return_value = None
        r = self._result(doc_id="ART-orphan", source_path="src/legacy.py")
        _attach_display_paths([r], catalog=catalog)
        assert "_display_path" not in r.metadata

    def test_repeated_doc_id_only_hits_catalog_once(self):
        """Multi-chunk results sharing the same doc_id share one
        catalog lookup. Pre-fix code that loops per result without a
        cache hits the catalog N times (slow on large result sets).
        """
        catalog = MagicMock()
        catalog.by_doc_id.return_value = SimpleNamespace(
            file_path="/abs/foo.py",
        )
        results = [
            self._result(doc_id="ART-shared"),
            self._result(doc_id="ART-shared"),
            self._result(doc_id="ART-shared"),
        ]
        _attach_display_paths(results, catalog=catalog)
        assert catalog.by_doc_id.call_count == 1
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
