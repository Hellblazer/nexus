# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the pure parts of the RDR-210 P4.3 ranking-measurement harness
(``scripts/spikes/rdr210-p43/``, bead nexus-t189m): chunker determinism, Kendall
tau, the seeded bootstrap, the best-F1 threshold, the inversion counter, pool
building, the token cap, and the fixed query set. No engine, no network, no
tenant. Voyage models appear only as inert tokens.
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import pytest

from nexus.mcp import core
from nexus.scoring import order_by_calibrated_distance
from nexus.types import SearchResult

SPIKE = Path(__file__).resolve().parent.parent / "scripts" / "spikes" / "rdr210-p43"
if str(SPIKE) not in sys.path:
    sys.path.insert(0, str(SPIKE))

import p43_budget as budget  # noqa: E402
import p43_chunker as chunker  # noqa: E402
import p43_corpus as corp  # noqa: E402
import p43_engine as eng  # noqa: E402
import p43_measure_a as ma  # noqa: E402
import p43_measure_b as mb  # noqa: E402
import p43_measure_c as mc  # noqa: E402
import p43_plan as plan_mod  # noqa: E402
import p43_queries as qs  # noqa: E402
import p43_run  # noqa: E402
import p43_results as res  # noqa: E402
import p43_stats as st  # noqa: E402

REPO = SPIKE.parent.parent.parent

SAMPLE = """# Title

First paragraph of the document, a few words long.

Second paragraph, also short.

## Section two

""" + "\n\n".join(f"Paragraph number {i} " + "word " * 60 for i in range(12)) + "\n"


# ── chunker ─────────────────────────────────────────────────────────────────

class TestChunker:
    def test_same_text_same_chunks(self):
        assert chunker.chunk_text(SAMPLE) == chunker.chunk_text(SAMPLE)

    def test_no_chunk_exceeds_the_cap_and_none_is_empty(self):
        chunks = chunker.chunk_text(SAMPLE, 500)
        assert len(chunks) > 3
        assert all(0 < len(c) <= 500 for c in chunks)

    def test_a_heading_starts_a_new_chunk(self):
        chunks = chunker.chunk_text(SAMPLE, 1500)
        assert any(c.startswith("## Section two") for c in chunks)
        assert chunks[0].startswith("# Title")

    def test_one_overlong_line_is_split_by_characters(self):
        chunks = chunker.chunk_text("x" * 3500, 1000)
        assert [len(c) for c in chunks] == [1000, 1000, 1000, 500]

    def test_no_text_is_lost(self):
        text = "alpha\n\nbeta\n\ngamma delta\n"
        joined = " ".join(chunker.chunk_text(text, 12)).split()
        assert joined == ["alpha", "beta", "gamma", "delta"]

    def test_python_defs_start_chunks_and_decorators_stay_with_them(self):
        src = "import os\n\n\n@dec\ndef a():\n    return 1\n\n\ndef b():\n    return 2\n"
        chunks = chunker.chunk_text(src, 1500)
        assert chunks[0] == "import os"
        assert chunks[1].startswith("@dec\ndef a")
        assert chunks[2].startswith("def b")

    def test_whitespace_only_yields_nothing(self):
        assert chunker.chunk_text("  \n\n \n") == []

    def test_cap_must_be_positive(self):
        with pytest.raises(ValueError):
            chunker.chunk_text("a", 0)


# ── Kendall tau ─────────────────────────────────────────────────────────────

