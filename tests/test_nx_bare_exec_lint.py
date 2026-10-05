# SPDX-License-Identifier: AGPL-3.0-or-later
"""No bare ``"nx"`` or ``"schtasks"`` argv[0] in ``src/nexus`` (RDR-224,
nexus-f9bgu.33, review S1).

On Windows ``CreateProcess`` searches the current directory before
``System32``, and ``shutil.which`` prepends it, so a bare name is a binary
planting hole: an ``nx.exe`` / ``schtasks.exe`` in a cloned repository runs as
the user. The sanctioned spellings are:

* ``nexus.util.nx_argv.nx_argv(...)`` for ``nx`` (an absolute interpreter plus
  ``-m nexus.cli`` on Windows; the one place the bare ``"nx"`` literal lives),
* the manager-command builders in ``nexus/daemon/installer.py`` for
  ``schtasks``, whose lists reach a spawn only through ``_run_manager`` ->
  ``_manager_executable``, which resolves ``schtasks`` to
  ``%SystemRoot%\\System32``.

The scan reads list and tuple literals whose first element is the bare name.
Non-vacuity: it must find both sanctioned spellings, and the installer
resolver must still route ``schtasks`` through the Windows path helper with
``_run_manager`` the only function there that spawns.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

pytestmark = pytest.mark.lint

REPO_ROOT = pathlib.Path(__file__).parent.parent
SRC_ROOT = REPO_ROOT / "src" / "nexus"

_BARE_NAMES = frozenset({"nx", "nx.exe", "schtasks", "schtasks.exe"})

_NX_ARGV = "nexus/util/nx_argv.py"
_INSTALLER = "nexus/daemon/installer.py"

#: (path relative to src/, enclosing function, name): every allowed bare argv[0].
_ALLOWED: frozenset[tuple[str, str, str]] = frozenset({
    (_NX_ARGV, "nx_argv", "nx"),
    (_INSTALLER, "_task_run_cmd", "schtasks"),
    (_INSTALLER, "_task_end_cmd", "schtasks"),
    (_INSTALLER, "_activate_cmd", "schtasks"),
    (_INSTALLER, "_deactivate_cmd", "schtasks"),
    (_INSTALLER, "_activation_query_cmd", "schtasks"),
    (_INSTALLER, "_windows_task_activation", "schtasks"),
    (_INSTALLER, "_windows_task_registered", "schtasks"),
})


class _Finder(ast.NodeVisitor):
    def __init__(self) -> None:
        self._scope: list[str] = []
        self.hits: list[tuple[str, str, int]] = []

    def _enter(self, node: ast.AST, name: str) -> None:
        self._scope.append(name)
        self.generic_visit(node)
        self._scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._enter(node, node.name)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._enter(node, node.name)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._enter(node, node.name)

    def _check(self, node: ast.List | ast.Tuple) -> None:
        if (
            node.elts
            and isinstance(node.elts[0], ast.Constant)
            and node.elts[0].value in _BARE_NAMES
        ):
            self.hits.append(
                (self._scope[-1] if self._scope else "<module>", str(node.elts[0].value), node.lineno)
            )

    def visit_List(self, node: ast.List) -> None:
        self._check(node)
        self.generic_visit(node)

    def visit_Tuple(self, node: ast.Tuple) -> None:
        self._check(node)
        self.generic_visit(node)


def _hits_in(source: str) -> list[tuple[str, str, int]]:
    finder = _Finder()
    finder.visit(ast.parse(source))
    return finder.hits


def _scan() -> dict[tuple[str, str, str], list[int]]:
    found: dict[tuple[str, str, str], list[int]] = {}
    for path in sorted(SRC_ROOT.rglob("*.py")):
        rel = path.relative_to(SRC_ROOT.parent).as_posix()
        for scope, name, line in _hits_in(path.read_text(encoding="utf-8")):
            found.setdefault((rel, scope, name), []).append(line)
    return found


def test_no_bare_nx_or_schtasks_argv_outside_the_sanctioned_resolvers() -> None:
    found = _scan()
    assert set(_ALLOWED) <= set(found), (
        "the scan did not find a sanctioned bare argv[0]; the detector is broken "
        f"or a resolver moved. Missing: {sorted(set(_ALLOWED) - set(found))}"
    )
    stray = {k: v for k, v in found.items() if k not in _ALLOWED}
    assert not stray, (
        "a bare nx/schtasks argv[0] is resolved through the Windows current "
        "directory; use nexus.util.nx_argv.nx_argv (nx) or a manager command "
        "run through installer._run_manager (schtasks): "
        + ", ".join(f"{p}:{lines} in {s} ({n!r})" for (p, s, n), lines in sorted(stray.items()))
    )


def test_the_detector_sees_every_list_shape_it_claims_to_police() -> None:
    assert _hits_in('def f():\n    return ["nx", "x"]\n') == [("f", "nx", 2)]
    assert _hits_in('X = ("schtasks", "/Query")\n') == [("<module>", "schtasks", 1)]
    assert _hits_in('def f(a):\n    return [*a, "nx"]\n') == []  # not an argv[0]
    assert _hits_in('def f():\n    return ["python", "-m", "nx"]\n') == []


def _function(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found")


def test_installer_resolves_schtasks_through_the_windows_path_and_spawns_in_one_place() -> None:
    tree = ast.parse((SRC_ROOT / "daemon" / "installer.py").read_text(encoding="utf-8"))

    resolver = _function(tree, "_manager_executable")
    called = {
        n.func.id for n in ast.walk(resolver)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert "_windows_manager_path" in called, (
        "_manager_executable no longer resolves a Windows manager to System32"
    )
    # The Windows arm comes BEFORE the PATH lookup, so a planted binary never wins.
    first_which = min(
        n.lineno for n in ast.walk(resolver)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "which"
    )
    first_windows = min(
        n.lineno for n in ast.walk(resolver)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "_windows_manager_path"
    )
    assert first_windows < first_which

    spawners = {
        fn.name
        for fn in ast.walk(tree)
        if isinstance(fn, ast.FunctionDef)
        for n in ast.walk(fn)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id in {"run_bounded", "_REAL_RUN_BOUNDED"}
    }
    # _stop_service_stack_best_effort spawns the `nx` stop verb, whose argv comes
    # from _resolve_nx_bin (absolute on Windows); it is not a manager command.
    assert spawners == {"_run_manager", "_stop_service_stack_best_effort"}, (
        "a manager command is spawned outside _run_manager, so it skips "
        f"_manager_executable: {sorted(spawners)}"
    )
