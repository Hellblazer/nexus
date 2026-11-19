# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""tests/e2e/restart-sessions/sandbox_work.py: the sandbox's private tmux server is registered as its identity's work, and
its cleanup asks ``nx identity may-stop`` before ``kill-server`` (RDR-253 Step 6, nexus-hczdo.1.9). ``nx`` is a stand-in
script on PATH that records its arguments and answers as the real verbs do (tests/identity/test_work_registry.py pins
the real answers)."""
from __future__ import annotations

import importlib.util
import stat
import subprocess
from pathlib import Path

import pytest

SIBLING = Path(__file__).resolve().parents[1] / "e2e" / "restart-sessions" / "sandbox_work.py"


@pytest.fixture(scope="module")
def sw():
    spec = importlib.util.spec_from_file_location("restart_sandbox_work_test", SIBLING)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fake_nx(tmp_path: Path, *, may_stop: tuple[int, str] = (0, "")) -> tuple[str, Path]:
    """A stand-in ``nx`` (the sandbox's ``real_nx``) and the file its calls are logged to."""
    log = tmp_path / "nx.log"
    nx = tmp_path / "bin" / "nx"
    nx.parent.mkdir(parents=True)
    code, text = may_stop
    nx.write_text(f'#!/bin/sh\necho "$*" >> "{log}"\n'
                  f'case "$2" in may-stop) echo \'{text}\' >&2; exit {code};; esac\nexit 0\n')
    nx.chmod(nx.stat().st_mode | stat.S_IXUSR)
    return str(nx), log


class Sandbox:
    sock = "rs-1234"

    def __init__(self, real_nx: str = "") -> None:
        self.real_nx = real_nx
        self.calls: list[tuple[str, ...]] = []
        self.hang = False

    def tmux(self, *args: str, timeout: float | None = None):
        self.calls.append(args)
        if self.hang:
            raise subprocess.TimeoutExpired("tmux", 1)


def lines(log: Path) -> list[str]:
    return log.read_text().splitlines() if log.exists() else []


def test_the_server_is_registered_with_its_socket_as_the_label(sw, tmp_path) -> None:
    nx, log = fake_nx(tmp_path)
    sw.register(nx, "rs-1234")
    assert lines(log) == ["identity work register --kind tmux --ref rs-1234"]


def test_the_server_is_killed_after_the_check_and_marked_done(sw, tmp_path) -> None:
    nx, log = fake_nx(tmp_path)
    sb, notes = Sandbox(nx), []
    sw.kill_server(sb, notes)
    assert sb.calls == [("kill-server",)] and notes == []
    assert lines(log) == ["identity may-stop --kind tmux --ref rs-1234", "identity work done --kind tmux --ref rs-1234"]


def test_a_server_a_live_identity_owns_is_not_killed_and_the_owner_is_named(sw, tmp_path) -> None:
    msg = "Error: tmux rs-1234 is owned by i7 (alpha) (its holder is alive): not stopping it."
    nx, log = fake_nx(tmp_path, may_stop=(3, msg))
    sb, notes = Sandbox(nx), []
    sw.kill_server(sb, notes)
    assert sb.calls == [], "kill-server was not sent"
    assert len(notes) == 1 and "NOT sent" in notes[0] and "i7 (alpha)" in notes[0]
    assert "work done" not in "\n".join(lines(log)), "a server that was not stopped is not done"


def test_the_refusal_is_the_exit_status_not_the_words(sw, tmp_path) -> None:
    """Exit 3 refuses whatever the text says; the old wording at any other status is not a refusal."""
    nx, _log = fake_nx(tmp_path, may_stop=(3, "reworded entirely"))
    sb, notes = Sandbox(nx), []
    sw.kill_server(sb, notes)
    assert sb.calls == [] and "reworded entirely" in notes[0]
    nx, _log = fake_nx(tmp_path / "b", may_stop=(1, "Error: tmux rs-1234 is owned by i7"))
    sb, notes = Sandbox(nx), []
    sw.kill_server(sb, notes)
    assert sb.calls == [("kill-server",)] and notes == []


def test_a_check_that_cannot_run_does_not_leave_the_server_up(sw, tmp_path) -> None:
    """The engine being down is not an owner: the run's credential-bearing server must still go."""
    nx, _log = fake_nx(tmp_path, may_stop=(1, "Error: the engine is not answering (ConnectError)"))
    sb, notes = Sandbox(nx), []
    sw.kill_server(sb, notes)
    assert sb.calls == [("kill-server",)] and notes == []


def test_without_an_nx_nothing_is_asked_and_the_server_is_killed(sw) -> None:
    sb, notes = Sandbox(""), []
    sw.register("", "rs-1234")
    sw.kill_server(sb, notes)
    assert sb.calls == [("kill-server",)] and notes == []


def test_a_kill_that_times_out_is_a_note(sw, tmp_path) -> None:
    nx, _log = fake_nx(tmp_path)
    sb, notes = Sandbox(nx), []
    sb.hang = True
    sw.kill_server(sb, notes)
    assert notes == ["tmux kill-server timed out"]
