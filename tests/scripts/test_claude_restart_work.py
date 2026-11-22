# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""scripts/claude-restart-sessions: the readiness round reports a session's running work (RDR-253 Step 6, nexus-hczdo.1.9).

``--status`` prints each target's registered work that is still running, and ``--ask-ready`` tells each session its own
list. This is a REPORT: the refusal criteria of the restart (a ready reply under max-age, an idle session, no background
task) are pre-declared and do not read it; a test below fails if they start to. A running row of an identity whose holder
is gone is stale, and so is one whose registering process is gone or that nobody wrote for 12 hours; the script reads
every row and marks a stale one, which the report lists and the ready check does not block on (tests/identity/test_work_registry.py
pins the stale rule), so a crashed session's work does not read as busy. ``nx`` is replaced by a stand-in that answers in the CLI's own shape.
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


def test_the_work_is_read_by_session_id_with_stale_rows_kept_and_marked(crs, monkeypatch) -> None:
    """Part 1 (nexus-hczdo): the script no longer asks for ``--running``, which drops a stale row unseen. It reads every
    row, keeps the running ones, and marks a stale one so the report lists it and the ready check does not block on it."""
    done = {**row("labrun", "finished"), "state": "done"}
    nx = stand_in([row("worktree", "/w/a"), row("tmux", "sock"), {**row("agent", "agent:a1"), "stale": True}, done])
    monkeypatch.setattr(crs, "_nx_identity", nx)
    assert crs.session_work({"sessionId": "sid-1"}) == ["worktree /w/a", "tmux sock", "agent agent:a1 (stale)"]
    assert nx.calls == [("work", "list", "--session", "sid-1", "--reap", "--json")]
    assert nx.timeouts == [5] == [crs.WORK_TIMEOUT_S], "a hung engine must not stall --status for the default minute"


def test_the_agent_kind_blocks_ready_unless_it_is_stale(crs) -> None:
    """A dispatched agent runs under the session and ends with it (RDR-253 Step 6 part 1)."""
    assert "agent" in crs.WORK_ENDS_WITH_SESSION
    live, stale = "agent agent:a1", "agent agent:a2 (stale)"
    assert crs.blocking_work([live, stale, "wait ci:develop@abc", "wait ci:develop@old (stale)", "worktree /w"]) == [live, "wait ci:develop@abc"]
    assert crs.blocking_work([stale]) == []


def test_the_note_lists_a_stale_row_apart_and_says_it_does_not_block(crs) -> None:
    note = crs.work_note(["agent agent:a1", "agent agent:a2 (stale)", "worktree /w/a"])
    assert "A restart cuts off 1 of them" in note and "agent agent:a1" in note
    assert "1 stale row(s), listed and not blocking" in note and "agent:a2" in note.split("not blocking")[1]
    only_stale = crs.work_note(["labrun r1 (stale)"])
    assert "cuts off" not in only_stale and "Answer not ready" not in only_stale and "not blocking" in only_stale


def test_the_standing_mail_wait_is_not_reported_other_waits_are(crs, monkeypatch) -> None:
    monkeypatch.setattr(crs, "_nx_identity", stand_in([row("wait", "sid-1"), row("wait", "other")]))
    assert crs.session_work({"sessionId": "sid-1"}) == ["wait other"]


