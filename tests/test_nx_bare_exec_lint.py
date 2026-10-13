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
The nx-hook verbs under ``nexus/hooks`` get a wider scan (every tool, every spawn);
see the section at the end of this file. Non-vacuity: it must find both sanctioned spellings, and the installer
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


def _is_which_call(node: ast.AST, names: object = _WHICH_NAMES) -> bool:
    return (
        isinstance(node, ast.Call)
        and _callee_name(node) == "which"
        and bool(node.args)
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value in names  # type: ignore[operator]
    )


def _which_name(node: ast.AST) -> str:
    assert isinstance(node, ast.Call) and isinstance(node.args[0], ast.Constant)
    return str(node.args[0].value)


def _which_reaching_argv(source: str, names: object = _WHICH_NAMES) -> list[tuple[str, str, int]]:
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
            tools = sorted(_which_name(n) for n in ast.walk(node.value) if _is_which_call(n, names))
            if tools:
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        tainted[(id(scope_node(node)), target.id)] = tools[0]
    for node in ast.walk(tree):
        if _is_which_call(node, names) and reaches_argv(node):
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


def _function(tree: ast.Module, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
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


# -- the nx-hook verbs: every tool, every spawn (RDR-224 Phase 4 critique S1) -------
#
# The scans above police nx and schtasks across src/nexus. The verbs under
# ``nexus/hooks`` run with the PROJECT as cwd, so for them the hole is wider: any
# tool spawned by bare name (git, bd, uv, claude) runs a planted .exe on Windows.
# They get the strictest form of the rules below. The rest of ``src/nexus`` has the
# same hole (a CLI command or the indexer runs in whatever directory the user is
# in, normally a clone); it is policed by the tree-wide section at the end of this
# file, whose rules are the same three with ``run_bounded`` trusted as a resolver.
#
# What this scan sees: in ``nexus/hooks`` and ``nexus/_hook_runtime``,
#   R1  a list/tuple literal (or a module constant standing in for the head) whose
#       first element is a bare tool name, unless it is a direct argument of a
#       resolving call (``argv_off_cwd`` or a per-file wrapper that calls it);
#   R2  a spawn (``run_bounded``, ``subprocess.run/Popen/check_output/...``) whose
#       argv argument is not itself a call to ``argv_off_cwd`` / ``nx_argv`` /
#       ``nx_argv_for``, which covers run_bounded callers whose argv is built
#       elsewhere and passed as a name;
#   R3  an aliased spawner: ``from subprocess import run as r``, ``import subprocess
#       as sp``, or ``run_bounded`` used as a value rather than called.
# What it cannot see: argv assembled in another module and handed to a wrapper
# declared in ``_HOOK_SPAWN_ALLOWED``; a spawner reached through ``getattr`` or an
# injected callable parameter; ``os.execv*``; a head that is a variable holding a
# bare name; and a lookup through an alias such as preflight's ``_which`` (the
# which-scan above matches the callee name ``which`` only). The behavioural tests in
# ``tests/hooks/test_hook_verbs_exec_off_cwd.py`` exercise every current site, which
# is what covers those shapes.

_HOOK_SCOPE = ("nexus/hooks/", "nexus/_hook_runtime/")
_HOOK_BARE_NAMES = _BARE_NAMES | frozenset({
    "git", "git.exe", "bd", "bd.exe", "uv", "uv.exe", "claude", "claude.exe",
})
_ARGV_RESOLVERS = frozenset({"argv_off_cwd", "nx_argv", "nx_argv_for"})

#: wrapper name -> the one file whose argv it resolves through ``argv_off_cwd``
#: itself. A literal handed to one of these is resolved;
#: ``test_every_hook_wrapper_resolves_its_argv_through_argv_off_cwd`` pins that
#: each really does.
_HOOK_WRAPPERS: dict[str, str] = {
    "run_command": "nexus/hooks/session_context.py",
    "_run_captured": "nexus/hooks/subagent_start.py",
    "_probe": "nexus/hooks/preflight_verb.py",
}

_SUBPROCESS_SPAWNS = frozenset({"run", "Popen", "check_output", "check_call", "call"})
_BOUNDED_SPAWNS = frozenset({"run_bounded", "_REAL_RUN_BOUNDED"})

#: (path relative to src/, enclosing function): spawns whose argv is a parameter.
_HOOK_SPAWN_ALLOWED: frozenset[tuple[str, str]] = frozenset({
    # ``_spawn_detached(argv)`` is called once, from ``run``, with the list
    # ``nx_argv_for(which_off_cwd("nx"), ...)`` builds (absolute on both platforms).
    ("nexus/hooks/upgrade_auto.py", "_spawn_detached"),
})


def _is_hook_spawn(node: ast.Call) -> bool:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id in _BOUNDED_SPAWNS
    return (
        isinstance(func, ast.Attribute)
        and func.attr in _SUBPROCESS_SPAWNS
        and isinstance(func.value, ast.Name)
        and func.value.id == "subprocess"
    )


def _module_constants_for(tree: ast.Module, names: frozenset[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for node in tree.body:
        value = getattr(node, "value", None)
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(value, ast.Constant) and value.value in names:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    out[target.id] = str(value.value)
    return out


def _hook_scope_hits(source: str, rel: str = "") -> list[tuple[str, str, int]]:
    """``(scope, kind, line)`` for each R1/R2/R3 violation in *source*."""
    tree = ast.parse(source)
    constants = _module_constants_for(tree, _HOOK_BARE_NAMES)
    resolving = _ARGV_RESOLVERS | {n for n, f in _HOOK_WRAPPERS.items() if f == rel}
    parents: dict[int, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[id(child)] = parent

    def scope_of(node: ast.AST) -> str:
        cur: ast.AST | None = node
        while cur is not None:
            if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return cur.name
            cur = parents.get(id(cur))
        return "<module>"

    hits: set[tuple[str, str, int]] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.List, ast.Tuple)) and node.elts:
            first = node.elts[0]
            bare = (
                isinstance(first, ast.Constant) and first.value in _HOOK_BARE_NAMES
            ) or (isinstance(first, ast.Name) and first.id in constants)
            if bare:
                parent = parents.get(id(node))
                handed_to_resolver = (
                    isinstance(parent, ast.Call)
                    and node in parent.args
                    and _callee_name(parent) in resolving
                )
                if not handed_to_resolver:
                    hits.add((scope_of(node), "bare-argv0", node.lineno))
        elif isinstance(node, ast.Call) and _is_hook_spawn(node):
            argv = node.args[0] if node.args else next(
                (kw.value for kw in node.keywords if kw.arg == "args"), None
            )
            resolved = isinstance(argv, ast.Call) and _callee_name(argv) in _ARGV_RESOLVERS
            if not resolved and (rel, scope_of(node)) not in _HOOK_SPAWN_ALLOWED:
                hits.add((scope_of(node), "unresolved-spawn-argv", node.lineno))
        elif isinstance(node, ast.ImportFrom) and node.module in {"subprocess", "nexus.bounded_subprocess"}:
            for alias in node.names:
                if alias.asname and alias.name in (_SUBPROCESS_SPAWNS | _BOUNDED_SPAWNS):
                    hits.add((scope_of(node), "aliased-spawner", node.lineno))
                elif node.module == "subprocess" and alias.name in _SUBPROCESS_SPAWNS:
                    hits.add((scope_of(node), "imported-spawner", node.lineno))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "subprocess" and alias.asname:
                    hits.add((scope_of(node), "aliased-spawner", node.lineno))
        elif isinstance(node, ast.Name) and node.id in _BOUNDED_SPAWNS and isinstance(node.ctx, ast.Load):
            parent = parents.get(id(node))
            if not (isinstance(parent, ast.Call) and parent.func is node):
                hits.add((scope_of(node), "aliased-spawner", node.lineno))
    return sorted(hits, key=lambda h: (h[2], h[1]))


def _hook_scope_files() -> list[pathlib.Path]:
    return [
        p for p in sorted(SRC_ROOT.rglob("*.py"))
        if p.relative_to(SRC_ROOT.parent).as_posix().startswith(_HOOK_SCOPE)
    ]


def _wrapped_hook_literals() -> int:
    """How many bare-name literals sit correctly inside a resolving call: the
    non-vacuity count, so a detector gone blind cannot pass by finding nothing."""
    count = 0
    for path in _hook_scope_files():
        rel = path.relative_to(SRC_ROOT.parent).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        resolving = _ARGV_RESOLVERS | {n for n, f in _HOOK_WRAPPERS.items() if f == rel}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _callee_name(node) in resolving:
                count += sum(
                    1 for a in node.args
                    if isinstance(a, (ast.List, ast.Tuple)) and a.elts
                    and isinstance(a.elts[0], ast.Constant) and a.elts[0].value in _HOOK_BARE_NAMES
                )
    return count


def test_no_hook_verb_spawns_a_tool_the_cwd_can_supply() -> None:
    files = _hook_scope_files()
    assert len(files) >= 10, f"the hook scope matched {len(files)} files; the glob is wrong"
    stray = []
    for path in files:
        rel = path.relative_to(SRC_ROOT.parent).as_posix()
        for scope, kind, line in _hook_scope_hits(path.read_text(encoding="utf-8"), rel):
            stray.append(f"{rel}:{line} in {scope} ({kind})")
    assert not stray, (
        "a hook verb runs with the project as its cwd, and on Windows CreateProcess "
        "takes a planted git.exe / bd.exe / uv.exe before PATH. Spawn through "
        "nexus.util.nx_argv.argv_off_cwd (or nx_argv / nx_argv_for for nx): "
        + "; ".join(stray)
    )


def test_the_hook_scope_scan_finds_the_sites_it_polices() -> None:
    # git x1 (hooks/__init__), git x3 (rdr_verb), git+bd (session_context), git
    # (subagent_start), bd (preflight_verb): eight literals, each inside a resolver.
    assert _wrapped_hook_literals() >= 8


def test_every_hook_wrapper_resolves_its_argv_through_argv_off_cwd() -> None:
    for name, rel in _HOOK_WRAPPERS.items():
        tree = ast.parse((SRC_ROOT.parent / rel).read_text(encoding="utf-8"))
        fn = _function(tree, name)
        calls = {_callee_name(n) for n in ast.walk(fn) if isinstance(n, ast.Call)}
        assert "argv_off_cwd" in calls, f"{rel}:{name} is trusted as a resolver but does not call argv_off_cwd"


def test_the_hook_scope_detector_sees_every_shape_it_claims_to_police() -> None:
    def kinds(src: str, rel: str = "nexus/hooks/x.py") -> list[str]:
        return [k for _s, k, _l in _hook_scope_hits(src, rel)]

    # R1: a bare head, in a list or a tuple, for each tool and via a module constant.
    assert kinds('def f(run):\n    run(["git", "log"])\n') == ["bare-argv0"]
    assert kinds('X = ("bd", "ready")\n') == ["bare-argv0"]
    assert kinds('_G = "uv"\ndef f():\n    return [_G, "run"]\n') == ["bare-argv0"]
    assert kinds('def f():\n    return ["claude.exe", "-p"]\n') == ["bare-argv0"]
    assert kinds('def f():\n    return ["python", "-m", "x"]\n') == []
    # ... but not when handed straight to a resolver or a per-file wrapper.
    assert kinds('def f():\n    return argv_off_cwd(["git", "log"])\n') == []
    assert kinds('def f():\n    return run_command(["git", "log"], 5)\n') == ["bare-argv0"]
    assert kinds('def f():\n    return run_command(["git", "log"], 5)\n', "nexus/hooks/session_context.py") == []
    assert kinds('def f(x):\n    return other(["git", "log"])\n') == ["bare-argv0"]
    # R2: a spawn whose argv is not itself resolved, including a name and a keyword.
    assert kinds('def f(a):\n    run_bounded(a, timeout=1)\n') == ["unresolved-spawn-argv"]
    assert kinds('import subprocess\ndef f(a):\n    subprocess.run(args=a)\n') == ["unresolved-spawn-argv"]
    assert kinds('import subprocess\ndef f(a):\n    subprocess.Popen(a, text=True)\n') == ["unresolved-spawn-argv"]
    assert kinds('import subprocess\ndef f():\n    subprocess.run("git log", shell=True)\n') == ["unresolved-spawn-argv"]
    assert kinds('def f():\n    run_bounded(argv_off_cwd(a), timeout=1)\n') == []
    assert kinds('import subprocess\ndef f():\n    subprocess.run(nx_argv_for(p, "x"))\n') == []
    assert kinds('def f(a):\n    a.run(b)\n') == []  # not the subprocess module
    assert kinds('import subprocess\ndef _spawn_detached(a):\n    subprocess.Popen(a)\n', "nexus/hooks/upgrade_auto.py") == []
    # R3: an alias hides the callee from R2, so the alias itself is the finding.
    assert kinds('from subprocess import run as r\n') == ["aliased-spawner"]
    # ... and so does importing a spawner by name, aliased or not.
    assert kinds('from subprocess import Popen, check_output, run, call, check_call\n') == ["imported-spawner"]
    assert kinds('from subprocess import PIPE, TimeoutExpired\n') == []
    assert kinds('from nexus.bounded_subprocess import run_bounded\n') == []
    assert kinds('import subprocess as sp\n') == ["aliased-spawner"]
    assert kinds('def f():\n    from nexus.bounded_subprocess import run_bounded as rb\n') == ["aliased-spawner"]
    assert kinds('def f():\n    runner = run_bounded\n    return runner\n') == ["aliased-spawner"]
    assert kinds('def f():\n    from nexus.bounded_subprocess import run_bounded\n    run_bounded(argv_off_cwd(a))\n') == []


def test_the_hook_scope_is_a_scope_and_the_rest_of_the_tree_has_its_own_section() -> None:
    """The hook scan is the strict one (``run_bounded`` callers must name the
    resolver); everything else under ``src/nexus`` is covered by the tree-wide
    section below, which trusts the pinned ``run_bounded``."""
    inside = {p.relative_to(SRC_ROOT.parent).as_posix() for p in _hook_scope_files()}
    assert "nexus/indexer.py" not in inside
    assert "nexus/hooks/rdr_verb.py" in inside
    assert "nexus/indexer.py" in {p.relative_to(SRC_ROOT.parent).as_posix() for p in _tree_scope_files()}
    assert not (inside & {p.relative_to(SRC_ROOT.parent).as_posix() for p in _tree_scope_files()})


# -- the rest of src/nexus: every tool, every spawn (nexus-f9bgu.37 sibling sweep) --
#
# An ``nx`` command or the indexer runs in the directory the user chose, normally a
# clone, so every bare tool name has the hook verbs' hole: on Windows
# ``CreateProcess`` takes a planted ``git.exe`` / ``bd.exe`` / ``ps.exe`` from the
# working directory before ``PATH``. Measured on 7581cc834's parent: 49 list
# literals with a bare head in 15 modules outside the hook scope, and the spawns
# that carry them are the same three shapes the hook scan already names. The fix is
# in two layers, and this section pins both:
#
#   * ``nexus.bounded_subprocess.run_bounded`` resolves its own argv head through
#     ``argv_off_cwd``, so every caller (about 60 call sites, plus the injected
#     ``run=run_bounded`` defaults in ``plugin_lockstep`` and ``win_console``) is
#     covered once, and a new caller is covered without remembering anything.
#     ``test_run_bounded_resolves_its_argv_through_argv_off_cwd`` pins it.
#   * a spawn that does not go through ``run_bounded`` (``subprocess.run``,
#     ``Popen``, ``check_output``, ``asyncio.create_subprocess_exec``, ``os.exec*``)
#     must name ``argv_off_cwd`` / ``nx_argv`` / ``nx_argv_for`` on its argv, or sit
#     in ``_TREE_ALLOWED`` with a reason.
#
# What the scan sees, in every file under ``src/nexus`` except the hook scope (which
# has the stricter scan above):
#   R1  a list/tuple literal (or a module constant standing in for the head) whose
#       first element is a bare name from ``_TREE_TOOLS``, unless it is a direct
#       argument of a resolving call (the hook resolvers plus ``run_bounded``) or is
#       bound to a local name every use of which is such an argument (or a list
#       mutator call on the name);
#   R2  a raw spawn whose argv argument is not itself a call to a resolver;
#   R3  an aliased spawner: ``from subprocess import run as r``, ``import
#       subprocess as sp``, ``subprocess.Popen`` (or ``run``, ...) used as a value
#       rather than called, or ``run_bounded`` used as a value;
#   R4  ``shutil.which("<constant>")`` whose result reaches an argv, for every
#       constant name (the which-scan above polices only nx, git, bd and claude);
#       use ``which_off_cwd``.
# What it cannot see: argv built in one function and spawned in another by a caller
# that is not itself a spawn (an allowed spawn site is the place a reviewer checks the
# head); a head that is a variable holding a bare name; a lookup through an alias
# (``which = shutil.which`` and a call to ``which``, or a non-constant name); a
# tool named outside ``_TREE_TOOLS``; a spawn through ``getattr`` or an injected
# callable that is not one of the aliases R3 names; ``shell=True`` strings other
# than the ones the older scan reads; the Windows search order itself. The
# behavioural tests in ``tests/test_exec_off_cwd_outside_hooks.py`` exercise the
# wrapper and one site per module, which is what covers those shapes.

#: Every program name a spawn in ``src/nexus`` uses, plus the ones a reviewer would
#: expect (database and process tools, shells). A name not listed here is not seen
#: by R1.
_TREE_TOOLS = frozenset({
    "git", "bd", "gh", "uv", "claude", "nx", "ps", "osascript", "open", "bash", "sh",
    "bat", "ss", "java", "psql", "pg_ctl", "initdb", "pg_dump", "pg_restore", "docker",
    "launchctl", "systemctl", "schtasks", "powershell", "pwsh", "taskkill", "tasklist",
    "wmic", "cmd", "mineru-api", "pip", "pgrep", "pkill", "kill", "lsof",
    "curl", "tar", "unzip", "security", "pdftotext", "tesseract", "rg",
})
_TREE_BARE_NAMES = _HOOK_BARE_NAMES | _TREE_TOOLS | frozenset(f"{t}.exe" for t in _TREE_TOOLS)

#: Callees that resolve the head of the argv they are handed. ``run_bounded`` is one
#: because it calls ``argv_off_cwd`` itself (pinned below).
_TREE_RESOLVERS = _ARGV_RESOLVERS | _BOUNDED_SPAWNS

#: (file relative to src/, function name): a local helper that hands its first
#: argument to a resolver, so a literal passed to it is resolved.
#: ``test_every_tree_wrapper_hands_its_argv_to_a_resolver`` pins each one.
_TREE_WRAPPERS: frozenset[tuple[str, str]] = frozenset({
    ("nexus/commands/command_context.py", "_check_output"),
    ("nexus/indexer.py", "run"),
    ("nexus/indexer_utils.py", "_run"),
    ("nexus/operators/dispatch.py", "_spawn_with_prompt_file"),
    ("nexus/_install/generation_core.py", "_run"),
})

_ASYNCIO_SPAWNS = frozenset({"create_subprocess_exec", "create_subprocess_shell"})
_OS_SPAWN_PREFIXES = ("exec", "spawn")
_OS_SPAWN_NAMES = frozenset({"system", "popen", "startfile"})
_LIST_MUTATORS = frozenset({"append", "extend", "insert"})

#: (path relative to src/, enclosing function, kind) -> why it is safe. Every entry
#: is a place the scan found and a reviewer cleared; there is no silent skip.
_TREE_ALLOWED: dict[tuple[str, str, str], str] = {
    # -- the argv is an absolute path to an interpreter or an installed binary ------
    ("nexus/_install/layout_core.py", "declared_console_scripts_detail", "unresolved-spawn-argv"):
        "head is the generation's own venv interpreter, an absolute path (bootstrap-safe module)",
    ("nexus/_session_end_launcher.py", "_spawn_detached_cleanup", "unresolved-spawn-argv"):
        "head is sys.executable, absolute (module runs before nexus imports; see its docstring)",
    ("nexus/commands/index.py", "_spawn_deferred_labeling", "unresolved-spawn-argv"):
        "head is sys.executable, absolute",
    ("nexus/pdf_extractor.py", "_mineru_run_subprocess", "unresolved-spawn-argv"):
        "head is sys.executable, absolute",
    ("nexus/health.py", "_probe_mcp_server", "unresolved-spawn-argv"):
        "head is _resolve_mcp_binary's result: a hit accepted only from the PATH directory "
        "being scanned (nexus-f9bgu.35), so never the working directory",
    ("nexus/_mineru_spawn.py", "spawn_server_process", "unresolved-spawn-argv"):
        "head is _resolve_mineru_api_bin: an interpreter or source-tree sibling, or which_off_cwd",
    # -- POSIX-only branches: the program does not exist on Windows -------------------
    ("nexus/_install/census_core.py", "ps_snapshot", "unresolved-spawn-argv"):
        "ps runs on the non-win32 arm only (the win32 arm returns first); bootstrap-safe module",
    ("nexus/commands/dt.py", "open_cmd", "unresolved-spawn-argv"):
        "macOS `open`, behind the _is_darwin() gate at the top of the command",
    # -- the argv is resolved on the line above, or in the caller, by a name the scan --
    # -- does not recognise ----------------------------------------------------------
    ("nexus/_install/generation_core.py", "_run", "unresolved-spawn-argv"):
        "argv = _off_cwd(argv) one line above (the module's stdlib-only copy of argv_off_cwd)",
    ("nexus/_install/generation_core.py", "build_generation", "bare-argv0"):
        "install is spliced into the argv of the _run call that follows (_run resolves it)",
    ("nexus/_install/census_core.py", "<module>", "bare-argv0"):
        "PS_COMMAND, spawned only by ps_snapshot's non-win32 arm (see below)",
    ("nexus/commands/dt.py", "open_cmd", "bare-argv0"):
        "macOS `open`, behind the _is_darwin() gate",
    ("nexus/aspect_extractor.py", "_run_claude_isolated", "bare-argv0"):
        "argv = _argv or [claude ...]; the Popen takes argv_off_cwd(argv) in the same function",
    ("nexus/commands/self_cmd.py", "_build_argv", "bare-argv0"):
        "returns the install_generation.sh argv; _build_flip_shims spawns it via argv_off_cwd(build)",
    ("nexus/commands/self_cmd.py", "_converge_legacy_install", "bare-argv0"):
        "build is spawned via argv_off_cwd(build) in the same function (it is also echoed on --dry-run)",
    ("nexus/devonthink.py", "_run_osascript", "bare-argv0"):
        "the CalledProcessError cmd= field, a display value; the spawn above it is run_bounded",
    ("nexus/plugin_lockstep.py", "_resolve_ref_sha", "bare-argv0"):
        "handed to the injected Runner, whose default is run_bounded; a caller that injects "
        "another runner owns its head",
    ("nexus/db/pg_provision.py", "missing_names", "bare-argv0"):
        "(name, path) pairs for a presence check, not an argv",
    ("nexus/util/nx_argv.py", "nx_argv", "bare-argv0"):
        "the POSIX arm of the sanctioned builder; the Windows arm returns the interpreter form first",
    # The manager command builders in installer.py (launchctl, systemctl, schtasks) are
    # cleared one by one: each builds an argv that reaches a spawn only through
    # _run_manager -> run_bounded (pinned by test_installer_resolves_schtasks_through_the_
    # windows_path_and_spawns_in_one_place), and a Windows manager resolves to System32
    # first. A new bare spawn elsewhere in the file is a finding.
    **{
        (_INSTALLER, builder, "bare-argv0"): (
            "manager command builder: spawned only through _run_manager -> run_bounded, "
            "and a Windows manager resolves to System32 first"
        )
        for builder in (
            "_task_run_cmd", "_task_end_cmd", "_activate_cmd", "_deactivate_cmd",
            "_activation_query_cmd", "_windows_task_activation", "_windows_task_registered",
            "_launchd_loaded_now",
        )
    },
    ("nexus/bounded_subprocess.py", "run_bounded", "unresolved-spawn-argv"):
        "the wrapper itself: argv = argv_off_cwd(argv) before the Popen (pinned by test)",
    # -- detached daemon spawns: the head is an installed binary or this interpreter ---
    ("nexus/commands/daemon.py", "<module>", "aliased-spawner"):
        "_popen = subprocess.Popen, the test seam for the supervisor spawn whose head is "
        "_resolve_nx_bin's absolute result",
    ("nexus/daemon/storage_service_daemon.py", "<module>", "aliased-spawner"):
        "_popen = subprocess.Popen, the test seam for the engine spawn: the installed "
        "native binary or the java found by which_off_cwd",
    ("nexus/daemon/aspect_worker_daemon.py", "ensure_aspect_worker_daemon", "aliased-spawner"):
        "popen default; the argv head is _resolve_nx_bin's absolute result",
    ("nexus/daemon/windows_autostart.py", "_supervise_once", "aliased-spawner"):
        "popen default; the argv head is the console interpreter, absolute",
    ("nexus/db/pg_provision.py", "_pg_ctl_start_detached", "aliased-spawner"):
        "popen default; the head is the bundle's own pg_ctl, an absolute path",
    ("nexus/db/pg_provision.py", "_start_cluster", "aliased-spawner"):
        "popen default passed through to _pg_ctl_start_detached; same absolute pg_ctl",
}


def _tree_allowed(key: tuple[str, str, str]) -> bool:
    return key in _TREE_ALLOWED


def _is_raw_spawn(node: ast.Call) -> bool:
    func = node.func
    if not (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)):
        return False
    owner, attr = func.value.id, func.attr
    return (
        (owner == "subprocess" and attr in _SUBPROCESS_SPAWNS)
        or (owner == "asyncio" and attr in _ASYNCIO_SPAWNS)
        or (owner == "os" and (attr.startswith(_OS_SPAWN_PREFIXES) or attr in _OS_SPAWN_NAMES))
    )


def _tree_scope_files() -> list[pathlib.Path]:
    return [
        p for p in sorted(SRC_ROOT.rglob("*.py"))
        if not p.relative_to(SRC_ROOT.parent).as_posix().startswith(_HOOK_SCOPE)
    ]


def _tree_hits(source: str, rel: str = "") -> list[tuple[str, str, int]]:
    """``(scope, kind, line)`` for each R1/R2/R3 violation in *source*."""
    tree = ast.parse(source)
    constants = _module_constants_for(tree, _TREE_BARE_NAMES)
    resolving = _TREE_RESOLVERS | {n for f, n in _TREE_WRAPPERS if f == rel}
    parents: dict[int, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[id(child)] = parent

    def enclosing_function(node: ast.AST) -> ast.AST | None:
        cur: ast.AST | None = node
        while cur is not None:
            if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return cur
            cur = parents.get(id(cur))
        return None

    def scope_of(node: ast.AST) -> str:
        fn = enclosing_function(node)
        return fn.name if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) else "<module>"

    def resolved_call_arg(node: ast.AST) -> bool:
        """*node* is handed straight to a resolving call as its argv, directly or as
        the left side of a ``+`` that extends it (``["bd", "list"] + args``)."""
        parent = parents.get(id(node))
        while isinstance(parent, ast.BinOp) and isinstance(parent.op, ast.Add) and parent.left is node:
            node, parent = parent, parents.get(id(parent))
        if isinstance(parent, ast.keyword):
            parent = parents.get(id(parent))
        return (
            isinstance(parent, ast.Call)
            and node is not parent.func
            and _callee_name(parent) in resolving
        )

    def only_resolved_uses(assign: ast.Assign | ast.AnnAssign) -> bool:
        """A literal bound to one local name whose every use is an argv handed to a
        resolver (or a list mutator call, which does not move the head)."""
        targets = assign.targets if isinstance(assign, ast.Assign) else [assign.target]
        if len(targets) != 1 or not isinstance(targets[0], ast.Name):
            return False
        name, fn = targets[0].id, enclosing_function(assign)
        scope = fn if fn is not None else tree
        uses = [
            n for n in ast.walk(scope)
            if isinstance(n, ast.Name) and n.id == name and isinstance(n.ctx, ast.Load)
        ]
        if not uses:
            return False
        for use in uses:
            parent = parents.get(id(use))
            if isinstance(parent, ast.Attribute) and parent.attr in _LIST_MUTATORS:
                continue
            if not resolved_call_arg(use):
                return False
        return True

    hits: set[tuple[str, str, int]] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.List, ast.Tuple)) and node.elts:
            first = node.elts[0]
            bare = (
                isinstance(first, ast.Constant) and first.value in _TREE_BARE_NAMES
            ) or (isinstance(first, ast.Name) and first.id in constants)
            if not bare:
                continue
            parent = parents.get(id(node))
            if resolved_call_arg(node):
                continue
            if isinstance(parent, (ast.Assign, ast.AnnAssign)) and parent.value is node and only_resolved_uses(parent):
                continue
            hits.add((scope_of(node), "bare-argv0", node.lineno))
        elif isinstance(node, ast.Call) and _is_raw_spawn(node):
            argv = node.args[0] if node.args else next(
                (kw.value for kw in node.keywords if kw.arg == "args"), None
            )
            if isinstance(argv, ast.Starred):
                argv = argv.value
            resolved = isinstance(argv, ast.Call) and _callee_name(argv) in _ARGV_RESOLVERS
            if not resolved:
                hits.add((scope_of(node), "unresolved-spawn-argv", node.lineno))
        elif isinstance(node, ast.ImportFrom) and node.module in {"subprocess", "asyncio"}:
            # A spawner imported by name hides the callee as well as an alias does:
            # ``run(argv)`` carries no ``subprocess.`` for R2 to match. Aliased or not.
            for alias in node.names:
                if alias.name in (_SUBPROCESS_SPAWNS | _ASYNCIO_SPAWNS):
                    kind = "aliased-spawner" if alias.asname else "imported-spawner"
                    hits.add((scope_of(node), kind, node.lineno))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in {"subprocess", "asyncio"} and alias.asname:
                    hits.add((scope_of(node), "aliased-spawner", node.lineno))
        elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
            # subprocess.Popen / subprocess.run used as a VALUE (a default argument,
            # an assignment), not called: the spawn happens elsewhere under another
            # name. An annotation or an isinstance() test names the type, not a spawn.
            if (
                isinstance(node.value, ast.Name)
                and node.value.id == "subprocess"
                and node.attr in _SUBPROCESS_SPAWNS
            ):
                parent = parents.get(id(node))
                if isinstance(parent, ast.Call) and parent.func is node:
                    continue
                if _names_a_type(node, parents):
                    continue
                hits.add((scope_of(node), "aliased-spawner", node.lineno))
    return sorted(hits, key=lambda h: (h[2], h[1]))


