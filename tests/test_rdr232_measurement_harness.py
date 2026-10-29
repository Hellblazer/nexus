# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-232 Phase 3 measurement harness (nexus-jypmj.3.10): facet map, envelope adapter, oracle scorer and driver.

Tenant-free and model-free. The envelope the adapter and scorer read is a RECORDED one
(``tests/fixtures/rdr232_envelope_g06_stub.json``): the real ``nx_answer`` and the real coverage boundary run over the
G06 fixture pieces with the model call stubbed at the dispatcher, by an assessor that is wrong in two known ways (it
never finds the evidence facet; it contradicts the numerical-conventions facet from an item the oracle calls
neutral). The scorer's expected numbers are therefore known in advance, not read back from the code under test. It
is NOT a live-model recording. To re-record after an envelope change::

    NX_RDR232_RECORD_ENVELOPE=1 NX_TEST_T2_SUBSTRATE=none uv run pytest tests/test_rdr232_measurement_harness.py -k fixture_is_current
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from bench import rdr232_oracle as oracle_mod  # noqa: E402
from bench import rdr232_phase3_measure as driver  # noqa: E402
from bench.answer_shape_metrics import NotMeasurable, compute_measures  # noqa: E402
from bench.rdr232_envelope import (  # noqa: E402
    assessment_usd,
    envelope_facts,
    observation_from_envelope,
    repair_by_origin,
)
from bench.rdr232_facet_map import DEFAULT_MAP_PATH, load_facet_map, resolve_facets  # noqa: E402
from bench.schema import load_golden_set  # noqa: E402

from nexus.mcp import core  # noqa: E402
from nexus.plans import switches  # noqa: E402
from nexus.plans.request_coverage import derive_request_coverage  # noqa: E402
from tests._rdr232_golden import ART_RDR_PIECES  # noqa: E402
from tests.test_rdr232_coverage_ledger import RDR_COLL, Dispatcher, _structured, _tag_lines  # noqa: E402
from tests.test_rdr232_coverage_wiring import _drive  # noqa: E402

FIXTURE = REPO / "tests" / "fixtures" / "rdr232_envelope_g06_stub.json"
GOLDEN = load_golden_set(driver.GOLDEN_PATH)
HELDOUT = load_golden_set(driver.HELDOUT_PATH)
ALL = [*GOLDEN, *HELDOUT]
BY_QID = {q.qid: q for q in ALL}
G06 = BY_QID["G06-synthesis-art-rdr084"]
PIECES = [text for _f, text in ART_RDR_PIECES]
LABEL_OF = {text: facet for facet, text in ART_RDR_PIECES}


# ── recording the fixture ────────────────────────────────────────────────────────────────────────────────


class _G06Dispatcher(Dispatcher):
    """Retrieval answers with the 12 fixture pieces (seven by search, five by traverse), so each anchor's chash is the
    fixture's own chunk hash and the oracle's chash labels apply."""

    async def __call__(self, tool: str, args: dict) -> dict:
        if tool == "search":
            self.calls.append((tool, dict(args)))
            return _structured(PIECES[:7], RDR_COLL)
        if tool == "traverse":
            self.calls.append((tool, dict(args)))
            return _structured(PIECES[7:], RDR_COLL) | {"traversal": {"policy_applied": True, "widened": None}}
        if tool == "store_get_many":
            self.calls.append((tool, dict(args)))
            return {"contents": list(PIECES)}
        return await super().__call__(tool, args)


def _flawed_assessor(items: str, instruction: str) -> dict:
    """Supports each facet from the items the FIXTURE authored for it, except (1) it never finds the evidence facet
    (a false negative) and (2) it contradicts numerical-conventions from the open-by-design item (spurious)."""
    slug_of = dict(zip((f"f{i}" for i in range(1, 7)), G06.required_facets, strict=True))
    payload = json.loads(items)
    records: list[dict[str, str]] = []
    for tag in _tag_lines(instruction):
        slug = slug_of[tag]
        for item in payload:
            label = LABEL_OF.get(item["text"])
            if label == slug and slug != "evidence":
                records.append({"item_id": item["id"], "quote": f"[{tag}] quoted", "role": "supports"})
            if slug == "numerical-conventions" and label == "open-by-design":
                records.append({"item_id": item["id"], "quote": f"[{tag}] the opposite", "role": "contradicts"})
    if not records:
        records.append({"item_id": payload[0]["id"], "quote": f"[{_tag_lines(instruction)[0]}] only mentions the topic", "role": "neutral"})
    return {"ok": True, "evidence": records}


