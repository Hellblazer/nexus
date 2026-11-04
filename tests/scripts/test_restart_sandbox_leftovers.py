# SPDX-License-Identifier: AGPL-3.0-or-later
"""What a restart sandbox scenario leaves outside its scratch HOME (nexus-xwijj Phase 2).

The SessionEnd launcher's detached grandchild keeps the session's working directory (the
sandbox work folder) but names nothing of the HOME in its argv; a cwd scan finds it without
reading its environment, which holds the token, and ends it only as an orphan. The footprint
check fails a scenario for what it left in shared places, counting only entries that name the
scenario, and never reads as clean when it could not look.
"""
from __future__ import annotations

import ast
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from tests._module_seam import setattr_in

RUN_PY = Path(__file__).resolve().parents[1] / "e2e" / "restart-sessions" / "run.py"
SID = "1234abcd-0000-0000-0000-000000000000"


@pytest.fixture(scope="module")
def run():
    spec = importlib.util.spec_from_file_location("restart_sandbox_run_leftovers", RUN_PY)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # its dataclasses look their module up there
    spec.loader.exec_module(module)
    return module


def _sandbox(run, home: Path, scenario: str = "gate"):
    return run.Sandbox(home, "nx-rs-test-fp", "claude", "sonnet", run.SPECS[scenario])


def _fake_nx(tmp_path: Path, stats: str = '{"available": 0, "claimed": 0, "lapsed": 0, "dead": 0}',
             stats_rc: int = 0, directory: str = "", directory_rc: int = 0) -> str:
    nx = tmp_path / "fake-nx"
    nx.write_text("#!/bin/sh\n"
                  f'if [ "$2" = "stats" ]; then echo \'{stats}\'; exit {stats_rc}; fi\n'
                  f'if [ "$2" = "directory" ]; then\n  case "$3" in\n{directory}'
                  f"  *) echo \"No live directory entry for '$3'.\" ;;\n  esac\n  exit {directory_rc}\nfi\n"
                  "exit 2\n", encoding="utf-8")
    nx.chmod(0o755)
    return str(nx)


@pytest.fixture
def places(run, tmp_path, monkeypatch):
    """Shared places under tmp_path: a temp folder, a Claude temp root and an operator home."""
    temp, claude_root, op = tmp_path / "shared-tmp", tmp_path / "claude-tmp", tmp_path / "operator"
    temp.mkdir()
    claude_root.mkdir()
    (op / ".claude" / "sessions").mkdir(parents=True)
    (op / ".config" / "nexus").mkdir(parents=True)
    (op / ".claude" / "restart-sessions.log").write_text("old line 1234abcd\n", encoding="utf-8")
    monkeypatch.setattr(run, "TEMP_DIRS", (temp,))
    monkeypatch.setattr(run, "claude_temp_roots", lambda: [claude_root])
    monkeypatch.setattr(run, "OPERATOR_HOME", op)
    return temp, claude_root, op


# -- the cwd sweep ---------------------------------------------------------------------------

def test_parse_cwds(run) -> None:
    out = "p10\nfcwd\nn/a/b\np11\nfcwd\nn/c d\npx\nnjunk\n"
    assert run.parse_cwds(out) == {10: "/a/b", 11: "/c d"}


def test_under_matches_the_home_and_its_realpath(run, tmp_path) -> None:
    assert run.under(str(tmp_path / "work"), tmp_path)
    assert run.under(str(tmp_path), tmp_path)
    assert not run.under(str(tmp_path) + "-other/x", tmp_path)
    real = Path(tempfile.gettempdir()).resolve()
    assert run.under(str(real / "x"), Path(tempfile.gettempdir()))


def _orphan_in(cwd: Path, tmp_path: Path, seconds: int) -> int:
    """Start `sleep <seconds>` in *cwd* as an orphan (its parent exits at once); return its pid."""
    pidfile = tmp_path / f"orphan-{seconds}.pid"
    subprocess.run([sys.executable, "-c",
                    "import subprocess\n"
                    f"p = subprocess.Popen(['sleep', '{seconds}'], cwd={str(cwd)!r}, start_new_session=True)\n"
                    f"open({str(pidfile)!r}, 'w').write(str(p.pid))\n"], check=True)
    pid = int(pidfile.read_text(encoding="utf-8"))
    for _ in range(50):  # wait for the reparent
        if subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True).stdout.strip() == "1":
            return pid
        time.sleep(0.1)
    return pid


