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

The scan reads list and tuple literals whose first element is the bare name, or a
module-level constant holding it (``_NX = "nx"; [_NX, ...]``), and shell strings that
start with it (``run("nx ...", shell=True)``, ``os.system``). A second scan polices
LOOKUPS: a ``shutil.which("nx" | "git" | "bd" | "claude")`` whose result reaches an
argv (a list or tuple element, or an argument of a call that is not a display sink) is
the same hole by another road, because on Windows ``which`` returns the current
directory's hit first. The sanctioned spelling is ``nexus.util.nx_argv.which_off_cwd``
(or ``nx_argv``); a result used only to test existence or to print is not an argv and
is not flagged. ``_WHICH_ALLOWED`` carries each justified exception.
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


def _module_constants(tree: ast.Module) -> dict[str, str]:
    """``NAME = "<bare name>"`` at module level (and annotated ``NAME: str = ...``)."""
    out: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            for target in node.targets:
                if isinstance(target, ast.Name) and node.value.value in _BARE_NAMES:
                    out[target.id] = str(node.value.value)
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and isinstance(node.value, ast.Constant)
            and node.value.value in _BARE_NAMES
        ):
            out[node.target.id] = str(node.value.value)
    return out


def _leading_text(node: ast.AST) -> str | None:
    """The literal text a string expression starts with (a str constant, or the
    first piece of an f-string); ``None`` when it does not start with one."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr) and node.values:
        first = node.values[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            return first.value
    return None


def _shell_bare_name(text: str | None) -> str | None:
    """The bare name a shell string starts with (``"nx daemon stop"`` -> ``nx``)."""
    if not text:
        return None
    head = text.lstrip().split(None, 1)
    if head and head[0] in _BARE_NAMES:
        return head[0]
    return None


class _Finder(ast.NodeVisitor):
    def __init__(self, constants: dict[str, str] | None = None) -> None:
        self._scope: list[str] = []
        self._constants = constants or {}
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

    def _here(self) -> str:
        return self._scope[-1] if self._scope else "<module>"

    def _check(self, node: ast.List | ast.Tuple) -> None:
        if not node.elts:
            return
        first = node.elts[0]
        if isinstance(first, ast.Constant) and first.value in _BARE_NAMES:
            self.hits.append((self._here(), str(first.value), node.lineno))
        elif isinstance(first, ast.Name) and first.id in self._constants:
            self.hits.append((self._here(), self._constants[first.id], node.lineno))

    def visit_Call(self, node: ast.Call) -> None:
        # A shell string: resolved by the shell and CreateProcess the same way.
        callee = _callee_name(node)
        shell = any(
            kw.arg == "shell" and isinstance(kw.value, ast.Constant) and kw.value.value is True
            for kw in node.keywords
        )
        if node.args and (shell or (callee in {"system", "popen"} and _is_os(node))):
            name = _shell_bare_name(_leading_text(node.args[0]))
            if name is not None:
                self.hits.append((self._here(), name, node.lineno))
        self.generic_visit(node)

    def visit_List(self, node: ast.List) -> None:
        self._check(node)
        self.generic_visit(node)

    def visit_Tuple(self, node: ast.Tuple) -> None:
        self._check(node)
        self.generic_visit(node)


def _callee_name(node: ast.Call) -> str:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return ""


def _is_os(node: ast.Call) -> bool:
    return (
        isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "os"
    )


def _hits_in(source: str) -> list[tuple[str, str, int]]:
    tree = ast.parse(source)
    finder = _Finder(_module_constants(tree))
    finder.visit(tree)
    return finder.hits


# -- lookups whose result reaches an argv ---------------------------------------

_WHICH_NAMES = frozenset({"nx", "git", "bd", "claude"})

#: Callees that only format or test a lookup result; an argument here is not an argv.
#: ``HealthResult`` is ``nx doctor``'s report row: the path is printed, never spawned.
_DISPLAY_SINKS = frozenset({
    "bool", "str", "repr", "len", "print", "isinstance", "Path", "emit", "HealthResult",
})


def _is_which_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and _callee_name(node) == "which"
        and bool(node.args)
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value in _WHICH_NAMES
    )


def _which_name(node: ast.AST) -> str:
    assert isinstance(node, ast.Call) and isinstance(node.args[0], ast.Constant)
    return str(node.args[0].value)


def _which_reaching_argv(source: str) -> list[tuple[str, str, int]]:
    """``(scope, tool, line)`` for each ``which("<tool>")`` result that reaches an argv:
    as a list or tuple element, or as an argument of a call that is not a display sink,
    either directly or through a name assigned from it in the same scope."""
    tree = ast.parse(source)
    parents: dict[int, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[id(child)] = parent

    def scope_node(node: ast.AST) -> ast.AST:
        cur: ast.AST | None = node
        while cur is not None:
            if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Module)):
                return cur
            cur = parents.get(id(cur))
        return tree

    def scope_of(node: ast.AST) -> str:
        found = scope_node(node)
        return found.name if isinstance(found, (ast.FunctionDef, ast.AsyncFunctionDef)) else "<module>"

    def reaches_argv(use: ast.AST) -> bool:
        parent = parents.get(id(use))
        if isinstance(parent, (ast.List, ast.Tuple)):
            return True
        if isinstance(parent, ast.keyword):
            parent = parents.get(id(parent))
        if isinstance(parent, ast.Call) and use is not parent.func:
            return _callee_name(parent) not in _DISPLAY_SINKS
        return False

    hits: set[tuple[str, str, int]] = set()
    tainted: dict[tuple[int, str], str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            tools = sorted(_which_name(n) for n in ast.walk(node.value) if _is_which_call(n))
            if tools:
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        tainted[(id(scope_node(node)), target.id)] = tools[0]
    for node in ast.walk(tree):
        if _is_which_call(node) and reaches_argv(node):
            hits.add((scope_of(node), _which_name(node), node.lineno))
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            tool = tainted.get((id(scope_node(node)), node.id))
            if tool is not None and reaches_argv(node):
                hits.add((scope_of(node), tool, node.lineno))
    return sorted(hits, key=lambda h: h[2])


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


_WHICH_ALLOWED: frozenset[tuple[str, str, str]] = frozenset({
    # POSIX arm only: the function returns the interpreter form on win32 first, and
    # POSIX ``which`` searches PATH alone (nx_argv.py's own docstring).
    ("nexus/commands/daemon.py", "_resolve_nx_bin", "nx"),
})


def _scan_which() -> dict[tuple[str, str, str], list[int]]:
    found: dict[tuple[str, str, str], list[int]] = {}
    for path in sorted(SRC_ROOT.rglob("*.py")):
        rel = path.relative_to(SRC_ROOT.parent).as_posix()
        for scope, tool, line in _which_reaching_argv(path.read_text(encoding="utf-8")):
            found.setdefault((rel, scope, tool), []).append(line)
    return found


def test_no_lookup_of_nx_git_bd_or_claude_reaches_an_argv_outside_the_sanctioned_resolvers() -> None:
    found = _scan_which()
    assert set(_WHICH_ALLOWED) <= set(found), (
        "the which-scan did not find a sanctioned site; the detector is broken or the "
        f"site moved. Missing: {sorted(set(_WHICH_ALLOWED) - set(found))}"
    )
    stray = {k: v for k, v in found.items() if k not in _WHICH_ALLOWED}
    assert not stray, (
        "a shutil.which result is spawned; on Windows which() answers with the current "
        "directory's hit first. Use nexus.util.nx_argv.which_off_cwd (or nx_argv/"
        "nx_argv_for for nx): "
        + ", ".join(f"{p}:{lines} in {s} ({n!r})" for (p, s, n), lines in sorted(stray.items()))
    )


def test_the_which_detector_sees_every_shape_it_claims_to_police() -> None:
    # L01: the result of a lookup in an argv, directly and through a name.
    assert _which_reaching_argv('import shutil\ndef f():\n    x = shutil.which("nx")\n    return [x, "up"]\n') == [("f", "nx", 4)]
    assert _which_reaching_argv('import shutil\ndef f():\n    return [shutil.which("git"), "log"]\n') == [("f", "git", 3)]
    assert _which_reaching_argv('import shutil\ndef f(run):\n    c = shutil.which("claude")\n    run(c, "x")\n') == [("f", "claude", 4)]
    assert _which_reaching_argv('def f(which):\n    p = which("bd") or "bd"\n    return (p, "list")\n') == [("f", "bd", 3)]
    # keyword form, and a name assigned from `a or which(...)`
    assert _which_reaching_argv('import shutil\ndef f(run, a):\n    c = a or shutil.which("claude")\n    run(cmd=c)\n') == [("f", "claude", 4)]
    # Not an argv: an existence test, a display value, a name this lint does not police.
    assert _which_reaching_argv('import shutil\ndef f():\n    return shutil.which("nx") is None\n') == []
    assert _which_reaching_argv('import shutil\ndef f():\n    p = shutil.which("git")\n    return f"git at {p}", bool(p)\n') == []
    assert _which_reaching_argv('import shutil\ndef f():\n    return [shutil.which("java"), "-v"]\n') == []
    assert _which_reaching_argv('import shutil\ndef f(name):\n    return [shutil.which(name)]\n') == []
    # A name assigned in ANOTHER function is not this function's lookup.
    assert _which_reaching_argv('import shutil\ndef a():\n    x = shutil.which("nx")\n    return x\ndef b(x):\n    return [x, "q"]\n') == []


def test_the_detector_sees_constant_and_shell_string_forms_of_a_bare_name() -> None:
    # L03: a module constant standing in for the literal.
    assert _hits_in('_NX = "nx"\ndef f():\n    return [_NX, "up"]\n') == [("f", "nx", 3)]
    assert _hits_in('NX: str = "schtasks"\nX = (NX, "/Query")\n') == [("<module>", "schtasks", 2)]
    assert _hits_in('_X = "other"\ndef f():\n    return [_X, "up"]\n') == []
    # L04: a shell string, plain or f-string, and os.system.
    assert _hits_in('import subprocess\ndef f():\n    subprocess.run("nx daemon stop", shell=True)\n') == [("f", "nx", 3)]
    assert _hits_in('import subprocess\ndef f(a):\n    subprocess.run(f"nx {a}", shell=True)\n') == [("f", "nx", 3)]
    assert _hits_in('import os\ndef f():\n    os.system("schtasks /Query")\n') == [("f", "schtasks", 3)]
    assert _hits_in('import subprocess\ndef f():\n    subprocess.run("echo nx", shell=True)\n') == []
    assert _hits_in('def f(log):\n    log("nx daemon stop")\n') == []  # prose is not a spawn


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
    # _stop_service_stack_best_effort and _stop_mineru_if_started spawn `nx`
    # stop verbs, whose argv comes from _resolve_nx_bin (absolute on Windows);
    # they are not manager commands.
    assert spawners == {
        "_run_manager", "_stop_service_stack_best_effort", "_stop_mineru_if_started",
    }, (
        "a manager command is spawned outside _run_manager, so it skips "
        f"_manager_executable: {sorted(spawners)}"
    )
