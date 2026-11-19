# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""claude-restart-sessions after the 2026-11-19 11:04 incident (T2 nexus/incident-sandbox-restart-reached-live-sessions-2026-11-19).

D1: the detached half restarts the sessions the foreground planned (a plan file named on its command line), never
the ones its own HOME's sessions dir happens to hold, and it refuses when the plan is missing or unreadable.
D2: the run restarts a session only where the tmux server it reaches is the one that session's tmux field names
(its session, window and pane, and its process running in that pane); a run that reaches no such server refuses,
naming the server it reached and what each session records, and never falls back to the default server silently.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests._module_seam import setattr_in

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "claude-restart-sessions"

DEFAULT_SERVER = ("/tmp/tmux-501/default", 12254)
OTHER_SERVER = ("/tmp/sandbox-tmux/sbx", 777)


@pytest.fixture(scope="module")
def crs():
    loader = importlib.machinery.SourceFileLoader("claude_restart_sessions", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def world(crs, monkeypatch, tmp_path):
    """A scratch ~/.claude, no mailed asks on record, no signal handlers, no real tmux."""
    (tmp_path / "sessions").mkdir()
    monkeypatch.setattr(crs, "SESSIONS", tmp_path / "sessions")
    monkeypatch.setattr(crs, "LOG", tmp_path / "restart.log")
    monkeypatch.setattr(crs, "LAST_RUN", tmp_path / "restart-last.json")
    monkeypatch.setattr(crs, "READY_MANIFEST", tmp_path / "restart-ready.json")
    monkeypatch.setattr(crs, "PLAN_DIR", tmp_path / "plans")
    monkeypatch.setattr(crs, "ASK_MARKERS", tmp_path / "restart-asks")
    monkeypatch.setattr(crs, "SENT_ASKS", {})
    monkeypatch.setattr(crs, "SENT_MARKERS", {})
    monkeypatch.setattr(crs, "MADE_MARKERS", set())
    monkeypatch.setattr(crs, "UNWITHDRAWN", [])
    monkeypatch.setattr(crs, "_prune_handoffs_at_write", lambda: None)
    monkeypatch.setattr(crs, "update_plugins", lambda: None)
    setattr_in(monkeypatch, crs, "signal.signal", lambda *_a: None)
    monkeypatch.delenv("TMUX_PANE", raising=False)
    monkeypatch.delenv("TMUX", raising=False)
    return tmp_path


def _session(name: str, pane: str = "%5", tmux: str | None = None, pid: int = 4242, home: Path | None = None) -> dict:
    return {"sessionId": f"sid-{name}", "name": name, "pane": pane, "pid": pid, "cwd": "/w", "status": "idle",
            "kind": "interactive", "tmux": tmux or f"vibes:@1.{pane}", "argv": "claude",
            "file": (home or Path("/nonexistent")) / f"{name}.json"}


class FakeTmux:
    """One tmux server: its socket and pid, and the panes it has (pane id -> (session:window.pane, pane pid))."""

    def __init__(self, server: tuple[str, int], panes: dict[str, tuple[str, int]]) -> None:
        self.server, self.panes, self.calls = server, panes, []

    def __call__(self, *args: str) -> subprocess.CompletedProcess:
        self.calls.append(list(args))
        if args[:2] == ("display-message", "-p") and "-t" not in args:
            return subprocess.CompletedProcess(args, 0, stdout=f"{self.server[0]}\t{self.server[1]}\n", stderr="")
        if args[:2] == ("display-message", "-p") and args[2] == "-t":
            pane = self.panes.get(args[3])
            if pane is None:
                return subprocess.CompletedProcess(args, 1, stdout="", stderr=f"can't find pane: {args[3]}")
            return subprocess.CompletedProcess(args, 0, stdout=f"{pane[0]}\t{pane[1]}\n", stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")


def _install(crs, monkeypatch, server: tuple[str, int], panes: dict[str, tuple[str, int]],
             parents: dict[int, int] | None = None) -> FakeTmux:
    fake = FakeTmux(server, panes)
    monkeypatch.setattr(crs, "tmux_run", fake)
    table = parents or {}
    monkeypatch.setattr(crs, "parent_pid", lambda pid: table.get(pid, 0))
    return fake


# -- D2: the server the run reaches is the one the sessions name --------------------------------------------------

def test_a_session_on_the_reached_server_is_kept(crs, monkeypatch) -> None:
    _install(crs, monkeypatch, DEFAULT_SERVER, {"%5": ("vibes:@1.%5", 900)}, parents={4242: 901, 901: 900})
    server, kept, problem = crs.check_server([_session("alpha")])
    assert problem == "" and server == DEFAULT_SERVER and [s["name"] for s in kept] == ["alpha"]


def test_a_session_that_is_the_panes_own_process_is_kept(crs, monkeypatch) -> None:
    _install(crs, monkeypatch, DEFAULT_SERVER, {"%5": ("vibes:@1.%5", 4242)})  # `exec claude`: no shell between
    assert crs.check_server([_session("alpha")])[1]


def test_no_tmux_and_a_default_server_that_is_not_the_sessions_server_refuses(crs, monkeypatch) -> None:
    """The incident: TMUX unset, the default server is the operator's, the sessions are another server's."""
    assert "TMUX" not in os.environ
    _install(crs, monkeypatch, DEFAULT_SERVER, {"%5": ("vibes:@5.%5", 900)}, parents={4242: 4000})
    server, kept, problem = crs.check_server([_session("alpha", tmux="sbx:@1.%5")])
    assert kept == [] and problem
    assert DEFAULT_SERVER[0] in problem and str(DEFAULT_SERVER[1]) in problem      # the server it would have used
    assert "sbx:@1.%5" in problem and "alpha" in problem                            # what the session records
    assert "TMUX is not set" in problem                                             # and why it fell to the default


def test_a_pane_the_server_does_not_have_refuses(crs, monkeypatch) -> None:
    _install(crs, monkeypatch, DEFAULT_SERVER, {})
    kept, problem = crs.check_server([_session("alpha")])[1:]
    assert kept == [] and "alpha" in problem and "%5" in problem


def test_a_pane_with_the_right_name_but_another_process_is_not_the_session(crs, monkeypatch) -> None:
    """Pane ids and names repeat across servers; the process running in the pane is what ties it to this server."""
    _install(crs, monkeypatch, DEFAULT_SERVER, {"%5": ("vibes:@1.%5", 900)}, parents={4242: 4000})
    assert crs.check_server([_session("alpha")])[1] == []


def test_an_unreachable_tmux_refuses(crs, monkeypatch) -> None:
    monkeypatch.setattr(crs, "tmux_run", lambda *a: subprocess.CompletedProcess(a, 1, stdout="", stderr="no server running"))
    server, kept, problem = crs.check_server([_session("alpha")])
    assert server is None and kept == [] and "no tmux server answers" in problem


def test_a_session_on_another_server_is_skipped_and_the_rest_go_on(crs, monkeypatch, world) -> None:
    """Skipped, not a refusal of the whole run, when the reached server holds at least one planned session: the run
    never touches a pane it cannot tie to this server, and one stray entry (a session on another tmux server) must not
    block restarting the sessions that are here. A run with no session here at all refuses (the test above)."""
    _install(crs, monkeypatch, DEFAULT_SERVER, {"%5": ("vibes:@1.%5", 900), "%6": ("vibes:@1.%6", 910)},
             parents={4242: 900, 4343: 4000})
    mine = _session("alpha")
    stray = _session("stray", pane="%6", tmux="sbx:@3.%6", pid=4343)
    _, kept, problem = crs.check_server([mine, stray])
    assert problem == "" and [s["name"] for s in kept] == ["alpha"]
    log = crs.LOG.read_text(encoding="utf-8")
    assert "SKIP stray" in log and "sbx:@3.%6" in log and DEFAULT_SERVER[0] in log


# -- D1: the plan ------------------------------------------------------------------------------------------------

def test_a_plan_round_trips_the_sessions_and_the_server(crs, world) -> None:
    sess = [_session("alpha", home=world / "sessions"), _session("beta", pane="%6", home=world / "sessions")]
    path = crs.write_plan(sess, DEFAULT_SERVER)
    assert path.parent == crs.PLAN_DIR and oct(path.stat().st_mode & 0o777) == "0o600"
    server, planned = crs.load_plan(str(path))
    assert server == DEFAULT_SERVER
    assert [(s["sessionId"], s["pane"], s["name"], s["pid"], s["tmux"]) for s in planned] == \
        [(s["sessionId"], s["pane"], s["name"], s["pid"], s["tmux"]) for s in sess]
    assert all(isinstance(s["file"], Path) for s in planned)


@pytest.mark.parametrize("content", [None, "", "{not json", "[1]", "{}", '{"version": 1, "sessions": []}',
                                     '{"version": 1, "server": {"socket": "s", "pid": 1}, "sessions": [{"pane": "%1"}]}',
                                     '{"version": 9, "server": {"socket": "s", "pid": 1}, "sessions": []}'],
                         ids=["missing", "empty", "garbled", "list", "no-fields", "no-sessions", "bad-session", "version"])
def test_a_missing_or_bad_plan_is_refused_to_load(crs, world, content) -> None:
    path = world / "plan.json"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    with pytest.raises(crs.PlanError):
        crs.load_plan(str(path))


def test_no_plan_argument_is_refused_to_load(crs) -> None:
    with pytest.raises(crs.PlanError):
        crs.load_plan("")


def _detached(crs, monkeypatch, *argv: str) -> tuple[int, list[list[str]]]:
    """main() as the detached half, with the roll recorded: its exit code and the session names each roll got."""
    rolled: list[list[str]] = []
    monkeypatch.setattr(crs, "_roll", lambda a, found, *_r, **_k: rolled.append([s["name"] for s in found]) or 0)
    monkeypatch.setattr(crs, "footprint_report", lambda *a, **k: [])
    monkeypatch.setattr(crs, "withdraw_ready_asks", lambda *a, **k: 0)
    monkeypatch.setattr(sys, "argv", ["claude-restart-sessions", "--detached", "--resume", "--no-update", "--force", *argv])
    return crs.main(), rolled


def _trust_server(crs, monkeypatch, server=DEFAULT_SERVER) -> None:
    monkeypatch.setattr(crs, "check_server", lambda found: (server, list(found), ""))


def test_the_detached_half_restarts_exactly_the_planned_sessions(crs, monkeypatch, world) -> None:
    """Its own sessions dir holds a session the foreground never planned; it is left alone."""
    for name in ("alpha", "beta", "intruder"):
        (world / "sessions" / f"{name}.json").write_text(json.dumps(
            {"kind": "interactive", "tmux": "vibes:@1.%5", "pid": os.getpid(), "sessionId": f"sid-{name}", "name": name,
             "status": "idle"}), encoding="utf-8")
    plan = crs.write_plan([_session("alpha", home=world / "sessions"), _session("beta", pane="%6", home=world / "sessions")],
                          DEFAULT_SERVER)
    monkeypatch.setattr(crs, "sessions", lambda: pytest.fail("the detached half enumerated sessions"))
    _trust_server(crs, monkeypatch)
    code, rolled = _detached(crs, monkeypatch, "--plan", str(plan))
    assert code == 0 and rolled == [["alpha", "beta"]]


@pytest.mark.parametrize("how", ["no-argument", "missing", "corrupt"])
def test_the_detached_half_refuses_without_a_readable_plan_and_never_enumerates(crs, monkeypatch, world, how) -> None:
    (world / "sessions" / "alpha.json").write_text(json.dumps(
        {"kind": "interactive", "tmux": "vibes:@1.%5", "pid": os.getpid(), "sessionId": "sid-alpha", "name": "alpha"}),
        encoding="utf-8")
    monkeypatch.setattr(crs, "sessions", lambda: pytest.fail("the detached half enumerated sessions"))
    _trust_server(crs, monkeypatch)
    argv: list[str] = []
    if how == "missing":
        argv = ["--plan", str(world / "absent.json")]
    elif how == "corrupt":
        (world / "bad.json").write_text("{nope", encoding="utf-8")
        argv = ["--plan", str(world / "bad.json")]
    code, rolled = _detached(crs, monkeypatch, *argv)
    assert code == crs.EXIT_REFUSED and rolled == []
    assert "ABORT: " in crs.LOG.read_text(encoding="utf-8")


def test_the_detached_half_refuses_when_it_reaches_another_server_than_the_plan_was_made_on(crs, monkeypatch, world) -> None:
    plan = crs.write_plan([_session("alpha", home=world / "sessions")], OTHER_SERVER)
    _trust_server(crs, monkeypatch, DEFAULT_SERVER)  # the sessions check out on the server it reached, which is not the plan's
    code, rolled = _detached(crs, monkeypatch, "--plan", str(plan))
    assert code == crs.EXIT_REFUSED and rolled == []
    log = crs.LOG.read_text(encoding="utf-8")
    assert OTHER_SERVER[0] in log and DEFAULT_SERVER[0] in log


def test_the_detached_half_refuses_when_no_planned_session_is_on_its_server(crs, monkeypatch, world) -> None:
    plan = crs.write_plan([_session("alpha", home=world / "sessions")], DEFAULT_SERVER)
    monkeypatch.setattr(crs, "check_server", lambda found: (DEFAULT_SERVER, [], "none of the sessions live on it"))
    code, rolled = _detached(crs, monkeypatch, "--plan", str(plan))
    assert code == crs.EXIT_REFUSED and rolled == []


def test_the_detached_half_removes_its_plan_when_done(crs, monkeypatch, world) -> None:
    plan = crs.write_plan([_session("alpha", home=world / "sessions")], DEFAULT_SERVER)
    _trust_server(crs, monkeypatch)
    assert _detached(crs, monkeypatch, "--plan", str(plan))[0] == 0
    assert not plan.exists()


# -- the foreground carries its plan to the detached half --------------------------------------------------------------

def _foreground(crs, monkeypatch, world, found: list[dict], *argv: str) -> tuple[int, list[list[str]]]:
    ran: list[list[str]] = []
    monkeypatch.setattr(crs, "sessions", lambda: found)
    setattr_in(monkeypatch, crs, "subprocess.run", lambda cmd, *_a, **_k: ran.append(cmd) or subprocess.CompletedProcess(cmd, 0))
    monkeypatch.setattr(sys, "argv", ["claude-restart-sessions", "--no-ready-check", "--no-update", *argv])
    return crs.main(), ran


def test_the_foreground_names_its_plan_on_the_detached_command_line(crs, monkeypatch, world) -> None:
    _install(crs, monkeypatch, DEFAULT_SERVER, {"%5": ("vibes:@1.%5", 900)}, parents={4242: 900})
    code, ran = _foreground(crs, monkeypatch, world, [_session("alpha", home=world / "sessions")], "--resume")
    assert code == 0 and ran[0][:3] == ["tmux", "run-shell", "-b"]
    cmd = ran[0][3]
    assert "--detached" in cmd and "--plan" in cmd
    plan_path = cmd.split("--plan ")[1].split(" ")[0].strip("'")
    server, planned = crs.load_plan(plan_path)
    assert server == DEFAULT_SERVER and [s["name"] for s in planned] == ["alpha"]


def test_the_foreground_plans_only_the_sessions_on_its_server(crs, monkeypatch, world) -> None:
    _install(crs, monkeypatch, DEFAULT_SERVER, {"%5": ("vibes:@1.%5", 900)}, parents={4242: 900, 4343: 4000})
    found = [_session("alpha", home=world / "sessions"), _session("stray", pane="%6", tmux="sbx:@3.%6", pid=4343)]
    code, ran = _foreground(crs, monkeypatch, world, found, "--resume")
    assert code == 0
    plan_path = ran[0][3].split("--plan ")[1].split(" ")[0].strip("'")
    assert [s["name"] for s in crs.load_plan(plan_path)[1]] == ["alpha"]


def test_the_foreground_refuses_before_launching_when_the_server_is_not_theirs(crs, monkeypatch, world, capsys) -> None:
    _install(crs, monkeypatch, DEFAULT_SERVER, {"%5": ("vibes:@5.%5", 900)}, parents={4242: 4000})
    code, ran = _foreground(crs, monkeypatch, world, [_session("alpha", tmux="sbx:@1.%5", home=world / "sessions")], "--resume")
    assert code == crs.EXIT_REFUSED and ran == []
    out = capsys.readouterr().out
    assert DEFAULT_SERVER[0] in out and "sbx:@1.%5" in out
    assert not crs.PLAN_DIR.exists() or list(crs.PLAN_DIR.iterdir()) == []


def test_a_dry_run_refuses_the_same_way_and_launches_nothing(crs, monkeypatch, world) -> None:
    _install(crs, monkeypatch, DEFAULT_SERVER, {})
    code, ran = _foreground(crs, monkeypatch, world, [_session("alpha", home=world / "sessions")], "--resume", "--dry-run")
    assert code == crs.EXIT_REFUSED and ran == []


def test_the_removed_sandbox_flag_is_gone(crs, monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["claude-restart-sessions", "--skip-sandbox-check", "habit"])
    with pytest.raises(SystemExit) as exc:
        crs.main()
    assert exc.value.code == 2
