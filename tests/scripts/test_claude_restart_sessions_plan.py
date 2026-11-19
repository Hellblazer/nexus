# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""claude-restart-sessions after the 2026-11-19 11:04 incident (T2 nexus/incident-sandbox-restart-reached-live-sessions-2026-11-19).

D1: the detached half restarts the sessions the foreground planned (a plan file named on its command line), never
the ones its own HOME's sessions dir happens to hold, and it refuses when the plan is missing or unreadable.
D2: the run restarts a session only where the tmux server it reaches has the session's pane and runs the session's
process in it (a pane's process belongs to exactly one server); the session and window NAMES in the recorded tmux field
are not compared, because a rename, a moved window or a break-pane changes them and proves nothing about the server. A
run that reaches no such server refuses, naming the server it reached and what each session records, and never falls
back to the default server silently.

Round 1: `--ask-ready` and `--status` check the server too; the plan is refreshed from each session's own file after the
slow phase; skipped sessions are counted on the closing lines; a plan is checked, deleted on every exit path and pruned;
`--dry-run` writes nothing to the log.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
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
    monkeypatch.setattr(crs, "alive", lambda _pid: True)  # the fake pids below are not real processes
    monkeypatch.setattr(crs, "SKIPPED_OFF_SERVER", 0)
    setattr_in(monkeypatch, crs, "signal.signal", lambda *_a: None)
    monkeypatch.delenv("TMUX_PANE", raising=False)
    monkeypatch.delenv("TMUX", raising=False)
    return tmp_path


def _session(name: str, pane: str = "%5", tmux: str | None = None, pid: int = 4242, home: Path | None = None) -> dict:
    """A live session dict; with *home* its sessions/<name>.json is written there too (what the plan refresh re-reads)."""
    s = {"sessionId": f"sid-{name}", "name": name, "pane": pane, "pid": pid, "cwd": "/w", "status": "idle",
         "kind": "interactive", "tmux": tmux or f"vibes:@1.{pane}", "argv": "claude",
         "file": (home or Path("/nonexistent")) / f"{name}.json"}
    if home is not None:
        _write_session_file(s)
    return s


def _write_session_file(s: dict, **over) -> None:
    body = {"kind": "interactive", "pid": s["pid"], "sessionId": s["sessionId"], "name": s["name"], "status": s["status"],
            "tmux": s["tmux"], "cwd": s["cwd"], **over}
    Path(s["file"]).write_text(json.dumps(body), encoding="utf-8")


class FakeTmux:
    """One tmux server: its socket and pid, and the panes it has (pane id -> (session:window.pane, pane pid)).

    A pane it does not have answers as real tmux 3.6a does: exit 0 and an empty line, not a failure."""

    def __init__(self, server: tuple[str, int], panes: dict[str, tuple[str, int]]) -> None:
        self.server, self.panes, self.calls = server, panes, []

    def __call__(self, *args: str) -> subprocess.CompletedProcess:
        self.calls.append(list(args))
        if args[:2] == ("display-message", "-p") and "-t" not in args:
            return subprocess.CompletedProcess(args, 0, stdout=f"{self.server[0]}\t{self.server[1]}\n", stderr="")
        if args[:2] == ("display-message", "-p") and args[2] == "-t":
            pane = self.panes.get(args[3])
            if pane is None:
                return subprocess.CompletedProcess(args, 0, stdout="\n", stderr="")
            return subprocess.CompletedProcess(args, 0, stdout=f"{args[3]}\t{pane[1]}\t{pane[0]}\n", stderr="")
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
    server, planned, skipped = crs.load_plan(str(path))
    assert server == DEFAULT_SERVER and skipped == 0
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
    server, planned, _ = crs.load_plan(plan_path)
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


# -- round 1 (1): the proof is the pane id and the process under it, not the names ----------------------------------

def test_a_renamed_session_or_moved_window_does_not_unhost_its_session(crs, monkeypatch) -> None:
    """The record says vibes:@1.%5; the server now calls that pane other:@9.%5. Same pane id, same process tree."""
    _install(crs, monkeypatch, DEFAULT_SERVER, {"%5": ("other:@9.%5", 900)}, parents={4242: 900})
    server, kept, problem = crs.check_server([_session("alpha", tmux="vibes:@1.%5")])
    assert problem == "" and [s["name"] for s in kept] == ["alpha"]


def test_the_skip_message_names_the_pane_as_the_server_calls_it_now(crs, monkeypatch, world) -> None:
    _install(crs, monkeypatch, DEFAULT_SERVER, {"%5": ("other:@9.%5", 900), "%6": ("vibes:@1.%6", 910)},
             parents={4242: 900, 4343: 4000})
    mine = _session("alpha", tmux="vibes:@1.%5")
    stray = _session("stray", pane="%6", tmux="vibes:@1.%6", pid=4343)  # its process is not under pane %6
    _, kept, _ = crs.check_server([mine, stray])
    assert [s["name"] for s in kept] == ["alpha"]
    log = crs.LOG.read_text(encoding="utf-8")
    assert "SKIP stray" in log and "vibes:@1.%6" in log and "does not run in" in log


def test_a_missing_pane_that_tmux_answers_with_an_empty_line_is_refused(crs, monkeypatch) -> None:
    """Real tmux 3.6a answers a pane it lacks with exit 0 and an empty line (FakeTmux does the same)."""
    _install(crs, monkeypatch, DEFAULT_SERVER, {})
    _, kept, problem = crs.check_server([_session("alpha")])
    assert kept == [] and "has no pane %5" in problem


@pytest.fixture
def private_tmux(crs, monkeypatch):
    """A tmux server this test starts on its own `-L <unique>` socket and kills at the end; `crs.tmux_run` is pointed
    at it. The default server and every other socket are never named. Yields a function that runs a tmux command there."""
    if shutil.which("tmux") is None:
        pytest.skip("tmux is not installed")
    name = f"nxrt-{uuid.uuid4().hex[:10]}"
    env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}

    def run(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["tmux", "-L", name, "-f", "/dev/null", *args], capture_output=True, text=True, env=env, check=False)

    monkeypatch.setattr(crs, "tmux_run", run)
    try:
        yield run
    finally:
        sock = run("display-message", "-p", "#{socket_path}").stdout.strip()
        run("kill-server")
        if sock and Path(sock).name == name:  # tmux leaves the dead socket file behind: remove only our own
            Path(sock).unlink(missing_ok=True)


