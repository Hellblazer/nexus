# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for per-corpus distance thresholds (RDR-056 Phase 1c)."""
from __future__ import annotations

from nexus.search_engine import _threshold_for_collection, search_cross_corpus
from nexus.types import SearchResult


class TestThresholdForCollection:
    def test_code_collection_threshold(self) -> None:
        cfg = {"search": {"distance_threshold": {"code": 0.45}}}
        assert _threshold_for_collection("code__nexus", cfg) == 0.45

    def test_knowledge_collection_threshold(self) -> None:
        cfg = {"search": {"distance_threshold": {"knowledge": 0.65}}}
        assert _threshold_for_collection("knowledge__papers", cfg) == 0.65

    def test_docs_collection_threshold(self) -> None:
        cfg = {"search": {"distance_threshold": {"docs": 0.65}}}
        assert _threshold_for_collection("docs__corpus", cfg) == 0.65

    def test_rdr_collection_threshold(self) -> None:
        cfg = {"search": {"distance_threshold": {"rdr": 0.65}}}
        assert _threshold_for_collection("rdr__reviews", cfg) == 0.65

    def test_default_threshold_for_unknown_prefix(self) -> None:
        cfg = {"search": {"distance_threshold": {"default": 0.55}}}
        assert _threshold_for_collection("custom__stuff", cfg) == 0.55

    def test_none_when_no_threshold_config(self) -> None:
        cfg: dict = {"search": {}}
        assert _threshold_for_collection("code__nexus", cfg) is None

    def test_uses_default_config_when_not_overridden(self) -> None:
        from nexus.config import load_config
        cfg = load_config()
        assert _threshold_for_collection("code__nexus", cfg) == 0.45
        assert _threshold_for_collection("knowledge__x", cfg) == 0.65
        assert _threshold_for_collection("docs__x", cfg) == 0.65
        assert _threshold_for_collection("rdr__x", cfg) == 0.65
        assert _threshold_for_collection("other__x", cfg) == 0.55


class TestSearchCrossCorpusThresholdFiltering:
    """Tests that search_cross_corpus filters results exceeding thresholds."""

    class _FakeT3:
        _voyage_client = "fake-voyage"  # Enables threshold filtering

        def __init__(self, results_by_col: dict[str, list[dict]]) -> None:
            self._results = results_by_col

        def search(self, query, collection_names, n_results=10, where=None):
            return self._results.get(collection_names[0], [])

    def test_code_result_above_threshold_filtered(self) -> None:
        """code__nexus result with distance=0.50 is filtered (>0.45)."""
        t3 = self._FakeT3({
            "code__nexus": [
                {"id": "a", "content": "good", "distance": 0.30},
                {"id": "b", "content": "noise", "distance": 0.50},
            ],
        })
        results = search_cross_corpus("test", ["code__nexus"], 10, t3)
        assert len(results) == 1
        assert results[0].id == "a"

    def test_knowledge_result_below_threshold_passes(self) -> None:
        """knowledge__papers result with distance=0.60 passes (<=0.65)."""
        t3 = self._FakeT3({
            "knowledge__papers": [
                {"id": "a", "content": "relevant", "distance": 0.60},
            ],
        })
        results = search_cross_corpus("test", ["knowledge__papers"], 10, t3)
        assert len(results) == 1

    def test_knowledge_result_above_threshold_filtered(self) -> None:
        """knowledge__papers result with distance=0.70 is filtered (>0.65)."""
        t3 = self._FakeT3({
            "knowledge__papers": [
                {"id": "a", "content": "noise", "distance": 0.70},
            ],
        })
        results = search_cross_corpus("test", ["knowledge__papers"], 10, t3)
        assert len(results) == 0

    def test_cross_corpus_default_threshold(self) -> None:
        """Unknown prefix uses default threshold (0.55)."""
        t3 = self._FakeT3({
            "custom__stuff": [
                {"id": "a", "content": "ok", "distance": 0.52},
                {"id": "b", "content": "noise", "distance": 0.60},
            ],
        })
        results = search_cross_corpus("test", ["custom__stuff"], 10, t3)
        assert len(results) == 1
        assert results[0].id == "a"

    def test_at_threshold_passes(self) -> None:
        """Result exactly at threshold passes (<=, not <)."""
        t3 = self._FakeT3({
            "code__nexus": [
                {"id": "a", "content": "edge", "distance": 0.45},
            ],
        })
        results = search_cross_corpus("test", ["code__nexus"], 10, t3)
        assert len(results) == 1

    def test_non_voyage_skips_thresholds(self) -> None:
        """Non-Voyage embeddings (ONNX MiniLM) skip Voyage-calibrated thresholds."""
        class _NonVoyageT3:
            _voyage_client = None
            def search(self, query, collection_names, n_results=10, where=None):
                return [{"id": "a", "content": "x", "distance": 0.90}]
        results = search_cross_corpus("test", ["code__nexus"], 10, _NonVoyageT3())
        assert len(results) == 1  # 0.90 > 0.45 but NOT filtered without Voyage

    def test_multi_corpus_applies_per_corpus_threshold(self) -> None:
        """Different thresholds applied per corpus in cross-corpus search."""
        t3 = self._FakeT3({
            "code__nexus": [
                {"id": "c1", "content": "code", "distance": 0.40},
                {"id": "c2", "content": "code noise", "distance": 0.48},
            ],
            "knowledge__papers": [
                {"id": "k1", "content": "knowledge", "distance": 0.60},
                {"id": "k2", "content": "know noise", "distance": 0.70},
            ],
        })
        results = search_cross_corpus(
            "test", ["code__nexus", "knowledge__papers"], 10, t3,
        )
        ids = {r.id for r in results}
        assert ids == {"c1", "k1"}


