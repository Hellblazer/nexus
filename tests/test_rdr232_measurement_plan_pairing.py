# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-232 Phase 3 measurement (nexus-jypmj.3.10, critique S2): the two arms are not comparable when the plan differs.

The driver runs every request coverage-off, then every request coverage-on. A plan-miss ``nx_answer`` grows a plan, so
the on arm can match the plan the off arm just grew and skip the inline planner: the pair then differs in planner cost
and latency, not only in the coverage switch. The envelope's ``plan_id`` does NOT expose this: after a grow it is the
grown row's id, and the on arm's hit reports the same id. The plan the arm RAN WITH is ``plan_choice.chosen_plan_id``
(``None`` plan_choice on a miss), which is what these tests pin the pairing on.

Model-free and tenant-free: envelopes are the recorded G06 one with the plan fields set by hand.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from bench import rdr232_phase3_measure as driver  # noqa: E402
from bench.rdr232_envelope import (  # noqa: E402
    EnvelopeRecord,
    envelope_facts,
    read_envelopes_file,
    write_envelopes_file,
)

from nexus.mcp import core  # noqa: E402

FIXTURE = REPO / "tests" / "fixtures" / "rdr232_envelope_g06_stub.json"
RECORDED = json.loads(FIXTURE.read_text(encoding="utf-8"))


def _env(*, plan_id: int, chosen: int | None, cost: float = 0.4, shape: str = "answered") -> dict[str, Any]:
    """The recorded envelope with its plan identity set: *chosen* is ``plan_choice.chosen_plan_id`` (``None``: a plan
    miss, where the envelope's ``plan_choice`` is ``None``); *plan_id* is the envelope's own field."""
    env = json.loads(json.dumps(RECORDED))
    env["plan_id"] = plan_id
    env["plan_choice"] = None if chosen is None else {**env["plan_choice"], "chosen_plan_id": chosen}
    env["cost_usd"] = cost
    env["answer_shape"] = shape
    return env


def _run(qid: str, arm: str, env: dict[str, Any] | None, wall_ms: int = 1000, error: str = "") -> driver.ArmRun:
    return driver.ArmRun(qid, arm, wall_ms, facts=envelope_facts(env) if env is not None else {}, envelope=env, error=error)


# ── the plan an arm ran with ─────────────────────────────────────────────────────────────────────────────


def test_the_facts_name_the_plan_the_arm_ran_with_apart_from_the_envelopes_plan_id() -> None:
    hit = envelope_facts(_env(plan_id=7, chosen=7))
    assert (hit["plan_id"], hit["matched_plan_id"]) == (7, 7)
    miss_then_grown = envelope_facts(_env(plan_id=912, chosen=None))
    assert miss_then_grown["plan_id"] == 912, "the envelope reports the grown row's id after a miss"
    assert miss_then_grown["matched_plan_id"] == 0, "but the arm ran an inline plan: no library plan matched"


# ── compare_arms ─────────────────────────────────────────────────────────────────────────────────────────


def test_a_plan_the_off_arm_grew_and_the_on_arm_matched_is_excluded_even_though_the_envelope_plan_ids_are_equal() -> None:
    """THE confound. Off: a miss that grew plan 912 (inline planner ran: more cost, more time). On: a hit on 912. Both
    envelopes say plan_id 912, so a filter on ``plan_id`` equality would keep this pair and quote its delta."""
    off = [_run("Q1", "off", _env(plan_id=912, chosen=None, cost=0.60), wall_ms=40_000)]
    on = [_run("Q1", "on", _env(plan_id=912, chosen=912, cost=0.45), wall_ms=30_000)]
    assert off[0].facts["plan_id"] == on[0].facts["plan_id"] == 912
    cmp = driver.compare_arms(off, on)
    assert (cmp["pairs"], cmp["pairs_plan_matched"], cmp["pairs_plan_mismatched"], cmp["excluded_plan_mismatch"]) == (1, 0, 1, 1)
    assert cmp["cost_usd_delta"]["n"] == 0 and cmp["latency_ms_delta"]["n"] == 0, "no delta is quoted over a confounded pair"


