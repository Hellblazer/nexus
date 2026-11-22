# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx ci wait`` labels itself as work its identity owns (RDR-253 Step 6 part 1, nexus-hczdo): ``kind=wait`` with ref
``ci:<branch>@<sha>``, running while it waits and ``done`` at exit, whatever the exit. A label is never a reason for the
wait to fail, to wait on the engine, or to disagree with the exit status it reports."""
from __future__ import annotations

import pytest
from click.testing import CliRunner

from nexus import ci_wait
from nexus.commands import ci_cmd
from nexus.commands.ci_cmd import ci_group


@pytest.fixture(autouse=True)
def _origin(monkeypatch: pytest.MonkeyPatch) -> None:
    """`nx ci wait` reads the repo from the origin remote and has no default repo: these tests are nexus's."""
    monkeypatch.setattr(ci_cmd, "_origin_url", lambda: "git@github.com:Hellblazer/nexus.git")
from nexus.identity.work import Label

SHA = "a0dc17ec284ffce6d4ff5264a626b7ef8e1a293a"


class _Store:
    pass


@pytest.fixture
def labels(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, str]]:
    """What the wait wrote: ``(kind, ref, state)`` per registration."""
    seen: list[tuple[str, str, str]] = []
    monkeypatch.setattr(ci_cmd, "_store", _Store)
    monkeypatch.setattr("nexus.identity.work.caller_identity", lambda: "i1")
    monkeypatch.setattr("nexus.identity.work.register_own",
                        lambda kind, ref, state="running", **k: seen.append((kind, ref, state)) or True)
    return seen


def _wait(*extra: str) -> object:
    return CliRunner().invoke(ci_group, ["wait", SHA, "--timeout", "1", *extra])


def test_a_wait_is_running_work_while_it_waits_and_done_when_it_exits(monkeypatch: pytest.MonkeyPatch, labels: list) -> None:
    during: list[int] = []

    def find(*a, **k):
        during.append(len(labels))
        return None

    monkeypatch.setattr(ci_wait, "find_done_row", find)
    result = _wait("--fresh")
    assert result.exit_code == ci_wait.EXIT_TIMEOUT, result.output
    assert during == [1], "registered before the first board read"
    assert labels == [("wait", f"ci:develop@{SHA}", "running"), ("wait", f"ci:develop@{SHA}", "done")]


def test_a_wait_that_cannot_read_the_board_is_still_marked_done(monkeypatch: pytest.MonkeyPatch, labels: list) -> None:
    def unreadable(*a, **k):
        raise ci_wait.BoardUnreadableError("down")

    monkeypatch.setattr(ci_wait, "find_done_row", unreadable)
    assert _wait().exit_code == ci_wait.EXIT_UNUSABLE
    assert labels[-1] == ("wait", f"ci:develop@{SHA}", "done")


def test_a_wait_that_raises_is_still_marked_done(monkeypatch: pytest.MonkeyPatch, labels: list) -> None:
    def boom(*a, **k):
        raise RuntimeError("bug")

    monkeypatch.setattr(ci_wait, "find_done_row", boom)
    assert _wait().exit_code == 1
    assert labels[-1] == ("wait", f"ci:develop@{SHA}", "done")


def test_the_github_only_wait_is_labelled_with_its_branch(monkeypatch: pytest.MonkeyPatch, labels: list) -> None:
    monkeypatch.setattr(ci_wait, "wait_on_github", lambda *a, **k: ("line", 0))
    result = _wait("--branch", "release/v9")
    assert result.exit_code == 0, result.output
    assert labels == [("wait", f"ci:release/v9@{SHA}", "running"), ("wait", f"ci:release/v9@{SHA}", "done")]


def test_a_wait_that_never_starts_registers_nothing(labels: list) -> None:
    result = CliRunner().invoke(ci_group, ["wait", "not-a-sha"])
    assert result.exit_code == ci_wait.EXIT_UNUSABLE and labels == []


def test_no_identity_means_no_label_and_the_wait_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ci_cmd, "_store", _Store)
    monkeypatch.setattr("nexus.identity.work.caller_identity", lambda: None)
    written: list[str] = []
    monkeypatch.setattr("nexus.identity.work.register_own", lambda *a, **k: written.append("x") or True)
    monkeypatch.setattr(ci_wait, "find_done_row", lambda *a, **k: None)
    assert _wait("--fresh").exit_code == ci_wait.EXIT_TIMEOUT and written == []


def test_a_sigterm_arm_that_raises_never_fails_the_wait(monkeypatch: pytest.MonkeyPatch, labels: list) -> None:
    """Review item 6: ``label.arm_sigterm()`` sat outside the try, so an exception there failed the wait before it ran."""
    def boom(self) -> None:
        raise RuntimeError("signal setup")

    monkeypatch.setattr(Label, "arm_sigterm", boom)
    monkeypatch.setattr(ci_wait, "find_done_row", lambda *a, **k: None)
    assert _wait("--fresh").exit_code == ci_wait.EXIT_TIMEOUT
    assert labels[-1] == ("wait", f"ci:develop@{SHA}", "done"), "and the row still ends"
