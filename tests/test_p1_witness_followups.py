# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-189 P1 follow-ups: a check or verify step with no evidence still returns a Witness Schema v1
payload (nexus-z1d0l.1.10), and a verify step with no claim is caught before any step runs: the
inline planner re-asks once, then fails by name, and plan_save, the seed loader, the matcher and
nx plan hygiene refuse it (nexus-z1d0l.1.11)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml
from structlog.testing import capture_logs

from nexus.commands.plan import _hygiene_scan
from nexus.mcp import core as mcp_core
from nexus.plans import runner
from nexus.plans.matcher import plan_match
from nexus.plans.seed_loader import load_seed_directory
from tests.test_6pfgb11_provenance_from_catalog import COLL, X, _position, _t3
from tests.test_6pfgb50_runner_chash_reuses_rows import _CountingCatalog, _plain
from tests.test_plan_run import _match
from tests.test_rdr232_embedded_ref_guards import (  # noqa: F401 - info_logging is a fixture
    _Cache,
    _HygieneLibrary,
    _Library,
    _planner_run,
    _row,
    _save,
    _SeedLibrary,
    _template,
    info_logging,
)

# ── nexus-z1d0l.1.10: zero evidence is still a v1 payload ───────────────────────────────


def _assert_empty_v1(result: dict, tool: str) -> None:
    assert result["schema_version"] == 1
    assert result["evidence"] == [] and result["rejected"] == []
    assert result["requires_review"] is True, "no evidence never reads as reviewed"
    assert result["text"].startswith(runner._NO_EVIDENCE_RESULT_PREFIX) and f"tool={tool}" in result["text"]


@pytest.mark.asyncio
async def test_a_check_step_whose_items_are_all_empty_returns_an_empty_v1_payload_without_dispatch() -> None:
    with patch("nexus.mcp.core.operator_check") as op:
        result = await runner._default_dispatcher("check", {"inputs": ["", ""], "check_instruction": "agree?"})
    op.assert_not_called()
    _assert_empty_v1(result, "check")
    assert result["ok"] is None, "no model ran, so there is no verdict"
    assert result["uncovered"] == []


@pytest.mark.asyncio
async def test_a_verify_step_whose_ids_hydrate_to_nothing_returns_an_empty_v1_payload_without_dispatch() -> None:
    catalog = _CountingCatalog([_plain("1.1.1")], [_position(X, "1.1.1")])
    with patch("nexus.mcp_infra.get_catalog", return_value=catalog), patch(
        "nexus.mcp.core._get_catalog", return_value=catalog,
    ), patch("nexus.mcp.core._get_t3", return_value=_t3({})), patch(
        "nexus.mcp.core._read_scope", return_value=[COLL],
    ), patch("nexus.mcp.core.operator_verify") as op:
        result = await runner._default_dispatcher("verify", {"ids": [X], "collections": COLL, "claim": "latency drops"})
    op.assert_not_called()
    _assert_empty_v1(result, "verify")
    assert result["verified"] is None
    assert result["reason"] == result["text"] and result["citations"] == []


@pytest.mark.asyncio
async def test_an_operator_without_a_witness_keeps_the_text_marker_alone() -> None:
    with patch("nexus.mcp.core.operator_compare") as op:
        result = await runner._default_dispatcher("compare", {"inputs": ["", ""], "focus": "diffs"})
    op.assert_not_called()
    assert set(result) == {"text"}


@pytest.mark.asyncio
async def test_plan_run_returns_the_v1_payload_as_the_answer_of_a_zero_evidence_check() -> None:
    plan = {"steps": [{"tool": "check", "args": {"inputs": ["", ""], "check_instruction": "agree?"}}]}
    with patch.object(mcp_core, "operator_check") as op:
        result = await runner.plan_run(_match(plan), {}, bundle_operators=False)
    op.assert_not_called()
    assert result.final is not None
    _assert_empty_v1(result.final, "check")


# ── nexus-z1d0l.1.11: the planner and a verify step with no claim ───────────────────────


def _steps(*verify_args: dict, after: list[dict] | None = None) -> dict:
    steps = [{"tool": "search", "args": {"query": "latency", "corpus": "knowledge"}}]
    steps += [{"tool": "verify", "args": {"ids": "$step1.ids", **a}} for a in verify_args]
    return {"steps": steps + (after or [])}


CLAIMLESS = _steps({})
CLAIMED = _steps({"claim": "latency drops"})


