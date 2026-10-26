# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-232 Phase 1 (nexus-jypmj.1.15): the RDR-090 bench runner reads the golden set and scores
NDCG@3 on the requests that carry ``ground_truth``.

RDR-232 section 5 says to extend ``scripts/bench`` rather than build a parallel runner and to reuse
the RDR-090 axes where they apply. ``answer_shape_metrics.py`` scores the new answer-shape axes; this
file pins the old axis on the new set: ``runner.py`` loads ``bench/queries/golden_v1.yaml`` through the
strict golden loader, computes ``ndcg_at_k`` from each handler's retrieved chunks for the requests that
have ``ground_truth`` (G01, G02, G03, G09 and G13), and averages only those. A request with no
``ground_truth`` has an empty ideal ranking, so ``ndcg_at_k`` returns 0.0 for it by convention; averaging
those zeros in would read as a retrieval failure on 19 of 24 requests, which is why the report carries a
separate scored mean.

Tenant-free: handlers are fakes, no engine, no model call.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from bench import runner
from bench.schema import GOLDEN_SET_VERSION, Query, load_golden_set

REPO = Path(__file__).resolve().parents[1]
GOLDEN = REPO / "bench" / "queries" / "golden_v1.yaml"

#: The requests the bead names, which must carry ground_truth in the committed set.
NAMED_GT_QIDS = {
    "G01-lookup-tumblers", "G02-lookup-chash", "G03-lookup-taxonomy", "G09-synthesis-retrieval-evolution",
}


def _gt_queries() -> list[Query]:
    return [q for q in load_golden_set(GOLDEN) if q.ground_truth]


def _chunks_for(q: Query, *, best_first: bool = True) -> list[dict]:
    """Chunks whose paths match the request's ground-truth keys, best grade first (or last)."""
    keys = sorted(q.ground_truth, key=lambda k: -q.ground_truth[k])
    if not best_first:
        keys = keys[::-1]
    return [{"source_path": f"/docs/rdr/{k}fixture.md"} for k in keys]


def _ideal_handler(q: Query) -> dict:
    # ndcg_at_3 deliberately 0.0 and grades empty: only the runner's own scoring can produce a 1.0.
    return {"elapsed_s": 0.0, "error": None, "chunks": _chunks_for(q), "grades": [], "ndcg_at_3": 0.0}


def _wrong_handler(q: Query) -> dict:
    return {"elapsed_s": 0.0, "error": None, "chunks": [{"source_path": "/docs/rdr/rdr-999-nothing.md"}],
            "grades": [], "ndcg_at_3": 1.0}


class TestRunnerLoadsTheGoldenSet:
    def test_loader_reads_golden_v1_through_the_strict_loader(self) -> None:
        qs = runner.load_query_file(GOLDEN)
        assert [q.qid for q in qs] == [q.qid for q in load_golden_set(GOLDEN)]
        assert len(qs) >= 20, "non-vacuity: a set this small cannot back a bench"
        assert all(q.answer_kind for q in qs), "the golden labels came through"

    def test_a_golden_file_with_the_wrong_version_is_rejected_by_the_runner(self, tmp_path: Path) -> None:
        # The lenient loader would read this file; only the strict one checks the version stamp.
        doc = yaml.safe_load(GOLDEN.read_text(encoding="utf-8"))
        doc["golden_set_version"] = GOLDEN_SET_VERSION + 1
        bad = tmp_path / "golden_v2.yaml"
        bad.write_text(yaml.safe_dump(doc), encoding="utf-8")
        with pytest.raises(ValueError, match="golden_set_version"):
            runner.load_query_file(bad)

    def test_a_plain_rdr090_file_still_loads_leniently(self) -> None:
        qs = runner.load_query_file(REPO / "bench" / "queries" / "spike_5q.yaml")
        assert len(qs) == 5 and all(q.answer_kind == "" for q in qs)

    def test_the_named_requests_carry_ground_truth(self) -> None:
        have = {q.qid for q in _gt_queries()}
        assert NAMED_GT_QIDS <= have, sorted(NAMED_GT_QIDS - have)
        # G13 also carries ground_truth although the bead names only four; it is scored too.
        assert "G13-comparison-hooks" in have


