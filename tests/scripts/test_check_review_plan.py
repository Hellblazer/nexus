# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-258 Phase 1 step 5 (nexus-2jomg.2): ``scripts/check_review.py deletion-plan``.

The plan works at the level of tests, not modules: a check's unit is its ``class_a`` tests, and a module can hold one marked
test among twenty (tests/test_mode_declarations_are_explicit.py does). It cross-checks the record it is handed, and a
``retire-early`` (the census case, T2 nexus/rdr258-phase1-step3-cause-test-2026-11-28) must carry its evidence:

* the marked tests to remove and the unmarked ones that stay; ``git rm`` of a module only when every test is class_a, or
  ``module_whole`` with a written reason;
* a due check with no entry, an entry for a check that is not due, and an empty plan are refused;
* a ``retire-early`` needs a full cause-fix sha that is an ancestor, is neither the demotion nor the birth commit and touches
  the guards, the three written statements and the evidence object, and may be proposed before day 60.
"""
from __future__ import annotations

import json

import pytest
from _check_review_fixture import MODULE, STEM, check, commit, git, history, load, record

cr = load()

CONSUMERS = {
    ".github/workflows/ci.yml": f"jobs:\n  test-mode-census:\n    steps:\n      - run: NX_CENSUS_ONLY_JOB=1 pytest tests/{STEM}.py\n",
    "scripts/push_check_gate.py": f"# gates {STEM}\n",
    "tests/conftest.py": "_MODE_LINT_EXCLUDE_FILES = set()\n_MODE_LINT_EXCLUDE_NODEIDS = set()\n",
    ".nexus/procedures/preflight.lua": "-- partial_session_view_reason, NX_CENSUS_ONLY_JOB\n",
    "docs/notes.md": f"mentions {STEM}\n",
}
ONE_OF_FOUR = (("test_one", True), ("test_two", False), ("test_three", False), ("test_four", False))
CENSUS_NAMES = ["NX_CENSUS_ONLY_JOB", "test-mode-census", "_MODE_LINT_EXCLUDE", "partial_session_view_reason"]
FULL_SHA = "f" * 40


def _setup(tmp_path, **kw):
    repo, art, sha = history(tmp_path, extra={**CONSUMERS, **kw.pop("extra", {})}, **kw)
    return repo, art, sha


def _entry(rec, **over):
    return {**rec["due_for_deletion"][0], **over}


def _plan(tmp_path, capsys, repo, rec, *entries):
    path = tmp_path / "record.json"
    path.write_text(json.dumps({**rec, "due_for_deletion": list(entries)}), encoding="utf-8")
    code = cr.main(["deletion-plan", str(path), "--repo", str(repo)])
    cap = capsys.readouterr()
    return code, cap.out, cap.err


# -- test level -----------------------------------------------------------------------------------------------------------


def test_a_module_whose_every_test_is_class_a_is_removed_whole_with_its_consumers_and_counts(tmp_path, capsys):
    repo, art, _ = _setup(tmp_path)
    rec = record(cr, repo, art)
    code, out, err = _plan(tmp_path, capsys, repo, rec, _entry(rec))
    assert code == 0, err
    assert f"git rm {MODULE}" in out and "ci-workflow" in out and "test-mode-census" in out and "push-gate" in out
    assert "before:" in out and "per name:" in out and f"{STEM} " in out and "git grep -c -F" in out


def test_a_module_with_one_marked_test_among_four_removes_the_one_and_keeps_the_three(tmp_path, capsys):
    repo, art, _ = _setup(tmp_path, tests=ONE_OF_FOUR)
    rec = record(cr, repo, art)
    c = check(rec)
    assert c["marked_tests"] == ["test_one"] and c["unmarked_tests"] == ["test_two", "test_three", "test_four"]
    code, out, err = _plan(tmp_path, capsys, repo, rec, _entry(rec))
    assert code == 0, err
    assert "git rm" not in out.replace("do not git rm the module", "")
    assert "delete the 1 class_a test(s): test_one" in out
    assert "3 unmarked test(s) that still block: test_two, test_three, test_four" in out


def test_module_whole_needs_a_written_reason_and_then_names_the_unmarked_tests_it_deletes(tmp_path, capsys):
    repo, art, _ = _setup(tmp_path, tests=ONE_OF_FOUR)
    rec = record(cr, repo, art)
    code, out, err = _plan(tmp_path, capsys, repo, rec, _entry(rec, module_whole=True, module_whole_reason="  "))
    assert code == 1 and "module_whole_reason" in err and "git rm" not in out
    code, out, err = _plan(tmp_path, capsys, repo, rec, _entry(rec, module_whole=True, module_whole_reason="the other three are dead copies"))
    assert code == 0, err
    assert f"git rm {MODULE}" in out and "also deletes 3 unmarked test(s): test_two, test_three, test_four" in out and "dead copies" in out


def test_a_module_with_no_class_a_test_has_nothing_to_remove(tmp_path, capsys):
    repo, art, _ = _setup(tmp_path)
    rec = record(cr, repo, art)
    commit(repo, {MODULE: (repo / MODULE).read_text().replace("pytest.mark.class_a", "pytest.mark.slow")}, 0, "unmark")
    code, _out, err = _plan(tmp_path, capsys, repo, rec, _entry(rec))
    assert code == 1 and "no class_a-marked test" in err


# -- the record is cross-checked -------------------------------------------------------------------------------------------


def test_a_due_check_with_no_entry_is_refused_because_a_dropped_entry_would_read_as_a_keep(tmp_path, capsys):
    repo, art, _ = _setup(tmp_path)
    rec = record(cr, repo, art)
    code, out, err = _plan(tmp_path, capsys, repo, rec)
    assert code == 1 and "due in this record but has no entry" in err and "nothing is planned" not in err and "git rm" not in out


def test_an_entry_for_a_check_that_is_not_due_is_refused_unless_it_is_a_retire_early(tmp_path, capsys):
    repo, art, _ = _setup(tmp_path, demoted_days_ago=40)
    rec = record(cr, repo, art, demoted_days_ago=40)
    assert not check(rec)["due"]
    entry = {"check": STEM, "module": MODULE, "disposition": "delete", "demotion_sha": check(rec)["demotion_sha"]}
    code, out, err = _plan(tmp_path, capsys, repo, rec, entry)
    assert code == 1 and "not due in this record" in err and "git rm" not in out


def test_an_empty_plan_exits_non_zero(tmp_path, capsys):
    repo, art, _ = _setup(tmp_path, demoted_days_ago=40)
    rec = record(cr, repo, art, demoted_days_ago=40)
    code, out, err = _plan(tmp_path, capsys, repo, rec)
    assert code == 1 and "nothing is planned" in err and out == ""


def test_a_keep_without_a_reason_is_refused_and_with_one_is_recorded(tmp_path, capsys):
    repo, art, _ = _setup(tmp_path)
    rec = record(cr, repo, art)
    code, out, err = _plan(tmp_path, capsys, repo, rec, _entry(rec, disposition="keep", keep_reason="   "))
    assert code == 1 and "keep_reason" in err and "git rm" not in out
    code, out, err = _plan(tmp_path, capsys, repo, rec, _entry(rec, disposition="keep", keep_reason="guards a live incident"))
    assert code == 0 and "KEEP" in out and "guards a live incident" in out and "git rm" not in out


def test_a_plan_whose_consumer_grep_finds_nothing_is_refused_as_vacuous(tmp_path, capsys):
    repo, art, _ = history(tmp_path)   # nothing outside the module names the check
    rec = record(cr, repo, art)
    code, out, err = _plan(tmp_path, capsys, repo, rec, _entry(rec))
    assert code == 1 and "vacuous" in err and "git rm" not in out


def test_an_unknown_disposition_is_refused(tmp_path, capsys):
    repo, art, _ = _setup(tmp_path)
    rec = record(cr, repo, art)
    code, _out, err = _plan(tmp_path, capsys, repo, rec, _entry(rec, disposition="maybe"))
    assert code == 1 and "disposition" in err


def test_a_stuck_check_can_be_deleted_with_a_written_reason_and_not_without_one(tmp_path, capsys):
    repo, art, _ = _setup(tmp_path, guards=None, demoted_days_ago=80)
    rec = record(cr, repo, art, demoted_days_ago=80)
    assert [s["check"] for s in rec["stuck"]] == [STEM] and rec["due_for_deletion"] == []
    entry = {"check": STEM, "disposition": "delete"}
    code, out, err = _plan(tmp_path, capsys, repo, rec, entry)
    assert code == 1 and "delete_reason" in err and "git rm" not in out
    code, out, err = _plan(tmp_path, capsys, repo, rec, {**entry, "delete_reason": "no guards header in 80 days and nobody owns it"})
    assert code == 0, err
    assert f"git rm {MODULE}" in out and "stuck check, deleted for: no guards header" in out
    code, _out, err = _plan(tmp_path, capsys, repo, rec, {"check": "other", "disposition": "delete", "delete_reason": "x", "module": MODULE})
    assert code == 1 and "neither" not in err and "other" in err, "a delete for a check that is neither due nor stuck is still refused"


def test_an_entry_whose_module_or_kind_disagrees_with_the_records_check_is_refused(tmp_path, capsys):
    repo, art, _ = _setup(tmp_path)
    rec = record(cr, repo, art)
    code, out, err = _plan(tmp_path, capsys, repo, rec, _entry(rec, module="scripts/push_check_gate.py"))
    assert code == 1 and "the record's check says" in err and "git rm" not in out
    code, out, err = _plan(tmp_path, capsys, repo, rec, _entry(rec, kind="numeric"))
    assert code == 1 and "kind" in err and "git rm" not in out
    code, out, err = _plan(tmp_path, capsys, repo, rec, {k: v for k, v in _entry(rec).items() if k not in ("module", "kind")})
    assert code == 0 and f"git rm {MODULE}" in out, "an entry that names neither takes both from the record"


def test_a_catch_set_aside_as_non_substantive_is_shown_in_the_plan(tmp_path, capsys):
    repo, art, _ = _setup(tmp_path)
    rec = record(cr, repo, art)
    aside = {"from_sha": "a" * 40, "to_sha": "b" * 40, "non_substantive": True, "non_substantive_reason": "a rename, not a defect"}
    code, out, err = _plan(tmp_path, capsys, repo, rec, _entry(rec, set_aside_catches=[aside]))
    assert code == 0, err
    assert "set aside as non_substantive" in out and "a rename, not a defect" in out


# -- retire-early: the census case ----------------------------------------------------------------------------------------


def _census(tmp_path):
    """The census, 20 days after its demotion, with the cause fixed 10 days ago by a commit that touches its guards."""
    repo, art, sha = _setup(tmp_path, demoted_days_ago=20, guards="nexus-4h20a; tests/_env_restore.py", fix_day=-1,
                            between={"tests/_env_restore.py": "# an early draft, before the check was demoted\n"})
    cause = commit(repo, {"tests/_env_restore.py": "# snapshots NX_LOCAL and friends\n"}, 10, "isolate the mode state at its cause")
    rec = record(cr, repo, art, demoted_days_ago=20)
    assert check(rec)["due"] is False and rec["due_for_deletion"] == [], "20 days: retire-early is proposed before day 60"
    entry = {
        "check": STEM, "module": MODULE, "disposition": "retire-early", "demotion_sha": sha["demote"], "cause_fix_sha": cause,
        "names": CENSUS_NAMES, "leaves_unguarded": "a new test naming a cloud embedder that silently runs local",
        "replacement_guard": "tests/_env_restore.py with tests/test_mode_state_isolation.py (planted pairs)",
        "reopen_condition": "a develop red traced to a result that depended on an undeclared mode",
        "evidence": {"guards_readings": "narrow: no test passes only on carried state; wide: every cloud-naming test declares a mode",
                     "red_classification": {"neutral-token swap": 32, "declaration added": 10, "real leak": 0},
                     "ablation": "88% of declarations decorative", "consumer_names": CENSUS_NAMES}}
    return repo, rec, sha, entry, cause


def test_a_retire_early_proposed_at_20_days_with_its_evidence_is_planned_with_every_consumer_name_counted(tmp_path, capsys):
    repo, rec, _, entry, cause = _census(tmp_path)
    code, out, err = _plan(tmp_path, capsys, repo, rec, entry)
    assert code == 0, err
    assert "[retire-early]" in out and cause[:10] in out and "88% of declarations decorative" in out and "replacement guard: tests/_env_restore.py" in out
    for name in CENSUS_NAMES:
        assert f"{name} " in out.split("per name:")[1].split("\n")[0], name
    assert "NX_CENSUS_ONLY_JOB" in out and ".github/workflows/ci.yml" in out and "partial_session_view_reason" in out
    assert "git grep -c -F -e " + MODULE in out, "the exact after-check is printed"


@pytest.mark.parametrize("field,value,needle", [
    ("cause_fix_sha", "", "full 40-character sha"),
    ("cause_fix_sha", "abc1234", "full 40-character sha"),
    ("cause_fix_sha", FULL_SHA, "not a commit"),
    ("leaves_unguarded", "  ", "leaves_unguarded"),
    ("reopen_condition", "", "reopen_condition"),
    ("replacement_guard", "", "replacement_guard"),
    ("evidence", {}, "evidence.guards_readings"),
    ("evidence", {"guards_readings": "x", "consumer_names": ["a"]}, "evidence.red_classification"),
    ("evidence", {"guards_readings": "x", "red_classification": {"a": 1}}, "evidence.consumer_names"),
])
def test_a_retire_early_missing_its_sha_statements_or_evidence_is_refused(tmp_path, capsys, field, value, needle):
    repo, rec, _, entry, _ = _census(tmp_path)
    code, out, err = _plan(tmp_path, capsys, repo, rec, {**entry, field: value})
    assert code == 1 and needle in err and "git rm" not in out and "delete the" not in out


def test_a_retire_early_cause_that_is_the_demotion_the_birth_or_off_the_guards_is_refused(tmp_path, capsys):
    repo, rec, sha, entry, _ = _census(tmp_path)
    for bad, needle in ((sha["demote"], "demotion or birth"), (sha["born"], "demotion or birth"), (sha["d5"], "touches none of the guarded paths"),
                        (sha["between"], "predates the demotion")):
        code, _out, err = _plan(tmp_path, capsys, repo, rec, {**entry, "cause_fix_sha": bad})
        assert code == 1 and needle in err, (bad, err)


def test_a_retire_early_cause_that_is_not_an_ancestor_of_the_reviewed_rev_is_refused(tmp_path, capsys):
    repo, rec, _, entry, _ = _census(tmp_path)
    tree = git(repo, "rev-parse", "HEAD^{tree}")
    stray = git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit-tree", tree, "-m", "elsewhere")
    code, _out, err = _plan(tmp_path, capsys, repo, rec, {**entry, "cause_fix_sha": stray})
    assert code == 1 and "not an ancestor" in err
