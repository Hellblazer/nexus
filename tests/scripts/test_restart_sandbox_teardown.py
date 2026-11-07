# SPDX-License-Identifier: AGPL-3.0-or-later
"""The restart sandbox judges nx-mcp's SIGHUP teardown on the engine (nexus-iz8ub).

nx-mcp runs its exit teardown on SIGHUP since 75e22da36 (7.97.0). The sandbox's cleanup used to delete
config.yml right after ``kill-server``, which starves that teardown (it builds its token store when it
runs), and it read no engine-side result: the harness's own file deletion stood in for the teardown and
the directory lease was "left on purpose". These tests run the new wait and the engine-side checks
against fakes, with no claude: a pid that exits late, a lease still present, a lease gone, a T1 session
still honoured, a probe that proves nothing, a check that examined nothing. The tmux mechanism runs for
real on a private ``-L`` server.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import types
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


# -- nx_mcp_procs --------------------------------------------------------------------------------

def test_nx_mcp_procs_picks_the_server_not_its_neighbours(run) -> None:
    procs = [(1, "/x/bin/nx-mcp"), (2, "python /p/launch/nx_launch.py nx-mcp"),
             (3, "/x/bin/nx-mcp-catalog"), (4, "python /p/launch/nx_launch.py nx-mcp-catalog"),
             (5, "claude --model sonnet -n rs_alpha"), (6, "grep nx-mcp-foo"), (7, "sleep 5")]
    assert [p for p, _ in run.nx_mcp_procs(procs)] == [1, 2]


# -- wait_for_exit -------------------------------------------------------------------------------

def test_wait_for_exit_waits_for_a_pid_that_exits_late(run) -> None:
    """A teardown that takes 3.5 s (a slow flush) is waited for, and the wait ends when it exits."""
    t = _Clock()
    procs = [(10, "nx-mcp a"), (11, "nx-mcp b")]
    exits = {10: 0.5, 11: 3.5}
    args = {10: "nx-mcp a", 11: "nx-mcp b"}
    left = run.wait_for_exit(procs, 10.0, args_of=lambda pid: args[pid] if t.now < exits[pid] else "",
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


def test_a_ps_that_does_not_answer_is_unknown_not_exited(run) -> None:
    """ps timing out (None) keeps the pid in the wait: it is a straggler at the bound, and it is waited
    on again once ps answers 'gone'."""
    t = _Clock()
    assert run.wait_for_exit([(10, "nx-mcp a")], 5.0, args_of=lambda _pid: None, clock=t.clock,
                             sleep=t.sleep) == [(10, "nx-mcp a")]
    assert t.slept == pytest.approx(5.0)
    t = _Clock()
    assert run.wait_for_exit([(10, "nx-mcp a")], 10.0, args_of=lambda _pid: None if t.now < 2 else "",
                             clock=t.clock, sleep=t.sleep) == []


def test_args_or_none_says_none_when_ps_times_out(run, monkeypatch) -> None:
    def slow(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 30)

    monkeypatch.setattr(run.subprocess, "run", slow)
    assert run._args_or_none(1234) is None


def test_args_or_none_reads_a_running_process_and_a_gone_one(run) -> None:
    assert "python" in run._args_or_none(os.getpid()).lower()
    assert run._args_or_none(2**22 + 11) == ""


def test_poll_reads_until_done_or_the_bound(run) -> None:
    t = _Clock()
    seen = []

    def read() -> int:
        seen.append(t.now)
        return len(seen)

    assert run.poll(read, lambda v: v >= 3, 10.0, clock=t.clock, sleep=t.sleep) == 3
    assert t.slept == pytest.approx(2.0)
    t = _Clock()
    assert run.poll(lambda: 0, lambda v: v, 4.0, clock=t.clock, sleep=t.sleep) == 0
    assert t.slept == pytest.approx(4.0)
    t = _Clock()
    assert run.poll(lambda: 0, lambda v: v, 0, clock=t.clock, sleep=t.sleep) == 0  # one read, no wait
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


def test_read_t1_leases_ignores_the_transient_publish_files(run, tmp_path) -> None:
    """publish_t1_session_lease writes ``<lease>.<pid>.<uuid>.tmp`` and renames it: a complete one in
    flight is not a second session."""
    (tmp_path / "t1_session_lease.aaa").write_text(json.dumps({"token": "t-a"}), encoding="utf-8")
    (tmp_path / "t1_session_lease.aaa.4242.0123abcd.tmp").write_text(json.dumps({"token": "t-new"}), encoding="utf-8")
    assert run.read_t1_leases(tmp_path) == {"aaa": "t-a"}


def test_the_revoked_marker_is_the_one_nx_prints(run) -> None:
    """T1_UNAUTHORIZED is read off ``nx scratch list``'s error; it must stay inside the marker the store raises."""
    assert run.T1_UNAUTHORIZED in SESSION_UNAUTHORIZED_MARKER


