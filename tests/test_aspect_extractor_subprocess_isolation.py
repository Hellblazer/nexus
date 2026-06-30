# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-173 Phase 6 (bead nexus-yobit) — SIGKILL-orphan hardening (RF-8).

``extract_aspects`` runs ``claude -p`` via subprocess with NO process-group
isolation. On a timeout, killing only the direct child can orphan any
grandchildren claude spawned, which keep running and burning API quota with the
row stuck in_progress. The fix: spawn the child in its OWN session/process group
(``start_new_session=True``) and, on ``TimeoutExpired``, kill the WHOLE group
(``os.killpg``) so nothing is orphaned. (This is a quota/cleanliness fix; the
Phase-3 reclaim loop already recovers the stranded row regardless.)

DECISION (the bead's Open Question): SHIP with RDR-173 — small, localized, and a
real quota-burn reduction. Trade-off accepted: with the child in its own group it
no longer dies with a host SIGTERM to the daemon group; it runs to completion or
its own timeout, and the reclaim loop resets the row. No orphaned grandchildren
on the timeout path, which is the common quota-burn case.
"""
from __future__ import annotations

import contextlib
import signal
import subprocess

import pytest

import nexus.aspect_extractor as ax


def test_happy_path_returns_completed_process() -> None:
    """The isolated runner round-trips stdin → stdout and returns a normal
    CompletedProcess (behavioral parity with the old subprocess.run)."""
    cp = ax._run_claude_isolated(
        "hello-stdin", timeout=10,
        _argv=["python", "-c", "import sys; sys.stdout.write(sys.stdin.read())"],
    )
    assert cp.returncode == 0
    assert cp.stdout == "hello-stdin"


def test_timeout_kills_whole_process_group(monkeypatch) -> None:
    """On TimeoutExpired the runner kills the child's PROCESS GROUP (not just the
    direct child), so claude's grandchildren cannot orphan + burn quota."""
    real_killpg = ax.os.killpg
    calls: list[tuple[int, int]] = []

    def _spy(pgid: int, sig: int) -> None:
        calls.append((pgid, sig))
        real_killpg(pgid, sig)

    monkeypatch.setattr(ax.os, "killpg", _spy)

    with pytest.raises(subprocess.TimeoutExpired):
        ax._run_claude_isolated(
            "x", timeout=0.3,
            _argv=["python", "-c", "import time; time.sleep(30)"],
        )
    assert calls, "timeout did not kill the process group"
    assert calls[0][1] == signal.SIGKILL


def test_invoke_once_uses_isolated_runner(monkeypatch) -> None:
    """The single-paper extract path must route through _run_claude_isolated
    (not bare subprocess.run) so the hardening actually applies in production."""
    seen: dict = {}

    def _fake_runner(prompt, timeout, **kw):
        seen["timeout"] = timeout
        return subprocess.CompletedProcess(
            ["claude"], 0, '{"is_paper": false}', "",
        )

    monkeypatch.setattr(ax, "_run_claude_isolated", _fake_runner)
    # Downstream JSON parsing is irrelevant here; we only assert the extract path
    # routes through the isolated runner with the single-paper budget.
    with contextlib.suppress(Exception):
        ax._invoke_once("some prompt")
    assert seen["timeout"] == 180   # single-paper budget, via the isolated runner
