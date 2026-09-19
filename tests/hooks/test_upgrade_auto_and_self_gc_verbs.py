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
  ``HookResult.stdout`` is ``None``; a child's stdout is captured, never
  forwarded. Under the shell form the child inherited fd 1 and anything it
  printed was parsed by Claude Code as the hook's JSON.
"""
from __future__ import annotations

import subprocess
import sys

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


def test_upgrade_auto_spawns_nx_upgrade_auto(spy, monkeypatch):
    calls, _ = spy
    monkeypatch.setattr(upgrade_auto.shutil, "which", lambda _: "/gen/bin/nx")
    result = upgrade_auto.run(None)
    assert calls == [["/gen/bin/nx", "upgrade", "--auto"]]
    assert isinstance(result, HookResult)


def test_upgrade_auto_is_silent_on_success(spy, monkeypatch, capsys):
    calls, state = spy
    state["rc"] = 0
    state["stderr"] = "a warning the shell form sent to /dev/null"
    monkeypatch.setattr(upgrade_auto.shutil, "which", lambda _: "/gen/bin/nx")
    result = upgrade_auto.run(None)
    captured = capsys.readouterr()
    assert result.stdout is None, "never writes to the decision channel"
    assert captured.out == ""
    assert captured.err == "", "2>/dev/null: the child's stderr is swallowed"


def test_upgrade_auto_emits_the_skew_guidance_on_a_nonzero_child(spy, monkeypatch, capsys):
    """The `|| echo ... >&2` half. --auto exits 0 always, so nonzero means skew."""
    _, state = spy
    state["rc"] = 2
    monkeypatch.setattr(upgrade_auto.shutil, "which", lambda _: "/gen/bin/nx")
    result = upgrade_auto.run(None)
    captured = capsys.readouterr()
    assert result.stdout is None
    assert captured.out == ""
    assert "nx self install" in captured.err, (
        "the guidance must name the remedy, as the shell string did"
    )
    assert upgrade_auto.SKEW_GUIDANCE in captured.err


def test_upgrade_auto_emits_the_guidance_when_nx_is_not_on_path(spy, monkeypatch, capsys):
    """`nx` absent was exit 127 under the shell, which fired the same `||`."""
    calls, _ = spy
    monkeypatch.setattr(upgrade_auto.shutil, "which", lambda _: None)
    result = upgrade_auto.run(None)
    captured = capsys.readouterr()
    assert calls == [], "nothing is spawned when there is no nx to spawn"
    assert result.stdout is None
    assert upgrade_auto.SKEW_GUIDANCE in captured.err


def test_upgrade_auto_swallows_a_spawn_failure(monkeypatch, capsys):
    """An OSError from the spawn itself is still a hook that must not fail."""
    monkeypatch.setattr(upgrade_auto.shutil, "which", lambda _: "/gen/bin/nx")

    def _boom(cmd, **kwargs):
        raise OSError("no fork for you")

    monkeypatch.setattr(subprocess, "run", _boom)
    result = upgrade_auto.run(None)
    assert result.stdout is None
    assert upgrade_auto.SKEW_GUIDANCE in capsys.readouterr().err


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
