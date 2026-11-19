# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""scripts/claude-restart-sessions: the readiness round reports a session's running work (RDR-253 Step 6, nexus-hczdo.1.9).

``--status`` prints each target's registered work that is still running, and ``--ask-ready`` tells each session its own
list. This is a REPORT: the refusal criteria of the restart (a ready reply under max-age, an idle session, no background
task) are pre-declared and do not read it; a test below fails if they start to. A running row of an identity whose holder
is gone is stale, and ``nx identity work list --running`` never lists it (tests/identity/test_work_registry.py), so a
crashed session's work does not read as busy. ``nx`` is replaced by a stand-in that answers in the CLI's own shape.
"""
from __future__ import annotations

import argparse
import importlib.machinery
import importlib.util
import json
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "claude-restart-sessions"


@pytest.fixture(scope="module")
def crs():
    loader = importlib.machinery.SourceFileLoader("claude_restart_sessions_work", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def stand_in(rows: list[dict[str, Any]] | None = None, *, code: int = 0, text: str | None = None):
    calls: list[tuple[str, ...]] = []
    seen_timeouts: list[float] = []

    def nx(*args: str, timeout: float = 60) -> subprocess.CompletedProcess:
        calls.append(args)
        seen_timeouts.append(timeout)
        body = text if text is not None else json.dumps({"identity": "i1", "alive": True, "work": rows or []})
        return subprocess.CompletedProcess(args, code, body if code == 0 else "", body if code else "")

    nx.calls = calls  # type: ignore[attr-defined]
    nx.timeouts = seen_timeouts  # type: ignore[attr-defined]
    return nx


def row(kind: str, ref: str) -> dict[str, Any]:
    return {"kind": kind, "ref": ref, "state": "running", "updated_at": "t", "stale": False}


def test_the_work_is_read_through_the_running_filter_by_session_id(crs, monkeypatch) -> None:
    nx = stand_in([row("worktree", "/w/a"), row("tmux", "sock")])
    monkeypatch.setattr(crs, "_nx_identity", nx)
    assert crs.session_work({"sessionId": "sid-1"}) == ["worktree /w/a", "tmux sock"]
    assert nx.calls == [("work", "list", "--session", "sid-1", "--running", "--json")], "stale rows are the CLI's to drop"
    assert nx.timeouts == [5] == [crs.WORK_TIMEOUT_S], "a hung engine must not stall --status for the default minute"


def test_the_standing_mail_wait_is_not_reported_other_waits_are(crs, monkeypatch) -> None:
    monkeypatch.setattr(crs, "_nx_identity", stand_in([row("wait", "sid-1"), row("wait", "other")]))
    assert crs.session_work({"sessionId": "sid-1"}) == ["wait other"]


@pytest.mark.parametrize(("code", "text", "want"), [
    (1, "Error: unknown_session: no identity has held session sid-1", []),      # no identity: no work
    (1, "Error: the engine is not answering", None),
    (0, "not json", None),
    (0, json.dumps({"identity": "i1"}), None),
])
def test_what_cannot_be_read_is_none_never_an_empty_list(crs, monkeypatch, code, text, want) -> None:
    monkeypatch.setattr(crs, "_nx_identity", stand_in(code=code, text=text))
    assert crs.session_work({"sessionId": "sid-1"}) == want


def test_a_hung_nx_is_none_not_a_stalled_status(crs, monkeypatch) -> None:
    def hung(*a, timeout=60):
        raise subprocess.TimeoutExpired("nx", timeout)

    monkeypatch.setattr(crs, "_nx_identity", hung)
    assert crs.session_work({"sessionId": "sid-1"}) is None


def test_a_missing_nx_is_none(crs, monkeypatch) -> None:
    def boom(*a, **k):
        raise FileNotFoundError("nx")

    monkeypatch.setattr(crs, "_nx_identity", boom)
    assert crs.session_work({"sessionId": "sid-1"}) is None
    assert crs.session_work({}) is None


def test_the_ready_ask_tells_the_session_its_running_work(crs, monkeypatch) -> None:
    monkeypatch.setattr(crs, "session_tasks", lambda s: [])
    monkeypatch.setattr(crs, "_nx_identity", stand_in([row("worktree", "/w/a")]))
    text = crs.ready_prompt({"sessionId": "sid-1", "name": "alpha", "pid": 1}, "/tmp/x.json")
    assert "1 running work row(s)" in text and "worktree /w/a" in text
    assert "outlive a restart" in text and "cuts off" not in text, "a worktree survives the restart"
    monkeypatch.setattr(crs, "_nx_identity", stand_in([]))
    assert "no running work registered" in crs.ready_prompt({"sessionId": "sid-1", "name": "alpha", "pid": 1}, "/tmp/x.json")
    monkeypatch.setattr(crs, "_nx_identity", stand_in(code=1, text="Error: the engine is not answering"))
    assert "could not read the work" in crs.ready_prompt({"sessionId": "sid-1", "name": "alpha", "pid": 1}, "/tmp/x.json")


def test_the_note_says_per_kind_what_a_restart_cuts_off_and_what_it_leaves(crs) -> None:
    """S6: waits, watchers and work-run lab runs are the session's children; a worktree and a tmux server outlive it. The
    readiness reply is the pre-declared criterion, so a note that says 'a restart does not end them' about a lab run would
    steer an agent to answer ready with the run in flight."""
    note = crs.work_note(["labrun pytest-full", "wait other", "watcher w", "worktree /w/a", "tmux sock"])
    cut, kept = note.split("A restart cuts off ")[1].split(" outlive a restart")[0], note.split(" outlive a restart")[1]
    assert "3 of them" in note and all(k in cut for k in ("labrun pytest-full", "wait other", "watcher w"))
    assert "Answer not ready" in cut and "worktree" not in cut.split("Answer not ready")[0]
    assert "2" in note.split("A restart cuts off")[1] and all(k in kept for k in ("worktree /w/a", "tmux sock"))
    only_kept = crs.work_note(["worktree /w/a"])
    assert "cuts off" not in only_kept and "Answer not ready" not in only_kept
    only_cut = crs.work_note(["labrun r"])
    assert "Answer not ready" in only_cut and "outlive" not in only_cut


def test_status_prints_each_targets_running_work_and_the_exit_code_ignores_it(crs, monkeypatch, capsys) -> None:
    now = time.time()
    manifest = {"stamp": "s", "at": now - 60, "targets": [{"sessionId": "sid-1", "path": "/x"}], "excluded": []}
    found = [{"sessionId": "sid-1", "name": "alpha", "pane": "%1", "pid": 1}]
    ready = [{"name": "alpha", "sessionId": "sid-1", "live": "idle", "age": 30.0, "reason": "", "eta": None,
              "state": "ready", "blocker": ""}]
    monkeypatch.setattr(crs, "load_manifest", lambda: (manifest, ""))
    monkeypatch.setattr(crs, "readiness", lambda *a, **k: [dict(r) for r in ready])
    monkeypatch.setattr(crs, "withdraw_ready_asks", lambda *a, **k: 0)
    monkeypatch.setattr(crs, "read_reply", lambda *a, **k: ("none", {}))
    monkeypatch.setattr(crs, "round_problem", lambda *a, **k: "")
    monkeypatch.setattr(crs, "sandbox_pass_check", lambda *a: ("", ""))
    monkeypatch.setattr(crs, "script_sha256", lambda *a: "")
    monkeypatch.setattr(crs, "claude_version", lambda *a: "")
    args = argparse.Namespace(status=True, ready_max_age=300)

    monkeypatch.setattr(crs, "session_work", lambda s: ["worktree /w/a", "tmux sock"])
    assert crs.ready_round(args, found, found) == 0, "a session with running work still counts as ready: the report is not a blocker"
    out = capsys.readouterr().out
    assert "work: worktree /w/a" in out and "work: tmux sock" in out

    monkeypatch.setattr(crs, "session_work", lambda s: [])
    assert crs.ready_round(args, found, found) == 0
    assert "work:" not in capsys.readouterr().out

    monkeypatch.setattr(crs, "session_work", lambda s: None)
    assert crs.ready_round(args, found, found) == 0
    assert "work: could not be read" in capsys.readouterr().out


def test_a_running_row_a_restart_would_cut_off_blocks_ready(crs, monkeypatch) -> None:
    """Sam 2026-11-19 (T2 nexus/sam-ruling-step6-ready-gate-2026-11-19): the ready check keeps the process table AND reads
    the work rows. The gate gets a real manifest and reply, so it reaches ``readiness()``. A running wait, watcher or
    labrun row blocks; a worktree or tmux row (it outlives the session) and an empty list do not; an unreadable list
    blocks as an unreadable task list does. Stale rows never arrive here: ``session_work`` reads ``--running``, which
    leaves them out (tests/identity/test_work_registry.py pins that filter)."""
    now = time.time()
    found = [{"sessionId": "sid-1", "name": "alpha", "pane": "%1", "pid": 1}]
    manifest = {"stamp": "s", "at": now - 120, "targets": [{"sessionId": "sid-1", "path": "/reply", "name": "alpha", "how": "", "marker": ""}],
                "excluded": []}
    monkeypatch.setattr(crs, "load_manifest", lambda: (manifest, ""))
    monkeypatch.setattr(crs, "read_reply", lambda *a, **k: ("ok", {"ready": True, "reason": "", "eta": None, "mtime": now - 30}))
    monkeypatch.setattr(crs, "status", lambda s: "idle")
    monkeypatch.setattr(crs, "session_tasks", lambda s: [])
    monkeypatch.setattr(crs, "LOG", Path("/dev/null"))

    def gate(work):
        monkeypatch.setattr(crs, "session_work", lambda s: work)
        return crs.go_gate(found, force=False, max_age=300, now=now, caller_pane="%9")

    assert gate([]) == 0
    assert gate(["worktree /w", "tmux sock"]) == 0, "a worktree or a tmux server outlives the session"
    for kind in ("wait sid-other", "watcher ci", "labrun r1"):
        assert gate(["worktree /w", kind]) == 3, f"{kind} is cut off by a restart: it blocks ready"
    assert gate(None) == 3, "a work list that cannot be read is not 'no work'"
    rows = crs.readiness(found, manifest, now, 300, "%9")
    assert "registered work could not be read" in rows[0]["blocker"]
    monkeypatch.setattr(crs, "session_work", lambda s: ["labrun r1"])
    assert "1 registered work row(s) still run: labrun r1" in crs.readiness(found, manifest, now, 300, "%9")[0]["blocker"]
    # the caller's own session is exempt, as it is from the task list
    assert crs.go_gate(found, force=False, max_age=300, now=now, caller_pane="%1") == 0
