# SPDX-License-Identifier: AGPL-3.0-or-later
"""The restart sandbox leaves nothing behind (nexus-xwijj Phase 1).

A kept HOME on 2026-11-02 still held ``t1_session_lease.*`` files (each carries a T1 token);
sessions spawned ``nx upgrade --auto`` and the lockstep hook could reach ``nx upgrade``;
cleanup could be cut short by a second signal; and Serena and SessionEnd grandchildren of a
session that exited mid-roll were reparented to init with no HOME in their argv, so neither
the pre-kill descendant walk nor the HOME sweep found them.
"""
from __future__ import annotations

import ast
import importlib.util
import shutil
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

RUN_PY = Path(__file__).resolve().parents[1] / "e2e" / "restart-sessions" / "run.py"


@pytest.fixture(scope="module")
def run():
    spec = importlib.util.spec_from_file_location("restart_sandbox_run_footprint", RUN_PY)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # its dataclasses look their module up there
    spec.loader.exec_module(module)
    return module


def _sandbox(run, home: Path):
    return run.Sandbox(home, "nx-rs-test", "claude", "sonnet", run.SPECS["gate"])


def test_delete_config_removes_t1_session_leases(run, tmp_path) -> None:
    sb = _sandbox(run, tmp_path)
    sb.nexus_config.mkdir(parents=True)
    for name in ("config.yml", "data_token_lease.a", "t1_session_lease.abc", "t1_session_lease.def", "keep.txt"):
        (sb.nexus_config / name).write_text("x", encoding="utf-8")
    sb.delete_config()
    assert sorted(p.name for p in sb.nexus_config.iterdir()) == ["keep.txt"]


def test_credentials_left_names_a_lease(run, tmp_path) -> None:
    sb = _sandbox(run, tmp_path)
    sb.nexus_config.mkdir(parents=True)
    (sb.nexus_config / "t1_session_lease.abc").write_text("x", encoding="utf-8")
    (sb.nexus_config / "mcp_connect_marker.abc").write_text("x", encoding="utf-8")
    assert run.credentials_left(sb.nexus_config) == ["t1_session_lease.abc"]


def test_sessions_run_with_the_auto_upgrade_kill_switch(run, tmp_path) -> None:
    assert _sandbox(run, tmp_path).env["NX_UPGRADE_AUTO"] == "0"


def test_sessions_do_not_get_the_operator_s_tools_dir(run, tmp_path, monkeypatch) -> None:
    """With NX_TOOLS_DIR set, the hooks resolve nx in the operator's generation, past the stand-in."""
    monkeypatch.setenv("NX_TOOLS_DIR", str(tmp_path / "operator-tools"))
    assert "NX_TOOLS_DIR" not in _sandbox(run, tmp_path).env


def _shim(run, tmp_path: Path) -> tuple[Path, Path, Path]:
    real = tmp_path / "real-nx"
    real.write_text('#!/bin/sh\necho "real $*"\n', encoding="utf-8")
    real.chmod(0o755)
    marker, refused = tmp_path / "installed", tmp_path / "nx-refused.log"
    shim = tmp_path / "nx"
    shim.write_text(run.nx_shim_text(str(real), marker, refused), encoding="utf-8")
    shim.chmod(0o755)
    return shim, marker, refused


@pytest.mark.parametrize("argv", [["upgrade"], ["upgrade", "--auto"], ["-v", "upgrade", "--yes"]])
def test_the_nx_shim_refuses_upgrade_and_records_it(run, tmp_path, argv) -> None:
    shim, _, refused = _shim(run, tmp_path)
    r = subprocess.run([str(shim), *argv], capture_output=True, text=True)
    assert r.returncode == 97
    assert "real" not in r.stdout
    assert refused.read_text(encoding="utf-8").strip() == " ".join(argv)


def test_the_nx_shim_passes_other_verbs_and_reports_the_installed_version(run, tmp_path) -> None:
    shim, marker, refused = _shim(run, tmp_path)
    assert subprocess.run([str(shim), "search", "upgrade"], capture_output=True, text=True).stdout.strip() == "real search upgrade"
    assert subprocess.run([str(shim), "--version"], capture_output=True, text=True).stdout.strip() == "real --version"
    marker.write_text("9.9.9\n", encoding="utf-8")
    assert subprocess.run([str(shim), "--version"], capture_output=True, text=True).stdout.strip() == "nx, version 9.9.9"
    assert not refused.exists()