def _names_a_type(node: ast.AST, parents: dict[int, ast.AST]) -> bool:
    """*node* sits in an annotation, a subscript (``Popen[bytes]``) or an
    ``isinstance`` / ``issubclass`` test, where it names a type rather than spawns."""
    cur: ast.AST | None = node
    while cur is not None:
        parent = parents.get(id(cur))
        if isinstance(parent, ast.Subscript) and parent.value is cur:
            return True
        if isinstance(parent, ast.arg) and parent.annotation is cur:
            return True
        if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)) and parent.returns is cur:
            return True
        if isinstance(parent, ast.AnnAssign) and parent.annotation is cur:
            return True
        if isinstance(parent, ast.Call) and _callee_name(parent) in {"isinstance", "issubclass"}:
            return True
        cur = parent if isinstance(parent, (ast.BinOp, ast.Tuple, ast.Subscript)) else None
    return False


def _tree_scan() -> dict[tuple[str, str, str], list[int]]:
    found: dict[tuple[str, str, str], list[int]] = {}
    for path in _tree_scope_files():
        rel = path.relative_to(SRC_ROOT.parent).as_posix()
        for scope, kind, line in _tree_hits(path.read_text(encoding="utf-8"), rel):
            found.setdefault((rel, scope, kind), []).append(line)
    return found


