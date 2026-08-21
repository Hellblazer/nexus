# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-196 Minimum Viable Validation (nexus-nyry9.11, RDR-196 .p1e DO item 5).

Per the RDR's own "Minimum Viable Validation" section: one ``nx_answer``
call end-to-end on a multi-step plan produces a run row whose ``cost_usd``
equals the sum of its step rows, each step row carrying the model and
token counts the subprocess reported, and ``nx_answer(...,
structured=True)`` returns the per-step breakdown. Asserted against the
ENGINE SUBSTRATE (real Postgres via ``tests/conftest.py``'s
``ensure_engine``/``mint_test_tenant``, the ``t2_service_env`` fixture),
not a mock.

This is genuinely a heavy test: it drives a REAL ``claude -p`` subprocess
(the ``summarize`` operator step), so it needs real ``claude`` CLI auth
and costs a small, real amount of API spend. Skips LOUDLY (not silently)
when auth is unavailable — the module-scoped autouse fixture below reports
the skip reason so an unmeasured run is never mistaken for a passing one
(nexus-moht0 vacuous-gate doctrine).

Run with:
  uv run pytest -m integration tests/integration/test_nx_answer_step_telemetry_mvv.py
"""
from __future__ import annotations

import json
import subprocess
from unittest.mock import patch

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.lived_in]


def _claude_auth_available() -> bool:
    """Return True iff ``claude auth status --json`` reports loggedIn.

    Deliberately duplicated from
    ``tests/integration/test_nx_answer_equivalence.py`` rather than
    imported — keeps this file independently runnable without a
    cross-file import into another integration module's internals.
    """
    try:
        result = subprocess.run(
            ["claude", "auth", "status", "--json"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return False
        data = json.loads(result.stdout)
        return bool(data.get("loggedIn") or data.get("isLoggedIn"))
    except Exception:
        return False


@pytest.fixture(scope="module", autouse=True)
def _skip_without_auth():
    if not _claude_auth_available():
        pytest.skip(
            "claude auth not available -- skipping the live RDR-196 MVV "
            "(non-vacuity: this reason is the whole point of the skip, "
            "not a silent pass)"
        )


class TestStepTelemetryMVV:
    """RDR-196 § Minimum Viable Validation, verbatim."""

    @pytest.mark.asyncio
    async def test_multi_step_nx_answer_produces_consistent_step_telemetry(
        self, t2_service_env,
    ) -> None:
        from nexus.db.t2.http_telemetry_store import HttpTelemetryStore
        from nexus.plans.match import Match

        # Hand-built 2-step plan (search -> summarize): deterministic
        # shape, independent of the inline planner's own nondeterministic
        # authoring. `search` stays ISOLATED (not fused into a bundle --
        # bundling only fuses CONSECUTIVE operator steps, and `search`
        # is not one), so this plan is guaranteed to produce two separate
        # StepRecords: one "sql"-source (search) and one "llm"-source
        # (summarize, a REAL claude -p dispatch).
        question = "RDR-196 MVV: describe retrieval patterns in this codebase"
        match = Match(
            plan_id=0, name="mvv-multi-step", description="mvv",
            confidence=None, dimensions={}, tags="",
            plan_json=json.dumps({
                "steps": [
                    {"tool": "search", "args": {
                        "query": "$intent", "corpus": "knowledge", "limit": 3,
                    }},
                    {"tool": "summarize", "args": {
                        "cited": False, "content": "$step1.ids",
                    }},
                ],
            }),
            required_bindings=["intent"], optional_bindings=[],
            default_bindings={}, parent_dims=None,
        )

        with patch("nexus.plans.matcher.plan_match", return_value=[match]):
            from nexus.mcp.core import nx_answer
            result = await nx_answer(question=question, structured=True, trace=True)

        assert isinstance(result, dict), f"expected the structured envelope, got {result!r}"
        steps = result["steps"]
        assert len(steps) == 2, f"expected 2 step records (search + summarize), got {steps}"

        llm_steps = [s for s in steps if s["source"] == "llm"]
        assert llm_steps, f"expected >=1 llm-source step (the real summarize dispatch): {steps}"
        for s in llm_steps:
            if not s["ok"]:
                continue
            assert s["model"], f"a successful llm step must carry a non-null model: {s}"
            assert (s["input_tokens"] or 0) > 0, f"a successful llm step must carry non-zero input_tokens: {s}"
            assert (s["output_tokens"] or 0) > 0, f"a successful llm step must carry non-zero output_tokens: {s}"

        # Sum identity: envelope cost_usd vs sum(steps.cost_usd) -- same
        # blanket rule as _nx_answer_record_run (None+known -> sum of
        # known; all-None -> None, never a fabricated 0.0).
        known_envelope_costs = [s["cost_usd"] for s in steps if s["cost_usd"] is not None]
        envelope_step_sum = sum(known_envelope_costs) if known_envelope_costs else None
        if result["cost_usd"] is None or envelope_step_sum is None:
            assert result["cost_usd"] is None and envelope_step_sum is None, (
                f"cost_usd={result['cost_usd']!r} but sum(steps)={envelope_step_sum!r} -- "
                "one None and one known is a genuine inconsistency"
            )
        else:
            assert result["cost_usd"] == pytest.approx(envelope_step_sum)

        # ── Read back through the REAL engine (not the in-process result) ──
        store = HttpTelemetryStore()
        read_back = store.query_nx_answer_runs(limit=10, include_steps=True)
        assert read_back.get("steps_supported") is True, (
            "the engine substrate this test runs against must carry the "
            "nx_answer_steps read route (nexus-lme1s) -- a False here "
            "means the jar is stale, not that the client is broken"
        )
        matching = next(
            (r for r in read_back["rows"] if r.get("question") == question),
            None,
        )
        assert matching is not None, (
            f"the just-written run row must be readable back from the "
            f"engine; rows seen: {[r.get('question') for r in read_back['rows']]}"
        )
        row_steps = matching.get("steps") or []
        assert len(row_steps) == 2

        # RDR-196 .p1e review-fix (Minor M3, substantive-critic T2
        # [23111]): import the real epsilon check instead of duplicating
        # its formula -- a duplicate literal is a drift risk the moment
        # answer_runs.py's epsilon tightens/loosens.
        from nexus.commands.answer_runs import _costs_agree

        run_cost = matching.get("cost_usd")
        known_row_costs = [s.get("cost_usd") for s in row_steps if s.get("cost_usd") is not None]
        row_step_sum = sum(known_row_costs) if known_row_costs else None
        assert _costs_agree(run_cost, row_step_sum), (
            f"run.cost_usd={run_cost} vs sum(steps)={row_step_sum} "
            "disagree beyond the epsilon _costs_agree enforces"
        )

        # ── Same data, via the CLI surface (`nx answer-runs --steps --json`) ──
        from click.testing import CliRunner

        from nexus.commands.answer_runs import answer_runs_cmd

        cli_result = CliRunner().invoke(
            answer_runs_cmd, ["--steps", "--json", "--limit", "10"],
        )
        assert cli_result.exit_code == 0, cli_result.output
        payload = json.loads(cli_result.stdout)
        assert payload["step_breakdown"]["steps_supported"] is True
        cli_matching = next(
            (r for r in payload["rows"] if r.get("question") == question), None,
        )
        assert cli_matching is not None
        assert len(cli_matching.get("steps") or []) == 2
        assert payload["executed_ok_count"] >= 1

    @pytest.mark.asyncio
    async def test_bundled_operator_steps_produce_one_undivided_step_record(
        self, t2_service_env,
    ) -> None:
        """RDR-196 .p1e review-fix (S5, substantive-critic T2 [23111]):
        the sibling MVV test above deliberately uses an isolated
        (non-fusable) retrieval step + one operator step to get
        deterministic sql+llm sources — it never exercises the BUNDLE
        path end to end, even though bundling is the DEFAULT for two
        consecutive operator steps (runner.py's ``bundle_operators=True``
        default, never overridden by ``nx_answer``). This test drives
        exactly that: two consecutive operator steps (extract ->
        summarize), which ``plan_run`` fuses into ONE real ``claude -p``
        dispatch, and asserts the resulting telemetry is exactly one
        ``source="bundle"`` step record with ``bundled_steps`` populated
        and a real, UNDIVIDED cost (never split across the two fused
        plan indices — see ``StepRecord``'s own docstring on why
        inventing a per-step cost by dividing the bundle's real cost
        would fabricate data).
        """
        from nexus.plans.match import Match

        question = "RDR-196 MVV bundle: summarize this codebase's storage tiers"
        match = Match(
            plan_id=0, name="mvv-bundle-step", description="mvv-bundle",
            confidence=None, dimensions={}, tags="",
            plan_json=json.dumps({
                "steps": [
                    {"tool": "extract", "args": {
                        "fields": "summary",
                        "inputs": json.dumps([
                            "The nexus retrieval layer uses a three-tier "
                            "storage architecture: T1 session scratch, T2 "
                            "persistent notes, and T3 permanent knowledge."
                        ]),
                    }},
                    {"tool": "summarize", "args": {
                        "cited": False, "content": "$step1.extractions",
                    }},
                ],
            }),
            required_bindings=[], optional_bindings=[],
            default_bindings={}, parent_dims=None,
        )

        with patch("nexus.plans.matcher.plan_match", return_value=[match]):
            from nexus.mcp.core import nx_answer
            result = await nx_answer(question=question, structured=True, trace=True)

        assert isinstance(result, dict), f"expected the structured envelope, got {result!r}"
        steps = result["steps"]
        assert len(steps) == 1, (
            f"two consecutive operator steps must fuse into exactly ONE "
            f"StepRecord (bundling, not two isolated dispatches): {steps}"
        )
        bundled = steps[0]
        assert bundled["source"] == "bundle", (
            f"expected the fused-dispatch source label, got {bundled['source']!r} "
            f"-- if this is 'llm', bundling did not fire and the plan ran "
            f"isolated instead (check bundle-eligibility / segment_steps)"
        )
        assert bundled["bundled_steps"] == [0, 1], (
            f"expected both fused plan indices recorded: {bundled['bundled_steps']}"
        )
        if bundled["ok"]:
            assert bundled["model"], f"a successful bundle must carry a non-null model: {bundled}"
            assert (bundled["input_tokens"] or 0) > 0, f"bundle must carry non-zero input_tokens: {bundled}"
            assert (bundled["output_tokens"] or 0) > 0, f"bundle must carry non-zero output_tokens: {bundled}"
            assert bundled["cost_usd"] is not None and bundled["cost_usd"] > 0, (
                f"a successful bundle must carry a real, non-zero, UNDIVIDED "
                f"cost (one dispatch, one real price -- never fabricated by "
                f"dividing across bundled_steps): {bundled}"
            )
        # The envelope's cost_usd is exactly this ONE record's cost --
        # nothing to sum/divide, which is the whole point of "undivided".
        assert result["cost_usd"] == bundled["cost_usd"]