class TestKendall:
    def test_identical_is_one(self):
        assert st.kendall_tau_b([1, 2, 3, 4], [10, 20, 30, 40]) == 1.0

    def test_reversed_is_minus_one(self):
        assert st.kendall_tau_b([1, 2, 3, 4], [4, 3, 2, 1]) == -1.0

    def test_one_swap_of_four(self):
        # 6 pairs, 5 concordant, 1 discordant
        assert st.kendall_tau_b([1, 2, 3, 4], [1, 2, 4, 3]) == pytest.approx(4 / 6)

    def test_ties_use_the_tau_b_denominator(self):
        x = [1, 2, 3, 3]
        y = [1, 2, 3, 4]
        # pairs: 5 concordant, 1 tied in x only
        assert st.kendall_tau_b(x, y) == pytest.approx(5 / math.sqrt(6 * 5))

    def test_both_constant_agree_one_constant_does_not(self):
        assert st.kendall_tau_b([1, 1, 1], [2, 2, 2]) == 1.0
        assert st.kendall_tau_b([1, 1, 1], [1, 2, 3]) == 0.0

    def test_length_mismatch_raises(self):
        with pytest.raises(ValueError):
            st.kendall_tau_b([1, 2], [1])

    def test_overlap_and_percentile(self):
        assert st.top_k_overlap(["a", "b", "c"], ["a", "c", "d"], 2) == 0.5
        assert st.percentile([1.0, 2.0, 3.0, 4.0], 50) == 2.5


# ── best-F1 threshold, bootstrap, factor ────────────────────────────────────

def _queries(n: int = 20):
    """Relevant candidates near 0.30-0.38, irrelevant near 0.50-0.70: separable at ~0.4."""
    out = []
    for i in range(n):
        rel = [(0.30 + 0.004 * i, True), (0.38 + 0.002 * i, True)]
        irr = [(0.50 + 0.01 * j + 0.003 * i, False) for j in range(8)]
        out.append(st.LabelledQuery(tuple(rel + irr), relevant_in_corpus=3))
    return out


class TestThreshold:
    def test_separable_set_finds_the_gap(self):
        t, f1 = st.best_f1_threshold(_queries())
        assert 0.38 <= t < 0.50
        assert f1 == pytest.approx(1.0)

    def test_corpus_basis_counts_unretrieved_relevant_chunks_as_misses(self):
        _t, f1_pool = st.best_f1_threshold(_queries(), fn_basis="pool")
        _t, f1_corpus = st.best_f1_threshold(_queries(), fn_basis="corpus")
        assert f1_corpus < f1_pool

    def test_no_relevant_candidate_has_no_threshold(self):
        q = st.LabelledQuery(((0.5, False), (0.6, False)))
        t, f1 = st.best_f1_threshold([q])
        assert math.isnan(t) and f1 == 0.0

    def test_ties_take_the_lowest_threshold(self):
        q = st.LabelledQuery(((0.2, True), (0.4, False)))
        assert st.best_f1_threshold([q])[0] == 0.2

    def test_bad_basis_raises(self):
        with pytest.raises(ValueError):
            st.best_f1_threshold(_queries(), fn_basis="nope")

    def test_bootstrap_is_seeded(self):
        a = st.bootstrap_threshold(_queries(), resamples=50, seed=7)
        b = st.bootstrap_threshold(_queries(), resamples=50, seed=7)
        c = st.bootstrap_threshold(_queries(), resamples=50, seed=8)
        assert a == b
        assert a != c
        assert len(a) == 50

    def test_factor_is_baseline_over_threshold_and_ci_is_swapped(self):
        r = st.factor_interval(_queries(), resamples=200, seed=3)
        assert r.factor == pytest.approx(0.45 / r.threshold)
        lo_t, hi_t = r.threshold_ci
        assert r.factor_ci == (pytest.approx(0.45 / hi_t), pytest.approx(0.45 / lo_t))
        assert r.factor_ci[0] <= r.factor <= r.factor_ci[1]

    def test_interval_overlap(self):
        assert st.intervals_overlap((1.30, 1.40), (1.36, 1.48))
        assert not st.intervals_overlap((1.0, 1.2), (1.36, 1.48))
        assert not st.intervals_overlap((float("nan"), 1.5), (1.36, 1.48))


# ── inversion counting ──────────────────────────────────────────────────────

def R(fam: str, rel: bool) -> st.RankedRow:
    return st.RankedRow(fam, rel)