def test_no_spawn_in_the_tree_can_run_a_tool_the_cwd_supplies() -> None:
    found = _tree_scan()
    stray = {k: v for k, v in found.items() if not _tree_allowed(k)}
    assert not stray, (
        "on Windows CreateProcess takes a planted git.exe / bd.exe / ps.exe from the "
        "working directory before PATH, and an nx command runs in the user's clone. "
        "Spawn through nexus.bounded_subprocess.run_bounded (which resolves its head), or "
        "wrap the argv in nexus.util.nx_argv.argv_off_cwd, or look the tool up with "
        "which_off_cwd; an unavoidable exception goes in _TREE_ALLOWED with its reason: "
        + "; ".join(f"{p}:{lines} in {s} ({k})" for (p, s, k), lines in sorted(stray.items()))
    )


def test_every_tree_allowance_still_matches_a_site() -> None:
    """An allow-list entry for a site that moved is a hole the scan no longer sees."""
    found = set(_tree_scan())
    stale = sorted(k for k in _TREE_ALLOWED if k not in found)
    assert not stale, f"_TREE_ALLOWED entries that match no site (fix or delete them): {stale}"
    assert all(reason.strip() for reason in _TREE_ALLOWED.values())


def test_the_tree_scan_is_not_vacuous() -> None:
    files = _tree_scope_files()
    assert len(files) >= 300, f"the tree scope matched {len(files)} files; the glob is wrong"
    run_bounded_calls = 0
    bare_literals_handed_to_run_bounded = 0
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _callee_name(node) in _BOUNDED_SPAWNS:
                run_bounded_calls += 1
                bare_literals_handed_to_run_bounded += sum(
                    1 for a in node.args
                    if isinstance(a, (ast.List, ast.Tuple)) and a.elts
                    and isinstance(a.elts[0], ast.Constant) and a.elts[0].value in _TREE_BARE_NAMES
                )
    assert run_bounded_calls >= 40, run_bounded_calls
    assert bare_literals_handed_to_run_bounded >= 20, bare_literals_handed_to_run_bounded
    # The allowed raw spawns are real: at least the ones whose reason names a bare head.
    assert len(_tree_scan()) >= len(_TREE_ALLOWED)


