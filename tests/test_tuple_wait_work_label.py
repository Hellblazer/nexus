# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx tuple wait`` labels itself as work its identity owns (RDR-253 Step 6, nexus-hczdo.1.9): ``kind=wait`` for the
session id, running while the wait parks, ``done`` when it exits, whatever the reason. A label is never a reason for the
wait to fail or to stop."""
from __future__ import annotations

import sys

import pytest
from click.testing import CliRunner

from nexus.commands import tuple_cmd
from nexus.commands.tuple_cmd import tuple_group
from nexus.tuple_wait import DELIVERED, WaitOutcome
from tests._module_seam import setattr_in


class _Store:
    def close(self) -> None:
        pass


@pytest.fixture(autouse=True)
def _session_bound_to_an_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """``--session s1`` names the session of a bound identity (RDR-253 Phase 2: the wait watches the identity's mailbox)."""
    monkeypatch.setattr("nexus.identity.board.identity_for_session", lambda sid, cd=None: "i1")


@pytest.fixture
def labels(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, str]]:
    """What the wait wrote: ``(kind, ref, state)`` per registration, as the identity the token file named at the start."""
    seen: list[tuple[str, str, str]] = []
    monkeypatch.setattr("nexus.channel_launch._claude_ancestor_argv", lambda: "claude --resume x")
    monkeypatch.setattr(tuple_cmd, "_store", _Store)
    monkeypatch.setattr(tuple_cmd, "_board_entries_reader", lambda _sid: (lambda: []))
    monkeypatch.setattr("nexus.identity.work.caller_identity", lambda: "i1")
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


