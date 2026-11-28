# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-258 Phase 1 step 5 (nexus-2jomg.2): ``scripts/check_review.py record``.

The 2-week review reads git and the uploaded CLASS_A_ITEM artifacts and writes one record. A class-A check with no guard
catch in the 60 days since its clock started is DELETED by default (Sam, 2026-11-27, T2
nexus/sam-ruling-rdr258-retire-by-default-and-half-pins-2026-11-27). The tests build a real git repository with a push a day
(``_check_review_fixture.history``) and pin what the record says, and what it refuses to say:

* a 61-day-old demotion with no catch is due; a real guard catch, a ledger-only fix, a mixed fix, a prefix that is not on a
  path separator, an item that only moved lines, and a range that resolves nothing are not catches;
* evidence is data: a snapshot where the check did not run is no evidence, sparse days are an evidence gap, expired
  artifacts and a capped run list are holes, and a check is never due for want of a ``guards:`` header;
* the clock starts at the current unbroken stretch (re-promotion resets it, a rename keeps it) and at the first CI run that
  holds the demotion commit.
"""
from __future__ import annotations

import importlib.util
import io
import json
import sys
import zipfile
from datetime import date, timedelta
from pathlib import Path

import pytest
from _check_review_fixture import MODULE, STEM, UNTIL, artifact, check, commit, git, history, load, module_text, record

from tests._module_seam import setattr_in

from tests import _quarantine_registry as reg

cr = load()


def _rec(repo, art, **kw):
    return record(cr, repo, art, **kw)


# -- due, and what is not a catch ------------------------------------------------------------------------------------------


def test_a_61_day_old_demotion_with_no_guard_catch_and_a_push_a_day_is_due_for_deletion_by_default(tmp_path):
    repo, art, sha = history(tmp_path)
    rec = _rec(repo, art)
    c = check(rec)
    assert c["demotion_sha"] == sha["demote"] and c["demotion_date"] == "2026-09-28" and c["days_since_demotion"] == 61
    assert c["catches_total"] == 0 and c["due"] is True and c["coverage"]["pct"] >= 80 and c["coverage"]["max_gap_days"] <= 3
    assert "fallback" in c["clock_source"], "no CI run in the fetch: the record says it fell back"
    assert rec["thresholds"]["coverage_min_pct"] == 80 and rec["thresholds"]["gap_max_days"] == 3
    (entry,) = rec["due_for_deletion"]
    assert entry["check"] == STEM and entry["module"] == MODULE
    assert entry["disposition"] == "delete" and entry["keep_reason"] == "" and entry["cause_fix_sha"] == ""
    assert entry["marked_tests"] == ["test_x"] and entry["unmarked_tests"] == []


def test_the_60_day_boundary_is_due_at_60_and_not_at_59(tmp_path):
    repo, art, _ = history(tmp_path / "a", demoted_days_ago=60)
    assert check(_rec(repo, art, demoted_days_ago=60))["due"] is True
    repo, art, _ = history(tmp_path / "b", demoted_days_ago=59)
    c = check(_rec(repo, art, demoted_days_ago=59))
    assert c["days_since_demotion"] == 59 and c["due"] is False and "59 days" in c["due_reason"]


def test_a_real_guard_catch_keeps_the_check_out_of_the_due_list(tmp_path):
    repo, art, sha = history(tmp_path, fix_files={"src/foo/x.py": "x = 2\n"})
    rec = _rec(repo, art)
    c = check(rec)
    assert c["catches_total"] == 1 and c["due"] is False and rec["due_for_deletion"] == []
    (catch,) = c["catches"]
    assert (catch["from_sha"], catch["to_sha"]) == (sha["d31"], sha["d30"])
    assert catch["items_resolved"] == ["pkg/b.py"] and catch["guard_files"] == ["src/foo/x.py"]


def test_a_fix_that_touched_only_the_checks_own_allowlist_is_not_a_catch(tmp_path):
    repo, art, _ = history(tmp_path, guards="nexus-1; conf/", fix_files={"conf/allowlist.txt": "a\nb\n"})
    c = check(_rec(repo, art))
    assert c["catches_total"] == 0 and c["due"] is True


def test_a_mixed_fix_of_a_guarded_path_and_a_ledger_is_no_catch_and_is_listed(tmp_path):
    repo, art, _ = history(tmp_path, fix_files={"src/foo/x.py": "x = 2\n", "conf/allowlist.txt": "a\nb\n"})
    rec = _rec(repo, art)
    c = check(rec)
    assert c["catches_total"] == 0 and c["due"] is True
    (mixed,) = c["mixed_fixes"]
    assert mixed["guard_files"] == ["src/foo/x.py"] and mixed["ledger_files"] == ["conf/allowlist.txt"]
    assert "mixed fix, not a catch" in cr.render_markdown(rec)


def test_a_guards_prefix_matches_on_a_path_separator(tmp_path):
    repo, art, _ = history(tmp_path, guards="nexus-1; src/foo", fix_files={"src/foobar/y.py": "y = 1\n"})
    assert check(_rec(repo, art))["catches_total"] == 0
    assert cr.under("src/foo/x.py", "src/foo") and cr.under("src/foo/x.py", "src/foo/") and not cr.under("src/foobar/y.py", "src/foo")


def test_a_range_that_resolves_nothing_is_not_a_catch(tmp_path):
    repo, art, _ = history(tmp_path, fix_files={"src/foo/x.py": "x = 2\n"}, items_after=["pkg/a.py:10", "pkg/b.py:20"])
    assert check(_rec(repo, art))["catches_total"] == 0


def test_items_that_only_moved_lines_are_not_a_catch_and_a_rise_is_listed(tmp_path):
    # the guarded edit shifts every line number and adds a failing item: nothing was fixed, the count rose
    repo, art, _ = history(tmp_path, fix_files={"src/foo/x.py": "x = 2\n"}, items_after=["pkg/a.py:15", "pkg/b.py:25", "pkg/c.py:3"])
    rec = _rec(repo, art)
    c = check(rec)
    assert c["catches_total"] == 0 and c["rose"] is True and (c["rose_from"], c["rose_to"]) == (2, 3)
    assert [r["check"] for r in rec["rose"]] == [STEM]


def test_a_disappearance_with_a_guard_touch_needs_the_count_to_fall_or_the_failing_path_fixed(tmp_path):
    # b.py's item is replaced by c.py's: the count stays 2 and no failing path was touched
    repo, art, _ = history(tmp_path, fix_files={"src/foo/x.py": "x = 2\n"}, items_after=["pkg/a.py:10", "pkg/c.py:3"])
    assert check(_rec(repo, art))["catches_total"] == 0
    # the same swap with the failing path itself fixed in the range is a catch
    repo, art, _ = history(tmp_path / "fixed", fix_files={"src/foo/x.py": "x = 2\n", "pkg/b.py": "fixed\n"}, items_after=["pkg/a.py:10", "pkg/c.py:3"])
    assert check(_rec(repo, art))["catches_total"] == 1


def test_fixing_one_of_two_offenders_in_the_same_file_is_a_catch(tmp_path):
    repo, art, _ = history(tmp_path, fix_files={"src/foo/x.py": "x = 2\n"}, items_before=["pkg/a.py:10", "pkg/a.py:20"], items_after=["pkg/a.py:10"])
    c = check(_rec(repo, art))
    assert c["catches_total"] == 1 and c["catches"][0]["items_resolved"] == ["pkg/a.py"] and c["due"] is False


def test_ids_that_move_lines_in_a_file_edited_in_the_range_are_not_a_catch(tmp_path):
    # pkg/a.py is itself edited in the fixing range and its one failing id moves from line 10 to 15: same count, same item
    repo, art, _ = history(tmp_path, fix_files={"src/foo/x.py": "x = 2\n", "pkg/a.py": "edited\n"}, items_before=["pkg/a.py:10"], items_after=["pkg/a.py:15"])
    c = check(_rec(repo, art))
    assert c["catches_total"] == 0 and c["due"] is True and c["rose"] is False


def test_a_conftest_edit_counts_as_a_ledger_edit_only_when_it_touches_an_exclusion_set_the_check_reads(tmp_path):
    reads = "from tests.conftest import _MODE_LINT_EXCLUDE_FILES\n"
    repo, art, _ = history(tmp_path / "sets", tail=reads,
                           fix_files={"src/foo/x.py": "x = 2\n", "tests/conftest.py": "_MODE_LINT_EXCLUDE_FILES = {'a'}\n"})
    c = check(_rec(repo, art))
    assert c["catches_total"] == 0 and c["mixed_fixes"][0]["ledger_files"] == ["tests/conftest.py"]
    repo, art, _ = history(tmp_path / "other", tail=reads, fix_files={"src/foo/x.py": "x = 2\n", "tests/conftest.py": "def helper():\n    return 1\n"})
    c = check(_rec(repo, art))
    assert c["catches_total"] == 1 and c["mixed_fixes"] == []


def test_catches_carried_from_an_earlier_record_are_matched_by_demotion_sha_so_a_re_promoted_stretch_starts_clean(tmp_path):
    repo, art, _ = history(tmp_path)
    now = check(_rec(repo, art))
    caught = {"check": STEM, "from_sha": "1" * 40, "to_sha": "2" * 40, "items_resolved": ["q"], "guard_files": ["src/foo/y.py"]}
    other_stretch = {"checks": [{"check": STEM, "demotion_sha": "9" * 40, "demotion_date": "2026-08-01", "clock_source": "x", "catches": [caught], "usable_dates": []}]}
    again = check(_rec(repo, art, priors=[other_stretch]))
    assert again["catches_total"] == 0 and again["due"] is True
    same_stretch = {"checks": [{**other_stretch["checks"][0], "demotion_sha": now["demotion_sha"], "demotion_date": now["demotion_date"]}]}
    assert check(_rec(repo, art, priors=[same_stretch]))["catches_total"] == 1


def test_a_catch_marked_non_substantive_with_a_reason_is_honoured_by_a_later_run(tmp_path):
    repo, art, _ = history(tmp_path, fix_files={"src/foo/x.py": "x = 2\n"})
    first = _rec(repo, art)
    assert check(first)["catches_total"] == 1
    first["checks"][0]["catches"][0].update(non_substantive=True, non_substantive_reason="a rename, not a defect")
    again = _rec(repo, art, priors=[first])
    c = check(again)
    assert c["catches_total"] == 0 and c["due"] is True and c["catches"][0]["non_substantive"] is True
    assert again["due_for_deletion"][0]["set_aside_catches"][0]["non_substantive_reason"] == "a rename, not a defect"
    first["checks"][0]["catches"][0]["non_substantive_reason"] = " "
    assert check(_rec(repo, art, priors=[first]))["catches_total"] == 1, "a mark with no written reason is not honoured"


# -- evidence is data ------------------------------------------------------------------------------------------------------


def test_a_snapshot_where_the_check_did_not_run_is_no_evidence(tmp_path):
    repo, art, sha = history(tmp_path, fix_files={"src/foo/x.py": "x = 2\n"})
    for key, s in sha.items():  # no run examined the check at all
        if key.startswith("d") or key == "demote":
            artifact(art, s, [], examined=None)
    c = check(_rec(repo, art))
    assert c["catches_total"] == 0 and c["due"] is False and "evidence gap" in c["due_reason"] and "no report run" in c["due_reason"]


def test_one_run_that_did_not_examine_the_check_does_not_turn_its_items_into_a_resolved_catch(tmp_path):
    repo, art, sha = history(tmp_path, fix_files={"src/foo/x.py": "x = 2\n"}, fix_day=30)
    artifact(art, sha["d30"], [], examined=None)   # the run after the fix did not run the check: its items "vanished"
    c = check(_rec(repo, art))
    assert c["catches_total"] == 1, "the resolution is read across the runs that DID examine the check, once"
    assert (c["catches"][0]["from_sha"], c["catches"][0]["to_sha"]) == (sha["d31"], sha["d29"])


def test_sparse_snapshots_are_an_evidence_gap_not_a_due_check(tmp_path):
    repo, art, _ = history(tmp_path, skip=frozenset(range(0, 61)) - {50, 10})
    c = check(_rec(repo, art))
    assert c["due"] is False and c["due_reason"].startswith("evidence gap") and c["evidence_gap"] is True
    assert c["coverage"]["max_gap_days"] > 3


def test_a_gap_of_three_days_is_fine_and_four_is_not(tmp_path):
    repo, art, _ = history(tmp_path / "three", skip=frozenset({40, 39}))
    c = check(_rec(repo, art))
    assert c["coverage"]["max_gap_days"] == 3 and c["due"] is True
    repo, art, _ = history(tmp_path / "four", skip=frozenset({40, 39, 38}))
    c = check(_rec(repo, art))
    assert c["coverage"]["max_gap_days"] == 4 and c["due"] is False and "largest gap 4" in c["due_reason"]


def test_coverage_under_80_percent_is_not_due_even_with_small_gaps(tmp_path):
    repo, art, _ = history(tmp_path, step=2)   # a push every other day: gaps of 2, coverage about 50%
    c = check(_rec(repo, art))
    assert c["coverage"]["max_gap_days"] == 2 and c["coverage"]["pct"] < 80 and c["due"] is False and "coverage" in c["due_reason"]


def test_a_prior_records_usable_days_close_the_evidence_for_a_directory_that_holds_only_the_last_two_weeks(tmp_path):
    repo, art, _ = history(tmp_path, skip=frozenset(range(15, 61)))   # artifacts live 30 days; only days 14..0 survive
    c = check(_rec(repo, art))
    assert c["due"] is False and "evidence gap" in c["due_reason"]
    days = [(UNTIL - timedelta(days=n)).isoformat() for n in range(61, 14, -1)]
    prior = {"checks": [{"check": STEM, "demotion_sha": c["demotion_sha"], "demotion_date": c["demotion_date"], "clock_source": c["clock_source"],
                         "catches": [], "usable_dates": days}]}
    assert check(_rec(repo, art, priors=[prior]))["due"] is True


def test_expired_artifacts_in_the_window_and_a_capped_run_list_are_coverage_holes(tmp_path):
    repo, art, sha = history(tmp_path)
    (art / "_fetch.json").write_text(json.dumps({"expired": [{"id": 1, "head_sha": sha["d20"], "created_at": "2026-11-08T10:00:00Z", "name": "class-a-items"}],
                                                 "missing": [], "capped": False}))
    c = check(_rec(repo, art))
    assert c["due"] is False and "1 expired artifact(s) in the window" in c["due_reason"]
    (art / "_fetch.json").write_text(json.dumps({"expired": [], "missing": [], "capped": True}))
    c = check(_rec(repo, art))
    assert c["due"] is False and "cap" in c["due_reason"]


def test_a_check_without_a_guards_header_is_a_finding_never_due_and_stuck_past_75_days(tmp_path):
    repo, art, _ = history(tmp_path, guards=None, demoted_days_ago=80)
    rec = _rec(repo, art, demoted_days_ago=80)
    assert not check(rec)["due"] and rec["due_for_deletion"] == []
    assert any(f["module"] == MODULE and "guards" in f["detail"] for f in rec["findings"])
    (stuck,) = rec["stuck"]
    assert stuck["check"] == STEM and any("restart the clock" in ch for ch in stuck["choices"]) and "Stuck" in cr.render_markdown(rec)
    repo, art, _ = history(tmp_path / "young", guards=None, demoted_days_ago=70)
    assert _rec(repo, art, demoted_days_ago=70)["stuck"] == [], "stuck begins after 75 days"


def test_a_class_u_header_on_a_class_a_marked_check_is_a_finding_and_never_due(tmp_path):
    repo, art, _ = history(tmp_path, klass="U")
    rec = _rec(repo, art)
    assert not check(rec)["due"] and any("class: U" in f["detail"] for f in rec["findings"])


# -- the clock ------------------------------------------------------------------------------------------------------------


def test_the_clock_is_the_first_develop_push_ci_run_that_holds_the_demotion_commit(tmp_path):
    repo, art, sha = history(tmp_path)
    (art / "_runs.json").write_text(json.dumps([{"id": 778, "head_sha": sha["d54"], "created_at": "2026-10-05T08:00:00Z"},
                                                {"id": 777, "head_sha": sha["d55"], "created_at": "2026-10-04T08:00:00Z"}]))
    c = check(_rec(repo, art))
    assert c["demotion_date"] == "2026-10-04" and c["days_since_demotion"] == 55 and "run 777" in c["clock_source"] and c["due"] is False


def test_a_later_record_carries_the_clock_of_an_earlier_one(tmp_path):
    repo, art, _ = history(tmp_path)
    first = _rec(repo, art)
    first["checks"][0].update(demotion_date="2026-09-20", clock_source="first develop push CI run containing it (run 1)")
    again = check(_rec(repo, art, priors=[first]))
    assert again["demotion_date"] == "2026-09-20" and "carried from an earlier record" in again["clock_source"]


def test_a_re_promotion_resets_the_clock_and_a_rename_keeps_it(tmp_path):
    repo, art, sha = history(tmp_path)
    commit(repo, {MODULE: module_text(marked=False)}, 21, "un-mark")
    again = commit(repo, {MODULE: module_text()}, 20, "re-promote")
    assert cr.stretch_start(repo, "HEAD", cr.Module(MODULE)) == (again, MODULE)
    repo2, _, sha2 = history(tmp_path / "rn")
    new = "tests/test_foo_check_renamed.py"
    commit(repo2, {new: (repo2 / MODULE).read_text(), MODULE: None}, 5, "rename")
    assert cr.stretch_start(repo2, "HEAD", cr.Module(new))[0] == sha2["demote"]


def test_the_header_at_demotion_and_at_tip_are_both_recorded_and_a_difference_is_flagged(tmp_path):
    repo, art, _ = history(tmp_path)
    commit(repo, {MODULE: module_text(guards="nexus-9; src/other/")}, 3, "retarget the guards")
    rec = _rec(repo, art)
    c = check(rec)
    assert c["header_at_demotion"]["guard_paths"] == ["src/foo/"] and c["header"]["guard_paths"] == ["src/other/"]
    assert any("header changed since the demotion" in f["detail"] for f in rec["findings"])
    repo, art, _ = history(tmp_path / "late", guards=None)
    commit(repo, {MODULE: module_text()}, 3, "add the header later")
    assert any("header added after the demotion" in f["detail"] for f in _rec(repo, art)["findings"])


# -- what else the record carries ----------------------------------------------------------------------------------------


def test_failing_item_counts_per_run_and_the_opportunity_count_are_listed(tmp_path):
    repo, art, _ = history(tmp_path, fix_files={"src/foo/x.py": "x = 2\n"})
    c = check(_rec(repo, art, since=UNTIL - timedelta(days=60)))
    counts = [r["failing_items"] for r in c["runs"]]
    assert counts[:2] == [2, 2] and counts[-1] == 1
    assert all(r["passed"] == 5 and r["examined"] == 5 for r in c["runs"]), "the record shows the check was examined, not vacuous"
    assert c["opportunity"] == 1, "one commit in the window touched src/foo/"


def test_a_run_before_the_window_is_the_baseline_not_a_counted_run(tmp_path):
    repo, art, sha = history(tmp_path)
    rec = _rec(repo, art, since=UNTIL - timedelta(days=3))
    assert [r["sha"] for r in check(rec)["runs"]] == [sha["d3"], sha["d2"], sha["d1"], sha["d0"]]


def test_the_previous_reviews_keep_reason_is_carried_forward_and_shown(tmp_path):
    repo, art, _ = history(tmp_path)
    prior = {"checks": [], "due_for_deletion": [{"check": STEM, "disposition": "keep", "keep_reason": "guards the live incident nexus-77"}]}
    rec = _rec(repo, art, priors=[prior])
    assert rec["due_for_deletion"][0]["previous_keep_reason"] == "guards the live incident nexus-77"
    assert rec["due_for_deletion"][0]["disposition"] == "delete", "the carried reason is shown, not pre-filled as a keep"
    assert "PREVIOUS REVIEW KEPT IT: guards the live incident nexus-77" in cr.render_markdown(rec)


def test_a_class_a_module_the_tool_cannot_clock_is_listed(tmp_path):
    repo, art, _ = history(tmp_path, extra={"scripts/adv_check.py": "# owner: ci\n# guards: nexus-5; src/\n# class: A\nADVISORY = '--advisory'\n"})
    rec = _rec(repo, art)
    assert [u["module"] for u in rec["unclockable"]] == ["scripts/adv_check.py"] and "advisory" in rec["unclockable"][0]["why"]


def test_a_demotion_that_is_not_on_the_reviewed_rev_has_no_clock(tmp_path):
    repo, art, sha = history(tmp_path)
    rec = _rec(repo, art, rev=sha["born"])
    assert rec["checks"] == [] and rec["due_for_deletion"] == [] and rec["totals"]["class_a_with_clock"] == 0


def _registry(*ages: tuple[str, int]) -> str:
    """tests/quarantine.toml text with one entry per ``(nodeid, days old at UNTIL)``, written through the registry's own renderer."""
    out = []
    for n, (nodeid, age) in enumerate(ages):
        since = UNTIL - timedelta(days=age)
        out.append(reg.render(reg.Entry(nodeid, "pytest", since, reg.deadline_for(since), f"nexus-q{n}", "flakes under load", "a" * 64)))
    return "\n".join(out)


