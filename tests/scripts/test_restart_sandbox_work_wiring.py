# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""run.py is wired to sandbox_work.py (RDR-253 Step 6, nexus-hczdo.1.9).

tests/scripts/test_restart_sandbox_work.py tests the sibling module alone, so reverting run.py to a bare
``sb.tmux("kill-server")`` left every test green. Two checks close that: the sandbox's start registers its private tmux
server right after it creates it, and its cleanup stops the server through ``sandbox_work.kill_server`` (staged with the
fixtures of test_restart_sandbox_teardown.py: a real cleanup against a fake nx and a fake nx-mcp)."""
from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

import pytest

from test_restart_sandbox_teardown import _cleanup

RUN_PY = Path(__file__).resolve().parents[1] / "e2e" / "restart-sessions" / "run.py"


@pytest.fixture(scope="module")
def run():
    spec = importlib.util.spec_from_file_location("restart_sandbox_run_work_wiring", RUN_PY)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_the_sandbox_start_registers_its_private_tmux_server_after_creating_it() -> None:
    """The registration is in the function that creates the server, after the ``new-session`` and with the run's own nx and socket."""
    tree = ast.parse(RUN_PY.read_text(encoding="utf-8"))
    starts = [fn for cls in tree.body if isinstance(cls, ast.ClassDef) and cls.name == "Sandbox"
              for fn in cls.body if isinstance(fn, ast.FunctionDef) and "new-session" in ast.unparse(fn)]
    assert len(starts) == 1, "exactly one Sandbox method creates the server"
    source = ast.unparse(starts[0])
    assert "_load_slash('sandbox_work').register(self.real_nx, self.sock)" in source
    assert source.index("new-session") < source.index("sandbox_work")


def test_cleanup_stops_the_server_through_the_sibling_guard(run, tmp_path, monkeypatch) -> None:
    seen: list[tuple[str, object]] = []
    real = run._load_slash("sandbox_work")

    class Recorder:
        """The sibling module, with its kill_server observed; every other name is the real one."""

        def kill_server(self, sb, notes):
            seen.append(("kill_server", sb.sock))
            return real.kill_server(sb, notes)

        def __getattr__(self, name):
            return getattr(real, name)

    original = run._load_slash
    monkeypatch.setattr(run, "_load_slash", lambda name="slash_restart": Recorder() if name == "sandbox_work" else original(name))
    dirty, _notes, _saw, _cfg = _cleanup(run, monkeypatch, tmp_path)
    assert seen == [("kill_server", "nx-rs-test-td")], "cleanup asks the guard, once, for the sandbox's own socket"
    assert dirty == []