class TestInversions:
    def test_clean_interleave_has_no_inversion(self):
        rows = [R("bge", True), R("voyage", True), R("bge", False), R("voyage", False)]
        r = st.count_inversions(rows, "bge", "voyage")
        assert (r.a_over_b, r.b_over_a) == (False, False)

    def test_irrelevant_bge_above_the_best_relevant_voyage(self):
        rows = [R("bge", False), R("voyage", True), R("bge", True)]
        r = st.count_inversions(rows, "bge", "voyage")
        assert r.a_over_b is True      # irrelevant bge sank the relevant Voyage hit
        assert r.b_over_a is False     # no irrelevant Voyage row above bge's best

    def test_irrelevant_voyage_above_the_best_relevant_bge(self):
        rows = [R("voyage", False), R("bge", True), R("voyage", True)]
        r = st.count_inversions(rows, "bge", "voyage")
        assert (r.a_over_b, r.b_over_a) == (False, True)

    def test_both_directions_in_one_list(self):
        rows = [R("bge", False), R("voyage", False), R("bge", True), R("voyage", True)]
        r = st.count_inversions(rows, "bge", "voyage")
        assert (r.a_over_b, r.b_over_a) == (True, True)

    def test_same_family_irrelevant_row_is_not_an_inversion(self):
        rows = [R("voyage", False), R("voyage", True), R("bge", True)]
        r = st.count_inversions(rows, "bge", "voyage")
        assert r.a_over_b is False
        assert r.b_over_a is True      # irrelevant Voyage above bge's best

    def test_family_with_no_relevant_row_is_reported_missing_not_counted(self):
        rows = [R("bge", False), R("bge", True)]
        r = st.count_inversions(rows, "bge", "voyage")
        assert r.b_missing is True and r.a_missing is False
        assert r.a_over_b is False     # nothing relevant of Voyage to sink

    def test_tally(self):
        res_ = [
            st.count_inversions([R("bge", False), R("voyage", True), R("bge", True)], "bge", "voyage"),
            st.count_inversions([R("bge", True), R("voyage", True)], "bge", "voyage"),
        ]
        t = st.tally_inversions(res_)
        assert (t.queries, t.a_over_b, t.b_over_a) == (2, 1, 0)

    def test_calibrated_order(self):
        rows = [(0.40, "bge"), (0.45, "voyage")]
        factor = {"bge": 1.5, "voyage": 1.0}.__getitem__
        assert st.order_by_calibrated(rows, factor) == [1, 0]