def test_a_wait_stopped_by_ctrl_c_is_still_marked_done(monkeypatch: pytest.MonkeyPatch, labels: list) -> None:
    def interrupted(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr("nexus.tuple_wait.run_wait", interrupted)
    _wait()
    assert labels == [("wait", "s1", "running"), ("wait", "s1", "done")]


def test_no_identity_means_no_label_and_the_wait_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("nexus.channel_launch._claude_ancestor_argv", lambda: "claude --resume x")
    monkeypatch.setattr(tuple_cmd, "_store", _Store)
    monkeypatch.setattr(tuple_cmd, "_board_entries_reader", lambda _sid: (lambda: []))
    monkeypatch.setattr("nexus.identity.work.caller_identity", lambda: None)
    called: list[str] = []
    monkeypatch.setattr("nexus.tuple_wait.run_wait", lambda *a, **k: called.append("ran") or WaitOutcome(reason=DELIVERED, lines=["x"]))
    assert _wait().exit_code == 0 and called == ["ran"]


def test_a_superseded_wait_leaves_its_successors_row_alone(monkeypatch: pytest.MonkeyPatch, labels: list) -> None:
    """S5/F2: the older wait exits AFTER the newer one registered running; its done must not overwrite that row."""
    from nexus.tuple_wait import SUPERSEDED

    monkeypatch.setattr("nexus.tuple_wait.run_wait", lambda *a, **k: WaitOutcome(reason=SUPERSEDED))
    assert _wait().exit_code == 0
    assert labels == [("wait", "s1", "running")], "no done for a superseded wait"


def test_the_wake_notice_is_printed_before_the_row_is_finished(monkeypatch: pytest.MonkeyPatch, labels: list) -> None:
    """S5: a slow engine must not delay the delivery the wait exists to produce."""
    order: list[str] = []
    real_echo = tuple_cmd.click.echo
    monkeypatch.setattr(tuple_cmd.click, "echo", lambda *a, **k: (order.append("echo"), real_echo(*a, **k))[1])
    monkeypatch.setattr("nexus.identity.work.register_own",
                        lambda kind, ref, state="running", **k: order.append(state) or True)
    monkeypatch.setattr("nexus.tuple_wait.run_wait", lambda *a, **k: WaitOutcome(reason=DELIVERED, lines=["NOTICE"]))
    assert _wait().exit_code == 0
    assert order[0] == "running" and order[-1] == "done" and order.index("echo") < order.index("done"), order


def test_the_end_is_written_as_the_identity_captured_at_the_start(monkeypatch: pytest.MonkeyPatch) -> None:
    """S5: a token that changed (or expired) while the wait parked must not move the end to another identity or lose it."""
    from nexus.identity.work import Label

    written: list[tuple[str, str | None]] = []
    monkeypatch.setattr("nexus.identity.work.register_own",
                        lambda kind, ref, state="running", **k: written.append((state, k.get("identity"))) or True)
    who = iter(["i1", "i2", None])
    monkeypatch.setattr("nexus.identity.work.caller_identity", lambda: next(who))
    label = Label("wait", "s1")
    label.start()
    label.finish()
    label.finish()                                                              # once only
    assert written == [("running", "i1"), ("done", "i1")]


def test_a_label_that_never_started_writes_no_end(monkeypatch: pytest.MonkeyPatch) -> None:
    from nexus.identity.work import Label

    written: list[str] = []
    monkeypatch.setattr("nexus.identity.work.register_own", lambda *a, **k: written.append("x") or True)
    monkeypatch.setattr("nexus.identity.work.caller_identity", lambda: None)
    label = Label("wait", "s1")
    assert label.start() is False
    label.finish()
    assert written == []


def test_sigterm_ends_the_row_then_dies_of_the_term(monkeypatch: pytest.MonkeyPatch) -> None:
    """S5: TaskStop sends TERM; the finally never runs, so the handler writes the end and restores the default."""
    import signal
    import sys

    from nexus.identity.work import Label

    if sys.platform == "win32":
        pytest.skip("POSIX signals")
    written: list[str] = []
    monkeypatch.setattr("nexus.identity.work.register_own", lambda kind, ref, state="running", **k: written.append(state) or True)
    monkeypatch.setattr("nexus.identity.work.caller_identity", lambda: "i1")
    killed: list[int] = []
    setattr_in(monkeypatch, "nexus.identity.work", "os.kill", lambda pid, sig: killed.append(sig))
    before = signal.signal(signal.SIGTERM, signal.SIG_DFL)              # an unhandled TERM, as a plain `nx tuple wait` has
    try:
        label = Label("wait", "s1")
        label.start()
        label.arm_sigterm()
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler) and handler is not before
        handler(int(signal.SIGTERM), None)
        assert written == ["running", "done"] and killed == [int(signal.SIGTERM)]
        assert signal.getsignal(signal.SIGTERM) == signal.SIG_DFL, "restored, so the re-sent TERM kills as it would have"
    finally:
        signal.signal(signal.SIGTERM, before)


def test_the_registrars_engine_calls_carry_a_short_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """S5: a slow engine, not only a down one, must not hold the wait for the client's 10 s."""
    from nexus.identity import work

    seen: list[float] = []

    class Fake:
        def put_work(self, *a, **k):
            return {"identity": "i1", "kind": "wait", "ref": "s1", "state": "running"}

        def close(self) -> None:
            pass

    def from_config(cls, *, timeout: float = 10.0):
        seen.append(timeout)
        return Fake()

    monkeypatch.setattr("nexus.identity.bind_client.IdentityClient.from_config", classmethod(from_config))
    # the suite refuses the real client seam (tests/conftest.py); this test's own fake client stands behind it
    monkeypatch.setattr(work, "_new_client", lambda timeout: work.IdentityClient.from_config(timeout=timeout))
    assert work.register_own("wait", "s1", identity="i1") and work.finish_own("wait", "s1", identity="i1")
    assert seen == [work.LABEL_TIMEOUT_S] * 2 and work.LABEL_TIMEOUT_S <= 2.0