def _gone(pid: int) -> bool:
    for _ in range(80):
        st = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
        if not st or st.startswith("Z"):
            return True
        time.sleep(0.1)
    return False


def test_an_orphan_working_in_the_home_is_found_and_a_parented_process_is_not(run, tmp_path) -> None:
    """The orphan's argv names nothing of the HOME; a process with a live parent (an operator's shell
    in the work folder) is never selected."""
    work = tmp_path / "work"
    work.mkdir()
    orphan = _orphan_in(work, tmp_path, 317)
    child = subprocess.Popen(["sleep", "318"], cwd=work)
    try:
        found, err = run._sandbox_procs(tmp_path)
        assert err == ""
        assert (orphan, "sleep 317") in found
        assert all(p != child.pid for p, _ in found)
        assert all(p != orphan for p, _ in run._naming(tmp_path))  # the argv sweep alone misses it
    finally:
        child.kill()
        child.wait()
        try:
            os.kill(orphan, 9)
        except ProcessLookupError:
            pass


def test_the_sweep_says_so_when_it_cannot_read_the_table(run, tmp_path, monkeypatch) -> None:
    """A read that misses the harness itself would miss a leftover too: it is not a clean read."""
    setattr_in(monkeypatch, run, "os.getpid", lambda: 2**22 + 7)
    with pytest.raises(run.SweepUnavailable):
        run._cwds()
    _, err = run._sandbox_procs(tmp_path / "absent-home")
    assert "lacks this harness" in err