class TestSurfaceScoring:
    def _corpus(self):
        chunk = corp.chunk_file
        a = chunk("a.md", "alpha chunk text\n\nmore alpha")
        b = chunk("b.md", "beta chunk text")
        return corp.Corpus("prose", ["a.md", "b.md"], {"a.md": a, "b.md": b})

    def test_ranked_list_is_scored_by_target_file(self):
        corpus = self._corpus()
        q = qs.Query("prose-01", "prose", "something", "a.md")
        bge = "knowledge__o__bge-base-en-v15-768__v1"
        other = "knowledge__o__voyage-model-a__v1"
        a0 = corpus.chunks_by_file["a.md"][0].chash
        b0 = corpus.chunks_by_file["b.md"][0].chash
        ranked = [(bge, b0), (other, a0), (bge, a0)]
        got = mc.score_surface([(q, ranked)], {"prose": corpus}, depth=None)
        assert got["prose"]["dir1_bge_over_voyage"] == 1
        assert got["prose"]["dir2_voyage_over_bge"] == 0

    def test_depth_truncates_before_judging(self):
        corpus = self._corpus()
        q = qs.Query("prose-01", "prose", "x", "a.md")
        bge = "knowledge__o__bge-base-en-v15-768__v1"
        other = "knowledge__o__voyage-model-a__v1"
        a0 = corpus.chunks_by_file["a.md"][0].chash
        b0 = corpus.chunks_by_file["b.md"][0].chash
        ranked = [(bge, b0), (other, a0)]
        shallow = mc.score_surface([(q, ranked)], {"prose": corpus}, depth=1)
        assert shallow["prose"]["voyage_relevant_missing"] == 1
        assert shallow["prose"]["dir1_bge_over_voyage"] == 0

    def test_bound(self):
        assert mc.within_bound({"prose": {"dir1_bge_over_voyage": 3, "dir2_voyage_over_bge": 1}}) == {"prose": True}
        assert mc.within_bound({"code": {"dir1_bge_over_voyage": 4, "dir2_voyage_over_bge": 0}}) == {"code": False}

    def test_the_factor_patch_reorders_through_the_real_scoring_code(self):
        # A bge row at 0.40 against a Voyage-family row at 0.60: the calibrated order
        # flips as the bge factor passes the Voyage factor ratio (0.60 * 0.692 / 0.40 = 1.04).
        bge = f"knowledge__o__{mc.BGE_MODEL}__v1"
        voyage = f"knowledge__o__{eng.CONTEXT_MODEL}__v1"
        model_of = {bge: mc.BGE_MODEL, voyage: eng.CONTEXT_MODEL}.get

        def order() -> list[str]:
            rows = [SearchResult("b", "x", 0.40, bge), SearchResult("v", "y", 0.60, voyage)]
            return [r.id for r in order_by_calibrated_distance(rows, model_of=model_of)]

        before = mc.table_factor()
        with mc.bge_factor(1.0):
            assert order() == ["b", "v"]
            assert mc.table_factor() == pytest.approx(1.0)
        with mc.bge_factor(1.6):
            assert order() == ["v", "b"]
        assert mc.table_factor() == pytest.approx(before)     # restored

    def test_grouped_combined_query_orders_across_families_by_calibrated_distance(self, monkeypatch):
        bge = f"knowledge__o__{mc.BGE_MODEL}__v1"
        voyage = f"knowledge__o__{eng.CONTEXT_MODEL}__v1"
        monkeypatch.setattr(core, "collection_model",
                            lambda name: mc.BGE_MODEL if name == bge else eng.CONTEXT_MODEL)
        rows = {bge: [{"collection": bge, "distance": 0.40, "chash": "b"}],
                voyage: [{"collection": voyage, "distance": 0.60, "chash": "v"}]}
        call = lambda group: rows[group[0]]  # noqa: E731
        with mc.bge_factor(1.0):
            assert [r["chash"] for r in core._grouped_combined_query([bge, voyage], call)] == ["b", "v"]
        with mc.bge_factor(1.6):
            assert [r["chash"] for r in core._grouped_combined_query([bge, voyage], call)] == ["v", "b"]

    def test_grid_covers_1_0_to_1_6(self):
        assert mc.FACTOR_GRID[0] == 1.0 and mc.FACTOR_GRID[-1] == 1.6 and len(mc.FACTOR_GRID) == 7


class _StubSurfaces(mc.LiveSurfaces):
    """Canned rankings; the order depends on the bge factor in force, as the real merge's does."""

    def __init__(self, corpora):
        super().__init__()
        self._corpora = corpora

    def _lists(self, query_text, collections):
        kind = "prose" if "prose" in collections[0] else "code"
        corpus = self._corpora[kind]
        q = next(x for x in _STUB_QUERIES if x.text == query_text)
        relevant = corpus.chunks_by_file[q.target][0].chash
        other = next(c.chash for c in corpus.all_chunks() if c.file != q.target)
        return collections[0], collections[1], relevant, other

    def _ranked(self, query_text, collections):
        bge, voy, relevant, other = self._lists(query_text, collections)
        if mc.table_factor() >= 1.25:
            return [(voy, relevant), (bge, relevant), (bge, other)]
        return [(bge, other), (voy, relevant), (bge, relevant)]       # irrelevant bge sinks the Voyage hit

    def nx_search(self, query, collections, n):
        return self._ranked(query, collections)

    def query_retrieve(self, query, collections, fetch_n):
        return [query, collections], None

    def query_rank(self, raw, model_of):
        return self._ranked(*raw)

    def catalog_routed(self, query, collections, fetch_n, cache):
        return self._ranked(query, collections)


_STUB_QUERIES = [qs.Query("prose-01", "prose", "stub prose question", "a.md"),
                 qs.Query("code-01", "code", "stub code question", "c.py")]


