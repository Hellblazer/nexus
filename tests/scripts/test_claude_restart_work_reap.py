# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""scripts/claude-restart-sessions and the work-row lifecycle (nexus-hczdo, Sam 2026-11-22 "i really want all this
nailed"): the work read closes ended rows first (``--reap``), retries a cold ``nx`` once, names why a list could not be
read, and holds that one session back instead of refusing the batch. The 2026-11-21 18:18:25 shape: right after ``--nx``
reinstalled the client, bob's first ``nx identity work list`` missed the 5 s bound and the 12-session batch was refused."""
from __future__ import annotations

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
    loader = importlib.machinery.SourceFileLoader("claude_restart_sessions_work_reap", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def answers(*steps: Any):
    """A stand-in ``_nx_identity``: each call takes the next step, a CompletedProcess body (str), an exit (int, str) or
    an exception to raise."""
    calls: list[tuple[tuple[str, ...], float]] = []
    queue = list(steps)

    def nx(*args: str, timeout: float = 60) -> subprocess.CompletedProcess:
        calls.append((args, timeout))
        step = queue.pop(0)
        if isinstance(step, BaseException):
            raise step
        if isinstance(step, tuple):
            code, text = step
            return subprocess.CompletedProcess(args, code, "", text)
        return subprocess.CompletedProcess(args, 0, step, "")

    nx.calls = calls  # type: ignore[attr-defined]
    return nx


def body(*rows: dict[str, Any], reaped: list[dict[str, str]] | None = None) -> str:
    out: dict[str, Any] = {"identity": "i1", "alive": True, "work": list(rows)}
    if reaped is not None:
        out["reaped"] = reaped
    return json.dumps(out)


def run_row(kind: str, ref: str, stale: bool = False) -> dict[str, Any]:
    return {"kind": kind, "ref": ref, "state": "running", "updated_at": "t", "stale": stale}


@pytest.fixture(autouse=True)
def _engine_answers(crs, monkeypatch):
    monkeypatch.setattr(crs, "ENGINE_UNANSWERING", False)


def test_a_cold_nx_that_misses_the_first_bound_is_read_on_the_retry(crs, monkeypatch) -> None:
    nx = answers(subprocess.TimeoutExpired(["nx"], 5), body(run_row("worktree", "/w")))
    monkeypatch.setattr(crs, "_nx_identity", nx)
    assert crs.session_work({"sessionId": "sid-bob"}, reap=True) == ["worktree /w"]
    assert all("--reap" in args for args, _ in nx.calls)
    assert [t for _, t in nx.calls] == [crs.WORK_TIMEOUT_S, crs.WORK_RETRY_TIMEOUT_S] == [5, 30]
    assert "sid-bob" not in crs.WORK_READ_FAILURES


def test_a_list_that_cannot_be_read_says_why(crs, monkeypatch) -> None:
    cases = [
        (answers(subprocess.TimeoutExpired(["nx"], 5), subprocess.TimeoutExpired(["nx"], 30)), "timed out after 5 s and again after 30 s"),
        (answers((1, "engine said no")), "nx exit 1: engine said no"),
        (answers("not json"), "unreadable JSON"),
        (answers(OSError("no nx")), "nx could not run: OSError"),
    ]
    for nx, why in cases:
        monkeypatch.setattr(crs, "ENGINE_UNANSWERING", False)
        monkeypatch.setattr(crs, "_nx_identity", nx)
        assert crs.session_work({"sessionId": "sid-x"}) is None
        assert why in crs.WORK_READ_FAILURES["sid-x"], why


def test_an_installed_nx_without_reap_is_read_without_it(crs, monkeypatch) -> None:
    nx = answers((2, "Error: No such option: --reap"), body(run_row("labrun", "r1")))
    monkeypatch.setattr(crs, "_nx_identity", nx)
    assert crs.session_work({"sessionId": "sid-1"}, reap=True) == ["labrun r1"]
    assert nx.calls[0][0] == ("work", "list", "--session", "sid-1", "--reap", "--json")
    assert nx.calls[1][0] == ("work", "list", "--session", "sid-1", "--json")


def test_the_rows_the_reap_closed_are_logged_and_not_listed(crs, monkeypatch) -> None:
    seen: list[str] = []
    monkeypatch.setattr(crs, "log", seen.append)
    closed = [{"kind": "labrun", "ref": "h:401:1", "why": "its process is gone", "closed": "done"},
              {"kind": "agent", "ref": "agent:a5", "why": "registered before this session started", "closed": "forgotten"}]
    monkeypatch.setattr(crs, "_nx_identity", answers(body(run_row("worktree", "/w"), reaped=closed)))
    assert crs.session_work({"sessionId": "sid-1", "name": "bob"}, reap=True) == ["worktree /w"]
    assert any("closed labrun h:401:1 (its process is gone; done)" in line for line in seen)
    assert any("closed agent agent:a5" in line for line in seen)


def _round(crs, monkeypatch, names: list[str], unreadable: set[str]) -> list[dict]:
    now = time.time()
    found = [{"sessionId": f"sid-{n}", "name": n, "pane": f"%{i}", "pid": i + 10} for i, n in enumerate(names)]
    manifest = {"stamp": "round-9", "at": now - 120, "excluded": [],
                "targets": [{"sessionId": s["sessionId"], "path": f"/r/{s['name']}", "name": s["name"], "how": "", "marker": ""}
                            for s in found]}
    monkeypatch.setattr(crs, "load_manifest", lambda: (manifest, ""))
    monkeypatch.setattr(crs, "read_reply", lambda *a, **k: ("ok", {"ready": True, "reason": "", "eta": None, "mtime": now - 30}))
    monkeypatch.setattr(crs, "status", lambda s: "idle")
    monkeypatch.setattr(crs, "session_tasks", lambda s: [])
    monkeypatch.setattr(crs, "READY_LOG", Path("/dev/null"))

    def work(s: dict, reap: bool = False) -> list[str] | None:
        reaps.append(reap)
        if s["name"] in unreadable:
            crs.WORK_READ_FAILURES[s["sessionId"]] = "nx timed out after 5 s and again after 30 s"
            return None
        return ["labrun h:401:1 (stale)", "agent agent:a1 (stale)"]

    monkeypatch.setattr(crs, "session_work", work)
    return found


reaps: list[bool] = []


def test_one_unreadable_list_holds_that_session_back_and_the_batch_goes(crs, monkeypatch) -> None:
    seen: list[str] = []
    monkeypatch.setattr(crs, "log", seen.append)
    names = ["folio", "elsinore", "linda", "wren", "tamsin", "colophon", "pyrrho", "halcyon", "marginalia", "marlowe",
             "labrun_79e", "bob"]
    found = _round(crs, monkeypatch, names, {"bob"})
    assert crs.go_gate(found, force=False, max_age=300, caller_pane="%99") == 0
    assert [s["name"] for s in found] == names[:-1], "bob is out of this batch; the other eleven restart"
    held = [line for line in seen if line.startswith("held back bob:")]
    assert held and "timed out after 5 s and again after 30 s" in held[0] and "restart it alone" in held[0]
    assert any("11 session(s) ready and idle; 1 held back" in line for line in seen)


def test_a_batch_of_only_unreadable_sessions_is_refused_and_force_keeps_everyone(crs, monkeypatch) -> None:
    monkeypatch.setattr(crs, "log", lambda line: None)
    found = _round(crs, monkeypatch, ["bob"], {"bob"})
    assert crs.go_gate(found, force=False, max_age=300, caller_pane="%99") == 3
    assert [s["name"] for s in found] == ["bob"]
    found = _round(crs, monkeypatch, ["bob", "linda"], {"bob"})
    assert crs.go_gate(found, force=True, max_age=300, caller_pane="%99") == 0
    assert [s["name"] for s in found] == ["bob", "linda"], "--force restarts every session it names"


def test_stale_rows_alone_neither_block_nor_ask_anyone_to_mark_them(crs, monkeypatch) -> None:
    monkeypatch.setattr(crs, "log", lambda line: None)
    found = _round(crs, monkeypatch, ["bob", "linda"], set())
    assert crs.go_gate(found, force=False, max_age=300, caller_pane="%99") == 0 and len(found) == 2
    note = crs.work_note(["labrun h:401:1 (stale)", "agent agent:a1 (stale)"])
    assert "Mark them" not in note and "need nothing from you" in note


def test_only_the_gate_reaps_and_a_dry_run_or_a_view_only_reads(crs, monkeypatch) -> None:
    """S3 / important 3: --status, --dry-run and the ask are views; only the restart's own gate closes rows."""
    real_session_work = crs.session_work
    monkeypatch.setattr(crs, "log", lambda line: None)
    found = _round(crs, monkeypatch, ["bob"], set())
    reaps.clear()
    crs.go_gate(found, force=False, max_age=300, caller_pane="%99", dry_run=True)
    assert reaps == [False]
    reaps.clear()
    crs.go_gate(found, force=False, max_age=300, caller_pane="%99")
    assert reaps == [True]
    nx = answers(body())
    monkeypatch.setattr(crs, "_nx_identity", nx)
    real_session_work({"sessionId": "sid-1"})
    assert "--reap" not in nx.calls[0][0]


def test_after_one_read_times_out_twice_later_reads_get_the_first_bound_only(crs, monkeypatch) -> None:
    """Important 4: a hung engine cost 35 s a session (seven minutes for twelve); now about one minute."""
    t = subprocess.TimeoutExpired(["nx"], 5)
    nx = answers(t, t, t)
    monkeypatch.setattr(crs, "_nx_identity", nx)
    assert crs.session_work({"sessionId": "sid-a"}) is None
    assert crs.session_work({"sessionId": "sid-b"}) is None
    assert [timeout for _, timeout in nx.calls] == [5, 30, 5]
    assert "no retry" in crs.WORK_READ_FAILURES["sid-b"]


def test_a_reap_error_is_logged_and_the_rows_are_still_read(crs, monkeypatch) -> None:
    seen: list[str] = []
    monkeypatch.setattr(crs, "log", seen.append)
    text = json.dumps({"identity": "i1", "alive": True, "work": [run_row("labrun", "r1")], "reap_error": "RuntimeError: x",
                       "reaped": ["not-a-dict"]})
    monkeypatch.setattr(crs, "_nx_identity", answers(text))
    assert crs.session_work({"sessionId": "sid-1", "name": "bob"}, reap=True) == ["labrun r1"]
    assert any("the reap failed" in line for line in seen)
