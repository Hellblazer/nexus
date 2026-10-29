# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-232 Phase 3 measurement (nexus-jypmj.3.10, critique S3): drafted labels, scored apart, with an interval.

Sam's 2026-10-28 decision: an agent that never sees the assessor ledger drafts the oracle labels from the run's
retrieved items (provenance ``drafted``), Sam spot-checks a sample, and drafted and hand labels are reported apart. The
critique's points: a fourth provenance and a split of every rate (a); roughly 97 percent of the pairs would be drafted
(b); the G01-G03 labels are document-level and charge the assessor a false negative for a chunk it rightly found empty
(c); 15 pairs bound label error poorly, so the sample is stratified and every assessor/drafter disagreement is in it
(d). Model-free: the cases are built by hand.
"""
from __future__ import annotations

import hashlib
import math
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from bench import rdr232_oracle as om  # noqa: E402
from bench.rdr232_facet_map import FacetResolution  # noqa: E402
from bench.schema import load_golden_set  # noqa: E402

GOLDEN = load_golden_set(om.GOLDEN_PATH)
HELDOUT = load_golden_set(om.HELDOUT_PATH)
ALL = [*GOLDEN, *HELDOUT]
BY_QID = {q.qid: q for q in ALL}
G06 = BY_QID["G06-synthesis-art-rdr084"]
SLUGS = tuple(G06.required_facets)


def _h(n: int) -> str:
    return hashlib.sha256(f"chunk-{n}".encode()).hexdigest()


def _resolution(query=G06) -> FacetResolution:
    slugs = tuple(query.required_facets)
    fid_to_slugs = {f"f{i + 1}": (s,) for i, s in enumerate(slugs)}
    return FacetResolution(
        query.qid, fid_to_slugs, {s: (f"f{i + 1}",) for i, s in enumerate(slugs)}, (), (), {f: "entry" for f in fid_to_slugs},
    )


def _drafted_oracle(qid: str = G06.qid, *, supports_items: int = 12, neutral_items: int = 8, slugs: tuple[str, ...] = SLUGS) -> om.RequestOracle:
    """Items 0..supports_items-1 each support one slug (round robin); the next ``neutral_items`` are closed (neutral for
    every slug)."""
    items: list[om.ItemLabel] = []
    for i in range(supports_items):
        items.append(om.ItemLabel(chash=_h(i), closed=False, facets={slugs[i % len(slugs)]: "supports"}))
    for j in range(neutral_items):
        items.append(om.ItemLabel(chash=_h(supports_items + j), closed=True, facets={}))
    return om.RequestOracle(qid, slugs, "drafted", tuple(items))


def _coverage(n_anchors: int, findings: list[dict[str, Any]]) -> dict[str, Any]:
    return {"anchors": [{"item_id": f"e{i + 1}", "chash": _h(i)} for i in range(n_anchors)], "findings": findings, "skip_generation": False}


def _finding(fid: str, state: str, cited: list[int], reason: str = "") -> dict[str, Any]:
    return {"facet_id": fid, "state": state, "evidence_ids": [f"e{i + 1}" for i in cited], "reason": reason}


# ── the vocabulary ───────────────────────────────────────────────────────────────────────────────────────


def test_drafted_is_a_provenance_and_a_ledger_still_is_not() -> None:
    assert "drafted" in om.PROVENANCES and "ledger" not in om.PROVENANCES
    assert [om.provenance_group(p) for p in ("fixture", "ground_truth", "human", "drafted")] == ["hand", "hand", "hand", "drafted"]
    with pytest.raises(ValueError, match="provenance"):
        om.provenance_group("ledger")


# ── the loader merges a separate drafted file ────────────────────────────────────────────────────────────


def _write(tmp_path: Path, requests: dict[str, Any], name: str = "drafted.yaml") -> Path:
    p = tmp_path / name
    p.write_text(yaml.safe_dump({"oracle_version": 1, "golden_set_version": 1, "requests": requests}), encoding="utf-8")
    return p


def _drafted_entry(slug: str) -> dict[str, Any]:
    return {"provenance": "drafted", "items": [{"chash": _h(1), "closed": False, "facets": {slug: "supports"}}]}


def test_a_drafted_file_fills_the_requests_the_hand_file_left_todo(tmp_path: Path) -> None:
    hand_only = om.load_oracle(om.DEFAULT_ORACLE_PATH, GOLDEN)
    drafted = _write(tmp_path, {"G04-lookup-min-confidence": _drafted_entry(BY_QID["G04-lookup-min-confidence"].required_facets[0])})
    merged = om.load_oracle(om.DEFAULT_ORACLE_PATH, GOLDEN, drafted_path=drafted)
    r = merged.requests["G04-lookup-min-confidence"]
    assert r.provenance == "drafted" and not r.todo
    assert merged.requests["G06-synthesis-art-rdr084"] == hand_only.requests["G06-synthesis-art-rdr084"], "hand labels are untouched"
    assert merged.summary()["pairs_todo"] == hand_only.summary()["pairs_todo"] - 1
    assert merged.provenance_summary() == {
        "hand": {"requests": 4, "pairs": 9}, "drafted": {"requests": 1, "pairs": 1},
    }


def test_a_drafted_label_may_not_shadow_a_hand_label(tmp_path: Path) -> None:
    drafted = _write(tmp_path, {"G06-synthesis-art-rdr084": _drafted_entry("evidence")})
    with pytest.raises(ValueError, match="shadow"):
        om.load_oracle(om.DEFAULT_ORACLE_PATH, GOLDEN, drafted_path=drafted)


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        ({"provenance": "human", "items": [{"chash": _h(1), "closed": True, "facets": {}}]}, "drafted"),
        ({"provenance": "drafted", "todo": True, "items": []}, "items"),
    ],
    ids=["wrong_provenance", "empty"],
)
def test_the_drafted_file_holds_only_drafted_entries_with_items(tmp_path: Path, entry, message) -> None:
    drafted = _write(tmp_path, {"G04-lookup-min-confidence": entry})
    with pytest.raises(ValueError, match=message):
        om.load_oracle(om.DEFAULT_ORACLE_PATH, GOLDEN, drafted_path=drafted)


def test_a_drafted_label_for_an_unknown_request_or_slug_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown request"):
        om.load_oracle(om.DEFAULT_ORACLE_PATH, GOLDEN, drafted_path=_write(tmp_path, {"G99": _drafted_entry("x")}))
    with pytest.raises(ValueError, match="not a required facet"):
        om.load_oracle(om.DEFAULT_ORACLE_PATH, GOLDEN, drafted_path=_write(tmp_path, {"G04-lookup-min-confidence": _drafted_entry("no-such-slug")}, "d2.yaml"))


def test_a_named_drafted_file_that_does_not_exist_is_an_error_not_an_empty_set(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        om.load_oracle(om.DEFAULT_ORACLE_PATH, GOLDEN, drafted_path=tmp_path / "absent.yaml")


def test_the_committed_drafted_file_is_a_loadable_empty_skeleton() -> None:
    assert om.DEFAULT_DRAFTED_PATH.name == "golden_oracle_labels_drafted_v1.yaml" and om.DEFAULT_DRAFTED_PATH.is_file()
    assert om.load_oracle(om.DEFAULT_ORACLE_PATH, GOLDEN, drafted_path=om.DEFAULT_DRAFTED_PATH).summary() == om.load_oracle(om.DEFAULT_ORACLE_PATH, GOLDEN).summary()


# ── document-level labels ────────────────────────────────────────────────────────────────────────────────


def test_a_path_label_is_document_level_and_a_chash_label_is_chunk_level() -> None:
    assert om.ItemLabel(doc_path_contains="rdr-049-", facets={"x": "supports"}).level == "document"
    assert om.ItemLabel(chash=_h(1), facets={"x": "supports"}).level == "chunk"


def test_the_committed_ground_truth_items_say_they_are_document_level() -> None:
    on_disk = yaml.safe_load(om.DEFAULT_ORACLE_PATH.read_text(encoding="utf-8"))["requests"]
    for qid in ("G01-lookup-tumblers", "G02-lookup-chash", "G03-lookup-taxonomy"):
        assert [i["level"] for i in on_disk[qid]["items"]] == ["document"], qid


def test_a_level_that_contradicts_the_item_key_is_refused(tmp_path: Path) -> None:
    data = yaml.safe_load(om.DEFAULT_ORACLE_PATH.read_text(encoding="utf-8"))
    data["requests"]["G01-lookup-tumblers"]["items"][0]["level"] = "chunk"
    bad = tmp_path / "o.yaml"
    bad.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ValueError, match="level"):
        om.load_oracle(bad, GOLDEN)


def _g01_scores(assessor_state: str) -> om.RunScore:
    """G01's one facet, a hand document-level label, an assessor that rightly found nothing in the chunk it was shown."""
    o = om.load_oracle(om.DEFAULT_ORACLE_PATH, GOLDEN).requests["G01-lookup-tumblers"]
    cov = {"anchors": [{"item_id": "e1", "chash": _h(1), "tumbler": "1.1.2"}], "skip_generation": False,
           "findings": [{"facet_id": "f1", "state": assessor_state, "evidence_ids": [], "reason": "no_supporting_evidence"}]}
    res = FacetResolution(o.qid, {"f1": ("introducing-rdr",)}, {"introducing-rdr": ("f1",)}, (), (), {"f1": "sole_facet"})
    return om.score_run(o, cov, res, doc_path_of=lambda a: "docs/rdr/rdr-049-catalog-tumblers.md")