def test_a_normal_finish_puts_the_previous_sigterm_handler_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """The handler is for a TERM that arrives while the wait parks; once the row is ended it must not outlive the wait."""
    import signal
    import sys

    from nexus.identity.work import Label

    if sys.platform == "win32":
        pytest.skip("POSIX signals")
    monkeypatch.setattr("nexus.identity.work.register_own", lambda *a, **k: True)
    monkeypatch.setattr("nexus.identity.work.caller_identity", lambda: "i1")
    before = signal.getsignal(signal.SIGTERM)
    label = Label("wait", "s1")
    label.start()
    label.arm_sigterm()
    assert signal.getsignal(signal.SIGTERM) != before
    label.finish()
    assert signal.getsignal(signal.SIGTERM) == before


def _posix_label(monkeypatch: pytest.MonkeyPatch, written: list[str]):
    import sys

    from nexus.identity.work import Label

    if sys.platform == "win32":
        pytest.skip("POSIX signals")
    monkeypatch.setattr("nexus.identity.work.register_own", lambda kind, ref, state="running", **k: written.append(state) or True)
    monkeypatch.setattr("nexus.identity.work.caller_identity", lambda: "i1")
    label = Label("wait", "s1")
    label.start()
    return label


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals: checked before any signal.SIGHUP is touched")
def test_a_hangup_ends_the_row_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """r1 item 3: a closed terminal sends HUP; a wait that died of it left its row running until the purge."""
    import signal

    written: list[str] = []
    killed: list[int] = []
    setattr_in(monkeypatch, "nexus.identity.work", "os.kill", lambda pid, sig: killed.append(sig))
    before = signal.signal(signal.SIGHUP, signal.SIG_DFL)
    try:
        label = _posix_label(monkeypatch, written)
        label.arm_sigterm()
        signal.getsignal(signal.SIGHUP)(int(signal.SIGHUP), None)
        assert written == ["running", "done"] and killed == [int(signal.SIGHUP)]
    finally:
        signal.signal(signal.SIGHUP, before)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals: checked before any signal.SIGHUP is touched")
def test_an_ignored_hangup_stays_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """``nohup nx tuple wait``: HUP is SIG_IGN, and arming must not make the process killable by it."""
    import signal

    before = signal.signal(signal.SIGHUP, signal.SIG_IGN)
    try:
        label = _posix_label(monkeypatch, [])
        label.arm_sigterm()
        assert signal.getsignal(signal.SIGHUP) == signal.SIG_IGN
        label.finish()
        assert signal.getsignal(signal.SIGHUP) == signal.SIG_IGN
    finally:
        signal.signal(signal.SIGHUP, before)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals: checked before any signal.SIGHUP is touched")
def test_a_previous_python_handler_is_called_not_bypassed(monkeypatch: pytest.MonkeyPatch) -> None:
    import signal

    written: list[str] = []
    calls: list[int] = []
    before = signal.signal(signal.SIGTERM, lambda signum, frame: calls.append(signum))
    try:
        label = _posix_label(monkeypatch, written)
        label.arm_sigterm()
        signal.getsignal(signal.SIGTERM)(int(signal.SIGTERM), None)
        assert written == ["running", "done"] and calls == [int(signal.SIGTERM)], "the row ended, then the old handler ran"
    finally:
        signal.signal(signal.SIGTERM, before)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals: checked before any signal.SIGHUP is touched")
def test_a_handler_python_cannot_restore_falls_back_to_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """``signal.getsignal`` answers None for a handler installed outside Python; ``signal.signal(n, None)`` would raise."""
    import signal

    before = {n: signal.getsignal(n) for n in (signal.SIGTERM, signal.SIGHUP)}
    try:
        label = _posix_label(monkeypatch, [])
        setattr_in(monkeypatch, "nexus.identity.work", "signal.getsignal", lambda n: None)
        label.arm_sigterm()
        label.finish()
        assert signal.getsignal(signal.SIGTERM) == signal.SIG_DFL and signal.getsignal(signal.SIGHUP) == signal.SIG_DFL
    finally:
        for n, handler in before.items():
            signal.signal(n, handler if handler is not None else signal.SIG_DFL)