def test_a_quarantine_registry_entry_older_than_14_days_is_listed_with_its_deadline_and_bead_and_a_newer_one_is_not(tmp_path):
    registry = _registry(("tests/test_flaky_thing.py::test_flaky", 15), ("tests/test_new_flake.py::test_newflake", 13))
    repo, art, _ = history(tmp_path, registry=registry)
    q = _rec(repo, art)["quarantine"]
    assert q["present"] is True and len(q["all"]) == 2
    (old,) = q["over_14_days"]
    assert old["test"] == "tests/test_flaky_thing.py::test_flaky" and old["age_days"] == 15
    assert old["since"] == "2026-11-13" and old["deadline"] == "2026-11-27" and old["bead"] == "nexus-q0"
    assert [t["age_days"] for t in q["all"]] == [15, 13]
    text = cr.render_markdown(_rec(repo, art))
    assert "tests/test_flaky_thing.py::test_flaky" in text and "deadline 2026-11-27, bead nexus-q0" in text and "test_newflake" not in text.split("## Class-A modules")[0].split("## Quarantined")[1]


def test_an_empty_registry_is_an_empty_list_but_a_missing_one_fails_the_record_loudly(tmp_path, capsys):
    repo, art, _ = history(tmp_path / "empty", registry="")
    assert _rec(repo, art)["quarantine"] == {"registry": "tests/quarantine.toml", "present": False, "all": [], "over_14_days": []}
    repo, art, _ = history(tmp_path / "gone", registry=None)
    argv = ["record", "--repo", str(repo), "--rev", "HEAD", "--since", "2026-09-01", "--until", UNTIL.isoformat(), "--artifacts", str(art)]
    assert cr.main(argv) == 2
    cap = capsys.readouterr()
    assert "tests/quarantine.toml is missing" in cap.err and "empty list would say there is no quarantine" in cap.err and cap.out == ""
    with pytest.raises(cr.ReviewError, match="missing"):
        _rec(repo, art)


