# SPDX-License-Identifier: AGPL-3.0-or-later
"""The two SessionStart entries whose shell logic moved into a verb (RDR-215).

Bead ``nexus-q02nx.22``, Approach item 6. ``hooks.json`` carried these two as
shell command strings::

    nx upgrade --auto 2>/dev/null || echo '<skew guidance>' >&2
    nx self gc >/dev/null 2>&1 || true

Both are now ``nx-hook upgrade-auto`` / ``nx-hook self-gc`` in exec form, which
means the redirects and the ``||`` have no shell to run in any more: the verb
owns them. These tests pin what the shell used to guarantee, because nothing
else does once the string is gone.

The properties under test are the SHELL's, restated:

* ``2>/dev/null`` — the child's stderr never reaches the session.
* ``>/dev/null`` (self-gc) — and neither does its stdout.
* ``|| echo ... >&2`` — a NONZERO child exit prints the skew guidance on
  stderr, and a zero exit does not. ``nx upgrade --auto`` is documented
  "exit 0 always" (``src/nexus/commands/upgrade.py``: ``--auto`` swallows
  every exception and returns), so nonzero means the binary is absent or too
  old to know the flag, which is exactly the skew the guidance addresses.
* ``|| true`` (self-gc) — a nonzero child is swallowed silently, no guidance.
* Neither verb ever writes to the DECISION channel. Both returned
  ``HookResult.stdout`` is ``None``; a child's stdout is never forwarded
  (self-gc captures it; upgrade-auto sends it to the null device). Under the
  shell form the child inherited fd 1 and anything it printed was parsed by
  Claude Code as the hook's JSON.

One property is NOT the shell's (nexus-wozn6): upgrade-auto no longer waits
for its child. ``nx upgrade --auto`` can take about a minute after a version
change, longer than the hook's 30 s timeout, and a cancelled hook most likely
took the child down with it (inferred from the ledger, not observed). The verb
now starts the child in its own session and returns after a short bounded wait.
"""
from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import time

import pytest

from nexus._hook_runtime._io import HookResult
from nexus._hook_runtime.entry import VERB_TABLE
from nexus.hooks import self_gc, upgrade_auto


class _FakeCompleted:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture
def spy(monkeypatch):
    """Capture the argv each verb would spawn, and drive its exit code."""
    calls: list[list[str]] = []
    state = {"rc": 0, "stdout": "", "stderr": ""}

    def _fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        assert kwargs.get("capture_output") is True, (
            "the child's streams must be CAPTURED, not inherited — inheriting "
            "puts them on the hook's decision channel, which is what the "
            "shell form's redirects prevented"
        )
        return _FakeCompleted(state["rc"], state["stdout"], state["stderr"])

    monkeypatch.setattr(subprocess, "run", _fake_run)
    return calls, state


# ── nx-hook upgrade-auto ────────────────────────────────────────────────────
#
# These drive a REAL child: a fake `nx` shell script in tmp_path. The verb now
# spawns detached and waits a bounded time (nexus-wozn6), so what matters is
# real process behaviour -- exit codes, streams, sessions -- not call shape.

@pytest.fixture(autouse=True)
def emitted(monkeypatch):
    """Record the verb's hook.log events instead of configuring structlog in
    the test process (which would also put them on the stderr these tests
    assert is empty)."""
    events: list[tuple[str, str, dict]] = []
    monkeypatch.setattr(
        upgrade_auto, "_emit",
        lambda level, event, **fields: events.append((level, event, fields)),
    )
    return events


_posix_only = pytest.mark.skipif(not hasattr(os, "getsid"), reason="session ids are POSIX")


@pytest.fixture
def fake_nx(tmp_path, monkeypatch):
    """Install a fake `nx` whose body the test supplies. The script records
    its argv and pid first, so a test can find the child afterwards."""
    argv_file = tmp_path / "argv"
    pid_file = tmp_path / "pid"

    def install(body: str):
        script = tmp_path / "nx"
        script.write_text(
            "#!/bin/sh\n"
            f'echo "$@" > "{argv_file}"\n'
            f'echo $$ > "{pid_file}"\n'
            f"{body}\n"
        )
        script.chmod(0o755)
        monkeypatch.setattr(upgrade_auto.shutil, "which", lambda _: str(script))
        return argv_file, pid_file

    return install