def test_run_bounded_resolves_its_argv_through_argv_off_cwd() -> None:
    """``run_bounded`` is trusted as a resolver by the tree scan, so it must be one:
    the call comes before the Popen, on every path."""
    tree = ast.parse((SRC_ROOT / "bounded_subprocess.py").read_text(encoding="utf-8"))
    fn = _function(tree, "run_bounded")
    resolve_lines = [
        n.lineno for n in ast.walk(fn)
        if isinstance(n, ast.Call) and _callee_name(n) == "argv_off_cwd"
    ]
    popen_lines = [
        n.lineno for n in ast.walk(fn)
        if isinstance(n, ast.Call) and _callee_name(n) == "Popen"
    ]
    assert resolve_lines, "run_bounded no longer calls argv_off_cwd"
    assert popen_lines and min(resolve_lines) < min(popen_lines)
    # ... and the Popen is handed the resolved name, not a parameter that was shadowed away.
    popen = next(n for n in ast.walk(fn) if isinstance(n, ast.Call) and _callee_name(n) == "Popen")
    assert isinstance(popen.args[0], ast.Name) and popen.args[0].id == "argv"
    assigned = [
        n for n in ast.walk(fn)
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "argv" for t in n.targets)
        and isinstance(n.value, ast.Call) and _callee_name(n.value) == "argv_off_cwd"
    ]
    assert assigned


