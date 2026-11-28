# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-258 Phase 1 step 1 (nexus-2jomg.2): the storage-touch ratchet is a report-mode check with an alarm line.

Sam's ruling, 2026-11-27: the check fails only above 5544 (the 5394 baseline plus 150); every run reports its value,
the change since the previous run and a 10-run trend. These tests run the real fan-in (``python -m
tests._storage_touch_fan_in``) over hermetic shard reports and ceiling files:

* below the baseline and between the baseline and the alarm line it reports and exits 0 (the second with a warning);
* above the alarm line it fails, naming the line; exactly on the line it passes;
* the delta and the trend come from the history file, which keeps ten runs, replaces a re-run of one commit and
  survives a corrupt file;
* a ceiling file with no alarm line keeps the old rule (the baseline and its tolerance gate), and a malformed alarm
  line is refused, not ignored;
* the committed ceiling file carries Sam's line, and the gate script and the workflow carry the history through.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
import yaml

from tests import _storage_touch_fan_in as fan

REPO = Path(__file__).resolve().parents[2]
CEILINGS = REPO / "tests" / "storage_touch_ceiling.toml"
CI_YML = REPO / ".github" / "workflows" / "ci.yml"
SHARDS = 3


def _reports(root: Path, total: int) -> Path:
    """Three whole-run shard reports whose ``provisioned`` sum to *total*."""
    root.mkdir(parents=True, exist_ok=True)
    share, extra = divmod(total, SHARDS)
    for i in range(SHARDS):
        shard = root / f"shard{i}"
        shard.mkdir(exist_ok=True)
        (shard / fan.REPORT_NAME).write_text(json.dumps({
            "counts": {"tests": 20_000, "provisioned": share + (extra if i == 0 else 0)}, "run_id": f"run-{i}", "sweep": True,
        }))
    return root


def _ceiling(tmp_path: Path, *, ceiling: int = 5394, tolerance: int = 0, alarm: str | None = "5544") -> Path:
    path = tmp_path / "ceiling.toml"
    alarm_line = f"alarm_line_tests = {alarm}\n" if alarm is not None else ""
    path.write_text(f"[python_unit]\nprovisioned_tests = {ceiling}\ntolerance_tests = {tolerance}\n{alarm_line}")
    return path


def _run(tmp_path: Path, total: int, *extra: str, alarm: str | None = "5544", name: str = "reports") -> subprocess.CompletedProcess[str]:
    reports = _reports(tmp_path / name, total)
    return subprocess.run(
        [sys.executable, "-m", "tests._storage_touch_fan_in", str(reports), "--suite", "python_unit", "--shards", str(SHARDS),
         "--ceiling-file", str(_ceiling(tmp_path, alarm=alarm)), *extra],
        cwd=REPO, capture_output=True, text=True, check=False,
    )


def _report_line(proc: subprocess.CompletedProcess[str]) -> str:
    return next(ln for ln in proc.stdout.splitlines() if ln.startswith("storage_touch value="))


def test_a_count_at_or_below_the_baseline_reports_ok(tmp_path):
    proc = _run(tmp_path, 5394)
    assert proc.returncode == 0, proc.stdout
    line = _report_line(proc)
    assert "value=5394 baseline=5394 vs_baseline=+0" in line and line.endswith("alarm_line=5544 verdict: ok")
    assert "::warning" not in proc.stdout
    lower = _run(tmp_path, 5380, name="lower")
    assert lower.returncode == 0 and "verdict: ok" in _report_line(lower)
    assert "the ceiling can be lowered to 5380" in lower.stdout


def test_a_count_between_the_baseline_and_the_alarm_line_reports_and_passes(tmp_path):
    proc = _run(tmp_path, 5401)
    assert proc.returncode == 0, proc.stdout
    assert "status=report" in proc.stdout
    assert "value=5401 baseline=5394 vs_baseline=+7" in _report_line(proc) and _report_line(proc).endswith("verdict: report")
    assert "::warning title=storage-touch::provisioned=5401 is 7 above the baseline 5394 and within the alarm line 5544" in proc.stdout


def test_a_count_exactly_on_the_alarm_line_passes_and_one_above_fails(tmp_path):
    on_line = _run(tmp_path, 5544)
    assert on_line.returncode == 0, on_line.stdout
    assert _report_line(on_line).endswith("verdict: report")
    over = _run(tmp_path, 5545, name="over")
    assert over.returncode == 1
    assert "status=over" in over.stdout and _report_line(over).endswith("verdict: fail")
    assert "above the alarm line 5544" in over.stdout and "baseline 5394, +151" in over.stdout


