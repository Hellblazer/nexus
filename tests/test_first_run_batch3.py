# SPDX-License-Identifier: AGPL-3.0-or-later
"""First-run triage lines for 8.1.3, batch three (nexus-f3yqd.6): F2, F4, F9, F17, F22, F23."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from nexus.mcp import setup_tools

ROOT = Path(__file__).resolve().parents[1]


# F2: a CLI older than the setup tools (pre-7.81) runs its own `nx self install`, whose uv cache prune (bounded only
# since 8.1.0) can wait silently; the skill says so instead of letting the user think it hung.
def test_the_setup_skill_warns_an_old_cli_user_about_the_silent_prune() -> None:
    text = " ".join((ROOT / "conexus" / "commands" / "setup.md").read_text(encoding="utf-8").split())
    assert "can sit silent for up to ten minutes" in text
    assert "it is not stuck and to let it finish" in text


# F4: the setup tool's own failure named no cause and no log, and offered `nx init` (the install that may have failed).

def _child(monkeypatch: pytest.MonkeyPatch, *, code: int, out: str = "", err: str = "") -> None:
    monkeypatch.setattr(setup_tools, "run_bounded", lambda *a, **k: subprocess.CompletedProcess([], code, out, err))


def test_a_failed_setup_process_names_its_cause_before_the_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    _child(monkeypatch, code=1, err="Traceback (most recent call last):\n  File x\nPermissionError: [Errno 13] setup dir\n")
    with pytest.raises(setup_tools.SetupError) as raised:
        setup_tools._call_script("nx_setup.py", ["status"])
    text = str(raised.value)
    assert text == (
        "The setup process failed (exit 1) and printed no answer. It printed: PermissionError: [Errno 13] setup dir"
        " Setup can also be done in a terminal: the user runs `nx init`."
    ), text


def test_with_no_stderr_the_message_says_only_what_it_knows(monkeypatch: pytest.MonkeyPatch) -> None:
    _child(monkeypatch, code=0, out="not json\n")
    with pytest.raises(setup_tools.SetupError) as raised:
        setup_tools._call_script("nx_setup.py", ["status"])
    assert str(raised.value) == (
        "The setup process printed something other than JSON. Setup can also be done in a terminal: the user runs `nx init`."
    )