def test_every_tree_wrapper_hands_its_argv_to_a_resolver() -> None:
    """A helper in ``_TREE_WRAPPERS`` is trusted because its body spawns through a
    resolver; if it stops, a literal passed to it is no longer resolved."""
    resolvers = _TREE_RESOLVERS | {"_off_cwd"}
    for rel, name in sorted(_TREE_WRAPPERS):
        tree = ast.parse((SRC_ROOT.parent / rel).read_text(encoding="utf-8"))
        fn = _function(tree, name)
        calls = {_callee_name(n) for n in ast.walk(fn) if isinstance(n, ast.Call)}
        assert calls & resolvers, f"{rel}:{name} is trusted as a resolver but calls none"


def test_the_stdlib_only_installer_core_resolves_its_argv_before_spawning() -> None:
    """``generation_core`` cannot import ``nx_argv`` (it runs with nexus absent), so
    its ``_run`` is trusted on the strength of its own ``_off_cwd`` call."""
    tree = ast.parse((SRC_ROOT / "_install" / "generation_core.py").read_text(encoding="utf-8"))
    fn = _function(tree, "_run")
    calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)]
    off = [n.lineno for n in calls if _callee_name(n) == "_off_cwd"]
    spawn = [n.lineno for n in calls if _is_raw_spawn(n)]
    assert off and spawn and min(off) < min(spawn)