def test_a_malformed_registry_fails_the_record_too(tmp_path):
    repo, art, _ = history(tmp_path, registry="[[quarantine]]\nnodeid = 1\n")
    with pytest.raises(cr.ReviewError, match="not a valid registry"):
        _rec(repo, art)


# -- a numeric check -------------------------------------------------------------------------------------------------------


def _numeric_repo(tmp_path, value_at, alarm: int = 5544, demoted: int = 70, fix_day: int | None = None):
    repo, art = tmp_path / "repo", tmp_path / "art"
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "develop")
    mod = "tests/_storage_touch_fan_in.py"
    head = "# owner: ci\n# guards: nexus-2; src/\n# class: A\n"
    commit(repo, {mod: head, "src/a.py": "a\n", "tests/quarantine.toml": ""}, 80, "born")
    commit(repo, {mod: head + "alarm_line_tests = 5544\n"}, demoted, "demote")
    for d in range(demoted, -1, -1):
        s = commit(repo, {"docs/a.md": f"{d}\n", **({"src/a.py": "fixed\n"} if d == fix_day else {})}, d, f"run {d}")
        (art / s).mkdir(parents=True)
        (art / s / "pytest-gate.log").write_text(f"storage_touch value={value_at(d)} baseline=5394 vs_baseline=+1 delta=+1 trend(10)=+9 over 3 runs alarm_line={alarm} verdict: report\n")
    return repo, art