def test_matched_pairs_carry_the_deltas_and_the_excluded_count_is_reported_beside_them() -> None:
    off = [
        _run("Q1", "off", _env(plan_id=7, chosen=7, cost=0.40), wall_ms=30_000),
        _run("Q2", "off", _env(plan_id=900, chosen=None, cost=0.70), wall_ms=60_000),
        _run("Q3", "off", _env(plan_id=7, chosen=7, cost=0.50), wall_ms=20_000),
    ]
    on = [
        _run("Q1", "on", _env(plan_id=7, chosen=7, cost=0.42), wall_ms=150_000),
        _run("Q2", "on", _env(plan_id=900, chosen=900, cost=0.45), wall_ms=50_000),
        _run("Q3", "on", _env(plan_id=7, chosen=7, cost=0.52), wall_ms=100_000),
    ]
    cmp = driver.compare_arms(off, on)
    assert (cmp["pairs"], cmp["pairs_plan_matched"], cmp["excluded_plan_mismatch"]) == (3, 2, 1)
    assert cmp["cost_usd_delta"]["n"] == 2 and cmp["cost_usd_delta"]["mean"] == pytest.approx(0.02)
    assert cmp["latency_ms_delta"]["n"] == 2 and cmp["latency_ms_delta"]["mean"] == pytest.approx(100_000.0), (
        "Q2's -10 s (a planner skipped, not coverage) must not pull the mean down")
    assert [p["qid"] for p in cmp["plan_pairs"] if not p["matched"]] == ["Q2"]


def test_the_plan_ids_of_every_pair_are_recorded_per_arm() -> None:
    off = [_run("Q1", "off", _env(plan_id=900, chosen=None))]
    on = [_run("Q1", "on", _env(plan_id=900, chosen=900))]
    (pair,) = driver.compare_arms(off, on)["plan_pairs"]
    assert pair == {"qid": "Q1", "off_plan_id": 900, "on_plan_id": 900, "off_matched_plan_id": 0, "on_matched_plan_id": 900, "matched": False}


def test_two_inline_planned_runs_are_a_matched_pair_and_counted_as_such() -> None:
    """With plan growth switched off both arms plan inline every time (matched id 0 on both): symmetrical, so the pair
    stands, but the report says how many pairs rest on it."""
    off = [_run("Q1", "off", _env(plan_id=0, chosen=None), wall_ms=45_000)]
    on = [_run("Q1", "on", _env(plan_id=0, chosen=None), wall_ms=160_000)]
    cmp = driver.compare_arms(off, on)
    assert cmp["pairs_plan_matched"] == 1 and cmp["pairs_both_inline_planned"] == 1
    assert cmp["latency_ms_delta"]["mean"] == pytest.approx(115_000.0)


def test_an_errored_run_is_not_a_pair_and_is_counted_apart() -> None:
    off = [_run("Q1", "off", _env(plan_id=7, chosen=7)), _run("Q2", "off", None, error="RuntimeError")]
    on = [_run("Q1", "on", _env(plan_id=7, chosen=7)), _run("Q2", "on", _env(plan_id=7, chosen=7))]
    cmp = driver.compare_arms(off, on)
    assert (cmp["pairs"], cmp["pairs_with_error"]) == (1, 1)


def test_answer_shape_counts_read_only_the_matched_pairs() -> None:
    off = [_run("Q1", "off", _env(plan_id=7, chosen=7, shape="answered")), _run("Q2", "off", _env(plan_id=900, chosen=None, shape="answered"))]
    on = [
        _run("Q1", "on", _env(plan_id=7, chosen=7, shape="uncovered")),
        _run("Q2", "on", _env(plan_id=900, chosen=900, shape="uncovered")),
    ]
    cmp = driver.compare_arms(off, on)
    assert cmp["answer_shape_changed"] == 1 and cmp["answer_withheld_only_with_coverage_on"] == 1, "Q2 is a different plan, not a coverage effect"


def test_the_report_has_no_unfiltered_delta_key() -> None:
    cmp = driver.compare_arms([], [])
    assert set(cmp) >= {"pairs", "pairs_plan_matched", "pairs_plan_mismatched", "excluded_plan_mismatch", "plan_pairs", "latency_basis"}
    assert not any(k.startswith("unfiltered") or k.endswith("_all") for k in cmp)


# ── the driver end to end ────────────────────────────────────────────────────────────────────────────────