def test_the_installer_allowance_names_its_builders_and_covers_nothing_else() -> None:
    """The manager command builders are cleared one by one: a new bare ``["git", ...]``
    or ``["launchctl", ...]`` in another function of ``installer.py`` is a finding, not
    something a file-wide wildcard waves through."""
    assert not [k for k in _TREE_ALLOWED if k[1] == "*"], "a wildcard allowance hides every new site in its file"
    named = {scope for rel, scope, kind in _TREE_ALLOWED if rel == _INSTALLER and kind == "bare-argv0"}
    assert named == {
        "_task_run_cmd", "_task_end_cmd", "_activate_cmd", "_deactivate_cmd",
        "_activation_query_cmd", "_windows_task_activation", "_windows_task_registered",
        "_launchd_loaded_now",
    }
    assert not _tree_allowed((_INSTALLER, "_a_new_function", "bare-argv0"))
    assert not _tree_allowed(("nexus/daemon/other.py", "_task_run_cmd", "bare-argv0"))
    new_site = (
        'import subprocess\n'
        'def _task_run_cmd():\n    return ["schtasks", "/Run"]\n'
        'def _brand_new():\n    return ["git", "log"]\n'
    )
    found = {(scope, kind) for scope, kind, _line in _tree_hits(new_site, _INSTALLER)}
    assert {(s, k) for s, k in found if not _tree_allowed((_INSTALLER, s, k))} == {("_brand_new", "bare-argv0")}


