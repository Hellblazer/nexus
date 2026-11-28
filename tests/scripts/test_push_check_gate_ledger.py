# SPDX-License-Identifier: AGPL-3.0-or-later
"""The push gate's class-A ledger checks hold a child's output to the mode the gate pinned (RDR-258 Phase 4, nexus-2jomg.5).

``run_check`` pins NX_LEDGER_MODE from the spec or ``--ledger-report`` and then reads what the child printed. These tests drive it
with stub scripts in a throwaway tree, the way the gate's other tests do: the child is a stub that prints a fixed output and
exits with a fixed code, so each refusal is one case and none depends on the real changelog, cloud-leg or plugin ledger.
"""
from __future__ import annotations

import dataclasses
import io
import shutil
import subprocess
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


@pytest.mark.parametrize("name", ["changelog", "changelog-coverage", "cloud-leg", "plugin-ledger"])
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


# ── changelog-coverage: develop lists commits with no fragment, report only (RDR-258 Phase 4 step 2, nexus-2jomg.5) ─────────

#: Report mode reached by the spec's default (no flag: changelog-coverage's own) and by the --ledger-report flag.
COVERAGE_ROUTES = pytest.mark.parametrize("reason", ["", REASON], ids=["default", "flag"])

COVERAGE = gate.SPECS["changelog-coverage"]
COV_OK = "changelog_coverage: ok (3 commit(s) examined)"
COV_FINDING = "LEDGER-FINDING changelog-coverage: abc1234567 feat: x (nexus-t1): changes shipped code (src/nexus/x.py) and has no fragment"
COV_SUMMARY = "LEDGER-REPORT changelog-coverage: 1 finding(s), 3 item(s) examined (mode=report)"
COV_SUMMARY_NONE = "LEDGER-REPORT changelog-coverage: 0 finding(s), 3 item(s) examined (mode=report)"


def _cov_tree(tmp_path: Path, body: str, *, with_ledger_mode: bool = True) -> Path:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "changelog_coverage.py").write_text(body)
    if with_ledger_mode:
        (scripts / "ledger_mode.py").write_text("")
    return tmp_path


def _cov_stub(output: str, code: int = 0) -> str:
    return f"import sys\nprint({output!r})\nsys.exit({code})\n"


def _cov_run(tree: Path, reason: str = "", base: str = "refs/remotes/origin/develop", tip: str = "f" * 40) -> object:
    return gate.run_check(COVERAGE, tree, [sys.executable], [sys.executable, "-m", "pytest"], reason, base, tip)


def test_the_coverage_spec_is_a_report_by_default_ledger_selected_always_and_skippable() -> None:
    assert COVERAGE.default_mode == "report" and COVERAGE.ledger and COVERAGE.kind == "run"
    assert "changelog-coverage" in gate.ORDER and "changelog-coverage" in gate.SKIPPABLE
    chosen = gate.select({"README.md"}, lambda p: False, lambda p: False)
    assert chosen["changelog-coverage"] == "always"
    assert gate.ledger_mode_for(COVERAGE) == ("report", "default")


def test_the_coverage_command_names_the_outbound_range_and_leaves_the_mode_to_the_pinned_environment() -> None:
    command = gate.command_for("changelog-coverage", ["py"], ["py"], "refs/remotes/origin/develop", "a" * 40)
    assert command == ["py", "scripts/changelog_coverage.py", "--base", "refs/remotes/origin/develop", "--head", "a" * 40]


def test_the_coverage_child_gets_the_range_and_a_pinned_report_mode(tmp_path: Path) -> None:
    """The stub fails unless it was handed the gate's base and tip and NX_LEDGER_MODE=report (the spec's default, no flag)."""
    body = ("import os, sys\n"
            "ok = sys.argv[1:] == ['--base', 'refs/remotes/origin/develop', '--head', 'abc'] and os.environ['NX_LEDGER_MODE'] == 'report'\n"
            f"print({COV_SUMMARY_NONE!r} if ok else 'wrong argv or mode: ' + repr(sys.argv))\nsys.exit(0 if ok else 3)\n")
    outcome = _cov_run(_cov_tree(tmp_path, body), tip="abc")
    assert outcome.ok and not outcome.reported, outcome.detail  # type: ignore[attr-defined]