class _Watch:
    def __init__(self, procs=()) -> None:
        self.procs = list(procs)

    def poll(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def seen(self):
        return self.procs or [(2, "x")]

    def still_running(self):
        return []


def _cleanup_sandbox(run, home: Path):
    sb = _sandbox(run, home)
    sb.nexus_config.mkdir(parents=True)
    sb.watch = _Watch()
    sb.before = {"rs_alpha": {"sessionId": SID}}
    sb.tmux = lambda *a, **k: subprocess.CompletedProcess(a, 1, "", "")
    return sb


def test_cleanup_ends_an_orphan_working_in_the_home(run, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(run, "LINGER_S", 0)  # do not wait for it to end by itself
    home = tmp_path / "home"
    (home / "work").mkdir(parents=True)
    orphan = _orphan_in(home / "work", tmp_path, 319)
    try:
        dirty = run.cleanup(_cleanup_sandbox(run, home), notes := [])
        assert _gone(orphan)
        assert dirty == []
        assert any("1 more still naming or working in the HOME" in n for n in notes)
    finally:
        try:
            os.kill(orphan, 9)
        except ProcessLookupError:
            pass


def test_cleanup_is_dirty_when_the_sweep_cannot_run(run, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(run, "LINGER_S", 0)
    sb = _cleanup_sandbox(run, tmp_path / "home")
    monkeypatch.setattr(run, "_working_in", lambda _home: (_ for _ in ()).throw(run.SweepUnavailable("no lsof")))
    assert run.cleanup(sb, []) == ["the cwd sweep could not run: no lsof"]


def test_cleanup_removes_this_run_s_claude_temp_folders(run, tmp_path, monkeypatch) -> None:
    root = tmp_path / "claude-tmp"
    home = tmp_path / "nx-restart-sandbox-roll-a_b1"
    mine = root / f"-private{run._slug(str(home / 'work'))}"
    other = root / "-Users-someone-git-nexus"
    for d in (mine / "sid" / "tasks", other):
        d.mkdir(parents=True)
    monkeypatch.setattr(run, "claude_temp_roots", lambda: [root])
    assert run.claude_temp_dirs(home) == [mine]
    assert run.cleanup(_cleanup_sandbox(run, home), []) == []
    assert not mine.exists() and other.exists()


def test_cleanup_removes_the_tmux_socket(run, tmp_path) -> None:
    sb = _cleanup_sandbox(run, tmp_path / "home")
    sock_dir = Path(tempfile.mkdtemp(prefix="rs", dir="/tmp"))  # a socket path must fit in ~104 bytes
    try:
        sb.env["TMUX_TMPDIR"] = str(sock_dir)
        sb.socket_path().parent.mkdir(parents=True)
        srv = socket.socket(socket.AF_UNIX)
        srv.bind(str(sb.socket_path()))
        srv.close()  # a socket file nobody serves, as kill-server leaves it
        assert sb.socket_path().is_socket()
        assert run.cleanup(sb, []) == []
        assert not sb.socket_path().exists()
    finally:
        shutil.rmtree(sock_dir, ignore_errors=True)


# -- the footprint ---------------------------------------------------------------------------

def test_footprint_counts_only_entries_naming_the_scenario(run, tmp_path, places) -> None:
    temp, claude_root, op = places
    sb = _sandbox(run, tmp_path / "nx-restart-sandbox-gate-a_c")  # mkdtemp names can hold "_"; the slug has "-"
    before = run.footprint_before()
    (temp / "rh-1234abcd.ask").write_text("x", encoding="utf-8")
    (temp / "someone-elses-file").write_text("x", encoding="utf-8")
    (claude_root / f"-x{run._slug(sb.home.name)}-work").mkdir()
    (claude_root / "-Users-someone-git-nexus").mkdir()
    sessions = op / ".claude" / "sessions"
    (sessions / "9.json").write_text(json.dumps({"name": "rs_alpha", "sessionId": SID}), encoding="utf-8")
    (sessions / "10.json").write_text(json.dumps({"name": "rs_alpha", "sessionId": "ffff"}), encoding="utf-8")
    with (op / ".claude" / "restart-sessions.log").open("a", encoding="utf-8") as fh:
        fh.write("asked rs_alpha 1234abcd\nasked workers_x rs_alpha ffff0000\n")
    (op / ".config" / "nexus" / "t1_session_lease.1234abcd-0000").write_text("x", encoding="utf-8")
    (op / ".config" / "nexus" / "t1_session_lease.ffffffff").write_text("x", encoding="utf-8")
    left = run.footprint_after(before, sb, {SID}, _fake_nx(tmp_path))
    assert left == {f"temp {temp}": ["rh-1234abcd.ask"],
                    f"claude temp {claude_root}": [f"-x{run._slug(sb.home.name)}-work"],
                    "operator ~/.claude/sessions": ["9.json"],
                    "operator restart log": ["asked rs_alpha 1234abcd"],
                    "operator nexus config": ["t1_session_lease.1234abcd-0000"]}


def test_footprint_is_empty_when_nothing_was_left(run, tmp_path, places) -> None:
    temp, _, _ = places
    sb = _sandbox(run, tmp_path / "home")
    before = run.footprint_before()
    (temp / "unrelated").write_text("x", encoding="utf-8")
    assert run.footprint_after(before, sb, {SID}, _fake_nx(tmp_path)) == {}


def test_a_failed_scenario_s_kept_files_are_not_counted(run, tmp_path, places) -> None:
    temp, _, _ = places
    sb = _sandbox(run, temp / "nx-restart-sandbox-roll-abc")
    before = run.footprint_before()
    sb.home.mkdir()
    for n in ("nexus-continuation-restart-rs_alpha-1234abcd-x.md", "rh-1234abcd.txt", "other-1234abcd.log"):
        (temp / n).write_text("x", encoding="utf-8")
    nx = _fake_nx(tmp_path)
    assert run.footprint_after(before, sb, {SID}, nx, kept=True) == {f"temp {temp}": ["other-1234abcd.log"]}
    assert run.footprint_after(before, sb, {SID}, nx, kept=False)[f"temp {temp}"] == [
        "nexus-continuation-restart-rs_alpha-1234abcd-x.md", "other-1234abcd.log", "rh-1234abcd.txt"]


def test_the_home_itself_is_never_counted(run, tmp_path, places) -> None:
    """run_scenario checks before it removes the HOME on a pass (the 2026-11-02 run failed all four
    scenarios on their own HOME when it did not)."""
    temp, _, _ = places
    sb = _sandbox(run, temp / "nx-restart-sandbox-gate-xyz")
    before = run.footprint_before()
    sb.home.mkdir()
    assert run.footprint_after(before, sb, {SID}, _fake_nx(tmp_path), kept=False) == {}


def test_a_mailbox_row_is_counted(run, tmp_path, places) -> None:
    nx = _fake_nx(tmp_path, stats='{"available": 1, "claimed": 0, "lapsed": 0, "dead": 0}')
    left = run.footprint_after(run.footprint_before(), _sandbox(run, tmp_path / "home"), {SID}, nx)
    assert left == {"operator service": ["mailbox/1234abcd {'available': 1, 'claimed': 0, 'lapsed': 0, 'dead': 0}"]}


@pytest.mark.parametrize(("kw", "row"), [
    ({"stats_rc": 1}, "mailbox/1234abcd: not read (exit 1)"),
    ({"stats": "not json"}, "mailbox/1234abcd: not read (JSONDecodeError)"),
    ({"directory_rc": 3}, "directory/rs_alpha: not read (exit 3)"),
])
def test_a_service_read_that_fails_is_a_finding_not_a_clean_read(run, tmp_path, places, kw, row) -> None:
    left = run.footprint_after(run.footprint_before(), _sandbox(run, tmp_path / "home"), {SID}, _fake_nx(tmp_path, **kw))
    assert row in left["operator service"]


def test_no_nx_and_no_session_ids_are_findings(run, tmp_path, places) -> None:
    sb = _sandbox(run, tmp_path / "home")
    before = run.footprint_before()
    assert "operator service" in run.footprint_after(before, sb, {SID}, "")
    assert "session ids" in run.footprint_after(before, sb, set(), _fake_nx(tmp_path))


def test_an_unreadable_temp_folder_is_a_finding(run, tmp_path, places, monkeypatch) -> None:
    temp, _, _ = places
    sb = _sandbox(run, tmp_path / "home")
    before = run.footprint_before()
    real = run._names

    def names(d, strict=False):
        if Path(d) == temp and strict:
            raise PermissionError("denied")
        return real(d, strict)

    monkeypatch.setattr(run, "_names", names)
    assert run.footprint_after(before, sb, {SID}, _fake_nx(tmp_path))[f"temp {temp}"] == ["not readable: denied"]


def test_directory_leases_count_only_this_scenario_s_sessions(run, tmp_path, places) -> None:
    """A lease another scenario's rs_alpha still holds is not this one's."""
    nx = _fake_nx(tmp_path, directory=f'  rs_alpha) echo "  session_id: {SID}  created_at: t  expires_at: t" ;;\n')
    sb = _sandbox(run, tmp_path / "home")
    before = run.footprint_before()
    assert run.footprint_after(before, sb, {SID}, nx) == {"directory lease": ["directory/rs_alpha (1234abcd)"]}
    assert run.footprint_after(before, sb, {"bbbb2222-y"}, nx) == {}


def test_route_leftovers_notes_what_is_expected_and_fails_the_rest(run) -> None:
    failures, notes = [], []
    assert run.route_leftovers({"directory lease": ["directory/rs_alpha (1234abcd)"]}, failures, notes) is False
    assert failures == [] and notes[0].startswith("left on purpose: directory lease")
    assert run.route_leftovers({"tmux socket": ["/tmp/x"], "directory lease": ["d"]}, failures, notes) is True
    assert failures == ["left outside the HOME: tmux socket: ['/tmp/x']"]


def _function(name: str):
    tree = ast.parse(RUN_PY.read_text(encoding="utf-8"))
    return next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name)


def test_run_scenario_fails_a_scenario_on_what_it_left() -> None:
    fn = _function("run_scenario")
    calls = {ast.unparse(c) for c in ast.walk(fn) if isinstance(c, ast.Call)}
    assert "footprint_after(fp_before, sb, full, sb.real_nx, kept=not ok)" in calls
    assert "route_leftovers(left, failures, notes)" in calls
    assert "upgrade_check(sb)" in calls
    ifs = [n for n in ast.walk(fn) if isinstance(n, ast.If) and ast.unparse(n.test) == "route_leftovers(left, failures, notes)"]
    assert ifs and any(ast.unparse(s) == "ok = False" for s in ifs[0].body)


# -- the auto-upgrade kill switch and the settled statuses -----------------------------------

def test_upgrade_check(run, tmp_path) -> None:
    sb = _sandbox(run, tmp_path / "home")
    assert run.upgrade_check(sb) == []  # no session booted
    sb.before = {"rs_alpha": {"sessionId": SID}}
    assert "hook log not read" in run.upgrade_check(sb)[0]
    log = sb.nexus_config / "logs" / "hook.log"
    log.parent.mkdir(parents=True)
    log.write_text("x event='upgrade_auto_disabled' reason='NX_UPGRADE_AUTO=0'\n", encoding="utf-8")
    assert run.upgrade_check(sb) == []
    log.write_text("x event='upgrade_auto_child_spawned' pid=1\n", encoding="utf-8")
    assert len(run.upgrade_check(sb)) == 2


def test_shell_settles_only_a_session_without_the_channel(run) -> None:
    roll = run.SPECS["roll"]
    assert run.settled_for(roll, "rs_nochan") == frozenset({"idle", "shell"})
    assert run.settled_for(roll, "rs_alpha") == frozenset({"idle"})


# -- review round 2 ---------------------------------------------------------------------------

def test_cleanup_ends_an_orphan_s_children_too(run, tmp_path, monkeypatch) -> None:
    """Ending an orphaned tree's root orphans its children; cleanup sweeps again and ends them."""
    monkeypatch.setattr(run, "LINGER_S", 0)
    home = tmp_path / "home"
    (home / "work").mkdir(parents=True)
    pidfile = tmp_path / "tree.pid"
    subprocess.run([sys.executable, "-c",
                    "import subprocess\n"
                    "p = subprocess.Popen(['sh', '-c', 'sleep 321 & echo $! > " + str(pidfile) + ".kid; wait'],"
                    f" cwd={str(home / 'work')!r}, start_new_session=True)\n"
                    f"open({str(pidfile)!r}, 'w').write(str(p.pid))\n"], check=True)
    for _ in range(50):
        if Path(f"{pidfile}.kid").exists() and Path(f"{pidfile}.kid").read_text(encoding="utf-8").strip():
            break
        time.sleep(0.1)
    root, kid = int(pidfile.read_text(encoding="utf-8")), int(Path(f"{pidfile}.kid").read_text(encoding="utf-8"))
    try:
        assert run.cleanup(_cleanup_sandbox(run, home), []) == []
        assert _gone(root) and _gone(kid)
    finally:
        for p in (root, kid):
            try:
                os.kill(p, 9)
            except ProcessLookupError:
                pass


def test_a_sweep_that_times_out_is_not_clean(run, tmp_path, monkeypatch) -> None:
    real = subprocess.run

    def slow(argv, *a, **k):
        if argv[0] == "pgrep":
            raise subprocess.TimeoutExpired(argv, 30)
        return real(argv, *a, **k)

    setattr_in(monkeypatch, run, "subprocess.run", slow)
    _, err = run._sandbox_procs(tmp_path / "absent-home")
    assert "argv search did not answer" in err


def test_a_new_session_file_that_will_not_parse_is_a_finding(run, tmp_path, places) -> None:
    _, _, op = places
    sb = _sandbox(run, tmp_path / "home")
    before = run.footprint_before()
    (op / ".claude" / "sessions" / "77.json").write_text("{not json", encoding="utf-8")
    left = run.footprint_after(before, sb, {SID}, _fake_nx(tmp_path))
    assert left == {"operator ~/.claude/sessions": ["77.json (not parsable: cannot tell whose it is)"]}


def test_check_fresh_waits_with_settled_for() -> None:
    calls = [ast.unparse(c) for c in ast.walk(_function("check_fresh")) if isinstance(c, ast.Call)]
    assert sum("settled_for(sb.spec, name)" in c for c in calls) >= 2
    assert not any("SETTLED" in c and "settled_for" not in c for c in calls)


class _Args:
    keep = False
    model = "sonnet"


@pytest.fixture
def scenario(run, monkeypatch):
    """run_scenario with the sandbox stubbed: no tmux, no sessions; each check returns what a test sets."""
    state = {"dirty": [], "upgrade": [], "left": {}, "raise": None, "guarded": []}
    monkeypatch.setattr(run.Sandbox, "setup", lambda self, crs: None)
    monkeypatch.setattr(run.Sandbox, "entries", lambda self: [])
    monkeypatch.setattr(run, "RUNNERS", {"gate": lambda sb, crs, a, failures, notes: None})
    monkeypatch.setattr(run, "cleanup", lambda sb, notes: list(state["dirty"]))
    monkeypatch.setattr(run, "upgrade_check", lambda sb: list(state["upgrade"]))
    original = run.signal.getsignal(run.signal.SIGTERM)

    def footprint(before, sb, sids, nx, kept=False):
        state["guarded"].append(run.signal.getsignal(run.signal.SIGTERM) is not original)
        if state["raise"]:
            raise state["raise"]
        return dict(state["left"])

    monkeypatch.setattr(run, "footprint_after", footprint)
    return state


def test_run_scenario_passes_a_clean_scenario(run, scenario) -> None:
    ok, failures, _ = run.run_scenario("gate", "claude", _Args(), None)
    assert ok and failures == []
    assert scenario["guarded"] == [True]  # the footprint runs inside the signal guard


@pytest.mark.parametrize(("key", "value", "expect"), [
    ("dirty", ["tmux server x still running"], "cleanup: tmux server x still running"),
    ("upgrade", ["auto-upgrade: a session spawned nx upgrade --auto"], "auto-upgrade: a session spawned"),
    ("left", {"tmux socket": ["/tmp/x"]}, "left outside the HOME: tmux socket"),
    ("raise", RuntimeError("boom"), "left outside the HOME: footprint: ['not taken: RuntimeError: boom']"),
])
def test_run_scenario_fails_on_each_check(run, scenario, key, value, expect) -> None:
    scenario[key] = value
    ok, failures, _ = run.run_scenario("gate", "claude", _Args(), None)
    assert not ok
    assert any(f.startswith(expect) for f in failures), failures


def test_run_scenario_passes_with_only_what_is_left_on_purpose(run, scenario) -> None:
    scenario["left"] = {"directory lease": ["directory/rs_alpha (1234abcd)"]}
    ok, failures, notes = run.run_scenario("gate", "claude", _Args(), None)
    assert ok and failures == []
    assert any(n.startswith("left on purpose: directory lease") for n in notes)


def test_claude_session_files_names_only_this_scenario_s(run, tmp_path, monkeypatch) -> None:
    root = tmp_path / "claude-tmp"
    root.mkdir()
    mine, other = root / f"cache-break-state-{SID}.json", root / "cache-break-state-ffff0000-1.json"
    for f in (mine, other):
        f.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(run, "claude_temp_roots", lambda: [root])
    assert run.claude_session_files({SID, ""}) == [mine]


def test_run_scenario_removes_claude_session_files(run, scenario, tmp_path, monkeypatch) -> None:
    removed = tmp_path / f"cache-break-state-{SID}.json"
    removed.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(run.Sandbox, "entries", lambda self: [{"sessionId": SID}])
    monkeypatch.setattr(run, "claude_session_files", lambda sids: [removed] if SID in sids else [])
    ok, _, _ = run.run_scenario("gate", "claude", _Args(), None)
    assert ok and not removed.exists()


# -- the withdrawal check (nexus-wjlbk) -------------------------------------------------------

def test_check_withdrawals(run) -> None:
    before = {"rs_alpha": {"sessionId": "aaaa-1"}, "rs_draft": {"sessionId": "bbbb-2"},
              "rs_nochan": {"sessionId": "cccc-3"}, "rs_busy": {"sessionId": "dddd-4"}}
    log = "\n".join([
        "10:00:00 mailing handoff ask to rs_alpha -> /tmp/a.md",
        "10:00:01 mailing handoff ask to rs_draft -> /tmp/d.md",
        "10:00:02 mailing handoff ask to rs_busy -> /tmp/b.md",
        "10:01:00 mailbox/aaaa-1: handoff ask t-aaaaaaaaaaa withdrawn; 1 other row(s) claimed and released",
        "10:01:01 mailbox/bbbb-2: handoff ask t-bbbbbbbbbbb already taken or expired",
        "10:01:02 mailbox/dddd-4: handoff ask t-ddddddddddd not withdrawn (in exited 1: down)",
    ])
    failures: list[str] = []
    assert run.check_withdrawals(log, before, failures) == 3  # rs_nochan was typed, not mailed
    assert failures == ["rs_busy: its mailed handoff ask has no withdrawn or already-taken line in the log"]


def test_run_scenario_fails_a_rolling_scenario_with_no_mailed_ask_to_check(run, scenario, monkeypatch) -> None:
    monkeypatch.setattr(run, "RUNNERS", {"roll": lambda sb, crs, a, failures, notes: setattr(sb, "before", {"rs_alpha": {"sessionId": SID}})})
    ok, failures, _ = run.run_scenario("roll", "claude", _Args(), None)
    assert not ok and "withdrawals: no mailed handoff ask found in the log to check" in failures


# -- the real withdrawal probe (nexus-wjlbk) --------------------------------------------------

def _probe_crs(tmp_path, *, mailed=True, tid=True, outcome="withdrawn"):
    from types import SimpleNamespace  # noqa: PLC0415 -- test-local
    crs = SimpleNamespace(LOG=tmp_path / "operator.log", SENT_ASKS={}, calls=[], nx=None)
    crs._nx_binary = lambda: crs.nx

    def mail_ask(s, path, sender, ttl=1200, **kw):
        crs.calls.append(("mail", crs.LOG))
        if tid:
            crs.SENT_ASKS[s["sessionId"]] = "t-probe"
        return mailed

    def withdraw_ask(sid, tid="", what="handoff ask"):
        crs.calls.append(("withdraw", crs.LOG))
        crs.SENT_ASKS.pop(sid, None)
        return outcome

    crs.mail_ask, crs.withdraw_ask = mail_ask, withdraw_ask
    return crs


@pytest.mark.parametrize(("kw", "stats", "want"), [
    ({}, '{"available": 0, "claimed": 0, "lapsed": 0, "dead": 0}', None),
    ({"outcome": "already taken or expired"}, '{"available": 0}', "said 'already taken or expired', not 'withdrawn'"),
    ({"mailed": False}, '{}', "could not be mailed"),
    ({"tid": False}, '{}', "printed no tuple id"),
    ({}, '{"available": 1}', "rows left in the probe's mailbox"),
    ({}, "not json", "could not be read back"),
])
def test_the_withdrawal_probe(run, tmp_path, kw, stats, want) -> None:
    crs = _probe_crs(tmp_path, **kw)
    sb = _sandbox(run, tmp_path / "home")
    sb.real_nx = "/nonexistent/nx"  # the probe reads back with the script's own nx, not the harness's
    crs.nx = _fake_nx(tmp_path, stats=stats)
    failures, notes = [], []
    run.check_real_withdrawal(sb, crs, failures, notes)
    if want is None:
        assert failures == [] and any("withdrawn against the real engine" in n for n in notes)
    else:
        assert len(failures) == 1 and want in failures[0], failures
    assert all(log == sb.log for _, log in crs.calls)  # the script logged to the sandbox's log
    assert crs.LOG == tmp_path / "operator.log"  # and the module's LOG is put back


def test_the_gate_scenario_runs_the_withdrawal_probe() -> None:
    calls = [ast.unparse(c.func) for c in ast.walk(_function("scenario_gate")) if isinstance(c, ast.Call)]
    assert "check_real_withdrawal" in calls and "check_sandbox_refusal" in calls


def test_harness_under_test_is_the_harness_beside_the_script_under_test(run, tmp_path, monkeypatch) -> None:
    """The sandbox HOME's record carries the harness the script under test computes for itself: the
    repo's run.py for the repo script, none for a positive control's copy outside a checkout."""
    import hashlib  # noqa: PLC0415 -- test-local
    crs = run.load_script()
    assert run.harness_under_test(crs) == hashlib.sha256(RUN_PY.read_bytes()).hexdigest()
    copy = tmp_path / "scripts" / "claude-restart-sessions"
    copy.parent.mkdir()
    copy.write_bytes(run.SCRIPT.read_bytes())
    monkeypatch.setattr(run, "SCRIPT", copy)
    assert run.harness_under_test(crs) == ""
