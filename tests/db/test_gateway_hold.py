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
        assert sleeps == [2.0] * 19

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