def _growing_caller():
    """The coverage-off arm misses and grows a plan; the coverage-on arm then matches it (the S2 confound)."""
    grown: dict[str, int] = {}

    async def caller(q, *, arm, scope, bindings):  # noqa: ARG001
        if arm == "off":
            grown[q.qid] = 900 + len(grown)
            return _env(plan_id=grown[q.qid], chosen=None, cost=0.60)
        return _env(plan_id=grown[q.qid], chosen=grown[q.qid], cost=0.45)

    return caller


def test_the_driver_flags_every_pair_a_growing_off_arm_confounded(tmp_path: Path) -> None:
    out = tmp_path / "r.json"
    rc = driver.main(["--set", "golden", "--qid", "G01-lookup-tumblers", "--qid", "G02-lookup-chash", "--out", str(out)], caller=_growing_caller())
    assert rc == 0
    cmp = json.loads(out.read_text(encoding="utf-8"))["compare_on_vs_off"]
    assert (cmp["pairs"], cmp["pairs_plan_matched"], cmp["excluded_plan_mismatch"]) == (2, 0, 2)
    assert cmp["cost_usd_delta"]["n"] == 0


# ── stopping plan growth during a measurement ────────────────────────────────────────────────────────────


def test_the_live_caller_can_stop_plan_growth_and_restores_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """``nx_answer`` grows a plan only when ``_load_ad_hoc_ttl()`` is above 0 (it exposes no per-call switch, and
    ``core.py`` is not this bead's to change), so the driver sets that to 0 for the duration of each call."""
    monkeypatch.delenv(driver.COVERAGE_ENV, raising=False)
    seen: list[int] = []
    before = core._load_ad_hoc_ttl

    async def fake_nx_answer(**kw):  # noqa: ARG001
        seen.append(core._load_ad_hoc_ttl())
        return {"answer_shape": "answered"}

    monkeypatch.setattr(core, "nx_answer", fake_nx_answer)
    q = driver.load_golden_set(driver.GOLDEN_PATH)[0]
    asyncio.run(driver.live_caller(no_plan_growth=True)(q, arm="off", scope="", bindings={}))
    assert seen == [0] and core._load_ad_hoc_ttl is before, "the growth guard is restored after the call"
    asyncio.run(driver.live_caller()(q, arm="off", scope="", bindings={}))
    assert seen[1] == before(), "without the option the configured ttl stands"


def test_the_growth_guard_is_restored_when_the_call_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    before = core._load_ad_hoc_ttl

    async def boom(**kw):  # noqa: ARG001
        raise RuntimeError("engine down")

    monkeypatch.setattr(core, "nx_answer", boom)
    q = driver.load_golden_set(driver.GOLDEN_PATH)[0]
    with pytest.raises(RuntimeError):
        asyncio.run(driver.live_caller(no_plan_growth=True)(q, arm="on", scope="", bindings={}))
    assert core._load_ad_hoc_ttl is before


def test_the_report_records_whether_plan_growth_was_allowed(tmp_path: Path) -> None:
    out = tmp_path / "r.json"
    assert driver.main(["--set", "golden", "--qid", "G01-lookup-tumblers", "--stub", str(FIXTURE), "--out", str(out)]) == 0
    assert json.loads(out.read_text(encoding="utf-8"))["meta"]["plan_growth"] == "not_applicable_stub"
    assert driver.main(["--set", "golden", "--qid", "G01-lookup-tumblers", "--out", str(out), "--no-plan-growth"], caller=_growing_caller()) == 0
    assert json.loads(out.read_text(encoding="utf-8"))["meta"]["plan_growth"] == "disabled"
    assert driver.main(["--set", "golden", "--qid", "G01-lookup-tumblers", "--out", str(out)], caller=_growing_caller()) == 0
    assert json.loads(out.read_text(encoding="utf-8"))["meta"]["plan_growth"] == "disabled", "growth is off by default"
    assert driver.main(["--set", "golden", "--qid", "G01-lookup-tumblers", "--out", str(out), "--allow-plan-growth"],
                       caller=_growing_caller()) == 0
    assert json.loads(out.read_text(encoding="utf-8"))["meta"]["plan_growth"] == "enabled"


# ── re-analysing an envelopes file ───────────────────────────────────────────────────────────────────────


