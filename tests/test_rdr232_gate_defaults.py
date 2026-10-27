# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-232 Phase 2 step 4 (nexus-jypmj.2.8): the answer-kind gate and the structured request extraction are on by
default, each with an environment kill switch, and the gate runs in shadow against a library that was not reseeded.

Tenant-free. The defaults are pinned on the pure functions; the library precondition is pinned on the plan cache's
population, on the helper ``nx_answer`` calls, and end to end through ``nx_answer`` over a fake library (the harness of
``tests/test_rdr232_eligibility.py``). Every pin was shown failing with its code removed (the hand-back lists how).
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from structlog.testing import capture_logs

from nexus.plans import eligibility as el
from nexus.plans import request_coverage as rc
from tests.test_rdr232_eligibility import (
    G06,
    LEGACY_BUILTIN,
    LOOKUP_473,
    SYNTH_12,
    _drive,
)

OFF_VALUES = ["0", "false", "off", "no", "OFF", " False ", "No"]
ON_VALUES = ["", "   ", "1", "true", "yes", "on", "ON", "anything-else"]


# ── the gate: on by default, a kill switch ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("value", ON_VALUES)
def test_the_gate_enforces_when_the_setting_is_unset_blank_truthy_or_unrecognised(value: str) -> None:
    assert el.eligibility_enforced({el.ANSWER_KIND_ELIGIBILITY_ENV: value}) is True


def test_the_gate_enforces_with_no_setting_at_all() -> None:
    assert el.eligibility_enforced({}) is True
    assert el.ANSWER_KIND_ELIGIBILITY_ENV == "NX_ANSWER_KIND_ELIGIBILITY"


@pytest.mark.parametrize("value", OFF_VALUES)
def test_each_off_value_is_the_gates_kill_switch(value: str) -> None:
    assert el.eligibility_enforced({el.ANSWER_KIND_ELIGIBILITY_ENV: value}) is False


