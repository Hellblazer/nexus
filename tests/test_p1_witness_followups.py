# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-189 P1 follow-ups: a check or verify step with no evidence still returns a Witness Schema v1
payload (nexus-z1d0l.1.10), and the inline planner re-asks once for a verify step with no claim,
then drops it when it is the last step (nexus-z1d0l.1.11)."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from structlog.testing import capture_logs

from nexus.mcp import core as mcp_core
from nexus.plans import runner
from tests.test_6pfgb11_provenance_from_catalog import COLL, X, _position, _t3
from tests.test_6pfgb50_runner_chash_reuses_rows import _CountingCatalog, _plain
from tests.test_plan_run import _match
from tests.test_rdr232_embedded_ref_guards import _planner_run

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
        (CLAIMED, None),
        (_steps({"claim": "$step1.text"}), None),
        ({"steps": [{"tool": "operator_verify", "args": {}}]}, 0),
        ({"steps": [{"tool": "search", "args": {}}]}, None),
        ("not json", None),
        ({"no": "steps"}, None),
        (None, None),
    ],
    ids=["missing", "blank", "null", "claimed", "step_ref", "operator_name", "no_verify", "bad_json", "no_steps", "none"],
)
def test_the_scan_finds_the_first_verify_step_with_no_claim(plan, expected) -> None:
    assert runner.find_claimless_verify(json.dumps(plan) if isinstance(plan, dict) else plan) == expected


def test_a_claimless_verify_is_re_asked_once_naming_the_step(monkeypatch) -> None:
    with capture_logs() as logs:
        match, prompts, cost = _planner_run(monkeypatch, [CLAIMLESS, CLAIMED])
    assert len(prompts) == 2 and prompts[1].startswith(prompts[0])
    added = prompts[1][len(prompts[0]):]
    assert "step 2 is a verify step with no claim" in added
    assert json.loads(match.plan_json) == CLAIMED
    assert cost == pytest.approx(0.50)
    assert [e["step"] for e in logs if e.get("event") == "nx_answer_planner_verify_claim_retry"] == [2]


def test_a_claimed_plan_is_not_re_asked(monkeypatch) -> None:
    match, prompts, _ = _planner_run(monkeypatch, [CLAIMED])
    assert len(prompts) == 1 and json.loads(match.plan_json) == CLAIMED


def test_a_second_claimless_last_step_is_dropped_and_the_answer_is_the_step_before_it(monkeypatch) -> None:
    with capture_logs() as logs:
        match, prompts, _ = _planner_run(monkeypatch, [CLAIMLESS, CLAIMLESS])
    assert len(prompts) == 2, "one retry, no more"
    assert json.loads(match.plan_json) == {"steps": CLAIMLESS["steps"][:1]}
    assert [e["step"] for e in logs if e.get("event") == "nx_answer_planner_verify_step_dropped"] == [2]


def test_a_failed_retry_keeps_the_first_plan_and_still_drops_its_claimless_last_step(monkeypatch) -> None:
    with capture_logs() as logs:
        match, prompts, _ = _planner_run(monkeypatch, [CLAIMLESS, TimeoutError("planner timed out")])
    assert len(prompts) == 2
    assert json.loads(match.plan_json) == {"steps": CLAIMLESS["steps"][:1]}
    assert any(e.get("event") == "nx_answer_planner_verify_claim_retry_failed" for e in logs)


def test_a_claimless_verify_that_a_later_step_reads_is_left_for_plan_run_to_refuse(monkeypatch) -> None:
    plan = _steps({}, after=[{"tool": "summarize", "args": {"content": "$step2.text"}}])
    match, prompts, _ = _planner_run(monkeypatch, [plan, plan])
    assert len(prompts) == 2
    assert json.loads(match.plan_json) == plan
    assert runner.find_claimless_verify(match.plan_json) == 1


def test_a_plan_that_is_only_a_claimless_verify_is_not_emptied(monkeypatch) -> None:
    plan = {"steps": [{"tool": "verify", "args": {"ids": ["a"]}}]}
    match, _, _ = _planner_run(monkeypatch, [plan, plan])
    assert json.loads(match.plan_json) == plan


def test_after_an_embedded_ref_retry_a_claimless_last_verify_is_dropped_without_a_third_call(monkeypatch) -> None:
    embedded = _steps({}, after=[])
    embedded["steps"][0]["args"]["query"] = "see $step9.text"
    match, prompts, _ = _planner_run(monkeypatch, [embedded, CLAIMLESS])
    assert len(prompts) == 2, "the embedded-ref retry is the answer's one retry"
    assert json.loads(match.plan_json) == {"steps": CLAIMLESS["steps"][:1]}
