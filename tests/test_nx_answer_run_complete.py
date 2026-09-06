# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-203 Phase 1 (bead nexus-dt2tu.1): the client choke point
``_nx_answer_record_complete`` that replaces the ten converting
``(_nx_answer_record_run, _nx_answer_record_outcome)`` pairs in
``nx_answer`` (``src/nexus/mcp/core.py``).

Three test classes, matching the RDR's Tests section and this phase's
named tests:

- ``TestConvertingArmCensus`` —
  ``test_every_converting_arm_routes_through_the_choke_point``. An AST
  census over ``core.py`` pinning the SURVIVOR COUNT and SURVIVOR NAMING
  clauses only (P1's slice of the named test; the preceded-by clause
  names ``_nx_answer_ensure_run_started``, which does not exist until P3
  — residual 13). Naming the survivors, not merely counting them, is
  what makes the census both true and useful: a quietly-folded-in D6
  exclusion changes WHICH lines survive without changing the count.
- ``TestRecordingArmsDownstreamOfRunStart`` —
  ``test_recording_arms_are_downstream_of_run_start``. Reads A11 (RDR
  plan-audit residual, round 2) before writing itself: a bare "is this
  arm's own source line greater than run-start's" check is FALSE for the
  arm nested inside ``_budget_exhausted_response`` — that function is
  DEFINED upstream of the run-start site in the file, but every one of
  its CALL SITES is downstream in execution. This test does real
  call-site reasoning for exactly that one case rather than a uniform
  source-order scan, and states which of A11's two options it took.
- ``TestChokePointComposesRecordAndOutcome`` — the redaction test named
  in the RDR's Tests section (residual 9), plus the ``cost_usd``
  aggregation and the outcome no-op/independent-catch behaviour residual
  11 assigns to this phase. Unit-level: calls
  ``_nx_answer_record_complete`` directly against a ``MagicMock`` ``db``,
  never a real T2 substrate — the wire calls it makes
  (``db.telemetry.record_nx_answer_run`` /
  ``db.plans.increment_run_outcome``) are exactly what's asserted on.

