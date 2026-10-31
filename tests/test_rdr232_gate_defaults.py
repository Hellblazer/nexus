# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-232 Phase 2 step 4 (nexus-jypmj.2.8): the answer-kind gate and the structured request extraction are on by
default, each with an environment kill switch, and the gate runs in shadow while the extraction is off or against a
library that was not reseeded.

Tenant-free. The defaults are pinned on the pure functions; the library precondition is pinned on the plan cache's
population, on the helpers ``nx_answer`` calls, and end to end through ``nx_answer`` over a fake library (the harness of
``tests/test_rdr232_eligibility.py``). Every pin was shown failing with its code removed (the hand-back lists how).
A spy that guards "no lookup happens" RECORDS its calls and the test asserts the list is empty: a spy that raises is
swallowed by ``_nx_answer_isolated`` and proves nothing.
"""
from __future__ import annotations

import asyncio
import math
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from structlog.testing import capture_logs

from nexus.plans import eligibility as el
from nexus.plans import request_coverage as rc
from nexus.plans import switches
from nexus.plans.schema import canonical_dimensions_json
from tests.test_plan_answer_kind import _BUILTIN_DIR, _TABLE, _template
from tests.test_rdr232_eligibility import (
    G06,
    LEGACY_BUILTIN,
    LOOKUP_473,
    SYNTH_12,
    _drive,
    _extraction_that_works,
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

SHIPPED = sorted(_TABLE)  # the shipped builtin template files


def _shipped_dims(template_file: str) -> str:
    """The canonical dimensions the seed loader stores a shipped template under (global tier)."""
    dims = dict(_template(template_file)["dimensions"])
    dims["scope"] = "global"
    return canonical_dimensions_json(dims)


def _builtin_row(pid: int, template_file: str, *, declared: bool) -> dict:
    """A builtin row as a reseed can fix it: global tier, a shipped template's dimensions."""
    base = LOOKUP_473 if declared else LEGACY_BUILTIN
    return dict(base, id=pid, name=Path(template_file).stem, project="", dimensions=_shipped_dims(template_file))


def _library_rows(*, undeclared: int, declared: int = 2) -> list[dict]:
    """The rows a library read returns: *declared* reseeded builtins, then *undeclared* unreseeded ones."""
    files = SHIPPED[: declared + undeclared]
    return [_builtin_row(100 + i, f, declared=i < declared) for i, f in enumerate(files)]


def _library_db(rows):
    db = MagicMock(plans=MagicMock())
    db.plans.list_active_plans.return_value = rows
    return db


def _spy(rows):
    """A ``_t2_index_write`` stand-in that RECORDS each call (an exception would be swallowed) and returns *rows*."""
    calls: list[str | None] = []

    def spy(fn, **kw):  # noqa: ARG001
        calls.append(kw.get("op"))
        return rows

    return calls, spy


def test_the_shipped_set_the_check_reads_is_every_builtin_file_plus_the_retired_ones() -> None:
    """Non-vacuity: the dimensions a reseed can fix are the whole shipped directory, not a partial read."""
    from nexus.plans.seed_loader import RETIRED_TEMPLATE_DIMENSIONS

    dims = el.reseedable_dimensions()
    assert dims is not None
    shipped = {_shipped_dims(f) for f in SHIPPED}
    assert len(SHIPPED) == len(list(_BUILTIN_DIR.glob("*.yml"))) >= 12 and len(shipped) == len(SHIPPED)
    assert dims == shipped | RETIRED_TEMPLATE_DIMENSIONS


def test_the_protected_tags_are_the_seed_loaders_grown_tags() -> None:
    from nexus.plans.seed_loader import _GROWN_TAGS

    assert el.PROTECTED_TAGS == _GROWN_TAGS


def test_check_builtins_examines_only_the_builtin_rows_a_reseed_can_fix() -> None:
    from nexus.plans.seed_loader import RETIRED_TEMPLATE_DIMENSIONS

    retired = sorted(RETIRED_TEMPLATE_DIMENSIONS)[0]
    rows = [
        _builtin_row(1, SHIPPED[0], declared=True),                                  # examined, declared
        _builtin_row(2, SHIPPED[1], declared=False),                                 # examined, undeclared
        dict(_builtin_row(3, SHIPPED[2], declared=False), plan_json="{not json"),    # examined, unreadable = undeclared
        dict(LEGACY_BUILTIN, id=4, name="orphan", project="", dimensions='{"scope":"global","verb":"orphan"}'),
        dict(_builtin_row(5, SHIPPED[3], declared=False), tags="builtin-template,grown"),   # protected from a reseed
        dict(_builtin_row(6, SHIPPED[4], declared=False), project="nexus"),          # not a global-tier row
        dict(_builtin_row(7, SHIPPED[5], declared=False), tags="user-note"),         # not a builtin
        dict(LEGACY_BUILTIN, id=8, name="retired-one", project="", dimensions=retired),  # a reseed deletes it
        "not a row",
    ]
    check = el.check_builtins(rows)
    assert check == el.BuiltinCheck(checked=4, undeclared=3, undeclared_names=(
        f"{Path(SHIPPED[1]).stem} (id 2)", f"{Path(SHIPPED[2]).stem} (id 3)", "retired-one (id 8)"))