class TestNoResultsMessage:
    """nexus-uro6c (5.1.0 shakeout): a zero-hit search must surface a
    threshold drop instead of an undifferentiated 'No results.'."""

    def test_footer_when_all_candidates_dropped(self) -> None:
        from nexus.mcp.core import _no_results_message
        from nexus.search_engine import SearchDiagnostics

        # knowledge__papers: raw=3, all 3 dropped, threshold 0.65, closest
        # dropped candidate at distance 0.5515 (the shakeout's recovered match).
        diag = SearchDiagnostics(
            per_collection={"knowledge__papers": (3, 3, 0.65, 0.5515)},
            total_dropped=3,
            total_raw=3,
        )
        msg = _no_results_message([diag])
        assert "0.5515" in msg                 # closest dropped distance
        assert "0.6500" in msg                 # the blocking threshold
        assert "knowledge__papers" in msg      # which collection
        assert "threshold=0.60" in msg         # 0.5515 + 0.05 relax hint

    def test_plain_when_no_diagnostics(self) -> None:
        from nexus.mcp.core import _no_results_message

        assert _no_results_message([]) == "No results."

    def test_plain_when_nothing_dropped(self) -> None:
        from nexus.mcp.core import _no_results_message
        from nexus.search_engine import SearchDiagnostics

        # raw=0 => genuine miss, no candidate to surface => base message.
        diag = SearchDiagnostics(
            per_collection={"code__x": (0, 0, 0.45, None)},
        )
        assert _no_results_message([diag]) == "No results."


class TestStructuredNoResultsCarriesDiagnostic:
    """nexus-1obui: ``structured=True`` used to return bare empty lists on a
    zero-hit and DISCARD the uro6c diagnostic, so every programmatic consumer
    (nx_tidy, the plan-runner step-output contract) could not tell a threshold
    drop from a genuinely empty topic. Measured 2026-08-27: nx_tidy reported
    "nothing to consolidate" on a topic whose two nearest entries had been
    dropped at 0.629 and 0.645 and which a prose search returned."""

    def test_threshold_drop_is_visible_and_machine_readable(self) -> None:
        from nexus.mcp.core import _structured_no_results
        from nexus.search_engine import SearchDiagnostics

        diag = SearchDiagnostics(
            per_collection={"knowledge__papers": (3, 3, 0.65, 0.5515)},
            total_dropped=3, total_raw=3,
        )
        out = _structured_no_results([diag])
        # The six original keys keep their exact shape — additive change only.
        for k in ("ids", "tumblers", "distances", "collections",
                  "chunk_collections", "chunk_text_hash"):
            assert out[k] == [], f"{k} must stay an empty list"
        # A caller must not have to parse prose to branch on this.
        assert out["threshold_dropped"] is True
        assert out["closest_dropped"]["collection"] == "knowledge__papers"
        assert out["closest_dropped"]["distance"] == 0.5515
        assert out["closest_dropped"]["threshold"] == 0.65
        assert out["closest_dropped"]["retry_threshold"] == 0.60
        assert "0.5515" in out["no_results_reason"]

    def test_genuine_miss_is_distinguishable_from_a_drop(self) -> None:
        from nexus.mcp.core import _structured_no_results
        from nexus.search_engine import SearchDiagnostics

        diag = SearchDiagnostics(per_collection={"code__x": (0, 0, 0.45, None)})
        out = _structured_no_results([diag])
        assert out["threshold_dropped"] is False
        assert "closest_dropped" not in out
        assert out["no_results_reason"] == "No results."

    def test_offset_base_message_is_preserved(self) -> None:
        from nexus.mcp.core import _structured_no_results

        out = _structured_no_results([], base="No results at offset 50 (total 3).")
        assert out["no_results_reason"] == "No results at offset 50 (total 3)."
        assert out["threshold_dropped"] is False


