# SPDX-License-Identifier: AGPL-3.0-or-later
"""The restart sandbox records a full pass only for the script bytes and Claude Code version it ran."""
from __future__ import annotations

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
