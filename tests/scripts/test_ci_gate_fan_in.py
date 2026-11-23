# SPDX-License-Identifier: AGPL-3.0-or-later
"""``scripts/ci_gate_fan_in.py``: pytest-gate evaluates every whole-run ceiling, not the first one that fails.

GitHub skips the steps after a failed one, so when the DDL/bytes fan-in, the storage-touch fan-in and the
shard-collection check were three steps, a WAL overage in the first hid a tenant-saving overage in the second
and cost a CI cycle to find. The script runs the three as subprocesses in that order, lets each print its own
verdict untouched, and exits 1 once if any failed, naming all of them in one summary line.

Every case runs the real three checks against hermetic reports and ceilings (the two ceiling flags point them
at tmp files, so a re-measured committed ceiling cannot move these tests).
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from tests import _shard_collection as sc

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "ci_gate_fan_in.py"
SHARDS = 3
IDS = [f"tests/test_x.py::test_{i}" for i in range(12)]

DDL_NAME = "DDL/bytes ratchet over the whole run (fan-in)"
STORAGE_NAME = "Tenant-saving ratchet over the whole run (storage-touch fan-in)"
COLLECTION_NAME = "Every shard split the same collection"


def _load():
    spec = importlib.util.spec_from_file_location("ci_gate_fan_in", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["ci_gate_fan_in"] = module
    spec.loader.exec_module(module)
    return module


def _tally(total: int) -> dict:
    return {"total": total, "toplevel": 0, "nested": total, "by_kind": {}}


def _seed(d: Path, *, provisioned: int = 10, drop_collection_group: int | None = None) -> None:
    """Three shards, each: 40 tests / 2 workers / 600k WAL / 5000 DDL; 100 tests of which `provisioned` minted a
    tenant; a collection report for its group of one shared split."""
    for g in range(1, SHARDS + 1):
        shard = d / f"ddl-report-shard-{g}"
        shard.mkdir(parents=True)
        (shard / "nx-ddl-report.json").write_text(json.dumps({
            "workers": 2, "workers_booted": 2, "tests_run": 40, "workers_lost": 0, "workers_ready_mark_seen": 2,
            "buckets": {"boot": _tally(6), "migration": _tally(2000), "tests": _tally(3000)},
            "total": _tally(5000),
            "wal": {"migration_bytes": 200_000, "tests_bytes": 400_000, "total_bytes": 600_000,
                    "dbsize_growth_bytes": 1, "workers_complete": 2},
        }))
        (shard / "nx-storage-touch-report.json").write_text(json.dumps(
            {"counts": {"tests": 100, "provisioned": provisioned}, "run_id": f"run-{g}", "sweep": True}))
        if g != drop_collection_group:
            rec = sc.report(IDS, IDS[g - 1::SHARDS], g, SHARDS)
            rec["verified"] = True
            (shard / "nx-shard-collection-gw0.json").write_text(json.dumps(rec))


def _ceilings(tmp_path: Path, *, ddl_per_test: float = 75, storage: int = 30) -> tuple[Path, Path]:
    ddl = tmp_path / "ddl_ceiling.toml"
    ddl.write_text(
        "[python_unit]\nbytes_tolerance = 0.05\nddl_tolerance = 0.0\nwal_bytes_per_test = 10000\n"
        f"ddl_per_test = {ddl_per_test}\nmigration_wal_bytes_per_worker = 100000\nmigration_ddl_per_worker = 1000\n")
    touch = tmp_path / "storage_touch_ceiling.toml"
    touch.write_text(f"[python_unit]\nprovisioned_tests = {storage}\ntolerance_tests = 0\n")
    return ddl, touch


def _gate(tmp_path: Path, reports: Path, ddl: Path, touch: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--reports-dir", str(reports), "--suite", "python_unit", "--shards", str(SHARDS),
         "--ddl-ceilings", str(ddl), "--storage-touch-ceilings", str(touch), *extra],
        cwd=REPO, capture_output=True, text=True, timeout=120, check=False)


def _direct(*argv: str) -> str:
    return subprocess.run([sys.executable, *argv], cwd=REPO, capture_output=True, text=True, timeout=120, check=False).stdout


def test_two_over_ceiling_reports_are_both_named_and_the_gate_fails_once(tmp_path: Path) -> None:
    reports = tmp_path / "reports"
    _seed(reports, provisioned=40)  # 120 tenant-minting tests over a ceiling of 30
    ddl, touch = _ceilings(tmp_path, ddl_per_test=74)  # 75 measured over 74
    run = _gate(tmp_path, reports, ddl, touch)
    out = run.stdout
    assert run.returncode == 1, out + run.stderr
    assert "ddl ratchet python_unit fan-in" in out and "ABOVE" in out and "ddl_per_test" in out
    assert "is above the ceiling 30" in out and "provisioned ratchet [python_unit]" in out
    # the third check still ran, and passed, after the first two failed
    assert "shard collection check:" in out and ": ok" in out
    summary = [ln for ln in out.splitlines() if ln.startswith("ci_gate_fan_in:")]
    assert len(summary) == 1, out
    assert DDL_NAME in summary[0] and STORAGE_NAME in summary[0] and COLLECTION_NAME not in summary[0]
    assert "2 of 3" in summary[0]


def test_every_check_passing_exits_zero_and_says_so(tmp_path: Path) -> None:
    reports = tmp_path / "reports"
    _seed(reports)
    ddl, touch = _ceilings(tmp_path)
    run = _gate(tmp_path, reports, ddl, touch)
    assert run.returncode == 0, run.stdout + run.stderr
    assert "within every ceiling" in run.stdout and "status=ok" in run.stdout and "shard collection check:" in run.stdout
    assert [ln for ln in run.stdout.splitlines() if ln.startswith("ci_gate_fan_in:")] == ["ci_gate_fan_in: all 3 checks passed"]


def test_only_the_third_check_failing_still_fails_the_gate(tmp_path: Path) -> None:
    reports = tmp_path / "reports"
    _seed(reports, drop_collection_group=2)  # group 2 wrote no collection report
    ddl, touch = _ceilings(tmp_path)
    run = _gate(tmp_path, reports, ddl, touch)
    assert run.returncode == 1, run.stdout + run.stderr
    assert "no report from group(s) [2]" in run.stdout
    summary = [ln for ln in run.stdout.splitlines() if ln.startswith("ci_gate_fan_in:")]
    assert len(summary) == 1 and COLLECTION_NAME in summary[0] and DDL_NAME not in summary[0] and STORAGE_NAME not in summary[0]
    assert "1 of 3" in summary[0]


def test_each_check_prints_exactly_what_it_prints_on_its_own(tmp_path: Path) -> None:
    """The verdict text of each check is the check's own, byte for byte: the script adds lines around it, never inside."""
    reports = tmp_path / "reports"
    _seed(reports, provisioned=40)
    ddl, touch = _ceilings(tmp_path, ddl_per_test=74)
    out = _gate(tmp_path, reports, ddl, touch).stdout
    alone = [
        _direct("scripts/ddl_ratchet.py", "fan-in", "--suite", "python_unit", "--shards", str(SHARDS), "--report",
                *sorted(str(p) for p in reports.glob("*/nx-ddl-report.json")), "--ceilings", str(ddl)),
        _direct("-m", "tests._storage_touch_fan_in", str(reports), "--suite", "python_unit", "--shards", str(SHARDS),
                "--ceiling-file", str(touch)),
        _direct("-m", "tests._shard_collection", str(reports), "--shards", str(SHARDS)),
    ]
    assert all(alone), alone
    for text in alone:
        assert text in out, (text, out)
    assert out.index(alone[0]) < out.index(alone[1]) < out.index(alone[2]), "the checks run in the old step order"


