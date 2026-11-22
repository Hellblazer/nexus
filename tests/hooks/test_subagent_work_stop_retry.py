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
    assert [c.get("timeout") for c in calls] == [None, subagent_work.STOP_RETRY_TIMEOUT_S]
    assert "subagent_work_forgotten" in log.names()


def test_a_stop_that_cannot_end_its_row_says_why(monkeypatch, log) -> None:
    from nexus.hooks import subagent_work
    from nexus.identity import work

    wlog = Log()
    monkeypatch.setattr(work, "_log", wlog)
    monkeypatch.setattr(work, "caller_identity", lambda: None)
    assert subagent_work.register_stop(PAYLOAD) is False
    assert wlog.names() == ["identity_work_forget_no_identity"] * 2 and log.names() == ["subagent_work_stop_failed"]
    log.events.clear()
    monkeypatch.setattr(work, "caller_identity", lambda: "i1")
    monkeypatch.setattr(work, "forget_own", lambda kind, ref, **k: False)
    assert subagent_work.register_stop(PAYLOAD) is False
    assert log.names() == ["subagent_work_stop_failed"]


def test_the_stop_retry_fits_the_hooks_budget() -> None:
    import json
    from pathlib import Path

    from nexus.hooks import subagent_work
    from nexus.identity import work

    hooks = json.loads((Path(__file__).resolve().parents[2] / "conexus" / "hooks" / "hooks.json").read_text())
    budget = min(h["timeout"] for group in hooks["hooks"]["SubagentStop"] for h in group["hooks"])
    assert work.LABEL_TIMEOUT_S + subagent_work.STOP_RETRY_TIMEOUT_S < budget


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