def _pane_facts(run, target: str) -> tuple[str, int]:
    out = run("display-message", "-p", "-t", target, "#{pane_id} #{pane_pid}").stdout.split()
    return out[0], int(out[1])


def _descendant_of(pane_pid: int) -> int:
    for _ in range(50):
        kids = subprocess.run(["pgrep", "-P", str(pane_pid)], capture_output=True, text=True).stdout.split()
        if kids:
            return int(kids[0])
        time.sleep(0.1)
    raise AssertionError("the pane's shell started no child")


def test_on_a_real_private_tmux_a_renamed_session_and_a_broken_out_pane_are_still_kept(crs, private_tmux) -> None:
    run = private_tmux
    assert run("new-session", "-d", "-s", "one", "sh -c 'sleep 600 & wait'").returncode == 0
    assert run("split-window", "-d", "-t", "one", "sleep 600").returncode == 0  # a second pane, so the first can break out
    pane, pane_pid = _pane_facts(run, "one:0.0")
    claude_pid = _descendant_of(pane_pid)
    fmt = "#{session_name}:#{window_id}.#{pane_id}"
    field = run("display-message", "-p", "-t", pane, fmt).stdout.strip()
    sess = {"sessionId": "sid-r", "name": "r", "pane": pane, "pid": claude_pid, "tmux": field}
    assert crs.check_server([sess])[1] == [sess], "before any change"
    assert run("rename-session", "-t", "one", "two").returncode == 0
    assert run("break-pane", "-d", "-s", pane).returncode == 0  # the pane moves to a window of its own: a new window id
    after = run("display-message", "-p", "-t", pane, fmt).stdout.strip()
    assert after.startswith("two:") and after.split(".")[0] != field.split(".")[0], f"the recorded field is stale: {field} vs {after}"
    server, kept, problem = crs.check_server([sess])
    assert problem == "" and kept == [sess] and server is not None