def test_check_builtins_names_at_most_eight_rows_and_falls_back_to_the_id() -> None:
    rows = [dict(r, name=None) for r in _library_rows(undeclared=len(SHIPPED), declared=0)]
    check = el.check_builtins(rows)
    assert check.undeclared == check.checked == len(SHIPPED) > 8
    assert len(check.undeclared_names) == 8 and check.undeclared_names[0] == "id 100"


def test_rows_that_share_a_name_are_listed_apart_by_id() -> None:
    """Live: four builtin rows are all named ``default`` (one per verb), so the name alone repeats."""
    rows = [dict(r, name="default") for r in _library_rows(undeclared=4, declared=0)]
    check = el.check_builtins(rows)
    assert check.undeclared == 4
    assert check.undeclared_names == ("default (id 100)", "default (id 101)", "default (id 102)", "default (id 103)")
    assert len(set(check.undeclared_names)) == 4, "each listed row is told apart from the others"


@pytest.mark.parametrize("dimensions", [{"scope": "global", "verb": "x"}, ["scope"], 7, None])
def test_check_builtins_does_not_raise_on_dimensions_that_are_not_a_string(dimensions) -> None:
    """The docstring promises a row that cannot be read is not examined; an unhashable value used to raise TypeError."""
    unreadable = dict(_builtin_row(1, SHIPPED[0], declared=False), dimensions=dimensions)
    readable = _builtin_row(2, SHIPPED[1], declared=False)
    assert el.check_builtins([unreadable, readable]) == el.BuiltinCheck(
        checked=1, undeclared=1, undeclared_names=(f"{Path(SHIPPED[1]).stem} (id 2)",),
    )


def test_a_library_with_no_builtin_to_examine_checks_zero_and_is_not_undeclared() -> None:
    assert el.check_builtins([]) == el.BuiltinCheck(0, 0, ())
    assert el.check_builtins([SYNTH_12, LOOKUP_473]) == el.BuiltinCheck(0, 0, ()), "project-tier rows are not reseeded"


def test_check_builtins_is_unknown_when_the_shipped_templates_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(el, "_reseedable", None)
    monkeypatch.setattr("nexus.commands.catalog._resolve_plugin_root", lambda _root: tmp_path / "nowhere")
    with capture_logs() as logs:
        assert el.check_builtins(_library_rows(undeclared=2)) is None
    assert any(e["event"] == "answer_kind_gate_shipped_templates_unreadable" for e in logs)
    (tmp_path / "plans" / "builtin").mkdir(parents=True)
    monkeypatch.setattr("nexus.commands.catalog._resolve_plugin_root", lambda _root: tmp_path)
    assert el.reseedable_dimensions() is None, "an empty directory is no evidence either"


def test_a_cache_population_examines_the_builtins_from_the_rows_it_already_loads() -> None:
    from nexus.plans.session_cache import PlanSessionCache
    from tests.conftest import make_vector_test_client

    rows = _library_rows(undeclared=2, declared=1)

    class Lib:
        calls = 0

        def list_active_plans(self, project=""):  # noqa: ARG002
            Lib.calls += 1
            return rows

    cache = PlanSessionCache(client=make_vector_test_client(), session_id="gate-defaults")
    assert cache.library_check is None, "unknown until a population has run"
    cache.populate(Lib())
    assert cache.library_check == el.BuiltinCheck(
        3, 2, (f"{Path(SHIPPED[1]).stem} (id 101)", f"{Path(SHIPPED[2]).stem} (id 102)")) and Lib.calls == 1