@pytest.mark.parametrize(
    ("plan", "expected"),
    [
        (CLAIMLESS, 1),
        (_steps({"claim": "  "}), 1),
        (_steps({"claim": None}), 1),
        (_steps({"claim": ["latency drops"]}), 1),
        (CLAIMED, None),
        (_steps({"claim": "$step1.text"}), None),
        ({"steps": [{"tool": "operator_verify", "args": {}}]}, 0),
        ({"steps": [{"tool": "mcp__plugin_conexus_nexus__operator_verify", "args": {}}]}, 0),
        ({"steps": [{"tool": "mcp__plugin_conexus_nexus__verify", "args": {"claim": "x"}}]}, None),
        ({"steps": [{"tool": "search", "args": {}}]}, None),
        ("not json", None),
        ({"no": "steps"}, None),
        (None, None),
    ],
    ids=["missing", "blank", "null", "non_string", "claimed", "step_ref", "operator_name", "mcp_prefixed", "mcp_prefixed_claimed", "no_verify", "bad_json", "no_steps", "none"],
)
def test_the_scan_finds_the_first_verify_step_with_no_claim(plan, expected) -> None:
    assert runner.find_claimless_verify(json.dumps(plan) if isinstance(plan, dict) else plan) == expected


def test_a_claimless_verify_is_re_asked_once_naming_the_step(monkeypatch) -> None:
    with capture_logs() as logs:
        match, prompts, cost = _planner_run(monkeypatch, [CLAIMLESS, CLAIMED])
    assert len(prompts) == 2 and prompts[1].startswith(prompts[0])
    assert "step 2 is a verify step with no claim" in prompts[1][len(prompts[0]):]
    assert json.loads(match.plan_json) == CLAIMED
    assert cost == pytest.approx(0.50)
    assert [e["step"] for e in logs if e.get("event") == "nx_answer_planner_verify_claim_retry"] == [2]


def test_a_claimed_plan_is_not_re_asked(monkeypatch) -> None:
    match, prompts, _ = _planner_run(monkeypatch, [CLAIMED])
    assert len(prompts) == 1 and json.loads(match.plan_json) == CLAIMED


@pytest.mark.parametrize(
    "second",
    [
        CLAIMLESS,
        _steps({"claim": "latency drops"}, {}),
        _steps({}, after=[{"tool": "summarize", "args": {"content": "$step2.text"}}]),
        TimeoutError("planner timed out"),
    ],
    ids=["same_again", "a_second_verify_without_one", "one_a_later_step_reads", "retry_failed"],
)
def test_a_plan_still_holding_a_claimless_verify_fails_in_the_planner_before_any_step_runs(
    monkeypatch, second
) -> None:
    """Never dropped: an answer without its planned check would pass for a checked one."""
    with pytest.raises(ValueError, match="is a verify step with no claim") as err:
        _planner_run(monkeypatch, [CLAIMLESS, second])
    assert str(err.value).startswith("planner plan cannot run: step ")
    assert str(err.value).endswith("; no step was run")


def test_after_an_embedded_ref_retry_a_claimless_verify_fails_without_a_third_call(monkeypatch) -> None:
    embedded = _steps({})
    embedded["steps"][0]["args"]["query"] = "see $step9.text"
    calls: list = []
    with pytest.raises(ValueError, match="step 2 is a verify step with no claim"):
        try:
            _planner_run(monkeypatch, [embedded, CLAIMLESS])
        except IndexError:  # a third planner call would read past the replies
            calls.append("third")
            raise
    assert calls == [], "the embedded-ref retry is the answer's one retry"


@pytest.mark.asyncio
async def test_nx_answer_returns_the_planner_failure_records_it_and_runs_and_grows_nothing() -> None:
    async def miss(*_a, **_kw):
        raise ValueError(f"planner plan cannot run: {runner.claimless_verify_reason(CLAIMLESS)}; no step was run")

    record, library, run = MagicMock(), MagicMock(), AsyncMock()
    db = MagicMock(plans=library)
    with patch("nexus.plans.matcher.plan_match", return_value=[]), patch.object(
        mcp_core, "_nx_answer_plan_miss", AsyncMock(side_effect=miss),
    ), patch("nexus.plans.runner.plan_run", run), patch.object(mcp_core, "_nx_answer_record_run", record), patch.object(
        mcp_core, "_t2_ctx",
    ) as t2, patch.object(mcp_core, "_t2_index_write", lambda fn, **_kw: fn(db)), patch.object(
        mcp_core, "scratch", return_value="ok",
    ), patch("nexus.mcp_infra.get_t1_plan_cache", return_value=None):
        t2.return_value.__enter__.return_value = db
        out = await mcp_core.nx_answer(question="does latency drop")
    assert "planner_error" in out and "step 2 is a verify step with no claim" in out
    run.assert_not_called()
    assert record.call_count == 1
    kw = record.call_args.kwargs
    assert kw["summary_outcome"] == "planner_failure" and kw["step_count"] == 0 and kw["plan_id"] is None
    assert not any(c[0] == "save_plan" for c in library.method_calls), "nothing is grown"