def test_the_default_has_one_source_the_module_constant(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset follows the constant (a test flips it); a set value is read at call time and beats the constant."""
    monkeypatch.setattr(el, "ANSWER_KIND_ELIGIBILITY_ENABLED", False)
    assert el.eligibility_enforced({}) is False
    assert el.eligibility_enforced({el.ANSWER_KIND_ELIGIBILITY_ENV: "1"}) is True
    monkeypatch.setattr(el, "ANSWER_KIND_ELIGIBILITY_ENABLED", True)
    assert el.eligibility_enforced({}) is True
    assert el.eligibility_enforced({el.ANSWER_KIND_ELIGIBILITY_ENV: "0"}) is False


def test_nx_answer_enforces_by_default_and_the_kill_switch_restores_shadow(monkeypatch: pytest.MonkeyPatch) -> None:
    env, _, seen = _drive(G06, [LOOKUP_473, SYNTH_12], [(473, 0.1), (12, 0.4)])
    wire = env["plan_eligibility"]
    assert wire["enforced"] is True and wire["shadow_reason"] is None and seen[0]["eligibility"].enforce is True
    assert env["plan_id"] == 12, "plan 473 (lookup-shaped) is refused for a synthesis request"

    monkeypatch.setenv(el.ANSWER_KIND_ELIGIBILITY_ENV, "0")
    env, _, seen = _drive(G06, [LOOKUP_473, SYNTH_12], [(473, 0.1), (12, 0.4)])
    wire = env["plan_eligibility"]
    assert wire["enforced"] is False and wire["shadow_reason"] == "disabled_by_setting"
    assert env["plan_id"] == 473, "shadow: today's behaviour, 473 still selected"
    assert wire["branches"]["cosine"]["excluded"] == 1, "and the report still says what the gate would refuse"


# ── the library precondition ─────────────────────────────────────────────────────────────────────────────────


def test_count_undeclared_builtins_counts_only_builtins_without_a_valid_declaration() -> None:
    rows = [
        LOOKUP_473,                                              # builtin, declared
        LEGACY_BUILTIN,                                          # builtin, declares nothing
        dict(LEGACY_BUILTIN, id=15, plan_json="{not json"),      # builtin, unreadable
        dict(LEGACY_BUILTIN, id=16, tags="ad-hoc,grown"),        # not a builtin: not this check's business
        dict(LEGACY_BUILTIN, id=17, tags="user-note"),
        "not a row",
    ]
    assert el.count_undeclared_builtins(rows) == 2
    assert el.count_undeclared_builtins([]) == 0


def test_a_cache_population_counts_the_undeclared_builtins_from_the_rows_it_already_loads() -> None:
    from nexus.plans.session_cache import PlanSessionCache
    from tests.conftest import make_vector_test_client

    class Lib:
        calls = 0

        def list_active_plans(self, project=""):  # noqa: ARG002
            Lib.calls += 1
            return [LOOKUP_473, SYNTH_12, LEGACY_BUILTIN, dict(LEGACY_BUILTIN, id=15, name="legacy-two")]

    cache = PlanSessionCache(client=make_vector_test_client(), session_id="gate-defaults")
    assert cache.undeclared_builtins is None, "unknown until a population has run"
    cache.populate(Lib())
    assert cache.undeclared_builtins == 2 and Lib.calls == 1


def test_the_precondition_reads_the_cache_count_and_makes_no_t2_call_when_it_has_one() -> None:
    import nexus.mcp.core as core

    cache = SimpleNamespace(undeclared_builtins=3)
    with patch("nexus.mcp.core._t2_index_write", side_effect=AssertionError("no T2 round trip when the rows are in hand")):
        assert core._nx_answer_undeclared_builtins(cache) == 3
        filt = el.EligibilityFilter("synthesis", "heuristic", enforce=True)
        core._nx_answer_library_precondition(filt, cache)
    assert filt.enforce is False and filt.shadow_reason == "library_not_reseeded" and filt.undeclared_builtins == 3


def test_a_cache_that_counted_zero_leaves_the_gate_enforcing() -> None:
    import nexus.mcp.core as core

    filt = el.EligibilityFilter("synthesis", "heuristic", enforce=True)
    with patch("nexus.mcp.core._t2_index_write", side_effect=AssertionError("no T2 round trip")):
        core._nx_answer_library_precondition(filt, SimpleNamespace(undeclared_builtins=0))
    assert filt.enforce is True and filt.shadow_reason is None and filt.undeclared_builtins is None


def test_with_no_cache_the_library_is_listed_once_and_an_unreadable_library_keeps_enforcing() -> None:
    import nexus.mcp.core as core

    plans = MagicMock()
    plans.list_active_plans.return_value = [SYNTH_12, LEGACY_BUILTIN]
    with patch("nexus.mcp.core._t2_index_write", lambda fn, **_kw: fn(SimpleNamespace(plans=plans))):
        assert core._nx_answer_undeclared_builtins(None) == 1
        plans.list_active_plans.assert_called_once()
        plans.list_active_plans.side_effect = RuntimeError("t2 down")
        filt = el.EligibilityFilter("synthesis", "heuristic", enforce=True)
        core._nx_answer_library_precondition(filt, None)
    assert filt.enforce is True, "unknown is not 'not reseeded': the cost of an unreadable library is recall only"


def test_a_shadow_filter_is_not_checked_so_the_setting_off_costs_no_lookup() -> None:
    import nexus.mcp.core as core

    filt = el.EligibilityFilter("synthesis", "heuristic", enforce=False, shadow_reason="disabled_by_setting")
    with patch("nexus.mcp.core._t2_index_write", side_effect=AssertionError("must not be asked")):
        core._nx_answer_library_precondition(filt, None)
    assert filt.shadow_reason == "disabled_by_setting"


def _library_db(rows):
    db = MagicMock(plans=MagicMock())
    db.plans.list_active_plans.return_value = rows
    return db


def test_an_unreseeded_library_runs_the_request_in_shadow_with_a_named_reason_and_the_count() -> None:
    el._warned_counts.clear()
    rows = [LOOKUP_473, SYNTH_12, LEGACY_BUILTIN]
    with capture_logs() as logs:
        env, miss, seen = _drive(G06, rows, [(473, 0.1), (12, 0.4)], db=_library_db(rows))
    wire = env["plan_eligibility"]
    assert wire["enforced"] is False
    assert wire["shadow_reason"] == "library_not_reseeded" and wire["undeclared_builtins"] == 1
    assert seen[0]["eligibility"].enforce is False
    assert env["plan_id"] == 473, "shadow: nothing was removed, the lookup plan is still selected"
    assert wire["branches"]["cosine"]["excluded"] == 1, "and the verdicts are still reported"
    warnings = [e for e in logs if e["event"] == "answer_kind_gate_shadowed_library_not_reseeded"]
    assert len(warnings) == 1 and warnings[0]["log_level"] == "warning"
    assert warnings[0]["remedy"] == "nx plan reseed" and warnings[0]["undeclared_builtins"] == 1


def test_the_not_reseeded_warning_is_logged_once_per_count_not_once_per_request() -> None:
    el._warned_counts.clear()
    rows = [LOOKUP_473, SYNTH_12, LEGACY_BUILTIN]
    with capture_logs() as logs:
        for _ in range(3):
            _drive(G06, rows, [(473, 0.1), (12, 0.4)], db=_library_db(rows))
    assert len([e for e in logs if e["event"] == "answer_kind_gate_shadowed_library_not_reseeded"]) == 1


def test_a_reseeded_library_enforces() -> None:
    rows = [LOOKUP_473, SYNTH_12]
    env, _, seen = _drive(G06, rows, [(473, 0.1), (12, 0.4)], db=_library_db(rows))
    wire = env["plan_eligibility"]
    assert wire["enforced"] is True and wire["shadow_reason"] is None and wire["undeclared_builtins"] is None
    assert env["plan_id"] == 12 and seen[0]["eligibility"].enforce is True


def test_the_planner_is_told_the_degraded_decision_not_the_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    """The few-shot filter follows the request's final ``enforce``, so the matcher and the planner agree."""
    el._warned_counts.clear()
    rows = [LEGACY_BUILTIN]
    _, miss, _ = _drive(G06, rows, [], db=_library_db(rows))
    assert miss.call_args.kwargs["enforce_eligibility"] is False
    ok_rows = [SYNTH_12]
    _, miss, _ = _drive(G06, ok_rows, [], db=_library_db(ok_rows))
    assert miss.call_args.kwargs["enforce_eligibility"] is True


def test_a_reconciled_library_no_longer_trips_the_precondition() -> None:
    """The upgrade ladder's plan-library axis calls ``seed_plan_templates(reconcile=True)``, which rewrites every
    builtin with its declaration; after it the count is zero, so an upgraded install enforces."""
    from nexus.plans.seed_loader import load_seed_directory
    from tests.test_plan_answer_kind import _BUILTIN_DIR, _pre_declaration_library

    lib = _pre_declaration_library()
    assert el.count_undeclared_builtins(lib.rows.values()) == 12, "a pre-declaration install: every builtin undeclared"
    load_seed_directory(_BUILTIN_DIR, library=lib, reconcile=True)
    assert el.count_undeclared_builtins(lib.rows.values()) == 0


def test_the_upgrade_ladders_plan_library_axis_reconciles_not_just_inserts() -> None:
    from nexus.upgrade_ladder import preconditions

    seen: dict = {}

    def fake_seed(*, reconcile: bool = False):
        seen["reconcile"] = reconcile
        return SimpleNamespace(inserted=0, updated=12)

    with patch("nexus.commands.catalog.seed_plan_templates", fake_seed):
        report = preconditions._plan_library_report()
    assert seen == {"reconcile": True} and report.current is True


# ── the structured extraction: on by default, a kill switch, haiku ──────────────────────────────────────────


@pytest.mark.parametrize("value", ON_VALUES)
def test_extraction_is_on_when_unset_blank_truthy_or_unrecognised(value: str) -> None:
    assert rc.extraction_enabled({rc.EXTRACT_ENV: value}) is True


def test_extraction_is_on_with_no_setting_at_all() -> None:
    assert rc.extraction_enabled({}) is True


@pytest.mark.parametrize("value", OFF_VALUES)
def test_each_off_value_is_the_extraction_kill_switch(value: str) -> None:
    assert rc.extraction_enabled({rc.EXTRACT_ENV: value}) is False


@pytest.mark.parametrize("value", [None, "", "   "])
def test_the_extraction_model_defaults_to_haiku(value: str | None) -> None:
    environ = {} if value is None else {rc.EXTRACT_MODEL_ENV: value}
    assert rc.extraction_model(environ) == "haiku" == rc.DEFAULT_EXTRACT_MODEL


def test_a_set_extraction_model_wins_over_the_default() -> None:
    assert rc.extraction_model({rc.EXTRACT_MODEL_ENV: " sonnet "}) == "sonnet"
    assert rc.extraction_model({rc.EXTRACT_MODEL_ENV: "claude-opus-4-1"}) == "claude-opus-4-1"


class _FakeDispatch:
    def __init__(self) -> None:
        self.extra: list[dict] = []

    async def __call__(self, prompt, schema, *, timeout=300.0, usage_sink=None, **kw):  # noqa: ARG002
        self.extra.append(kw)
        return {"answer_kind": "synthesis", "facets": ["plan matching"], "constraints": []}


def _extraction_run(monkeypatch: pytest.MonkeyPatch, **env: str | None) -> tuple[dict, _FakeDispatch]:
    """Drive nx_answer on a question the deterministic pass leaves unknown, with a fake dispatch (never live)."""
    for name in (rc.EXTRACT_ENV, rc.EXTRACT_MODEL_ENV, "NX_OPERATOR_MODEL_TIERING"):
        monkeypatch.delenv(name, raising=False)  # the suite-wide autouse fixture turns extraction off; clear it
    for name, value in env.items():
        if value is not None:
            monkeypatch.setenv(name, value)
    fake = _FakeDispatch()
    with patch("nexus.operators.dispatch.claude_dispatch", fake):
        out, _, _ = _drive("Tell me about plan matching.", [], [])
    return out, fake


def test_nx_answer_with_no_settings_makes_one_haiku_extraction_call(monkeypatch: pytest.MonkeyPatch) -> None:
    out, fake = _extraction_run(monkeypatch)
    assert len(fake.extra) == 1
    assert fake.extra[0]["model"] == "haiku" and fake.extra[0]["operator"] == "request_coverage_extract"
    assert out["request_coverage"]["extraction"]["outcome"] == "ok"


def test_the_extraction_kill_switch_makes_no_call(monkeypatch: pytest.MonkeyPatch) -> None:
    out, fake = _extraction_run(monkeypatch, **{rc.EXTRACT_ENV: "0"})
    assert fake.extra == []
    assert out["request_coverage"]["extraction"]["outcome"] == "disabled"


def test_a_set_extraction_model_reaches_the_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    _, fake = _extraction_run(monkeypatch, **{rc.EXTRACT_MODEL_ENV: "sonnet"})
    assert fake.extra[0]["model"] == "sonnet"


def test_operator_model_tiering_zero_does_not_strip_the_default_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """Documented rule: the extraction model is always passed explicitly (the setting, else haiku), so
    ``NX_OPERATOR_MODEL_TIERING=0`` (which only controls dispatches that name no model) leaves it alone."""
    _, fake = _extraction_run(monkeypatch, NX_OPERATOR_MODEL_TIERING="0")
    assert fake.extra[0]["model"] == "haiku"


def test_the_suite_wide_default_keeps_unit_tests_off_the_live_model() -> None:
    """The autouse fixture in conftest: a test that does not mean to call a model never does."""
    import os

    assert os.environ.get(rc.EXTRACT_ENV) == "0"
    assert os.environ.get(el.ANSWER_KIND_ELIGIBILITY_ENV) is None
