# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-v1929: a 503 from the edge holds every client process on the box.

19 concurrent ``nx upgrade --auto`` children each retried 503s on their own
clock (~2,000 gateway retries), keeping the edge admission pool at zero. The
hold file under the config dir makes the retry load shrink with process count
instead of scaling with it: a 503 writes a not-before time, and every client
waits it out before its first attempt and its retries.
"""
from __future__ import annotations

import json
import os
import random
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import pytest

from nexus import _locking
from nexus.db import gateway_hold as gh

SCHEDULE = (2.0, 5.0, 10.0)


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    return tmp_path


def _rng() -> random.Random:
    return random.Random(1929)


class TestRecord:
    def test_no_file_means_no_wait(self, cfg: Path) -> None:
        assert gh.wait_before_attempt(now=1000.0, rng=_rng()) == 0.0
        assert not (cfg / gh.HOLD_FILENAME).exists()

    def test_one_process_keeps_its_own_schedule(self, cfg: Path) -> None:
        """A lone client sees exactly the 2/5/10 schedule it had before."""
        sleeps = [
            gh.record_503(delay, retry_after=None, url="https://edge", now=1000.0 + i, rng=_rng())
            for i, delay in enumerate(SCHEDULE)
        ]
        assert sleeps == [2.0, 5.0, 10.0]

    def test_a_503_holds_a_second_process_before_its_first_attempt(self, cfg: Path) -> None:
        gh.record_503(2.0, retry_after=None, url="https://edge", now=1000.0, rng=_rng())
        wait = gh.wait_before_attempt(now=1000.5, rng=_rng())
        assert 1.5 <= wait <= 1.5 + gh.MAX_JITTER_S

    def test_nineteen_503s_in_one_hold_are_one_strike(self, cfg: Path) -> None:
        """The incident shape: every process 503s inside the same hold. One wave, one strike."""
        sleeps = [
            gh.record_503(2.0, retry_after=None, url="https://edge", now=1000.0 + i * 0.05, rng=_rng())
            for i in range(19)
        ]
        assert json.loads((cfg / gh.HOLD_FILENAME).read_text())["strikes"] == 1
        # One strike, but nobody resends together (nexus-xe1et): the first leaves at the hold's
        # end, each later one a slot after the one before.
        assert sleeps[0] == 2.0
        wakes = [1000.0 + i * 0.05 + sleep_s for i, sleep_s in enumerate(sleeps)]
        assert all(b - a == pytest.approx(gh.SLOT_S) for a, b in zip(wakes, wakes[1:], strict=False))

    def test_failed_waves_escalate_whichever_process_sees_them(self, cfg: Path) -> None:
        """A 503 after the hold passed is the next strike: 2, 5, 10, 20, 30, capped."""
        now, holds = 1000.0, []
        for _ in range(7):
            sleep_s = gh.record_503(2.0, retry_after=None, url="https://edge", now=now, rng=_rng())
            holds.append(json.loads((cfg / gh.HOLD_FILENAME).read_text())["not_before"] - now)
            now += sleep_s + 0.3  # the resend lands after the hold, and 503s again
        assert holds == [2.0, 5.0, 10.0, 20.0, 30.0, 30.0, 30.0]

    def test_a_success_after_the_hold_starts_the_count_over(self, cfg: Path) -> None:
        """The critic's lone-process case: two calls, each 503ing then succeeding, both see 2/5."""
        now = 1000.0
        for _call in range(2):
            first = gh.record_503(2.0, retry_after=None, url="https://edge", now=now, rng=_rng())
            now += first + 0.3
            second = gh.record_503(5.0, retry_after=None, url="https://edge", now=now, rng=_rng())
            now += second + 0.3
            gh.record_success(now=now)
            assert (first, second) == (2.0, 5.0)
            now += 5.0
        assert not (cfg / gh.HOLD_FILENAME).exists()

    def test_a_success_inside_the_hold_leaves_it(self, cfg: Path) -> None:
        gh.record_503(5.0, retry_after=None, url="https://edge", now=1000.0, rng=_rng())
        gh.record_success(now=1001.0)
        assert gh.wait_before_attempt(now=1001.0, rng=_rng()) >= 4.0

    def test_a_success_with_no_hold_is_a_no_op(self, cfg: Path) -> None:
        gh.record_success(now=1000.0)
        assert not (cfg / gh.HOLD_FILENAME).exists()

    def test_a_retry_waits_only_past_what_it_already_slept(self, cfg: Path) -> None:
        gh.record_503(2.0, retry_after=None, url="https://edge", now=1000.0, rng=_rng())
        assert gh.wait_before_attempt(after=1002.0, now=1000.0, rng=_rng()) == 0.0
        assert 1.0 <= gh.wait_before_attempt(after=1001.0, now=1000.0, rng=_rng()) <= 1.0 + gh.MAX_JITTER_S

    def test_strikes_reset_after_a_quiet_window(self, cfg: Path) -> None:
        for i in range(4):
            gh.record_503(2.0, retry_after=None, url="https://edge", now=1000.0 + i, rng=_rng())
        later = 1000.0 + gh.STRIKE_WINDOW_S + 100.0
        assert gh.record_503(2.0, retry_after=None, url="https://edge", now=later, rng=_rng()) == 2.0
        assert json.loads((cfg / gh.HOLD_FILENAME).read_text())["strikes"] == 1

    def test_retry_after_lengthens_the_hold(self, cfg: Path) -> None:
        assert gh.record_503(2.0, retry_after=12.0, url="https://edge", now=1000.0, rng=_rng()) == 12.0
        assert gh.wait_before_attempt(now=1000.0, rng=_rng()) >= 12.0

    def test_retry_after_is_capped(self, cfg: Path) -> None:
        assert gh.record_503(2.0, retry_after=600.0, url="https://edge", now=1000.0, rng=_rng()) == gh.HOLD_CAP_S

    def test_zero_delay_writes_nothing(self, cfg: Path) -> None:
        """Tests zero the schedule; a zero hold must not leave a file behind."""
        assert gh.record_503(0.0, retry_after=None, url="https://edge", now=1000.0, rng=_rng()) == 0.0
        assert not (cfg / gh.HOLD_FILENAME).exists()

    def test_an_expired_hold_does_not_wait(self, cfg: Path) -> None:
        gh.record_503(2.0, retry_after=None, url="https://edge", now=1000.0, rng=_rng())
        assert gh.wait_before_attempt(now=1003.0, rng=_rng()) == 0.0

    @pytest.mark.parametrize("body", ["", "not json", "[]", '{"not_before": "soon"}'])
    def test_a_garbled_file_is_ignored(self, cfg: Path, body: str) -> None:
        """Read as no hold, and the next 503 overwrites it with a valid one."""
        (cfg / gh.HOLD_FILENAME).write_text(body)
        assert gh.wait_before_attempt(now=1000.0, rng=_rng()) == 0.0
        assert gh.record_503(2.0, retry_after=None, url="https://edge", now=1000.0, rng=_rng()) == 2.0
        state = json.loads((cfg / gh.HOLD_FILENAME).read_text())
        assert (state["not_before"], state["strikes"]) == (1002.0, 1)

    def test_max_wait_clamps_a_short_budget_caller(self, cfg: Path) -> None:
        gh.record_503(10.0, retry_after=None, url="https://edge", now=1000.0, rng=_rng())
        assert gh.wait_before_attempt(max_wait=3.0, now=1000.0, rng=_rng()) == 3.0

    def test_a_wedged_lock_holder_falls_back_to_the_own_schedule(
        self, cfg: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(gh, "LOCK_WAIT_S", 0.1)
        fd = os.open(cfg / (gh.HOLD_FILENAME + ".lock"), os.O_RDWR | os.O_CREAT)
        try:
            _locking.lock_fd(fd, blocking=True)
            assert gh.record_503(5.0, retry_after=None, url="https://edge", now=1000.0, rng=_rng()) == 5.0
        finally:
            _locking.unlock_fd(fd)
            os.close(fd)
        assert not (cfg / gh.HOLD_FILENAME).exists()

    def test_a_far_future_hold_is_ignored(self, cfg: Path) -> None:
        """A clock step back or a hand edit must not stall every request on a healthy edge."""
        (cfg / gh.HOLD_FILENAME).write_text(json.dumps({"not_before": 1e12, "strikes": 9, "last_503": 1000.0}))
        assert gh.wait_before_attempt(now=1000.0, rng=_rng()) == 0.0
        assert gh.record_503(2.0, retry_after=None, url="https://edge", now=1000.0, rng=_rng()) == 2.0
        state = json.loads((cfg / gh.HOLD_FILENAME).read_text())
        assert state["strikes"] == 1
        assert state["not_before"] <= 1000.0 + gh.HOLD_CAP_S

    def test_the_file_never_carries_userinfo(self, cfg: Path) -> None:
        gh.record_503(2.0, retry_after=None, url="https://user:secret@edge/x", now=1000.0, rng=_rng())
        assert "secret" not in (cfg / gh.HOLD_FILENAME).read_text()


class TestEndpointScope:
    """nexus-cphvg: a 503 holds the clients of the endpoint that sent it, not every client on the box."""

    def test_a_hold_for_another_endpoint_is_no_hold(self, cfg: Path) -> None:
        gh.record_503(5.0, retry_after=None, url="http://127.0.0.1:8123", now=1000.0, rng=_rng())
        assert gh.wait_before_attempt(now=1000.5, rng=_rng(), url="https://api.example.com") == 0.0
        assert gh.wait_before_attempt(now=1000.5, rng=_rng(), url="http://127.0.0.1:8123") > 3.0

    def test_scheme_host_and_port_all_count(self, cfg: Path) -> None:
        gh.record_503(5.0, retry_after=None, url="https://api.example.com:8443/v1/x", now=1000.0, rng=_rng())
        for other in ("http://api.example.com:8443", "https://api.example.com", "https://other.example.com:8443"):
            assert gh.wait_before_attempt(now=1000.5, rng=_rng(), url=other) == 0.0, other
        # the path and userinfo are not part of the endpoint
        assert gh.wait_before_attempt(now=1000.5, rng=_rng(), url="https://u:p@API.example.com:8443/z") > 3.0

    def test_a_caller_that_cannot_name_its_endpoint_is_held(self, cfg: Path) -> None:
        gh.record_503(5.0, retry_after=None, url="http://127.0.0.1:8123", now=1000.0, rng=_rng())
        assert gh.wait_before_attempt(now=1000.5, rng=_rng(), url="") > 3.0

    def test_a_hold_with_no_recorded_endpoint_holds_everyone(self, cfg: Path) -> None:
        """A file written before endpoints were recorded, or by hand."""
        (cfg / gh.HOLD_FILENAME).write_text(json.dumps({"not_before": 1005.0, "strikes": 1, "last_503": 1000.0}))
        assert gh.wait_before_attempt(now=1000.5, rng=_rng(), url="https://api.example.com") > 3.0

    def test_a_success_elsewhere_leaves_the_hold(self, cfg: Path) -> None:
        gh.record_503(2.0, retry_after=None, url="https://api.example.com", now=1000.0, rng=_rng())
        gh.record_success(now=1010.0, url="http://127.0.0.1:8123")
        assert (cfg / gh.HOLD_FILENAME).exists()
        gh.record_success(now=1010.0, url="https://api.example.com")
        assert not (cfg / gh.HOLD_FILENAME).exists()

    def test_a_503_from_another_endpoint_replaces_the_hold(self, cfg: Path) -> None:
        """One file, one endpoint: the strike count of the other one does not carry over."""
        for i in range(3):
            gh.record_503(2.0, retry_after=None, url="https://api.example.com", now=1000.0 + i * 20, rng=_rng())
        assert gh.record_503(2.0, retry_after=None, url="http://127.0.0.1:8123", now=1100.0, rng=_rng()) == 2.0
        state = json.loads((cfg / gh.HOLD_FILENAME).read_text())
        assert (state["url"], state["strikes"]) == ("http://127.0.0.1:8123", 1)


class TestEngineReason:
    """nexus-cphvg: a 503 with an engine reason is about one tenant or request, not the edge."""

    def test_reason_from_body(self) -> None:
        assert gh.reason_from_body(b'{"error": "busy", "reason": "tenant_creation_busy"}') == "tenant_creation_busy"
        for body in (b"", None, b"not json", b"<html>503</html>", b"[]", b'{"error": "x"}', b'{"reason": ""}', b'{"reason": 3}'):
            assert gh.reason_from_body(body) is None, body

    def test_an_engine_reason_records_nothing_and_keeps_the_callers_delay(self, cfg: Path) -> None:
        got = gh.record_503(
            2.0, retry_after=7.0, url="https://api.example.com", now=1000.0, rng=_rng(),
            reason="tenant_creation_busy",
        )
        assert got == 7.0  # the engine's own Retry-After is still honoured
        assert not (cfg / gh.HOLD_FILENAME).exists()

    def test_an_engine_reason_does_not_extend_a_live_hold(self, cfg: Path) -> None:
        gh.record_503(2.0, retry_after=None, url="https://api.example.com", now=1000.0, rng=_rng())
        before = (cfg / gh.HOLD_FILENAME).read_text()
        gh.record_503(2.0, retry_after=None, url="https://api.example.com", now=1000.5, rng=_rng(), reason="tenant_creation_busy")
        assert (cfg / gh.HOLD_FILENAME).read_text() == before


class TestStaggeredWake:
    """nexus-xe1et: processes held on one hold leave it one slot apart, not together.

    What the tests count is the concurrency at the edge after a hold: senders whose
    request is still in flight when another one leaves, on a fixed clock.
    """

    REQUEST_S = 0.3

    @staticmethod
    def _peak_concurrency(wakes: list[float], request_s: float) -> int:
        events = sorted([(w, 1) for w in wakes] + [(w + request_s, -1) for w in wakes], key=lambda e: (e[0], e[1]))
        live = peak = 0
        for _, step in events:
            live += step
            peak = max(peak, live)
        return peak

    def test_waiters_leave_the_hold_one_slot_apart(self, cfg: Path) -> None:
        gh.record_503(5.0, retry_after=None, url="https://edge", now=1000.0, rng=_rng())  # not_before 1005
        waits = [gh.wait_before_attempt(now=1001.0, rng=_rng(), url="https://edge") for _ in range(20)]
        wakes = sorted(1001.0 + w for w in waits)
        assert wakes[0] >= 1005.0
        assert all(b - a >= gh.SLOT_S - 1e-9 for a, b in zip(wakes, wakes[1:], strict=False))
        assert self._peak_concurrency(wakes, self.REQUEST_S) <= 2

    def test_the_peak_does_not_grow_with_the_number_of_waiters(self, cfg: Path) -> None:
        gh.record_503(5.0, retry_after=None, url="https://edge", now=1000.0, rng=_rng())
        waits = [gh.wait_before_attempt(now=1001.0, rng=_rng(), url="https://edge") for _ in range(100)]
        wakes = [1001.0 + w for w in waits]
        assert self._peak_concurrency(wakes, self.REQUEST_S) <= 2
        assert max(waits) <= gh.HOLD_CAP_S  # no waiter is queued past the cap

    def test_a_503_recorder_takes_a_slot_too(self, cfg: Path) -> None:
        """The sleep record_503 returns and the wait wait_before_attempt returns draw on one queue."""
        wakes = [1000.0 + gh.record_503(5.0, retry_after=None, url="https://edge", now=1000.0, rng=_rng()) for _ in range(5)]
        wakes += [1000.0 + gh.wait_before_attempt(now=1000.0, rng=_rng(), url="https://edge") for _ in range(5)]
        wakes.sort()
        assert wakes[0] == 1005.0
        assert all(b - a >= gh.SLOT_S - 1e-9 for a, b in zip(wakes, wakes[1:], strict=False))

    def test_the_first_waiter_after_a_lone_recorder_is_one_slot_behind_it(self, cfg: Path) -> None:
        gh.record_503(5.0, retry_after=None, url="https://edge", now=1000.0, rng=_rng())
        assert gh.wait_before_attempt(now=1001.0, rng=_rng(), url="https://edge") == pytest.approx(4.0 + gh.SLOT_S)

    def test_a_caller_past_the_hold_claims_no_slot(self, cfg: Path) -> None:
        gh.record_503(2.0, retry_after=None, url="https://edge", now=1000.0, rng=_rng())
        before = (cfg / gh.HOLD_FILENAME).read_text()
        assert gh.wait_before_attempt(after=1002.5, now=1000.0, rng=_rng(), url="https://edge") == 0.0
        assert (cfg / gh.HOLD_FILENAME).read_text() == before

    def test_a_wait_clamped_by_the_callers_budget_claims_no_slot(self, cfg: Path) -> None:
        gh.record_503(10.0, retry_after=None, url="https://edge", now=1000.0, rng=_rng())
        before = (cfg / gh.HOLD_FILENAME).read_text()
        assert gh.wait_before_attempt(max_wait=3.0, now=1000.0, rng=_rng(), url="https://edge") == 3.0
        assert (cfg / gh.HOLD_FILENAME).read_text() == before

    def test_a_wedged_lock_falls_back_to_the_jittered_wait(self, cfg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        gh.record_503(5.0, retry_after=None, url="https://edge", now=1000.0, rng=_rng())
        monkeypatch.setattr(gh, "LOCK_WAIT_S", 0.1)
        fd = os.open(cfg / (gh.HOLD_FILENAME + ".lock"), os.O_RDWR | os.O_CREAT)
        try:
            _locking.lock_fd(fd, blocking=True)
            wait = gh.wait_before_attempt(now=1001.0, rng=_rng(), url="https://edge")
        finally:
            _locking.unlock_fd(fd)
            os.close(fd)
        assert 4.0 <= wait <= 4.0 + gh.MAX_JITTER_S

    def test_threads_racing_for_slots_get_distinct_ones(self, cfg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(gh, "LOCK_WAIT_S", 10.0)
        gh.record_503(5.0, retry_after=None, url="https://edge", now=1000.0, rng=_rng())
        waits: list[float] = []
        gate = threading.Barrier(12)

        def claim() -> None:
            gate.wait()
            waits.append(gh.wait_before_attempt(now=1001.0, rng=_rng(), url="https://edge"))

        threads = [threading.Thread(target=claim) for _ in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        wakes = sorted(1001.0 + w for w in waits)
        assert len(wakes) == 12
        assert all(b - a >= gh.SLOT_S - 1e-9 for a, b in zip(wakes, wakes[1:], strict=False))
        assert self._peak_concurrency(wakes, self.REQUEST_S) <= 2

    def test_real_processes_racing_for_slots_get_distinct_ones(self, cfg: Path) -> None:
        """Eight processes ask at the same instant of a fixed clock; the file is the only thing they share."""
        gh.record_503(5.0, retry_after=None, url="https://edge", now=1000.0, rng=_rng())
        child = textwrap.dedent(
            """
            from nexus.db import gateway_hold as gh
            gh.LOCK_WAIT_S = 10.0
            print(gh.wait_before_attempt(now=1001.0, url="https://edge"))
            """
        )
        env = {**os.environ, "NEXUS_CONFIG_DIR": str(cfg), "NX_NO_TELEMETRY": "1"}
        procs = [
            subprocess.Popen([sys.executable, "-c", child], stdout=subprocess.PIPE, text=True, env=env)
            for _ in range(8)
        ]
        outs = [p.communicate()[0] for p in procs]
        assert all(p.returncode == 0 for p in procs)
        wakes = sorted(1001.0 + float(o) for o in outs)
        assert len(wakes) == 8
        assert all(b - a >= gh.SLOT_S - 1e-9 for a, b in zip(wakes, wakes[1:], strict=False))
        assert self._peak_concurrency(wakes, self.REQUEST_S) <= 2


def test_the_hold_is_shared_between_real_processes(cfg: Path) -> None:
    """A child process's 503 holds this process: the state is on disk, not in memory."""
    child = textwrap.dedent(
        """
        import time
        from nexus.db import gateway_hold as gh
        print(gh.record_503(5.0, retry_after=None, url="https://edge", now=time.time()))
        """
    )
    out = subprocess.run(
        [sys.executable, "-c", child], capture_output=True, text=True, check=True,
        env={**os.environ, "NEXUS_CONFIG_DIR": str(cfg), "NX_NO_TELEMETRY": "1"},
    )
    assert float(out.stdout.strip()) == 5.0
    assert gh.wait_before_attempt(rng=_rng()) > 3.0