def test_an_rdr_phase_of_untouched_tenant_tests_breaches_the_line(tmp_path):
    """The RDR-243 Phase 1 shape: 223 tenant tests landed at once, unconsolidated."""
    proc = _run(tmp_path, 5394 + 223)
    assert proc.returncode == 1
    assert "value=5617 baseline=5394 vs_baseline=+223" in _report_line(proc)


def test_without_an_alarm_line_the_baseline_and_its_tolerance_still_gate(tmp_path):
    ok = _run(tmp_path, 5394, alarm=None)
    assert ok.returncode == 0 and _report_line(ok).endswith("verdict: ok")
    over = _run(tmp_path, 5395, alarm=None, name="over")
    assert over.returncode == 1 and "above the ceiling 5394" in over.stdout and _report_line(over).endswith("verdict: fail")


@pytest.mark.parametrize("alarm", ["5393", "true", "5544.5", '"5544"', "0"])
def test_a_malformed_or_too_low_alarm_line_is_refused(tmp_path, alarm):
    proc = _run(tmp_path, 5394, alarm=alarm)
    assert proc.returncode == 1
    assert "status=unreadable" in proc.stdout and "cannot read the alarm line" in proc.stdout


def test_the_alarm_line_may_not_sit_below_baseline_plus_tolerance(tmp_path):
    path = _ceiling(tmp_path, ceiling=5394, tolerance=5, alarm="5398")
    with pytest.raises(ValueError, match="below provisioned_tests"):
        fan._alarm_line(path, "python_unit", 5394, 5)
    assert fan._alarm_line(_ceiling(tmp_path, tolerance=5, alarm="5399"), "python_unit", 5394, 5) == 5399
    assert fan._alarm_line(_ceiling(tmp_path, alarm=None), "python_unit", 5394, 0) is None


# ── the delta and the trend ────────────────────────────────────────────────


def test_the_history_gives_the_delta_since_the_previous_run_and_the_trend(tmp_path):
    history = tmp_path / "hist" / "history.json"
    lines = []
    for i, total in enumerate((5394, 5401, 5410)):
        proc = _run(tmp_path, total, "--history", str(history), "--label", f"sha{i}", name=f"r{i}")
        assert proc.returncode == 0, proc.stdout
        lines.append(_report_line(proc))
    assert "delta=n/a trend(10)=n/a (no earlier run recorded)" in lines[0]
    assert "delta=+7 trend(10)=+7 over 2 runs" in lines[1]
    assert "delta=+9 trend(10)=+16 over 3 runs" in lines[2]
    assert [r["provisioned"] for r in json.loads(history.read_text())] == [5394, 5401, 5410]


def test_the_history_keeps_ten_runs_and_the_trend_spans_them(tmp_path):
    history = tmp_path / "history.json"
    for i in range(13):
        proc = _run(tmp_path, 5400 + i, "--history", str(history), "--label", f"sha{i}", name=f"r{i}")
    assert [r["label"] for r in json.loads(history.read_text())] == [f"sha{i}" for i in range(3, 13)]
    assert "delta=+1 trend(10)=+9 over 10 runs" in _report_line(proc)


def test_a_rerun_of_one_commit_replaces_its_entry(tmp_path):
    history = tmp_path / "history.json"
    _run(tmp_path, 5394, "--history", str(history), "--label", "a", name="a")
    _run(tmp_path, 5400, "--history", str(history), "--label", "b", name="b")
    again = _run(tmp_path, 5402, "--history", str(history), "--label", "b", name="b2")
    assert [(r["label"], r["provisioned"]) for r in json.loads(history.read_text())] == [("a", 5394), ("b", 5402)]
    assert "delta=+8 trend(10)=+8 over 2 runs" in _report_line(again)


@pytest.mark.parametrize("content", ["not json", "[1, 2]", '[{"label": "x"}]', '{"a": 1}', ""])
def test_a_corrupt_history_is_no_history_not_a_failure(tmp_path, content):
    history = tmp_path / "history.json"
    history.write_text(content)
    proc = _run(tmp_path, 5394, "--history", str(history), "--label", "z")
    assert proc.returncode == 0, proc.stdout
    assert "trend(10)=n/a" in _report_line(proc)
    assert [r["label"] for r in json.loads(history.read_text())] == ["z"]  # and it is repaired


def test_a_breach_is_recorded_so_the_next_trend_shows_it(tmp_path):
    history = tmp_path / "history.json"
    _run(tmp_path, 5394, "--history", str(history), "--label", "a", name="a")
    over = _run(tmp_path, 5600, "--history", str(history), "--label", "b", name="b")
    assert over.returncode == 1
    assert [r["provisioned"] for r in json.loads(history.read_text())] == [5394, 5600]
    assert "delta=+206 trend(10)=+206 over 2 runs" in _report_line(over)


