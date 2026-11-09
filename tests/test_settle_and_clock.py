# SPDX-License-Identifier: AGPL-3.0-or-later
"""The clock carrier (``nexus.clock``) and the test helpers built on it
(``tests/_settle.py``): ``settle``, which waits on named pending work and says
what is still pending when it gives up, and ``FakeClock``, a virtual clock that
code under test waits on until the test advances it (nexus-3ceda.11)."""
from __future__ import annotations

import threading
import time

import pytest

from nexus import clock as nxclock
from tests._module_seam import patch_time
from tests._settle import FakeClock, settle


class TestSettle:
    def test_returns_as_soon_as_nothing_is_pending(self) -> None:
        state = {"left": 3}

        def pending() -> dict[str, int] | None:
            state["left"] -= 1
            return {"left": state["left"]} if state["left"] > 0 else None

        settle(pending, deadline=5.0, interval=0.0)
        assert state["left"] == 0

    def test_a_timeout_names_what_is_still_pending(self) -> None:
        with pytest.raises(pytest.fail.Exception) as info:
            settle(lambda: {"queue_depth": 2, "unacked": ["t1"]}, deadline=0.05, interval=0.01, what="the drain")
        text = str(info.value)
        assert "the drain" in text and "queue_depth" in text and "'t1'" in text

    @pytest.mark.parametrize("settled", [None, False, 0, "", [], {}, set()])
    def test_every_empty_or_false_answer_means_settled(self, settled: object) -> None:
        settle(lambda: settled, deadline=0.05)

    def test_an_exception_in_the_probe_propagates(self) -> None:
        def broken() -> None:
            raise RuntimeError("probe failed")

        with pytest.raises(RuntimeError, match="probe failed"):
            settle(broken, deadline=1.0)

    def test_the_deadline_is_checked_after_a_final_probe(self) -> None:
        """A probe that settles exactly at the deadline is a pass, not a timeout."""
        calls = {"n": 0}

        def pending() -> bool:
            calls["n"] += 1
            return calls["n"] < 2

        # The first probe is before the deadline; the sleep carries past it; the
        # second probe, after the deadline, settles and is a pass.
        settle(pending, deadline=0.05, interval=0.1)
        assert calls["n"] == 2


class TestFakeClock:
    def test_it_is_a_clock_callable_and_advances_only_when_told(self) -> None:
        clock = FakeClock(start=100.0)
        assert clock() == 100.0
        time.sleep(0.01)
        assert clock() == 100.0, "real time does not move it"
        clock.advance(2.5)
        assert clock() == 102.5

    def test_a_wait_returns_true_when_the_event_is_set(self) -> None:
        clock = FakeClock()
        event = threading.Event()
        threading.Timer(0.02, event.set).start()
        assert nxclock.wait(clock, event, 3600.0) is True
        assert clock() == 0.0

    def test_a_wait_times_out_only_when_virtual_time_passes_its_timeout(self) -> None:
        clock = FakeClock()
        event = threading.Event()
        result: list[bool] = []
        t = threading.Thread(target=lambda: result.append(nxclock.wait(clock, event, 10.0)), daemon=True)
        t.start()
        settle(lambda: clock.waiters() != 1 and f"waiters={clock.waiters()}", deadline=5.0)
        clock.advance(9.0)
        time.sleep(0.05)
        assert t.is_alive() and not result, "9 s of 10: still waiting"
        clock.advance(1.0)
        t.join(5.0)
        assert result == [False]
        assert clock.waiters() == 0

    def test_a_sleep_blocks_until_advanced(self) -> None:
        clock = FakeClock()
        done = threading.Event()

        def sleeper() -> None:
            nxclock.sleep(clock, 5.0)
            done.set()

        threading.Thread(target=sleeper, daemon=True).start()
        settle(lambda: clock.waiters() != 1, deadline=5.0)
        assert not done.is_set()
        clock.advance(5.0)
        assert done.wait(5.0)

    def test_auto_advance_lets_single_threaded_code_sleep_through(self) -> None:
        clock = FakeClock(auto_advance=True)
        nxclock.sleep(clock, 30.0)
        assert clock() == 30.0
        assert nxclock.wait(clock, threading.Event(), 7.0) is False
        assert clock() == 37.0

    def test_step_advances_once_the_given_number_of_waiters_are_parked(self) -> None:
        clock = FakeClock()
        woke: list[float] = []
        stop = threading.Event()

        def loop() -> None:
            while not nxclock.wait(clock, stop, 4.0):
                time.sleep(0.02)  # the round's work takes real time; step waits for it
                woke.append(clock())

        t = threading.Thread(target=loop, daemon=True)
        t.start()
        for n in range(1, 4):
            clock.step(4.0)
            assert len(woke) == n, "step returned before the round's work was done"
        stop.set()
        t.join(5.0)
        assert woke == [4.0, 8.0, 12.0]

    def test_a_step_shorter_than_the_wait_fails_instead_of_returning_without_a_round(self) -> None:
        clock = FakeClock()
        stop = threading.Event()
        t = threading.Thread(target=lambda: nxclock.wait(clock, stop, 4.0), daemon=True)
        t.start()
        with pytest.raises(pytest.fail.Exception, match="released 0 waiter"):
            clock.step(1.0)
        stop.set()
        t.join(5.0)

    def test_a_step_on_a_loop_that_has_ended_counts_as_settled(self) -> None:
        clock = FakeClock()
        clock.step(5.0, exited=lambda: True)
        assert clock() == 5.0

    def test_advance_refuses_to_go_backwards(self) -> None:
        with pytest.raises(ValueError):
            FakeClock().advance(-1.0)


class TestTheCarrierFallsBackToRealTime:
    def test_a_plain_callable_waits_on_the_event_itself(self) -> None:
        event = threading.Event()
        t0 = time.monotonic()
        assert nxclock.wait(time.monotonic, event, 0.05) is False
        assert time.monotonic() - t0 >= 0.04

    def test_a_plain_callable_sleeps_for_real(self) -> None:
        with patch_time("nexus.clock", "sleep") as slept:
            nxclock.sleep(time.monotonic, 1.5)
        slept.assert_called_once_with(1.5)

    def test_system_clock_is_monotonic_time(self) -> None:
        a = nxclock.SYSTEM()
        b = time.monotonic()
        assert abs(b - a) < 1.0
