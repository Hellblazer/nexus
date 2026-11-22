# SPDX-License-Identifier: AGPL-3.0-or-later
"""A work row ends with its work (nexus-hczdo, Sam 2026-11-22): the agent's stop and a Label's finish retry a failed end
once on a longer bound, and an end that still did not write says so in the log, so the reap's count is not the only trace.
On 2026-11-21 the start hook logged 251 ``subagent_work_registered`` lines and the stops that missed their rows left none."""
from __future__ import annotations

import pytest


class Log:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def info(self, event: str, **kw) -> None:
        self.events.append((event, kw))

    warning = info

    def names(self) -> list[str]:
        return [e for e, _ in self.events]


PAYLOAD = {"agent_id": "a1", "agent_type": "Explore", "session_id": "sid-1"}


@pytest.fixture
def log(monkeypatch: pytest.MonkeyPatch) -> Log:
    from nexus.hooks import subagent_work

    out = Log()
    monkeypatch.setattr(subagent_work, "_log", out)
    return out


def test_a_stop_whose_first_forget_fails_retries_once_on_the_longer_bound(monkeypatch, log) -> None:
    from nexus.hooks import subagent_work
    from nexus.identity import work

    calls: list[dict] = []
    monkeypatch.setattr(work, "caller_identity", lambda: "i1")
    monkeypatch.setattr(work, "forget_own", lambda kind, ref, **k: calls.append(k) or len(calls) > 1)
    assert subagent_work.register_stop(PAYLOAD) is True
    assert calls[0].get("timeout") is None
    assert subagent_work.STOP_RETRY_FLOOR_S <= calls[1]["timeout"] <= subagent_work.STOP_RETRY_TIMEOUT_S
    assert "subagent_work_forgotten" in log.names()


def test_a_stop_that_cannot_end_its_row_says_why_and_one_with_no_identity_says_nothing(monkeypatch, log) -> None:
    from nexus.hooks import subagent_work
    from nexus.identity import work

    calls: list[dict] = []
    monkeypatch.setattr(work, "caller_identity", lambda: None)
    monkeypatch.setattr(work, "forget_own", lambda kind, ref, **k: calls.append(k) or False)
    assert subagent_work.register_stop(PAYLOAD) is False
    assert log.names() == [] and len(calls) == 1, "no identity: it registered nothing, so no retry and no false alarm"
    monkeypatch.setattr(work, "caller_identity", lambda: "i1")
    assert subagent_work.register_stop(PAYLOAD) is False
    assert log.names() == ["subagent_work_stop_failed"] and len(calls) == 3


def test_the_retry_takes_only_what_is_left_of_the_budget(monkeypatch, log) -> None:
    """A first call that used the whole budget leaves no retry (the hook would be killed before it ended)."""
    from nexus.hooks import subagent_work
    from nexus.identity import work

    clock = iter([0.0, subagent_work.STOP_BUDGET_S - 0.1])
    from tests._module_seam import setattr_in

    setattr_in(monkeypatch, subagent_work, "time.monotonic", lambda: next(clock))
    monkeypatch.setattr(work, "caller_identity", lambda: "i1")
    calls: list[dict] = []
    monkeypatch.setattr(work, "forget_own", lambda kind, ref, **k: calls.append(k) or False)
    assert subagent_work.register_stop(PAYLOAD) is False and len(calls) == 1


def test_the_stop_writes_its_lines_to_the_hook_log(monkeypatch, tmp_path) -> None:
    """S2 (critic): the stop verb configured no logging, so its lines never reached hook.log."""
    from nexus.hooks import subagent_work

    seen: list[bool] = []
    monkeypatch.setattr("nexus._hook_runtime._io.configure_hook_logging", lambda: seen.append(True))
    monkeypatch.setattr("nexus.identity.work.forget_own", lambda kind, ref, **k: True)
    assert subagent_work.register_stop(PAYLOAD) is True and seen == [True]


def test_the_stop_retry_fits_the_hooks_budget() -> None:
    import json
    from pathlib import Path

    from nexus.hooks import subagent_work
    from nexus.identity import work

    hooks = json.loads((Path(__file__).resolve().parents[2] / "conexus" / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    budget = min(h["timeout"] for group in hooks["hooks"]["SubagentStop"] for h in group["hooks"])
    assert subagent_work.STOP_BUDGET_S <= budget - 3.0, "3 s of the hook's budget go to uv, the imports and the nudge first"
    assert work.LABEL_TIMEOUT_S + subagent_work.STOP_RETRY_FLOOR_S <= subagent_work.STOP_BUDGET_S


def test_a_label_whose_end_fails_retries_once_and_logs_a_second_failure(monkeypatch) -> None:
    from nexus.identity import work

    out = Log()
    monkeypatch.setattr(work, "_log", out)
    monkeypatch.setattr(work, "caller_identity", lambda: "i1")
    monkeypatch.setattr(work, "register_own", lambda *a, **k: True)
    ends: list[float] = []
    monkeypatch.setattr(work, "finish_own", lambda kind, ref, **k: ends.append(k["timeout"]) or len(ends) > 1)
    label = work.Label("wait", "ci:develop@abc")
    assert label.start()
    label.finish()
    assert ends == [work.LABEL_TIMEOUT_S, work.FINISH_RETRY_TIMEOUT_S] and "identity_work_finish_failed" not in out.names()
    ends.clear()
    monkeypatch.setattr(work, "finish_own", lambda kind, ref, **k: ends.append(k["timeout"]) and False)
    label = work.Label("wait", "ci:develop@def")
    assert label.start()
    label.finish()
    assert len(ends) == 2 and "identity_work_finish_failed" in out.names()
