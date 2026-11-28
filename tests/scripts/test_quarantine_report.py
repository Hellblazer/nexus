# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-258 Phase 5 (nexus-2jomg.6): ``scripts/quarantine_report.py`` makes the 14-day rule visible.

It lists each quarantined test's age, deadline and days left, flags overdue entries and exits 1 on any, joins the
junit of the nightly's passes ("passed N of N"), selects entries by kind for the workflow, classifies a runner's
exit code for one pass, and ``add`` writes entries for nodeids the candidates file's ``candidates`` array names,
refusing anything else. Every case runs the script as a subprocess, as the workflow does.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "quarantine_report.py"
SHA = "d" * 64
A = "tests/test_alpha.py::test_one"
B = "tests/test_beta.py::test_two"
J = "ChashVectorConcurrencyTest::concurrentChashAndVectorUpserts_noServerErrors"


def _entry(nodeid: str, since: str, deadline: str, bead: str = "nexus-x1", kind: str = "pytest") -> str:
    return (
        f'[[quarantine]]\nnodeid = "{nodeid}"\nkind = "{kind}"\nsince = {since}\ndeadline = {deadline}\n'
        f'bead = "{bead}"\nreason = "flaky on the pool"\nsource = "{SHA}"\n'
    )


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-I", str(_SCRIPT), *args], capture_output=True, text=True, check=False)


def _junit(path: Path, cases: list[tuple[str, str, str]]) -> Path:
    body = ""
    for classname, name, outcome in cases:
        inner = {"failed": "<failure/>", "skipped": "<skipped/>", "passed": ""}[outcome]
        body += f'<testcase classname="{classname}" name="{name}">{inner}</testcase>'
    path.write_text(f'<?xml version="1.0"?><testsuites><testsuite name="s">{body}</testsuite></testsuites>')
    return path


@pytest.fixture
def registry(tmp_path: Path) -> Path:
    path = tmp_path / "quarantine.toml"
    path.write_text(_entry(A, "2026-11-27", "2026-12-11") + "\n" + _entry(B, "2026-12-01", "2026-12-15", "nexus-x2"))
    return path


A_CASE = ("tests.test_alpha", "test_one")
B_CASE = ("tests.test_beta", "test_two")


class TestReport:
    def test_empty_registry_reports_zero_quarantined_and_exits_zero(self, tmp_path: Path) -> None:
        path = tmp_path / "q.toml"
        path.write_text("# empty\n")
        r = _run("--registry", str(path), "--today", "2026-12-20")
        assert r.returncode == 0
        assert "0 quarantined" in r.stdout

    def test_lists_kind_age_deadline_and_days_left(self, registry: Path) -> None:
        r = _run("--registry", str(registry), "--today", "2026-12-03")
        assert r.returncode == 0, r.stderr
        line_a = next(ln for ln in r.stdout.splitlines() if A in ln)
        assert "(pytest)" in line_a and "age 6d" in line_a and "deadline 2026-12-11" in line_a and "8 days left" in line_a
        line_b = next(ln for ln in r.stdout.splitlines() if B in ln)
        assert "age 2d" in line_b and "12 days left" in line_b and "nexus-x2" in line_b
        assert "OVERDUE" not in r.stdout
        assert "2 quarantined" in r.stdout

    def test_the_deadline_day_is_not_overdue(self, registry: Path) -> None:
        r = _run("--registry", str(registry), "--today", "2026-12-11")
        assert r.returncode == 0
        assert "0 days left" in r.stdout

    def test_an_overdue_entry_is_flagged_and_exits_nonzero(self, registry: Path) -> None:
        r = _run("--registry", str(registry), "--today", "2026-12-12")
        assert r.returncode == 1
        line_a = next(ln for ln in r.stdout.splitlines() if A in ln)
        assert "OVERDUE" in line_a and "1 days overdue" in line_a
        assert "OVERDUE" not in next(ln for ln in r.stdout.splitlines() if B in ln)
        assert "1 overdue" in r.stdout

    def test_json_carries_each_entry_and_the_totals(self, registry: Path) -> None:
        r = _run("--registry", str(registry), "--today", "2026-12-12", "--json")
        assert r.returncode == 1
        doc = json.loads(r.stdout)
        assert (doc["today"], doc["count"], doc["overdue"]) == ("2026-12-12", 2, 1)
        a = next(e for e in doc["entries"] if e["nodeid"] == A)
        assert a == {
            "nodeid": A, "kind": "pytest", "since": "2026-11-27", "deadline": "2026-12-11", "bead": "nexus-x1",
            "reason": "flaky on the pool", "source": SHA, "age_days": 15, "days_left": -1, "overdue": True,
        }

    def test_a_broken_registry_exits_two_naming_the_problem(self, tmp_path: Path) -> None:
        path = tmp_path / "q.toml"
        path.write_text(_entry(A, "2026-11-27", "2027-01-01"))
        r = _run("--registry", str(path))
        assert r.returncode == 2
        assert "deadline" in r.stderr

    def test_a_missing_registry_file_is_an_error(self, tmp_path: Path) -> None:
        assert _run("--registry", str(tmp_path / "absent.toml")).returncode == 2

    def test_runs_in_the_repo_with_the_registry_it_ships(self) -> None:
        r = _run("--today", "2026-11-27")
        assert r.returncode == 0, r.stderr