class TestRunC:
    def _corpora(self):
        return {
            "prose": corp.Corpus("prose", ["a.md", "b.md"], {
                "a.md": corp.chunk_file("a.md", "alpha text"), "b.md": corp.chunk_file("b.md", "beta text")}),
            "code": corp.Corpus("code", ["c.py", "d.py"], {
                "c.py": corp.chunk_file("c.py", "def c(): pass"), "d.py": corp.chunk_file("d.py", "def d(): pass")}),
        }

    def test_the_grid_moves_the_counts_and_the_summary_renders(self):
        corpora = self._corpora()
        bge, voy = f"x__prose__{mc.BGE_MODEL}__v1", f"x__prose__{eng.CONTEXT_MODEL}__v1"
        cbge, cvoy = f"x__code__{mc.BGE_MODEL}__v1", f"x__code__{eng.CODE_MODEL}__v1"
        names = {("prose", "bge"): bge, ("prose", "voyage"): voy, ("code", "bge"): cbge, ("code", "voyage"): cvoy}
        out = mc.run_c(_StubSurfaces(corpora), _STUB_QUERIES, corpora, names, depths=(2, 3), log=lambda *_: None)
        grid = out["factor_grid_surface_2"]
        low = grid["1.0"]["3"]["all"]["dir1_bge_over_voyage"]
        high = grid["1.6"]["3"]["all"]["dir1_bge_over_voyage"]
        assert (low, high) == (2, 0)                      # the stub inverts below 1.25 and not above
        assert out["calibration_table_bge_factor"] == pytest.approx(mc.table_factor())
        assert set(out["surfaces"]) == {"1_nx_search_rerank_on", "2_mcp_query_plain_rerank_off",
                                        "3_catalog_routed_grouped_combined_query"}
        text = res.summary_text({"meta": {}, "tokens": {}, "measurement_c": out})
        assert "C. cross-family inversions" in text and "factor_grid_surface_2" in text


# ── measurement A analysis ──────────────────────────────────────────────────

def _raw_a():
    raw = []
    for i in range(20):
        for fam, shift in (("bge", 0.0), ("voyage", 0.3)):
            rows = [[f"c{j}", 0.30 + shift + 0.003 * i + 0.002 * j, j < 2] for j in range(2)]
            rows += [[f"d{j}", 0.55 + shift + 0.01 * j, False] for j in range(8)]
            raw.append({"set": "prose" if i < 10 else "code", "query_id": f"q{i}", "family": fam,
                        "collection": f"c-{fam}", "relevant_in_corpus": 3, "rows": rows})
    return raw


class TestMeasurementA:
    def test_analysis_is_seeded_and_reports_every_set(self):
        a1 = ma.analyze_a(_raw_a(), resamples=100, seed=5)
        a2 = ma.analyze_a(_raw_a(), resamples=100, seed=5)
        assert a1 == a2
        assert set(a1["sets"]) == {"prose", "code", "pooled"}
        assert a1["verdict"]["headline"] in ("PASS", "FAIL")

    def test_voyage_threshold_sits_above_bge(self):
        a = ma.analyze_a(_raw_a(), resamples=50, seed=5)
        bge = a["sets"]["pooled"]["bge"]["pool_basis"]["threshold"]
        voyage = a["sets"]["pooled"]["voyage"]["pool_basis"]["threshold"]
        assert voyage > bge

    def test_a_factor_interval_outside_the_expected_range_fails_plainly(self):
        # bge threshold ~0.38 -> factor ~1.18, interval well below 1.36
        a = ma.analyze_a(_raw_a(), resamples=100, seed=5)
        pooled = a["verdict"]["per_set"]["pooled"]
        assert pooled["overlaps_1.36_1.48"] is False
        assert pooled["result"] == "FAIL"


# ── measurement B pure parts ────────────────────────────────────────────────

