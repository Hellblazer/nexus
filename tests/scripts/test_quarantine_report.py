# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-258 Phase 5 (nexus-2jomg.6): ``scripts/quarantine_report.py`` makes the 14-day rule visible.

It lists each quarantined test's age, deadline and days left; flags overdue entries and exits 1 on any; counts
entries for the nightly workflow; and ``add`` writes entries for nodeids the candidates file's ``flake_infra``
array names, refusing anything else. Every case runs the script as a subprocess, as the workflow does.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "quarantine_report.py"
SHA = "d" * 64
A = "tests/test_alpha.py::test_one"
B = "tests/test_beta.py::test_two"


def _entry(nodeid: str, since: str, deadline: str, bead: str = "nexus-x1") -> str:
    return (
        f'[[quarantine]]\nnodeid = "{nodeid}"\nsince = {since}\ndeadline = {deadline}\n'
        f'bead = "{bead}"\nreason = "flaky on the pool"\nsource = "{SHA}"\n'
    )


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-I", str(_SCRIPT), *args], capture_output=True, text=True, check=False)


@pytest.fixture
def registry(tmp_path: Path) -> Path:
    path = tmp_path / "quarantine.toml"
    path.write_text(_entry(A, "2026-11-27", "2026-12-11") + "\n" + _entry(B, "2026-12-01", "2026-12-15", "nexus-x2"))
    return path


class TestReport:
    def test_empty_registry_reports_zero_quarantined_and_exits_zero(self, tmp_path: Path) -> None:
        path = tmp_path / "q.toml"
        path.write_text("# empty\n")
        r = _run("--registry", str(path), "--today", "2026-12-20")
        assert r.returncode == 0
        assert "0 quarantined" in r.stdout

    def test_lists_age_deadline_and_days_left(self, registry: Path) -> None:
        r = _run("--registry", str(registry), "--today", "2026-12-03")
        assert r.returncode == 0, r.stderr
        line_a = next(ln for ln in r.stdout.splitlines() if A in ln)
        assert "age 6d" in line_a and "deadline 2026-12-11" in line_a and "8 days left" in line_a
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
        line_b = next(ln for ln in r.stdout.splitlines() if B in ln)
        assert "OVERDUE" not in line_b
        assert "1 overdue" in r.stdout

    def test_json_carries_each_entry_and_the_totals(self, registry: Path) -> None:
        r = _run("--registry", str(registry), "--today", "2026-12-12", "--json")
        assert r.returncode == 1
        doc = json.loads(r.stdout)
        assert (doc["today"], doc["count"], doc["overdue"]) == ("2026-12-12", 2, 1)
        a = next(e for e in doc["entries"] if e["nodeid"] == A)
        assert a == {
            "nodeid": A, "since": "2026-11-27", "deadline": "2026-12-11", "bead": "nexus-x1",
            "reason": "flaky on the pool", "source": SHA, "age_days": 15, "days_left": -1, "overdue": True,
        }

    def test_results_join_each_entry_and_a_pass_is_released_by_hand(self, registry: Path, tmp_path: Path) -> None:
        results = tmp_path / "results.json"
        results.write_text(json.dumps({"results": {A: "passed"}}))
        r = _run("--registry", str(registry), "--today", "2026-12-03", "--results", str(results))
        line_a = next(ln for ln in r.stdout.splitlines() if A in ln)
        assert "passed" in line_a and "release by hand" in line_a
        line_b = next(ln for ln in r.stdout.splitlines() if B in ln)
        assert "not run" in line_b
        doc = json.loads(_run("--registry", str(registry), "--today", "2026-12-03", "--results", str(results), "--json").stdout)
        assert {e["nodeid"]: e["result"] for e in doc["entries"]} == {A: "passed", B: "not run"}
        assert registry.read_text().count("[[quarantine]]") == 2  # nothing was released

    def test_expect_all_fails_when_a_listed_test_has_no_result(self, registry: Path, tmp_path: Path) -> None:
        """Non-vacuity for the nightly: a pass over tests that never ran must not read as green."""
        results = tmp_path / "results.json"
        results.write_text(json.dumps({"results": {A: "passed"}}))
        r = _run("--registry", str(registry), "--today", "2026-12-03", "--results", str(results), "--expect-all")
        assert r.returncode == 3
        assert "did not run" in r.stdout and B in r.stdout

    def test_expect_all_passes_when_every_listed_test_has_a_result(self, registry: Path, tmp_path: Path) -> None:
        results = tmp_path / "results.json"
        results.write_text(json.dumps({"results": {A: "passed", B: "failed"}}))
        r = _run("--registry", str(registry), "--today", "2026-12-03", "--results", str(results), "--expect-all")
        assert r.returncode == 0

    def test_overdue_outranks_a_missing_result(self, registry: Path, tmp_path: Path) -> None:
        results = tmp_path / "results.json"
        results.write_text(json.dumps({"results": {}}))
        r = _run("--registry", str(registry), "--today", "2026-12-12", "--results", str(results), "--expect-all")
        assert r.returncode == 1

    def test_expect_all_without_results_is_a_usage_error(self, registry: Path) -> None:
        assert _run("--registry", str(registry), "--expect-all").returncode == 2

    def test_expect_all_on_an_empty_registry_passes(self, tmp_path: Path) -> None:
        path = tmp_path / "q.toml"
        path.write_text("")
        results = tmp_path / "results.json"
        results.write_text(json.dumps({"results": {}}))
        assert _run("--registry", str(path), "--results", str(results), "--expect-all").returncode == 0

    def test_a_missing_results_file_is_an_error_not_a_silent_empty(self, registry: Path, tmp_path: Path) -> None:
        r = _run("--registry", str(registry), "--results", str(tmp_path / "nope.json"))
        assert r.returncode == 2

    def test_a_broken_registry_exits_two_naming_the_problem(self, tmp_path: Path) -> None:
        path = tmp_path / "q.toml"
        path.write_text(_entry(A, "2026-11-27", "2027-01-01"))
        r = _run("--registry", str(path))
        assert r.returncode == 2
        assert "deadline" in r.stderr

    def test_a_missing_registry_file_is_an_error(self, tmp_path: Path) -> None:
        assert _run("--registry", str(tmp_path / "absent.toml")).returncode == 2


