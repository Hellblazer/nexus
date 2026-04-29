# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-q5yt (RDR-090 P1.6): bench harness scaffold tests.

Three layers:

  * Pure-function tests for metrics (``ndcg_at_k``, ``dedupe_by_doc``,
    ``grade_for_path``, ``multi_hop_precision``). These pin the
    arithmetic against hand-computed values lifted from the spike.
  * YAML schema tests for ``Query`` loading.
  * Runner orchestration tests with mocked path handlers — the live
    path-A/B/C calls are exercised by the spike script and the
    integration test marked ``@pytest.mark.integration``; the unit
    suite asserts the dispatch + report-writing shape.

The metrics module is the durable home for what the spike kept inline.
The spike script is intentionally not refactored to depend on it — it's
a frozen artifact pinning the gate decision; the harness is the thing
that goes forward.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import pytest


# ── metrics ─────────────────────────────────────────────────────────────────


class TestDedupeByDoc:
    def test_collapses_same_source_path_first_wins(self) -> None:
        from bench.metrics import dedupe_by_doc

        chunks = [
            {"id": "c1", "source_path": "/a/rdr-066.md"},
            {"id": "c2", "source_path": "/a/rdr-066.md"},
            {"id": "c3", "source_path": "/a/rdr-067.md"},
        ]
        out = dedupe_by_doc(chunks)
        assert [c["id"] for c in out] == ["c1", "c3"]

    def test_preserves_order(self) -> None:
        from bench.metrics import dedupe_by_doc

        chunks = [
            {"id": f"c{i}", "source_path": f"/x/{name}"}
            for i, name in enumerate(["a", "b", "a", "c", "b"])
        ]
        out = dedupe_by_doc(chunks)
        assert [c["id"] for c in out] == ["c0", "c1", "c3"]

    def test_empty_source_path_treated_as_distinct(self) -> None:
        """Empty source_path entries collapse together (one slot for unknowns)."""
        from bench.metrics import dedupe_by_doc

        chunks = [
            {"id": "c1", "source_path": ""},
            {"id": "c2", "source_path": ""},
            {"id": "c3", "source_path": "/x/known.md"},
        ]
        out = dedupe_by_doc(chunks)
        assert [c["id"] for c in out] == ["c1", "c3"]


class TestGradeForPath:
    def test_returns_highest_matching_gt_grade(self) -> None:
        from bench.metrics import grade_for_path

        gt = {"rdr-049-": 3, "rdr-053-": 1}
        assert grade_for_path("/abs/docs/rdr/rdr-049-tumblers.md", gt) == 3
        assert grade_for_path("/abs/docs/rdr/rdr-053-fidelity.md", gt) == 1

    def test_picks_max_when_multiple_keys_match(self) -> None:
        """If multiple GT keys substring-match, the highest grade wins."""
        from bench.metrics import grade_for_path

        gt = {"rdr-": 1, "rdr-049-": 3}
        assert grade_for_path("rdr-049-anything.md", gt) == 3

    def test_unmatched_returns_zero(self) -> None:
        from bench.metrics import grade_for_path

        assert grade_for_path("/x/rdr-100-other.md", {"rdr-049-": 3}) == 0
        assert grade_for_path("", {"rdr-049-": 3}) == 0


class TestNdcgAtK:
    def test_ideal_ranking_returns_one(self) -> None:
        from bench.metrics import ndcg_at_k

        gt = {"a": 3, "b": 2, "c": 1}
        # grades[0]=3 (a), grades[1]=2 (b), grades[2]=1 (c) — IDCG order
        assert ndcg_at_k([3, 2, 1], gt, k=3) == pytest.approx(1.0)

    def test_empty_idcg_returns_zero(self) -> None:
        """No relevant docs in GT → NDCG defined as 0.0."""
        from bench.metrics import ndcg_at_k

        assert ndcg_at_k([0, 0, 0], gt={}, k=3) == 0.0

    def test_misranked_drops_below_one(self) -> None:
        from bench.metrics import ndcg_at_k

        gt = {"a": 3, "b": 2, "c": 1}
        # Reverse order should be < 1.0
        assert ndcg_at_k([1, 2, 3], gt, k=3) < 1.0

    def test_matches_spike_q1_value(self) -> None:
        """Q1 in the spike scored NDCG@3 = 0.9173194... with grades [3, 0, 0]
        against GT {rdr-049-: 3, rdr-053-: 1}. Pin the arithmetic."""
        from bench.metrics import ndcg_at_k

        gt = {"rdr-049-": 3, "rdr-053-": 1}
        # DCG = (2^3 - 1) / log2(2) + 0 + 0 = 7
        # IDCG = 7 / log2(2) + (2^1 - 1) / log2(3) = 7 + 0.6309... = 7.6309...
        # NDCG = 7 / 7.6309... = 0.9173...
        result = ndcg_at_k([3, 0, 0], gt, k=3)
        assert result == pytest.approx(0.9173194127, rel=1e-6)

    def test_truncates_to_k(self) -> None:
        from bench.metrics import ndcg_at_k

        gt = {"a": 3, "b": 3, "c": 3, "d": 3}
        # First 3 grades are all 3; idcg also takes first 3.
        assert ndcg_at_k([3, 3, 3, 3, 3], gt, k=3) == pytest.approx(1.0)


