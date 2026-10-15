# SPDX-License-Identifier: AGPL-3.0-or-later
"""nx doctor's "Plugin setup" row (RDR-228 Day 2, nexus-sv2wx.19).

The row reads the setup directory the plugin's setup process writes
(``conexus/launch/nx_setup.py``): ``status.json``, the lock's heartbeat, the log.
The two are separate release artifacts, so every case here is built from
``tests/fixtures/setup_status_format.json``, the file ``tests/hooks/test_nx_setup.py``
reads for the writer's side. One test per outcome of the bead: not applicable,
live (not a failure: the setup process's own verify step runs ``nx doctor`` with
its lock held), failed (a warning with the failure line and the log path) and
interrupted (a warning that says ``setup`` called again resumes).
"""
from __future__ import annotations

import inspect
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from nexus.health import _SETUP_STALE_SECONDS, HealthResult, _check_setup_status, run_health_checks

_ROOT = Path(__file__).resolve().parent.parent
_FIXTURE_PATH = _ROOT / "tests" / "fixtures" / "setup_status_format.json"
FIXTURE: dict[str, Any] = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))
CASES: dict[str, dict[str, Any]] = FIXTURE["cases"]
_LABEL = "Plugin setup"


def build_setup_dir(root: Path, case: dict[str, Any]) -> Path:
    """The case as a setup directory under *root*; returns the path the row is given."""
    setup = root / "setup"
    if not case["setup_dir"]:
        return setup
    setup.mkdir(parents=True)
    files = FIXTURE["files"]
    log = str(setup / files["log"])

    def body(value: Any) -> str:
        # A JSON body gets the path JSON-escaped: a Windows path's backslashes
        # pasted raw into JSON text are invalid escapes, and the file reads as unreadable.
        if isinstance(value, str):
            return value.replace("{LOG}", log)
        return json.dumps(value).replace("{LOG}", json.dumps(log)[1:-1])

    if case["status"] is not None:
        (setup / files["status"]).write_text(body(case["status"]), encoding="utf-8")
    if case["error"] is not None:
        (setup / files["error"]).write_text(body(case["error"]), encoding="utf-8")
    if case["not_now"]:
        (setup / files["marker"]).write_text("", encoding="utf-8")
    age = case["heartbeat_age_seconds"]
    if age is not None:
        (setup / files["lock"]).mkdir()
        (setup / files["lock"] / "owner").write_text("token", encoding="utf-8")
        beat = setup / files["heartbeat"]
        beat.write_text("0", encoding="utf-8")
        then = time.time() - age
        os.utime(beat, (then, then))
    return setup


def _row(setup: Path) -> HealthResult:
    results = _check_setup_status(setup)
    assert len(results) == 1
    assert results[0].label == _LABEL
    return results[0]


def _outcome(r: HealthResult) -> str:
    if r.ok:
        return "not-applicable" if "not applicable" in r.detail else "ok"
    assert r.warn is True and r.fatal is False, "the row is never a hard failure"
    return "warn"


def _check_case(name: str, tmp_path: Path) -> HealthResult:
    case = CASES[name]
    setup = build_setup_dir(tmp_path, case)
    row = _row(setup)
    expect = case["row"]
    assert _outcome(row) == expect["outcome"], (name, row)
    log = str(setup / FIXTURE["files"]["log"])
    text = row.detail + " " + " ".join(row.fix_suggestions)
    for needle in expect["contains"]:
        assert needle.replace("{LOG}", log) in text, (name, needle, row)
    return row


# -- Build item 2: no setup directory ------------------------------------------------------------


def test_no_setup_directory_is_not_applicable(tmp_path: Path) -> None:
    row = _check_case("virgin", tmp_path)
    assert row.ok is True and not row.warn and not row.fix_suggestions


def test_a_declined_setup_is_not_applicable(tmp_path: Path) -> None:
    """The "not now" marker alone is no run."""
    row = _check_case("declined", tmp_path)
    assert row.ok is True and not row.warn


def test_not_applicable_is_decided_after_the_failure_logic(tmp_path: Path) -> None:
    """A directory with something to judge is never short-circuited into not-applicable:
    each of these has a setup directory and a verdict other than "not applicable"."""
    verdicts = {}
    for name, case in CASES.items():
        if case["setup_dir"] and (case["status"] is not None or case["error"] is not None
                                  or case["heartbeat_age_seconds"] is not None):
            sub = tmp_path / name
            sub.mkdir()
            verdicts[name] = _outcome(_row(build_setup_dir(sub, case)))
    assert verdicts, "the fixture has cases with something to judge"
    assert "not-applicable" not in verdicts.values(), verdicts


# -- Build item 3: a live run is not a failure ---------------------------------------------------


def test_a_live_run_reports_its_stage_and_is_not_a_failure(tmp_path: Path) -> None:
    row = _check_case("live", tmp_path)
    assert row.ok is True and not row.warn and not row.fatal


def test_a_live_run_with_an_unreadable_status_is_still_not_a_failure(tmp_path: Path) -> None:
    """The window in which setup's own verify step reads a status the run is replacing."""
    row = _check_case("live-unreadable-status", tmp_path)
    assert row.ok is True and not row.warn


@pytest.mark.skipif(sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
                    reason="a mode-000 file is how POSIX produces the unreadable status; root reads it")
def test_a_live_run_whose_status_cannot_be_opened_is_not_a_failure(tmp_path: Path) -> None:
    setup = build_setup_dir(tmp_path, CASES["live"])
    status = setup / FIXTURE["files"]["status"]
    status.chmod(0)
    try:
        row = _row(setup)
    finally:
        status.chmod(0o600)
    assert row.ok is True and not row.warn and "in progress" in row.detail


