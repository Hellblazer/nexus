# SPDX-License-Identifier: AGPL-3.0-or-later
"""The push gate's class-A ledger checks hold a child's output to the mode the gate pinned (RDR-258 Phase 4, nexus-2jomg.5).

``run_check`` pins NX_LEDGER_MODE from the spec or ``--ledger-report`` and then reads what the child printed. These tests drive it
with stub scripts in a throwaway tree, the way the gate's other tests do: the child is a stub that prints a fixed output and
exits with a fixed code, so each refusal is one case and none depends on the real changelog, cloud-leg or plugin ledger.
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest

import push_check_gate as gate  # scripts/ is on pythonpath: the one instance test_ledger_mode.py shares

OK = "changelog_fold check: ok"
FINDING = "LEDGER-FINDING changelog: nexus-x.md has no section"
SUMMARY = "LEDGER-REPORT changelog: 1 finding(s), 4 item(s) examined (mode=report)"
SUMMARY_NONE = "LEDGER-REPORT changelog: 0 finding(s), 4 item(s) examined (mode=report)"
VACUOUS = "LEDGER-VACUOUS changelog: examined 0 items; report mode does not pass a check that examined nothing"
REASON = "the fragments are being rewritten, nexus-xyz"


def _tree(tmp_path: Path, output: str, code: int = 0, *, body: str = "", with_ledger_mode: bool = True) -> Path:
    """A tree whose scripts/changelog_fold.py prints *output* and exits *code* (or runs *body* instead)."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "changelog_fold.py").write_text(body or f"import sys\nprint({output!r})\nsys.exit({code})\n")
    if with_ledger_mode:
        (scripts / "ledger_mode.py").write_text("")
    return tmp_path


def _run(tree: Path, reason: str = "") -> object:
    return gate.run_check(gate.SPECS["changelog"], tree, [sys.executable], [sys.executable, "-m", "pytest"], reason)


def _refused(outcome: object, *fragments: str) -> None:
    assert outcome.ok is False, outcome  # type: ignore[attr-defined]
    for fragment in fragments:
        assert fragment in outcome.detail, outcome.detail  # type: ignore[attr-defined]


# ── S1: the output is held to the pinned mode ───────────────────────────────

def test_block_mode_passes_on_its_own_marker_and_reports_nothing(tmp_path: Path) -> None:
    outcome = _run(_tree(tmp_path, OK))
    assert outcome.ok and not outcome.reported and outcome.lines == []


def test_block_mode_exit_zero_with_a_finding_line_is_refused(tmp_path: Path) -> None:
    _refused(_run(_tree(tmp_path, f"{OK}\n{FINDING}")), "block mode", "LEDGER-FINDING")


def test_block_mode_does_not_pass_on_a_report_summary_line(tmp_path: Path) -> None:
    """The changelog marker's report alternative is for report mode only: a LEDGER-REPORT line is not block mode's ``ok``."""
    _refused(_run(_tree(tmp_path, SUMMARY_NONE)), "no /changelog_fold check: ok/")


@pytest.mark.parametrize("reason", ["", REASON], ids=["block", "report"])
def test_a_vacuous_line_is_refused_in_either_mode(tmp_path: Path, reason: str) -> None:
    _refused(_run(_tree(tmp_path, f"{OK}\n{SUMMARY_NONE}\n{VACUOUS}"), reason), "LEDGER-VACUOUS", "vacuous")


def test_report_mode_findings_with_no_summary_are_refused(tmp_path: Path) -> None:
    _refused(_run(_tree(tmp_path, f"{OK}\n{FINDING}"), REASON), "no LEDGER-REPORT summary")


def test_report_mode_findings_with_a_summary_pass_and_are_reported(tmp_path: Path) -> None:
    outcome = _run(_tree(tmp_path, f"{FINDING}\n{SUMMARY}"), REASON)
    assert outcome.ok and outcome.reported and outcome.lines == [FINDING, SUMMARY]


def test_report_mode_with_zero_findings_passes_unreported_and_prints_nothing(tmp_path: Path) -> None:
    outcome = _run(_tree(tmp_path, SUMMARY_NONE), REASON)
    assert outcome.ok and not outcome.reported
    out, err = io.StringIO(), io.StringIO()
    gate._print_report("changelog", gate.SPECS["changelog"], outcome, "t" * 40, "its path", out, err)
    assert out.getvalue() == "" and err.getvalue() == "", "a check that reported nothing prints nothing, summary line included"