def test_a_coverage_finding_is_reported_not_refused_and_prints_through_the_one_report_path(tmp_path: Path) -> None:
    outcome = _cov_run(_cov_tree(tmp_path, _cov_stub(f"{COV_FINDING}\n{COV_SUMMARY}")))
    assert outcome.ok and outcome.reported and outcome.lines == [COV_FINDING, COV_SUMMARY]  # type: ignore[attr-defined]
    out, err = io.StringIO(), io.StringIO()
    gate._print_report("changelog-coverage", COVERAGE, outcome, "t" * 40, "always", out, err)
    assert out.getvalue() == ""
    assert err.getvalue().splitlines() == [f"PUSH_CHECK_REPORT changelog-coverage: {COV_FINDING}",
                                           f"PUSH_CHECK_REPORT changelog-coverage: {COV_SUMMARY}"]


def test_a_clean_coverage_run_passes_unreported(tmp_path: Path) -> None:
    outcome = _cov_run(_cov_tree(tmp_path, _cov_stub(COV_SUMMARY_NONE)))
    assert outcome.ok and not outcome.reported  # type: ignore[attr-defined]


def test_a_coverage_pass_with_neither_marker_is_vacuous_and_refused(tmp_path: Path) -> None:
    _refused(_cov_run(_cov_tree(tmp_path, _cov_stub("nothing examined"))), "vacuous")


@COVERAGE_ROUTES
def test_a_coverage_crash_after_printing_a_report_is_refused(tmp_path: Path, reason: str) -> None:
    body = f"print({COV_FINDING!r})\nprint({COV_SUMMARY!r})\nraise RuntimeError('boom')\n"
    outcome = _cov_run(_cov_tree(tmp_path, body), reason)
    _refused(outcome, "RuntimeError: boom")
    assert "exit 0 but" not in outcome.detail, "refused for its exit code, not by one of the output checks"  # type: ignore[attr-defined]


@COVERAGE_ROUTES
def test_coverage_exit_two_is_refused(tmp_path: Path, reason: str) -> None:
    outcome = _cov_run(_cov_tree(tmp_path, _cov_stub(COV_SUMMARY_NONE, 2)), reason)
    _refused(outcome, COV_SUMMARY_NONE)
    assert "exit 0 but" not in outcome.detail, "refused for its exit code, not by one of the output checks"  # type: ignore[attr-defined]


@COVERAGE_ROUTES
def test_coverage_exit_ten_on_a_range_that_examined_nothing_is_refused(tmp_path: Path, reason: str) -> None:
    _refused(_cov_run(_cov_tree(tmp_path, _cov_stub("error: refs/remotes/origin/develop..abc holds no commit to examine", 10)), reason),
             "holds no commit to examine")


@COVERAGE_ROUTES
def test_a_coverage_timeout_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reason: str) -> None:
    monkeypatch.setenv("NX_PUSH_CHECK_TIMEOUT_S", "1")
    _refused(_cov_run(_cov_tree(tmp_path, "import time\ntime.sleep(60)\n"), reason), "ran past 1s")


def test_a_tree_with_no_coverage_script_is_refused(tmp_path: Path) -> None:
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "ledger_mode.py").write_text("")
    _refused(_cov_run(tmp_path), "scripts/changelog_coverage.py", "refusal, not a skip")


def test_a_coverage_run_against_a_real_range_reports_the_commit_with_no_fragment(tmp_path: Path) -> None:
    """End to end on a real repository and the real script: the base and tip the gate names bound what is examined, a commit
    that changes shipped code with no fragment is a reported finding (not a refusal), and a fragment for it clears the finding."""
    root = Path(__file__).resolve().parents[2]
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    for name in ("changelog_coverage.py", "ledger_mode.py"):
        shutil.copy(root / "scripts" / name, repo / "scripts" / name)

    def git(*args: str) -> str:
        done = subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
                              capture_output=True, text=True, check=True)
        return done.stdout.strip()

    git("init", "-q")
    (repo / "README.md").write_text("x\n")
    git("add", "README.md", "scripts")
    git("commit", "-q", "-m", "chore: seed (nexus-seed)")
    base = git("rev-parse", "HEAD")
    (repo / "src" / "nexus").mkdir(parents=True)
    (repo / "src" / "nexus" / "x.py").write_text("X = 1\n")
    git("add", "src/nexus/x.py")
    git("commit", "-q", "-m", "feat: add x (nexus-t1)")
    tip = git("rev-parse", "HEAD")

    outcome = _cov_run(repo, base=base, tip=tip)
    assert outcome.ok and outcome.reported, getattr(outcome, "detail", "")  # type: ignore[attr-defined]
    # The vacuous-gate rule, pinned: a range with real non-merge commits is EXAMINED (not the exit-10 "examined nothing" the gate lets
    # a merge-only range pass on), so this report cannot be one that looked at nothing.
    summary = [ln for ln in outcome.lines if ln.startswith("LEDGER-REPORT changelog-coverage:")]  # type: ignore[attr-defined]
    assert summary and "1 finding(s), 1 item(s) examined" in summary[0], summary
    assert outcome.note == "", "a range that was examined carries no examined-nothing note"  # type: ignore[attr-defined]
    finding = [ln for ln in outcome.lines if ln.startswith("LEDGER-FINDING changelog-coverage:")]  # type: ignore[attr-defined]
    assert len(finding) == 1 and "feat: add x (nexus-t1)" in finding[0] and "nexus-seed" not in finding[0]

    (repo / "changelog" / "unreleased").mkdir(parents=True)
    (repo / "changelog" / "unreleased" / "nexus-t1.md").write_text("### Added\n\n- x (nexus-t1)\n")
    git("add", "changelog")
    git("commit", "-q", "-m", "docs: fragment (nexus-t1)")
    covered = _cov_run(repo, base=base, tip=git("rev-parse", "HEAD"))
    assert covered.ok and not covered.reported, getattr(covered, "detail", "")  # type: ignore[attr-defined]


