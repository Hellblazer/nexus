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
