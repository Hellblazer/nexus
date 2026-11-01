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
import random
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

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

    def test_strikes_escalate_across_processes(self, cfg: Path) -> None:
        """Every process's first 503 is a delay of 2s; shared strikes grow the hold."""
        sleeps = [
            gh.record_503(2.0, retry_after=None, url="https://edge", now=1000.0 + i * 0.1, rng=_rng())
            for i in range(6)
        ]
        holds = [json.loads((cfg / gh.HOLD_FILENAME).read_text())["strikes"]]
        assert holds == [6]
        # Later strikers wait at least the escalated hold, capped.
        assert sleeps[0] == 2.0
        assert sleeps[-1] >= 20.0
        assert max(sleeps) <= gh.HOLD_CAP_S + gh.MAX_JITTER_S

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
        (cfg / gh.HOLD_FILENAME).write_text(body)
        assert gh.wait_before_attempt(now=1000.0, rng=_rng()) == 0.0
        assert gh.record_503(2.0, retry_after=None, url="https://edge", now=1000.0, rng=_rng()) == 2.0

    def test_a_far_future_hold_is_capped(self, cfg: Path) -> None:
        """A clock jump or a hand-edited file never stalls a client past the cap."""
        (cfg / gh.HOLD_FILENAME).write_text(json.dumps({"not_before": 1e12, "strikes": 1, "last_503": 1000.0}))
        assert gh.wait_before_attempt(now=1000.0, rng=_rng()) <= gh.HOLD_CAP_S + gh.MAX_JITTER_S

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
        env={**__import__("os").environ, "NEXUS_CONFIG_DIR": str(cfg), "NX_NO_TELEMETRY": "1"},
    )
    assert float(out.stdout.strip()) == 5.0
    assert gh.wait_before_attempt(rng=_rng()) > 3.0