class _Run:
    """subprocess.run stand-in: answers by the nx verb, records the calls. A scratch/directory answer may
    be a list: answered in order, the last repeating."""

    def __init__(self, scratch=(0, "", ""), directory=None, exc=None) -> None:
        self.scratch, self.directory, self.exc = scratch, directory or {}, exc
        self.calls: list[tuple[list[str], dict]] = []
        self.counts: dict[str, int] = {}

    def _next(self, key: str, value):
        if not isinstance(value, list):
            return value
        i = self.counts.get(key, 0)
        self.counts[key] = i + 1
        return value[min(i, len(value) - 1)]

    def __call__(self, argv, **kw):
        self.calls.append((list(argv), kw))
        if self.exc and argv[1] == "scratch":
            raise self.exc
        if argv[1:3] == ["scratch", "list"]:
            rc, out, err = self._next("scratch", self.scratch)
            return subprocess.CompletedProcess(argv, rc, out, err)
        if argv[1:3] == ["tuple", "directory"]:
            rc, out = self._next(f"dir-{argv[3]}", self.directory.get(argv[3], (0, "No live directory entry.")))
            return subprocess.CompletedProcess(argv, rc, out, "")
        raise AssertionError(argv)


def test_t1_session_state_reads_live_revoked_and_unknown(run) -> None:
    env, nap = {"PATH": "/bin"}, (lambda _s: None)
    assert run.t1_session_state("nx", SID, TOKEN, env, run=_Run(scratch=(0, "", "")), sleep=nap) == "live"
    revoked = (1, "", "Error: HttpScratchStore: unauthorized T1 session (HTTP 401) (heal: declined)")
    assert run.t1_session_state("nx", SID, TOKEN, env, run=_Run(scratch=revoked), sleep=nap) == "revoked"
    assert run.t1_session_state("nx", SID, TOKEN, env, run=_Run(scratch=(2, "", "boom")),
                                sleep=nap) == "unknown (exit 2: boom)"
    timed_out = _Run(exc=subprocess.TimeoutExpired("nx", 20))
    assert run.t1_session_state("nx", SID, TOKEN, env, run=timed_out, sleep=nap) == "unknown (TimeoutExpired)"


def test_t1_session_state_retries_an_unknown_and_bounds_each_try(run) -> None:
    """A transient failure (the engine restarting) is tried again, with a short timeout, and a later
    answer wins; the whole probe is bounded."""
    t = _Clock()
    fake = _Run(scratch=[(1, "", "connection refused"), (1, "", "connection refused"), (0, "", "")])
    assert run.t1_session_state("nx", SID, TOKEN, {}, run=fake, sleep=t.sleep) == "live"
    assert len(fake.calls) == 3 and t.slept == pytest.approx(2.0)
    assert all(kw["timeout"] <= 20 for _, kw in fake.calls)
    fake = _Run(scratch=(1, "", "connection refused"))
    t = _Clock()
    assert run.t1_session_state("nx", SID, TOKEN, {}, run=fake, sleep=t.sleep) == "unknown (exit 1: connection refused)"
    assert len(fake.calls) == 3  # 2-3 tries, never more