def _git_in(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
                          capture_output=True, text=True, check=True).stdout.strip()


def _merge_only_repo(tmp_path: Path) -> tuple[Path, str, str]:
    """(repo, base, tip) where base..tip is ONE merge commit and nothing else: the tip merges the base with one of the base's own
    ancestors, which is what a back-merge looks like to a range whose base already holds the other side's commits."""
    root = Path(__file__).resolve().parents[2]
    repo = tmp_path / "mrepo"
    (repo / "scripts").mkdir(parents=True)
    for name in ("changelog_coverage.py", "ledger_mode.py"):
        shutil.copy(root / "scripts" / name, repo / "scripts" / name)

    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "README.md").write_text("x\n")
    _git_in(repo, "add", "README.md", "scripts")
    _git_in(repo, "commit", "-q", "-m", "chore: seed (nexus-seed)")
    first = _git_in(repo, "rev-parse", "HEAD")
    (repo / "README.md").write_text("y\n")
    _git_in(repo, "commit", "-q", "-am", "docs: second (nexus-seed)")
    base = _git_in(repo, "rev-parse", "HEAD")
    tip = _git_in(repo, "commit-tree", f"{base}^{{tree}}", "-p", base, "-p", first, "-m", "Merge back")
    return repo, base, tip


def _mixed_range(repo: Path, tip: str) -> str:
    """The sha of one real (non-merge) commit on top of *tip*: base..<result> holds a merge commit AND a real commit."""
    _git_in(repo, "checkout", "-q", "-b", "side", tip)
    (repo / "README.md").write_text("z\n")
    _git_in(repo, "commit", "-q", "-am", "docs: z (nexus-seed)")
    return _git_in(repo, "rev-parse", "HEAD")


def _stub_script(repo: Path, relpath: str, output: str, code: int) -> None:
    (repo / relpath).write_text(_cov_stub(output, code))


VACUOUS = "LEDGER-VACUOUS changelog-coverage: examined 0 items; report mode does not pass a check that examined nothing"


def test_only_the_missing_fragment_listing_may_pass_having_examined_nothing() -> None:
    assert {n for n, s in gate.SPECS.items() if s.merge_only_ok} == {"changelog-coverage"}


def test_a_range_of_merge_commits_only_passes_with_one_loud_examined_nothing_line(tmp_path: Path) -> None:
    """A release back-merge is merge-only: the script exits 10 (it examined nothing), the gate lets it pass and says so."""
    repo, base, tip = _merge_only_repo(tmp_path)
    outcome = _cov_run(repo, base=base, tip=tip)
    assert outcome.ok and not outcome.reported, getattr(outcome, "detail", "")  # type: ignore[attr-defined]
    assert outcome.note == "changelog-coverage examined nothing: 1 merge commit(s), no fragment check ran"  # type: ignore[attr-defined]


def test_an_exit_ten_that_is_not_the_vacuous_line_on_a_merge_only_range_is_refused(tmp_path: Path) -> None:
    """Exit 10 is also the script's error exit (a git it could not read): on a merge-only range it passes only when it says it examined
    nothing. A script that printed ``error: boom`` and exited 10 has not examined nothing, it has failed."""
    repo, base, tip = _merge_only_repo(tmp_path)
    assert gate.merge_only_range(repo, base, tip) == 1
    _stub_script(repo, "scripts/changelog_coverage.py", "error: boom", 10)
    _refused(_cov_run(repo, base=base, tip=tip), "boom")
    _refused(_cov_run(repo, REASON, base=base, tip=tip), "boom")


