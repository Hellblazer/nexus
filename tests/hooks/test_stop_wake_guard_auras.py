# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-249 acceptance test 9 (S-r3-1, nexus-mdbds.11.1): an aura-owned
subscription to a CI --done topic is not a CI wake. The status record is built
by the real ChannelWaiter over the real SubscriptionSet, and the real Stop-hook
guard reads it: an aura's hold leaves board_topics without the topic, so a turn
that ends on a develop-CI wait still blocks. When the session subscribes the
same topic itself, the guard counts the session's subscription."""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
from pathlib import Path

from nexus.mcp import channel
from nexus.mcp.subscriptions import SubscriptionSet

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "conexus" / "hooks" / "scripts" / "stop_wake_guard.py"
_SID = "11111111-2222-3333-4444-555555555555"
_TOPIC = "board/ci/nexus-develop--done"
_CI_WAIT_TEXT = "Pushed. I'll read the CI result once the runs finish."


def _load():
    spec = importlib.util.spec_from_file_location("stop_wake_guard_auras_under_test", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


swg = _load()


class _NoStore:
    """Every read and wait returns no rows: the waiter's only engine contact."""

    class _Tuples:
        def rd(self, *a, **k):
            return []

        def wait(self, *a, **k):
            return []

    def __enter__(self):
        self.tuples = self._Tuples()
        return self

    def __exit__(self, *exc) -> None:
        return None


def _no_store():
    raise AssertionError("a board subscribe must not touch the store")


def _payload(text: str) -> dict:
    return {
        "session_id": _SID, "hook_event_name": "Stop", "stop_hook_active": False,
        "last_assistant_message": text, "background_tasks": [], "session_crons": [],
    }


def _run_waiter_and_decide(tmp_path: Path, monkeypatch, subs: SubscriptionSet):
    gate = asyncio.Event()
    gate.set()
    monkeypatch.setattr(channel, "_initialized_event", gate)
    waiter = channel.ChannelWaiter(
        _SID, _NoStore, subs, state_dir=tmp_path, channel_flag_probe=lambda: True,
        engine_version_probe=lambda: None, wait_timeout_s=1, min_tick_interval_s=0.05,
    )
    env = {"NEXUS_CONFIG_DIR": str(tmp_path)}

    async def scenario():
        task = asyncio.create_task(waiter.run())
        try:
            for _ in range(200):
                record = channel.read_channel_status(tmp_path, _SID)
                if record is not None and record.get("alive") is True and record.get("gate_open") is True:
                    # Decide while the waiter runs: a stopped waiter's record blocks for its own reason.
                    return record, swg.decide(_payload(_CI_WAIT_TEXT), env)
                await asyncio.sleep(0.02)
            raise AssertionError("the waiter never published an alive record")
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    return asyncio.run(scenario())


def test_an_aura_held_done_topic_is_not_a_ci_wake(tmp_path: Path, monkeypatch) -> None:
    subs = SubscriptionSet(session_id=_SID)
    subs.subscribe_aura("ci-red", _TOPIC)
    assert _TOPIC in [e["subspace"] for e in subs.entries()]  # it is delivered
    record, decision = _run_waiter_and_decide(tmp_path, monkeypatch, subs)
    assert _TOPIC not in record["board_topics"]
    # Non-vacuity: the record is otherwise one the guard reads as a wake.
    assert record["alive"] is True and record["channel_flag"] is True and record["gate_open"] is True
    assert decision.block and decision.reason == swg.CLASS_CI_WAIT, decision


def test_the_session_subscribing_the_aura_topic_is_counted(tmp_path: Path, monkeypatch) -> None:
    subs = SubscriptionSet(session_id=_SID)
    subs.subscribe_aura("ci-red", _TOPIC)
    # The session's own subscribe, after the aura's hold (a board topic never touches the store).
    subs.subscribe(_TOPIC, templates=[])  # type: ignore[arg-type]
    record, decision = _run_waiter_and_decide(tmp_path, monkeypatch, subs)
    assert record["board_topics"] == [_TOPIC]
    assert not decision.block and decision.reason == "ci-wake-armed", decision
