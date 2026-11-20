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
RESTART_TESTS = sorted(p for p in Path(__file__).parent.glob("test_claude_restart_sessions*.py") if p.name != Path(__file__).name)


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
    assert sleeps and sum(sleeps) == now[0] - start == 60, "the wait ends at its deadline on the fake clock"


def _silenced_without_a_clock(src: str) -> list[str]:
    """Functions in *src* that patch the script's time.sleep with something other than a shortened REAL sleep,
    and neither patch its time.time nor route through fake_clock."""
    bad = []
    for fn in ast.walk(ast.parse(src)):
        if not isinstance(fn, ast.FunctionDef) or fn.name in {"fake_clock", "_fake_clock"}:
            continue
        sleep_values, patched = [], set()
        for c in ast.walk(fn):
            if (isinstance(c, ast.Call) and getattr(c.func, "id", "") == "setattr_in" and len(c.args) >= 4
                    and isinstance(c.args[2], ast.Constant)):
                patched.add(c.args[2].value)
                if c.args[2].value == "time.sleep":
                    sleep_values.append(ast.get_source_segment(src, c.args[3]) or "")
        calls_clock = any(isinstance(c, ast.Call) and getattr(c.func, "id", "") in {"fake_clock", "_fake_clock"}
                          for c in ast.walk(fn))
        silenced = [v for v in sleep_values if "real_sleep" not in v]  # a shortened REAL sleep polls a real process
        if silenced and "time.time" not in patched and not calls_clock:
            bad.append(f"{fn.name} (line {fn.lineno})")
    return bad


def test_every_fake_that_silences_the_scripts_sleep_also_fakes_its_clock() -> None:
    """A function in the restart tests that turns the script's time.sleep into a no-op must fake its time.time
    too (or call fake_clock), or each deadline loop it reaches spins on the real clock for its whole wait."""
    assert len(RESTART_TESTS) >= 4, RESTART_TESTS
    bad = {p.name: b for p in RESTART_TESTS if (b := _silenced_without_a_clock(p.read_text(encoding="utf-8")))}
    assert not bad, f"these silence the script's sleep but leave its real clock running: {bad}"
    uses = sum(p.read_text(encoding="utf-8").count("fake_clock(") for p in RESTART_TESTS)
    assert uses >= 10, f"non-vacuity: the restart tests route their fakes through fake_clock ({uses} calls)"


def test_the_rule_flags_a_bare_no_op_sleep_and_spares_a_real_one() -> None:
    src = (
        "def a(crs, monkeypatch):\n    setattr_in(monkeypatch, crs, 'time.sleep', lambda _s: None)\n"
        "def b(crs, monkeypatch):\n    setattr_in(monkeypatch, crs, 'time.sleep', lambda s: real_sleep(s / 4))\n"
        "def c(crs, monkeypatch):\n    real_sleep = 1\n    setattr_in(monkeypatch, crs, 'time.sleep', lambda _s: None)\n"
    )
    assert _silenced_without_a_clock(src) == ["a (line 1)", "c (line 5)"]