def test_the_merge_only_pass_belongs_to_the_spec_that_asks_for_it(tmp_path: Path) -> None:
    """The same vacuous exit 10 on the same merge-only range is a refusal for a spec without ``merge_only_ok`` (the flag is what permits
    the pass, not the range), and for the changelog ledger, which is such a spec."""
    repo, base, tip = _merge_only_repo(tmp_path)
    _stub_script(repo, "scripts/changelog_coverage.py", VACUOUS, 10)
    assert _cov_run(repo, base=base, tip=tip).ok                                    # the permitted case, for contrast
    strict = dataclasses.replace(COVERAGE, merge_only_ok=False)
    _refused(gate.run_check(strict, repo, [sys.executable], [sys.executable, "-m", "pytest"], "", base, tip), "LEDGER-VACUOUS")
    _stub_script(repo, "scripts/changelog_fold.py", VACUOUS.replace("changelog-coverage", "changelog"), 10)
    other = gate.run_check(gate.SPECS["changelog"], repo, [sys.executable], [sys.executable, "-m", "pytest"], "", base, tip)
    _refused(other, "LEDGER-VACUOUS")


def test_an_empty_range_and_a_vacuous_stub_outside_a_merge_only_range_still_refuse(tmp_path: Path) -> None:
    repo, base, _tip = _merge_only_repo(tmp_path)
    _refused(_cov_run(repo, base=base, tip=base), "LEDGER-VACUOUS")                           # an empty range proves nothing
    plain = tmp_path / "plain"
    plain.mkdir()
    _refused(_cov_run(_cov_tree(plain, _cov_stub(VACUOUS, 10))), "LEDGER-VACUOUS")             # not a git range at all


def test_a_range_with_a_real_commit_and_a_merge_is_not_merge_only_even_if_the_script_says_vacuous(tmp_path: Path) -> None:
    """The pass is for merge commits ONLY: a non-merge commit in the range makes an exit 10 a refusal, through run_check."""
    repo, base, tip = _merge_only_repo(tmp_path)
    assert gate.merge_only_range(repo, base, tip) == 1
    later = _mixed_range(repo, tip)
    assert gate.merge_only_range(repo, base, later) == 0
    assert gate.merge_only_range(repo, base, base) == 0
    _stub_script(repo, "scripts/changelog_coverage.py", VACUOUS, 10)                          # the script says "examined nothing"
    _refused(_cov_run(repo, base=base, tip=later), "LEDGER-VACUOUS")                          # the range says otherwise: refused
    assert _cov_run(repo, base=base, tip=tip).ok, "the same stub on the merge-only range passes: only the range differs"


# ── a range of release-preparation and revert commits only: the script exits 0 and says so; the gate says it loudly too ──────

def test_a_range_of_release_preparation_commits_only_passes_with_the_examined_nothing_line(tmp_path: Path) -> None:
    repo, base, _tip = _merge_only_repo(tmp_path)
    (repo / "README.md").write_text("v\n")
    _git_in(repo, "commit", "-q", "-am", "chore(release): v9.9.9")
    (repo / "README.md").write_text("w\n")
    _git_in(repo, "commit", "-q", "-am", 'Revert "docs: second (nexus-seed)"')
    tip = _git_in(repo, "rev-parse", "HEAD")
    outcome = _cov_run(repo, base=base, tip=tip)
    assert outcome.ok and not outcome.reported, getattr(outcome, "detail", "")  # type: ignore[attr-defined]
    assert outcome.note == "changelog-coverage examined nothing: 2 release-preparation or revert commit(s), no fragment check ran"  # type: ignore[attr-defined]


def test_a_summary_that_examined_something_carries_no_examined_nothing_note(tmp_path: Path) -> None:
    clean = _cov_run(_cov_tree(tmp_path, _cov_stub(COV_SUMMARY_NONE)))
    assert clean.ok and clean.note == ""  # type: ignore[attr-defined]
    other = "LEDGER-REPORT changelog-coverage: 0 finding(s), 0 item(s) examined (mode=report): the range holds only 2 merge commit(s)"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    assert _cov_run(_cov_tree(elsewhere, _cov_stub(other))).note == ""  # type: ignore[attr-defined]
