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
    for name in (ledger_mode.ENV, "GITHUB_ACTIONS"):
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


def test_plugin_report_mode_with_no_pinned_plugin_examines_nothing_and_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = plug.TestPerPluginDriftScoping._cut_world(tmp_path, monkeypatch, sn_drift=False)
    (repo / ".claude-plugin" / "marketplace.json").write_text(json.dumps({"plugins": []}))
    monkeypatch.setenv(ledger_mode.ENV, "report")
    with pytest.raises(AssertionError, match="declares no plugin with a source.ref"):
        plug.test_every_drifted_file_is_declared_in_the_ledger()


# ── the push gate runs the checks under the switch ───────────────────────────

def _gate_tree(tmp_path: Path, *, bad: bool) -> Path:
    tree = _changelog_root(tmp_path / "tree", bad=bad)
    (tree / "scripts").mkdir()
    for name in ("changelog_fold.py", "ledger_mode.py"):
        shutil.copy(REPO / "scripts" / name, tree / "scripts" / name)
    return tree


def _run_changelog(tree: Path):
    return gate.run_check(gate.SPECS["changelog"], tree, [sys.executable], [sys.executable])


def test_the_gate_refuses_a_ledger_finding_by_default(tmp_path: Path) -> None:
    out = _run_changelog(_gate_tree(tmp_path, bad=True))
    assert out.ok is False and "nexus-aaa.1.md" in out.detail


def test_the_gate_passes_a_report_mode_finding_and_hands_its_lines_on(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ledger_mode.ENV, "report")
    out = _run_changelog(_gate_tree(tmp_path, bad=True))
    assert out.ok is True, out.detail
    assert any(ln.startswith("LEDGER-FINDING changelog:") for ln in out.lines)
    assert any(ln.startswith("LEDGER-REPORT changelog: 1 finding(s)") for ln in out.lines)


def test_the_gate_still_refuses_a_report_mode_run_that_examined_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ledger_mode.ENV, "report")
    tree = _gate_tree(tmp_path, bad=False)
    shutil.rmtree(tree / "changelog")
    (tree / "CHANGELOG.md").unlink()
    assert _run_changelog(tree).ok is False


def test_the_gate_refuses_a_bad_mode_value_and_pins_the_mode_for_the_three_ledger_checks(tmp_path: Path,
                                                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ledger_mode.ENV, "warn")
    out = _run_changelog(_gate_tree(tmp_path, bad=False))
    assert out.ok is False and "NX_LEDGER_MODE" in out.detail
    assert {n for n, s in gate.SPECS.items() if s.ledger} == {"changelog", "cloud-leg", "plugin-ledger"}
    assert all(gate.LEDGER_MODE_SCRIPT in gate.SPECS[n].needs for n in ("changelog", "cloud-leg", "plugin-ledger"))


def test_the_gate_reads_report_lines_that_pytest_printed_at_the_start_of_a_line() -> None:
    text = ("..\nLEDGER-FINDING plugin-ledger: stale entry: a\n"
            "LEDGER-REPORT plugin-ledger: 1 finding(s), 4 item(s) examined (mode=report)\n.. 30 passed in 1s\n")
    assert gate._LEDGER_LINE.findall(text) == ["LEDGER-FINDING plugin-ledger: stale entry: a",
                                               "LEDGER-REPORT plugin-ledger: 1 finding(s), 4 item(s) examined (mode=report)"]


def test_a_spec_default_mode_applies_only_when_the_variable_is_unset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import dataclasses  # noqa: PLC0415

    modes = {n: s.default_mode for n, s in gate.SPECS.items() if s.ledger}
    assert modes == {"changelog": "block", "cloud-leg": "block", "plugin-ledger": "block"}, "today's behaviour"
    spec = dataclasses.replace(gate.SPECS["changelog"], default_mode="report")
    tree = _gate_tree(tmp_path, bad=True)
    assert gate.run_check(spec, tree, [sys.executable], [sys.executable]).ok is True
    monkeypatch.setenv(ledger_mode.ENV, "block")
    assert gate.run_check(spec, tree, [sys.executable], [sys.executable]).ok is False, "the variable wins over a spec default"