def test_t1_session_state_hands_the_token_to_nx_and_never_returns_it(run) -> None:
    fake = _Run(scratch=(1, f"{TOKEN} leaked", f"line one\nbad {TOKEN} here"))
    state = run.t1_session_state("nx", SID, TOKEN, {"PATH": "/bin"}, run=fake, sleep=lambda _s: None)
    argv, kw = fake.calls[0]
    assert argv == ["nx", "scratch", "list"]
    assert kw["env"]["NX_T1_SESSION"] == TOKEN and kw["env"]["NX_T1_SESSION_ID"] == SID
    assert kw["timeout"] > 0
    assert TOKEN not in state and state == "unknown (exit 1: bad <token> here)"


# -- engine_teardown_findings --------------------------------------------------------------------

REVOKED = (1, "", "unauthorized T1 session (HTTP 401)")
HELD = (0, f"  session_id: {SID}  created_at: t  expires_at: t")
NONE = (0, "No live directory entry.")


def _base(run, **kw):
    fields = dict(leases={SID: TOKEN}, live={SID: "live"}, sids={SID}, directory=([f"directory/rs_alpha ({SID[:8]})"], []))
    fields.update(kw)
    return run.Baseline(**fields)


def _findings(run, tmp_path, *, after=REVOKED, directory=None, base=None, exited=True, stragglers=(),
              lease_file=False, nx="nx", expect=False, settle_s=0.0, t=None):
    base = base or _base(run)
    if lease_file:
        (tmp_path / f"t1_session_lease.{SID}").write_text("x", encoding="utf-8")
    t = t or _Clock()
    return run.engine_teardown_findings(
        nx=nx, names=["rs_alpha", "rs_blank"], base=base, env={}, config_dir=tmp_path,
        exited=[(10, "/x/nx-mcp")] if exited else [], stragglers=list(stragglers), expect=expect,
        settle_s=settle_s, run=_Run(scratch=after, directory=directory), sleep=t.sleep, clock=t.clock)


def test_a_teardown_that_worked_has_no_findings(run, tmp_path) -> None:
    assert _findings(run, tmp_path, expect=True) == []


def test_a_directory_lease_still_held_is_a_finding(run, tmp_path) -> None:
    out = _findings(run, tmp_path, directory={"rs_alpha": HELD})
    assert out == ["directory lease still held after nx-mcp exited: ['directory/rs_alpha (1234abcd)']"]


def test_a_directory_release_that_lags_is_waited_for(run, tmp_path) -> None:
    """The release re-sends the row with a 1 s TTL: the first read after the exit can still see it."""
    t = _Clock()
    assert _findings(run, tmp_path, directory={"rs_alpha": [HELD, HELD, NONE]}, settle_s=5.0, t=t) == []
    assert 2.0 <= t.slept < 5.0  # waited for it, and no longer than it took
    t = _Clock()
    assert len(_findings(run, tmp_path, directory={"rs_alpha": HELD}, settle_s=5.0, t=t)) == 1
    assert t.slept >= 5.0  # a lease that never goes is reported only at the bound


def test_a_t1_revocation_that_lags_is_waited_for(run, tmp_path) -> None:
    t = _Clock()
    assert _findings(run, tmp_path, after=[(0, "", ""), REVOKED], settle_s=5.0, t=t) == []
    assert t.slept >= 1.0


def test_another_session_s_directory_lease_is_not_this_one_s(run, tmp_path) -> None:
    other = {"rs_alpha": (0, "  session_id: ffffffff-other  created_at: t")}
    assert _findings(run, tmp_path, directory=other) == []


def test_a_directory_read_that_fails_is_a_finding_not_a_release(run, tmp_path) -> None:
    out = _findings(run, tmp_path, directory={"rs_alpha": (3, "")})
    assert out == ["directory/rs_alpha: not read (exit 3)"]


def test_a_directory_release_with_no_lease_held_before_proves_nothing(run, tmp_path) -> None:
    """The positive control, as for T1: a row never held cannot be seen released."""
    out = _findings(run, tmp_path, base=_base(run, directory=([], [])))
    assert len(out) == 1 and out[0].startswith("no directory lease was held by the scenario's sessions")
    out = _findings(run, tmp_path, base=_base(run, directory=None))
    assert out == ["directory leases not read before the kill: their release proves nothing"]
    out = _findings(run, tmp_path, base=_base(run, directory=(["d"], ["directory/rs_alpha: not read (exit 3)"])))
    assert out == ["before the kill: directory/rs_alpha: not read (exit 3)"]


