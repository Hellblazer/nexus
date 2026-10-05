# SPDX-License-Identifier: AGPL-3.0-or-later
"""The per-test watchdog turns a hang into a failure (RDR-224, nexus-f9bgu.33).

Two mutations of the Windows stop path (a sharing retry with no bound, a spawn
lock that ignores a stop request) hung the scoped run instead of failing it.
``tests/daemon/_watchdog.py`` bounds every test in the stop-channel and
conformance files. These tests pin the mechanism, the watched set, and that the
fixture is really installed on those files.
"""
from __future__ import annotations

import ast
import signal
import time
from pathlib import Path

import pytest

from tests.daemon import _watchdog

HERE = Path(__file__).parent


class TestMechanism:
    def test_windows_uses_faulthandler_and_everything_else_the_alarm(self) -> None:
        assert _watchdog.mechanism("win32") == "faulthandler"
        for plat in ("linux", "darwin"):
            assert _watchdog.mechanism(plat) == "alarm"

    @pytest.mark.skipif(not hasattr(signal, "setitimer"), reason="no SIGALRM on this host: the faulthandler arm ends the process and cannot be run in-process")
    def test_a_loop_with_no_bound_fails_instead_of_hanging(self) -> None:
        start = time.monotonic()
        with pytest.raises(_watchdog.WatchdogTimeout, match="no bound"):
            with _watchdog.watchdog(0.3, "t", platform="linux"):
                while True:  # the shape of an unbounded retry on an injected clock that never advances
                    pass
        assert time.monotonic() - start < 5.0

    @pytest.mark.skipif(not hasattr(signal, "setitimer"), reason="no SIGALRM on this host")
    def test_a_blocking_wait_is_interrupted_too(self) -> None:
        with pytest.raises(_watchdog.WatchdogTimeout):
            with _watchdog.watchdog(0.2, "t", platform="linux"):
                time.sleep(30)

    @pytest.mark.skipif(not hasattr(signal, "setitimer"), reason="no SIGALRM on this host")
    def test_a_test_that_finishes_in_time_is_untouched_and_the_timer_is_cleared(self) -> None:
        before = signal.getsignal(signal.SIGALRM)
        with _watchdog.watchdog(5.0, "t", platform="linux"):
            pass
        assert signal.getsignal(signal.SIGALRM) == before
        assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)

    def test_the_limit_is_finite_and_generous(self) -> None:
        assert 30.0 <= _watchdog.PER_TEST_TIMEOUT_S <= 600.0


class TestTheFilesAreWatched:
    def test_every_watched_module_exists_and_the_stop_files_are_among_them(self) -> None:
        for name in _watchdog.WATCHED_MODULES:
            assert (HERE / f"{name}.py").is_file(), f"{name} is named in WATCHED_MODULES but is not a test file here"
        assert {"test_storage_service_stop_channel", "test_rdr149_lifecycle_conformance"} <= _watchdog.WATCHED_MODULES

    def test_the_conftest_installs_the_watchdog_as_an_autouse_fixture(self) -> None:
        tree = ast.parse((HERE / "conftest.py").read_text(encoding="utf-8"))
        fixtures = [
            n for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == "_watched_tests_cannot_hang"
        ]
        assert len(fixtures) == 1
        decorator = ast.unparse(fixtures[0].decorator_list[0])
        assert "autouse=True" in decorator