def test_a_document_level_false_negative_is_kept_out_of_the_chunk_level_rate_and_scored_apart() -> None:
    score = _g01_scores("unsupported")
    assert score.facets[0].oracle_state == "supported" and score.facets[0].oracle_level == "document"
    hand = om.aggregate([score])["hand"]
    assert hand["false_negative"]["denominator"] == 0 and hand["false_negative"]["rate"] is None, "not charged to the chunk-level rate"
    doc = hand["document_level"]["false_negative"]
    assert (doc["numerator"], doc["denominator"], doc["rate"]) == (1, 1, 1.0)
    assert "document" in hand["document_level"]["note"].lower()


# ── every rate is split by provenance and carries an interval ────────────────────────────────────────────


def test_wilson_interval_values() -> None:
    lo, hi = om.wilson_interval(0, 15)
    assert lo == 0.0 and hi == pytest.approx(3.8416 / (15 + 3.8416), abs=1e-4), "0 errors in 15 still leaves an upper bound near 20 percent"
    lo, hi = om.wilson_interval(1, 6)
    assert (lo, hi) == (pytest.approx(0.0300, abs=1e-3), pytest.approx(0.5635, abs=1e-3))
    lo, hi = om.wilson_interval(110, 110)
    assert hi == 1.0 and lo > 0.95
    assert om.wilson_interval(0, 0) is None
    with pytest.raises(ValueError):
        om.wilson_interval(3, 2)