def test_the_precondition_reads_the_cache_check_and_makes_no_t2_call_when_it_has_one() -> None:
    import nexus.mcp.core as core

    check = el.BuiltinCheck(5, 3, ("a", "b", "c"))
    cache = SimpleNamespace(library_check=check)
    calls, spy = _spy([])
    with patch("nexus.mcp.core._t2_index_write", spy):
        assert core._nx_answer_library_check(cache) is check
        filt = el.EligibilityFilter("synthesis", "heuristic", enforce=True)
        core._nx_answer_library_precondition(filt, cache)
    assert calls == [], "no T2 round trip when the rows are in hand"
    assert filt.enforce is False and filt.shadow_reason == "library_not_reseeded"
    assert (filt.library_check, filt.builtins_checked, filt.undeclared_builtins) == ("checked", 5, 3)
    assert filt.undeclared_builtin_names == ["a", "b", "c"]


def test_a_cache_that_counted_zero_leaves_the_gate_enforcing() -> None:
    """A count of zero is an answer (checked, nothing undeclared), not an unknown to look up again."""
    import nexus.mcp.core as core

    check = el.BuiltinCheck(4, 0, ())
    cache = SimpleNamespace(library_check=check)
    calls, spy = _spy([])
    with patch("nexus.mcp.core._t2_index_write", spy):
        assert core._nx_answer_library_check(cache) is check and check.undeclared == 0
        filt = el.EligibilityFilter("synthesis", "heuristic", enforce=True)
        core._nx_answer_library_precondition(filt, cache)
    assert calls == [], "a zero count must not send the request to list the library"
    assert filt.enforce is True and filt.shadow_reason is None
    assert (filt.library_check, filt.builtins_checked, filt.undeclared_builtins) == ("checked", 4, 0)


def test_with_no_cache_the_library_is_listed_once_per_bound_not_once_per_request() -> None:
    import nexus.mcp.core as core

    plans = MagicMock()
    plans.list_active_plans.return_value = _library_rows(undeclared=1)
    with patch("nexus.mcp.core._t2_index_write", lambda fn, **_kw: fn(SimpleNamespace(plans=plans))):
        for _ in range(3):
            filt = el.EligibilityFilter("synthesis", "heuristic", enforce=True)
            core._nx_answer_library_precondition(filt, None)
            assert filt.shadow_reason == "library_not_reseeded" and filt.undeclared_builtins == 1
        assert plans.list_active_plans.call_count == 1, "the fallback listing is reused inside the bound"
        el.reset_library_memo()
        core._nx_answer_library_precondition(el.EligibilityFilter("synthesis", "heuristic", enforce=True), None)
        assert plans.list_active_plans.call_count == 2, "and a reset (or the bound passing) lists again"


def test_the_fallback_listing_is_read_again_once_the_bound_has_passed(monkeypatch: pytest.MonkeyPatch) -> None:
    import nexus.mcp.core as core

    plans = MagicMock()
    plans.list_active_plans.return_value = _library_rows(undeclared=0)
    monkeypatch.setattr(el, "LIBRARY_CHECK_TTL_S", 0.0)
    with patch("nexus.mcp.core._t2_index_write", lambda fn, **_kw: fn(SimpleNamespace(plans=plans))):
        for _ in range(2):
            core._nx_answer_library_precondition(el.EligibilityFilter("synthesis", "heuristic", enforce=True), None)
    assert plans.list_active_plans.call_count == 2


def test_the_memo_expires_on_the_injected_clock() -> None:
    reads: list[int] = []
    clock = [1000.0]

    def read():
        reads.append(1)
        return el.BuiltinCheck(len(reads), 0, ())

    first = el.memoized_library_check(read, now=lambda: clock[0])
    clock[0] += el.LIBRARY_CHECK_TTL_S - 1
    assert el.memoized_library_check(read, now=lambda: clock[0]) is first and len(reads) == 1
    clock[0] += 2
    assert el.memoized_library_check(read, now=lambda: clock[0]) == el.BuiltinCheck(2, 0, ()) and len(reads) == 2


def test_an_unreadable_library_keeps_enforcing_says_so_and_is_not_listed_again_inside_the_bound() -> None:
    import nexus.mcp.core as core

    plans = MagicMock()
    plans.list_active_plans.side_effect = RuntimeError("t2 down")
    with patch("nexus.mcp.core._t2_index_write", lambda fn, **_kw: fn(SimpleNamespace(plans=plans))):
        for _ in range(3):
            filt = el.EligibilityFilter("synthesis", "heuristic", enforce=True)
            core._nx_answer_library_precondition(filt, None)
            assert filt.enforce is True, "unknown is not 'not reseeded': the cost of an unreadable library is recall only"
            assert filt.library_check == "unreadable" and filt.builtins_checked is None
    assert plans.list_active_plans.call_count == 1, "a failure is reused inside the bound too"


