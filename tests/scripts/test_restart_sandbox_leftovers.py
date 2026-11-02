# SPDX-License-Identifier: AGPL-3.0-or-later
"""What a restart sandbox scenario leaves outside its scratch HOME (nexus-xwijj Phase 2).

The SessionEnd launcher's detached grandchild keeps the session's working directory (the
sandbox work folder) but names nothing of the HOME in its argv; a cwd scan finds it without
reading its environment, which holds the token. The footprint snapshot reports what a scenario
left in shared places, counting only entries that name the scenario.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

RUN_PY = Path(__file__).resolve().parents[1] / "e2e" / "restart-sessions" / "run.py"


@pytest.fixture(scope="module")
def run():
    spec = importlib.util.spec_from_file_location("restart_sandbox_run_leftovers", RUN_PY)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # its dataclasses look their module up there
    spec.loader.exec_module(module)
    return module


def test_parse_cwds(run) -> None:
    out = "p10\nfcwd\nn/a/b\np11\nfcwd\nn/c d\nn\npx\nnjunk\n"
    assert run.parse_cwds(out) == {10: "/a/b", 11: ""}


def test_under_matches_the_home_and_its_realpath(run, tmp_path) -> None:
    assert run.under(str(tmp_path / "work"), tmp_path)
    assert run.under(str(tmp_path), tmp_path)
    assert not run.under(str(tmp_path) + "-other/x", tmp_path)
    real = Path(tempfile.gettempdir()).resolve()
    assert run.under(str(real / "x"), Path(tempfile.gettempdir()))


def test_a_detached_process_working_in_the_home_is_found(run, tmp_path) -> None:
    """Its argv names nothing of the HOME; its working directory is the work folder."""
    work = tmp_path / "work"
    work.mkdir()
    proc = subprocess.Popen(["sleep", "317"], cwd=work, start_new_session=True)
    try:
        found = run._sandbox_procs(tmp_path)
        assert (proc.pid, "sleep 317") in found
        assert all(p != proc.pid for p, _ in run._naming(tmp_path))  # the argv sweep alone misses it
    finally:
        proc.kill()
        proc.wait()
    assert all(p != proc.pid for p, _ in run._sandbox_procs(tmp_path))


def _sandbox(run, home: Path):
    sb = run.Sandbox(home, "nx-rs-test-fp", "claude", "sonnet", run.SPECS["gate"])
    return sb


def test_footprint_counts_only_entries_naming_the_scenario(run, tmp_path, monkeypatch) -> None:
    temp = tmp_path / "shared-tmp"
    temp.mkdir()
    op = tmp_path / "operator"
    (op / ".claude" / "sessions").mkdir(parents=True)
    (op / ".config" / "nexus").mkdir(parents=True)
    (op / ".claude" / "restart-sessions.log").write_text("old line rs_alpha\n", encoding="utf-8")
    monkeypatch.setattr(run, "TEMP_DIRS", (temp,))
    monkeypatch.setattr(run, "OPERATOR_HOME", op)
    sb = _sandbox(run, tmp_path / "nx-restart-sandbox-gate-abc")
    before = run.footprint_before()
    sid = "1234abcd-0000-0000-0000-000000000000"
    (temp / "rh-1234abcd.ask").write_text("x", encoding="utf-8")
    (temp / "someone-elses-file").write_text("x", encoding="utf-8")
    (op / ".claude" / "sessions" / "9.json").write_text(json.dumps({"name": "rs_alpha"}), encoding="utf-8")
    (op / ".claude" / "sessions" / "10.json").write_text(json.dumps({"name": "wren"}), encoding="utf-8")
    with (op / ".claude" / "restart-sessions.log").open("a", encoding="utf-8") as fh:
        fh.write("new line rs_alpha asked\nnew line wren\n")
    (op / ".config" / "nexus" / "t1_session_lease.1234abcd-0000").write_text("x", encoding="utf-8")
    (op / ".config" / "nexus" / "t1_session_lease.ffffffff").write_text("x", encoding="utf-8")
    left = run.footprint_after(before, sb, {sid}, "")
    assert left == {f"temp {temp}": ["rh-1234abcd.ask"],
                    "operator ~/.claude/sessions": ["9.json"],
                    "operator restart log": ["new line rs_alpha asked"],
                    "operator nexus config": ["t1_session_lease.1234abcd-0000"]}


def test_footprint_is_empty_when_nothing_was_left(run, tmp_path, monkeypatch) -> None:
    temp = tmp_path / "shared-tmp"
    temp.mkdir()
    monkeypatch.setattr(run, "TEMP_DIRS", (temp,))
    monkeypatch.setattr(run, "OPERATOR_HOME", tmp_path / "operator")
    sb = _sandbox(run, tmp_path / "home")
    before = run.footprint_before()
    (temp / "unrelated").write_text("x", encoding="utf-8")
    assert run.footprint_after(before, sb, {"1234abcd-x"}, "") == {}