# The doors a stored plan comes through: each refuses or excludes a claim-less verify by name.


def test_plan_save_refuses_a_claimless_verify_and_writes_nothing() -> None:
    out, writes = _save(CLAIMLESS)
    assert out.startswith("Plan not saved: step 2 is a verify step with no claim")
    assert writes == []
    out, writes = _save(CLAIMED)
    assert out.startswith("Saved plan: [7]") and len(writes) == 1, "non-vacuity"


def test_the_seed_loader_refuses_a_claimless_verify_template(tmp_path) -> None:
    (tmp_path / "bad.yml").write_text(yaml.safe_dump(_template("bad-claimless", CLAIMLESS)))
    (tmp_path / "good.yml").write_text(yaml.safe_dump(_template("good-claimed", CLAIMED)))
    result = load_seed_directory(tmp_path, library=_SeedLibrary())
    assert result.inserted == ["good.yml"]
    assert any("bad-claimless" in msg and "verify step with no claim" in msg for _src, msg in result.errors)


@pytest.mark.parametrize("path", ["t1", "fts"])
def test_the_matcher_excludes_a_stored_claimless_verify(path, info_logging) -> None:
    lib = _Library([_row(1, CLAIMLESS, "does latency drop"), _row(2, CLAIMED, "does latency drop, claimed")])
    cache = _Cache([(1, 0.01), (2, 0.05)]) if path == "t1" else None
    with capture_logs() as logs:
        matches = plan_match(intent="does latency drop", library=lib, cache=cache, min_confidence=0.5)
    ids = [m.plan_id for m in matches]
    assert 2 in ids and 1 not in ids
    excluded = [e for e in logs if e.get("event") == "plan_match_embedded_step_ref_excluded"]
    assert [(d["plan_id"], d["ref"]) for d in excluded[0]["detail"]] == [(1, "verify")]
    assert 1 not in lib.bumped


def test_hygiene_reports_a_stored_claimless_verify() -> None:
    rows = [{**_row(1, CLAIMLESS, "claimless"), "verb": "research"}, {**_row(2, CLAIMED, "claimed"), "verb": "research"}]
    findings = _hygiene_scan(_HygieneLibrary(rows))
    assert [f["id"] for f in findings] == [1]
    assert "step 2 is a verify step with no claim" in findings[0]["reason"]


# nexus-iy2ep: the same pay-then-abort class beyond a claim-less verify, caught at the same doors before any step runs.
NO_EVIDENCE = {"steps": [{"tool": "search", "args": {"query": "latency", "corpus": "knowledge"}},
                         {"tool": "verify", "args": {"claim": "latency drops"}}]}
NO_INSTRUCTION = {"steps": [{"tool": "search", "args": {"query": "latency", "corpus": "knowledge"}},
                            {"tool": "check", "args": {"ids": "$step1.ids"}}]}
INSTRUCTED = {"steps": [{"tool": "search", "args": {"query": "latency", "corpus": "knowledge"}},
                        {"tool": "check", "args": {"ids": "$step1.ids", "check_instruction": "does latency drop"}}]}
_DEFECTS = [
    (NO_EVIDENCE, "step 2 is a verify step with neither ids nor evidence"),
    (NO_INSTRUCTION, "step 2 is a check step with no check_instruction"),
]