class TestTidyPrefetchNamesItsFailures:
    """nexus-1obui: _tidy_prefetch collapsed six outcomes to ("", 0) and the
    caller rendered all six as "nothing to consolidate" — so a T3 outage was
    indistinguishable from an empty topic. Exactly one branch is a genuine
    miss; every other branch must refuse that framing."""

    def _prefetch(self, monkeypatch, *, hits=None, exc=None):
        from nexus.mcp import core

        def fake_search(**_kw):
            if exc is not None:
                raise exc
            return hits
        monkeypatch.setattr(core, "search", fake_search)
        return core._tidy_prefetch("some topic", "knowledge")

    def test_threshold_drop_is_not_a_genuine_miss(self, monkeypatch) -> None:
        block, n, reason, genuine = self._prefetch(monkeypatch, hits={
            "ids": [], "threshold_dropped": True,
            "no_results_reason": "No results. Closest candidate was dropped "
                                 "at distance 0.6290 ...",
        })
        assert (block, n) == ("", 0)
        assert genuine is False, "a drop must never read as an empty collection"
        assert "THRESHOLD DROPPED" in reason
        assert "0.6290" in reason

    def test_empty_collection_is_a_genuine_miss(self, monkeypatch) -> None:
        block, n, reason, genuine = self._prefetch(monkeypatch, hits={
            "ids": [], "threshold_dropped": False,
            "no_results_reason": "No results.",
        })
        assert (block, n) == ("", 0)
        assert genuine is True
        assert "THRESHOLD DROPPED" not in reason

    def test_search_failure_is_not_a_genuine_miss(self, monkeypatch) -> None:
        block, n, reason, genuine = self._prefetch(
            monkeypatch, exc=RuntimeError("T3 unreachable"),
        )
        assert (block, n) == ("", 0)
        assert genuine is False
        assert "retrieval FAILED" in reason
        assert "T3 unreachable" in reason

    def test_hydration_failure_is_not_a_genuine_miss(self, monkeypatch) -> None:
        from nexus.mcp import core

        monkeypatch.setattr(core, "search", lambda **_k: {
            "ids": ["a", "b"], "chunk_collections": ["c", "c"],
        })

        def boom(*_a, **_k):
            raise RuntimeError("hydrate exploded")
        monkeypatch.setattr(core, "store_get_many", boom)
        block, n, reason, genuine = core._tidy_prefetch("t", "knowledge")
        assert (block, n) == ("", 0)
        assert genuine is False, "an outage must not read as an empty collection"
        assert "HYDRATION FAILED" in reason
        assert "matched 2 entries" in reason

    def test_successful_retrieval_reports_no_reason(self, monkeypatch) -> None:
        from nexus.mcp import core

        monkeypatch.setattr(core, "search", lambda **_k: {
            "ids": ["a"], "chunk_collections": ["c"],
        })
        monkeypatch.setattr(core, "store_get_many", lambda *_a, **_k: {
            "contents": ["real body text"],
        })
        block, n, reason, genuine = core._tidy_prefetch("t", "knowledge")
        assert n == 1
        assert "real body text" in block
        assert reason == ""
        assert genuine is False


class TestNoResultsMessageFailedCollections:
    """nexus-pebfx.8: a zero-hit where collections were excluded by service
    errors must say so — otherwise a partial outage reads as a genuine miss."""

    def test_failed_collections_noted_when_nothing_dropped(self) -> None:
        from nexus.mcp.core import _no_results_message
        from nexus.search_engine import SearchDiagnostics

        diag = SearchDiagnostics(
            per_collection={"code__x": (0, 0, 0.45, None)},
            failed_collections={
                "knowledge__seam": "POST /v1/vectors/search → HTTP 400: dim mismatch",
            },
        )
        msg = _no_results_message([diag])
        assert msg.startswith("No results.")
        assert "1 collection(s) were excluded" in msg
        assert "knowledge__seam" in msg
        assert "HTTP 400" in msg

    def test_failed_collections_appended_to_threshold_note(self) -> None:
        from nexus.mcp.core import _no_results_message
        from nexus.search_engine import SearchDiagnostics

        diag = SearchDiagnostics(
            per_collection={"knowledge__papers": (3, 3, 0.65, 0.5515)},
            total_dropped=3,
            total_raw=3,
            failed_collections={"knowledge__seam": "HTTP 400: dim mismatch"},
        )
        msg = _no_results_message([diag])
        assert "0.5515" in msg
        assert "knowledge__seam" in msg

    def test_no_suffix_without_failures(self) -> None:
        from nexus.mcp.core import _no_results_message
        from nexus.search_engine import SearchDiagnostics

        diag = SearchDiagnostics(per_collection={"code__x": (0, 0, 0.45, None)})
        assert _no_results_message([diag]) == "No results."