def test_the_tree_detector_sees_every_shape_it_claims_to_police() -> None:
    def kinds(src: str, rel: str = "nexus/x.py") -> list[str]:
        return [k for _s, k, _l in _tree_hits(src, rel)]

    # R1: any tool in the set, bare, in a list or tuple, via a module constant.
    assert kinds('def f(run):\n    run(["ps", "-ww"])\n') == ["bare-argv0"]
    assert kinds('X = ("osascript", "-e")\n') == ["bare-argv0"]
    assert kinds('_P = "psql"\ndef f():\n    return [_P, "-c"]\n') == ["bare-argv0"]
    assert kinds('def f():\n    return ["ss.exe", "-l"]\n') == ["bare-argv0"]
    assert kinds('def f():\n    return ["python", "-m", "x"]\n') == []
    # ... but not handed to a resolver, directly or through a local name, including run_bounded.
    assert kinds('def f():\n    return run_bounded(["git", "log"], timeout=1)\n') == []
    assert kinds('def f():\n    return argv_off_cwd(["git", "log"])\n') == []
    assert kinds('def f():\n    cmd = ["git", "log"]\n    cmd.append("-1")\n    return run_bounded(cmd, timeout=1)\n') == []
    assert kinds('def f():\n    cmd = ["git", "log"]\n    return run_bounded(cmd, timeout=1), cmd\n') == ["bare-argv0"]
    assert kinds('def f():\n    cmd = ["git", "log"]\n    return other(cmd)\n') == ["bare-argv0"]
    assert kinds('def f(args):\n    return run_bounded(["git", "log"] + args, timeout=1)\n') == []
    assert kinds('def f():\n    cmd: list[str] = ["git", "log"]\n    return run_bounded(cmd, timeout=1)\n') == []
    assert kinds('def f():\n    return other(["git", "log"])\n') == ["bare-argv0"]
    # R2: a raw spawn whose argv is not itself resolved, in every spawner family.
    assert kinds('import subprocess\ndef f(a):\n    subprocess.run(a)\n') == ["unresolved-spawn-argv"]
    assert kinds('import subprocess\ndef f(a):\n    subprocess.check_output(a, text=True)\n') == ["unresolved-spawn-argv"]
    assert kinds('import asyncio\nasync def f(a):\n    await asyncio.create_subprocess_exec(*a)\n') == ["unresolved-spawn-argv"]
    assert kinds('import os\ndef f(a):\n    os.execvp(a[0], a)\n') == ["unresolved-spawn-argv"]
    assert kinds('import os\ndef f(a):\n    os.system(a)\n') == ["unresolved-spawn-argv"]
    assert kinds('import subprocess\ndef f():\n    subprocess.run("git log", shell=True)\n') == ["unresolved-spawn-argv"]
    assert kinds('import subprocess\ndef f(a):\n    subprocess.run(argv_off_cwd(a))\n') == []
    assert kinds('import asyncio\nasync def f(a):\n    await asyncio.create_subprocess_exec(*argv_off_cwd(a))\n') == []
    assert kinds('def f(a):\n    run_bounded(a, timeout=1)\n') == []  # trusted: pinned separately
    assert kinds('def f(a):\n    a.run(b)\n') == []
    # R3: an alias hides the callee, so the alias itself is the finding.
    assert kinds('from subprocess import run as r\n') == ["aliased-spawner"]
    # R3b: importing a spawner by name hides the callee as well as an alias does
    # (``run(argv)`` carries no ``subprocess.`` for R2 to see), aliased or not.
    assert kinds('from subprocess import Popen, check_output, run, call, check_call\n') == ["imported-spawner"]
    assert kinds('from asyncio import create_subprocess_exec, create_subprocess_shell\n') == ["imported-spawner"]
    assert kinds('def f():\n    from subprocess import run\n    return run(["python"])\n') == ["imported-spawner"]
    assert kinds('from subprocess import run as r, Popen\n') == ["aliased-spawner", "imported-spawner"]
    assert kinds('from subprocess import PIPE, TimeoutExpired, CompletedProcess\n') == []
    assert kinds('from asyncio import sleep, gather\n') == []
    assert kinds('import subprocess as sp\n') == ["aliased-spawner"]
    assert kinds('import asyncio as aio\n') == ["aliased-spawner"]
    assert kinds('import subprocess\n_popen = subprocess.Popen\n') == ["aliased-spawner"]
    assert kinds('import subprocess\ndef f(popen=subprocess.Popen):\n    return popen\n') == ["aliased-spawner"]
    assert kinds('def f():\n    runner = run_bounded\n    return runner\n') == []  # an alias of the resolver is the resolver
    assert kinds('import subprocess\ndef f(p: subprocess.Popen[bytes], q: "subprocess.Popen"):\n    return isinstance(p, subprocess.Popen)\n') == []
    assert kinds('import subprocess\ndef f():\n    return subprocess.PIPE, subprocess.TimeoutExpired\n') == []


