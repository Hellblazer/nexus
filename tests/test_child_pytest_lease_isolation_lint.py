# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every child pytest a test spawns is isolated from the box's build lease (nexus-fam6l).

A child pytest is a full session: its ``pytest_sessionstart`` runs
``_gate_on_build_lease``, which reads the build lease every worktree on the
machine shares. Unless the child is told otherwise, a Maven run anywhere on the
box makes it exit 75 before it does what the parent test is checking, and the
parent reports a failure that has nothing to do with the code under test. That
is how ``test_sqlite_substrate_is_rejected_not_silently_upgraded`` went red in a
full run beside a worktree agent's engine suite, and two sibling spawners had
the same exposure.

The rule: the function that spawns the child mentions ``NX_BUILD_LEASE_ROOT``
(its own lease root) or sets ``NX_TEST_T2_SUBSTRATE`` to ``none`` (a run the gate
never checks), directly or through a module-level constant it references.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.lint

_TESTS = Path(__file__).resolve().parent

#: Spawners present when this lint landed. A scan that finds fewer is broken,
#: not clean.
_MIN_SPAWNERS = 9

_SPAWN_FUNCS = {"run", "Popen", "check_output", "check_call", "call"}


def _is_child_pytest(call: ast.Call) -> bool:
    func = call.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
    if name not in _SPAWN_FUNCS or not call.args or not isinstance(call.args[0], ast.List):
        return False
    consts = [e.value for e in call.args[0].elts if isinstance(e, ast.Constant)]
    return "pytest" in consts


def _isolates(text: str) -> bool:
    if "NX_BUILD_LEASE_ROOT" in text:
        return True
    return "NX_TEST_T2_SUBSTRATE" in text and '"none"' in text


def _spawners() -> list[tuple[Path, int, bool]]:
    found: list[tuple[Path, int, bool]] = []
    for path in sorted(_TESTS.rglob("*.py")):
        if path.name == Path(__file__).name:
            continue
        source = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        module_consts = {
            target.id: ast.get_source_segment(source, node) or ""
            for node in tree.body
            if isinstance(node, (ast.Assign, ast.AnnAssign))
            for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
            if isinstance(target, ast.Name)
        }
        for func in ast.walk(tree):
            if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            # The nearest enclosing function owns the call: skip calls that
            # belong to a function nested inside this one.
            nested = {
                id(n)
                for inner in ast.walk(func)
                if inner is not func and isinstance(inner, (ast.FunctionDef, ast.AsyncFunctionDef))
                for n in ast.walk(inner)
            }
            text = ast.get_source_segment(source, func) or ""
            referenced = {n.id for n in ast.walk(func) if isinstance(n, ast.Name)}
            text += "".join(module_consts[name] for name in referenced if name in module_consts)
            for node in ast.walk(func):
                if id(node) in nested or not isinstance(node, ast.Call) or not _is_child_pytest(node):
                    continue
                found.append((path.relative_to(_TESTS.parent), node.lineno, _isolates(text)))
    return found


def test_every_child_pytest_is_isolated_from_the_box_build_lease() -> None:
    spawners = _spawners()
    assert len(spawners) >= _MIN_SPAWNERS, (
        f"found {len(spawners)} child-pytest spawners, expected at least "
        f"{_MIN_SPAWNERS}: the scan no longer recognises how tests spawn pytest"
    )
    exposed = [f"{path}:{line}" for path, line, ok in spawners if not ok]
    assert not exposed, (
        "these tests spawn a child pytest that reads the machine's shared build "
        "lease, so any Maven run on the box turns them red (nexus-fam6l). Give "
        "the child env its own NX_BUILD_LEASE_ROOT, or NX_TEST_T2_SUBSTRATE=none "
        "if it needs no engine:\n  " + "\n  ".join(exposed)
    )
