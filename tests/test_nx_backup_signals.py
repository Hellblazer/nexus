"""nx backup (nexus-f3yqd.4): a Ctrl-C, a kill or a closed terminal never leaves pg_dump running or a partial
dump on disk, and the verb's signal handling leaves the process as it found it. Split from
tests/test_nx_backup.py (its size cap); the stand-in and verb helpers are imported from there."""

from __future__ import annotations

import contextlib
import os
import shlex
import signal
import subprocess
import threading
from pathlib import Path

import pytest
from click.testing import CliRunner

from nexus.backup import pg_backup
from nexus.backup.pg_backup import run_dump
from nexus.cli import main
from nexus.commands import backup_cmd
from tests._module_seam import setattr_in
from tests.test_nx_backup import _no_siblings, _stub_target, _verb, posix_only

_SLEEPS_AFTER_PARTIAL_OUTPUT = """#!/bin/sh
while [ $# -gt 0 ]; do if [ "$1" = "-f" ]; then out="$2"; fi; shift; done
printf 'half a dump' >> "$out"
echo $$ > {pidfile}
{extra}
exec sleep 60
"""


@posix_only
def test_an_interrupt_kills_pg_dump_and_removes_the_partial_file(tmp_path: Path) -> None:
    dest = tmp_path / "out" / "x.dump"
    pidfile = tmp_path / "pid"
    target = _stub_target(tmp_path, _SLEEPS_AFTER_PARTIAL_OUTPUT.format(pidfile=shlex.quote(str(pidfile)), extra=""))
    calls = {"n": 0}

    def interrupt_once_it_is_running(_done: int) -> None:
        calls["n"] += 1
        if pidfile.exists() and calls["n"] > 3:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_dump(target, dest, on_progress=interrupt_once_it_is_running, poll_s=0.02)
    assert not dest.exists()
    _no_siblings(dest.parent, set())
    with pytest.raises(ProcessLookupError):
        os.kill(int(pidfile.read_text().strip()), 0)  # reaped, not left running behind the user's Ctrl-C


@posix_only
@pytest.mark.parametrize("name", ["TERM", "HUP"])
def test_a_kill_or_a_closed_terminal_cleans_up_like_ctrl_c(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SIGTERM and SIGHUP must not leave pg_dump running and a half dump on disk."""
    dest = tmp_path / "out" / "x.dump"
    pidfile = tmp_path / "pid"
    _verb(monkeypatch, _stub_target(
        tmp_path, _SLEEPS_AFTER_PARTIAL_OUTPUT.format(pidfile=shlex.quote(str(pidfile)), extra=f"kill -{name} $PPID"),
    ))
    result = CliRunner().invoke(main, ["backup", "--to", str(dest)])
    assert result.exit_code != 0
    assert not dest.exists()
    _no_siblings(dest.parent, set())
    with pytest.raises(ProcessLookupError):
        os.kill(int(pidfile.read_text().strip()), 0)


@posix_only
def test_a_kill_that_lands_while_pg_dump_is_being_spawned_still_reaps_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The signal arrives before ``Popen`` has returned: on a fast worker the stand-in above signalled its
    parent that early, the interrupt was raised inside ``Popen``, ``proc`` was never set and pg_dump ran on
    orphaned (develop CI run 42823046479). Here the signal is sent from inside the spawn, every time."""
    dest = tmp_path / "out" / "x.dump"
    pidfile = tmp_path / "pid"
    _verb(monkeypatch, _stub_target(
        tmp_path, _SLEEPS_AFTER_PARTIAL_OUTPUT.format(pidfile=shlex.quote(str(pidfile)), extra=""),
    ))
    real_popen = subprocess.Popen
    spawned: list[int] = []

    def signal_inside_the_spawn(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        proc = real_popen(*args, **kwargs)  # type: ignore[call-overload]
        spawned.append(proc.pid)
        os.kill(os.getpid(), signal.SIGTERM)  # before Popen returns to the code that keeps proc
        return proc

    setattr_in(monkeypatch, pg_backup, "subprocess.Popen", signal_inside_the_spawn)
    try:
        result = CliRunner().invoke(main, ["backup", "--to", str(dest)])
        assert result.exit_code != 0, result.output
        assert spawned, "the stand-in was never spawned"
        assert not dest.exists()
        _no_siblings(dest.parent, set())
        with pytest.raises(ProcessLookupError):
            os.kill(spawned[0], 0)  # killed and reaped, not left running behind the kill
    finally:
        for pid in spawned:  # an orphan from a red run must not outlive the test
            with contextlib.suppress(ProcessLookupError, ChildProcessError):
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)


@posix_only
def test_the_signal_handlers_are_put_back_afterwards() -> None:
    def mine(signum: int, frame: object) -> None: ...

    before = {sig: signal.signal(sig, mine) for sig in (signal.SIGTERM, signal.SIGHUP)}
    try:
        with backup_cmd._signals_as_interrupt():
            assert signal.getsignal(signal.SIGTERM) is not mine and signal.getsignal(signal.SIGHUP) is not mine
        assert signal.getsignal(signal.SIGTERM) is mine and signal.getsignal(signal.SIGHUP) is mine
    finally:
        for sig, handler in before.items():
            signal.signal(sig, handler)


@posix_only
def test_a_handler_installed_from_c_is_replaced_by_the_default_not_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """signal.signal returns None for a handler Python did not install; putting that back is a TypeError."""
    calls: list[tuple[int, object]] = []

    def fake(sig: int, handler: object) -> object:
        calls.append((sig, handler))
        return None

    setattr_in(monkeypatch, backup_cmd, "signal.signal", fake)
    with backup_cmd._signals_as_interrupt():
        pass
    restored = [h for _, h in calls[len(calls) // 2:]]
    assert restored and all(h == signal.SIG_DFL for h in restored)


def test_off_the_main_thread_the_verbs_signal_handling_does_nothing_and_does_not_fail() -> None:
    outcome: list[object] = []

    def body() -> None:
        try:
            with backup_cmd._signals_as_interrupt():
                outcome.append("ran")
        except BaseException as exc:  # noqa: BLE001 - the point is that nothing escapes
            outcome.append(exc)

    t = threading.Thread(target=body)
    t.start()
    t.join()
    assert outcome == ["ran"]
