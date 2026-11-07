# SPDX-License-Identifier: AGPL-3.0-or-later
"""The restart sandbox judges nx-mcp's SIGHUP teardown on the engine (nexus-iz8ub).

nx-mcp runs its exit teardown on SIGHUP since 75e22da36 (7.97.0). The sandbox's cleanup used to delete
config.yml right after ``kill-server``, which starves that teardown (it builds its token store when it
runs), and it read no engine-side result: the harness's own file deletion stood in for the teardown and
the directory lease was "left on purpose". These tests run the new wait and the engine-side checks
against fakes, with no claude: a pid that exits late, a lease still present, a lease gone, a T1 session
still honoured, a probe that proves nothing. The tmux premise runs for real on a private ``-L`` server.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from nexus.db.http_scratch_store import SESSION_UNAUTHORIZED_MARKER

RUN_PY = Path(__file__).resolve().parents[1] / "e2e" / "restart-sessions" / "run.py"
SID = "1234abcd-0000-0000-0000-000000000001"
TOKEN = "tok-secret-do-not-print"


@pytest.fixture(scope="module")
def run():
    spec = importlib.util.spec_from_file_location("restart_sandbox_run_teardown", RUN_PY)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# -- nx_mcp_procs --------------------------------------------------------------------------------

def test_nx_mcp_procs_picks_the_server_not_its_neighbours(run) -> None:
    procs = [(1, "/x/bin/nx-mcp"), (2, "python /p/launch/nx_launch.py nx-mcp"),
             (3, "/x/bin/nx-mcp-catalog"), (4, "python /p/launch/nx_launch.py nx-mcp-catalog"),
             (5, "claude --model sonnet -n rs_alpha"), (6, "grep nx-mcp-foo"), (7, "sleep 5")]
    assert [p for p, _ in run.nx_mcp_procs(procs)] == [1, 2]


# -- wait_for_exit -------------------------------------------------------------------------------

class _Clock:
    """A fake monotonic clock: sleeping advances it."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept = 0.0

    def clock(self) -> float:
        return self.now

    def sleep(self, s: float) -> None:
        self.now += s
        self.slept += s


def test_wait_for_exit_waits_for_a_pid_that_exits_late(run) -> None:
    """A teardown that takes 3.5 s (a slow flush) is waited for, and the wait ends when it exits."""
    t = _Clock()
    procs = [(10, "nx-mcp a"), (11, "nx-mcp b")]
    exits = {10: 0.5, 11: 3.5}
    alive = lambda pid: t.now < exits[pid]  # noqa: E731
    args = {10: "nx-mcp a", 11: "nx-mcp b"}
    left = run.wait_for_exit(procs, 10.0, args_of=lambda pid: args[pid] if alive(pid) else "",
                             clock=t.clock, sleep=t.sleep)
    assert left == []
    assert 3.5 <= t.slept < 4.0  # no waiting once the last one is gone


def test_wait_for_exit_reports_a_straggler_at_the_bound(run) -> None:
    t = _Clock()
    left = run.wait_for_exit([(10, "nx-mcp a"), (11, "nx-mcp b")], 10.0,
                             args_of=lambda pid: "nx-mcp b" if pid == 11 else "", clock=t.clock, sleep=t.sleep)
    assert left == [(11, "nx-mcp b")]
    assert t.slept == pytest.approx(10.0)  # bounded: never longer than asked


def test_wait_for_exit_does_not_wait_on_a_reused_pid(run) -> None:
    """The pid now runs something else (a reused pid): the nx-mcp it was is gone."""
    t = _Clock()
    left = run.wait_for_exit([(10, "nx-mcp a")], 10.0, args_of=lambda _pid: "vim notes.txt",
                             clock=t.clock, sleep=t.sleep)
    assert left == [] and t.slept == 0.0


def test_wait_for_exit_with_nothing_to_wait_for_returns_at_once(run) -> None:
    t = _Clock()
    assert run.wait_for_exit([], 10.0, clock=t.clock, sleep=t.sleep) == []
    assert t.slept == 0.0


# -- leases and the T1 probe ---------------------------------------------------------------------