Every one of these tests was exercised RED before being left GREEN: the
docstring of each records the concrete falsifier and confirms it flips
the test, per this phase's TDD constraint ("a gate that cannot go red
proves nothing").
"""
from __future__ import annotations

import ast
import json
import pathlib
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

import nexus.mcp_infra as mi
from nexus.mcp.core import _nx_answer_record_complete
from nexus.plans.runner import StepRecord

REPO_ROOT = pathlib.Path(__file__).parent.parent
CORE_PY = REPO_ROOT / "src" / "nexus" / "mcp" / "core.py"


def _parse_core() -> ast.Module:
    return ast.parse(CORE_PY.read_text())


def _find_function(tree: ast.AST, name: str) -> ast.AsyncFunctionDef | ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name!r} not found in {CORE_PY}")


def _direct_calls(scope: ast.AST, func_name: str) -> list[ast.Call]:
    """Every ``ast.Call`` in *scope* whose callee is the bare NAME
    *func_name* (a module-level function call, not ``obj.func_name(...)``
    and not the ``def func_name`` itself, which is a different node type).
    """
    return [
        node for node in ast.walk(scope)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == func_name
    ]


def _kwarg(call: ast.Call, name: str) -> ast.expr | None:
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


def _is_none_constant(node: ast.expr | None) -> bool:
    return isinstance(node, ast.Constant) and node.value is None


def _is_name(node: ast.expr | None, name: str) -> bool:
    return isinstance(node, ast.Name) and node.id == name


class TestConvertingArmCensus:
    """``test_every_converting_arm_routes_through_the_choke_point``
    (P1's slice — survivor-count and survivor-naming clauses only).

    Falsifier exercised: restored one converted arm to its old direct
    ``(_nx_answer_record_run, _nx_answer_record_outcome)`` pair (the
    empty-retrieval-guard arm) and confirmed
    ``test_exactly_two_record_run_survivors`` and
    ``test_exactly_one_record_outcome_survivor`` both red (three
    ``_nx_answer_record_run`` calls, two ``_nx_answer_record_outcome``
    calls) before reverting the edit.
    """

    def setup_method(self) -> None:
        self.tree = _parse_core()
        self.record_run_calls = _direct_calls(self.tree, "_nx_answer_record_run")
        self.record_outcome_calls = _direct_calls(self.tree, "_nx_answer_record_outcome")
        self.record_complete_calls = _direct_calls(self.tree, "_nx_answer_record_complete")

    def test_exactly_two_record_run_survivors(self) -> None:
        assert len(self.record_run_calls) == 2, (
            f"expected exactly 2 direct _nx_answer_record_run call sites "
            f"(the two D6 exclusions), found {len(self.record_run_calls)} "
            f"at lines {sorted(c.lineno for c in self.record_run_calls)}. "
            "A ten-and-two split means a converted arm was reverted (or a "
            "new arm was added and left unconverted)."
        )

    def test_exactly_one_record_outcome_survivor(self) -> None:
        assert len(self.record_outcome_calls) == 1, (
            f"expected exactly 1 direct _nx_answer_record_outcome call "
            f"site (the RDR-200 handoff's), found "
            f"{len(self.record_outcome_calls)} at lines "
            f"{sorted(c.lineno for c in self.record_outcome_calls)}."
        )

    def test_survivors_are_named_as_the_two_d6_exclusions(self) -> None:
        """Not just two survivors — THESE two: the planner-failure arm
        (records ``plan_id=None``) and the RDR-200 continuation handoff
        (records ``final_text=_handoff_text``). An implementer who
        quietly folds one D6 exclusion into the choke point while
        leaving some OTHER direct pair unconverted would still pass the
        bare count assertions above; this is the clause that catches it.
        """
        planner_failure = [
            c for c in self.record_run_calls if _is_none_constant(_kwarg(c, "plan_id"))
        ]
        handoff = [
            c for c in self.record_run_calls
            if _is_name(_kwarg(c, "final_text"), "_handoff_text")
        ]
        assert len(planner_failure) == 1, (
            "expected exactly one surviving _nx_answer_record_run call "
            "with plan_id=None (the planner-failure arm); found "
            f"{len(planner_failure)}"
        )
        assert len(handoff) == 1, (
            "expected exactly one surviving _nx_answer_record_run call "
            "with final_text=_handoff_text (the RDR-200 continuation "
            f"handoff); found {len(handoff)}"
        )
        # The two survivors must be DISTINCT call sites, not one call
        # that happens to satisfy both predicates.
        assert planner_failure[0] is not handoff[0]

        # The one surviving _nx_answer_record_outcome call belongs to the
        # handoff arm: it sits a short distance after the handoff's
        # record call, with no other _nx_answer_record_run /
        # _nx_answer_record_complete call between them.
        handoff_line = handoff[0].lineno
        [outcome_call] = self.record_outcome_calls
        assert handoff_line < outcome_call.lineno <= handoff_line + 20, (
            f"the surviving _nx_answer_record_outcome call at line "
            f"{outcome_call.lineno} is not immediately after the handoff's "
            f"record call at line {handoff_line} — it may belong to a "
            "different (unconverted) arm."
        )
        between = [
            c for c in (*self.record_run_calls, *self.record_complete_calls)
            if handoff_line < c.lineno < outcome_call.lineno
        ]
        assert not between, (
            "a converting or surviving call sits between the handoff's "
            f"record and outcome calls: lines {[c.lineno for c in between]}"
        )
        success_kwarg = _kwarg(outcome_call, "success")
        assert isinstance(success_kwarg, ast.Constant) and success_kwarg.value is True, (
            "the surviving _nx_answer_record_outcome call is expected to "
            "record success=True, matching today's RDR-200 handoff "
            f"(RDR-200 R2 ordering); got {ast.dump(success_kwarg)}"
        )

    def test_ten_converting_arms_route_through_the_choke_point(self) -> None:
        """Sanity companion to the two counts above: exactly ten call
        sites reach the new choke point at all. This does not, by
        itself, catch a reverted arm (that shows up as a record_run/
        record_outcome survivor-count red instead) — it catches the
        choke point being bypassed some OTHER way, e.g. an arm calling
        ``db.telemetry.record_nx_answer_run`` directly.
        """
        assert len(self.record_complete_calls) == 10, (
            f"expected exactly 10 _nx_answer_record_complete call sites, "
            f"found {len(self.record_complete_calls)} at lines "
            f"{sorted(c.lineno for c in self.record_complete_calls)}"
        )

    def test_survivors_preceded_by_ensure_run_started(self) -> None:
        """RDR-203 P3 residual 13 (the second half of P1's split test):
        each surviving direct ``_nx_answer_record_run`` call is
        IMMEDIATELY preceded by an ``_nx_answer_ensure_run_started``
        call. Catches a survivor that keeps its record write and loses
        its deferred bump. Falsifier: delete one survivor's
        ``_nx_answer_ensure_run_started`` call and this reds.
        """
        ensure_calls = _direct_calls(self.tree, "_nx_answer_ensure_run_started")
        # Three call sites in the whole file: the choke point's own
        # degradation-branch call (residual A1), and the two D6
        # survivors' calls. Precise, not a lower bound: a fourth call
        # site anywhere would be worth investigating on its own, not
        # silently accepted by a `>=` check.
        assert len(ensure_calls) == 3, (
            f"expected exactly 3 _nx_answer_ensure_run_started call sites "
            f"(the choke point's own degradation branch, plus the two D6 "
            f"survivors), found {len(ensure_calls)} at lines "
            f"{sorted(c.lineno for c in ensure_calls)}"
        )
        for record_call in self.record_run_calls:
            preceding = [c for c in ensure_calls if c.lineno < record_call.lineno]
            assert preceding, (
                f"_nx_answer_record_run call at line {record_call.lineno} "
                f"has no preceding _nx_answer_ensure_run_started call at all"
            )
            nearest = max(preceding, key=lambda c: c.lineno)
            between = [
                n for n in ast.walk(self.tree)
                if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Name)
                and n.func.id in ("_nx_answer_record_run", "_nx_answer_ensure_run_started")
                and nearest.lineno < n.lineno < record_call.lineno
            ]
            assert not between, (
                f"expected the _nx_answer_ensure_run_started call at line "
                f"{nearest.lineno} to be IMMEDIATELY before the "
                f"_nx_answer_record_run call at line {record_call.lineno}, "
                f"but found intervening calls at "
                f"{[n.lineno for n in between]}"
            )


class TestRecordingArmsDownstreamOfRunStart:
    """``test_recording_arms_are_downstream_of_run_start``.

    A11 choice, stated per the residual's own instruction to say which
    of the two options was taken: this test does NOT implement a general
    call-graph walk from ``nx_answer``'s entry. It implements the
    narrower, genuinely checkable property the RDR's own text already
    proves is the one case source-order gets wrong:
    ``_budget_exhausted_response`` is DEFINED upstream of the run-start
    guard (it is a nested function whose body — containing one
    converting arm — is textually written before the guard), but it is
    only ever INVOKED from call sites downstream of it. For that one
    function this test reasons about call sites, which IS real
    (one-hop) call-graph reasoning, not source order.

    For every OTHER converting call — a plain, un-nested statement
    directly in ``nx_answer``'s own body — source order equals execution
    order: they are sequential statements in one straight-line async
    function with no loops feeding back above the run-start guard, so a
    later source line cannot execute before an earlier one. That
    equivalence is what license this test to use ``lineno`` comparison
    for those calls without overclaiming a general property; it is
    verified by the "not otherwise reasoned about" check below, which
    excludes only the one function whose body executes out of source
    order (D6's planner-failure arm is excluded on its own terms, as the
    one arm the RDR names as legitimately upstream — it records
    ``plan_id=None`` and takes no counters, so nothing double-counts).

    Falsifier exercised: temporarily moved one direct converting arm's
    ``_nx_answer_record_complete`` call (the single-step-fast-path
    success arm) to a point in the source textually BEFORE the
    run-start guard, confirmed
    ``test_direct_choke_point_calls_are_downstream_of_run_start`` reds,
    then reverted. Separately moved a ``_budget_exhausted_response(``
    call site above the guard and confirmed
    ``test_budget_exhausted_response_call_sites_are_downstream_of_run_start``
    reds, then reverted.
    """

    def setup_method(self) -> None:
        self.tree = _parse_core()
        self.nx_answer = _find_function(self.tree, "nx_answer")
        run_start_calls = [
            node for node in ast.walk(self.nx_answer)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "increment_run_started"
        ]
        assert len(run_start_calls) == 1, (
            "expected exactly one increment_run_started call in nx_answer "
            f"(the run-start site); found {len(run_start_calls)}"
        )
        self.run_start_line = run_start_calls[0].lineno

        budget_fns = [
            node for node in ast.walk(self.nx_answer)
            if isinstance(node, ast.FunctionDef)
            and node.name == "_budget_exhausted_response"
        ]
        assert len(budget_fns) == 1
        self.budget_fn = budget_fns[0]

    def _inside_budget_fn(self, node: ast.AST) -> bool:
        return self.budget_fn.lineno <= node.lineno <= self.budget_fn.end_lineno

    def test_budget_exhausted_response_call_sites_are_downstream_of_run_start(self) -> None:
        call_sites = [
            node for node in ast.walk(self.nx_answer)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_budget_exhausted_response"
        ]
        assert call_sites, "expected at least one _budget_exhausted_response(...) call site"
        for call in call_sites:
            assert call.lineno > self.run_start_line, (
                f"_budget_exhausted_response(...) called at line {call.lineno}, "
                f"which is NOT downstream of the run-start site at line "
                f"{self.run_start_line} — the arm it contains would execute "
                "before use_count is bumped, double-counting or "
                "under-counting a plan's use."
            )

    def test_direct_choke_point_calls_are_downstream_of_run_start(self) -> None:
        direct_calls = [
            c for c in _direct_calls(self.nx_answer, "_nx_answer_record_complete")
            if not self._inside_budget_fn(c)
        ]
        assert direct_calls, "expected at least one direct _nx_answer_record_complete call"
        for call in direct_calls:
            assert call.lineno > self.run_start_line, (
                f"_nx_answer_record_complete(...) at line {call.lineno} is "
                f"NOT downstream of the run-start site at line "
                f"{self.run_start_line}."
            )

    def test_planner_failure_arm_is_the_one_pinned_exception(self) -> None:
        record_run_calls = _direct_calls(self.nx_answer, "_nx_answer_record_run")
        upstream = [c for c in record_run_calls if c.lineno < self.run_start_line]
        downstream = [c for c in record_run_calls if c.lineno > self.run_start_line]
        assert len(upstream) == 1, (
            "expected exactly one surviving _nx_answer_record_run call "
            f"upstream of run-start (the planner-failure arm); found "
            f"{len(upstream)} at lines {[c.lineno for c in upstream]}"
        )
        assert _is_none_constant(_kwarg(upstream[0], "plan_id")), (
            "the one recording arm upstream of run-start must be the "
            "planner-failure arm (plan_id=None) — a different arm has "
            "moved upstream instead."
        )
        assert len(downstream) == 1, (
            f"expected exactly one surviving _nx_answer_record_run call "
            f"downstream of run-start (the RDR-200 handoff); found "
            f"{len(downstream)}"
        )


class TestChokePointComposesRecordAndOutcome:
    """Residuals 9 and 11: the choke point must reproduce
    ``_nx_answer_record_run``'s redaction/``cost_usd`` behaviour and
    ``_nx_answer_record_outcome``'s guard/catch, on the composite path
    specifically — not merely on the two functions it replaces (which
    keep their own tests elsewhere and are untouched by this phase).

    Falsifiers exercised for each test below are noted in its own
    docstring.
    """

    def _make_db(self) -> MagicMock:
        db = MagicMock()
        db.telemetry.record_nx_answer_run = MagicMock()
        return db

    def _fake_t2_index_write(self, outcome_db: MagicMock):
        """Stand-in for ``nexus.mcp.core._t2_index_write``: runs
        ``write_fn`` against *outcome_db* (a database SEPARATE from the
        record half's ``db``) and records every ``op`` it was called
        with. This is what actually exercising the round-2 review fix
        requires — the outcome half no longer reuses the record half's
        ``db``, it issues its own independent ``_t2_index_write`` call
        (T2 nexus/code-review-nexus-dt2tu-1-p1 [24711],
        nexus/critique-nexus-dt2tu-1-p1 [24713]), so a test that only
        ever hands the choke point one shared ``MagicMock`` and checks
        that same mock's ``increment_run_outcome`` never observes the
        real call at all — exactly the gap the round-2 critique named.
        Does not swallow an exception raised inside ``write_fn``, same
        as the real ``_t2_index_write`` -> ``_service_t2_write_locked``
        (which classifies, may evict, then re-raises).
        """
        ops: list[str] = []

        def _fake(write_fn, *, op: str = "t2_write"):
            ops.append(op)
            return write_fn(outcome_db)

        return _fake, ops

    def test_redacts_question_and_final_text_when_trace_is_false(self) -> None:
        """Falsifier: deleted the ``trace`` branch (hardcoded ``q =
        question`` / ``text = final_text``) — this test reds because the
        record call then carries the real question/final_text instead
        of ``"[redacted]"``.
        """
        db = self._make_db()
        _nx_answer_record_complete(
            db, question="what is the secret plan", plan_id=0,
            matched_confidence=None, step_count=0, final_text="the secret answer",
            step_records=[], duration_ms=10, trace=False, success=True,
            composite_supported_at_start=False, early_bump_fired=False,
        )
        _, kwargs = db.telemetry.record_nx_answer_run.call_args
        assert kwargs["question"] == "[redacted]"
        assert kwargs["final_text"] == "[redacted]"

    def test_does_not_redact_when_trace_is_true(self) -> None:
        db = self._make_db()
        _nx_answer_record_complete(
            db, question="what is the secret plan", plan_id=0,
            matched_confidence=None, step_count=0, final_text="the secret answer",
            step_records=[], duration_ms=10, trace=True, success=True,
            composite_supported_at_start=False, early_bump_fired=False,
        )
        _, kwargs = db.telemetry.record_nx_answer_run.call_args
        assert kwargs["question"] == "what is the secret plan"
        assert kwargs["final_text"] == "the secret answer"

    def test_cost_usd_sums_known_step_costs(self) -> None:
        """Falsifier: hardcoded ``cost_usd=0.0`` in place of the sum —
        this test reds because it would assert ``0.6`` and see ``0.0``.
        """
        db = self._make_db()
        steps = [
            StepRecord(cost_usd=0.25),
            StepRecord(cost_usd=None),
            StepRecord(cost_usd=0.35),
        ]
        _nx_answer_record_complete(
            db, question="q", plan_id=0, matched_confidence=None, step_count=3,
            final_text="a", step_records=steps, duration_ms=10, trace=True,
            success=True,
            composite_supported_at_start=False, early_bump_fired=False,
        )
        _, kwargs = db.telemetry.record_nx_answer_run.call_args
        assert kwargs["cost_usd"] == pytest.approx(0.6)

    def test_cost_usd_is_none_when_no_step_reports_a_known_cost(self) -> None:
        """Falsifier: ``sum(_known_costs) if _known_costs else None``
        replaced with a bare ``sum(_known_costs)`` — this test reds
        because ``sum([])`` is ``0.0``, a fabricated known-zero cost, not
        ``None``.
        """
        db = self._make_db()
        steps = [StepRecord(cost_usd=None), StepRecord(cost_usd=None)]
        _nx_answer_record_complete(
            db, question="q", plan_id=0, matched_confidence=None, step_count=2,
            final_text="a", step_records=steps, duration_ms=10, trace=True,
            success=True,
            composite_supported_at_start=False, early_bump_fired=False,
        )
        _, kwargs = db.telemetry.record_nx_answer_run.call_args
        assert kwargs["cost_usd"] is None

    def test_outcome_bump_is_a_no_op_for_falsy_plan_id(self) -> None:
        """Falsifier: dropped the ``if plan_id:`` guard — this test reds
        because ``_t2_index_write`` would then be invoked at all (with
        ``plan_id=0``, the synthetic inline-planner id that has no
        library row to bump), instead of never firing.
        """
        db = self._make_db()
        outcome_db = MagicMock()
        fake_write, ops = self._fake_t2_index_write(outcome_db)
        with patch("nexus.mcp.core._t2_index_write", fake_write):
            _nx_answer_record_complete(
                db, question="q", plan_id=0, matched_confidence=None, step_count=1,
                final_text="a", step_records=[], duration_ms=10, trace=True,
                success=True,
                composite_supported_at_start=False, early_bump_fired=False,
            )
        assert ops == [], "the outcome half must not call _t2_index_write at all for plan_id=0"
        outcome_db.plans.increment_run_outcome.assert_not_called()
        db.telemetry.record_nx_answer_run.assert_called_once()

    def test_outcome_bump_fires_for_a_usable_plan_id(self) -> None:
        """Round-2 review fix (T2 [24711]/[24713]): the outcome bump
        goes through the choke point's OWN ``_t2_index_write(op=
        "run_outcome")`` call, not the record half's ``db`` — see
        ``_fake_t2_index_write``'s docstring.
        """
        db = self._make_db()
        outcome_db = MagicMock()
        fake_write, ops = self._fake_t2_index_write(outcome_db)
        with patch("nexus.mcp.core._t2_index_write", fake_write):
            _nx_answer_record_complete(
                db, question="q", plan_id=42, matched_confidence=0.9, step_count=1,
                final_text="a", step_records=[], duration_ms=10, trace=True,
                success=True,
                composite_supported_at_start=False, early_bump_fired=False,
            )
        outcome_db.plans.increment_run_outcome.assert_called_once_with(42, success=True)
        assert ops == ["run_outcome"]

    def test_record_failure_does_not_block_the_outcome_bump(self) -> None:
        """Residual 11: the two writes carry INDEPENDENT boundary
        catches. Falsifier: wrapped both writes in one shared
        try/except — this test reds because a record-write failure
        would then swallow the outcome bump too.
        """
        db = self._make_db()
        db.telemetry.record_nx_answer_run.side_effect = RuntimeError("connection reset")
        outcome_db = MagicMock()
        fake_write, ops = self._fake_t2_index_write(outcome_db)
        with patch("nexus.mcp.core._t2_index_write", fake_write):
            _nx_answer_record_complete(
                db, question="q", plan_id=7, matched_confidence=0.5, step_count=1,
                final_text="a", step_records=[], duration_ms=10, trace=True,
                success=False,
                composite_supported_at_start=False, early_bump_fired=False,
            )
        outcome_db.plans.increment_run_outcome.assert_called_once_with(7, success=False)
        assert ops == ["run_outcome"]

    def test_outcome_failure_does_not_raise(self) -> None:
        """Residual 11: losing the outcome's boundary catch turns a
        best-effort telemetry failure into a crashed answer. Falsifier:
        removed the outcome's try/except — this test reds with an
        uncaught ``RuntimeError`` instead of returning normally. The
        real ``_t2_index_write`` -> ``_service_t2_write_locked`` does
        NOT swallow the exception itself (it classifies for eviction,
        then re-raises), so ``_fake_t2_index_write`` mirrors that: this
        test proves the choke point's OWN catch is what keeps the
        caller from seeing the exception, not the transport.
        """
        db = self._make_db()
        outcome_db = MagicMock()
        outcome_db.plans.increment_run_outcome.side_effect = RuntimeError("connection reset")
        fake_write, ops = self._fake_t2_index_write(outcome_db)
        with patch("nexus.mcp.core._t2_index_write", fake_write):
            _nx_answer_record_complete(
                db, question="q", plan_id=7, matched_confidence=0.5, step_count=1,
                final_text="a", step_records=[], duration_ms=10, trace=True,
                success=True,
                composite_supported_at_start=False, early_bump_fired=False,
            )  # must not raise
        db.telemetry.record_nx_answer_run.assert_called_once()
        assert ops == ["run_outcome"]


class TestOutcomeBumpReachesEvictionClassifier:
    """Round-2 review fix (T2 nexus/code-review-nexus-dt2tu-1-p1 [24711],
    nexus/critique-nexus-dt2tu-1-p1 [24713]): before this fix, the
    choke point called ``db.plans.increment_run_outcome(...)`` directly
    against the record half's already-open ``db``, which for 9 of 10
    arms is a fresh, non-singleton ``T2Database`` from ``_t2_ctx()`` —
    it never touches ``_service_t2_write_locked``'s connectivity
    classifier at all, so a connectivity failure on the outcome bump
    could no longer trigger the shared singleton's self-healing
    eviction, a real behaviour ``_nx_answer_record_outcome`` provided
    before this bead. This test proves the fix against the REAL
    mechanism (``mcp_infra.t2_index_write`` / ``_service_t2_write_locked``
    / the shared singleton), not a ``MagicMock`` that bypasses it —
    mirrors the harness shape of
    ``tests/test_t2_index_write_service_mode_cache.py``'s
    ``test_record_run_connectivity_error_is_swallowed_not_evicted`` /
    ``test_price_table_connectivity_error_is_swallowed_not_evicted``,
    but proves the OPPOSITE direction: eviction DOES fire here, because
    the outcome bump's failure must reach the classifier before this
    function's own boundary catch absorbs it.

    Falsifier exercised: temporarily replaced the outcome half's
    ``_t2_index_write(lambda db: db.plans.increment_run_outcome(...),
    op="run_outcome")`` call with a bare
    ``db.plans.increment_run_outcome(...)`` against the record half's
    ``db`` (today's pre-fix, committed shape) and confirmed this test
    reds: with the singleton pre-warmed and healthy, the buggy code
    raises the SAME ``ConnectionError`` (armed on the caller-supplied
    ``db`` too, so the failure is genuinely there to see either way) but
    never routes it through ``_t2_index_write`` at all, so the singleton
    is left completely untouched (``mi._service_t2_db is original``,
    ``original.closed is False``) instead of evicted. Reverted after
    confirming red.

    NON-VACUITY NOTE for anyone re-deriving this test: a version that
    skips the pre-warm step (asserts starting from ``_service_t2_db is
    None`` and ending at ``is None``) is vacuous against the bug — the
    buggy code never touches ``_service_t2_write_locked`` at all for the
    outcome write, so the singleton would stay ``None`` -> ``None``
    whether or not the fix is present, and the assertion would pass on
    BOTH the fixed and the buggy code. Pre-warming to a known, non-``None``,
    unclosed instance is what makes "evicted" and "untouched" distinguishable.
    """

    def test_connectivity_failure_on_outcome_bump_evicts_the_shared_singleton(
        self, monkeypatch,
    ) -> None:
        class _FakeT2Database:
            def __init__(self, *_a, **_kw) -> None:
                self.telemetry = MagicMock()
                self.plans = MagicMock()
                self.plans.increment_run_outcome.side_effect = ConnectionError(
                    "telemetry store unreachable",
                )
                self.closed = False

            def close(self) -> None:
                self.closed = True

        monkeypatch.setattr("nexus.db.t2.T2Database", _FakeT2Database)

        assert mi._service_t2_db is None, (
            "test must start with no resolved singleton -- the suite's "
            "autouse _reset_service_t2_db fixture (conftest.py) should "
            "already guarantee this"
        )

        # Pre-warm the shared singleton via a successful write (never
        # touches increment_run_outcome, so the armed side_effect above
        # does not fire here) -- see the class docstring's non-vacuity
        # note for why this step is load-bearing.
        mi.t2_index_write(lambda db: None, op="warmup")
        original = mi._service_t2_db
        assert original is not None and original.closed is False, (
            "pre-warm must leave a healthy, resolved singleton in place"
        )

        # The record half's own db is a SEPARATE object from the
        # singleton (exactly as `with _t2_ctx() as db:` provides for 9 of
        # the 10 real arms) -- armed with the SAME failure so the buggy
        # code (which calls increment_run_outcome directly on THIS db)
        # genuinely encounters a connectivity error too, rather than the
        # test passing merely because nothing raised at all.
        db = MagicMock()
        db.telemetry.record_nx_answer_run = MagicMock()
        db.plans.increment_run_outcome.side_effect = ConnectionError(
            "telemetry store unreachable",
        )

        _nx_answer_record_complete(
            db, question="q", plan_id=99, matched_confidence=0.5, step_count=1,
            final_text="a", step_records=[], duration_ms=10, trace=True,
            success=True,
            composite_supported_at_start=False, early_bump_fired=False,
        )  # must not raise -- the choke point's own catch absorbs it

        assert mi._service_t2_db is None, (
            "the connectivity failure on the outcome bump must evict the "
            "pre-warmed singleton (routed through _service_t2_write_locked's "
            "classifier), not leave it in place"
        )
        assert original.closed is True, (
            "the evicted singleton must actually be closed once its "
            "refcount drains, not merely detached from _service_t2_db"
        )


# ── RDR-203 P3 (nexus-dt2tu.3): the capability-probe branch at the ────────────
# run-start site, and the choke point's composite/degradation routing.
#
# These tests drive nx_answer() END TO END with a controllable MagicMock
# db_stub, exactly matching the pattern already established in
# tests/test_nx_answer.py (patch plan_match + plan_run, patch _t2_ctx AND
# _t2_index_write to route to the SAME db_stub) -- unit-testing
# _nx_answer_record_complete alone cannot exercise the run-start site's own
# branching (the capability probe read, the early-bump guard), which live in
# nx_answer's own body, upstream of the choke point.


async def _drive_success_path(
    db_stub: MagicMock, *, question: str = "what is projection quality?",
) -> str:
    """Drive one nx_answer() call through the plan_run ("needs_operators")
    happy path against *db_stub* -- a real library plan match (plan_id=1),
    plan_run mocked to a trivial success. Callers pre-arm db_stub's
    telemetry/plans mocks before calling this."""
    from nexus.mcp.core import nx_answer
    from tests.test_nx_answer import _make_match

    match = _make_match(confidence=0.9)
    plan_run_result = MagicMock()
    plan_run_result.steps = [{"text": "The final answer."}]

    with patch("nexus.plans.matcher.plan_match", return_value=[match]), \
         patch("nexus.plans.runner.plan_run", AsyncMock(return_value=plan_run_result)), \
         patch("nexus.mcp.core._t2_ctx") as t2_ctx, \
         patch("nexus.mcp.core._t2_index_write", lambda fn, **_kw: fn(db_stub)), \
         patch("nexus.mcp.core.scratch", return_value="ok"), \
         patch("nexus.mcp_infra.get_t1_plan_cache", return_value=None):
        t2_ctx.return_value.__enter__.return_value = db_stub
        return await nx_answer(question=question)


def _make_ordered_db_stub() -> tuple[MagicMock, list[str]]:
    """A db_stub whose plans/telemetry methods append to a shared, ordered
    call log -- what the degradation-path ordering tests assert on."""
    order: list[str] = []
    db_stub = MagicMock()
    db_stub.plans.increment_run_started.side_effect = (
        lambda *_a, **_kw: order.append("run_start")
    )
    db_stub.telemetry.record_nx_answer_run.side_effect = (
        lambda **_kw: order.append("record")
    )
    db_stub.plans.increment_run_outcome.side_effect = (
        lambda *_a, **_kw: order.append("outcome")
    )
    db_stub.telemetry.record_nx_answer_run_complete.side_effect = (
        lambda **_kw: order.append("complete")
    )
    return db_stub, order


class TestCapabilityProbeAtRunStart:
    """The D5 per-call capability record: read once at the run-start site,
    outside the ``if best.plan_id:`` guard, carried by every terminating
    arm -- never re-read."""

    @pytest.mark.asyncio
    async def test_unsupported_engine_degrades_to_three_calls(self) -> None:
        """RDR Tests: a stub /version with no
        nx_answer_run_complete_supported key; assert exactly run_start,
        record, outcome, in that order, and no request to /complete.
        Falsifier: force the flag true against the same stub and the
        assertion reds (see test_supported_engine_skips_run_start below,
        which is exactly that flip)."""
        db_stub, order = _make_ordered_db_stub()
        db_stub.telemetry._supports_nx_answer_run_complete.return_value = False

        result = await _drive_success_path(db_stub)

        assert "final answer" in result.lower()
        assert order == ["run_start", "record", "outcome"]
        db_stub.telemetry.record_nx_answer_run_complete.assert_not_called()

    @pytest.mark.asyncio
    async def test_probe_failure_reads_as_unsupported(self) -> None:
        """``/version`` raises; same three calls, same order."""
        db_stub, order = _make_ordered_db_stub()
        db_stub.telemetry._supports_nx_answer_run_complete.side_effect = (
            RuntimeError("engine unreachable")
        )

        result = await _drive_success_path(db_stub)

        assert "final answer" in result.lower()
        assert order == ["run_start", "record", "outcome"]
        db_stub.telemetry.record_nx_answer_run_complete.assert_not_called()

    @pytest.mark.asyncio
    async def test_supported_engine_skips_run_start(self) -> None:
        """No request to ``/v1/plans/metrics/run_start`` at all -- the
        falsifier for the unsupported-degrades test above."""
        db_stub, order = _make_ordered_db_stub()
        db_stub.telemetry._supports_nx_answer_run_complete.return_value = True

        result = await _drive_success_path(db_stub)

        assert "final answer" in result.lower()
        db_stub.plans.increment_run_started.assert_not_called()
        assert "run_start" not in order
        db_stub.telemetry.record_nx_answer_run_complete.assert_called_once()

    @pytest.mark.asyncio
    async def test_mid_call_flag_flip_does_not_change_this_calls_bump_count(
        self,
    ) -> None:
        """Flip the shared store's cached capability flag from true to
        false between the run-start site's read and the terminating arm
        -- simulated by a probe mock that would answer differently on a
        SECOND call -- and assert exactly one bump for this call.
        Falsifier: have the arm re-consult the flag instead of the
        per-call record and this either double-reads the probe or takes
        the wrong route; asserting call_count == 1 here catches the
        re-read directly, which is the property under test.
        """
        db_stub, order = _make_ordered_db_stub()
        db_stub.telemetry._supports_nx_answer_run_complete = MagicMock(
            side_effect=[True, False, False, False],
        )

        result = await _drive_success_path(db_stub)

        assert "final answer" in result.lower()
        assert db_stub.telemetry._supports_nx_answer_run_complete.call_count == 1, (
            "the probe must be read exactly once per call, even though it "
            "is configured to answer differently if called again"
        )
        db_stub.telemetry.record_nx_answer_run_complete.assert_called_once()
        db_stub.plans.increment_run_started.assert_not_called()
        assert "run_start" not in order

    @pytest.mark.asyncio
    async def test_plan_miss_against_supporting_engine_takes_composite(self) -> None:
        """The falsifier for D5's placement rule: a plan-miss call
        (``plan_id == 0``, the synthetic inline-planner match) against a
        supporting stub posts to ``/complete`` once, with plan_id null or
        zero, and no counter routes touched. Falsifier: move the probe
        read and the two assignments inside the ``if best.plan_id:``
        guard and this reds, because ``composite_supported_at_start``
        stays at its entry default and every plan-miss call silently
        takes the degradation path."""
        from nexus.plans.match import Match

        db_stub, order = _make_ordered_db_stub()
        db_stub.telemetry._supports_nx_answer_run_complete.return_value = True

        ad_hoc_plan = json.dumps({
            "steps": [
                {"tool": "search", "args": {"query": "$intent", "corpus": "knowledge"}},
            ],
        })

        async def fake_miss(question, scope="", max_steps=6, few_shot_matches=None):
            return Match(
                plan_id=0, name="ad-hoc", description="", confidence=None,
                dimensions={}, tags="", plan_json=ad_hoc_plan,
                required_bindings=[], optional_bindings=[],
                default_bindings={}, parent_dims=None,
            )

        plan_run_result = MagicMock()
        plan_run_result.steps = [{"text": "The final answer."}]

        from nexus.mcp.core import nx_answer

        with patch("nexus.plans.matcher.plan_match", return_value=[]), \
             patch("nexus.mcp.core._nx_answer_plan_miss", AsyncMock(side_effect=fake_miss)), \
             patch("nexus.plans.runner.plan_run", AsyncMock(return_value=plan_run_result)), \
             patch("nexus.mcp.core._t2_ctx") as t2_ctx, \
             patch("nexus.mcp.core._t2_index_write", lambda fn, **_kw: fn(db_stub)), \
             patch("nexus.mcp.core.scratch", return_value="ok"), \
             patch("nexus.mcp_infra.get_t1_plan_cache", return_value=None):
            t2_ctx.return_value.__enter__.return_value = db_stub
            result = await nx_answer(question="ad hoc question")

        assert "final answer" in result.lower()
        db_stub.telemetry.record_nx_answer_run_complete.assert_called_once()
        _, kwargs = db_stub.telemetry.record_nx_answer_run_complete.call_args
        assert not kwargs["plan_id"], "the ad-hoc plan_id (0) must ride the composite payload as-is"
        db_stub.plans.increment_run_started.assert_not_called()
        db_stub.plans.increment_run_outcome.assert_not_called()

    @pytest.mark.asyncio
    async def test_planner_failure_arm_upstream_of_run_start_still_records(self) -> None:
        """The falsifier for the entry-initialisation rule in D5: force
        the inline planner to raise so the call terminates on the arm
        upstream of the run-start site, and assert the planner-failure
        run row is still written. Falsifier: bind the two booleans at
        the run-start site instead of at entry and the row stops
        appearing, silently, because that arm's
        ``except Exception: pass`` swallows the resulting
        ``UnboundLocalError``. Nothing else in the suite would notice."""
        from nexus.mcp.core import nx_answer

        db_stub = MagicMock()
        recorded: list = []
        db_stub.telemetry.record_nx_answer_run.side_effect = (
            lambda **kw: recorded.append(kw)
        )

        async def failing_miss(question, scope="", max_steps=6, few_shot_matches=None):
            raise RuntimeError("planner blew up")

        with patch("nexus.plans.matcher.plan_match", return_value=[]), \
             patch("nexus.mcp.core._nx_answer_plan_miss", AsyncMock(side_effect=failing_miss)), \
             patch("nexus.mcp.core._t2_ctx") as t2_ctx, \
             patch("nexus.mcp.core._t2_index_write", lambda fn, **_kw: fn(db_stub)), \
             patch("nexus.mcp.core.scratch", return_value="ok"), \
             patch("nexus.mcp_infra.get_t1_plan_cache", return_value=None):
            t2_ctx.return_value.__enter__.return_value = db_stub
            result = await nx_answer(question="will fail before run-start")

        assert len(recorded) == 1, (
            f"expected exactly one planner-failure run row, got {len(recorded)} "
            f"-- an UnboundLocalError on composite_supported_at_start/"
            f"early_bump_fired would silently drop this row instead"
        )
        assert recorded[0]["plan_id"] is None
        assert "Planner error" in recorded[0]["final_text"]
        assert "planner blew up" in recorded[0]["final_text"]
        assert "No matching plan found" in result


class TestFourOhFourDowngradeGuard:
    """D5's downgrade guard: a 404 from ``/complete`` is a probe
    correction, not a transport failure."""

    @pytest.mark.asyncio
    async def test_404_downgrade_issues_deferred_run_start_then_record_then_outcome(
        self,
    ) -> None:
        """The tripping call issues exactly three calls, in order:
        run_start, record, outcome, after the 404 from /complete.
        Falsifier: drop the deferred run_start from the fallback and the
        first assertion sees two calls instead of three."""
        db_stub, order = _make_ordered_db_stub()
        db_stub.telemetry._supports_nx_answer_run_complete.return_value = True
        request = httpx.Request("POST", "http://fake/v1/telemetry/nx_answer_runs/complete")
        response = httpx.Response(404, request=request)
        db_stub.telemetry.record_nx_answer_run_complete.side_effect = (
            httpx.HTTPStatusError("not found", request=request, response=response)
        )

        result = await _drive_success_path(db_stub)

        assert "final answer" in result.lower()
        assert order == ["run_start", "record", "outcome"], (
            f"expected the deferred (run_start, record, outcome) fallback "
            f"in that order after the 404; got {order}"
        )
        db_stub.telemetry._downgrade_nx_answer_run_complete_support.assert_called_once()

    @pytest.mark.asyncio
    async def test_non_404_failure_drops_the_whole_composite_write(self) -> None:
        """A 429/5xx is a transport failure, not a probe correction: the
        whole composite write is dropped, exactly as a single
        _nx_answer_record_run failure is dropped today -- never silently
        converted into the three-call fallback."""
        db_stub, order = _make_ordered_db_stub()
        db_stub.telemetry._supports_nx_answer_run_complete.return_value = True
        request = httpx.Request("POST", "http://fake/v1/telemetry/nx_answer_runs/complete")
        response = httpx.Response(429, request=request)
        db_stub.telemetry.record_nx_answer_run_complete.side_effect = (
            httpx.HTTPStatusError("too many requests", request=request, response=response)
        )

        result = await _drive_success_path(db_stub)

        assert "final answer" in result.lower()
        assert order == [], "a non-404 failure must drop the write, not fall back"
        db_stub.telemetry._downgrade_nx_answer_run_complete_support.assert_not_called()


class TestCompositePayloadCreatedAt:
    """D1's required field, from the client's side (unit-level, direct
    choke-point call -- the store-level wire test is
    tests/db/test_http_telemetry_store.py::TestRecordNxAnswerRunComplete)."""

    def test_composite_payload_always_carries_created_at(self) -> None:
        """The client half of D1's required-field rule: created_at is on
        the wire at all, stamped at the choke point before the first
        attempt. Falsifier: drop the stamp (pass ``created_at=None`` or
        omit the kwarg) and this reds."""
        db = MagicMock()
        _nx_answer_record_complete(
            db, question="q", plan_id=5, matched_confidence=0.5, step_count=1,
            final_text="a", step_records=[], duration_ms=10, trace=True,
            success=True, composite_supported_at_start=True, early_bump_fired=False,
        )
        _, kwargs = db.telemetry.record_nx_answer_run_complete.call_args
        assert kwargs["created_at"], "created_at must be present and non-empty"
        from datetime import datetime as _dt

        _dt.fromisoformat(kwargs["created_at"])  # must parse as ISO-8601


class TestHandoffArmUseCountInvariant:
    """D4's invariant (use_count == success_count + failure_count) for the
    RDR-200 continuation handoff, the one D6 survivor with a real outcome
    to reconcile against."""

    @pytest.mark.asyncio
    async def test_non_supporting_engine_handoff_arm_bumps_use_count_exactly_once(
        self,
    ) -> None:
        """Against a stub whose /version reports no support, drive a call
        that terminates on the handoff arm and assert exactly one call to
        increment_run_started: the early site fires it, and the
        survivor's helper no-ops on early_bump_fired. Falsifier: make the
        helper's no-op condition plan_id-only again and the assertion
        sees two."""
        from nexus.mcp import core as mcp_core
        from nexus.mcp.core import nx_answer
        from tests.test_nx_answer import _make_multi_step_match

        db_stub = MagicMock()
        db_stub.telemetry._supports_nx_answer_run_complete.return_value = False
        db_stub.plans.save_plan = MagicMock(return_value=1)
        db_stub.plans.get_plan = MagicMock(return_value={"id": 1})

        async def stub_search(**kwargs):
            return {
                "ids": ["a"], "tumblers": ["1.1"], "distances": [0.1],
                "collections": ["knowledge"], "chunk_text_hash": ["h1"],
                "chunk_collections": ["knowledge"],
            }

        async def stub_extract(**kwargs):
            return {"extractions": []}

        with patch("nexus.plans.matcher.plan_match",
                    return_value=[_make_multi_step_match()]), \
             patch("nexus.mcp.core._t2_ctx") as t2_ctx, \
             patch("nexus.mcp.core._t2_index_write", lambda fn, **_kw: fn(db_stub)), \
             patch("nexus.mcp.core.scratch", return_value="ok"), \
             patch("nexus.mcp_infra.get_t1_plan_cache", return_value=None), \
             patch("nexus.plans.continuation_envelope._CONTINUATION_GO_LIVE", True), \
             patch.object(mcp_core, "search", stub_search), \
             patch.object(mcp_core, "operator_extract", stub_extract):
            t2_ctx.return_value.__enter__.return_value = db_stub
            await nx_answer(question="q", continuation=True)

        db_stub.plans.increment_run_started.assert_called_once_with(1)

    @pytest.mark.asyncio
    async def test_handoff_arm_against_supporting_engine_keeps_use_count_equal_to_outcomes(
        self,
    ) -> None:
        """Against the self-provisioned engine substrate (autouse
        _pin_t2_substrate) with /complete supported, drive an nx_answer
        call that terminates on the handoff arm, then read the plan row
        back and assert use_count == success_count + failure_count.
        Falsifier: remove the survivor's _nx_answer_ensure_run_started
        and the read comes back with use_count one short. Never against
        the operator's live install -- this is the self-provisioned test
        tenant every test in this suite already runs against."""
        from nexus.mcp import core as mcp_core
        from nexus.mcp.core import nx_answer
        from nexus.mcp_infra import t2_ctx
        from nexus.plans.match import Match

        plan_json = json.dumps({
            "steps": [
                {"tool": "search", "args": {"query": "$intent", "corpus": "knowledge"}},
                {"tool": "extract", "args": {"inputs": "$step1.ids", "fields": "title,summary"}},
            ],
        })
        with t2_ctx() as db:
            plan_id = db.plans.save_plan(
                "rdr-203 handoff invariant probe", plan_json, verb="research",
            )

        match = Match(
            plan_id=plan_id, name="handoff-invariant-probe", description="test",
            confidence=0.9, dimensions={}, tags="", plan_json=plan_json,
            required_bindings=["intent"], optional_bindings=[],
            default_bindings={"intent": "rdr-203 handoff invariant probe"},
            parent_dims=None,
        )

        async def stub_search(**kwargs):
            return {
                "ids": ["a"], "tumblers": ["1.1"], "distances": [0.1],
                "collections": ["knowledge"], "chunk_text_hash": ["h1"],
                "chunk_collections": ["knowledge"],
            }

        async def stub_extract(**kwargs):
            return {"extractions": []}

        with patch("nexus.plans.matcher.plan_match", return_value=[match]), \
             patch("nexus.mcp.core.scratch", MagicMock()), \
             patch("nexus.plans.continuation_envelope._CONTINUATION_GO_LIVE", True), \
             patch.object(mcp_core, "search", stub_search), \
             patch.object(mcp_core, "operator_extract", stub_extract):
            result = await nx_answer(
                question="rdr-203 handoff invariant probe", continuation=True,
            )

        assert "nx_answer_report" in result, (
            f"expected the call to terminate on the RDR-200 handoff arm "
            f"(a rendered continuation instruction naming nx_answer_report); "
            f"got: {result!r}"
        )

        with t2_ctx() as db:
            row = db.plans.get_plan(plan_id)

        assert row is not None, f"plan {plan_id} must still exist"
        assert row["use_count"] == row["success_count"] + row["failure_count"], (
            f"D4 invariant violated: use_count={row['use_count']} but "
            f"success_count={row['success_count']} + "
            f"failure_count={row['failure_count']} = "
            f"{row['success_count'] + row['failure_count']}"
        )
        assert row["use_count"] >= 1, "the handoff arm must have bumped use_count at all"
