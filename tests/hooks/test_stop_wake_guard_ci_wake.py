# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Stop wake guard on a CI wake the MCP server armed (nexus-1d6pz.11.5).

Split from test_stop_wake_guard.py, which is over its file-size ceiling (nexus-sby49.4). The guard module, the payload
builder, the subprocess runner and the CI command are the parent file's; these tests only add the channel-status record
and the subscription-change republish.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
from pathlib import Path

import pytest

from nexus.mcp import channel
from nexus.mcp.subscriptions import SubscriptionSet
from tests.hooks.test_stop_wake_guard import _CI_COMMAND, _log, _payload, _run, swg


# --- a CI wake the MCP server armed (nexus-1d6pz.11.5) ---------------------------------------------
#
# The guard reads the record the session's nexus MCP server writes
# (<config>/tuple-watch/channel-status.d/<session id>, nexus.mcp.channel.write_channel_status). A
# CI wait at the end of a turn is covered when the waiter is alive and fresh, the channel flag is
# present and a board/ci/*--done topic is subscribed; with the subscription but no flag it blocks
# naming `nx ci wait`.

_SID = "11111111-2222-3333-4444-555555555555"
_CI_WAIT_TEXT = "Pushed. I'll read the CI result once the runs finish."


def _status(tmp_path: Path, **fields: object) -> Path:
    record = {
        "alive": True, "gate_stalled": False, "channel_flag": True, "gate_open": True,
        "board_topics": ["board/ci/nexus-develop--done"], **fields,
    }
    path = tmp_path / "tuple-watch" / "channel-status.d" / _SID
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


class TestCiWakeArmed:
    def _decide(self, tmp_path: Path, text: str = _CI_WAIT_TEXT, **extra: object):
        return swg.decide(_payload(text, **extra), {"NEXUS_CONFIG_DIR": str(tmp_path)})

    def test_the_text_blocks_as_a_ci_wait_with_nothing_armed(self, tmp_path: Path) -> None:
        decision = self._decide(tmp_path)
        assert decision.block and decision.reason == swg.CLASS_CI_WAIT, decision
        reason = swg.block_reason(decision.reason, decision.clause, ci_wait=_CI_COMMAND)
        assert f"`{_CI_COMMAND}`. Run it as a background command" in reason
        assert "Monitor" in reason  # still offered for a run that is not CI
        assert "CI board" not in swg.block_reason(decision.reason, decision.clause)

    def test_channel_and_a_done_subscription_cover_a_ci_wait(self, tmp_path: Path) -> None:
        _status(tmp_path)
        decision = self._decide(tmp_path)
        assert not decision.block and decision.reason == "ci-wake-armed", decision

    def test_a_subscription_without_the_channel_blocks_as_the_generic_ci_wait(self, tmp_path: Path) -> None:
        _status(tmp_path, channel_flag=False)
        decision = self._decide(tmp_path)
        assert decision.block and decision.reason == swg.CLASS_CI_WAIT, decision
        reason = swg.block_reason(decision.reason, decision.clause, ci_wait=_CI_COMMAND)
        assert f"`{_CI_COMMAND}`" in reason and "channel" not in reason
        assert "channel" not in swg.block_reason(decision.reason, decision.clause)

    @pytest.mark.parametrize("fields", [
        {"alive": False}, {"gate_stalled": True}, {"gate_open": False}, {"board_topics": ["board/ci/nexus-develop"]},
        {"board_topics": []}, {"channel_flag": None},
    ])
    def test_a_record_that_proves_no_wake_still_blocks_as_a_plain_ci_wait(self, tmp_path: Path, fields: dict) -> None:
        _status(tmp_path, **fields)
        decision = self._decide(tmp_path)
        assert decision.block and decision.reason == swg.CLASS_CI_WAIT, decision

    def test_a_dead_waiter_without_the_flag_is_not_called_a_missing_channel(self, tmp_path: Path) -> None:
        _status(tmp_path, alive=False, channel_flag=False)
        assert self._decide(tmp_path).reason == swg.CLASS_CI_WAIT

    def test_a_record_from_the_future_is_no_wake(self, tmp_path: Path) -> None:
        path = _status(tmp_path)
        future = time.time() + 86400
        os.utime(path, (future, future))
        assert self._decide(tmp_path).block

    def test_only_the_servers_config_dir_is_read(self, tmp_path: Path) -> None:
        _status(tmp_path)
        decision = swg.decide(_payload(_CI_WAIT_TEXT), {"NX_CONFIG_DIR": str(tmp_path), "HOME": str(tmp_path / "h")})
        assert decision.block, "nexus_config_dir() reads NEXUS_CONFIG_DIR only; the record is not where the server writes"

    @pytest.mark.parametrize("text", [
        # a covered CI wait never hides a later promise or timed step, in either order
        "Pushed. I'll read the CI result once the runs finish. Then I'll recheck the staging deploy in 2 hours.",
        "I'll recheck the staging deploy in 2 hours. Pushed; I'll read the CI result once the runs finish.",
        "The CI checks are still running. I'll check the dashboard again at 14:30.",
    ])
    def test_an_armed_wake_covers_the_ci_clause_and_nothing_else(self, tmp_path: Path, text: str) -> None:
        _status(tmp_path)
        decision = self._decide(tmp_path, text)
        assert decision.block and decision.reason in (swg.CLASS_PROMISE, swg.CLASS_TIMED), decision

    @pytest.mark.parametrize("text", [
        "CI is still running, then I'll check the dashboard at 14:30.",
        "CI is still running, then I'll rebase the branch.",
    ])
    def test_a_clause_that_carries_a_second_step_is_not_covered(self, tmp_path: Path, text: str) -> None:
        _status(tmp_path)
        assert self._decide(tmp_path, text).block

    @pytest.mark.parametrize("text", [
        "The reviewers are still running.",
        "The embedding index build is in progress.",
        "The release PR checks are still running.",
        "The Monitor hasn't reported yet.",
        "Once the tests finish I'll run the migration.",
    ])
    def test_a_wait_the_develop_wake_does_not_report_is_not_covered(self, tmp_path: Path, text: str) -> None:
        _status(tmp_path)
        decision = self._decide(tmp_path, text)
        assert decision.block and decision.reason == swg.CLASS_CI_WAIT, decision

    def test_a_stale_record_is_no_wake(self, tmp_path: Path) -> None:
        path = _status(tmp_path)
        old = time.time() - swg.CI_WAKE_FRESH_S - 60
        os.utime(path, (old, old))
        assert self._decide(tmp_path).block

    def test_a_record_for_another_session_is_not_read(self, tmp_path: Path) -> None:
        _status(tmp_path)
        assert swg.decide(_payload(_CI_WAIT_TEXT, session_id="99999999-2222-3333-4444-555555555555"),
                          {"NEXUS_CONFIG_DIR": str(tmp_path)}).block
        assert swg.ci_wake_state("../../etc", {"NEXUS_CONFIG_DIR": str(tmp_path)}) == swg.CI_WAKE_NONE

    def test_a_ci_subscription_does_not_cover_a_timed_promise(self, tmp_path: Path) -> None:
        _status(tmp_path)
        decision = self._decide(tmp_path, "I'll check the dashboard again at 14:30.")
        assert decision.block and decision.reason != swg.CLASS_CI_WAIT, decision

    def test_regression_pin_a_running_nx_ci_wait_is_a_background_task(self, tmp_path: Path) -> None:
        """Not new behaviour: any running background task arms the guard (the bead asks only for this pin)."""
        task = {"id": "b1", "type": "shell", "status": "running", "command": "nx ci wait abc1234"}
        decision = self._decide(tmp_path, background_tasks=[task])
        assert not decision.block and decision.reason == "armed", decision

    def test_end_to_end_the_script_allows_and_logs_the_armed_wake(self, tmp_path: Path) -> None:
        _status(tmp_path)
        proc = _run(_payload(_CI_WAIT_TEXT), tmp_path)
        assert proc.returncode == 0 and proc.stdout == "", proc
        assert "allow reason=ci-wake-armed" in _log(tmp_path)