class TestMultiHopPrecision:
    def test_all_required_retrieved(self) -> None:
        from bench.metrics import multi_hop_precision

        gt = {"rdr-070-": 3, "rdr-095-": 3, "rdr-089-": 3}
        retrieved_paths = ["/x/rdr-070-x.md", "/x/rdr-095-y.md", "/x/rdr-089-z.md"]
        assert multi_hop_precision(retrieved_paths, gt) == pytest.approx(1.0)

    def test_partial_retrieval(self) -> None:
        from bench.metrics import multi_hop_precision

        gt = {"rdr-070-": 3, "rdr-095-": 3, "rdr-089-": 3}
        retrieved_paths = ["/x/rdr-070-x.md", "/x/rdr-100-other.md"]
        # 1 of 3 required keys hit → 1/3
        assert multi_hop_precision(retrieved_paths, gt) == pytest.approx(1.0 / 3.0)

    def test_only_high_grade_keys_count(self) -> None:
        """Multi-hop precision counts keys with grade>=2 as 'required'.

        Adjacent (grade=1) keys are not part of the multi-hop chain.
        """
        from bench.metrics import multi_hop_precision

        gt = {"rdr-049-": 3, "rdr-053-": 1}  # rdr-053- is adjacent, not required
        retrieved_paths = ["/x/rdr-049-x.md"]
        # 1 of 1 required keys hit
        assert multi_hop_precision(retrieved_paths, gt) == pytest.approx(1.0)

    def test_no_required_returns_none(self) -> None:
        """When GT has no high-grade keys, the metric is undefined → None."""
        from bench.metrics import multi_hop_precision

        assert multi_hop_precision(["/x/anything.md"], {"rdr-": 1}) is None
        assert multi_hop_precision(["/x/anything.md"], {}) is None


# ── schema ─────────────────────────────────────────────────────────────────


class TestQuerySchema:
    def test_load_queries_from_yaml(self, tmp_path: Path) -> None:
        from bench.schema import load_queries

        yaml_path = tmp_path / "q.yaml"
        yaml_path.write_text(
            """\
queries:
  - qid: Q1
    category: factual
    text: "Which RDR introduced X?"
    ground_truth:
      "rdr-049-": 3
"""
        )
        qs = load_queries(yaml_path)
        assert len(qs) == 1
        assert qs[0].qid == "Q1"
        assert qs[0].category == "factual"
        assert qs[0].text == "Which RDR introduced X?"
        assert qs[0].ground_truth == {"rdr-049-": 3}

    def test_load_queries_validates_required_fields(self, tmp_path: Path) -> None:
        from bench.schema import load_queries

        yaml_path = tmp_path / "q.yaml"
        yaml_path.write_text(
            """\
queries:
  - qid: Q1
    text: "missing category"
"""
        )
        with pytest.raises(ValueError, match="category"):
            load_queries(yaml_path)

    def test_load_queries_rejects_unknown_category(self, tmp_path: Path) -> None:
        from bench.schema import load_queries

        yaml_path = tmp_path / "q.yaml"
        yaml_path.write_text(
            """\
queries:
  - qid: Q1
    category: bogus
    text: "x"
"""
        )
        with pytest.raises(ValueError, match="category"):
            load_queries(yaml_path)


# ── runner ──────────────────────────────────────────────────────────────────


