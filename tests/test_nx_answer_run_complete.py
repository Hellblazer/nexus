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
import pathlib
from unittest.mock import MagicMock, patch

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