def test_on_a_real_private_tmux_a_pid_not_under_the_pane_is_still_refused(crs, private_tmux) -> None:
    run = private_tmux
    assert run("new-session", "-d", "-s", "one", "sleep 600").returncode == 0
    pane, _ = _pane_facts(run, "one")
    field = run("display-message", "-p", "-t", pane, "#{session_name}:#{window_id}.#{pane_id}").stdout.strip()
    sess = {"sessionId": "sid-x", "name": "x", "pane": pane, "pid": os.getpid(), "tmux": field}  # this test process
    _, kept, problem = crs.check_server([sess])
    assert kept == [] and "does not run in" in problem and field in problem


def test_on_a_real_private_tmux_a_pane_it_lacks_is_refused(crs, private_tmux) -> None:
    run = private_tmux
    assert run("new-session", "-d", "-s", "one", "sleep 600").returncode == 0
    sess = {"sessionId": "sid-x", "name": "x", "pane": "%987", "pid": os.getpid(), "tmux": "one:@1.%987"}
    _, kept, problem = crs.check_server([sess])
    assert kept == [] and "has no pane %987" in problem


# -- round 1 (2): --ask-ready and --status check the server before they type or mail anything -------------------

@pytest.mark.parametrize("flag", ["--ask-ready", "--status"])
def test_ask_ready_and_status_refuse_when_no_session_is_on_the_reached_server(crs, monkeypatch, world, flag, capsys) -> None:
    _install(crs, monkeypatch, DEFAULT_SERVER, {"%5": ("vibes:@5.%5", 900)}, parents={4242: 4000})
    monkeypatch.setattr(crs, "sessions", lambda: [_session("alpha", tmux="sbx:@1.%5", home=world / "sessions")])
    monkeypatch.setattr(crs, "ready_round", lambda *a, **k: pytest.fail("the ready round ran on the wrong server"))
    monkeypatch.setattr(sys, "argv", ["claude-restart-sessions", flag])
    assert crs.main() == crs.EXIT_REFUSED
    out = capsys.readouterr().out
    assert DEFAULT_SERVER[0] in out and "sbx:@1.%5" in out


@pytest.mark.parametrize("flag", ["--ask-ready", "--status"])
def test_ask_ready_and_status_round_only_the_sessions_on_the_reached_server(crs, monkeypatch, world, flag) -> None:
    _install(crs, monkeypatch, DEFAULT_SERVER, {"%5": ("vibes:@1.%5", 900)}, parents={4242: 900, 4343: 4000})
    found = [_session("alpha", home=world / "sessions"), _session("stray", pane="%6", tmux="sbx:@3.%6", pid=4343)]
    monkeypatch.setattr(crs, "sessions", lambda: found)
    seen: list = []
    monkeypatch.setattr(crs, "ready_round", lambda a, listed, kept: seen.append(([s["name"] for s in listed], [s["name"] for s in kept])) or 0)
    monkeypatch.setattr(sys, "argv", ["claude-restart-sessions", flag])
    assert crs.main() == 0
    assert seen == [(["alpha"], ["alpha"])], "the round saw only the session this server hosts"
    assert "SKIP stray" in crs.LOG.read_text(encoding="utf-8")


# -- round 1 (3): the plan is refreshed from each session's own file after the slow phase ---------------------------