class TestRunBench:
    def test_calls_each_path_once_per_query(self) -> None:
        """The dispatcher calls each registered path handler once per query."""
        from bench.runner import run_bench
        from bench.schema import Query

        calls: list[tuple[str, str]] = []

        def make_handler(label: str):
            def _h(q: Query) -> dict:
                calls.append((label, q.qid))
                return {
                    "path": label, "qid": q.qid, "elapsed_s": 0.01,
                    "error": None, "chunks": [], "grades": [], "ndcg_at_3": 0.0,
                }
            return _h

        queries = [
            Query(qid="Q1", category="factual", text="x"),
            Query(qid="Q2", category="factual", text="y"),
        ]
        report = run_bench(
            queries,
            handlers={"A": make_handler("A"), "B": make_handler("B")},
        )
        assert sorted(calls) == [("A", "Q1"), ("A", "Q2"), ("B", "Q1"), ("B", "Q2")]
        assert "by_path" in report
        assert set(report["by_path"].keys()) == {"A", "B"}

    def test_report_aggregates_per_path_means(self) -> None:
        from bench.runner import run_bench
        from bench.schema import Query

        def handler_a(q: Query) -> dict:
            scores = {"Q1": 1.0, "Q2": 0.5}
            return {
                "path": "A", "qid": q.qid, "elapsed_s": 0.1,
                "error": None, "chunks": [], "grades": [],
                "ndcg_at_3": scores[q.qid],
            }

        queries = [
            Query(qid="Q1", category="factual", text="x"),
            Query(qid="Q2", category="factual", text="y"),
        ]
        report = run_bench(queries, handlers={"A": handler_a})
        assert report["by_path"]["A"]["mean_ndcg_at_3"] == pytest.approx(0.75)
        assert report["by_path"]["A"]["errors"] == 0
        assert report["by_path"]["A"]["n"] == 2

    def test_report_aggregates_multi_hop_for_compositional(self) -> None:
        """Compositional queries get a multi-hop precision aggregate."""
        from bench.runner import run_bench
        from bench.schema import Query

        def handler_a(q: Query) -> dict:
            # Mock that retrieves all required docs for Q1, none for Q2.
            paths = (
                ["/x/rdr-070-y.md", "/x/rdr-095-z.md", "/x/rdr-089-w.md"]
                if q.qid == "Q1"
                else []
            )
            return {
                "path": "A", "qid": q.qid, "elapsed_s": 0.1,
                "error": None,
                "chunks": [{"source_path": p} for p in paths],
                "grades": [], "ndcg_at_3": 0.0,
            }

        queries = [
            Query(
                qid="Q1", category="compositional", text="x",
                ground_truth={"rdr-070-": 3, "rdr-095-": 3, "rdr-089-": 3},
            ),
            Query(
                qid="Q2", category="compositional", text="y",
                ground_truth={"rdr-070-": 3, "rdr-095-": 3},
            ),
        ]
        report = run_bench(queries, handlers={"A": handler_a})
        # Q1: 1.0, Q2: 0.0 → mean 0.5
        mhp = report["by_path"]["A"]["mean_multi_hop_precision"]
        assert mhp == pytest.approx(0.5)

    def test_report_writes_stable_json(self, tmp_path: Path) -> None:
        from bench.runner import run_bench, write_report
        from bench.schema import Query

        def handler_a(q: Query) -> dict:
            return {
                "path": "A", "qid": q.qid, "elapsed_s": 0.1, "error": None,
                "chunks": [], "grades": [], "ndcg_at_3": 0.5,
            }

        queries = [Query(qid="Q1", category="factual", text="x")]
        report = run_bench(queries, handlers={"A": handler_a})
        out = tmp_path / "report.json"
        write_report(report, out)
        loaded = json.loads(out.read_text())
        # Stable top-level keys (sorted ensures deterministic diffs).
        assert set(loaded.keys()) >= {
            "queries", "k", "by_path", "by_category", "rows",
        }

    def test_handler_error_recorded_not_raised(self) -> None:
        """A handler that raises is captured into the row's error field."""
        from bench.runner import run_bench
        from bench.schema import Query

        def boom(q: Query) -> dict:
            raise RuntimeError("nope")

        queries = [Query(qid="Q1", category="factual", text="x")]
        report = run_bench(queries, handlers={"A": boom})
        rows = [r for r in report["rows"] if r["path"] == "A"]
        assert len(rows) == 1
        assert "nope" in (rows[0].get("error") or "")
        assert report["by_path"]["A"]["errors"] == 1


class TestSpike5qFixture:
    """The spike's 5 queries, expressed as YAML, must round-trip."""

    def test_spike_5q_yaml_loads(self) -> None:
        from bench.schema import load_queries

        path = Path("bench/queries/spike_5q.yaml")
        if not path.exists():
            pytest.skip(f"{path} not yet shipped")
        qs = load_queries(path)
        assert len(qs) == 5
        qids = {q.qid for q in qs}
        assert qids == {
            "Q1-factual-tumblers",
            "Q2-factual-chash",
            "Q3-factual-taxonomy",
            "Q4-comparative-hooks",
            "Q5-compositional-retrieval",
        }
        # At least one compositional + one comparative query.
        cats = {q.category for q in qs}
        assert {"factual", "comparative", "compositional"} <= cats
