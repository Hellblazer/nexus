# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-ln9y5: keep RDR command preambles in sync with their source scripts.

The 9 RDR-lifecycle slash commands inline their Python preamble into a
documented ```! fenced block via a ``python3 <<'PYEOF'`` heredoc (the brace
``!{ }`` form never executed; the by-path ``$CLAUDE_PLUGIN_ROOT`` form failed
because that var is empty in command bash). The canonical, directly-unit-tested
code lives in ``conexus/resources/rdr_commands/<name>.py``; the ``.md`` inlines
its code-after-docstring verbatim. This test enforces md-body == .py code so the
two never drift, and runs each inlined body to prove it executes (Layer 1).
"""
from __future__ import annotations

import ast
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
CMD = REPO_ROOT / "conexus" / "commands"
RES = REPO_ROOT / "conexus" / "resources" / "rdr_commands"

# command stem -> script stem
RDR_COMMANDS = {
    "rdr-list": "rdr_list",
    "rdr-show": "rdr_show",
    "rdr-gate": "rdr_gate",
    "rdr-accept": "rdr_accept",
    "rdr-close": "rdr_close",
    "rdr-create": "rdr_create",
    "rdr-research": "rdr_research",
    "rdr-audit": "rdr_audit",
    "phase-review-gate": "phase_review_gate",
}

_FENCED = re.compile(r"(?ms)^```!\n(?P<body>.*?)\n```[ \t]*$")
_HEREDOC = re.compile(r"(?ms)<<'PYEOF'\n(?P<code>.*?)\nPYEOF$")


def _code_after_docstring(py_text: str) -> str:
    """Source with shebang/SPDX/module-docstring stripped (matches transformer)."""
    tree = ast.parse(py_text)
    body = tree.body
    start = 0
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(getattr(body[0], "value", None), ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        start = body[0].end_lineno
    return "\n".join(py_text.splitlines()[start:]).strip("\n")


def _inlined_code(md_text: str) -> str:
    fenced = _FENCED.search(md_text)
    assert fenced, "no ```! fenced block"
    hd = _HEREDOC.search(fenced.group("body"))
    assert hd, "no python3 <<'PYEOF' heredoc inside the ```! block"
    return hd.group("code")


@pytest.mark.parametrize("cmd,script", sorted(RDR_COMMANDS.items()))
def test_md_heredoc_matches_script_source(cmd: str, script: str) -> None:
    md = (CMD / f"{cmd}.md").read_text()
    py = (RES / f"{script}.py").read_text()
    assert _inlined_code(md) == _code_after_docstring(py), (
        f"{cmd}.md inlined body has drifted from {script}.py. "
        "Re-inline the script's code-after-docstring (nexus-ln9y5)."
    )


@pytest.mark.parametrize("cmd,script", sorted(RDR_COMMANDS.items()))
def test_inlined_body_executes(cmd: str, script: str) -> None:
    """The inlined code runs and prints output (no NameError from a bad strip)."""
    code = _inlined_code((CMD / f"{cmd}.md").read_text())
    # Args that exercise the happy path without mutating state.
    env = {**os.environ, "NEXUS_RDR_ARGS": "129"}
    r = subprocess.run(
        [sys.executable, "-c", code], input="", capture_output=True, text=True, env=env
    )
    assert r.returncode == 0, f"{cmd} inlined body crashed:\n{r.stderr}"
    assert r.stdout.strip(), f"{cmd} inlined body produced no output"


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
