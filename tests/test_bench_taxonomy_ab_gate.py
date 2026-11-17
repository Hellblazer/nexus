# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-134 gated leg-1 re-score (nexus-7fku2.1.7 follow-up): ``taxonomy_ab.py --gate-from`` and
``taxonomy_ab_compare.py``. Every case runs on stubs; nothing here reaches an engine or Voyage."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import nexus.catalog.factory
import nexus.commands._helpers
import nexus.db
import nexus.db.t2

import bench.taxonomy_ab as ab
from bench.taxonomy_ab_compare import compare, main as compare_main

ROUTED = "docs__1-1__m__v1"
GATED = "docs__1-2__m__v1"
S1 = "rdr__1-9__m__v1"
SETS = {"S1": [S1], "S2": [GATED, ROUTED]}
VARIANTS = [("band", 0.03), ("cband", 0.03), ("topk", 1)]
GT = {"rdr-049-": 3}
REPO = Path(__file__).resolve().parent.parent
COVERAGE = REPO / "bench" / "results" / "rdr134-taxonomy-coverage-2026-11-16.json"


def _hit(chash: str, distance: float, c: str) -> dict[str, Any]:
    return {"chash": chash, "distance": distance, "collection": c}


class _Vectors:
    """Unscoped search: the gated collection holds the target; its topic-scoped search does not."""

    def __init__(self) -> None:
        self.searches: list[str] = []
        self.topic_searches: list[tuple[str, str]] = []
        self.embeds = 0

    def search(self, query: str, colls: list[str], n_results: int = 10, **_: Any) -> list[dict[str, Any]]:
        (c,) = colls
        self.searches.append(c)
        return {
            GATED: [_hit("rdr-049-in-gated", 0.10, c), _hit("other-gated", 0.50, c)],
            ROUTED: [_hit("miss-routed", 0.30, c)],
            S1: [_hit("miss-s1", 0.30, c)],
        }[c]

    def search_topic_scoped(self, query: str, topic: str, c: str, **_: Any) -> list[dict[str, Any]]:
        self.topic_searches.append((c, topic))
        return {GATED: [_hit("topic-gated", 0.20, c)], ROUTED: [_hit("topic-routed", 0.25, c)],
                S1: [_hit("topic-s1", 0.25, c)]}[c]

    def embed_for_collection(self, c: str, texts: list[str]) -> list[list[float]]:
        self.embeds += 1
        return [[0.0]]


class _Centroids:
    # The gated collection has the best centroid across the set; the routed ones sit below the band.
    SIM = {GATED: 0.99, ROUTED: 0.80, S1: 0.80}

    def __init__(self) -> None:
        self.ranked: list[str] = []

    def ann_query(self, vec: list[float], c: str, n_results: int = 10) -> list[Any]:
        self.ranked.append(c)
        return [SimpleNamespace(topic_id=1, similarity=self.SIM[c])]


class _Taxonomy:
    def __init__(self) -> None:
        self._centroid = _Centroids()

    def get_topic_by_id(self, topic_id: int) -> dict[str, Any]:
        return {"label": f"topic{topic_id}"}

    def get_distinct_collections(self) -> list[str]:
        return [GATED, ROUTED, S1]


@pytest.fixture(autouse=True)
def _paths(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ab, "_resolve_paths_via_catalog",
                        lambda catalog, chashes: {c: f"/x/{c}.md" for c in chashes})


def _measure(gated: Any = None) -> tuple[dict[str, Any], _Vectors, _Taxonomy]:
    t3, tax = _Vectors(), _Taxonomy()
    q = {"qid": "Q", "text": "which rdr?", "gt": GT}
    args = (q, SETS, VARIANTS, t3, None, tax, tax._centroid)
    return (ab.measure_query(*args) if gated is None else ab.measure_query(*args, gated=gated)), t3, tax


def _strip_time(row: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(row), object_hook=lambda d: {k: v for k, v in d.items() if k != "s"})


def _coverage(tmp_path: Path, rows: list[dict[str, Any]], complete: bool = True) -> Path:
    p = tmp_path / "coverage.json"
    p.write_text(json.dumps({"complete": complete, "collections": rows}))
    return p


def _row(c: str, status: str = "ok", above: bool = True) -> dict[str, Any]:
    return {"collection": c, "status": status, "above_0_9": above, "gate_uncertain": False}


class TestLoadGate:
    def test_status_ok_below_the_gate_only(self, tmp_path: Path) -> None:
        p = _coverage(tmp_path, [_row("a"), _row("b", above=False), _row("c", status="no_taxonomy", above=False)])
        assert ab.load_gate(p) == {"b"}

    def test_the_saved_baseline_gates_the_eleven(self) -> None:
        report = json.loads(COVERAGE.read_text())
        gated = ab.load_gate(COVERAGE)
        assert len(gated) == 11 == report["partition"]["definite_miss"]
        assert gated <= set(report["not_routed_by_gate_names"])  # and the no_taxonomy one is not among them
        assert "docs__1-20__voyage-context-3__v1" not in gated

    @pytest.mark.parametrize("report", [
        {"complete": False, "collections": [_row("a", above=False)]},
        {"complete": True},
        {"complete": True, "collections": [_row("a")]},
    ])
    def test_refuses_a_partial_malformed_or_empty_gate(self, tmp_path: Path, report: dict[str, Any]) -> None:
        p = tmp_path / "c.json"
        p.write_text(json.dumps(report))
        with pytest.raises(ValueError):
            ab.load_gate(p)


