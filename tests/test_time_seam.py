# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-hkafl: a test's fake clock or sleep must stay inside the module under test.

``tests/test_vector_retry.py`` installed a blocking fake sleep with
``monkeypatch.setattr(retry_mod.time, "sleep", fc.sleep)``. ``retry_mod.time`` is the
stdlib ``time`` module, so the fake replaced ``time.sleep`` for the whole worker. A
substrate teardown in the same worker (``drop_test_tenant``) then ran
``subprocess.run(timeout=60)``, whose wait loop sleeps through ``time.sleep``, and
blocked on the fake until the run was killed about 50 minutes later.

``tests/_time_seam.py`` replaces the module's own ``time`` binding instead. These
tests pin that a fake installed that way is seen by the module and not by
``subprocess``, and lint ``tests/`` so the global form does not come back.
"""
from __future__ import annotations

import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

import nexus.rate_brake as rate_brake_mod
import nexus.retry as retry_mod
from tests._time_seam import TimeProxy, module_time, patch_time

_TESTS = Path(__file__).resolve().parent

#: The blocking fake gives up after this long, so a regression fails the test
#: instead of hanging the worker the way nexus-hkafl did.
_FAKE_SLEEP_GIVE_UP_S = 20.0


class _NeverAdvancingSleep:
    """The ``_SyncFakeClock.sleep`` shape with nobody calling ``advance()``:
    every call blocks. Records who called it."""

    def __init__(self) -> None:
        self.calls: list[float] = []
        self._never = threading.Event()

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        if not self._never.wait(timeout=_FAKE_SLEEP_GIVE_UP_S):
            raise AssertionError("a blocking fake sleep was reached outside the module under test")


def _child_that_outlives_a_few_wait_polls() -> list[str]:
    return [sys.executable, "-c", "import time; time.sleep(0.3)"]


def test_subprocess_run_with_timeout_returns_while_a_module_fake_sleep_blocks(monkeypatch) -> None:
    fake = _NeverAdvancingSleep()
    module_time(monkeypatch, retry_mod).sleep = fake

    # The fake is live for the module under test...
    assert retry_mod.time.sleep is fake
    # ...and for the brake that module's callers reach.
    assert rate_brake_mod.time is retry_mod.time
    # The process-wide module is untouched.
    assert time.sleep is not fake

    started = time.monotonic()
    result = subprocess.run(_child_that_outlives_a_few_wait_polls(), timeout=60, check=False)  # noqa: S603
    assert result.returncode == 0
    assert time.monotonic() - started < _FAKE_SLEEP_GIVE_UP_S
    assert fake.calls == []


def test_patch_time_is_local_and_restores(monkeypatch) -> None:
    original_binding = retry_mod.time
    with patch_time("nexus.retry", "sleep") as mock_sleep:
        retry_mod.time.sleep(5.0)
        assert time.sleep is not mock_sleep
        result = subprocess.run(_child_that_outlives_a_few_wait_polls(), timeout=60, check=False)  # noqa: S603
        assert result.returncode == 0
    mock_sleep.assert_called_once_with(5.0)
    assert retry_mod.time is original_binding
    assert rate_brake_mod.time is time


def test_module_time_reuses_one_proxy_and_forwards_the_rest(monkeypatch) -> None:
    proxy = module_time(monkeypatch, "nexus.retry")
    assert isinstance(proxy, TimeProxy)
    assert module_time(monkeypatch, retry_mod) is proxy
    proxy.monotonic = lambda: 42.0
    assert retry_mod.time.monotonic() == 42.0
    assert retry_mod.time.perf_counter is time.perf_counter


@pytest.mark.skipif(sys.platform == "win32", reason="Windows waits on the process handle, not a time.sleep poll")
def test_control_a_global_fake_sleep_is_reached_by_subprocess_wait() -> None:
    """The exposure the seam removes, shown directly: with ``time.sleep`` replaced
    process-wide, ``subprocess.run(timeout=...)`` calls the fake. If this stops
    being true the regression test above proves nothing on this platform."""

    class _Reached(Exception):
        pass

    def boom(_seconds: float) -> None:
        raise _Reached

    with patch("time.sleep", side_effect=boom), pytest.raises(_Reached):
        subprocess.run(_child_that_outlives_a_few_wait_polls(), timeout=60, check=False)  # noqa: S603


# ── lint: no test patches the global time module ─────────────────────────────

_GLOBAL_TIME_PATCH = re.compile(
    r"""
      \bpatch\(\s*["'](?:[\w.]+\.)?time\.\w+["']                 # patch("x.time.sleep")
    | \bsetattr\(\s*["'](?:[\w.]+\.)?time\.\w+["']               # monkeypatch.setattr("x.time.sleep", f)
    | \bsetattr\(\s*(?:[\w.]+\.)?time\s*,\s*["']\w+["']          # monkeypatch.setattr(mod.time, "sleep", f)
    | \bpatch\.object\(\s*(?:[\w.]+\.)?time\s*,\s*["']\w+["']    # patch.object(mod.time, "sleep")
    """,
    re.VERBOSE,
)

#: This file shows the exposure on purpose (the control test above).
_EXEMPT = {Path(__file__).resolve(), (_TESTS / "_time_seam.py").resolve()}


def _offenders() -> list[str]:
    found: list[str] = []
    for path in sorted(_TESTS.rglob("*.py")):
        if path.resolve() in _EXEMPT:
            continue
        text = path.read_text(encoding="utf-8")
        for match in _GLOBAL_TIME_PATCH.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            found.append(f"{path.relative_to(_TESTS.parent)}:{line}: {match.group(0).strip()}")
    return found


@pytest.mark.lint
def test_no_test_patches_the_global_time_module() -> None:
    """Every ``<module>.time`` is the one stdlib module, so these forms fake the
    clock for every thread and every fixture teardown in the worker. Use
    ``tests._time_seam.module_time`` or ``patch_time`` instead."""
    offenders = _offenders()
    assert offenders == [], "global time patches (use tests/_time_seam.py):\n" + "\n".join(offenders)


@pytest.mark.lint
def test_the_lint_sees_each_global_form() -> None:
    """Non-vacuity: every shape the lint exists for is matched."""
    for sample in (
        'patch("nexus.retry.time.sleep")',
        'patch("time.sleep", side_effect=f)',
        'monkeypatch.setattr("nexus.retry.time.sleep", f)',
        'monkeypatch.setattr(retry_mod.time, "sleep", f)',
        'monkeypatch.setattr(time, "monotonic", f)',
        'monkeypatch.setattr(\n        mod.time, "time_ns", f)',
        'patch.object(time, "sleep")',
    ):
        assert _GLOBAL_TIME_PATCH.search(sample), sample
    for sample in (
        'patch_time("nexus.retry", "sleep")',
        'module_time(monkeypatch, retry_mod).sleep = f',
        'patch("nexus.indexer.timeout_s", 3)',
        'monkeypatch.setattr(mod, "time", proxy)',
    ):
        assert not _GLOBAL_TIME_PATCH.search(sample), sample
