# SPDX-License-Identifier: AGPL-3.0-or-later
"""run-scenario.sh reports a path run NOT-RUN when no MarkupEditor harness is in place (nexus-scmcs.2.14, critic C2).

A path run stops at step 6b of the skill in a headless session (no MarkupEditor tools), leaves no `brief.py filter`
output, and verdicts.py used to score the per-kind scenarios NOT-MEASURABLE with no reason. Now the runner records
the scenario in not-run.txt with the reason and exits 3, and verdicts.py prints NOT-RUN for it.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.prose_edit.conftest import ROOT

ACC = ROOT / "tests" / "prose_edit" / "acceptance"
F = "tests/prose_edit/fixtures"


def classify(prompt: str) -> bool:
    """runner_prompt_is_path_run's verdict for a prompt."""
    proc = subprocess.run(["bash", "-c", '. "$1"; runner_prompt_is_path_run "$2"', "x", str(ACC / "runner_guard.bash"), prompt],
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode in (0, 1), proc.stderr
    return proc.returncode == 0


@pytest.mark.parametrize("prompt", [
    f"/prose-edit {F}/qualifiers-a.md --genre reference-doc --budget 5",
    f"/prose-edit {F}/range-notes.md:10-20 --genre changelog",
    f"/conexus-prose-edit:prose-edit {F}/xanadu.md --genre exploration-essay",
    "/prose-edit ./rejections --genre reference-doc",
    "/prose-edit -- setup --genre how-to",
])
def test_a_path_run_prompt_is_a_path_run(prompt: str) -> None:
    assert classify(prompt)


@pytest.mark.parametrize("prompt", [
    "/prose-edit - --genre commit-message\n\nfix: make the queue drain in order basically",
    "/prose-edit -\n\nfix: make the queue drain",
    "/prose-edit setup",
    # prose-cut has no app: a cut-only run and its decide turn are not path runs (nexus-ger02.10)
    f"/conexus-prose-edit:prose-cut {F}/review-scenario.md --mode cut-only --voice-card c.txt --word-budget 40 --genre how-to",
    "/prose-cut docs/x.md --mode cut-only --voice-card c.txt --word-budget 40 --genre how-to --budget 20",
    "/conexus-prose-edit:prose-cut decide /tmp/prose-edit-ab12cd34 --accept 2-4 --reject 1",
    "/prose-edit rejections docs/x.md",
    "/prose-edit exemplar rdr docs/x.md:1-3",
    "Run the shell command `nx --version` with the Bash tool and print its exact output. Do nothing else.",
    "Yes, apply.",
])
def test_a_stdin_run_a_subcommand_a_prose_cut_run_and_an_answer_turn_are_not_path_runs(prompt: str) -> None:
    assert not classify(prompt)


def run_scenario(tmp_path: Path, name: str, prompt: str, **env: str) -> subprocess.CompletedProcess[str]:
    marker = tmp_path / "claude-was-called"
    stub = tmp_path / "claude"
    stub.write_text(f"#!/bin/sh\ntouch {marker}\nexit 0\n", encoding="utf-8")
    stub.chmod(0o755)
    e = {**os.environ, "OUT_DIR": str(tmp_path / "out"), "CLAUDE_BIN": str(stub), "TMPDIR": str(tmp_path)}
    e.pop("PROSE_EDIT_APP_RUNNER", None)
    e.update(env)
    return subprocess.run(["bash", str(ACC / "run-scenario.sh"), name, prompt], capture_output=True, text=True,
                          env=e, timeout=60)


@pytest.mark.parametrize("name", ["xanadu-1", "linda-1", "refrain-1", "qa-1", "qb-1", "qc-1", "protected-1", "budget-1",
                                  "range-1", "compression-1"])
def test_a_per_kind_scenario_is_not_run_without_a_harness_and_says_why(tmp_path: Path, name: str) -> None:
    proc = run_scenario(tmp_path, name, f"/prose-edit {F}/qualifiers-a.md --genre reference-doc --budget 5")
    assert proc.returncode == 3, proc.stderr
    assert not (tmp_path / "claude-was-called").exists()  # no session, so no tokens
    recorded = (tmp_path / "out" / "not-run.txt").read_text(encoding="utf-8")
    assert recorded.startswith(f"{name}: ") and "MarkupEditor" in recorded
    assert "NOT-RUN" in proc.stderr
    assert not (tmp_path / "out" / f"{name}.jsonl").exists() and not (tmp_path / "out" / f"{name}.rc").exists()


def test_a_prose_cut_scenario_runs_without_a_harness(tmp_path: Path) -> None:
    run_scenario(tmp_path, "cutonly-1", f"/conexus-prose-edit:prose-cut {F}/review-scenario.md --mode cut-only "
                 "--voice-card c.txt --word-budget 40 --genre how-to")
    assert not (tmp_path / "out" / "not-run.txt").exists()  # no MarkupEditor is needed, so the guard lets it start


def test_the_harness_switch_lets_a_path_run_through(tmp_path: Path) -> None:
    run_scenario(tmp_path, "xanadu-1", f"/prose-edit {F}/qualifiers-a.md --genre reference-doc", PROSE_EDIT_APP_RUNNER="1")
    assert not (tmp_path / "out" / "not-run.txt").exists()


@pytest.mark.parametrize(("name", "prompt"), [
    ("canary-fail", f"/prose-edit {F}/qualifiers-a.md --genre reference-doc --budget 5"),   # T2 fails before the app
    ("unmapped", f"/prose-edit {F}/unmapped-notes.txt"),                                      # asks the genre first
    ("stdin", "/prose-edit - --genre commit-message\n\nfix: x basically"),
    ("canary-nx", "Run the shell command `nx --version` with the Bash tool."),
])
def test_a_scenario_that_stops_before_the_app_is_not_guarded(tmp_path: Path, name: str, prompt: str) -> None:
    run_scenario(tmp_path, name, prompt)
    assert not (tmp_path / "out" / "not-run.txt").exists(), name


def test_verdicts_scores_a_not_run_scenario_not_run_and_exits_3(tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    (out / "not-run.txt").write_text("xanadu-1: a path run reviews its edits in the MarkupEditor app\n"
                                     "qa-1: a path run reviews its edits in the MarkupEditor app\n", encoding="utf-8")
    proc = subprocess.run([sys.executable, str(ACC / "verdicts.py"), str(out)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 3, proc.stdout + proc.stderr
    rows = {ln.split()[0]: ln for ln in proc.stdout.splitlines() if ln.startswith(("xanadu-1", "qa-1"))}
    assert set(rows) == {"xanadu-1", "qa-1"}
    for ln in rows.values():
        assert "NOT-RUN" in ln and "MarkupEditor app" in ln and "NOT-MEASURABLE" not in ln


def test_a_failed_run_still_wins_over_a_not_run_scenario(tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    (out / "not-run.txt").write_text("xanadu-1: reason\n", encoding="utf-8")
    (out / "canary-nx.rc").write_text("rc=0\n", encoding="utf-8")
    (out / "canary-nx.jsonl").write_text("", encoding="utf-8")  # no nx attempt: FAIL
    proc = subprocess.run([sys.executable, str(ACC / "verdicts.py"), str(out)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 1, proc.stdout


def test_the_readme_names_the_per_kind_scenarios_in_its_not_run_table() -> None:
    text = (ACC / "README.md").read_text(encoding="utf-8")
    table = text[text.index("## Path-run review is NOT-RUN"):text.index("## One runner at a time")]
    assert "run-scenario.sh" in table and "PROSE_EDIT_APP_RUNNER" in table
    for kind in ("xanadu", "qa", "compression"):
        assert kind in table, kind
    assert "canary-fail" in table and "unmapped" in table  # the ones that stay valid, and why
    assert "marked-up copy" not in text  # the Typora copy is gone