def test_a_numeric_check_with_headroom_and_no_breach_is_due_with_its_headroom_and_trend(tmp_path):
    repo, art = _numeric_repo(tmp_path, lambda d: 5450 - d * 50 // 70)
    rec = cr.build_record(repo, "HEAD", UNTIL - timedelta(days=79), UNTIL, art)
    c = check(rec, "_storage_touch_fan_in")
    assert c["kind"] == "numeric" and c["due"] is True and c["days_since_demotion"] == 70
    (entry,) = rec["due_for_deletion"]
    assert entry["headroom"] == 5544 - 5450 and entry["trend"] == 50 and c["rose"] is True


def test_a_numeric_breach_cleared_by_a_guarded_fix_is_a_catch_and_not_due(tmp_path):
    repo, art = _numeric_repo(tmp_path, lambda d: 5400 if d > 40 else 5600 if d > 30 else 5500, fix_day=30)
    c = check(cr.build_record(repo, "HEAD", UNTIL - timedelta(days=79), UNTIL, art), "_storage_touch_fan_in")
    assert c["catches_total"] == 1 and c["due"] is False and c["catches"][0]["items_resolved"] == ["over the alarm line"]
    assert c["catches"][0]["guard_files"] == ["src/a.py"]


# -- the markdown and the command line ----------------------------------------------------------------------------------------


def test_the_markdown_record_carries_the_due_list_the_thresholds_and_round_trips(tmp_path):
    repo, art, _ = history(tmp_path)
    rec = _rec(repo, art)
    text = cr.render_markdown(rec)
    assert "Due for deletion" in text and "disposition" in text and STEM in text and "80%" in text
    assert cr.load_record_text(text) == json.loads(json.dumps(rec))


def test_record_prints_the_markdown_and_json_format_prints_only_the_record(tmp_path, capsys):
    repo, art, _ = history(tmp_path)
    argv = ["record", "--repo", str(repo), "--rev", "HEAD", "--since", "2026-09-01", "--until", UNTIL.isoformat(), "--artifacts", str(art)]
    assert cr.main(argv) == 0
    assert capsys.readouterr().out.startswith("# Check value review")
    assert cr.main([*argv, "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["totals"]["class_a_with_clock"] == 1


# -- fetching artifacts through gh api -------------------------------------------------------------------------------------


def _zipped(name: str, body: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(name, body)
    return buf.getvalue()


def _gh(calls: list[str], runs: list[dict], total: int | None = None, artifacts: dict[int, list[dict]] | None = None, jobs=False):
    def gh(endpoint: str, binary: bool = False):
        calls.append(endpoint)
        if "/workflows/ci.yml/runs" in endpoint:
            page = int(endpoint.rsplit("page=", 1)[1])
            return {"workflow_runs": runs if page == 1 else [], "total_count": total if total is not None else len(runs)}
        if endpoint.endswith("/artifacts?per_page=100"):
            rid = int(endpoint.split("/runs/")[1].split("/")[0])
            return {"artifacts": (artifacts or {}).get(rid, [{"id": 5, "name": "class-a-items", "expired": False}])}
        if endpoint.endswith("/zip"):
            return _zipped("class-a-items.json", '{"items": []}')
        if endpoint.endswith("/jobs?per_page=100"):
            return {"jobs": [{"id": 9, "name": "pytest-gate"}, {"id": 10, "name": "other"}]} if jobs else {"jobs": []}
        if endpoint.endswith("/jobs/9/logs"):
            return b"noise\n2026-11-1 storage_touch value=5400 baseline=5394 alarm_line=5544 verdict: report\nmore noise\n"
        raise AssertionError(endpoint)
    return gh


def test_fetch_reads_one_call_per_page_lays_artifacts_out_by_sha_and_counts_what_it_could_not_read(tmp_path, capsys):
    runs = [{"id": 11, "head_sha": "1" * 40, "created_at": "2026-11-20T10:00:00Z"}, {"id": 12, "head_sha": "2" * 40, "created_at": "2026-11-21T10:00:00Z"},
            {"id": 13, "head_sha": "3" * 40, "created_at": "2026-11-22T10:00:00Z"}]
    arts = {11: [{"id": 77, "name": "class-a-items", "expired": False}, {"id": 78, "name": "service-jar", "expired": False}],
            12: [{"id": 79, "name": "class-a-items", "expired": True}], 13: []}
    calls: list[str] = []
    out = tmp_path / "out"
    info = cr.fetch_artifacts("o/r", date(2026, 11, 1), UNTIL, out, gh=_gh(calls, runs, artifacts=arts), log_job=None)
    assert (out / ("1" * 40) / "class-a-items.json").read_text() == '{"items": []}'
    assert info["with_data"] == 1 and [e["head_sha"] for e in info["expired"]] == ["2" * 40] and [m["head_sha"] for m in info["missing"]] == ["3" * 40]
    assert len([c for c in calls if "/workflows/ci.yml/runs" in c]) == 1, "a page shorter than per_page ends the walk: no extra call, no polling"
    assert not any(c.endswith("/artifacts/78/zip") or c.endswith("/artifacts/79/zip") for c in calls), "only live class-a-items artifacts are downloaded"
    assert json.loads((out / "_runs.json").read_text())[0]["head_sha"] == "1" * 40 and json.loads((out / "_fetch.json").read_text())["expired"]
    err = capsys.readouterr().err
    assert "1 expired artifact(s)" in err and "1 run(s) with none" in err


def _capped_gh(runs):
    def gh(endpoint: str, binary: bool = False):
        if "/workflows/ci.yml/runs" in endpoint:
            return {"workflow_runs": runs, "total_count": 1400}
        return {"artifacts": []}
    return gh


def test_fetch_flags_a_run_list_at_the_api_cap(tmp_path):
    runs = [{"id": i, "head_sha": f"{i:040x}", "created_at": "2026-11-20T10:00:00Z"} for i in range(100)]
    info = cr.fetch_artifacts("o/r", date(2026, 11, 1), UNTIL, tmp_path / "o", gh=_capped_gh(runs), log_job=None)
    assert info["capped"] is True and info["runs"] == 1000, "ten pages of 100, then it stops and says so"


def test_fetch_reads_the_gate_job_log_for_a_numeric_checks_value_line(tmp_path):
    calls: list[str] = []
    runs = [{"id": 11, "head_sha": "1" * 40, "created_at": "2026-11-20T10:00:00Z"}]
    cr.fetch_artifacts("o/r", date(2026, 11, 1), UNTIL, tmp_path / "o", gh=_gh(calls, runs, jobs=True))
    log = (tmp_path / "o" / ("1" * 40) / "pytest-gate.log").read_text()
    assert log.count("storage_touch value=5400") == 1 and "noise" not in log
    assert any(c.endswith("/jobs/9/logs") for c in calls) and not any(c.endswith("/jobs/10/logs") for c in calls)


def test_run_gh_only_ever_issues_a_get(monkeypatch):
    seen: list[list[str]] = []

    class Done:
        returncode, stdout, stderr = 0, b"{}", b""
    setattr_in(monkeypatch, cr, "subprocess.run", lambda argv, **kw: seen.append(argv) or Done())  # check_review's view only
    cr.run_gh("repos/o/r/actions/runs?per_page=100")
    cr.run_gh("repos/o/r/actions/artifacts/1/zip", binary=True)
    assert seen and all(argv[:4] == ["gh", "api", "--method", "GET"] for argv in seen)
    assert not any(flag in argv for argv in seen for flag in ("-X", "--input", "-f", "-F", "--field", "--raw-field", "POST", "PATCH", "DELETE"))


# -- the header reader ------------------------------------------------------------------------------------------------------

#: (text, (owner, guard ids, guard paths, class) or None). This reader and scripts/check_headers.py (feature/rdr258-check-headers)
#: read these the same way; once check_headers.py is on this tree the second test runs the table over it too.
GRAMMAR_SAMPLES = [
    ("# owner: ci\n# guards: nexus-9, nexus-10; src/a/, tests/b.py\n# class: A\nimport os\n", ("ci", ("nexus-9", "nexus-10"), ("src/a/", "tests/b.py"), "A")),
    ("#!/usr/bin/env python3\n# SPDX-License-Identifier: X\n# owner: engine\n# guards: nexus-3; service/src/main/\n# class: U\n\nx = 1\n",
     ("engine", ("nexus-3",), ("service/src/main/",), "U")),
    ("// owner: engine\n// guards: nexus-3; service/src/main/\n// class: U\n", ("engine", ("nexus-3",), ("service/src/main/",), "U")),
    ("# owner: ci\n# guards: nexus-9 src/a/\n# class: A\n", None),
    ("# owner: ci\n# guards: nexus-9; src/a/\n# class: X\n", None),
    ("# owner: ci\n# class: A\n", None),
    ("import os\n# owner: ci\n# guards: nexus-9; src/a/\n# class: A\n", None),
]


def test_the_header_grammar_samples():
    for text, want in GRAMMAR_SAMPLES:
        header, findings = cr.parse_header(text)
        got = (header.owner, header.guard_ids, header.guard_paths, header.klass) if header else None
        assert got == want and bool(findings) == (want is None), (text, got, findings)


def test_the_header_grammar_agrees_with_check_headers_once_that_module_is_on_this_tree():
    path = Path(__file__).resolve().parents[2] / "scripts" / "check_headers.py"
    if not path.is_file():   # feature/rdr258-check-headers has not landed on this base; the table above is the standing check
        return
    spec = importlib.util.spec_from_file_location("check_headers", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_headers"] = module
    spec.loader.exec_module(module)
    for text, want in GRAMMAR_SAMPLES:
        result = module.parse_header("sample.py", text)
        got = (result.header.owner, result.header.guard_ids, result.header.guard_paths, result.header.klass) if result.header else None
        assert got == want, text