class TestMeasurementB:
    def test_pool_has_targets_first_then_nearest_others_and_is_unique(self):
        pool = mb.build_pool(["t1", "t2"], ["t1", "t2", "t3"], [f"o{i}" for i in range(100)], size=10, max_targets=3)
        assert pool[:3] == ["t1", "t2", "t3"]
        assert pool[3:] == [f"o{i}" for i in range(7)]
        assert len(set(pool)) == 10

    def test_pool_skips_a_target_already_listed_among_others(self):
        pool = mb.build_pool(["t1"], ["t1"], ["t1", "o1", "o2"], size=3, max_targets=2)
        assert pool == ["t1", "o1", "o2"]

    def test_split_is_seeded_complete_and_disjoint(self):
        pool = [f"h{i}" for i in range(60)]
        a, b = mb.split_pool(pool, 4)
        assert (a, b) == mb.split_pool(pool, 4)
        assert (a, b) != mb.split_pool(pool, 5)
        assert len(a) == 30 and len(b) == 30
        assert set(a) | set(b) == set(pool) and not set(a) & set(b)

    def test_shuffle_keeps_members(self):
        pool = [f"h{i}" for i in range(20)]
        s = mb.shuffled(pool, 1)
        assert sorted(s) == sorted(pool) and s != pool

    def test_identical_scores_agree_fully(self):
        scores = {f"c{i}": i / 10 for i in range(20)}
        ag = mb.compare_scores(scores, dict(scores))
        assert ag.tau == 1.0 and ag.top1_same and ag.top10_overlap == 1.0 and ag.mean_abs_diff == 0.0

    def test_a_small_score_shift_that_keeps_the_order_keeps_tau(self):
        a = {f"c{i}": float(i) for i in range(30)}
        b = {k: v + 0.001 for k, v in a.items()}
        ag = mb.compare_scores(a, b)
        assert ag.tau == 1.0
        assert ag.mean_abs_diff == pytest.approx(0.001)

    def test_swapping_the_top_two_changes_top1(self):
        a = {"x": 3.0, "y": 2.0, "z": 1.0}
        b = {"x": 2.0, "y": 3.0, "z": 1.0}
        ag = mb.compare_scores(a, b)
        assert not ag.top1_same and ag.tau < 1.0

    def test_different_chunk_sets_raise(self):
        with pytest.raises(ValueError):
            mb.compare_scores({"a": 1.0}, {"b": 1.0})

    def test_merge_refuses_a_chunk_scored_twice(self):
        with pytest.raises(ValueError):
            mb.merge_by_score({"a": 1.0}, {"a": 2.0})

    def test_verdict_uses_the_noise_floor_gap(self):
        ok = mb.verdict_b({"tau_mean": 0.995}, {"tau_mean": 0.998})
        bad = mb.verdict_b({"tau_mean": 0.95}, {"tau_mean": 0.998})
        assert ok["result"] == "PASS" and bad["result"] == "FAIL"

    def test_unscored_rows_make_a_rerank_answer_invalid(self):
        rows = [{"id": "a", "rerank_score": 1.0}, {"id": "b"}]
        with pytest.raises(mb.RerankInvalid):
            mb.scores_of(rows, {}, 2, "q")
        with pytest.raises(mb.RerankInvalid):
            mb.scores_of([{"id": "a", "rerank_score": 1.0}], {"rerank_degraded": True}, 1, "q")

    def test_pool_tag_depends_on_the_pool(self):
        assert mb.pool_tag("h", "q1", ["a", "b"]) == mb.pool_tag("h", "q1", ["b", "a"])
        assert mb.pool_tag("h", "q1", ["a", "b"]) != mb.pool_tag("h", "q1", ["a", "c"])


# ── token budget and the cap ────────────────────────────────────────────────