class TestJunit:
    def test_each_entry_reports_passed_n_of_n_and_failed_k_of_n_over_every_junit_given(self, registry: Path, tmp_path: Path) -> None:
        j1 = _junit(tmp_path / "p1.xml", [(*A_CASE, "passed"), (*B_CASE, "failed")])
        j2 = _junit(tmp_path / "p2.xml", [(*A_CASE, "passed"), (*B_CASE, "passed")])
        r = _run("--registry", str(registry), "--today", "2026-12-03", "--junit", str(j1), "--junit", str(j2))
        line_a = next(ln for ln in r.stdout.splitlines() if A in ln)
        assert "[passed 2 of 2]" in line_a
        line_b = next(ln for ln in r.stdout.splitlines() if B in ln)
        assert "passed 1 of 2, failed 1 of 2" in line_b

    def test_a_pass_never_suggests_a_release(self, registry: Path, tmp_path: Path) -> None:
        j = _junit(tmp_path / "p.xml", [(*A_CASE, "passed"), (*B_CASE, "passed")])
        r = _run("--registry", str(registry), "--today", "2026-12-03", "--junit", str(j))
        assert "release" not in r.stdout.lower()
        assert registry.read_text().count("[[quarantine]]") == 2

    def test_a_directory_of_junit_is_read(self, registry: Path, tmp_path: Path) -> None:
        d = tmp_path / "results"
        d.mkdir()
        _junit(d / "pytest-1.xml", [(*A_CASE, "passed")])
        _junit(d / "pytest-2.xml", [(*A_CASE, "failed")])
        r = _run("--registry", str(registry), "--today", "2026-12-03", "--junit", str(d), "--json")
        a = next(e for e in json.loads(r.stdout)["entries"] if e["nodeid"] == A)
        assert (a["passed"], a["failed"], a["skipped"], a["runs"], a["result"]) == (1, 1, 0, 2, "passed 1 of 2, failed 1 of 2")

    def test_a_java_entry_is_found_by_class_and_method(self, tmp_path: Path) -> None:
        path = tmp_path / "q.toml"
        path.write_text(_entry(J, "2026-11-27", "2026-12-11", kind="java"))
        j = _junit(tmp_path / "TEST-x.xml", [("dev.nexus.service.ChashVectorConcurrencyTest", "concurrentChashAndVectorUpserts_noServerErrors()", "passed")])
        r = _run("--registry", str(path), "--today", "2026-12-03", "--junit", str(j))
        assert "passed 1 of 1" in r.stdout

    def test_a_missing_junit_file_is_an_error(self, registry: Path, tmp_path: Path) -> None:
        assert _run("--registry", str(registry), "--junit", str(tmp_path / "nope.xml")).returncode == 2