class TestNdcgOnGroundTruthRequests:
    def test_ideal_retrieval_scores_one_on_every_ground_truth_request_and_only_those(self) -> None:
        qs = runner.load_query_file(GOLDEN)
        report = runner.run_bench(qs, handlers={"A": _ideal_handler})
        gt_qids = {q.qid for q in _gt_queries()}
        scored = {r["qid"] for r in report["rows"] if r["ndcg_scored"]}
        assert scored == gt_qids and len(scored) >= 4
        for r in report["rows"]:
            if r["qid"] in gt_qids:
                assert r["ndcg_at_3"] == pytest.approx(1.0), r["qid"]
                assert r["grades"], "the grades the score came from are in the row"
        summary = report["by_path"]["A"]
        assert summary["ndcg_scored_n"] == len(gt_qids)
        assert summary["mean_ndcg_at_3_scored"] == pytest.approx(1.0)
        # the unscored requests are 0.0 by convention and dilute the all-request mean, which is why
        # the scored mean is reported separately
        assert summary["mean_ndcg_at_3"] == pytest.approx(len(gt_qids) / len(qs))

    def test_wrong_retrieval_scores_zero_even_if_the_handler_claims_otherwise(self) -> None:
        report = runner.run_bench(runner.load_query_file(GOLDEN), handlers={"A": _wrong_handler})
        summary = report["by_path"]["A"]
        assert summary["ndcg_scored_n"] >= 4
        assert summary["mean_ndcg_at_3_scored"] == 0.0

    def test_a_misranked_retrieval_scores_below_an_ideal_one(self) -> None:
        q = next(q for q in _gt_queries() if q.qid == "G01-lookup-tumblers")

        def reversed_handler(_q: Query) -> dict:
            return {"elapsed_s": 0.0, "error": None, "chunks": _chunks_for(q, best_first=False),
                    "grades": [], "ndcg_at_3": 0.0}

        ideal = runner.run_bench([q], handlers={"A": _ideal_handler})["rows"][0]["ndcg_at_3"]
        worse = runner.run_bench([q], handlers={"A": reversed_handler})["rows"][0]["ndcg_at_3"]
        assert ideal == pytest.approx(1.0) and 0.0 < worse < ideal

    def test_an_errored_ground_truth_row_is_not_scored(self) -> None:
        def boom(_q: Query) -> dict:
            raise RuntimeError("service down")

        report = runner.run_bench(_gt_queries(), handlers={"A": boom})
        summary = report["by_path"]["A"]
        assert summary["ndcg_scored_n"] == 0 and summary["mean_ndcg_at_3_scored"] is None
        assert summary["errors"] == len(_gt_queries())

    def test_a_set_with_no_ground_truth_reports_no_scored_mean(self) -> None:
        qs = [q for q in load_golden_set(GOLDEN) if not q.ground_truth]
        summary = runner.run_bench(qs, handlers={"A": _ideal_handler})["by_path"]["A"]
        assert summary["ndcg_scored_n"] == 0 and summary["mean_ndcg_at_3_scored"] is None