def _score(oracle: om.RequestOracle, cov: dict[str, Any]) -> om.RunScore:
    return om.score_run(oracle, cov, _resolution(BY_QID[oracle.qid]))


def _fn_case(provenance: str) -> om.RunScore:
    base = _drafted_oracle()
    o = om.RequestOracle(base.qid, base.slugs, provenance, base.items)
    findings = [_finding(f"f{i + 1}", "covered", [i, i + 6]) for i in range(5)] + [_finding("f6", "unsupported", [], "no_supporting_evidence")]
    return _score(o, _coverage(20, findings))


def test_aggregate_never_pools_hand_and_drafted() -> None:
    agg = om.aggregate([_fn_case("fixture"), _fn_case("drafted"), _fn_case("drafted")])
    assert set(agg) == {"hand", "drafted"}
    assert (agg["hand"]["runs"], agg["drafted"]["runs"]) == (1, 2)
    assert agg["hand"]["false_negative"]["numerator"] == 1 and agg["hand"]["false_negative"]["denominator"] == 6
    assert agg["drafted"]["false_negative"]["numerator"] == 2 and agg["drafted"]["false_negative"]["denominator"] == 12
    assert agg["drafted"]["provenances"] == {"drafted": 2} and agg["hand"]["provenances"] == {"fixture": 1}