@pytest.mark.parametrize(
    ("plan", "expected"),
    [
        (CLAIMLESS, (1, runner.claimless_verify_reason(CLAIMLESS))),
        (NO_EVIDENCE, 1),
        (_steps({"claim": "x", "evidence": "  "}), None),  # ids present: hydrated at run time
        ({"steps": [{"tool": "verify", "args": {"claim": "x", "evidence": "$step1.text"}}]}, None),
        ({"steps": [{"tool": "verify", "args": {"claim": "x", "evidence": " "}}]}, 0),
        (NO_INSTRUCTION, 1),
        ({"steps": [{"tool": "mcp__plugin_conexus_nexus__operator_check", "args": {"check_instruction": ""}}]}, 0),
        (INSTRUCTED, None),
        (CLAIMED, None),
        ("not json", None),
        (None, None),
    ],
)
def test_the_operator_arg_scan_finds_the_first_step_missing_an_author_supplied_arg(plan, expected) -> None:
    found = runner.find_operator_arg_defect(plan)
    if expected is None or isinstance(expected, tuple):
        assert found == expected
    else:
        assert found is not None and found[0] == expected


@pytest.mark.parametrize(("plan", "why"), _DEFECTS, ids=["no_evidence", "no_instruction"])
def test_the_planner_re_asks_once_naming_the_defect_then_fails_before_any_step_runs(monkeypatch, plan, why) -> None:
    match, prompts, _ = _planner_run(monkeypatch, [plan, INSTRUCTED])
    assert len(prompts) == 2 and why in prompts[1][len(prompts[0]):]
    assert json.loads(match.plan_json) == INSTRUCTED
    with pytest.raises(ValueError, match=why):
        _planner_run(monkeypatch, [plan, plan])


@pytest.mark.parametrize(("plan", "why"), _DEFECTS, ids=["no_evidence", "no_instruction"])
def test_plan_save_refuses_the_defect_and_writes_nothing(plan, why) -> None:
    out, writes = _save(plan)
    assert out.startswith(f"Plan not saved: {why}") and writes == []


@pytest.mark.parametrize(("plan", "why"), _DEFECTS, ids=["no_evidence", "no_instruction"])
def test_the_seed_loader_refuses_the_defect(tmp_path, plan, why) -> None:
    (tmp_path / "bad.yml").write_text(yaml.safe_dump(_template("bad-arg", plan)))
    (tmp_path / "good.yml").write_text(yaml.safe_dump(_template("good-instructed", INSTRUCTED)))
    result = load_seed_directory(tmp_path, library=_SeedLibrary())
    assert result.inserted == ["good.yml"]
    assert any("bad-arg" in msg and why in msg for _src, msg in result.errors)


@pytest.mark.parametrize(("plan", "why"), _DEFECTS, ids=["no_evidence", "no_instruction"])
def test_the_matcher_excludes_and_hygiene_reports_the_defect(plan, why, info_logging) -> None:
    lib = _Library([_row(1, plan, "does latency drop"), _row(2, INSTRUCTED, "does latency drop, instructed")])
    ids = [m.plan_id for m in plan_match(intent="does latency drop", library=lib, cache=None, min_confidence=0.5)]
    assert 2 in ids and 1 not in ids
    rows = [{**_row(1, plan, "bad"), "verb": "research"}, {**_row(2, INSTRUCTED, "good"), "verb": "research"}]
    findings = _hygiene_scan(_HygieneLibrary(rows))
    assert [f["id"] for f in findings] == [1] and why in findings[0]["reason"]


@pytest.mark.parametrize("name", ["Verify", "VERIFY", "Check"])
def test_a_planned_operator_in_another_case_is_kept_not_dropped(monkeypatch, name) -> None:
    """A dropped verify step would let the answer pass for a checked one."""
    args = {"ids": "$step1.ids", "claim": "latency drops"} if name.lower() == "verify" else {
        "ids": "$step1.ids", "check_instruction": "does latency drop"}
    plan = {"steps": [{"tool": "search", "args": {"query": "latency", "corpus": "knowledge"}}, {"tool": name, "args": args}]}
    match, _, _ = _planner_run(monkeypatch, [plan])
    assert [s["tool"] for s in json.loads(match.plan_json)["steps"]] == ["search", name.lower()]


def test_a_retried_plan_with_an_embedded_step_ref_fails_in_the_planner(monkeypatch) -> None:
    """nexus-iy2ep: the claim retry's plan is rescanned, so an embedded ref it brings fails here, by name."""
    embedded = json.loads(json.dumps(CLAIMED))
    embedded["steps"][0]["args"]["query"] = "see $step9.text"
    with pytest.raises(ValueError, match=r"planner plan cannot run: step reference .*no step was run"):
        _planner_run(monkeypatch, [CLAIMLESS, embedded])