def test_signals_are_held_during_cleanup_then_acted_on(run) -> None:
    """A signal during cleanup neither cuts it short nor is lost: it is recorded, the handlers set
    before come back, and raise_deferred exits as main()'s handler would have."""
    def mine(*_):
        raise AssertionError("a signal reached the handler during cleanup")

    saved = {s: signal.getsignal(s) for s in run.CLEANUP_SIGNALS}
    try:
        for s in run.CLEANUP_SIGNALS:
            signal.signal(s, mine)
        with run.signals_deferred() as pending:
            os.kill(os.getpid(), signal.SIGHUP)  # held: no AssertionError
            os.kill(os.getpid(), signal.SIGTERM)
            time.sleep(0.05)
            reached_end = True
        assert reached_end
        assert pending == [signal.SIGHUP, signal.SIGTERM]
        assert all(signal.getsignal(s) is mine for s in run.CLEANUP_SIGNALS)
    finally:
        for s, h in saved.items():
            signal.signal(s, h)
    with pytest.raises(SystemExit) as exc:
        run.raise_deferred(pending)
    assert exc.value.code == 129
    with pytest.raises(KeyboardInterrupt):
        run.raise_deferred([signal.SIGINT])
    run.raise_deferred([])  # nothing held: returns
    assert set(run.CLEANUP_SIGNALS) == {signal.SIGTERM, signal.SIGHUP, signal.SIGINT}


def test_tree_of_walks_every_descendant(run) -> None:
    rows = "\n".join(["  10     1 tmux -L x", "  11    10 claude a", "  12    11 serena --x",
                      "  13    12 python hook", "  20     1 other", "  21    20 other child", "garbage"])
    assert sorted(run.tree_of(rows, 10)) == [(11, "claude a"), (12, "serena --x"), (13, "python hook")]


def test_proc_watch_records_a_grandchild_that_outlives_its_parent(run, tmp_path) -> None:
    """A child that exits after starting a grandchild: the grandchild is reparented and its argv
    names nothing of the sandbox, so only a record taken while the tree was whole finds it."""
    pidfile = tmp_path / "gc.pid"
    parent = subprocess.Popen([sys.executable, "-c",
                               "import subprocess, sys, time\n"
                               "p = subprocess.Popen(['sleep', '307'], start_new_session=True)\n"
                               f"open({str(pidfile)!r}, 'w').write(str(p.pid))\n"
                               "time.sleep(1.5)\n"])
    watch = run.ProcWatch(lambda: [parent.pid], interval=0.2)
    watch.start()
    try:
        parent.wait(timeout=10)
        gc = int(pidfile.read_text(encoding="utf-8"))
        time.sleep(0.5)
        watch.stop()
        assert any(p == gc and "sleep 307" in args for p, args in watch.seen())
        assert [p for p, _ in watch.still_running()] == [gc]
        assert run._end([(p, a) for p, a in watch.still_running() if p == gc]) == 1
        for _ in range(50):
            try:
                os.kill(gc, 0)
            except ProcessLookupError:
                break
            if subprocess.run(["ps", "-o", "stat=", "-p", str(gc)], capture_output=True, text=True).stdout.strip().startswith("Z"):
                break
            time.sleep(0.1)
        else:
            pytest.fail("the recorded grandchild survived _end")
    finally:
        watch.stop()
        try:
            os.kill(int(pidfile.read_text(encoding="utf-8")), signal.SIGKILL)
        except (OSError, ValueError):
            pass


def _function(name: str):
    tree = ast.parse(RUN_PY.read_text(encoding="utf-8"))
    return next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name)


def test_run_scenario_cleans_up_with_signals_deferred() -> None:
    """The finally collects session ids and runs cleanup inside signals_deferred() (a signal there
    would raise SystemExit past the kill-server and the config deletion) and ends by acting on
    what it held."""
    fn = _function("run_scenario")
    final = next(n for n in ast.walk(fn) if isinstance(n, ast.Try) and n.finalbody).finalbody
    guard = next(s for s in final if isinstance(s, ast.With)
                 and ast.unparse(s.items[0].context_expr) == "signals_deferred()")
    inside = [ast.unparse(c.func) for c in ast.walk(guard) if isinstance(c, ast.Call)]
    assert "cleanup" in inside and "sb.entries" in inside
    before = final[:final.index(guard)]
    assert not before, "nothing in the finally may run before the signal guard"
    last = final[-1]
    assert isinstance(last, ast.Expr) and ast.unparse(last.value) == "raise_deferred(pending)"