def _two_request_run(tmp_path: Path) -> tuple[Path, Path]:
    envelopes, report = tmp_path / "env.json", tmp_path / "r.json"
    rc = driver.main(
        ["--set", "golden", "--qid", "G01-lookup-tumblers", "--qid", "G02-lookup-chash", "--out", str(report), "--envelopes-out", str(envelopes)],
        caller=_growing_caller(), clock=iter(range(0, 10_000, 3)).__next__,
    )
    assert rc == 0
    return envelopes, report


def test_an_envelopes_file_is_reanalysed_without_a_caller_and_matches_the_live_report(tmp_path: Path) -> None:
    envelopes, report = _two_request_run(tmp_path)
    again = tmp_path / "again.json"

    async def never(*a, **k):  # noqa: ARG001
        raise AssertionError("re-analysis must not call nx_answer")

    rc = driver.main(
        ["--set", "golden", "--qid", "G01-lookup-tumblers", "--qid", "G02-lookup-chash", "--from-envelopes", str(envelopes), "--out", str(again)],
        caller=never,
    )
    assert rc == 0
    a, b = (json.loads(p.read_text(encoding="utf-8")) for p in (report, again))
    assert a["compare_on_vs_off"] == b["compare_on_vs_off"] and a["arms"]["on"]["states"] == b["arms"]["on"]["states"]
    assert b["meta"]["mode"] == "from_envelopes" and b["meta"]["source_envelopes"] == str(envelopes)


def test_a_legacy_bare_list_envelopes_file_is_read_by_request_order(tmp_path: Path) -> None:
    """The file the run already in progress writes: ``{arm: [envelope or null, ...]}`` in request order, no qids and no
    wall times. Latency then comes from the step rows, and the report says so."""
    legacy = tmp_path / "legacy.json"
    off = [_env(plan_id=7, chosen=7, cost=0.40), _env(plan_id=900, chosen=None, cost=0.70)]
    on = [_env(plan_id=7, chosen=7, cost=0.42), _env(plan_id=900, chosen=900, cost=0.45)]
    legacy.write_text(json.dumps({"off": off, "on": on}), encoding="utf-8")
    out = tmp_path / "r.json"
    rc = driver.main(["--set", "golden", "--qid", "G01-lookup-tumblers", "--qid", "G02-lookup-chash", "--from-envelopes", str(legacy), "--out", str(out)])
    assert rc == 0
    cmp = json.loads(out.read_text(encoding="utf-8"))["compare_on_vs_off"]
    assert (cmp["pairs"], cmp["pairs_plan_matched"], cmp["excluded_plan_mismatch"]) == (2, 1, 1)
    assert cmp["latency_basis"] == "step_elapsed_sum"
    assert cmp["cost_usd_delta"]["n"] == 1 and cmp["cost_usd_delta"]["mean"] == pytest.approx(0.02)


def test_a_legacy_file_that_does_not_fit_the_selected_requests_is_refused(tmp_path: Path, capsys) -> None:
    legacy = tmp_path / "legacy.json"
    legacy.write_text(json.dumps({"off": [_env(plan_id=7, chosen=7)], "on": [_env(plan_id=7, chosen=7)]}), encoding="utf-8")
    rc = driver.main(["--set", "golden", "--qid", "G01-lookup-tumblers", "--qid", "G02-lookup-chash", "--from-envelopes", str(legacy)])
    assert rc == 2 and "2 requests" in capsys.readouterr().err


def test_from_envelopes_refuses_live_and_stub(tmp_path: Path, capsys) -> None:
    legacy = tmp_path / "legacy.json"
    legacy.write_text("{}", encoding="utf-8")
    assert driver.main(["--set", "golden", "--from-envelopes", str(legacy), "--live"]) == 2
    assert "exactly one" in capsys.readouterr().err


def test_the_envelopes_file_round_trips_qid_wall_time_and_error(tmp_path: Path) -> None:
    p = tmp_path / "e.json"
    recs = {
        "off": [EnvelopeRecord("Q1", 1200, "", {"plan_id": 7}), EnvelopeRecord("Q2", 300, "RuntimeError", None)],
        "on": [EnvelopeRecord("Q1", 5200, "", {"plan_id": 7}), EnvelopeRecord("Q2", 10, "", {"plan_id": 8})],
    }
    write_envelopes_file(p, recs)
    assert read_envelopes_file(p, None) == recs
    assert json.loads(p.read_text(encoding="utf-8"))["format"] == 2
    assert os.path.getsize(p) > 0
