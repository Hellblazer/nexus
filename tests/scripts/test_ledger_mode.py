# SPDX-License-Identifier: AGPL-3.0-or-later
"""NX_LEDGER_MODE: the class-A ledger checks list a finding in report mode and fail it in block mode (RDR-258 Phase 4,
nexus-2jomg.5). Each check runs for real on files in a tmp repo: the changelog script as a subprocess, the plugin ledger's own
test functions against a git world, the cloud-leg route check against a ledger file, and the push gate's runner on a tree."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import ledger_mode
import pytest

import push_check_gate as gate
import tests.test_plugin_release_drift_ledger as plug
from tests._cloud_leg_ledger import check_routes
from tests.scripts import test_cloud_leg as tcl

REPO = Path(__file__).resolve().parents[2]
CHANGELOG_FOLD = REPO / "scripts" / "changelog_fold.py"
SN_PROBE = plug.TestPerPluginDriftScoping.SN_PROBE


@pytest.fixture(autouse=True)
def _plain_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (ledger_mode.ENV, ledger_mode.REPORT_FILE_ENV, "GITHUB_ACTIONS"):
        monkeypatch.delenv(name, raising=False)


# ── the switch ──────────────────────────────────────────────────────────────

def test_the_default_is_block_and_a_bad_value_is_an_error_not_a_default() -> None:
    assert ledger_mode.mode({}) == "block" and ledger_mode.mode({ledger_mode.ENV: ""}) == "block"
    assert ledger_mode.mode({ledger_mode.ENV: " Report "}) == "report"
    with pytest.raises(ledger_mode.LedgerModeError, match="NX_LEDGER_MODE='warn'"):
        ledger_mode.mode({ledger_mode.ENV: "warn"})


def test_block_mode_prints_nothing_and_passes_exactly_when_there_is_no_finding(capsys: pytest.CaptureFixture[str]) -> None:
    assert ledger_mode.settle("x", [], 0) is True, "block mode does not judge what was examined: the check's own assertions do"
    assert ledger_mode.settle("x", ["f"], 9) is False
    assert capsys.readouterr().out == ""


def test_report_mode_lists_each_finding_summarises_and_annotates_under_github_actions(capsys: pytest.CaptureFixture[str]) -> None:
    env = {ledger_mode.ENV: "report", "GITHUB_ACTIONS": "true"}
    assert ledger_mode.settle("led", ["one\nwrapped", "two"], 5, environ=env) is True
    lines = capsys.readouterr().out.splitlines()
    assert lines == ["LEDGER-FINDING led: one wrapped", "::warning title=led::one wrapped", "LEDGER-FINDING led: two",
                     "::warning title=led::two", "LEDGER-REPORT led: 2 finding(s), 5 item(s) examined (mode=report)"]


def test_report_mode_fails_a_check_that_examined_nothing_even_with_no_finding(capsys: pytest.CaptureFixture[str]) -> None:
    assert ledger_mode.settle("led", [], 0, environ={ledger_mode.ENV: "report"}) is False
    out = capsys.readouterr().out
    assert "LEDGER-REPORT led: 0 finding(s), 0 item(s) examined" in out and "LEDGER-VACUOUS led:" in out


# ── changelog (scripts/changelog_fold.py check) ──────────────────────────────

def _changelog_root(root: Path, *, bad: bool) -> Path:
    (root / "changelog" / "unreleased").mkdir(parents=True)
    (root / "changelog" / "unreleased" / "README.md").write_text("readme\n")
    (root / "CHANGELOG.md").write_text("# Changelog\n\n## [Unreleased]\n\n## [1.0.0] - 2026-01-01\n")
    (root / "changelog" / "unreleased" / "nexus-aaa.1.md").write_text(
        "### Nonsense\n\n- a bullet under a section that does not exist\n" if bad else "### Added\n\n- a good entry\n")
    return root


def _fold(root: Path, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(CHANGELOG_FOLD), "--root", str(root), "check", *args],
                          capture_output=True, text=True, env={**os.environ, **(env or {})}, check=False)


def test_changelog_block_mode_fails_a_planted_format_error_as_it_always_did(tmp_path: Path) -> None:
    done = _fold(_changelog_root(tmp_path, bad=True))
    assert done.returncode == 10 and "nexus-aaa.1.md" in done.stderr and "LEDGER" not in done.stdout + done.stderr


def test_changelog_report_mode_lists_the_planted_error_and_exits_zero(tmp_path: Path) -> None:
    done = _fold(_changelog_root(tmp_path, bad=True), env={ledger_mode.ENV: "report", "GITHUB_ACTIONS": "true"})
    assert done.returncode == 0, done.stderr
    out = done.stdout.splitlines()
    assert any(ln.startswith("LEDGER-FINDING changelog: ") and "nexus-aaa.1.md" in ln for ln in out)
    assert any(ln.startswith("::warning title=changelog::") for ln in out)
    assert "LEDGER-REPORT changelog: 1 finding(s), 2 item(s) examined (mode=report)" in out
    assert "changelog_fold check: ok" not in out, "a report with findings is not the clean marker"


def test_changelog_report_mode_on_a_clean_tree_keeps_the_ok_line_and_the_flag_mirrors_the_variable(tmp_path: Path) -> None:
    done = _fold(_changelog_root(tmp_path / "clean", bad=False), "--mode", "report")
    assert done.returncode == 0 and "changelog_fold check: ok" in done.stdout
    assert "LEDGER-REPORT changelog: 0 finding(s), 2 item(s) examined" in done.stdout
    bad = _changelog_root(tmp_path / "bad", bad=True)
    assert _fold(bad, "--mode", "block", env={ledger_mode.ENV: "report"}).returncode == 10, "the flag wins over the variable"


def test_changelog_report_mode_that_examined_nothing_fails(tmp_path: Path) -> None:
    done = _fold(tmp_path, env={ledger_mode.ENV: "report"})   # no fragments directory, no changelog
    assert done.returncode == 10 and "LEDGER-VACUOUS changelog:" in done.stdout
    assert _fold(tmp_path).returncode == 10, "block mode fails the same tree for the missing changelog"


def test_changelog_a_bad_mode_value_exits_with_an_error(tmp_path: Path) -> None:
    done = _fold(_changelog_root(tmp_path, bad=False), env={ledger_mode.ENV: "warn"})
    assert done.returncode == 10 and "NX_LEDGER_MODE" in done.stderr


# ── cloud-leg route ledger ───────────────────────────────────────────────────

def _routes(tmp_path: Path, text: str) -> list:
    ledger = tmp_path / "wire.md"
    ledger.write_text(text)
    return tcl.cl.ledger_entries(ledger)


def test_cloud_leg_block_mode_fails_an_unmapped_route(tmp_path: Path) -> None:
    with pytest.raises(AssertionError, match=r"map these in ROUTE_MAP.*POST /v1/widgets/frob"):
        check_routes(tcl.cl, _routes(tmp_path, tcl._FIXTURE_LEDGER))


def test_cloud_leg_report_mode_lists_the_unmapped_route_and_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                                    capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv(ledger_mode.ENV, "report")
    assert check_routes(tcl.cl, _routes(tmp_path, tcl._FIXTURE_LEDGER)) == 3
    out = capsys.readouterr().out.splitlines()
    assert any(ln.startswith("LEDGER-FINDING cloud-leg-routes: bbbbbbb nexus-x.2 POST /v1/widgets/frob has no ROUTE_MAP entry") for ln in out)
    assert "LEDGER-REPORT cloud-leg-routes: 1 finding(s), 3 item(s) examined (mode=report)" in out


def test_cloud_leg_a_ledger_with_no_routes_fails_in_both_modes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    entries = _routes(tmp_path, "## Unshipped\n\n- `ccccccc3333` -- bead nexus-x.3 -- engine tag `engine-service-v9.9.9` -- "
                                "[additive] text only. OLD client + NEW engine is safe.\n\n## Shipped\n")
    for value in ("block", "report"):
        monkeypatch.setenv(ledger_mode.ENV, value)
        with pytest.raises(AssertionError, match="vacuous"):
            check_routes(tcl.cl, entries)


# ── plugin ledger (a real git world) ─────────────────────────────────────────

def test_plugin_block_mode_fails_undeclared_drift_and_report_mode_lists_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                                            capsys: pytest.CaptureFixture[str]) -> None:
    plug.TestPerPluginDriftScoping._cut_world(tmp_path, monkeypatch, sn_drift=True)
    with pytest.raises(AssertionError, match=SN_PROBE):
        plug.test_every_drifted_file_is_declared_in_the_ledger()
    monkeypatch.setenv(ledger_mode.ENV, "report")
    plug.test_every_drifted_file_is_declared_in_the_ledger()
    out = capsys.readouterr().out.splitlines()
    assert any(ln.startswith("LEDGER-FINDING plugin-ledger: undeclared drift: " + SN_PROBE) for ln in out)
    assert any(ln.startswith("LEDGER-REPORT plugin-ledger: 1 finding(s), ") and "examined (mode=report)" in ln for ln in out)


def test_plugin_report_mode_lists_a_stale_entry_that_block_mode_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                                       capsys: pytest.CaptureFixture[str]) -> None:
    repo = plug.TestPerPluginDriftScoping._cut_world(tmp_path, monkeypatch, sn_drift=False)
    with (repo / "conexus" / "PENDING_RELEASE.md").open("a", encoding="utf-8") as fh:
        fh.write("- `conexus/hooks/hook.py`: shipped by the v9.9.1 release, so stale (fixture)\n")
    with pytest.raises(AssertionError, match="NO LONGER differ"):
        plug.test_the_ledger_has_no_stale_entries()
    monkeypatch.setenv(ledger_mode.ENV, "report")
    plug.test_the_ledger_has_no_stale_entries()
    assert "LEDGER-FINDING plugin-ledger: stale entry: conexus/hooks/hook.py" in capsys.readouterr().out


def test_plugin_a_marketplace_with_no_pinned_plugin_fails_in_both_modes_before_report_mode_counts_anything(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The plugin ledger's examined count is the plugins it judged, so it is never zero: a marketplace with none dies on
    _pinned_refs's own assertion first. settle's LEDGER-VACUOUS branch is the unit test above, not reachable from this check."""
    repo = plug.TestPerPluginDriftScoping._cut_world(tmp_path, monkeypatch, sn_drift=False)
    (repo / ".claude-plugin" / "marketplace.json").write_text(json.dumps({"plugins": []}))
    for value in ("block", "report"):
        monkeypatch.setenv(ledger_mode.ENV, value)
        with pytest.raises(AssertionError, match="declares no plugin with a source.ref"):
            plug.test_every_drifted_file_is_declared_in_the_ledger()