class TestCount:
    def test_prints_the_entry_count_alone(self, registry: Path) -> None:
        r = _run("count", "--registry", str(registry))
        assert (r.returncode, r.stdout.strip()) == (0, "2")

    def test_prints_zero_for_the_empty_registry(self, tmp_path: Path) -> None:
        path = tmp_path / "q.toml"
        path.write_text("")
        r = _run("count", "--registry", str(path))
        assert (r.returncode, r.stdout.strip()) == (0, "0")

    def test_a_broken_registry_fails_the_count(self, tmp_path: Path) -> None:
        path = tmp_path / "q.toml"
        path.write_text("[[quarantine\n")
        assert _run("count", "--registry", str(path)).returncode == 2

    def test_runs_in_the_repo_with_the_registry_it_ships(self) -> None:
        r = _run("count")
        assert (r.returncode, r.stdout.strip()) == (0, "0")


def _row(nodeid: str) -> dict:
    return {"nodeid": nodeid, "red_runs": 4, "first_red": "2026-10-02", "last_red": "2026-11-25",
            "venues": ["lab-6c18g", "hosted"], "rule": "flake"}


@pytest.fixture
def candidates(tmp_path: Path) -> Path:
    path = tmp_path / "candidates.json"
    path.write_text(json.dumps({
        "window": {"start": "2026-09-27", "end": "2026-11-26"},
        "sha256": "e" * 64,
        "flake_infra": [_row(A), _row(B)],
        "unattributed": [_row("tests/test_gamma.py::test_three")],
    }))
    return path


class TestAdd:
    def test_writes_an_entry_with_a_checked_deadline_and_the_candidates_hash(self, candidates: Path, tmp_path: Path) -> None:
        path = tmp_path / "q.toml"
        path.write_text("# header\n")
        r = _run("add", "--registry", str(path), "--candidates", str(candidates), "--bead", "nexus-x9",
                 "--reason", "times out", "--since", "2026-11-27", A)
        assert r.returncode == 0, r.stderr
        c = _run("--registry", str(path), "--today", "2026-11-28", "--json")
        (entry,) = json.loads(c.stdout)["entries"]
        assert (entry["nodeid"], entry["deadline"], entry["bead"], entry["days_left"]) == (A, "2026-12-11", "nexus-x9", 13)
        import hashlib
        assert entry["source"] == hashlib.sha256(candidates.read_bytes()).hexdigest()
        assert path.read_text().startswith("# header\n")

    def test_refuses_a_nodeid_the_candidates_file_does_not_name(self, candidates: Path, tmp_path: Path) -> None:
        path = tmp_path / "q.toml"
        path.write_text("")
        r = _run("add", "--registry", str(path), "--candidates", str(candidates), "--bead", "b", "--reason", "r",
                 "tests/test_zzz.py::test_nope")
        assert r.returncode == 2
        assert "not named by the candidates file" in r.stderr
        assert path.read_text() == ""

    def test_refuses_an_unattributed_nodeid_and_says_it_is_sams_to_decide(self, candidates: Path, tmp_path: Path) -> None:
        path = tmp_path / "q.toml"
        path.write_text("")
        r = _run("add", "--registry", str(path), "--candidates", str(candidates), "--bead", "b", "--reason", "r",
                 "tests/test_gamma.py::test_three")
        assert r.returncode == 2
        assert "unattributed" in r.stderr and "Sam" in r.stderr
        assert path.read_text() == ""

    def test_has_no_flag_that_reads_the_unattributed_class(self, candidates: Path, tmp_path: Path) -> None:
        path = tmp_path / "q.toml"
        path.write_text("")
        r = _run("add", "--registry", str(path), "--candidates", str(candidates), "--bead", "b", "--reason", "r",
                 "--include-unattributed", "tests/test_gamma.py::test_three")
        assert r.returncode == 2
        assert path.read_text() == ""

    def test_one_refused_nodeid_writes_nothing_for_the_others(self, candidates: Path, tmp_path: Path) -> None:
        path = tmp_path / "q.toml"
        path.write_text("")
        r = _run("add", "--registry", str(path), "--candidates", str(candidates), "--bead", "b", "--reason", "r",
                 A, "tests/test_gamma.py::test_three")
        assert r.returncode == 2
        assert path.read_text() == ""

    def test_requires_a_bead_and_a_reason(self, candidates: Path, tmp_path: Path) -> None:
        path = tmp_path / "q.toml"
        path.write_text("")
        r = _run("add", "--registry", str(path), "--candidates", str(candidates), A)
        assert r.returncode == 2
        assert path.read_text() == ""
