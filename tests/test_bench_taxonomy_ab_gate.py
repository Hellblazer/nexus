# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-134 gated re-score (nexus-7fku2.1.7 follow-up): ``taxonomy_ab.py --gate-from`` (leg 1),
``taxonomy_ab2.py --gate-from`` (the production set) and ``taxonomy_ab_compare.py``. Every case runs on
stubs; nothing here reaches an engine or Voyage."""
from __future__ import annotations

import json
import math
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import nexus.catalog.factory
import nexus.commands._helpers
import nexus.db
import nexus.db.t2
import nexus.search_engine

import bench.taxonomy_ab as ab
import bench.taxonomy_ab2 as ab2
from bench.taxonomy_ab_compare import compare, main as compare_main, mismatches, paired, paired_only, shape

ROUTED = "docs__1-1__m__v1"
GATED = "docs__1-2__m__v1"
NOTOPIC = "docs__1-3__m__v1"
S1C = "rdr__1-9__m__v1"
S2 = sorted([GATED, ROUTED, NOTOPIC, S1C])
SETS = {"S1": [S1C], "S2": S2}
VARIANTS = [("band", 0.03), ("cband", 0.03), ("topk", 1)]
GT = {"rdr-049-": 3}
RANK2 = 1 / math.log2(3)  # ndcg3 of the one target at rank 2: the nearer no-topic document is graded 0
GATES = {"0.9": {GATED}, "0.96": {GATED, ROUTED}}
REPO = Path(__file__).resolve().parent.parent
COVERAGE = REPO / "bench" / "results" / "rdr134-taxonomy-coverage-2026-11-16.json"
SAVED_LEG1 = REPO / "bench" / "results" / "rdr134-taxonomy-ab-leg1-2026-11-16.json"


def _hit(chash: str, distance: float, c: str) -> dict[str, Any]:
    return {"chash": chash, "distance": distance, "collection": c}


class _Vectors:
    """Unscoped search: the gated collection holds the target; its topic-scoped search does not. The
    no-topic collection holds a graded-0 document nearer than anything else."""

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
            NOTOPIC: [_hit("rdr-041-in-notopic", 0.05, c)],
            S1C: [_hit("miss-s1", 0.30, c)],
        }[c]

    def search_topic_scoped(self, query: str, topic: str, c: str, **_: Any) -> list[dict[str, Any]]:
        self.topic_searches.append((c, topic))
        return {GATED: [_hit("topic-gated", 0.20, c)], ROUTED: [_hit("topic-routed", 0.25, c)],
                S1C: [_hit("topic-s1", 0.25, c)]}[c]

    def embed_for_collection(self, c: str, texts: list[str]) -> list[list[float]]:
        self.embeds += 1
        return [[0.0]]


class _Centroids:
    # The gated collection has the best centroid across the set; the routed ones sit below the 0.03
    # band; the no-topic collection has no centroid at all.
    SIM = {GATED: [0.99], ROUTED: [0.80], S1C: [0.80], NOTOPIC: []}

    def __init__(self) -> None:
        self.ranked: list[str] = []

    def ann_query(self, vec: list[float], c: str, n_results: int = 10) -> list[Any]:
        self.ranked.append(c)
        return [SimpleNamespace(topic_id=i + 1, similarity=s) for i, s in enumerate(self.SIM[c])]


class _Taxonomy:
    def __init__(self) -> None:
        self._centroid = _Centroids()

    def get_topic_by_id(self, topic_id: int) -> dict[str, Any]:
        return {"label": f"topic{topic_id}"}

    def get_distinct_collections(self) -> list[str]:
        return [GATED, ROUTED, NOTOPIC, S1C]


@pytest.fixture(autouse=True)
def _paths(monkeypatch: pytest.MonkeyPatch) -> None:
    def resolve(catalog: Any, chashes: list[str]) -> dict[str, str]:
        return {c: f"/x/{c}.md" for c in chashes}

    monkeypatch.setattr(ab, "_resolve_paths_via_catalog", resolve)
    monkeypatch.setattr(ab2, "_resolve_paths_via_catalog", resolve)


Q = {"qid": "Q", "kind": "rdr", "text": "which rdr?", "gt": GT}
# taxonomy_ab2 grades by normalised names: load_queries normalises the keys, so a prod question must too
PGT = {ab2._norm(k): v for k, v in GT.items()}
PQ = {**Q, "gt": PGT}


def _measure(gates: dict[str, set[str]] | None,
             sets: dict[str, list[str]] = SETS) -> tuple[dict[str, Any], _Vectors, _Taxonomy]:
    t3, tax = _Vectors(), _Taxonomy()
    args = (Q, sets, VARIANTS, t3, None, tax, tax._centroid)
    row = ab.measure_query(*args) if gates is None else ab.measure_query_gated(*args, gates=gates)
    return row, t3, tax


def _strip_time(row: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(row), object_hook=lambda d: {k: v for k, v in d.items() if k != "s"})


def _coverage(tmp_path: Path, rows: list[dict[str, Any]], complete: bool = True) -> Path:
    p = tmp_path / "coverage.json"
    p.write_text(json.dumps({"complete": complete, "collections": rows}))
    return p


def _row(c: str, status: str = "ok", above: bool = True, coverage: float | None = 1.0) -> dict[str, Any]:
    return {"collection": c, "status": status, "above_0_9": above, "coverage": coverage, "gate_uncertain": False}


class TestLoadGates:
    def test_status_ok_below_the_gate_only(self, tmp_path: Path) -> None:
        p = _coverage(tmp_path, [_row("a"), _row("b", above=False, coverage=0.5),
                                 _row("c", status="no_taxonomy", above=False, coverage=None)])
        assert ab.load_gate(p) == {"b"}
        assert ab.report_collections(p) == {"a", "b", "c"}

    def test_the_096_gate_adds_the_collections_between_09_and_096(self, tmp_path: Path) -> None:
        p = _coverage(tmp_path, [_row("hi"), _row("mid", coverage=0.951), _row("lo", above=False, coverage=0.4),
                                 _row("edge", coverage=0.96),
                                 _row("none", status="no_taxonomy", above=False, coverage=None)])
        assert ab.load_gates(p) == {"0.9": {"lo"}, "0.96": {"lo", "mid"}}

    def test_the_saved_baseline(self) -> None:
        report = json.loads(COVERAGE.read_text())
        gates = ab.load_gates(COVERAGE)
        assert len(gates["0.9"]) == 11 == report["partition"]["definite_miss"]
        assert gates["0.9"] <= set(report["not_routed_by_gate_names"])
        assert "docs__1-20__voyage-context-3__v1" not in gates["0.9"]  # no_taxonomy: never routable
        assert gates["0.96"] - gates["0.9"] == {"rdr__1-79__voyage-context-3__v1",
                                                 "knowledge__dt-papers__voyage-context-3__v1"}
        assert "knowledge__1-1__voyage-context-3__v1" not in ab.report_collections(COVERAGE)

    @pytest.mark.parametrize("report", [
        {"complete": False, "collections": [_row("a", above=False)]},
        {"complete": True},
        {"complete": True, "collections": [_row("a")]},
    ])
    def test_refuses_a_partial_malformed_or_empty_gate(self, tmp_path: Path, report: dict[str, Any]) -> None:
        p = tmp_path / "c.json"
        p.write_text(json.dumps(report))
        with pytest.raises(ValueError):
            ab.load_gates(p)


class TestGatedLeg1:
    def test_ungated_and_gated_arms_come_from_one_run(self) -> None:
        row, t3, tax = _measure(GATES)
        names = set(row["S2"])
        assert {"OFF", "calls", "band0.03", "cband0.03", "topk1", "band0.03@g0.9", "band0.03@g0.96",
                "topk1@g0.96"} <= names
        # one OFF search per collection, one ranking per collection, one embed per question:
        assert sorted(t3.searches) == sorted(S2 + [S1C])
        assert sorted(tax._centroid.ranked) == sorted(S2 + [S1C])
        assert t3.embeds == 1
        assert {k: v for k, v in row["S2"]["calls"].items() if k != "topic_search"} == {"search": 4, "embed": 0, "ann": 4}
        # and no topic is read twice, however many arms keep it
        assert len(t3.topic_searches) == len(set(t3.topic_searches))
        assert row["S2"]["calls"]["topic_search"] + row["S1"]["calls"]["topic_search"] == len(t3.topic_searches)

    def test_gated_collection_goes_down_the_no_topics_path(self) -> None:
        row, _, _ = _measure(GATES)
        arm = row["S2"]["band0.03@g0.9"]
        assert arm["unscoped"] == [GATED, NOTOPIC]
        assert "rdr-049-in-gated.md" in arm["top3"]  # its unscoped rows reach the arm; its topic search would miss them
        assert arm["top3_coll"][arm["top3"].index("rdr-049-in-gated.md")] == GATED
        assert "rdr-049-in-gated.md" not in row["S2"]["band0.03"]["top3"]

    def test_gated_collection_sets_no_band(self) -> None:
        row, _, _ = _measure(GATES)
        s2 = row["S2"]
        # ungated: GATED's 0.99 centroid is the best, the 0.80 ones fall outside the band and are skipped
        assert (s2["band0.03"]["n_colls"], s2["band0.03"]["skipped"]) == (1, [ROUTED, S1C])
        # gated at 0.9: it leaves the ranking, the routed ones are inside their own band and kept
        assert (s2["band0.03@g0.9"]["n_colls"], s2["band0.03@g0.9"]["skipped"]) == (2, [])
        assert s2["band0.03@g0.9"]["unscoped"] == [GATED, NOTOPIC]
        # at 0.96 ROUTED goes too: only S1C is left to route
        assert (s2["band0.03@g0.96"]["n_colls"], s2["band0.03@g0.96"]["unscoped"]) == (1, [ROUTED, GATED, NOTOPIC])

    def test_a_collection_with_no_topic_is_searched_plain_in_every_on_arm(self) -> None:
        row, _, _ = _measure(GATES)
        for arm, v in row["S2"].items():
            if arm in ("OFF", "calls"):
                continue
            assert NOTOPIC in v["unscoped"], arm
            assert v["top3"][0] == "rdr-041-in-notopic.md" and v["top3_coll"][0] == NOTOPIC, arm
        # the flag-off run is the one that skips it
        off_row, _, _ = _measure(None)
        assert "rdr-041-in-notopic.md" not in off_row["S2"]["band0.03"]["top3"]

    def test_skipped_and_unscoped_partition_the_set(self) -> None:
        row, _, _ = _measure(GATES)
        for arm, v in row["S2"].items():
            if arm in ("OFF", "calls"):
                continue
            assert v["n_colls"] == len(set(S2) - set(v["unscoped"]) - set(v["skipped"])), arm
            assert not set(v["unscoped"]) & set(v["skipped"]), arm

    def test_a_gated_collection_in_s1(self) -> None:
        sets = {"S1": [GATED], "S2": [GATED, ROUTED]}
        row, _, _ = _measure({"0.9": {GATED}, "0.96": {GATED}}, sets)
        ungated, gated = row["S1"]["topk1"], row["S1"]["topk1@g0.9"]
        assert ungated["top3"] == ["topic-gated.md"] and ungated["unscoped"] == []
        assert gated["unscoped"] == [GATED] and gated["n_topics"] == 0 and gated["n_colls"] == 0
        assert gated["top3"] == row["S1"]["OFF"]["top3"] == ["rdr-049-in-gated.md", "other-gated.md"]
        assert gated["ndcg3"] == row["S1"]["OFF"]["ndcg3"] == 1.0 > ungated["ndcg3"]

    def test_the_ungated_arm_is_not_moved_by_the_gate_it_shares_a_run_with(self) -> None:
        with_gate, _, _ = _measure(GATES)
        no_gate, _, _ = _measure({})
        for arm in ("band0.03", "cband0.03", "topk1", "OFF"):
            assert _strip_time(with_gate["S2"][arm]) == _strip_time(no_gate["S2"][arm])


class TestFlagOffChangesNothing:
    def _reference(self) -> tuple[dict[str, Any], _Vectors]:
        """The per-question loop as it ran before --gate-from, spelled out against the unchanged helpers."""
        t3, tax = _Vectors(), _Taxonomy()
        row: dict[str, Any] = {"qid": Q["qid"], "text": Q["text"], "gt": Q["gt"]}
        vec_cache: dict[str, list[float]] = {}
        for sname, colls in SETS.items():
            calls = {"search": 0, "embed": 0, "ann": 0, "topic_search": 0}
            off = ab.score(ab.arm_off(t3, Q["text"], colls, calls), Q["gt"], None)
            off["s"] = 0.0
            ranked = ab.rank_topics(t3, tax._centroid, tax, Q["text"], colls, calls, vec_cache)
            row[sname] = {"OFF": off}
            for mode, p in VARIANTS:
                chosen = ab.select(ranked, mode, p)
                empty: list[str] = []
                on = ab.score(ab.arm_on(t3, Q["text"], chosen, calls, empty), Q["gt"], None)
                on.update({"empty_topics": empty, "s": 0.0, "n_topics": len(chosen),
                           "n_colls": len({c for c, _ in chosen})})
                row[sname][f"{mode}{p}"] = on
            row[sname]["calls"] = calls
        return row, t3

    def test_measure_query_matches_the_old_loop(self) -> None:
        ref, ref_t3 = self._reference()
        row, t3, _ = _measure(None)
        assert _strip_time(row) == _strip_time(ref)
        assert (t3.searches, t3.topic_searches, t3.embeds) == (ref_t3.searches, ref_t3.topic_searches, ref_t3.embeds)

    def test_no_new_key_and_the_helpers_keep_their_old_defaults(self) -> None:
        row, _, _ = _measure(None)
        assert all(set(v) <= {"ndcg3", "recall10", "top3", "unresolved", "s", "empty_topics", "n_topics", "n_colls"}
                   for s in ("S1", "S2") for k, v in row[s].items() if k != "calls")
        t3 = _Vectors()
        calls = {"search": 0, "embed": 0, "ann": 0, "topic_search": 0}
        assert ab.arm_off(t3, "q", [ROUTED], calls) == [_hit("miss-routed", 0.30, ROUTED)]
        assert ab.arm_on(t3, "q", [(ROUTED, "t")], calls) == [_hit("topic-routed", 0.25, ROUTED)]
        assert ab.arm_on(t3, "q", [(ROUTED, "t")], calls)  # no cache: a second identical read goes to the engine
        assert calls["topic_search"] == 2


def _install_stubs(monkeypatch: pytest.MonkeyPatch, tax: _Taxonomy, t3: _Vectors) -> None:
    class _T2:
        def __init__(self, *_: Any, **__: Any) -> None:
            self.taxonomy = tax

        def __enter__(self) -> "_T2":
            return self

        def __exit__(self, *_: Any) -> None:
            return None

    t3.list_collections = lambda: sorted(S2)  # type: ignore[attr-defined]
    monkeypatch.setattr(nexus.catalog.factory, "make_catalog_reader", lambda: None)
    monkeypatch.setattr(nexus.commands._helpers, "default_db_path", lambda: Path("unused"))
    monkeypatch.setattr(nexus.db, "make_t3", lambda: t3)
    monkeypatch.setattr(nexus.db.t2, "T2Database", _T2)


QUERIES = ("queries:\n  - qid: Q1\n    text: which rdr?\n    ground_truth: {rdr-049-: 3}\n"
           "  - qid: Q2\n    text: and then?\n    ground_truth: {rdr-049-: 3}\n")


class TestMainLeg1:
    def _run(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *extra: str) -> tuple[dict[str, Any], _Vectors]:
        t3, tax = _Vectors(), _Taxonomy()
        _install_stubs(monkeypatch, tax, t3)
        qf = tmp_path / "q.yaml"
        qf.write_text(QUERIES)
        out = tmp_path / "out.json"
        assert ab.main(["--queries", str(qf), "--s1", S1C, "--out", str(out), *extra]) == 0
        return json.loads(out.read_text()), t3

    def test_gated_run_records_the_gate_the_notes_and_the_paired_arms(self, tmp_path: Path,
                                                                      monkeypatch: pytest.MonkeyPatch) -> None:
        cov = _coverage(tmp_path, [_row(GATED, above=False, coverage=0.5),
                                   _row("docs__elsewhere", above=False, coverage=0.2), _row(ROUTED, coverage=0.95)])
        out, _ = self._run(tmp_path, monkeypatch, "--gate-from", str(cov))
        assert out["gate"] == {"from": str(cov), "unrouted": {"0.9": [GATED], "0.96": [ROUTED, GATED]},
                               "gate_not_in_sets": ["docs__elsewhere"],
                               "sets_not_in_report": sorted([NOTOPIC, S1C])}
        assert any("zero-row fallback" in n for n in out["notes"])
        s2 = out["summary"]["S2"]
        assert {"band0.03", "band0.03@g0.9", "topk1@g0.96"} <= set(s2)
        assert s2["topk1@g0.9"]["ndcg3"] == s2["OFF"]["ndcg3"] == pytest.approx(RANK2)
        assert s2["topk1"]["ndcg3"] == 0.0  # routing alone loses the target
        assert set(s2["band0.03@g0.9"]) >= {"ndcg3", "recall10", "wins", "losses", "d_ndcg3"}
        pr = paired(out)
        assert pr["S2"]["topk1@g0.9"] == {"better": 2, "worse": 0, "same": 0, "n": 2,
                                          "mean_d_ndcg3": pytest.approx(RANK2), "mean_d_recall10": pytest.approx(1.0)}

    def test_without_the_flag_the_output_is_the_old_one(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        out, t3 = self._run(tmp_path, monkeypatch)
        assert set(out) == {"summary", "sets", "results"}
        assert (GATED, "topic1") in t3.topic_searches  # routed as before
        assert out["sets"] == {"S1": [S1C], "S2": S2}
        assert not any("@g" in arm for arm in out["summary"]["S2"])
        assert out["summary"]["S2"]["topk1"]["ndcg3"] == 0.0 < out["summary"]["S2"]["OFF"]["ndcg3"]

    def test_a_gate_that_matches_no_collection_is_refused(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        cov = _coverage(tmp_path, [_row("docs__elsewhere", above=False, coverage=0.1)])
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


class TestProd:
    """taxonomy_ab2: the production set (every prose collection, routed or not)."""

    PROD = sorted([GATED, ROUTED, NOTOPIC])
    ROUTED_SET = [GATED, ROUTED]

    def _measure(self, gates: dict[str, set[str]] | None) -> tuple[dict[str, Any], _Vectors]:
        t3, tax = _Vectors(), _Taxonomy()

        def p0(text: str, prod: list[str], n: int, t3_: Any, **kw: Any) -> list[Any]:
            return [SimpleNamespace(id="rdr-049-in-gated", distance=0.1, collection=GATED)]

        row = ab2.measure_prod_query(PQ, self.PROD, self.ROUTED_SET, t3, None, tax, tax._centroid, p0, gates)
        return row, t3

    def test_gated_arms_use_plain_rows_for_gated_collections_only(self) -> None:
        row, _ = self._measure(GATES)
        assert PGT == {"rdr049": 3} and row["OFF"]["ndcg3"] > 0  # a zero here would make every comparison below vacuous
        # ungated: GATED is routed (its topic read has no target); gated: its plain rows have it
        assert row["top1_F0"]["ndcg3"] == 0.0
        assert row["top1_F0@g0.9"]["ndcg3"] == pytest.approx(RANK2) == row["OFF"]["ndcg3"]
        assert "rdr-049-in-gated.md" in row["top1_Fk@g0.96"]["top3"]
        arm = row["top1_F0@g0.9"]
        assert arm["top3_coll"][arm["top3"].index("rdr-049-in-gated.md")] == GATED
        # ROUTED keeps its topic rows at 0.9 and goes plain at 0.96
        assert "topic-routed.md" in row["top1_F0@g0.9"]["top3"] and "miss-routed.md" not in row["top1_F0@g0.9"]["top3"]
        assert "miss-routed.md" in row["top1_F0@g0.96"]["top3"] + ["miss-routed.md"]  # plain rows are in OFF

    def test_the_gated_arms_cost_no_engine_call(self) -> None:
        _, plain_t3 = self._measure(None)
        _, gated_t3 = self._measure(GATES)
        assert (gated_t3.searches, gated_t3.topic_searches, gated_t3.embeds) == (
            plain_t3.searches, plain_t3.topic_searches, plain_t3.embeds)

    def test_flag_off_row_has_no_new_key_and_matches_the_old_loop(self) -> None:
        row, t3 = self._measure(None)
        assert row["OFF"]["ndcg3"] > 0 and row["P0"]["ndcg3"] > 0  # non-vacuity: the target is graded
        assert list(row) == ["qid", "kind", "gt", "P0", "P0s", "OFF", "top1_F0", "top1_Fk", "stats"]
        assert all("top3_coll" not in row[a] for a in ("P0", "P0s", "OFF", "top1_F0", "top1_Fk"))
        tax = _Taxonomy()
        ref_t3 = _Vectors()
        plain = {c: ab2.search_one(ref_t3, Q["text"], c) for c in self.PROD}
        ranked = ab.rank_topics(ref_t3, tax._centroid, tax, Q["text"], self.ROUTED_SET,
                                {"search": 0, "embed": 0, "ann": 0, "topic_search": 0}, {})
        top1 = {c: ab2.topic_one(ref_t3, Q["text"], c, r[0][0]) for c, r in ranked.items() if r}
        for arm, rule in (("top1_F0", lambda n: n == 0), ("top1_Fk", lambda n: n < ab2.K_PROD)):
            rows = [r for c in self.PROD for r in (plain[c] if top1.get(c) is None or rule(len(top1[c])) else top1[c])]
            assert row[arm] == ab2.score(ab2.merge(rows), PGT, None)
        # F0 and Fk are scored from rows that include the target only if a collection was read plain
        assert row["top1_F0"]["ndcg3"] == 0.0 < row["OFF"]["ndcg3"]  # the ungated arm routes GATED away from it
        assert (t3.searches, t3.topic_searches) == (ref_t3.searches, ref_t3.topic_searches)
        assert row["stats"] == {"zero": 0, "short": 2, "routed": 2, "no_topics": 1}

    def _main(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *extra: str) -> dict[str, Any]:
        t3, tax = _Vectors(), _Taxonomy()
        _install_stubs(monkeypatch, tax, t3)
        monkeypatch.setattr(ab2, "prose_collections", lambda: list(self.PROD))
        monkeypatch.setattr(nexus.search_engine, "search_cross_corpus",
                            lambda *a, **k: [SimpleNamespace(id="rdr-049-in-gated", distance=0.1, collection=GATED)])
        qf = tmp_path / "q.yaml"
        qf.write_text(QUERIES)
        out = tmp_path / "out.json"
        assert ab2.main(["--queries", str(qf), "--out", str(out), *extra]) == 0
        return json.loads(out.read_text())

    def test_main_summarises_the_gated_arms_against_their_ungated_arm(self, tmp_path: Path,
                                                                      monkeypatch: pytest.MonkeyPatch) -> None:
        cov = _coverage(tmp_path, [_row(GATED, above=False, coverage=0.5), _row(ROUTED, coverage=0.95)])
        out = self._main(tmp_path, monkeypatch, "--gate-from", str(cov))
        s = out["summary"]["all"]
        assert s["OFF"]["ndcg3"] > 0 and s["top1_F0@g0.9"]["ndcg3"] > 0  # the yaml's gt went through load_queries
        assert s["top1_F0@g0.9"]["vs_ungated"] == {"better": 2, "worse": 0, "same": 0,
                                                    "mean_delta_ndcg3": pytest.approx(RANK2)}
        # wins and losses are against OFF: the gated arm recovers OFF exactly, the ungated arm loses the target
        assert (s["top1_F0@g0.9"]["wins"], s["top1_F0@g0.9"]["losses"]) == (0, 0)
        assert (s["top1_F0"]["wins"], s["top1_F0"]["losses"]) == (0, 2)
        assert "vs_ungated" not in s["top1_F0"]
        assert out["gate"]["unrouted"] == {"0.9": [GATED], "0.96": [ROUTED, GATED]}
        # the stub lists NOTOPIC among the collections with topics, and the coverage rows here name only GATED and ROUTED
        assert out["gate"]["routed_not_in_report"] == [NOTOPIC]
        assert out["notes"]

    def test_main_without_the_flag_has_no_gate_keys(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        out = self._main(tmp_path, monkeypatch)
        assert set(out) == {"summary", "prod", "results"}
        assert not any("@g" in arm for arm in out["summary"]["all"])

    def test_main_refuses_a_gate_that_matches_nothing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        cov = _coverage(tmp_path, [_row("docs__elsewhere", above=False, coverage=0.1)])
        with pytest.raises(SystemExit):
            self._main(tmp_path, monkeypatch, "--gate-from", str(cov))


def _arm(ndcg: float, top3: list[str] | None = None, colls: list[str] | None = None,
         recall: float = 0.5) -> dict[str, Any]:
    d: dict[str, Any] = {"ndcg3": ndcg, "recall10": recall, "top3": top3 or ["a.md"], "s": 0.0, "n_topics": 1}
    if colls is not None:
        d["top3_coll"] = colls
    return d


def _run_file(qids: list[str], sets: dict[str, list[str]] | None = None, n: int | None = None,
              gated_delta: list[float] | None = None, moved: bool = False) -> dict[str, Any]:
    sets = sets or {"S1": ["c1"], "S2": ["c1", "c2"]}
    results = []
    for i, qid in enumerate(qids):
        row: dict[str, Any] = {"qid": qid}
        for sname in sets:
            row[sname] = {"OFF": _arm(0.5), "topk1": _arm(0.5, ["a.md"], ["c1"])}
            if gated_delta is not None:
                row[sname]["topk1@g0.9"] = _arm(0.5 + gated_delta[i], ["b.md"] if moved else ["a.md"],
                                                 ["c2"] if moved else ["c1"])
        results.append(row)
    summ: dict[str, Any] = {"n": len(qids) if n is None else n, "S2_collections": len(sets["S2"])}
    for sname in sets:
        summ[sname] = {"OFF": {"ndcg3": 0.5, "recall10": 0.5, "s": 0.0},
                       "topk1": {"ndcg3": 0.5, "recall10": 0.5, "wins": 0, "losses": 0, "d_ndcg3": 0.0}}
    return {"summary": summ, "sets": sets, "results": results}


class TestCompare:
    def test_refuses_runs_that_are_not_the_same_measurement(self, tmp_path: Path,
                                                            capsys: pytest.CaptureFixture[str]) -> None:
        base = _run_file(["Q1", "Q2"])
        cases = {
            "n": _run_file(["Q1", "Q2"], n=3),
            "S2_collections": _run_file(["Q1", "Q2"], sets={"S1": ["c1"], "S2": ["c1", "c2", "c3"]}),
            "set S2": _run_file(["Q1", "Q2"], sets={"S1": ["c1"], "S2": ["c1", "c9"]}),
            "question ids": _run_file(["Q1", "Q3"]),
        }
        for key, other in cases.items():
            bad = mismatches(base, other)
            assert any(m.startswith(key) for m in bad), (key, bad)
            text = compare(base, other)
            assert text.startswith("MISMATCH") and "paired" not in text
        a, b = tmp_path / "a.json", tmp_path / "b.json"
        a.write_text(json.dumps(base))
        b.write_text(json.dumps(cases["question ids"]))
        assert compare_main([str(a), str(b)]) == 1
        assert "MISMATCH" in capsys.readouterr().out
        b.write_text(json.dumps(base))
        assert compare_main([str(a), str(b)]) == 0
        assert compare_main([]) == 2

    def test_paired_table_counts_questions_within_one_run(self) -> None:
        gated = _run_file(["Q1", "Q2", "Q3"], gated_delta=[0.2, -0.1, 0.0])
        gated["gate"] = {"unrouted": {"0.9": ["c2"], "0.96": ["c2", "c3"]}, "gate_not_in_sets": [],
                         "sets_not_in_report": ["knowledge__1-1"]}
        pr = paired(gated)
        assert pr["S2"]["topk1@g0.9"] == {"better": 1, "worse": 1, "same": 1, "n": 3,
                                          "mean_d_ndcg3": pytest.approx(0.1 / 3), "mean_d_recall10": 0.0}
        text = compare(_run_file(["Q1", "Q2", "Q3"]), gated)
        assert "topk1@g0.9" in text and "better 1, worse 1, same 1 of 3" in text
        assert "unrouted by the gate at 0.96 (2): c2, c3" in text
        assert "knowledge__1-1" in text

    def test_names_the_collections_a_changed_question_moved_between(self) -> None:
        gated = _run_file(["Q1", "Q2"], gated_delta=[0.0, 0.0], moved=True)
        text = compare(_run_file(["Q1", "Q2"]), gated)
        assert "changed questions (4)" in text  # 2 questions x 2 sets
        assert "Q1 S2 topk1@g0.9: top3 ['a.md'] -> ['b.md']; collections moved: c1 -> c2" in text

    def test_an_unchanged_question_is_not_listed(self) -> None:
        text = compare(_run_file(["Q1"]), _run_file(["Q1"], gated_delta=[0.0]))
        assert "changed questions (0)" in text

    def test_runs_on_the_saved_leg1_file(self) -> None:
        saved = json.loads(SAVED_LEG1.read_text())
        text = compare(saved, saved)
        assert "S2  (saved run -> the gated run's ungated arms)" in text and "band0.03" in text and "(+0.0000)" in text
        assert "paired" not in text  # no gated arms in a saved run
        assert not mismatches(saved, saved)


def _rule_run(n: int = 26, g90: list[float] | None = None, g96: list[float] | None = None,
              ung: list[float] | None = None, cb: list[float] | None = None, s1: float = 0.0,
              gated: bool = True) -> dict[str, Any]:
    """A leg-1 run: OFF is 0.3 on every question and every arm is OFF plus its per-question delta list."""
    sets = {"S1": ["c1"], "S2": ["c1", "c2"]}

    def d(lst: list[float] | None, i: int) -> float:
        return 0.0 if lst is None else lst[i]

    results = []
    for i in range(n):
        s2 = {"OFF": _arm(0.3), "topk1": _arm(0.3 + d(ung, i)), "cband0.03": _arm(0.3)}
        s1_rows = {"OFF": _arm(0.3), "topk1": _arm(0.3 + s1), "cband0.03": _arm(0.3)}
        if gated:
            s2 |= {"topk1@g0.9": _arm(0.3 + d(g90, i)), "topk1@g0.96": _arm(0.3 + d(g96, i)),
                   "cband0.03@g0.9": _arm(0.3 + d(cb, i))}
            s1_rows |= {"topk1@g0.9": s1_rows["topk1"], "topk1@g0.96": s1_rows["topk1"],
                        "cband0.03@g0.9": s1_rows["cband0.03"]}
        results.append({"qid": f"Q{i}", "S1": s1_rows, "S2": s2})
    return {"summary": ab.summarize(results, sets), "sets": sets, "results": results}


GOOD = [0.3] * 13 + [0.0] * 13  # gain 0.15, 13 wins, no loss


def _verdict(text: str) -> str:
    (line,) = [ln for ln in text.splitlines() if ln.startswith("verdict:")]
    return line.removeprefix("verdict: ")


def _block(text: str, arm: str) -> list[str]:
    lines = text.splitlines()
    i = lines.index(arm)
    return lines[i : i + 8]


class TestRuleBlock:
    def test_a_gate_that_changes_nothing_and_keeps_the_gain_stands(self) -> None:
        run = _rule_run(g90=GOOD, g96=GOOD, ung=GOOD, cb=GOOD)
        text = paired_only(run)
        assert _verdict(text) == "stand"
        block = _block(text, "topk1@g0.9")
        assert "gain vs OFF +0.1500; wins 13, losses 0; min per-question delta vs OFF +0.0000" in block[1]
        assert "gated minus ungated mean +0.0000" in block[2] and "S1 delta within the run 0.0000 (no saved run)" in block[2]
        assert block[-1] == "  => PASS"
        assert all(ln.endswith("PASS") for ln in block[3:7])
        assert "cband0.03@g0.9" in text  # the second arm is reported too
        assert "n = 26 questions" in text

    def test_a_gate_that_lifts_a_failing_ungated_arm_helps(self) -> None:
        text = paired_only(_rule_run(g90=GOOD, g96=GOOD))
        assert _verdict(text) == "gate helps"
        assert _block(text, "topk1@g0.9")[6].endswith("FAIL")  # moved +0.15, beyond the tolerance

    def test_the_096_arm_is_reported_beside_the_verdict_and_never_decides_it(self) -> None:
        text = paired_only(_rule_run(g96=GOOD))
        assert _verdict(text) == "design changes"  # topk1@g0.9 has no gain, whatever the 0.96 arm does
        assert _block(text, "topk1@g0.9")[3].endswith("FAIL") and _block(text, "topk1@g0.96")[3].endswith("PASS")

    def test_threshold_needs_work_when_the_gate_passes_but_costs_more_than_the_tolerance(self) -> None:
        text = paired_only(_rule_run(g90=GOOD, g96=GOOD, ung=[0.5] * 26))
        assert _verdict(text) == "threshold needs work"
        assert "gated minus ungated mean -0.3500" in text

    def test_design_changes_when_neither_threshold_gains(self) -> None:
        assert _verdict(paired_only(_rule_run())) == "design changes"

    def test_the_gain_boundary(self) -> None:
        at = [0.21] * 10 + [0.0] * 10  # mean exactly +0.105
        below = [0.2] * 10 + [0.0] * 10  # +0.100
        assert "gain >= +0.105: PASS" in paired_only(_rule_run(20, g90=at, g96=at, ung=at))
        assert "gain >= +0.105: FAIL" in paired_only(_rule_run(20, g90=below, g96=below, ung=below))

    def test_wins_and_losses(self) -> None:
        few_wins = [0.5] * 7 + [0.0] * 19  # gain 0.135, 7 wins
        assert "wins >= 8 and losses <= 1: FAIL" in _block(paired_only(_rule_run(g90=few_wins)), "topk1@g0.9")[4]
        two_losses = [0.3] * 13 + [-0.05, -0.05] + [0.0] * 11
        block = _block(paired_only(_rule_run(g90=two_losses)), "topk1@g0.9")
        assert "losses 2" in block[1] and block[4].endswith("FAIL")
        one_loss = [0.3] * 13 + [-0.05] + [0.0] * 12
        assert _block(paired_only(_rule_run(g90=one_loss)), "topk1@g0.9")[4].endswith("PASS")

    def test_the_worst_question_boundary(self) -> None:
        at = [0.3] * 13 + [-0.1] + [0.0] * 12
        past = [0.3] * 13 + [-0.11] + [0.0] * 12
        ok = _block(paired_only(_rule_run(g90=at)), "topk1@g0.9")
        assert "min per-question delta vs OFF -0.1000" in ok[1] and ok[5].endswith("PASS")
        bad = _block(paired_only(_rule_run(g90=past)), "topk1@g0.9")
        assert "min per-question delta vs OFF -0.1100" in bad[1] and bad[5].endswith("FAIL")

    def test_the_s1_drift_against_the_saved_run_widens_the_tolerance(self) -> None:
        g90, ung = [0.25] * 26, [0.2] * 26  # gated minus ungated +0.05
        alone = paired_only(_rule_run(g90=g90, g96=g90, ung=ung, s1=0.08))
        assert "S1 delta within the run 0.0000" in alone and _verdict(alone) == "gate helps"
        assert _block(alone, "topk1@g0.9")[6].endswith("FAIL")  # +0.05 is beyond the 0.03 floor
        saved = _rule_run(gated=False, s1=0.0)
        both = compare(saved, _rule_run(g90=g90, g96=g90, ung=ung, s1=0.08))
        assert "S1 ON drift vs the saved run 0.0800" in both and "tolerance max(0.03, drift) = 0.0800" in both
        assert _block(both, "topk1@g0.9")[6].endswith("PASS")  # the S1 drift lifts the tolerance to 0.08
        assert _verdict(both) == "gate helps"  # but a gain of +0.05 over ungated is >= +0.03, and that case comes first

    def test_the_second_arm_does_not_decide_the_verdict(self) -> None:
        run = _rule_run(g90=GOOD, g96=GOOD, ung=GOOD, cb=[0.0] * 26)
        text = paired_only(run)
        assert _verdict(text) == "stand" and _block(text, "cband0.03@g0.9")[3].endswith("FAIL")

    def test_a_run_without_gated_arms_says_so(self) -> None:
        assert "not a --gate-from run" in paired_only(_rule_run(gated=False))

    def test_the_verdict_mapping_is_printed(self) -> None:
        text = paired_only(_rule_run())
        assert "design changes = gain < +0.03 or wins <= losses; threshold needs work = gain in [+0.03, +0.105)" in text
        assert "topk1@g0.96 and cband0.03@g0.9 are reported beside it and do not decide" in text


class TestModes:
    def test_one_file_is_the_paired_only_mode(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        f = tmp_path / "gated.json"
        f.write_text(json.dumps(_rule_run(g90=GOOD, g96=GOOD, ung=GOOD)))
        assert compare_main([str(f)]) == 0
        out = capsys.readouterr().out
        assert "paired, gated minus ungated, same run" in out and "verdict: stand" in out
        assert "saved run ->" not in out  # no saved-vs-gated section without a saved file
        assert compare_main([]) == 2 and compare_main([str(f), str(f), str(f)]) == 2

    def test_two_files_that_differ_in_the_live_set_are_refused_and_one_file_still_works(self, tmp_path: Path) -> None:
        saved, live = _rule_run(gated=False), _rule_run(g90=GOOD, g96=GOOD, ung=GOOD)
        live["sets"]["S2"] = ["c1", "c9"]
        assert compare(saved, live).startswith("MISMATCH")
        assert _verdict(paired_only(live)) == "stand"

    def test_the_shape_of_each_output(self) -> None:
        assert shape(_rule_run()) == "leg1"
        assert shape({"summary": {"all": {}}, "prod": [], "results": []}) == "prod"
        assert shape({"summary": {}}) == "unknown"

    def test_other_files_are_refused_with_a_message_not_a_key_error(self, tmp_path: Path,
                                                                    capsys: pytest.CaptureFixture[str]) -> None:
        prod = {"summary": {"all": {}}, "prod": ["x"], "results": []}
        assert compare(prod, _rule_run()).startswith("REFUSED: the saved file is a taxonomy_ab2 (prod) output")
        assert compare(_rule_run(), {"x": 1}).startswith("REFUSED: the gated file is neither")
        f = tmp_path / "junk.json"
        f.write_text("{}")
        assert compare_main([str(f)]) == 1
        assert "REFUSED" in capsys.readouterr().out
        assert paired_only({"nope": 1}).startswith("REFUSED")


class TestProdRuleLines:
    def _prod(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
        cov = _coverage(tmp_path, [_row(GATED, above=False, coverage=0.5), _row(ROUTED, coverage=0.95)])
        return TestProd()._main(tmp_path, monkeypatch, "--gate-from", str(cov))

    def test_a_prod_output_is_read_alone_through_its_vs_ungated_summary(self, tmp_path: Path,
                                                                        monkeypatch: pytest.MonkeyPatch) -> None:
        prod = self._prod(tmp_path, monkeypatch)
        assert shape(prod) == "prod"
        text = paired_only(prod)
        assert "prod rule (all 2 questions" in text
        line = text.splitlines()
        i = next(k for k, ln in enumerate(line) if ln.startswith("top1_F0@g0.9"))
        assert "ungated arm top1_F0: ndcg3 0.0000, wins 0, losses 2" in line[i]
        assert "gain vs OFF +0.0000; wins 0, losses 0; min per-question delta vs OFF +0.0000" in line[i + 1]
        assert "better 2, worse 0, same 0" in line[i + 2] and f"mean +{RANK2:.4f}" in line[i + 2]
        assert "gain FAIL; wins/losses FAIL" in line[i + 3] and "|gated - ungated| FAIL" in line[i + 3]
        assert "verdict" not in text

    def test_a_prod_output_without_gated_arms_says_so(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        prod = self._prod(tmp_path, monkeypatch)
        prod["summary"]["all"] = {k: v for k, v in prod["summary"]["all"].items() if "@g" not in k}
        assert "no @g arms" in paired_only(prod)


def _same(lst: list[float], n: int = 20) -> dict[str, Any]:
    """A run whose ungated arm equals the gated one, so only the gain conditions are in play."""
    return paired_only(_rule_run(n, g90=lst, g96=lst, ung=lst))


class TestVerdictCases:
    """The pre-registered cases and their boundaries (T2 nexus_rdr/134-rescore-rule-2026-11-17)."""

    def test_design_changes_below_a_gain_of_003_and_threshold_needs_work_at_it(self) -> None:
        assert _verdict(_same([0.3, 0.3] + [0.0] * 18)) == "threshold needs work"  # gain exactly +0.03
        assert _verdict(_same([0.29, 0.29] + [0.0] * 18)) == "design changes"  # +0.029

    def test_design_changes_when_wins_do_not_exceed_losses(self) -> None:
        assert _verdict(_same([0.7, -0.05] + [0.0] * 18)) == "design changes"  # gain +0.0325, 1 win, 1 loss
        assert _verdict(_same([0.7, 0.1, -0.05] + [0.0] * 17)) == "threshold needs work"  # 2 wins, 1 loss
        assert _verdict(_same([-0.05, 0.0] * 10)) == "design changes"  # no gain at all

    def test_a_gain_between_003_and_0105_is_threshold_needs_work_and_0105_is_not(self) -> None:
        assert _verdict(_same([0.2] * 10 + [0.0] * 10)) == "threshold needs work"  # +0.100
        assert _verdict(_same([0.21] * 10 + [0.0] * 10)) == "stand"  # exactly +0.105, the gate does nothing else

    def test_two_questions_worse_than_ungated_at_minus_003_stand_and_three_are_threshold_needs_work(self) -> None:
        g = [0.3] * 10 + [0.0] * 10  # gain +0.15
        three = [0.3] * 10 + [0.2] * 3 + [0.0] * 7  # ungated mean +0.18: gated minus ungated -0.03, 3 worse
        two = [0.3] * 12 + [0.0] * 8  # also +0.18, 2 worse
        t3 = paired_only(_rule_run(20, g90=g, g96=g, ung=three))
        t2 = paired_only(_rule_run(20, g90=g, g96=g, ung=two))
        assert "gated minus ungated mean -0.0300 (3 questions worse than ungated)" in t3
        assert _verdict(t3) == "threshold needs work"
        assert "gated minus ungated mean -0.0300 (2 questions worse than ungated)" in t2
        assert _verdict(t2) == "stand"  # inside the tolerance, and too few questions to call it a loss

    def test_a_gate_helps_at_plus_003_and_stands_just_under(self) -> None:
        g = [0.3] * 10 + [0.0] * 10
        at = [0.3] * 8 + [0.0] * 12  # ungated +0.12
        under = [0.3] * 8 + [0.02] + [0.0] * 11  # ungated +0.121, a gap of +0.029
        assert _verdict(paired_only(_rule_run(20, g90=g, g96=g, ung=at))) == "gate helps"
        assert _verdict(paired_only(_rule_run(20, g90=g, g96=g, ung=under))) == "stand"

    def test_a_result_matching_no_case_prints_the_numbers_not_a_guess(self) -> None:
        worst_question = [0.3] * 13 + [-0.11] + [0.0] * 12  # gain +0.146, but one question is 0.11 below OFF
        text = _same(worst_question, 26)
        line = _verdict(text)
        assert line.startswith("no rule case matched: gain +0.1458, wins 13, losses 1, worst question -0.1100")
        assert "gated minus ungated +0.0000 (0 questions worse than ungated)" in line
        # a movement past the tolerance with only 2 questions worse is the other way to land here
        g = [0.3] * 10 + [0.0] * 10
        ung = [0.3] * 10 + [0.4, 0.4] + [0.0] * 8
        assert _verdict(paired_only(_rule_run(20, g90=g, g96=g, ung=ung))).startswith("no rule case matched")

    def test_the_order_of_precedence(self) -> None:
        # design changes beats a hurt gate: gain +0.0 with ungated far above
        assert _verdict(paired_only(_rule_run(20, ung=[0.3] * 20))) == "design changes"
        # threshold needs work (gain band) beats gate helps (+0.0325 is 0.0325 above an ungated zero)
        assert _verdict(paired_only(_rule_run(20, g90=[0.65] + [0.0] * 19, g96=[0.65] + [0.0] * 19))) == "threshold needs work"