def _calls(name: str) -> list[str]:
    return [ast.unparse(c.func) for c in ast.walk(_function(name)) if isinstance(c, ast.Call)]


def test_setup_starts_the_watch_and_installs_the_nx_stand_in() -> None:
    assert "self.watch.start" in _calls("setup")
    assert "nx_shim_text" in _calls("setup_release")


class _Watch:
    """A ProcWatch stand-in holding what a run recorded."""

    def __init__(self, procs: list[tuple[int, str]]) -> None:
        self.procs = procs

    def poll(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def seen(self) -> list[tuple[int, str]]:
        return list(self.procs)

    def still_running(self) -> list[tuple[int, str]]:
        return [(p, a) for p, a in self.procs if run_module_args(p) == a]


def run_module_args(pid: int) -> str:
    return subprocess.run(["ps", "-ww", "-o", "args=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()


def _cleanup_sandbox(run, tmp_path: Path, procs: list[tuple[int, str]]):
    sb = _sandbox(run, tmp_path)
    sb.nexus_config.mkdir(parents=True)
    sb.watch = _Watch(procs)
    sb.before = {"rs_alpha": {"sessionId": "x"}}
    sb.tmux = lambda *a, **k: subprocess.CompletedProcess(a, 1, "", "")  # no server: nothing to kill
    return sb


def test_cleanup_ends_a_recorded_process_and_reports_a_lease(run, tmp_path) -> None:
    """The cleanup path end to end, without tmux: a process the watch recorded (its argv names
    nothing of the HOME) is ended, and a lease written after the config deletion is dirty."""
    proc = subprocess.Popen(["sleep", "311"], start_new_session=True)
    try:
        sb = _cleanup_sandbox(run, tmp_path, [(proc.pid, run_module_args(proc.pid))])
        (sb.nexus_config / "t1_session_lease.abc").write_text("x", encoding="utf-8")
        real_delete = sb.delete_config
        calls = []

        def delete_then_leak() -> None:  # a lingering process mints a lease after the last deletion
            real_delete()
            calls.append(1)
            if len(calls) == 2:
                (sb.nexus_config / "t1_session_lease.late").write_text("x", encoding="utf-8")

        sb.delete_config = delete_then_leak
        notes: list[str] = []
        dirty = run.cleanup(sb, notes)
        assert proc.wait(timeout=10) is not None
        assert dirty == [f"credentials left in {sb.nexus_config}: ['t1_session_lease.late']"]
        assert not (sb.nexus_config / "t1_session_lease.abc").exists()
        assert any("1 process(es) recorded below the tmux server" in n for n in notes)
    finally:
        if proc.poll() is None:
            proc.kill()


def test_cleanup_fails_a_run_whose_watch_recorded_nothing(run, tmp_path) -> None:
    dirty = run.cleanup(_cleanup_sandbox(run, tmp_path, []), [])
    assert dirty == ["the process watch recorded nothing below the tmux server although sessions ran"]


def test_a_reused_pid_is_not_still_running(run) -> None:
    """A pid recorded with one start time and seen now with another is left out."""
    proc = subprocess.Popen(["sleep", "313"])
    try:
        watch = run.ProcWatch(lambda: [], interval=60)
        args = run_module_args(proc.pid)
        watch._seen[proc.pid] = ("Thu Jan  1 00:00:00 1970", args)
        assert watch.still_running() == []
        watch._seen[proc.pid] = (run.started_at([proc.pid])[proc.pid], args)
        assert watch.still_running() == [(proc.pid, args)]
    finally:
        proc.kill()
        proc.wait()


@pytest.mark.skipif(not shutil.which("tmux"), reason="tmux not installed")
def test_server_pids_names_the_private_tmux_server(run, tmp_path) -> None:
    sb = _sandbox(run, tmp_path)
    sb.sock = f"nx-rs-test-{os.getpid()}"
    assert sb.server_pids() == []  # no server: none, and none started
    sb.tmux("new-session", "-d", "-s", "rs", "sleep 60", check=True)
    try:
        pids = sb.server_pids()
        assert len(pids) == 1
        assert "tmux" in run_module_args(pids[0])
        assert sb.watch is not None
        watch = run.ProcWatch(sb.server_pids, interval=60)
        watch.poll()
        assert any("sleep 60" in a for _, a in watch.seen())
    finally:
        sb.tmux("kill-server")
    assert sb.server_pids() == []
