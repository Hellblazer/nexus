# SPDX-License-Identifier: AGPL-3.0-or-later
"""The restart sandbox records a full pass only for the script bytes and Claude Code version it ran."""
from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

RUN_PY = Path(__file__).resolve().parents[1] / "e2e" / "restart-sessions" / "run.py"


def _run_module():
    spec = importlib.util.spec_from_file_location("restart_sandbox_run_pass", RUN_PY)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _crs(tmp_path, sha: str, version: str | None, *, fail_write: bool = False):
    written: list[tuple] = []

    def record(path, s, v, chosen, elapsed):
        if fail_write:
            raise PermissionError("read-only")
        written.append((path, s, v, list(chosen)))

    return SimpleNamespace(script_sha256=lambda *_: sha, claude_version=lambda *_: version,
                           record_sandbox_pass=record, SANDBOX_PASS=tmp_path / "sandbox-pass.json"), written


def test_an_unchanged_run_is_recorded_for_what_ran(tmp_path) -> None:
    crs, written = _crs(tmp_path, "a" * 64, "2.1.316")
    line = _run_module().record_full_pass(crs, "claude", ("a" * 64, "2.1.316"), ["s1"], 1.0)
    assert line.startswith("recorded the pass")
    assert written == [(tmp_path / "sandbox-pass.json", "a" * 64, "2.1.316", ["s1"])]


def test_a_script_changed_during_the_run_is_not_recorded(tmp_path) -> None:
    crs, written = _crs(tmp_path, "b" * 64, "2.1.316")
    line = _run_module().record_full_pass(crs, "claude", ("a" * 64, "2.1.316"), ["s1"], 1.0)
    assert line.startswith("pass NOT recorded: the script or Claude Code changed") and written == []


def test_a_claude_update_during_the_run_is_not_recorded(tmp_path) -> None:
    crs, written = _crs(tmp_path, "a" * 64, "2.1.317")
    line = _run_module().record_full_pass(crs, "claude", ("a" * 64, "2.1.316"), ["s1"], 1.0)
    assert line.startswith("pass NOT recorded: the script or Claude Code changed") and written == []


def test_an_unreadable_version_is_not_recorded(tmp_path) -> None:
    crs, written = _crs(tmp_path, "a" * 64, None)
    line = _run_module().record_full_pass(crs, "claude", ("a" * 64, None), ["s1"], 1.0)
    assert line.startswith("pass NOT recorded: could not read") and written == []


def test_a_failed_write_is_reported_not_raised(tmp_path) -> None:
    crs, _ = _crs(tmp_path, "a" * 64, "2.1.316", fail_write=True)
    line = _run_module().record_full_pass(crs, "claude", ("a" * 64, "2.1.316"), ["s1"], 1.0)
    assert line.startswith("pass NOT recorded: could not write")


def test_only_a_full_clean_run_records_a_pass() -> None:
    run = _run_module()
    every = list(run.SCENARIOS)
    assert run.records_a_pass(every, [])
    assert not run.records_a_pass(every, [every[0]])  # a failed scenario
    assert not run.records_a_pass(every[:-1], [])  # a partial run
    assert not run.records_a_pass(list(reversed(every)), [])  # not the order the gate was proven in


def _function(name: str, cls: str | None = None) -> ast.FunctionDef:
    tree = ast.parse(RUN_PY.read_text(encoding="utf-8"))
    scope = tree.body
    if cls:
        scope = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == cls).body
    return next(n for n in scope if isinstance(n, ast.FunctionDef) and n.name == name)


def test_main_reads_what_it_tests_before_the_first_scenario() -> None:
    """The (sha, version) a pass is recorded for is read before the scenario loop, so a change
    during the run is caught by record_full_pass's re-read instead of being recorded as tested."""
    main = _function("main")
    tested = next(n for n in ast.walk(main) if isinstance(n, ast.Assign)
                  and any(isinstance(t, ast.Name) and t.id == "tested" for t in n.targets))
    loop = next(n for n in ast.walk(main) if isinstance(n, ast.For)
                and "run_scenario" in ast.unparse(n))
    assert tested.lineno < loop.lineno
    assert "script_sha256" in ast.unparse(tested.value) and "claude_version" in ast.unparse(tested.value)
    assert "records_a_pass(" in ast.unparse(main)


def test_each_sandbox_home_is_seeded_with_a_pass_before_any_session_starts() -> None:
    """Every roll in the sandbox goes through the script's gate and must be let by it."""
    setup = _function("setup", "Sandbox")
    calls = [ast.unparse(n.func) for n in ast.walk(setup) if isinstance(n, ast.Call)]
    assert "crs.record_sandbox_pass" in calls
    assert calls.index("crs.record_sandbox_pass") < calls.index("self.tmux")