# ── the push gate runs the checks under the switch ───────────────────────────

def _gate_tree(tmp_path: Path, *, bad: bool) -> Path:
    tree = _changelog_root(tmp_path / "tree", bad=bad)
    (tree / "scripts").mkdir()
    for name in ("changelog_fold.py", "ledger_mode.py"):
        shutil.copy(REPO / "scripts" / name, tree / "scripts" / name)
    return tree


def _run_changelog(tree: Path, reason: str = "", spec=None):
    return gate.run_check(spec or gate.SPECS["changelog"], tree, [sys.executable], [sys.executable], reason)


def test_the_gate_refuses_a_ledger_finding_by_default(tmp_path: Path) -> None:
    out = _run_changelog(_gate_tree(tmp_path, bad=True))
    assert out.ok is False and "nexus-aaa.1.md" in out.detail


def test_the_gate_passes_a_finding_only_under_the_ledger_report_flag_and_hands_its_lines_on(tmp_path: Path) -> None:
    out = _run_changelog(_gate_tree(tmp_path, bad=True), "demoting while the ledger is rebuilt")
    assert out.ok is True, out.detail
    assert any(ln.startswith("LEDGER-FINDING changelog:") for ln in out.lines)
    assert any(ln.startswith("LEDGER-REPORT changelog: 1 finding(s)") for ln in out.lines)


