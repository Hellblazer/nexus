# SPDX-License-Identifier: AGPL-3.0-or-later
"""scripts/red_run_report.py over a small synthetic snapshot and a throwaway git repo (nexus-2jomg.1).

No network, no tenant fixture: the snapshot is files in ``tmp_path`` and the history is a repo
``git init``-ed there. The MVV tolerance math is tested on counts, not on the real snapshot.

Run it with ``NX_TEST_T2_SUBSTRATE=none uv run pytest tests/test_red_run_report.py`` after ``uv sync``:
the conftest refuses to start the engine substrate in a worktree whose service jar is not built, and
this file needs no engine.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from tests._red_run_fixtures import _extract, _job, extraction, history, snapshot  # noqa: F401

import red_run_report as rrr


# --- log parsing ----------------------------------------------------------------------------


def test_parse_log_reads_pytest_java_and_infra() -> None:
    lines = [
        "﻿2026-10-01T10:00:00.1Z FAILED tests/test_x.py::test_a[p q] - ValueError: no\n",
        "2026-10-01T10:00:00.2Z ERROR tests/test_y.py::test_b - boom\n",
        "2026-10-01T10:00:00.3Z FAILED tests/test_x.py::test_a[p q] - ValueError: no\n",
        "2026-10-01T10:00:01Z [ERROR] Failures: \n",
        "2026-10-01T10:00:01Z [ERROR]   dev.nexus.FooTest.bar:664 \n",
        "2026-10-01T10:00:01Z [ERROR] \n",
        "2026-10-01T10:00:01Z [ERROR]   NotATest.after:1 \n",
        "2026-10-01T10:00:02Z ##[error]Unexpected HTTP response: 504\n",
        "2026-10-01T10:00:03Z FAILED tests\\daemon\\test_w.py::test_win - OSError\n",   # Windows path
    ]
    got = rrr.parse_log(lines)
    assert [(t["kind"], t["file"], t["name"]) for t in got["tests"]] == [
        ("pytest", "tests/test_x.py", "test_a[p q]"), ("pytest", "tests/test_y.py", "test_b"),
        ("java", "FooTest", "bar"), ("pytest", "tests/daemon/test_w.py", "test_win")]
    assert got["infra"] is True and got["errors"] == ["Unexpected HTTP response: 504"]
    assert got["evidence"] is True


def test_parse_log_keeps_a_parametrize_id_with_a_dash_and_strips_the_java_outer_class() -> None:
    got = rrr.parse_log(["FAILED tests/t.py::test_a[x - y] - boom\n",
                         "[ERROR] Failures: \n", "[ERROR]   Outer$Inner.method:3 \n"])
    assert [(t["file"], t["name"], t["msg"]) for t in got["tests"]] == [
        ("tests/t.py", "test_a[x - y]", "boom"), ("Outer", "method", "")]


def test_parse_log_sums_failed_and_error_counts_and_reads_failure_sections() -> None:
    got = rrr.parse_log(["==== 3 failed, 5 passed, 2 errors in 4.1s ====\n"])
    assert got["failed_count"] == 5 and got["tests"] == []
    got = rrr.parse_log(["____ TestPre.test_a[x] ____\n", "E       AssertionError: over budget\n",
                         "==== 1 failed in 1.0s ====\n"])
    assert [(t["file"], t["name"], t["msg"]) for t in got["tests"]] == [
        ("", "TestPre.test_a[x]", "AssertionError: over budget")]
    assert got["first_e"] == "AssertionError: over budget"


def test_parse_log_marks_a_bare_exit_status_as_no_evidence() -> None:
    assert rrr.parse_log(["##[error]Process completed with exit code 1.\n"])["evidence"] is False
    assert rrr.parse_log(["##[error]the real-PID tests did not pass\n"])["evidence"] is True


def test_an_infra_signature_counts_only_on_error_annotations() -> None:
    noise = ["UserWarning: PG-bundle self-provisioning failed (failed to download x)\n",
             "WARN Voyage rate limit exceeded, HTTP 500 retrying\n"]
    assert rrr.parse_log(noise)["infra"] is False          # test-output noise is not the failure
    assert rrr.parse_log(["##[error]failed to download x\n"])["infra"] is True


@pytest.mark.parametrize(("labels", "want"), [
    (["ubuntu-latest"], "hosted"), (["ubuntu-24.04-arm"], "hosted"), (["macos-14"], "hosted"),
    (["lab-6c18g"], "self-hosted"), (["self-hosted", "Linux", "X64", "qwen-linux"], "self-hosted"),
    (["hellmini"], "self-hosted"), (["win-release"], "windows")])
def test_venue_from_runner_labels(labels: list[str], want: str) -> None:
    assert rrr.venue(labels) == want


# --- extraction -----------------------------------------------------------------------------


def test_extract_scope_rows_and_coverage(extraction: list[dict]) -> None:
    meta = extraction[0]
    assert meta["t"] == "meta"
    # r12 has no failed job record (dropped); r11 is cancelled (not red)
    assert meta["red_runs"] == 23 and meta["dropped_jobless"] == 1
    assert meta["failed_jobs"] == 24 and meta["failed_jobs_with_log"] == 19
    assert meta["manifest"] is False and meta["final"] is False
    assert meta["gaps"] == 5 and meta["gaps_explained"] == 0       # r1 gate, r5, r10 (empty), r13 (.err), r32
    items = [r for r in extraction if r["t"] == "item"]
    by = {(i["run"], i["item"]): i for i in items}
    test = by[(1, "test_x[a b]")]
    assert (test["kind"], test["file"], test["venue"], test["derived"]) == ("test", "tests/test_a.py", "hosted", False)
    assert test["touched_test"] is True and test["fix_touched_product"] is True
    assert (test["fix_distance"], test["fix_is_fix"], test["fix_table_only"]) == (1, True, False)
    assert (test["path_source"], by[(9, "bar")]["path_source"]) == ("summary", "java")
    assert by[(13, "Windows test set")]["path_source"] == "" and "tb_path" not in test
    assert by[(1, "Verify the pytest suite or its legitimate skip")]["derived"] is True
    assert by[(9, "bar")]["kind"] == "java" and by[(9, "bar")]["venue"] == "self-hosted"
    assert by[(13, "Windows test set")]["venue"] == "windows"
    runs = {r["id"]: r for r in extraction if r["t"] == "run"}
    assert runs[1]["fix_product_commit"] and runs[1]["green_run"] == 2 and runs[1]["range_len"] == 1
    assert runs[3]["rerun_green"] and runs[3]["fix_commits"] == []
    assert runs[24]["unresolved"] and runs[24]["green_run"] is None
    assert by[(24, "m")]["touched_test"] is None                   # never green: no fix signal
    assert runs[1]["event"] == "push"
    commits = {r["sha"]: r for r in extraction if r["t"] == "commit"}
    assert sorted(commits[runs[1]["fix_commits"][0]]["files"]) == ["src/nexus/a.py", "tests/test_a.py"]


def test_a_green_on_a_commit_that_does_not_descend_from_the_red_sha_is_skipped(
        extraction: list[dict]) -> None:
    runs = {r["id"]: r for r in extraction if r["t"] == "run"}
    # r37 is a success on the side branch and the only later CI green after r29..r36 (red at c6)
    assert runs[29]["green_run"] is None and runs[36]["unresolved"] is True


def test_fix_signal_ties_the_failure_to_the_first_commit_touching_the_file(extraction: list[dict]) -> None:
    items = {(i["run"], i["item"]): i for i in extraction if i["t"] == "item"}
    entries = items[(18, "test_entries")]
    assert (entries["fix_distance"], entries["fix_table_only"]) == (1, True)
    old = items[(20, "test_o")]
    assert (old["fix_distance"], old["fix_touched_product"], old["fix_is_fix"]) == (1, False, False)
    foo = items[(22, "bar")]
    assert (foo["touched_test"], foo["fix_touched_product"], foo["fix_is_fix"]) == (True, True, True)
    assert items[(14, "test_flaky")]["touched_test"] is False and items[(14, "test_flaky")]["fix_distance"] is None


def test_labels_name_the_rule_that_moved_each_run(extraction: list[dict]) -> None:
    an = rrr.analyze(extraction)
    got = {rid: (r["category"], r["rules"]) for rid, r in an["runs"].items()}
    assert got[1] == (rrr.PRODUCT, ["product:fix-touched-src"])
    assert got[3] == (rrr.ACCOUNTING, ["accounting:job"])       # accounting wins over the rerun-green
    assert got[5] == (rrr.ACCOUNTING, ["accounting:step"])
    assert got[7] == (rrr.FLAKE, ["flake:infra-signature"])
    assert got[9] == (rrr.UNATTRIBUTED, ["unattributed:resolved-without-test-fix"])   # no commit touched it
    assert got[10] == (rrr.BUILD, ["build:step-name"])
    assert got[13] == (rrr.UNLABELLED, ["unlabelled:no-log"])  # no log: stays visible
    assert got[14] == (rrr.UNATTRIBUTED, ["unattributed:resolved-without-test-fix"])
    assert got[16] == (rrr.STALE, ["stale:windows-venue-failed-count"])
    assert got[17] == (rrr.UNLABELLED, ["unlabelled:no-rule"])   # a log, but no name, count or reason
    assert got[18] == (rrr.ACCOUNTING, ["accounting:fix-table-edit"])
    assert got[20] == (rrr.STALE, ["stale:fix-touched-test"])
    assert got[22] == (rrr.PRODUCT, ["product:fix-touched-src"])         # product on a Java test
    assert got[24] == (rrr.UNATTRIBUTED, ["unattributed:no-verified-green"])
    assert got[25] == (rrr.ACCOUNTING, ["accounting:meta-msg"])          # a named Windows test, "budget"
    assert got[26] == (rrr.FLAKE, ["flake:timing-message"])
    assert got[27] == (rrr.FLAKE, ["flake:rerun-green"])
    assert got[29] == (rrr.HARNESS, ["harness:file"])
    assert got[30] == (rrr.ACCOUNTING, ["accounting:meta-file"])
    assert got[31] == (rrr.ACCOUNTING, ["accounting:meta-msg"])
    assert got[32] == (rrr.FLAKE, ["flake:infra-step"])
    assert got[33] == (rrr.STALE, ["stale:unnamed-test-failures"])
    assert got[36] == (rrr.UNLABELLED, ["unlabelled:no-rule"])  # the signature is not on an ##[error] line


def _item(**kw: object) -> dict:
    base = {"kind": "test", "file": "tests/test_z.py", "item": "test_z",
            "job": "pytest (Python 3.12, shard 1/6)", "step": "Run tests", "msg": "", "infra": False,
            "venue": "hosted", "has_log": True, "evidence": True, "failed_count": 0, "fix_distance": None,
            "fix_table_only": None, "fix_touched_product": None, "fix_is_fix": False,
            "touched_test": None, "derived": False}
    return {**base, **kw}


_RUN = {"rerun_green": False, "unresolved": False}


def test_a_touch_at_any_distance_is_the_fix_because_the_range_ends_at_the_verified_green() -> None:
    far = rrr.label_item(_item(fix_distance=40, touched_test=True, fix_touched_product=False), _RUN)
    assert far == (rrr.STALE, "stale:fix-touched-test")      # an earlier 10-commit window missed d=11..15


def test_a_test_with_no_verified_green_and_no_other_rule_is_unattributed() -> None:
    item = _item()
    assert rrr.label_item(item, {"rerun_green": False, "unresolved": True}) == (
        rrr.UNATTRIBUTED, "unattributed:no-verified-green")
    assert rrr.label_item(item, _RUN) == (rrr.UNATTRIBUTED, "unattributed:resolved-without-test-fix")


def test_a_count_pin_on_an_entries_dict_is_accounting_by_what_it_checks() -> None:
    pin = _item(file="", item="TestRealWindows.test_the_walk_finds_the_uv_entries", venue="windows",
                msg="AssertionError: {'conexus': 18, 'sn': 4}")
    other = _item(file="", item="TestRealWindows.test_x", venue="windows", msg="AssertionError: {0: 2, 1: 3}")
    assert rrr.label_item(pin, _RUN) == (rrr.ACCOUNTING, "accounting:meta-msg")
    assert rrr.label_item(other, _RUN) == (rrr.STALE, "stale:windows-venue")
    assert rrr.COUNT_PIN_RE.search("assert {'a': 1} == {'a': 2}")
    assert not rrr.COUNT_PIN_RE.search("AssertionError: {'a': [1, 2]}")


def test_a_product_fix_needs_a_fix_subject_and_a_table_edit_is_accounting() -> None:
    feat = _item(fix_distance=1, touched_test=True, fix_touched_product=True, fix_is_fix=False)
    fix = _item(fix_distance=1, touched_test=True, fix_touched_product=True, fix_is_fix=True)
    table = _item(fix_distance=1, touched_test=True, fix_table_only=True)
    assert rrr.label_item(feat, _RUN) == (rrr.STALE, "stale:fix-touched-test")    # an in-flight feature
    assert rrr.label_item(fix, _RUN) == (rrr.PRODUCT, "product:fix-touched-src")
    assert rrr.label_item(table, _RUN) == (rrr.ACCOUNTING, "accounting:fix-table-edit")


def test_the_name_regex_no_longer_swallows_product_tests() -> None:
    for stem in ("TaxonomyCoverageTest", "test_hooks_json_verb_release_floor", "test_catalog_walk",
                 "test_manifest_write_many"):
        item = _item(kind="java", file=stem) if stem[0].isupper() else _item(file=f"tests/{stem}.py")
        assert not rrr.META_FILE_RE.search(stem), stem
        assert rrr.label_item(item, _RUN)[0] != rrr.ACCOUNTING, stem
    for stem in ("test_storage_touch_guard", "test_module_seam", "SqlStringLiteralGateTest",
                 "test_wal_ceiling", "test_skip_floor"):
        assert rrr.META_FILE_RE.search(stem), stem


def test_a_test_function_name_and_a_drift_message_can_mark_an_accounting_check() -> None:
    by_name = _item(file="tests/daemon/test_lifecycle_gate.py", item="test_flock_acquire_sites_are_allowlisted")
    drifted = _item(msg="AssertionError: MCP wire schema drifted from the committed snapshot")
    sanctioned = _item(kind="java", file="ExtensionBootstrapSiteConformanceTest",
                       msg="[CREATE EXTENSION string literal outside SANCTIONED_JAVA_FILES]")
    assert rrr.label_item(by_name, _RUN) == (rrr.ACCOUNTING, "accounting:meta-file")
    behaviour = _item(item="test_the_jvm_env_allowlist_does_not_carry_the_password")
    drift_name = _item(kind="java", file="TaxonomyRepositoryTest", item="getDocCountDrift_namesOnlyTopics")
    assert rrr.label_item(behaviour, _RUN)[0] != rrr.ACCOUNTING          # an env allowlist is behaviour
    assert rrr.label_item(drift_name, _RUN)[0] != rrr.ACCOUNTING         # DocCountDrift is a feature
    assert rrr.label_item(drifted, _RUN) == (rrr.ACCOUNTING, "accounting:meta-msg")
    assert rrr.label_item(sanctioned, _RUN) == (rrr.ACCOUNTING, "accounting:meta-msg")


def test_message_rules_for_census_and_timing_come_before_the_file_rule() -> None:
    census = _item(msg="AssertionError: HTTP call sites that neither send the header nor say why")
    timing = _item(file="tests/test_x_census.py", msg="AssertionError: predicate not satisfied within 5.0s")
    assert rrr.label_item(census, _RUN) == (rrr.ACCOUNTING, "accounting:meta-msg")
    assert rrr.label_item(timing, _RUN) == (rrr.FLAKE, "flake:timing-message")


def test_step_items_without_a_name_use_the_first_error_line_then_the_failed_count() -> None:
    step = {"kind": "step", "file": "", "item": "Windows test set", "venue": "windows", "failed_count": 2}
    served = _item(**step, msg="AssertionError: 33 >= 36 right GETs were served while x")
    assert rrr.label_item(served, _RUN) == (rrr.FLAKE, "flake:timing-message")
    over = _item(**step, msg="AssertionError: over budget")
    assert rrr.label_item(over, _RUN) == (rrr.ACCOUNTING, "accounting:meta-msg")
    plain = _item(**step, msg="OSError: WinError 1314")
    assert rrr.label_item(plain, _RUN) == (rrr.STALE, "stale:windows-venue-failed-count")
    hosted = _item(**{**step, "venue": "hosted"}, msg="")
    assert rrr.label_item(hosted, _RUN) == (rrr.STALE, "stale:unnamed-test-failures")


def test_an_infra_signature_labels_only_items_with_no_named_test() -> None:
    step = _item(kind="step", file="", item="Run tests", infra=True)
    named = _item(infra=True, fix_distance=1, touched_test=True, fix_touched_product=False)
    assert rrr.label_item(step, _RUN) == (rrr.FLAKE, "flake:infra-signature")
    assert rrr.label_item(named, _RUN) == (rrr.STALE, "stale:fix-touched-test")


def test_a_named_windows_test_with_no_fix_is_windows_venue() -> None:
    item = _item(file="", item="TestPre.test_x", venue="windows")
    assert rrr.label_item(item, _RUN) == (rrr.STALE, "stale:windows-venue")


def test_a_fileless_test_is_matched_by_its_function_name_not_its_class() -> None:
    pin = _item(file="", item="test_flock_acquire_sites_are_allowlisted", venue="windows")
    cls = _item(file="", item="TestCensus.test_the_exact_spelling_still_matches", venue="windows")
    assert rrr.label_item(pin, _RUN) == (rrr.ACCOUNTING, "accounting:meta-file")
    assert rrr.label_item(cls, _RUN) == (rrr.STALE, "stale:windows-venue")    # a pid census, not a repo one


def test_checks_are_named_and_derived_items_do_not_count(extraction: list[dict]) -> None:
    counts = rrr.check_counts(rrr.analyze(extraction))
    assert counts["storage-touch ratchet"] == 1 and counts["docs_reference_rot"] == 1
    assert counts["a"] == 1 and counts["FooIntegrationTest"] == 1 and counts["Lint src/ with ruff"] == 1
    assert counts["Windows test set"] == 2 and counts["BarTest"] == 1
    assert counts["TestPre"] == 1                      # a fileless Windows test is named by its class
    assert "pytest-gate verdict" not in counts         # r1's gate step is derived from the shard failure


def test_a_floor_step_is_derived_only_when_another_item_shows_what_failed() -> None:
    quiet = {"failed_count": 0, "evidence": False, "infra": False}
    floor = {"kind": "step", "item": "Non-vacuity floor", **quiet}
    silent = {"kind": "step", "item": "Windows test set", **quiet}
    named = {"kind": "test", "item": "t", **quiet}
    only, with_silent, with_named = [dict(floor)], [dict(silent), dict(floor)], [dict(named), dict(floor)]
    for items in (only, with_silent, with_named):
        rrr._mark_derived(items)
    assert only[0]["derived"] is False and with_silent[1]["derived"] is False
    assert with_named[1]["derived"] is True


def test_only_the_final_attempts_failed_jobs_count() -> None:
    jobs = [_job(1, "a", [], attempt=1), _job(2, "a", [], attempt=2),
            _job(3, "b", [], conclusion="success", attempt=2)]
    assert [j["id"] for j in rrr._failed_jobs({"run_attempt": 2}, jobs)] == [2]
    assert [j["id"] for j in rrr._failed_jobs({"run_attempt": 1}, [_job(5, "x", [])])] == [5]


def test_extra_log_roots_are_searched_after_the_snapshot(tmp_path: Path) -> None:
    snap, extra = tmp_path / "snap", tmp_path / "extra"
    (extra / "7").mkdir(parents=True)
    (extra / "7" / "9.log").write_text("FAILED tests/test_q.py::test_q - boom\n")
    assert rrr.read_log(snap, 7, 9) is None
    got = rrr.read_log(snap, 7, 9, [extra])
    assert got is not None and got["extra"] is True and got["tests"][0]["file"] == "tests/test_q.py"


def test_a_run_with_no_jobs_file_stays_visible_and_is_a_gap(
        snapshot: Path, history: tuple[Path, dict[str, str]]) -> None:
    (snapshot / "jobs" / "26.json").unlink()
    recs = _extract(snapshot, history[0], since="2026-10-01", until="2026-10-01")
    assert recs[0]["runs_missing_jobs_file"] == 1 and recs[0]["red_runs"] == 23
    assert recs[0]["gaps"] == 6                      # the five log gaps and r26's missing jobs list
    assert rrr.analyze(recs)["runs"][26]["rules"] == ["unlabelled:jobs-missing"]


def test_extraction_is_deterministic_independent_of_input_order_and_round_trips(
        snapshot: Path, history: tuple[Path, dict[str, str]], tmp_path: Path) -> None:
    first = _extract(snapshot, history[0])
    runs = snapshot / "runs.jsonl"
    runs.write_text("".join(reversed(runs.read_text().splitlines(keepends=True))))
    second = _extract(snapshot, history[0])
    a, b = tmp_path / "a.jsonl.gz", tmp_path / "b.jsonl.gz"
    rrr.write_extraction(first, a)
    rrr.write_extraction(second, b)
    assert a.read_bytes() == b.read_bytes()
    assert rrr.read_extraction(a) == json.loads(json.dumps(first))


def test_scope_options_are_explicit(snapshot: Path, history: tuple[Path, dict[str, str]]) -> None:
    kw = {"since": "2026-10-01", "until": "2026-10-01"}
    # a cancelled run has no failed job record, so widening the conclusions alone adds nothing
    assert _extract(snapshot, history[0], conclusions=("failure", "cancelled"), **kw)[0]["red_runs"] == 23
    wide = _extract(snapshot, history[0], conclusions=("failure", "cancelled"), include_jobless=True, **kw)[0]
    assert wide["red_runs"] == 25 and wide["dropped_jobless"] == 0
    assert _extract(snapshot, history[0], since="2026-10-02", until="2026-10-03")[0]["red_runs"] == 0


# --- git failures are not empty histories ----------------------------------------------------


def test_a_bad_repo_raises_instead_of_reading_as_an_empty_history(snapshot: Path, tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="git"):
        rrr.extract(snapshot, tmp_path / "no-such-repo", since="2026-10-01", until="2026-10-01")
    with pytest.raises(RuntimeError, match="git"):
        rrr.commit_mix(tmp_path / "no-such-repo", "2026-10-01", "2026-10-02")


def test_an_extraction_that_read_no_commit_is_refused_by_the_guard_not_by_git(
        snapshot: Path, history: tuple[Path, dict[str, str]], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rrr.History, "load", lambda self, shas: None)     # git works; no commit is read
    with pytest.raises(RuntimeError, match="no commit was read"):
        _extract(snapshot, history[0])


def test_commit_mix_counts_the_fixture_history(history: tuple[Path, dict[str, str]]) -> None:
    repo, _ = history
    mix = rrr.commit_mix(repo, "2000-01-01", "2037-12-31", ref="origin/develop")
    assert mix["total"] == 7 and mix["product"] == 3        # c1 c2 c6 touch src/ or service/src/main
    assert mix["tests_only"] == 3 and mix.get("ledger_or_ceiling", 0) == 0 and mix["other"] == 1   # c3 c4 c5; c0
    with pytest.raises(RuntimeError, match="no commit"):
        rrr.commit_mix(repo, "2000-01-01", "2000-01-02", ref="origin/develop")


def test_is_table_line_separates_data_from_logic() -> None:
    for line in ("    'b',", '    "src/x.py",', "# a comment", "", "    (1, 2),", "X = 'a'", "]"):
        assert rrr.is_table_line(line), line
    for line in ("    assert x == 1", "def test_a():", "    return 1", "    if y:", "    call(arg)"):
        assert not rrr.is_table_line(line), line


# --- snapshot completeness --------------------------------------------------------------------


def _manifest(snap: Path, tsv: str, jobs_missing: str = "") -> None:
    snap.mkdir(exist_ok=True)
    (snap / "MANIFEST.json").write_text(json.dumps({"jobs_missing": jobs_missing, "red_scope_buckets": "b=1"}))
    (snap / "missing.tsv").write_text(tsv)


def test_a_snapshot_is_final_only_when_every_gap_is_listed_in_the_manifest(tmp_path: Path) -> None:
    gap_404 = {"run": 5, "job": 50, "kind": "log", "steps": 0}
    assert rrr.snapshot_status(tmp_path / "none", [gap_404]) == {
        "manifest": False, "final": False, "gaps": 1, "gaps_explained": 0, "buckets": ""}
    snap = tmp_path / "snap"
    _manifest(snap, "5\t50\tlog\tHTTP 404 gh: HTTP 404\n")
    assert rrr.snapshot_status(snap, [gap_404])["final"] is True
    assert rrr.snapshot_status(snap, [])["final"] is True
    with_steps = {**gap_404, "steps": 3}                       # a job that ran steps should have a log
    unlisted = {"run": 6, "job": 60, "kind": "log", "steps": 0}
    assert rrr.snapshot_status(snap, [with_steps])["final"] is False
    assert rrr.snapshot_status(snap, [gap_404, unlisted]) == {
        "manifest": True, "final": False, "gaps": 2, "gaps_explained": 1, "buckets": "b=1"}
    _manifest(snap, "5\t50\tlog\tHTTP 500 gh: server error\n")
    assert rrr.snapshot_status(snap, [gap_404])["final"] is False      # not a 404


def test_a_run_with_no_jobs_list_is_explained_only_by_the_manifest(tmp_path: Path) -> None:
    gap = {"run": 9, "job": 0, "kind": "jobs", "steps": 0}
    snap = tmp_path / "snap"
    _manifest(snap, "9\t-\tjobs\tgh: Server Error (HTTP 502)\n", jobs_missing="run 9: jobs list HTTP 502")
    assert rrr.snapshot_status(snap, [gap])["final"] is True
    _manifest(snap, "9\t-\tjobs\tgh: Server Error (HTTP 502)\n", jobs_missing="")
    assert rrr.snapshot_status(snap, [gap])["final"] is False


# --- red time, caveat, samples ---------------------------------------------------------------


def test_red_hours_run_from_red_completion_to_the_next_decided_run_completion(extraction: list[dict]) -> None:
    hrs = rrr.red_hours(rrr.analyze(extraction))
    # Each CI red run's stretch lasts from its completion to the completion of the next decided CI
    # run, and every run here completes 30 s after it starts, so a stretch is the minutes between
    # the two start times. In minutes: r1 r3 r5 r7 r14 r17 r18 r20 r26 r27 r29..r32 r36 take 1 each
    # (15 runs), r10 -> r14 takes 4 and r33 -> r36 takes 3: 22 in all.
    stretches = {1: 1, 3: 1, 5: 1, 7: 1, 10: 4, 14: 1, 17: 1, 18: 1, 20: 1, 26: 1, 27: 1, 29: 1, 30: 1,
                 31: 1, 32: 1, 33: 3, 36: 1}
    assert hrs["_total"] == pytest.approx(sum(stretches.values()) / 60, abs=0.006)
    assert hrs["product_defect"] == pytest.approx(1 / 60, abs=0.006)      # r1 alone
    assert hrs["build_ruff"] == pytest.approx(4 / 60, abs=0.006)          # r10 -> r14 is four minutes


def test_the_accounting_caveat_counts_items_fixes_and_product_touching_fixes(extraction: list[dict]) -> None:
    text = rrr.accounting_caveat(rrr.analyze(extraction))
    assert "258-research-15" in text and "accounting items" in text
    assert "followed by a fix commit that touched the failing file" in text
    assert "of all 23 red runs (floor) to" in text and "attributed runs" in text


def test_the_unattributed_caveat_states_the_bracket(extraction: list[dict]) -> None:
    an = rrr.analyze(extraction)
    cats = rrr.category_counts(an)
    n = cats[rrr.UNATTRIBUTED]
    text = rrr.unattributed_caveat(an)
    assert n == 3 and f"unattributed {n} runs" in text
    assert f"flake is {cats[rrr.FLAKE]}..{cats[rrr.FLAKE] + n}" in text
    assert f"stale {cats[rrr.STALE]}..{cats[rrr.STALE] + n}" in text
    assert "product candidates" in text and "unattributed:no-verified-green" in text


def test_sample_is_deterministic_bounded_and_drawn_from_the_rule(extraction: list[dict]) -> None:
    an = rrr.analyze(extraction)
    both = rrr.sample_runs(an, "accounting:meta-msg", 5, 7)
    assert sorted(r["run"] for r in both) == [25, 31] and {r["rule"] for r in both} == {"accounting:meta-msg"}
    assert both[0]["candidates"] == 2 and rrr.sample_runs(an, "no:such-rule", 3, 1) == []
    assert "accounting:meta-msg" in rrr.render_sample(an, "accounting:meta-msg", 2, 7)


def _population(n: int) -> dict:
    runs = {i: {"rules": ["r"], "range_len": 1} for i in range(n)}
    items = {i: [{"rule": "r", "derived": False, "workflow": "CI", "job": "j", "item": f"t{i}", "file": "f",
                  "msg": ""}] for i in range(n)}
    return {"runs": runs, "by_run": items, "commits": {}}


def test_the_sample_depends_on_the_seed_and_only_on_the_seed() -> None:
    an = _population(60)
    draw = lambda seed: [r["run"] for r in rrr.sample_runs(an, "r", 6, seed)]   # noqa: E731
    assert draw(1) == draw(1) and draw(2) == draw(2)
    assert draw(1) != draw(2)                                   # a draw that ignored the seed would not
    assert len(set(draw(1))) == 6 and set(draw(1)) <= set(range(60))
    assert draw(1) == sorted(draw(1))                           # reported in run order


# --- guard-catch seam -------------------------------------------------------------------------


def test_guard_catches_follow_the_mapping(extraction: list[dict]) -> None:
    an = rrr.analyze(extraction)
    # the only fix commit in range for r1 touched src/nexus/a.py and tests/test_a.py
    assert rrr.guard_catches(an, {"a": ["src/nexus/"]}) == {"a": 1}
    assert rrr.guard_catches(an, {"a": ["docs/"]}) == {"a": 0}
    assert rrr.guard_catches(an, {"unknown-check": ["src/"]}) == {}


def test_guard_catches_ignore_ledger_ceiling_and_allowlist_data_files() -> None:
    def an_with(files: list[str]) -> dict:
        return {"items": [{"check": "c", "run": 1, "derived": False}],
                "runs": {1: {"fix_commits": ["abc"]}}, "commits": {"abc": {"files": files}}}

    guards = {"c": ["tests/", "scripts/"]}
    assert rrr.guard_catches(an_with(["tests/ddl_ceiling.toml"]), guards) == {"c": 0}
    assert rrr.guard_catches(an_with(["scripts/file-size-exceptions.txt", "tests/x_allowlist.txt"]),
                             guards) == {"c": 0}
    assert rrr.guard_catches(an_with(["tests/ddl_ceiling.toml", "tests/test_z.py"]), guards) == {"c": 1}
    # a test module that merely has "allowlist" or "pending" in its name is guard code, not a ledger
    assert rrr.guard_catches(an_with(["tests/plans/test_plan_step_tool_allowlist.py"]), guards) == {"c": 1}
    assert rrr.is_ledger_file("docs/wire-contract-pending.md")
    assert not rrr.is_ledger_file("tests/test_pending_jobs.py")


def test_a_fallback_fix_pick_is_unattributed_unless_a_rule_that_ignores_the_fix_commit_labels_it_first() -> None:
    fb = {"fix_distance": 3, "touched_test": True, "fix_touched_product": True, "fix_is_fix": True,
          "fix_pick": "fallback"}
    assert rrr.label_item(_item(**fb), _RUN) == (rrr.UNATTRIBUTED, "unattributed:fix-pick-fallback")
    assert rrr.label_item(_item(**{**fb, "fix_pick": "qualified"}), _RUN) == (rrr.PRODUCT, "product:fix-touched-src")
    pin = _item(**fb, file="", item="TestRealWindows.test_the_walk_finds_the_uv_entries", venue="windows",
                msg="AssertionError: {'conexus': 18, 'sn': 4}")
    assert rrr.label_item(pin, _RUN) == (rrr.ACCOUNTING, "accounting:meta-msg")
    assert rrr.label_item(_item(**fb), {"rerun_green": True, "unresolved": False}) == (rrr.FLAKE, "flake:rerun-green")


def test_the_accounting_caveat_counts_fallback_picks_in_every_category_and_in_the_product_figure() -> None:
    def it(category: str, pick: str, product: bool) -> dict:
        return {"category": category, "derived": False, "fix_distance": 1, "fix_pick": pick,
                "fix_touched_product": product}

    an = {"runs": {1: {"category": rrr.ACCOUNTING}, 2: {"category": rrr.UNATTRIBUTED}},
          "items": [it(rrr.ACCOUNTING, "fallback", True), it(rrr.ACCOUNTING, "qualified", True),
                    it(rrr.ACCOUNTING, "qualified", False), it(rrr.STALE, "fallback", True),
                    it(rrr.UNATTRIBUTED, "fallback", True)]}
    text = rrr.accounting_caveat(an)
    assert "1 of the 3 and 1 of the 2 rest on a fallback pick" in text
    assert f"{{'{rrr.ACCOUNTING}': 1, '{rrr.STALE}': 1, '{rrr.UNATTRIBUTED}': 1}}" in text
