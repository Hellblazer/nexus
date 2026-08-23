# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The Sweep step's rc handling, EXECUTED rather than grepped.

scheduled-failure-watch.yml runs the watchdog, captures its exit code, and
publishes it as the ``findings`` output. The consumer step branches on
``FINDINGS == "1"`` and, on anything else, CLOSES the tracking issue with
"All scheduled workflows are green and firing on cadence." So every exit
code the Sweep step forwards is a verdict, and any code it forwards by
accident becomes a false all-clear over a watchdog that never ran.

Only 0 and 1 are verdicts. 2 is a usage error; 127/124/137 are a missing
interpreter, a timeout, and a kill. The step used to special-case 2 alone.

This module executes the step's REAL shell -- the literal ``run:`` block
parsed out of the workflow -- with a stub ``uv`` on PATH that exits with a
chosen code. That is the pattern tests/test_dorny_guard_shell_logic.py
already established in this repo for the same problem: shell inside a
``run:`` block is executed by nothing in the suite, so its first execution
is a real CI run. Grepping the block for the string "exit 1" would assert
nothing about which codes reach it.
"""
from __future__ import annotations

import os
import pathlib
import subprocess

import pytest
import yaml

REPO_ROOT = pathlib.Path(__file__).parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "scheduled-failure-watch.yml"


def _sweep_run_block() -> str:
    wf = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    for job in wf["jobs"].values():
        for step in job.get("steps") or []:
            if step.get("id") == "sweep":
                return step["run"]
    pytest.fail("no step with id 'sweep' in scheduled-failure-watch.yml")


def _run_sweep_with_watchdog_exiting(code: int, tmp_path: pathlib.Path):
    """Execute the real Sweep block with a stub `uv` exiting *code*."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "uv"
    stub.write_text(f"#!/bin/bash\nexit {code}\n")
    stub.chmod(0o755)

    github_output = tmp_path / "gh_output"
    github_output.touch()
    env = dict(
        os.environ,
        PATH=f"{bin_dir}:{os.environ['PATH']}",
        GITHUB_OUTPUT=str(github_output),
    )
    proc = subprocess.run(
        ["bash", "-c", _sweep_run_block()],
        cwd=tmp_path, capture_output=True, text=True, timeout=60, env=env,
    )
    return proc, github_output.read_text()


def test_the_block_is_not_vacuous(tmp_path: pathlib.Path) -> None:
    """Guards the extraction: an empty block would pass every case below."""
    block = _sweep_run_block()
    assert "scheduled_workflow_watchdog.py" in block
    assert "GITHUB_OUTPUT" in block


@pytest.mark.parametrize("code", [0, 1])
def test_a_real_verdict_is_forwarded(code: int, tmp_path: pathlib.Path) -> None:
    proc, output = _run_sweep_with_watchdog_exiting(code, tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert f"findings={code}" in output


@pytest.mark.parametrize("code", [2, 3, 124, 127, 137])
def test_a_non_verdict_exit_fails_the_step(code: int, tmp_path: pathlib.Path) -> None:
    """THE REGRESSION for everything except 2, which was already handled.

    A forwarded non-verdict reaches the consumer as FINDINGS != "1" and
    closes the tracking issue with an all-clear.
    """
    proc, output = _run_sweep_with_watchdog_exiting(code, tmp_path)
    assert proc.returncode != 0, (
        f"watchdog exit {code} is not a verdict; the step must fail rather "
        f"than forward it. stdout={proc.stdout!r}"
    )
    assert "findings=" not in output, (
        f"exit {code} was forwarded as a verdict: {output!r} -- the consumer "
        "step would read it as 'not 1' and close the tracking issue"
    )
