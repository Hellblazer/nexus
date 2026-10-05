# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every ``os.kill(<pid>, 0)`` liveness probe in ``src/nexus`` lives in
``nexus.daemon.service_registry.pid_alive`` (RDR-224, nexus-f9bgu.25).

On Windows ``os.kill(pid, 0)`` is not a probe: CPython sends
``CTRL_C_EVENT`` to the target's console process group. ``pid_alive`` is
the one place with a Windows branch, so a raw probe anywhere else is a
Windows bug, however correct it is on POSIX. ``src/nexus/daemon/AGENTS.md``
states that ``pid_alive`` is the single definition; this file keeps that
true.

The scan resolves the names a module binds to ``os`` (``import os``,
``import os as _os``) and to ``os.kill`` (``from os import kill``), so an
aliased import is not a blind spot. A signal argument other than the
literal ``0`` is not a probe and is not this file's subject.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

pytestmark = pytest.mark.lint

REPO_ROOT = pathlib.Path(__file__).parent.parent
SRC_ROOT = REPO_ROOT / "src" / "nexus"

#: (path relative to src/, enclosing function) of every allowed probe.
#: Exactly one entry, and the scan must find it (non-vacuity).
_ALLOWED: frozenset[tuple[str, str]] = frozenset({
    ("nexus/daemon/service_registry.py", "pid_alive"),
})


class _ProbeFinder(ast.NodeVisitor):
    def __init__(self) -> None:
        self.os_names: set[str] = set()
        self.kill_names: set[str] = set()
        self._scope: list[str] = []
        self.hits: list[tuple[str, int]] = []

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.name == "os":
                self.os_names.add(alias.asname or "os")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module == "os":
            for alias in node.names:
                if alias.name == "kill":
                    self.kill_names.add(alias.asname or "kill")
        self.generic_visit(node)

    def _visit_scope(self, node: ast.AST, name: str) -> None:
        self._scope.append(name)
        self.generic_visit(node)
        self._scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_scope(node, node.name)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_scope(node, node.name)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._visit_scope(node, node.name)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        is_kill = (
            isinstance(func, ast.Attribute)
            and func.attr == "kill"
            and isinstance(func.value, ast.Name)
            and func.value.id in self.os_names
        ) or (isinstance(func, ast.Name) and func.id in self.kill_names)
        if (
            is_kill
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value == 0
        ):
            self.hits.append((self._scope[-1] if self._scope else "<module>", node.lineno))
        self.generic_visit(node)


def _probes_in(source: str) -> list[tuple[str, int]]:
    tree = ast.parse(source)
    finder = _ProbeFinder()
    # Imports are collected in a first pass so a function-local
    # ``import os`` (aspect_worker.py does this) still binds the name for
    # calls that appear before it in walk order.
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            finder.visit(node)
    finder.visit(tree)
    return finder.hits


def _scan() -> dict[tuple[str, str], list[int]]:
    found: dict[tuple[str, str], list[int]] = {}
    for path in sorted(SRC_ROOT.rglob("*.py")):
        rel = path.relative_to(SRC_ROOT.parent).as_posix()
        for scope, line in _probes_in(path.read_text(encoding="utf-8")):
            found.setdefault((rel, scope), []).append(line)
    return found


def test_signal_zero_probes_live_only_in_pid_alive() -> None:
    found = _scan()
    assert set(_ALLOWED) <= set(found), (
        "the scan did not find pid_alive's own os.kill(pid, 0); the detector "
        f"is broken or pid_alive moved. Found: {found}"
    )
    stray = {k: v for k, v in found.items() if k not in _ALLOWED}
    assert not stray, (
        "raw os.kill(<pid>, 0) outside nexus.daemon.service_registry.pid_alive "
        "(on Windows it sends CTRL_C_EVENT); call pid_alive instead: "
        + ", ".join(f"{p}:{lines} in {s}" for (p, s), lines in sorted(stray.items()))
    )


@pytest.mark.parametrize(
    "source",
    [
        "import os\ndef f(pid):\n    os.kill(pid, 0)\n",
        "import os as _os\ndef f(pid):\n    _os.kill(pid, 0)\n",
        "from os import kill\ndef f(pid):\n    kill(pid, 0)\n",
        "from os import kill as k\ndef f(p):\n    k(p.pid, 0)\n",
        "def f(pid):\n    import os\n    os.kill(pid, 0)\n",
        "import os\nos.kill(1, 0)\n",
    ],
)
def test_planted_probe_is_detected(source: str) -> None:
    assert _probes_in(source), source


@pytest.mark.parametrize(
    "source",
    [
        "import os, signal\ndef f(pid):\n    os.kill(pid, signal.SIGTERM)\n",
        "import os\ndef f(pid):\n    os.kill(pid, 15)\n",
        "def f(proc):\n    proc.kill(0)\n",
    ],
)
def test_non_probe_is_ignored(source: str) -> None:
    assert not _probes_in(source), source
