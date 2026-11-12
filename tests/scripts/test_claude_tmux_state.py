# SPDX-License-Identifier: AGPL-3.0-or-later
"""scripts/claude-tmux-state: the by-hand verbs return at once even when stdin stays open.

`hold` and `need` are run by hand from a Claude Bash call, whose stdin can stay
open for the life of the call. The script used to read stdin for them too, so
`need` blocked until the caller's stdin closed: two sessions sat 21 h and 30 h
in it on 2026-11-12. The hook verbs still read their payload, which the harness
closes.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "claude-tmux-state"

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX sh script")


def _fake_tmux(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "tmux.log"
    fake = bin_dir / "tmux"
    fake.write_text(f'#!/bin/sh\necho "$*" >> "{log}"\nexit 0\n')
    fake.chmod(0o755)
    return bin_dir


def _run_with_open_stdin(verb: str, env: dict[str, str]) -> float:
    """Run the verb with a pipe as stdin that is never closed; return its wall time."""
    read_fd, write_fd = os.pipe()
    try:
        start = time.monotonic()
        proc = subprocess.run(
            ["sh", str(SCRIPT), verb], stdin=read_fd, env=env, timeout=10, check=False
        )
        elapsed = time.monotonic() - start
    finally:
        os.close(read_fd)
        os.close(write_fd)
    assert proc.returncode == 0
    return elapsed


@pytest.mark.parametrize("verb", ["hold", "need"])
def test_by_hand_verb_returns_with_stdin_open_without_tmux(verb: str) -> None:
    env = {k: v for k, v in os.environ.items() if k not in ("TMUX_PANE", "HC_TERMINAL_ID")}
    assert _run_with_open_stdin(verb, env) < 3


@pytest.mark.parametrize("verb", ["hold", "need"])
def test_by_hand_verb_returns_with_stdin_open_in_tmux(verb: str, tmp_path: Path) -> None:
    env = dict(os.environ)
    env.pop("HC_TERMINAL_ID", None)
    env["TMUX_PANE"] = "%999"
    env["PATH"] = f"{_fake_tmux(tmp_path)}:{env['PATH']}"
    assert _run_with_open_stdin(verb, env) < 3
    assert (tmp_path / "tmux.log").exists()


def test_a_hook_verb_still_reads_its_payload(tmp_path: Path) -> None:
    """working is a hook verb: it drains stdin, so a closed payload returns at once."""
    env = dict(os.environ)
    env.pop("HC_TERMINAL_ID", None)
    env["TMUX_PANE"] = "%999"
    env["PATH"] = f"{_fake_tmux(tmp_path)}:{env['PATH']}"
    proc = subprocess.run(
        ["sh", str(SCRIPT), "working"], input=b"{}", env=env, timeout=10, check=False
    )
    assert proc.returncode == 0
