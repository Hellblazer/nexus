# SPDX-License-Identifier: AGPL-3.0-or-later
"""The audience harness keeps its seam: a hook script it runs is not re-exec'd into another interpreter.

The harness sets ``_audience._ACCOUNT_HOME_OVERRIDE`` in its own process and runs the script under ``runpy`` as ``__main__``.
A hook script's ``_interpreter.reexec_if_needed()`` acts when its caller is ``__main__`` and ``resolve()`` names an interpreter
other than the running one, and it ``execv``s there: a fresh process, with the seam gone and the real account home back. On a
maintainer's laptop and a lab host uv's python and the venv's python are one binary, so nothing re-exec'd and the leg passed;
on a CI runner (a thin venv beside another python) the flagged fixture lost its flag and the guard passed a commit it must deny
(develop CI, run 45710195105). The harness now sets the re-exec marker itself.
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

from tests.hooks.audience_harness import strict_home, plain_repo

ROOT = Path(__file__).resolve().parents[2]
HARNESS = ROOT / "tests" / "hooks" / "audience_harness.py"
GUARD = ROOT / "conexus" / "hooks" / "scripts" / "routing" / "subagent_git_write_requires_orchestrator.py"


def _another_python(tmp_path: Path) -> tuple[Path, Path]:
    """An interpreter ``resolve()`` accepts that is not this one, and records that it was started for a script."""
    ran = tmp_path / "reexec-ran"
    stub = tmp_path / "other-python"
    stub.write_text(
        '#!/bin/sh\ncase "$1" in -c) exit 0 ;; esac\n'
        f'echo "$@" > "{ran}"\nexit 97\n',
        encoding="utf-8",
    )
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    return stub, ran


def test_the_harness_does_not_let_a_script_re_exec_away_from_its_seam(tmp_path: Path) -> None:
    other, ran = _another_python(tmp_path)
    repo = plain_repo(tmp_path / "users-repo")
    home = strict_home(tmp_path / "acct")
    payload = {
        "tool_name": "Bash", "tool_input": {"command": "git commit -am x"}, "cwd": repo,
        "agent_id": "aworker-x-0000000000", "agent_type": "general-purpose", "session_id": "s",
    }
    env = {**os.environ, "NX_HOOK_PYTHON": str(other)}
    env.pop("NX_HOOK_INTERPRETER_REEXEC", None)
    proc = subprocess.run(
        [sys.executable, str(HARNESS), home, str(GUARD)], input=json.dumps(payload), capture_output=True, text=True,
        encoding="utf-8", timeout=60, env=env,
    )
    assert not ran.exists(), "the guard re-exec'd into another interpreter, so the harness's account seam was lost"
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny", "the flagged account's guard"
