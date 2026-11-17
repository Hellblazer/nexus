# SPDX-License-Identifier: AGPL-3.0-or-later
"""The hook's side of the pending bind (RDR-253): when its own registry file never appears it binds nothing and writes
``pending.<claude pid>.<OS start epoch>``. The MCP server's bind from that file is test_pending_bind.py. Tenant-free:
no engine call is made before the wait gives up."""
from __future__ import annotations

from pathlib import Path

from nexus.hooks.identity_bind import bind_at_session_start, launch_name_from_args
from nexus.identity.pending import pending_files, read_pending
from tests.identity.fakes import FakeProcesses

T0 = "2026-11-17T08:00:00Z"


class _Time:
    """A clock that only moves when the code sleeps, so a 5-second wait costs no time."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class _NoEngine:  # noqa: D101
    def __getattr__(self, name: str):
        raise AssertionError(f"the engine must not be called before the registry file exists: {name}")


def test_a_registry_file_that_never_appears_leaves_a_pending_file_and_the_hook_says_so(tmp_path: Path) -> None:
    procs = FakeProcesses(tmp_path / "sessions")
    procs.start(700, T0, registry=False)                            # a running process, no registry file yet
    clock = _Time()
    got = bind_at_session_start("sid-p", "startup", env={"NX_SEAT": "alpha"}, claude_pid=700, procs=procs,
                                client=_NoEngine(), config_dir=tmp_path / "config", clock=clock, sleep=clock.sleep)
    assert got.outcome is None and any("not bound yet" in n for n in got.notices)
    assert clock.now - 1_000.0 >= 4.9, "it waited the full 5 seconds, polling"
    [path] = pending_files(tmp_path / "config" / "identity")
    record = read_pending(path)
    assert (record.session_id, record.source, record.seat, record.os_start, record.claude_pid) == ("sid-p", "startup", "alpha", T0, 700)
    assert path.name.startswith("pending.700.")


def test_launch_name_comes_from_the_claude_command_line_when_the_registry_has_none() -> None:
    assert launch_name_from_args("/x/claude -n marginalia --resume abc") == "marginalia"
    assert launch_name_from_args("claude --name=halcyon") == "halcyon"
    assert launch_name_from_args("claude --resume abc") is None and launch_name_from_args("") is None


def test_a_registry_file_that_is_not_this_processs_is_waited_past_like_an_absent_one(tmp_path: Path) -> None:
    """Claude deletes its registry file only on a clean exit. A crashed earlier owner of the pid leaves one behind: its
    sessionId is not the payload's and its procStart is not this process's OS start time, so its startedAt would be
    wrong. The hook waits past it (5 s, then the pending file) and takes the right one when it appears."""
    from nexus.identity.binder import LaunchFacts, SessionBinder

    procs = FakeProcesses(tmp_path / "sessions")
    procs.start(700, T0, registry=False)
    procs.write_registry(700, 111, "2026-11-10T00:00:00Z", session_id="old-owner-sid")     # stale: other session and start
    clock = _Time()
    binder = SessionBinder(client=_NoEngine(), procs=procs, identity_dir=tmp_path / "identity", clock=clock, sleep=clock.sleep)
    facts = LaunchFacts(source="startup", session_id="sid-new", host=procs.host, claude_pid=700, proc_start=T0)
    assert binder.await_registry(facts) is False and clock.now - 1_000.0 >= 4.9 and facts.started_at is None

    procs.write_registry(700, 222, "2026-11-10T00:00:00Z", session_id="sid-new")           # right session, wrong start
    assert binder.await_registry(facts) is False
    procs.write_registry(700, 333, T0, session_id="sid-new")                               # this process's own file
    assert binder.await_registry(facts) is True and facts.started_at == 333
