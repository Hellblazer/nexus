# SPDX-License-Identifier: AGPL-3.0-or-later
"""The restart-script tests run the script's deadline loops on a fake clock (tests/_restart_fake_clock.py)."""

from __future__ import annotations

import ast
import importlib.machinery
import importlib.util
from pathlib import Path

import pytest

from tests._restart_fake_clock import fake_clock

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "claude-restart-sessions"
RESTART_TESTS = Path(__file__).with_name("test_claude_restart_sessions.py")


@pytest.fixture(scope="module")
def crs():
    loader = importlib.machinery.SourceFileLoader("claude_restart_sessions_clock", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_a_deadline_loop_under_the_fake_clock_waits_its_seconds_and_no_more(crs, monkeypatch) -> None:
    """went_busy's 60 s wait, on a session that never goes busy, sleeps 30 times of 2 s on the fake clock."""
    sleeps: list[float] = []
    now = fake_clock(crs, monkeypatch, record=sleeps)
    start = now[0]
    monkeypatch.setattr(crs, "status", lambda _s: "idle")
    monkeypatch.setattr(crs, "prompt_count", lambda _sid: 0)
    assert crs.went_busy({"sessionId": "sid-x"}, 0, seconds=60) is False
    assert sleeps == [2] * 30
    assert now[0] - start == 60


def test_every_fake_that_silences_the_scripts_sleep_also_fakes_its_clock() -> None:
    """A function in the restart tests that turns the script's time.sleep into a no-op must fake its time.time
    too (or call _fake_clock), or each deadline loop it reaches spins on the real clock for its whole wait."""
    src = RESTART_TESTS.read_text(encoding="utf-8")
    bad = []
    for fn in ast.walk(ast.parse(src)):
        if not isinstance(fn, ast.FunctionDef):
            continue
        patched = {c.args[2].value for c in ast.walk(fn)
                   if isinstance(c, ast.Call) and getattr(c.func, "id", "") == "setattr_in" and len(c.args) >= 3
                   and isinstance(c.args[2], ast.Constant)}
        calls_clock = any(isinstance(c, ast.Call) and getattr(c.func, "id", "") == "_fake_clock" for c in ast.walk(fn))
        real = "real_sleep" in (ast.get_source_segment(src, fn) or "")  # a shortened REAL sleep polls a real process
        if "time.sleep" in patched and "time.time" not in patched and not calls_clock and not real:
            bad.append(f"{fn.name} (line {fn.lineno})")
    assert not bad, f"these silence the script's sleep but leave its real clock running: {bad}"
    assert src.count("_fake_clock(crs, monkeypatch") >= 6, "non-vacuity: the restart tests route their fakes here"