class TestExpectAll:
    """Non-vacuity for the nightly: a pass over tests that never ran, or only skipped, must not read as green."""

    def test_fails_when_a_listed_test_has_no_testcase(self, registry: Path, tmp_path: Path) -> None:
        j = _junit(tmp_path / "p.xml", [(*A_CASE, "passed")])
        r = _run("--registry", str(registry), "--today", "2026-12-03", "--junit", str(j), "--expect-all")
        assert r.returncode == 3
        assert "not examined" in r.stdout and B in r.stdout

    def test_fails_when_a_listed_test_only_skipped(self, registry: Path, tmp_path: Path) -> None:
        j = _junit(tmp_path / "p.xml", [(*A_CASE, "passed"), (*B_CASE, "skipped"), (*B_CASE, "skipped")])
        r = _run("--registry", str(registry), "--today", "2026-12-03", "--junit", str(j), "--expect-all")
        assert r.returncode == 3
        assert "skipped 2 of 2 (not examined)" in r.stdout

    def test_one_examining_run_among_skips_is_enough(self, registry: Path, tmp_path: Path) -> None:
        j = _junit(tmp_path / "p.xml", [(*A_CASE, "passed"), (*B_CASE, "skipped"), (*B_CASE, "failed")])
        assert _run("--registry", str(registry), "--today", "2026-12-03", "--junit", str(j), "--expect-all").returncode == 0

    def test_overdue_outranks_a_missing_result(self, registry: Path, tmp_path: Path) -> None:
        j = _junit(tmp_path / "p.xml", [])
        assert _run("--registry", str(registry), "--today", "2026-12-12", "--junit", str(j), "--expect-all").returncode == 1

    def test_without_junit_is_a_usage_error(self, registry: Path) -> None:
        assert _run("--registry", str(registry), "--expect-all").returncode == 2

    def test_an_empty_registry_passes(self, tmp_path: Path) -> None:
        path = tmp_path / "q.toml"
        path.write_text("")
        j = _junit(tmp_path / "p.xml", [])
        assert _run("--registry", str(path), "--junit", str(j), "--expect-all").returncode == 0


class TestSelect:
    @pytest.fixture
    def mixed(self, tmp_path: Path) -> Path:
        path = tmp_path / "q.toml"
        path.write_text(_entry(A, "2026-11-27", "2026-12-11") + "\n" + _entry(J, "2026-11-27", "2026-12-11", kind="java"))
        return path

    def test_lists_the_pytest_nodeids(self, mixed: Path) -> None:
        assert _run("select", "--kind", "pytest", "--registry", str(mixed)).stdout.split() == [A]

    def test_prints_the_surefire_selector_for_java(self, mixed: Path) -> None:
        r = _run("select", "--kind", "java", "--format", "surefire", "--registry", str(mixed))
        assert r.stdout.strip() == "ChashVectorConcurrencyTest#concurrentChashAndVectorUpserts_noServerErrors"

    def test_prints_the_surefire_exclusion_for_java(self, mixed: Path) -> None:
        r = _run("select", "--kind", "java", "--format", "surefire-exclude", "--registry", str(mixed))
        assert r.stdout.strip() == "!ChashVectorConcurrencyTest#concurrentChashAndVectorUpserts_noServerErrors"

    def test_prints_nothing_for_a_kind_with_no_entry(self, registry: Path) -> None:
        r = _run("select", "--kind", "java", "--registry", str(registry))
        assert (r.returncode, r.stdout) == (0, "")

    def test_a_broken_registry_fails_the_select(self, tmp_path: Path) -> None:
        path = tmp_path / "q.toml"
        path.write_text("[[quarantine\n")
        assert _run("select", "--kind", "pytest", "--registry", str(path)).returncode == 2