def test_the_gate_still_refuses_a_report_mode_run_that_examined_nothing(tmp_path: Path) -> None:
    tree = _gate_tree(tmp_path, bad=False)
    shutil.rmtree(tree / "changelog")
    (tree / "CHANGELOG.md").unlink()
    assert _run_changelog(tree, "reason").ok is False


@pytest.mark.parametrize("value", ["report", "REPORT", "warn", "block"])
def test_nx_ledger_mode_in_the_environment_demotes_nothing_on_the_gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                                       value: str) -> None:
    monkeypatch.setenv(ledger_mode.ENV, value)
    out = _run_changelog(_gate_tree(tmp_path, bad=True))
    assert out.ok is False and "nexus-aaa.1.md" in out.detail, "the finding still refuses, whatever the variable says"


def test_the_ledger_checks_are_the_three_and_need_the_helper() -> None:
    assert {n for n, s in gate.SPECS.items() if s.ledger} == {"changelog", "cloud-leg", "plugin-ledger"}
    assert all(gate.LEDGER_MODE_SCRIPT in gate.SPECS[n].needs for n in ("changelog", "cloud-leg", "plugin-ledger"))


def test_the_gate_reads_report_lines_that_pytest_printed_at_the_start_of_a_line() -> None:
    text = ("..\nLEDGER-FINDING plugin-ledger: stale entry: a\n"
            "LEDGER-REPORT plugin-ledger: 1 finding(s), 4 item(s) examined (mode=report)\n.. 30 passed in 1s\n")
    assert gate._LEDGER_LINE.findall(text) == ["LEDGER-FINDING plugin-ledger: stale entry: a",
                                               "LEDGER-REPORT plugin-ledger: 1 finding(s), 4 item(s) examined (mode=report)"]