def test_read_t1_leases_reads_tokens_expired_or_not_and_skips_junk(run, tmp_path) -> None:
    (tmp_path / "t1_session_lease.aaa").write_text(json.dumps({"token": "t-a", "expires_at": 1}), encoding="utf-8")
    (tmp_path / "t1_session_lease.bbb").write_text(json.dumps({"token": "t-b", "expires_at": 9e12}), encoding="utf-8")
    (tmp_path / "t1_session_lease.old").write_text("bare-token", encoding="utf-8")
    (tmp_path / "t1_session_lease.empty").write_text(json.dumps({"token": ""}), encoding="utf-8")
    (tmp_path / "data_token_lease.aaa").write_text(json.dumps({"token": "no"}), encoding="utf-8")
    assert run.read_t1_leases(tmp_path) == {"aaa": "t-a", "bbb": "t-b"}
    assert run.read_t1_leases(tmp_path / "absent") == {}


def test_the_revoked_marker_is_the_one_nx_prints() -> None:
    """T1_UNAUTHORIZED is read off ``nx scratch list``'s error; it must stay inside the marker the store raises."""
    assert SESSION_UNAUTHORIZED_MARKER.endswith(RUN_MARKER())


def RUN_MARKER() -> str:  # noqa: N802 -- read from the file: the module under test is loaded by the fixture
    for line in RUN_PY.read_text(encoding="utf-8").splitlines():
        if line.startswith("T1_UNAUTHORIZED = "):
            return line.split('"')[1]
    raise AssertionError("T1_UNAUTHORIZED not found")


class _Run:
    """subprocess.run stand-in: answers by the nx verb, records the calls."""

    def __init__(self, scratch=(0, "", ""), directory=None, exc=None) -> None:
        self.scratch, self.directory, self.exc = scratch, directory or {}, exc
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, argv, **kw):
        self.calls.append((list(argv), kw))
        if self.exc and argv[1] == "scratch":
            raise self.exc
        if argv[1:3] == ["scratch", "list"]:
            rc, out, err = self.scratch
            return subprocess.CompletedProcess(argv, rc, out, err)
        if argv[1:3] == ["tuple", "directory"]:
            rc, out = self.directory.get(argv[3], (0, "No live directory entry."))
            return subprocess.CompletedProcess(argv, rc, out, "")
        raise AssertionError(argv)


def test_t1_session_state_reads_live_revoked_and_unknown(run) -> None:
    env = {"PATH": "/bin"}
    assert run.t1_session_state("nx", SID, TOKEN, env, run=_Run(scratch=(0, "", ""))) == "live"
    revoked = (1, "", "Error: HttpScratchStore: unauthorized T1 session (HTTP 401) (heal: declined)")
    assert run.t1_session_state("nx", SID, TOKEN, env, run=_Run(scratch=revoked)) == "revoked"
    assert run.t1_session_state("nx", SID, TOKEN, env, run=_Run(scratch=(2, "", "boom"))) == "unknown (exit 2)"
    timed_out = _Run(exc=subprocess.TimeoutExpired("nx", 60))
    assert run.t1_session_state("nx", SID, TOKEN, env, run=timed_out) == "unknown (TimeoutExpired)"


def test_t1_session_state_hands_the_token_to_nx_and_never_returns_it(run) -> None:
    fake = _Run(scratch=(1, f"{TOKEN} leaked", f"{TOKEN} leaked"))
    state = run.t1_session_state("nx", SID, TOKEN, {"PATH": "/bin"}, run=fake)
    argv, kw = fake.calls[0]
    assert argv == ["nx", "scratch", "list"]
    assert kw["env"]["NX_T1_SESSION"] == TOKEN and kw["env"]["NX_T1_SESSION_ID"] == SID
    assert kw["timeout"] > 0
    assert TOKEN not in state


# -- engine_teardown_findings --------------------------------------------------------------------

def _findings(run, tmp_path, *, after, before=None, directory=None, leases=None, exited=True,
              stragglers=(), lease_file=False, nx="nx"):
    leases = {SID: TOKEN} if leases is None else leases
    if lease_file:
        (tmp_path / f"t1_session_lease.{SID}").write_text("x", encoding="utf-8")
    exited_procs = [(10, "/x/nx-mcp")] if exited else []
    return run.engine_teardown_findings(
        nx=nx, names=["rs_alpha", "rs_blank"], sids={SID}, leases=leases,
        before={SID: "live"} if before is None else before, env={}, config_dir=tmp_path,
        exited=exited_procs, stragglers=list(stragglers), run=_Run(scratch=after, directory=directory))