def test_a_group_with_no_runs_reports_none_not_zero() -> None:
    agg = om.aggregate([_fn_case("drafted")])
    assert agg["hand"]["runs"] == 0 and agg["hand"]["false_negative"]["rate"] is None and agg["hand"]["false_negative"]["ci95"] is None


def test_every_reported_rate_carries_a_95_percent_interval() -> None:
    d = om.aggregate([_fn_case("drafted")])["drafted"]
    fn = d["false_negative"]
    lo, hi = fn["ci95"]
    assert fn["rate"] == pytest.approx(1 / 6) and lo == pytest.approx(0.0300, abs=1e-3) and hi == pytest.approx(0.5635, abs=1e-3)
    assert d["spurious_contradicts"]["ci95"] is None, "no flagged facets: no interval, never [0, 0]"
    for block, key in ((d["false_skip"], "ci95_of_skips"), (d["false_skip"], "ci95_of_runs"), (d["negative_control"], "falsely_covered_ci95"),
                       (d["spurious_contradicts"], "ci95"), (d["false_negative"], "ci95"),
                       (d["document_level"]["false_negative"], "ci95"), (d["document_level"]["false_skip"], "ci95_of_skips")):
        assert key in block, (block, key)


# ── the spot-check sample ────────────────────────────────────────────────────────────────────────────────


def _case(oracle: om.RequestOracle | None = None, findings: list[dict[str, Any]] | None = None) -> om.SpotCase:
    o = oracle or _drafted_oracle()
    # f1..f5 covered citing their own supporting items; f1 ALSO cites a neutral item (disagreement); f6 unsupported
    # although two items support it (disagreement on those two pairs)
    fs = findings or ([_finding("f1", "covered", [0, 6, 12])] + [_finding(f"f{i + 1}", "covered", [i, i + 6]) for i in range(1, 5)]
                      + [_finding("f6", "unsupported", [], "no_supporting_evidence")])
    return om.SpotCase(o, _coverage(20, fs), _resolution(BY_QID[o.qid]))


def test_the_sample_is_stratified_and_holds_every_disagreement() -> None:
    sample = om.spot_check_sample([_case()], seed=7)
    by = {}
    for p in sample.pairs:
        by.setdefault(p.stratum, []).append(p)
    assert {(p.item_id, p.slug) for p in by["disagreement"]} == {
        ("e6", SLUGS[5]), ("e12", SLUGS[5]),  # the facet the assessor left unsupported: both pairs the drafter says support it
        ("e13", SLUGS[0]),                     # a neutral item the assessor cited for f1
    }
    assert len(by["supports_multi_facet"]) == 5 and len(by["neutral"]) == 3
    assert all(p.drafted_role == "supports" for p in by["supports_multi_facet"]) and all(p.drafted_role == "neutral" for p in by["neutral"])
    assert not {(p.item_id, p.slug) for p in by["disagreement"]} & {(p.item_id, p.slug) for p in by["supports_multi_facet"] + by["neutral"]}, (
        "the strata are drawn from the agreeing pairs, so the sample is not made of the pairs most likely to be wrong")
    assert sample.shortfall == {} and sample.pool["supports_multi_facet"] == 10


def test_every_sampled_pair_starts_unchecked_and_the_rows_say_so() -> None:
    sample = om.spot_check_sample([_case()], seed=1)
    rows = sample.rows()
    assert len(rows) == len(sample.pairs) == 11
    assert all(r["checked"] is False and r["label_correct"] is None for r in rows)
    assert set(rows[0]) == {"qid", "item_id", "chash", "slug", "drafted_role", "stratum", "checked", "label_correct"}


