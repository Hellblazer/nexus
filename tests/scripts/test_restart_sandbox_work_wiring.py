# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""run.py is wired to sandbox_work.py (RDR-253 Step 6, nexus-hczdo.1.9).

tests/scripts/test_restart_sandbox_work.py tests the sibling module alone, so reverting run.py to a bare
``sb.tmux("kill-server")`` left every test green. Two checks close that: the sandbox's start registers its private tmux
server right after it creates it, and its cleanup stops the server through ``sandbox_work.kill_server`` (staged with the
fixtures of test_restart_sandbox_teardown.py: a real cleanup against a fake nx and a fake nx-mcp)."""
from __future__ import annotations

import ast
import re
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


REPO = RUN_PY.parents[3]

#: Every file under tests/ and scripts/ that sends ``tmux kill-server`` without the guard, with how many times. Each site
#: carries a ``kill-server exempt`` comment within the three lines above it saying why (a private per-run socket, or a
#: throwaway container); a new site, or a site without its reason, fails the test below.
EXEMPT = {
    "tests/cc-validation/runner.sh": 2,
    "tests/cc-validation/connection-race-ladder/run_ladder.py": 5,
    "tests/e2e/hook-surface-shakeout/shakeout_in_container.sh": 1,
    "tests/e2e/run.sh": 1,
}
#: The guard itself: sandbox_work.kill_server sends the one guarded kill-server.
GUARD = "tests/e2e/restart-sessions/sandbox_work.py"
_SHELL_KILL = re.compile(r"(?:^\s*|[;&|]\s*)(?:_tmux|T|tmux(?:\s+-[LS]\s+\S+)?)\s+kill-server\b")


def _kill_server_lines(path: Path) -> list[int]:
    text = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix == ".py":
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return []
        return sorted(node.lineno for node in ast.walk(tree) if isinstance(node, ast.Call)
                      and any(isinstance(a, ast.Constant) and a.value == "kill-server" for a in node.args))
    return [n for n, line in enumerate(text.splitlines(), 1)
            if not line.lstrip().startswith("#") and _SHELL_KILL.search(line)]


def test_every_kill_server_under_tests_and_scripts_is_guarded_or_a_named_exemption() -> None:
    """RDR-253 Step 6 (nexus-hczdo.1.10 r1 item 2, confirmation C1): a tmux server is stopped through
    ``sandbox_work.kill_server`` (``nx identity may-stop`` first), or the site says in a ``kill-server exempt`` comment
    why no other identity can own it. Test files that only grep for the verb are not sites."""
    found: dict[str, list[int]] = {}
    for root in ("tests", "scripts"):
        for path in (REPO / root).rglob("*"):
            if not path.is_file() or path.suffix in (".md", ".txt", ".json", ".pyc") or path.name.startswith("test_") \
                    or path.name.endswith("_test.sh") or "__pycache__" in path.parts:
                continue
            lines = _kill_server_lines(path)
            if lines:
                found[path.relative_to(REPO).as_posix()] = lines
    assert found.pop(GUARD, None), "the guard's own kill-server was not found: the scan is blind"
    assert {k: len(v) for k, v in found.items()} == EXEMPT, "a kill-server outside the guard that is not a named exemption"
    for rel, lines in found.items():
        text = (REPO / rel).read_text(encoding="utf-8").splitlines()
        for n in lines:
            assert any("kill-server exempt" in text[i] for i in range(max(0, n - 4), n - 1)), f"{rel}:{n} gives no reason"


def test_the_cc_validation_and_ladder_sockets_are_per_run() -> None:
    """A fixed socket name let one session's run kill another's server ("stale server" reset at start); both now carry
    the run's pid (nexus-hczdo.1.10 confirmation C1)."""
    runner = (REPO / "tests/cc-validation/runner.sh").read_text(encoding="utf-8")
    assert re.search(r'^NX_TMUX_SOCKET="cc-val-sock-\$\$"', runner, re.M)
    assert re.search(r'^TEST_HOME="\$\{TMPDIR%/\}/nexus-cc-val-home-\$\$"', runner, re.M), "and its home, which cleanup removes"
    ladder = (REPO / "tests/cc-validation/connection-race-ladder/run_ladder.py").read_text(encoding="utf-8")
    assert 'default=f"veh77-ladder-{os.getpid()}"' in ladder