REVOKED = (1, "", "unauthorized T1 session (HTTP 401)")
HELD = {"rs_alpha": (0, f"  session_id: {SID}  created_at: t  expires_at: t")}


def test_a_teardown_that_worked_has_no_findings(run, tmp_path) -> None:
    assert _findings(run, tmp_path, after=REVOKED) == []


def test_a_directory_lease_still_held_is_a_finding(run, tmp_path) -> None:
    out = _findings(run, tmp_path, after=REVOKED, directory=HELD)
    assert out == ["directory lease still held after nx-mcp exited: ['directory/rs_alpha (1234abcd)']"]


def test_another_session_s_directory_lease_is_not_this_one_s(run, tmp_path) -> None:
    other = {"rs_alpha": (0, "  session_id: ffffffff-other  created_at: t")}
    assert _findings(run, tmp_path, after=REVOKED, directory=other) == []


def test_a_directory_read_that_fails_is_a_finding_not_a_release(run, tmp_path) -> None:
    out = _findings(run, tmp_path, after=REVOKED, directory={"rs_alpha": (3, "")})
    assert out == ["directory/rs_alpha: not read (exit 3)"]


def test_a_t1_session_the_engine_still_honours_is_a_finding(run, tmp_path) -> None:
    out = _findings(run, tmp_path, after=(0, "", ""))
    assert out == [f"T1 session {SID[:8]}: not revoked after its nx-mcp exited"]


def test_a_probe_that_cannot_tell_is_a_finding(run, tmp_path) -> None:
    out = _findings(run, tmp_path, after=(2, "", "connection refused"))
    assert out == [f"T1 session {SID[:8]}: revocation not determined, the probe read unknown (exit 2)"]


def test_a_probe_that_did_not_read_live_before_the_kill_proves_nothing(run, tmp_path) -> None:
    """A broken probe reads 'revoked' for a session that was never live: that must not pass."""
    out = _findings(run, tmp_path, after=REVOKED, before={SID: "revoked"})
    assert len(out) == 1 and "revocation not provable" in out[0] and "'live'" in out[0]
    assert _findings(run, tmp_path, after=REVOKED, before={})[0].endswith("(it must read 'live')")


def test_a_straggler_is_a_finding(run, tmp_path) -> None:
    out = _findings(run, tmp_path, after=REVOKED, stragglers=[(10, "/x/nx-mcp --flag")])
    assert len(out) == 1 and out[0].startswith("nx-mcp pid 10 still running 10 s after kill-server")


def test_the_lease_file_nx_mcp_left_is_a_finding(run, tmp_path) -> None:
    out = _findings(run, tmp_path, after=REVOKED, lease_file=True)
    assert out == [f"T1 session {SID[:8]}: nx-mcp left its lease file t1_session_lease.{SID[:8]}... behind"]


def test_leases_with_no_nx_mcp_process_is_not_a_pass(run, tmp_path) -> None:
    """Non-vacuity: a lease was published, yet nothing was found to wait for."""
    out = _findings(run, tmp_path, after=REVOKED, exited=False)
    assert len(out) == 1 and "examined nothing" in out[0]


def test_no_leases_and_no_nx_means_nothing_is_read(run, tmp_path) -> None:
    """With no nx the engine is not asked (footprint_after reports the missing nx); the local checks still run."""
    assert _findings(run, tmp_path, after=REVOKED, leases={}, exited=False, nx="") == []
    out = _findings(run, tmp_path, after=REVOKED, nx="", stragglers=[(10, "nx-mcp")])
    assert len(out) == 1 and "still running" in out[0]


# -- cleanup, end to end with a real (fake) nx-mcp process ----------------------------------------

