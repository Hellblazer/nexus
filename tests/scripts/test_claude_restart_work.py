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

    def nx(*args: str) -> subprocess.CompletedProcess:
        calls.append(args)
        body = text if text is not None else json.dumps({"identity": "i1", "alive": True, "work": rows or []})
        return subprocess.CompletedProcess(args, code, body if code == 0 else "", body if code else "")

    nx.calls = calls  # type: ignore[attr-defined]
    return nx


def row(kind: str, ref: str) -> dict[str, Any]:
    return {"kind": kind, "ref": ref, "state": "running", "updated_at": "t", "stale": False}


def test_the_work_is_read_through_the_running_filter_by_session_id(crs, monkeypatch) -> None:
    nx = stand_in([row("worktree", "/w/a"), row("tmux", "sock")])
    monkeypatch.setattr(crs, "_nx_identity", nx)
    assert crs.session_work({"sessionId": "sid-1"}) == ["worktree /w/a", "tmux sock"]
    assert nx.calls == [("work", "list", "--session", "sid-1", "--running", "--json")], "stale rows are the CLI's to drop"


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


def test_a_missing_nx_is_none(crs, monkeypatch) -> None:
    def boom(*a):
        raise FileNotFoundError("nx")

    monkeypatch.setattr(crs, "_nx_identity", boom)
    assert crs.session_work({"sessionId": "sid-1"}) is None
    assert crs.session_work({}) is None


def test_the_ready_ask_tells_the_session_its_running_work(crs, monkeypatch) -> None:
    monkeypatch.setattr(crs, "session_tasks", lambda s: [])
    monkeypatch.setattr(crs, "_nx_identity", stand_in([row("worktree", "/w/a")]))
    text = crs.ready_prompt({"sessionId": "sid-1", "name": "alpha", "pid": 1}, "/tmp/x.json")
    assert "1 running work row(s)" in text and "worktree /w/a" in text
    monkeypatch.setattr(crs, "_nx_identity", stand_in([]))
    assert "no running work registered" in crs.ready_prompt({"sessionId": "sid-1", "name": "alpha", "pid": 1}, "/tmp/x.json")
    monkeypatch.setattr(crs, "_nx_identity", stand_in(code=1, text="Error: the engine is not answering"))
    assert "could not read the work" in crs.ready_prompt({"sessionId": "sid-1", "name": "alpha", "pid": 1}, "/tmp/x.json")


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


def test_the_go_gate_never_reads_the_work(crs, monkeypatch) -> None:
    """The restart's refusal criteria are pre-declared: they must not start to read the work report."""
    def forbidden(*a, **k):
        raise AssertionError("the go gate read the work report")

    monkeypatch.setattr(crs, "session_work", forbidden)
    monkeypatch.setattr(crs, "_nx_identity", forbidden)
    monkeypatch.setattr(crs, "load_manifest", lambda: (None, "no ready round yet"))
    monkeypatch.setattr(crs, "LOG", Path("/dev/null"))
    assert crs.go_gate([{"sessionId": "sid-1", "name": "alpha", "pane": "%1"}], force=False, max_age=300) == 3