class TestGatedCollectionIsUnscoped:
    def test_gated_collection_goes_down_the_no_topics_path(self) -> None:
        row, t3, tax = _measure(frozenset({GATED}))
        # never topic-searched and never ranked ...
        assert all(c != GATED for c, _ in t3.topic_searches)
        assert GATED not in tax._centroid.ranked
        # ... and its unscoped rows reach every ON arm, which finds the target the topic search would miss
        for arm in ("band0.03", "cband0.03", "topk1"):
            assert row["S2"][arm]["top3"][0] == "rdr-049-in-gated.md"
            assert row["S2"][arm]["n_unscoped"] == 1
        assert row["S2"]["OFF"]["top3"][0] == "rdr-049-in-gated.md"

    def test_gated_collection_sets_no_band(self) -> None:
        # Ungated, the gated collection's 0.99 centroid sets the band and the routed one is dropped.
        ungated, _, _ = _measure(frozenset())
        assert ungated["S2"]["band0.03"]["n_topics"] == 1
        gated, t3, _ = _measure(frozenset({GATED}))
        assert gated["S2"]["band0.03"]["n_topics"] == 1
        assert (ROUTED, "topic1") in t3.topic_searches  # the routed one is now inside its own band

    def test_searches_are_not_repeated_for_the_unscoped_rows(self) -> None:
        _, t3, _ = _measure(frozenset({GATED}))
        assert t3.searches.count(GATED) == 1  # the OFF search's rows are reused by every ON arm
        assert t3.embeds == 1  # one embed per question and model, as before

    def test_a_set_without_a_gated_collection_is_unchanged(self) -> None:
        gated, _, _ = _measure(frozenset({GATED}))
        ungated, _, _ = _measure(frozenset())
        assert _strip_time(gated["S1"]) == _strip_time(ungated["S1"])


class TestFlagOffChangesNothing:
    def _reference(self) -> tuple[dict[str, Any], _Vectors]:
        """The per-question loop as it ran before --gate-from, spelled out against the unchanged helpers."""
        t3, tax = _Vectors(), _Taxonomy()
        q = {"qid": "Q", "text": "which rdr?", "gt": GT}
        row: dict[str, Any] = {"qid": q["qid"], "text": q["text"], "gt": q["gt"]}
        vec_cache: dict[str, list[float]] = {}
        for sname, colls in SETS.items():
            calls = {"search": 0, "embed": 0, "ann": 0, "topic_search": 0}
            off = ab.score(ab.arm_off(t3, q["text"], colls, calls), q["gt"], None)
            off["s"] = 0.0
            ranked = ab.rank_topics(t3, tax._centroid, tax, q["text"], colls, calls, vec_cache)
            row[sname] = {"OFF": off}
            for mode, p in VARIANTS:
                chosen = ab.select(ranked, mode, p)
                empty: list[str] = []
                on = ab.score(ab.arm_on(t3, q["text"], chosen, calls, empty), q["gt"], None)
                on.update({"empty_topics": empty, "s": 0.0, "n_topics": len(chosen),
                           "n_colls": len({c for c, _ in chosen})})
                row[sname][f"{mode}{p}"] = on
            row[sname]["calls"] = calls
        return row, t3

    def test_default_and_empty_gate_match_the_old_loop(self) -> None:
        ref, ref_t3 = self._reference()
        for gated in (None, frozenset(), set()):
            row, t3, _ = _measure(gated)
            assert _strip_time(row) == _strip_time(ref)
            assert (t3.searches, t3.topic_searches, t3.embeds) == (ref_t3.searches, ref_t3.topic_searches, ref_t3.embeds)

    def test_no_unscoped_key_and_arm_helpers_keep_their_old_defaults(self) -> None:
        row, _, _ = _measure()
        assert all("n_unscoped" not in v for s in ("S1", "S2") for k, v in row[s].items() if k != "calls")
        t3 = _Vectors()
        calls = {"search": 0, "embed": 0, "ann": 0, "topic_search": 0}
        assert ab.arm_off(t3, "q", [ROUTED], calls) == [_hit("miss-routed", 0.30, ROUTED)]
        assert ab.arm_on(t3, "q", [], calls) == []


def _install_stubs(monkeypatch: pytest.MonkeyPatch, tax: _Taxonomy, t3: _Vectors) -> None:
    class _T2:
        def __init__(self, *_: Any, **__: Any) -> None:
            self.taxonomy = tax

        def __enter__(self) -> "_T2":
            return self

        def __exit__(self, *_: Any) -> None:
            return None

    t3.list_collections = lambda: [GATED, ROUTED, S1]  # type: ignore[attr-defined]
    monkeypatch.setattr(nexus.catalog.factory, "make_catalog_reader", lambda: None)
    monkeypatch.setattr(nexus.commands._helpers, "default_db_path", lambda: Path("unused"))
    monkeypatch.setattr(nexus.db, "make_t3", lambda: t3)
    monkeypatch.setattr(nexus.db.t2, "T2Database", _T2)