class TestBudget:
    def test_estimate_is_chars_over_3_3_rounded_up(self):
        assert budget.chars_to_tokens(330) == 100
        assert budget.chars_to_tokens(331) == 101
        assert budget.chars_to_tokens(0) == 0

    def test_charge_stops_before_the_cap_is_crossed(self):
        b = budget.TokenBudget(cap=1000)
        b.charge(600, "embed")
        with pytest.raises(budget.BudgetExceeded):
            b.charge(401, "embed")
        assert b.spent == 600            # the refused charge was not applied
        b.charge(400, "embed")           # exactly the cap is allowed
        assert b.remaining() == 0 and b.by_label == {"embed": 1000}

    def test_check_cap_refuses_over_and_allows_equal(self):
        budget.check_cap(1_500_000, 1_500_000)
        with pytest.raises(budget.BudgetExceeded):
            budget.check_cap(1_500_001, 1_500_000)

    def test_rerank_tokens_bill_the_query_with_every_document(self):
        assert budget.rerank_tokens("q" * 33, ["d" * 297, "d" * 297]) == 2 * budget.chars_to_tokens(330)

    def test_engine_reported_usage_is_recorded_beside_the_estimate(self):
        b = budget.TokenBudget(cap=10)
        b.note_reported(7)
        b.note_reported(-3)
        assert b.snapshot()["engine_reported_tokens"] == 7

    def test_write_caps_follow_the_family(self):
        assert eng.write_cap("bge-base-en-v15-768") == 16
        assert eng.write_cap(eng.CONTEXT_MODEL) == 64
        assert eng.write_cap(eng.CODE_MODEL) == 300
        assert eng.is_voyage_model("voyage-model-a") and not eng.is_voyage_model("bge-base-en-v15-768")

    def test_engine_refuses_a_voyage_write_past_the_cap_before_sending(self):
        tokens = budget.TokenBudget(cap=10)
        e = eng.Engine("http://127.0.0.1:1", "t", tokens)
        sent: list = []
        e.request = lambda *a, **k: sent.append(a)  # type: ignore[method-assign]
        chunk = corp.Chunk("f.md", 0, "x" * 1000, "h" * 64)
        with pytest.raises(budget.BudgetExceeded):
            e.write_doc("c__o__voyage-model-a__v1", "voyage-model-a", "9.1.1", [chunk], label="t")
        assert sent == []                # nothing left the process

    def test_engine_refuses_a_voyage_search_past_the_cap_before_sending(self):
        e = eng.Engine("http://127.0.0.1:1", "t", budget.TokenBudget(cap=0))
        sent: list = []
        e.request = lambda *a, **k: sent.append(a)  # type: ignore[method-assign]
        with pytest.raises(budget.BudgetExceeded):
            e.search("a question", ["knowledge__o__voyage-model-a__v1"], 10)
        assert sent == []

    def test_a_bge_search_costs_nothing_and_is_sent(self):
        e = eng.Engine("http://127.0.0.1:1", "t", budget.TokenBudget(cap=0))
        e.request = lambda *a, **k: eng.Response(200, [])  # type: ignore[method-assign]
        assert e.search("a question", ["knowledge__o__bge-base-en-v15-768__v1"], 10) == ([], {})


# ── corpora, queries, plan ──────────────────────────────────────────────────

@pytest.fixture(scope="module")
def world():
    pinned = corp.load_pinned(SPIKE / "corpus-files.json")
    corpora = {k: corp.build_corpus(REPO, k, pinned) for k in corp.KINDS}
    queries = qs.load_queries(SPIKE / "queries.json")
    return pinned, corpora, queries


class TestCorpusAndQueries:
    def test_pinned_lists_have_the_stated_sizes_and_exist(self, world):
        pinned, corpora, _ = world
        assert len(pinned["prose"]) == corp.PROSE_FILES == 46
        assert len(pinned["code"]) == corp.CODE_FILES == 40
        for kind, c in corpora.items():
            assert len(c.files) == len(pinned[kind])
            assert c.n_chunks > 0

    def test_selection_is_deterministic_and_matches_the_pin(self, world):
        pinned, _, _ = world
        for kind in corp.KINDS:
            assert corp.select_files(REPO, kind) == [e["path"] for e in pinned[kind]]
            assert corp.select_files(REPO, kind) == corp.select_files(REPO, kind)

    def test_chunks_respect_the_cap_and_hash_is_stable(self, world):
        _, corpora, _ = world
        again = corp.build_corpus(REPO, "code", corp.load_pinned(SPIKE / "corpus-files.json"))
        assert corpora["code"].corpus_hash == again.corpus_hash
        for c in corpora["prose"].all_chunks() + corpora["code"].all_chunks():
            assert 0 < len(c.text) <= chunker.DEFAULT_MAX_CHARS
            assert c.chash == corp.chash_of(c.text)

    def test_the_fixed_query_set_validates_against_the_corpora(self, world):
        _, corpora, queries = world
        qs.validate(queries, corpora)
        assert len([q for q in queries if q.set == "prose"]) == 20
        assert len([q for q in queries if q.set == "code"]) == 20

    def test_no_query_copies_a_sentence_of_its_target(self, world):
        _, corpora, queries = world
        for q in queries:
            text = (REPO / q.target).read_text(encoding="utf-8").lower()
            words = q.text.lower().replace("?", "").replace(",", "").split()
            # no run of nine consecutive words of the query appears verbatim in the target
            for i in range(len(words) - 8):
                assert " ".join(words[i:i + 9]) not in " ".join(text.split()), (q.id, words[i:i + 9])

    def test_validate_rejects_a_missing_target(self, world):
        _, corpora, queries = world
        bad = [*queries[:-1], qs.Query("code-99", "code", "x", "src/nexus/nope.py")]
        with pytest.raises(ValueError):
            qs.validate(bad, corpora)

    def test_relevance_is_by_chunk_hash_including_shared_text(self):
        a = corp.chunk_file("a.md", "same text")
        b = corp.chunk_file("b.md", "same text")
        c = corp.Corpus("prose", ["a.md", "b.md"], {"a.md": a, "b.md": b})
        assert c.chash_files()[a[0].chash] == {"a.md", "b.md"}