def test_a_shadow_filter_is_not_checked_so_the_setting_off_costs_no_lookup() -> None:
    import nexus.mcp.core as core

    calls, spy = _spy(_library_rows(undeclared=3))
    cache = SimpleNamespace(library_check=el.BuiltinCheck(5, 3, ("a", "b", "c")))
    for reason in ("disabled_by_setting", "extraction_disabled"):
        for the_cache in (None, cache):
            filt = el.EligibilityFilter("synthesis", "heuristic", enforce=False, shadow_reason=reason)
            with patch("nexus.mcp.core._t2_index_write", spy):
                core._nx_answer_library_precondition(filt, the_cache)
            assert filt.shadow_reason == reason, "the setting's own reason is not overwritten by the library's"
            assert filt.library_check is None and filt.undeclared_builtins is None
    assert calls == [], "a filter already in shadow must not read the library"


def _drive_with_library(db_rows, matcher_rows=None, hits=None, **kw):
    el._warned_libraries.clear()
    el.reset_library_memo()  # one drive is one process-moment: the previous drive's library read is not this one's
    matcher_rows = [LOOKUP_473, SYNTH_12, LEGACY_BUILTIN] if matcher_rows is None else matcher_rows
    return _drive(G06, matcher_rows, [(473, 0.1), (12, 0.4)] if hits is None else hits, db=_library_db(db_rows), **kw)


def test_an_unreseeded_library_runs_the_request_in_shadow_with_a_named_reason_and_the_count() -> None:
    with capture_logs() as logs:
        env, miss, seen = _drive_with_library(_library_rows(undeclared=1, declared=2))
    wire = env["plan_eligibility"]
    assert wire["enforced"] is False
    assert wire["shadow_reason"] == "library_not_reseeded" and wire["library_check"] == "checked"
    assert (wire["builtins_checked"], wire["undeclared_builtins"]) == (3, 1)
    assert wire["undeclared_builtin_names"] == [f"{Path(SHIPPED[2]).stem} (id 102)"]
    assert seen[0]["eligibility"].enforce is False
    assert env["plan_id"] == 473, "shadow: nothing was removed, the lookup plan is still selected"
    assert wire["branches"]["cosine"]["excluded"] == 1, "and the verdicts are still reported"
    warnings = [e for e in logs if e["event"] == "answer_kind_gate_shadowed_library_not_reseeded"]
    assert len(warnings) == 1 and warnings[0]["log_level"] == "warning"
    assert warnings[0]["remedy"] == el.LIBRARY_REMEDY and warnings[0]["undeclared_builtins"] == 1
    assert warnings[0]["builtins_checked"] == 3 and warnings[0]["undeclared_builtin_names"] == [f"{Path(SHIPPED[2]).stem} (id 102)"]


def test_the_not_reseeded_warning_is_logged_once_per_library_state_not_once_per_request() -> None:
    el._warned_libraries.clear()
    rows = _library_rows(undeclared=1)
    with capture_logs() as logs:
        for _ in range(3):
            el.reset_library_memo()
            _drive(G06, [LOOKUP_473, SYNTH_12], [(473, 0.1), (12, 0.4)], db=_library_db(rows))
    assert len([e for e in logs if e["event"] == "answer_kind_gate_shadowed_library_not_reseeded"]) == 1


def test_a_reseeded_library_enforces_and_the_envelope_says_it_was_examined() -> None:
    env, _, seen = _drive_with_library(_library_rows(undeclared=0, declared=3), matcher_rows=[LOOKUP_473, SYNTH_12])
    wire = env["plan_eligibility"]
    assert wire["enforced"] is True and wire["shadow_reason"] is None and wire["library_check"] == "checked"
    assert (wire["builtins_checked"], wire["undeclared_builtins"], wire["undeclared_builtin_names"]) == (3, 0, [])
    assert env["plan_id"] == 12 and seen[0]["eligibility"].enforce is True


def test_a_library_with_no_builtin_enforces_and_says_it_examined_none() -> None:
    """Decision: nothing to reseed means the precondition cannot fail; the envelope tells a vacuous pass from a real one."""
    env, _, _ = _drive_with_library([SYNTH_12], matcher_rows=[LOOKUP_473, SYNTH_12])
    wire = env["plan_eligibility"]
    assert wire["enforced"] is True and wire["shadow_reason"] is None
    assert (wire["library_check"], wire["builtins_checked"], wire["undeclared_builtins"]) == ("checked", 0, 0)