def _ps_args(pid: int) -> str:
    return subprocess.run(["ps", "-ww", "-o", "args=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()


class _Watch:
    def __init__(self, procs) -> None:
        self.procs = procs

    def poll(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def seen(self):
        return list(self.procs)

    def still_running(self):
        return [(p, a) for p, a in self.procs if _ps_args(p) == a]


def _fake_world(tmp_path: Path, *, teardown_s: float, revokes: bool = True, removes_lease: bool = True,
                holds_directory: bool = False):
    """A fake nx-mcp that does its 'teardown' teardown_s after it starts (the kill-server is not run; this
    stands in for the hangup), and a fake nx that reads the state it leaves. Records whether config.yml
    was still there when the 'teardown' ran, which is the whole point of the wait."""
    d = tmp_path / "world"
    d.mkdir()
    state, saw_config = d / "t1-state", d / "saw-config"
    state.write_text("live", encoding="utf-8")
    dir_out = d / "dir-out"
    dir_out.write_text(f"  session_id: {SID}  created_at: t\n" if holds_directory else "No live directory entry.\n",
                       encoding="utf-8")
    home = tmp_path / "home"
    cfg = home / ".config" / "nexus"
    lease = cfg / f"t1_session_lease.{SID}"
    mcp = d / "nx-mcp"
    mcp.write_text("#!/bin/sh\n" f"sleep {teardown_s}\n"
                   f'[ -f "{cfg}/config.yml" ] && echo yes > "{saw_config}"\n'
                   + (f'echo revoked > "{state}"\n' if revokes else "")
                   + (f'rm -f "{lease}"\n' if removes_lease else ""), encoding="utf-8")
    mcp.chmod(0o755)
    nx = d / "nx"
    nx.write_text("#!/bin/sh\n"
                  'if [ "$1" = scratch ] && [ "$2" = list ]; then\n'
                  f'  if [ "$(cat "{state}")" = live ]; then exit 0; fi\n'
                  '  echo "Error: HttpScratchStore: unauthorized T1 session (HTTP 401)" >&2; exit 1\nfi\n'
                  f'if [ "$1" = tuple ] && [ "$2" = directory ]; then\n  if [ "$3" = rs_alpha ]; then cat "{dir_out}"; '
                  'else echo "No live directory entry."; fi\n  exit 0\nfi\nexit 2\n', encoding="utf-8")
    nx.chmod(0o755)
    return home, cfg, lease, mcp, nx, saw_config


def _cleanup(run, monkeypatch, tmp_path, *, teardown_s, wait_s, **world):
    home, cfg, lease, mcp, nx, saw_config = _fake_world(tmp_path, teardown_s=teardown_s, **world)
    sb = run.Sandbox(home, "nx-rs-test-td", "claude", "sonnet", run.SPECS["gate"])
    cfg.mkdir(parents=True)
    (cfg / "config.yml").write_text("x", encoding="utf-8")
    lease.write_text(json.dumps({"token": TOKEN, "expires_at": 9e12}), encoding="utf-8")
    sb.real_nx = str(nx)
    sb.before = {"rs_alpha": {"sessionId": SID}}
    sb.tmux = lambda *a, **k: subprocess.CompletedProcess(a, 1, "", "")
    monkeypatch.setattr(run, "LINGER_S", 0)
    monkeypatch.setattr(run, "TEARDOWN_WAIT_S", wait_s)
    proc = subprocess.Popen([str(mcp)], start_new_session=True)
    sb.watch = _Watch([(proc.pid, _ps_args(proc.pid))])
    notes: list[str] = []
    try:
        dirty = run.cleanup(sb, notes)
    finally:
        try:
            os.killpg(proc.pid, 9)  # its own session: the script and its sleep, nothing else
        except (ProcessLookupError, PermissionError):  # already gone and reaped
            pass
        proc.wait()
    return dirty, notes, saw_config, cfg


def test_cleanup_waits_for_a_late_teardown_with_the_config_still_there(run, tmp_path, monkeypatch) -> None:
    """The fake nx-mcp tears down 1.5 s after the hangup: cleanup waits, finds the engine clean, and the
    config was still on disk when the teardown ran (the old order deleted it first)."""
    dirty, notes, saw_config, cfg = _cleanup(run, monkeypatch, tmp_path, teardown_s=1.5, wait_s=10.0)
    assert dirty == []
    assert saw_config.read_text(encoding="utf-8").strip() == "yes"
    assert not (cfg / "config.yml").exists()  # and gone once the wait is over
    assert any(n.startswith("teardown: waited on 1 nx-mcp process(es), 0 still running") for n in notes)


def test_cleanup_reports_an_nx_mcp_that_outlives_the_bound(run, tmp_path, monkeypatch) -> None:
    dirty, _, _, _ = _cleanup(run, monkeypatch, tmp_path, teardown_s=30, wait_s=1.0)
    assert any(d.startswith("nx-mcp pid") and "still running 1 s after kill-server" in d for d in dirty), dirty


def test_cleanup_fails_a_lease_the_teardown_left(run, tmp_path, monkeypatch) -> None:
    dirty, _, _, _ = _cleanup(run, monkeypatch, tmp_path, teardown_s=0.5, wait_s=10.0, holds_directory=True)
    assert dirty == ["directory lease still held after nx-mcp exited: ['directory/rs_alpha (1234abcd)']"]


def test_cleanup_fails_a_t1_session_the_teardown_did_not_revoke(run, tmp_path, monkeypatch) -> None:
    dirty, _, _, _ = _cleanup(run, monkeypatch, tmp_path, teardown_s=0.5, wait_s=10.0, revokes=False)
    assert dirty == [f"T1 session {SID[:8]}: not revoked after its nx-mcp exited"]


def test_cleanup_does_not_credit_its_own_file_deletion(run, tmp_path, monkeypatch) -> None:
    """nx-mcp left its lease file: the harness deleting it later must not hide that."""
    dirty, _, _, cfg = _cleanup(run, monkeypatch, tmp_path, teardown_s=0.5, wait_s=10.0, removes_lease=False)
    assert dirty == [f"T1 session {SID[:8]}: nx-mcp left its lease file t1_session_lease.{SID[:8]}... behind"]
    assert not list(cfg.glob("t1_session_lease.*"))  # still deleted: the credential does not stay on disk


# -- the leftovers rule and the footprint ----------------------------------------------------------

def test_nothing_is_left_on_purpose_any_more(run) -> None:
    assert run.EXPECTED_PLACES == ()
    failures, notes = [], []
    assert run.route_leftovers({"directory lease": ["directory/rs_alpha (1234abcd)"]}, failures, notes) is True
    assert failures == ["left outside the HOME: directory lease: ['directory/rs_alpha (1234abcd)']"]


def test_footprint_and_cleanup_read_the_same_directory_names(run, tmp_path) -> None:
    sb = run.Sandbox(tmp_path, "nx-rs-test-td", "claude", "sonnet", run.SPECS["roll"])
    assert run.directory_names(sb) == sorted({*sb.spec.sessions, "rs_blank"})
    assert "rs_blank" in run.directory_names(sb) and "rs_alpha" in run.directory_names(sb)


# -- kill-server hangs up a child process group (the premise of the whole teardown) ---------------

needs_tmux = pytest.mark.skipif(not shutil.which("tmux"), reason="tmux not installed")


@needs_tmux
@pytest.mark.parametrize("shape", ["shell", "direct"])
def test_tmux_kill_server_hangs_up_a_child_process_group(run, tmp_path, shape) -> None:
    """On a private -L server, a child that is not its group's leader gets SIGHUP when the server is
    killed, whether the pane runs bash (the command typed in) or the command itself (rs_direct)."""
    assert run.tmux_hangup_probe(tmp_path, shape) == []


@needs_tmux
def test_the_hangup_probe_can_fail(run, tmp_path) -> None:
    """Red control: a child that ignores SIGHUP (an inherited ignore, as under nohup) is reported."""
    ignoring = ('echo "$$ $(ps -o pgid= -p $$ | tr -d " ")" > "$1"\n' "trap '' HUP\n" "sleep 4\n")
    out = run.tmux_hangup_probe(tmp_path, "shell", wait_s=2.0, child_script=ignoring)
    assert len(out) == 1 and "kill-server sent no SIGHUP to the child" in out[0]


@needs_tmux
def test_the_hangup_probe_never_touches_the_default_tmux_server(run, tmp_path) -> None:
    def source() -> str:
        return RUN_PY.read_text(encoding="utf-8")

    text = source()
    body = text[text.index("def tmux_hangup_probe"):text.index("def hangup_check_main")]
    assert '"-L", sock' in body and '"-f", "/dev/null"' in body
    assert "kill-server" in body
    # every tmux call goes through the local helper that carries -L; there is no bare ["tmux", ...] elsewhere
    assert body.count('["tmux"') == 1


@needs_tmux
def test_the_hangup_check_command_passes_without_claude_or_a_token() -> None:
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDE_CODE_OAUTH_TOKEN", "TMUX")}
    r = subprocess.run([sys.executable, "-I", str(RUN_PY), "--hangup-check"], capture_output=True, text=True,
                       env=env, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "TMUX HANGUP CHECK PASSED" in r.stdout