def test_the_refresh_takes_the_current_session_id_and_status_from_the_sessions_own_file(crs, world) -> None:
    s = _session("alpha", home=world / "sessions")
    _write_session_file(s, sessionId="sid-new", status="busy", name="alpha2")  # a /clear changed the id
    out = crs.refresh_planned([s])
    assert [(x["sessionId"], x["status"], x["name"]) for x in out] == [("sid-new", "busy", "alpha2")]
    assert "sid-alpha" in crs.LOG.read_text(encoding="utf-8") and "sid-new" in crs.LOG.read_text(encoding="utf-8")


@pytest.mark.parametrize("change", ["pid", "file-gone", "pid-dead", "pane-moved", "not-interactive"])
def test_the_refresh_drops_a_session_that_is_no_longer_the_one_that_was_planned(crs, monkeypatch, world, change) -> None:
    s = _session("alpha", home=world / "sessions")
    keep = _session("beta", pane="%6", pid=4343, home=world / "sessions")
    if change == "pid":
        _write_session_file(s, pid=9999)
    elif change == "file-gone":
        Path(s["file"]).unlink()
    elif change == "pid-dead":
        monkeypatch.setattr(crs, "alive", lambda pid: pid != 4242)
    elif change == "pane-moved":
        _write_session_file(s, tmux="vibes:@1.%9")
    else:
        _write_session_file(s, kind="background")
    assert [x["name"] for x in crs.refresh_planned([s, keep])] == ["beta"]
    assert "SKIP alpha" in crs.LOG.read_text(encoding="utf-8")


def test_the_foreground_plans_the_session_id_the_session_has_after_the_slow_phase(crs, monkeypatch, world) -> None:
    _install(crs, monkeypatch, DEFAULT_SERVER, {"%5": ("vibes:@1.%5", 900)}, parents={4242: 900})
    s = _session("alpha", home=world / "sessions")
    # record_last_run runs after the gates and the install: a /clear there changes the id
    monkeypatch.setattr(crs, "record_last_run", lambda a: _write_session_file(s, sessionId="sid-after-clear"))
    code, ran = _foreground(crs, monkeypatch, world, [s], "--resume")
    assert code == 0
    plan_path = ran[0][3].split("--plan ")[1].split(" ")[0].strip("'")
    assert [x["sessionId"] for x in crs.load_plan(plan_path)[1]] == ["sid-after-clear"]


def test_the_foreground_refuses_when_no_planned_session_survives_the_slow_phase(crs, monkeypatch, world) -> None:
    _install(crs, monkeypatch, DEFAULT_SERVER, {"%5": ("vibes:@1.%5", 900)}, parents={4242: 900})
    s = _session("alpha", home=world / "sessions")
    monkeypatch.setattr(crs, "record_last_run", lambda a: Path(s["file"]).unlink())
    code, ran = _foreground(crs, monkeypatch, world, [s], "--resume")
    assert code == crs.EXIT_REFUSED and ran == []
    assert not crs.PLAN_DIR.exists() or list(crs.PLAN_DIR.iterdir()) == []


# -- round 1 (4): skipped sessions are visible on the closing lines ------------------------------------------------------

def test_the_foreground_final_line_counts_the_sessions_it_skipped(crs, monkeypatch, world, capsys) -> None:
    _install(crs, monkeypatch, DEFAULT_SERVER, {"%5": ("vibes:@1.%5", 900)}, parents={4242: 900, 4343: 4000})
    found = [_session("alpha", home=world / "sessions"), _session("stray", pane="%6", tmux="sbx:@3.%6", pid=4343)]
    assert _foreground(crs, monkeypatch, world, found, "--resume")[0] == 0
    out = capsys.readouterr().out
    assert "started in the background for 1 session(s) (1 skipped: not on this tmux server)" in out


def test_the_foreground_final_line_says_nothing_of_skips_when_none(crs, monkeypatch, world, capsys) -> None:
    _install(crs, monkeypatch, DEFAULT_SERVER, {"%5": ("vibes:@1.%5", 900)}, parents={4242: 900})
    assert _foreground(crs, monkeypatch, world, [_session("alpha", home=world / "sessions")], "--resume")[0] == 0
    assert "skipped" not in capsys.readouterr().out