class _NoStore:
    """A store with nothing in it: every read and wait returns no rows. The waiter's only engine contact."""

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


def test_the_waiter_status_record_carries_what_the_guard_reads(tmp_path: Path, monkeypatch) -> None:
    """The writer half, through ChannelWaiter.run(): the record a running waiter publishes reads as ARMED,
    so dropping the run-start flag read (or either status field) fails here."""
    subs = SubscriptionSet(session_id=_SID)
    subs._board["board/ci/nexus-develop--done"] = None  # noqa: SLF001 -- a subscription without the store
    gate = asyncio.Event()
    gate.set()
    monkeypatch.setattr(channel, "_initialized_event", gate)
    waiter = channel.ChannelWaiter(
        _SID, _NoStore, subs, state_dir=tmp_path, channel_flag_probe=lambda: True,
        engine_version_probe=lambda: None, wait_timeout_s=1, min_tick_interval_s=0.05,
    )

    async def scenario() -> str:
        task = asyncio.create_task(waiter.run())
        try:
            for _ in range(200):
                state = swg.ci_wake_state(_SID, {"NEXUS_CONFIG_DIR": str(tmp_path)})
                if state == swg.CI_WAKE_ARMED:
                    return state
                await asyncio.sleep(0.02)
            return swg.ci_wake_state(_SID, {"NEXUS_CONFIG_DIR": str(tmp_path)})
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    assert asyncio.run(scenario()) == swg.CI_WAKE_ARMED
    record = channel.read_channel_status(tmp_path, _SID)
    assert record is not None and record["board_topics"] == ["board/ci/nexus-develop--done"]


def test_a_subscription_change_republishes_the_record(tmp_path: Path, monkeypatch) -> None:
    """A --done subscription made just before a turn ends shows in the record at once, not after a park."""
    subs = SubscriptionSet(session_id=_SID)
    gate = asyncio.Event()
    gate.set()
    monkeypatch.setattr(channel, "_initialized_event", gate)
    waiter = channel.ChannelWaiter(
        _SID, _NoStore, subs, state_dir=tmp_path, channel_flag_probe=lambda: True,
        engine_version_probe=lambda: None, wait_timeout_s=1,
    )

    async def scenario() -> list[str]:
        waiter._loop = asyncio.get_running_loop()  # noqa: SLF001
        waiter._alive = True  # noqa: SLF001
        waiter._channel_flag = True  # noqa: SLF001
        subs._board["board/ci/nexus-develop--done"] = None  # noqa: SLF001
        waiter._on_subscription_change(subs)  # noqa: SLF001
        await asyncio.sleep(0.05)
        record = channel.read_channel_status(tmp_path, _SID)
        return record["board_topics"] if record else []

    assert asyncio.run(scenario()) == ["board/ci/nexus-develop--done"]