# -- R4: lookups whose result reaches an argv, every constant name -------------------


class _AnyName:
    """Membership test that accepts every string constant."""

    def __contains__(self, item: object) -> bool:
        return isinstance(item, str)


def _which_reaching_argv_any(source: str) -> list[tuple[str, str, int]]:
    """``_which_reaching_argv`` with the name filter lifted to any constant."""
    return _which_reaching_argv(source, _AnyName())


def _scan_which_any() -> dict[tuple[str, str, str], list[int]]:
    found: dict[tuple[str, str, str], list[int]] = {}
    for path in sorted(SRC_ROOT.rglob("*.py")):
        rel = path.relative_to(SRC_ROOT.parent).as_posix()
        for scope, tool, line in _which_reaching_argv_any(path.read_text(encoding="utf-8")):
            found.setdefault((rel, scope, tool), []).append(line)
    return found


#: Lookups whose result is spawned. The older scan polices nx/git/bd/claude with its
#: own allow list; this one covers every constant name, and the three it found
#: (mineru-api, java, ss) were converted to ``which_off_cwd``.
_WHICH_ANY_ALLOWED: frozenset[tuple[str, str, str]] = frozenset({
    ("nexus/commands/daemon.py", "_resolve_nx_bin", "nx"),
})


def test_no_lookup_of_any_tool_reaches_an_argv_outside_the_sanctioned_resolvers() -> None:
    found = _scan_which_any()
    stray = {k: v for k, v in found.items() if k not in _WHICH_ANY_ALLOWED}
    assert not stray, (
        "a shutil.which result is spawned; on Windows which() answers with the current "
        "directory's hit first. Use nexus.util.nx_argv.which_off_cwd: "
        + ", ".join(f"{p}:{lines} in {s} ({n!r})" for (p, s, n), lines in sorted(stray.items()))
    )
    assert set(_WHICH_ANY_ALLOWED) <= set(found), "the sanctioned lookup moved; the detector may be blind"


def test_the_any_name_which_detector_sees_a_name_the_narrow_one_ignores() -> None:
    src = 'import shutil\ndef f():\n    j = shutil.which("java")\n    return [j, "-jar"]\n'
    assert _which_reaching_argv(src) == []
    assert _which_reaching_argv_any(src) == [("f", "java", 4)]
    assert _which_reaching_argv_any('import shutil\ndef f():\n    return shutil.which("java") is None\n') == []