def test_the_plan_carries_the_skipped_count_and_the_closing_log_line_reports_it(crs, monkeypatch, world) -> None:
    real_roll = crs._roll
    sess = [_session("alpha", home=world / "sessions")]
    plan = crs.write_plan(sess, DEFAULT_SERVER, skipped=2)
    assert crs.load_plan(str(plan))[2] == 2
    monkeypatch.setattr(crs, "check_server", lambda found, quiet=False: (DEFAULT_SERVER, list(found), ""))
    monkeypatch.setattr(crs, "footprint_report", lambda *a, **k: [])
    monkeypatch.setattr(crs, "withdraw_ready_asks", lambda *a, **k: 0)
    monkeypatch.setattr(sys, "argv", ["claude-restart-sessions", "--detached", "--resume", "--no-update", "--force", "--plan", str(plan)])
    seen: list[int] = []

    def roll(*_a, **_k):
        seen.append(crs.SKIPPED_OFF_SERVER)
        return 0

    monkeypatch.setattr(crs, "_roll", roll)
    assert crs.main() == 0 and seen == [2]
    # the closing line of the real roll carries it
    crs.SKIPPED_OFF_SERVER = 2
    assert real_roll(argparse_ns(), [], [], [], {"idle"}, None) == 0
    assert "(2 skipped: not on this tmux server)" in crs.LOG.read_text(encoding="utf-8").splitlines()[-1]


def argparse_ns():
    import argparse
    return argparse.Namespace(fresh=False, nx="", fallback_resume=False, wait=1, handoff_timeout=1, repo="", endpoint="",
                              caller_pane="", only=[], skip=[], keep_dev_channel=False, force=True, no_ready_check=True)


# -- round 1 (5): the plan's lifecycle -------------------------------------------------------------------------------

@pytest.mark.parametrize("how", ["other-server", "none-on-server", "bad-server-field"])
def test_a_detached_half_that_refuses_early_removes_its_plan(crs, monkeypatch, world, how) -> None:
    plan = crs.write_plan([_session("alpha", home=world / "sessions")], OTHER_SERVER if how == "other-server" else DEFAULT_SERVER)
    if how == "none-on-server":
        monkeypatch.setattr(crs, "check_server", lambda found, quiet=False: (DEFAULT_SERVER, [], "none live here"))
    else:
        monkeypatch.setattr(crs, "check_server", lambda found, quiet=False: (DEFAULT_SERVER, list(found), ""))
    if how == "bad-server-field":
        body = json.loads(plan.read_text(encoding="utf-8"))
        body["sessions"][0]["pid"] = "x"
        plan.write_text(json.dumps(body), encoding="utf-8")
    monkeypatch.setattr(crs, "_roll", lambda *a, **k: pytest.fail("rolled"))
    monkeypatch.setattr(sys, "argv", ["claude-restart-sessions", "--detached", "--resume", "--no-update", "--force", "--plan", str(plan)])
    assert crs.main() == crs.EXIT_REFUSED
    assert not plan.exists()


def test_a_detached_half_never_removes_a_file_outside_the_plan_dir(crs, monkeypatch, world) -> None:
    stray = world / "not-a-plan.json"
    stray.write_text("{nope", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["claude-restart-sessions", "--detached", "--resume", "--no-update", "--force", "--plan", str(stray)])
    assert crs.main() == crs.EXIT_REFUSED
    assert stray.exists()