def test_a_spec_default_mode_applies_without_the_flag_and_the_environment_cannot_change_it(tmp_path: Path,
                                                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    import dataclasses  # noqa: PLC0415

    modes = {n: s.default_mode for n, s in gate.SPECS.items() if s.ledger}
    assert modes == {"changelog": "block", "cloud-leg": "block", "plugin-ledger": "block"}, "today's behaviour"
    spec = dataclasses.replace(gate.SPECS["changelog"], default_mode="report")
    tree = _gate_tree(tmp_path, bad=True)
    assert _run_changelog(tree, spec=spec).ok is True
    monkeypatch.setenv(ledger_mode.ENV, "block")
    assert _run_changelog(tree, spec=spec).ok is True, "an exported variable does not move the spec's mode either way"
    assert gate.ledger_mode_for(gate.SPECS["changelog"]) == ("block", "default")
    assert gate.ledger_mode_for(gate.SPECS["changelog"], "why") == ("report", "flag")


# ── run_gate: the mode is printed, recorded as an override, and the relay is capped ──

def _gate_sh_repo(tmp_path: Path) -> tuple[Path, str, str]:
    """A repository whose develop is armed (carries the size ratchet) and whose tip changes a changelog fragment."""
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    for name in ("file_size_ratchet.py", "changelog_fold.py", "ledger_mode.py", "context_budgets.py"):
        shutil.copy(REPO / "scripts" / name, repo / "scripts" / name)
    _changelog_root(repo, bad=False)

    def run(*args: str) -> str:
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e.com", "GIT_COMMITTER_NAME": "t",
               "GIT_COMMITTER_EMAIL": "t@e.com", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
        return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, env=env, check=True).stdout.strip()

    run("init", "-q", "-b", "develop")
    run("add", "-A")
    run("commit", "-q", "-m", "base")
    base = run("rev-parse", "HEAD")
    (repo / "changelog" / "unreleased" / "nexus-aaa.2.md").write_text("### Nonsense\n\n- bad section\n")
    run("add", "-A")
    run("commit", "-q", "-m", "tip")
    return repo, base, run("rev-parse", "HEAD")