def _wait_for(path, seconds=5.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if path.exists() and path.read_text().strip():
            return path.read_text().strip()
        time.sleep(0.02)
    raise AssertionError(f"{path} never written")


def test_upgrade_auto_spawns_nx_upgrade_auto(fake_nx):
    argv_file, _ = fake_nx("exit 0")
    result = upgrade_auto.run(None)
    assert _wait_for(argv_file) == "upgrade --auto"
    assert isinstance(result, HookResult)


def test_upgrade_auto_is_silent_on_success(fake_nx, capfd):
    """2>/dev/null, and stdout never reaches the decision channel: checked at
    the FILE DESCRIPTOR, which is what a real child writes to."""
    fake_nx('echo "on stdout"; echo "a warning the shell form sent to /dev/null" >&2; exit 0')
    result = upgrade_auto.run(None)
    captured = capfd.readouterr()
    assert result.stdout is None, "never writes to the decision channel"
    assert captured.out == ""
    assert captured.err == "", "the child's stderr never reaches the hook's own stderr"
    log = upgrade_auto._child_log_path()
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and not (log.exists() and log.read_text()):
        time.sleep(0.02)
    assert "a warning the shell form sent to /dev/null" in log.read_text(), (
        "the child outlives the hook, so its stderr goes to a log file, not the null device"
    )
    assert "on stdout" not in log.read_text(), "stdout stays on the null device"


def test_upgrade_auto_emits_the_skew_guidance_on_a_nonzero_child(fake_nx, capsys):
    """The `|| echo ... >&2` half. --auto exits 0 always, so nonzero means
    skew; an `nx` too old to know the flag fails in about 0.17 s, well inside
    the bounded wait."""
    fake_nx("exit 2")
    result = upgrade_auto.run(None)
    captured = capsys.readouterr()
    assert result.stdout is None
    assert captured.out == ""
    assert "nx self install" in captured.err, (
        "the guidance must name the remedy, as the shell string did"
    )
    assert upgrade_auto.SKEW_GUIDANCE in captured.err


def test_upgrade_auto_emits_the_guidance_when_nx_is_not_on_path(monkeypatch, capsys):
    """`nx` absent was exit 127 under the shell, which fired the same `||`."""
    spawned: list[object] = []
    monkeypatch.setattr(upgrade_auto.shutil, "which", lambda _: None)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: spawned.append(a))
    result = upgrade_auto.run(None)
    captured = capsys.readouterr()
    assert spawned == [], "nothing is spawned when there is no nx to spawn"
    assert result.stdout is None
    assert upgrade_auto.SKEW_GUIDANCE in captured.err


def test_upgrade_auto_swallows_a_spawn_failure(monkeypatch, capsys, emitted):
    """An OSError from the spawn itself is still a hook that must not fail."""
    monkeypatch.setattr(upgrade_auto.shutil, "which", lambda _: "/gen/bin/nx")

    def _boom(cmd, **kwargs):
        raise OSError("no fork for you")

    monkeypatch.setattr(subprocess, "Popen", _boom)
    result = upgrade_auto.run(None)
    assert result.stdout is None
    assert upgrade_auto.SKEW_GUIDANCE in capsys.readouterr().err
    assert [e[1] for e in emitted] == ["upgrade_auto_spawn_failed"]
    assert emitted[0][2]["error_type"] == "OSError"


@_posix_only
def test_upgrade_auto_returns_while_a_long_upgrade_runs_on_in_its_own_session(
    fake_nx, monkeypatch, capsys, emitted,
):
    """nexus-wozn6. Since 7.68 `nx upgrade --auto` runs the RDR-192 census
    after every package-version change: 51.5 s over 98 collections, against
    this hook's 30 s cap. The old verb waited on the child and Claude Code
    cancelled the hook at 30 s; the census most likely died with it (the
    ledger shows no completion, nobody watched the child). The verb must
    return within its short wait while the child keeps running, in a session
    of its own that a kill aimed at the hook's process tree does not reach."""
    monkeypatch.setattr(upgrade_auto, "_SKEW_WAIT_S", 0.3)
    _, pid_file = fake_nx("exec sleep 4")
    started = time.monotonic()
    result = upgrade_auto.run(None)
    elapsed = time.monotonic() - started
    pid = int(_wait_for(pid_file))
    try:
        assert elapsed < 2.0, f"the hook waited {elapsed:.2f}s on a child that runs 4s"
        os.kill(pid, 0)  # still running after the hook returned
        assert os.getsid(pid) != os.getsid(0), "the child shares the hook's session"
        assert result.stdout is None
        assert capsys.readouterr().err == "", "a still-running child is not skew"
        (level, event, fields), = emitted
        assert (level, event) == ("info", "upgrade_auto_child_spawned")
        assert fields["pid"] == pid
        assert fields["argv"][1:] == ["upgrade", "--auto"]
        assert fields["returncode"] is None, "still running when the hook returned"
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)