async def _record_g06() -> dict[str, Any]:
    derived = derive_request_coverage(G06.text)
    # the request's own source hints (RDR-084@ART) are dropped: no named source is bound in this stubbed run, and a
    # hint with nothing resolved would decide every facet deterministically (`source_missing`), before the assessor
    request = replace(derived, source_hints=(), named_sources=(), facets=tuple(replace(f, source_hints=()) for f in derived.facets))
    with patch("nexus.plans.runner._resolve_non_evidentiary_chashes", return_value=set()):
        run = await _drive(plan_id=7, dispatcher=_G06Dispatcher(_flawed_assessor), request=request)
    return json.loads(json.dumps(run.out, default=str))


def _shape(value: Any) -> Any:
    """The key structure of a JSON value (lists collapse to the union of their elements' shapes)."""
    if isinstance(value, dict):
        return {k: _shape(v) for k, v in sorted(value.items())}
    if isinstance(value, list):
        merged: dict[str, Any] = {}
        for item in value:
            s = _shape(item)
            if isinstance(s, dict):
                merged.update(s)
        return [merged] if merged else []
    return type(value).__name__ if value is not None else "NoneType"


@pytest.fixture(autouse=True)
def _coverage_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(switches.ANSWER_COVERAGE_ENV, "1")


@pytest.fixture(scope="module")
def envelope() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.mark.asyncio
async def test_the_recorded_fixture_is_current(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fixture has the key structure a fresh run of the same harness produces, so an envelope change that the
    adapter has not met fails here instead of in a live run."""
    fresh = await _record_g06()
    if os.environ.get("NX_RDR232_RECORD_ENVELOPE"):
        FIXTURE.parent.mkdir(parents=True, exist_ok=True)
        FIXTURE.write_text(json.dumps(fresh, indent=1, sort_keys=True, default=str) + "\n", encoding="utf-8")
        pytest.skip(f"recorded {FIXTURE}")
    assert FIXTURE.is_file(), "record it with NX_RDR232_RECORD_ENVELOPE=1 (see the module docstring)"
    assert _shape(json.loads(FIXTURE.read_text(encoding="utf-8"))) == _shape(fresh)


# ── the facet map ────────────────────────────────────────────────────────────────────────────────────────


def _derived_facets(q) -> list[dict[str, Any]]:
    return [{"id": f.id, "text": f.text, "origin": f.origin} for f in derive_request_coverage(q.text).facets]


def test_the_map_resolves_every_golden_request_to_its_slugs_except_the_recorded_todo() -> None:
    entries = load_facet_map(DEFAULT_MAP_PATH, ALL)
    incomplete = {
        q.qid: (r.unmapped_fids, r.unmapped_slugs)
        for q in ALL if not (r := resolve_facets(q, _derived_facets(q), entries.get(q.qid, ()))).complete
    }
    assert set(incomplete) == {"H02"}, f"requests whose facets or slugs the map leaves unmapped: {incomplete}"
    assert incomplete["H02"][0] == ("f1", "f2", "f3"), "H02's three loosely worded clauses have no clean slug each: the recorded TODO"


def test_g06_runtime_facets_map_one_to_one_by_text_not_by_number() -> None:
    entries = load_facet_map(DEFAULT_MAP_PATH, ALL)
    facets = _derived_facets(G06)
    res = resolve_facets(G06, facets, entries["G06-synthesis-art-rdr084"])
    assert [res.fid_to_slugs[f"f{i}"] for i in range(1, 7)] == [(s,) for s in G06.required_facets]
    shuffled = list(reversed(facets))
    res2 = resolve_facets(G06, shuffled, entries["G06-synthesis-art-rdr084"])
    assert res2.fid_to_slugs == res.fid_to_slugs, "the join is on facet text, so the order of the list does not matter"


def test_a_sole_runtime_facet_carries_every_slug_and_an_unmatched_facet_is_reported_not_guessed() -> None:
    sole = resolve_facets(BY_QID["G09-synthesis-retrieval-evolution"], [{"id": "f1", "text": "anything"}])
    assert sole.fid_to_slugs["f1"] == tuple(BY_QID["G09-synthesis-retrieval-evolution"].required_facets) and sole.via["f1"] == "sole_facet"
    two = resolve_facets(G06, [{"id": "f1", "text": "no entry matches"}, {"id": "f2", "text": "nor this"}])
    assert two.unmapped_fids == ("f1", "f2") and set(two.unmapped_slugs) == set(G06.required_facets) and not two.complete


def test_the_loader_refuses_a_slug_the_request_does_not_require(tmp_path: Path) -> None:
    bad = tmp_path / "map.yaml"
    bad.write_text(yaml.safe_dump({
        "golden_set_version": 1, "facet_map_version": 1,
        "requests": {"G06-synthesis-art-rdr084": [{"match": "x", "slugs": ["not-a-slug"]}]},
    }))
    with pytest.raises(ValueError, match="does not require"):
        load_facet_map(bad, ALL)


# ── the oracle label file ────────────────────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def oracle() -> oracle_mod.OracleSet:
    return oracle_mod.load_oracle(oracle_mod.DEFAULT_ORACLE_PATH, ALL)


def test_the_committed_fixture_and_ground_truth_labels_are_the_mechanical_derivation(oracle) -> None:
    """Labels of provenance fixture or ground_truth are re-derived from their source (the authored pieces, the hand
    ground_truth grades) and must equal the file, so none of them was typed or read off a ledger."""
    derived = oracle_mod.scaffold(ALL, fixture={"G06-synthesis-art-rdr084": list(ART_RDR_PIECES)})["requests"]
    on_disk = yaml.safe_load(oracle_mod.DEFAULT_ORACLE_PATH.read_text(encoding="utf-8"))["requests"]
    mechanical = [qid for qid, r in on_disk.items() if r["provenance"] in ("fixture", "ground_truth")]
    assert set(mechanical) == {"G01-lookup-tumblers", "G02-lookup-chash", "G03-lookup-taxonomy", "G06-synthesis-art-rdr084"}
    for qid in mechanical:
        assert on_disk[qid] == derived[qid], qid


def test_the_label_scaffold_counts_what_a_human_pass_still_owes(oracle) -> None:
    s = oracle.summary()
    assert s == {"requests": 53, "requests_labelled": 4, "requests_todo": 49, "request_facet_pairs": 113,
                 "pairs_labelled": 9, "pairs_todo": 104}
    assert len(oracle.todo_pairs()) == 104 and ("G04-lookup-min-confidence", "default-min-confidence") in oracle.todo_pairs()


def test_g06_labels_every_authored_piece_and_names_each_required_facet_once_or_more(oracle) -> None:
    g06 = oracle.requests["G06-synthesis-art-rdr084"]
    assert len(g06.items) == len(ART_RDR_PIECES) and g06.labelled_slugs() == set(G06.required_facets)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda d: d["requests"]["G04-lookup-min-confidence"].update(provenance="ledger"), "circular"),
        (lambda d: d["requests"].pop("G04-lookup-min-confidence"), "without an entry"),
        (lambda d: d["requests"]["G06-synthesis-art-rdr084"]["items"][2]["facets"].update({"not-a-facet": "supports"}), "not a required facet"),
        (lambda d: d["requests"]["G06-synthesis-art-rdr084"]["items"][2]["facets"].update({"evidence": "maybe"}), "must be one of"),
        (lambda d: d["requests"]["G04-lookup-min-confidence"].update(todo=False), "not both and not neither"),
    ],
    ids=["ledger_provenance", "missing_request", "unknown_slug", "bad_role", "todo_flag_wrong"],
)
def test_the_loader_refuses_a_label_file_that_is_circular_incomplete_or_malformed(tmp_path: Path, mutation, message) -> None:
    data = yaml.safe_load(oracle_mod.DEFAULT_ORACLE_PATH.read_text(encoding="utf-8"))
    mutation(data)
    bad = tmp_path / "oracle.yaml"
    bad.write_text(yaml.safe_dump(data))
    with pytest.raises(ValueError, match=message):
        oracle_mod.load_oracle(bad, ALL)


def test_there_is_no_provenance_that_reads_a_ledger() -> None:
    assert "ledger" not in oracle_mod.PROVENANCES and "assessor" not in oracle_mod.PROVENANCES


# ── the adapter and the scorer over the recorded envelope ────────────────────────────────────────────────


def test_the_recorded_envelope_is_the_flawed_assessors_run(envelope) -> None:
    f = envelope_facts(envelope)
    assert f["state"] == "partial" and f["facets"] == {"covered": 5, "unsupported": 1, "contradicted": 0, "unassessed": 0}
    assert f["facets_conflicted"] == 1 and f["repair_entered"] is True and f["repair_outcome"] == "no_new_evidence"
    assert f["reply_classes"] == {"support": 2, "neutral_or_contradict": 0, "all_rejected": 0, "no_records": 0, "raised": 0}
    assert f["call_log"] == [["vector", 6, "support", 3, 0], ["graph", 6, "support", 6, 0]]
    assert (f["accepted_records"], f["failure_cause"], f["persistence"]) == (9, "none", "persisted")
    assert f["code_anchor_share"] == 0.0


def test_the_adapter_builds_a_run_observation_from_the_envelope(envelope, oracle) -> None:
    obs = observation_from_envelope(G06.qid, envelope, oracle=oracle.requests[G06.qid])
    assert obs.qid == G06.qid and obs.evidence is not None and len(obs.evidence) == len(PIECES)
    assert {e.corpus for e in obs.evidence} == {"rdr"}
    labelled = [e for e in obs.evidence if e.facets]
    assert len(labelled) == 10, "ten of the twelve pieces were authored to evidence a facet; the other two evidence none"
    assert obs.repair is not None and (obs.repair.missing, obs.repair.repaired) == (1, 0)
    assert obs.terminal is None and obs.selected_plan is None
    unlabelled = observation_from_envelope(G06.qid, envelope)
    assert all(e.facets is None for e in unlabelled.evidence or []), "no oracle, no facet labels: never taken from the ledger"


def test_the_observation_feeds_the_existing_measures(envelope, oracle) -> None:
    obs = observation_from_envelope(G06.qid, envelope, oracle=oracle.requests[G06.qid])
    m = compute_measures([G06], [obs])
    assert m["facet_evidence_coverage"].value == 1.0, "every required facet has an oracle-labelled item among the evidence"
    assert m["repair_yield_and_cost"].value == 0.0 and m["repair_yield_and_cost"].n == 1
    assert isinstance(m["terminal_completeness"].value, NotMeasurable)
    no_coverage = dict(envelope, coverage={"state": "unassessed", "anchors": [], "findings": [], "repair": {"outcome": "not_attempted"}})
    assert observation_from_envelope(G06.qid, no_coverage).evidence is None and observation_from_envelope(G06.qid, no_coverage).repair is None


def test_repair_yield_is_split_by_facet_origin_and_assessment_usd_comes_from_the_step_rows(envelope) -> None:
    assert repair_by_origin(envelope) == {"clause": {"targeted": 1, "covered_after": 0}}
    env = dict(envelope, steps=[
        {"operator": "search", "cost_usd": 0.0}, {"operator": "operator_check", "cost_usd": 0.0123}, {"operator": "operator_check", "cost_usd": 0.0077},
    ])
    assert assessment_usd(env) == 0.02


def test_the_scorer_finds_the_assessors_two_known_errors(envelope, oracle) -> None:
    entries = load_facet_map(DEFAULT_MAP_PATH, ALL)
    facets = envelope["request_coverage"]["facets"]
    res = resolve_facets(G06, facets, entries[G06.qid])
    score = oracle_mod.score_run(oracle.requests[G06.qid], envelope["coverage"], res)
    by_slug = {s.fid: s for s in score.facets}
    assert by_slug["f3"].oracle_state == "supported" and by_slug["f3"].assessor_state == "unsupported" and by_slug["f3"].false_negative is True
    assert by_slug["f2"].assessor_state == "covered" and by_slug["f2"].assessor_reason == "conflicted" and by_slug["f2"].spurious_contradict is True
    assert [s.false_negative for s in score.facets] == [False, False, True, False, False, False]
    assert score.skipped is False and score.false_skip is None
    agg = oracle_mod.aggregate([score])["hand"]
    assert {k: agg["false_negative"][k] for k in ("rate", "numerator", "denominator", "supported_but_unassessed")} == {
        "rate": 1 / 6, "numerator": 1, "denominator": 6, "supported_but_unassessed": 0}
    assert agg["spurious_contradicts"]["rate"] == 1.0 and agg["spurious_contradicts"]["denominator"] == 1
    assert agg["false_skip"]["rate_of_skips"] is None and agg["false_skip"]["skips"] == 0
    assert {k: agg["negative_control"][k] for k in ("facets", "assessor_state", "falsely_covered")} == {"facets": 0, "assessor_state": {}, "falsely_covered": None}
    assert oracle_mod.aggregate([score])["drafted"]["runs"] == 0, "a fixture label is a hand label: nothing is drafted"


def test_a_contradiction_the_oracle_confirms_is_not_spurious_and_an_unlabelled_one_is_not_scored(envelope, oracle) -> None:
    entries = load_facet_map(DEFAULT_MAP_PATH, ALL)
    res = resolve_facets(G06, envelope["request_coverage"]["facets"], entries[G06.qid])
    base = oracle.requests[G06.qid]
    confirming = oracle_mod.RequestOracle(base.qid, base.slugs, base.provenance, (
        oracle_mod.ItemLabel(chash=envelope["coverage"]["anchors"][-1]["chash"], closed=False, facets={"numerical-conventions": "contradicts"}),
        *base.items,
    ))
    assert oracle_mod.score_run(confirming, envelope["coverage"], res).facets[1].spurious_contradict is False
    sparse = oracle_mod.RequestOracle(base.qid, base.slugs, base.provenance, base.items[:5])
    assert oracle_mod.score_run(sparse, envelope["coverage"], res).facets[1].spurious_contradict is None


def test_a_skipped_run_over_evidence_the_oracle_says_supports_a_facet_is_a_false_skip(envelope, oracle) -> None:
    entries = load_facet_map(DEFAULT_MAP_PATH, ALL)
    res = resolve_facets(G06, envelope["request_coverage"]["facets"], entries[G06.qid])
    skipped = dict(envelope["coverage"], skip_generation=True, state="unavailable", reason="uncovered")
    skipped["findings"] = [dict(f, state="unsupported", evidence_ids=[], reason="no_supporting_evidence") for f in skipped["findings"]]
    s = oracle_mod.score_run(oracle.requests[G06.qid], skipped, res)
    assert s.skipped and s.false_skip is True
    agg = oracle_mod.aggregate([s])["hand"]
    assert agg["false_skip"]["rate_of_skips"] == 1.0 and agg["false_negative"]["rate"] == 1.0


def test_a_facet_decided_by_a_deterministic_row_is_not_the_assessors_error(envelope, oracle) -> None:
    entries = load_facet_map(DEFAULT_MAP_PATH, ALL)
    res = resolve_facets(G06, envelope["request_coverage"]["facets"], entries[G06.qid])
    cov = json.loads(json.dumps(envelope["coverage"]))
    cov["findings"][2] = dict(cov["findings"][2], state="unsupported", reason="source_missing")
    assert oracle_mod.score_run(oracle.requests[G06.qid], cov, res).facets[2].false_negative is None


def test_a_negative_control_counts_the_assessors_state_where_nothing_bears_on_the_facet(oracle) -> None:
    q = BY_QID["G06-synthesis-art-rdr084"]
    base = oracle.requests[q.qid]
    # two closed items that evidence no facet, and an assessor that nonetheless says f1 is covered
    anchors = [{"item_id": "e1", "chash": base.items[0].chash}, {"item_id": "e2", "chash": base.items[1].chash}]
    cov = {"anchors": anchors, "skip_generation": False,
           "findings": [{"facet_id": "f1", "state": "covered", "evidence_ids": ["e1"], "reason": ""}]}
    res = resolve_facets(q, [{"id": "f1", "text": "the locked architecture"}], load_facet_map(DEFAULT_MAP_PATH, ALL)[q.qid])
    agg = oracle_mod.aggregate([oracle_mod.score_run(base, cov, res)])["hand"]
    assert {k: agg["negative_control"][k] for k in ("facets", "assessor_state", "falsely_covered")} == {"facets": 1, "assessor_state": {"covered": 1}, "falsely_covered": 1.0}


# ── the driver ───────────────────────────────────────────────────────────────────────────────────────────


def _ticking_clock():
    t = {"now": 0.0}

    def clock() -> float:
        t["now"] += 0.5
        return t["now"]

    return clock


def test_the_driver_refuses_to_run_without_live_or_stub(capsys) -> None:
    assert driver.main(["--set", "golden"]) == 2
    assert "--live" in capsys.readouterr().err


def test_the_driver_runs_end_to_end_on_a_recorded_envelope(tmp_path: Path) -> None:
    out = tmp_path / "report.json"
    rc = driver.main(
        ["--set", "golden", "--stub", str(FIXTURE), "--out", str(out), "--engine", "stub-0", "--owner", "ART=1.2"],
        clock=_ticking_clock(),
    )
    assert rc == 0
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["meta"]["requests"] == 24 and report["meta"]["engine"] == "stub-0" and report["meta"]["mode"] == "stub"
    off, on = report["arms"]["off"], report["arms"]["on"]
    assert off["runs"] == on["runs"] == 24 and off["assessed_runs"] == 0 and on["assessed_runs"] == 24
    calls = on["assessment_calls"]["all"]
    assert calls["calls"] == 24 * 2 and calls["support"] == calls["calls"] and calls["zero_accepted_share"] == 0.0
    assert set(on["assessment_calls"]["by_stream"]) == {"vector", "graph"}
    assert on["conflicted"]["facets_conflicted"] == 24 and on["repair"]["runs_entered"] == 24
    assert on["repair"]["by_facet_origin"]["clause"] == {"targeted": 24, "covered_after": 0, "yield": 0.0}
    assert report["compare_on_vs_off"]["pairs"] == 24 and report["compare_on_vs_off"]["latency_ms_delta"]["mean"] == 0.0
    assert report["oracle_labels"]["pairs_todo"] == 104 and report["facet_map"]["requests_resolved"] == 24
    assert report["mvv_g06"]["coverage_state"] == "partial" and report["mvv_g06"]["repair_outcome"] == "no_new_evidence"
    assert {"MAX_FACETS_PER_ASSESSMENT_CALL", "MAX_REPAIR_QUERIES"} <= set(report["config"])
    assert any("continuation_fallback" in n for n in report["not_measured"])
    # the oracle labels four requests (G01-G03 by document path, which the stub cannot resolve, and G06 by chunk hash)
    hand = on["oracle"]["hand"]
    assert on["oracle"]["runs_scored"] == 4 and hand["false_negative"]["numerator"] == 1, "only G06's chashes match the recorded envelope"
    assert on["oracle"]["drafted"]["runs"] == 0 and report["oracle_labels_by_provenance"]["drafted"] == {"requests": 0, "pairs": 0}
    assert hand["oracle_state_counts"]["supported"] == 6 and hand["oracle_state_counts"]["unmapped"] == 18, (
        "the stub replays G06's six facets for G01-G03 too, which the map cannot join to their one slug: unmapped, not guessed")


def test_the_driver_sets_the_coverage_switch_per_arm_and_records_a_failed_request(tmp_path: Path) -> None:
    seen: list[tuple[str, str, dict[str, Any]]] = []
    recorded = json.loads(FIXTURE.read_text(encoding="utf-8"))

    async def caller(q, *, arm, scope, bindings):
        seen.append((q.qid, arm, dict(bindings)))
        if q.qid == "G02-lookup-chash" and arm == "on":
            raise RuntimeError("tenant went away")
        return recorded

    out = tmp_path / "r.json"
    rc = driver.main(["--set", "golden", "--qid", "G01-lookup-tumblers", "--qid", "G02-lookup-chash", "--out", str(out)], caller=caller)
    assert rc == 0
    assert [(q, a) for q, a, _ in seen] == [("G01-lookup-tumblers", "off"), ("G02-lookup-chash", "off"), ("G01-lookup-tumblers", "on"), ("G02-lookup-chash", "on")]
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["arms"]["on"]["errors"] == {"RuntimeError": 1} and report["arms"]["on"]["runs"] == 2


def test_the_live_caller_sets_and_restores_the_coverage_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(driver.COVERAGE_ENV, raising=False)
    seen: dict[str, Any] = {}

    async def fake_nx_answer(**kw):
        seen["env"] = os.environ.get(driver.COVERAGE_ENV)
        seen["kw"] = kw
        return {"answer_shape": "answered"}

    monkeypatch.setattr(core, "nx_answer", fake_nx_answer)
    out = asyncio.run(driver.live_caller()(G06, arm="on", scope="rdr", bindings={"source": "RDR-084"}))
    assert out == {"answer_shape": "answered"} and seen["env"] == "1" and os.environ.get(driver.COVERAGE_ENV) is None
    assert seen["kw"]["structured"] is True and seen["kw"]["trace"] is True and seen["kw"]["bindings"] == {"source": "RDR-084"}
    asyncio.run(driver.live_caller()(G06, arm="off", scope="", bindings={}))
    assert seen["env"] == "0" and seen["kw"]["bindings"] is None


def test_call_shape_binds_the_art_request_to_an_owner_when_given_one() -> None:
    scope, with_owner = driver.call_shape(G06, {"ART": "1.2"})
    assert scope == "rdr" and with_owner == {"content_type": "rdr", "source": "RDR-084", "owner": "1.2"}
    assert driver.call_shape(G06)[1] == {"content_type": "rdr", "source": "RDR-084", "project": "ART"}
    assert driver.call_shape(BY_QID["G01-lookup-tumblers"])[1] == {"source": "RDR-049"}
    assert driver.call_shape(BY_QID["G05-lookup-hybridrag-cap"])[1] == {}, "a paper title names no SourceRef"


def test_project_binding_names_how_each_request_gets_its_project(tmp_path: Path) -> None:
    """nexus-jypmj.3.17: a request that names a project (``@ART``) travels with it as a binding; every other request runs
    in the project directory, so the active-project tier can resolve a number two projects carry."""
    _, art = driver.call_shape(G06)
    assert driver.project_binding(art, tmp_path) == {"kind": "explicit_project", "value": "ART", "project_dir": None}
    _, art_owner = driver.call_shape(G06, {"ART": "1.2"})
    assert driver.project_binding(art_owner, tmp_path) == {"kind": "explicit_owner", "value": "1.2", "project_dir": None}
    _, plain = driver.call_shape(BY_QID["G01-lookup-tumblers"])
    assert driver.project_binding(plain, tmp_path) == {"kind": "active_project_dir", "value": tmp_path.name, "project_dir": str(tmp_path)}
    assert driver.project_binding(plain, None) == {"kind": "none", "value": None, "project_dir": None}


def test_run_arm_runs_each_call_in_its_project_and_restores_the_process(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    here = os.getcwd()
    seen: dict[str, tuple[str | None, str]] = {}

    async def caller(q, *, arm, scope, bindings):  # noqa: ARG001
        seen[q.qid] = (os.environ.get("CLAUDE_PROJECT_DIR"), os.getcwd())
        return {}

    queries = [BY_QID["G01-lookup-tumblers"], G06]
    runs = asyncio.run(driver.run_arm(queries, caller, "on", project_dir=tmp_path))
    home = str(tmp_path.resolve())
    assert seen["G01-lookup-tumblers"] == (home, home), "the active-project tier needs the variable AND the working directory"
    assert seen[G06.qid] == (None, here), "a request that binds its own project is not given the directory"
    assert [(r.qid, r.binding["kind"]) for r in runs] == [("G01-lookup-tumblers", "active_project_dir"), (G06.qid, "explicit_project")]
    assert os.getcwd() == here and "CLAUDE_PROJECT_DIR" not in os.environ


def test_the_report_records_the_binding_of_every_request(tmp_path: Path) -> None:
    async def caller(q, *, arm, scope, bindings):  # noqa: ARG001
        return json.loads(FIXTURE.read_text(encoding="utf-8"))

    out = tmp_path / "r.json"
    rc = driver.main(
        ["--set", "golden", "--qid", "G01-lookup-tumblers", "--qid", G06.qid, "--project-dir", str(tmp_path), "--out", str(out)],
        caller=caller)
    assert rc == 0
    meta = json.loads(out.read_text(encoding="utf-8"))["meta"]
    assert meta["project_dir"] == str(tmp_path)
    assert {k: v["kind"] for k, v in meta["project_bindings"].items()} == {"G01-lookup-tumblers": "active_project_dir", G06.qid: "explicit_project"}


def test_a_missing_project_dir_is_refused(tmp_path: Path, capsys) -> None:
    assert driver.main(["--set", "golden", "--stub", str(FIXTURE), "--project-dir", str(tmp_path / "nope")]) == 2
    assert "--project-dir" in capsys.readouterr().err


def test_a_live_run_defaults_to_the_repository_as_its_project(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The golden requests are about this repository; a live run with no ``--project-dir`` binds it (``--no-project-dir``
    opts out), so the tier is not silently absent."""
    seen: list[Path | None] = []
    real = driver.run_arm

    async def spy(queries, caller, arm, **kw):
        seen.append(kw.get("project_dir"))
        return await real(queries, caller, arm, **kw)

    monkeypatch.setattr(driver, "run_arm", spy)

    async def caller(q, *, arm, scope, bindings):  # noqa: ARG001
        return json.loads(FIXTURE.read_text(encoding="utf-8"))

    args = ["--set", "golden", "--qid", "G01-lookup-tumblers", "--live", "--no-oracle", "--out", str(tmp_path / "r.json")]
    assert driver.main(args, caller=caller) == 0
    assert seen[0] == driver.REPO_ROOT
    seen.clear()
    assert driver.main([*args, "--no-project-dir"], caller=caller) == 0
    assert seen[0] is None


def test_the_catalog_path_resolver_maps_a_tumbler_to_its_file_path() -> None:
    class Entry:
        file_path = "docs/rdr/rdr-049-catalog-tumblers.md"

    class FakeCatalog:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def resolve_many(self, ids):
            assert ids == ["1.1.2"]
            return {"1.1.2": Entry()}

    with patch("nexus.catalog.http_catalog_client.HttpCatalogClient", FakeCatalog):
        paths = driver.catalog_doc_paths(["1.1.2", "1.1.2", ""])
    assert paths({"tumbler": "1.1.2"}) == "docs/rdr/rdr-049-catalog-tumblers.md" and paths({"tumbler": "9"}) == ""
    o = oracle_mod.load_oracle(oracle_mod.DEFAULT_ORACLE_PATH, ALL).requests["G01-lookup-tumblers"]
    assert o.role_of("", paths({"tumbler": "1.1.2"}), "introducing-rdr") == "supports"