# -- Build item 4: the last run failed -----------------------------------------------------------


def test_a_failed_run_warns_with_the_failure_line_and_the_log_path(tmp_path: Path) -> None:
    row = _check_case("failed", tmp_path)
    assert row.ok is False and row.warn is True and not row.fatal
    assert str(tmp_path / "setup" / "setup.log") in row.detail


def test_a_failed_status_beside_a_fresh_lock_still_warns(tmp_path: Path) -> None:
    """A run that has written its terminal status and not yet released its lock has ended."""
    _check_case("failed-with-leftover-lock", tmp_path)


def test_a_run_that_could_not_record_its_status_warns_with_the_reason(tmp_path: Path) -> None:
    _check_case("unwritable-status", tmp_path)


def test_a_verify_step_that_found_failing_rows_warns_and_names_them(tmp_path: Path) -> None:
    row = _check_case("doctor-failed", tmp_path)
    assert "Service: not running" in row.detail


def test_a_status_file_that_cannot_be_read_with_no_run_live_warns(tmp_path: Path) -> None:
    _check_case("unreadable-status-no-run", tmp_path)


def test_a_finished_run_is_green(tmp_path: Path) -> None:
    row = _check_case("done", tmp_path)
    assert row.ok is True and not row.warn


def test_an_unsupported_host_is_information_not_a_warning(tmp_path: Path) -> None:
    """The CLI exists by the time this runs, so the install the setup could not do was done another way."""
    row = _check_case("unsupported", tmp_path)
    assert row.ok is True and not row.warn


# -- Build item 5: an interrupted run ------------------------------------------------------------


def test_a_stale_heartbeat_with_a_running_status_warns_that_setup_resumes(tmp_path: Path) -> None:
    row = _check_case("interrupted", tmp_path)
    assert row.ok is False and row.warn is True
    assert "call the `setup` tool again" in " ".join(row.fix_suggestions), row


def test_a_running_status_with_no_lock_is_interrupted(tmp_path: Path) -> None:
    _check_case("interrupted-no-lock", tmp_path)


# -- the format, from both sides -----------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(CASES))
def test_every_fixture_case_gives_its_recorded_outcome(name: str, tmp_path: Path) -> None:
    _check_case(name, tmp_path)


def test_the_fixture_covers_every_state_and_every_outcome() -> None:
    """Non-vacuity: a fixture that lost its cases would pass every test above."""
    states = {c["status"]["state"] for c in CASES.values() if isinstance(c["status"], dict)}
    assert states == set(FIXTURE["states"])
    assert {c["row"]["outcome"] for c in CASES.values()} == {"not-applicable", "ok", "warn"}
    assert len(CASES) >= 10


def test_the_stale_threshold_is_the_writers() -> None:
    assert _SETUP_STALE_SECONDS == FIXTURE["stale_after_seconds"]


def test_a_heartbeat_just_inside_and_just_outside_the_threshold(tmp_path: Path) -> None:
    for age, live in ((FIXTURE["stale_after_seconds"] - 20, True), (FIXTURE["stale_after_seconds"] + 20, False)):
        case = {**CASES["live"], "heartbeat_age_seconds": age}
        sub = tmp_path / str(age)
        sub.mkdir()
        row = _row(build_setup_dir(sub, case))
        assert (row.ok is True) is live, (age, row)


# -- what the row prints -------------------------------------------------------------------------


def test_a_long_or_multiline_message_is_one_bounded_line(tmp_path: Path) -> None:
    case = json.loads(json.dumps(CASES["failed"]))
    case["status"]["message"] = "boom\n" + "x" * 5000 + "\x1b[31m"
    row = _row(build_setup_dir(tmp_path, case))
    assert "\n" not in row.detail and "\x1b" not in row.detail
    assert len(row.detail) < 1200


def test_the_log_path_is_the_directorys_own_not_the_status_files(tmp_path: Path) -> None:
    case = json.loads(json.dumps(CASES["failed"]))
    case["status"]["log"] = "/elsewhere/other.log"
    case["status"]["next"] = "call setup again"
    setup = build_setup_dir(tmp_path, case)
    row = _row(setup)
    assert str(setup / "setup.log") in row.detail
    assert "/elsewhere/other.log" not in row.detail


# -- registration --------------------------------------------------------------------------------


def test_the_row_is_in_the_default_sweep() -> None:
    assert "_check_setup_status()" in inspect.getsource(run_health_checks)


def test_the_default_setup_directory_is_setup_under_the_config_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With no argument the row reads ``<NEXUS_CONFIG_DIR>/setup``, the directory the plugin script writes.
    A live run there is not a failure: the setup process runs ``nx doctor`` with its lock held."""
    build_setup_dir(tmp_path, CASES["live"])
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    (row,) = _check_setup_status()
    assert row.ok is True and not row.warn and "in progress" in row.detail


def test_a_virgin_config_directory_is_not_applicable_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path / "nothing-here"))
    (row,) = _check_setup_status()
    assert row.ok is True and "not applicable" in row.detail


def test_the_row_is_absent_from_the_fresh_install_mvv_allowlist() -> None:
    """nexus-7zhag doctrine: a new row resolves not-applicable on a virgin box and is never
    allow-listed in ``tests/e2e/fresh-install-mvv.sh``."""
    source = (_ROOT / "tests" / "e2e" / "fresh-install-mvv.sh").read_text(encoding="utf-8")
    match = re.search(r"ALLOWLIST_REGEX='([^']*)'", source)
    assert match is not None, "fresh-install-mvv.sh must still define ALLOWLIST_REGEX"
    assert "Plugin setup" not in match.group(1)