def test_upgrade_auto_logs_the_spawn_with_the_childs_pid_and_exit_status(fake_nx, emitted):
    """nexus-wozn6: the detached child runs unattended, so its start is on
    record in hook.log: pid and argv, and the exit status when it exited
    inside the wait."""
    _, pid_file = fake_nx("exit 0")
    upgrade_auto.run(None)
    pid = int(_wait_for(pid_file))
    (level, event, fields), = emitted
    assert (level, event) == ("info", "upgrade_auto_child_spawned")
    assert fields["pid"] == pid
    assert fields["argv"][1:] == ["upgrade", "--auto"]
    assert fields["returncode"] == 0
    assert fields["stderr_log"] == str(upgrade_auto._child_log_path())


def test_child_log_is_rotated_once_it_reaches_the_cap(monkeypatch):
    monkeypatch.setattr(upgrade_auto, "_CHILD_LOG_MAX_BYTES", 100)
    log = upgrade_auto._child_log_path()
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_bytes(b"old" * 50)
    handle, path = upgrade_auto._open_child_log()
    assert handle is not None and path == log
    handle.write(b"new")
    handle.close()
    assert log.read_bytes() == b"new"
    assert log.with_name(log.name + ".1").read_bytes() == b"old" * 50


def test_child_log_under_the_cap_is_appended_not_rotated(monkeypatch):
    monkeypatch.setattr(upgrade_auto, "_CHILD_LOG_MAX_BYTES", 100)
    log = upgrade_auto._child_log_path()
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_bytes(b"old")
    handle, _ = upgrade_auto._open_child_log()
    handle.write(b"new")
    handle.close()
    assert log.read_bytes() == b"oldnew"
    assert not log.with_name(log.name + ".1").exists()


def test_an_unopenable_child_log_falls_back_to_the_null_device(monkeypatch, tmp_path):
    """A log problem must never stop the upgrade: the parent of the log
    directory is a FILE, so mkdir fails, and the child starts on DEVNULL."""
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    monkeypatch.setattr(upgrade_auto, "_child_log_path", lambda: blocker / "logs" / "child.log")
    monkeypatch.setattr(upgrade_auto.shutil, "which", lambda _: "/gen/bin/nx")
    seen: list[dict] = []

    def _popen(argv, **kwargs):
        seen.append(kwargs)
        raise OSError("stop here")

    monkeypatch.setattr(subprocess, "Popen", _popen)
    upgrade_auto.run(None)
    assert seen[0]["stderr"] == subprocess.DEVNULL


# Windows. No Windows host runs these: Popen is monkeypatched and the flags
# are pinned by their literal values, so a wrong constant fails here.
_WIN_NO_WINDOW = 0x08000000
_WIN_NEW_GROUP = 0x00000200
_WIN_BREAKAWAY = 0x01000000
_WIN_DETACHED = 0x00000008


def _windows_popen(monkeypatch, *, refuse_first: int = 0):
    monkeypatch.setattr(upgrade_auto, "_IS_WINDOWS", True)
    seen: list[dict] = []

    def _popen(argv, **kwargs):
        seen.append({"argv": argv, **kwargs})
        if len(seen) <= refuse_first:
            raise PermissionError("breakaway not permitted by the job")
        return object()

    monkeypatch.setattr(subprocess, "Popen", _popen)
    return seen


def test_windows_spawn_hides_the_console_asks_for_breakaway_then_retries_without(monkeypatch):
    """CREATE_NO_WINDOW, not DETACHED_PROCESS: a detached process has no
    console, so the console grandchild ``nx.exe`` starts would allocate a
    visible one. The first attempt asks for breakaway from the job; a job
    that forbids it refuses CreateProcess, and the retry drops only that."""
    seen = _windows_popen(monkeypatch, refuse_first=1)
    upgrade_auto._spawn_detached(["nx", "upgrade", "--auto"], 7)
    base = _WIN_NO_WINDOW | _WIN_NEW_GROUP
    assert [c["creationflags"] for c in seen] == [base | _WIN_BREAKAWAY, base]
    for call in seen:
        assert not call["creationflags"] & _WIN_DETACHED
        assert "start_new_session" not in call
        assert call["stdin"] == call["stdout"] == subprocess.DEVNULL
        assert call["stderr"] == 7, "the stderr handle reaches the child on both attempts"
        assert call["close_fds"] is True


