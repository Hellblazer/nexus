# SPDX-License-Identifier: AGPL-3.0-or-later
"""``win_console.send_ctrl_break_via_console`` (RDR-224, nexus-f9bgu.17).

The attach / send / re-attach sequence the CLI uses to reach the supervisor's
hidden console. The Win32 calls are an injected fake, so every branch runs on
macOS and Linux. The recorded order is the assertion: a send that skips
FreeConsole fails on a caller with a console, and one that skips the final
re-attach leaves the CLI without stdout (T2 nexus_rdr/224-research-20, -21).
"""
from __future__ import annotations

import threading

import pytest

from nexus.util import win_console
from nexus.util.win_console import (
    ERROR_ACCESS_DENIED,
    ERROR_INVALID_HANDLE,
    send_ctrl_break_via_console,
)


class _FakeConsoleApi:
    def __init__(
        self,
        *,
        attach_ok: bool = True,
        attach_error: int = 0,
        send_ok: bool = True,
        send_error: int = 0,
        parent_ok: bool = True,
        sessions: dict[int, int] | None = None,
    ) -> None:
        self.calls: list[tuple[str, int | None]] = []
        self._attach = (attach_ok, attach_error)
        self._send = (send_ok, send_error)
        self._parent = (parent_ok, 0 if parent_ok else 6)
        self._sessions = sessions or {}

    def free_console(self) -> bool:
        self.calls.append(("free", None))
        return True

    def attach_console(self, pid: int) -> tuple[bool, int]:
        self.calls.append(("attach", pid))
        return self._attach

    def generate_ctrl_break(self, pid: int) -> tuple[bool, int]:
        self.calls.append(("send", pid))
        return self._send

    def attach_parent_console(self) -> tuple[bool, int]:
        self.calls.append(("parent", None))
        return self._parent

    def session_of(self, pid: int) -> int | None:
        self.calls.append(("session", pid))
        return self._sessions.get(pid)


def test_sequence_is_free_attach_send_reattach_in_that_order() -> None:
    api = _FakeConsoleApi()
    result = send_ctrl_break_via_console(4242, api)
    # The second free is load-bearing: AttachConsole(ATTACH_PARENT_PROCESS) is
    # refused while the caller is still on the target's console.
    assert api.calls == [
        ("free", None), ("attach", 4242), ("send", 4242), ("free", None), ("parent", None),
    ]
    assert result.sent is True
    assert result.refused is False
    assert result.stage == "ok"
    assert result.reattached is True


def test_access_denied_on_attach_is_a_refusal_naming_both_sessions() -> None:
    api = _FakeConsoleApi(
        attach_ok=False,
        attach_error=ERROR_ACCESS_DENIED,
        sessions={4242: 1, 777: 0},
    )
    result = send_ctrl_break_via_console(4242, api, own_pid=777)
    assert result.sent is False
    assert result.refused is True
    assert result.stage == "attach"
    assert result.error == ERROR_ACCESS_DENIED
    assert (result.target_session, result.own_session) == (1, 0)
    # No send was attempted, and the caller's console came back.
    assert ("send", 4242) not in api.calls
    # The caller's console came back BEFORE anything else was asked of it.
    assert api.calls[:3] == [("free", None), ("attach", 4242), ("parent", None)]


def test_target_without_a_console_is_not_a_refusal() -> None:
    # A DETACHED_PROCESS target: AttachConsole fails with ERROR_INVALID_HANDLE.
    api = _FakeConsoleApi(attach_ok=False, attach_error=ERROR_INVALID_HANDLE)
    result = send_ctrl_break_via_console(4242, api)
    assert (result.sent, result.refused, result.stage) == (False, False, "attach")
    assert result.error == ERROR_INVALID_HANDLE
    assert result.target_session is None
    # Sessions are only looked up for a refusal.
    assert not any(name == "session" for name, _ in api.calls)


def test_send_failure_still_reattaches_the_parent_console() -> None:
    api = _FakeConsoleApi(send_ok=False, send_error=87)
    result = send_ctrl_break_via_console(4242, api)
    assert (result.sent, result.stage, result.error) == (False, "generate", 87)
    assert api.calls[-2:] == [("free", None), ("parent", None)]


def test_send_exception_still_reattaches_the_parent_console() -> None:
    class _Boom(_FakeConsoleApi):
        def generate_ctrl_break(self, pid: int) -> tuple[bool, int]:
            raise OSError("boom")

    api = _Boom()
    with pytest.raises(OSError):
        send_ctrl_break_via_console(4242, api)
    assert api.calls[-2:] == [("free", None), ("parent", None)]


def test_no_parent_console_is_reported_not_raised() -> None:
    api = _FakeConsoleApi(parent_ok=False)
    result = send_ctrl_break_via_console(4242, api)
    assert result.sent is True
    assert result.reattached is False


@pytest.mark.parametrize("pid", [0, -5, 2**32])
def test_invalid_pid_touches_no_console(pid: int) -> None:
    api = _FakeConsoleApi()
    result = send_ctrl_break_via_console(pid, api)
    assert (result.sent, result.stage) == (False, "invalid")
    assert api.calls == []


def test_concurrent_sends_do_not_interleave_their_console_steps() -> None:
    """A process has one console at a time: two sends must run whole."""
    order: list[str] = []
    gate = threading.Event()

    class _Slow(_FakeConsoleApi):
        def attach_console(self, pid: int) -> tuple[bool, int]:
            order.append(f"attach{pid}")
            gate.wait(0.2)
            return True, 0

        def attach_parent_console(self) -> tuple[bool, int]:
            order.append("parent")
            return True, 0

    api = _Slow()
    threads = [
        threading.Thread(target=send_ctrl_break_via_console, args=(pid, api))
        for pid in (11, 22)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    # Each attach is followed by its own parent re-attach before the next attach.
    assert order in (
        ["attach11", "parent", "attach22", "parent"],
        ["attach22", "parent", "attach11", "parent"],
    )


def test_real_binding_is_not_built_off_windows() -> None:
    # The module imports cleanly everywhere; only the binding needs kernel32.
    assert win_console.CTRL_BREAK_EVENT == 1
    assert win_console.ATTACH_PARENT_PROCESS == 0xFFFFFFFF
    import sys

    if sys.platform != "win32":
        with pytest.raises((AttributeError, OSError)):
            win_console.ctypes_win_console_api()