def test_an_unreadable_library_is_named_in_the_envelope_not_shown_as_checked_zero() -> None:
    el.reset_library_memo()
    env, _, _ = _drive(G06, [LOOKUP_473, SYNTH_12], [(473, 0.1), (12, 0.4)])  # the default db stub lists a MagicMock
    wire = env["plan_eligibility"]
    assert wire["enforced"] is True and wire["library_check"] == "unreadable"
    assert wire["builtins_checked"] is None and wire["undeclared_builtins"] is None


def test_the_planner_is_told_the_degraded_decision_not_the_setting() -> None:
    """The few-shot filter follows the request's final ``enforce``, so the matcher and the planner agree."""
    _, miss, _ = _drive_with_library(_library_rows(undeclared=1), matcher_rows=[LEGACY_BUILTIN], hits=[])
    assert miss.call_args.kwargs["enforce_eligibility"] is False
    _, miss, _ = _drive_with_library(_library_rows(undeclared=0), matcher_rows=[SYNTH_12], hits=[])
    assert miss.call_args.kwargs["enforce_eligibility"] is True


def test_a_reconciled_library_no_longer_trips_the_precondition_and_an_orphan_never_did() -> None:
    """The upgrade ladder's plan-library axis calls ``seed_plan_templates(reconcile=True)``, which rewrites every
    builtin with its declaration; after it nothing is undeclared, so an upgraded install enforces. A builtin-tagged
    orphan (dimensions no shipped template carries) is left in place by the seed loader and is not counted."""
    from nexus.plans.seed_loader import load_seed_directory
    from tests.test_plan_answer_kind import _pre_declaration_library

    shipped = len(list(_BUILTIN_DIR.glob("*.yml")))
    lib = _pre_declaration_library()
    lib.save_plan(
        query="A builtin-tagged row no shipped template describes.", plan_json='{"steps": []}', outcome="success",
        tags="builtin-template", project="", name="orphan-builtin", verb="orphan", scope="global",
        dimensions='{"scope":"global","verb":"orphan"}', default_bindings=None, parent_dims=None,
    )
    before = el.check_builtins(lib.rows.values())
    assert before.checked == before.undeclared == shipped, "a pre-declaration install: every shipped builtin undeclared"
    assert "orphan-builtin" not in before.undeclared_names
    load_seed_directory(_BUILTIN_DIR, library=lib, reconcile=True)
    orphan = next(r for r in lib.rows.values() if r["name"] == "orphan-builtin")
    assert "answer_kind" not in orphan["plan_json"], "the seed loader leaves the orphan alone: a reseed cannot fix it"
    assert el.check_builtins(lib.rows.values()) == el.BuiltinCheck(checked=shipped, undeclared=0, undeclared_names=())


def test_the_upgrade_ladders_plan_library_axis_reconciles_not_just_inserts() -> None:
    from nexus.upgrade_ladder import preconditions

    seen: dict = {}

    def fake_seed(*, reconcile: bool = False):
        seen["reconcile"] = reconcile
        return SimpleNamespace(inserted=0, updated=12)

    with patch("nexus.commands.catalog.seed_plan_templates", fake_seed):
        report = preconditions._plan_library_report()
    assert seen == {"reconcile": True} and report.current is True


# ── the gate follows the extraction switch ───────────────────────────────────────────────────────────────────


def test_extraction_off_by_setting_runs_the_gate_in_shadow_and_costs_no_library_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(rc.EXTRACT_ENV, "0")
    db = _library_db(_library_rows(undeclared=0))
    env, _, seen = _drive(G06, [LOOKUP_473, SYNTH_12], [(473, 0.1), (12, 0.4)], db=db)
    wire = env["plan_eligibility"]
    assert wire["enforced"] is False and wire["shadow_reason"] == "extraction_disabled"
    assert seen[0]["eligibility"].enforce is False and env["plan_id"] == 473, "shadow: today's selection"
    assert wire["branches"]["cosine"]["excluded"] == 1, "the verdicts are still reported"
    assert wire["library_check"] is None
    db.plans.list_active_plans.assert_not_called()


def test_extraction_on_by_default_keeps_the_gate_enforcing_and_the_reason_empty() -> None:
    env, _, _ = _drive(G06, [LOOKUP_473, SYNTH_12], [(473, 0.1), (12, 0.4)])
    wire = env["plan_eligibility"]
    assert wire["enforced"] is True and wire["shadow_reason"] is None and env["plan_id"] == 12