QUERIES = "queries:\n  - qid: Q1\n    text: which rdr?\n    ground_truth: {rdr-049-: 3}\n"


class TestMain:
    def _run(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *extra: str) -> tuple[dict[str, Any], _Vectors]:
        t3, tax = _Vectors(), _Taxonomy()
        _install_stubs(monkeypatch, tax, t3)
        qf = tmp_path / "q.yaml"
        qf.write_text(QUERIES)
        out = tmp_path / "out.json"
        assert ab.main(["--queries", str(qf), "--s1", S1, "--out", str(out), *extra]) == 0
        return json.loads(out.read_text()), t3

    def test_gated_run_records_what_it_unrouted(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        cov = _coverage(tmp_path, [_row(GATED, above=False), _row("docs__elsewhere", above=False), _row(ROUTED)])
        out, t3 = self._run(tmp_path, monkeypatch, "--gate-from", str(cov))
        assert out["gate"] == {"from": str(cov), "unrouted": [GATED], "gate_not_in_sets": ["docs__elsewhere"]}
        assert all(c != GATED for c, _ in t3.topic_searches)
        s2 = out["summary"]["S2"]
        assert s2["topk1"]["ndcg3"] == s2["OFF"]["ndcg3"] == 1.0  # topic routing alone would have lost the target
        assert set(out["summary"]["S2"]["band0.03"]) >= {"ndcg3", "recall10", "wins", "losses", "d_ndcg3"}

    def test_without_the_flag_the_output_has_no_gate_block(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        out, t3 = self._run(tmp_path, monkeypatch)
        assert "gate" not in out
        assert (GATED, "topic1") in t3.topic_searches  # routed as before
        assert out["sets"] == {"S1": [S1], "S2": sorted([GATED, ROUTED, S1])}
        assert out["summary"]["S2"]["topk1"]["ndcg3"] == 0.0 < out["summary"]["S2"]["OFF"]["ndcg3"]

    def test_a_gate_that_matches_no_collection_is_refused(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        cov = _coverage(tmp_path, [_row("docs__elsewhere", above=False)])
        with pytest.raises(SystemExit):
            self._run(tmp_path, monkeypatch, "--gate-from", str(cov))

    def test_a_bad_gate_file_fails_before_any_engine_call(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom() -> None:
            raise AssertionError("make_t3 reached")

        cov = _coverage(tmp_path, [_row("a", above=False)], complete=False)
        _install_stubs(monkeypatch, _Taxonomy(), _Vectors())
        monkeypatch.setattr(nexus.db, "make_t3", _boom)
        with pytest.raises(ValueError):
            ab.main(["--out", str(tmp_path / "o.json"), "--gate-from", str(cov)])


def _summary(ndcg: float, wins: int = 1) -> dict[str, Any]:
    return {"n": 26, "S2_collections": 38,
            "S1": {"OFF": {"ndcg3": 0.5, "recall10": 0.8, "s": 1.0},
                   "topk1": {"ndcg3": ndcg, "recall10": 0.8, "wins": wins, "losses": 0, "d_ndcg3": ndcg - 0.5}}}


class TestCompare:
    def test_prints_saved_against_gated_per_arm(self) -> None:
        text = compare({"summary": _summary(0.60)},
                       {"summary": _summary(0.65, wins=3), "gate": {"unrouted": ["c1", "c2"], "gate_not_in_sets": []}})
        assert "unrouted by the gate (2): c1, c2" in text
        assert "topk1" in text and "ndcg3=0.6000->0.6500 (+0.0500)" in text
        assert "wins=1->3" in text and "losses=0->0" in text
        assert "OFF" in text and "wins=-" in text

    def test_names_an_arm_or_set_present_on_one_side_only(self) -> None:
        saved, gated = _summary(0.6), _summary(0.6)
        del gated["S1"]["topk1"]
        gated["S2"] = gated["S1"]
        text = compare({"summary": saved}, {"summary": gated})
        assert "topk1: only in saved" in text
        assert "S2: only in gated" in text

    def test_the_cli_reads_two_files(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        a, b = tmp_path / "a.json", tmp_path / "b.json"
        a.write_text(json.dumps({"summary": _summary(0.6)}))
        b.write_text(json.dumps({"summary": _summary(0.7)}))
        assert compare_main([str(a), str(b)]) == 0
        assert "0.6000->0.7000" in capsys.readouterr().out
        assert compare_main([str(a)]) == 2

    def test_runs_on_the_saved_leg1_file(self) -> None:
        saved = json.loads((REPO / "bench" / "results" / "rdr134-taxonomy-ab-leg1-2026-11-16.json").read_text())
        text = compare(saved, saved)
        assert "S2  (saved->gated)" in text and "band0.03" in text and "(+0.0000)" in text