class TestMainRunsTheGoldenSet:
    def test_main_loads_golden_v1_and_reports_the_scored_ndcg(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setattr(runner, "_live_handlers", lambda corpus, scope_b: {
            "A": _ideal_handler, "B": _wrong_handler, "C": _ideal_handler})
        out = tmp_path / "report.json"
        assert runner.main([str(GOLDEN), "--out", str(out), "--paths", "A,B"]) == 0
        report = json.loads(out.read_text(encoding="utf-8"))
        assert report["queries"] == len(load_golden_set(GOLDEN))
        assert report["by_path"]["A"]["mean_ndcg_at_3_scored"] == pytest.approx(1.0)
        assert report["by_path"]["B"]["mean_ndcg_at_3_scored"] == 0.0
        text = capsys.readouterr().out
        assert "scored NDCG@3=1.000 (n=5)" in text and "scored NDCG@3=0.000 (n=5)" in text, text


class TestStampIsNotOptional:
    def test_a_golden_file_whose_stamp_was_removed_is_refused(self, tmp_path: Path) -> None:
        doc = yaml.safe_load(GOLDEN.read_text(encoding="utf-8"))
        del doc["golden_set_version"]
        bad = tmp_path / "golden_unstamped.yaml"
        bad.write_text(yaml.safe_dump(doc), encoding="utf-8")
        with pytest.raises(ValueError, match="golden_set_version"):
            runner.load_query_file(bad)

    def test_one_golden_only_field_is_enough_to_refuse_an_unstamped_file(self, tmp_path: Path) -> None:
        for key, value in (("unacceptable_plans", []), ("required_facets", ["f"]), ("answer_kind", "lookup")):
            f = tmp_path / f"{key}.yaml"
            f.write_text(yaml.safe_dump({"queries": [
                {"qid": "Q", "category": "factual", "text": "t", key: value}]}), encoding="utf-8")
            with pytest.raises(ValueError, match=key):
                runner.load_query_file(f)

    def test_non_vacuity_the_stamped_file_still_loads(self) -> None:
        assert len(runner.load_query_file(GOLDEN)) == len(load_golden_set(GOLDEN))


class TestRdr090FileScoresAsBefore:
    """Scoring an existing RDR-090 file through the runner's NDCG step, against a fixed handler whose
    chunks are known, gives the numbers the original arithmetic gives. Hand values: Q1 ground truth
    {rdr-049-: 3, rdr-053-: 1}; chunks [053, 049, other] grade [1, 3, 0]; DCG = 1/log2(2) + 7/log2(3) =
    5.41648; IDCG = 7/log2(2) + 1/log2(3) = 7.63093; NDCG = 0.70981."""

    SPIKE = REPO / "bench" / "queries" / "spike_5q.yaml"

    @staticmethod
    def _handler(q: Query) -> dict:
        keys = sorted(q.ground_truth, key=lambda k: q.ground_truth[k])  # worst grade first
        chunks = [{"source_path": f"/docs/rdr/{k}x.md"} for k in keys] + [{"source_path": "/docs/rdr/other.md"}]
        return {"elapsed_s": 0.0, "error": None, "chunks": chunks[:3], "grades": [], "ndcg_at_3": 0.123}

    def test_q1_matches_the_hand_value_and_the_all_request_mean_is_the_scored_mean(self) -> None:
        qs = runner.load_query_file(self.SPIKE)
        report = runner.run_bench(qs, handlers={"A": self._handler})
        q1 = next(r for r in report["rows"] if r["qid"] == "Q1-factual-tumblers")
        assert q1["grades"] == [1, 3, 0]
        assert q1["ndcg_at_3"] == pytest.approx(0.70981, abs=1e-5)
        s = report["by_path"]["A"]
        # every spike request carries ground_truth, so the old all-request mean and the scored one agree
        assert s["ndcg_scored_n"] == s["n"] == 5
        assert s["mean_ndcg_at_3"] == pytest.approx(s["mean_ndcg_at_3_scored"])
        assert 0.0 < s["mean_ndcg_at_3"] < 1.0
        for cat, by_label in report["by_category"].items():
            assert by_label["A"] == pytest.approx(report["by_category_scored"][cat]["A"]), cat


class TestByCategoryScored:
    def test_scored_category_mean_excludes_the_unscored_zeros(self) -> None:
        qs = runner.load_query_file(GOLDEN)
        report = runner.run_bench(qs, handlers={"A": _ideal_handler})
        gt_cats = {q.category for q in qs if q.ground_truth}
        no_gt_cats = {q.category for q in qs} - gt_cats
        assert gt_cats, "non-vacuity"
        for cat in gt_cats:
            assert report["by_category_scored"][cat]["A"] == pytest.approx(1.0), cat
            n_cat = sum(1 for q in qs if q.category == cat)
            n_gt = sum(1 for q in qs if q.category == cat and q.ground_truth)
            # the all-request category mean is diluted by the categories' unscored requests
            assert report["by_category"][cat]["A"] == pytest.approx(n_gt / n_cat), cat
        for cat in no_gt_cats:
            assert report["by_category_scored"][cat]["A"] is None, cat

    def test_a_category_with_no_ground_truth_is_none_not_zero(self) -> None:
        qs = [q for q in load_golden_set(GOLDEN) if not q.ground_truth]
        report = runner.run_bench(qs, handlers={"A": _ideal_handler})
        for cat in report["by_category_scored"]:
            assert report["by_category_scored"][cat]["A"] is None