def test_a_check_that_cannot_start_is_a_failure_and_the_rest_still_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                                       capsys: pytest.CaptureFixture[str]) -> None:
    gate = _load()
    def cannot_exec(argv, **kwargs):  # what subprocess raises for a missing or non-executable interpreter
        raise FileNotFoundError(2, "No such file or directory", argv[0])

    monkeypatch.setattr(gate.subprocess, "run", cannot_exec)
    code = gate.main(["--reports-dir", str(tmp_path), "--suite", "python_unit", "--shards", "1"])
    out = capsys.readouterr().out
    assert code == 1
    assert out.count("could not start (") == 3, out
    assert "3 of 3" in out


def test_the_commands_are_the_ones_the_three_steps_ran(tmp_path: Path) -> None:
    gate = _load()
    reports = tmp_path / "ddl-reports"
    for g in (2, 1):
        (reports / f"ddl-report-shard-{g}").mkdir(parents=True)
        (reports / f"ddl-report-shard-{g}" / "nx-ddl-report.json").write_text("{}")
    py = sys.executable
    args = gate.parse_args(["--reports-dir", str(reports), "--suite", "python_unit", "--shards", "6"])
    checks = gate.build_checks(args)
    assert [c.name for c in checks] == [DDL_NAME, STORAGE_NAME, COLLECTION_NAME]
    found = [str(reports / "ddl-report-shard-1" / "nx-ddl-report.json"), str(reports / "ddl-report-shard-2" / "nx-ddl-report.json")]
    assert checks[0].argv == [py, "scripts/ddl_ratchet.py", "fan-in", "--suite", "python_unit", "--shards", "6", "--report", *found]
    assert checks[1].argv == [py, "-m", "tests._storage_touch_fan_in", str(reports), "--suite", "python_unit", "--shards", "6"]
    assert checks[2].argv == [py, "-m", "tests._shard_collection", str(reports), "--shards", "6"]
    flagged = gate.build_checks(gate.parse_args(
        ["--reports-dir", str(reports), "--suite", "python_unit", "--shards", "6", "--bytes-advisory", "--advisory"]))
    assert flagged[0].argv == [*checks[0].argv, "--advisory", "--bytes-advisory"]
    assert flagged[1].argv == checks[1].argv and flagged[2].argv == checks[2].argv, "the flags belong to the DDL check alone"