def _run_gate(repo: Path, base: str, tip: str, tmp_path: Path, **kw):
    import io  # noqa: PLC0415

    out, err = io.StringIO(), io.StringIO()
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    code = gate.run_gate(repo, base, tip, [], {"file-size": "not this test", "context-budget": "not this test"}, work, sys.executable,
                         out=out, err=err, **kw)
    return code, out.getvalue(), err.getvalue()


def test_run_gate_prints_each_ledger_checks_mode_and_hands_a_flag_override_to_the_record(tmp_path: Path,
                                                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    repo, base, tip = _gate_sh_repo(tmp_path)
    monkeypatch.setenv(ledger_mode.ENV, "report")                       # ignored, and said so
    code, out, err = _run_gate(repo, base, tip, tmp_path)
    assert code == 14 and "PUSH_CHECK_LEDGER_MODE changelog mode=block source=default" in out and "PUSH_REFUSED_CHANGELOG" in out
    assert "PUSH_CHECK_NOTE NX_LEDGER_MODE='report' in the environment is ignored" in err
    monkeypatch.delenv(ledger_mode.ENV)
    code, out, err = _run_gate(repo, base, tip, tmp_path, ledger_report="the ledger is being rebuilt")
    assert code == 0, out + err
    assert "PUSH_CHECK_LEDGER_MODE changelog mode=report source=flag reason=the ledger is being rebuilt" in out
    assert "PUSH_CHECK_REPORT changelog: LEDGER-FINDING changelog:" in err and "PUSH_CHECK_NOTE" not in err


def test_the_relay_of_a_report_is_capped_with_a_more_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, base, tip = _gate_sh_repo(tmp_path)
    many = ["LEDGER-FINDING plugin-ledger: finding %d" % i for i in range(gate.MAX_REPORT_FINDINGS + 5)]
    summary = "LEDGER-REPORT plugin-ledger: %d finding(s), 99 item(s) examined (mode=report)" % len(many)
    monkeypatch.setattr(gate, "run_check", lambda spec, tree, py, pc, reason="": gate.Outcome(spec.name, True, "", 0.1,
                        [*many, summary] if spec.ledger else [], reported=bool(spec.ledger)))
    code, out, err = _run_gate(repo, base, tip, tmp_path, ledger_report="r")
    shown = [ln for ln in err.splitlines() if "LEDGER-FINDING plugin-ledger" in ln]
    assert code == 0 and len(shown) == gate.MAX_REPORT_FINDINGS
    assert "PUSH_CHECK_REPORT changelog: +5 more finding(s); reproduce:" in err
    assert f"PUSH_CHECK_REPORT changelog: {summary}" in err, "the summary line always comes through"


# ── a real pytest run prints a report-mode finding on its own stdout (what the push gate and a CI log read) ──

END_TO_END = '''
import json

import pytest

import tests.test_plugin_release_drift_ledger as plug
from tests import _cloud_leg_ledger
from tests._ledger_terminal import ledger_terminal  # noqa: F401
from tests.scripts import test_cloud_leg as tcl


def test_plugin_finding(tmp_path, monkeypatch, ledger_terminal):
    plug.TestPerPluginDriftScoping._cut_world(tmp_path, monkeypatch, sn_drift=True)
    plug.test_every_drifted_file_is_declared_in_the_ledger()


def test_cloud_leg_finding(tmp_path, ledger_terminal):
    ledger = tmp_path / "wire.md"
    ledger.write_text(tcl._FIXTURE_LEDGER)
    _cloud_leg_ledger.check_routes(tcl.cl, tcl.cl.ledger_entries(ledger))
'''


def test_a_report_mode_plugin_or_cloud_leg_finding_reaches_the_stdout_of_a_serial_pytest_run(tmp_path: Path) -> None:
    (tmp_path / "test_end_to_end.py").write_text(END_TO_END)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTEST_", "NX_TEST_", "NX_LEDGER"))}
    env.update({ledger_mode.ENV: "report", "NX_TEST_T2_SUBSTRATE": "none", "PYTHONPATH": os.pathsep.join([str(REPO / "scripts"), str(REPO)])})
    done = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-o", "addopts=", "--rootdir", str(tmp_path),
                           "test_end_to_end.py"], cwd=tmp_path, capture_output=True, text=True, env=env, check=False)
    assert done.returncode == 0, done.stdout + done.stderr
    lines = done.stdout.splitlines()
    assert any(ln.startswith("LEDGER-FINDING plugin-ledger: undeclared drift: sn/hooks/probe.py") for ln in lines), done.stdout
    assert any(ln.startswith("LEDGER-FINDING cloud-leg-routes: bbbbbbb nexus-x.2 POST /v1/widgets/frob") for ln in lines), done.stdout
    assert any(ln.startswith("LEDGER-REPORT plugin-ledger: 1 finding(s)") for ln in lines)
    assert any(ln.startswith("LEDGER-REPORT cloud-leg-routes: 1 finding(s)") for ln in lines)