class TestPlan:
    def test_full_plan_is_within_the_default_cap(self, world):
        _, corpora, queries = world
        p = plan_mod.make_plan(corpora, queries, keyless=False, cap=budget.DEFAULT_CAP_TOKENS)
        assert not p.refused
        assert p.estimates["embed_tokens"] == budget.chars_to_tokens(
            sum(c.n_chars for c in corpora.values()))
        assert len(p.collections) == 4
        assert p.pool_collections == 40 * 4

    def test_keyless_plan_spends_nothing_and_names_bge_collections_only(self, world):
        _, corpora, queries = world
        p = plan_mod.make_plan(corpora, queries, keyless=True, cap=1)
        assert p.total_tokens == 0 and not p.refused
        assert all("bge-base-en-v15-768" in c for c in p.collections) and len(p.collections) == 2

    def test_a_plan_over_the_cap_is_refused(self, world):
        _, corpora, queries = world
        p = plan_mod.make_plan(corpora, queries, keyless=False, cap=10_000)
        assert p.refused
        assert "REFUSED" in plan_mod.format_plan(p)

    def test_the_cap_can_be_raised(self, world):
        _, corpora, queries = world
        p = plan_mod.make_plan(corpora, queries, keyless=False, cap=10_000_000, assume_voyage_rerank=True)
        assert not p.refused and p.total_tokens > 1_500_000   # with Voyage rerank counted it is over the default

    def test_plan_mode_refuses_with_a_nonzero_exit_and_calls_nothing(self, capsys):
        assert p43_run.main(["--plan", "--cap-tokens", "1000"]) == 3
        out = capsys.readouterr().out
        assert "REFUSED" in out and "nothing is called" in out
        assert p43_run.main(["--plan"]) == 0


# ── results writer ──────────────────────────────────────────────────────────

class TestResults:
    def test_nan_and_inf_become_null_and_the_file_is_strict_json(self, tmp_path):
        doc = {"a": float("nan"), "b": [float("inf"), 1.5], "c": {1: (2, 3)}}
        res.write_results(tmp_path / "r.json", doc)
        loaded = json.loads((tmp_path / "r.json").read_text(), parse_constant=lambda c: pytest.fail(c))
        assert loaded == {"a": None, "b": [None, 1.5], "c": {"1": [2, 3]}}

    def test_summary_renders_a_b_and_c_blocks(self):
        a = ma.analyze_a(_raw_a(), resamples=20, seed=1)
        agr = [mb.compare_scores({"a": 1.0, "b": 2.0}, {"a": 1.0, "b": 2.0})]
        b = {"split_merge": mb.summarize(agr), "noise_floor_shuffled_pool": mb.summarize(agr),
             "noise_floor_repeat": mb.summarize(agr), "rerank_models_seen": ["m"],
             "verdict": mb.verdict_b(mb.summarize(agr), mb.summarize(agr))}
        text = res.summary_text({"meta": {"mode": "full"}, "tokens": {}, "measurement_a": a, "measurement_b": b})
        assert "A. bge calibration factor" in text and "B. cross-encoder" in text
        assert "PASS" in text
