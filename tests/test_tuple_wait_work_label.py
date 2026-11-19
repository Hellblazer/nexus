# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx tuple wait`` labels itself as work its identity owns (RDR-253 Step 6, nexus-hczdo.1.9): ``kind=wait`` for the
session id, running while the wait parks, ``done`` when it exits, whatever the reason. A label is never a reason for the
wait to fail or to stop."""
from __future__ import annotations

import pytest
from click.testing import CliRunner

from nexus.commands import tuple_cmd
from nexus.commands.tuple_cmd import tuple_group
from nexus.tuple_wait import DELIVERED, WaitOutcome


class _Store:
    def close(self) -> None:
        pass


@pytest.fixture
def labels(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, str]]:
    seen: list[tuple[str, str, str]] = []
    monkeypatch.setattr("nexus.channel_launch._claude_ancestor_argv", lambda: "claude --resume x")
    monkeypatch.setattr(tuple_cmd, "_store", _Store)
    monkeypatch.setattr(tuple_cmd, "_board_entries_reader", lambda _sid: (lambda: []))
    monkeypatch.setattr("nexus.identity.work.register_own",
                        lambda kind, ref, state="running", **k: seen.append((kind, ref, state)) or True)
    return seen


def _wait() -> object:
    return CliRunner().invoke(tuple_group, ["wait", "--session", "s1", "--max-parks", "1"])


def test_a_wait_is_running_work_while_it_parks_and_done_when_it_returns(monkeypatch: pytest.MonkeyPatch, labels: list) -> None:
    during: list[int] = []
    monkeypatch.setattr("nexus.tuple_wait.run_wait", lambda *a, **k: during.append(len(labels)) or WaitOutcome(reason=DELIVERED, lines=["x"]))
    assert _wait().exit_code == 0
    assert during == [1], "registered before the park"
    assert labels == [("wait", "s1", "running"), ("wait", "s1", "done")]


def test_a_wait_that_fails_is_still_marked_done(monkeypatch: pytest.MonkeyPatch, labels: list) -> None:
    def boom(*a, **k):
        raise RuntimeError("engine down")

    monkeypatch.setattr("nexus.tuple_wait.run_wait", boom)
    assert _wait().exit_code == 1
    assert labels[-1] == ("wait", "s1", "done")


def test_no_identity_means_no_label_and_the_wait_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("nexus.channel_launch._claude_ancestor_argv", lambda: "claude --resume x")
    monkeypatch.setattr(tuple_cmd, "_store", _Store)
    monkeypatch.setattr(tuple_cmd, "_board_entries_reader", lambda _sid: (lambda: []))
    monkeypatch.setattr("nexus.identity.work.caller_identity", lambda: None)
    called: list[str] = []
    monkeypatch.setattr("nexus.tuple_wait.run_wait", lambda *a, **k: called.append("ran") or WaitOutcome(reason=DELIVERED, lines=["x"]))
    assert _wait().exit_code == 0 and called == ["ran"]
