# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-258 Phase 1 step 7 (nexus-2jomg.2): ``scripts/check_review.py batch`` proposes the next review batch.

The review demotes the class-A remainder in batches, largest baseline red-run count first, each batch one decision for
Sam (T2 nexus/sam-ruling-rdr258-accept-2026-11-27). The batch stops when the projected accounting share of red runs falls
under the 30% target: with the baseline's other red runs held constant, accounting red runs must stay under 3/7 of them
(the RDR's "about 122 per 60 days" from 286 others; this extraction's 318 others give 136; both are printed). These tests pin:

* order by baseline red-run count, with each numeric check's alarm line labelled as a proposal;
* the stop at the target, counted per run (a run that also fails a still-blocking check stays red), exactly (30% is not
  under 30%);
* an unclassified check (no ``class:`` header) never enters a batch, nor does a class-U one, one already demoted, or one
  another phase owns (an explicit list: ``cloud_leg`` is Phase 4, ``middleware`` is not Phase 3);
* the change text works at the level of tests: a module that is not wholly the check gets only its tests marked;
* the real committed extraction loads, and on this tree (no ``class:`` headers yet) it proposes nothing.
"""
from __future__ import annotations

from fractions import Fraction
from pathlib import Path

import pytest
from _check_review_fixture import load

cr = load("check_review_b")
ROOT = Path(__file__).resolve().parents[2]
HEAD = "# owner: ci\n# guards: nexus-1; src/x/\n# class: A\n"


def _tree(tmp_path: Path, classes: dict[str, str | None], owner: str = "ci") -> Path:
    """A repo tree whose check module ``tests/test_<name>.py`` carries the given class (None: no header at all)."""
    for name, klass in classes.items():
        path = tmp_path / "tests" / f"test_{name}.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        head = f"# owner: {owner}\n# guards: nexus-1; src/{name}/\n# class: {klass}\n" if klass else ""
        path.write_text(head + "def test_x():\n    pass\n", encoding="utf-8")
    return tmp_path


def _baseline(reds: dict[str, int], other: int = 100, shared: dict[str, list[str]] | None = None, tests: dict[str, list[str]] | None = None) -> dict:
    """Accounting runs: ``reds[name]`` runs that fail only that check, plus ``shared`` runs failing several at once."""
    runs: dict[str, list[str]] = {}
    for name, n in reds.items():
        for i in range(n):
            runs[f"{name}-{i}"] = [name]
    for i, (_label, names) in enumerate((shared or {}).items()):
        runs[f"shared-{i}"] = list(names)
    modules = {name: [f"tests/test_{name}.py"] for name in {*reds, *(n for v in (shared or {}).values() for n in v)}}
    return {"other_runs": other, "accounting_runs": runs, "check_modules": modules, "check_tests": tests or {}}


def _batch(tmp_path, reds, classes, *, other=100, shared=None, size=10, **kw):
    base = cr.baseline_from_json(_baseline(reds, other, shared))
    return cr.build_batch(base, _tree(tmp_path, classes), size=size, **kw)


def test_the_batch_is_ordered_by_baseline_red_runs_largest_first(tmp_path):
    out = _batch(tmp_path, {"small": 3, "big": 30, "mid": 12}, {"small": "A", "big": "A", "mid": "A"}, other=1)
    assert [c["check"] for c in out["batch"]] == ["big", "mid", "small"]
    assert [c["red_runs"] for c in out["batch"]] == [30, 12, 3]


def test_the_batch_stops_when_the_projected_share_falls_under_the_target(tmp_path):
    # 100 other runs: accounting must fall under 3/7 * 100 = 42.8, so 42 or fewer still-blocking accounting runs.
    reds = {"a": 40, "b": 30, "c": 20, "d": 10}
    out = _batch(tmp_path, reds, dict.fromkeys(reds, "A"), other=100)
    assert out["before"]["accounting_runs"] == 100 and out["before"]["share"] == pytest.approx(0.5)
    assert [c["check"] for c in out["batch"]] == ["a", "b"]
    assert out["after"]["accounting_runs"] == 30 and out["after"]["share"] < 0.30
    assert out["target_reached"] is True and out["bound_from_this_extraction"]["accounting_runs_max"] == 42


def test_exactly_30_percent_is_not_under_the_target_and_29_is(tmp_path):
    t = Fraction(3, 10)
    assert cr.under_target(30, 70, t) is False and cr.under_target(29, 70, t) is True
    assert cr.max_accounting(70, t) == 29 and cr.max_accounting(286, t) == 122 and cr.max_accounting(318, t) == 136 and cr.max_accounting(7, t) == 2
    # demoting "big" leaves 30 accounting runs against 70 others: exactly 30%, so not yet under; "rest" is needed too
    out = _batch(tmp_path, {"big": 30, "rest": 30}, {"big": "A", "rest": "A"}, other=70, size=1)
    assert [c["check"] for c in out["batch"]] == ["big"] and out["after"]["accounting_runs"] == 30
    assert out["after"]["share"] == pytest.approx(0.30) and out["after"]["under_target"] is False
    assert out["target_reached"] is False and out["checks_to_target"] == 2


def test_a_target_never_reached_says_so_and_proposes_what_it_can(tmp_path):
    out = _batch(tmp_path, {"a": 40, "b": 30}, {"a": "A", "b": None}, other=40)
    assert [c["check"] for c in out["batch"]] == ["a"] and out["target_reached"] is False and out["checks_to_target"] is None
    assert [c["check"] for c in out["unclassified"]] == ["b"]
    assert out["floor_runs_still_blocking_after_all_candidates"]["unclassified"] == 30 and "No prefix" in out["unmet"]


def test_the_proposal_says_how_far_down_the_order_the_target_is_whatever_the_batch_size(tmp_path):
    reds = {"a": 40, "b": 30, "c": 20, "d": 10}
    out = _batch(tmp_path, reds, dict.fromkeys(reds, "A"), other=100, size=1)
    assert [c["check"] for c in out["batch"]] == ["a"] and out["target_reached"] is False and out["checks_to_target"] == 2


def test_an_unclassified_check_is_listed_and_never_put_in_a_batch(tmp_path):
    out = _batch(tmp_path, {"a": 40, "b": 50, "c": 5}, {"a": "A", "b": None, "c": "A"}, other=10, size=5)
    assert "b" not in [c["check"] for c in out["batch"]]
    assert [c["check"] for c in out["unclassified"]] == ["b"] and out["unclassified"][0]["red_runs"] == 50


def test_a_check_with_no_module_resolved_is_unclassified_too(tmp_path):
    base = cr.baseline_from_json({"other_runs": 10, "accounting_runs": {"1": ["mystery"], "2": ["mystery"]}, "check_modules": {}})
    out = cr.build_batch(base, _tree(tmp_path, {}), size=5)
    assert out["batch"] == [] and [c["check"] for c in out["unclassified"]] == ["mystery"]


def test_class_u_demoted_and_owned_checks_stay_out_of_the_batch_by_an_explicit_list(tmp_path):
    reds = {"user_facing": 50, "already": 40, "file_size_ratchet": 30, "plugin_ledger": 20, "cloud_leg": 15, "middleware_lint": 12, "ddl_ratchet": 11, "plain": 10}
    classes = dict.fromkeys(reds, "A") | {"user_facing": "U"}
    out = _batch(tmp_path, reds, classes, other=10, demoted={"already"})
    assert [c["check"] for c in out["batch"]] == ["middleware_lint", "plain"], "ddl in middleware is not the DDL ratchet"
    skipped = {c["check"]: c["why"] for c in out["excluded"]}
    assert skipped == {"user_facing": "class U", "already": "already demoted", "file_size_ratchet": "owned by Phase 3 (ceilings and budgets)",
                       "plugin_ledger": "owned by Phase 4 (ledgers)", "cloud_leg": "owned by Phase 4 (ledgers)", "ddl_ratchet": "owned by Phase 3 (ceilings and budgets)"}


def test_wire_pairing_is_phase_2_and_a_header_owner_of_release_is_noted_not_excluded(tmp_path):
    base = cr.baseline_from_json({"other_runs": 1, "accounting_runs": {"1": ["wire pairing"], "2": ["relcheck"]},
                                  "check_modules": {"wire pairing": ["tests/test_wire_contract_pairing_lint.py"], "relcheck": ["tests/test_relcheck.py"]}})
    tree = _tree(tmp_path, {"relcheck": "A"}, owner="release")
    out = cr.build_batch(base, tree, size=3)
    assert {c["check"]: c["why"] for c in out["excluded"]}["wire pairing"] == "owned by Phase 2 (wire pairing)"
    assert "header owner release" in out["batch"][0]["ownership_note"]


def test_a_run_that_also_fails_a_still_blocking_check_stays_red(tmp_path):
    base = _baseline({"a": 2, "b": 1}, other=5, shared={"s": ["a", "b"]})
    out = cr.build_batch(cr.baseline_from_json(base), _tree(tmp_path, {"a": "A", "b": None}), size=1)
    assert [c["check"] for c in out["batch"]] == ["a"]
    assert out["before"]["accounting_runs"] == 4          # a-0, a-1, b-0, shared
    assert out["after"]["accounting_runs"] == 2           # b-0 and shared (b is still blocking there)


def test_the_floor_names_what_stays_blocking_class_u_and_phase_2_included(tmp_path):
    base = cr.baseline_from_json({"other_runs": 5, "accounting_runs": {"1": ["wire pairing"], "2": ["uonly"], "3": ["cloud_leg"], "4": ["mystery"], "5": ["plain"]},
                                  "check_modules": {"uonly": ["tests/test_uonly.py"], "plain": ["tests/test_plain.py"], "wire pairing": [], "cloud_leg": [], "mystery": []}})
    out = cr.build_batch(base, _tree(tmp_path, {"uonly": "U", "plain": "A"}), size=3)
    assert out["floor_runs_still_blocking_after_all_candidates"] == {"unclassified": 1, "class_u": 1, "phase_2": 1, "phase_3": 0, "phase_4": 1}
    text = cr.render_batch_markdown(out)
    assert "class U 1" in text and "Phase 2 1" in text and "Phase 4 1" in text


def test_a_numeric_checks_alarm_line_is_a_labelled_proposal_from_its_value(tmp_path):
    out = _batch(tmp_path, {"budget_thing": 9}, {"budget_thing": "A"}, other=2, values={"budget_thing": 2000})
    (c,) = out["batch"]
    prop = c["alarm_line"]
    assert prop["label"] == "proposal" and prop["value"] == 2000 and prop["proposed"] > 2000 and prop["rule"].startswith("value +")


def test_the_advisory_job_and_push_gate_changes_come_from_the_consumers_of_the_module(tmp_path):
    tree = _tree(tmp_path, {"a": "A"})
    (tree / "scripts").mkdir()
    (tree / "scripts" / "push_check_gate.py").write_text("# runs test_a\n")
    (tree / ".github" / "workflows").mkdir(parents=True)
    (tree / ".github" / "workflows" / "ci.yml").write_text("jobs:\n  test-lint:\n    steps:\n      - run: pytest tests/test_a.py\n")
    out = cr.build_batch(cr.baseline_from_json(_baseline({"a": 5}, other=1, tests={"a": ["test_x"]})), tree, size=3)
    (c,) = out["batch"]
    text = "\n".join(c["changes"])
    assert "pytest.mark.class_a" in text and "scripts/push_check_gate.py" in text and "test-lint" in text


def test_a_module_that_is_not_wholly_the_check_gets_only_its_tests_marked(tmp_path):
    tree = tmp_path
    (tree / "tests").mkdir()
    (tree / "tests" / "test_mixed.py").write_text(HEAD + "import pytest\n\ndef test_the_check():\n    pass\n\ndef test_b():\n    pass\n\ndef test_c():\n    pass\n\ndef test_d():\n    pass\n")
    base = cr.baseline_from_json(_baseline({"mixed": 5}, other=1, tests={"mixed": ["test_the_check"]}))
    (c,) = cr.build_batch(base, tree, size=2)["batch"]
    text = "\n".join(c["changes"])
    assert "add pytest.mark.class_a to ONLY these test(s) (test_the_check)" in text and "--require tests/test_mixed::test_the_check=1" in text
    assert "the other 3 test(s) in the module stay blocking" in text and "pytestmark" not in text


def test_a_module_that_is_the_check_by_its_lint_pytestmark_is_marked_as_a_whole(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_whole.py").write_text(HEAD + "import pytest\npytestmark = pytest.mark.lint\n\ndef test_a():\n    pass\n\ndef test_b():\n    pass\n")
    base = cr.baseline_from_json(_baseline({"whole": 5}, other=1, tests={"whole": ["test_a"]}))
    (c,) = cr.build_batch(base, tmp_path, size=2)["batch"]
    text = "\n".join(c["changes"])
    assert "every test is the check" in text and "--require tests/test_whole=2" in text and "ONLY" not in text


def test_the_markdown_names_every_check_its_count_and_both_bounds(tmp_path):
    out = _batch(tmp_path, {"a": 40, "b": 30}, {"a": "A", "b": "A"}, other=100, values={"a": 100})
    text = cr.render_batch_markdown(out)
    assert "`a`" in text and "proposal" in text and "40" in text and "30%" in text
    assert "this extraction: at most 42" in text and "the RDR's own figure: about 122" in text and "286 others" in text


def test_the_real_extraction_loads_and_proposes_nothing_on_a_tree_with_no_class_headers():
    extraction = ROOT / "scripts/red-run-data/red-run-extraction-2026-09-23_2026-11-23.jsonl.gz"
    base = cr.load_baseline(extraction)
    assert base.total_runs == 788 and base.accounting_runs == 470, "the Phase 0 baseline"
    out = cr.build_batch(base, ROOT, size=50)
    assert out["bound_from_this_extraction"]["accounting_runs_max"] == 136 and out["bound_from_this_extraction"]["other_runs"] == 318
    assert out["bound_from_rdr"]["accounting_runs_max"] == 122 and out["before"]["other_runs"] == 318
    assert out["batch"] == [], "no module on this tree carries a class: header, so nothing is classified and nothing may be batched; when scripts/check_headers.py's headers land this assertion is the one to revisit"
    assert len(out["unclassified"]) > 50 and all(c["class"] == "A" for c in out["batch"])
    assert sum(out["other_breakdown"].values()) == 318