def test_a_gate_switched_off_by_its_own_setting_reports_that_when_extraction_is_off_too(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(rc.EXTRACT_ENV, "off")
    monkeypatch.setenv(el.ANSWER_KIND_ELIGIBILITY_ENV, "0")
    env, _, _ = _drive(G06, [LOOKUP_473, SYNTH_12], [(473, 0.1), (12, 0.4)])
    assert env["plan_eligibility"]["shadow_reason"] == "disabled_by_setting"


def test_extraction_off_outranks_an_unreseeded_library_and_logs_no_library_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(rc.EXTRACT_ENV, "no")
    with capture_logs() as logs:
        env, _, _ = _drive_with_library(_library_rows(undeclared=2))
    wire = env["plan_eligibility"]
    assert wire["shadow_reason"] == "extraction_disabled" and wire["undeclared_builtins"] is None
    assert not [e for e in logs if e["event"] == "answer_kind_gate_shadowed_library_not_reseeded"]


class _FailingDispatch:
    def __init__(self, error: BaseException) -> None:
        self.error = error
        self.calls = 0

    async def __call__(self, prompt, schema, **kw):  # noqa: ARG002
        self.calls += 1
        raise self.error


_OPEN_QUESTION = "Tell me about plan matching."
_HITS = [(473, 0.1), (12, 0.4)]


@pytest.mark.parametrize("error, outcome", [
    (RuntimeError("claude exited 1"), "error"),
    (asyncio.TimeoutError(), "timeout"),
])
def test_a_failing_extraction_call_runs_that_requests_gate_in_shadow(error, outcome, monkeypatch: pytest.MonkeyPatch) -> None:
    """nexus-x3eoc (Sam, 2026-10-31): a request whose extraction call fails keeps its first-pass kind, the regime
    the held-out measurement failed, so the gate reports what it would exclude and excludes nothing."""
    fake = _FailingDispatch(error)
    monkeypatch.setattr("nexus.operators.dispatch.claude_dispatch", fake)
    env, miss, seen = _drive(_OPEN_QUESTION, [LOOKUP_473, SYNTH_12], _HITS)
    assert fake.calls == 1 and env["request_coverage"]["extraction"]["outcome"] == outcome
    wire = env["plan_eligibility"]
    assert wire["enforced"] is False and wire["shadow_reason"] == "extraction_failed"
    assert seen[0]["eligibility"].enforce is False
    assert wire["excluded_plan_ids"] == [473] and wire["branches"]["cosine"]["excluded"] == 1, "the would-be exclusion is reported"
    assert env["plan_id"] == 473 and wire["selected"]["plan_id"] == 473, "and the plan is still selected: nothing was excluded"
    assert wire["fallback_reason"] is None and not miss.called


def test_an_extraction_answer_outside_the_schema_runs_the_gate_in_shadow(monkeypatch: pytest.MonkeyPatch) -> None:
    async def invalid(prompt, schema, **kw):  # noqa: ARG001
        return {"answer_kind": "not-a-kind"}

    monkeypatch.setattr("nexus.operators.dispatch.claude_dispatch", invalid)
    env, _, _ = _drive(_OPEN_QUESTION, [LOOKUP_473, SYNTH_12], _HITS)
    assert env["request_coverage"]["extraction"]["outcome"] == "invalid"
    assert env["plan_eligibility"]["enforced"] is False and env["plan_eligibility"]["shadow_reason"] == "extraction_failed"
    assert env["plan_id"] == 473


def test_a_failed_extraction_degrades_only_its_own_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """The next request, whose extraction answers, enforces again: no state carries over."""
    with monkeypatch.context() as failing:
        failing.setattr("nexus.operators.dispatch.claude_dispatch", _FailingDispatch(RuntimeError("down")))
        first, _, _ = _drive(_OPEN_QUESTION, [LOOKUP_473, SYNTH_12], _HITS)
    assert first["plan_eligibility"]["enforced"] is False
    monkeypatch.setattr("nexus.operators.dispatch.claude_dispatch", _extraction_that_works)
    second, _, _ = _drive(_OPEN_QUESTION, [LOOKUP_473, SYNTH_12], _HITS)
    assert second["request_coverage"]["extraction"]["outcome"] == "unchanged"
    wire = second["plan_eligibility"]
    assert wire["enforced"] is True and wire["shadow_reason"] is None
    assert wire["excluded_plan_ids"] == [473] and second["plan_id"] == 12


def test_the_gates_own_switch_outranks_a_failed_extraction(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(el.ANSWER_KIND_ELIGIBILITY_ENV, "0")
    monkeypatch.setattr("nexus.operators.dispatch.claude_dispatch", _FailingDispatch(RuntimeError("down")))
    env, _, _ = _drive(_OPEN_QUESTION, [LOOKUP_473, SYNTH_12], _HITS)
    assert env["request_coverage"]["extraction"]["outcome"] == "error"
    assert env["plan_eligibility"]["shadow_reason"] == "disabled_by_setting"


def test_a_failed_extraction_outranks_an_unreseeded_library_and_costs_no_library_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("nexus.operators.dispatch.claude_dispatch", _FailingDispatch(RuntimeError("down")))
    db = _library_db(_library_rows(undeclared=2))
    env, _, _ = _drive(_OPEN_QUESTION, [LOOKUP_473, SYNTH_12], _HITS, db=db)
    wire = env["plan_eligibility"]
    assert wire["shadow_reason"] == "extraction_failed" and wire["library_check"] is None
    db.plans.list_active_plans.assert_not_called()


# ── the shadow reasons are a closed set ──────────────────────────────────────────────────────────────────────


def test_a_shadow_reason_outside_the_closed_set_is_refused() -> None:
    assert el.SHADOW_REASONS == {
        "disabled_by_setting", "extraction_disabled", "extraction_failed", "extraction_skipped", "library_not_reseeded",
    }
    filt = el.EligibilityFilter("synthesis", "heuristic", enforce=True)
    with pytest.raises(ValueError, match="not_a_reason"):
        filt.degrade_to_shadow("not_a_reason")
    assert filt.enforce is True and filt.shadow_reason is None, "a refused reason changes nothing"
    with pytest.raises(ValueError, match="made_up"):
        el.EligibilityFilter("synthesis", enforce=False, shadow_reason="made_up")
    for reason in el.SHADOW_REASONS:
        el.EligibilityFilter("synthesis", enforce=True).degrade_to_shadow(reason)


def test_every_shadow_reason_nx_answer_emits_is_in_the_set_and_the_set_has_no_unused_member(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    emitted: set[str] = set()

    def run(**env: str) -> None:
        with monkeypatch.context() as m:
            for name, value in env.items():
                m.setenv(name, value)
            out, _, _ = _drive_with_library(_library_rows(undeclared=1))
            reason = out["plan_eligibility"]["shadow_reason"]
            if reason is not None:
                emitted.add(reason)

    run(**{el.ANSWER_KIND_ELIGIBILITY_ENV: "0"})
    run(**{rc.EXTRACT_ENV: "0"})
    run()  # the unreseeded library
    with monkeypatch.context() as m:  # one request's failed extraction (nexus-x3eoc)
        m.setattr("nexus.operators.dispatch.claude_dispatch", _FailingDispatch(RuntimeError("down")))
        out, _, _ = _drive(_OPEN_QUESTION, [LOOKUP_473, SYNTH_12], _HITS, db=_library_db(_library_rows(undeclared=1)))
        emitted.add(out["plan_eligibility"]["shadow_reason"])
    with monkeypatch.context() as m:  # and one whose extraction call was skipped for lack of time (Sam 2026-10-31)
        m.setattr("nexus.operators.dispatch.claude_dispatch", _FailingDispatch(RuntimeError("never called")))
        out, _, _ = _drive(
            _OPEN_QUESTION, [LOOKUP_473, SYNTH_12], _HITS, db=_library_db(_library_rows(undeclared=1)), budget_seconds=0.5,
        )
        emitted.add(out["plan_eligibility"]["shadow_reason"])
    assert emitted == el.SHADOW_REASONS
    # and no other reason is written anywhere a filter is built or degraded
    src = Path(__file__).resolve().parents[1] / "src" / "nexus"
    literals = set()
    for path in (src / "mcp" / "core.py", src / "plans" / "eligibility.py"):
        text = path.read_text(encoding="utf-8")
        literals |= set(re.findall(r'degrade_to_shadow\("([a-z_]+)"\)', text))
        literals |= set(re.findall(r'shadow_reason=\(?\s*"([a-z_]+)"', text))
    assert literals and literals <= el.SHADOW_REASONS, literals


# ── an unrecognised switch value ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("variable, read", [
    (el.ANSWER_KIND_ELIGIBILITY_ENV, el.eligibility_enforced),
    (rc.EXTRACT_ENV, rc.extraction_enabled),
])
def test_a_value_in_neither_vocabulary_reads_as_on_and_warns_once_per_process(variable, read) -> None:
    switches.reset_warned()
    with capture_logs() as logs:
        for value in ("disable", "Disabled", "off!"):
            assert read({variable: value}) is True, "behaviour unchanged: unrecognised means on"
    events = [e for e in logs if e["event"] == "answer_switch_value_unrecognised"]
    assert len(events) == 1 and events[0]["variable"] == variable and events[0]["log_level"] == "warning"
    assert events[0]["read_as"] == "on" and "0" in events[0]["off_values"]


def test_the_two_switches_warn_independently_and_a_valid_value_never_warns() -> None:
    switches.reset_warned()
    with capture_logs() as logs:
        for value in switches.ON_VALUES | switches.OFF_VALUES | {"", "  ", "ON", " No "}:
            el.eligibility_enforced({el.ANSWER_KIND_ELIGIBILITY_ENV: value})
            rc.extraction_enabled({rc.EXTRACT_ENV: value})
        el.eligibility_enforced({el.ANSWER_KIND_ELIGIBILITY_ENV: "disable"})
        rc.extraction_enabled({rc.EXTRACT_ENV: "disable"})
    events = [e for e in logs if e["event"] == "answer_switch_value_unrecognised"]
    assert sorted(e["variable"] for e in events) == sorted([el.ANSWER_KIND_ELIGIBILITY_ENV, rc.EXTRACT_ENV])


# ── the extraction timeout ───────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("raw, expected", [
    ("nan", 20.0), ("NaN", 20.0), ("inf", 20.0), ("-inf", 20.0), ("abc", 20.0), ("", 20.0),
    ("0.1", 1.0), ("5", 5.0), ("999", 60.0), ("-3", 1.0),
])
def test_the_extraction_timeout_is_the_default_for_anything_not_a_finite_number(raw: str, expected: float) -> None:
    got = rc.extraction_timeout({rc.EXTRACT_TIMEOUT_ENV: raw})
    assert math.isfinite(got) and got == expected


