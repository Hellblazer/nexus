# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-151 nexus-u2vmv: cause-agnostic event-loop spin guard.

The T2 daemon must be constitutionally unable to peg at ~100% CPU regardless of
client version mix or takeover races. These tests cover the detector
(`SpinGuardSelector` counts zero-timeout ready-returns), the watchdog decision
logic (fires after K consecutive over-threshold windows, never on bounded load),
and a REAL selector spin (a no-op reader on a perpetually-ready pipe) to prove
the guard detects and breaks an actual 100% spin.
"""
from __future__ import annotations

import asyncio
import os
import selectors
import threading

import pytest

from nexus.daemon.spin_guard import SpinGuardSelector, SpinWatchdog


# ── detector ──────────────────────────────────────────────────────────────


def test_spinguard_selector_counts_zero_timeout_ready_returns() -> None:
    """A select(timeout≈0) that returns a ready fd is the spin signature and
    must be counted per-fd; a blocking/empty select must not."""
    sel = SpinGuardSelector()
    r, w = os.pipe()
    try:
        sel.register(r, selectors.EVENT_READ)
        os.write(w, b"x")  # read end now perpetually ready
        before = sel.zero_to_ready
        for _ in range(5):
            sel.select(0)  # zero-timeout poll, fd ready each time => spin shape
        assert sel.zero_to_ready == before + 5
        assert sel.ready_fd_hits.get(r, 0) >= 5
    finally:
        sel.unregister(r)
        os.close(r)
        os.close(w)
        sel.close()


def test_spinguard_selector_blocking_select_not_counted() -> None:
    """A select with a real timeout that returns nothing is healthy, not a spin."""
    sel = SpinGuardSelector()
    try:
        before = sel.zero_to_ready
        sel.select(0.001)  # nothing registered → returns empty
        assert sel.zero_to_ready == before
    finally:
        sel.close()


# ── watchdog decision logic (deterministic, injected clock + counter) ───────


class _FakeCounter:
    def __init__(self) -> None:
        self.zero_to_ready = 0
        self.ready_fd_hits: dict[int, int] = {}


def test_watchdog_fires_after_consecutive_over_threshold_windows() -> None:
    fired: list[dict] = []
    ctr = _FakeCounter()
    wd = SpinWatchdog(
        ctr,
        threshold_per_s=1000,
        window_s=1.0,
        consecutive=3,
        on_spin=lambda info: fired.append(info),
    )
    t = 0.0
    # 3 windows each adding 5000 zero_to_ready over 1s => 5000/s >> 1000/s
    for _ in range(3):
        ctr.zero_to_ready += 5000
        t += 1.0
        wd.poll(now=t)
    assert len(fired) == 1, "must fire once after 3 consecutive hot windows"
    assert fired[0]["rate_per_s"] >= 1000


def test_watchdog_does_not_fire_on_bounded_load() -> None:
    """Healthy high-but-bounded throughput must not trip the guard."""
    fired: list[dict] = []
    ctr = _FakeCounter()
    wd = SpinWatchdog(
        ctr, threshold_per_s=5000, window_s=1.0, consecutive=3,
        on_spin=lambda info: fired.append(info),
    )
    t = 0.0
    for _ in range(10):
        ctr.zero_to_ready += 800  # 800/s, well under threshold
        t += 1.0
        wd.poll(now=t)
    assert fired == []


def test_watchdog_resets_on_dip_below_threshold() -> None:
    """A transient burst (not sustained) must not fire — consecutive resets."""
    fired: list[dict] = []
    ctr = _FakeCounter()
    wd = SpinWatchdog(
        ctr, threshold_per_s=1000, window_s=1.0, consecutive=3,
        on_spin=lambda info: fired.append(info),
    )
    t = 0.0
    pattern = [5000, 5000, 0, 5000, 5000]  # never 3 in a row
    for d in pattern:
        ctr.zero_to_ready += d
        t += 1.0
        wd.poll(now=t)
    assert fired == []


# ── real spin: prove the guard catches an actual 100% selector spin ─────────


def test_real_selector_spin_is_detected_and_broken() -> None:
    """Register a no-op reader on a perpetually-ready pipe — asyncio will call
    _run_once at ~100% CPU forever. The watchdog must detect it and invoke
    on_spin, which stops the loop (the spin-breaker). A fallback timer stops the
    loop after 15s so a regression FAILS the assertion instead of hanging CI."""
    sel = SpinGuardSelector()
    loop = asyncio.SelectorEventLoop(sel)
    r, w = os.pipe()
    os.write(w, b"spin")  # read end perpetually ready; the reader never drains it
    fired: list[dict] = []

    def _on_spin(info: dict) -> None:
        fired.append(info)
        loop.call_soon_threadsafe(loop.stop)

    # Regression fallback: if the guard never fires, don't hang — stop the loop.
    guard_timer = threading.Timer(
        15.0, lambda: loop.call_soon_threadsafe(loop.stop)
    )
    guard_timer.start()

    def _noop_reader() -> None:
        # Deliberately does NOT read r — fd stays ready => busy spin.
        pass

    try:
        loop.add_reader(r, _noop_reader)
        wd = SpinWatchdog(
            sel, threshold_per_s=2000, window_s=0.5, consecutive=2,
            on_spin=_on_spin,
        )
        wd.start(loop)  # background thread
        loop.run_forever()
    finally:
        guard_timer.cancel()
        wd.stop()
        loop.remove_reader(r)
        loop.close()
        os.close(r)
        os.close(w)

    assert fired, "watchdog must detect and break a real selector spin"
    assert fired[0].get("hot_fd") == r, "capture must name the perpetually-ready fd"


# ── self-heal bounding: never worse than a pegged-but-serving daemon ─────────


class _FakeLoop:
    def __init__(self) -> None:
        self._ready: list = []


def test_spin_heal_count_window_prunes_old(tmp_path) -> None:
    from nexus.daemon import t2_daemon as td

    now = 1_000_000.0
    # Two stamps inside the window, one far outside.
    old = now - td._SPIN_HEAL_WINDOW_S - 100
    (tmp_path / ".t2_spin_heals").write_text(f"{old}\n{now - 10}\n")
    count = td._spin_heal_count_in_window(tmp_path, now)
    assert count == 2, "old stamp pruned; recent + current counted"


def test_spin_heal_disarms_after_max_never_suppresses(tmp_path, monkeypatch) -> None:
    """Critic C2: a persistent trigger must NOT drive repeated os._exit (which
    the supervisor crash-loop guard would turn into permanent suppression). After
    _SPIN_HEAL_MAX heals the daemon disarms self-heal and stays up (no SIGTERM,
    no exit timer) — pegged-but-serving == the pre-guard baseline, never worse."""
    from nexus.daemon import t2_daemon as td

    kills: list = []
    timers: list = []
    monkeypatch.setattr(td.os, "kill", lambda pid, sig: kills.append((pid, sig)))

    class _FakeTimer:
        def __init__(self, *a, **k) -> None:
            self.daemon = False

        def start(self) -> None:
            timers.append(1)

    monkeypatch.setattr(td.threading, "Timer", _FakeTimer)

    loop = _FakeLoop()
    info = {
        "hot_fd": 7, "rate_per_s": 99999.0,
        "ready_fd_hits": {7: 99999}, "threshold_per_s": td._SPIN_THRESHOLD_PER_S,
    }
    # First _SPIN_HEAL_MAX calls self-heal (SIGTERM + exit timer armed).
    for _ in range(td._SPIN_HEAL_MAX):
        td._t2_spin_capture_and_heal(tmp_path, loop, info)
    assert len(kills) == td._SPIN_HEAL_MAX
    assert len(timers) == td._SPIN_HEAL_MAX

    # The next spin EXCEEDS the bound → disarm: no further kill, no exit timer.
    td._t2_spin_capture_and_heal(tmp_path, loop, info)
    assert len(kills) == td._SPIN_HEAL_MAX, "must NOT self-heal past the bound"
    assert len(timers) == td._SPIN_HEAL_MAX, "must NOT arm os._exit past the bound"


def test_spin_capture_records_loop_ready_len(tmp_path, monkeypatch) -> None:
    """Critic C4: the capture must record loop._ready length (distinguishes a
    perpetually-ready-fd spin from a self-rescheduling call_soon spin)."""
    from nexus.daemon import t2_daemon as td

    monkeypatch.setattr(td.os, "kill", lambda *a: None)
    monkeypatch.setattr(td.threading, "Timer", lambda *a, **k: type(
        "T", (), {"daemon": False, "start": lambda self: None}
    )())

    loop = _FakeLoop()
    loop._ready = ["cb1", "cb2", "cb3"]
    td._t2_spin_capture_and_heal(
        tmp_path, loop,
        {"hot_fd": 9, "rate_per_s": 5e4, "ready_fd_hits": {9: 1}, "threshold_per_s": 1e4},
    )
    caps = list((tmp_path / "logs").glob("t2_spin_*.txt"))
    assert caps, "a capture file must be written"
    text = caps[0].read_text()
    assert "loop_ready_len=3" in text