class TestClassify:
    def test_exit_0_is_acceptable(self, tmp_path: Path) -> None:
        j = _junit(tmp_path / "p.xml", [("c", "n", "passed")])
        assert _run("classify", "--rc", "0", "--junit", str(j)).returncode == 0

    def test_exit_1_with_a_failed_testcase_is_acceptable(self, tmp_path: Path) -> None:
        j = _junit(tmp_path / "p.xml", [("c", "n", "failed")])
        r = _run("classify", "--rc", "1", "--junit", str(j))
        assert r.returncode == 0 and "quarantined tests failed" in r.stdout

    def test_exit_1_without_a_failed_testcase_is_a_guard_verdict_and_red(self, tmp_path: Path) -> None:
        j = _junit(tmp_path / "p.xml", [("c", "n", "passed")])
        r = _run("classify", "--rc", "1", "--junit", str(j))
        assert r.returncode == 1 and "guard" in r.stdout

    def test_exit_1_with_no_junit_at_all_is_red(self, tmp_path: Path) -> None:
        assert _run("classify", "--rc", "1", "--junit", str(tmp_path / "never-written.xml")).returncode == 1

    @pytest.mark.parametrize("rc", ["2", "3", "4", "5", "137"])
    def test_every_other_exit_is_red(self, tmp_path: Path, rc: str) -> None:
        j = _junit(tmp_path / "p.xml", [("c", "n", "failed")])
        assert _run("classify", "--rc", rc, "--junit", str(j)).returncode == 1

    def test_several_junit_files_are_read_together(self, tmp_path: Path) -> None:
        a = _junit(tmp_path / "a.xml", [("c", "n", "passed")])
        b = _junit(tmp_path / "b.xml", [("c", "m", "failed")])
        assert _run("classify", "--rc", "1", "--junit", str(a), "--junit", str(b)).returncode == 0


def _row(nodeid: str, kind: str) -> dict:
    return {"nodeid": nodeid, "kind": kind, "red_runs": 4, "first_red": "2026-10-02", "last_red": "2026-11-25",
            "venues": ["self-hosted"], "rule": "flake:timing-message", "run_ids": 4}


@pytest.fixture
def candidates(tmp_path: Path) -> Path:
    path = tmp_path / "candidates.json"
    path.write_text(json.dumps({
        "header": {"window": ["2026-09-23", "2026-11-23"], "archive_sha256": "1" * 64, "extraction_sha256": "2" * 64,
                   "script_commit": "3" * 40, "candidate_tests": 2, "unattributed_tests": 1},
        "candidates": [_row(A, "test"), _row(J, "java")],
        "unattributed": [_row(B, "test")],
    }))
    return path


_ADD = ("--today", "2026-11-27")