def test_the_extraction_timeout_default_when_unset() -> None:
    assert rc.extraction_timeout({}) == rc.DEFAULT_EXTRACT_TIMEOUT_S == 20.0


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


def test_the_suite_pins_coverage_off_while_the_shipped_default_is_on() -> None:
    """nexus-jypmj.17: ``NX_ANSWER_COVERAGE`` ships ON, and tests/conftest.py pins it to ``0`` for every test (unlike the
    two other switches, which it leaves at their defaults) so a unit test over its own operator stubs does not reach
    the coverage boundary unless it opts in. A test of the shipped default deletes the variable."""
    import os

    assert os.environ.get(switches.ANSWER_COVERAGE_ENV) == "0", "the suite-wide pin is not in force"
    assert switches.coverage_enabled(os.environ) is False
    assert switches.ANSWER_COVERAGE_ENABLED is True and switches.coverage_enabled({}) is True


def test_the_suite_wide_default_keeps_unit_tests_off_the_live_model() -> None:
    """The autouse fixture in conftest: the setting stays at its default (so the gate enforces in every test) and
    the dispatch refuses the extraction operator, so a test that does not mean to call a model never does."""
    import os

    from nexus.operators import dispatch

    assert os.environ.get(rc.EXTRACT_ENV) is None and os.environ.get(el.ANSWER_KIND_ELIGIBILITY_ENV) is None
    assert rc.extraction_enabled(os.environ) is True and el.eligibility_enforced(os.environ) is True
    # The assertion is on the wrapper, not on a call that would reach the live model if the guard regressed:
    # the autouse fixture wraps the real dispatcher with functools.wraps, so ``__wrapped__`` is the real one.
    guard = dispatch.claude_dispatch
    real = getattr(guard, "__wrapped__", None)
    assert real is not None and real is not guard, "claude_dispatch is not the conftest guard: the suite-wide fixture is not in force"
    assert asyncio.iscoroutinefunction(guard) and asyncio.iscoroutinefunction(real)
    assert guard.__module__ == real.__module__ and guard.__qualname__ == real.__qualname__, "the guard wraps claude_dispatch itself"
    # And the refusal itself, with no way to spend a model call even if the guard forwarded: an unknown backend
    # setting makes the real dispatcher raise OperatorError before it picks a backend or spawns anything.
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("NX_LLM_BACKEND", "no-such-backend")
        with pytest.raises(RuntimeError, match="live structured extraction"):
            asyncio.run(guard("p", {}, operator="request_coverage_extract"))