def test_no_report_on_disk_hands_the_check_the_literal_pattern_like_the_shell_did(tmp_path: Path) -> None:
    gate = _load()
    args = gate.parse_args(["--reports-dir", str(tmp_path / "none"), "--suite", "python_unit", "--shards", "6"])
    assert gate.build_checks(args)[0].argv[-1] == str(tmp_path / "none" / "*" / "nx-ddl-report.json")


def _groups(out: str) -> dict[str, str]:
    """Each check's text between its ::group:: and ::endgroup:: markers."""
    found: dict[str, str] = {}
    lines = out.splitlines(keepends=True)
    name = None
    for line in lines:
        if line.startswith("::group::"):
            name, found[line[len("::group::"):].rstrip("\n")] = line[len("::group::"):].rstrip("\n"), ""
        elif line.startswith("::endgroup::"):
            name = None
        elif name is not None:
            found[name] += line
    return found


def test_each_check_sits_in_its_own_group_and_a_failure_is_annotated(tmp_path: Path) -> None:
    reports = tmp_path / "reports"
    _seed(reports, provisioned=40)
    ddl, touch = _ceilings(tmp_path, ddl_per_test=74)
    out = _gate(tmp_path, reports, ddl, touch).stdout
    groups = _groups(out)
    assert list(groups) == [DDL_NAME, STORAGE_NAME, COLLECTION_NAME], out
    assert out.count("::endgroup::") == 3
    # the check's own text is untouched inside its group
    assert groups[DDL_NAME] == _direct("scripts/ddl_ratchet.py", "fan-in", "--suite", "python_unit", "--shards", str(SHARDS),
                                       "--report", *sorted(str(p) for p in reports.glob("*/nx-ddl-report.json")),
                                       "--ceilings", str(ddl))
    assert groups[STORAGE_NAME] == _direct("-m", "tests._storage_touch_fan_in", str(reports), "--suite", "python_unit",
                                           "--shards", str(SHARDS), "--ceiling-file", str(touch))
    assert groups[COLLECTION_NAME] == _direct("-m", "tests._shard_collection", str(reports), "--shards", str(SHARDS))
    errors = [ln for ln in out.splitlines() if ln.startswith("::error")]
    assert errors == [f"::error title=pytest-gate::{DDL_NAME} failed (exit 10)",
                      f"::error title=pytest-gate::{STORAGE_NAME} failed (exit 1)"], errors
    # each annotation follows its group's endgroup, so it is not folded away
    assert out.index("::endgroup::") < out.index(errors[0])


def test_a_passing_gate_emits_no_error_annotation(tmp_path: Path) -> None:
    reports = tmp_path / "reports"
    _seed(reports)
    ddl, touch = _ceilings(tmp_path)
    assert "::error" not in _gate(tmp_path, reports, ddl, touch).stdout


def test_a_check_that_runs_too_long_is_failed_and_the_rest_still_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                                    capsys: pytest.CaptureFixture[str]) -> None:
    gate = _load()
    sleeper = tmp_path / "sleeper"
    sleeper.write_text("#!/bin/sh\nexec sleep 30\n")
    sleeper.chmod(0o755)
    monkeypatch.setattr(gate.sys, "executable", str(sleeper))
    monkeypatch.setattr(gate, "CHECK_TIMEOUT_S", 0.3)
    code = gate.main(["--reports-dir", str(tmp_path), "--suite", "python_unit", "--shards", "1"])
    out = capsys.readouterr().out
    assert code == 1
    assert out.count("::error title=pytest-gate::") == 3 and out.count("failed (timed out)") == 3, out
    assert out.count("::endgroup::") == 3, "every check ran, the later ones after the first timed out"
    assert "3 of 3" in out and "(timed out)" in out


def test_the_checks_run_from_the_repo_root_whatever_the_callers_directory(tmp_path: Path) -> None:
    reports = tmp_path / "reports"
    _seed(reports)
    ddl, touch = _ceilings(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    run = subprocess.run(
        [sys.executable, str(SCRIPT), "--reports-dir", str(reports), "--suite", "python_unit", "--shards", str(SHARDS),
         "--ddl-ceilings", str(ddl), "--storage-touch-ceilings", str(touch)],
        cwd=elsewhere, capture_output=True, text=True, timeout=120, check=False)
    assert run.returncode == 0, run.stdout + run.stderr
    assert "all 3 checks passed" in run.stdout


def test_the_script_derives_the_repo_root_from_its_own_path() -> None:
    assert _load().ROOT == REPO