def test_the_sample_is_deterministic_for_a_seed_and_varies_with_it() -> None:
    a = [(p.item_id, p.slug) for p in om.spot_check_sample([_case()], seed=3).pairs]
    b = [(p.item_id, p.slug) for p in om.spot_check_sample([_case()], seed=3).pairs]
    c = [(p.item_id, p.slug) for p in om.spot_check_sample([_case()], seed=4).pairs]
    assert a == b and a != c


def test_a_pool_too_small_for_a_stratum_is_a_recorded_shortfall_not_a_quiet_smaller_sample() -> None:
    small = _drafted_oracle(supports_items=2, neutral_items=1, slugs=SLUGS)
    findings = [_finding(f"f{i + 1}", "unsupported", [], "no_supporting_evidence") for i in range(6)]
    case = om.SpotCase(small, {"anchors": [{"item_id": f"e{i + 1}", "chash": _h(i)} for i in range(3)], "findings": findings, "skip_generation": False},
                       _resolution())
    sample = om.spot_check_sample([case])
    # both supporting pairs are disagreements (their facets were left unsupported), so none is left for the stratum
    assert sample.shortfall == {"supports_multi_facet": 5}
    assert sample.pool["supports_multi_facet"] == 0 and sample.pool["disagreement"] == 2
    assert sum(1 for p in sample.pairs if p.stratum == "neutral") == 3 and sum(1 for p in sample.pairs if p.stratum == "disagreement") == 2


def test_a_single_facet_request_does_not_count_toward_the_multi_facet_supports_stratum() -> None:
    q = BY_QID["G04-lookup-min-confidence"]
    o = _drafted_oracle(q.qid, supports_items=6, neutral_items=0, slugs=tuple(q.required_facets))
    cov = {"anchors": [{"item_id": f"e{i + 1}", "chash": _h(i)} for i in range(6)], "skip_generation": False,
           "findings": [_finding("f1", "covered", [0, 1, 2, 3, 4, 5])]}
    sample = om.spot_check_sample([om.SpotCase(o, cov, _resolution(q))])
    assert sample.pool["supports_multi_facet"] == 0 and sample.shortfall["supports_multi_facet"] == 5


def test_hand_labelled_requests_are_not_in_the_drafted_sample() -> None:
    base = _drafted_oracle()
    hand = om.RequestOracle(base.qid, base.slugs, "fixture", base.items)
    sample = om.spot_check_sample([_case(hand)])
    assert sample.pairs == () and sample.pool == {"supports_multi_facet": 0, "neutral": 0, "disagreement": 0}


def test_spot_check_marks_are_written_read_and_summarised_with_an_interval(tmp_path: Path) -> None:
    sample = om.spot_check_sample([_case()], seed=2)
    path = tmp_path / "spot.yaml"
    om.write_spot_check(path, sample)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert data["spot_check_version"] == 1 and data["shortfall"] == {} and len(data["pairs"]) == 11
    s0 = om.summarize_spot_check(path)
    assert (s0["sampled"], s0["checked"], s0["unchecked"], s0["error_rate"]) == (11, 0, 11, None)
    for i, p in enumerate(data["pairs"][:8]):  # Sam checks 8 pairs and finds 1 wrong
        p["checked"], p["label_correct"] = True, i != 0
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    s = om.summarize_spot_check(path)
    assert (s["checked"], s["unchecked"], s["errors"]) == (8, 3, 1) and s["error_rate"] == pytest.approx(1 / 8)
    lo, hi = s["error_rate_ci95"]
    assert lo == pytest.approx(0.0224, abs=1e-3) and hi == pytest.approx(0.4710, abs=1e-3)
    assert math.isclose(sum(v["sampled"] for v in s["by_stratum"].values()), 11)


def test_a_checked_pair_must_say_whether_the_label_was_right(tmp_path: Path) -> None:
    sample = om.spot_check_sample([_case()])
    path = tmp_path / "spot.yaml"
    om.write_spot_check(path, sample)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["pairs"][0]["checked"] = True
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ValueError, match="label_correct"):
        om.summarize_spot_check(path)
