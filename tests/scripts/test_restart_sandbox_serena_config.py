# SPDX-License-Identifier: AGPL-3.0-or-later
"""The restart sandbox gives each scratch HOME a Serena config that opens no dashboard.

Without one, every sandbox session's Serena wrote its default config, whose dashboard opens a
tab in the operator's browser: four to six tabs a second during a run (2026-11-01).
"""
from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

import yaml

RUN_PY = Path(__file__).resolve().parents[1] / "e2e" / "restart-sessions" / "run.py"


def _run_module():
    spec = importlib.util.spec_from_file_location("restart_sandbox_run", RUN_PY)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # its dataclasses look their module up there
    spec.loader.exec_module(module)
    return module


def test_the_config_turns_the_dashboard_and_log_window_off(tmp_path) -> None:
    path = _run_module().write_serena_config(tmp_path)
    assert path == tmp_path / ".serena" / "serena_config.yml"
    cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert cfg == {"projects": [], "web_dashboard": False, "web_dashboard_open_on_launch": False, "gui_log_window": False}


def test_setup_writes_it_first() -> None:
    """Sandbox.setup writes the config before it starts the tmux server any session runs in."""
    tree = ast.parse(RUN_PY.read_text(encoding="utf-8"))
    setup = next(n for c in ast.walk(tree) if isinstance(c, ast.ClassDef) and c.name == "Sandbox"
                 for n in c.body if isinstance(n, ast.FunctionDef) and n.name == "setup")
    calls = [ast.unparse(n.func) for n in ast.walk(setup) if isinstance(n, ast.Call)]
    assert "write_serena_config" in calls
    assert calls.index("write_serena_config") < calls.index("self.tmux")
