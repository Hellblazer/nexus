"""nexus-zgqxm: the T1 heartbeat loop drives a periodic orphan sweep so a
long-lived MCP reaps PPID=1 orphan resource-trackers / chromadbs continuously,
not only at the next startup (closing the POSIX-semaphore exhaustion gap).
"""
import asyncio

import pytest

from nexus.mcp import core


def test_periodic_sweep_protects_live_chroma_pid(monkeypatch):
    """The live chroma ``server_pid`` is passed as ``protected_pids`` so the
    sweep can never target this session's own tracker."""
    captured: dict[str, set[int] | None] = {}

    def _fake_trackers(*, protected_pids=None):
        captured["trackers"] = protected_pids
        return 0

    def _fake_chromas(*, protected_pids=None):
        captured["chromas"] = protected_pids
        return 0

    monkeypatch.setattr(
        "nexus.session.sweep_orphan_resource_trackers", _fake_trackers
    )
    monkeypatch.setattr(
        "nexus.session.sweep_orphan_t1_chromadbs", _fake_chromas
    )
    monkeypatch.setitem(core._OWNED_CHROMA, "server_pid", 424242)

    core._periodic_orphan_sweep()

    assert captured["trackers"] == {424242}
    assert captured["chromas"] == {424242}


def test_periodic_sweep_no_pid_uses_empty_protected(monkeypatch):
    """No live chroma → empty protected set (still safe; PPID=1 gate applies)."""
    captured: dict[str, set[int] | None] = {}
    monkeypatch.setattr(
        "nexus.session.sweep_orphan_resource_trackers",
        lambda *, protected_pids=None: captured.setdefault("p", protected_pids) or 0,
    )
    monkeypatch.setattr(
        "nexus.session.sweep_orphan_t1_chromadbs",
        lambda *, protected_pids=None: 0,
    )
    core._OWNED_CHROMA.pop("server_pid", None)

    core._periodic_orphan_sweep()

    assert captured["p"] == set()


def test_periodic_sweep_swallows_errors(monkeypatch):
    """A sweep failure never propagates out of the heartbeat tick."""
    def _boom(*, protected_pids=None):
        raise RuntimeError("ps exploded")

    monkeypatch.setattr(
        "nexus.session.sweep_orphan_resource_trackers", _boom
    )
    monkeypatch.setattr(
        "nexus.session.sweep_orphan_t1_chromadbs",
        lambda *, protected_pids=None: 0,
    )
    # Must not raise.
    core._periodic_orphan_sweep()


def test_heartbeat_loop_triggers_periodic_sweep(monkeypatch):
    """With the interval forced to 0, the loop runs the sweep on its ticks."""
    calls = {"sweep": 0, "tick": 0}

    class _Publisher:
        def tick(self):
            calls["tick"] += 1

    monkeypatch.setattr(core, "_PERIODIC_SWEEP_INTERVAL_S", 0.0)
    monkeypatch.setattr(
        core, "_periodic_orphan_sweep", lambda: calls.__setitem__("sweep", calls["sweep"] + 1)
    )

    async def _run():
        loop_task = asyncio.create_task(
            core._t1_heartbeat_loop(_Publisher(), interval=0.001)
        )
        await asyncio.sleep(0.05)
        loop_task.cancel()
        try:
            await loop_task
        except asyncio.CancelledError:
            pass

    asyncio.run(_run())
    assert calls["tick"] >= 1
    assert calls["sweep"] >= 1
