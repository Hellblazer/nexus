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


def test_signals_are_ignored_during_cleanup_and_restored_after(run) -> None:
    def mine(*_):
        raise AssertionError("a signal reached the handler during cleanup")

    saved = {s: signal.getsignal(s) for s in run.CLEANUP_SIGNALS}
    try:
        for s in run.CLEANUP_SIGNALS:
            signal.signal(s, mine)
        with run.signals_ignored():
            for s in run.CLEANUP_SIGNALS:
                assert signal.getsignal(s) is signal.SIG_IGN
            os.kill(os.getpid(), signal.SIGHUP)  # ignored: no AssertionError
            time.sleep(0.05)
        assert all(signal.getsignal(s) is mine for s in run.CLEANUP_SIGNALS)
    finally:
        for s, h in saved.items():
            signal.signal(s, h)
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
        assert run._end([(p, a) for p, a in watch.seen() if p == gc]) == 1
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


def test_run_scenario_cleans_up_with_signals_ignored() -> None:
    """The finally collects session ids and runs cleanup inside signals_ignored(): a signal there
    would raise SystemExit past the kill-server and the config deletion."""
    fn = _function("run_scenario")
    final = next(n for n in ast.walk(fn) if isinstance(n, ast.Try) and n.finalbody).finalbody
    guard = next(s for s in final if isinstance(s, ast.With)
                 and ast.unparse(s.items[0].context_expr) == "signals_ignored()")
    inside = [ast.unparse(c.func) for c in ast.walk(guard) if isinstance(c, ast.Call)]
    assert "cleanup" in inside and "sb.entries" in inside
    before = final[:final.index(guard)]
    assert not before, "nothing in the finally may run before the signal guard"


def test_cleanup_ends_what_the_watch_recorded() -> None:
    lines: dict[str, int] = {}
    for c in ast.walk(_function("cleanup")):
        if isinstance(c, ast.Call):
            name = ast.unparse(c.func)
            lines[name] = min(lines.get(name, c.lineno), c.lineno)
    assert "sb.watch.seen" in lines and "sb.watch.stop" in lines
    assert lines["sb.watch.seen"] < lines["sb.tmux"]  # read before the kill-server