# ── S2: a ledger check that crashes still refuses under --ledger-report ─────

def test_a_crash_with_a_traceback_is_refused_even_after_findings_were_printed(tmp_path: Path) -> None:
    # Prints everything a passing report-mode run prints, then crashes: only the non-zero exit makes it a refusal.
    body = f"print({OK!r})\nprint({FINDING!r})\nprint({SUMMARY!r})\nraise RuntimeError('boom')\n"
    outcome = _run(_tree(tmp_path, "", body=body), REASON)
    _refused(outcome, "RuntimeError: boom")
    assert "exit 0 but" not in outcome.detail, "refused for its exit code, not by one of the output checks"


def test_exit_two_is_refused_under_report_mode(tmp_path: Path) -> None:
    # A passing run's output (marker and summary) with exit 2: only the exit code makes it a refusal.
    outcome = _run(_tree(tmp_path, f"{OK}\n{SUMMARY_NONE}", 2), REASON)
    _refused(outcome, SUMMARY_NONE)
    assert "exit 0 but" not in outcome.detail, "refused for its exit code, not by one of the output checks"


def test_a_timeout_is_refused_under_report_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NX_PUSH_CHECK_TIMEOUT_S", "1")
    _refused(_run(_tree(tmp_path, "", body="import time\ntime.sleep(60)\n"), REASON), "ran past 1s")


@pytest.mark.parametrize("name", ["changelog", "cloud-leg", "plugin-ledger"])
def test_a_tree_with_no_ledger_mode_script_is_refused_under_report_mode(tmp_path: Path, name: str) -> None:
    """The child needs scripts/ledger_mode.py to import; without it there is no report to trust, and the run is a refusal."""
    spec = gate.SPECS[name]
    for need in spec.needs:
        if need != gate.LEDGER_MODE_SCRIPT:
            (tmp_path / need).parent.mkdir(parents=True, exist_ok=True)
            (tmp_path / need).write_text("import sys\nprint('changelog_fold check: ok')\nsys.exit(0)\n")
    outcome = gate.run_check(spec, tmp_path, [sys.executable], [sys.executable, "-m", "pytest"], REASON)
    _refused(outcome, "scripts/ledger_mode.py", "refusal, not a skip")


def test_the_same_tree_with_the_script_present_runs_the_check(tmp_path: Path) -> None:
    """Control for the refusal above: the stub passes, so only the missing script made it red."""
    assert _run(_tree(tmp_path, OK), REASON).ok


# ── M2: the report form follows the spec, not whatever detail holds ─────────

def test_a_ledger_report_prints_its_findings_and_summary_and_ignores_detail(tmp_path: Path) -> None:
    spec = gate.SPECS["changelog"]
    outcome = gate.Outcome("changelog", True, "stray detail", 0.1, [FINDING, SUMMARY], reported=True)
    out, err = io.StringIO(), io.StringIO()
    gate._print_report("changelog", spec, outcome, "t" * 40, "its path", out, err)
    assert out.getvalue() == "" and "stray detail" not in err.getvalue()
    assert err.getvalue().splitlines() == [f"PUSH_CHECK_REPORT changelog: {FINDING}", f"PUSH_CHECK_REPORT changelog: {SUMMARY}"]


def test_the_wire_form_is_chosen_by_the_spec_even_when_detail_is_empty() -> None:
    spec = gate.SPECS["wire-ledger"]
    outcome = gate.Outcome("wire-ledger", True, "", 0.1, reported=True)
    out, err = io.StringIO(), io.StringIO()
    gate._print_report("wire-ledger", spec, outcome, "t" * 40, "its path", out, err)
    assert out.getvalue().startswith("PUSH_CHECK_REPORT wire-ledger is red at the tip's tree") and err.getvalue() == ""


def test_the_report_is_capped_at_eight_findings_and_then_names_the_rest() -> None:
    spec = gate.SPECS["changelog"]
    findings = [f"LEDGER-FINDING changelog: f{i}" for i in range(11)]
    outcome = gate.Outcome("changelog", True, "", 0.1, [*findings, SUMMARY], reported=True)
    err = io.StringIO()
    gate._print_report("changelog", spec, outcome, "t" * 40, "its path", io.StringIO(), err)
    text = err.getvalue()
    assert text.count("LEDGER-FINDING") == 8 and "+3 more finding(s)" in text and SUMMARY in text