def test_the_summary_carries_the_report_line_and_the_recorded_values(tmp_path):
    summary = tmp_path / "summary.md"
    history = tmp_path / "history.json"
    _run(tmp_path, 5394, "--history", str(history), "--label", "a", name="a")
    _run(tmp_path, 5401, "--history", str(history), "--label", "b", "--summary", str(summary), name="b")
    text = summary.read_text()
    assert "storage_touch value=5401" in text and "verdict: report" in text and "5394, 5401" in text


def test_a_failure_before_a_count_exists_prints_no_report_line_and_writes_no_history(tmp_path):
    history = tmp_path / "history.json"
    proc = subprocess.run(
        [sys.executable, "-m", "tests._storage_touch_fan_in", str(tmp_path / "empty"), "--suite", "python_unit", "--shards", "3",
         "--ceiling-file", str(_ceiling(tmp_path)), "--history", str(history)],
        cwd=REPO, capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 1 and "status=no_reports" in proc.stdout
    assert "storage_touch value=" not in proc.stdout and not history.exists()


def test_an_unwritable_history_is_one_line_and_not_a_failure(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where a directory should be")
    proc = _run(tmp_path, 5394, "--history", str(blocker / "history.json"), "--label", "a")
    assert proc.returncode == 0, proc.stdout
    assert "storage-touch history not written" in proc.stdout


# ── the committed line, and the wiring that carries the history ───────────────────


def test_the_committed_ceiling_file_carries_sams_alarm_line():
    section = tomllib.loads(CEILINGS.read_text(encoding="utf-8"))["python_unit"]
    assert section["alarm_line_tests"] == 5544, "Sam's line (2026-11-27); moving it is a reviewed commit"
    assert fan._alarm_line(CEILINGS, "python_unit", section["provisioned_tests"], section["tolerance_tests"]) == 5544
    assert "alarm_line_tests" not in tomllib.loads(CEILINGS.read_text(encoding="utf-8"))["docs_readers"]


def test_the_gate_script_forwards_the_history_and_the_summary_to_the_storage_touch_check_alone():
    spec = importlib.util.spec_from_file_location("ci_gate_fan_in_h", REPO / "scripts" / "ci_gate_fan_in.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["ci_gate_fan_in_h"] = module
    spec.loader.exec_module(module)
    args = module.parse_args(["--reports-dir", "r", "--suite", "python_unit", "--shards", "6",
                              "--storage-touch-history", "h/history.json", "--storage-touch-summary", "s.md"])
    ddl, touch, collection = module.build_checks(args)
    assert touch.argv[-4:] == ["--history", "h/history.json", "--summary", "s.md"]
    assert "--history" not in ddl.argv and "--history" not in collection.argv
    bare = module.build_checks(module.parse_args(["--reports-dir", "r", "--suite", "python_unit", "--shards", "6"]))[1]
    assert "--history" not in bare.argv and "--summary" not in bare.argv


def test_the_workflow_restores_runs_and_saves_the_history_around_the_gate_step():
    doc = yaml.safe_load(CI_YML.read_text(encoding="utf-8"))
    steps = doc["jobs"]["pytest-gate"]["steps"]
    names = [s.get("name", "") for s in steps]
    restore, save = names.index("Restore the storage-touch history"), names.index("Save the storage-touch history")
    gate = next(i for i, s in enumerate(steps) if "scripts/ci_gate_fan_in.py" in str(s.get("run", "")))
    assert restore < gate < save
    run = steps[gate]["run"]
    assert "--storage-touch-history storage-touch-history/history.json" in run and '--storage-touch-summary "$GITHUB_STEP_SUMMARY"' in run
    assert steps[restore]["uses"].startswith("actions/cache/restore@") and steps[restore]["with"]["path"] == "storage-touch-history"
    assert steps[restore]["with"]["restore-keys"].strip() == "storage-touch-history-"
    # the history is the trunk's: only a push saves it, and it saves after a failed gate too
    assert steps[save]["uses"].startswith("actions/cache/save@")
    assert "github.event_name == 'push'" in steps[save]["if"] and "always()" in steps[save]["if"]
    assert steps[save]["with"]["path"] == steps[restore]["with"]["path"]
    assert steps[save]["with"]["key"].startswith("storage-touch-history-")
    # and the fan-in itself is not made to run after a failed verdict by this wiring
    assert "always()" not in steps[gate]["if"]
