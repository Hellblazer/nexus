# SPDX-License-Identifier: AGPL-3.0-or-later
"""The suite lease line is first come, first served, and a wait ends on the wall clock too (nexus-74bc6).

A waiter used to poll ``mkdir`` once a second, so whichever poll landed first after a
release won and, with three or more waiters, one could lose every round. A waiter now
holds a ticket (``suite.queue/<arrival ns>-<pid>``) and tries the lease only with no live
older ticket. ``time.monotonic`` stops while a Mac sleeps, so the wait also reads the wall
clock. The helpers are the ones ``tests/test_suite_lease.py`` already uses.
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

from tests import _suite_lease
from tests._module_seam import module_time, setattr_in
from tests.test_suite_lease import _dead_pid, _events, _live_holder, _virtual_time, events_log, lease_root  # noqa: F401 -- fixtures


def _ticket(root: Path, *, older_by_s: float = 5.0, pid: int | None = None, label: str = "first-in-line") -> Path:
    """A waiter's ticket as a peer would have left it, ``older_by_s`` before now."""
    queue_dir = root / _suite_lease.QUEUE_DIR
    queue_dir.mkdir(parents=True, exist_ok=True)
    path = queue_dir / f"{time.time_ns() - int(older_by_s * 1e9):020d}-{os.getpid() if pid is None else pid}"
    path.write_text(f"{label}\n")
    return path


def _tickets(root: Path) -> list[str]:
    queue_dir = root / _suite_lease.QUEUE_DIR
    return sorted(p.name for p in queue_dir.iterdir()) if queue_dir.is_dir() else []


