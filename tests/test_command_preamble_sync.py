# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-ln9y5 / nexus-2fnet: guard the 16 agent-relay fenced command blocks.

RDR-130 P1.4 flipped the 9 RDR-lifecycle commands from inline Python heredocs
to ``!`nx rdr preamble <name> -- "$ARGUMENTS"``` calls.  The heredoc-sync tests
(test_md_heredoc_matches_script_source, test_inlined_body_executes) and the
``conexus/resources/rdr_commands/*.py`` scripts they checked against are gone.

This file now covers only the 16 still-fenced agent-relay commands:
  - ``test_project_detector_identifies_python``: regression that the project-type
    detector emits ``- Python`` for this repo (the old JVM/Node-only logic
    produced "Unknown").
  - ``test_fenced_block_executes_with_output``: every ````` ```! ````` block runs
    cleanly and emits at least one line of output — effect check, not format check.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
CMD = REPO_ROOT / "conexus" / "commands"

_FENCED = re.compile(r"(?ms)^```!\n(?P<body>.*?)\n```[ \t]*$")


DETECTOR_COMMANDS = ["analyze-code", "architecture", "create-plan", "implement"]


@pytest.mark.parametrize("cmd", DETECTOR_COMMANDS)
def test_project_detector_identifies_python(cmd: str) -> None:
    """The agent-relay project-type detector must identify nexus as Python.

    Correctness check (nexus-ln9y5): the original four detectors knew only
    Maven/Gradle/Node, so they labeled this Python repo 'Unknown' (analyze-code)
    or emitted nothing (create-plan). The unified marker-file detector covers
    ~21 ecosystems and lists all matches. This guards against regressing to the
    JVM/Node-only logic. Runs from the repo root, where pyproject.toml lives.
    """
    if shutil.which("bash") is None:
        pytest.skip("bash not available")
    body = _FENCED.search((CMD / f"{cmd}.md").read_text()).group("body")
    env = {**os.environ, "ARGUMENTS": "", "NEXUS_RDR_ARGS": "", "NEXUS_PROJECT_ROOTS": ""}
    r = subprocess.run(
        ["bash", "-c", body], capture_output=True, text=True, timeout=120, env=env
    )
    assert r.returncode == 0, f"{cmd}: detector block exited {r.returncode}"
    assert "- Python" in r.stdout, f"{cmd}: did not detect nexus as Python:\n{r.stdout[:600]}"
    assert "Unknown" not in r.stdout, f"{cmd}: reported Unknown for a Python repo"


def _all_fenced_commands() -> list[Path]:
    return sorted(p for p in CMD.glob("*.md") if _FENCED.search(p.read_text()))


@pytest.mark.parametrize(
    "cmd_path", _all_fenced_commands(), ids=lambda p: p.stem
)
def test_fenced_block_executes_with_output(cmd_path: Path) -> None:
    """Every command's ```! block actually runs and emits output (nexus-ln9y5).

    This is the effect check, not a format check: it runs the WHOLE fenced bash
    block as the harness would (env prefix + `python3 <<'PYEOF'` heredoc for the
    RDR commands, plain shell for the rest) and asserts a clean exit with real
    output. The legacy !{ } commands never executed at all; the t1b1k by-path
    form exited non-zero (empty $CLAUDE_PLUGIN_ROOT). Both would fail here.
    The blocks are written defensively against absent tools (nx/bd), so a clean
    exit holds even on a minimal CI runner.
    """
    if shutil.which("bash") is None:
        pytest.skip("bash not available")
    body = _FENCED.search(cmd_path.read_text()).group("body")
    env = {**os.environ, "ARGUMENTS": "", "NEXUS_RDR_ARGS": "", "NEXUS_PROJECT_ROOTS": ""}
    r = subprocess.run(
        ["bash", "-c", body], capture_output=True, text=True, timeout=120, env=env
    )
    assert r.returncode == 0, (
        f"{cmd_path.name}: ```! block exited {r.returncode} (nexus-ln9y5):\n"
        f"{r.stderr[:800]}"
    )
    assert r.stdout.strip(), f"{cmd_path.name}: ```! block produced no output"