@pytest.mark.parametrize(("code", "text", "want"), [
    (1, "Error: unknown_session: no identity has held session sid-1", []),      # no identity: no work
    (2, "Usage: nx identity [OPTIONS] COMMAND [ARGS]...\nError: No such command 'work'.", []),   # an nx older than Step 6
    (2, "Usage: nx [OPTIONS] COMMAND [ARGS]...\nError: No such command 'identity'.", []),   # older still
    (2, "Error: No such option: --running", None),                              # a usage error is not "no work"
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
    the work rows. The gate gets a real manifest and reply, so it reaches ``readiness()``. A running labrun or watcher
    row blocks (no wait row reaches the gate: the standing one is dropped by ``session_work``, older ones are stale); a worktree or tmux row (it outlives the session) and an empty list do not; an unreadable list
    blocks as an unreadable task list does. A stale row arrives marked and never blocks (``blocking_work``)."""
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
    for kind in ("watcher ci", "labrun r1"):
        assert gate(["worktree /w", kind]) == 3, f"{kind} is cut off by a restart: it blocks ready"
    assert gate(None) == 3, "a work list that cannot be read is not 'no work'"
    rows = crs.readiness(found, manifest, now, 300, "%9")
    assert "registered work could not be read" in rows[0]["blocker"]
    monkeypatch.setattr(crs, "session_work", lambda s: ["labrun r1"])
    assert "1 registered work row(s) still run: labrun r1" in crs.readiness(found, manifest, now, 300, "%9")[0]["blocker"]
    # the caller's own session is exempt, as it is from the task list
    assert crs.go_gate(found, force=False, max_age=300, now=now, caller_pane="%1") == 0


def test_session_work_against_a_real_child_process_serving_list_running(crs, monkeypatch, tmp_path) -> None:
    """End to end through a subprocess: an ``nx`` that serves ``identity work list --session S --running --json`` the way
    the real CLI shapes it, and one that predates the verb (exit 2, "No such command 'identity'", as 8.4.1 answers)."""
    fake = tmp_path / "nx"
    fake.write_text(
        "#!/bin/sh\n"
        'if [ "$1 $2 $3" = "identity work list" ]; then\n'
        "  echo '{\"identity\": \"i1\", \"alive\": true, \"work\": ["
        "{\"kind\": \"wait\", \"ref\": \"sid-1\", \"state\": \"running\", \"updated_at\": \"t\", \"stale\": false}, "
        "{\"kind\": \"labrun\", \"ref\": \"r1\", \"state\": \"running\", \"updated_at\": \"t\", \"stale\": false}]}'\n"
        "  exit 0\nfi\nexit 9\n", encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setattr(crs, "_nx_binary", lambda: str(fake))
    work = crs.session_work({"sessionId": "sid-1"})
    assert work == ["labrun r1"], "the standing wait is left out; the lab run is listed"
    assert crs.blocking_work(work) == ["labrun r1"]
    fake.write_text("#!/bin/sh\necho \"Usage: nx [OPTIONS] COMMAND [ARGS]...\" >&2\n"
                    "echo \"Error: No such command 'identity'. Did you mean 'init'?\" >&2\nexit 2\n", encoding="utf-8")
    assert crs.session_work({"sessionId": "sid-1"}) == [], "an nx older than Step 6 has no registered work"


def _one_target(now: float) -> tuple[list[dict], dict]:
    found = [{"sessionId": "sid-1", "name": "alpha", "pane": "%1", "pid": 1}]
    manifest = {"stamp": "round-7", "at": now - 120,
                "targets": [{"sessionId": "sid-1", "path": "/reply", "name": "alpha", "how": "", "marker": ""}], "excluded": []}
    return found, manifest


def _ready_session(crs, monkeypatch, manifest: dict, now: float) -> None:
    monkeypatch.setattr(crs, "load_manifest", lambda: (manifest, ""))
    monkeypatch.setattr(crs, "read_reply", lambda *a, **k: ("ok", {"ready": True, "reason": "", "eta": None, "mtime": now - 30}))
    monkeypatch.setattr(crs, "status", lambda s: "idle")
    monkeypatch.setattr(crs, "session_tasks", lambda s: [])
    monkeypatch.setattr(crs, "LOG", Path("/dev/null"))


def test_every_gate_decision_leaves_one_jsonl_line_per_session_with_its_blockers_and_stale_rows(crs, monkeypatch, tmp_path) -> None:
    """Review item 7 (the measure, RDR-253 Step 6: ready rounds that answered ready while running work a restart cut off): the
    work list lived only in memory. One line per session per gate decision, in the script's state directory."""
    now = time.time()
    found, manifest = _one_target(now)
    rounds = tmp_path / "state" / "rounds.jsonl"
    monkeypatch.setattr(crs, "READY_LOG", rounds)
    _ready_session(crs, monkeypatch, manifest, now)

    monkeypatch.setattr(crs, "session_work", lambda s: ["agent agent:a1", "labrun r1 (stale)", "worktree /w"])
    assert crs.go_gate(found, force=False, max_age=300, now=now, caller_pane="%9") == 3
    monkeypatch.setattr(crs, "session_work", lambda s: ["labrun r1 (stale)"])
    assert crs.go_gate(found, force=False, max_age=300, now=now, caller_pane="%9") == 0, "a stale row alone does not block"
    lines = [json.loads(x) for x in rounds.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 2
    first, second = lines
    assert first["round"] == "round-7" and first["session"] == "sid-1" and first["name"] == "alpha" and first["ready"] is False
    assert first["blockers"] and "agent:a1" in first["blockers"][0] and first["stale"] == ["labrun r1"]
    assert first["work"] == ["agent agent:a1", "labrun r1 (stale)", "worktree /w"] and isinstance(first["at"], float)
    assert second["ready"] is True and second["blockers"] == [] and second["stale"] == ["labrun r1"]


def test_a_journal_that_cannot_be_written_never_changes_the_verdict(crs, monkeypatch, tmp_path) -> None:
    now = time.time()
    found, manifest = _one_target(now)
    blocked = tmp_path / "file"
    blocked.write_text("x", encoding="utf-8")
    monkeypatch.setattr(crs, "READY_LOG", blocked / "rounds.jsonl")             # a path under a regular file
    _ready_session(crs, monkeypatch, manifest, now)
    monkeypatch.setattr(crs, "session_work", lambda s: [])
    assert crs.go_gate(found, force=False, max_age=300, now=now, caller_pane="%9") == 0


def test_a_dry_run_and_the_second_installed_call_write_no_journal_lines(crs, monkeypatch, tmp_path) -> None:
    """Review r2: ``go_gate`` is called again after ``--nx`` installed (``installed=True``) and with ``dry_run=True`` by
    previews; each wrote a set of lines, so one restart round was journalled two or three times. One set per round."""
    now = time.time()
    found, manifest = _one_target(now)
    rounds = tmp_path / "rounds.jsonl"
    monkeypatch.setattr(crs, "READY_LOG", rounds)
    _ready_session(crs, monkeypatch, manifest, now)
    monkeypatch.setattr(crs, "session_work", lambda s: ["agent agent:a1"])
    crs.go_gate(found, force=False, max_age=300, now=now, caller_pane="%9", dry_run=True)
    assert not rounds.exists(), "a dry run decides nothing"
    crs.go_gate(found, force=False, max_age=300, now=now, caller_pane="%9")
    crs.go_gate(found, force=False, max_age=300, now=now, caller_pane="%9", installed=True)
    assert len(rounds.read_text(encoding="utf-8").splitlines()) == 1, "the second call, after --nx installed, adds none"
