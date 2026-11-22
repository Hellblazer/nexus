# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-252 Step 4 round 1 (B1, bead nexus-igea9.7): the aura state file is gone after the MCP server's normal stop.

``_sigterm_handler`` ends in ``os._exit(0)``, which skips ``atexit``, so a removal registered only with ``atexit`` never
runs on SIGTERM, the usual way a server stops. The removal belongs to ``_exit_teardown``, which both exit paths share.
The state file is written by the real ``state_writer``; only the process exit (``os._exit``) is faked.
"""
from __future__ import annotations

import threading
from pathlib import Path

import pytest

from nexus.auras import loaded_state as ls
from nexus.auras import session as aura_session
from nexus.mcp import core
from tests._module_seam import setattr_in
from tests.identity.fakes import FakeProcesses

START = "2026-11-17T08:00:00Z"


class _Exit(Exception):
    pass


@pytest.fixture
def written(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A state file written by the real writer, with its atexit registration stood in (the test must not leave one)."""
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path / "cfg"))
    procs = FakeProcesses(tmp_path / "sessions")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(procs.sessions_dir.parent))
    procs.start(4242, START, started_at=4242000)
    monkeypatch.setattr(aura_session, "_OWN_STATE_FILES", {}, raising=False)
    write = aura_session.state_writer("sid-exit", register_exit=lambda *_a, **_k: None)
    write(4242, ls.LoadedState(identity="id-a", loaded_identity="id-a", auras={"x": ls.AuraState(True, "loaded", None)}))
    path = tmp_path / "cfg" / "auras" / "loaded-4242.4242000.json"
    assert path.exists()
    return path


def _stubs(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    order: list[str] = []
    monkeypatch.setattr(core, "_SHUTDOWN_IN_FLIGHT", False)
    monkeypatch.setattr(core, "_flush_search_telemetry_at_exit", lambda: None)
    monkeypatch.setattr(core, "_t1_shutdown", lambda: order.append("t1_shutdown"))

    def _fake_exit(code: int) -> None:
        order.append(f"exit {code}")
        raise _Exit

    setattr_in(monkeypatch, core, "_os._exit", _fake_exit)
    return order


def test_sigterm_removes_the_state_file_before_os_exit(written: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    order = _stubs(monkeypatch)
    with pytest.raises(_Exit):
        core._sigterm_handler(15, None)
    assert order == ["t1_shutdown", "exit 0"]
    assert not written.exists(), "os._exit skips atexit: the teardown itself must remove the file"


def test_the_teardown_removes_the_file_once_and_a_second_run_is_harmless(
    written: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stubs(monkeypatch)
    core._exit_teardown()
    assert not written.exists()
    core._exit_teardown()                                   # atexit after the lifespan: nothing left, nothing raised


def test_the_teardown_leaves_a_successors_file(written: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A server that exits after a ``/mcp`` reconnect must not remove the file its successor wrote."""
    _stubs(monkeypatch)
    state = ls.read_state(written)
    assert state is not None
    ls.write_state(written, ls.LoadedState(**{**state.__dict__, "server_pid": state.server_pid + 1}))
    core._exit_teardown()
    assert written.exists()


def test_a_failing_removal_never_stops_the_t1_teardown(written: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    order = _stubs(monkeypatch)

    def _boom() -> None:
        raise OSError("disk gone")

    monkeypatch.setattr(aura_session, "remove_own_state_files", _boom, raising=False)
    core._exit_teardown()
    assert order == ["t1_shutdown"]


def _blocked_sink(monkeypatch: pytest.MonkeyPatch):
    """A real sink over the real writer whose file write blocks on an event, so a write is in flight when the removal runs."""
    started, release = threading.Event(), threading.Event()
    real_write = ls.write_state

    def _slow(path: Path, state: ls.LoadedState) -> None:
        started.set()
        assert release.wait(10), "the test never released the write"
        real_write(path, state)

    monkeypatch.setattr(ls, "write_state", _slow)
    monkeypatch.setattr(aura_session, "_CLOSE_WAIT_S", 0.05, raising=False)
    sink = aura_session.threaded_sink(aura_session.state_writer("sid-race", register_exit=lambda *_a, **_k: None))
    return sink, started, release


def test_a_write_in_flight_at_removal_does_not_recreate_the_file(
    written: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RDR-252 Step 4 confirmation round (nexus-igea9.7): a write still running on the sink when the teardown removes the
    file used to land afterwards and leave the file behind; a write submitted after the removal did the same."""
    written.unlink()
    sink, started, release = _blocked_sink(monkeypatch)
    state = ls.LoadedState(identity="id-b", loaded_identity="id-b", auras={"x": ls.AuraState(True, "loaded", None)})
    sink(4242, state)
    assert started.wait(10), "the sink write never began"

    aura_session.remove_own_state_files()                    # the write is blocked: nothing on disk yet
    release.set()
    assert sink.wait_idle(10)
    assert not written.exists(), "a write in flight at removal recreated the state file"

    sink(4242, state)                                        # submitted after the removal
    assert sink.wait_idle(10)
    assert not written.exists(), "a write after the removal recreated the state file"


def test_a_queued_write_is_dropped_by_the_removal(written: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    written.unlink()
    sink, started, release = _blocked_sink(monkeypatch)
    sink(4242, ls.LoadedState(identity="id-b", loaded_identity="id-b", auras={}))
    assert started.wait(10)
    sink(4242, ls.LoadedState(identity="id-c", loaded_identity="id-c", auras={}))   # queued behind the blocked one

    aura_session.remove_own_state_files()
    release.set()
    assert sink.wait_idle(10)
    assert not written.exists()