# ── report lines survive pytest-xdist workers (test-lint runs -n 4) ──────────

XDIST_TESTS = '''
import ledger_mode
import pytest


@pytest.mark.parametrize("n", [1, 2, 3, 4])
def test_each_worker_reports(n):
    assert ledger_mode.settle("xdist-ledger", [f"finding {n}"], 7)
'''


def test_a_report_written_by_xdist_workers_reaches_the_report_file(tmp_path: Path) -> None:
    (tmp_path / "test_workers.py").write_text(XDIST_TESTS)
    report = tmp_path / "ledger-report.txt"
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTEST_", "NX_TEST_"))}
    env.update({ledger_mode.ENV: "report", ledger_mode.REPORT_FILE_ENV: str(report), "NX_TEST_T2_SUBSTRATE": "none",
                "PYTHONPATH": str(REPO / "scripts")})
    done = subprocess.run([sys.executable, "-m", "pytest", "-n", "2", "-q", "-p", "no:cacheprovider", "-o", "addopts=",
                           "--rootdir", str(tmp_path), "test_workers.py"], cwd=tmp_path, capture_output=True, text=True, env=env,
                          check=False)
    assert done.returncode == 0, done.stdout + done.stderr
    lines = sorted(report.read_text().splitlines())
    assert [ln for ln in lines if ln.startswith("LEDGER-FINDING")] == [f"LEDGER-FINDING xdist-ledger: finding {n}" for n in (1, 2, 3, 4)]
    assert sum(ln.startswith("LEDGER-REPORT xdist-ledger: 1 finding(s), 7 item(s)") for ln in lines) == 4
    assert "LEDGER-FINDING" not in done.stdout, "the workers' own stdout is what the file exists to replace"


def test_block_mode_writes_no_report_file(tmp_path: Path) -> None:
    report = tmp_path / "r.txt"
    env = {ledger_mode.ENV: "block", ledger_mode.REPORT_FILE_ENV: str(report)}
    assert ledger_mode.settle("x", ["f"], 3, environ=env) is False and not report.exists()


def test_the_release_preflight_pins_block_on_its_pin_tests() -> None:
    line = next(ln for ln in (REPO / "tests" / "e2e" / "release-preflight.sh").read_text().splitlines()
                if ln.startswith('check "pin-tests"'))
    assert "NX_LEDGER_MODE=block" in line, "a variable left exported by another session must not demote a release gate"


def test_the_dev_preflight_pins_block_on_the_changelog_check_and_the_plugin_ledger_test() -> None:
    lua = (REPO / ".nexus" / "procedures" / "preflight.lua").read_text()
    assert '"NX_LEDGER_MODE=block uv run python scripts/changelog_fold.py check"' in lua
    assert 'gated("NX_LEDGER_MODE=block " .. PYTEST .. " tests/test_plugin_release_drift_ledger.py' in lua


@pytest.mark.parametrize("reason", ["", "   "])
def test_the_gate_refuses_a_blank_ledger_report_reason(reason: str) -> None:
    with pytest.raises(SystemExit) as exc:
        gate.main(["--repo", ".", "--base", "x", "--tip", "y", "--ledger-report", reason])
    assert exc.value.code == 2