class TestAdd:
    def test_writes_entries_with_kind_a_checked_deadline_and_the_candidates_hash(self, candidates: Path, tmp_path: Path) -> None:
        path = tmp_path / "q.toml"
        path.write_text("# header\n")
        r = _run("add", *_ADD, "--registry", str(path), "--candidates", str(candidates), "--bead", "nexus-x9", "--reason", "times out", A, J)
        assert r.returncode == 0, r.stderr
        doc = json.loads(_run("--registry", str(path), "--today", "2026-11-28", "--json").stdout)
        by = {e["nodeid"]: e for e in doc["entries"]}
        assert (by[A]["kind"], by[J]["kind"]) == ("pytest", "java")
        assert (by[A]["deadline"], by[A]["bead"], by[A]["days_left"]) == ("2026-12-11", "nexus-x9", 13)
        assert by[A]["source"] == hashlib.sha256(candidates.read_bytes()).hexdigest()
        assert path.read_text().startswith("# header\n")

    @pytest.mark.parametrize("order", ["before", "after"])
    def test_registry_given_before_or_after_the_subcommand_is_the_one_written(self, candidates: Path, tmp_path: Path, order: str) -> None:
        path = tmp_path / "elsewhere.toml"
        path.write_text("")
        common = [*_ADD, "--candidates", str(candidates), "--bead", "b", "--reason", "r", A]
        r = _run("--registry", str(path), "add", *common) if order == "before" else _run("add", "--registry", str(path), *common)
        assert r.returncode == 0, r.stderr
        assert path.read_text().count("[[quarantine]]") == 1

    def test_refuses_a_nodeid_the_candidates_file_does_not_name(self, candidates: Path, tmp_path: Path) -> None:
        path = tmp_path / "q.toml"
        path.write_text("")
        r = _run("add", *_ADD, "--registry", str(path), "--candidates", str(candidates), "--bead", "b", "--reason", "r", "tests/test_zzz.py::test_nope")
        assert r.returncode == 2 and "not named by the candidates file" in r.stderr
        assert path.read_text() == ""

    def test_refuses_an_unattributed_nodeid_without_a_review(self, candidates: Path, tmp_path: Path) -> None:
        path = tmp_path / "q.toml"
        path.write_text("")
        r = _run("add", *_ADD, "--registry", str(path), "--candidates", str(candidates), "--bead", "b", "--reason", "r", B)
        assert r.returncode == 2 and "unattributed" in r.stderr and "--review" in r.stderr
        assert path.read_text() == ""

    def test_accepts_an_unattributed_nodeid_with_a_review_and_keeps_the_ref(self, candidates: Path, tmp_path: Path) -> None:
        path = tmp_path / "q.toml"
        path.write_text("")
        r = _run("add", *_ADD, "--registry", str(path), "--candidates", str(candidates), "--bead", "b", "--reason", "confirmed flaky",
                 "--review", "2026-12-11 review item 4", B)
        assert r.returncode == 0, r.stderr
        (entry,) = json.loads(_run("--registry", str(path), "--today", "2026-11-28", "--json").stdout)["entries"]
        assert entry["nodeid"] == B and "2026-12-11 review item 4" in entry["reason"]

    def test_a_review_with_two_nodeids_writes_nothing(self, candidates: Path, tmp_path: Path) -> None:
        path = tmp_path / "q.toml"
        path.write_text("")
        r = _run("add", *_ADD, "--registry", str(path), "--candidates", str(candidates), "--bead", "b", "--reason", "r", "--review", "x", B, A)
        assert r.returncode == 2
        assert path.read_text() == ""

    def test_has_no_flag_that_reads_the_unattributed_class_wholesale(self, candidates: Path, tmp_path: Path) -> None:
        path = tmp_path / "q.toml"
        path.write_text("")
        r = _run("add", *_ADD, "--registry", str(path), "--candidates", str(candidates), "--bead", "b", "--reason", "r", "--include-unattributed", B)
        assert r.returncode == 2
        assert path.read_text() == ""

    def test_one_refused_nodeid_writes_nothing_for_the_others(self, candidates: Path, tmp_path: Path) -> None:
        path = tmp_path / "q.toml"
        path.write_text("")
        r = _run("add", *_ADD, "--registry", str(path), "--candidates", str(candidates), "--bead", "b", "--reason", "r", A, B)
        assert r.returncode == 2
        assert path.read_text() == ""

    def test_requires_a_bead_and_a_reason(self, candidates: Path, tmp_path: Path) -> None:
        path = tmp_path / "q.toml"
        path.write_text("")
        assert _run("add", *_ADD, "--registry", str(path), "--candidates", str(candidates), A).returncode == 2
        assert path.read_text() == ""

    @pytest.mark.parametrize(("since", "why"), [("2026-11-28", "future"), ("2026-11-20", "backdated")])
    def test_refuses_a_since_in_the_future_or_more_than_a_day_back(self, candidates: Path, tmp_path: Path, since: str, why: str) -> None:
        path = tmp_path / "q.toml"
        path.write_text("")
        r = _run("add", *_ADD, "--since", since, "--registry", str(path), "--candidates", str(candidates), "--bead", "b", "--reason", "r", A)
        assert r.returncode == 2 and why in r.stderr
        assert path.read_text() == ""

    def test_since_defaults_to_today_in_utc_not_the_local_date(self) -> None:
        from datetime import datetime, timezone

        from tests import _quarantine_registry as reg

        assert reg.today_utc() == datetime.now(timezone.utc).date()