def test_the_foreground_prunes_plans_older_than_a_day_and_nothing_else(crs, monkeypatch, world) -> None:
    crs.PLAN_DIR.mkdir(parents=True)
    old, fresh, other = crs.PLAN_DIR / "plan-old.json", crs.PLAN_DIR / "plan-fresh.json", crs.PLAN_DIR / "notes.txt"
    for f in (old, fresh, other):
        f.write_text("{}", encoding="utf-8")
    long_ago = time.time() - 2 * 86400
    os.utime(old, (long_ago, long_ago))
    os.utime(other, (long_ago, long_ago))
    link = crs.PLAN_DIR / "plan-link.json"
    link.symlink_to(world / "sessions")
    os.utime(link, (long_ago, long_ago), follow_symlinks=False)
    _install(crs, monkeypatch, DEFAULT_SERVER, {"%5": ("vibes:@1.%5", 900)}, parents={4242: 900})
    assert _foreground(crs, monkeypatch, world, [_session("alpha", home=world / "sessions")], "--resume")[0] == 0
    assert not old.exists() and fresh.exists() and other.exists(), "only an old plan-*.json goes"
    assert link.is_symlink() and (world / "sessions").is_dir(), "a symlink is neither followed nor removed"


def _plan_body(**over) -> dict:
    body = {"version": 1, "created": time.time(), "server": {"socket": "s", "pid": 1}, "skipped": 0,
            "sessions": [{"sessionId": "x", "pane": "%1", "pid": 1, "tmux": "a:@1.%1", "file": "f"}]}
    return {**body, **over}


def test_a_plan_over_a_day_old_by_its_created_field_is_refused(crs, world) -> None:
    path = world / "plan.json"
    path.write_text(json.dumps(_plan_body(created=time.time() - 2 * 86400)), encoding="utf-8")
    with pytest.raises(crs.PlanError, match="old"):
        crs.load_plan(str(path))
    path.write_text(json.dumps(_plan_body()), encoding="utf-8")
    assert crs.load_plan(str(path))[0] == ("s", 1)


def test_a_plan_that_is_not_a_regular_file_is_refused(crs, world) -> None:
    real = world / "real.json"
    real.write_text(json.dumps(_plan_body()), encoding="utf-8")
    link = world / "link.json"
    link.symlink_to(real)
    for bad in (link, world):
        with pytest.raises(crs.PlanError, match="regular file"):
            crs.load_plan(str(bad))


def test_a_plan_owned_by_someone_else_is_refused(crs, monkeypatch, world) -> None:
    path = world / "plan.json"
    path.write_text(json.dumps(_plan_body()), encoding="utf-8")
    monkeypatch.setattr(crs, "_my_uid", lambda: os.getuid() + 1)
    with pytest.raises(crs.PlanError, match="owned"):
        crs.load_plan(str(path))


def test_an_oversized_plan_is_refused_unread(crs, world) -> None:
    path = world / "plan.json"
    path.write_text(json.dumps(_plan_body(pad="x" * (crs.PLAN_MAX_BYTES + 1))), encoding="utf-8")
    with pytest.raises(crs.PlanError, match="large"):
        crs.load_plan(str(path))


# -- round 1 (6): a dry run says what it would do and writes nothing to the log ------------------------------------

def test_a_dry_run_writes_nothing_to_the_log_even_when_it_skips_or_refuses(crs, monkeypatch, world, capsys) -> None:
    _install(crs, monkeypatch, DEFAULT_SERVER, {"%5": ("vibes:@1.%5", 900)}, parents={4242: 900, 4343: 4000})
    found = [_session("alpha", home=world / "sessions"), _session("stray", pane="%6", tmux="sbx:@3.%6", pid=4343)]
    code, ran = _foreground(crs, monkeypatch, world, found, "--resume", "--dry-run")
    assert code == 0 and ran == []
    out = capsys.readouterr().out
    assert "SKIP stray" in out and "alpha" in out
    assert not crs.LOG.exists()
    _install(crs, monkeypatch, DEFAULT_SERVER, {})
    code, _ = _foreground(crs, monkeypatch, world, found, "--resume", "--dry-run")
    assert code == crs.EXIT_REFUSED and not crs.LOG.exists()
    assert "refusing to restart anything" in capsys.readouterr().out