def test_a_t1_session_the_engine_still_honours_is_a_finding(run, tmp_path) -> None:
    out = _findings(run, tmp_path, after=(0, "", ""))
    assert out == [f"T1 session {SID[:8]}: not revoked after its nx-mcp exited"]


def test_a_probe_that_cannot_tell_is_a_finding_with_the_reason(run, tmp_path) -> None:
    out = _findings(run, tmp_path, after=(2, "", "connection refused"))
    assert out == [f"T1 session {SID[:8]}: revocation not determined, the probe read "
                   f"unknown (exit 2: connection refused)"]


def test_a_probe_that_did_not_read_live_before_the_kill_proves_nothing(run, tmp_path) -> None:
    """A broken probe reads 'revoked' for a session that was never live: that must not pass."""
    out = _findings(run, tmp_path, base=_base(run, live={SID: "revoked"}))
    assert len(out) == 1 and "revocation not provable" in out[0] and "'live'" in out[0]
    assert _findings(run, tmp_path, base=_base(run, live={}))[0].endswith("(it must read 'live')")


def test_a_straggler_is_a_finding(run, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(run, "TEARDOWN_WAIT_S", 20.0)
    out = _findings(run, tmp_path, stragglers=[(77, "/x/nx-mcp --flag")])
    assert len(out) == 1
    assert out[0].startswith("nx-mcp pid 77 still running (or unreadable) 20 s after kill-server")
    assert "10 s" not in out[0]


def test_the_lease_file_nx_mcp_left_is_a_finding(run, tmp_path) -> None:
    out = _findings(run, tmp_path, lease_file=True)
    assert out == [f"T1 session {SID[:8]}: nx-mcp left its lease file t1_session_lease.{SID[:8]}... behind"]


def test_leases_with_no_nx_mcp_process_is_not_a_pass(run, tmp_path) -> None:
    """Non-vacuity: a lease was published, yet nothing was found to wait for."""
    out = _findings(run, tmp_path, exited=False)
    assert len(out) == 1 and "no nx-mcp process was found to wait for" in out[0]


def test_sessions_that_ran_with_nothing_found_to_examine_is_a_finding_not_a_pass(run, tmp_path) -> None:
    """The ship-blocker: sessions ran and there is an nx, yet no lease, no nx-mcp pid and no session id
    were found (a deferred mint, a launcher change, a wrong argv match). Every check would pass on that."""
    out = _findings(run, tmp_path, base=run.Baseline(), exited=False, expect=True)
    assert [o.split(":")[0] for o in out] == ["teardown unexamined"] * 3
    assert any("no T1 session lease" in o for o in out)
    assert any("no nx-mcp process" in o for o in out)
    assert any("no session id" in o for o in out)
    # the same input is quiet when no session ran / no nx was available to ask
    assert _findings(run, tmp_path, base=run.Baseline(), exited=False, expect=False) == []


def test_without_an_nx_the_engine_is_not_asked_but_local_checks_still_run(run, tmp_path) -> None:
    assert _findings(run, tmp_path, nx="") == []
    out = _findings(run, tmp_path, nx="", stragglers=[(10, "nx-mcp")])
    assert len(out) == 1 and "still running" in out[0]


def test_fewer_leases_than_live_sessions_is_a_finding(run, tmp_path) -> None:
    """One session's lease read is not the others': every live session owns an nx-mcp and a lease."""
    out = _findings(run, tmp_path, base=_base(run, sessions=3), expect=True)
    assert out == ["teardown unexamined for 2 session(s): 3 live session(s) but only 1 T1 session lease(s) "
                   "were published or readable"]
    assert _findings(run, tmp_path, base=_base(run, sessions=1), expect=True) == []
    assert _findings(run, tmp_path, base=_base(run, sessions=3), expect=False) == []


def test_the_baseline_counts_the_live_sessions(run, tmp_path, monkeypatch) -> None:
    sb = run.Sandbox(tmp_path, "nx-rs-test-td", "claude", "sonnet", run.SPECS["gate"])
    sb.real_nx = ""
    monkeypatch.setattr(run.Sandbox, "entries", lambda self: [{"sessionId": "a"}, {"sessionId": "b"}])
    base = run.read_baseline(sb, True)
    assert base.sessions == 2 and base.sids == {"a", "b"}


def test_cleanup_with_a_session_that_published_no_lease_is_not_a_pass(run, tmp_path, monkeypatch) -> None:
    """Two live sessions, one lease: the second session's teardown was never looked at."""
    monkeypatch.setattr(run.Sandbox, "entries", lambda self: [{"sessionId": SID}, {"sessionId": "ffffffff-2"}])
    dirty, _, _, _ = _cleanup(run, monkeypatch, tmp_path)
    assert dirty == ["teardown unexamined for 1 session(s): 2 live session(s) but only 1 T1 session lease(s) "
                     "were published or readable"]


def test_the_teardown_wait_is_at_least_the_worst_case_and_cleanup_uses_it(run) -> None:
    """flush 5 s + revoke 5 s run serially, plus telemetry and the name release: 10 s is too short."""
    assert run.TEARDOWN_WAIT_S >= 20
    assert run.wait_for_exit.__defaults__[0] == run.TEARDOWN_WAIT_S
    text = RUN_PY.read_text(encoding="utf-8")
    assert "wait_for_exit(nx_mcp, TEARDOWN_WAIT_S)" in text


def test_a_baseline_that_could_not_be_read_is_a_finding(run, tmp_path) -> None:
    out = _findings(run, tmp_path, base=_base(run, errors=["T1 session probes not read before the kill: OSError: x"]))
    assert out == ["T1 session probes not read before the kill: OSError: x"]


# -- the version precondition ----------------------------------------------------------------------

def _version_run(out: str, rc: int = 0, exc=None):
    def fake(argv, **kw):
        if exc:
            raise exc
        return subprocess.CompletedProcess(argv, rc, out, "")
    return fake


def test_nx_version_parses_and_gates_the_teardown(run) -> None:
    assert run.nx_version("nx", run=_version_run("nx, version 7.97.0\n")) == (7, 97, 0)
    assert run.nx_version("nx", run=_version_run("nx, version 7.98.1.dev3\n")) == (7, 98, 1)
    assert run.nx_version("nx", run=_version_run("", rc=2)) is None
    assert run.nx_version("nx", run=_version_run("junk")) is None
    assert run.nx_version("nx", run=_version_run("", exc=subprocess.TimeoutExpired("nx", 30))) is None
    assert run.teardown_unverified_reason("nx", run=_version_run("nx, version 7.97.0")) == ""
    assert run.teardown_unverified_reason("nx", run=_version_run("nx, version 8.0.1")) == ""
    assert "7.96.9, below 7.97.0" in run.teardown_unverified_reason("nx", run=_version_run("nx, version 7.96.9"))
    assert "could not read" in run.teardown_unverified_reason("nx", run=_version_run("", rc=1))


def test_the_sandbox_drops_an_inherited_t1_session_token(run, tmp_path, monkeypatch) -> None:
    """With NX_T1_SESSION inherited, nx-mcp would take USE_INHERITED and never mint or publish a lease."""
    monkeypatch.setenv("NX_T1_SESSION", "operator-token")
    sb = run.Sandbox(tmp_path, "nx-rs-test-td", "claude", "sonnet", run.SPECS["gate"])
    assert "NX_T1_SESSION" not in sb.env and "NX_T1_SESSION_ID" not in sb.env


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


def _fake_world(tmp_path: Path, *, teardown_s: float, revokes: bool, removes_lease: bool, holds_directory: bool,
                directory_before: bool, release_lag_s: float, version: str):
    """A fake nx-mcp that does its 'teardown' teardown_s after it starts (the kill-server is not run; this
    stands in for the hangup), and a fake nx that reads the state it leaves. Records whether config.yml
    was still there when the 'teardown' ran, which is the whole point of the wait."""
    d = tmp_path / "world"
    d.mkdir()
    state, saw_config, dir_out = d / "t1-state", d / "saw-config", d / "dir-out"
    state.write_text("live", encoding="utf-8")
    held = f"  session_id: {SID}  created_at: t\n"
    none = "No live directory entry.\n"
    dir_out.write_text(held if directory_before else none, encoding="utf-8")
    home = tmp_path / "home"
    cfg = home / ".config" / "nexus"
    lease = cfg / f"t1_session_lease.{SID}"
    mcp = d / "nx-mcp"
    release = ""
    if directory_before and not holds_directory:
        release = (f'echo "{none.strip()}" > "{dir_out}"\n' if not release_lag_s
                   else f'( sleep {release_lag_s}; echo "{none.strip()}" > "{dir_out}" ) &\n')
    mcp.write_text("#!/bin/sh\n" f"sleep {teardown_s}\n"
                   f'[ -f "{cfg}/config.yml" ] && echo yes > "{saw_config}"\n'
                   + (f'echo revoked > "{state}"\n' if revokes else "")
                   + (f'rm -f "{lease}"\n' if removes_lease else "") + release, encoding="utf-8")
    mcp.chmod(0o755)
    nx = d / "nx"
    nx.write_text("#!/bin/sh\n"
                  f'if [ "$1" = --version ]; then echo "nx, version {version}"; exit 0; fi\n'
                  'if [ "$1" = scratch ] && [ "$2" = list ]; then\n'
                  f'  if [ "$(cat "{state}")" = live ]; then exit 0; fi\n'
                  '  echo "Error: HttpScratchStore: unauthorized T1 session (HTTP 401)" >&2; exit 1\nfi\n'
                  f'if [ "$1" = tuple ] && [ "$2" = directory ]; then\n  if [ "$3" = rs_alpha ]; then cat "{dir_out}"; '
                  'else echo "No live directory entry."; fi\n  exit 0\nfi\nexit 2\n', encoding="utf-8")
    nx.chmod(0o755)
    return home, cfg, lease, mcp, nx, saw_config


def _cleanup(run, monkeypatch, tmp_path, *, teardown_s=0.5, wait_s=10.0, revokes=True, removes_lease=True,
             holds_directory=False, directory_before=True, release_lag_s=0.0, version="7.97.0",
             publish_lease=True, settle_s=1.0):
    home, cfg, lease, mcp, nx, saw_config = _fake_world(
        tmp_path, teardown_s=teardown_s, revokes=revokes, removes_lease=removes_lease,
        holds_directory=holds_directory, directory_before=directory_before, release_lag_s=release_lag_s,
        version=version)
    sb = run.Sandbox(home, "nx-rs-test-td", "claude", "sonnet", run.SPECS["gate"])
    cfg.mkdir(parents=True)
    (cfg / "config.yml").write_text("x", encoding="utf-8")
    if publish_lease:
        lease.write_text(json.dumps({"token": TOKEN, "expires_at": 9e12}), encoding="utf-8")
    sb.real_nx = str(nx)
    sb.before = {"rs_alpha": {"sessionId": SID}}
    sb.tmux = lambda *a, **k: subprocess.CompletedProcess(a, 1, "", "")
    monkeypatch.setattr(run, "LINGER_S", 0)
    monkeypatch.setattr(run, "TEARDOWN_WAIT_S", wait_s)
    monkeypatch.setattr(run, "SETTLE_S", settle_s)
    run.UNVERIFIED.clear()
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
    dirty, notes, saw_config, cfg = _cleanup(run, monkeypatch, tmp_path, teardown_s=1.5)
    assert dirty == []
    assert saw_config.read_text(encoding="utf-8").strip() == "yes"
    assert not (cfg / "config.yml").exists()  # and gone once the wait is over
    assert any(n.startswith("teardown: waited on 1 nx-mcp process(es), 0 still running") for n in notes)
    assert run.UNVERIFIED == []


def test_cleanup_waits_for_the_directory_release_to_land(run, tmp_path, monkeypatch) -> None:
    """The release lands 1.5 s after the process exits (the 1 s TTL row): a single read right after the
    exit would call it held."""
    dirty, _, _, _ = _cleanup(run, monkeypatch, tmp_path, release_lag_s=1.5, settle_s=5.0)
    assert dirty == []


def test_cleanup_reports_an_nx_mcp_that_outlives_the_bound(run, tmp_path, monkeypatch) -> None:
    dirty, _, _, _ = _cleanup(run, monkeypatch, tmp_path, teardown_s=30, wait_s=1.0)
    assert any(d.startswith("nx-mcp pid") and "still running (or unreadable) 1 s after kill-server" in d
               for d in dirty), dirty


def test_cleanup_fails_a_lease_the_teardown_left(run, tmp_path, monkeypatch) -> None:
    dirty, _, _, _ = _cleanup(run, monkeypatch, tmp_path, holds_directory=True)
    assert dirty == ["directory lease still held after nx-mcp exited: ['directory/rs_alpha (1234abcd)']"]


def test_cleanup_fails_a_t1_session_the_teardown_did_not_revoke(run, tmp_path, monkeypatch) -> None:
    dirty, _, _, _ = _cleanup(run, monkeypatch, tmp_path, revokes=False)
    assert dirty == [f"T1 session {SID[:8]}: not revoked after its nx-mcp exited"]


def test_cleanup_does_not_credit_its_own_file_deletion(run, tmp_path, monkeypatch) -> None:
    """nx-mcp left its lease file: the harness deleting it later must not hide that."""
    dirty, _, _, cfg = _cleanup(run, monkeypatch, tmp_path, removes_lease=False)
    assert dirty == [f"T1 session {SID[:8]}: nx-mcp left its lease file t1_session_lease.{SID[:8]}... behind"]
    assert not list(cfg.glob("t1_session_lease.*"))  # still deleted: the credential does not stay on disk


def test_cleanup_fails_a_directory_that_was_never_held(run, tmp_path, monkeypatch) -> None:
    dirty, _, _, _ = _cleanup(run, monkeypatch, tmp_path, directory_before=False)
    assert len(dirty) == 1 and dirty[0].startswith("no directory lease was held by the scenario's sessions")


def test_cleanup_with_sessions_but_no_lease_is_not_a_pass(run, tmp_path, monkeypatch) -> None:
    """The ship-blocker end to end: the session published no lease (a deferred mint, an inherited
    NX_T1_SESSION): the old code passed this having examined no T1 session at all."""
    dirty, _, _, _ = _cleanup(run, monkeypatch, tmp_path, publish_lease=False)
    assert dirty == ["teardown unexamined: sessions ran but no T1 session lease was published or readable"]


def test_cleanup_with_a_baseline_read_that_raises_is_a_finding(run, tmp_path, monkeypatch) -> None:
    def boom(_d):
        raise RuntimeError("disk gone")

    monkeypatch.setattr(run, "read_t1_leases", boom)
    dirty, _, _, _ = _cleanup(run, monkeypatch, tmp_path)
    assert "T1 session leases not read before the kill: RuntimeError: disk gone" in dirty
    assert any(d.startswith("teardown unexamined") for d in dirty)


def test_cleanup_with_an_older_nx_skips_the_findings_loudly_and_is_unverified(run, tmp_path, monkeypatch) -> None:
    """Below 7.97.0 nx-mcp does not tear down on SIGHUP: the gate says it cannot judge, not that it failed."""
    dirty, notes, _, _ = _cleanup(run, monkeypatch, tmp_path, version="7.96.1", revokes=False, removes_lease=False,
                                  holds_directory=True)
    assert dirty == []
    assert len(run.UNVERIFIED) == 1 and "below 7.97.0" in run.UNVERIFIED[0] and run.UNVERIFIED[0].startswith("gate:")
    assert any(n.startswith("UNVERIFIED: nx-mcp's teardown is NOT judged") for n in notes)


def _main_with(run, monkeypatch, *, bad: bool, unverified: bool):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "x")
    monkeypatch.setattr(run.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(sys, "argv", ["run.py"])
    crs = types.SimpleNamespace(script_sha256=lambda _s: "s", claude_version=lambda _c: "1", tmux_version=lambda: "t",
                                harness_sha256=lambda *a: "h")
    monkeypatch.setattr(run, "load_script", lambda: crs)
    recorded = []

    def scenario(name, claude, a, crs):
        if unverified:
            run.UNVERIFIED.append(f"{name}: installed nx is 7.96.0, below 7.97.0")
        return (not bad, ["boom"] if bad else [], [])

    monkeypatch.setattr(run, "run_scenario", scenario)
    monkeypatch.setattr(run, "record_full_pass", lambda *a: recorded.append(a) or "recorded")
    saved = {s: run.signal.getsignal(s) for s in (run.signal.SIGTERM, run.signal.SIGHUP)}
    try:
        return run.main(), recorded
    finally:
        for s, h in saved.items():
            run.signal.signal(s, h)


def test_main_exits_unverified_and_records_no_pass_when_the_teardown_was_not_judged(run, monkeypatch, capsys) -> None:
    code, recorded = _main_with(run, monkeypatch, bad=False, unverified=True)
    out = capsys.readouterr().out
    assert code == 2 and recorded == []
    assert "RESTART SANDBOX UNVERIFIED -- nx-mcp's teardown was not judged" in out
    assert "RESTART SANDBOX PASSED" not in out  # the verdict line itself must not say PASSED
    assert out.startswith("RESTART SANDBOX UNVERIFIED -- ")


def test_a_keep_run_is_unverified_not_a_pass(run, tmp_path, monkeypatch) -> None:
    """--keep skips cleanup(), the only place the teardown is judged: the scenario passes, the run is
    unverified, so main() exits 2 and records nothing."""
    home = tmp_path / "kept"
    home.mkdir()
    monkeypatch.setattr(run.tempfile, "mkdtemp", lambda prefix="": str(home))
    monkeypatch.setattr(run.Sandbox, "setup", lambda self, crs: None)
    monkeypatch.setattr(run.Sandbox, "entries", lambda self: [])
    monkeypatch.setattr(run, "RUNNERS", {"gate": lambda sb, crs, a, failures, notes: None})
    monkeypatch.setattr(run, "footprint_before", lambda: {})
    run.UNVERIFIED.clear()
    args = types.SimpleNamespace(keep=True, model="sonnet")
    ok, failures, _ = run.run_scenario("gate", "claude", args, None)
    assert ok and failures == []
    assert len(run.UNVERIFIED) == 1 and run.UNVERIFIED[0].startswith("gate: --keep skips cleanup")
    run.UNVERIFIED.clear()


def test_main_still_fails_a_failed_scenario_and_passes_a_judged_one(run, monkeypatch) -> None:
    assert _main_with(run, monkeypatch, bad=True, unverified=True)[0] == 1
    code, recorded = _main_with(run, monkeypatch, bad=False, unverified=False)
    assert code == 0 and len(recorded) == 1


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


# -- kill-server hangs up a child process group (the tmux half of the teardown) ---------------------

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


def test_the_hangup_probe_never_touches_the_default_tmux_server() -> None:
    text = RUN_PY.read_text(encoding="utf-8")
    body = text[text.index("def tmux_hangup_probe"):text.index("def hangup_check_main")]
    assert '"-L", sock' in body and '"-f", "/dev/null"' in body
    assert "kill-server" in body
    # every tmux call goes through the local helper that carries -L; there is no bare ["tmux", ...] elsewhere
    assert body.count('["tmux"') == 1


def test_the_docstring_says_what_the_hangup_check_does_not_show() -> None:
    doc = RUN_PY.read_text(encoding="utf-8").split('"""')[1]
    assert "not the real claude -> uv ->\nnx-mcp chain" in doc
    assert "however it was triggered" in doc and "stdin EOF" in doc


@needs_tmux
def test_the_hangup_check_command_passes_without_claude_or_a_token() -> None:
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDE_CODE_OAUTH_TOKEN", "TMUX")}
    r = subprocess.run([sys.executable, "-I", str(RUN_PY), "--hangup-check"], capture_output=True, text=True,
                       env=env, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "TMUX HANGUP CHECK PASSED" in r.stdout
