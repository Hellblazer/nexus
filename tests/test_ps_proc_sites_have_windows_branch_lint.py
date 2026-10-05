# SPDX-License-Identifier: AGPL-3.0-or-later
"""A function that asks ``ps`` or ``/proc`` who a process is must carry a
Windows branch, or sit on a path that does (RDR-224, nexus-f9bgu.21).

Windows has neither. A new ``["ps", ...]`` argv or ``/proc`` read with no
Windows path is a silent "no processes found" there, the fail-open class the
identity work removes.

The scan is by AST, so quote style, line breaks and aliasing do not matter. A
SITE is any list or tuple literal whose first element is the string ``"ps"``,
any call whose first argument is a string, f-string or ``"/proc/" + ...``
concatenation starting ``/proc``, and any use of the module's ``PROCFS_ROOT``.

Granularity is the function, no longer the file: one ``"win32"`` anywhere in a
module used to excuse every ps and /proc site in it. A site function passes
when

* it has a Windows branch (a ``"win32"`` string, or a reference to
  ``winproc_core``), or
* it calls a function of the same module that has one (the shape
  ``_windows(platform)`` and ``_is_windows(platform)`` helpers give), or
* a function of the same module on a call path to it has one: the guard sits at
  the public entry (``all_process_rows``, ``process_command``) and the private
  readers beneath it are reached only through it.

A site at module level (a ``PS_COMMAND`` constant) needs the module to have a
Windows branch somewhere, because nothing is on a call path to it.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.lint

_SRC = Path(__file__).resolve().parents[1] / "src" / "nexus"
#: Modules that read the process table only as pure parsers or messages.
_EXEMPT: frozenset[str] = frozenset()

_MODULE = "<module>"


def _starts_with_proc(node: ast.expr) -> bool:
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str) and node.value.startswith("/proc")
    if isinstance(node, ast.JoinedStr):
        first = node.values[0] if node.values else None
        return isinstance(first, ast.Constant) and _starts_with_proc(first)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _starts_with_proc(node.left)
    return False


def _is_site(node: ast.AST) -> bool:
    if isinstance(node, (ast.List, ast.Tuple)):
        first = node.elts[0] if node.elts else None
        return isinstance(first, ast.Constant) and first.value == "ps"
    if isinstance(node, ast.Call):
        return bool(node.args) and _starts_with_proc(node.args[0])
    if isinstance(node, ast.Name):
        return node.id == "PROCFS_ROOT" and isinstance(node.ctx, ast.Load)
    return False


def _has_windows_marker(node: ast.AST) -> bool:
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and sub.value == "win32":
            return True
        if isinstance(sub, ast.Name) and sub.id == "winproc_core":
            return True
        if isinstance(sub, ast.Attribute) and sub.attr == "winproc_core":
            return True
        if isinstance(sub, ast.ImportFrom) and any(a.name == "winproc_core" for a in sub.names):
            return True
    return False


def _called_names(node: ast.AST) -> set[str]:
    names: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            if isinstance(sub.func, ast.Name):
                names.add(sub.func.id)
            elif isinstance(sub.func, ast.Attribute):
                names.add(sub.func.attr)
    return names


def unguarded_sites(source: str) -> list[tuple[str, int]]:
    """``(function, line)`` of every ps or /proc site in *source* with no Windows
    branch on its own function or on a call path to it."""
    tree = ast.parse(source)
    funcs: dict[str, list[ast.AST]] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            funcs.setdefault(node.name, []).append(node)

    has_branch = {name for name, defs in funcs.items() if any(_has_windows_marker(d) for d in defs)}
    calls = {name: set().union(*(_called_names(d) for d in defs)) for name, defs in funcs.items()}
    # A function is guarded when it, something it calls, or any in-module
    # function on a call path to it has a Windows branch.
    guarded = set(has_branch) | {n for n, c in calls.items() if c & has_branch}
    # ...and a guarded function guards everything in the module it calls.
    frontier = list(guarded)
    while frontier:
        for callee in calls.get(frontier.pop(), ()):
            if callee in funcs and callee not in guarded:
                guarded.add(callee)
                frontier.append(callee)

    module_has_branch = _has_windows_marker(tree)
    out: list[tuple[str, int]] = []

    def visit(node: ast.AST, scope: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                visit(child, child.name)
                continue
            if _is_site(child):
                ok = module_has_branch if scope == _MODULE else scope in guarded
                if not ok:
                    out.append((scope, child.lineno))
            visit(child, scope)

    visit(tree, _MODULE)
    return out


def _sites() -> dict[str, list[tuple[str, int]]]:
    """Every module with a ps or /proc site, mapped to its UNGUARDED sites."""
    found: dict[str, list[tuple[str, int]]] = {}
    for path in sorted(_SRC.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text)
        if any(_is_site(n) for n in ast.walk(tree)):
            found[str(path.relative_to(_SRC))] = unguarded_sites(text)
    return found


def test_every_process_table_reader_has_a_windows_branch() -> None:
    offenders = {
        rel: sites for rel, sites in _sites().items() if rel not in _EXEMPT and sites
    }
    assert offenders == {}, (
        "these functions read ps or /proc with no Windows branch on them or on a "
        "call path to them (route through nexus._install.winproc_core): "
        + "; ".join(f"{rel}: {sites}" for rel, sites in offenders.items())
    )


def test_the_scan_sees_the_modules_it_guards() -> None:
    """Non-vacuity: a scan that found nothing would pass the check above."""
    found = set(_sites())
    for expected in (
        "session.py",
        "daemon/service_registry.py",
        "daemon/aspect_worker_daemon.py",
        "commands/doctor.py",
        "_install/census_core.py",
    ):
        assert expected in found, (expected, sorted(found))
    # Function granularity: the scan must also count SITES, not only files.
    total = sum(
        sum(1 for n in ast.walk(ast.parse(p.read_text(encoding="utf-8"))) if _is_site(n))
        for p in _SRC.rglob("*.py")
    )
    assert total >= 15, f"the scan found only {total} ps//proc sites; its patterns drifted"


# ── planted violations: the lint must be able to fail ────────────────────────────


def _planted(body: str) -> list[tuple[str, int]]:
    return unguarded_sites(body)


def test_a_double_quoted_ps_argv_with_no_branch_is_caught() -> None:
    assert _planted('import subprocess\ndef f():\n    subprocess.run(["ps", "-ef"])\n') == [("f", 3)]


def test_a_single_quoted_ps_argv_is_caught() -> None:
    assert _planted("import subprocess\ndef f():\n    subprocess.run(['ps', '-ef'])\n") == [("f", 3)]


def test_a_multiline_tuple_ps_argv_is_caught() -> None:
    src = 'def f():\n    cmd = (\n        "ps",\n        "-ef",\n    )\n    return cmd\n'
    assert _planted(src) == [("f", 2)]


def test_open_of_a_proc_path_is_caught() -> None:
    assert _planted('def f(pid):\n    return open("/proc/1/stat").read()\n') == [("f", 2)]


def test_open_of_an_fstring_proc_path_is_caught() -> None:
    assert _planted('def f(pid):\n    return open(f"/proc/{pid}/stat").read()\n') == [("f", 2)]


def test_a_concatenated_proc_path_is_caught() -> None:
    assert _planted('def f(pid):\n    return open("/proc/" + str(pid)).read()\n') == [("f", 2)]


def test_path_read_of_proc_is_caught() -> None:
    src = 'from pathlib import Path\ndef f():\n    return Path("/proc/uptime").read_text()\n'
    assert _planted(src) == [("f", 3)]


def test_a_windows_branch_in_ANOTHER_function_no_longer_excuses_the_site() -> None:
    """The old file-granularity rule passed this: ``g`` has the branch, the
    function holding the ps call has none and is on no path from ``g``."""
    src = (
        "import subprocess, sys\n"
        "def g():\n"
        '    return sys.platform == "win32"\n'
        "def f():\n"
        '    subprocess.run(["ps", "-ef"])\n'
    )
    assert _planted(src) == [("f", 5)]


def test_a_site_function_with_its_own_branch_passes() -> None:
    src = (
        "import subprocess, sys\n"
        "def f():\n"
        '    if sys.platform == "win32":\n'
        "        return []\n"
        '    return subprocess.run(["ps", "-ef"])\n'
    )
    assert _planted(src) == []


def test_a_site_function_that_calls_a_branching_helper_passes() -> None:
    src = (
        "import subprocess, sys\n"
        "def _windows():\n"
        '    return sys.platform == "win32"\n'
        "def f():\n"
        "    if _windows():\n"
        "        return []\n"
        '    return subprocess.run(["ps", "-ef"])\n'
    )
    assert _planted(src) == []


def test_a_private_reader_under_a_guarded_entry_passes() -> None:
    src = (
        "import subprocess, sys\n"
        "def entry():\n"
        '    if sys.platform == "win32":\n'
        "        return []\n"
        "    return _reader()\n"
        "def _reader():\n"
        '    return subprocess.run(["ps", "-ef"])\n'
    )
    assert _planted(src) == []


def test_a_private_reader_reached_only_from_an_unguarded_entry_is_caught() -> None:
    src = (
        "import subprocess\n"
        "def entry():\n"
        "    return _reader()\n"
        "def _reader():\n"
        '    return subprocess.run(["ps", "-ef"])\n'
    )
    assert _planted(src) == [("_reader", 5)]


def test_the_winproc_core_reference_counts_as_a_branch() -> None:
    src = (
        "import subprocess\n"
        "from nexus._install import winproc_core\n"
        "def f():\n"
        "    rows = winproc_core.enumerate_processes()\n"
        '    return rows or subprocess.run(["ps", "-ef"])\n'
    )
    assert _planted(src) == []


def test_a_module_level_ps_constant_needs_a_branch_somewhere_in_the_module() -> None:
    assert _planted('PS = ("ps", "axww")\n') == [("<module>", 1)]
    assert _planted('import sys\nPS = ("ps", "axww")\ndef f():\n    return sys.platform == "win32"\n') == []


def test_use_of_procfs_root_is_a_site() -> None:
    src = 'PROCFS_ROOT = None\ndef f():\n    return (PROCFS_ROOT / "uptime").read_text()\n'
    assert _planted(src) == [("f", 3)]