def test_a_live_older_ticket_keeps_a_later_arrival_off_a_free_lease(
        lease_root: Path, events_log: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _ticket(lease_root)
    clock = _virtual_time(monkeypatch)
    assert _suite_lease.acquire("later", wait_seconds=5, lease_root=lease_root) is None
    assert not (lease_root / _suite_lease.RESOURCE).exists(), "the free lease was taken past the head of the line"
    assert clock["t"] >= 5
    (event,) = _events(events_log)
    assert event["outcome"] == "gave_up" and f"holder_pid={os.getpid()} holder_label=first-in-line" in event["rest"]


def test_the_next_in_line_takes_the_lease_once_the_one_ahead_leaves(
        lease_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ahead = _ticket(lease_root)
    seen: list[str] = []
    _virtual_time(monkeypatch, on_sleep=lambda t: ahead.unlink(missing_ok=True) if t >= 2 else None)
    release = _suite_lease.acquire("second", wait_seconds=60, lease_root=lease_root,
                                   on_wait=lambda who, _w: seen.append(who))
    assert release is not None
    try:
        assert seen and seen[0] == f"pid {os.getpid()} (first-in-line)", seen
    finally:
        release()


def test_a_dead_waiters_ticket_does_not_hold_the_line_and_is_removed(lease_root: Path) -> None:
    corpse = _ticket(lease_root, pid=_dead_pid(), label="killed waiter")
    release = _suite_lease.acquire("next", wait_seconds=60, lease_root=lease_root)
    assert release is not None, "a killed waiter's ticket wedged the line"
    release()
    assert not corpse.exists()


def test_a_ticket_older_than_any_wait_is_ignored_even_with_a_live_pid(lease_root: Path) -> None:
    """A reused pid must not hold the line forever."""
    stale = _ticket(lease_root, older_by_s=_suite_lease.TICKET_MAX_AGE_SECONDS + 60)
    release = _suite_lease.acquire("next", wait_seconds=60, lease_root=lease_root)
    assert release is not None
    release()
    assert not stale.exists()


def test_a_later_ticket_does_not_block_an_earlier_arrival(lease_root: Path) -> None:
    behind = _ticket(lease_root, older_by_s=-30.0, label="behind me")
    release = _suite_lease.acquire("first", wait_seconds=60, lease_root=lease_root)
    assert release is not None
    release()
    assert behind.exists(), "another waiter's ticket is not ours to remove"


def test_a_garbage_ticket_name_is_skipped(lease_root: Path) -> None:
    queue_dir = lease_root / _suite_lease.QUEUE_DIR
    queue_dir.mkdir(parents=True)
    (queue_dir / "00000000000000000001-notapid").write_text("x\n")
    release = _suite_lease.acquire("next", wait_seconds=60, lease_root=lease_root)
    assert release is not None
    release()


def test_a_ticket_is_removed_on_every_way_out(lease_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Won, given up, and refused with no wait budget (which never joins the line at all)."""
    first = _suite_lease.acquire("holder", wait_seconds=60, lease_root=lease_root)
    assert first is not None and _tickets(lease_root) == []
    _virtual_time(monkeypatch)
    assert _suite_lease.acquire("gives up", wait_seconds=3, lease_root=lease_root) is None
    assert _tickets(lease_root) == [], "a give-up left its ticket in the line"
    assert _suite_lease.acquire("no wait", wait_seconds=0, lease_root=lease_root) is None
    assert _tickets(lease_root) == []
    first()


def test_a_run_with_no_wait_budget_does_not_queue_behind_a_ticket(lease_root: Path) -> None:
    """NX_SUITE_LEASE_WAIT=0 means "do not queue": a free lease is taken, as before."""
    _ticket(lease_root)
    release = _suite_lease.acquire("no wait", wait_seconds=0, lease_root=lease_root)
    assert release is not None
    release()


def test_an_unreadable_queue_falls_back_to_the_unordered_poll(lease_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _ticket(lease_root)
    real_listdir = os.listdir

    def deny(path, *a, **k):
        if str(path).endswith(_suite_lease.QUEUE_DIR):
            raise PermissionError(path)
        return real_listdir(path, *a, **k)

    setattr_in(monkeypatch, _suite_lease, "os.listdir", deny)
    release = _suite_lease.acquire("next", wait_seconds=60, lease_root=lease_root)
    assert release is not None, "a queue nobody can read must not wedge the lease"
    release()


def test_three_waiters_are_served_in_the_order_they_arrived(lease_root: Path) -> None:
    """Arrivals at 0.0, 0.4 and 0.8 s poll at those phases of each second and the holder lets go at 1.5 s,
    so unordered polling serves the LAST arrival first (its next poll is at 1.8 s, w1's at 2.0 s). The
    line serves w1 first. Real time: the 1 s poll is not injectable across threads."""
    holder = _suite_lease.acquire("holder", lease_root=lease_root)
    assert holder is not None
    served: list[str] = []
    errors: list[BaseException] = []

    def waiter(name: str) -> None:
        try:
            release = _suite_lease.acquire(name, wait_seconds=60, lease_root=lease_root)
            assert release is not None
            served.append(name)
            time.sleep(0.1)
            release()
        except BaseException as exc:  # noqa: BLE001 -- surfaced by the main thread
            errors.append(exc)

    threads = []
    for name, delay in (("w1", 0.0), ("w2", 0.4), ("w3", 0.4)):
        time.sleep(delay)
        thread = threading.Thread(target=waiter, args=(name,))
        thread.start()
        threads.append(thread)
    time.sleep(0.7)  # t = 1.5 s: all three are in the line
    holder()
    for thread in threads:
        thread.join(timeout=30)
    assert not errors, errors
    assert served == ["w1", "w2", "w3"], served


def test_a_wait_spent_asleep_ends_on_the_wall_clock(lease_root: Path, events_log: Path,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    """time.monotonic does not advance while a Mac sleeps; a 60 s wait must not stretch past the wall clock."""
    _live_holder(lease_root, "the-long-run")
    state = {"mono": 0.0, "wall": 1_000_000.0, "sleeps": 0}

    def fake_sleep(seconds: float) -> None:
        state["sleeps"] += 1
        state["mono"] += seconds
        state["wall"] += 40.0  # the lid was shut for most of each second
        assert state["sleeps"] < 10, "the wait ran on the monotonic clock alone"

    proxy = module_time(monkeypatch, _suite_lease)
    proxy.sleep = fake_sleep
    proxy.monotonic = lambda: state["mono"]
    proxy.time = lambda: state["wall"]
    assert _suite_lease.acquire("sleeper", wait_seconds=60, lease_root=lease_root) is None
    assert state["sleeps"] == 2
    (event,) = _events(events_log)
    assert event["outcome"] == "gave_up" and int(event["waited"]) >= 60


def test_a_wall_clock_stepped_backwards_does_not_lengthen_the_wait(lease_root: Path,
                                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    _live_holder(lease_root)
    state = {"mono": 0.0, "wall": 1_000_000.0}

    def fake_sleep(seconds: float) -> None:
        state["mono"] += seconds
        state["wall"] -= 100.0
        assert state["mono"] < 100, "the wait ran on the wall clock alone"

    proxy = module_time(monkeypatch, _suite_lease)
    proxy.sleep = fake_sleep
    proxy.monotonic = lambda: state["mono"]
    proxy.time = lambda: state["wall"]
    assert _suite_lease.acquire("steps", wait_seconds=5, lease_root=lease_root) is None
    assert state["mono"] == 5.0