def test_windows_spawn_stops_after_one_attempt_when_breakaway_is_allowed(monkeypatch):
    seen = _windows_popen(monkeypatch)
    upgrade_auto._spawn_detached(["nx"], subprocess.DEVNULL)
    assert len(seen) == 1
    assert seen[0]["creationflags"] == _WIN_NO_WINDOW | _WIN_NEW_GROUP | _WIN_BREAKAWAY


def test_windows_spawn_raises_when_both_attempts_fail(monkeypatch):
    seen = _windows_popen(monkeypatch, refuse_first=2)
    with pytest.raises(PermissionError):
        upgrade_auto._spawn_detached(["nx"])
    assert len(seen) == 2


# ── nx-hook self-gc ─────────────────────────────────────────────────────────


def test_self_gc_spawns_nx_self_gc(spy, monkeypatch):
    calls, _ = spy
    monkeypatch.setattr(self_gc.shutil, "which", lambda _: "/gen/bin/nx")
    result = self_gc.run(None)
    assert calls == [["/gen/bin/nx", "self", "gc"]]
    assert isinstance(result, HookResult)


@pytest.mark.parametrize("rc", [0, 1, 2, 127])
def test_self_gc_is_silent_on_every_exit_code(spy, monkeypatch, capsys, rc):
    """`>/dev/null 2>&1 || true`: nothing reaches the session, ever."""
    _, state = spy
    state["rc"] = rc
    state["stdout"] = "reclaimed 3 generations"
    state["stderr"] = "could not stat gen-20260101"
    monkeypatch.setattr(self_gc.shutil, "which", lambda _: "/gen/bin/nx")
    result = self_gc.run(None)
    captured = capsys.readouterr()
    assert result.stdout is None
    assert captured.out == ""
    assert captured.err == ""


def test_self_gc_is_silent_when_nx_is_not_on_path(spy, monkeypatch, capsys):
    """Unlike upgrade-auto, self-gc has no guidance to emit: it was `|| true`."""
    calls, _ = spy
    monkeypatch.setattr(self_gc.shutil, "which", lambda _: None)
    result = self_gc.run(None)
    captured = capsys.readouterr()
    assert calls == []
    assert result.stdout is None
    assert captured.out == ""
    assert captured.err == ""


def test_self_gc_swallows_a_spawn_failure(monkeypatch, capsys):
    monkeypatch.setattr(self_gc.shutil, "which", lambda _: "/gen/bin/nx")

    def _boom(cmd, **kwargs):
        raise OSError("no fork for you")

    monkeypatch.setattr(subprocess, "run", _boom)
    result = self_gc.run(None)
    captured = capsys.readouterr()
    assert result.stdout is None
    assert captured.out == ""
    assert captured.err == ""


# ── both are registered, and reachable through the real entry point ─────────


@pytest.mark.parametrize("verb", ["upgrade-auto", "self-gc"])
def test_the_verb_is_registered_in_the_dispatch_table(verb):
    assert verb in VERB_TABLE, (
        f"{verb!r} is declared in conexus/hooks/hooks.json; an unregistered "
        "verb exits 2 on every SessionStart for every plugin user"
    )


@pytest.mark.parametrize("verb", ["upgrade-auto", "self-gc"])
def test_the_verb_dispatches_through_nx_hook_without_nx_present(verb, tmp_path):
    """End to end through the real console script, with `nx` removed from PATH.

    Proves the whole path — entry point, VERB_TABLE, import, never_fail — and
    proves it on the skew branch, which is the one that has no `nx` to call and
    so is the only branch safe to run for real in a test process.
    """
    proc = subprocess.run(
        [sys.executable, "-c", "from nexus._hook_runtime.entry import main; main()", verb],
        capture_output=True,
        text=True,
        env={"PATH": str(tmp_path), "HOME": str(tmp_path)},
        stdin=subprocess.DEVNULL,
        timeout=60,
    )
    assert proc.returncode == 0, f"a hook verb must exit 0; stderr={proc.stderr[-400:]}"
    assert proc.stdout == "", "neither verb writes to the decision channel"
